# 港口 AIS 停靠记录服务

从 AIS 补报消息中整理**有进出港依据的停靠记录**，并把依据不足的港内连续点列为**待补记录**。
FastAPI 提供 HTTP API，SQLite（WAL 模式）持久化批次、消息与每版核算结果，支持 docker compose 启动。

## 1. 业务规则

### 港区

- 一个矩形港区，经纬度闭区间判定，**边界算港内**（`PORT_MIN_LON/PORT_MIN_LAT/PORT_MAX_LON/PORT_MAX_LAT` 可配）。

### 批次与消息

- 按**批号补交**（`mode=submit`）或**整批换交**（`mode=replace`）。
- 每条消息含：消息号 `msg_no`、船号 `ship_no`、UTC 时刻 `ts`（ISO-8601，带时区，建议 `Z` 结尾）、经纬度。
- 有效批次内：**同号同内容只算一条**；**同号异值**或**同船同刻异号**则**整批退回**（HTTP 409，不落任何数据）。
- 同一消息号可由多个有效批次提供（同内容叠加，来源批号全部保留）；与现行消息冲突（同号异值/同船同刻异号）同样整批退回。
- **换交**只影响本批：本批不再提供、且其他批次也不再提供的消息失效；其他批次仍提供的消息继续有效。

### 停靠判定（逐船）

1. 消息按 UTC 时刻排列；
2. 相邻点相隔 **超过 30 分钟**即断成不同段；
3. 段内连续港内点，**前后在同一段内都紧邻港外点** → `stop`（停靠）；
4. 否则（段首/段尾缺一侧或两侧港外点）→ `pending`（待补），并给出原因。

### 增量沿用（性能规则）

补交/换交只重算受影响的船：

- 某段与所有改动点（新增/变化点、被删消息的旧时刻）之间都隔着未变的港外点时，**整段沿用旧判定，不读取该段港内明细**；
- 同段内，某个港内连续点及其紧邻港外点都未被改动时，该港内段沿用旧判定；
- 响应中的 `reused_records` / `recomputed_records` 给出沿用与重算条数。

### 版本与一致性

- 每次批次生效生成一个不可变的**结果版本**，旧版本永久保留，可带 `version_id` 查询旧结果；
- 批次写入与新结果生成在**同一个 SQLite 事务**内完成，任何写入失败整体回滚，**原批次与原结果保留**；
- WAL 模式保证核算（写事务）进行期间，旧结果仍可被并发查询；
- `BEGIN IMMEDIATE` 串行化写事务，并发补交不会互相覆盖。

## 2. 启动

### docker compose

```bash
docker compose up -d --build
curl http://localhost:8000/health
```

SQLite 数据位于命名卷 `ais-data`（容器内 `/data/ais.db`）。

### 本地运行

```bash
pip install -r requirements.txt
AIS_DB_PATH=./data/ais.db uvicorn app.main:app --reload
```

### 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT_MIN_LON` / `PORT_MIN_LAT` | `121.0` / `31.0` | 矩形港区西南角（含边界） |
| `PORT_MAX_LON` / `PORT_MAX_LAT` | `122.0` / `32.0` | 矩形港区东北角（含边界） |
| `AIS_DB_PATH` | `/data/ais.db` | SQLite 文件路径 |

## 3. API

交互式文档：启动后访问 `/docs`（Swagger UI）。

### 补交 / 换交批次

```bash
# 补交（批号不可已存在）
curl -X POST "http://localhost:8000/batches/B1" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"msg_no":"m1","ship_no":"SH01","ts":"2026-11-24T08:00:00Z","lon":120.0,"lat":30.0}]}'

# 整批换交（批号必须已存在）
curl -X POST "http://localhost:8000/batches/B1?mode=replace" \
  -H 'Content-Type: application/json' -d '{"messages":[ ... ]}'
```

成功返回 `version_id`、受影响船、沿用/重算条数；校验失败返回 `409` 且整批不落库。

### 按船查询两类记录

```bash
curl http://localhost:8000/ships/SH01/stops           # 现行版本停靠记录
curl http://localhost:8000/ships/SH01/pending         # 现行版本待补记录
curl "http://localhost:8000/ships/SH01/stops?version_id=1"  # 指定历史版本
```

每条记录附**首/末港内点**以及**可用港外点**（待补可能为 `null`）的时刻、消息号和**现行来源批号**
（消息已失效时来源为空数组，但历史版本记录仍可查）。

### 其他

| 方法/路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查、现行港区与现行版本号 |
| `GET /batches/{batch_no}` | 批次信息与现行消息数 |
| `GET /versions` | 全部结果版本（含历史版本与 active 标记） |

### 各批贡献汇总

按**消息时刻的 UTC 自然日**统计每批给船位底表贡献了多少观测。重复报送的
同一条消息（同号同内容）在总数里只算一条，但会在仍提供它的批次之间分配。

```bash
curl "http://localhost:8000/contributions"                       # 全部 UTC 日
curl "http://localhost:8000/contributions?date=2026-11-24"       # 指定日期
curl "http://localhost:8000/contributions/2026-11-24"            # 同上（路径形式）
curl "http://localhost:8000/batches/B1/contributions"            # 某批按日展开
```

每天返回：

| 字段 | 说明 |
| --- | --- |
| `dedup_total` | 当天去重后的有效消息总数（同号同内容只算一条） |
| `batches[].valid_message_count` | 有效消息数：该批仍提供的消息条数 |
| `batches[].exclusive_message_count` | 独家消息数：仅由该批提供的消息条数 |
| `batches[].apportioned_count` | 折算数：每条消息在仍提供它的批次间平分 1 条，`"分子/分母"` 精确分数 |

例如同一条消息由 B1、B2 共同提供，则 B1、B2 各记 `1/2`；各批折算数之和
**恒等于当天 `dedup_total`**（用精确分数累计，无浮点误差）。

口径与一致性：

- 可带 `version_id` 查询历史版本的汇总，缺省为现行版本；
- 汇总与停靠结果、批次在同一个写事务内生成，写入失败整体回滚；查询只读该
  已生效版本的汇总行，各批统计与去重总数来自同一份已生效数据；
- 补交/换交只重建**来源集合确有增减**的消息所在的 UTC 日。即使当天去重总数
  没变，受影响的批次也会按新份额重算（例如某条共享消息少了/多了一个来源）；
- 旧库首次启用（表刚建、现行版本尚无汇总）时，按现行消息与来源自动补建。

## 4. 测试

```bash
pip install pytest httpx
python -m pytest -q
```

覆盖：矩形边界、30 分钟断段、停靠/待补分类、同号异值与同船同刻异号整批退回、
多批来源叠加、换交失效与他批保留、段级/港内段级增量沿用、历史版本查询、写入失败回滚；
贡献汇总的有效/独家/折算分数、同日来源增减后的重新分摊、UTC 日切分、历史版本与旧库补建。

## 5. 目录结构

```
app/
  config.py       # 港区矩形与 DB 路径配置
  database.py     # SQLite 连接、WAL、表结构、事务上下文
  engine.py       # 纯逻辑：UTC 解析、断段、港内连续点与沿用判定
  contribution.py # 贡献汇总：精确分数分摊、按版本存储、旧库补建
  models.py       # Pydantic 请求/响应模型
  service.py      # 批次校验、换交、增量核算事务、查询装配
  main.py         # FastAPI 路由
tests/            # pytest 引擎单测、API 端到端与贡献汇总测试
docker-compose.yml / Dockerfile / requirements.txt
```
