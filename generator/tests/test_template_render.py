"""Snapshot tests for Jinja2 template rendering."""

import re

from jinja2 import Environment, FileSystemLoader
from generator.paths import TEMPLATES_DIR
from generator.jinja_filters import register_filters


def _make_env():
    """Create a Jinja2 environment pointing to the templates directory."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR, encoding='utf-8'),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    register_filters(env)
    return env


def _minimal_context():
    """Return a minimal but valid template context for main.c.j2."""
    return {
        "project_name": "test_proj",
        "project_version": "1.0.0",
        "project_version_packed": 0x010000,
        "mcu": {
            "part": "STM32G0B1RET6",
            "clock_source": "HSI",
            "clock_freq_hz": 16000000,
            "core_clock_mhz": 64,
            "hse_freq": 8000000,
        },
        "pins": [
            {
                "id": "PA5",
                "function": "GPIO_Output",
                "label": "LED",
                "exti": {},
                "notify_task": "",
                "af": 0,
            }
        ],
        "sleep": {},
        "app_tasks": [],
        "hal_sources": [],
        "peripherals": [],
        "drivers": [],
        "has_i2c": False,
        "has_rtc": False,
        "has_pwm": False,
        "has_spi": False,
        "has_spi_flash": False,
        "has_mpu6050": False,
        "has_adc": False,
        "has_uart": False,
        "has_rs485": False,
        "has_ir": False,
        "has_cellular": False,
        "has_modbus": False,
        "has_mqtt": False,
        "has_cli": False,
        "has_led": True,
        "has_led_task": False,
        "has_behavior": False,
        "has_substate": False,
        "has_bootloader": False,
        "has_fota": False,
        "has_event_mgr": True,
        "hil_mode": False,
        "uart_name": "",
        "rs485_name": "",
        "modbus_name": "",
        "cli_uart_name": "",
        "behavior": {},
        "boot_config": {},
        "hil": {"baudrate": 115200, "uart": "UART2", "tx_pin": "PA2", "rx_pin": "PA3"},
        "boot_max_retries": 3,
        "hil_tests": [{"name": "test_dummy", "body": "TEST_PASS();"}],
        "heap_size": "0x200",
        "stack_size": "0x400",
        "static_dir_absolute": "/fake/static",
        "defer_actions": [],
        "defer_timer_names": [],
        "timer_events": [],
        "published_events": [],
    }


def test_main_c_template_basic():
    """Verify main.c renders with basic context containing expected strings."""
    env = _make_env()
    template = env.get_template("src/main.c.j2")
    context = _minimal_context()
    rendered = template.render(context)

    # Core includes always present
    assert '#include "stm32g0xx_hal.h"' in rendered
    assert '#include "FreeRTOS.h"' in rendered
    assert '#include "event_mgr.h"' in rendered

    # GPIO init is always called
    assert "MX_GPIO_Init" in rendered
    assert "void MX_GPIO_Init(void);" in rendered

    # Standard HAL flow
    assert "HAL_Init()" in rendered
    assert "SystemClock_Config()" in rendered

    # LED pin definition
    assert "LED_GPIO_Port" in rendered
    assert "LED_GPIO_Pin" in rendered

    # Event manager task
    assert "EventMgr_Task" in rendered

    # FreeRTOS scheduler
    assert "vTaskStartScheduler" in rendered


def test_main_c_template_with_rtc():
    """Main.c renders RTC-related code when has_rtc=True."""
    env = _make_env()
    template = env.get_template("src/main.c.j2")
    context = _minimal_context()
    context["has_rtc"] = True
    context["peripherals"] = [
        {
            "name": "rtc1",
            "type": "Internal_RTC",
            "model": {
                "model": "STM32G0_RTC",
                "type": "Internal_RTC",
                "interface": "internal",
            },
        }
    ]
    rendered = template.render(context)

    assert "RTC_Init()" in rendered
    assert "RTC_Start()" in rendered


def test_main_c_template_with_bootloader():
    """Main.c renders bootloader-related code when has_bootloader=True.

    A bootloader-enabled application declares IWDG, but the watchdog is only
    armed when a designated refresh owner exists (the component step task or
    the rtc_demo task) — declaring IWDG without one would only guarantee a
    reset loop, so it must stay disarmed.
    """
    env = _make_env()
    template = env.get_template("src/main.c.j2")
    context = _minimal_context()
    context["has_bootloader"] = True
    context["has_iwdg"] = True
    context["has_components"] = True
    context["boot_config"] = {
        "enabled": True,
        "size_kb": 8,
        "app_a_offset": 0x2000,
        "app_b_offset": 0x40000,
        "wdg_timeout_ms": 5000,
    }
    rendered = template.render(context)

    assert '#include "boot_app.h"' in rendered
    assert "IWDG_Init()" in rendered
    assert "boot_app_mark_ok()" in rendered

    no_owner = _minimal_context()
    no_owner["has_bootloader"] = True
    no_owner["has_iwdg"] = True
    no_owner["has_components"] = False
    assert "IWDG_Init()" not in template.render(no_owner)


def test_main_c_keeps_rtos_object_creation_after_tick_dependent_init():
    """Pin the pre-scheduler ordering required by the vendored FreeRTOS port.

    portable/GCC/ARM_CM0/port.c initialises ulCriticalNesting to the poison
    value 0xAAAAAAAA, so the first pre-scheduler taskEXIT_CRITICAL() leaves
    PRIMASK set and every HAL_GetTick()-based wait that follows spins forever.
    hw2c does not patch the vendored FreeRTOS sources, so main.c must keep:

        EventMgr_Init() -> __enable_irq() -> (tick-dependent init)
                        -> [cli_init / telemetry_init / xTaskCreate]
                        -> vTaskStartScheduler()
    """
    env = _make_env()
    template = env.get_template("src/main.c.j2")
    context = _minimal_context()
    context.update({
        "has_components": True,
        "has_cli": True,
        "cli_uart_name": "usart2",
        "has_log": True,
        "has_telemetry": True,
        "has_iwdg": True,
    })
    rendered = template.render(context)
    # Comments mention the very calls under test, so match code only.
    code = re.sub(r"/\*.*?\*/", " ", rendered, flags=re.S)
    code = re.sub(r"//[^\n]*", " ", code)

    def pos(needle, start=0):
        idx = code.find(needle, start)
        assert idx >= 0, f"{needle!r} missing from rendered main.c"
        return idx

    event_mgr = pos("EventMgr_Init();")
    repair = pos("__enable_irq();", event_mgr)   # the repair, not the early one
    comp_init = pos("component_init_all();")
    iwdg = pos("IWDG_Init();")
    cli = pos("cli_init(")
    telemetry = pos("telemetry_init();")
    scheduler = pos("vTaskStartScheduler();")

    # the repair follows the only early RTOS object creation
    assert event_mgr < repair

    # tick-dependent init runs with interrupts enabled, i.e. before cli_init()
    assert repair < comp_init < cli
    assert repair < iwdg < cli

    # every RTOS object creation is confined to the tail block
    assert cli < scheduler
    assert telemetry < scheduler
    for needle in ("xSemaphoreCreate", "xQueueCreate", "telemetry_init();",
                   "create_task_checked("):
        assert needle not in code[repair:cli], (
            f"{needle} must not run between the interrupt repair and the "
            "tail block — it would mask interrupts for the waits in between"
        )


def test_main_c_template_with_behavior():
    """Main.c renders statemachine when has_behavior=True."""
    env = _make_env()
    template = env.get_template("src/main.c.j2")
    context = _minimal_context()
    context["has_behavior"] = True
    context["behavior"] = {"states": [{"name": "idle", "initial_state": "idle"}]}
    rendered = template.render(context)

    assert '#include "statemachine.h"' in rendered
    assert "statemachine_init()" in rendered


def test_main_c_template_with_led_task():
    """Main.c renders led_task task when has_led_task=True."""
    env = _make_env()
    template = env.get_template("src/main.c.j2")
    context = _minimal_context()
    context["has_led_task"] = True
    context["app_tasks"] = [
        {
            "name": "led_task",
            "priority": 5,
            "stack_size": 128,
        }
    ]
    rendered = template.render(context)

    assert "led_task_handle" in rendered
    assert "void led_task(void *pvParameters)" in rendered
    # LED handling moved to the component framework; the task body is now
    # the generic periodic loop rendered for every app task.
    assert "vTaskDelay(1000)" in rendered


def test_main_c_template_with_cli():
    """Main.c renders CLI code when has_cli=True and cli driver is present."""
    env = _make_env()
    template = env.get_template("src/main.c.j2")
    context = _minimal_context()
    context["has_cli"] = True
    context["has_uart"] = True
    context["cli_uart_name"] = "uart2"
    context["drivers"] = [
        {
            "name": "cli",
            "template": "drivers/drv_cli.c.j2",
            "header_template": "drivers/drv_cli.h.j2",
            "model": {},
            "peripheral": {"name": "cli", "type": "Internal_CLI"},
        }
    ]
    rendered = template.render(context)

    assert '#include "drv_cli.h"' in rendered
    assert "cli_init" in rendered
    assert "cli_task" in rendered


def test_main_c_no_led_no_bootloader_no_flow():
    """Minimal main.c without LED label should not have LED definitions."""
    env = _make_env()
    template = env.get_template("src/main.c.j2")
    context = _minimal_context()
    context["has_led"] = False
    context["pins"] = [
        {
            "id": "PA0",
            "function": "GPIO_Input",
            "label": "BTN",
            "exti": {},
            "notify_task": "",
            "af": 0,
        }
    ]
    rendered = template.render(context)

    # No LED macro should be generated
    assert "LED_GPIO_Port" not in rendered
    assert "LED_GPIO_Pin" not in rendered
    # Core includes still present
    assert '#include "stm32g0xx_hal.h"' in rendered


def test_macros_template_available():
    """macros.j2 can be imported by templates."""
    env = _make_env()
    # Just verify the template environment can load macros.j2
    template = env.get_template("src/main.c.j2")
    # If we get here without jinja2.TemplateNotFound, macros.j2 was found
    assert template is not None


def test_template_environment_has_macros():
    """Verify the main.c template uses macros from macros.j2."""
    env = _make_env()
    template = env.get_template("src/main.c.j2")
    context = _minimal_context()

    # Render with a pin that exercises pin_port and pin_number macros
    context["pins"] = [
        {
            "id": "PC13",
            "function": "GPIO_Output",
            "label": "LED",
            "exti": {},
            "notify_task": "",
            "af": 0,
        }
    ]

    rendered = template.render(context)
    # PC13 → port C, pin 13
    assert "GPIOC" in rendered
    assert "GPIO_PIN_13" in rendered


def test_pid_ctrl_template_renders_thermo_dual_config():
    """pid_ctrl component must render for a temperature + dual actuator
    (heat/cool) configuration, proving the middleware is domain-agnostic."""
    env = _make_env()
    template = env.get_template("app/pid_ctrl_component.c.j2")
    context = _minimal_context()
    context.update({
        "has_components": True,
        "has_pid_ctrl": True,
        "comp_config": {
            "description": "thermo PID",
            "feedback": {"source": "temp", "unit": "degC",
                         "topic": "temperature"},
            "actuator": {"mode": "pwm_dual",
                         "heat_pwm": "heater", "heat_channel": 1,
                         "cool_pwm": "cooler", "cool_channel": 2},
            "control": {"period_ms": 100, "hysteresis": 0.5,
                        "target_min": -40},
        },
        "components": [{
            "name": "pid_ctrl",
            "type": "pid_ctrl",
            "config": {
                "description": "thermo PID",
                "feedback": {"source": "temp", "unit": "degC",
                             "topic": "temperature"},
                "actuator": {"mode": "pwm_dual",
                             "heat_pwm": "heater", "heat_channel": 1,
                             "cool_pwm": "cooler", "cool_channel": 2},
                "control": {"period_ms": 100, "hysteresis": 0.5,
                            "target_min": -40},
            },
        }],
        "peripherals": [
            {"name": "temp", "type": "I2C_TempSensor", "bus": "I2C1",
             "address": 0x48,
             "extra": {"scale_per_lsb": 0.01, "offset": 0.0,
                       "unit": "degC", "data_width": 16}},
            {"name": "heater", "type": "Internal_PWM", "timer": "TIM2",
             "extra": {"default_freq": 10}},
            {"name": "cooler", "type": "Internal_PWM", "timer": "TIM3",
             "extra": {"default_freq": 10}},
        ],
        "params": [
            {"name": "pid_kp", "type": "float", "default": 5.0},
            {"name": "pid_ki", "type": "float", "default": 0.2},
            {"name": "pid_kd", "type": "float", "default": 0.0},
            {"name": "pid_target", "type": "float", "default": 25.0},
            {"name": "pid_max_value", "type": "float", "default": 120.0},
            {"name": "pid_duty_min_pct", "type": "uint32", "default": 0},
            {"name": "pid_duty_max_pct", "type": "uint32", "default": 100},
            {"name": "pid_ramp_timeout_ms", "type": "uint32",
             "default": 60000},
        ],
        "events": [
            {"name": "TARGET_REACHED", "source": "custom",
             "type": "asynchronous",
             "payload": {"type": "float", "unit": "degC"}},
            {"name": "FAULT_TRIPPED", "source": "custom",
             "type": "asynchronous", "payload": {"type": "uint32"}},
        ],
        "topics": [{"name": "temperature",
                    "value": {"type": "int32", "unit": "0.1 degC"}}],
    })
    rendered = template.render(context)

    # generic feedback adapter bound to the temperature sensor
    assert "pid_feedback_read" in rendered
    # Bus handles are opened with the lower-cased bus name: the POSIX bus
    # registry stores lower-case names, so generating "I2C1" here used to
    # fail on target only (see the 2026-09-15 review, P0-1).
    assert "temp_read(i2c_open(\"i2c1\", NULL)" in rendered
    assert "s.process_value" in rendered
    # dual actuator: both heat and cool channels driven
    assert "heater_set_duty((uint8_t)1, ctx->heat_duty)" in rendered
    assert "cooler_set_duty((uint8_t)2, ctx->cool_duty)" in rendered
    # domain-agnostic typed accessors and events
    assert "param_pid_target_get()" in rendered
    assert "event_post_target_reached" in rendered
    assert "bus_publish_temperature" in rendered
    # no pressure-domain leftovers
    assert "pressure_kpa" not in rendered
    assert "PID_STAGE_VENT" not in rendered


def test_pid_ctrl_template_renders_ntc_single_config():
    """pid_ctrl with NTC (ADC) feedback + single PWM heater must render the
    NTC read adapter and keep the pressure/I2C specifics out."""
    env = _make_env()
    template = env.get_template("app/pid_ctrl_component.c.j2")
    context = _minimal_context()
    context.update({
        "has_components": True,
        "has_pid_ctrl": True,
        "has_adc": True,
        "comp_config": {
            "description": "ntc thermo PID",
            "feedback": {"source": "ntc_temp", "unit": "degC",
                         "topic": "temperature"},
            "actuator": {"mode": "pwm_single", "pwm": "heater",
                         "channel": 1},
            "control": {"period_ms": 100, "target_min": -40},
        },
        "components": [{
            "name": "pid_ctrl", "type": "pid_ctrl", "config": {},
        }],
        "peripherals": [
            {"name": "ntc_temp", "type": "NTC_TempSensor",
             "extra": {"adc": "adc1", "channel": 1,
                       "r_fixed_ohm": 100000.0, "r0_ohm": 100000.0,
                       "t0_c": 25.0, "b_value": 3950.0,
                       "vref_mv": 3300.0, "ntc_high": True}},
            {"name": "adc1", "type": "Internal_ADC",
             "extra": {"resolution": "12bit"}},
            {"name": "heater", "type": "Internal_PWM", "timer": "TIM2",
             "extra": {"default_freq": 10}},
        ],
        "params": [
            {"name": "pid_kp", "type": "float", "default": 5.0},
            {"name": "pid_ki", "type": "float", "default": 0.2},
            {"name": "pid_kd", "type": "float", "default": 0.0},
            {"name": "pid_target", "type": "float", "default": 25.0},
            {"name": "pid_max_value", "type": "float", "default": 120.0},
            {"name": "pid_duty_min_pct", "type": "uint32", "default": 0},
            {"name": "pid_duty_max_pct", "type": "uint32", "default": 100},
            {"name": "pid_ramp_timeout_ms", "type": "uint32",
             "default": 60000},
        ],
        "events": [
            {"name": "TARGET_REACHED", "source": "custom",
             "type": "asynchronous",
             "payload": {"type": "float", "unit": "degC"}},
            {"name": "FAULT_TRIPPED", "source": "custom",
             "type": "asynchronous", "payload": {"type": "uint32"}},
        ],
        "topics": [{"name": "temperature",
                    "value": {"type": "int32", "unit": "0.1 degC"}}],
    })
    rendered = template.render(context)

    assert "ntc_temp_read(&s)" in rendered
    assert "s.temp_c" in rendered
    assert "heater_set_duty" in rendered
    assert "param_pid_target_get()" in rendered
    assert "event_post_target_reached" in rendered
    assert "pressure_kpa" not in rendered
    assert "i2c_open" not in rendered


def test_rtc_isr_acknowledges_every_flag_before_scheduler():
    """The pre-scheduler RTC path must clear ALL flags, not a subset.

    Regression guard for a boot deadlock found on target: the millisecond
    one-shot timers arm Alarm B while RTC_Init() runs, Alarm B then fires
    before vTaskStartScheduler(), and the early-out path used to clear only
    WUTF and ALRAF.  The still-asserted ALRBF held the RTC interrupt line
    high, so RTC_TAMP_IRQHandler was re-entered the instant it returned and
    main() never reached the scheduler (measured: SR = MISR = 0x02, SCR = 0,
    100% of samples inside the handler).
    """
    env = _make_env()
    template = env.get_template("drivers/drv_rtc.c.j2")
    context = _minimal_context()
    context.update({
        "has_rtc": True,
        "has_log": True,
        "peripheral": {"name": "rtc", "type": "Internal_RTC"},
        "rtc_async_prediv": 127,
        "rtc_sync_prediv": 32767,
        "rtc_alarms": [{"period_s": 1, "period_ms": 1000, "event": "TICK_1S"}],
        "rtc_init_time": {"year": 26, "month": 7, "day": 29,
                          "hour": 22, "min": 7, "sec": 0},
    })
    rendered = template.render(context)

    # Every flag is acknowledged through the single helper ...
    assert "static void rtc_clear_all_flags(void)" in rendered

    # ... whose mask covers the alarm B flag that used to be missed.
    mask_def = re.search(r"#define RTC_CLEAR_ALL_FLAGS_MASK\s*\((.*?)\)\s*\n",
                         rendered, re.S)
    assert mask_def, "RTC_CLEAR_ALL_FLAGS_MASK not found"
    mask = mask_def.group(1)
    for bit in ("RTC_SCR_CALRAF", "RTC_SCR_CALRBF", "RTC_SCR_CWUTF",
                "RTC_SCR_CTSF", "RTC_SCR_CTSOVF", "RTC_SCR_CITSF"):
        assert bit in mask, f"{bit} missing from the clear-all mask"

    # The pre-scheduler early-out must not fall back to a subset clear.
    # Scope the search to the interrupt handler: rtc_timer_post_event() has
    # the same guard at the top of the file and would match first.
    isr_start = rendered.index("void RTC_TAMP_IRQHandler(void)\n{")
    isr = rendered[isr_start:]
    early = re.search(
        r"if \(xTaskGetSchedulerState\(\) == taskSCHEDULER_NOT_STARTED\)"
        r"\s*\{(.*?)\n    \}", isr, re.S)
    assert early, "pre-scheduler early-out branch not found in the ISR"
    body = early.group(1)
    assert "rtc_clear_all_flags();" in body
    assert "__HAL_RTC_WAKEUPTIMER_CLEAR_FLAG" not in body
    assert "__HAL_RTC_ALARM_CLEAR_FLAG" not in body


# ---------------------------------------------------------------------------
# CLI RX dispatch: line editor vs. binary handover
# ---------------------------------------------------------------------------

def _strip_c_comments(code):
    """Drop /* ... */ and // ... so structural searches cannot match text
    inside a comment (the comments here explain the very code being searched
    for, so an un-stripped search would pass on a comment alone)."""
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"//[^\n]*", "", code)


def _body_of(code, signature):
    """Extract a complete function body by brace balancing. Comments must
    already be stripped."""
    i = code.index(signature)
    j = code.index("{", i)
    depth = 0
    for k in range(j, len(code)):
        if code[k] == "{":
            depth += 1
        elif code[k] == "}":
            depth -= 1
            if depth == 0:
                return code[i:k + 1]
    raise AssertionError("unbalanced braces: %s" % signature)


_SINK_READ = "cli_rx_sink_t sink = cli_rx_sink;"
_LOOP_HEAD = "while (i < len)"
_HANDOVER = "sink(&buf[i]"
_EDITOR = "hw2c_cli_input("


def _rx_dispatch_layout(code):
    """Return the byte offsets of the four landmarks in cli_dispatch_rx.

    A pure function of the source text, so a deliberately regressed copy can
    be fed in to prove the check actually fires — no compiler needed.
    """
    body = _body_of(code, "static void cli_dispatch_rx(")
    return {
        "loop": body.find(_LOOP_HEAD),
        "read": body.find(_SINK_READ),
        "handover": body.find(_HANDOVER),
        "editor": body.find(_EDITOR),
    }


def _render_cli_driver(version=None, packed=None):
    """Render drivers/drv_cli.c.j2 with a UART + CLI + bootloader setup."""
    from generator.context.builder import build_context

    hardware = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [
            {"id": "PA2", "function": "USART2_TX", "af": 1},
            {"id": "PA3", "function": "USART2_RX", "af": 1},
            {"id": "PC0", "function": "GPIO_Output", "label": "LED",
             "active_level": "low"},
        ],
        "peripherals": [
            {"name": "usart2", "type": "UART_Serial", "instance": "USART2",
             "interface": "uart", "extra": {"baudrate": 115200}},
            {"name": "cli", "type": "Internal_CLI", "uart": "usart2",
             "extra": {"prompt": "cli> "}},
        ],
        "bootloader": {"enabled": True},
    }
    context = build_context(hardware, "cli-dispatch-guard")
    context["peripheral"] = {"name": "cli", "uart_name": "usart2"}
    context["model"] = {"type": "Internal_CLI"}
    if version is not None:
        context["project_version"] = version
    if packed is not None:
        context["project_version_packed"] = packed
    return _make_env().get_template("drivers/drv_cli.c.j2").render(context)


def test_cli_rx_dispatch_rechecks_the_handover_sink_per_byte():
    """A single RX batch may contain BOTH the command that installs the
    handover sink AND the first bytes meant for it.

    `cli_task` drains the ring buffer in 64-byte batches.  If it only sampled
    `cli_rx_sink` once per batch, the batch holding `fota ymodem\\r` followed
    by YMODEM block 0 would run the command, install the sink, and then feed
    the remaining bytes of the SAME batch to the line editor anyway.  The
    command succeeds and the sink is live, so `fota status` looks clean
    (state IDLE, last error 0, not even a filename) while the device answers
    nothing at all — the only visible symptom is a stray
    `Unknown command: <binary garbage>`.

    A human operator cannot hit this (hundreds of ms between the command and
    the transfer); a script does every time.  Measured on target; the capture
    is in docs/reviews/onboard-capture-2026-09-17.txt.
    """
    code = _strip_c_comments(_render_cli_driver())
    layout = _rx_dispatch_layout(code)

    assert layout["loop"] >= 0, "cli_dispatch_rx lost its per-byte loop"
    assert layout["read"] >= 0, (
        "cli_dispatch_rx no longer reads cli_rx_sink at all"
    )
    assert layout["handover"] >= 0, (
        "cli_dispatch_rx no longer hands the remaining bytes to the sink"
    )
    assert layout["editor"] >= 0, (
        "cli_dispatch_rx no longer feeds the line editor"
    )

    # The read must sit inside the loop: after the `while` head and before
    # the editor call.  Hoisting it above the loop is exactly the regression.
    assert layout["loop"] < layout["read"], (
        "the sink read was hoisted out of the per-byte loop — a batch holding "
        "both the `fota ymodem` command and YMODEM block 0 will be swallowed "
        "by the line editor again"
    )
    assert layout["read"] < layout["editor"], (
        "the sink is read after the line editor call, so it can never take over"
    )

    # cli_task itself must no longer touch the editor directly.
    task = _body_of(code, "void cli_task(")
    assert "cli_dispatch_rx(" in task, "cli_task stopped using the dispatcher"
    assert "hw2c_cli_input(" not in task, (
        "cli_task feeds the line editor directly again — the dispatch went "
        "back to sampling the sink once per batch"
    )

    # Mutation check: hoist the read above the loop and confirm the check fires.
    mutated = code.replace(
        "    while (i < len) {\n        " + _SINK_READ,
        "    " + _SINK_READ + "\n    while (i < len) {", 1)
    assert mutated != code, "mutation anchor not found"
    assert _rx_dispatch_layout(mutated)["read"] < _rx_dispatch_layout(mutated)["loop"], (
        "hoisting the sink read out of the loop was not detected — this check "
        "is dead"
    )


# ---------------------------------------------------------------------------
# Project version: task.yaml -> banner / `version` command
# ---------------------------------------------------------------------------

def test_the_configured_project_version_reaches_the_firmware():
    """`task.yaml: project.version` must end up in the firmware verbatim.

    Both places used to hardcode it: the boot banner printed `v1.0` for every
    project, and `cmd_version()` had a `{% if fw_version is defined %}` branch
    that **nothing ever satisfied** (no such context key existed), so it always
    fell through to the literal `1.0.0`.

    The symptom is not an error — it is that a differential OTA looks like it
    did nothing, because the device reports the same version before and after
    a successful upgrade.  There is no other user-visible proof of "which
    firmware is running", so this is the acceptance criterion of the whole
    feature and it has to be pinned.

    The check is a differential one on purpose: it renders the same template
    twice with two different versions and requires the output to move.  A test
    that only asserted "1.0.0 appears" would pass on the hardcoded version.
    """
    def render(version, packed):
        # has_log gates the banner block in main.c.j2; without it the whole
        # boot banner (logo + project line) is not emitted at all.
        banner = _make_env().get_template("src/main.c.j2").render(
            {**_minimal_context(),
             "has_log": True,
             "project_version": version,
             "project_version_packed": packed})
        cli = _render_cli_driver(version=version, packed=packed)
        return banner, cli

    banner_a, cli_a = render("1.0.0", 0x010000)
    banner_b, cli_b = render("2.3.4", 0x020304)

    # The banner must carry the configured version, not the old literal.
    assert "v1.0.0 — Hardware2Code" in banner_a
    assert "v2.3.4 — Hardware2Code" in banner_b
    assert "v1.0 — Hardware2Code" not in banner_b, (
        "the banner still prints the hardcoded v1.0"
    )

    # `version` must be derived, and must actually differ between the two.
    assert cli_a != cli_b, (
        "cmd_version() renders identically for two different project "
        "versions — the version is hardcoded again"
    )
    assert "Firmware version: 1.0.0" not in cli_b, (
        "the hardcoded '1.0.0' literal is back in cmd_version()"
    )
    assert "0x020304" in cli_b, (
        "the packed project version never reaches the C source"
    )


def test_project_version_is_parsed_strictly():
    """Bad version strings must fail at generation time, not silently degrade.

    A wrong version is indistinguishable from "OTA did not work", so the
    generator refuses rather than guessing.  Short forms are padded.
    """
    from generator.context.builder import _parse_project_version

    assert _parse_project_version(None) == ("1.0.0", 0x010000)
    assert _parse_project_version("") == ("1.0.0", 0x010000)
    assert _parse_project_version("2") == ("2.0.0", 0x020000)
    assert _parse_project_version("2.1") == ("2.1.0", 0x020100)
    assert _parse_project_version("2.1.3") == ("2.1.3", 0x020103)
    # The YAML may hand us an int for a bare `version: 2`.
    assert _parse_project_version(2) == ("2.0.0", 0x020000)

    for bad in ("v1.0.1", "1.0.1-rc1", "1.2.3.4", "1.256.0", "1..0", "1.x"):
        try:
            _parse_project_version(bad)
        except ValueError:
            continue
        raise AssertionError(
            "project.version %r was accepted; a malformed version must be "
            "rejected at generation time" % (bad,))


def test_project_version_survives_the_three_layer_merge():
    """`mapper.merge()` -> `HardwareModel` must not drop the version.

    The merge step already carried `project.name` and silently dropped
    `project.version`; the pydantic model on top of it accepts extra keys, so
    either link could lose it again without anything failing.
    """
    from generator.mapper import merge
    from generator.schemas.hardware import HardwareModel

    hardware = """
mcu:
  part: STM32G0B1RET6
pins:
  - id: PC0
    function: GPIO_Output
    label: LED
"""
    task = """
project:
  name: version_probe
  version: "3.2.1"
"""

    merged = merge(hardware, task, "")
    assert merged.get("project_version") == "3.2.1", (
        "mapper.merge() dropped project.version — the firmware will report "
        "the hardcoded default no matter what task.yaml says"
    )

    dumped = HardwareModel.model_validate(merged).model_dump(exclude_none=True)
    assert dumped.get("project_version") == "3.2.1", (
        "HardwareModel dropped project_version on the way to build_context()"
    )


if __name__ == "__main__":
    test_main_c_template_basic()
    test_main_c_template_with_rtc()
    test_main_c_template_with_bootloader()
    test_main_c_template_with_behavior()
    test_main_c_template_with_led_task()
    test_main_c_template_with_cli()
    test_main_c_no_led_no_bootloader_no_flow()
    test_macros_template_available()
    test_template_environment_has_macros()
    test_rtc_isr_acknowledges_every_flag_before_scheduler()
    print("All template_render tests passed.")
