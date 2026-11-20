# 水质平台 · 离线归档筛查

水质档案员按**质控码**与**工况**调取旧报文前，先用本系统筛一遍离线归档包：
在型号页登记两个枚举字段，录入每个归档包的包号、包内各**已发布固件版本（固件段）**
的报文条数与固件契约，以及整包在两个字段上**各状态的报文条数**
（**字段缺省 missing 与显式 null 分开计数**）。筛查时为两个字段各选一个待查状态，
系统列出每个包中哪些固件段**可能存在同时命中两个状态的报文**。

## 判定规则（核心）

- 对每个固件段的受限 JSON Schema 契约，用 **z3** 求解「契约合法 ∧ 双字段同时处于
  待查状态」的报文，支持**顶层 anyOf**（对各分支分别证明）：
  - **UNSAT（no）**⇒ 该段不可能产生同时命中的报文，可排除；
  - **SAT（yes）**⇒ 还原一条见证报文，并用独立纯 Python 判定器复核
    （确实满足契约、确实呈现两个待查状态）；
  - **UNKNOWN** ⇒ 无法判定，整包标「待核」。
- **整包计数与各段契约必须同时成立**：摘要中某状态条数为正，却没有任何固件段能产生
  该状态 ⇒ 摘要与契约矛盾，整包标「**待核**」。
- **不按报文条数展开**：只在契约语言集合上做可满足性证明，不枚举、不抽样具体报文，
  也不按条数复制变量。
- **只有所有固件段都被证明不可能命中（no）**，或整包待查状态计数为 0，才可**跳过**该包；
  任一段可能命中（yes）即为**命中候选**，列出可能命中的固件段。
- 包级三态结论：`hit`（命中候选 · 保留）/ `skip`（可跳过）/ `review`（待核）。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

- 操作入口（网页）：http://localhost:8000
- 健康检查：http://localhost:8000/api/health
- 数据保存在命名卷 `pgdata`（PostgreSQL），服务重启后已录入数据保留。

## 网页操作

1. 首页创建型号（如 `WQ-5000`），进入型号页。
2. 登记两个枚举字段（默认质控码 `qc` ∈ {N, Q}、工况 `mode` ∈ {routine, storm}）。
   `missing`（缺省）与 `null`（显式空值）是两个独立隐式状态，自动分开计数，不列入枚举清单。
3. 在「录入离线归档包」粘贴包 JSON（内置 4 个示例一键填入）：
   - **PKG-A 新旧固件混装**：v1 的 `qc` 必填、v2 允许缺省/null；查「qc 缺省 × storm」
     时 v1 被 UNSAT 排除、v2 可能命中；
   - **PKG-B 顶层 anyOf**：v3 的风暴报文分支没有 `qc`，anyOf 两段均可能命中；
   - **PKG-C 摘要矛盾（待核）**：摘要登记 2 条 storm，但唯一段契约的 mode 枚举只有 routine；
   - **PKG-D 全段不可能（可跳过）**：单段 v1 双字段必填，缺省报文不存在。
4. 在「双字段联合筛查」为两个字段各选一个待查状态（missing / null / 具体枚举值），
   提交后逐包展示段级判定、见证报文、整包计数核对与包级结论。

## 包 JSON 结构

```json
{
  "package_no": "PKG-A",
  "note": "可选备注",
  "counts": {
    "qc":   {"missing": 2, "null": 1, "value:\"N\"": 2, "value:\"Q\"": 1},
    "mode": {"value:\"routine\"": 4, "value:\"storm\"": 2}
  },
  "segments": [
    {"segment": "v1", "message_count": 3, "schema": { /* 受限 JSON Schema */ }},
    {"segment": "v2", "message_count": 3, "schema": { /* 支持顶层 anyOf */ }}
  ]
}
```

- 状态键：缺省 `missing`、显式空值 `null`、枚举值 `value:<JSON 值>`
  （字符串 `value:"N"`、整数 `value:3`）；未列出的状态按 0 处理。
- 守恒约束：每个字段各状态条数之和必须等于各固件段报文条数之和，否则拒绝录入。
- 每包至多 8 个固件段，每型号至多 32 个包。

## 受限 JSON Schema 语法（其余一律拒绝）

- 平面对象：`{"type":"object","properties":{...},"required":[...],"additionalProperties":false}`
  （`required` 可省略，默认空；`additionalProperties` 必须显式为 `false`）
- 顶层 `anyOf`：`{"anyOf":[<平面对象>, ...]}`，且只能单独出现在顶层
- 整数上下界：`{"type":"integer","minimum":m,"maximum":M}`（上下界均可省略）
- 字符串/整数/空值枚举：`{"enum":[...]}`，可配 `"type"` 限定；枚举值只允许字符串、整数、`null`
- 空值类型：`{"type":"null"}`

嵌套对象、数组、`pattern`、`oneOf`/`allOf`/`not`、浮点/布尔值、`string` 无枚举等语法均被拒绝。

## JSON API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/models` | 创建型号 `{"name": "WQ-5000"}` |
| GET | `/api/models/{name}` | 查看字段、归档包与固件段 |
| POST | `/api/models/{name}/fields` | 登记两个枚举字段（一次性，不可改） |
| POST | `/api/models/{name}/packages` | 录入归档包（含计数、段条数、契约） |
| POST | `/api/models/{name}/screen` | 双字段联合筛查 `{"query": {"qc":"missing","mode":"value:\"storm\""}}` |
| GET | `/api/examples` | 内置字段定义与四个示例包 |

录入不合法（语法、守恒、上限、状态键越界等）返回 `422`；筛查结果中
`verdict ∈ hit | skip | review`，并附带每段 `yes/no/unknown` 判定、见证报文与原因。

## 本地开发与自测

```bash
pip install -r app/requirements.txt httpx
python3 tests/test_units.py                 # 状态解析 + z3 可满足性 + 筛查服务单元自测
cd app && DATABASE_URL=sqlite:////tmp/archive.db uvicorn main:app --port 8000 &
python3 tests/selftest.py                   # 集成自测（API 全流程 + HTML 冒烟）
```

`DATABASE_URL` 缺省为 SQLite（便于本地自测）；Compose 中注入 PostgreSQL 连接串。

## 目录结构

```
app/
  main.py         FastAPI 入口（HTML 页面 + JSON API）
  service.py      录入校验、摘要×契约一致性、双字段联合筛查
  solver.py       z3 字段状态可满足性判定（含见证报文纯 Python 复核）
  schema_spec.py  受限 JSON Schema 子集解析、状态键与纯 Python 报文判定
  db.py           SQLAlchemy 模型（PostgreSQL JSONB / SQLite JSON）
  examples.py     质控码×工况示例字段与四个示例包
  templates/      页面模板
tests/
  test_units.py   单元自测
  selftest.py     集成自测
docker-compose.yml
```
