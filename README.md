# PM2.5 修订采集与查询平台

市环境监测中心会补正各站点的小时 PM2.5,分析作业需按原版复算。本平台包含:

- **source**(模拟源站,FastAPI):**按 UTC 小时隔离**的数据源。每个小时有自己独立的
  首版(v1)全量快照、修订序列(各自从 v1 递增)、当前版指针和分页故障。首版含全量站点,
  此后每版只列新增、改值(`upsert`)和删除(`delete`),响应带 `hour`、连续版号 `version`、
  页号 `page`、总页数 `total_pages`。管理接口可**按小时**发布/重置修订、注入缺页、超时、
  同版同站冲突故障。
- **collector**(采集与查询 API,FastAPI + SQLite):采集某 UTC 小时前先**固定该小时**
  在源站的当前版,再逐版补齐;每个确认版的完整快照均可查询;数据与当前版指针在同一事务
  生效。各小时的已确认记录彼此独立。

## 快速开始

```bash
docker compose up --build
```

- 采集/查询 API: http://localhost:8000 (文档 `/docs`)
- 模拟源站: http://localhost:8001 (文档 `/docs`)

端到端冒烟(自动挑两个从未采集过的小时,40+ 项断言,覆盖小时隔离/缺页/超时/冲突/回退):

```bash
python3 scripts/smoke.py
# 也可固定小时: SMOKE_HOUR=2026-11-18T10 python3 scripts/smoke.py
```

## 小时隔离约定

源站所有数据与管理接口都必须指定 `hour`(UTC,形如 `2026-11-18T10`);不指定会返回 422。

- 每个小时的 v1 全量由 `(seed, hour)` 共同决定,因此不同小时数据不同且可复现。
- 版本号在小时内独立递增:10 点的 v3 与 11 点的 v3 是两套完全不同的修订。
- 发布、故障注入、故障清除都只作用于指定小时;10 点的修订/缺页不会影响 11 点。
- 采集端固定版本(`GET /api/revisions/current?hour=…`)与逐页抓取
  (`GET /api/revisions/{v}/pages/{p}?hour=…`)都只读目标小时。采集开始后源站即使发布了
  同一小时的新修订、或其他小时的修订,都不会改变本次采集的目标版。

## 典型流程(两个小时,互不干扰)

```bash
# 1. 采集两个小时的各自首版(全量)
curl -X POST localhost:8000/collections -H 'Content-Type: application/json' \
     -d '{"hour": "2026-11-18T10"}'
curl -X POST localhost:8000/collections -H 'Content-Type: application/json' \
     -d '{"hour": "2026-11-18T11"}'

# 2. 只给 10 点发布两条修订
curl -X POST localhost:8001/admin/publish -H 'Content-Type: application/json' \
     -d '{"hour": "2026-11-18T10"}'
curl -X POST localhost:8001/admin/publish -H 'Content-Type: application/json' \
     -d '{"hour": "2026-11-18T10"}'

# 3. 再采集 10 点:逐版补齐到 10 点自己的当前版;11 点仍是 v1
curl -X POST localhost:8000/collections -H 'Content-Type: application/json' \
     -d '{"hour": "2026-11-18T10"}'

# 4. 查询:各自当前版(默认)或任一已确认版当时的完整数据
curl localhost:8000/hours/2026-11-18T10/data
curl 'localhost:8000/hours/2026-11-18T11/data?version=1'
curl 'localhost:8000/hours/2026-11-18T10/data?version=3&station_id=S07'
curl localhost:8000/hours/2026-11-18T10/versions
curl localhost:8000/hours/2026-11-18T11/versions
```

## 故障注入演示(按小时;缺页 / 超时 / 同版同站冲突)

```bash
# 只给 11 点下一版第 1 页注入持续缺页
curl -X PUT localhost:8001/admin/faults -H 'Content-Type: application/json' -d \
  '{"hour":"2026-11-18T11","faults":[{"version":2,"page":1,"kind":"missing","times":-1}]}'

# 一次性超时:重试可成功,且不混入上次残页
curl -X PUT localhost:8001/admin/faults -H 'Content-Type: application/json' -d \
  '{"hour":"2026-11-18T10","faults":[{"version":2,"page":1,"kind":"timeout","times":1}]}'

# kind 还可取 conflict(同版同站不同值);times=-1 表示持续生效
curl -X DELETE 'localhost:8001/admin/faults?hour=2026-11-18T11'  # 只清 11 点
curl -X DELETE localhost:8001/admin/faults                        # 清除全部小时
```

采集端对每版最多重试 `MAX_ATTEMPTS` 次;仍失败则**只保留该小时原已确认供数**(当前版指针
不动),该小时状态记为 `failed`;其他小时照常采集。清除故障后再次采集即可继续补齐。

## 需求映射

| 需求 | 实现 |
| --- | --- |
| 源站按 UTC 小时隔离首版全量 | `source/app/main.py::_build_hour`:每小时独立 `truth`/`versions`/`current`,v1 由 `(seed, hour)` 生成 |
| 修订、故障也按小时隔离 | `STATE["hours"][hour]`;`/admin/publish`、`/admin/faults` 均要求 `hour` |
| 发布与故障注入须指定小时 | 源站接口 `hour` 为必填查询参数/请求字段,缺失返回 422 |
| 采集端只固定并读目标小时 | `collector/app/service.py`:固定版本与每页抓取都带 `hour`,并校验响应 `hour` |
| 采集 10 点期间发布 11 点修订不影响本次目标 | 目标版在采集开始时一次性固定(`pinned_version`),之后只抓 `prev+1..pinned` |
| 某小时缺页/失败只保留该小时原供数 | 错误只更新该小时 `hours.status/last_error`,`current_version` 指针不动;失败版及后续版不确认 |
| 其他小时仍能采集 | 锁按小时(`_lock_for`)、SQLite 按 `(hour, version)` 隔离;错误处理只触及当前小时行 |
| 重试不能混入残页 | 页面只存内存、从不落库;每次重试从第 1 页整版重取(`_fetch_version`) |
| 已确认版均可查当时完整数据 | 每版确认时物化全量快照到 `entries`,`GET /hours/{hour}/data?version=N` |
| 数据与当前版指针一同生效 | 单事务写入 `versions` + `entries` + `hours.current_version`(`db.py::tx`) |
| 失败或重启不得暴露半版 | 事务原子提交;启动时仅复位 `collecting` 状态元信息;SQLite WAL + 数据卷持久化 |

## API 一览

**collector(8000)**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/collections` | 采集某 UTC 小时,body `{"hour":"YYYY-MM-DDTHH"}`;该小时采集中返回 409 |
| GET | `/collections/{hour}` | 该小时采集状态/最近错误 |
| GET | `/hours` | 全部小时及各自当前版指针 |
| GET | `/hours/{hour}/versions` | 该小时已确认版列表 |
| GET | `/hours/{hour}/data` | 某版完整数据;`version` 缺省取该小时当前版,`station_id` 可单站过滤 |

**source(8001)** — 所有接口均需 `hour`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/revisions/current?hour=` | 该小时当前版号 |
| GET | `/api/revisions?hour=` | 该小时全部修订元信息 |
| GET | `/api/revisions/{v}/pages/{p}?hour=` | 该小时某版某页(`hour`/`version`/`page`/`total_pages`/`items`) |
| POST | `/admin/publish` | body `{"hour":…, "ops":[…]}`;发布该小时下一修订(缺省自动生成确定性增量) |
| POST | `/admin/reset` | body `{"hour":…}` 重置该小时回首版;缺省 `hour` 则重置全部小时 |
| GET/PUT/DELETE | `/admin/faults` | `PUT` body 含 `hour`;`GET`/`DELETE` 可用 `?hour=` 限定,缺省为全部 |
| GET | `/admin/state` | `?hour=` 看该小时;缺省看全部小时内部状态 |

## 本地开发(不用 Docker)

```bash
pip install -r source/requirements.txt -r collector/requirements.txt
(cd source && uvicorn app.main:app --port 8001) &
(cd collector && SOURCE_BASE_URL=http://localhost:8001 DB_PATH=/tmp/collector.db \
    uvicorn app.main:app --port 8000) &
python3 scripts/smoke.py
```
