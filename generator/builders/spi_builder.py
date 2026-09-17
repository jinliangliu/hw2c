"""
spi_builder.py - SPI peripheral builder.

Pre-calculates SPI baudrate prescaler based on peripheral clock and
requested speed, replacing hardcoded SPI_BAUDRATEPRESCALER_8 in templates.

一个 builder 覆盖整个 SPI NOR Flash 家族（型号集合见
`generator/peripheral_types.py`）。**型号差异一律走模型文件**
（`models/SPI_Flash_*.yaml` 里的几何、命令码、JEDEC ID），
builder 只负责 SPI 时序计算 —— 这样新增一颗 Flash 不需要动代码。
"""

from __future__ import annotations

import logging
from typing import Any

from ..peripheral_types import (
    SPI_FLASH_GENERIC,
    SPI_FLASH_P25Q64H,
    SPI_FLASH_TYPES,
    SPI_FLASH_W25Q32,
    SPI_SENSOR_MPU6500,
)
from ..schemas.hardware import SPIConfig

from .base import PeripheralBuilder
from .registry import register_builder

logger = logging.getLogger("hw2c.spi")

# SPI peripheral clock on STM32G0B1 = PCLK1 = HCLK = 16 MHz (HSI default)
_SPI_CLK_HZ_DEFAULT = 16_000_000

# SPI baudrate prescaler enum values -> divisor mapping
_PRESCALER_MAP: dict[int, int] = {
    2: 0x00,    # SPI_BAUDRATEPRESCALER_2
    4: 0x08,    # SPI_BAUDRATEPRESCALER_4
    8: 0x10,    # SPI_BAUDRATEPRESCALER_8
    16: 0x18,   # SPI_BAUDRATEPRESCALER_16
    32: 0x20,   # SPI_BAUDRATEPRESCALER_32
    64: 0x28,   # SPI_BAUDRATEPRESCALER_64
    128: 0x30,  # SPI_BAUDRATEPRESCALER_128
    256: 0x38,  # SPI_BAUDRATEPRESCALER_256
}


class SpiBusBuilder(PeripheralBuilder):
    """SPI 总线上的器件共用的**时钟计算**。

    只做一件事：按器件模型的上限与调用方的请求速度，算出分频系数，
    输出 `spi_prescaler_code` / `spi_actual_hz` 供模板使用。

    之所以把它从 flash 里提出来单独成层：`SPI_Sensor_MPU6500` 在此之前
    **根本没有 builder**，而 `context/builder.py` 对 `get_builder()` 返回
    None 是**静默容忍**的 —— 于是传感器所在总线的分频一直走模板里的
    硬编码兜底值，`extra.spi_speed_hz` 写了也不生效，没有任何提示。
    护栏见 tests/test_peripheral_type_registry.py。
    """

    #: 调用方未声明速度时使用的目标频率。子类可覆盖。
    default_speed_hz: int = 1_000_000

    #: 模型未声明 `max_freq` 时的兜底上限。宁可慢，不可超频 —— 超频的
    #: SPI 器件在室温下常常"能用"，只在低温或批次差异时才出错。
    default_max_freq_hz: int = 1_000_000

    def identify(self, peripheral: dict) -> bool:
        """由 `extra_keys()` 声称的类型集合判定，子类用 `types()` 给集合。"""
        return peripheral.get("type", "") in self.types()

    @classmethod
    def types(cls) -> frozenset[str]:
        raise NotImplementedError

    def calculate(self, peripheral: dict, mcu: dict, context: dict) -> dict[str, Any]:
        bus = peripheral.get("bus", "SPI1")
        bus_index = int(bus[-1]) if bus[-1].isdigit() else 1

        model = peripheral.get("model") or {}
        model_max = int(model.get("max_freq") or self.default_max_freq_hz)

        # PCLK1 == HCLK on STM32G0B1（APB 无分频），所以 MCU 时钟就是 SPI 内核时钟。
        clk_hz = int(mcu.get("clock_freq_hz") or _SPI_CLK_HZ_DEFAULT)

        # 未声明 spi_speed_hz 时用类的默认值（保持历史行为，不翻新既有示例的
        # 产物）；声明了就不许超过器件上限。
        requested = int(
            (peripheral.get("extra") or {}).get("spi_speed_hz", self.default_speed_hz)
        )
        target_speed = min(requested, model_max)
        if requested > model_max:
            logger.warning(
                f"SPI {bus}: requested {requested} Hz exceeds the device limit "
                f"{model_max} Hz ({model.get('model', '?')}) - clamped"
            )

        prescaler = _calc_spi_prescaler(clk_hz, target_speed)
        prescaler_code = _PRESCALER_MAP.get(prescaler, 0x10)
        spi_cfg = SPIConfig(
            instance=bus,
            bus_index=bus_index,
            prescaler=prescaler,
            handle_name=f"hspi{bus_index}",
        )
        logger.info(
            f"SPI {bus}: prescaler={prescaler} (code=0x{prescaler_code:02X}) "
            f"for {target_speed}Hz @ {clk_hz}Hz "
            f"-> actual {clk_hz // prescaler}Hz"
        )
        return {
            "spi": spi_cfg,
            "spi_prescaler_code": prescaler_code,
            # 实际 SCK 频率，供模板/日志核对（不是"请求值"，是真实分频结果）
            "spi_actual_hz": clk_hz // prescaler,
        }


class SpiFlashBuilder(SpiBusBuilder):
    """SPI NOR Flash 家族的共用实现。子类只为注册型号而存在。"""

    @classmethod
    def types(cls) -> frozenset[str]:
        return SPI_FLASH_TYPES


# 型号与 builder 的注册是**一对多**：注册表按 hardware.yaml 的 type 字符串
# 查表（见 registry.get_builder），所以每个型号都要登记一次。每个型号之间
# 的行为差异全部在 models/<type>.yaml 里，这里不再重复列举型号。
# 漏登记的护栏见 tests/test_peripheral_type_registry.py。

@register_builder(SPI_FLASH_W25Q32)
class SpiFlashW25Q32Builder(SpiFlashBuilder):
    """W25Q32（4 MB）。"""


@register_builder(SPI_FLASH_GENERIC)
class SpiFlashGenericBuilder(SpiFlashBuilder):
    """通用 SPI NOR —— 几何与命令集完全取自模型文件。"""


@register_builder(SPI_FLASH_P25Q64H)
class SpiFlashP25Q64HBuilder(SpiFlashBuilder):
    """P25Q64H（Puya，8 MB / 4 KB 扇区 / 256 B 页）。"""


@register_builder(SPI_SENSOR_MPU6500)
class SpiSensorMpu6500Builder(SpiBusBuilder):
    """MPU-6500 IMU 的 SPI 接口。

    与 flash 家族的区别只在模型文件（没有几何概念）；时钟计算是同一条
    代码路径 —— 这也正是把它提到 `SpiBusBuilder` 的原因。
    """

    @classmethod
    def types(cls) -> frozenset[str]:
        return frozenset({SPI_SENSOR_MPU6500})


def _calc_spi_prescaler(spi_clk_hz: int, target_speed_hz: int) -> int:
    """Find the smallest prescaler divisor that keeps SCK <= target_speed.

    Returns: prescaler divisor (2, 4, 8, 16, 32, 64, 128, 256).
    """
    divisors = sorted(_PRESCALER_MAP.keys())
    for div in divisors:
        actual_speed = spi_clk_hz // div
        if actual_speed <= target_speed_hz:
            return div
    return 256  # Slowest fallback
