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


def _render_rtc_driver(initial_time):
    """Render drivers/drv_rtc.c.j2 for an RTC configured with `initial_time`."""
    from generator.context.builder import build_context

    hardware = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
        "peripherals": [{
            "name": "rtc1", "type": "Internal_RTC",
            "extra": {"clock_source": "LSE", "wakeup_interval_ms": 1000,
                      "initial_time": initial_time},
        }],
    }
    context = build_context(hardware, "rtc-calendar-guard")
    context["peripheral"] = {"name": "rtc1", "_clock_source": "LSE"}
    context["model"] = {"type": "Internal_RTC"}
    return _make_env().get_template("drivers/drv_rtc.c.j2").render(context)


def test_rtc_calendar_call_carries_the_configured_initial_time():
    """The parsed calendar must reach the real HAL calls as numbers.

    Parsing correctly and rendering correctly are two links of one chain, and
    each is easy to break without moving the other: a template that reads the
    wrong key (or keeps a hard-coded default) leaves every parser test green
    while the device still boots on the wrong date.  So assert on the arguments
    `HAL_RTC_SetTime/SetDate` actually receive, and assert `sDate.Date` is a
    day-of-month (1..31) -- a day/month/year swap used to put 2026 there.
    """
    import re

    code = _render_rtc_driver("2026-09-17 10:00:00")

    def _arg(name):
        m = re.search(r"%s\s*=\s*(\d+)\s*;" % re.escape(name), code)
        assert m is not None, (
            "the rendered RTC init no longer assigns %s — the value parsed "
            "from initial_time did not reach HAL_RTC_SetTime/SetDate" % (name,))
        return int(m.group(1))

    assert _arg("sTime.Hours") == 10
    assert _arg("sTime.Minutes") == 0
    assert _arg("sTime.Seconds") == 0
    # The RTC stores a 2-digit year, i.e. an offset from 2000.
    assert _arg("sDate.Year") == 26
    assert _arg("sDate.Month") == 9
    assert _arg("sDate.Date") == 17, (
        "sDate.Date is not the day of month; the calendar fields are being "
        "written into the wrong RTC struct members")


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


# ---------------------------------------------------------------------------
# Image header fw_version: derived from project.version, never a constant
# ---------------------------------------------------------------------------

_CMAKE_BLOCK_START = "set(HW2C_FW_VERSION_DERIVED"
_CMAKE_BLOCK_END = 'message(STATUS "hw2c: firmware version (image header fw_version)'


def _render_cmake_lists(version, packed, has_bootloader=True):
    """Render project/CMakeLists.txt.j2 for a project, with the version forced.

    `packed=None` simulates the context losing `project_version_packed` — the
    template is contractually required to refuse that, not to generate a broken
    project.
    """
    from generator.context.builder import build_context

    hardware = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [
            {"id": "PA2", "function": "USART2_TX", "af": 1},
            {"id": "PA3", "function": "USART2_RX", "af": 1},
        ],
        "peripherals": [
            {"name": "usart2", "type": "UART_Serial", "instance": "USART2",
             "interface": "uart", "extra": {"baudrate": 115200}},
        ],
        "bootloader": {"enabled": has_bootloader},
    }
    context = build_context(hardware, "cmake-version-guard")
    context["project_version"] = version
    if packed is None:
        context.pop("project_version_packed", None)
    else:
        context["project_version_packed"] = packed
    return _make_env().get_template("project/CMakeLists.txt.j2").render(context)


def _version_resolution_block(cmake_text):
    """Cut the fw_version resolution logic out of the rendered CMakeLists.

    That slice is plain script-mode CMake — no project, no targets, no
    toolchain — so `cmake -P` can execute it.  Running the real thing beats
    pattern-matching the source: 'the default is derived' is a claim about the
    decision CMake makes, including which of the three sources wins.
    """
    start = cmake_text.index(_CMAKE_BLOCK_START)
    end = cmake_text.index(_CMAKE_BLOCK_END, start)
    end = cmake_text.index("\n", end) + 1
    return cmake_text[start:end]


def _resolve_fw_version(cmake_text, tmp_path, name="probe", env=None,
                        defines=()):
    """Run the extracted block under `cmake -P`; return (value, source)."""
    import os
    import shutil
    import subprocess

    cmake = None
    for cand in (os.environ.get("H2C_CMAKE_PATH"), shutil.which("cmake"),
                 "C:/mingw64/bin/cmake.exe"):
        if cand and shutil.which(cand):
            cmake = cand
            break
    if cmake is None:
        import pytest
        pytest.skip("cmake not available")

    script = tmp_path / ("fw_version_%s.cmake" % name)
    script.write_text(
        _version_resolution_block(cmake_text)
        + '\nmessage(STATUS "H2C_RESOLVED=${FW_VERSION}|'
          '${HW2C_FW_VERSION_SOURCE}")\n',
        encoding="utf-8", newline="\n")

    run_env = dict(os.environ)
    # A developer machine with FOTA_VERSION exported would otherwise win over
    # the case under test and make this probe answer the wrong question.
    run_env.pop("FOTA_VERSION", None)
    if env:
        run_env.update(env)

    proc = subprocess.run([cmake, *defines, "-P", str(script)],
                          capture_output=True, text=True, env=run_env,
                          cwd=str(tmp_path))
    assert proc.returncode == 0, (
        "cmake -P rejected the rendered fw_version block:\n%s\n%s"
        % (proc.stdout, proc.stderr))
    for line in proc.stdout.splitlines():
        if "H2C_RESOLVED=" in line:
            value, _, source = line.split("H2C_RESOLVED=", 1)[1].partition("|")
            return value.strip(), source.strip()
    raise AssertionError("probe produced no decision:\n%s" % proc.stdout)


def test_image_header_version_is_derived_from_project_version(tmp_path):
    """FR-14.7: what the bootloader compares must be derived, not a constant.

    This is the other half of
    `test_the_configured_project_version_reaches_the_firmware`: that one pins
    what the firmware **prints**, this one pins what the image header
    **carries**.  They used to disagree — CMake wrote the constant `1` into
    every image header while the banner printed `1.0.0` for every project — and
    the disagreement is invisible until you try to tell two firmware versions
    apart, which is exactly what "did the OTA work?" rests on.

    Assertions are differential on purpose: a single-version assertion passes on
    a hardcoded value.
    """
    cmake_a = _render_cmake_lists("1.0.0", 0x010000)
    cmake_b = _render_cmake_lists("1.0.1", 0x010001)

    # 1. Default (no override) = packed project.version, and it moves when the
    #    configured version moves.
    value_a, source_a = _resolve_fw_version(cmake_a, tmp_path, name="a")
    value_b, source_b = _resolve_fw_version(cmake_b, tmp_path, name="b")
    assert value_b == str(0x010001), (
        "the image header version is not derived from project.version "
        "(got %r, want 65537)" % (value_b,))
    assert value_a != value_b, (
        "two different project versions produced the same image header "
        "version — the default is a constant again")
    assert "1.0.1" in source_b, (
        "the chosen source is not reported, so an override that silently won "
        "cannot be told apart from the derived default")

    # 2. It must actually reach patch_crc.py, otherwise the header stays 0.
    assert "--version ${FW_VERSION}" in cmake_b, (
        "patch_crc.py no longer receives the resolved version")

    # 3. ...and it must equal what the firmware reports, or the device cannot
    #    prove which image it is running (the E_CRC(-19) failure mode).
    assert "0x010001" in _render_cli_driver(version="1.0.1", packed=0x010001), (
        "the header version and the version printed by cmd_version() come from "
        "different numbers")

    # 4. The overrides survive, and the loser of the precedence is visible.
    assert _resolve_fw_version(cmake_b, tmp_path, name="cli",
                               defines=("-DFW_VERSION=9",))[0] == "9"
    assert _resolve_fw_version(cmake_b, tmp_path, name="env",
                               env={"FOTA_VERSION": "42"})[0] == "42"

    # 5. The legacy constant 1 must be treated as stale, not as "the operator
    #    asked for it" — otherwise an old build directory keeps writing 1 into
    #    the header forever.
    assert _resolve_fw_version(cmake_b, tmp_path, name="legacy",
                               defines=("-DFW_VERSION=1",))[0] == str(0x010001)

    # 6. The default must not be written to the CMake cache.  A cached default
    #    goes sticky: reconfigure after bumping project.version and the header
    #    keeps the old number while the banner prints the new one.
    #    Comments are stripped first: the block *documents* the old
    #    `set(FW_VERSION "1" CACHE STRING …)` line, and matching that would make
    #    this check pass/fail on prose instead of on executed CMake.
    executable = "\n".join(
        line for line in cmake_b.splitlines()
        if not line.lstrip().startswith("#"))
    assert not re.search(r"set\(FW_VERSION[^\n]*CACHE", executable), (
        "the fw_version default is written to the CMake cache again — it will "
        "go sticky and the image header will disagree with the firmware banner")

    # Mutation check: restore the exact regression (constant 1) and confirm the
    # derived-value assertion fires.
    mutated = cmake_b.replace("set(HW2C_FW_VERSION_DERIVED 65537)",
                              "set(HW2C_FW_VERSION_DERIVED 1)")
    assert mutated != cmake_b, "mutation anchor not found"
    value_m, _ = _resolve_fw_version(mutated, tmp_path, name="mutated")
    assert value_m == "1", (
        "the probe did not observe the injected constant — this check is dead")
    assert value_m != value_b, (
        "the mutated default reproduced the failing build and the probe still "
        "reported the derived value; the probe is not reading the block")


def test_missing_project_version_packed_is_refused_at_generation_time():
    """The derivation input must be mandatory, not optional.

    `project_version_packed` missing from the context used to be survivable: the
    generated CMakeLists would expand `--version` to nothing and `patch_crc.py`
    would fail with argparse's `argument --version: expected one argument`,
    which points nowhere near the real cause.  Refusing during generation keeps
    the failure where the information is.
    """
    cmake = _render_cmake_lists("1.0.0", None)

    assert "FATAL_ERROR" in cmake, (
        "a project without project_version_packed renders a CMakeLists that "
        "silently writes no version into the image header")
    assert "FR-14.7" in cmake, (
        "the refusal does not point at the requirement it enforces")


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
    test_rtc_calendar_call_carries_the_configured_initial_time()
    print("All template_render tests passed.")
