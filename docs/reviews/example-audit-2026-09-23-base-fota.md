# 示例复核：examples/base 与 examples/fota_demo（2026-09-23）

复核对象：两个示例的**六层 YAML 配置** + **当前模板实际生成并编译出来的产物**，
而不是 `output/` 下可能过期的旧目录（`output/` 已被 `.gitignore` 忽略，
`output/base` 原产物停留在 2026-09-17，早于 FR-15.6，本次已重新生成并编译）。

复核手段：`examples/*` 全量横向扫描（项目名 / bootloader 开关 / 各层条目数）、
`base` 与 `fota_demo` 逐层文件哈希比对、生成产物目录树差集、FOTA 侧导出 API 与
生成测试清单、重新生成 + Ninja 全量编译。

---

## 1. 结论速览

| | base | fota_demo |
|---|---|---|
| 定位 | 最小可用系统单元，CI 唯一构建对象 | base + 双槽 bootloader/差分 OTA 全套 |
| 六层 YAML 差异 | — | **仅 `hardware.yaml` 多了 `bootloader:` 段**，其余五层逐字节相同 |
| 生成产物增量 | — | 31 个文件（引导器子工程 + FOTA 驱动 + 双槽链接脚本 + 3 个新增测试） |
| CLI 命令 | 12 | 13（多 `fota`） |
| FreeRTOS 堆 | 11264 B（自动推算） | 13312 B（自动推算，含 FOTA） |
| 本机编译 | ✅ 重新生成 + 编译通过（22 s） | ✅ 首次本机编译通过（17 s） |
| 是否进 CI | ✅ `.github/workflows/build_and_test.yml` | ❌ **未进** |

---

## 2. base：功能清单

**硬件层**
- MCU STM32G0B1RET6 @ 16 MHz（HSI）
- PC0 = LED（低电平点亮）、PC13 = 按键（上拉 + EXTI 双沿）
- PA2/PA3 = USART2 @ 115200（CLI + 日志）
- 外设：USART2、`Internal_CLI`（提示符 `hw2c> `）、RTC（LSE 32.768 kHz，1 s 唤醒，**8 个闹钟**：1/15/30 s、2 min、5 min 周期 + 5 s、500 ms、2 s 单次）、内部温度传感器（ADC1 CH16）
- 日志：环形缓冲区 1024 B + 中断驱动 USART TX
- 低功耗：STOP1 + tickless（`sleep.mode` / `sleep.tickless`）

**软件层**
- 组件 3 个：`shell`（50 ms）、`led`（50 ms，off/fast/slow/fault 四种 pattern）、`btn`（10 ms，短按/双击/长按；长按 3000 ms、双击窗口 500 ms、消抖 50 ms）
- 任务：`events_process_task`（prio 3 / 栈 512 / 队列 16）+ 自动的 `cli_task`
- 状态机：单状态 `IDLE`，3 个按键手势 → LED 模式 + 日志 + `shell_temp`（读片内温度）
- 周期事件：`SECONDS_30` → `telemetry_snapshot` + `power_status`；`MINUTE_5` → `telemetry_snapshot`
- 参数 7 个、pubsub 4 个 topic、CLI 12 命令（FR-15.1 的"内置 12 命令"与之吻合）
- 故障溯源：`hw2c_fault` 上电上报上次复位捕获的故障码与次数
- 宿主测试 12 个（`test_btn/test_cli/test_event_mgr/test_gpio/test_hw2c_fault/test_led/test_rtc/test_rtc_timers/test_statemachine/test_uart` 等）+ `test/sil`

**没有的**：bootloader、双槽链接脚本、FOTA 驱动、IWDG（`drv_iwdg` 仅在开启 bootloader 时自动注入）。

---

## 3. fota_demo：在 base 之上多出的 31 项

**引导器子工程 `bootloader/`**
- `main.c` 决策表：① 硬件初始化（3 次慢闪存活指示）② 读持久化状态 ③ `boot_ok` 缺失 → `attempt+1` ④ `attempt > max_retries(3)` → 换槽软复位 ④**补丁落地检测**（`fota_meta` 为 `DONE` 且目标槽 CRC 通过 → 切槽软复位）⑤ 硬件 CRC32 校验活动槽 ⑥ 失败则**直接验另一槽**：可用就切槽软复位，两槽都坏则进入 SOS 死循环（FR-14.8）
- `boot_crc`（镜像头 CRC32，覆盖 `magic + fw_version + payload`）、`boot_nvm`（TAMP：`BKP0R` 失败计数 / `BKP1R` 活动槽 / `BKP2R` boot_ok / `BKP3R` 初始化魔数）、`boot_jump`（切 MSP + 跳 App 向量表）

**App 侧 FOTA**
- `drv_fota`：状态机 `IDLE→RECEIVING→READY→APPLYING→DONE/ERROR`；帧协议 `START 0xA5 / DATA 0xA4 / FINISH 0xA6 / ACK 0x06 / NAK 0x15`，分片 1024 B、ACK 超时 2 s、CRC-16/CCITT-FALSE 覆盖 `seq|len|data`；补丁暂存到**目标槽尾部**（掉电免重传），续传粒度 = 已提交 DATA 帧
- `drv_fota_meta`：引导器区最后一页 Flash（2 KB）日志式追加，24 B/条 × 85 条，`max(seq)` 最新者胜，CRC16 校验，页满才擦
- `drv_fota_delta`：H2CD v1 信封（48 B）解析 + HPatchLite lite 流应用 + 应用后 CRC 复算与向量表检查
- `drv_fota_ymodem`：YMODEM 通道（`fota ymodem`，Tera Term / lrzsz `sb` 可直接发）
- `drv_iwdg`：自动注入，5 s 超时，差分擦写期间按页喂狗
- `boot_app`：`boot_app_mark_ok()` + `boot_app_image_version/magic/slot_base`（FR-15.6 横幅版本自检）

**构建与真源**
- 链接脚本三套：`bootloader.ld`、`app_slot_a.ld`（0x08002000）、`app_slot_b.ld`（0x08040000），槽首 `0xC0` 预留 16 B 镜像头
- `fota_format.json`：镜像头 / 差分信封 / 传输帧 / 暂存 / 元数据五节的唯一真源（`generator/tests/test_fota_format.py` 钉死 C 侧与 Python 侧一致）
- 主机侧工具：`generator/patch_crc.py`（CRC 与版本号原位回填）、`delta_tool.py`、`fota_sender.py`、`fota_ymodem_sender.py`
- CLI：`fota status | progress | recv | ymodem | apply | erase`
- 生成测试：`test_boot_crc.c`、`test_boot_jump.c`、`test_boot_nvm.c`、`test_fota_protocol.c`、`test_fota_ymodem.c`

---

## 4. 复核发现的问题

### P1 — fota_demo 不在 CI 里，与其自身注释宣称的职责矛盾

`.github/workflows/build_and_test.yml` 只有 `EXAMPLE_PATH: examples/base` / `OUTPUT_DIR: output/base`，
job `build` 也只生成并编译这一个示例。而 `examples/fota_demo/hardware.yaml` 顶部明写着：

> 因此本示例的职责是"当编译对象"：它打开的每一条分支…都必须能生成并通过编译，**否则 CI 会失败而不是静默略过**。

实际这条不成立 —— fota_demo 从没进过 CI 的编译矩阵。也就是说，FR-14（bootloader/差分 OTA）
这条被标 ✅ 的需求，在 CI 里仍然是"一个从不参与构建的模板"，正是该注释本想消灭的状态。
本次复核在本机补做了验证（fota_demo 生成 + 编译通过），但这不能替代 CI。

### P2 — FR-15.5 的 `param get/set` 在生成固件里不存在

需求 FR-15.5：「运行时可调参数经 CLI `param get/set`」。实际模板没有 `param` 命令：
base 生成 12 条命令、fota_demo 13 条，都不含 `param`。`param_registry.c` 只提供 C API。

进一步看消费者：base/fota_demo 定义的 7 个参数里，只有 2 个真被读
（`btn_BUTTON_long_press_ms`、`btn_BUTTON_double_click_ms`，`btn_component.c`），
其余 5 个（`led_brightness`、`log_level`、`telemetry_enabled`、`telemetry_interval_s`、`sleep_depth`）
注册进表里但没有调用点 —— 属于"YAML 写了、没有代码会读"的同类缺陷。
其中 `led_brightness` 还挂在 `component: shell` 下、描述写"LED PWM duty"，
而这份硬件配置里根本没有 PWM。

### P3 — pubsub 的 4 个 topic 是纯声明

`component_bus.h` 生成了 `TOPIC_LED_STATE / BUTTON_PRESS / TEMPERATURE / ALARM` 与
`bus_publish_*`、`bus_subscribe` 全套 API，但整个 `src/` 里没有任何调用点 ——
运行期零流量。topic 只存在于枚举与 `bus_topic_name()` 里。

### P4 — fota_demo 的 `task.yaml::project.name` 是复制遗留的死键

`examples/fota_demo/task.yaml` 写的是 `project.name: base`，而实际生成的工程名/横幅是
`fota_demo`（取自**输出目录名**，`generate.py:1187` `os.path.basename(actual_output)`）。
`mapper.merge()` 虽然也读 `task.project.name`，但在六层 YAML 主路径上被 `build_context()`
的 `project_name` 实参覆盖 —— 于是这个键既不生效、还与产物名矛盾，读配置的人会被带偏
（十个示例里只有这一个不一致）。

### P5 — 两个示例都没设 `rtos_heap_size`

base/fota_demo 未配置，走自动推算：11264 B / 13312 B；`mhde_mainboard` 显式设了 20480 B。
示例是给别人抄的模板，默认值偏小且不显式，容易被当成推荐值照抄。

---

## 5. 本次做的验证

| 项 | 结果 |
|---|---|
| `examples/base` 重新生成（旧产物移到 `output/_old_base_20260923`） | gen=0 |
| base cmake configure + 全量编译 | cfg=0 / build=0，`base.elf` 产出 |
| base 横幅（非 bootloader 分支） | `base v1.0.0 — Hardware2Code \| …` + `Firmware v%lu.%lu.%lu (0x%06lX)` |
| `examples/fota_demo` 生成（09:56）+ 编译 | cfg=0 / build=0 |
| base CLI 命令表 | help/version/uptime/free/tasks/reset/gpio/led/rtc/telemetry/power/sysinfo（12） |
| fota_demo CLI 命令表 | 上述 12 + `fota`（13） |

即 FR-15.6 落地后 CI 路径（base，无 bootloader 分支）仍然编译通过 —— 这条此前没验过。

---

## 6. 建议动作（按优先级）

1. **把 fota_demo 加进 CI 矩阵**（或直接加第二个 job）：至少 `-o output/fota_demo` 再生成+编译一遍；
   否则 FR-14 的"编译覆盖"仍是空的。若要更强的保证，再跑 `output/fota_demo/test/run_tests.py`。
2. **补 `param` CLI 命令**（`param list/get/set`），并让 `telemetry_enabled` / `telemetry_interval_s` /
   `log_level` / `sleep_depth` 有真实消费者；无人消费的参数要么删、要么接到现有组件上。
   `led_brightness` 挂 `component: shell` 且无 PWM，建议改成 `component: led` 并由 led 组件使用，或删除。
3. **改 fota_demo 的 `project.name`** 为 `fota_demo`；更彻底的做法是让生成器在
   `task.project.name` 与输出目录名不一致时**报警**（现在静默取目录名）。
4. **pubsub**：给 led/btn 组件接上 `bus_publish_*`，或在文档里写明"topic 需由用户组件自行接入"，
   别让人以为声明了就自动有消息流。
5. 两个示例显式写 `rtos_heap_size`，值取能证明够用的数（参考 mhde_mainboard 的 20480）。
