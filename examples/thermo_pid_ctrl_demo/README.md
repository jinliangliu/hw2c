# thermo_pid_ctrl_demo

NTC 温度控制示例，验证泛化后的 **过程变量 PID 中间件**（`pid_ctrl`
组件）在温控领域的复用。与 `solenoid_valve_pid_ctrl_demo` 共用同一组件，
仅通过 YAML 声明反馈源与执行器。

## 硬件

| 部件 | 接口 | 说明 |
|------|------|------|
| NTC 热敏电阻 | ADC1 IN1 (PA1) | 1% 100K / B=3950，分压电路 |
| 加热器 | TIM2 CH1 (PA0) PWM @ 10 Hz | 占空比 = 加热功率 |
| 调试串口 | USART2 (PA2/PA3) @ 115200 | CLI + 日志 |
| LED / 按键 | PC0 / PC13 | 状态指示 / 手势 |
| MCU | STM32G0B1VET6 | 512 KB Flash / 144 KB RAM |

分压拓扑（默认）：`VCC(3.3V) - NTC(100K@25°C) - R_fixed(100K) - GND`，
ADC 采分压中点。若你的板子拓扑不同，改 `hardware.yaml` 中 `ntc_temp`
外设的 `ntc_high`、`r_fixed_ohm`、`r0_ohm`、`b_value`、`vref_mv`。

## 温度换算（B 参数方程）

```
R_ntc = R_fixed * (VCC/V_adc - 1)        (ntc_high=true)
1/T = 1/T0 + ln(R_ntc/R0)/B               T、T0 为开尔文温度
```

## CLI

```
solenoid start [degC]   启动加热到目标温度
solenoid stop           停止（关闭加热）
solenoid set <degC>     在线修改目标温度
solenoid status         显示 stage/temperature/setpoint/duty/fault
solenoid reset          清除故障
```

> CLI 命令名沿用 `solenoid`（与压力 demo 一致）；温度场景语义相同。

## 参数

`pid_kp / pid_ki / pid_kd`、`pid_target`（默认 25°C）、
`pid_max_value`（超温联锁 120°C）、`pid_duty_min_pct / pid_duty_max_pct`、
`pid_ramp_timeout_ms`。

## 验证

- 主机单测：复用 `test_pid_math`（算法与领域无关）
- SIL 闭环：NTC 反算分压电压驱动 ADC mock，热质量模型（加热升温
  ~0.05°C/s·%）验证升温到目标进入 HOLD、超温触发 FAULT 且加热器关闭
- 台架联调：`solenoid start 50` 观察温度逼近 50°C 后进入 HOLD
