# hw2c 项目记忆（AGENTS.md）

本文件是给后续 AI 会话 / 协作者的项目记忆。新会话先读本文件，
再动手，避免重复踩坑。**用户工作习惯：改完先本地 commit，不 push，
除非用户明确要求 push。**

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

# 6) 烧录 + 串口验证（可选，需要 DAP-Link）
cmake --build build --target flash-daplink   # OpenOCD CMSIS-DAP
# COM4 @ 115200 抓启动日志与 CLI；烧录后等 CLI 就绪再发命令
```

## 环境事实（本机 Windows）

- Python：`C:/Users/pc/anaconda3/python.exe`
- arm-none-eabi-gcc：`C:/Arm/mingw-w64-i686-arm-none-eabi/bin/`
- cmake/ninja：`C:/mingw64/bin/`（cmake 4.x 要求绝对路径传 toolchain/编译器）
- OpenOCD：`C:/Arm/openocd-cb52502-i686-w64-mingw32/bin/openocd.exe`
- 调试器：CMSIS-DAP（DAP-Link），串口 COM4（另有 COM3）
- 串口调试脚本（保留本地、不提交）：`cmd_capture.py`、`flash_capture.py`

## 关键设计决策与教训（重要）

1. **FreeRTOS 子模块禁止提交本地 commit**。
   ARMv6-M 端口 pre-scheduler 临界区（V11 毒值 `0xAAAAAAAA`）会卡死
   PRIMASK，导致 HAL_GetTick 冻结、启动挂起。修复是**父仓库侧补丁**：
   `generate.py::_apply_vendored_patches()` 每次生成时幂等改写
   `static/stm32g0/FreeRTOS-Kernel/portable/GCC/ARM_CM0/port.c`
   （`= 0xaaaaaaaaUL;` → `= 0UL;`）。子模块 gitlink 必须始终指向
   上游可获取的 commit（当前 `78069a79e`）—— 指向本地 commit 会让
   GitHub Actions checkout 失败（历史 run 35/36）。
2. **驱动模板参数化传输**：`drv_mpu6050` 按 `model.interface` 生成
   I2C 或 SPI 传输宏；模型新增芯片时复用寄存器表。组件/姿态层与总线无关。
3. **Jinja 陷阱**：`{% if %}` 块内的 `{% set %}` 不会泄漏到块外——
   多分支 set 必须写成顶层三元表达式（如 `drv_pwm`/`drv_mpu6050` 里的
   `chans` 计算）。改模板后务必重新生成并跑主机测试。
4. **Python 生成器缩进**：`generator/validator.py` 等文件的校验逻辑在
   循环内（12 空格），apply_patch 时保留原缩进，否则 IndentationError。
5. **`--force` 会删 build/**：重新生成后必须重新 cmake configure。
6. **OpenOCD 残留进程**：probe 报 `0x57 WriteFile` 错误时先
   `Get-Process | Where-Object {$_.ProcessName -match 'openocd|gdb'} |
   Stop-Process -Force`。
7. **上板验证时序**：烧录后立即抓串口可能为空——先开串口再
   `reset run`，等 "System ready" 后再发 CLI 命令。
8. **引脚防冲突三层校验**（新增外设时保留）：
   Pydantic 重复 pin id → MCU 数据库交叉校验（存在性/AF 支持/同 pin
   冲突）→ 外设字段引用校验（cs_pin/de_pin 必须声明、不得跨外设共享，
   UART↔RS485 DE 伴侣除外）。外设引用未声明引脚会报错。
9. **命名约定**：产品名 `HW2C-DevKit`（丝印/简介），工程名
   `hw2c-devkit`（仓库/目录）；嘉立创简介字段 ≤256 字符。

## CI（.github/workflows/build_and_test.yml）

三个 job：Lint（flake8/black，均有容错）→ Build & Test（生成 base +
编译 + 主机测试 + SIL）→ Deploy Docs（MkDocs → GitHub Pages，需
`environment: github-pages`）。子模块 checkout 是历史失败点（见教训 1）。
