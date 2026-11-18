# 外部智能体访问（MCP Server / WorkBuddy Connector）实施规划

> 状态：规划定稿（第三版）；P1–P3 已实现，P4 待做。需求入口为 `docs/requirements.md` FR-19 / §5.6。
> 本文件只写"怎么做"，不重复需求条款。日期：2026-11-18。
> **范围：本机 stdio 单形态，不上架 Connector 市场，不暴露网络端口。**

## 1. 目标场景

用户在 WorkBuddy 中用自然语言提出需求 → WorkBuddy 经本机 Connector 调用 hw2c MCP 工具
→ hw2c 解析/校验/生成源码 → **编译出可执行固件** → 返回本机产物路径与校验值；
用户再提要求时，可按提示词烧录到接在这台机器上的板子。

闭环是"提示词 → 可执行固件（→ 可选烧录）"。全链路在用户自己的机器上运行，
不经过任何远程服务端。

## 2. 已定决策

| 决策项 | 选择 | 约束后果 |
|---|---|---|
| 主要消费者 | WorkBuddy 本机 Connector | 只需 `connector-meta.json` + `mcp.json` + 图标 + Skill；**不上架、不审核、不递增版本重审** |
| 传输 | 单一 `stdio` | 不监听端口；不需要 token、workspace 隔离、CORS、HTTPS |
| 能力边界 | 只读 + 校验 + 生成 + 编译 + 烧录（默认禁用） | 硬件与工具链本就在本机，烧录可以做；但必须有 confirm 闸门 |
| 代码落点 | hw2c-web 仓内的独立 Python 包 | 不建第三仓；MCP 是**独立 stdio 进程**，不挂在 FastAPI 上 |
| hw2c core | 不改，生成走子进程 | `sys.exit` / 全局 logging 用进程边界兜住；静默绿灯显式标注 |

### 2.1 硬约束：WorkBuddy 默认 30 秒超时

WorkBuddy 开放平台文档：MCP `mcp.json` 的 `timeout` 默认 `30000` ms，
"单次请求建议 30 秒内响应"。hw2c 生成 + 编译实测需要 60–180 秒（cmake configure /
build / 额外 target 三阶段，见 `generator/generate.py:829-926`）。

**结论：生成/编译必须做成异步任务。** 同步工具调用会被 WorkBuddy 掐断。
提高 `timeout` 不是解法——客户端体验与网关限制不受我们控制。

### 2.2 不上架带来的简化（以及不能简化的部分）

上一版按"双形态 + 上架"设计，下列内容**整体删除**：

- `hw2c-cloud` Connector 包、`token-schema.json`、`auth_mode: token`、
  `minWorkbuddyVersion`、Bearer token 映射、workspace 隔离、CORS 整改、市场审核流程。
- 工具注册不再分形态：只有一套工具，烧录四工具由 `HW2C_ENABLE_FLASH=1` 控制。

**但本机运行不等于无风险，以下一条都不能删**：

- 路径校验（`project_name` 不得含 `..`、产物必须落在工作根内）；
- 体积限制与超时（防止智能体一次塞进超大 YAML 或卡死生成子进程）；
- stderr 脱敏（原始编译错误可能含本机绝对路径与用户名）；
- `compile_check` 三态（工具链缺失时 hw2c 会静默报成功）；
- 烧录的 `confirm` 闸门与烧前回读对拍（智能体的提示词不等于二次确认）。

### 2.3 本机形态是优势，不再是限制

pyOCD + ST-Link/DAP-Link、COM5 串口、`arm-none-eabi-gcc` 都在用户机器上，
本机 stdio 进程可以直接访问。上一版"远程形态做不到烧录"的物理限制自然消失，
但仍不能让智能体无闸门地改板子——见 §5.1。

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
- 无认证、无多租户、无数据库。本机 MCP 不复用它的 HTTP 层，只复用/抽出 service。

### 3.3 WorkBuddy Connector 规范（查证于开放平台文档）

- 包结构：`connector-meta.json`（必需）、`mcp.json`（必需）、`icon.svg`（必需）、
  `skills/{name}/SKILL.md`（可选）。**`token-schema.json` 不需要**——本机 stdio 无 token。
- 传输 `stdio` 支持 `cwd`（v4.22.15+）与 `env`；`runtime` 托管目前只支持 Node，
  **Python 入口必须写解释器绝对路径 + `cwd`**，不能依赖托管运行时。
- 不上架时通过 WorkBuddy 的本地 Connector 入口安装；等价的裸配置也可写进
  `~/.workbuddy/mcp.json`，并在连接器页对该 Server 点"信任"后启用。
  前者带 Skill 引导，后者更轻量。以 Connector 包为主。

### 3.4 已有的 codegraph MCP

外部工具，不纳入本规划。它索引 Python 与 vendored C，`.j2` / `.ld` 索引数为 0。
模板语义与六层契约必须由本规划的 resources 补齐；其调用计数不可信，须 Grep 复核。

## 4. 目标结构

```text
hw2c-web/
├── backend/
│   ├── app.py                      现有；本机 MCP 不挂在它上面
│   ├── services/
│   │   ├── parse_service.py        新增：从 routes/parse.py 抽出
│   │   ├── validate_service.py     新增：包装 hw2c 纯函数
│   │   ├── generate_service.py     新增：异步生成 + 编译 + 产物管理（Web/MCP 共用）
│   │   └── flash_service.py        新增：pyOCD 烧录 + 回读校验
│   ├── mcp/
│   │   ├── server.py               新增：stdio 入口，工具/资源注册
│   │   ├── tools.py                工具实现
│   │   ├── resources.py            资源实现
│   │   └── jobs.py                 内存任务表 + 并发闸门
│   ├── routes/                     现有 Web 路由，后续瘦身为调 service
│   └── tests/test_mcp_contract.py  新增：契约 + 安全护栏（含变异验证）
└── connector/
    └── local/                      hw2c-local Connector 包（stdio，本地安装）
        ├── connector-meta.json
        ├── mcp.json                 command = Python 绝对路径，含 cwd/env
        ├── icon.svg
        └── skills/hw2c-firmware/SKILL.md
```

hw2c 主仓只改 `docs/requirements.md` 与本文档；**不改 `generator/`、`parser/`、`templates/`**。

## 5. 契约设计

### 5.1 工具集

| 工具 | 入参 | 出参 | 同步性 |
|---|---|---|---|
| `parse_eda` | `netlist_text`, `bom_text?`, `filename?` | 六层 YAML + `issues[]` | 同步（< 30 s） |
| `validate_config` | `layers` | `{ok, issues:[{severity,code,message,layer,path}]}` | 同步 |
| `list_examples` / `get_example` / `get_mcu_pinout` | 见需求 §5.6 | 同步 |
| `generate_firmware` | `layers`, `project_name?`, `options?` | `{request_id, status:"queued"}` | **异步，立即返回** |
| `get_build_status` | `request_id` | `{status, progress, compile_check, artifact_manifest}` | 同步 |
| `list_artifacts` | `request_id` | `[{name, size, sha256, uri}]` | 同步 |

**烧录四工具**（`HW2C_ENABLE_FLASH=1` 才注册，默认不注册）：

| 工具 | 入参 | 出参 |
|---|---|---|
| `list_probes` | 无 | pyOCD 探测到的调试器列表 |
| `flash_firmware` | `request_id`, `artifact`, `slot?`, `confirm` | 回读对拍结果 + 烧录结果 |
| `verify_flash` | `request_id`, `artifact`, `slot?` | 逐字节比对结果（只读，槽位可按向量表自动定位） |
| `capture_serial` | `port`, `seconds?`, `trigger?` | 带速率自检的串口文本（只读，不向设备写字节） |

`flash_firmware` 的安全闸门：`confirm` 必须为**字面 JSON 布尔 true**，否则只返回 dry-run
计划（目标地址、预计覆盖区、镜像 sha256）；先回读对拍再决定是否烧；带 bootloader 的工程默认烧
`combined.bin` 到 `0x08000000`；槽 B 需显式 `slot: "B"`（且必须单独按槽 B 基址链接，
见项目教训 15/22）。这些约束来自项目教训，工具描述里必须写清楚，不能指望智能体自己知道。

**SDK 会强转 `confirm`**：签名若写 `confirm: bool`，MCP 的 pydantic 参数校验会把字符串
`"true"` 变成 `True` 再进函数，闸门形同虚设（P3 实测）。因此签名故意不写 `bool`，
在函数内用 `confirm is not True` 判断，并用渲染级护栏钉死这个注解。

### 5.2 资源集

| URI | 内容 |
|---|---|
| `hw2c://requirements` / `hw2c://requirements/{section}` | 需求文档 |
| `hw2c://docs/user-guide/{layer}-yaml` | 六层配置指南 |
| `hw2c://schema/{layer}` | `Model.model_json_schema()` **动态生成** |
| `hw2c://examples/{name}/{layer}.yaml` | 示例配置 |
| `hw2c://format/{fota_format,ymodem_format}.json` | 格式真源 |
| `hw2c://models/{name}.yaml` | 器件模型 |
| `hw2c://artifacts/{request_id}/{filename}` | 生成产物（本机路径） |

不暴露 `.workbuddy/` 内部记忆与 `static/` 原始源码；产物资源只指向本次会话的工作根。

### 5.3 异步任务与编译产物

`generate_firmware` 立即返回 `request_id`；后台线程执行 `generate_service`：

1. subprocess 调 `parser.cli gen`，产物落在
   `<workspace>/<request_id>/<project_name>/`。**不能带 `--force`**：
   `generator/generate.py:1307` 会因此整个跳过编译自检；每次用全新目录即可。
2. 生成解释器与 MCP host **分开配**：本机 WorkBuddy 内置 Python 有 MCP SDK
   但没有 `jinja2`，直接拿它跑生成器必然 `ModuleNotFoundError`。由
   `HW2C_BUILD_PYTHON` 指定带 hw2c 依赖的解释器；`connector/local/install.py`
   探测后写进 mcp.json，不写死在仓库里。
3. hw2c 的编译自检在 staging 目录做，原子提交**不带 `build/`**
   ⇒ 生成成功后必须在输出目录再跑一次
   `cmake -B build -G Ninja -DCMAKE_TOOLCHAIN_FILE=toolchain.cmake` +
   `cmake --build build -j`，否则 manifest 恒为空、交付不了 `.elf/.bin`。
   带 bootloader 的工程要额外点名 `bootloader` / `app` / `combined`
   ——它们是 `add_custom_target`，不在默认目标里。
4. 从 `<project>/build/` **顶层**收集 `*.elf / *.bin / *.hex / *.map`，
   递归扫会捡到 CMake 的 `CMakeDetermineCompilerABI_*.bin` 探测文件。
   计算 size + sha256，生成 `artifact_manifest`。**这就是"可执行固件"的交付物**。
5. 任务状态机：`queued → running → succeeded | failed | cancelled`，
   存内存表（不做数据库），TTL 1 小时。WorkBuddy 侧每 2 s 轮询 `get_build_status`，上限 180 s。
6. 编译状态三态：`compile_check ∈ {passed, failed, skipped}`。MCP 侧自己探测
   cmake/ninja/arm-none-eabi-* 是否存在；缺任一即 `skipped` + warning，
   **不得**把 hw2c 的静默绿灯报成 `passed`。
7. 输出目录固定，禁止接受用户传入 output 路径或 `--force`；命中返回 `E_FORBIDDEN`。

### 5.4 加固项（本机仍必须）

- 体积：每层 YAML ≤ 64 KiB，总 ≤ 256 KiB；产物 ZIP ≤ 32 MiB → `E_TOO_LARGE`。
- 路径：`project_name` 必须 `Path(name).name == name` 且不含 `..`；`resolve()` 校验在根内。
- 超时：生成 subprocess 120 s；并发 semaphore=2；排队 30 s；超时 kill 进程组。
- 错误脱敏：原始 stderr 不回传；解析为 `GenerationIssue{code,message,location}`，
  单条 ≤ 200 字符、最多 20 条，原文只写本地日志。
- 不监听任何网络端口；stdio 仅由 WorkBuddy 启动。

### 5.5 Connector 包要点

- `connector-meta.json`：无需 `auth_mode` / `token-schema.json` / `minWorkbuddyVersion`。
- `mcp.json`：`type: "stdio"`，`command` 写本机 Python 绝对路径，
  `args: ["-m", "hw2c_connector.local"]`，`cwd` 指向 hw2c-web/backend，
  `env` 含 `HW2C_ROOT`、`HW2C_WORK_ROOT`、`HW2C_ENABLE_FLASH`。
  绝对路径因机器而异 ⇒ 安装时生成，不把某台机器的路径写进仓库文件。
- `SKILL.md` 必须逐一说明工具用途/参数/返回值，并给出提示词示例：
  "用 table 的六层配置生成一个 STM32G0B1 固件并编译" /
  "把上次生成的固件烧到板子的槽 B，烧之前先回读对拍"。
  模型主要靠工具描述决定是否调用，描述写糊了就不会被用。

### 5.5.1 P3 实施事实

- `services/flash_service.py` 只做纯规划与受控子进程：bin 镜像必须来自 manifest，
  sha256 与 size 二次核对；Reset_Handler 与**本次构建产物里的** `linker/*.ld`
  区间对账，不猜基址。`combined.bin` 故意跨 bootloader 与槽 A（实测 122668 B，
  bootloader 区只有 6144 B），按"必须装进单个区间"判会误拒，已按两区总上限放行。
- pyOCD 目标只由 `hardware.yaml::mcu.part` 映射到已验证的 `stm32g0b1retx` /
  `stm32g0b1vetx`；未知型号直接拒绝，不 "自动检测"。
- 回读走 `services/pyocd_readback.py`（pyOCD Python API + `read_memory_block8`），
  因为 CLI `savemem` 在本机对任何路径都报 invalid argument（教训 13）。
- 写入走 `pyocd load -e sector -a <base> --no-reset`，**不整片擦除**：槽 B 写入不能
  波及引导器/槽 A；写完保持停机，不给串口监听制造竞态。
- 本机 Connector 的 `timeout` 从默认 30 s 提到 180000 ms：全片回读 + 写 + 再回读
  无法塞进 30 s。生成仍然异步，不靠放宽超时解决（§2.1 不变）。
- 真机状态（2026-11-18）：stdio 闭环已拿到 12 工具、探针 UID、fota_demo 六层生成
  （`compile_check=passed`，manifest 含 `combined.bin`）、dry-run 计划与字符串确认
  拒绝；回读阶段 ST-Link 枚举正常但 SWD 报 `Get IDCODE error`（1 MHz 同样失败），
  属硬件链路问题，待板子断电重插后补做写前/写后对拍。

## 6. 实施阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 本文档 + FR-19 / §5.6 / §8 / §9 / §10（本轮） | 需求评审通过 |
| P1 | service 抽取 + 只读工具 + 全部 resources + local Connector 骨架 | stdio 接入 WorkBuddy，工具可见 |
| P2 | 异步 `generate_firmware` + `get_build_status` + 编译产物 manifest | 提示词到 `.elf/.bin` 闭环，30 s 内返回 request_id |
| P3 | flash 四工具（默认禁用）+ 真机烧录回读对拍 | 契约 33/33、变异 33/33、stdio dry-run 已过；回读待 SWD 硬件恢复 |
| P4 | Web 路由切 service；SKILL.md 与提示词示例打磨 | e2e 全绿；Skill 能引导智能体一次成功 |

## 7. 护栏与验收

`backend/tests/test_mcp_contract.py`（沿用现有护栏风格）：

- 工具清单整体钉死数量与名称；每个工具有 input schema。
- 烧录四工具默认不注册，只有 `HW2C_ENABLE_FLASH=1` 才出现。
- `generate_firmware` 必须在 1 s 内返回且结果含 `request_id`；同步返回完整产物的实现判失败。
- `artifact_manifest` 至少含 `.elf` 与 `.bin`，且 size/sha256 与磁盘文件一致。
- resource URI 覆盖六层 + 需求 + 格式真源；`schema/{layer}` 必须动态取。
- 安全：路径穿越 → `E_FORBIDDEN`；超尺寸 → `E_TOO_LARGE`；原始 stderr 不得出现在任何响应；
  `compile_check` 只有三态；未确认的 `flash_firmware` 只返回 dry-run 且不调用 pyOCD。
- **变异验证是唯一验收标准**：至少 6 个变异体 —— 删路径校验、恢复 stderr 透传、
  把 `compile_check` 固定为 `passed`、删掉一个 tool 注册、把异步生成改成同步等待、
  删掉 `confirm` 闸门。删掉判据后护栏必须红；仍全绿说明断言恒真，重写。

回归基线：`pytest tests parser/tests generator/tests -q`（当前 783 passed）、
hw2c-web `pytest backend/tests`（现有 e2e 必须保持通过）。

## 8. 明确排除

- 不建第三仓，不在 hw2c 主仓引入 FastAPI 依赖。
- 不改 hw2c core 的 `sys.exit` / logging / 生成器语义（本轮）。
- **不上架 Connector 市场、不做云端形态、不引入 token 鉴权或网络监听**。
- 不重复 codegraph 的代码索引能力；不暴露 `.workbuddy/` 内部记忆。
- 不做数据库与用户注册。
- 不做"提示词直接改硬件 YAML 里的引脚"这类会绕过校验器的工具；所有配置必须过 validate。
