# solenoid_valve_pid_ctrl_demo

燃气电磁阀加压控制示例，用于验证 hw2c 的 **PID 控制中间件**
（`pid_math` 纯算法 + `pid_ctrl` 可选装组件）。控制策略按行业公共工程
实践从零实现（非任何第三方固件代码）。

## 硬件

| 部件 | 接口 | 说明 |
|------|------|------|
| 电磁阀（开关阀） | TIM2 CH1 (PA0) PWM @ 16 Hz | 开/关 <5 ms，PWM 占空比 = 平均开度 |
| 压力传感器 | I2C1 (PB6/PB7) @ 0x28 | 通用 24-bit 模型，raw×scale+offset → kPa |
| 调试串口 | USART2 (PA2/PA3) @ 115200 | CLI + 日志 |
| LED / 按键 | PC0 / PC13 | 状态指示 / 手势 |
| MCU | STM32G0B1VET6 | 512 KB Flash / 144 KB RAM |

> 压力传感器型号未指定：`hardware.yaml` 中 `pressure` 外设的
> `reg_start / scale_kpa_per_lsb / offset_kpa / byte_order` 按实际器件调整。

## 控制策略（原创实现）

开关阀无法连续调节开度，因此用 16 Hz PWM 占空比调制"平均开度"：

- **STANDBY**：阀门关闭，等待指令
- **RAMP_UP**：PID 输出 0..100% 占空比升压（setpoint 速率限制防冲击）
- **HOLD**：小占空比补偿泄漏，维持目标压力
- **VENT**：阀门关闭泄压（由下游排放）
- **FAULT**：联锁，阀门强制全关

联锁：超压（> `pressure_max_kpa`）、传感器失效（连续读失败）、升压超时。
PID 具备抗积分饱和（条件积分 + 积分钳位）与微分低通滤波。

## CLI

```
solenoid start [kpa]   启动加压（可选指定目标）
solenoid stop          停止（进入 STANDBY）
solenoid vent          泄压
solenoid set <kpa>     在线修改目标
solenoid step_test <duty> <stop_kpa>   开环阶跃辨识整定（推荐，单阀适用）
solenoid status        显示 stage/pressure/setpoint/duty/fault
solenoid reset         清除故障
```

## 参数（params.yaml，运行时经 `param set`）

`pid_kp / pid_ki / pid_kd`、`pid_target`、`pid_max_value`（超压联锁）、
`pid_duty_min_pct`（低于此占空比阀门保持关闭）、`pid_duty_max_pct`、
`pid_ramp_timeout_ms`。

## 开环阶跃辨识整定（`solenoid step_test <duty> <stop_kpa>`）

燃气管道加压的**安全整定方式**：不做继电器振荡（压力不自然下降），
而是开环固定占空比加压、记录压力上升曲线，拟合一阶惯性+纯滞后模型：

```
P(t) = P0 + K·u·(1 - exp(-(t-L)/τ))
```

辨识步骤：

1. 以指定 `duty`（建议 20~40%）开阀加压，压力单调上升，到达
   `stop_kpa`（建议取目标压力的 70~90%，确保低于超压联锁）时停止
2. 记录压力跨过终值 28.3% 与 63.2% 的时刻 t1/t2：
   `τ = 1.5·(t2-t1)`，`L = t2-τ`，`K = ΔP / (duty/100)`
3. 按 Ziegler-Nichols 开环阶跃公式计算并自动写入参数：

```
Kp = 1.2·τ/(K·L)
Ki = Kp/(2·L)
Kd = Kp·(0.5·L)
```

安全要点：整定全程压力单调上升、无振荡、无超压风险；`stop_kpa` 必须
低于 `pressure_max_kpa` 联锁值；完成后 `solenoid start <目标>` 即可用
整定参数闭环。管道加压近似积分过程，一阶拟合给出保守参数，台架上按
需微调 Kp/Ki/Kd。

## 验证

- 主机单测：`test_pid_math`（阶跃收敛、抗饱和、微分滤波、setpoint ramp）
- SIL 闭环：压力罐模型（开阀升压 ~1 kPa/s·%duty，关阀保压）验证升压
  到达目标进入 HOLD、超压触发 FAULT 且阀门全关
- 整定：`test_pid_steptune` 用一阶+滞后模型验证辨识 K/τ/L 精度与
  ZN 参数；SIL 组件级 `step_test` 全流程通过并应用增益
- 台架联调：`solenoid start 100` 观察压力逼近目标后进入 HOLD
