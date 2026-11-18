#!/usr/bin/env python3
"""端到端冒烟测试:对运行中的 source + collector 验证按 UTC 小时隔离等关键行为。

用法:
    python3 scripts/smoke.py                 # 默认打 http://localhost:8000 / 8001
    COLLECTOR_URL=... SOURCE_URL=... python3 scripts/smoke.py

每个小时在源站都有独立的 v1 全量、修订序列、当前版指针和分页故障;测试默认
自动挑选两个采集端从未采集过的小时,因此对同一环境可反复运行。
也可用 SMOKE_HOUR=2026-11-18T10 指定(第二个小时为其后一小时)。
"""
from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

COLLECTOR = os.environ.get("COLLECTOR_URL", "http://localhost:8000").rstrip("/")
SOURCE = os.environ.get("SOURCE_URL", "http://localhost:8001").rstrip("/")

_checks = 0


def req(method: str, base: str, path: str, body=None, expect: int | None = 200):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            code, payload = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    if expect is not None and code != expect:
        raise AssertionError(f"{method} {url} -> {code}, want {expect}: {payload[:400]!r}")
    return code, (json.loads(payload) if payload else {})


def check(name: str, cond: bool, extra: str = "") -> None:
    global _checks
    if not cond:
        raise AssertionError(f"CHECK FAILED: {name} {extra}")
    _checks += 1
    print(f"  ok [{_checks:02d}] {name}")


def stations(base: str, hour: str, version: int | None = None) -> dict:
    q = f"?version={version}" if version is not None else ""
    _, data = req("GET", base, f"/hours/{hour}/data{q}")
    return {s["station_id"]: s["pm25"] for s in data["stations"]}


def _plus_hour(hour: str, delta: int = 1) -> str:
    dt = datetime.strptime(hour, "%Y-%m-%dT%H")
    return (dt + timedelta(hours=delta)).strftime("%Y-%m-%dT%H")


def _unused(hour: str) -> bool:
    code, _ = req("GET", COLLECTOR, f"/collections/{hour}", expect=None)
    return code == 404


def pick_hours() -> tuple[str, str]:
    env = os.environ.get("SMOKE_HOUR")
    if env:
        return env, _plus_hour(env, 1)
    rnd = random.Random()
    for _ in range(500):
        dt = datetime(2026, 1, 1) + timedelta(
            days=rnd.randrange(0, 900), hours=rnd.randrange(0, 24)
        )
        a = dt.strftime("%Y-%m-%dT%H")
        b = _plus_hour(a, 1)
        if _unused(a) and _unused(b):
            return a, b
    raise AssertionError("could not find two unused hours; set SMOKE_HOUR")


def reset_hour(hour: str, station_count: int = 20, seed: int = 20261118):
    _, r = req("POST", SOURCE, "/admin/reset",
               {"hour": hour, "station_count": station_count, "seed": seed})
    check(f"{hour} 源站重置为 v1 全量", r["current_version"] == 1 and r["station_count"] == 20,
          json.dumps(r))


def collect(hour: str):
    _, r = req("POST", COLLECTOR, "/collections", {"hour": hour})
    return r


def main() -> int:
    hour_a, hour_b = pick_hours()
    print(f"== smoke: collector={COLLECTOR} source={SOURCE} "
          f"hour_a={hour_a} hour_b={hour_b}")

    print("-- 0. 健康检查")
    req("GET", SOURCE, "/healthz")
    req("GET", COLLECTOR, "/healthz")

    print("-- 1. 两个小时各自首版全量,互不相同")
    reset_hour(hour_a)
    reset_hour(hour_b)
    # 源站接口必须指定小时
    req("GET", SOURCE, "/api/revisions/current", expect=422)
    req("GET", SOURCE, "/api/revisions/1/pages/1", expect=422)
    _, cur_b = req("GET", SOURCE, f"/api/revisions/current?hour={hour_b}")
    check("源站 11 点当前版=1", cur_b["hour"] == hour_b and cur_b["current_version"] == 1,
          json.dumps(cur_b))

    r = collect(hour_a)
    check("10 点采集首版 ok", r["hour"] == hour_a and r["status"] == "ok"
          and r["confirmed_to"] == 1, json.dumps(r))
    v1_a = stations(COLLECTOR, hour_a, 1)
    check("10 点 v1 全量 20 站", len(v1_a) == 20)

    r = collect(hour_b)
    check("11 点采集首版 ok", r["hour"] == hour_b and r["status"] == "ok"
          and r["confirmed_to"] == 1, json.dumps(r))
    v1_b = stations(COLLECTOR, hour_b, 1)
    check("11 点 v1 全量 20 站", len(v1_b) == 20)
    check("两个小时首版数据彼此独立", v1_a != v1_b)
    check("11 点当前版仍为 1,不受 10 点影响", stations(COLLECTOR, hour_b) == v1_b)
    req("GET", COLLECTOR, f"/hours/{hour_a}/data?version=2", expect=404)

    print("-- 2. 10 点发布 v2/v3 并逐版补齐,11 点完全不受影响")
    _, p2 = req("POST", SOURCE, "/admin/publish", {"hour": hour_a})
    _, p3 = req("POST", SOURCE, "/admin/publish", {"hour": hour_a})
    check("源站 10 点版本推进到 3", p3["version"] == 3, json.dumps(p3))
    _, cur_b = req("GET", SOURCE, f"/api/revisions/current?hour={hour_b}")
    check("源站 11 点版本仍为 1", cur_b["current_version"] == 1, json.dumps(cur_b))

    deleted_v2 = [o["station_id"] for o in p2["ops"] if o["op"] == "delete"]
    r = collect(hour_a)
    check("10 点补齐到 v3", r["status"] == "ok" and r["confirmed_from"] == 1
          and r["confirmed_to"] == 3, json.dumps(r))
    v2_a, v3_a = stations(COLLECTOR, hour_a, 2), stations(COLLECTOR, hour_a, 3)
    check("10 点 v2 删除了目标站", bool(deleted_v2) and all(s not in v2_a for s in deleted_v2))
    check("10 点 v1 仍含被删站", all(s in v1_a for s in deleted_v2))
    check("10 点 v3 含新站 N03", "N03" in v3_a and "N03" not in v1_a)
    check("10 点历史版未被污染", stations(COLLECTOR, hour_a, 1) == v1_a)

    r = collect(hour_b)
    check("11 点重复采集 up_to_date", r["status"] == "up_to_date"
          and r["current_version"] == 1, json.dumps(r))
    check("11 点 v1 未被 10 点修订污染", stations(COLLECTOR, hour_b, 1) == v1_b)

    print("-- 3. 11 点缺页只影响 11 点,10 点仍可继续采集")
    req("PUT", SOURCE, "/admin/faults",
        {"hour": hour_b, "faults": [{"version": 2, "page": 1, "kind": "missing", "times": -1}]})
    req("POST", SOURCE, "/admin/publish", {"hour": hour_b})
    r = collect(hour_b)
    check("11 点 v2 缺页失败", r["status"] == "failed" and "missing page" in (r["error"] or ""),
          json.dumps(r))
    check("11 点指针保留在 v1", r["current_version"] == 1, json.dumps(r))
    req("GET", COLLECTOR, f"/hours/{hour_b}/data?version=2", expect=404)
    check("11 点 v2 不可查", True)

    req("POST", SOURCE, "/admin/publish", {"hour": hour_a})
    r = collect(hour_a)
    check("10 点不受 11 点故障影响,补齐 v4", r["status"] == "ok"
          and r["confirmed_to"] == 4, json.dumps(r))

    req("DELETE", SOURCE, f"/admin/faults?hour={hour_b}")
    _, faults_a = req("GET", SOURCE, f"/admin/faults?hour={hour_a}")
    check("清除 11 点故障后 10 点故障列表仍为空", faults_a["faults"] == [])
    r = collect(hour_b)
    check("清障后 11 点补齐 v2", r["status"] == "ok" and r["confirmed_to"] == 2,
          json.dumps(r))

    print("-- 4. 同版同站冲突:保留该小时原供数版")
    req("PUT", SOURCE, "/admin/faults",
        {"hour": hour_b, "faults": [{"version": 3, "page": 1, "kind": "conflict", "times": -1}]})
    req("POST", SOURCE, "/admin/publish", {"hour": hour_b})
    r = collect(hour_b)
    check("11 点 v3 冲突失败", r["status"] == "failed"
          and "conflict" in (r["error"] or "").lower(), json.dumps(r))
    check("11 点指针保留在 v2", r["current_version"] == 2, json.dumps(r))
    req("DELETE", SOURCE, f"/admin/faults?hour={hour_b}")
    r = collect(hour_b)
    check("清障后 11 点补齐 v3", r["status"] == "ok" and r["confirmed_to"] == 3,
          json.dumps(r))

    print("-- 5. 采集 10 点期间发布 11 点修订:不改变本次采集目标")
    # 给 10 点 v5 第 1 页注入一次性超时(源站挂起 FAULT_SLEEP,采集端超时后重试)。
    req("PUT", SOURCE, "/admin/faults",
        {"hour": hour_a, "faults": [{"version": 5, "page": 1, "kind": "timeout", "times": 1}]})
    req("POST", SOURCE, "/admin/publish", {"hour": hour_a})  # 10 点 v5
    result: dict = {}

    def _run_a():
        result.update(collect(hour_a))

    t = threading.Thread(target=_run_a)
    t.start()
    time.sleep(0.8)  # 让采集端先固定目标版并开始抓页
    _, pb = req("POST", SOURCE, "/admin/publish", {"hour": hour_b})  # 11 点 v4
    _, pa = req("POST", SOURCE, "/admin/publish", {"hour": hour_a})  # 10 点 v6
    t.join(timeout=60)
    check("采集线程结束", not t.is_alive())
    check("10 点本次目标仍固定为 v5", result.get("pinned_version") == 5
          and result.get("confirmed_to") == 5 and result.get("status") == "ok",
          json.dumps(result))
    check("10 点 v5 第 2 次尝试成功", result.get("attempts", {}).get("5") == 2,
          json.dumps(result.get("attempts")))
    req("GET", COLLECTOR, f"/hours/{hour_a}/data?version=6", expect=404)
    check("采集期间发布的 10 点 v6 未被本次确认", True)
    _, cur_b = req("GET", SOURCE, f"/api/revisions/current?hour={hour_b}")
    check("11 点源站已到 v4", cur_b["current_version"] == 4, json.dumps(cur_b))
    check("11 点采集端仍停在 v3(发布不触发采集)",
          stations(COLLECTOR, hour_b) == stations(COLLECTOR, hour_b, 3))

    r = collect(hour_a)
    check("10 点下次采集补齐 v6", r["status"] == "ok" and r["confirmed_to"] == 6,
          json.dumps(r))
    r = collect(hour_b)
    check("11 点补齐到自己的 v4", r["status"] == "ok" and r["pinned_version"] == 4
          and r["confirmed_to"] == 4, json.dumps(r))
    check("11 点 v4 含新站 N04", "N04" in stations(COLLECTOR, hour_b, 4))
    check("11 点不含 10 点的新站 N03", "N03" not in stations(COLLECTOR, hour_b, 1))

    print("-- 6. 已确认版完整性:逐小时、逐版快照与版本登记一致")
    for hour, top in ((hour_a, 6), (hour_b, 4)):
        _, vers = req("GET", COLLECTOR, f"/hours/{hour}/versions")
        check(f"{hour} 确认版 1..{top} 连续",
              [v["version"] for v in vers["versions"]] == list(range(1, top + 1)))
        for v in vers["versions"]:
            snap = stations(COLLECTOR, hour, v["version"])
            check(f"{hour} v{v['version']} 快照站数={v['station_count']}",
                  len(snap) == v["station_count"])
    check("10 点 v3 含 N03、11 点 v4 含 N04(版本序列互不串号)",
          "N03" in stations(COLLECTOR, hour_a, 3)
          and "N04" in stations(COLLECTOR, hour_b, 4))
    check("11 点不含只在 10 点出现的新站 N06", "N06" not in stations(COLLECTOR, hour_b, 4))
    check("同一版号在两小时数据不同",
          stations(COLLECTOR, hour_a, 2) != stations(COLLECTOR, hour_b, 2))

    print("-- 7. 单小时源站回退保护,另一小时不受影响")
    req("POST", SOURCE, "/admin/reset", {"hour": hour_a, "station_count": 20, "seed": 1})
    _, cur_b = req("GET", SOURCE, f"/api/revisions/current?hour={hour_b}")
    check("重置 10 点后源站 11 点版本仍为 4", cur_b["current_version"] == 4, json.dumps(cur_b))
    r = collect(hour_a)
    check("10 点回退被拒绝且保留 v6", r["status"] == "source_regressed"
          and r["current_version"] == 6, json.dumps(r))
    check("回退后 10 点历史版仍可查", len(stations(COLLECTOR, hour_a, 6)) > 0)
    r = collect(hour_b)
    check("11 点不受 10 点回退影响", r["status"] == "up_to_date"
          and r["current_version"] == 4, json.dumps(r))

    print("-- 8. /hours 同时列出两个小时各自的当前版")
    _, hours = req("GET", COLLECTOR, "/hours")
    by_hour = {h["hour"]: h for h in hours["hours"]}
    check("两小时均出现在 /hours", hour_a in by_hour and hour_b in by_hour)
    check("各自当前版正确", by_hour[hour_a]["current_version"] == 6
          and by_hour[hour_b]["current_version"] == 4)

    print(f"== smoke 全部通过 ({_checks} 项检查)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
