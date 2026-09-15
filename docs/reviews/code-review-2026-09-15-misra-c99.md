# hw2c 生成代码 CodeReview — MISRA C:2012 / C99 / 实时性与安全性

- **审查日期**：2026-09-15
- **审查对象**：`output/solenoid_valve_pid_ctrl_demo/`（安全关键示例，PID 压力闭环 + 电磁阀执行器）
- **基线**：`main @ ff939e1`（v0.4.0）
- **验收基准**：`docs/requirements.md` NFR-3（MISRA C:2012 **Mandatory** 规则通过；单函数 ≤100 行、圈复杂度 ≤10）
- **编译工具链**：**arm-none-eabi-gcc 15.2.1**（Arm GNU Toolchain 15.2.Rel1），target `arm-none-eabi`，`-mcpu=cortex-m0plus -mthumb`，**soft-float ABI / 无 FPU**，C 库 newlib-nano（`-specs=nano.specs -specs=nosys.specs`），**未指定 `-std=`（实测按 C23 编译）** —— 完整基线与 ABI 取证见 **§11 附录 A**
- **分析工具**：cppcheck 2.21.0（`misra.py` addon，`--platform=unix32` —— 注意该平台是 **x86/ILP32 ABI**，与目标 ARM EABI 不一致，见 §6.1 与 §11 附录 A.3）；arm-none-eabi-gcc 原生严格告警集与 `-fanalyzer`
- **修订**：v2（2026-09-15）—— 补充 §11 附录 A「编译工具链基线」；新增缺陷 **P2-7**；更正 §6.1 的分析平台参数说明
- **结论**：**不通过**。发现 3 项阻塞级缺陷（含 1 项导致安全闭环完全无法工作、1 项 C99 符合性失效）、4 项严重影响实时性/失效安全的缺陷、7 项中等缺陷；MISRA C:2012 扫描 1306 条违规，且**当前无 CI 集成**（Phase 6 未落地）。

---

## 1. 审查范围与方法

### 1.1 范围界定

审查**由 hw2c 模板生成的应用层代码**，排除 vendor 代码（STM32 HAL、CMSIS、FreeRTOS、Unity、lwrb、hw2c_cli）——后者的合规责任不由本项目承担。

统计（`output/solenoid_valve_pid_ctrl_demo/`）：

| 分类 | 文件数 | 行数 |
|------|--------|------|
| 应用层 `src/*.c/.h` | 31 | ~4,100 |
| 驱动层 `src/drivers/*.c/.h` | 20 | ~3,900 |
| **合计（生成代码）** | **51** | **~8,000** |

### 1.2 使用的方法

1. **静态分析**：cppcheck 2.21 + `misra.py`（MISRA C:2012 规则集），作用域 = 生成代码。
2. **编译验证**：用工程真实 flags 分别以 `-std=c99` 与默认标准编译，比对结果。
3. **链接产物取证**：检查 `build/*.map` 源码级符号，验证运行时库能力假设。
4. **人工语义审查**：并发/原子性、ISR 共享数据、时序预算、失效安全（fail-safe）行为、浮点边界（NaN/Inf）。
5. **模板回溯**：每个问题定位到 `templates/*.j2` 的生成位置，保证修复在上游。

> 说明：本机 `arm-none-eabi-gcc` 默认标准为 **C23**（实测 `__STDC_VERSION__ = 202311L`），因此第 2 步的比对结果是本报告的关键证据之一。

---

## 2. 复现命令

```bash
cd output/solenoid_valve_pid_ctrl_demo

# ① MISRA C:2012 扫描（生成代码）
cppcheck --enable=all --addon="C:/mingw64/share/cppcheck/addons/misra.py" \
  --std=c99 --language=c --inline-suppr \
  --suppress=missingIncludeSystem --suppress=unusedFunction --suppress=missingInclude \
  -DSTM32G0B1VET6 -DSTM32G0B1xx -DUSE_HAL_DRIVER -DHSI_VALUE=16000000 \
  -DHCLK_FREQ=16000000 -DLWRB_DISABLE_ATOMIC \
  -I<STATIC>/stm32g0/HAL/Inc -I<STATIC>/stm32g0/CMSIS/Device/ST/STM32G0xx/Include \
  -I<STATIC>/stm32g0/CMSIS/Core -I<STATIC>/stm32g0/FreeRTOS-Kernel/include \
  -I<STATIC>/stm32g0/FreeRTOS-Kernel/portable/GCC/ARM_CM0 \
  -I<STATIC>/third_party/lwrb -I<STATIC>/hw2c_cli -Iconfig -Isrc -Isrc/drivers \
  src/*.c src/drivers/*.c

# ② C99 符合性验证（在真实 flags 基础上仅加 -std=c99）
arm-none-eabi-gcc -mcpu=cortex-m0plus -mthumb -Wall -Wextra -Wno-unused-parameter \
  -Og -fsyntax-only -std=c99 <同上 -D/-I> src/*.c src/drivers/*.c
# → 报 3 个 error（bool/true 未定义）

# ③ 浮点格式化支持取证
grep -c "_printf_float\|_scanf_float" build/*.map     # → 0
```

---

## 3. P0 — 阻塞级缺陷

### P0-1 ⛔ I2C 总线名大小写不一致 → PID 闭环永远无法运行

**这是本次审查最严重的缺陷：安全闭环在硬件上从第一秒起就处于故障态。**

完整证据链：

| # | 位置 | 内容 |
|---|------|------|
| 1 | `examples/solenoid_valve_pid_ctrl_demo/hardware.yaml:93` | `bus: I2C1`（大写） |
| 2 | `templates/drivers/posix/i2c_api.c.j2:44` | `{ .name = "{{ bus \| lower }}", ... }` → 注册为 **`"i2c1"`** |
| 3 | `templates/app/pid_ctrl_component.c.j2:217` | `i2c_open("{{ fb_peri.bus }}", NULL)` → 传入**未归一化**的 `"I2C1"` |
| 4 | `src/drivers/i2c_api.c:48` | `strcmp(g_i2c_buses[i].name, bus_name) == 0` → **区分大小写**，不匹配 |
| 5 | `src/drivers/i2c_api.c:58` | `return NULL;` |
| 6 | `src/drivers/drv_pressure.c:25-27` | `if (bus == NULL \|\| out == NULL) return -1;` |
| 7 | `src/pid_ctrl_component.c:300-306` | `read_failures++`，累计 ≥8 触发 `PID_FAULT_SENSOR` |

**后果**：`pid_feedback_read()`（`pid_ctrl_component.c:98-107`）**每次调用都返回 -1**。以实际周期 70 ms 计，约 **0.56 s** 后必然 `PID_STAGE_FAULT`；且 `pid_ctrl_start()`（:140）因 `fault != PID_FAULT_NONE` 直接返回 -1。电磁阀永远停在 0% 占空比，压力闭环从未成立。

**为什么现有测试没发现**：主机侧 mock 的语义与生产实现**不等价**——`templates/test/posix_mock.c.j2:290` 的 `i2c_mock_alloc_bus()` 按名字**动态创建**总线，不做注册表查询、不区分大小写。因此单测/SIL 全绿，却掩盖了目标板的确定性故障。

**修复**：
- 必做：`templates/app/pid_ctrl_component.c.j2:217` 加 `| lower`；
- 必做：同样漏掉归一化的还有 `templates/app/mpu6050_component.c.j2:92`（`bus_name`）、`templates/drivers/drv_foc_motor.c.j2:95`（`enc_bus`）——三个模板应统一走同一个归一化过滤器，避免第四次遗漏；
- 建议（纵深防御）：`i2c_open()` 内部改为大小写无关比较，使总线查找不再依赖调用方纪律；
- 附带：`i2c_open()` 被放在**每次采样**里调用（`pid_feedback_read` 第 101 行），每次做线性搜索 + `strcmp`；应在组件 `init` 中取一次句柄并缓存；
- 治理：mock 应改为**复刻生产语义**（固定注册表 + 大小写敏感），否则"测试通过"不具备参考价值。

---

### P0-2 ⛔ 工程未指定 `-std=`，实际按 C23 编译；切到 C99 直接编译失败

**项目宣称遵循 C99，但构建系统从未要求 C99，且不满足 C99。**

证据：

- `templates/project/toolchain.cmake:21`：
  ```cmake
  set(CMAKE_C_FLAGS_INIT "-mcpu=cortex-m0plus -mthumb -fdata-sections -ffunction-sections -Wall -Wextra -Wno-unused-parameter")
  ```
  **无 `-std=`**；实测 `build/build.ninja` 的 `FLAGS` 同样无 `-std=`。
- 实测编译器默认：`__STDC_VERSION__ = 202311L` → **C23**（`gcc -dM -E` 验证）。
- 在真实 flags 上仅追加 `-std=c99`，编译**失败**：
  ```
  src/drivers/drv_rtc.c:27:8:  error: unknown type name 'bool'
  src/drivers/drv_rtc.c:27:41: error: 'false' undeclared here (not in a function)
  src/drivers/drv_rtc.c:426:17: error: 'true' undeclared (first use in this function)
  ```
- 根因：`templates/drivers/drv_rtc.h.j2` 只 `#include "stm32g0xx_hal.h"`，**缺少 `<stdbool.h>`**。`bool/true/false` 在 C23 中是关键字，所以"侥幸能编"。
- 同类风险面：`component_bus.c`、`drv_usart2.c`、`param_registry.c`、`power_mgr.c`、`telemetry.c` 使用 `bool` 却未自己包含 `<stdbool.h>`——目前多数靠头文件间接引入（`component_bus.h` / `param_registry.h` / `power_mgr.h` 有包含），属**依赖传递的偶然**，一旦头文件关系变动即断裂。

**后果**：MISRA C:2012 的合规基线是 C99（或 C90）；以 C23 编译意味着 C99 约束（如隐式函数声明、`//` 注释范围、整数提升）**从未被验证**。任何一次工具链升级或显式 `-std` 引入都可能使全部示例编译失败。

**修复**：
- `templates/drivers/drv_rtc.h.j2` 增加 `#include <stdbool.h>`；
- 全量审计 `.h.j2`，凡对外暴露或用 `bool` 者显式包含 `<stdbool.h>`（当前有 10 个模板已包含，需补齐缺口）；
- `templates/project/toolchain.cmake` 增加 `-std=c99 -pedantic`（或 `-std=gnu99`，两者均满足 MISRA C:2012 基线）；
- CI 中显式断言 `-std=c99` 下零错误、零警告（见 P0-3 与 §6.2）。

---

### P0-3 ⛔ 栈水位监控恒定上报 0（NFR-4 声称"栈水位可由遥测观测"）

`src/telemetry.c:126-129`：

```c
snap->tasks[i].peak_usage = (uint16_t)(
    task_stats[i].usStackHighWaterMark > 0
    ? 0  /* usStackHighWaterMark is remaining, not peak */
    : 0);
(void)snap->tasks[i].peak_usage;  /* suppressed: not directly available on all configs */
```

**三元的两个分支都是 `0`**，即 `peak_usage` 恒为 0。同时 `usStackHighWaterMark` 语义是**剩余**空间，正确算法应为 `stack_size - usStackHighWaterMark`。

**后果**：栈溢出监测**永远报告"健康"**。安全性上，一个恒真的监控比没有监控更危险——它会让集成方据此排除栈溢出假设，而实际溢出的症状（HardFault / 变量被破坏）会被错误归因。NFR-4 的验收项"栈水位可由遥测观测"**不成立**。

**附带 MISRA 违规**：Rule 2.2（死代码）、Rule 14.3（控制表达式不变）、Rule 17.7（返回值被 `(void)` 抑制）。

**修复**：`templates/src/telemetry.c.j2` 改为计算真实水位（`usStackHighWaterMark` 与任务栈大小之差）；若确实取不到栈大小，则**上报剩余量并更名为 `stack_free_min`**，而不是伪装成"使用量峰值"。同时移除 `(void)` 抑制。

---

## 4. P1 — 严重影响实时性与失效安全

### P1-1 🔴 控制回路 `dt` 与真实执行周期不一致（名义 62 ms vs 实际 70 ms）

| 环节 | 位置 | 值 |
|------|------|-----|
| PID `dt`（名义周期） | `templates/app/pid_ctrl_component.c.j2:42,161-165` | `control_period_ms` 默认 **62** → `pid_init(..., 62/1000.0f, ...)` |
| 组件调度周期 | `templates/src/component_registry.c.j2:37` | `.period_ms = 62` |
| 步进任务轮询间隔 | `templates/src/main.c.j2:562` | `vTaskDelay(pdMS_TO_TICKS(10))` → **10 ms** |
| 判定逻辑 | `component_registry.c:142-143` | `(now - c->last_tick) >= c->period_ms` 且 `c->last_tick = now`（**无累加器**） |

**机理**：10 ms 轮询粒度下，62 ms 的相位无法命中，首次满足条件发生在 **70 ms**。故 PID 以为控制周期是 62 ms，实际是 70 ms。

**后果（量化）**：
- 积分增益有效值偏差 `70/62 − 1 = **+12.9%**`，微分增益偏差 `1 − 62/70 = −11.4%`，且为**系统性偏差**，不随运行时间平均掉；
- `setpoint_rate = 20.0 (单位/秒)` 的斜坡约束同样慢 12.9%（`pid_math.c:67-75` 用 `setpoint_rate * p->dt` 计算单步上限）；
- 闭环稳定裕度下降，且**随 `period_ms` 配置漂移**——由于 `period_ms` 是 YAML 可配项，任何非 10 ms 整数倍的配置都会引入不同幅度的误差，导致**无法做时序分析**；
- `last_tick = now` 缺少相位补偿，误差还会随其它组件（shell 50 ms / led 50 ms / btn 10 ms）的执行耗时累加抖动。

**修复（模板层）**：
- 首选：`pid_ctrl_step()` 用实测间隔驱动 `p->dt`——`dt = (now - ctx->last_step_ms) / 1000.0f`，首次用名义值，并对异常 `dt`（0 或过大）做钳制；
- 或：`component_step_all()` 改用"下次截止时刻"累加（`c->last_tick += c->period_ms`）以消除相位漂移，并让步进任务周期整除最小 `period_ms`；
- 二者都应在模板生成，而非要求用户在 YAML 里手工对齐。

---

### P1-2 🔴 I2C 阻塞轮询 + 100 ms 超时，且无看门狗 —— 控制回路可无限期停摆

证据：

- `src/drivers/i2c_api.c:35` — `.default_timeout_ms = 100`
- `src/drivers/i2c_api.c:92-96` — 以空转计数作超时：
  ```c
  for (volatile uint32_t i = 0; i < 20000U; i++) {
      if (__HAL_I2C_GET_FLAG(h->hi2c, I2C_FLAG_BUSY) == RESET) return 0;
  }
  ```
- `src/drivers/i2c_api.c:141-143` — `HAL_I2C_Mem_Read(..., i2c_timeout(h, timeout_ms))`，**轮询模式，独占 CPU**
- `src/pid_ctrl_component.c:98-107` — 每次采样都走这条路径，且运行在 `component_step_task`（优先级 2）中
- `CMakeLists.txt` 的 `APP_SOURCES` **不含 `drv_iwdg.c`** → 本 demo **未启用独立看门狗**

**时序预算（Cortex-M0+ @16 MHz）**：

| 场景 | 耗时 | 对比控制周期 70 ms |
|------|------|---------------------|
| 总线正常（BUSY 立即清零） | 数周期，可忽略 | — |
| 总线卡死，`ensure_ready` 空转满 20000 次 | ≈ **10–20 ms** | 14–29% |
| `HAL_I2C_Mem_Read` poll 超时 | **100 ms** | **143%** |
| **最坏合计** | **≈ 110–120 ms** | **≈ 1.7 个控制周期** |

**后果**：
- 单次最坏阻塞已超过控制周期，闭环**丢拍**；`component_step_task` 是单任务串行轮询，期间 shell/led/btn/PID **全部停摆**；
- 空转时长由 `-O` 等级与主频决定，**不是可论证的时间界**（MISRA Dir 4.1"最小化运行时错误"、Rule 1.3）；
- 最危险的是**无 IWDG**：SDA 被从机拉低等硬故障下 I2C 永久卡死（`ensure_ready` 复位 PE 后仍 BUSY 时返回 -6，但 HAL 侧仍可能长时间占用），PID 输出**冻结在最后一次占空比**，电磁阀可能长期保持通电——对驱动气体阀的安全功能，这是典型的"MCU 失控但负载仍被驱动"。

**修复**：
- 模板改为**中断/DMA + `xSemaphoreTake(..., timeout)`**，让阻塞时让出 CPU；
- `ensure_ready` 改用有确定时基的超时（`HAL_GetTick()` 或 DWT 周期计数），并明确其上限；
- 安全关键示例**默认启用 IWDG**，由控制任务心跳喂狗，使控制回路停摆能触发复位；
- 为每个组件引入**执行时间预算**并在超预算时降级/告警（当前无任何监测）。

---

### P1-3 🔴 HardFault / 初始化失败均为 `while(1)` 静默挂死，执行器保持最后状态

证据：

- `src/stm32g0xx_it.c:9` — `void NMI_Handler(void) { while(1); }`
- `src/stm32g0xx_it.c:10-15` — HardFault 捕获 SP 到局部变量后 `while(1)`：
  ```c
  void HardFault_Handler(void) {
      uint32_t sp;
      __asm__("mov %0, sp" : "=r"(sp));
      while(1);
  }
  ```
- `src/main.c:79 / 85 / 100 / 117` — `if (HAL_...() != HAL_OK) while(1);`

**后果（失效安全视角，最严重的一类）**：CPU 停死，**但 TIM2 是硬件外设，PWM 仍在持续输出**——电磁阀保持通电；叠加 P1-2 的"无 IWDG"，系统**永不复位**。即：一旦故障，负载被无限期驱动，且无自恢复路径。

**附带问题**：
- `__asm__("mov %0, sp")` 是 GCC 扩展（MISRA Rule 1.2"不应使用语言扩展"、Dir 4.3"汇编需封装隔离"）；
- 结果写入局部变量后立即 `while(1)`，该变量会被优化掉，**调试价值为零**（实际从未有人能据此定位故障）。

**修复（模板生成统一故障处置原语）**：
```
hw2c_fault_trap(code):
  1) 将所有安全关键输出置为失效安全态（solenoid_pwm_set_duty(ch,0) / 关断 GPIO）
  2) 故障码写入 RTC 备份寄存器或 NVM（跨复位可查）
  3) 触发 NVIC_SystemReset()（或等待 IWDG 复位）
```
HardFault 的 SP 捕获改为写入 `__attribute__((used))` 的固定全局，供复位后读取现场。

---

### P1-4 🔴 `power sleep` 命令向操作员谎报状态；`sleep_compat` 安全互锁实际未生效

证据：

- `src/sleep.c:15-26` — 两个函数都是**桩**：
  ```c
  void vPortSuppressTicksAndSleep(TickType_t xExpectedIdleTime) { (void)xExpectedIdleTime; return; }
  uint32_t power_sleep_now(power_mode_t mode) { (void)mode; return 0; }
  ```
- `src/drivers/drv_shell.c:595-604`：
  ```
  out("Entering %s (wake by RTC 1s / UART RX / button)...\r\n", ...);
  uint32_t elapsed = power_sleep_now(m);      // 桩：什么都不做，返回 0
  out("Woke up after %lu ms, RTC keeps time, RAM intact.\r\n", elapsed);
  ```
  → 操作员看到 **"Entering STOP1 ..."** 紧跟 **"Woke up after 0 ms"**，而 MCU **从未离开 RUN**。
- `src/drivers/drv_shell.c:598-602` 的注释声称"RTC 日历与 HAL tick 在 `power_sleep_now()` 内部补偿"——**与实现不符**（桩无任何补偿）。
- `config/FreeRTOSConfig.h:56` — `configUSE_TICKLESS_IDLE 0`；且**全 `src/` 无 `__WFI()`** → 空闲任务纯空转，CPU 始终满速。
- `src/power_mgr.c:100-146` 的 `power_mgr_allowed_depth()`（约 60 行 `sleep_compat` 聚合逻辑）**无任何消费者**：`power_sleep_now()` 忽略入参，`drv_shell.c:597` 也不做 `mode <= allowed` 校验。

**后果**：
1. **功耗验证结论无效**——CLI/遥测显示已进入 STOP1，实际全程 RUN，操作员与测试报告都被误导；
2. **安全互锁是纸面的**——`sleep_compat`（`component_registry.c` 中每个组件都声明了 `"RUN"`）看起来是"PID 运行时禁止休眠"的保护，但**没有任何强制力**。一旦将来接入真实 `__WFI`/STOP 实现而忘记串接这条链，控制回路会在**执行器通电状态下被停机**（STOP 模式下 TIM2 时钟停止，PWM 输出冻结），这是明确的危险状态；
3. 注释与实现不一致（MISRA Dir 4.4"不应注释掉代码"、文档可靠性）。

**修复**：
- `power_sleep_now(mode)` 必须先校验 `mode <= power_mgr_allowed_depth()`，不满足返回错误码并使 CLI 明确报错；
- 桩实现应返回 **`-1`（未实现）** 而非 `0`（成功）——"未实现"与"成功"必须可区分；
- 若确认本 demo 不支持低功耗，则 CLI 应**移除或明确标注该命令为未实现**，而不是打印虚假的进入/唤醒信息；
- 修正 `drv_shell.c:598-602` 的注释。

---

## 5. P2 — 并发正确性、资源与 MISRA

### P2-1 🟠 ISR 更新的共享变量缺 `volatile`（全仓仅 1 处 `volatile`）

证据：

- `src/drivers/drv_rtc.c:28` — `uint32_t rtc_uptime_sec = 0;`（**无 `volatile`**，注释明示"increment in WakeUp 1s ISR"）
- ISR 内写入：`drv_rtc.c:447` `rtc_uptime_sec++;`（及 :459 的读-改-写）
- 任务上下文读取：`drv_rtc.c:57,169,170,178,219`、`power_mgr.c:103`、`telemetry.c:84`

**具体后果（可推演的失效）**：`power_mgr.c:104-110`
```c
uint32_t now = rtc_uptime_sec;
if (now == g_power_mgr.last_update) return g_power_mgr.current_allowed;  // 缓存
g_power_mgr.last_update = now;
```
若编译器把 `rtc_uptime_sec` 缓存进寄存器（合法，因未声明 `volatile`），`now` 恒等于 `last_update` → **缓存永不失效 → `sleep_compat` 允许深度被永久冻结**。

**现状评估（诚实定性）**：本工程无 LTO、跨 TU 访问，当前大概率"碰巧工作"；但这是**未受保护的隐式契约**：一旦引入 `-flto`、改 `-O` 等级或把该变量改为文件内 `static`，即静默失效。全仓唯一 `volatile` 是 `i2c_api.c:92` 的循环计数器，说明问题不是个别遗漏，而是**模板缺少并发标注约定**。

**修复**：凡被 ISR 修改、被任务读取的全局一律加 `volatile`；`rtc_uptime_sec` 在读侧做快照读取；在 CI 增加"MISRA Dir 4.1 / volatile 缺失"自检。

---

### P2-2 🟠 ISR 与 LED 组件争用同一 GPIO，且含调试残留

证据：

- `src/stm32g0xx_it.c:21-22`：
  ```c
  /* DEBUG: toggle LED to confirm ISR entry */
  HAL_GPIO_TogglePin(GPIOC, GPIO_PIN_0);
  ```
  无条件执行，且位于 `xTaskGetSchedulerState()` 判断**之前**。
- `src/led_component.c:35-36` — LED 组件同样拥有 `.port = GPIOC, .pin = GPIO_PIN_0`。
- 模板出处：`templates/src/stm32g0xx_it.c.j2:29`。

**后果**：
1. 每次按键中断都翻转 LED 引脚，与 LED 组件的周期写入**争用**；`HAL_GPIO_TogglePin` 是"读 ODR + 写 BSRR"的读-改-写，ISR 侧的 RMW 会**丢失任务侧的写入**（两组灯光状态互相打架）；
2. **调试代码进入了交付生成物**——这是模板级的卫生问题，会影响所有使用按键的示例；
3. 无调度器保护，早期启动阶段即在未初始化引脚上翻转。

**修复**：删除 `stm32g0xx_it.c.j2:29` 的调试翻转；若确需"ISR 活动"指示，使用独立引脚并由模板显式声明所有权（**不**注册为 led 组件），避免共享。

---

### P2-3 🟠 `xQueueCreate` / `xTaskCreate` 返回值未检查

证据：

- `src/event_mgr.c:80-81` — 两个队列创建**无 NULL 检查**；`EventMgr_Init()` 返回 `void`，无法上报失败
- `src/main.c:215 / 222 / 232` — 三处 `xTaskCreate` **忽略返回值**
- `config/FreeRTOSConfig.h` — `configTOTAL_HEAP_SIZE = 11264` B

**堆预算（`event_t` = 12 B，实测结构体）**：

| 项目 | 用量 |
|------|------|
| `event_queue` 100×12 + 开销 | ≈ 1.28 KB |
| `btn_queue` 16×12 + 开销 | ≈ 0.27 KB |
| 3 个任务栈 3×512 words×4 | 6.14 KB |
| 3 个 TCB | ≈ 0.27 KB |
| **合计 / 余量** | **≈ 7.96 KB / 余量 ≈ 3.3 KB** |

余量约 3.3 KB，**偏紧**（后续新增组件/队列容易触顶）。

**后果**：若队列创建失败，`event_queue = NULL` → `EventMgr_Task` 中 `xQueueReceive(NULL, ...)` 触发 FreeRTOS 断言或未定义行为；若 **`EventMgr_Task` 创建失败**，则**状态机永不运行**，而 `main()` 仍打印 `"System ready"` —— 启动自检给出假阳性。

**修复**：模板在创建后检查句柄/返回值，失败时进入 P1-3 的统一故障处置；`EventMgr_Init()` 改为返回状态码，`main()` 校验并上报（MISRA Rule 17.7：非 void 返回值应被使用）。

---

### P2-4 🟠 `pid_clampf()` 不处理 NaN → 未定义行为可直达执行器

证据：

- `src/pid_math.c:11-16`：
  ```c
  float pid_clampf(float v, float lo, float hi) {
      if (v < lo) return lo;
      if (v > hi) return hi;
      return v;          /* NaN 两个比较均为假 → 原样返回 NaN */
  }
  ```
- 传播链：`drv_pressure.c:38`（原始字节线性换算，**未做量程有效性校验**）→ `pid_update()` → `pid_clampf()` → `pid_apply_output()`（`src/pid_ctrl_component.c:113-115`）：
  ```c
  uint32_t duty = (u < (float)ctx->duty_min) ? 0u
                : (u > (float)ctx->duty_max) ? ctx->duty_max
                                             : (uint32_t)u;   /* NaN 同样穿透所有比较 */
  ```
- 同型问题：`src/pid_ctrl_component.c:406` `(int32_t)(ctx->process_value * 10.0f)`

**标准依据**：**C99 §6.3.1.4**——把 NaN 或无法用目标类型表示的浮点值转换为整型是**未定义行为**（对应 MISRA Rule 1.3"不得出现未定义行为"、Rule 10.8）。

**后果**：任何使反馈量为 NaN 的路径（传感器返回全 1 位模式、未初始化读、除零）都会把 NaN 送进 `(uint32_t)` 转换，结果依编译器/优化等级而定，可能得到一个巨大的占空比 → 执行器被错误驱动。

**修复**：
- `pid_clampf()` 显式处理 `isnan/isinf`，返回**安全侧**值（如 `lo`）；
- `pid_apply_output()` 使用**对 NaN 安全**的判定（`if (!(u >= 0.0f)) u = 0.0f;`——注意不能写成 `if (u < 0.0f)`，那对 NaN 同样为假）；
- `pid_feedback_read()` 增加量程合理性校验（越界即视为读失败，纳入 `read_failures` 计数）。

---

### P2-5 🟠 失效响应非即时：`pid_trip_fault()` 不清零执行器输出

证据：`src/pid_ctrl_component.c`

- `pid_trip_fault()`（:269-284）设置 `heat_duty = 0; cool_duty = 0;`、`stage = FAULT`、发事件，但**不调用** `pid_apply_output(0.0f)` 或 `solenoid_pwm_set_duty()` → 实际关断依赖后续的 `case PID_STAGE_FAULT: pid_apply_output(0.0f);`（:399-401）兜底。
- 因此存在"输出已给、故障后置"的窗口：
  - 调参分支内触发（:337-339 `rc == -1`、:362-364）时，本周期已应用的 `duty_out` **要到下一周期（≈70 ms）才归零**；
  - `PID_STAGE_RAMP_UP` 中，超时判定在 `pid_apply_output()` **之后**（:384-390）——同样的顺序缺陷。
- `pid_ctrl_terminate()`（:409-417）只置 `heat_duty = 0; initialized = 0;`，**从不关断 PWM** → 组件被终止后阀门保持通电（安全缺陷）。

**修复**：
- `pid_trip_fault()` 内**立即**调用 `solenoid_pwm_set_duty(ch, 0)`（故障处置不得依赖状态机的下一拍）；
- 把安全检查（超压、超时、读失败）**前移**到 `pid_apply_output()` 之前——原则是"先判安全、后给输出"；
- `pid_ctrl_terminate()` 显式输出失效安全态。

---

### P2-6 🟠 手动（CLI）与自动（PID）对同一执行器无仲裁

证据：

- `src/pid_ctrl_component.c:118` — `(void)solenoid_pwm_set_duty((uint8_t)1, duty);`（自动路径）
- `src/drivers/drv_shell.c:450-451` — `pwm` 命令同样调用 `solenoid_pwm_set_duty(...)`（手动路径）
- `src/drivers/drv_solenoid_pwm.c` 的 `g_pwm_channels[]` 中 `percent` / `pulse` 的读-改-写在**两个不同优先级任务**间无互斥：`cli` = 优先级 4，`comp_step` = 优先级 2；`pwm_reprogram()` 是"计算 pulse + 写 CCR"两步操作。

**后果**：闭环运行中执行 CLI 手动改占空比，会被 PID 下个周期覆盖（或反之，造成输出跳变）；两条控制路径同时写硬件，**没有所有权或互锁定义**。对执行器类安全功能，这属于典型的"控制权竞争"缺陷。

**修复**：
- `drv_solenoid_pwm` 内部用互斥（或 `taskENTER_CRITICAL`）保护读-改-写与"算+写"序列；
- 定义并强制"手动优先 / 自动优先"策略：PID 运行期间拒绝手动写（返回错误并提示先 `pid stop`），或手动写自动降级为 STANDBY——**无论选哪种，必须在生成代码中显式实现**。

---

### P2-7 🟠 POSIX 总线 API 的长度参数宽度与 HAL 不一致 → 静默截断

> **本项由 §11 附录 A 的 ABI 正确检查发现，首版遗漏**（原因见 A.4：未开启 `-Wconversion`）。

证据：

- `src/drivers/i2c_api.c:125-127, 146-148, 169-170, 186-187` —— `i2c_mem_read` / `i2c_mem_write` / `i2c_transmit` / `i2c_receive` 的长度参数均声明为 **`uint32_t len`**；
- `static/stm32g0/HAL/Inc/stm32g0xx_hal_i2c.h:634-635` —— `HAL_I2C_Mem_Read(..., uint8_t *pData, uint16_t Size, uint32_t Timeout)`，其中 **`Size` 只有 16 位**，而模板传入的 `len` 是 `uint32_t` → **隐式截断**。编译器直接指出：
  ```
  src/drivers/i2c_api.c:143:45: warning: conversion from 'uint32_t' to 'uint16_t' may change value [-Wconversion]
  src/drivers/i2c_api.c:164:57 / 182:63 / 199:51  同型
  ```
- `src/drivers/uart_api.c:72, 86` —— 同样声明 `uint32_t len`，而 `stm32g0xx_hal_uart.h:1630` 的 `HAL_UART_Transmit(..., uint16_t Size, uint32_t Timeout)` 亦为 16 位；`src/drivers/drv_usart2.c:32` 直接把 `strlen(str)`（`size_t`）作为长度传入（`[-Wconversion]`）。
- **模板级根因**（影响所有生成工程）：`templates/drivers/posix/i2c_api.h.j2:44,46,51,54` 与 `templates/drivers/posix/i2c_api.c.j2:126,133,142,163,185,202`。

**后果**：调用方按 API 声明（32 位）传长度，底层按 16 位执行 —— `len = 65536 + n` 变为 `n`，**读写长度被静默截断**。当前示例每次只读写 1–2 B 故未触发，但对"总线 API 是通用抽象、一总线多设备"这一核心设计目标，这是**接口契约与实现不符**（MISRA Rule 10.1 / 10.3 / 10.4、Dir 4.1）。同样因为主机 mock 不检查该宽度，**测试不会发现**（与 P0-1、§6.3 同一病灶）。

**修复**：
- 二选一保持一致：把 API 长度参数改为 `uint16_t`（与 HAL 对齐，并对 `len == 0` / 超长返回参数错误码），或在实现内**显式校验**（`if (len > UINT16_MAX) return -5;`）后再传入；
- 所有 `HAL_*` 边界禁止隐式窄化，按 MISRA Rule 10.3 显式转换；
- CI 开启 `-Wconversion -Wsign-conversion`（见 §11 A.6）。

---

## 6. 规范符合性评估

### 6.1 MISRA C:2012 扫描结果

- 扫描工具：cppcheck 2.21.0 + `misra.py`，作用域 = 生成代码（`src/` 全部）。
- **违规总数：1306 条**。

命中规则 Top（按次数）：

| 规则 | 次数 | 类别 | 说明 |
|------|------|------|------|
| 17.3 | 261 | **Mandatory** | 函数不应被隐式声明 —— **判定为误报**，详见下方说明 |
| 15.5 | 233 | Advisory | 函数应只有一个出口点 |
| 15.6 | 150 | Required | 迭代/分支语句体必须是复合语句（大括号） |
| 12.1 | 145 | Advisory | 应显式括号化运算符优先级 |
| 10.4 | 72 | Required | 常规算术转换的操作数须属同一基本类型类别 |
| 8.7 | 53 | Advisory | 仅单个 TU 使用的函数/对象不应有外部链接 |
| 8.4 | 46 | Required | 外部链接定义须有可见的兼容声明 |
| 14.4 | 35 | Required | `if`/迭代语句控制表达式须为基本布尔类型 |
| 21.6 | 33 | Required | **不得使用标准库输入/输出函数**（`snprintf`/`vsnprintf`/`sscanf`） |
| 12.3 | 29 | Advisory | 不得使用逗号运算符 |
| 11.5 / 21.1 / 2.5 | 22 / 19 / 19 | Required | `void*` 转换 / 保留标识符 / 未使用宏 |

按文件分布（Top）：`drv_rtc.c` 239、`drv_shell.c` 155、`pid_ctrl_component.c` 107、`param_registry.c` 95、`drv_log.c` 91。

> **关于唯一的 Mandatory 命中（17.3）：判定为假阳性。**
> 抽查 `btn_component.c:134`（`xQueueReceive`）、`:138`（`xTaskGetTickCount`）等 261 处，被"隐式声明"的函数均来自 FreeRTOS / HAL 头文件；cppcheck 未成功解析这些复杂头文件（本次已 `--suppress=missingInclude`），从而丢失声明。工程实际可编译（`-Wall -Wextra` 下报 0 条隐式声明），可佐证其为工具解析产物。
>
> **但这恰恰暴露了 NFR-3 的验收缺口**：当前**不存在可用的 MISRA 分析配置**（无 `misc`/`--rule-texts`、无头文件解析策略、无 CI 集成）。因此"MISRA Mandatory 规则通过"**既未被证实、也无法被证伪**——按当前证据应判定为**未通过（未验证）**。

其他工具局限（不可作为合规声明）：cppcheck 的 MISRA 覆盖不完整（不含头文件级与跨 TU 规则），Phase 6 规划的"Clang Static Analyzer"亦未接入。

### 6.2 C99 符合性：**不通过**

| 检查项 | 结果 |
|--------|------|
| 构建显式指定 C 标准 | ❌ 无 `-std=`（`templates/project/toolchain.cmake:21`） |
| 实际编译标准 | C23（`__STDC_VERSION__ = 202311L`） |
| `-std=c99` 下能否编译 | ❌ **3 个 error**（`drv_rtc.c:27,426`：`bool`/`true` 未定义） |
| `-std=c99` 下警告数 | 1 条（`param_registry.c:331` `-Wtype-limits`，见下） |
| `-Werror` | ❌ 未启用（警告不阻断构建） |

**额外发现（`src/param_registry.c:331`）**：
```
warning: comparison of unsigned expression in '< 0' is always false [-Wtype-limits]
```
对**无符号**量做 `< 0` 判定恒为假——该参数下界校验**完全失效**。这是编译器直接指出的逻辑缺陷（MISRA Rule 14.3 / 10.1 类问题），需确认该参数是否应为有符号类型。

### 6.3 浮点格式化在目标上不可用（高置信度）

- `templates/project/toolchain.cmake:24` 链接参数含 `-specs=nano.specs`，但**无 `-u _printf_float` / `-u _scanf_float`**。
- 链接产物取证：`grep -c "_printf_float\|_scanf_float" build/*.map` → **0**；map 中浮点相关符号仅 `floatunsisf`、`param_get_float/set_float`。
- 而生成代码大量使用浮点转换：`pid_ctrl_component.c:281,329,356,380`（`%.1f/%.2f/%.3f`）、`drv_shell.c:354,371,384,396-397`（`sscanf("%f")`）等。

**后果**：
1. 日志/遥测中的浮点字段在目标板上**输出为空或异常**——直接影响故障诊断与现场调试（`log_error("PID FAULT %lu: value=%.1f ...")` 恰好是最需要它工作的地方）；
2. `sscanf(argv[2], "%f", &kpa)` 在目标上**解析失败** → CLI 的 `pid set <kPa>`、`pid steptune <duty> <stop>` **不可用**；而主机测试用 glibc，**全部通过** → 又一个"测试与目标不一致"（同 P0-1 的病灶）。

**修复**：工具链模板补 `-u _printf_float -u _scanf_float`（约 +6~10 KB Flash），或改用**整数定点**格式化（更契合 MISRA Dir 4.6 与无 FPU 的 Cortex-M0+，也更有实时性优势）。无论选哪种，都需为该能力加一条**目标侧冒烟测试**。

### 6.4 NFR-3 复杂度目标：未度量

NFR-3 要求"单函数 ≤100 行、圈复杂度 ≤10"，当前**无任何度量**。目测已逼近或超限者：`pid_ctrl_step()`（约 115 行）、`drv_rtc.c`（948 行）、`drv_shell.c`（778 行）。建议接入 `lizard` / `pmccabe` / cppcheck `--enable=style`，并把阈值写入 CI 门禁。

---

## 7. 实时性时序分析

### 7.1 任务与优先级布局

| 任务 | 优先级 | 栈 | 周期 | 主要工作 |
|------|--------|-----|------|----------|
| `event_mgr` | **7**（`configMAX_PRIORITIES-1`） | 512 w | 事件驱动 | `statemachine_process`；每 30 s `telemetry_log_snapshot`（**snprintf 格式化**）；每个事件一次 `log_debug` |
| `cli` | 4 | 512 w | 事件驱动 | CLI 解析（`sscanf`）、可写执行器 |
| `comp_step` | **2** | 512 w | 轮询 10 ms | **全部组件 step：shell(50) / led(50) / btn(10) / pid_ctrl(62→70)** |

### 7.2 识别到的时序风险

1. **单任务串行轮询**（P1-1、P1-2）：4 个组件共享 `comp_step`，任一 `step()` 阻塞（I2C 最坏 ≈110–120 ms）即拖停其余全部组件；**无执行时间预算、无超限检测**。
2. **最高优先级任务做重活**：`event_mgr`（优先级 7）执行 `snprintf` 格式化与 `uxTaskGetSystemState`，期间可**任意抢占优先级 2 的控制任务**——典型的优先级倒置风险，且无优先级继承保护（无互斥参与）。
3. **每个事件一次 `log_debug`**（`event_mgr.c:90`，含 1 Hz RTC tick）：最高优先级路径上的日志开销，放大 2 的效应。
4. **周期相位无补偿**（P1-1）：`last_tick = now` 使误差随其他组件耗时累积，闭环抖动不可预测。
5. **无看门狗、无栈监控**（P1-2、P0-3）：最坏路径**无界**，且任何停摆都无法被检测/恢复。

---

## 8. 修复优先级与模板映射

| 编号 | 级别 | 问题 | 修复位置（模板） |
|------|------|------|------------------|
| P0-1 | ⛔ | I2C 总线名大小写不一致 | `templates/app/pid_ctrl_component.c.j2:217`（+`\| lower`）；`templates/app/mpu6050_component.c.j2:92`；`templates/drivers/drv_foc_motor.c.j2:95`；`templates/drivers/posix/i2c_api.c.j2:63`（大小写无关比较）；`templates/test/posix_mock.c.j2`（复刻生产语义） |
| P0-2 | ⛔ | 未指定 C 标准 / C99 编译失败 | `templates/project/toolchain.cmake:21`（`-std=c99 -pedantic`）；`templates/drivers/drv_rtc.h.j2:5`（补 `<stdbool.h>`）；审计全部 `.h.j2` |
| P0-3 | ⛔ | 栈水位恒为 0 | `templates/src/telemetry.c.j2`（改用 `stack_size - usStackHighWaterMark`） |
| P1-1 | 🔴 | PID `dt` 与真实周期不符 | `templates/app/pid_ctrl_component.c.j2:42,161-165`（实测 `dt`）；`templates/src/component_registry.c.j2:98`（deadline 累加器）；`templates/src/main.c.j2:562` |
| P1-2 | 🔴 | I2C 阻塞 + 无看门狗 | `templates/drivers/posix/i2c_api.c.j2:92-96,141-143`（中断/DMA + 信号量；确定时基超时）；`templates/project/CMakeLists.txt.j2`（安全关键 demo 默认含 `drv_iwdg.c`） |
| P1-3 | 🔴 | 故障态静默挂死、输出未关断 | `templates/src/stm32g0xx_it.c.j2:9-15`；`templates/src/main.c.j2`（`while(1)` → 统一 `hw2c_fault_trap()`） |
| P1-4 | 🔴 | 功耗状态谎报 / `sleep_compat` 未生效 | `templates/src/sleep.c.j2:15-26`（桩返回 `-1`；校验 allowed depth）；`templates/drivers/drv_cli.c.j2`（`power sleep` 报告）；`templates/src/power_mgr.c.j2` |
| P2-1 | 🟠 | ISR 共享变量缺 `volatile` | `templates/drivers/drv_rtc.c.j2:28`；全局并发标注约定 |
| P2-2 | 🟠 | ISR 调试 LED 与组件争用 GPIO | `templates/src/stm32g0xx_it.c.j2:29`（删除） |
| P2-3 | 🟠 | 队列/任务创建未检查返回值 | `templates/src/event_mgr.c.j2:77-81`；`templates/src/main.c.j2:215,222,232` |
| P2-4 | 🟠 | NaN 穿透 clamp → UB | `templates/app/pid_math.c.j2:11-16`；`templates/app/pid_ctrl_component.c.j2:113-115,406` |
| P2-5 | 🟠 | 故障未即时关断执行器 | `templates/app/pid_ctrl_component.c.j2:269-284,384-390,409-417` |
| P2-6 | 🟠 | 手动/自动无仲裁 | `templates/drivers/drv_solenoid_pwm.c.j2`（互斥 + 优先级策略）；`templates/drivers/drv_cli.c.j2` |
| P2-7 | 🟠 | 总线 API 长度参数窄化（`uint32_t` → HAL `uint16_t`） | `templates/drivers/posix/i2c_api.h.j2:44,46,51,54`；`templates/drivers/posix/i2c_api.c.j2:126,133,142,163,185,202`；`templates/drivers/posix/uart_api.*.j2` |
| §11-A.4 | — | 34 处函数缺前置声明（`-Wmissing-prototypes`） | 各组件 `*.h.j2` 生成本组件生命周期原型；`templates/src/stm32g0xx_it.c.j2` |
| §11-A.2 | — | 主机（x86/glibc）与目标（ARM EABI/newlib-nano）ABI 不一致 | `templates/test/*`：主机构建须同步 `-std=`/ABI 开关，并对 ABI 敏感行为补目标侧冒烟测试 |
| §6.2 | — | `-Wtype-limits` 恒假判定 | `templates/src/param_registry.c.j2:331` |
| §6.3 | — | 浮点格式化不可用 | `templates/project/toolchain.cmake:24`（`-u _printf_float -u _scanf_float`）或改定点 |

---

## 9. 复审验收清单

修复后需通过以下门禁（建议全部落在 CI）：

- [ ] **P0-1**：目标板 `pid` 命令可正常闭环，不再出现 `PID FAULT 2`；mock 与生产语义一致性测试存在且生效
- [ ] **P0-2**：`-std=c99 -pedantic` 零错误零警告；`-Werror` 开启
- [ ] **P0-3**：遥测 `peak_usage` 在人为制造深栈调用时能反映真实水位
- [ ] **P1-1**：实测控制周期与 `p->dt` 偏差 <5%（示波器或 GPIO 打点验证）
- [ ] **P1-2**：拔掉 I2C 从机，控制任务仍能保持周期性（看门狗不复位也不闭锁）
- [ ] **P1-3**：人为触发 HardFault，验证 PWM 输出被关断且系统复位、故障码可读
- [ ] **P1-4**：`power sleep stop1` 在未实现时返回明确错误，不再打印"Woke up"
- [ ] **P2 全项**：MISRA 扫描接入 CI，生成代码违规数纳入基线并只降不升
- [ ] **§6.3**：目标板打印一条含 `%.1f` 的日志，人工确认数值正确
- [ ] **§6.4**：复杂度门禁（函数 ≤100 行、圈复杂度 ≤10）在 CI 生效

---

## 10. 总体评价

生成代码的**架构与工程化程度值得肯定**：六层 YAML 分层清晰、组件生命周期统一、POSIX 风格总线抽象合理、SIL/主机测试齐备，`i2c_bus_ensure_ready()` 这类"防止阻塞总线挂死系统"的防御性设计说明作者具备嵌入式安全意识。

但本次审查表明，**"生成的代码可编译且测试通过"与"生成的代码在目标板上安全可用"之间仍有实质差距**，且差距集中在三个系统性病灶：

1. **主机测试与目标语义不等价**（P0-1、§6.3）——mock 与 newlib-nano 的差异使两类确定性目标故障在 CI 中完全不可见。**这是本次审查暴露的最重要方法论问题**：现有测试无法为"MISRA/实时性/安全性"提供证据。
2. **规范符合性未经构建强制**（P0-2、§6.1）——"遵循 C99 / MISRA C:2012"目前是文档声明而非可验证约束；NFR-3 的 Mandatory 目标既未证实也未证伪。
3. **失效安全语义缺失**（P1-3、P1-4、P2-5）——对驱动电磁阀这类负载，"MCU 挂了但负载仍被驱动""状态谎报"是最关键的安全反面模式，而当前生成框架没有统一的故障处置原语。

**建议的推进顺序**：先修 **P0-1**（功能不可用，且暴露测试方法论缺陷）→ 再落 **P0-2 + §6.3**（把规范符合性变成构建门禁）→ 然后 **P1 四项**（失效安全与实时性），最后把 **MISRA 扫描与复杂度度量接入 CI**（即 Phase 6 的实际落地）。其中 P0-1 与 §6.3 的修复必须**同时**补上"目标侧冒烟测试"，否则同类问题会再次被 mock 掩盖。

**关于工具链（v2 补充结论）**：本工程 C 代码由 **arm-none-eabi-gcc 15.2.1** 编译（Cortex-M0+ / ARMv6-M、**soft-float 无 FPU**、newlib-nano），而主机单测/SIL 由 x86 + glibc 编译。二者的 ABI 差异（`char` 符号性、枚举宽度 `-fshort-enums`、`long double`、浮点实现、`%f` 支持）**本身就是病灶 1 的机制根源**：凡属 ABI 敏感的行为，主机测试在原理上就无法提供证据（§11 A.2 已逐项列出）。同时，**"遵循 C99 / MISRA C:2012" 在工具链层面同样没有任何强制**——构建未指定 `-std=`（§6.2）、严格告警集未开启（开启后生成代码立即命中 50 条，§11 A.4）、内建分析器 `-fanalyzer` 可用但未接入。因此建议在上述顺序中并行加入 **§11 A.6 的工具链门禁**：它是把"MISRA/实时性/安全性"从文档声明变为可验证约束的**最低成本抓手**，且不依赖任何新引入的第三方工具。

---

## 11. 附录 A：编译工具链基线（arm-none-eabi-gcc）与 ABI 正确性

> 本附录为 v2 补充。首版报告已在用 arm-none-eabi-gcc 取证，但未把工具链作为**独立基线**固定下来；补充过程中同时发现首版 MISRA 扫描的平台参数与目标 ABI 不一致（§6.1 已更正），故一并以目标编译器补做 ABI 正确的检查。

### A.1 基线事实（逐项实测取证）

| 项目 | 事实 | 取证方式 |
|------|------|----------|
| 编译器 | Arm GNU Toolchain **15.2.Rel1**（Build arm-15.86），`arm-none-eabi-gcc` **15.2.1** 20251203 | `arm-none-eabi-gcc --version` |
| target triple | `arm-none-eabi`（裸机，无 OS） | `-dumpmachine` |
| 内核/指令集 | Cortex-M0+ / **ARMv6-M**，`Tag_CPU_name: "6S-M"`、`Tag_THUMB_ISA_use: Thumb-1` | `-mcpu=cortex-m0plus -mthumb`；`readelf -A` |
| 浮点 ABI | **soft-float，无 FPU**；选中 multilib `thumb/v6-m/nofp` | ELF `Flags: 0x5000200, Version5 EABI, soft-float ABI`；链接日志 `.../lib/thumb/v6-m/nofp/libc_nano.a` |
| C 运行库 | **newlib-nano**：`-specs=nano.specs -specs=nosys.specs` | `toolchain.cmake:24`；`build.ninja` `LINK_FLAGS` |
| 链接配置 | `-lm`；`-Wl,--gc-sections`；`-Wl,-Map=<name>.map`；`-T linker/STM32G0B1RETx_FLASH.ld` | `build.ninja` `LINK_LIBRARIES` / `LINK_FLAGS` |
| **C 标准** | **未指定 `-std=`**，实测默认 **C23**（`__STDC_VERSION__ = 202311L`） | `gcc -dM -E` |
| `char` 符号性 | **unsigned**（`__CHAR_UNSIGNED__` 已定义） | `gcc -dM -E` |
| 枚举宽度 | **默认 `-fshort-enums`**（`Tag_ABI_enum_size: small`）：取最小可容纳类型 | 对照实验：`enum{A=0,B=200}` → 默认 **1 B**；`-fno-short-enums` → **4 B**（`objdump -t`） |
| 基本类型宽度 | `int` / `long` / 指针 均 **4 B**；`float` 4 B；`double` 8 B；**`long double` 8 B（与 `double` 同宽）** | `__SIZEOF_*`；`Tag_ABI_PCS_wchar_t: 4` |
| 对齐要求 | **8-byte**（`Tag_ABI_align_needed: 8-byte`） | `readelf -A` |
| 独占访问 | **ARMv6-M 无 LDREX/STREX** → `__atomic_fetch_add` 编译为 `bl __atomic_fetch_add_4`；且**当前链接配置下该符号未定义（未链接 `-latomic`），链接失败** | 反汇编 + 链接实验 |
| 优化/调试 | `-Og -g3` | `build.ninja` `FLAGS` |
| 产物规模 | `text 78,228 B / data 828 B / bss 17,500 B`（目标 STM32G0B1RE：512 KB Flash / 144 KB RAM） | `arm-none-eabi-size` |

### A.2 与宿主机的 ABI 差异 —— "测试 ≠ 目标"的机制根源

主机侧单测 / SIL 用 `gcc`（x86_64 + glibc）编译，与目标 ABI 在以下各点**不一致**：

| 维度 | 目标（arm-none-eabi / ARM EABI） | 主机（x86_64 + glibc） | 潜在后果 |
|------|----------------------------------|------------------------|----------|
| `char` 符号性 | **unsigned** | **signed** | 主机上不可能复现"`char` 恒 ≥ 0 / `EOF` 判定失效"类缺陷 |
| 枚举宽度 | **1 B**（`-fshort-enums`） | 4 B | 结构体布局、通信帧、参数序列化在两侧不一致 |
| `long double` | 8 B（= `double`） | 16 B | 精度相关测试结论不可移植 |
| `wchar_t` | 4 B 无符号 | 4 B 有符号 | 字符串 / 编码相关逻辑 |
| 浮点实现 | **soft-float 库调用** | 硬件 SSE | 主机上的**时序**测量无意义 |
| C 库 | newlib-nano（`nano.specs`） | glibc | `%f` 支持、`sscanf("%f")` 等能力不同（见 §6.3） |
| 独占 / 原子 | **无 lock-free 原语** | 齐全 | 并发正确性无法在主机上验证 |

> **结论**：在 MISRA / 实时性 / 安全性三类审查中，**ABI 正确性只能由目标编译器本身提供**。cppcheck 无 ARM 平台选项（`--platform=unix32` 是 x86/ILP32），因此以它为主的扫描结果**不能**用于依赖 `char` 符号性、枚举宽度、浮点行为的规则判定；本附录补充的编译器内建检查正是这一缺口的补位。

### A.3 由 ABI 正确分析新增 / 修正的结论

**（1）更正首版一处分析方法缺陷。** §6.1 的 MISRA 扫描使用 `--platform=unix32`（x86 ABI，`char` 有符号、枚举 4 B），与目标 ABI 相反（已在该节加注）。这**不改变**该节最终判定（"无可用分析配置、Mandatory 目标未验证"），但让判定依据更准确。

**（2）坐实 P0-2（未指定 `-std=`）。** 目标编译器默认 C23 已实测；`-std=c99` 下的 3 个 error 亦由该编译器给出。故"项目宣称遵循 C99"在**工具链层面就没有任何强制**。

**（3）放大 §6.2 的 `-Wtype-limits` 发现。** 目标是 ARM EABI，`char` **无符号** —— 凡"对 `char` 做 `< 0` / `>= 0` 判定"或"用 `char` 接收 `EOF`"的写法在目标上恒真 / 恒假，而**主机（`char` 有符号）不会报警**。`param_registry.c:331` 的无符号 `< 0` 恒假判定（首版已列）正属此类，须按目标 ABI 复核。

**（4）枚举宽度差异进入 MISRA 与 SIL 结论。** `pid_stage_t` / `power_mode_t` 等枚举在目标上若取值 ≤255 则占 **1 B**，主机占 4 B。影响：(a) MISRA Rule 10.3 / 10.4 的整型转换分析不可跨主机移植；(b) **SIL / 单测与目标的结构体布局不同**，任何依赖布局的代码（通信帧、参数映射、寄存器镜像）在两侧行为不一致。

**（5）软浮点使浮点运算退化为函数调用（新增时序证据）。** 链接产物确认以下库调用：`__aeabi_fdiv`×4、`__aeabi_fmul`×3、`__aeabi_fadd`×3、`__aeabi_fsub`×3、`__aeabi_f2iz`×3、`__aeabi_f2uiz`×2、`__aeabi_f2d`×3、`__aeabi_fcmplt`×2、`__aeabi_fcmple/fcmpgt/fcmpge/fcmpeq` 各 1。Cortex-M0+ @16 MHz **无 FPU**，一次 `__aeabi_fdiv` 为数百周期级；`printf` 的默认实参提升还会引入 `__aeabi_f2d`（float → double）。这为 **P1-1 / P1-2 的时序预算**补上量化依据：控制回路内的浮点运算**不是"忽略不计"**。建议控制回路改用**定点（Q15 / Q31）**——同时更契合 MISRA Dir 4.6 与无 FPU 目标。

**（6）目标上不存在 lock-free 原子操作（坐实 P2-1）。** ARMv6-M 无 LDREX / STREX，`__atomic_fetch_add` 只能落到库调用；更关键的是**当前链接配置（未链接 `-latomic`）下该符号未定义、链接失败**。结论：所有 ISR / 任务共享状态**只能**靠 `volatile` + PRIMASK 临界区（`taskENTER_CRITICAL()` / `__disable_irq()`）保证。这既坐实 P2-1 的严重性，也解释了 `AGENTS.md` 教训 1（FreeRTOS ARM_CM0 端口毒值卡死 PRIMASK）为何在本平台是**真实且反复出现**的一类问题，以及 `LWRB_DISABLE_ATOMIC` 被定义的原因。

### A.4 目标编译器原生严格告警体检（ABI 正确，新增）

在**真实构建 flags** 上仅追加严格告警开关后执行（命令见 A.5）：

| 告警类别 | 生成代码命中 | 主要位置 | 性质 |
|----------|--------------|----------|------|
| `-Wmissing-prototypes` | **34** | 全部组件生命周期函数（`btn` / `led` / `shell` / `pid_ctrl` 的 `_init` / `_step` / `_terminate`）、全部 ISR 处理函数、`MX_GPIO_Init`、`SystemClock_Config` | **真问题**：组件注册表经**函数指针**调用这些函数，**签名不一致不会有任何编译期检查**；同时对应 MISRA Rule 8.4（定义须有可见声明） |
| `-Wconversion` | **9** | `i2c_api.c:143,164,182,199`（`uint32_t len` → HAL `uint16_t Size`，**见 P2-7**）；`telemetry.c:141`（`unsigned long → uint16_t`，与 **P0-3 同一函数**）；`drv_usart2.c:32`；`drv_rtc.c:831-833`（`unsigned long → uint8_t`，取值已掩码，**良性但缺显式转换**） | P2-7 为真缺陷；其余为 MISRA Rule 10.3 显式转换缺失 |
| `-Wshadow` | **4** | `drv_rtc.c:269,442,454,879` —— 局部 `hrtc` 遮蔽同名全局 | MISRA Rule 5.3 类 |
| `-Wsign-conversion` | **3** | `drv_log.c:198`、`drv_rtc.c:204,725` | MISRA Rule 10.3 |
| `-Wcast-align` / `-Wdouble-promotion` / `-Wfloat-equal` / `-Wstrict-prototypes` / `-Wpointer-arith` | **0** | — | 编译器自身不会生成非对齐访问；但 `param_registry.c:169-220` 的 `*(uint32_t *)p->value_ptr` 等强转仍**需人工确认对齐来源** |

生成代码合计 **50 条**；另有 64 条来自 CMSIS / lwrb 等 **vendor 头文件**（`-Wsign-conversion` 54、`-Wundef` 10），属 vendor 责任范围。

> 对比：首版 §6.2 只统计到 **1 条警告**，原因是未开启上述开关。把这组开关纳入 CI 是**零成本**的（无需新工具），却能把生成代码的转换/符号/声明问题直接变成可见数字。

**内建静态分析器 `-fanalyzer`**：已实测在本工具链上**可用** —— 对故意构造的样例可正确报出 `-Wanalyzer-null-dereference`（`[CWE-476]`）与 `-Wanalyzer-double-free`（`[CWE-415]`）；对生成代码 7 个关键文件（`pid_math.c`、`pid_ctrl_component.c`、`event_mgr.c`、`param_registry.c`、`telemetry.c`、`power_mgr.c`、`drv_pressure.c`）分析结果为 **0 条**。
**这不构成"代码无缺陷"的结论**：该分析器**不建模 ISR / RTOS 并发语义**，因此结构上无法发现 P0-1（总线名不匹配）、P2-1（`volatile` 缺失）一类问题。但作为确定性缺陷（空指针、越界、泄漏、UB）的**免费增量门禁**值得接入。
（注意：`-fsyntax-only` 会使 `-fanalyzer` 失效而不报错 —— 必须实际生成目标文件。）

### A.5 复现命令

```bash
# 1) 工具链事实
arm-none-eabi-gcc --version && arm-none-eabi-gcc -dumpmachine
echo "" | arm-none-eabi-gcc -mcpu=cortex-m0plus -mthumb -x c -dM -E - \
  | grep -E "STDC_VERSION|CHAR_UNSIGNED|SIZEOF_"
arm-none-eabi-readelf -A build/*.elf      # 浮点 ABI / 枚举宽度 / 对齐 / wchar_t
arm-none-eabi-size build/*.elf

# 2) ABI 正确的严格告警体检（在 §2 的真实 -D/-I 基础上追加开关）
arm-none-eabi-gcc -mcpu=cortex-m0plus -mthumb -Og -std=c99 -fsyntax-only \
  -Wcast-align -Wconversion -Wsign-conversion -Wdouble-promotion -Wfloat-equal \
  -Wshadow -Wundef -Wstrict-prototypes -Wmissing-prototypes -Wpointer-arith \
  <同上 -D/-I> src/*.c src/drivers/*.c
# → 生成代码 50 条告警（首版只统计 1 条，因未开启上述开关）

# 3) 内建静态分析器（必须实际编译；-fsyntax-only 会使其静默失效）
cd <tmpdir> && arm-none-eabi-gcc -mcpu=cortex-m0plus -mthumb -Og -std=c99 \
  -fanalyzer <同上 -D/-I> -c <src>/*.c

# 4) 软浮点证据（每个浮点运算 = 一次库调用）
grep -o "__aeabi_[a-z0-9]*" build/*.map | sort | uniq -c

# 5) 枚举宽度 ABI 对照实验
printf 'enum E { A=0, B=200 };\nenum E e;\n' > e.c
arm-none-eabi-gcc -mcpu=cortex-m0plus -mthumb -c e.c -o e1.o
arm-none-eabi-objdump -t e1.o | grep " e$"                      # → 1 B（-fshort-enums 默认）
arm-none-eabi-gcc -mcpu=cortex-m0plus -mthumb -fno-short-enums -c e.c -o e2.o
arm-none-eabi-objdump -t e2.o | grep " e$"                      # → 4 B
```

### A.6 建议纳入 CI 的工具链门禁

- [ ] 构建**显式固定** `-std=c99`（或 `gnu99`），并开启 `-Werror`；
- [ ] 目标编译器严格告警集（A.5-2）作为门禁，**基线 50 条只降不升**；
- [ ] `-fanalyzer` 全量接入（增量门禁）；
- [ ] 归档 `readelf -A` 输出，使浮点 ABI / 枚举宽度 / 对齐**可审计**；
- [ ] **ABI 相关开关（`-mfloat-abi`、`-fshort-enums` 等）一旦显式化，主机构建（单测 / SIL）须同步** —— 否则"测试 ≠ 目标"的缺口会进一步扩大（§10 病灶 1）。

---

## 12. 修复记录（2026-09-15，同日落地）

> 本节记录第 3–5 章 14 项缺陷的落地情况与可复现证据。
> 所有改动**均未 push**（遵循工程约定：先本地 commit）。
> 修复原则补充了一条**强制约束**（见 12.4）：**ST 官方 HAL、CMSIS 与 FreeRTOS
> 内核的系统代码一律不得修改**，只允许改配置文件与中断向量/回调文件。

### 12.1 逐项结论

| 编号 | 结论 | 关键改动（模板/模块） |
|------|------|----------------------|
| **P0-1** | ✅ 已修 | 总线句柄统一小写（`drv_cli`、`pid_ctrl_component`、`mpu6050_component`、`modbus_component`、`drv_foc_motor`、`drv_ntc`）；`posix/{i2c,spi,uart,adc,gpio}_api` 改为**大小写不敏感**的注册表查找；**`posix_mock.c` 重写为复刻生产语义**（固定注册表，不再按名动态建总线）——这是 P0-1 曾被掩盖的根因 |
| **P0-2** | ✅ 已修 | `project/toolchain.cmake` 加入 `-std=c99`；`drv_rtc.h` 补 `<stdbool.h>`；`param_registry.c` 修掉无符号 `< 0` 恒假的下界钳位（原为死代码） |
| **P0-3** | ✅ 已修 | `telemetry.c` 引入真实 `g_task_stack_table[]`（按 `app_tasks` / `cli_stack_size` 生成）与 `task_stack_alloc_words()`，`peak_usage = alloc - remaining` |
| **P1-1** | ✅ 已修 | `pid_ctrl_component.c` 用 `HAL_GetTick()` 增量重算实际 `dt`（`pid_measured_dt()`，限幅 `[1, period×10] ms`，异常回落名义值） |
| **P1-2** | ✅ 已修 | `i2c_bus_ensure_ready()` 改**毫秒上界**等待（5 ms，另有自旋上限兜底）；`drv_iwdg` 刷新值由唯一来源 `IWDG_RELOAD_VALUE` 生成；`has_iwdg` 贯通 `peripheral_context`/`hal_context`/`builder`，且**只有存在刷新责任方**（`component_step_task` 或 `rtc_demo_task`）才武装看门狗 |
| **P1-3** | ✅ 已修 | 新增统一失效安全模块 `src/hw2c_fault.{h,c}`：`hw2c_fault_trap()` 关中断 → 强制安全态（PWM 全 0% 并停）→ 记录原因到 `.noinit`（带 magic，可跨复位）→ `NVIC_SystemReset()`。`main.c` / `stm32g0xx_it.c` / `drv_rtc.c` 的 6 处 `while(1)` 全部替换；4 个链接脚本新增 `.noinit (NOLOAD)` |
| **P1-4** | ✅ 已修 | `power_sleep_now()` 未实现时返回 `POWER_SLEEP_SKIPPED`；`drv_cli` 的 `power sleep` 先查模式是否实现、是否被 `power_mgr_is_mode_allowed()` 允许，再按真实返回值如实报告，不再谎报"已进入" |
| **P2-1** | ✅ 已修 | `rtc_uptime_sec` 补 `volatile`（ISR 写、任务读） |
| **P2-2** | ✅ 已修 | 删除 `stm32g0xx_it.c` 中与 LED 组件争用同一引脚的调试 `HAL_GPIO_TogglePin` |
| **P2-3** | ✅ 已修 | `event_mgr.c` 检查 `xQueueCreate()` 返回 NULL → `hw2c_fault_trap(HW2C_FAULT_RTOS_OBJECT)`（否则 ISR 会解引用空队列）；`main.c` 新增 `create_task_checked()` 包装全部 5 处 `xTaskCreate()` |
| **P2-4** | ✅ 已修 | `pid_math.c` 的 `pid_clampf()` 对 `isnan` 返回下界；`pid_update()` 遇 NaN 误差时复位状态并返回 0（原先 NaN 逃过所有比较 → 污染积分项） |
| **P2-5** | ✅ 已修 | `pid_trip_fault()` / `stop()` / `vent()` 立即调用 `pid_apply_output(0.0f)`，不再等到下一个控制周期（原先执行器多通电 ~0.56 s） |
| **P2-6** | ✅ 已修 | 新增 `{{comp}}_manual_output_allowed()`（仅 STANDBY 放行）与 `{{comp}}_owned_channels()`；`drv_cli` 增加 `pwm_channel_owned_by_pid()`，`pwm set` 对被 PID 占用的通道直接拒绝并提示先 `stop` |
| **P2-7** | ✅ 已修 | POSIX 总线 API 增加 `*_API_MAX_XFER_BYTES`（0xFFFF），超限直接返回错误码，消除 HAL `Size`（`uint16_t`）静默截断 |

### 12.2 新增 / 强化的模块

- **`src/hw2c_fault.{h,c}`（新）**：统一故障处置原语。原因码 `HW2C_FAULT_NONE..
  UNEXPECTED`；`hw2c_safe_state()` 遍历所有 PWM 通道置 0% 并停止；记录写入
  `.noinit`（NOLOAD，复位不清零）；`hw2c_fault_last()` / `hw2c_fault_reset_count()`
  供启动日志上报上次故障；**且可在驱动初始化之前安全调用**（内部有 `pwm_ready()`
  守卫，见 12.3 的"自伤修复"）。
- **`test/hw2c_fault.{c}`（新）**：6 项用例覆盖 trap→复位、原因记录、次数累加、
  clear 保留计数、安全态清零并停 PWM。
- **`test/fault_trap_stub.c.j2`（新）**：为主机侧单测提供 `hw2c_fault_trap` 替身，
  避免把整条失效安全链路拖进每个 UT。
- **`test/posix_mock.c.j2`（重写）**：I2C/SPI mock 的行为改为**与生产实现一致**
  （固定注册表 + 大小写不敏感），使"mock 掩盖目标故障"这一类问题不再复现。

### 12.3 过程中发现并修复的**自身缺陷**（值得记录）

1. **`hw2c_safe_state()` 在驱动未初始化时解引用未初始化句柄** → 主机侧段错误；
   在目标上会是"故障处理里再触发 HardFault"。修法：`drv_pwm` 增加 `pwm_ready()`
   守卫，`set_duty`/`stop` 在未初始化时安全返回；`drv_pwm.h` 同时补充了
   "可在 init 前调用"的契约说明。**这正是 `hw2c_fault.h` 当初声称的性质，
   此前只是声明、不是事实。**
2. **`create_task_checked()` 形参个数与 `TaskFunction_t` 不匹配**（自引入，
   导致 7 个示例编译失败）。根因是 `--force` 会跳过生成器自带的编译自检，
   所以模板错误没有被生成阶段拦住 —— 改为全量显式编译验证。
3. **`test_hw2c_fault` 首次运行段错误**：定位到 (1)，而非测试本身问题。

### 12.4 供应商源码只读约束（本次新增的强制规则）

用户明确要求：**STM32G0 官方 HAL、CMSIS 与 FreeRTOS（除配置文件外）的系统代码
不得修改**。此前的做法违反了该约束：

- ❌ **旧做法**：`generate.py::_apply_vendored_patches()` 每次生成时幂等改写
  `static/stm32g0/FreeRTOS-Kernel/portable/GCC/ARM_CM0/port.c`
  （`ulCriticalNesting` 毒值 `0xaaaaaaaaUL` → `0UL`）。
- ✅ **新做法（已落地）**：**删除该补丁**，port.c 还原为上游原样，
  改由 `templates/src/main.c.j2` 的**排序**承担等价保证。

**机制**：FreeRTOS ARMv6-M 端口把 `ulCriticalNesting` 初始化为毒值
`0xAAAAAAAA`（上游 `345a86d49`（2024-03-26）引入，至 `78069a79e`（2026-07-16）
**仍未修复**，故"升级 submodule"不是出路）。因此 `xPortStartScheduler()` 之前
**第一次** `taskEXIT_CRITICAL()` 递减后仍非 0，永远走不到恢复 PRIMASK 的分支 →
中断在调度器启动前持续屏蔽 → `HAL_GetTick()`（TIM14 中断驱动）冻结 →
LSE/I2C/Flash/IWDG 等一切按 tick 计时的等待死循环。

**排序约束**（现已成为硬性不变量）：

```
EventMgr_Init()            ← 唯一提前的 RTOS 对象（须先于 RTC_Start，RTC ISR 投递其队列）
__enable_irq()             ← 中断状态修复（紧接其后）
… 外设初始化 / component_init_all() / IWDG_Init() …   ← 依赖 tick，必须在修复之后
… 日志 + log_flush()（轮询排空，位置无关）…
─── 以下块禁止任何依赖 tick / 依赖中断的调用 ───
cli_init() / telemetry_init() / 所有 xTaskCreate()
vTaskStartScheduler()      ← 自身把 ulCriticalNesting 归零并开中断
```

**回归护栏**：`generator/tests/test_template_render.py::
test_main_c_keeps_rtos_object_creation_after_tick_dependent_init`
（断言"修复点在唯一提前的 RTOS 对象之后、所有 tick 依赖代码之前，
且 `repair..cli_init` 区间内不得出现任何 RTOS 对象创建"）。

**机械校验**：另有脚本对 8 个示例的生成 `main.c` 逐一解析，
确认**被屏蔽区间内 tick 依赖调用数 = 0**，且该区间内 RTOS 对象创建者
只有 `cli_init` / `create_task_checked`（即预期的尾部块）。

### 12.5 验证证据（全部本机实测）

| 验证项 | 命令/方法 | 结果 |
|--------|-----------|------|
| 全量生成 | 8 个示例 × 六层 YAML，`--force` | **8/8 成功** |
| 目标交叉编译 | `arm-none-eabi-gcc 15.2.1` + Ninja | **8/8 成功，0 error**（每例 src 侧 9–16 条告警） |
| 主机单元测试 | `output/<demo>/test/run_tests.py` | **8/8 全绿**（含新增 `test_hw2c_fault` 6 项、`test_iwdg` 2 项、`test_event_mgr` 队列失败注入） |
| SIL 组件测试 | `output/<demo>/test/sil` | **8/8 全绿** |
| Python 测试 | `pytest generator/tests tests` | **306 passed**（§13 追加 RTC 标志护栏后） |
| 顺序不变量 | 生成代码静态解析 | **PASS**：屏蔽区间内 tick 依赖调用 0 处 |
| C99 强制 | `grep -o '\-std=[a-z0-9]*' build.ninja` | `-std=c99`（已生效） |
| 浮点格式化 | `map` 中 `_printf_float` / `_scanf_float` | 各 4 处（已链接） |
| `.noinit` 段 | `readelf -S base.elf` | `.noinit` 位于 `0x20000438`，`NOBITS`（启动不清零） |
| vendor 洁净 | `git status`（父仓 + 子模块） | port.c **已还原上游原样**；子模块无本地修改 |
| **上板启动** | **DAPLink + pyOCD，见 §13** | ✅ **真机启动成功至 `System ready` + 调度器运行** |

### 12.6 未完成 / 需上板确认

> **⚠️ 本节已被 §13 取代（2026-09-15 当日完成上板验证）。**
> 当时判断"未连接 DAP-Link"是因为**沙箱内 USB 枚举返回 0 个设备**；
> 实际调试器一直在位，且本机 OpenOCD 本来也驱动不了它（CMSIS-DAP v2/WinUSB，
> 该构建无 libusb 后端）。正确通路是 pyOCD，详见 §13.1。
> 结论：**上板启动已验证通过**，并在过程中发现并修复了 §13.3 的 P0-4。

- ✅ **上板启动验证**：已完成，见 §13.2 / §13.4。
- ✅ **§6.3 的 `%.1f` 实机打印**：已完成（`sysinfo` 打印 `28.0 C`），见 §13.2。
- ⏳ **§9 中仍需硬件配合的条目**：P1-1 的示波器验证、P1-2 的拔 I2C 从机测试、
  P1-3 的人为 HardFault 注入、P2-6（需烧录 solenoid 示例，**会上电驱动执行器**）。
- ⏳ **严格告警基线**：修复后 8 个示例合计 **538 条**（单例 57–73），
  其中 `-Wsign-conversion` 占大头。该数字与此前的"单例 50 条"**不可直接比较**
  （文件集与 `-std=c99` 均不同），需重新建立基线后再纳入 CI 门禁。
- ⏳ **CI 门禁**（§11 A.6）尚未接入。

---

## 13. 上板验证（2026-09-15，DAPLink 实测）

> §12.6 中"缺目标侧实证"的缺口已补齐。本节同时记录**上板后新发现的 1 个 P0 缺陷**
> （P0-4），它只在真机上暴露，主机侧单测与 SIL **都无法覆盖**——因为 `drv_rtc.c` 的
> ISR 在 `TEST` 构建下是空桩。
>
> 原始记录（烧录日志、启动日志原文、遥测快照、CLI 逐条回显、P0-4 故障现场寄存器 dump）：
> [`onboard-capture-2026-09-15.txt`](./onboard-capture-2026-09-15.txt)。

### 13.1 烧录通路（工具链现实，重要）

本次上板暴露了一个**此前未被记录的环境限制**：

| 事实 | 说明 |
|------|------|
| 该 DAPLink 是 **CMSIS-DAP v2** | `USB\VID_0D28&PID_0204&MI_00` = `[001] CMSIS-DAP`，驱动 `winusb.inf`；`MI_01` = CDC(COM3)；`MI_03` = HID 兼容接口 |
| 固件**没有 MSC 接口** | 无 USB 大容量存储设备 → **拖拽烧录不可用** |
| 本机 OpenOCD **无法使用它** | 该构建只编入 `hid` 后端：`cmsis-dap backend usb` 被判为非法参数；`auto` 落到 TCP；`hid` 能找到设备但 `WriteFile 0x00000057 (ERROR_INVALID_PARAMETER)` |
| ✅ **解法：pyOCD** | 隔离 venv `C:/Users/pc/.workbuddy/binaries/python/envs/default` + `pyocd 0.45.1`（含 `libusb-package`），走 WinUSB 批量接口 |
| 额外需要器件包 | pyOCD 内置无 STM32G0，需 `pyocd pack install STM32G0B1RETx`（下载 `Keil.STM32G0xx_DFP` 2.1.0），目标名 **`stm32g0b1retx`** |

实测：`pyocd list` → `USB [001] CMSIS-DAP  4485EDE7`；`DAP IDCODE = 0x0bc11477`（Cortex-M0+）。

> ⚠️ **所有 pyOCD / 串口操作必须在沙箱之外执行**。沙箱内 USB/HID 枚举返回 0 个设备
> （连已存在的 COM4 都看不到），会被误判为"没插调试器"。

### 13.2 结论：删除 vendor 补丁后**启动成功**

`output/base` 重新生成、编译（0 error）后烧录（擦除 86016 B / 42 扇区，写入 86016 B），
完整启动日志：

```
H   H  W   W  2222  CCCC
====================================================

[2000-00-00 00:00:00.999] [INF] base v1.0 — Hardware2Code | STM32G0B1RET6 @ 16 MHz
[2000-00-00 00:00:00.999] [INF] MX_GPIO_Init() OK
[2000-00-00 00:00:00.999] [INF] EventMgr_Init() OK
[2000-00-00 00:00:00.999] [INF] statemachine_init() OK
[2000-00-00 00:00:00.999] [INF] RTC clock source: LSE (32.768kHz)
[2026-07-29 22:07:00.002] [INF] temp_sensor_init() OK
[2026-07-29 22:07:00.004] [INF] RTC_Init() OK
[2026-07-29 22:07:00.006] [INF] component_init_all: 3/3 OK
[2026-07-29 22:07:00.008] [INF] component_bus initialized (4 topics)
[2026-07-29 22:07:00.011] [INF] param_registry initialized (7 params)
[2026-07-29 22:07:00.013] [INF] PowerMgr: init, default=STOP1
[2026-07-29 22:07:00.015] [INF] power_mgr initialized (default: STOP1)
[2026-07-29 22:07:00.017] [INF] System ready — 3 components, 9288 B heap free, starting scheduler
```

**§12.4 的排序不变量在真机上成立**：LSE 起振、RTC 初始化、组件探测（含 tick 超时）
全部通过，未出现"删补丁即卡死"。调度器确实在运行——30 秒后的遥测快照：

```
=== Telemetry Snapshot ===
  Uptime: 30 s   Heartbeat: 1
  Heap:   872 / 11264 bytes (min free 872)
  Tasks(5):
    event_mgr        stack 512 used=468 free=44  run=5
    IDLE             stack 0   used=0   free=85  run=29964
    comp_step        stack 512 used=59  free=453 run=0
    cli              stack 512 used=40  free=472 run=0
    Tmr Svc          stack 0   used=0   free=211 run=0
  Min stack free: 44 words
  Components: 3 total, 3 running, 0 error, 0 total-errors
```

> 这段快照**顺带在真机确认了 P0-3 的修复**：栈水位是真实值（`event_mgr` 512 字里用掉
> 468、只剩 44 字），此前该字段是常量 0。

CLI 实测（COM4, 115200）：

| 命令 | 实测输出 | 验证点 |
|------|----------|--------|
| `help` | 13 条命令 + `>` 提示符 | 交互正常 |
| `uptime` | `0:01:03` | RTC 1 s 心跳真实累加 |
| `tasks` | 5 个任务的真实栈高水位 | P0-3 |
| `sysinfo` | `Temperature : 28.0 C` / `Heap 10392/11264` / `Flash 82228/524288` | P0-2（`%.1f` 在目标可打印） |
| `rtc time` | `22:08:07  Date: 29/07/2026` | RTC 日历走动 |
| `led on` / `led off` | `LED ON` / `LED OFF` | GPIO 组件 |
| `power sleep` | `Entering STOP1 … Woke up after 276 ms, RTC keeps time, RAM intact.` | 真实 STOP1（`HAL_PWR_EnterSTOPMode(…, PWR_STOPENTRY_WFI)` + RTC 时间补偿），非谎报 |

> 关于 `power sleep`：本次确认 `templates/` 的 `sleep.c` **确实有真实 STOP 实现**
> （休眠期用 `rtc_get_time_ms()` 计量并回补 `uwTick`），因此 P1-4 的
> `POWER_SLEEP_SKIPPED` 分支只在"未生成 tickless"的构建里触发。
> 此前记忆里"全仓无 `__WFI`、STOP 未实现"的说法**是错的**（实现走
> `HAL_PWR_EnterSTOPMode`，不经字面 `__WFI`）。

### 13.3 新发现 **P0-4（阻塞级）**：RTC ISR 的清标志不完整 → 中断风暴 → 启动死锁

**现象**：首次上板，串口停在 `statemachine_init() OK` 后完全静默（25 s 无输出），
不断言、不复位（`.noinit` 故障记录全 0 → **不是** HardFault / 复位循环）。
日志窗口内 5 次采样 PC，**IPSR 全部 = 18**（= IRQ 2 `RTC_TAMP`），PC 在
`RTC_TAMP_IRQHandler` 与 `xTaskGetSchedulerState` 之间跳动 → CPU 100% 时间在该 ISR 内，
`main()` 永远走不到 `vTaskStartScheduler()`。

**硬件证据**：

| 寄存器 | 值 | 解读 |
|--------|-----|------|
| `RTC_SR` | `0x00000002` | **只有 bit1 = `RTC_SR_ALRBF`（Alarm B 标志）置位** |
| `RTC_MISR` | `0x00000002` | 该源的中断确实使能（未被屏蔽） |
| `RTC_CR` | `0x40007704` | bit8/9/10 = `ALRAIE`/`ALRBIE`/`WUTIE` = 111 |
| `RTC_SCR` | `0x00000000` | 清除寄存器**从未被写过** |
| `NVIC IABR` | `0` / `ISPR = 0x10080000` | TIM14 与 USART2 一直 pending 却得不到服务（被 RTC 抢占） |
| `.noinit`(0x20000438) | 全 0 | 未发生失效安全捕获 → 确认是纯中断风暴 |

**机制**（`templates/drivers/drv_rtc.c.j2`）：

1. `RTC_Init()` 里创建毫秒级单次定时器 `RTC_TimerCreateMs(500, …)` → `rtc_arm_alarm_b()`
   用 **Alarm B** 武装 500 ms 期限；
2. `RTC_Init()` 返回后，`main()` 还有组件/总线/参数/电源/CLI/遥测初始化 + 全部
   `xTaskCreate()`，**远超 500 ms**；
3. 于是 Alarm B 在**调度器启动之前**到期 → 进 ISR → 命中
   `xTaskGetSchedulerState() == taskSCHEDULER_NOT_STARTED` 的早退分支；
4. 该分支只清 `WUTF`(bit2) 与 `ALRAF`(bit0)，**`ALRBF`(bit1) 从未被清**；
5. 该 part 的标志寄存器是只读的，标志要靠写 `SCR` 对应位清除 → `ALRBF` 一直为 1 →
   **RTC 中断线持续拉高** → ISR 退出即刻重入 → 死锁。

**修法**（本次落地）：

- 新增统一原语 `rtc_clear_all_flags()`，掩码覆盖**全部** 6 个清除位
  （`CALRAF|CALRBF|CWUTF|CTSF|CTSOVF|CITSF`），经 `__HAL_RTC_CLEAR_FLAG()` 一次性写 `SCR`；
- 三处调用点全部改走该原语：① `RTC_Init()` 的陈旧标志清理；② 中断使能前的
  "解除残留报警/唤醒状态"；③ **ISR 的 pre-scheduler 早退分支**；
- 第 ② 处同时补上 `HAL_RTC_DeactivateAlarm(RTC_ALARM_A/B)` 与
  `__HAL_RTC_ALARM_DISABLE_IT(ALRA|ALRB)`、`__HAL_RTC_WAKEUPTIMER_DISABLE_IT(WUT)`，
  使备份域残留的"使能位"也不会在启动期发作（各通道随后按需重新武装：
  `HAL_RTCEx_SetWakeUpTimer_IT` / `HAL_RTC_SetAlarm_IT`）。
- 修完后再烧录：**完整启动成功**，见 13.2。

**回归护栏**：`generator/tests/test_template_render.py::`
`test_rtc_isr_acknowledges_every_flag_before_scheduler`
（断言掩码含 `RTC_SCR_CALRBF` 等 6 位，且 ISR 早退分支内不得回落到 subset clear）。

> **教训**：`TEST` 构建把 `RTC_TAMP_IRQHandler` 编译成空桩，所以这类"中断未清标志
> 导致重入"的缺陷**主机单测与 SIL 结构上不可能发现**。凡改动 ISR、中断使能、
> 备份域状态或标志清除逻辑，都必须有目标侧验证。

### 13.4 §9 验收清单的更新

| 原验收项 | 状态 |
|---|---|
| P0-3 栈水位为真实值 | ✅ 真机确认（`tasks` 与遥测快照给出逐任务 used/free） |
| P0-2 `%.1f` 在目标可打印 | ✅ 真机确认（`sysinfo` 打印 `28.0 C`） |
| P1-2 看门狗有刷新责任方 | ✅ 间接确认（本构建武装 IWDG 且 30 s 未复位） |
| P1-4 `power sleep` 不谎报 | ✅ 真机确认（真实 STOP1，276 ms 唤醒，RTC 时间连续） |
| §12.4 排序不变量（删补丁后能否启动） | ✅ **真机确认通过** |
| **P0-4（新）** | ✅ 已修并真机确认 |
| P1-1 dt 示波器验证 | ⏳ 仍待硬件（需示波器） |
| P1-2 拔 I2C 从机的超时行为 | ⏳ 仍待硬件（需压力传感器接线） |
| P1-3 人为 HardFault 注入 | ⏳ 仍待硬件（`base` 无 PWM/负载，可在 solenoid 示例上做） |
| P2-6 `pwm set` 对 PID 占用通道的拒绝 | ⏳ 仍待硬件（`base` 无 PWM，需烧录 solenoid 示例——**会上电驱动执行器**） |
| 严格告警基线 / CI 门禁 | ⏳ 未做 |

### 13.5 修复后稳态复验（P0-4 闭环确认）

烧录后让固件连续运行约 4 分钟，再次在线 halt 采样，确认 P0-4 的根因**在稳态下也不复现**：

| 观测量 | 修复前 | 修复后（稳态） | 判定 |
|---|---|---|---|
| `PC` | `RTC_TAMP_IRQHandler` 内反复 | `0x0800400a` = **`prvIdleTask`**（`tasks.c`） | 调度器在跑，CPU 空闲 |
| `xpsr` 低位 IPSR | 恒 **18**（IRQ 2） | **0** | 不在任何异常上下文 |
| `NVIC ISPR` | `0x10080000`（TIM14/USART2 被饿死） | `0x00000000` | 无 pending 积压 |
| `NVIC IABR` | 0 | 0 | 无 active |
| `NVIC ISER` | `0x10080084` | `0x10080084` | 使能集合不变（非靠关中断"修好"） |
| `RTC_SR` | `0x02`（`ALRBF` 置位） | **`0x00`** | 根因标志已清 |
| `RTC_MISR` | `0x02` | `0x00` | 无"使能且挂起"源 |
| `RTC_CR` | `0x40007704`（ALRAIE/ALRBIE/WUTIE 全开） | `0x00` | 500 ms 单次已消费、按需重臂 |
| `.noinit` | 全 0 | 全 0 | 4 分钟无失效安全捕获、非复位循环 |

`NVIC ISER` 与修复前完全相同，说明修复不是靠屏蔽 RTC 中断（那会连带废掉
`rtc_timer_post_event`、唤醒定时器与 STOP 唤醒），而是**真正把标志清干净**。
