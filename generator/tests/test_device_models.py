"""
器件模型契约测试（generator/device_models.py）。

为什么值得单独立一个测试文件
----------------------------
模型 YAML 是"硅片事实"的唯一落点，而它**写错不会编译失败**：
`total_size` 抄成别的型号、`sector_size` 少个零、命令码两两撞车 ——
生成器照样生成、编译器照样通过，症状是"偶发丢数据"或"某片区域永远
写不进去"。所以这里把可判定的部分全部判掉，判据是**手册常数与彼此之间
的整除/幂关系**，而不是"看起来合理"。

每个用例都在做同一件事：在**一份已知自洽的模型**上只改一处，断言
校验器恰好报出预期的那条错。这样"校验通过"才有信息量 —— 否则一个
恒返回空列表的校验器能让全部用例变绿。
"""

from __future__ import annotations

import copy
import glob
import os
import re

import pytest

from generator.device_models import (
    REQUIRED_FLASH_COMMANDS,
    validate_device_model,
    validate_model_file,
)
from generator.paths import MODELS_DIR, TEMPLATES_DIR
from generator.peripheral_types import SPI_FLASH_TYPES


# ---------------------------------------------------------------------------
# 自洽的基线模型（改动点只有下面 _MUTATIONS 里列出的那些）
# ---------------------------------------------------------------------------

def _baseline() -> dict:
    return {
        "jedec_id": [0x85, 0x60, 0x17],   # 容量字节 0x17 ⇒ 8 MB
        "page_size": 256,
        "sector_size": 4096,
        "block_size_32k": 32768,
        "block_size_64k": 65536,
        "total_size": 8388608,
        "commands": {
            "READ_ID": 0x9F,
            "READ_DATA": 0x03,
            "WRITE_ENABLE": 0x06,
            "READ_STATUS": 0x05,
            "PAGE_PROGRAM": 0x02,
            "SECTOR_ERASE": 0x20,
            "CHIP_ERASE": 0xC7,
        },
        "status_bits": {"WIP": 0x01, "WEL": 0x02},
        "timing": {
            "page_program_ms": 2,
            "sector_erase_ms": 10,
            "block_erase_32k_ms": 10,
            "block_erase_64k_ms": 10,
            "chip_erase_ms": 9000,
        },
    }


# 家族判定走**文件名**，所以每个用例都给一个属于家族的文件名。
_MODEL_NAME = "SPI_Flash_P25Q64H.yaml"


def _errors_for(model: dict) -> list[str]:
    return validate_device_model(model, os.path.join(MODELS_DIR, _MODEL_NAME))


def test_the_baseline_model_is_self_consistent():
    """先证明基线是干净的 —— 否则下面每个用例都可能因为基线本身就错而假绿。"""
    assert _errors_for(_baseline()) == []


# ---------------------------------------------------------------------------
# 逐项变异：只改一处，断言恰好报出预期的那条
# ---------------------------------------------------------------------------
# 每项 = (说明, 变异函数, 期望出现在错误里的子串)
_MUTATIONS = [
    (
        "容量字节与 total_size 不一致（抄成别的型号）",
        lambda m: m.update(total_size=4194304),
        "does not match the JEDEC capacity byte",
    ),
    (
        "jedec_id 只有两字节",
        lambda m: m.update(jedec_id=[0x85, 0x60]),
        "must be three bytes",
    ),
    (
        "jedec_id 的字节越界",
        lambda m: m.update(jedec_id=[0x85, 0x60, 0x1FF]),
        "must be three bytes",
    ),
    (
        "sector_size 不是 2 的幂",
        lambda m: m.update(sector_size=3000),
        "must be a positive power of two",
    ),
    (
        # 注意这里**必须**让 page_size > sector_size：两者都被要求是 2 的幂，
        # 所以只要 page ≤ sector，"能整除"是幂关系的推论、这条检查永远不
        # 会触发。真正会踩到的笔误是把两个值写反（页比扇区还大），那会让
        # 跨页切分按超过扇区的粒度去切 —— 这时才有响声。
        "page_size 比 sector_size 还大（两个值写反了）",
        lambda m: m.update(page_size=8192, sector_size=4096),
        "almost certainly swapped",
    ),
    (
        "total_size 不是 sector_size 的整数倍",
        lambda m: m.update(total_size=8388608 + 1024),
        "must be a multiple of",
    ),
    (
        "block_size_64k 不是 sector_size 的整数倍",
        lambda m: m.update(block_size_64k=65535),
        "must be a multiple of",
    ),
    (
        "缺 page_size（跨页切分要靠它）",
        lambda m: m.pop("page_size"),
        "'page_size' is required",
    ),
    (
        "缺 total_size（NVM 布局要靠它）",
        lambda m: m.pop("total_size"),
        "'total_size' is required",
    ),
    (
        "缺 READ_STATUS（无法等器件就绪）",
        lambda m: m["commands"].pop("READ_STATUS"),
        "command 'READ_STATUS' is missing",
    ),
    (
        "缺 PAGE_PROGRAM",
        lambda m: m["commands"].pop("PAGE_PROGRAM"),
        "command 'PAGE_PROGRAM' is missing",
    ),
    (
        "两个动作共用同一个命令码",
        lambda m: m["commands"].update(SECTOR_ERASE=0x02),
        "reuses code 0x02",
    ),
    (
        "命令码不是字节",
        lambda m: m["commands"].update(CHIP_ERASE=0x1C7),
        "is not a byte",
    ),
    (
        "缺 status_bits.WIP（只能用固定延时顶替）",
        lambda m: m.pop("status_bits"),
        "status_bits.WIP is missing",
    ),
    (
        "缺 timing（超时只能塞魔数）",
        lambda m: m.pop("timing"),
        "'timing' is missing",
    ),
    (
        "timing 里某一项为零",
        lambda m: m["timing"].update(sector_erase_ms=0),
        "timing.sector_erase_ms",
    ),
]


@pytest.mark.parametrize(
    "label,mutate,expected",
    _MUTATIONS,
    ids=[m[0] for m in _MUTATIONS],
)
def test_single_field_mutation_is_rejected(label, mutate, expected):
    model = _baseline()
    mutate(model)
    errors = _errors_for(model)
    assert any(expected in e for e in errors), (
        f"模型被改成「{label}」后校验器没报出「{expected}」，实际报的是：\n"
        + ("\n".join("  " + e for e in errors) or "  (什么都没报)")
    )


# ---------------------------------------------------------------------------
# 可选性与家族边界
# ---------------------------------------------------------------------------

def test_jedec_id_is_optional_for_the_unbound_generic_model():
    """`SPI_Flash_Generic` 刻意不绑定型号，没有 ID 可声明 —— 不能因此报错。"""
    model = _baseline()
    model.pop("jedec_id")
    assert _errors_for(model) == []


def test_non_flash_models_are_not_subject_to_flash_geometry():
    """别把 flash 的几何契约套到传感器上 —— 它们没有这套概念。"""
    assert validate_device_model({"page_size": 3}, "models/Internal_RTC.yaml") == []


def test_family_membership_is_decided_by_filename_not_the_type_field():
    """`type` 字段历史上并不统一（W25Q32 写 "SPI_Flash"），文件名才是查找键。"""
    model = _baseline()
    model["type"] = "SPI_Flash"                      # 与文件名不一致
    assert _errors_for(model) == []
    # 换个不属于家族的文件名，同样的坏模型就应被放行（没有 flash 契约）
    bad = _baseline()
    bad.pop("page_size")
    assert validate_device_model(bad, os.path.join(MODELS_DIR, "I2C_EEPROM.yaml")) == []


def test_missing_model_file_is_reported_not_silently_ignored():
    errors = validate_model_file(os.path.join(MODELS_DIR, "SPI_Flash_Nope.yaml"))
    assert len(errors) == 1 and "not found" in errors[0]


# ---------------------------------------------------------------------------
# 仓库里真实存在的模型文件（这才是真正被生成器消费的东西）
# ---------------------------------------------------------------------------

def _flash_model_paths() -> list[str]:
    return sorted(
        p for p in glob.glob(os.path.join(MODELS_DIR, "SPI_Flash_*.yaml"))
    )


def test_every_flash_family_type_has_a_model_file():
    found = {
        os.path.splitext(os.path.basename(p))[0] for p in _flash_model_paths()
    }
    # Generic 也在家族里，文件名同样匹配 SPI_Flash_*，所以这里是等集而非包含
    assert found == set(SPI_FLASH_TYPES), (
        "peripheral_types.SPI_FLASH_TYPES 与 models/SPI_Flash_*.yaml 不一致："
        f"缺文件 {sorted(set(SPI_FLASH_TYPES) - found)}，多文件 "
        f"{sorted(found - set(SPI_FLASH_TYPES))}"
    )


@pytest.mark.parametrize(
    "path", _flash_model_paths(), ids=lambda p: os.path.basename(p)
)
def test_shipped_flash_models_pass_their_own_contract(path):
    errors = validate_model_file(path)
    assert errors == [], "模型文件不符合契约：\n" + "\n".join(errors)


@pytest.mark.parametrize(
    "path", _flash_model_paths(), ids=lambda p: os.path.basename(p)
)
def test_shipped_flash_models_declare_data_plus_its_capacity(path):
    """`jedec_id` 的容量字节必须与 `total_size` 互相印证。

    `SPI_Flash_Generic` 是唯一允许没有 ID 的成员 —— 它的代价写在模型
    文件开头（不核对 ID 就等于不核对容量）。
    """
    from generator.device_models import load_device_model

    model = load_device_model(path)
    if "jedec_id" not in model:
        assert "Generic" in os.path.basename(path), (
            f"{os.path.basename(path)} 既没有 jedec_id 又不是 Generic —— "
            f"驱动将无法核对型号。"
        )
        return
    assert model["total_size"] == 1 << model["jedec_id"][2]


def test_required_command_list_covers_the_whole_program_cycle():
    """这条断言钉住的是**契约本身**，不是某个模型的取值。

    写前使能 → 写 → 等就绪 → 擦 → 读回：缺任何一环，驱动都无法在
    "器件说它写完了"之外拿到任何证据。往 REQUIRED_FLASH_COMMANDS 里
    删项会让所有模型同时少一项约束，所以这里显式列出。
    """
    assert set(REQUIRED_FLASH_COMMANDS) == {
        "READ_ID",
        "READ_DATA",
        "WRITE_ENABLE",
        "READ_STATUS",
        "PAGE_PROGRAM",
        "SECTOR_ERASE",
        "CHIP_ERASE",
    }


def test_validator_does_not_mutate_the_model_it_inspects():
    """校验器是只读的 —— 顺手改掉调用者的 dict 会污染生成管线。"""
    model = _baseline()
    snapshot = copy.deepcopy(model)
    _errors_for(model)
    assert model == snapshot


# ---------------------------------------------------------------------------
# 三方一致：模型 ↔ 驱动模板 ↔ mock
# ---------------------------------------------------------------------------
# 「模型是硅片事实的唯一落点」这句话只有在**其余两处都从它派生**时才成立。
# 实际上有三份副本，各自都可能凭印象漂移：
#   1. models/SPI_Flash_*.yaml        —— 真源
#   2. templates/drivers/drv_spi_flash.h.j2  —— 生成到固件里的宏
#   3. templates/test/mock_hal.h.j2   —— 主机侧假器件的命令码
# 第 3 份最危险：它读不到 YAML，只能重复一遍。若它和模型不一致，全部 NOR
# 行为测试就都在验证一个**不存在的器件**，而且全绿。
# 这与 A9（mock 把 TAMP 开到 BKP31R，凭空造出硅片上不存在的寄存器）同族。

_MOCK_HEADER = os.path.join(TEMPLATES_DIR, "test", "mock_hal.h.j2")
_DRIVER_HEADER = os.path.join(TEMPLATES_DIR, "drivers", "drv_spi_flash.h.j2")


def _parse_mock_nor_defines() -> dict[str, int]:
    """从 mock 头文件里抽出 MOCK_NOR_* 的取值（十六进制或十进制都接受）。"""
    with open(_MOCK_HEADER, "r", encoding="utf-8") as fh:
        text = fh.read()
    out: dict[str, int] = {}
    for m in re.finditer(
        r"^#define\s+(MOCK_NOR_[A-Z0-9_]+)\s+(0[xX][0-9A-Fa-f]+|\d+)[uUlL]*\s*$",
        text,
        re.MULTILINE,
    ):
        raw = m.group(2)
        # int(x, 0) 会按前缀自动选进制；纯十进制也走这条路。
        out[m.group(1)] = int(raw, 16) if raw.lower().startswith("0x") else int(raw, 10)
    return out


@pytest.mark.parametrize(
    "path", _flash_model_paths(), ids=lambda p: os.path.basename(p)
)
def test_mock_command_codes_match_the_model(path):
    """mock 的 NOR 命令码必须与模型的 `commands` 段逐个相等。"""
    from generator.device_models import load_device_model

    model = load_device_model(path)
    defines = _parse_mock_nor_defines()
    assert defines, f"没能从 {_MOCK_HEADER} 解析出任何 MOCK_NOR_* 定义"

    for name, code in model["commands"].items():
        key = f"MOCK_NOR_CMD_{name}"
        if key not in defines:
            # 模型声明的命令比 mock 支持的多是允许的（如 DEEP_POWER_DOWN
            # 驱动不发、测试也不需要），但**必备命令**不能缺。
            assert name not in REQUIRED_FLASH_COMMANDS, (
                f"{os.path.basename(path)} 声明了必备命令 {name}="
                f"0x{code:02X}，但 mock 里没有 {key} —— NOR 行为测试无法覆盖它。"
            )
            continue
        assert defines[key] == code, (
            f"{key} 在 mock 里是 0x{defines[key]:02X}，而 "
            f"{os.path.basename(path)} 的 commands.{name} 是 0x{code:02X}。"
            f"mock 建的是**不存在的器件**。"
        )

    for name, mask in model["status_bits"].items():
        key = f"MOCK_NOR_SR_{name}"
        assert key in defines, f"mock 缺少状态位 {key}"
        assert defines[key] == mask, (
            f"{key} 在 mock 里是 0x{defines[key]:02X}，模型是 0x{mask:02X}"
        )


@pytest.mark.parametrize(
    "path", _flash_model_paths(), ids=lambda p: os.path.basename(p)
)
def test_driver_header_emits_every_command_the_model_declares(path):
    """模型声明的命令必须真的被生成成宏。

    头文件模板用的是一个 Jinja 名字列表来展开宏。列表里少了某个名字，
    模型就会"声明了但驱动不认" —— 而生成期没有任何响声。
    """
    from generator.device_models import load_device_model

    model = load_device_model(path)
    with open(_DRIVER_HEADER, "r", encoding="utf-8") as fh:
        text = fh.read()

    listed = set(re.findall(r"'([A-Z][A-Z0-9_]+)'", text))
    for name in model["commands"]:
        assert name in listed, (
            f"{os.path.basename(path)} 声明了命令 {name}，但 "
            f"{os.path.basename(_DRIVER_HEADER)} 的宏展开列表里没有它 —— "
            f"该命令不会被生成到固件里。"
        )


def test_mock_block_sizes_match_the_models():
    """mock 的块擦尺寸也必须与模型一致（块擦命令码同样受上面那条约束）。"""
    from generator.device_models import load_device_model

    defines = _parse_mock_nor_defines()
    for path in _flash_model_paths():
        model = load_device_model(path)
        for key, field in (
            ("MOCK_NOR_BLOCK_32K", "block_size_32k"),
            ("MOCK_NOR_BLOCK_64K", "block_size_64k"),
        ):
            assert defines[key] == model[field], (
                f"{key} 在 mock 里是 {defines[key]}，"
                f"{os.path.basename(path)} 的 {field} 是 {model[field]}"
            )


# ---------------------------------------------------------------------------
# 接线：模型契约必须真的在生成期生效，而不是只活在单元测试里
# ---------------------------------------------------------------------------
# 一个只被测试调用、没接进管线的校验器和"没有校验"是等价的 ——
# 好模型会被查，坏模型照样生成固件。所以这里走真正的入口
# `validate_hardware()`，并通过改写 MODELS_DIR 指向一份临时模型来
# 证明"模型坏了 ⇒ 生成被拒"。

_SHW_YAML = """
mcu:
  part: STM32G0B1RET6
  clock_freq_hz: 16000000
pins:
  - id: PA2
    function: USART2_TX
  - id: PA3
    function: USART2_RX
  - id: PA5
    function: SPI1_SCK
  - id: PA6
    function: SPI1_MISO
  - id: PA7
    function: SPI1_MOSI
  - id: PC4
    function: GPIO_Output
    label: SPI1_NSS
peripherals:
  - name: flash
    type: SPI_Flash_P25Q64H
    bus: SPI1
    cs_pin: "PC4"
    extra:
      cs_pin: "PC4"
"""


def _validate_with_models(tmp_path, monkeypatch, model_text: str):
    """把 MODELS_DIR 指向一份只含临时模型的目录，然后跑真入口。"""
    import yaml as _yaml

    from generator import validator as validator_mod

    (tmp_path / "SPI_Flash_P25Q64H.yaml").write_text(model_text, encoding="utf-8")
    monkeypatch.setattr(validator_mod, "MODELS_DIR", str(tmp_path))
    hw = _yaml.safe_load(_SHW_YAML)
    return [e["message"] for e in validator_mod.validate_hardware(hw)]


def test_broken_model_is_rejected_by_the_pipeline(tmp_path, monkeypatch):
    with open(
        os.path.join(MODELS_DIR, "SPI_Flash_P25Q64H.yaml"), "r", encoding="utf-8"
    ) as fh:
        good = fh.read()

    # 先把好模型接进管线，确认这条路径**不是恒报错**
    assert not [
        m for m in _validate_with_models(tmp_path, monkeypatch, good) if "JEDEC" in m
    ]

    broken = good.replace("total_size: 8388608", "total_size: 4194304")
    assert broken != good, "替换没生效 —— 模型文件改过了，请同步本测试"
    messages = _validate_with_models(tmp_path, monkeypatch, broken)
    assert any("JEDEC" in m for m in messages), (
        "模型里 total_size 与 JEDEC 容量字节对不上，生成期却没拒绝；"
        "说明 validate_hardware 没有接上器件模型校验。实际报错：\n"
        + "\n".join(messages)
    )
