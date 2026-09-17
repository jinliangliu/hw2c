# hw2c 需求规格说明（Requirements Specification）

> **文档位置**：`docs/requirements.md`（本仓库）
> **基线**：`main` @ `ff939e1`（v0.4.0）
> **更新日期**：2026-09-15
> **说明**：本文档是对本仓库现有文档（README / AGENTS.md / docs/user-guide / docs/plans / docs/roadmap）的**需求提炼与归一**，作为需求的唯一入口。不做需求发明，所有条目均可追溯到源文件（见 §10 追溯矩阵）。
> **维护约定**：需求变更先更新本文档，再落到实现。

**状态图例**：✅ 已实现并验证 ｜ ⊙ 已实现未完整验证 ｜ ⏳ 规划中 / 未开始 ｜ ⛔ 明确不做

---

## 1. 项目背景与目标

### 1.1 项目定位

**Hardware2Code (hw2c)**：从 EDA 设计文件（Netlist / BOM）自动生成**可编译**嵌入式固件的通用代码生成平台。

一句话流程：**上传网表和 BOM → 拖拽编排任务与绑定 → 下载 `arm-none-eabi-gcc` 可直接编译的嵌入式工程。**

### 1.2 核心设计主张

**软硬件分离** —— 硬件描述层（YAML）与具体 MCU 解耦。换芯片只改硬件配置、重新生成，业务逻辑零改动。平台不绑定单一芯片。

### 1.3 解决的痛点

| 痛点 | 传统方式 | hw2c 方案 |
|------|----------|-----------|
| 硬件→软件信息断层 | 手动对照原理图写 init 代码 | 直接解析 Netlist/BOM，自动映射引脚-外设 |
| 驱动代码重复编写 | 每项目复制粘贴 GPIO/UART/SPI 模板 | 外设模型库驱动，按需渲染代码 |
| 业务逻辑调试低效 | 无状态机框架，if-else 嵌套失控 | 层级状态机 DSL，可视化编排，自动生成 C |
| 测试依赖硬件 | 烧录 → 看现象 → 改代码循环 | PC 端 Unity 单元测试 + Mock HAL，脱离硬件验证 |
| 硬件变更→软件适配 | 改原理图后手动改代码 | 重新生成，diff 仅业务逻辑 |

### 1.4 目标用户

- 嵌入式工程师（需要从原理图快速起工程，且不想手写外设初始化）
- 硬件工程师（能画原理图但不想写驱动）
- 教学 / 原型验证场景（需要在无硬件条件下验证业务逻辑）

---

## 2. 范围

### 2.1 当前范围（v0.4.0，已交付）

| 能力域 | 范围 |
|--------|------|
| 验证 MCU | STM32G0B1RE / B1VE（Cortex-M0+，512 KB Flash，144 KB RAM） |
| EDA 输入 | EasyEDA Pro `.enet`、KiCad 6+ S-Expression、KiCad Legacy XML + CSV BOM |
| 硬件描述 | 六层 YAML，Pydantic 类型安全校验 |
| 代码生成 | 123~158 个 Jinja2 模板 → 完整 CMake + Ninja 工程 |
| 运行时 | FreeRTOS + 组件框架 + 事件队列 + 层级状态机 + POSIX 风格总线 API |
| 外设覆盖 | GPIO/EXTI、USART、I2C、SPI、ADC、PWM、RTC、IWDG、RS485、红外、EEPROM、温度传感器、Modbus、MQTT、4G |
| 高级能力 | 低功耗（RUN/SLEEP/STOP0/STOP1）、Bootloader A/B、FOTA 差分升级（帧协议 + YMODEM 双通道）、CLI 12 命令、遥测、FOC 电机控制、PID 过程变量控制 |
| 测试 | 主机侧 Unity + Mock HAL、SIL 组件仿真、Python 侧 270+ 测试 |
| 工具链 | arm-none-eabi-gcc + CMake + Ninja，VSCode 配置生成，ST-Link / DAP-Link 烧录 |

### 2.2 明确不做（Out of Scope）

- 不支持 Altium Designer、OrCAD / Cadence 网表 ⛔
- 不支持 IAR / Keil MDK 编译链 ⛔
- 不支持 `.xlsx` BOM 或 EasyEDA 原生 BOM 格式（仅 CSV）⛔
- 状态机不支持多层深嵌套（仅一层复合状态）、不支持 Choice Point 与 Fork/Join ⛔
- 不引入 TypeScript 前端类型生成，保留 YAML 声明式设计 ⛔

### 2.3 扩展范围（未来）

多 MCU 后端（ESP32 / NXP / STM32F4 / H7）、Zephyr RTOS 后端、Web 可视化配置台成熟化、MISRA C 合规、插件市场、VSCode 扩展。

---

## 3. 功能需求（FR）

### FR-1 硬件设计导入 ✅

| ID | 需求 | 状态 |
|----|------|------|
| FR-1.1 | 支持 EasyEDA Pro `.enet` JSON v2.0 网表解析 | ✅ |
| FR-1.2 | 支持 KiCad Legacy `.net` XML（`<export version="D">`）解析 | ✅ |
| FR-1.3 | 支持 KiCad 6+ S-Expression（`.kicad_net`）解析 | ✅ |
| FR-1.4 | 三种网表格式**自动检测** | ✅ |
| FR-1.5 | CSV BOM 解析，需含 `Designator` / `Value` / `Footprint` 三列 | ✅ |
| FR-1.6 | 80+ 类外设芯片启发式匹配（I2C 传感器、SPI Flash、RS485、4G、WiFi/BT、GPS、CAN、电机驱动等） | ✅ |
| FR-1.7 | 从网表提取无源元件约束（阻容 / 晶振 / 连接器 / 稳压器） | ✅ |
| FR-1.8 | 原理图注解导入：从网络命名约定提取总线分配（SPI/I2C/UART/CAN/SWD）、外设分组、电源域、信号角色 | ✅ |
| FR-1.9 | 统一管线 `parser/pipeline.py`：Netlist + BOM + Passive + Annotator + Validator → enriched YAML | ✅ |
| FR-1.10 | Netlist vs YAML 交叉校验四级：MCU 匹配 → 引脚冲突 → 引脚缺失/多余 → 外设类型匹配 | ✅ |

### FR-2 硬件描述层 `hardware.yaml` ✅

| ID | 需求 |
|----|------|
| FR-2.1 | MCU 配置：`part` / `core` / `clock_source`(HSI/HSE) / `clock_freq_hz` / `core_clock_mhz` / `ram_kb` / `flash_kb` / `dual_bank` / `hse_freq` |
| FR-2.2 | 引脚配置：`id` / `function` / `label` / `active_level` / `pull` / `af` / `exti{enable,trigger}` |
| FR-2.3 | 外设配置：`name` / `type` / `bus` / `interface` / `clock_source` / `features` / `bearer` / `broker` / `extra` |
| FR-2.4 | 低功耗配置：`sleep.mode`（SLEEP/STOP0/STOP1/STOP2/STANDBY） |
| FR-2.5 | 时钟树配置：HSI/LSI/HSE/LSE/PLL/SYSCLK/APB/FreeRTOS tick 来源 |
| FR-2.6 | Bootloader 配置：`enabled` / `size_kb`(4–32) / `app_a_offset` / `app_b_offset` / `wdg_timeout_ms` / `max_retries`(1–10) |
| FR-2.7 | HIL 测试配置：`baudrate` / `uart` |
| FR-2.8 | 堆栈配置：`heap_size` / `stack_size` |
| FR-2.9 | 所有 GPIO/AF/IRQ 从 hardware.yaml 派生，**无硬编码** |

### FR-3 软件行为层 `task.yaml` + 业务 DSL ✅

| ID | 需求 |
|----|------|
| FR-3.1 | 工程元数据：`project.name` / `project.version` / `heap_size` |
| FR-3.2 | FreeRTOS 任务定义：`name` / `priority`(0–31) / `stack_size`（单次运行行为由触发条件隐式表达） |
| FR-3.3 | 层级状态机：`behavior.initial_state` / `states` / `transitions{event,target,actions}` |
| FR-3.4 | 复合子状态（`state.states`，**仅一层嵌套**） |
| FR-3.5 | 并行区域（`behavior.regions`，各自 `initial_state`） |
| FR-3.6 | 历史状态 |
| FR-3.7 | 状态引用复用（`ref` 引用子流程文件） |
| FR-3.8 | 状态动作：`entry` / `exit` / 转移 `actions` |
| FR-3.9 | 动作类型：内置动作、变量动作、定时器动作（defer / timeline）、事件动作（publish / publish_async / send_to）、条件动作（when） |
| FR-3.10 | 事件体系：内置事件、RTC 自动生成事件、定时器自动生成事件、自定义事件 |
| FR-3.11 | 周期事件：`periodic_events[{event, actions}]` |
| FR-3.12 | 自定义类型：struct（最多 2 层嵌套）/ enum / union / bitfield(1–32 位) |
| FR-3.13 | 变量声明：名称 / C 类型或自定义类型 / 数组 / 初值 |
| FR-3.14 | 跨区域通信：`send_to` + `publish_async` |
| FR-3.15 | guard 条件表达式原样嵌入 C（编译期报错），生成期做变量引用校验 |

### FR-4 接线 / 绑定层 `bind.yaml` ✅

| ID | 需求 |
|----|------|
| FR-4.1 | 中断绑定：`interrupt[{pin, component/task, event}]` |
| FR-4.2 | 外设归属绑定：`peripheral_assign[{peripheral, task, role}]`，role 含 `tick_timer` / `cli_uart` / `storage` 等约定 |
| FR-4.3 | 任务间路由：`routing[{from, to, signal, condition}]` |
| FR-4.4 | bind 中引用的 event / signal 必须与 task.yaml 的事件声明对齐（校验器强制） |
| FR-4.5 | 硬件/软件解耦：`hardware.yaml` 不再承载 `notify_task`，`app_tasks` 不再承载 `triggers` / `signals` / `run_mode` |

### FR-5 组件、参数与主题层 ✅

| ID | 需求 |
|----|------|
| FR-5.1 | `components.yaml`：注册组件 `name` / `type` / `driver` / `period_ms` / `sleep_compat` / `config` / `priority` / `stack_size` |
| FR-5.2 | 组件生命周期契约：`{name}_init(component_t*, void* cfg)` / `{name}_step(component_t*)` / `{name}_terminate(component_t*)`，由框架按 `period_ms` 周期调度 |
| FR-5.3 | `params.yaml`：运行时参数 `name` / `component` / `type` / `default` / `min` / `max` / `description`，可由 CLI 动态 get/set |
| FR-5.4 | `pubsub.yaml`：主题 `name` / `description` / `value{type,unit}`，生成 `TOPIC_<name>` 枚举 |
| FR-5.5 | 组件间通过 `component_bus` 发布/订阅解耦，不直接互相调用 |

### FR-6 校验体系 ✅

| ID | 需求 |
|----|------|
| FR-6.1 | 第一层 Pydantic schema 校验：类型安全、必填字段、重复 `pin id` 拦截 |
| FR-6.2 | 第二层 MCU 数据库交叉校验：引脚存在性 / AF 支持性 / 同引脚冲突，并给出替代引脚建议 |
| FR-6.3 | 第三层外设字段引脚引用校验：`cs_pin` / `de_pin` 必须声明且不得被多外设共享（UART↔RS485 DE 伴侣除外） |
| FR-6.4 | 第四层业务校验：DSL 事件生产者闭包（每个被消费的 event 必须有生产者）、topic 契约校验、动作/guard/when/calc 表达式校验 |
| FR-6.5 | 校验失败必须在**生成阶段**报错并定位，而非烧录后暴露 |

### FR-7 引脚自动分配 ✅

| ID | 需求 |
|----|------|
| FR-7.1 | 按外设需求自动分配引脚 |
| FR-7.2 | 自动分配时排除已用引脚与 SWD 调试引脚 |

### FR-8 代码生成引擎 ✅

| ID | 需求 |
|----|------|
| FR-8.1 | 六层 YAML 由 `mapper.py` 合并为统一渲染上下文；旧单体 YAML 由 `split_legacy()` 自动拆分，**上游零改动** |
| FR-8.2 | Jinja2 模板渲染全部 `.c` / `.h` / CMake / 链接脚本 / VSCode 配置 |
| FR-8.3 | 生成采用**暂存 + 原子替换**（`_atomic_commit`），避免半成品工程 |
| FR-8.4 | 基于 libcst 的 AST 合并保留 `USER CODE` 块，用户手写代码不丢失 |
| FR-8.5 | 生成后内置编译自检（`_run_compile_check`） |
| FR-8.6 | 生成日志（`generation.log`）记录生成元信息 |
| FR-8.7 | `--force` 原子替换 output 并清理 `build/` |
| FR-8.8 | 支持 `--dry-run` / `--diff` 预览 |
| FR-8.9 | 幂等：每次生成时对 vendor 侧（FreeRTOS ARMv6-M port）打幂等补丁 |

### FR-9 运行时框架 ✅

| ID | 需求 |
|----|------|
| FR-9.1 | 组件注册表：统一 `init` / `step` / `terminate` 生命周期，周期调度 |
| FR-9.2 | 事件队列：ISR → `event_queue` → 集中分发任务 → 状态机 |
| FR-9.3 | `component_bus`：发布/订阅事件总线 |
| FR-9.4 | `param_registry`：运行时参数注册表 |
| FR-9.5 | POSIX 风格总线 API：`uart_api` / `i2c_api` / `spi_api` / `gpio_api` / `adc_api`，一总线多设备（I2C 按 7 位地址、SPI 按 CS 引脚区分） |
| FR-9.6 | 状态机引擎：层级状态、并行区域、历史状态、守卫条件、after 超时 |
| FR-9.7 | I2C 总线繁忙防护（PE 循环复位），无设备时不挂死，优雅降级 |
| FR-9.8 | `power_mgr`：按组件级 `sleep_compat` 动态选择睡眠深度 |

### FR-10 外设驱动覆盖 ✅

| 需求 | 模板 | 单测 |
|------|------|:--:|
| GPIO + EXTI | `gpio.c` | ✅ |
| USART | `drv_uart.c` / `drv_log.c` | ✅ |
| I2C 总线抽象 | `i2c_api.c` | ✅ |
| SPI 总线抽象 | `spi_api.c` | ✅ |
| IMU（I2C/SPI 参数化） | `drv_mpu6050.c` | ✅ |
| I2C EEPROM | `drv_eeprom.c` | ✅ |
| SPI NOR Flash W25Q32 | `drv_spi_flash.c` | ✅ |
| ADC | `drv_adc.c` | ✅ |
| 内部温度传感器 | `drv_temp_sensor.c` | — |
| NTC 热敏电阻 | `drv_ntc.c` | ✅ |
| I2C 压力传感器 | `drv_pressure.c` | ✅ |
| PWM 多通道（定时器多路、逐路占空比） | `drv_pwm.c` | ✅ |
| RTC（1 Hz 心跳 + 10 路定时器） | `drv_rtc.c` | ✅ |
| IWDG | `drv_iwdg.c` | ✅ |
| RS485（`uart_api` 的 `rs485_de_pin` 通用特性） | — | ✅ |
| 红外 NEC/SIR | `drv_ir.c` | ✅ |
| Cellular 4G Cat.1 | `drv_cellular.c` | ✅ |
| MQTT 3.1.1 | `drv_mqtt.c` | ✅ |
| Modbus RTU 主/从（FC03/06/16 + CRC16 + 异常码） | `drv_modbus.c` | ✅ |
| CLI 调试终端 | `drv_cli.c` | ✅ |
| FOTA 差分升级（帧协议 + YMODEM 两通道） | `drv_fota.c`（接收侧状态机）+ `fota_delta.c`（解码侧）+ `drv_fota_ymodem.c`（YMODEM 通道） | ✅ |
| Bootloader A/B | `boot_*.c` | ✅ |

### FR-11 通信协议栈 ✅

| ID | 需求 |
|----|------|
| FR-11.1 | Modbus RTU 主/从双角色，FC03/06/16，CRC16，异常码，ISR 环形缓冲 RX |
| FR-11.2 | 传输可切换：USB-TTL 直连（USART1 默认）/ RS485 半双工（DE 引脚） |
| FR-11.3 | MQTT 3.1.1 客户端 |
| FR-11.4 | Cellular 4G（AT 指令 + PDP 拨号） |
| FR-11.5 | 提供主/从对测脚本 `modbus_tool.py` |

### FR-12 控制类中间件 ✅

| ID | 需求 |
|----|------|
| FR-12.1 | 通用过程变量 PID 中间件（`pid_math` 纯算法 + `pid_ctrl` 可选装组件），压力/温度/流量通用 |
| FR-12.2 | PID 自整定：继电振荡法（Ziegler-Nichols）与开环阶跃响应法 |
| FR-12.3 | FOC 电机控制：TIM1 直驱三相互补 PWM + ADC 电流采样 + I2C 磁编码器；Clarke/Park/逆 Park/中心化 SVPWM/PI（抗饱和）纯 C 可测 |
| FR-12.4 | 模式：力矩 / 速度 / 位置 |
| FR-12.5 | 力觉旋钮 Knob：力矩 = −阻尼×角速度 − 摩擦×方向 |
| FR-12.6 | 摔倒监测：自由落体 → 撞击 → 静止三阶段，阈值运行时可调，直接消费 IMU 组件数据 |
| FR-12.7 | IMU 姿态解算：互补滤波（加速度计静态参考 + 陀螺仪积分，1g 信任窗口抑制线性加速度干扰） |

### FR-13 低功耗与 RTC ✅

| ID | 需求 |
|----|------|
| FR-13.1 | 睡眠模式 RUN / SLEEP / STOP0 / STOP1 / STOP2 / STANDBY |
| FR-13.2 | RTC(LSE) 保持计时、RAM 保持、UART 起始位唤醒，唤醒后系统时间无偏差 |
| FR-13.3 | Tickless Idle |
| FR-13.4 | RTC 1 Hz 唤醒心跳 + 10 路定时器（秒/分/小时周期 + 毫秒单次） |
| FR-13.5 | RTC ISR 最高优先级，保证 STOP 模式可靠唤醒 |

### FR-14 Bootloader 与 FOTA ⏳

> 状态说明（2026-09-16 更正）：此前标 ✅ 与事实不符 —— 当时没有任何示例开启
> `bootloader.enabled`，整条引导/差分路径**从未被生成、编译或测试**。现已由
> `examples/fota_demo/` 拉进构建闸门：四个编译自检目标全通过，bootloader 主机
> 单测（crc/nvm/jump）可运行，差分应用层有 L6 掉电注入（含变异测试）。
> 仍缺：**真板 HIL 验证（P5）** —— 接收侧状态机（`drv_fota`，P3）与其 YMODEM 通道（P3'，
> FR-14.6）已实现，主机 L5 跨实现台架（真模板 + 主机 gcc + vendored 解码器 + Python 发送器
> 互喂字节）全绿，但"主机全绿 ≠ 真机可用"这类缺陷（A3/A9）只有上板才能排除。
> 详见 `docs/plans/differential-ota.md` §17、§18 与 §19（YMODEM 通道）。

| ID | 需求 |
|----|------|
| FR-14.1 | 双槽位 A/B Bootloader，硬件 CRC32 校验 |
| FR-14.2 | TAMP 备份寄存器记录启动状态 |
| FR-14.3 | 启动失败自动回退（`max_retries`） |
| FR-14.4 | 差分 FOTA：H2CD v1 信封 + HPatchLite 兼容 lite 流 + tinyuz 压缩（`delta_tool.py` 自研写侧），减小 OTA 传输体积，完整性校验 + 幂等重放 |
| FR-14.5 | 接收侧传输状态机：帧协议（START/DATA/END/ABORT，带序号与重传）驱动同一个 staging 会话；掉电续传、幂等重放、越界拒绝 |
| FR-14.6 | **YMODEM 传输通道**：CLI `fota ymodem` 进入接收态后，任何终端软件（Tera Term / SecureCRT / lrzsz `sb`）用**内置的 YMODEM 发送功能**即可完成升级，不需要专用上位机；与帧协议共用同一个 staging 会话与准入判据，不引入第二套 OTA 流程 |

### FR-15 调试与可观测性 ✅

| ID | 需求 |
|----|------|
| FR-15.1 | 交互式 CLI，内置 12 命令：`help` / `version` / `uptime` / `free` / `tasks` / `reset` / `gpio` / `led` / `rtc` / `telemetry` / `power` / `sysinfo`；示例启用 FOTA 接收时追加第 13 个命令 `fota`（`status` / `progress` / `recv` / `ymodem` / `apply` / `erase`，见 FR-14.5 / FR-14.6） |
| FR-15.2 | CLI 在 STOP 模式下可通过 UART 唤醒交互 |
| FR-15.3 | 遥测：单块时间戳快照（心跳 / 栈水位 / 堆 / 组件健康），`telemetry on/off` 开关 |
| FR-15.4 | 日志：环形缓冲区 + 中断驱动 USART TX，ISR 安全、零阻塞 |
| FR-15.5 | 运行时可调参数经 CLI `param get/set` |

### FR-16 测试体系 ✅

| ID | 需求 |
|----|------|
| FR-16.1 | Unity + Mock HAL 主机侧单元测试，**脱离硬件**，`python test/run_tests.py` 一键运行 |
| FR-16.2 | Mock HAL 覆盖全部 STM32 外设寄存器级模拟（含 CRC/PWR/RCC/TAMP/SCB/NVIC/SysTick） |
| FR-16.3 | SIL 组件级仿真测试（`test/sil`） |
| FR-16.4 | 闭环 SIL：压力罐模型、热质量模型 |
| FR-16.5 | 生成器/解析器 Python 单元测试 270+ 项 |
| FR-16.6 | HIL 测试框架（UART 回环，部分外设） |

### FR-17 工具链与 IDE 集成 ✅

| ID | 需求 |
|----|------|
| FR-17.1 | 生成可直接构建的 CMake 工程（含 `toolchain.cmake`） |
| FR-17.2 | 自动生成 VSCode `launch.json` / `tasks.json`（Cortex-Debug） |
| FR-17.3 | 烧录目标：`flash-daplink`（OpenOCD / CMSIS-DAP）、ST-Link |
| FR-17.4 | 提供 `hw2c parse` / `hw2c gen` 命令行入口 |
| FR-17.5 | CI：生成 → 编译 → 主机测试 → SIL → 文档发布 |

### FR-18 可视化配置台 ⏳

| ID | 需求 | 状态 |
|----|------|------|
| FR-18.1 | Web 端 YAML 编辑器 + 双向绑定 | ⊙ 仅支持 legacy 单体格式预览 |
| FR-18.2 | 外设配置面板（I2C 地址 / SPI 模式 / UART 波特率等约束化输入） | ⏳ |
| FR-18.3 | 引脚分配可视化（引脚封装预览） | ⏳ |
| FR-18.4 | 状态机可视化编辑器 + timeline 时间轴 | ⏳ |
| FR-18.5 | 任务分配面板 + BindGraph 拖拽绑定（引脚→任务、任务→任务） | ⏳ |
| FR-18.6 | 时钟树配置 | ⏳ |
| FR-18.7 | 实时 YAML 预览与 diff，可视化操作与手写 YAML 可切换 | ⏳ |
| FR-18.8 | 解析进度可视化反馈（当前为 WebSocket 单次推送最终结果） | ⏳ |

---

## 4. 非功能需求（NFR）

| ID | 需求 | 验证方式 |
|----|------|----------|
| NFR-1 | **可移植性**：硬件描述与 MCU 解耦，换芯片只改 `hardware.yaml`，业务 YAML 零改动 | 多后端生成对比 |
| NFR-2 | **零手工改动可编译**：生成工程在 `arm-none-eabi-gcc` + CMake + Ninja 下直接构建成功 | CI Build & Test |
| NFR-3 | **代码质量目标**：MISRA C:2012 Mandatory 规则通过；单函数 ≤ 100 行，圈复杂度 ≤ 10（Phase 6 目标） | 静态分析集成 |
| NFR-4 | **资源约束**：适配 Cortex-M0+（无 FPU、不支持非对齐访问）；Flash 512 KB / RAM 144 KB；栈水位可由遥测观测 | 遥测快照 |
| NFR-5 | **可测试性**：全部驱动与业务逻辑可在 PC 端脱离硬件验证 | `python test/run_tests.py` |
| NFR-6 | **向后兼容**：老格式单体 YAML 继续可用；弃用时间线 v1.0 WARNING → v1.1 ERROR 但可用 → v1.2 移除 | 兼容性测试 |
| NFR-7 | **可扩展性**：新增外设 = 模型 YAML + 驱动模板 + 测试模板 + 注册；新增 MCU = MCU JSON + 后端实现 + 注册 | 开发指南步骤 |
| NFR-8 | **生成幂等性**：同一输入多次生成结果一致；半成品不会污染输出（原子替换） | 重复生成 diff |
| NFR-9 | **许可证合规**：全部依赖为 MIT / Apache-2.0 / BSD-3-Clause，无 GPL/LGPL 传染性许可证，允许商业闭源 | 依赖清单审查 |
| NFR-10 | **中断安全**：日志与 CLI 输入使用无锁环形缓冲，ISR 内不阻塞 | 代码审查 |
| NFR-11 | **无硬编码配置**：时钟源、波特率、GPIO/AF/IRQ 全部从 YAML 派生 | 模板审查 |
| NFR-12 | **子模块可复现**：vendor 子模块 gitlink 必须指向上游可获取 commit，禁止指向本地 commit（否则 CI checkout 失败）；本地修复以父仓库幂等补丁实现 | CI 通过 |

---

## 5. 接口与契约

### 5.1 命令行（Python 侧）

```bash
# 解析网表/BOM → YAML
hw2c parse ...

# 生成工程
python -m generator.generate -i examples/<demo>/hardware.yaml -o output/<demo> --force \
  --task examples/<demo>/task.yaml \
  --components examples/<demo>/components.yaml \
  --bind examples/<demo>/bind.yaml \
  --params examples/<demo>/params.yaml \
  --pubsub examples/<demo>/pubsub.yaml
```

### 5.2 外设模型 YAML schema

```yaml
model: "MPU6050"                  # 芯片型号
type: "I2C_Sensor"                # 外设类型
interface: "I2C"                  # 传输接口（驱动模板按此参数化）
address: 0x68                     # I2C 7 位地址
driver_template: "drivers/drv_mpu6050.c.j2"
header_template: "drivers/drv_mpu6050.h.j2"
whoami: 0x68                      # 存在性探测寄存器值
init_sequence:                    # 初始化序列
  - write_register: [0x6B, 0x00]
registers: { ... }                # 寄存器地址表
capabilities: [accelerometer_read, gyroscope_read]
wakeup_capable: false             # 是否可作唤醒源
default_params: { accel_fs: 2, gyro_fs: 250, sample_rate_div: 0 }
extra_schema:                     # 用户可配参数的约束
  accel_fs: { type: int, required: false, default: 2, values: [2,4,8,16] }
```

### 5.3 组件生命周期契约

```c
int  {name}_init(component_t *c, void *cfg);   // 返回 0 表示成功
void {name}_step(component_t *c);              // 周期处理，周期由 period_ms 决定
void {name}_terminate(component_t *c);         // 清理
```

### 5.4 状态机 DSL 最小示例

```yaml
behavior:
  initial_state: IDLE
  states:
    - name: IDLE
      transitions:
        - { event: BTN_PRESS, target: ACTIVE }
    - name: ACTIVE
      entry: set(led, on)
      exit:  set(led, off)
      transitions:
        - { event: BTN_RELEASE, target: IDLE }
```

### 5.5 生成产物结构

```
output/<demo>/
├── CMakeLists.txt / toolchain.cmake
├── config/          FreeRTOSConfig.h / stm32g0xx_hal_conf.h
├── linker/          链接脚本
├── src/             main.c / event_mgr / statemachine / component_* / param_registry / sleep / it
│   └── drivers/     按需生成的驱动
├── test/            mock_hal + Unity 单测 + run_tests.py + sil/
├── .vscode/         launch.json / tasks.json
└── generation.log
```

---

## 6. 约束与依赖

| 类别 | 约束 |
|------|------|
| MCU | 仅 STM32G0B1RE / B1VE 完整验证；BOM 可识别 AT32/GD32 但缺 MCU 数据库 JSON |
| 内核限制 | Cortex-M0+ 无 FPU，不支持非对齐内存访问 |
| 编译链 | 仅 `arm-none-eabi-gcc` + CMake(≥3.20) + Ninja |
| Python | 3.10+；PyYAML ≥6.0、Jinja2 ≥3.0、Pydantic ≥2.0、libcst ≥1.0.0、Click ≥8.1.0、pytest ≥7.0 |
| 系统工具 | arm-none-eabi-gcc、CMake、Ninja、OpenOCD（可选） |
| 硬件调试 | CMSIS-DAP (DAP-Link) / ST-Link |
| 本机环境（开发机） | Python `C:/Users/pc/anaconda3/python.exe`；工具链 `C:/Arm/mingw-w64-i686-arm-none-eabi/bin/`、`C:/mingw64/bin/`；OpenOCD `C:/Arm/openocd-cb52502-i686-w64-mingw32/bin/openocd.exe`；串口 COM4 |
| **供应商源码（强制）** | **`static/stm32g0/` 下的 ST 官方 HAL、CMSIS 与 FreeRTOS 内核「系统代码」一律不得修改**；仅允许修改**配置文件**（`FreeRTOSConfig.h`、`stm32g0xx_hal_conf.h` 等）与**中断向量/回调类文件**（`stm32g0xx_it.c`）。生成器、模板、补丁脚本、构建脚本均不得写入这些目录 |

### 6.1 供应商源码只读约束（Vendor Source Read-Only）

**规则**：STM32G0 官方 HAL、CMSIS 与 FreeRTOS 内核的系统代码是**上游制品**，hw2c 只做「使用者」，不做「维护者」。

- **禁止**：任何形式的就地改写（生成期补丁、sed/replace、submodule 内本地 commit）。
- **允许**：修改**配置文件**（`FreeRTOSConfig.h`、`stm32g0xx_hal_conf.h`、链接脚本由 hw2c 自有的 `templates/linker/` 生成，不属于 vendor）以及**中断向量/回调文件**（`stm32g0xx_it.c`）。
- **例外通道**：若上游确实存在影响功能的缺陷，只有两条合规路径：
  1. **生成代码侧适配**——由 hw2c 自己产出的 `.c/.h`（`templates/src/`、`templates/drivers/`）承担规避，例如调整初始化顺序、显式修复中断状态；
  2. **升级 submodule 指针**——指向修好该缺陷的上游 commit（gitlink 必须始终指向上游可获取的 commit）。
  禁止把「本地补丁」当作第三条通道。

**已验证的合规案例（FR-9 运行时框架）**：FreeRTOS ARMv6-M 端口
`portable/GCC/ARM_CM0/port.c` 把 `ulCriticalNesting` 初始化为毒值 `0xAAAAAAAA`，
且上游至 `78069a79e`（2026-07-16）仍未修复。后果：`xPortStartScheduler()`
之前的第一次 `taskEXIT_CRITICAL()` 递减后仍非 0，永远走不到恢复 PRIMASK 的分支，
**中断在调度器启动前一直处于屏蔽状态**（`HAL_GetTick()` 依赖 TIM14 中断 → 冻结）。

hw2c 的处理方式（**不修改 vendor**）：

- `main.c` 模板把**所有**调度器启动前的 RTOS 对象创建
  （`EventMgr_Init()` 的 `xQueueCreate`、`cli_init()` 的 `xSemaphoreCreateBinary`、
  `telemetry_init()` 与各 `xTaskCreate`）收敛到**紧邻 `vTaskStartScheduler()` 之前的最后一个代码块**；
- 该块**不得**包含任何依赖 tick 或依赖中断的调用——所有
  `HAL_GetTick()` 等待（I2C 注册与探测、SPI Flash 忙等、IWDG 初始化、
  `component_init_all()` 的器件探测）与中断驱动的日志都排在它**之前**；
- `EventMgr_Init()` 因须先于 `RTC_Start()`（RTC ISR 向事件队列投递）而无法后移，
  是唯一例外，其后**紧接**一次 `__enable_irq()` 修复；
- `xPortStartScheduler()` 自身会把 `ulCriticalNesting` 归零并重新开中断，
  因此调度器启动后的状态有定义。

---

## 7. 验收标准

1. `examples/` 下每个示例均可**独立生成 + 编译通过 + 主机单元测试全绿**。
2. 六层 YAML 任一层的字段错误都能在生成阶段被拦截并给出可定位的报错。
3. 引脚冲突（重复声明 / 不存在引脚 / 不支持 AF / 同引脚被多外设占用 / `cs_pin` `de_pin` 未声明或跨外设共享）全部被拦截，冲突时给出替代引脚建议。
4. 老格式单体 YAML 无需用户改动即可生成。
5. 重新生成不破坏用户 `USER CODE` 块。
6. 上板验证：base 示例烧录后串口打印 `System ready`，CLI 可交互，STOP 模式下 UART 可唤醒且时间无偏差。
7. CI 三个 job（Lint / Build & Test / Deploy Docs）全绿。

---

## 8. 已知限制与风险

| 类别 | 限制 |
|------|------|
| MCU | 多 MCU 后端未实现（v0.5 规划） |
| 引脚/时钟 | 引脚-总线映射表硬编码（`_STM32G0_PIN_BUS`），新增 MCU 需手动添加；I2C TIMINGR 为预计算查找表，仅覆盖 16/64 MHz I2C 时钟场景 |
| RTC | 依赖真实 LSE 晶振；无外部时钟的旁路模式会导致日历漂移（启动时强制清 LSEBYP） |
| 网表/BOM | BOM 依赖启发式字符串匹配，非标准元件名可能漏识别；SPI CS 自动检测在复杂拓扑中可能不准 |
| 状态机 | 仅一层嵌套复合状态；不支持 Choice Point / Fork-Join；`event_t` 仅含 `id`，事件不带参数 |
| 模拟外设 | 内部温度传感器绝对精度依赖片上 TS_CAL1/TS_CAL2，适合相对变化检测 |
| Web 前端 | 解析进度无可视化反馈；YAML 编辑器仅支持单体 legacy 格式预览 |
| 工具链 | 环境依赖手工配置（Phase 6 计划用 Docker 固化） |

---

## 9. 路线图

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 4 | Netlist/BOM 驱动 + 原理图注解 + 统一管线（116 测试） | ✅ 已完成 |
| Phase 5 | 可视化配置台（YAML 编辑器 / 外设面板 / 引脚可视化 / 状态机编辑器 / 任务分配 / 生成下载） | ⏳ 进行中 |
| Phase 6 | 质量与合规：MISRA C:2012 扫描 + 自动修复、复杂度控制、静态分析、Docker 编译环境 | ⏳ |
| Phase 7 | 多平台与生态：STM32F4/H7、Zephyr 后端、增量生成、插件市场、VSCode 扩展、CI 模板 | ⏳ |
| v0.5 | 多 MCU 后端（ESP32、NXP） | ⏳ |
| v0.6 | `bind.yaml` 事件系统完整实现 + Web BindGraph 联动 | ⏳ |
| v1.0 | 免编程工作流闭环：EDA 上传 → 拖拽编排 → 一键固件 | ⏳ |

**待办（未归类）**：GPIO/ADC/UART HIL 测试、Bootloader 端到端测试、DSL 变量类型扩展（struct/array）、CHOICE 伪状态、USB CDC 日志模板、DMA 支持、FDCAN 模板、LPUART 模板、安全启动（固件签名）、FOTA 完整集成测试。

---

## 10. 需求追溯矩阵

| 需求 | 源码 / 文档位置 |
|------|-----------------|
| FR-1 硬件解析 | `parser/`（`netlist_parser*.py` / `bom_parser.py` / `passive_extractor.py` / `schematic_annotator.py` / `cross_validator.py` / `pipeline.py`）、`docs/reference/*.md` |
| FR-2 硬件层 | `docs/user-guide/hardware-yaml.md`、`generator/schemas/hardware.py`、`generator/data/mcu/*.json` |
| FR-3 业务层 | `docs/user-guide/task-yaml.md`、`generator/schemas/task.py`、`templates/app/statemachine.c.j2` |
| FR-4 绑定层 | `docs/user-guide/bind-yaml.md`、`generator/schemas/bind.py`、`generator/mapper.py` |
| FR-5 组件/参数/主题 | `docs/user-guide/{components,params,pubsub}-yaml.md`、`templates/src/{component_registry,component_bus,param_registry}.c.j2` |
| FR-6 校验体系 | `generator/validator.py`、`generator/validators/pin_conflict_validator.py`、`generator/mcu_database.py` |
| FR-7 引脚分配 | `generator/allocators/pin_allocator.py` |
| FR-8 生成引擎 | `generator/generate.py`、`generator/context/builder.py`、`generator/merger/c_merger.py` |
| FR-9 运行时框架 | `templates/src/*.j2`、`templates/drivers/posix/*.j2` |
| FR-10 外设驱动 | `templates/drivers/*.j2`、`models/*.yaml` |
| FR-11 协议栈 | `templates/drivers/drv_{modbus,mqtt,cellular,uart}.c.j2`、`examples/modbus_demo/` |
| FR-12 控制中间件 | `templates/app/pid_*.j2`、`foc_*.j2`、`fall_detect*.j2`、`attitude.c.j2`、`examples/{solenoid_valve,thermo,knob}_*/` |
| FR-13 低功耗/RTC | `templates/src/{sleep,power_mgr}.c.j2`、`templates/drivers/drv_rtc.c.j2`、`templates/rtos/tickless_idle.c.j2` |
| FR-14 Bootloader/FOTA | `templates/bootloader/`、`templates/drivers/drv_fota.{c,h}.j2`（接收侧状态机）、`templates/drivers/drv_fota_ymodem.{c,h}.j2`（YMODEM 通道）、`templates/drivers/fota_delta.{c,h}.j2`（解码与应用）、`generator/delta_tool.py`、`generator/fota_ymodem_sender.py`（测试侧发送器）、`generator/data/{fota_format,ymodem_format}.json`、`generator/tests/test_fota_delta_l6.py`、`generator/tests/test_fota_protocol*`、`generator/tests/test_fota_ymodem*.py`、`examples/fota_demo/`（旧的 `generator/bsdiff_tool.py` 与 `fota_bspatch.{c,h}.j2` 已退役，见 `docs/plans/differential-ota.md` §11.1 / §16.6） |
| FR-15 调试可观测 | `templates/drivers/drv_{cli,log}.c.j2`、`templates/src/telemetry.c.j2`、`docs/user-guide/cli-commands.md` |
| FR-16 测试体系 | `templates/test/`、`generator/run_tests.py`、`generator/tests/`、`tests/`、`parser/tests/` |
| FR-17 工具链/CI | `templates/project/*`、`templates/vscode/*`、`.github/workflows/build_and_test.yml` |
| FR-18 可视化配置台 | `docs/roadmap/phase5-plan.md`、`docs/plans/three-layer-split.md`（Web 章节） |
| NFR-3 / Phase 6 | `docs/roadmap/milestones.md` |
| NFR-12 子模块约束 | `AGENTS.md` 教训 1、`docs/requirements.md` §6.1、`templates/src/main.c.j2`（以初始化**排序**替代生成期补丁，旧的 `generate.py::_apply_vendored_patches()` 已删除） |
| 全部 | `README.md`、`AGENTS.md`、`docs/developer-guide/architecture-overview.md` |
