from __future__ import annotations

from typing import TypedDict, NotRequired, List, Dict


class PinConfig(TypedDict):
    """
    TypedDict for individual pin entries from YAML.
    """
    id: str
    function: str
    label: NotRequired[str]
    pull: NotRequired[str]
    af: NotRequired[int]
    notify_task: NotRequired[str]
    exti: NotRequired[dict]


class PeripheralConfig(TypedDict):
    """
    TypedDict for individual peripheral entries from YAML.
    """
    name: str
    type: str
    model: NotRequired[dict]
    bus: NotRequired[str]
    uart: NotRequired[str]
    bearer: NotRequired[str]
    broker: NotRequired[str]
    extra: NotRequired[dict]


class McuConfig(TypedDict):
    """
    MCU configuration section.
    """
    part: str
    core: NotRequired[str]
    core_clock_mhz: int
    hse_freq: int


class BootConfig(TypedDict):
    """
    Bootloader configuration section.
    """
    enabled: bool
    size_kb: int
    app_a_offset: int
    app_b_offset: int
    crc_method: str
    boot_flag_src: str
    max_retries: int
    wdg_timeout_ms: int


class HilConfig(TypedDict):
    """
    HIL (Hardware-In-Loop) configuration section.
    """
    baudrate: int
    uart: str
    tx_pin: str
    rx_pin: str


class DriverInfo(TypedDict):
    """
    Driver information entry used for template rendering.
    """
    name: str
    template: str
    header_template: str
    model: dict
    peripheral: dict


class ValidationError(TypedDict):
    """
    Structured validation error returned by validate_hardware().
    """
    severity: str  # "CRITICAL", "ERROR", "WARNING", "INFO"
    message: str


class BuildContext(TypedDict):
    """
    Complete context dictionary returned by build_context().
    Used for Jinja2 template rendering.
    """
    project_name: str
    mcu: McuConfig
    pins: list[PinConfig]
    sleep: dict
    app_tasks: list[dict]
    hal_sources: list[str]
    peripherals: list[PeripheralConfig]
    drivers: list[DriverInfo]
    has_i2c: bool
    has_rtc: bool
    has_pwm: bool
    has_spi: bool
    has_spi_flash: bool
    has_mpu6050: bool
    has_adc: bool
    has_uart: bool
    has_rs485: bool
    has_ir: bool
    has_cellular: bool
    has_modbus: bool
    has_mqtt: bool
    has_cli: bool
    has_led: bool
    has_led_task: bool
    has_behavior: bool
    has_substate: bool
    has_bootloader: bool
    has_fota: bool
    has_event_mgr: bool
    hil_mode: bool
    uart_name: str
    rs485_name: str
    modbus_name: str
    cli_uart_name: str
    behavior: dict
    periodic_events: list[dict]
    boot_config: dict
    hil: HilConfig
    boot_max_retries: int
    hil_tests: list[dict]
    heap_size: str
    stack_size: str
    static_dir_absolute: str
    defer_actions: list[dict]
    defer_timer_names: list[str]
    published_events: list[str]
    events: list[dict]
