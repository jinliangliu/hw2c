# 外部智能体访问（MCP Server / WorkBuddy Connector）实施规划

> 状态：规划定稿，待实现。需求入口为 `docs/requirements.md` FR-19 / §5.6。
> 本文件只写"怎么做"，不重复需求条款。日期：2026-11-18（第二版，补充 Connector 场景）。

## 1. 目标场景

用户在 WorkBuddy 中用自然语言提出需求 → WorkBuddy 经 Connector 调用 hw2c MCP 工具
→ hw2c 解析/校验/生成源码 → **编译出可执行固件** → 返回可下载产物；本机形态下还可
按提示词烧录到板子。闭环是"提示词 → 可执行固件"，不是"提示词 → 源码 ZIP"。

## 2. 已定决策与两处硬约束

| 决策项 | 选择 | 约束后果 |
|---|---|---|
| 主要消费者 | WorkBuddy Connector | 必须符合开放平台 Connector 包规范（`connector-meta.json` + `mcp.json` + 图标 + Skill） |
| 传输 | 双形态：`hw2c-local`（stdio，主）+ `hw2c-cloud`（streamableHttp，可选） | 本机形态才能访问编译链与调试探针；远程形态只做生成/编译 |
| 能力边界 | 只读 + 校验 + 生成 + **编译**；烧录**仅本机形态**且默认禁用 | 远程 MCP 访问不到本机 USB/串口，烧录放远程是物理上不可能 |
| 代码落点 | 并入 hw2c-web 仓；service 层共用，两种 MCP 入口 | 不建第三仓；不把生成逻辑复制两份 |
| hw2c core | 不改，生成走子进程 | `sys.exit` / 全局 logging 用进程边界兜住；静默绿灯显式标注 |

### 2.1 硬约束一：WorkBuddy 默认 30 秒超时

WorkBuddy 开放平台文档：MCP `mcp.json` 的 `timeout` 默认 `30000` ms，
"单次请求建议 30 秒内响应"。hw2c 生成 + 编译实测需要 60–180 秒（cmake configure /
build / 额外 target 三阶段，见 `generator/generate.py:829-926`）。

**结论：生成/编译必须做成异步任务。** 同步工具调用会被 WorkBuddy 掐断。
提高 `timeout` 不是解法——官方只是"建议"，但客户端体验与网关限制不受我们控制。

### 2.2 硬约束二：远程 MCP 无本机硬件

pyOCD + ST-Link/DAP-Link、COM4/COM5 串口、`arm-none-eabi-gcc` 工具链都在用户机器上。
远程 HTTP 服务端看不到这些设备。**烧录、串口抓取、上板复位只能放在 stdio 本机形态。**

这不是安全策略选择，是物理事实。远程形态若声称支持烧录，必然是在骗用户。

### 2.3 组合风险

hw2c-web 当前无认证、CORS 硬编码 localhost、`generate` 把 stderr 末 800 字节原样回传、
120 秒 subprocess 占请求线程。远程形态暴露这些内容全部变成漏洞，加固见 §5.4。

## 3. 现状事实（规划依据）

### 3.1 hw2c 侧

- 可同进程并发暴露的纯函数：`parser.pipeline.run_pipeline()`（`parser/pipeline.py:693`）、
  `generator.validator.validate_hardware()`（`:590`）、`validate_bind_cross_refs()`（`:947`）、
  `generator.validators.pin_conflict_validator.validate_pin_conflicts()`（`:296`）、
  `generator.mapper.merge()/split_legacy()`（`generator/mapper.py:18/178`）。
- 不可同进程暴露：`generate.main()` 读全局 `sys.argv`（`generate.py:1477`），
  `_setup_logging()` 清 root handlers（`:54-62`），`generate_project()` 有 11 处 `sys.exit`。
- 静默降级：`generate.py:918-920` cmake 缺失返回 `(True, "")`；`:1155-1160` MCU 库缺失时
  引脚校验与分配双双失效。MCP 结果不能只看退出码。
- **编译产物已存在**（无需另造编译服务）：`output/base/build/{base.elf,base.bin,base.hex,base.map}`；
  `output/fota_demo/build/` 另有 `combined.bin`（引导器+槽 A，烧录用）与 `fota_demo_crc.bin`。
  生成流程自带编译自检，产物就在 `<output>/build/`。
- 权威六层 schema 在 `generator/schemas/{hardware,task,bind,component,param,pubsub}.py`。

### 3.2 hw2c-web 侧

- 后端 FastAPI，3 个端点；无 service 层，逻辑内联在路由（`backend/app.py:37-43`）。
- `backend/paths.py:16-31` 解析 hw2c 根目录：`HW2C_ROOT` → 兄弟目录 → CI 工作区。
- `backend/routes/generate.py:55-144`：临时目录写 YAML → subprocess 调
  `python -m parser.cli gen ... --force`（cwd=hw2c 根，timeout=120）→ 内存 ZIP。
- 无认证、无多租户、无数据库。CORS 仅 localhost:5173。

### 3.3 WorkBuddy Connector 规范（查证于开放平台文档）

- 包结构：`connector-meta.json`（必需）、`mcp.json`（必需）、`icon.svg`（必需）、
  `skills/{name}/SKILL.md`（可选）、`token-schema.json`（`auth_mode: "token"` 时必需）。
- `mcpServers` **只能配置一个 Server** ⇒ 两个形态必须做成两个 Connector 包，各自独立 source。
- 传输：`stdio`（本地进程）、`sse`、`streamableHttp`（远程，URL 必须 HTTPS）。
- stdio 支持 `cwd`（v4.22.15+）与 `env`；`runtime` 托管目前只支持 Node，
  **Python 入口必须写解释器绝对路径 + `cwd`**，不能依赖托管运行时。
- `auth_mode: "token"` 需 `token-schema.json`，且声明 `minWorkbuddyVersion >= 4.23.0`。
- 需提交审核，通过后发布到 Connector 市场；更新必须递增 `version` 重新审核。

### 3.4 已有的 codegraph MCP

外部工具，不纳入本规划。它索引 Python 与 vendored C，`.j2` / `.ld` 索引数为 0。
模板语义与六层契约必须由本规划的 resources 补齐；其调用计数不可信，须 Grep 复核。

## 4. 目标结构

```text
hw2c-web/
├── backend/
│   ├── app.py                      现有；仅 cloud 形态挂载 MCP
│   ├── auth.py                     新增：Bearer token → workspace_id，fail-closed
│   ├── services/
│   │   ├── parse_service.py        新增：从 routes/parse.py 抽出
│   │   ├── validate_service.py     新增：包装 hw2c 纯函数
│   │   ├── generate_service.py     新增：异步生成 + 编译 + 产物管理（两种形态共用）
│   │   └── flash_service.py        新增：pyOCD 烧录 + 回读校验（仅本机）
│   ├── mcp/
│   │   ├── tools.py                工具实现（按形态注册子集）
│   │   ├── resources.py            资源实现
│   │   └── jobs.py                 内存任务表 + 并发闸门
│   ├── routes/                     现有 Web 路由，瘦身为调 service
│   └── tests/test_mcp_contract.py  新增：契约 + 安全护栏（含变异验证）
└── connector/
    ├── local/                      hw2c-local Connector 包（stdio）
    │   ├── connector-meta.json
    │   ├── mcp.json                 command = Python 绝对路径，含 cwd/env
    │   ├── icon.svg
    │   └── skills/hw2c-firmware/SKILL.md
    └── cloud/                      hw2c-cloud Connector 包（streamableHttp）
        ├── connector-meta.json      auth_mode: token
        ├── mcp.json                 HTTPS URL + ${HW2C_TOKEN}
        ├── token-schema.json
        ├── icon.svg
        └── skills/hw2c-firmware/SKILL.md
```

hw2c 主仓只改 `docs/requirements.md` 与本文档；**不改 `generator/`、`parser/`、`templates/`**。

## 5. 契约设计

### 5.1 工具集

**共用（两种形态）**

| 工具 | 入参 | 出参 | 同步性 |
|---|---|---|---|
| `parse_eda` | `netlist_text`, `bom_text?`, `filename?` | 六层 YAML + `issues[]` | 同步（< 30 s） |
| `validate_config` | `layers` | `{ok, issues:[{severity,code,message,layer,path}]}` | 同步 |
| `list_examples` / `get_example` / `get_mcu_pinout` | 见 §5.2 | 同步 |
| `generate_firmware` | `layers`, `project_name?`, `options?` | `{request_id, status:"queued"}` | **异步，立即返回** |
| `get_build_status` | `request_id` | `{status, progress, compile_check, artifact_manifest}` | 同步 |
| `list_artifacts` | `request_id` | `[{name, size, sha256, uri}]` | 同步 |

**仅本机形态**（`HW2C_ENABLE_FLASH=1` 才注册，默认不注册）

| 工具 | 入参 | 出参 |
|---|---|---|
| `list_probes` | 无 | pyOCD 探测到的调试器列表 |
| `flash_firmware` | `request_id`, `artifact`, `slot?`, `confirm` | 回读对拍结果 + 烧录结果 |
| `verify_flash` | `request_id`, `artifact` | 逐字节比对结果 |
| `capture_serial` | `port`, `seconds?`, `trigger?` | 带速率自检的串口文本 |

`flash_firmware` 的安全闸门：`confirm` 必须为 `true`，否则只返回 dry-run 计划（目标地址、
预计覆盖区、镜像 sha256）；先回读对拍再决定是否烧；带 bootloader 的工程默认烧
`combined.bin` 到 `0x08000000`；槽 B 需显式 `slot: "B"`。这些约束来自项目教训，
工具描述里必须写清楚，不能指望智能体自己知道。

### 5.2 资源集

| URI | 内容 |
|---|---|
| `hw2c://requirements` / `hw2c://requirements/{section}` | 需求文档 |
| `hw2c://docs/user-guide/{layer}-yaml` | 六层配置指南 |
| `hw2c://schema/{layer}` | `Model.model_json_schema()` **动态生成** |
| `hw2c://examples/{name}/{layer}.yaml` | 示例配置 |
| `hw2c://format/{fota_format,ymodem_format}.json` | 格式真源 |
| `hw2c://models/{name}.yaml` | 器件模型 |
| `hw2c://artifacts/{request_id}/{filename}` | 生成产物（仅同 workspace 可读） |

不暴露 `output/`、`build/`、`.workbuddy/`、`static/` 原始源码。

### 5.3 异步任务与编译产物

`generate_firmware` 立即返回 `request_id`；后台线程执行 `generate_service`：

1. subprocess 调 `parser.cli gen`，产物落在
   `<workspace>/<request_id>/<project_name>/`（含 build/）。
2. 从 `<project>/build/` 收集 `*.elf / *.bin / *.hex / *.map / combined.bin`，
   计算 size + sha256，生成 `artifact_manifest`。**这就是"可执行固件"的交付物**。
3. 任务状态机：`queued → running → succeeded | failed | cancelled`，
   存内存表（不做数据库），TTL 1 小时。WorkBuddy 侧每 2 s 轮询 `get_build_status`，上限 180 s。
4. 编译状态三态：`compile_check ∈ {passed, failed, skipped}`。MCP 侧自己探测
   cmake/ninja/arm-none-eabi-gcc 是否存在；缺任一即 `skipped` + warning，
   **不得**把 hw2c 的静默绿灯报成 `passed`。
5. 输出目录固定，禁止接受用户传入 output 路径或 `--force`；命中返回 `E_FORBIDDEN`。

### 5.4 加固项（两种形态共用）

- 体积：每层 YAML ≤ 64 KiB，总 ≤ 256 KiB；产物 ZIP ≤ 32 MiB → `E_TOO_LARGE`。
- 路径：`project_name` 必须 `Path(name).name == name` 且不含 `..`；`resolve()` 校验在根内。
- 超时：生成 subprocess 120 s；并发 semaphore=2；排队 30 s；超时 kill 进程组。
- 错误脱敏：原始 stderr 不回传；解析为 `GenerationIssue{code,message,location}`，
  单条 ≤ 200 字符、最多 20 条，原文只写服务端日志。
- 鉴权（cloud）：`Authorization: Bearer <token>`，配置缺失则不注册 MCP 路由（fail-closed）；
  token → workspace 用环境变量映射；日志只记哈希前 8 位；CORS 不是安全边界。
- 本机形态：不监听网络端口，由 WorkBuddy stdio 启动；烧录工具默认不注册。

### 5.5 Connector 包要点

- `hw2c-local`：`type: "stdio"`，`command` 写本机 Python 绝对路径，
  `args: ["-m", "hw2c_connector.local"]`，`cwd` 指向 hw2c-web/backend，
  `env` 含 `HW2C_ROOT`、`HW2C_WORK_ROOT`、`HW2C_ENABLE_FLASH`。
- `hw2c-cloud`：`type: "streamableHttp"`，HTTPS URL，`headers.Authorization = "Bearer ${HW2C_TOKEN}"`；
  `connector-meta.json` 用 `auth_mode: "token"`、`minWorkbuddyVersion: "4.23.0"`。
- `SKILL.md` 必须逐一说明工具用途/参数/返回值，并给出提示词示例：
  "用 table 的六层配置生成一个 STM32G0B1 固件并编译" /
  "把上次生成的固件烧到板子的槽 B，烧之前先回读对拍"。
  模型主要靠工具描述决定是否调用，描述写糊了就不会被用。

## 6. 实施阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 本文档 + FR-19 / §5.6 / §8 / §9 / §10（本轮） | 需求评审通过 |
| P1 | service 抽取 + 只读工具 + 全部 resources + local Connector 骨架 | stdio 接入 WorkBuddy，工具可见 |
| P2 | 异步 `generate_firmware` + `get_build_status` + 编译产物 manifest | 提示词到 `.elf/.bin` 闭环，30 s 内返回 request_id |
| P3 | flash 四工具（默认禁用）+ cloud Connector + token 鉴权 | 本机烧录回读对拍通过；cloud 无硬件工具 |
| P4 | Web 路由切 service；SKILL.md 与提示词示例打磨 | e2e 全绿；Skill 能引导智能体一次成功 |

## 7. 护栏与验收

`backend/tests/test_mcp_contract.py`（沿用现有护栏风格）：

- 工具清单按**形态**分别钉死数量与名称；每个工具有 input schema。
- cloud 形态注册表里**不得出现**任何 flash/serial 工具；local 形态默认也不注册，
  只有 `HW2C_ENABLE_FLASH=1` 才出现。
- `generate_firmware` 必须在 1 s 内返回且结果含 `request_id`；同步返回完整产物的实现判失败。
- `artifact_manifest` 至少含 `.elf` 与 `.bin`，且 size/sha256 与磁盘文件一致。
- resource URI 覆盖六层 + 需求 + 格式真源；`schema/{layer}` 必须动态取。
- 安全：无 token → cloud MCP 不可达；路径穿越 → `E_FORBIDDEN`；超尺寸 → `E_TOO_LARGE`；
  原始 stderr 不得出现在任何响应；`compile_check` 只有三态。
- **变异验证是唯一验收标准**：至少 6 个变异体 —— 删 token 校验、删路径校验、
  恢复 stderr 透传、把 `compile_check` 固定为 `passed`、删掉一个 tool 注册、
  把异步生成改成同步等待。删掉判据后护栏必须红；仍全绿说明断言恒真，重写。

回归基线：`pytest tests parser/tests generator/tests -q`（当前 783 passed）、
hw2c-web `pytest backend/tests`（现有 e2e 必须保持通过）。

## 8. 明确排除

- 不建第三仓，不在 hw2c 主仓引入 FastAPI 依赖。
- 不改 hw2c core 的 `sys.exit` / logging / 生成器语义（本轮）。
- 远程形态不提供烧录/串口/上板工具 —— 物理上做不到。
- 不重复 codegraph 的代码索引能力；不暴露 `.workbuddy/` 内部记忆。
- 不做数据库与用户注册；cloud 鉴权只用静态 token 映射。多人共享前必须换成 OAuth。
- 不做"提示词直接改硬件 YAML 里的引脚"这类会绕过校验器的工具；所有配置必须过 validate。
