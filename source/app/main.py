"""模拟源站:市环境监测中心 PM2.5 修订发布服务。

按 UTC 小时隔离:
- 每个小时有独立的 v1 全量快照、后续修订序列(各自从 v1 递增)、当前版指针
  与分页故障;发布/故障注入/读取都必须指定 hour。
- 不同小时之间互不影响:10 点发布修订不会改变 11 点的版本、数据或故障。
- 每版分页发布,响应携带 hour、连续版号 version、页号 page、总页数 total_pages。
- 管理接口支持:按小时发布新修订、重置、注入缺页/超时/同版同站冲突故障。
"""
from __future__ import annotations

import os
import random
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "8"))
FAULT_SLEEP = float(os.environ.get("FAULT_SLEEP_SECONDS", "10"))
DEFAULT_STATIONS = int(os.environ.get("STATION_COUNT", "20"))
DEFAULT_SEED = int(os.environ.get("SEED", "20261118"))

HOUR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")

LOCK = threading.Lock()

# ---------------- 内部状态(仅源站可见的"真实值"与已发布修订) ----------------
STATE = {
    "seed": DEFAULT_SEED,          # 新建小时的默认种子
    "station_count": DEFAULT_STATIONS,
    "hours": {},                   # hour -> 该小时的独立状态
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_hour(hour: str) -> str:
    hour = (hour or "").strip()
    if not HOUR_RE.match(hour):
        raise HTTPException(400, "hour must be a UTC hour like '2026-11-18T10'")
    try:
        datetime.strptime(hour, "%Y-%m-%dT%H")
    except ValueError:
        raise HTTPException(400, f"invalid UTC hour: {hour}")
    return hour


def _paginate(items: list[dict]) -> tuple[int, dict[int, list[dict]]]:
    total = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    return total, {p: items[(p - 1) * PAGE_SIZE: p * PAGE_SIZE] for p in range(1, total + 1)}


def _build_hour(hour: str, station_count: int, seed: int) -> dict:
    """为某个 UTC 小时创建独立的 v1 全量快照。

    种子混入 hour,因此不同小时的首版数据彼此独立,且同一输入可复现。
    """
    rng = random.Random(f"{seed}:{hour}")
    truth = {f"S{i:02d}": round(rng.uniform(5, 150), 1)
             for i in range(1, station_count + 1)}
    items = [{"station_id": sid, "op": "upsert", "pm25": pm}
             for sid, pm in sorted(truth.items())]
    total, pages = _paginate(items)
    return {
        "hour": hour,
        "seed": seed,
        "station_count": station_count,
        "rng": rng,
        "truth": truth,
        "versions": {
            1: {"version": 1, "items": items, "pages": pages,
                "total_pages": total, "created_at": _now()}
        },
        "current": 1,
        "faults": [],
    }


def _ensure_hour(hour: str) -> dict:
    """取出某小时状态;首次访问时惰性创建 v1(不触碰其他小时)。"""
    hs = STATE["hours"].get(hour)
    if hs is None:
        hs = _build_hour(hour, STATE["station_count"], STATE["seed"])
        STATE["hours"][hour] = hs
    return hs


def _auto_delta(hs: dict) -> list[dict]:
    """确定性地生成该小时下一条增量:3 个改值,偶数版删 1 站,并新增 1 个站。"""
    version = hs["current"] + 1
    rng = hs["rng"]
    truth = hs["truth"]
    ids = sorted(truth)
    ops: list[dict] = []
    chosen = rng.sample(ids, k=min(3, len(ids))) if ids else []
    for sid in chosen:
        ops.append({"station_id": sid, "op": "upsert", "pm25": round(rng.uniform(5, 150), 1)})
    if version % 2 == 0:
        remaining = [s for s in ids if s not in chosen]
        if len(remaining) > 1:
            ops.append({"station_id": rng.choice(remaining), "op": "delete"})
    new_id = f"N{version:02d}"
    if new_id not in truth:
        ops.append({"station_id": new_id, "op": "upsert", "pm25": round(rng.uniform(5, 150), 1)})
    return ops


def _apply_ops(hs: dict, ops: list[dict]) -> None:
    truth = hs["truth"]
    for it in ops:
        if it["op"] == "upsert":
            truth[it["station_id"]] = it["pm25"]
        else:
            truth.pop(it["station_id"], None)


def _consume_fault(hs: dict, version: int, page: int) -> Optional[str]:
    for f in list(hs["faults"]):
        if f["version"] == version and f["page"] == page and f["times"] != 0:
            if f["times"] > 0:
                f["times"] -= 1
                if f["times"] == 0:
                    hs["faults"].remove(f)
            return f["kind"]
    return None


# ---------------- 请求模型 ----------------
class Op(BaseModel):
    station_id: str = Field(min_length=1)
    op: Literal["upsert", "delete"]
    pm25: Optional[float] = Field(default=None, ge=0)


class PublishRequest(BaseModel):
    hour: str = Field(description="UTC 小时,如 2026-11-18T10;发布只影响该小时")
    ops: Optional[list[Op]] = None  # 缺省则自动生成一条确定性增量


class ResetRequest(BaseModel):
    hour: Optional[str] = Field(default=None, description="缺省重置全部小时")
    station_count: int = Field(default=DEFAULT_STATIONS, ge=1, le=500)
    seed: int = DEFAULT_SEED


class Fault(BaseModel):
    version: int
    page: int = Field(default=1, ge=1)
    kind: Literal["missing", "timeout", "conflict"]
    times: int = 1  # 触发次数;-1 表示一直生效


class FaultsRequest(BaseModel):
    hour: str = Field(description="UTC 小时,如 2026-11-18T10;故障只作用于该小时")
    faults: list[Fault]


def _validate_explicit(ops_in: list[Op]) -> list[dict]:
    seen: dict[str, tuple] = {}
    ops: list[dict] = []
    for o in ops_in:
        if o.op == "upsert" and o.pm25 is None:
            raise HTTPException(400, f"upsert for {o.station_id} requires pm25")
        if o.op == "delete" and o.pm25 is not None:
            raise HTTPException(400, f"delete for {o.station_id} must not carry pm25")
        key = (o.op, o.pm25)
        if o.station_id in seen:
            if seen[o.station_id] != key:
                raise HTTPException(400, f"conflicting ops for station {o.station_id}")
            continue
        seen[o.station_id] = key
        item = {"station_id": o.station_id, "op": o.op}
        if o.op == "upsert":
            item["pm25"] = o.pm25
        ops.append(item)
    return ops


def _hour_state(hs: dict) -> dict:
    return {
        "hour": hs["hour"],
        "current_version": hs["current"],
        "station_count": len(hs["truth"]),
        "seed": hs["seed"],
        "versions": [
            {"version": v, "total_pages": ver["total_pages"], "item_count": len(ver["items"])}
            for v, ver in sorted(hs["versions"].items())
        ],
        "faults": [dict(f) for f in hs["faults"]],
    }


# ---------------- 应用 ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    with LOCK:
        STATE["seed"] = DEFAULT_SEED
        STATE["station_count"] = DEFAULT_STATIONS
        STATE["hours"] = {}
    yield


app = FastAPI(title="PM2.5 Mock Source", lifespan=lifespan)


@app.get("/")
def index():
    return {"service": "pm25-mock-source", "docs": "/docs"}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ---------------- 数据接口(采集方使用,均按 hour 隔离) ----------------
@app.get("/api/revisions/current")
def current_revision(hour: str = Query(..., description="UTC 小时,如 2026-11-18T10")):
    hour = _check_hour(hour)
    with LOCK:
        hs = _ensure_hour(hour)
        return {"hour": hour, "current_version": hs["current"]}


@app.get("/api/revisions")
def list_revisions(hour: str = Query(..., description="UTC 小时,如 2026-11-18T10")):
    hour = _check_hour(hour)
    with LOCK:
        hs = _ensure_hour(hour)
        return {
            "hour": hour,
            "current_version": hs["current"],
            "versions": [
                {
                    "version": v,
                    "total_pages": ver["total_pages"],
                    "item_count": len(ver["items"]),
                    "created_at": ver["created_at"],
                }
                for v, ver in sorted(hs["versions"].items())
            ],
        }


@app.get("/api/revisions/{version}/pages/{page}")
def get_page(
    version: int,
    page: int,
    hour: str = Query(..., description="UTC 小时,如 2026-11-18T10"),
):
    hour = _check_hour(hour)
    with LOCK:
        hs = _ensure_hour(hour)
        ver = hs["versions"].get(version)
        if ver is None:
            raise HTTPException(404, f"version {version} of hour {hour} not found")
        if page < 1 or page > ver["total_pages"]:
            raise HTTPException(404, f"page {page} of version {version} hour {hour} not found")
        items = [dict(i) for i in ver["pages"][page]]
        total = ver["total_pages"]
        first_item = dict(ver["items"][0]) if ver["items"] else None
        fault = _consume_fault(hs, version, page)
    if fault == "missing":
        raise HTTPException(404, f"page {page} of version {version} hour {hour} not found (injected)")
    if fault == "timeout":
        # 故意挂起:不持锁,其他小时/其他请求不受影响
        time.sleep(FAULT_SLEEP)
    if fault == "conflict" and first_item is not None:
        # 同版同站冲突:追加一条与本版首条同站但取值不同的记录
        items.append(
            {
                "station_id": first_item["station_id"],
                "op": "upsert",
                "pm25": round((first_item.get("pm25") or 0) + 50.0, 1),
            }
        )
    return {"hour": hour, "version": version, "page": page,
            "total_pages": total, "items": items}


# ---------------- 管理接口(按小时切换修订 / 故障注入) ----------------
@app.post("/admin/reset")
def admin_reset(req: ResetRequest = ResetRequest()):
    with LOCK:
        if req.hour is not None:
            hour = _check_hour(req.hour)
            STATE["hours"][hour] = _build_hour(hour, req.station_count, req.seed)
            return {
                "hour": hour,
                "current_version": 1,
                "station_count": len(STATE["hours"][hour]["truth"]),
                "seed": req.seed,
            }
        # 重置全部小时:丢弃已有小时状态,之后的首次访问按新默认值惰性创建 v1
        STATE["seed"] = req.seed
        STATE["station_count"] = req.station_count
        STATE["hours"] = {}
        return {"reset": "all", "hours": [], "station_count": req.station_count, "seed": req.seed}


@app.post("/admin/publish")
def admin_publish(req: PublishRequest):
    hour = _check_hour(req.hour)
    with LOCK:
        hs = _ensure_hour(hour)
        version = hs["current"] + 1
        ops = _auto_delta(hs) if req.ops is None else _validate_explicit(req.ops)
        _apply_ops(hs, ops)
        total, pages = _paginate(ops)
        hs["versions"][version] = {
            "version": version,
            "items": ops,
            "pages": pages,
            "total_pages": total,
            "created_at": _now(),
        }
        hs["current"] = version
        return {"hour": hour, "version": version, "total_pages": total,
                "item_count": len(ops), "ops": ops}


@app.get("/admin/faults")
def get_faults(hour: Optional[str] = Query(default=None)):
    with LOCK:
        if hour is not None:
            h = _check_hour(hour)
            hs = STATE["hours"].get(h)
            return {"hour": h, "faults": [dict(f) for f in (hs["faults"] if hs else [])]}
        return {
            "faults": [
                {"hour": h, **f}
                for h, hs in sorted(STATE["hours"].items())
                for f in (dict(x) for x in hs["faults"])
            ]
        }


@app.put("/admin/faults")
def put_faults(req: FaultsRequest):
    hour = _check_hour(req.hour)
    with LOCK:
        hs = _ensure_hour(hour)
        hs["faults"] = [f.model_dump() for f in req.faults]
        return {"hour": hour, "faults": [dict(f) for f in hs["faults"]]}


@app.delete("/admin/faults")
def delete_faults(hour: Optional[str] = Query(default=None)):
    with LOCK:
        if hour is not None:
            h = _check_hour(hour)
            hs = STATE["hours"].get(h)
            if hs is not None:
                hs["faults"] = []
            return {"hour": h, "faults": []}
        for hs in STATE["hours"].values():
            hs["faults"] = []
        return {"faults": []}


@app.get("/admin/state")
def admin_state(hour: Optional[str] = Query(default=None)):
    with LOCK:
        if hour is not None:
            h = _check_hour(hour)
            return _hour_state(_ensure_hour(h))
        return {
            "seed": STATE["seed"],
            "station_count": STATE["station_count"],
            "hours": [_hour_state(hs) for _, hs in sorted(STATE["hours"].items())],
        }
