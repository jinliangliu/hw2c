# hw2c 项目记忆（AGENTS.md）

本文件是给后续 AI 会话 / 协作者的项目记忆。新会话先读本文件，
再动手，避免重复踩坑。**用户工作习惯：改完先本地 commit，不 push，
除非用户明确要求 push。**

**需求规格见 `docs/requirements.md`**（需求唯一入口：范围界定、FR-1..FR-18、
NFR-1..NFR-12、接口契约、约束、验收标准、已知限制、路线图、需求追溯矩阵）。
需求变更先改该文档，再落到实现。

## 项目定位

**Hardware2Code (hw2c)**：从 EDA 设计文件（Netlist/BOM）自动生成可编译
嵌入式固件的通用代码生成平台。核心卖点是**软硬件分离**：硬件描述层
（YAML）与具体 MCU 解耦，换芯片只改硬件配置、重新生成，业务逻辑零改动。
平台不绑定单一芯片（路线图含 STM32 / ESP32 / NXP 等多后端）。

- 仓库：https://github.com/jinliangliu/hw2c（MIT）
- 当前验证 MCU：STM32G0B1RET6 @ 16 MHz（HSI，可选 LSE RTC）
- 硬件参考板：HW2C-DevKit（嘉立创 EDA 设计中；产品名 `HW2C-DevKit`，
  工程/仓库名 `hw2c-devkit`）

## 架构速览

### 六层 YAML 配置（每个示例一套）

`hardware.yaml`（引脚/外设/时钟/休眠）、`task.yaml`（任务/状态机/周期事件）、
`components.yaml`（组件注册）、`bind.yaml`（中断/绑定）、`params.yaml`
（运行时参数）、`pubsub.yaml`（发布/订阅主题）。

### 生成器管线（`generator/`）

```
YAML → Pydantic 校验（schemas/hardware.py）
     → validator.py（业务校验）
     → validators/pin_conflict_validator.py（引脚冲突，三层）
     → allocators/pin_allocator.py（自动分配，排除 SWD/已用引脚）
     → context_builder（统一渲染上下文）
     → Jinja2 模板（templates/*.j2）→ output/<demo>/
```

### 运行时组件框架

- `component_registry`：统一生命周期 `init/step/terminate`
- `component_bus`：发布/订阅事件总线（topic 由 pubsub.yaml 生成）
- POSIX 风格总线 API：`uart_api` / `i2c_api` / `spi_api` / `gpio_api` /
  `adc_api`（templates/drivers/posix/）—— 一个总线句柄，设备按
  地址（I2C）或 CS（SPI）区分，天然支持一总线多设备

### 示例（examples/）

| 示例 | 内容 |
|------|------|
| base | 最小系统：RTC 1Hz 心跳 + 10 路定时器、低功耗 RUN/SLEEP/STOP0/STOP1、CLI、遥测快照 |
| modbus_demo | Modbus RTU 主/从（FC03/06/16），USB-TTL/RS485 双传输，`modbus_tool.py` 对测 |
| spi_flash_demo | W25Q32 SPI NOR Flash |
| mpu6050_demo | IMU：I2C 默认（MPU6050@0x68），SPI 变体（MPU6500，模型 `SPI_Sensor_MPU6500`）；姿态互补滤波 |
| pwm_demo | TIM2 双通道 PWM，逐路占空比/频率可调 |
| solenoid_valve_pid_ctrl_demo | 过程变量 PID 中间件（pid_math 纯算法 + pid_ctrl 可选装组件，压力/温度/流量通用）：电磁阀开关阀 16 Hz PWM 占空比调制加压、I2C 压力/温度反馈（I2C_Pressure/I2C_TempSensor）、单路/双路执行器、升压/保压/故障联锁、SIL 压力罐闭环仿真、CLI `solenoid` |
| thermo_pid_ctrl_demo | NTC 温控（复用 pid_ctrl 中间件）：NTC 100K/3950 分压 + ADC（B 参数方程）、PWM 加热器、超温联锁、SIL 热质量模型闭环仿真 |

## 标准工作流（改任何模板/YAML 后）

```powershell
# 1) 重新生成（六层参数齐全；--force 会原子替换 output 并删掉 build/）
python -m generator.generate -i examples/<demo>/hardware.yaml -o output/<demo> --force `
  --task examples/<demo>/task.yaml --bind examples/<demo>/bind.yaml `
  --components examples/<demo>/components.yaml --params examples/<demo>/params.yaml `
  --pubsub examples/<demo>/pubsub.yaml

# 2) 构建（必须显式编译器/工具路径）
cd output/<demo>
cmake -B build -G Ninja -DCMAKE_TOOLCHAIN_FILE="<repo>/output/<demo>/toolchain.cmake" `
  -DCMAKE_MAKE_PROGRAM="C:/mingw64/bin/ninja.exe" `
  -DCMAKE_C_COMPILER="C:/Arm/mingw-w64-i686-arm-none-eabi/bin/arm-none-eabi-gcc.exe"
cmake --build build

# 3) 主机侧单元测试（Unity + mock_hal，无硬件）
cd output/<demo>/test; python run_tests.py

# 4) SIL 组件测试
cd output/<demo>/test/sil
cmake -B build -G Ninja -DCMAKE_MAKE_PROGRAM="C:/mingw64/bin/ninja.exe" -DCMAKE_C_COMPILER="C:/mingw64/bin/gcc.exe"
cmake --build build; ./build/test_component_sil

# 5) 生成器/解析器 Python 测试
python -m pytest generator/tests tests -q

# 6) 烧录 + 串口验证（需要 DAP-Link）
#    ⚠️ 本机 OpenOCD 驱动不了这块 DAP-Link（CMSIS-DAP v2 / WinUSB），用 pyOCD
#    ⚠️ 必须在沙箱之外跑，否则 USB 枚举为空、会误判成"没插调试器"
PYOCD=C:/Users/pc/.workbuddy/binaries/python/envs/default/Scripts/pyocd.exe
$PYOCD flash output/<demo>/build/<demo>.bin --target stm32g0b1retx -O reset_type=hw
$PYOCD reset  --target stm32g0b1retx -O reset_type=hw   # 保证抓到完整启动
python flash_capture.py                                  # COM4 @115200 抓日志/CLI
```

## 环境事实（本机 Windows）

- Python：`C:/Users/pc/anaconda3/python.exe`
- arm-none-eabi-gcc：`C:/Arm/mingw-w64-i686-arm-none-eabi/bin/`
- cmake/ninja：`C:/mingw64/bin/`（cmake 4.x 要求绝对路径传 toolchain/编译器）
- OpenOCD：`C:/Arm/openocd-cb52502-i686-w64-mingw32/bin/openocd.exe` ——
  **驱动不了本机这块 DAP-Link**（该构建只编入 `hid` 后端，无 libusb）
- **pyOCD（上板首选）**：`C:/Users/pc/.workbuddy/binaries/python/envs/default/Scripts/pyocd.exe`
  （0.45.1 + `libusb-package`，走 WinUSB 批量接口）；
  **目标名 `stm32g0b1retx`**（依赖 `pyocd pack install STM32G0B1RETx` 装的
  `Keil.STM32G0xx_DFP` 2.1.0；pyOCD 内置目标里没有 STM32G0）
- 调试器：`VID_0D28&PID_0204` 的 **DAP-Link，CMSIS-DAP v2**：
  `MI_00`=WinUSB `[001] CMSIS-DAP`、`MI_01`=CDC(**COM3**)、`MI_03`=HID。
  **无 MSC 接口 → 不能用 U 盘拖拽烧录**
- 串口：**COM4**=CP210x（接 USART2 PA2/PA3，日志/CLI 实际走这个）、COM3=DAPLink CDC
- 串口调试脚本（保留本地、不提交）：`cmd_capture.py`、`flash_capture.py`

## 关键设计决策与教训（重要）

1. **供应商源码（HAL / CMSIS / FreeRTOS）只读，不得修改**。
   `static/stm32g0/` 下的 ST 官方 HAL、CMSIS 与 FreeRTOS 内核「系统代码」
   一律不得就地改写（禁止生成期补丁、sed/replace、submodule 内本地 commit）；
   只有**配置文件**（`FreeRTOSConfig.h`、`stm32g0xx_hal_conf.h` 等）与
   **中断向量/回调文件**（`stm32g0xx_it.c`）可以改。链接脚本由 hw2c 自有的
   `templates/linker/` 生成，不属于 vendor。
   上游缺陷只有两条合规出路：**生成代码侧适配**，或**升级 submodule 指针**
   到修好该缺陷的上游 commit（gitlink 必须始终指向上游可获取的 commit，
   指向本地 commit 会让 GitHub Actions checkout 失败，历史 run 35/36）。
   详见 `docs/requirements.md` §6.1。

   **`static/third_party/` 同样只读**，规则与上同：它是 vendored 的第三方源码
   （如差分解码器 HPatchLite、tinyuz），每个条目带 `PROVENANCE.json` 记录
   上游 URL / commit / 许可。**不得就地改写、不得打补丁**；需要改动时只有
   「生成代码侧适配」或「升级 vendored 版本并更新 PROVENANCE.json」两条路。
   判据与 `static/stm32g0/` 一致：`git diff --stat -- static/` 必须为空
   （§14 验收第 7 条）。

   **已验证案例 —— FreeRTOS ARMv6-M 端口毒值**：`portable/GCC/ARM_CM0/port.c`
   把 `ulCriticalNesting` 初始化为 `0xAAAAAAAA`，且上游至 `78069a79e`
   （2026-07-16）仍未修复。后果：`xPortStartScheduler()` 之前**第一次**
   `taskEXIT_CRITICAL()` 递减后仍非 0，永远走不到恢复 PRIMASK 的分支，
   中断在调度器启动前一直被屏蔽 → `HAL_GetTick()`（TIM14 中断驱动）冻结，
   LSE/I2C/Flash/IWDG 等所有按 tick 计时等待死循环。
   **hw2c 的合规修法是排序，不是打补丁**（`templates/src/main.c.j2`）：
   - 调度器启动前的 RTOS 对象创建（`EventMgr_Init` 的 `xQueueCreate`、
     `cli_init` 的 `xSemaphoreCreateBinary`、`telemetry_init` 与各
     `xTaskCreate`）全部收敛到**紧邻 `vTaskStartScheduler()` 之前的最后一个块**；
   - 该块**禁止**出现依赖 tick / 依赖中断的调用——`component_init_all()`、
     `IWDG_Init()`、SPI Flash 忙等任何 `HAL_GetTick()` 等待都排在它之前
     （`log_flush()` 是轮询排空，任何位置都安全）；
   - `EventMgr_Init()` 须先于 `RTC_Start()`（RTC ISR 向事件队列投递）而无法
     后移，是唯一例外，其后**紧接**一次 `__enable_irq()` 修复；
   - `xPortStartScheduler()` 自身会把 `ulCriticalNesting` 归零并开中断，
     所以调度器启动后的状态有定义。
   回归护栏：`generator/tests/test_template_render.py::
   test_main_c_keeps_rtos_object_creation_after_tick_dependent_init`。
   **新增任何「在 main() 里创建 RTOS 对象」的调用前，先想清楚它插在哪个位置。**
2. **驱动模板参数化传输**：`drv_mpu6050` 按 `model.interface` 生成
   I2C 或 SPI 传输宏；模型新增芯片时复用寄存器表。组件/姿态层与总线无关。
3. **Jinja 陷阱**：`{% if %}` 块内的 `{% set %}` 不会泄漏到块外——
   多分支 set 必须写成顶层三元表达式（如 `drv_pwm`/`drv_mpu6050` 里的
   `chans` 计算）。改模板后务必重新生成并跑主机测试。
   另：模板**注释里提到函数名会让「按位置断言」的测试误判**，
   写这类测试前先剥掉 C 注释。
4. **Python 生成器缩进**：`generator/validator.py` 等文件的校验逻辑在
   循环内（12 空格），apply_patch 时保留原缩进，否则 IndentationError。
5. **`--force` 会删 build/**：重新生成后必须重新 cmake configure。
6. **OpenOCD 驱动不了本机 DAP-Link —— `0x57 WriteFile` 的真因**：
   `hid` 后端能找到设备，但报 `error writing data: WriteFile: (0x00000057)`。
   真因不是残留进程，而是这块 DAP-Link 是 **CMSIS-DAP v2（WinUSB 批量）**，
   而本机 OpenOCD 构建**没有 libusb 后端**（`cmsis-dap backend usb` 被判为
   非法参数，`auto` 落到 TCP 后报"hostname or IP address … must be specified"）。
   杀进程重试只对 v1 设备可能有效。**正确做法：改用 pyOCD**（见"环境事实"）。
   **且必须在沙箱外执行**——沙箱内 USB/HID 枚举返回 0 个设备（连已存在的
   COM4 都看不到），会把"沙箱挡了"误判成"没插调试器"。
7. **上板验证时序**：**先开串口监听，再复位/烧录**，否则丢掉启动横幅。
   实测 `base` 启动序列：`HW2C` ASCII art → 版本行 → `MX_GPIO_Init() OK` →
   `EventMgr_Init() OK` → `statemachine_init() OK` → `temp_sensor_init() OK` →
   `RTC_Init() OK` → `component_init_all: 3/3 OK` → `component_bus` →
   `param_registry` → `PowerMgr` → `System ready — … starting scheduler`；
   约 30 s 后打出第一份遥测快照。**判定标准**：出现 `System ready` 且
   `help` / `uptime` 有回应。
   pyOCD 下额外做一次 `pyocd reset` 可保证捕获完整启动。
8. **引脚防冲突三层校验**（新增外设时保留）：
   Pydantic 重复 pin id → MCU 数据库交叉校验（存在性/AF 支持/同 pin
   冲突）→ 外设字段引用校验（cs_pin/de_pin 必须声明、不得跨外设共享，
   UART↔RS485 DE 伴侣除外）。外设引用未声明引脚会报错。
9. **命名约定**：产品名 `HW2C-DevKit`（丝印/简介），工程名
   `hw2c-devkit`（仓库/目录）；嘉立创简介字段 ≤256 字符。
10. **ISR 必须清掉它负责的「全部」中断标志**（P0-4，2026-09-15 上板发现）。
    本 part 的 RTC 标志寄存器是只读的，要靠写 `SCR` 对应位清除；只要有任一
    **已使能**的源仍处于置位状态，中断线就持续拉高，处理函数**退出即刻重入**
    → 死锁。实测事故：`RTC_Init()` 里 500 ms 单次定时器用 Alarm B 武装了期限，
    它在 `vTaskStartScheduler()` **之前**就到期，而 pre-scheduler 早退分支只清
    `WUTF`/`ALRAF`（漏掉 `ALRBF`）→ 启动停在 `statemachine_init() OK`，
    CPU 100% 时间在 `RTC_TAMP_IRQHandler` 内（`SR = MISR = 0x02`、`SCR = 0`）。
    修法：统一走 `rtc_clear_all_flags()`（掩码含全部 6 个清除位），并把备份域里
    残留的报警/唤醒通道**先失能再按需重新武装**。
    护栏：`test_template_render.py::test_rtc_isr_acknowledges_every_flag_before_scheduler`。
11. **`TEST` 构建里 ISR 是空桩 ⇒ 主机单测与 SIL 结构上覆盖不到中断类缺陷**。
    凡改动 ISR、中断使能、备份域状态或标志清除逻辑，**必须上板验证**。
    可复用的上板排查手法：
    ```bash
    pyocd cmd -t stm32g0b1retx -c halt -c "reg pc" -c "reg lr" -c "reg xpsr" \
              -c "read32 0xE000E300 4" -c "read32 0x40002800 40" \
              -c "read32 0x20000438 12"
    arm-none-eabi-addr2line -f -C -e <demo>.elf <pc>
    arm-none-eabi-objdump -d --start-address=<sym> --stop-address=<sym+0x60> <demo>.elf
    ```
    - `xpsr` 低位 `IPSR = 异常号`：**非 0 表示当前在异常上下文**，
      `IPSR - 16` = IRQ 号；连续多次采样同一 IRQ 即为「中断风暴」。
    - `NVIC IABR(0xE000E300)` / `ISPR(0xE000E200)` / `ISER(0xE000E100)`
      用来判断「谁在活跃 / 谁 pending / 谁使能」。
    - `.noinit`（`base` 为 `0x20000438`，magic/code/reset_count）**全 0**
      即可排除「失效安全捕获 / 复位循环」，把问题限定为纯挂死。
    - `pyocd cmd` 连接后**不会自动停机**，读寄存器前必须先 `-c halt`。
    - `read32 <addr> <count>` 的 count 单位是**字节**，不是字。

## CI（.github/workflows/build_and_test.yml）

三个 job：Lint（flake8/black，均有容错）→ Build & Test（生成 base +
编译 + 主机测试 + SIL）→ Deploy Docs（MkDocs → GitHub Pages，需
`environment: github-pages`）。子模块 checkout 是历史失败点（见教训 1）。
