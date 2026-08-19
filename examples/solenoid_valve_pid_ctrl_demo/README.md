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
solenoid status        显示 stage/pressure/setpoint/duty/fault
solenoid reset         清除故障
```

## 参数（params.yaml，运行时经 `param set`）

`pid_kp / pid_ki / pid_kd`、`target_pressure_kpa`、`pressure_max_kpa`、
`duty_min_pct`（低于此占空比阀门保持关闭）、`duty_max_pct`、
`ramp_timeout_ms`。

## 验证

- 主机单测：`test_pid_math`（阶跃收敛、抗饱和、微分滤波、setpoint ramp）
- SIL 闭环：压力罐模型（开阀升压 ~1 kPa/s·%duty，关阀保压）验证升压
  到达目标进入 HOLD、超压触发 FAULT 且阀门全关
- 台架联调：`solenoid start 100` 观察压力逼近目标后进入 HOLD
