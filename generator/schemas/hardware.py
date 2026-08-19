"""
hardware.py - Pydantic v2 type models for Hardware2Code YAML schema.

Comprehensive type-safe models that validate every field of the hardware
YAML before it enters the code-generation pipeline.  Replaces bare
dictionaries and provides:
  - Field-level validation (pin IDs, MCU part numbers, peripheral types)
  - Cross-field model validators (duplicate pin detection, LED-task consistency)
  - Nested sub-models for I2C, SPI, UART configuration
  - Auto-generated JSON Schema for IDE tooling

Usage:
    from schemas.hardware import HardwareModel
    hw = HardwareModel.model_validate(raw_yaml_dict)
    hw_dict = hw.model_dump(exclude_none=True)
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# =========================================================================
# 1. Enums
# =========================================================================

class PullMode(str, Enum):
    UP = "up"
    DOWN = "down"


class ExtiTrigger(str, Enum):
    RISING = "rising"
    FALLING = "falling"
    BOTH = "both"


class SleepMode(str, Enum):
    RUN = "RUN"
    SLEEP = "SLEEP"
    STOP0 = "STOP0"
    STOP1 = "STOP1"
    STOP2 = "STOP2"
    STANDBY = "STANDBY"


# =========================================================================
# 2. Pin models
# =========================================================================

_PIN_ID_RE = re.compile(r"^P[A-F][0-9]{1,2}$")


class ExtiConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    enable: bool = False
    trigger: Optional[ExtiTrigger] = None


class PinConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    function: str
    label: Optional[str] = None
    pull: Optional[PullMode] = None
    active_level: Optional[Literal["high", "low"]] = None
    af: int = 0
    notify_task: str = ""
    exti: ExtiConfig = Field(default_factory=ExtiConfig)

    @field_validator("id")
    @classmethod
    def _validate_pin_id(cls, v: str) -> str:
        if not _PIN_ID_RE.match(v):
            raise ValueError(
                f"Invalid pin ID '{v}'. Expected format like 'PA0' or 'PC13'."
            )
        return v

    @property
    def port(self) -> str:
        return f"GPIO{self.id[1]}"

    @property
    def pin_num(self) -> int:
        return int(self.id[2:])

    @property
    def hal_pin(self) -> str:
        return f"GPIO_PIN_{self.id[2:]}"


# Legacy alias for template compatibility
PinModel = PinConfig


# =========================================================================
# 3. I2C / SPI / UART sub-configs (pre-calculated)
# =========================================================================

class I2CConfig(BaseModel):
    """Pre-calculated I2C timing, computed by I2cBuilder."""
    model_config = ConfigDict(extra="allow")
    instance: str = ""
    bus_index: int = 1
    timing_r: int = 0x2000090E
    speed_hz: int = 100_000
    handle_name: str = ""

    def model_post_init(self, __context) -> None:
        if not self.handle_name:
            self.handle_name = f"hi2c{self.bus_index}"


class SPIConfig(BaseModel):
    """Pre-calculated SPI config, computed by SpiBuilder."""
    model_config = ConfigDict(extra="allow")
    instance: str = ""
    bus_index: int = 1
    prescaler: int = 8
    handle_name: str = ""

    def model_post_init(self, __context) -> None:
        if not self.handle_name:
            self.handle_name = f"hspi{self.bus_index}"


class UARTConfig(BaseModel):
    """Pre-calculated UART config, computed by UartBuilder."""
    model_config = ConfigDict(extra="allow")
    instance: str = ""
    baudrate: int = 115200
    handle_name: str = ""


# =========================================================================
# 4. MCU model
# =========================================================================

_MCU_PART_RE = re.compile(r"^STM32[A-Z0-9]+$")


class McuModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    part: str
    core: str = ""
    core_clock_mhz: int = 64
    hse_freq: int = 8_000_000

    @field_validator("part")
    @classmethod
    def _validate_part(cls, v: str) -> str:
        if not _MCU_PART_RE.match(v):
            raise ValueError(
                f"Invalid MCU part number '{v}'. "
                f"Expected format like 'STM32G0B1RET6'."
            )
        return v


# =========================================================================
# 5. Task model
# =========================================================================

class TaskModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    priority: int = Field(ge=0, le=31)
    stack_size: int = Field(default=128, gt=0)


# =========================================================================
# 6. Peripheral model
# =========================================================================

VALID_PERIPHERAL_TYPES: frozenset[str] = frozenset({
    "Internal_RTC", "Internal_PWM", "Internal_ADC", "Internal_IR",
    "Internal_CLI", "Internal_IWDG", "Internal_TempSensor",
    "UART_Serial",
    "I2C_Sensor_MPU6050", "I2C_EEPROM", "I2C_Pressure", "I2C_TempSensor",
    "NTC_TempSensor",
    "SPI_Flash_W25Q32", "SPI_Flash_Generic", "SPI_Sensor_MPU6500",
    "FOC_Motor",
    "RS485", "Cellular_4G",
    "Protocol_Modbus", "Protocol_MQTT",
})

VALID_PIN_FUNCTIONS: list[str] = [
    'GPIO_Output', 'GPIO_Input',
    'I2C_SCL', 'I2C_SDA',
    'SPI_SCK', 'SPI_MISO', 'SPI_MOSI', 'SPI_NSS',
    'UART_TX', 'UART_RX', 'USART_TX', 'USART_RX',
    'LPUART_TX', 'LPUART_RX',
    'RS485_DE', 'ADC_IN',
    'IR_OUT', 'IR_IN',
    'CELL_PWR', 'CELL_RST',
]

VALID_FUNCTION_PATTERNS: list[str] = [
    r'^I2C\d+_SCL$', r'^I2C\d+_SDA$',
    r'^SPI\d+_SCK$', r'^SPI\d+_MISO$', r'^SPI\d+_MOSI$', r'^SPI\d+_NSS$',
    r'^USART\d+_TX$', r'^USART\d+_RX$', r'^UART\d+_TX$', r'^UART\d+_RX$',
    r'^ADC_IN\d+$',
]


class PeripheralModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    type: str
    interface: Optional[str] = None
    bus: Optional[str] = None
    uart: Optional[str] = None
    bearer: Optional[str] = None
    broker: Optional[str] = None
    clock_source: Optional[str] = None
    features: list[str] = Field(default_factory=list)
    instance: Optional[str] = None
    extra: dict = Field(default_factory=dict)
    model: Optional[dict] = None
    i2c: Optional[I2CConfig] = None
    spi: Optional[SPIConfig] = None
    uart_cfg: Optional[UARTConfig] = None

    @field_validator("type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        if v not in VALID_PERIPHERAL_TYPES:
            raise ValueError(
                f"Unknown peripheral type '{v}'. "
                f"Valid types: {sorted(VALID_PERIPHERAL_TYPES)}"
            )
        return v


# =========================================================================
# 7. Config sections
# =========================================================================

class SleepModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    mode: Optional[SleepMode] = None
    tickless: Optional[bool] = None


class LogModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    enable: bool = False


class BootloaderModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    size_kb: int = Field(default=8, ge=4, le=32)
    app_a_offset: int = 0x2000
    app_b_offset: int = 0x40000
    crc_method: str = "crc32_hw"
    boot_flag_src: str = "tamp_bkp"
    max_retries: int = Field(default=3, ge=1, le=10)
    wdg_timeout_ms: int = 5000

    @model_validator(mode="after")
    def _validate_offsets(self) -> "BootloaderModel":
        if self.app_a_offset >= self.app_b_offset:
            raise ValueError(
                f"app_a_offset (0x{self.app_a_offset:X}) must be less than "
                f"app_b_offset (0x{self.app_b_offset:X})"
            )
        min_offset = self.size_kb * 1024
        if self.app_a_offset < min_offset:
            raise ValueError(
                f"app_a_offset (0x{self.app_a_offset:X}) must be >= "
                f"bootloader size ({self.size_kb}KB = 0x{min_offset:X})"
            )
        return self


class HilModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    baudrate: int = 115200
    uart: str = "UART2"
    tx_pin: str = "PA2"
    rx_pin: str = "PA3"


# =========================================================================
# 8. Business flow (state machine) models
# =========================================================================

ActionType = Union[str, dict[str, Any]]


class VariableModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    type: str
    initial: Any = None
    array: Optional[int] = None


class StructFieldModel(BaseModel):
    """A field within a struct or nested struct (max 2 levels)."""
    model_config = ConfigDict(extra="allow")
    name: str
    type: Optional[str] = None  # omit for nested struct (detected by 'fields')
    array: Optional[int] = None
    fields: Optional[list["StructFieldModel"]] = None  # nested struct (level 2 only)


class EnumValueModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    value: int = 0


class UnionFieldModel(BaseModel):
    """A member field within a union."""
    model_config = ConfigDict(extra="allow")
    name: str
    type: str
    array: Optional[int] = None


class BitfieldFieldModel(BaseModel):
    """A bitfield member."""
    model_config = ConfigDict(extra="allow")
    name: str
    width: int = Field(ge=1, le=32)


class TypeDefModel(BaseModel):
    """A custom C type definition (struct, enum, union, or bitfield)."""
    model_config = ConfigDict(extra="allow")
    name: str
    struct: Optional[list[StructFieldModel]] = None
    enum: Optional[list[EnumValueModel]] = None
    union: Optional[list[UnionFieldModel]] = None
    bitfield: Optional[list[BitfieldFieldModel]] = None


class TransitionModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    event: str
    target: Optional[str] = None
    guard: Optional[str] = None
    actions: list[ActionType] = Field(default_factory=list)


class StateModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    type: Optional[Literal["ref"]] = None
    ref: Optional[str] = None
    namespace: Optional[str] = None
    after: Optional[int] = None
    history: bool = False
    initial_state: Optional[str] = None
    on_entry: list[ActionType] = Field(default_factory=list)
    on_exit: list[ActionType] = Field(default_factory=list)
    transitions: list[TransitionModel] = Field(default_factory=list)
    states: list["StateModel"] = Field(default_factory=list)
    variables: list[VariableModel] = Field(default_factory=list)


class RegionModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    initial_state: str
    variables: list[VariableModel] = Field(default_factory=list)
    states: list[StateModel] = Field(default_factory=list)


class BehaviorModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    initial_state: Optional[str] = None
    types: list[TypeDefModel] = Field(default_factory=list)
    variables: list[VariableModel] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    states: list[StateModel] = Field(default_factory=list)
    regions: list[RegionModel] = Field(default_factory=list)


# =========================================================================
# 9. Root Hardware model
# =========================================================================

class HardwareModel(BaseModel):
    """Root model for a hardware YAML file.

    Validates all sections and cross-field constraints.
    Call .model_dump(exclude_none=True) for template rendering.
    """
    model_config = ConfigDict(extra="allow")

    mcu: McuModel
    project: Optional[dict] = None
    pins: list[PinConfig] = Field(default_factory=list)
    log: LogModel = Field(default_factory=LogModel)
    sleep: SleepModel = Field(default_factory=SleepModel)
    app_tasks: list[TaskModel] = Field(default_factory=list)
    peripherals: list[PeripheralModel] = Field(default_factory=list)
    behavior: Optional[BehaviorModel] = None
    bootloader: Optional[BootloaderModel] = None
    hil: Optional[HilModel] = None
    heap_size: str = "0x200"
    stack_size: str = "0x400"

    @field_validator("pins")
    @classmethod
    def _validate_unique_pin_ids(cls, v: list[PinConfig]) -> list[PinConfig]:
        ids = [p.id for p in v]
        duplicates = sorted({pid for pid in ids if ids.count(pid) > 1})
        if duplicates:
            raise ValueError(f"Duplicate pin IDs found: {duplicates}")
        return v

    @model_validator(mode="after")
    def _validate_led_task_consistency(self) -> "HardwareModel":
        has_led = any(p.label == "LED" for p in self.pins)
        has_led_task = any(t.name == "led_task" for t in self.app_tasks)
        if has_led_task and not has_led:
            raise ValueError(
                "'led_task' defined in app_tasks but no pin labeled 'LED' found."
            )
        return self


StateModel.model_rebuild()
