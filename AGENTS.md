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

# 5) 生成器/解析器 Python 测试（三个目录都要给，与 CI 一致）
python -m pytest tests parser/tests generator/tests -q
#    ⚠️ 只写 `generator/tests tests` 会静默漏掉 parser/tests 的 139 条
#    （715 条里少收 139 条，全绿但没测过网表/BOM 解析）

# 6) 烧录 + 串口验证
#    ⚠️ 本机 OpenOCD 驱动不了 DAP-Link（CMSIS-DAP v2 / WinUSB），用 pyOCD
#    ⚠️ 必须在沙箱之外跑，否则 USB 枚举为空、会误判成"没插调试器"
PYOCD=C:/Users/pc/.workbuddy/binaries/python/envs/default/Scripts/pyocd.exe
#    带 bootloader 的示例要烧 **combined.bin**（引导器 + Slot A）到 0x08000000，
#    烧 <demo>.bin（应用镜像、按 0x08002000 链接）会覆盖引导器 → 变砖
$PYOCD flash output/<demo>/build/combined.bin --target stm32g0b1vetx -e chip --no-reset
$PYOCD cmd  --target stm32g0b1vetx -c halt -c "read32 0x08000000 16"   # 回读校验
$PYOCD reset --target stm32g0b1vetx                                   # 再复位启动
python .workbuddy/tmp/serial_capture.py --out <log> --trigger "starting scheduler" \
       --script "version|uptime|free|fota status|sysinfo"             # 先开监听，再 reset
```

## 环境事实（本机 Windows）

- Python：`C:/Users/pc/anaconda3/python.exe`
- arm-none-eabi-gcc：`C:/Arm/mingw-w64-i686-arm-none-eabi/bin/`
- cmake/ninja：`C:/mingw64/bin/`（cmake 4.x 要求绝对路径传 toolchain/编译器）
- OpenOCD：`C:/Arm/openocd-cb52502-i686-w64-mingw32/bin/openocd.exe` ——
  **驱动不了本机这块 DAP-Link**（该构建只编入 `hid` 后端，无 libusb）
- **pyOCD（上板首选）**：`C:/Users/pc/.workbuddy/binaries/python/envs/default/Scripts/pyocd.exe`
  （0.45.1 + `libusb-package`，走 WinUSB 批量接口）；
  **目标名 `stm32g0b1retx` / `stm32g0b1vetx`**（同一 die，按封装选；依赖
  `pyocd pack install STM32G0B1RETx` 装的 `Keil.STM32G0xx_DFP` 2.1.0；
  pyOCD 内置目标里没有 STM32G0）。`pyocd list --targets | grep -i g0b1` 可列全。
- 调试探针**换过**，两种都能用（2026-09-17 起是 ST-Link）：
  - **ST-Link**：`STM32 STLink`，pyOCD 原生支持，无需 `reset_type` 参数
  - **DAP-Link**：`VID_0D28&PID_0204` 的 CMSIS-DAP v2：`MI_00`=WinUSB、
    `MI_01`=CDC(**COM3**)、`MI_03`=HID。**无 MSC 接口 → 不能拖拽烧录**
- 串口也换过：
  - **COM5 = FTDI FT232R**（`VID_0403&PID_6001`，SER=A5069RR4A）—— 2026-09-17 在用
  - **COM4 = CP210x**（接 USART2 PA2/PA3）—— 09-15 在用，当前 Disconnected
  - `pnputil /enum-devices /class Ports` 可以看清"哪个适配器真的插着"
- 串口调试脚本（保留本地、不提交）：`cmd_capture.py`、`flash_capture.py`、
  `.workbuddy/tmp/serial_capture.py`（带速率自检，见教训 12）

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
12. **看到"串口刷屏 / 同一段被重复几十次"先怀疑主机侧，别先改固件**（2026-09-17）。
    本机 FTDI(COM5) 会进入**陈旧缓冲反复回放**状态：把驱动/FIFO 里的旧内容按 USB
    轮询节奏重复交付，表现为 200+ KB/s 的固定片段刷屏，还会伪装成"启动横幅被打了
    72 次"这种看着特别像固件缺陷的现象。**三条判据**（任一条成立即可定性）：
    - **速率判据**：字节率不得超过 `baud/10`（115200 8N1 → 11.52 KB/s）。超了就是
      主机侧。更强的形式是换个波特率（9600/115200/230400）再看：**内容与速率都不变**
      ⇒ 这批数据根本没经过 UART 接收。
    - **因果判据**：用 pyOCD 的 **Python API 在同一进程里** `halt` 并保持会话，再采样；
      核停了还在流 ⇒ 与固件无关。（用 `pyocd cmd` 子进程做会因退出时自动 resume 而失真。）
    - **写入量判据**：固件的每个 UART 字节都只来自 `lwrb_read()`（TXE ISR 或
      `log_flush()`），**取走即删除** ⇒ 发出的字节数 ≤ 入队字节数。所以在
      `main` 里 `bl log_raw` 的**下一条**下断点，读 `log_ringbuf`
      （`templates/drivers/drv_log.c.j2` 里的 static，符号在 .bss 里查得到：
      `buff/size/r_ptr/w_ptr/evt_fn/arg` 六个字），`w_ptr` 就是本次入队字节数；
      与源码算出的字符串长度（含 `log_raw` 额外补的 CRLF）相等即证明只写了一次。
      再顺带 dump 缓冲内容，能直接看到"只有一份"。
    处置：`reset_input_buffer()` 有时能救；彻底恢复要**物理重插**（软件复位需管理员：
    `pnputil /restart-device "<instanceid>"`，非提升权限会 `Access is denied`）。
    **抓取脚本必须自带速率自检**（超限即告警并再 purge），否则会把主机故障当成固件回归。
    **pyOCD 断点两个坑**：① `resume()` 之后 `get_state()` 会短暂仍报 `HALTED`，
    于是把同一个现场当成新命中 → 判定条件要写成"停在**不同于**上次的位置"；
    ② 从断点地址本身 `resume` 会原地再次触发（且实测 `target.step()` 不前进），
    **优先只打断点一次、读完就走**，不要入口/返回各打一个。
13. **新增"第二条投递通道"时，别抄第一份的约定**（YMODEM 通道，2026-09-17）。
   FOTA 接收现在有两条传输通道（HWC 帧协议 `fota recv`、YMODEM `fota ymodem`），
   它们**只允许在"字节怎么进来"上不同**：定长分片 + 显式序号 + 停等 ACK，还是
   128/1024 字节块 + 16 位 CRC。字节进来之后的一切（容量准入公式、暂存区几何、
   续传判定、落盘与回读校验）必须逐字节相同，因此收敛在 `drv_fota.c` 的
   `fota_session_*` 里由两条通道共用 —— 各写一份的结局不是"代码重复"，而是
   **两份准入公式**，而"边界少算一页、应用期擦掉暂存区首页"这类缺陷的难点
   恰恰在于它只在一小段边界上出现，两份实现里总有一份没被那条边界测到。
   同理"哪些状态允许进入接收"是**安全策略**，只有一个gate
   （`fota_receive_ready()`），两个入口各判一次迟早会漏一个状态。

   ⚠️ **最贵的坑是"标准同名、实现不同"的校验**：帧协议用 CRC-16/CCITT-FALSE
   （init `0xFFFF`，`'123456789'` → `0x29B1`），而 YMODEM 规定 CRC-16/**XMODEM**
   （init `0x0000` → `0x31C3`）。图省事复用 `fota_crc16()` 会得到一个
   **"自研主机 ↔ 设备完全互通，但与 Tera Term / lrzsz sb / ExtraPuTTY 一个都
   连不上"**的实现——两侧错得一样，所以任何自测都不会红。判据因此必须是**标准**
   （`generator/data/ymodem_format.json` 写死 check 值当 KAT，独立验算用
   `python -c "import binascii; print(hex(binascii.crc_hqx(b'123456789', 0)))"`），
   不能是"另一侧的实现也这么算"。同一通道的另外三个必踩点（末块 `0x1A` 填充必须
   按块 0 声明长度截断、`CAN` 只在块边界且连续两个才算中止、
   `(uint8_t)256U == 0` 导致运行期除零）见 `docs/plans/differential-ota.md` §19。
   护栏：`generator/tests/test_fota_ymodem.py`（渲染层）+ `test_fota_ymodem_l5.py`
   （跨实现台架，14 用例，3 处变异各自只打掉一个用例）+
   `templates/test/test_fota_ymodem.c.j2`（生成工程内 15 用例，跟着
   `run_tests.py` 跑）。

   ⚠️ 同一件事的第二个教训：`drv_fota.c` 调 `fota_ymodem_process()` 之后，
   **已经存在的** `templates/test/test_fota_protocol.c.j2` 立刻链接失败
   （`undefined reference to 'fota_ymodem_process'`），而它的 pytest/L5 用例
   全绿 —— 那些用例不生成、也不编译工程内的测试。是
   `output/<demo>/test/run_tests.py` 报出来的（CI 也跑这一步）。
   ⇒ 给生成源码**新增一个跨模块调用**时，必须跑一次受影响示例的
   `run_tests.py`；并让相关测试 `#include` 进完整的源码集合，
   **不要给它加空桩**（空桩会让"调用了不存在的实现"继续静默）。

14. **"派生值"必须在真链路里断一次，且派生出的默认值不许落 CMake cache**
   （版本号契约，2026-09-17 真机发现）。
   `task.yaml: project.version` 同时决定**两个东西**：固件 banner / `version`
   打印的字符串，和镜像头 `fw_version`（引导器判"哪个槽更新"的单调计数器）。
   两者必须一致 —— 不一致时唯一症状是"升级成功了但版本号不变"，看起来像升级
   没生效。彻底修好它需要**同时**动两处，而两处各有一个独立的静默失效方式：

   - `generator/generate.py` 里把软件层字段搬进 `hw` 用的是**手写白名单**
     （`app_tasks`/`behavior`/`periodic_events`/`bind_routings` 四个 `if`），
     而 `hw` 在此之前已被 `HardwareModel.model_dump(hw_raw)` 整个换掉。
     mapper 后来开始携带 `project.name`/`project.version`，没人往白名单补两行
     ⇒ **配置里写的版本在真实链路里被静默丢弃**，退回默认 `1.0.0`。
     修法：删掉白名单，整体并入（`for k, v in merged.items(): hw.setdefault(k, v)`）。
     ⇒ 教训：任何"把合并结果按键搬过去"的代码都是**下一处静默丢失**；
     要搬就搬全部。
   - `templates/project/CMakeLists.txt.j2` 的默认值**不得写进 CMake cache**：
     cache 一旦写入就粘住，改完 YAML 重新 configure 时旧值继续进镜像头，
     而 banner 打的是新渲染的版本 ⇒ 同一块板子上两个版本号。
     覆盖判据也要按**值**而不是"cache 里有没有这个变量"：旧模板把常量 1 写进过
     cache，而 CMake 对命令行 `-D` 会沿用既有 cache 条目的类型，
     "类型是不是 UNINITIALIZED"区分不出"用户显式指定"与"陈旧自动值"。
     覆盖优先级：`$FOTA_VERSION` > `-DFW_VERSION` > 派生自 `project.version`，
     并把**被选中的来源**打进 configure 日志（不静默）。需求见
     `docs/requirements.md` FR-14.7。
   - 测试为什么没拦住：既有用例验的是 `merge()` 与 `HardwareModel` **两端**，
     而丢值发生在这两端**之间**；另一处把 CMake 的常量当"实现细节"跳过。
     ⇒ 看到"我改了但真机没反应"，先确认**派生到底有没有发生**（把中间值打出来），
     再怀疑下游。护栏：
     `test_generate.py::test_software_layer_fields_survive_the_real_pipeline`
     （在 `build_context_fn` 处观察真实实参，期望键集合由 `merge()` 现算）与
     `test_template_render.py::test_image_header_version_is_derived_from_project_version`
     （把渲染出的 CMake 解析块用 `cmake -P` 真跑一遍）。两者都做过变异验证。

15. **配置值解析不许有"静默出口"：兜底、钳制、`except: pass` 都算**
   （RTC `initial_time`，2026-09-17）。
   `hardware.yaml` 的 `Internal_RTC.extra.initial_time` 是**烤进固件**的日历常量：
   生成期写进 `HAL_RTC_SetTime/SetDate` 之后设备就按它走，再没有任何环节能发现
   它错了 —— 唯一症状是"日志时间戳不对"，而那种现象天然会被当成 LSE 晶振 /
   备份域问题去查硬件。旧实现在这里开了两个静默出口：

   - `except (ValueError, AttributeError): pass  # malformed, use defaults`
     ⇒ `"2026/09/17 10:00:00"`、漏写秒、任意拼错，都静默变成 2000-01-01。
   - 手写 `y, mo, d = date_part.split("-")` **不校验字段位次**：
     `"17-09-2026 10:00:00"`（日月年）解析出 `year = 17-2000` 被
     `max(0, min(99, …))` **钳成 0**，而 `day = 2026` **原样写进 `sDate.Date`**
     —— RTC 的 Date 合法范围是 1..31，钳制只是把错误挪了个位置。

   ⇒ 两条通用规则：**① 解析失败要失败，不要兜底；② 越界要拒绝，不要钳制**
   （钳制后的值看起来合法，等于替 YAML 作者做了一个他看不见的决定）。
   **只有字段整体省略**时才允许用默认值。需求见 FR-13.6。
   实现：先正则校验**形状**（`^\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{1,2}:\d{1,2}$`
   —— 月/日/时/分/秒可不补零，但年份 4 位且在最前，自然挡住 DD-MM-YYYY），
   再用 `datetime(...)` 构造校验**日历合法性**（2 月 30 日 / 13 月 / 25 时），
   最后判年份窗口 2000..2099（RTC 只存 2 位年份 —— 这是硅片的窗口，不是偏好）。
   另注意 `generate.py` 顶层有一串 `except`（含 `except Exception`）：新增
   `raise ValueError` 时要确认它落在 `logger.critical + sys.exit(1)` 那一支上，
   否则"报错"会退化成日志里一行容易被忽略的文字。
   护栏：`test_builder.py` 四条（含"拒绝必须发生在**真实路径**上"）+
   `test_template_render.py::test_rtc_calendar_call_carries_the_configured_initial_time`
   （断言 `HAL_RTC_Set*` **实参上的数字**，并断言 `sDate.Date` 是日 1..31 ——
   日月年错位时它会是 2026）。变异脚本 `.workbuddy/tmp/mutate_rtc_guard.py`。

16. **YAML 里写了、却没有任何代码会去读的键，必须在生成期出声**
   （`project.heap_size`，2026-09-17）。
   `mapper.merge()` 从 `task.yaml` 的 `project` 块里只取 `name` / `version`；而仓库里
   **10 个示例无一例外**都写了 `project.heap_size`（24576 / 16384）—— 作者以为堆配上了，
   实际生成的链接脚本一直是默认 `0x200`，FreeRTOS 堆照旧走自动推算。这不是"解析
   失败"，而是**根本没进解析**，所以教训 15 那类 fail-loud 拦不住它。

   ⇒ 两条判据：
   **① 每一层 YAML 的每个键，都要能指出"谁读它"**。`merge()` 那种白名单式提取
   （`project` 只认 `name` / `version`）必须对未知键出声；否则"写错层"和"写对了"
   在生成日志里长得一模一样。
   **② 同一概念在不同层有不同归属时，不要复用名字**。`heap_size` 属于
   `hardware.yaml`（进链接脚本，是留给 newlib 的系统堆），FreeRTOS 的堆另起名
   `rtos_heap_size` 放 `task.yaml` 顶层 —— 复用名字会让"放错层"看起来是对的。
   新增配置项时记得同步 `split_legacy()` 的层归属列表（旧单体 YAML 走那条路径）。

   ⚠️ **存量清理必须一次扫全，不能只修"当下 grep 到的那几个"**。第一次改只顺着
   报错现场清了 4 个示例，剩下 6 个照旧留着死键；生成机制加上告警之后，这 6 个
   每次生成都会各刷一条 WARNING —— 而长日志里的重复告警很快就会被无视，等于没修。
   改这类"横向缺陷"的正确顺序是：**先写一条扫全仓库的护栏，再按它的输出清干净**
   （`test_generate.py::test_no_shipped_example_declares_a_dead_project_key` 遍历真实
   `examples/*/task.yaml`，带正控防止断言恒真 —— 示例本身就是文档，留死配置等于
   教用户写错层）。变异脚本 `.workbuddy/tmp/mutate_dead_project_key_guard.py`
   （4/4 抓到，其中一条用的是从未出现过的新键名 `stack_pool_bytes`，证明挡的是
   缺陷类别而非字面量 `heap_size`）。

   FreeRTOS 堆：缺省由 `compute_heap_size()` 按任务集推算，显式配置优先，非法值
   （非整数 / ≤0 / 非 8 字节对齐 / 超过 RAM 容量）在**生成期报错**（FR-3.16）。
   ⚠️ **推算值偏紧**：mhde_mainboard 推算 13312 B，而实测稳态只剩 760 B 空闲
   （公式没把组件 step 任务算进去）—— 加组件或调大任一任务栈之前先看这个数字，
   而不是链接期剩下的那 112 KB。

17. **上板取证脚本自己也会说谎 —— 判据要先自证，结论才可信**（FR-14.8 验证，
   2026-09-23）。同一次实验里连续得出两个相反结论，两次都是脚本的错、不是设备的错：

   ① **分类函数的比较顺序**：`zone(pc)` 里 `if pc >= RESET_HANDLER` 排在
   `if pc >= APP_BASE` 前面 —— App 基址 `0x08002000` 数值上大于
   Reset_Handler `0x08000570`，于是**所有正常运行都被判成"反复复位"**。
   ⇒ 按地址区间分类时，**大的数值区间必须先判**；并且每次都要配一组
   **对照组**（健康状态下该落在哪，实测一遍），否则"PC 落在 SOS 循环"也可能
   只是地址算错了。

   ② **`delay_ms` 这类叶子函数无法区分调用者**：heartbeat 和 SOS 都调它，
   只看 PC 分不清"卡在 SOS"和"heartbeat 特别长"。`halt` 后读
   `lr & ~1`（返回地址）就能指出调用者落在哪一段。

   ③ **原字节必须从本地镜像取，不能从 flash 读**：要"恢复被改坏的那一页"时，
   若 flash 上那份正是上次留下的坏字节，读回来再写回去等于没改，
   于是下一步的前置校验永远失败。

   ④ **抓串口的脚本可能提前收尾**：`serial_capture.py` 的 `--script` 在触发器
   命中后就结束（实测 t=8.2 s 就收尾），把复位后的窗口整个切掉，看起来像
   "设备零输出"。要连续观察就用 `plain_capture.py`（纯读取、带逐行时间戳）。

   ⑤ **备份寄存器地址写错 4 字节，整条实验链就被悄悄改写了**（FR-15.6 验证，
   2026-09-23）。`TAMP_BASE = 0x4000B000`，备份寄存器从 `+0x100` 起每 4 字节一个：

   | 寄存器 | 地址 | 含义 |
   |---|---|---|
   | `BKP0R` | `0x4000B100` | 启动重试计数 |
   | `BKP1R` | `0x4000B104` | 活动槽（0=A / 1=B） |
   | `BKP2R` | `0x4000B108` | `boot_ok` 标志（`0xB007C0DE`） |

   写成 `0x4000B104/0x4000B108` 自以为是 BKP0R/BKP1R，实际清掉的是
   **`boot_ok`**、重试计数一次都没清。而引导器阶段 3 的判据是"`boot_ok` 没了
   ⇒ 上次启动失败 ⇒ `attempt+1`"：连做几次实验后 `attempt > BOOT_MAX_RETRIES`，
   阶段 4 直接 **swap 到另一个槽并软复位** —— 于是刚烧进活动槽的镜像
   **根本没被 CRC 校验过**，现象却是"这个镜像被引导器拒了"。
   ⇒ 取证脚本里凡涉及备份域，**三个寄存器一起设**（attempt=0 / slot=指定 /
   boot_ok=0），并把地址写成常量表而不是现算；看到"跑起来的不是我刚烧的那个槽"
   时先怀疑计数，不要先怀疑镜像。

   ⇒ 判据链上每一环都要能**独立自证**：对照组证明地址没算错，LR 证明调用者
   是誰，CRC 复算的"MISMATCH"要真是复算出来的（不能拿"头解析失败"当注入成功）。

## CI（.github/workflows/build_and_test.yml）

三个 job：Lint（flake8/black，均有容错）→ Build & Test（生成 base +
编译 + 主机测试 + SIL）→ Deploy Docs（MkDocs → GitHub Pages，需
`environment: github-pages`）。子模块 checkout 是历史失败点（见教训 1）。
