"""
peripheral_types.py - 外设"类型家族"的唯一真源。

为什么需要这个模块
------------------
外设的 `type` 字符串是 hardware.yaml 与 models/<type>.yaml 之间的键。
它曾经被硬编码在 8 个地方：schema 白名单、引脚信号表、builder 注册、
上下文检测、校验器的必填字段检查，以及两个模板的比较表达式。

每加一个器件型号都必须同时改完这 8 处，而**漏掉任何一处都不会报错**：

  · 模板里少一个分支  ⇒ 初始化语句静默消失（生成成功、编译通过、外设没起来）
  · 引脚表里少一条    ⇒ 自动分配时该外设不申请引脚，冲突检查跟着失效
  · 校验器里少一处    ⇒ `bus` 字段的必填检查静默失效

所以家族集合在这里定义一次，Python 侧从这里 import；模板侧不再比较类型
字符串，改为消费上下文集算出来的 `is_spi_flash` 标志（见
`context/peripheral_context.py`）。护栏见
`tests/test_peripheral_type_registry.py`。
"""

from __future__ import annotations

# =========================================================================
# SPI NOR Flash
# =========================================================================
# 字符串必须与 models/<type>.yaml 的文件名（去掉扩展名）逐字一致。
SPI_FLASH_W25Q32 = "SPI_Flash_W25Q32"
SPI_FLASH_GENERIC = "SPI_Flash_Generic"
SPI_FLASH_P25Q64H = "SPI_Flash_P25Q64H"

SPI_FLASH_TYPES: frozenset[str] = frozenset({
    SPI_FLASH_W25Q32,
    SPI_FLASH_GENERIC,
    SPI_FLASH_P25Q64H,
})

# =========================================================================
# 其他 SPI 器件
# =========================================================================
SPI_SENSOR_MPU6500 = "SPI_Sensor_MPU6500"
I2C_SENSOR_MPU6050 = "I2C_Sensor_MPU6050"

#: 所有挂在 SPI 总线上、需要 SCK/MISO/MOSI/NSS 的器件。
#: （NSS 由 GPIO 手动控制，不由 SPI 外设驱动。）
SPI_DEVICE_TYPES: frozenset[str] = SPI_FLASH_TYPES | {SPI_SENSOR_MPU6500}

#: SPI 器件的信号需求。四线 SPI 是固定四根，与型号无关。
SPI_DEVICE_SIGNALS: list[str] = ["SCK", "MISO", "MOSI", "NSS"]

#: MPU6050 家族的 IMU（同一颗器件的两种接口 —— 驱动与测试是共用的）。
#: 这个家族的价值在于它**跨接口**：按接口分类的写法会漏掉其中一个。
IMU_MPU_TYPES: frozenset[str] = frozenset({
    I2C_SENSOR_MPU6050,
    SPI_SENSOR_MPU6500,
})


# =========================================================================
# 家族 → 模板标志
# =========================================================================
#: 家族名 → 成员集合。
#:
#: **模板消费的布尔标志名 = "is_" + 家族名**，由 `family_flags()` 一次算齐。
#: 之所以是布尔标志而不是让模板比较类型字符串：Jinja 里
#: `{% if p.type in ('SPI_Flash_W25Q32', ...) %}` 少写一个型号**不会报错**，
#: 只会静默地不生成那段初始化代码。加了家族只改这一处即可。
PERIPHERAL_FAMILIES: dict[str, frozenset[str]] = {
    "spi_flash": SPI_FLASH_TYPES,
    "spi_sensor": frozenset({SPI_SENSOR_MPU6500}),
    "mpu6050": IMU_MPU_TYPES,
}


def family_flags(peripheral_type: str) -> dict[str, bool]:
    """算出该类型属于哪些家族，键为模板里用的 `is_<家族名>`。

    对**所有**外设都返回全部键（不属于则为 False），这样模板里
    `p.is_spi_flash` 不会因为键不存在而抛 UndefinedError。
    """
    return {
        f"is_{name}": peripheral_type in members
        for name, members in PERIPHERAL_FAMILIES.items()
    }


def is_spi_flash(peripheral_type: str) -> bool:
    """判断 hardware.yaml 里的 `type` 是否属于 SPI NOR Flash 家族。"""
    return peripheral_type in SPI_FLASH_TYPES


def is_spi_device(peripheral_type: str) -> bool:
    """判断是否是需要四线 SPI 信号的器件。"""
    return peripheral_type in SPI_DEVICE_TYPES
