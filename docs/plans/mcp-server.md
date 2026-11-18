# 外部智能体访问（MCP Server）实施规划

> 状态：规划定稿，待实现。需求入口为 `docs/requirements.md` FR-19 / §5.6。
> 本文件只写"怎么做"，不重复需求条款。日期：2026-11-18。

## 1. 已定决策

| 决策项 | 选择 | 约束后果 |
|---|---|---|
| 传输 | Streamable HTTP（远程） | 必须做鉴权与租户隔离；CORS 不是安全边界 |
| 能力边界 | 只读 + 校验 + 生成 | 不含烧录/上板；生成必须进程隔离 |
| 代码落点 | 并入 hw2c-web 的 FastAPI 进程 | 不再新建第三仓；必须先把业务逻辑从路由里抽成 service 层 |
| hw2c core | 不改，生成走子进程 | 接受 `sys.exit` / 全局 logging 的现状，用进程边界兜住；静默绿灯由 MCP 侧显式标注 |

**组合风险（必须先解决再上线）**：hw2c-web 当前无认证、CORS 硬编码 localhost、
`generate` 把 stderr 末 800 字节原样回传、120 秒 subprocess 直接占请求线程。
这四点在无认证远程暴露下全部变成漏洞。规划里对应措施见 §4。

## 2. 现状事实（规划依据）

### 2.1 hw2c 侧

- 可同进程并发暴露的纯函数：`parser.pipeline.run_pipeline()`（`parser/pipeline.py:693`）、
  `generator.validator.validate_hardware()`（`:590`）、`validate_bind_cross_refs()`（`:947`）、
  `generator.validators.pin_conflict_validator.validate_pin_conflicts()`（`:296`）、
  `generator.mapper.merge()/split_legacy()`（`generator/mapper.py:18/178`）。
  `split_legacy()` 的 docstring 已注明供 hw2c-web 使用。
- 不可同进程暴露：`generator.generate.main()` 读全局 `sys.argv`（`generate.py:1477`），
  `_setup_logging()` 清 root handlers（`:54-62`），`generate_project()` 有 11 处
  `sys.exit`（`:1026,1107,1143,1173,1322,1339,1343,1347,1351,1355,1360`）。
- 静默降级必须显式化：`generate.py:918-920` 在 cmake 缺失时返回 `(True, "")`；
  `:1155-1160` MCU 库缺失时引脚校验与分配双双失效。MCP 结果不能只看退出码。
- 权威六层 schema 在 `generator/schemas/{hardware,task,bind,component,param,pubsub}.py`。

### 2.2 hw2c-web 侧

- 后端 FastAPI，仅 3 个端点：`POST /api/parse`、`POST /api/generate`、`GET /api/health`
  （`backend/app.py:37-43`）。无 service 层，逻辑内联在路由。
- `backend/paths.py:16-31` 解析 hw2c 根目录：`HW2C_ROOT` 环境变量 → 兄弟目录 → CI 工作区。
- `backend/routes/generate.py:55-144`：临时目录写六层 YAML → `subprocess.run` 调
  `python -m parser.cli gen ... --force`（cwd=hw2c 根，timeout=120）→ 内存 ZIP → `finally` 清理。
- 无任何认证、token、多租户、数据库。CORS 仅 localhost:5173。
- CI `backend-e2e.yml` 已 checkout hw2c 到 `./hw2c` 并跑 parse→generate 闭环。

### 2.3 已有的 codegraph MCP

外部工具，不是本规划的一部分。它索引 Python 与 vendored C，**`.j2` / `.ld` 索引数为 0**。
模板语义、六层 YAML 契约、验收标准必须由本规划的 resources 补齐。
其 "Blast radius" 调用计数不可信，智能体若用它分析调用点须 Grep 复核。

## 3. 目标结构

```
hw2c-web/backend/
├── app.py                      现有；挂载 MCP 子应用
├── auth.py                     新增：Bearer token → workspace_id，fail-closed
├── services/
│   ├── parse_service.py        新增：从 routes/parse.py 抽出
│   ├── validate_service.py     新增：包装 hw2c 纯函数，结构化 issues
│   └── generate_service.py     新增：子进程执行器 + 沙箱 + 产物管理
├── routes/
│   ├── parse.py                瘦身为调用 service
│   └── generate.py             瘦身为调用 service（保留 ZIP 下载）
├── mcp/
│   ├── server.py               FastMCP 实例 + Streamable HTTP 挂载
│   ├── tools.py                §4.2 工具集
│   └── resources.py            §4.3 资源集
└── tests/
    ├── test_e2e.py             现有
    └── test_mcp_contract.py    新增：契约 + 安全护栏（含变异验证）
```

hw2c 主仓只改 `docs/requirements.md` 与本文档；**不改 `generator/`、`parser/`、`templates/`**。

## 4. 契约设计

### 4.1 挂载与鉴权

- FastMCP 以 Streamable HTTP 挂在 `/mcp`；`/api/health` 保持无认证用于探活。
- 启动时读 `HW2C_MCP_TOKEN`、`HW2C_WORK_ROOT`。**任一缺失则不注册 MCP 路由**（fail-closed），
  并在日志里写明原因；不允许"默认开发 token"。
- `Authorization: Bearer <token>` 用 `hmac.compare_digest` 校验；token → `workspace_id`
  映射由环境变量 `HW2C_MCP_WORKSPACES` 提供（格式 `id:sha256prefix,...`），不做数据库。
- 每个请求生成 `request_id`；所有临时文件落在
  `HW2C_WORK_ROOT/<workspace_id>/<request_id>/`，路径经 `resolve()` 确认在根目录内。
  跨租户按 `request_id` 无法枚举即隔离；`request_id` 用 `secrets.token_urlsafe(16)`。
- 日志只记 token 的 sha256 前 8 位，禁止记原文。
- CORS 保持只对 localhost dev server 开放；明确写进代码注释：**CORS 不是安全边界，
  `/mcp` 的唯一闸门是 token**。

### 4.2 工具集（初版 6 个）

| 工具 | 入参 | 输出 | 实现 |
|---|---|---|---|
| `parse_eda` | `netlist_text`, `bom_text?`, `filename?` | 六层 YAML 字符串 + `issues[]` | `run_pipeline()` |
| `validate_config` | `layers: {hardware, task?, bind?, components?, params?, pubsub?}` | `{ok, issues:[{severity, code, message, layer, path}]}` | Pydantic `model_validate` + 四个生产 validator |
| `get_example` | `name` | 六层 YAML | 只读 `examples/<name>/` |
| `list_examples` | 无 | `{name, summary}[]` | 只读目录 + `README` |
| `get_mcu_pinout` | `part` | 引脚/AF/可用外设 JSON | 只读 `generator/data/mcu/*.json` |
| `generate_firmware` | `layers`, `project_name?`, `options?` | `{request_id, status, artifact_uri, files[], warnings[], issues[], compile_check}` | §4.4 执行器 |

**明确不做**：烧录、串口抓取、上板复位、写用户 `output/`、传 `--force`、传 output 路径。
工具收到这类参数时返回 `E_FORBIDDEN`，不静默忽略。

`validate_config` 的 issues 必须保留 `severity`（CRITICAL/ERROR/WARNING/INFO）；
返回 `ok=true` 但 `warnings` 非空时，工具要显式提示"编译自检可能跳过"，不得只报成功。

### 4.3 资源集

| URI | 内容 | 注意 |
|---|---|---|
| `hw2c://requirements` | `docs/requirements.md` 全文 | 需求唯一入口 |
| `hw2c://requirements/{section}` | 按章节切片 | 供智能体只读相关条款 |
| `hw2c://docs/user-guide/{layer}-yaml` | 六层配置指南 | 补 codegraph 盲区 |
| `hw2c://schema/{layer}` | `Model.model_json_schema()` 动态生成 | 不写第四套 schema |
| `hw2c://examples/{name}/{layer}.yaml` | 示例配置 | 只读 |
| `hw2c://format/{fota_format,ymodem_format}.json` | 格式真源 | 来自 `generator/data/` |
| `hw2c://models/{name}.yaml` | 器件模型 | 来自 `models/` |
| `hw2c://artifacts/{request_id}/{filename}` | 生成产物 | 仅同 workspace 可读；只读 |

不暴露 `output/`、`build/`、`.workbuddy/`、`static/` 原始源码（后者交给 codegraph）。

### 4.4 生成执行器（关键）

从 `routes/generate.py:55-144` 抽出为 `services/generate_service.py`，Web 与 MCP 共用，
消除两处 subprocess 重复。加固项全部强制：

1. **路径**：`project_name` 必须满足 `Path(name).name == name`，且不含 `..`；
   输出目录固定在 `<workspace>/<request_id>/<project_name>`。
2. **体积**：每层 YAML ≤ 64 KiB，总 ≤ 256 KiB；产物 ZIP ≤ 32 MiB。超限返回 `E_TOO_LARGE`。
3. **超时**：subprocess 硬超时 120 秒（保留现值）；外层 `run_in_threadpool` + 并发
   semaphore=2 + 排队超时 30 秒；超时必须 kill 整个进程组，不留子进程。
4. **错误脱敏**：禁止回传原始 stderr。解析为 `GenerationIssue{code,message,location}`，
   白名单字段、单条 ≤ 200 字符、总数 ≤ 20；原文只写服务端日志。
5. **编译状态三态**：`compile_check ∈ {passed, failed, skipped}`。
   hw2c core 在 cmake 缺失时返回成功 ⇒ MCP 侧必须自己探测工具链是否存在，
   缺失就标 `skipped` 并给出 `warning`，不得报 `passed`。
6. **产物交付**：MCP 工具结果不塞 ZIP 二进制，返回 `artifact_uri`；智能体通过
   `hw2c://artifacts/{id}/{filename}` 读单个文件，Web UI 继续走原 REST 下载 ZIP。
7. **清理**：产物保留 1 小时后由请求时惰性清理（不引入后台线程）；清理失败记 warning。

### 4.5 与 Web 后端的关系

- Web 的 `/api/parse`、`/api/generate` 保留原语义，只把内联实现换成 service 调用。
- Web 路由继续无认证（本地配置台）；`/mcp` 有认证。二者不要合并成一个鉴权策略。
- 对称收益：以后 hw2c 的功能变更只需改 service 层，不再出现"Web 一份、MCP 一份"。

## 5. 实施阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 本文档 + `docs/requirements.md` FR-19 / §5.6 / §8 / §9 / §10 | 需求评审通过 |
| P1 | `auth.py` + service 抽取 + 5 个只读工具 + 全部 resources | 无 token 时 `/mcp` 不可达；只读工具可调用 |
| P2 | `generate_firmware` + §4.4 全部加固项 | 安全护栏变异验证全抓 |
| P3 | Web 路由切 service，删除重复 subprocess 实现 | `backend-e2e.yml` 全绿，Web 行为不变 |
| P4（可选） | `prompts/`：引导智能体按六层写配置的提示词模板 | 智能体能从零生成可编译配置 |

## 6. 护栏与验收

新增 `backend/tests/test_mcp_contract.py`，沿用仓库现有护栏风格：

- 工具清单**数量与名称**钉死（防止静默少注册一个工具）；每个工具有 input schema。
- resource URI 覆盖六层 + 需求 + 格式真源；`schema/{layer}` 必须从 Pydantic 动态取，
  发现硬编码副本即失败。
- 安全护栏：无 token → 401/404；路径穿越 → `E_FORBIDDEN`；超尺寸 → `E_TOO_LARGE`；
  原始 stderr 出现在任何工具响应 → 失败；`compile_check` 只有三态。
- **变异验证是唯一验收标准**：至少做 5 个变异体 —— 删 token 校验、删路径校验、
  恢复 stderr 原样透传、把 `compile_check` 固定成 `passed`、删掉一个 tool 注册。
  删掉判据后护栏必须红；若仍全绿，说明断言恒真，必须重写。

回归基线：`hw2c` 侧 `pytest tests parser/tests generator/tests -q`（当前 783 passed）、
hw2c-web `pytest backend/tests`（现有 e2e 必须保持通过）。

## 7. 明确排除

- 不建第三仓，不在 hw2c 主仓引入 FastAPI 依赖。
- 不改 hw2c core 的 `sys.exit` / logging / 生成器语义（本轮）。
- 不暴露烧录、串口、上板复位工具。
- 不重复 codegraph 的代码索引能力；不暴露 `.workbuddy/` 内部记忆。
- 不做数据库与用户注册；鉴权只用环境变量里的静态 token 映射。若将来要多人共享，
  这是第一个必须替换的点。
