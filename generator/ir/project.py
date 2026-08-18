"""
project.py - Root ProjectIR object.

The ProjectIR is the top-level IR node. It holds all sub-IR objects
and provides a legacy `to_dict()` method that produces a flat dict
compatible with the existing Jinja2 template rendering path.

Once all templates are migrated to access IR attributes directly,
the `to_dict()` method can be deprecated.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .base import IRObject
from .mcu import McuIR
from .pin import PinIR
from .peripheral import PeripheralIR, DriverIR
from .behavior import BehaviorIR
from .bootloader import BootIR
from .hil import HilIR
from .log import LogIR
from .exti import ExtiIR
from .rtc import RtcIR


@dataclass
class ProjectIR(IRObject):
    """Root IR object representing an entire firmware project."""

    # ---------- Project identity ----------
    project_name: str = "hw2code"

    # ---------- MCU ----------
    mcu: McuIR = field(default_factory=McuIR)

    # ---------- Hardware ----------
    pins: list[dict] = field(default_factory=list)
    sleep: dict = field(default_factory=dict)
    app_tasks: list[dict] = field(default_factory=list)

    # ---------- Peripherals ----------
    peripherals: list[dict] = field(default_factory=list)
    drivers: list[dict] = field(default_factory=list)
    hal_sources: list[str] = field(default_factory=list)

    # ---------- Capability flags ----------
    has_i2c: bool = False
    has_rtc: bool = False
    has_mpu6050: bool = False
    has_pwm: bool = False
    has_spi: bool = False
    has_spi_flash: bool = False
    has_adc: bool = False
    has_uart: bool = False
    has_rs485: bool = False
    has_ir: bool = False
    has_cellular: bool = False
    has_cli: bool = False
    has_temp_sensor: bool = False
    has_modbus: bool = False
    has_mqtt: bool = False
    has_led: bool = False
    has_led_task: bool = False
    has_btn_task: bool = False
    has_behavior: bool = False
    has_substate: bool = False
    has_bootloader: bool = False
    has_fota: bool = False
    has_iwdg: bool = False
    has_event_mgr: bool = True
    has_tickless: bool = False
    has_log: bool = False
    has_telemetry: bool = False
    has_power_mgr: bool = False

    # ---------- Named instances ----------
    uart_name: str = ""
    rs485_name: str = ""
    modbus_name: str = ""
    cli_uart_name: str = ""
    cli_name: str = ""

    # ---------- LED ----------
    led_active_low: bool = False
    led_task_name: str = ""

    # ---------- RTC ----------
    rtc: RtcIR = field(default_factory=RtcIR)

    # ---------- PWM ----------
    pwm_tim_prescaler: int = 15999
    pwm_tim_period: int = 999

    # ---------- Temperature sensor ----------
    temp_offset_deci: int = 0

    # ---------- USART ----------
    usart2_baudrate: int = 115200
    usart2_clock_freq_hz: int = 16000000

    # ---------- Log ----------
    log: LogIR = field(default_factory=LogIR)

    # ---------- Behavior ----------
    behavior: dict = field(default_factory=dict)
    periodic_events: list[dict] = field(default_factory=list)
    defer_actions: list[dict] = field(default_factory=list)
    defer_timer_names: list[str] = field(default_factory=list)
    user_timer_actions: list[dict] = field(default_factory=list)
    timer_events: list[str] = field(default_factory=list)
    published_events: list[str] = field(default_factory=list)
    transition_events: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    # ---------- Bootloader ----------
    boot: BootIR = field(default_factory=BootIR)

    # ---------- EXTI ----------
    exti: ExtiIR = field(default_factory=ExtiIR)

    # ---------- HIL ----------
    hil: HilIR = field(default_factory=HilIR)
    hil_mode: bool = False
    hil_tests: list[dict] = field(default_factory=list)

    # ---------- Memory ----------
    heap_size: str = "0x200"
    stack_size: str = "0x400"
    total_heap_size: int = 16384
    flash_kb: int = 512

    # ---------- Test ----------
    test_mode: bool = False

    # ---------- Filesystem ----------
    static_dir_absolute: str = ""

    def to_dict(self) -> dict:
        """Produce the legacy flat dict compatible with existing templates.

        This method explicitly maps IR fields to the exact dict keys that
        build_context() historically returned.  New code should access IR
        attributes directly; this method exists only for backward compat
        during the migration period.
        """
        d = asdict(self)

        # Flatten nested IR objects into their legacy key names
        # RTC → top-level keys
        rtc_dict = d.pop("rtc", {})
        d["has_rtc"] = rtc_dict.get("has_rtc", False)
        d["rtc_clock_source"] = rtc_dict.get("clock_source", "LSI")
        d["rtc_async_prediv"] = rtc_dict.get("async_prediv", 127) if d["has_rtc"] else None
        d["rtc_sync_prediv"] = rtc_dict.get("sync_prediv", 255) if d["has_rtc"] else None
        d["rtc_wakeup_interval_ms"] = rtc_dict.get("wakeup_interval_ms", 1000)
        d["rtc_alarms"] = rtc_dict.get("alarms", [])
        d["rtc_init_time"] = rtc_dict.get("init_time", {})

        # Log → top-level keys
        log_dict = d.pop("log", {})
        # Only override has_log if it was set via LogIR (non-default)
        if log_dict.get("enabled"):
            d["has_log"] = True
        d["log_ring_buf_size"] = log_dict.get("ring_buf_size", 1024)
        d["log_uart"] = {
            "instance": log_dict.get("uart_instance", ""),
            "irqn": log_dict.get("uart_irqn", ""),
            "rcc_usart_clk": log_dict.get("uart_rcc_clk", ""),
            "ccipr_sel_msk": log_dict.get("uart_ccipr_sel_msk", ""),
            "ccipr_hsi_src": log_dict.get("uart_ccipr_hsi_src", ""),
            "tx_port": log_dict.get("tx_port", ""),
            "tx_pin": log_dict.get("tx_pin", ""),
            "tx_af": log_dict.get("tx_af", ""),
            "rx_port": log_dict.get("rx_port", ""),
            "rx_pin": log_dict.get("rx_pin", ""),
            "rx_af": log_dict.get("rx_af", ""),
            "rcc_gpio_clk": log_dict.get("rcc_gpio_clk", ""),
        }

        # Bootloader → top-level keys
        boot_dict = d.pop("boot", {})
        d["has_bootloader"] = boot_dict.get("enabled", False)
        d["boot_config"] = boot_dict.get("raw", {})
        d["boot_max_retries"] = boot_dict.get("max_retries", 3)
        d["boot_size_bytes"] = boot_dict.get("size_bytes", 8192)
        d["boot_led_port"] = boot_dict.get("led_port", "GPIOC")
        d["boot_led_pin_num"] = boot_dict.get("led_pin_num", 0)
        d["boot_led_rcc_enable"] = boot_dict.get("led_rcc_enable", "RCC_IOPENR_GPIOCEN")
        d["iwdg_reload_value"] = boot_dict.get("iwdg_reload_value", 625) if boot_dict["enabled"] else None
        d["has_iwdg"] = boot_dict.get("enabled", False)

        # EXTI → top-level key
        exti_dict = d.pop("exti", {})
        d["exti_handler_groups"] = exti_dict.get("groups", {})

        # HIL → top-level keys
        hil_dict = d.pop("hil", {})
        d["hil"] = {
            "baudrate": hil_dict.get("baudrate", 115200),
            "uart": hil_dict.get("uart", "UART2"),
            "tx_pin": hil_dict.get("tx_pin", "PA2"),
            "rx_pin": hil_dict.get("rx_pin", "PA3"),
        }

        # Remove internal-only fields
        for key in list(d.keys()):
            if key.startswith("_"):
                del d[key]

        return d
