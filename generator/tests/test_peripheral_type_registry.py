"""
外设类型家族注册护栏（generator/peripheral_types.py）。

这个测试存在的原因
------------------
外设的 `type` 字符串曾经被硬编码在 8 个地方，而**漏掉任何一处都不会报错**：

  · templates/src/main.c.j2      少一个分支 ⇒ 外设初始化语句静默消失
  · templates/test/*.j2          少一个分支 ⇒ 该外设的测试根本不生成
  · allocators/pin_allocator.py  少一条     ⇒ 自动分配不申请引脚，冲突检查失效
  · schemas/hardware.py          少一个名字 ⇒ 在 schema 层被拒（有响声，尚可）
  · builders/spi_builder.py      没登记     ⇒ builder 返回 None，计算结果缺失
  · validator.py                少一处     ⇒ `bus` 必填检查静默失效
  · models/<type>.yaml           缺文件     ⇒ 生成期拿不到几何/命令码

所以家族集合在 peripheral_types.py 定义一次，其余各处**从它派生**；
本文件是那次重构的护栏：把每个消费者都查一遍，并做变异验证 ——
往家族里加一个假型号，每个消费者都必须报出它。

注意每个 check_* 都接受 types 参数（默认取真源），这样变异用例能把
一个"多了一个成员"的集合喂进去。如果护栏读的是模块级常量而不是入参，
变异验证就只能测出"测试自己在动"，测不出护栏是否真的连着数据源。
"""

from __future__ import annotations

import glob
import os
import re

import pytest

from generator.allocators.pin_allocator import _PERIPHERAL_SIGNALS
from generator.builders.registry import get_builder
from generator.paths import MODELS_DIR, TEMPLATES_DIR
from generator.peripheral_types import (
    PERIPHERAL_FAMILIES,
    SPI_DEVICE_SIGNALS,
    SPI_DEVICE_TYPES,
    SPI_FLASH_TYPES,
    SPI_SENSOR_MPU6500,
    family_flags,
    is_spi_device,
    is_spi_flash,
)
from generator.schemas.hardware import VALID_PERIPHERAL_TYPES


# ---------------------------------------------------------------------------
# 每个消费者一个 check_*（接受 types 以便变异注入）
# ---------------------------------------------------------------------------

def check_models_exist(types) -> list[str]:
    """每个家族成员都要有 models/<type>.yaml —— 几何与命令码的唯一来源。"""
    return [
        t for t in sorted(types)
        if not os.path.exists(os.path.join(MODELS_DIR, f"{t}.yaml"))
    ]


def check_schema_whitelist(types) -> list[str]:
    """schema 白名单必须收下家族成员，否则 hardware.yaml 直接校验失败。"""
    return [t for t in sorted(types) if t not in VALID_PERIPHERAL_TYPES]


def check_builder_registered(types) -> list[str]:
    """每个成员都要有 builder，且该 builder 必须**认领**这个类型。

    `context/builder.py` 对 `get_builder()` 返回 None 是静默容忍的（该外设
    就不拿到任何计算字段，模板走兜底值）。所以"没登记"和"登记了但
    `types()` 不含它"都必须被拦住 —— 后者同样会让计算结果静默缺失。
    """
    problems: list[str] = []
    for t in sorted(types):
        cls = get_builder({"type": t})
        if cls is None:
            problems.append(t)
            continue
        claimed = getattr(cls, "types", None)
        if claimed is None or t not in claimed():
            problems.append(t)
    return problems


def check_pin_signals(types) -> list[str]:
    """每个成员都要申请四线 SPI 引脚；漏了会让引脚冲突检查失效。"""
    return [
        t for t in sorted(types)
        if list(_PERIPHERAL_SIGNALS.get(t, [])) != list(SPI_DEVICE_SIGNALS)
    ]


# 模板里出现这些字面量就说明"第 8 处"又回来了：模板应当消费上下文集算出的
# `is_spi_flash` 标志（见 context/peripheral_context.py），而不是自己比较
# 类型字符串 —— 模板里少一个分支只会静默地不生成代码。
_FORBIDDEN_TEMPLATE_PATTERN = re.compile(r"""SPI_(?:Flash|Sensor)_\w+""")


def check_templates_are_model_driven(template_texts: dict[str, str]) -> list[str]:
    offenders: list[str] = []
    for path, text in sorted(template_texts.items()):
        for lineno, line in enumerate(text.splitlines(), start=1):
            found = _FORBIDDEN_TEMPLATE_PATTERN.search(line)
            if found:
                offenders.append(
                    f"{os.path.relpath(path, TEMPLATES_DIR)}:{lineno}: "
                    f"{found.group(0)} — 模板不应比较外设类型字符串"
                )
    return offenders


def _relative_templates(rel_dir: str) -> dict[str, str]:
    root = os.path.join(TEMPLATES_DIR, rel_dir)
    out: dict[str, str] = {}
    for path in glob.glob(os.path.join(root, "**", "*.j2"), recursive=True):
        with open(path, "r", encoding="utf-8") as fh:
            out[path] = fh.read()
    return out


# ---------------------------------------------------------------------------
# 真源自身
# ---------------------------------------------------------------------------

def test_flash_family_membership():
    assert SPI_FLASH_TYPES == {
        "SPI_Flash_W25Q32",
        "SPI_Flash_Generic",
        "SPI_Flash_P25Q64H",
    }
    # SPI 传感器不是 flash —— 这个区分决定了 main.c 走哪条初始化分支
    assert SPI_SENSOR_MPU6500 not in SPI_FLASH_TYPES
    assert SPI_DEVICE_TYPES == SPI_FLASH_TYPES | {SPI_SENSOR_MPU6500}


def test_predicates_agree_with_the_sets():
    for t in SPI_DEVICE_TYPES:
        assert is_spi_device(t)
        assert is_spi_flash(t) == (t in SPI_FLASH_TYPES)
    assert not is_spi_flash("I2C_EEPROM")
    assert not is_spi_device("I2C_EEPROM")


# ---------------------------------------------------------------------------
# 家族 → 模板标志
# ---------------------------------------------------------------------------

def test_every_family_has_a_non_empty_member_set():
    for name, members in PERIPHERAL_FAMILIES.items():
        assert members, f"家族 '{name}' 是空的 —— 它永远不会被命中"
        assert name and not name.startswith("is_"), (
            f"家族名 '{name}' 不应带 is_ 前缀；标志名由 family_flags() 加前缀，"
            f"带前缀会得到 is_is_xxx。"
        )


def test_family_flags_always_expose_every_key():
    """不属于任何家族的普通外设也要拿到全部键（且为 False）。

    否则模板里 `p.is_spi_flash` 会抛 UndefinedError 而不是走 else 分支 ——
    症状是生成期崩溃或（开了 Undefined 容错时）静默走错分支。
    """
    flags = family_flags("I2C_EEPROM")
    assert set(flags) == {f"is_{n}" for n in PERIPHERAL_FAMILIES}
    assert not any(flags.values())


@pytest.mark.parametrize("name", sorted(PERIPHERAL_FAMILIES))
def test_family_flags_select_exactly_their_members(name):
    key = f"is_{name}"
    for t in PERIPHERAL_FAMILIES[name]:
        assert family_flags(t)[key] is True, f"{t} 应当命中家族 '{name}'"
    for t in VALID_PERIPHERAL_TYPES - PERIPHERAL_FAMILIES[name]:
        assert family_flags(t)[key] is False, (
            f"{t} 不该命中家族 '{name}' —— 标志过宽会让模板对无关外设生成代码"
        )


def test_spi_device_family_is_the_union_of_its_parts():
    """`spi_sensor` 与 `spi_flash` 必须拼出整个 SPI 器件集合。

    这是"模板标志"与"引脚/白名单"两条线之间的连接点：引脚需求按
    SPI_DEVICE_TYPES 展开，模板标志按 PERIPHERAL_FAMILIES 算 ——
    两者一旦不一致，就会出现"申请了引脚但没有初始化代码"的外设。
    """
    from_flags = set()
    for name in ("spi_flash", "spi_sensor"):
        from_flags |= set(PERIPHERAL_FAMILIES[name])
    assert from_flags == SPI_DEVICE_TYPES


# ---------------------------------------------------------------------------
# 最终防线：每个 SPI 器件都必须真的拿到算好的分频
# ---------------------------------------------------------------------------
# 上面那些护栏查的是"登记了没有"，这一条查的是"登记了**并且**结果真的
# 落到上下文里"。两者不是一回事：`context/builder.py` 对 builder 抛异常
# 是 catch 住的（只打日志），所以一个会抛错的 builder 照样"已登记"。
# 这正是 SPI_Sensor_MPU6500 之前的处境 —— 它在白名单里、有引脚表、
# 有模板，但没有任何 builder，`extra.spi_speed_hz` 写了完全不生效，
# 模板静默走 `SPI_BAUDRATEPRESCALER_8` 兜底（比请求值快 4 倍）。

@pytest.mark.parametrize("ptype", sorted(SPI_DEVICE_TYPES))
def test_every_spi_device_gets_a_computed_prescaler(ptype):
    from generator.context.builder import build_context

    hw = {
        "mcu": {"part": "STM32G0B1RET6", "clock_freq_hz": 16_000_000},
        "pins": [
            {"id": "PA5", "function": "SPI1_SCK"},
            {"id": "PA6", "function": "SPI1_MISO"},
            {"id": "PA7", "function": "SPI1_MOSI"},
            {"id": "PC4", "function": "GPIO_Output", "label": "SPI1_NSS"},
        ],
        "peripherals": [{
            "name": "dev",
            "type": ptype,
            "bus": "SPI1",
            "cs_pin": "PC4",
            # 500 kHz @ 16 MHz ⇒ 分频 32。若走模板兜底值会是 8（2 MHz）——
            # 请求值被放大 4 倍，而这在室温下"能用"。
            "extra": {"cs_pin": "PC4", "spi_speed_hz": 500_000},
        }],
    }
    ctx = build_context(hw, "spi_dev_probe")
    peri = ctx["peripherals"][0]
    # 注意 `spi` 是 pydantic 的 SPIConfig（属性访问），不是 dict。
    spi_cfg = peri.get("spi")
    assert spi_cfg is not None, (
        f"{ptype} 没有拿到算好的 SPI 配置（spi 缺失）—— 说明它的 builder "
        f"没生效，模板会静默用兜底的 2 MHz 而不是请求的 500 kHz。"
    )
    assert spi_cfg.prescaler == 32, (
        f"{ptype} 的 SPI 分频不是 32（实际 {spi_cfg.prescaler}）—— "
        f"请求 500 kHz @ 16 MHz 被算错了。"
    )
    assert peri.get("spi_actual_hz") == 500_000
    assert peri.get("spi_prescaler_code") == 0x20


# ---------------------------------------------------------------------------
# 各消费者与真源一致（参数化，失败信息直接点名缺了哪个类型）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,check",
    [
        ("models/<type>.yaml 存在", check_models_exist),
        ("在 schema 白名单里", check_schema_whitelist),
        ("已登记 builder", check_builder_registered),
        ("已登记引脚信号", check_pin_signals),
    ],
)
def test_every_family_member_is_registered_everywhere(name, check):
    missing = check(SPI_DEVICE_TYPES)
    assert missing == [], f"这些 SPI 器件没有「{name}」：{missing}"


def test_templates_do_not_compare_peripheral_type_strings():
    offenders = check_templates_are_model_driven(_relative_templates("src"))
    offenders += check_templates_are_model_driven(_relative_templates("test"))
    assert offenders == [], (
        "模板里又出现了外设类型字面量；请改为消费上下文里的 is_spi_flash 标志：\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# 变异验证：往家族里塞一个假型号，每个护栏都必须报出它
# ---------------------------------------------------------------------------

_FAKE = "SPI_Flash_Fake"


@pytest.mark.parametrize(
    "name,check",
    [
        ("models/<type>.yaml 存在", check_models_exist),
        ("在 schema 白名单里", check_schema_whitelist),
        ("已登记 builder", check_builder_registered),
        ("已登记引脚信号", check_pin_signals),
    ],
)
def test_guards_catch_a_newly_added_type(name, check):
    """把假型号并入集合，护栏必须点名它。

    这一条防的是"护栏恒真"：如果 check_* 忽略了入参、或消费者其实是从
    另一份副本读的，这里就抓不到假型号，测试会红。
    """
    extended = SPI_DEVICE_TYPES | {_FAKE}
    assert check(extended) == [_FAKE], (
        f"往 SPI 家族加了 {_FAKE} 之后，「{name}」这条护栏没有报出它 —— "
        f"说明这条护栏没连着真源。"
    )
    # 而未扩集合仍然是干净的（证明上面那次的命中来自假型号本身）
    assert check(SPI_DEVICE_TYPES) == []


def test_template_guard_actually_detects_a_type_literal(tmp_path):
    """模板护栏的变异验证：给它一段含类型字面量的文本，必须抓到。"""
    fake_tpl = tmp_path / "fake.j2"
    fake_tpl.write_text(
        "{% if p.type in ('SPI_Flash_W25Q32', 'SPI_Flash_Generic') %}\n",
        encoding="utf-8",
    )
    offenders = check_templates_are_model_driven({str(fake_tpl): fake_tpl.read_text(encoding="utf-8")})
    assert len(offenders) == 1 and "SPI_Flash_W25Q32" in offenders[0]
