# hw2c Template Development Guide

## Overview

hw2c uses the **Jinja2** template engine to generate complete embedded C projects from hardware description files (`hardware.yaml`). Templates are located in the `templates/` directory and rendered by the Python generator into the target output directory.

## Directory Structure

```
templates/
├── macros.j2                     # Reusable Jinja2 macros (EXTI mapping helpers)
├── src/
│   ├── main.c.j2                 # App entry point + FreeRTOS task creation
│   ├── gpio.c.j2                 # GPIO + EXTI initialization
│   ├── sleep.c.j2                # Low-power idle hook
│   ├── stm32g0xx_it.c.j2         # Interrupt handlers (EXTI, RTC)
│   ├── event_mgr.c.j2            # Event manager (queue-based dispatcher)
│   └── event_mgr.h.j2            # Event manager header (event IDs, queue API)
├── app/
│   ├── statemachine.c.j2         # State machine engine (flat / substate / parallel)
│   └── statemachine.h.j2         # State machine header
├── drivers/
│   ├── drv_rtc.c.j2 + .h.j2      # RTC driver (calendar + software timer manager)
│   ├── drv_log.c.j2 + .h.j2      # USART2 log subsystem (ring buffer, TXE IRQ)
│   ├── drv_iwdg.c.j2 + .h.j2     # Independent watchdog (HAL_IWDG)
│   ├── drv_adc.c.j2 + .h.j2      # ADC driver
│   ├── drv_pwm.c.j2 + .h.j2      # PWM output driver
│   ├── drv_uart.c.j2 + .h.j2     # UART serial driver
│   ├── drv_spi_flash.c.j2 + .h.j2 # SPI Flash (W25Q32) driver
│   ├── drv_i2c_mpu6050.c.j2 + .h.j2 # I2C MPU6050 sensor driver
│   ├── drv_eeprom.c.j2 + .h.j2   # I2C EEPROM storage driver
│   ├── drv_rs485.c.j2 + .h.j2    # RS485 half-duplex transceiver driver
│   ├── drv_cellular.c.j2 + .h.j2 # Cellular 4G Cat.1 modem driver
│   ├── drv_ir.c.j2 + .h.j2       # Infrared NEC/SIR communication driver
│   ├── drv_modbus.c.j2 + .h.j2   # Modbus RTU protocol driver
│   ├── drv_mqtt.c.j2 + .h.j2     # MQTT 3.1.1 client driver
│   ├── drv_cli.c.j2 + .h.j2      # UART CLI debug shell driver
│   ├── drv_fota.c.j2 + .h.j2     # FOTA receive state machine + shared staging session
│   ├── drv_fota_ymodem.c.j2 + .h.j2 # FOTA over YMODEM (second byte-delivery channel)
│   ├── fota_meta.c.j2 + .h.j2    # Flash-page metadata journal (shared with the bootloader)
│   └── fota_delta.c.j2 + .h.j2   # H2CD delta decoder (pure algorithm + injectable I/O)
├── bootloader/
│   ├── boot_main.c.j2            # Bootloader entry point
│   ├── boot_nvm.c.j2 + .h.j2     # Non-volatile storage (TAMP backup registers)
│   ├── boot_crc.c.j2 + .h.j2     # Hardware CRC32 verification
│   ├── boot_jump.c.j2 + .h.j2    # App jump logic (SP/MSP validation, VTOR)
│   └── boot_app.c.j2 + .h.j2     # App-side boot flag helpers
├── linker/
│   ├── STM32G0B1RETx_FLASH.ld.j2  # Standard app linker script
│   ├── bootloader.ld.j2           # Bootloader linker script (8KB)
│   ├── app_slot_a.ld.j2           # App Slot A linker script
│   └── app_slot_b.ld.j2           # App Slot B linker script
├── project/
│   ├── CMakeLists.txt.j2          # App CMake build script
│   ├── bootloader_CMakeLists.txt.j2 # Bootloader standalone CMake script
│   └── toolchain.cmake            # ARM GCC cross-compilation toolchain (static)
├── config/
│   ├── FreeRTOSConfig.h.j2        # FreeRTOS kernel config
│   └── stm32g0xx_hal_conf.h.j2    # HAL module enable/disable
├── test/
│   ├── mock_hal.c.j2 + .h.j2      # Mock HAL for unit tests (PC-native)
│   ├── test_gpio.c.j2             # GPIO unit test
│   ├── test_rtc.c.j2              # RTC unit test
│   ├── test_rtc_timers.c.j2       # RTC software timer unit test
│   ├── test_event_mgr.c.j2        # Event manager unit test
│   ├── test_statemachine.c.j2     # Flat state machine unit test
│   ├── test_substate.c.j2         # Nested substate unit test
│   ├── test_parallel.c.j2         # Parallel region unit test
│   ├── test_adc.c.j2              # ADC unit test
│   ├── test_pwm.c.j2              # PWM unit test
│   ├── test_uart.c.j2             # UART unit test
│   ├── test_spi_flash.c.j2        # SPI Flash unit test
│   ├── test_mpu6050.c.j2          # MPU6050 unit test
│   ├── hil_test.c.j2              # HIL (Hardware-in-Loop) test
│   └── CMakeLists_test.txt.j2     # Test CMake build
└── vscode/
    ├── settings.json.j2           # VSCode C/C++ settings
    ├── tasks.json.j2              # VSCode build tasks
    ├── launch.json.j2             # VSCode debug launch config
    ├── c_cpp_properties.json.j2   # VSCode IntelliSense config
    └── extensions.json.j2         # VSCode recommended extensions
```

## Template Rendering Context

The generator parses `hardware.yaml` and builds a **context dictionary** passed to every template. All templates share the same context but use only the variables they need.

### Core Context Variables

| Variable | Type | Description | Example |
|----------|------|-------------|---------|
| `mcu` | object | MCU info (`part`, `core_clock_mhz`, `hse_freq`) | `{"part": "STM32G0B1RET6", "core_clock_mhz": 64}` |
| `pins` | list[object] | Pin list with `id`, `function`, `label`, `pull`, `exti`, `notify_task`, `af` | See [Pin Object](#pin-object) |
| `app_tasks` | list[object] | FreeRTOS tasks (`name`, `priority`, `stack_size`) | `[{"name": "led_task", "priority": 2, "stack_size": 128}]` |
| `sleep` | object | Low-power config (`mode`) | `{"mode": "STOP1"}` |
| `project_name` | string | Project name (default `hw2code`) | `"blinky_g0"` |
| `heap_size` / `stack_size` | string | Heap/stack sizes in hex | `"0x200"` |
| `peripherals` | list[object] | All configured peripherals with loaded `model` | See [Peripheral Object](#peripheral-object) |
| `drivers` | list[object] | All driver entries (name, template, header_template, model, peripheral) | See [Driver Object](#driver-object) |

### Condition Flags

| Variable | Type | When True |
|----------|------|-----------|
| `has_rtc` | bool | RTC peripheral configured |
| `has_log` | bool | UART peripheral configured (auto-enables USART2 logging) |
| `has_bootloader` | bool | `bootloader.enabled: true` in YAML |
| `has_behavior` | bool | `behavior` node exists in YAML |
| `has_substate` | bool | Business flow contains composite (nested) states |
| `has_led` | bool | A pin with `label: "LED"` exists |
| `has_led_task` | bool | A task named `led_task` is in `app_tasks` |
| `has_i2c` | bool | Any I2C peripheral configured |
| `has_spi` | bool | Any SPI peripheral configured |
| `has_spi_flash` | bool | `SPI_Flash_W25Q32` peripheral configured |
| `has_mpu6050` | bool | `I2C_Sensor_MPU6050` peripheral configured |
| `has_pwm` | bool | Any PWM peripheral configured |
| `has_adc` | bool | Any ADC peripheral configured |
| `has_uart` | bool | Any UART peripheral configured |
| `has_iwdg` | bool | IWDG peripheral or bootloader enabled |
| `has_eeprom` | bool | `I2C_EEPROM` peripheral configured |
| `has_rs485` | bool | `RS485` peripheral configured |
| `has_cellular` | bool | `Cellular_4G` peripheral configured |
| `has_ir` | bool | `Internal_IR` peripheral configured |
| `has_modbus` | bool | `Protocol_Modbus` peripheral configured |
| `has_mqtt` | bool | `Protocol_MQTT` peripheral configured |
| `has_cli` | bool | `Internal_CLI` peripheral configured |
| `has_fota` | bool | FOTA peripheral configured |

### Boot Config Object

Available when `has_bootloader` is true:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable dual-slot bootloader |
| `size_kb` | int | `8` | Bootloader flash size in KB |
| `app_a_offset` | hex | `0x2000` | App Slot A base offset |
| `app_b_offset` | hex | `0x40000` | App Slot B base offset |
| `crc_method` | string | `"crc32_hw"` | CRC method (`crc32_hw` = STM32G0 hardware CRC) |
| `boot_flag_src` | string | `"tamp_bkp"` | Boot flag storage (`tamp_bkp` = TAMP backup registers) |
| `max_retries` | int | `3` | Max boot retries before fallback |
| `wdg_timeout_ms` | int | `5000` | IWDG timeout in milliseconds |
| `boot_magic` | string | `"H2Ck"` | 4-byte magic for firmware metadata header |

### Pin Object

```json
{
    "id": "PC13",
    "label": "BUTTON",
    "function": "GPIO_Input",
    "pull": "up",
    "exti": {"enable": true, "trigger": "falling"},
    "notify_task": "button_led_task"
}
```

### Peripheral Object

```json
{
    "name": "rtc",
    "type": "Internal_RTC",
    "model": {
        "type": "Internal_RTC",
        "interface": "internal",
        "driver_template": "drivers/drv_rtc.c.j2",
        "header_template": "drivers/drv_rtc.h.j2"
    }
}
```

### Driver Object

```json
{
    "name": "rtc",
    "template": "drivers/drv_rtc.c.j2",
    "header_template": "drivers/drv_rtc.h.j2",
    "model": {"type": "Internal_RTC", ...},
    "peripheral": {"name": "rtc", ...}
}
```

---

## Core Templates

### `macros.j2`

Reusable helper macros.

| Macro | Input | Output | Description |
|-------|-------|--------|-------------|
| `exti_irq_name(pin_id)` | `"PC13"` | `EXTI4_15_IRQn` | NVIC IRQ number for the EXTI line |
| `exti_handler_name(pin_id)` | `"PC13"` | `EXTI4_15_IRQHandler` | ISR function name for the EXTI line |
| `pin_port(pin_id)` | `"PC13"` | `C` | Extract GPIO port letter |
| `pin_number(pin_id)` | `"PC13"` | `13` | Extract pin number |

### `src/main.c.j2`

**Output:** `src/main.c`

**Responsibilities:**
- Generates `LED_GPIO_Port` / `LED_GPIO_Pin` macros from `pins` with `label == 'LED'`
- Provides `SystemClock_Config()` (HSI 16 MHz default)
- Includes all driver headers via `{% for drv in drivers %}` loop
- Creates all FreeRTOS tasks from `app_tasks`
- Integration points for bootloader, logging, state machine
- LED heartbeat pattern when bootloader is enabled (2 quick blinks)

**Key logic:**
```jinja2
{% if has_bootloader %}
    IWDG_Init();
    boot_app_mark_ok();
{% endif %}
```

### `src/gpio.c.j2`

**Output:** `src/gpio.c`

**Responsibilities:**
- Enables SYSCFG clock for EXTI mapping (`__HAL_RCC_SYSCFG_CLK_ENABLE()`)
- Iterates `pins` and generates `HAL_GPIO_Init()` for each
- Supports: GPIO_Output, GPIO_Input (EXTI), I2C (AF_OD), SPI (AF_PP), UART (AF_PP), ADC (Analog)
- Enables NVIC IRQ for pins with `exti.enable == true`

### `src/sleep.c.j2`

**Output:** `src/sleep.c`

**Responsibilities:**
- Implements `vApplicationIdleHook()` → `__WFI()` for Tickless Idle
- Puts MCU into low-power mode when no tasks are ready

### `src/stm32g0xx_it.c.j2`

**Output:** `src/stm32g0xx_it.c`

**Responsibilities:**
- Generates EXTI IRQ handlers for each pin with EXTI configured
- Calls `xTaskNotifyFromISR()` if pin has `notify_task`
- Includes `RTC_TAMP_IRQHandler` for RTC wakeup timer
- Preserves FreeRTOS-required handlers (`SysTick_Handler`, `PendSV_Handler`, `SVC_Handler`)

### `src/event_mgr.c.j2` / `src/event_mgr.h.j2`

**Output:** `src/event_mgr.c` / `src/event_mgr.h`

**Responsibilities:**
- Defines all event IDs (`EVENT_BUTTON_PRESS`, `EVENT_RTC_TICK`, etc.)
- Event manager task (`EventMgr_Task`) reads from `event_queue` and dispatches
- Routes events to `statemachine_process()` if business flow is defined
- Calls `RTC_ProcessTimers()` for `EVENT_RTC_TICK`

---

## App Templates (State Machine)

### `app/statemachine.c.j2` / `app/statemachine.h.j2`

**Output:** `src/statemachine.c` / `src/statemachine.h`

**Responsibilities:**
- Generates the complete state machine engine from `behavior` DSL
- Supports three modes controlled by `has_substate` and `regions` presence:

| Mode | Condition | Features |
|------|-----------|----------|
| Flat | No `regions`, no nested `states` | Simple state transitions with actions |
| Substate | `states` contains nested `states` | Composite states, `return` action |
| Parallel | `regions` node present | Multiple concurrent regions, `send_to` cross-region communication |

**Generated artifacts:**
- State enum constants (`STATE_IDLE`, `STATE_ACTIVE`, etc.)
- `statemachine_init()` — enters initial state
- `statemachine_process(event_t *evt)` — event dispatch switch
- Software timer callbacks for `defer` and state timeout
- Action implementations: `toggle_led`, variable `set`, etc.

---

## Driver Templates

All driver templates follow a consistent pattern:
- `#ifdef TEST` / `#else` / `#endif` for mock vs. real HAL includes
- Header guard `#ifndef __DRV_XXX_H`
- Function naming: `{Peripheral}_Init()`, `{Peripheral}_{Action}()`

### `drivers/drv_rtc.c.j2` / `drivers/drv_rtc.h.j2`

**Condition:** Generated when `has_rtc` is true.

**API:**
| Function | Description |
|----------|-------------|
| `RTC_Init()` | Initialize RTC with LSE clock source and wakeup timer |
| `RTC_Start()` | Start RTC wakeup timer interrupts |
| `RTC_GetTime(rtc_time_t *)` | Read current calendar time |
| `RTC_SetTime(rtc_time_t *)` | Set calendar time |
| `RTC_AdjustDrift(int16_t ppm)` | Hardware smooth calibration |
| `RTC_TimerCreate(period_ms, mode, cb, arg)` | Create a software timer |
| `RTC_TimerStart(handle)` | Start a software timer |
| `RTC_TimerStop(handle)` | Stop a software timer |
| `RTC_TimerDelete(handle)` | Delete a software timer |
| `RTC_ProcessTimers()` | Process expired timers (called from EventMgr) |

**Design notes:**
- RTC init uses `HAL_RTC_Init()` + `HAL_RCCEx_PeriphCLKConfig()`, not direct register writes.
- Software timer manager uses a linked list. Timers are checked when `RTC_ProcessTimers()` is called.

### `drivers/drv_log.c.j2` / `drivers/drv_log.h.j2`

**Condition:** Generated when `has_uart` is true (auto-attaches to USART2 at PA2/PA3).

**API:**
| Function | Description |
|----------|-------------|
| `log_init()` | Initialize USART2 with TXE interrupt-driven ring buffer |
| `log_trace(fmt, ...)` | TRACE level log |
| `log_debug(fmt, ...)` | DEBUG level log |
| `log_info(fmt, ...)` | INFO level log |
| `log_warn(fmt, ...)` | WARN level log |
| `log_error(fmt, ...)` | ERROR level log |
| `log_fatal(fmt, ...)` | FATAL level log |
| `log_flush()` | Block until ring buffer is drained (for crash-safe logging) |
| `log_system_info()` | Print system banner (MCU, clock, RTOS version) |

**Design notes:**
- Ring buffer + TXE interrupt = ISR-safe, no blocking in normal operation.
- Based on rxi/log.c design patterns.
- `log_flush()` ensures pending logs are output before potentially crashing operations (e.g., TAMP register writes).

### `drivers/drv_iwdg.c.j2` / `drivers/drv_iwdg.h.j2`

**Condition:** Auto-injected when `has_bootloader` is true.

**API:**
| Function | Description |
|----------|-------------|
| `IWDG_Init()` | Initialize IWDG with `HAL_IWDG_Init()`, prescaler /256, timeout from `wdg_timeout_ms` |
| `IWDG_Refresh()` | Feed the watchdog via `HAL_IWDG_Refresh()` |

**Macro:**
| Macro | Source | Description |
|-------|--------|-------------|
| `IWDG_TIMEOUT_MS` | `peripheral.wdg_timeout_ms` | Watchdog timeout in milliseconds (default 5000) |

**Design notes:**
- Uses `HAL_IWDG_Init()` instead of direct register manipulation.
- Timeout calculation: `reload = IWDG_TIMEOUT_MS / 8` (LSI ~32 kHz, prescaler /256 → 8 ms per tick).
- Init is placed after `boot_app_mark_ok()` in `main()` so Bootloader can detect failed boots.

### `drivers/drv_adc.c.j2` / `drivers/drv_adc.h.j2`

**Condition:** Generated when `has_adc` is true (peripheral type `Internal_ADC`).

### `drivers/drv_pwm.c.j2` / `drivers/drv_pwm.h.j2`

**Condition:** Generated when `has_pwm` is true (peripheral type `Internal_PWM`).

### `drivers/drv_uart.c.j2` / `drivers/drv_uart.h.j2`

**Condition:** Generated when `has_uart` is true (peripheral type `UART_Serial`).

### `drivers/drv_spi_flash.c.j2` / `drivers/drv_spi_flash.h.j2`

**Condition:** Generated when `has_spi_flash` is true (peripheral type `SPI_Flash_W25Q32`).

### `drivers/drv_i2c_mpu6050.c.j2` / `drivers/drv_i2c_mpu6050.h.j2`

**Condition:** Generated when `has_mpu6050` is true (peripheral type `I2C_Sensor_MPU6050`).

### `drivers/drv_eeprom.c.j2` / `drivers/drv_eeprom.h.j2`

**Condition:** Generated when `has_eeprom` is true (peripheral type `I2C_EEPROM`).

I2C EEPROM storage driver. Supports page-aligned read/write, configurable I2C address, page size, and total storage size.

### `drivers/drv_rs485.c.j2` / `drivers/drv_rs485.h.j2`

**Condition:** Generated when `has_rs485` is true (peripheral type `RS485`).

RS485 half-duplex transceiver driver with automatic DE/RE direction control.

### `drivers/drv_cellular.c.j2` / `drivers/drv_cellular.h.j2`

**Condition:** Generated when `has_cellular` is true (peripheral type `Cellular_4G`).

Cellular 4G Cat.1 modem driver. Manages AT command interaction, PDP activation, and network registration.

### `drivers/drv_ir.c.j2` / `drivers/drv_ir.h.j2`

**Condition:** Generated when `has_ir` is true (peripheral type `Internal_IR`).

Infrared communication driver supporting NEC and SIR protocols for both transmit and receive.

### `drivers/drv_modbus.c.j2` / `drivers/drv_modbus.h.j2`

**Condition:** Generated when `has_modbus` is true (peripheral type `Protocol_Modbus`).

Modbus RTU protocol driver. Supports master and slave roles, with configurable slave ID. Typically used with RS485 or UART as the underlying bearer.

### `drivers/drv_mqtt.c.j2` / `drivers/drv_mqtt.h.j2`

**Condition:** Generated when `has_mqtt` is true (peripheral type `Protocol_MQTT`).

MQTT 3.1.1 client driver. Supports connect/publish/subscribe with configurable broker, client ID, and keep-alive. Typically used with Cellular_4G as the underlying bearer.

### `drivers/drv_cli.c.j2` / `drivers/drv_cli.h.j2`

**Condition:** Generated when `has_cli` is true (peripheral type `Internal_CLI`).

UART-based interactive debug shell. Provides commands including `help`, `version`, `uptime`, `free`, `tasks`, `reset`, `gpio read/write`, `led on/off/toggle`, `rtc time/set`, and peripheral-specific commands (Modbus, Cellular, MQTT, FOTA).

**Configuration:**
| Parameter | Default | Description |
|-----------|---------|-------------|
| `prompt` | `"> "` | Shell prompt string |
| `stack_size` | `512` | CLI task stack size in words |
| `priority` | `4` | CLI task priority |
| `max_cmd_len` | `64` | Maximum command line length |

### `drivers/fota_delta.c.j2` / `drivers/fota_delta.h.j2`

**Condition:** Generated when `has_bootloader` is true.

Device-side delta decoder. Pure algorithm with an **injectable I/O backend**
(`fota_delta_backend_t`: patch read / erase / program / read / watchdog /
progress) and **no `#ifdef TEST`** — the same source is compiled for the target
and for the host. Decoding a patch erases the destination slot, writes the new
image page by page, fills in the image header last, and re-reads the payload to
recompute and compare the CRC32.

Layout constants come from `generator/data/fota_format.json`; the templates
never contain a literal offset.

### `drivers/drv_fota.c.j2` / `drivers/drv_fota.h.j2`

**Condition:** Generated when `has_bootloader`, `has_uart` **and** a CLI driver
are all present (`has_fota_receive`).

FOTA receive state machine — **transport and persistence**, not algorithm.
Parses the three transport frames (START / DATA / FINISH), acknowledges in order,
re-ACKs duplicates and out-of-order chunks, NAKs bad CRCs, and stages the patch
into the **tail of the destination slot** so that a power loss does not require
re-transmission. The application step runs synchronously in the dedicated
low-priority `fota` task after the outputs have been driven to their safe state.

Transfer state lives in a **Flash metadata journal** (`fota_meta.c`, a dedicated
page — *not* TAMP backup registers: on STM32G0B1 the TAMP only has `BKP0R..BKP4R`
and they are already taken by `boot_nvm` / `boot_main`). The journal must be
self-describing (`magic` / `state` / `slot` / `progress` / `fmt_ver` / crc) rather
than positional, because the bootloader reads the same records and both sides
must agree byte for byte.

The UART byte stream is handed over explicitly by the `fota recv` CLI command
(`cli_set_rx_sink()`), because the CLI's line editor is otherwise the only
consumer of those bytes.

**`fota_session_*` — the shared staging session.** Both transports differ *only*
in how bytes arrive (fixed-size chunks with explicit sequence numbers and
stop-and-wait ACK vs. 128/1024-byte blocks with a 16-bit CRC). Everything after
that must be byte-identical: capacity admission formula, staging geometry
(`staging_base = slot_base + slot_size - align_up(record, 8)`), resume decision,
envelope-first commit, double-word aligned batching, `0xFF` tail padding, and the
final "read the whole record back out of Flash and recompute CRC32". Those live
here once — two copies would not be "duplicated code" but **two admission
formulas**, and the boundary cases such an off-by-one-page bug lives on are
exactly the ones one of the two copies fails to test. Likewise "which states may
enter receive mode" is a security policy, so it has a single gate
(`fota_receive_ready()`).

`fota_transport_t { NONE, FRAME, YMODEM }` exists because the two transports need
**mutually exclusive periodic work** in `fota_process()`: the frame protocol's
idle timeout aborts the receive, while YMODEM needs to re-send `'C'` and honour
block timeouts. Both derive their time base from the same `g_last_activity_ms`,
so without the discriminator a YMODEM transfer would be judged "host went silent"
and aborted mid-flight (after 2 s — it looks like "the device gives up halfway").

> `fota_transport_t` must be declared in the **header**, not the `.c`:
> `fota_transport_begin()` takes it as a parameter and `drv_shell.c` / `main.c` /
> `event_mgr.c` only include the header. Leaving it in the `.c` fails those
> translation units with `unknown type name`.

### `drivers/drv_fota_ymodem.c.j2` / `drivers/drv_fota_ymodem.h.j2`

**Condition:** Same as `drv_fota` (`has_fota_receive`).

YMODEM batch-mode receiver, i.e. the second byte-delivery channel. Lets an
operator upgrade with **any terminal software's built-in YMODEM send** (Tera Term,
SecureCRT, ExtraPuTTY, `lrzsz sb`) instead of the repo's own `fota_sender.py`.
Entry point is the `fota ymodem` CLI command; the payload is the whole `.h2cd`
patch record (48 B envelope + lite stream) — the same bytes the frame protocol
sends.

All control bytes, block sizes and timings come from
`generator/data/ymodem_format.json`; the template contains no control-byte
literal.

Four traps, each capable of "all-green in self-tests, dead against real
software":

| # | Trap | Consequence |
|---|---|---|
| 1 | **YMODEM uses CRC-16/XMODEM** (init `0x0000`, `'123456789'` → `0x31C3`), while this repo's frame protocol uses CRC-16/**CCITT-FALSE** (init `0xFFFF` → `0x29B1`) | Reusing `fota_crc16()` makes the device talk to our own sender perfectly and to **every real terminal not at all** — an A3-class "both sides implement a different convention, each self-consistently" defect. The device therefore implements its own XMODEM CRC. |
| 2 | Final block is padded with `0x1A` | Must be truncated to the length declared in block 0, or the patch grows a tail of `0x1A` |
| 3 | `CAN` (`0x18`) only counts as an abort **at a block boundary**, and only twice in a row | A random `0x18` inside the patch would otherwise abort the transfer |
| 4 | `(uint8_t)YMODEM_BLK_MODULUS` is `(uint8_t)256U` = `0` | Runtime division by zero → HardFault on real hardware. Do the modulo in 32-bit and cast last. |

Other semantics worth knowing before changing this file:

- YMODEM has **no whole-file check** — the device accumulates CRC-32/ISO-HDLC
  while receiving and hands it to the same `fota_session_finish()`.
- YMODEM has **no partial resume**, the host always re-sends from byte 0. The
  device treats the already-committed prefix as a replay to be re-verified byte
  by byte (`FOTA_STREAM_FROM_RECORD_START`) and fails fast on mismatch — skipping
  would also be correct but would report a corrupt stream only after a full
  transfer.
- Block 0's decimal length is the **only cross-check** between "length declared by
  the host" and "length declared by the envelope".
- A batch is exactly one patch; a second non-empty block 0 mid-transfer is
  rejected with `CAN CAN` rather than silently written into the same staging area.
- Transport-layer failures (handshake timeout, retry exhaustion, host `CAN`) do
  **not** escalate to `ERROR`: nothing on Flash was invalidated, the `RECEIVING`
  record is still valid and a re-send resumes. Escalating would show the operator
  a terminal state that calls for `fota erase` when retrying is all that is needed.

**Cost:** ~2.7 KB of code and a 1029 B block buffer (`g_blk`); see
`docs/plans/differential-ota.md` §19 for the measured numbers and the 14-case L5
harness.

**Tests:** two host-side test templates are generated alongside it when
`has_fota_receive` is true — `test_fota_protocol.c` (metadata journal semantics,
cross-reset state mapping, staging geometry) and `test_fota_ymodem.c` (handshake
cadence and attempt limit, block-number complement before CRC, CRC-16/XMODEM vs.
CCITT-FALSE, block-0 name/length parsing, the CAN boundary rule). Both must
`#include` **all three** FOTA sources (`drv_fota_meta.c`, `drv_fota.c`,
`drv_fota_ymodem.c`): `fota_process()` calls `fota_ymodem_process()`, so leaving
one out fails at link time. Full-batch behaviour (final-block `0x1A` truncation,
the two-packet EOT handshake, terminator block, resume re-verification) is
covered by the L5 harness instead — it needs the vendored decoder and a
block-by-block batch builder, neither of which exists in a generated project.

---

## Bootloader Templates

**Condition:** Generated when `has_bootloader` is true.

### `bootloader/boot_main.c.j2`

**Output:** `bootloader/main.c`

Bootloader entry point. Logic flow:
1. Check IWDG reset flag (`RCC->CSR & RCC_CSR_IWDGRSTF`) → count as boot failure
2. Read TAMP backup registers for boot state (active slot, fail count)
3. If fail count exceeds `max_retries`, fall back to alternate slot
4. Validate App header: search for `boot_magic` in vector table region
5. Verify CRC32 over firmware payload
6. If valid, jump to App via `boot_jump_to_app()`
7. If invalid, enter fail loop (diagnostic LED blink)

### `bootloader/boot_nvm.c.j2` / `bootloader/boot_nvm.h.j2`

**Output:** `bootloader/boot_nvm.c` / `bootloader/boot_nvm.h`

Non-volatile storage using TAMP backup registers (survives most resets).

| Function | Description |
|----------|-------------|
| `boot_nvm_read(uint32_t reg)` | Read a TAMP BKP register |
| `boot_nvm_write(uint32_t reg, uint32_t val)` | Write a TAMP BKP register (with DBP sync) |

### `bootloader/boot_crc.c.j2` / `bootloader/boot_crc.h.j2`

**Output:** `bootloader/boot_crc.c` / `bootloader/boot_crc.h`

Hardware CRC32 verification using STM32G0 CRC unit.

| Function | Description |
|----------|-------------|
| `boot_crc32_compute(uint32_t *data, uint32_t words)` | Compute CRC32 over word-aligned data |
| `boot_crc_verify(uint32_t flash_addr, uint32_t size, uint32_t expected)` | Verify CRC32 of flash region |

**Design notes:**
- Hardware CRC is configured with `CRC_CR_REV_IN_0 | CRC_CR_REV_OUT` to match CRC-32/MPEG-2 algorithm.
- Metadata header is located by searching for magic `"H2Ck"` in the vector table region, not at a fixed offset.

### `bootloader/boot_jump.c.j2` / `bootloader/boot_jump.h.j2`

**Output:** `bootloader/boot_jump.c` / `bootloader/boot_jump.h`

App jump logic.

| Function | Description |
|----------|-------------|
| `boot_jump_to_app(uint32_t app_addr)` | Validate SP/MSP, set VTOR, jump to App reset vector |

**Safety checks:**
- SP must be within SRAM range (`0x20000000` to `0x20024000` for 144KB SRAM)
- MSP initial value must be word-aligned
- Reset vector (PC) must be in flash range and thumb-mode (bit 0 set)

### `bootloader/boot_app.c.j2` / `bootloader/boot_app.h.j2`

**Output:** `src/boot_app.c` / `src/boot_app.h` (compiled into App, not Bootloader)

App-side helpers for marking boot status.

| Function | Description |
|----------|-------------|
| `boot_app_mark_ok()` | Write boot success flag to TAMP BKP register |

---

## Linker Templates

### `linker/STM32G0B1RETx_FLASH.ld.j2`

Standard App linker script (0x08000000 start, full 512KB flash).

### `linker/bootloader.ld.j2`

Bootloader linker script (first 8KB of flash, 0x08000000 to 0x08002000).

### `linker/app_slot_a.ld.j2`

App Slot A linker script (0x08002000 to 0x08040000).

### `linker/app_slot_b.ld.j2`

App Slot B linker script (0x08040000 to 0x08080000).

---

## Project Templates

### `project/CMakeLists.txt.j2`

**Output:** `CMakeLists.txt`

Cross-platform CMake build script with targets:
- `cmake --build build` — build firmware (default target)
- `cmake --build build --target flash` — ST-Link flash via STM32_Programmer_CLI
- `cmake --build build --target flash-daplink` — OpenOCD DAP-Link flash
- `cmake --build build --target test` — run unit tests
- `cmake --build build --target clean` — clean build artifacts

Uses `HARDWARE2CODE_STATIC` to reference HAL/CMSIS/FreeRTOS from `static/` directory.
Requires `cmake -B build -DCMAKE_TOOLCHAIN_FILE=toolchain.cmake` to configure before building.

### `project/toolchain.cmake` (static)

**Output:** `toolchain.cmake`

ARM GCC cross-compilation toolchain file. Defines Cortex-M0+ flags, finds
`arm-none-eabi-gcc` in PATH, sets nano.specs linker options.

### `project/bootloader_CMakeLists.txt.j2`

**Output:** `bootloader/CMakeLists.txt`

Standalone CMake script for building the Bootloader independently (bare-metal,
no FreeRTOS). Uses `-Os -g0` for minimal footprint.

---

## Config Templates

### `config/FreeRTOSConfig.h.j2`

**Output:** `config/FreeRTOSConfig.h`

- `configCPU_CLOCK_HZ` derived from `mcu.core_clock_mhz` (default 16 MHz)
- `configENABLE_MPU` = 0 (Cortex-M0+)
- `configTICK_RATE_HZ` = 1000
- Tickless Idle disabled (`configUSE_TICKLESS_IDLE` = 0)

### `config/stm32g0xx_hal_conf.h.j2`

**Output:** `config/stm32g0xx_hal_conf.h`

Standard HAL configuration with all modules enabled (conditional deactivation per project needs).

---

## Test Templates

**Condition:** Always generated. Tests are PC-native (compiled with `gcc -DTEST`), running against Mock HAL.

### `test/mock_hal.c.j2` / `test/mock_hal.h.j2`

**Output:** `test/mock_hal.c` / `test/mock_hal.h`

Complete mock layer for all HAL peripherals:
- GPIO (history-based: records all `HAL_GPIO_Init()` calls for verification)
- RTC, I2C, SPI, UART, ADC, PWM, TIM, IWDG
- FreeRTOS stubs (queues, tasks, notifications)
- CMSIS/Cortex stubs (NVIC, RCC, GPIO registers)
- Verification helpers (`mock_HAL_XXX_Init_called()`, `mock_HAL_XXX_reset()`)

### Unit Test Templates

Each test file includes `unity.h` and `mock_hal.h`, then `#include`s the source under test directly (`#include "../src/xxx.c"`). This allows white-box testing.

| Template | Tests | Compile-time include |
|----------|-------|---------------------|
| `test_gpio.c.j2` | Pin init call count, pin config verification | `../src/gpio.c` |
| `test_rtc.c.j2` | RTC_Init call, timer create/start | `../src/drivers/drv_rtc.c` |
| `test_rtc_timers.c.j2` | Multi-timer create, periodic trigger | `../src/drivers/drv_rtc.c` |
| `test_event_mgr.c.j2` | Queue create, event send/receive, dispatch | `../src/event_mgr.c` |
| `test_statemachine.c.j2` | Flat state transitions (IDLE ↔ ACTIVE) | `../src/statemachine.c` |
| `test_substate.c.j2` | Nested substate entry, switch, return | `../src/statemachine.c` |
| `test_parallel.c.j2` | Parallel region independent transitions | `../src/statemachine.c` |
| `test_adc.c.j2` | ADC init call verification | `../src/drivers/drv_adc.c` |
| `test_pwm.c.j2` | PWM init call verification | `../src/drivers/drv_pwm.c` |
| `test_uart.c.j2` | UART init call verification | `../src/drivers/drv_uart.c` |
| `test_spi_flash.c.j2` | SPI Flash init/read/write | `../src/drivers/drv_spi_flash.c` |
| `test_mpu6050.c.j2` | MPU6050 init/read | `../src/drivers/drv_i2c_mpu6050.c` |

### HIL Test

**`test/hil_test.c.j2`** — Hardware-in-Loop test firmware that flashes to the target board and reports results via UART. Only generated when `hil_mode` is active.

---

## VSCode Integration Templates

All `.vscode/` templates generate IDE configuration for:
- **IntelliSense:** Include paths for HAL, CMSIS, FreeRTOS, and generated source
- **Build tasks:** `make` integration
- **Debug launch:** OpenOCD + Cortex-Debug for DAP-Link
- **Recommended extensions:** C/C++, Cortex-Debug, arm assembly syntax

---

## Business Logic DSL Reference

Defined via `behavior` in YAML. Generates `statemachine.c/h`.

### Flat State Machine

```yaml
behavior:
  initial_state: "IDLE"
  states:
    - name: "IDLE"
      transitions:
        - event: "BUTTON_PRESS"
          target: "ACTIVE"
          actions:
            - "toggle_led"
            - "start_timer delay_timer 5000"
    - name: "ACTIVE"
      on_entry:
        - "defer 100 => publish COUNTER_TICK"
      transitions:
        - event: "TIMER_EXPIRED_delay_timer"
          target: "IDLE"
          actions:
            - "toggle_led"
```

### Substate (Composite) State Machine

```yaml
behavior:
  initial_state: "IDLE"
  states:
    - name: "IDLE"
      transitions:
        - event: "BUTTON_PRESS"
          target: "PLAYING"
          actions:
            - "toggle_led"
    - name: "PLAYING"
      initial_state: "step1"          # nested initial state
      states:
        - name: "step1"
          on_entry:
            - "defer 1000 => publish STEP_DONE"
          transitions:
            - event: "STEP_DONE"
              target: "step2"
        - name: "step2"
          transitions:
            - event: "STEP_DONE"
              target: "step1"          # loop within substate
            - event: "RETURN_NOW"
              actions:
                - "return"              # pop substate, go to parent's exit transition
      transitions:                      # parent-level transitions (on exit from substate via return)
        - event: ""
          target: "IDLE"                # implicit: any return → IDLE
          actions:
            - "toggle_led"
```

### Parallel Regions

```yaml
behavior:
  initial_state: "ACTIVE"
  regions:
    - name: "led_region"
      initial_state: "LED_OFF"
      states:
        - name: "LED_OFF"
          transitions:
            - event: "BUTTON_PRESS"
              target: "LED_ON"
              actions:
                - "toggle_led"
        - name: "LED_ON"
          transitions:
            - event: "BUTTON_PRESS"
              target: "LED_OFF"
              actions:
                - "toggle_led"
    - name: "counter_region"
      initial_state: "COUNTING"
      variables:
        count: "uint32_t"
      states:
        - name: "COUNTING"
          transitions:
            - event: "BUTTON_PRESS"
              target: "COUNTING"        # self-loop
              actions:
                - "set count inc"
```

### Ref (Reusable Substate)

```yaml
behavior:
  initial_state: "MAIN"
  states:
    - name: "MAIN"
      transitions:
        - event: "BUTTON_PRESS"
          target: "SUB"
    - name: "SUB"
      ref: "common_subflow"             # load from external YAML file
```

### Timeline

```yaml
behavior:
  initial_state: "IDLE"
  states:
    - name: "IDLE"
      on_entry:
        - "timeline led_sequence"
      transitions:
        - event: "BUTTON_PRESS"
          target: "TIMELINE_RUNNING"
      timelines:
        - name: "led_sequence"
          steps:
            - delay_ms: 500
              actions:
                - "toggle_led"
            - delay_ms: 300
              actions:
                - "toggle_led"
            - delay_ms: 200
              actions:
                - "publish SEQUENCE_DONE"
```

### Supported Actions

| Action | Syntax | Description |
|--------|--------|-------------|
| `toggle_led` | `- "toggle_led"` | Toggle LED via `led_task_notify()` |
| `start_timer` | `- "start_timer <name> <period_ms>"` | Start one-shot software timer |
| `stop_timer` | `- "stop_timer <name>"` | Stop a running software timer |
| `defer` | `- "defer <ms> => <action>"` | Delay an action by N milliseconds |
| `publish` | `- "publish <EVENT>"` | Publish an event to the event queue |
| `set` | `- "set <var> inc"` / `- "set <var> <value>"` | Modify a region variable |
| `send_to` | `- "send_to <region> <EVENT>"` | Send event to another parallel region |
| `return` | `- "return"` | Exit current substate, return to parent |
| `timeline` | `- "timeline <name>"` | Start a timeline sequence |

### Available Events

- All events defined in `event_mgr.h` (e.g., `EVENT_BUTTON_PRESS`, `EVENT_RTC_TICK`)
- Dynamically generated: `EVENT_TIMER_EXPIRED_<name>` (from `start_timer`)
- Dynamically generated: `EVENT_<name>` for `defer` callback completion
- User-defined events from `publish` action

---

## Adding a New Template

1. Create the `.j2` file in the appropriate `templates/` subdirectory.
2. Add a Jinja2 comment at the top describing the output file, required context variables, and purpose.
3. Register the template in `generator/generate.py` → `render_templates()`:
   - Standard templates: add to `standard_templates` dict
   - Driver templates: ensure the peripheral model YAML has `driver_template` / `header_template` fields
   - Test templates: add to `test_templates` dict with condition check
4. If the template needs new context variables, add them in `generator/context_builder.py`.
5. Add mock support in `templates/test/mock_hal.c.j2` / `mock_hal.h.j2` for unit tests.

## Macros and Variable Expansion

- Complex calculations should live in `macros.j2` or `context_builder.py`, keeping templates clean.
- When template logic becomes complex, move preprocessing to Python (e.g., building `handlers` dicts) instead of writing complex Jinja2 code.

## Debugging Tips

- Print the `context` dict in `generate.py` to inspect the actual data passed to templates.
- Jinja2 syntax errors include tracebacks pointing to the template file and line number.
- Compilation errors in generated code usually indicate missing context variables or incorrect macro output.
- Run `python output/<project>/test/run_tests.py` to verify unit tests pass after template changes.
