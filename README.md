# 实验室 FHIR R4 报告接入研究库（Lab FHIR Research Ingest API）

FastAPI + PostgreSQL 实现的 FHIR R4 事务型 Bundle 接入服务。检验科报告包以
transaction Bundle 提交，服务在**单数据库事务**内完成条件建患、观测/报告的
新增或版本化更新，并通过**接入单号幂等账本**保证“同内容重试复用首次回执、
异内容拒绝、响应丢失/进程重启不重复应用”。

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

- API 与交互式文档：<http://localhost:8000/docs>（Swagger UI，可直接在页面上发起 POST /fhir）
- OpenAPI：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>
- PostgreSQL：`localhost:5432`，库 `researchdb`，用户/密码 `research/research`

首次启动时 API 容器会等待数据库就绪并自动建表（`app/db_init.py`）。

## 2. 提交报告包

### 2.1 普通提交 `POST /fhir`（整包，≤20 项）

请求头必须携带接入单号：

```
Idempotency-Key: LAB-ACC-1001
Content-Type: application/json
```

示例（注意条目故意乱序：报告 → 观测 → 患者，依赖 `urn:uuid` 前向引用）：

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0003-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "DiagnosticReport",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "24323-8"}]},
        "subject": {"reference": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001"},
        "result": [
          {"reference": "urn:uuid:3c3f9d7e-0002-4b2a-9e1a-000000000001"}
        ]
      },
      "request": {"method": "POST", "url": "DiagnosticReport"}
    },
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0002-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7"}]},
        "subject": {"reference": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001"},
        "valueQuantity": {"value": 5.5, "unit": "mmol/L"}
      },
      "request": {"method": "POST", "url": "Observation"}
    },
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "Patient",
        "identifier": [{"system": "http://hospital/mrn", "value": "MRN-001"}]
      },
      "request": {"method": "POST", "url": "Patient"}
    }
  ]
}
```

### 更新（带 If-Match 的乐观锁）

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "resource": {
        "resourceType": "Observation",
        "id": "<已有观测id>",
        "status": "final",
        "code": {"coding": [{"code": "2345-7"}]},
        "subject": {"reference": "Patient/<患者id>"},
        "valueQuantity": {"value": 6.8, "unit": "mmol/L"}
      },
      "request": {
        "method": "PUT",
        "url": "Observation/<已有观测id>",
        "ifMatch": "W/\"1\""
      }
    }
  ]
}
```

- `ifMatch` 与当前版本不一致 → `412 Precondition Failed`，整包拒绝。
- 更新包内引用已存在资源请使用绝对引用（`Patient/<id>`、`Observation/<id>`）；
  `urn:uuid` 仅在同一 Bundle 内、且对应条目存在时有效。

### 2.2 大来件提交 `POST /fhir/intake`（按来件编号，≤100 项，分组续交）

一次检验科报告可能超过 20 项。大来件接口按**来件编号**（请求头
`Accession-Number`）接收最多 **100 项、仅含 `POST` 条目**的 FHIR R4
transaction Bundle，并在仓前完成整体校验后按依赖组依次入仓。

```
Accession-Number: LAB-ACC-2001
Content-Type: application/json
```

请求体与 `POST /fhir` 相同（`Bundle.type = transaction`），但引用规则更严格：

- `Observation.subject`、`DiagnosticReport.subject` 和
  `DiagnosticReport.result` **只能是本件内的 `urn:uuid` 引用**；绝对引用
  （`Patient/<id>`、`Observation/<id>`）或外部引用一律 `400`。
- 每个 `urn:uuid` 必须能解析到本件内的条目（可前向/乱序），否则 `422`。
- 通过 `subject` / `result` 直接或间接相连的条目属于**同一组**；单组仍受
  **20 项**上限约束，超限 → `400`。整件最多 100 项。

提交按**各组首次出现的位置**依次进行，每组在一个数据库事务中连同其回执
一起提交：

- 某组失败即停止，后续组不再提交；**已成功组仍可通过普通读取接口查询**。
- 失败组整体回滚，不留资源。
- 响应为 `transaction-response` Bundle，含 `status`（`completed` /
  `partial`）、按**原条目位置**排序的 `resourceMap`、各组状态 `groups[]`、
  以及失败位置 `failure`（`groupIndex` / `requestIndex` / `status` /
  `outcome`）。HTTP 状态仍为 `200`；响应头 `X-Intake-Status` 标明结果，
  部分成功时另带 `X-Intake-Failed-Group` / `X-Intake-Failed-Entry`。

**用同编号 + 原内容重提**：

- 跳过已成功组，续交未完成组，按原条目位置返回完整资源映射与失败位置；
- 全部组完成后同内容重提返回逐字节一致的回执，不重复入仓；
- 同编号 + 异内容 → `409`；
- 组进度持久化在 PostgreSQL，**服务重启后**仍可续交；
- 并发重提由组记录行锁串行化，已完成组不会被重复应用。

示例（Swagger UI 的 `/docs` 中该接口自带可直接提交的两组示例，改一下
`Accession-Number` 即可演示；把第二组改坏再提交可演示部分成功与续交）：

```bash
curl -X POST http://localhost:8000/fhir/intake \
  -H 'Accession-Number: LAB-ACC-2001' \
  -H 'Content-Type: application/json' \
  -d @large-bundle.json
```

**在 `/docs` 演示提交与续交**：

1. 打开 <http://localhost:8000/docs> → `POST /fhir/intake` → *Try it out*；
   `Accession-Number` 填 `LAB-ACC-2001`，请求体选示例
   **“Two groups under one accession number”** → *Execute*：应返回
   `status: completed`、按原位置排序的 `resourceMap` 和两组 `completed`。
2. 把 `Accession-Number` 改成 `LAB-ACC-DEMO-FAIL`，请求体选示例
   **“Partial failure + resume demo”** → *Execute*：第 1 组 `completed`，
   第 2 组 `failed`（`failure.requestIndex = 5`），`resourceMap` 仅含第 1 组
   的 0、1 两个位置；响应头 `X-Intake-Status: partial`。用返回的患者 `id`
   调 `GET /patients/{id}/observations`，可看到第 1 组已可查询。
3. 不改请求体与编号，再点一次 *Execute*：返回完全相同的部分回执，
   第 1 组不会重复入仓（观测数不变）。把第 2 组报告与观测改为同一患者后
   以同编号提交会得到 `409`（同号不同文），需换新编号。
4. 服务重启后再用同编号 + 原内容调用，会跳过已成功组、只续交未完成组。

## 3. 回执与读取接口

提交成功返回 `transaction-response` Bundle：

- `entry[].response`：FHIR 风格的 status / etag / location；
- `resourceMap[]`：按请求顺序给出每个条目的 `resourceType / id / version / action / fullUrl` 映射。

读取接口（均支持按患者维度）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/patients?identifier=system|value` | 按标识查患者 |
| GET | `/patients/{id}` | 患者摘要（含报告/观测计数） |
| GET | `/patients/{id}/observations` | 该患者全部观测 |
| GET | `/patients/{id}/diagnostic-reports` | 该患者全部报告（含观测 id 列表） |
| GET | `/observations/{id}` | 单个观测（响应头带 `ETag: W/"v"`） |
| GET | `/diagnostic-reports/{id}` | 单个报告 |
| GET | `/observations/{id}/_history` | 观测全部历史版本 |
| GET | `/diagnostic-reports/{id}/_history` | 报告全部历史版本 |
| GET | `/patients/{id}/_history` | 患者历史版本 |

## 4. 业务规则与失败语义

1. **条目上限 20**，仅允许 `Patient / Observation / DiagnosticReport`；
   超限或类型不符 → `400`。
2. **患者条件创建**：按 `identifier.system/value` 命中则复用，否则新建；
   包内标识重复 → `400`；并发同标识由数据库唯一约束 + 行锁保证只建一份。
3. **乱序前向引用**：`urn:uuid` 在解析阶段统一绑定，与条目顺序无关；
   无法解析、引用不存在的外部资源 → `422`，整包拒绝。
4. **患者一致性**：每份报告与其全部观测必须同属一个患者；把观测改挂其他
   患者时，若已有引用报告未在**同一 Bundle** 内一并更正 → `422`。
5. **原子性**：任何校验/版本/引用失败都会回滚整个事务，不留任何资源或
   修改；其他患者已提交的数据不受影响，后续合法 Bundle 仍可正常提交。
6. **幂等**：
   - 同 `Idempotency-Key` + 同内容（规范化 JSON 哈希，与键序/空白无关）→
     直接返回首次回执，不重复落库；
   - 同 key + 异内容 → `409`；
   - 失败的提交会释放幂等槽，修正后可用同一单号重试；
   - 幂等状态持久化在 PostgreSQL，进程重启/响应丢失后重试仍然安全。
7. 所有错误均返回 FHIR `OperationOutcome`（`application/fhir+json`）。
8. **大来件接口**（`POST /fhir/intake`）的规则见 §2.2：全件预校验通过后
   才入仓；组内原子、组间顺序；同编号同内容可断点续交、异内容拒绝。

## 5. 本地开发与自测（无需 Docker）

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg2://research:research@localhost:5432/researchdb"
python -m app.db_init
uvicorn app.main:app --reload
pytest -q
```

测试默认连接 `postgresql+psycopg2://research@localhost:5433/researchdb_test?host=/tmp`，
可用 `DATABASE_URL` 覆盖；并发测试（同标识建患、If-Match 竞争）仅在 PostgreSQL 下执行。

## 6. 目录结构

```
app/
  main.py            FastAPI 入口、异常处理
  config.py          环境配置
  database.py        引擎/会话/建表
  models.py          ORM（患者/标识/观测/报告/关联/版本/幂等账本/大来件账本，PG JSONB）
  errors.py          OperationOutcome 错误体系
  fhir_utils.py      ETag/版本工具
  routers/           /fhir、/fhir/intake 提交与资源读取路由
  services/
    bundle.py        Bundle 校验、幂等、事务处理、患者一致性规则
    intake.py        大来件校验、依赖分组、逐组事务、断点续交
    queries.py       读取与历史查询
scripts/entrypoint.sh  容器启动（等库+建表）
tests/               59 个 pytest 用例（含并发、重启、原子性、大来件续交）
docker-compose.yml   db + api 两服务，含健康检查
```
