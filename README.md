# 防汛雨量数据平台

把雨量站主、备翻斗计的**累计脉冲计数**转换为可信的 **UTC 小时降雨量**，供预警模型取数。
并支持按**面积权重**把多个站点合成为**流域面雨量**。
Flask + SQLite，网页上传/查询 + 等效 JSON API，Docker Compose 一键启动。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据库文件保存在命名卷 `rainfall-data`（容器内 `/data/rainfall.db`），重启后数据仍可查询。

本地开发（无需 Docker）：

```bash
pip install -r requirements.txt
python -m app.app                 # 开发服务器，监听 8000
# 或生产形态（与 Docker CMD 相同）：
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 "app.app:create_app()"
```

运行自测（45 个用例）：

```bash
python -m unittest discover -s tests -v
```

## CSV 格式

列顺序：`站点, 主备标记, UTC整分钟, 计数(0~9999), 修订号`；表头可选（支持中英文列名），无表头时按此顺序解析。

```csv
station,device,minute,counter,revision
ST01,primary,2026-11-17T10:00:00Z,9980,1
ST01,backup,2026-11-17T10:00:00Z,120,1
```

- **站点**：非空字符串（≤64 字符）。
- **主备标记**：`primary`/`backup`，也接受 `主`/`备` 等别名。
- **UTC 整分钟**：如 `2026-11-17T10:00:00Z`、`2026-11-17 10:00`；未带时区按 UTC 处理，带偏移会换算成 UTC；秒必须为 00。
- **计数**：整数 0~9999。
- **修订号**：非负整数。同一（站点, 设备, 分钟）**只接受更高修订号**，否则该行被拒绝并记入批次报告。

示例文件：`examples/sample.csv`（含完整小时、备计、回绕、缺口四种情形）。

## 计算规则

- 每小时 = 60 个相邻分钟间隔（整点 H:00 至下一整点，共 61 个端点）。
- 每个间隔：**主计两端齐全取主计差值，否则备计两端齐全取备计差值，绝不跨设备相减**；两端都无可用数据则该间隔缺失。
- 计数下降按一次 10000 回绕处理：差值 = (末 − 初) mod 10000。
- 每脉冲 0.2 mm；**任一间隔缺失则整小时不可供数**（`status=incomplete`，`rainfall_mm=null`），网页会标出缺口与每段来源。

## 流域面雨量

预警模型需要流域每小时的面雨量，而单站接口只给出单站结果。平台内置流域配置与面雨量查询。

**配置**（数据员在网页操作，或调用 `POST /api/basins`）：

- 一个流域配置 **2~8 个成员站**，每站一个**面积权重**；
- 每个权重必须 **大于 0**，且全部权重 **合计为 1**；
- 权重以百万分之一整数存储与校验（`0.1 + 0.2 + 0.7` 可精确等于 1），最多 6 位小数；
- 同一流域再次保存会生成新版本并整体替换成员；**保存失败（含合计不为 1）时旧定义保持不变**。

**查询**（UTC 整点，网页与 API 同一计算入口，结果一致）：

- 各成员站沿用单站的**主备取值与缺口规则**；
- 每个成员站该小时的 `compute_hour` 结果必须 `status=ok`；**任一成员站不可供数，流域即不可供数**
  （`status=incomplete`，`rainfall_mm=null`），**不按其余站点重新分配权重**；响应中 `unavailable_stations` 列出原因站点；
- 全部可供数时：各站贡献 = 站雨量 × 面积权重，**面雨量 = 各站贡献之和**（均保留 2 位小数）。

**一致性**：流域查询在**单个读事务（同一 SQLite 快照）**中同时读取流域定义与该小时全部成员站读数。
因此配置保存与 CSV 补传交错提交时，一次查询的配置与全部站点雨量必然来自同一个已提交状态，
不会混用同批补传前后的读数；整点读数的修订也会同时反映在相邻两小时（由 readings 表现算，无缓存）。

网页入口：`/basins`（配置）、`/basin?name=..&hour=..`（面雨量）。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /` | 首页：上传、查询、站点与批次列表 |
| `POST /upload` | 网页上传（文件或粘贴文本），返回批次报告页 |
| `POST /api/upload` | 同上，JSON 返回 |
| `GET /report?station=..&hour=..` | 网页小时报告（雨量、缺口、每段来源） |
| `GET /api/hourly?station=..&hour=..` | 等效 JSON（与网页同一计算入口，结果一致） |
| `GET /basins` | 流域配置页：列表 + 保存表单（`?name=..` 编辑已有流域） |
| `POST /basins` | 网页保存流域配置（表单） |
| `GET /basin?name=..&hour=..` | 网页流域面雨量报告（各站权重、贡献、缺口与不可供数站点） |
| `POST /api/basins` | JSON 保存流域配置 |
| `GET /api/basins` | 流域配置列表（含版本与成员权重） |
| `GET /api/basin_hourly?basin=..&hour=..` | 等效 JSON（与网页同一计算入口，结果一致） |
| `GET /api/stations` | 站点数据范围 |
| `GET /api/batches` | 上传批次列表 |
| `GET /batches/<id>` | 批次行级明细（接受/拒绝及原因） |
| `GET /api/health` | 健康检查 |

`hour` 需整点对齐，如 `2026-11-17T10:00Z`。预警模型取数时请检查 `status == "ok"` 再使用 `rainfall_mm`。

保存流域配置的 JSON 示例：

```json
POST /api/basins
{"name": "示例流域", "members": [
  {"station": "ST01", "weight": "0.4"},
  {"station": "ST02", "weight": "0.6"}
]}
```

成员必须 2~8 个、权重均大于 0 且合计为 1，否则返回 `400` 且**旧定义不变**。

## 一致性与可靠性设计

- **两小时一致**：小时雨量由 `readings` 表现算现算（无缓存），补传或修订整点读数提交后，前后两小时立即同时反映新值；网页与 API 共用同一计算函数。
- **流域查询快照一致**：流域定义与全部成员站读数在单个读事务（同一 SQLite 快照）中读取；配置保存（整版切换）与 CSV 补传（整批提交）都不会被读到一半，一次查询不会混用同批补传前后的读数。
- **原子性**：每批上传的读数、批次记录、行级明细在**单个事务**中写入；流域配置的版本号切换与成员写入同样在**单个事务**中完成，任何失败整体回滚。两者失败时都保持各自旧状态。
- **持久化**：SQLite（WAL 模式）落盘于挂载卷，重启后仍可查询。
- **并发**：单 gunicorn worker 多线程 + SQLite `BEGIN IMMEDIATE` 与 `busy_timeout`，写入串行化。

## 目录结构

```
app/
  core.py        # 纯逻辑：CSV 解析校验、小时雨量计算、流域权重校验与面雨量合成（不依赖 Flask/SQLite）
  db.py          # SQLite：模式、事务性批量入库、流域配置版本与快照查询
  app.py         # Flask 路由（网页 + API）
  templates/     # 页面模板（含 basins.html / basin_report.html）
tests/test_app.py  # 45 个自测用例
examples/sample.csv
Dockerfile / docker-compose.yml / requirements.txt
```
