"""Snapshot tests for Jinja2 template rendering."""

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
    """Main.c renders bootloader-related code when has_bootloader=True."""
    env = _make_env()
    template = env.get_template("src/main.c.j2")
    context = _minimal_context()
    context["has_bootloader"] = True
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
    assert "temp_read(i2c_open(\"I2C1\", NULL)" in rendered
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
    print("All template_render tests passed.")
