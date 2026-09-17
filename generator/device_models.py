"""
device_models.py - 器件模型文件（models/*.yaml）的自洽性校验。

为什么单独成模块
----------------
模型文件是"硅片事实"的唯一落点：几何（页/扇区/块）、命令码、JEDEC ID、
时序上限。生成器拿这些数字直接生成驱动，所以模型写错等于固件写错 ——
而症状通常不是编译错误，是"偶发丢数据"或"擦掉了不该擦的区域"。

最值得拦的一类错误是**几何与型号不一致**：比如把 P25Q64H 的
`total_size` 抄成 4 MB（W25Q32 的值），驱动照样能编译、能读写前 4 MB，
只是后半片永远用不到；或者把 `sector_size` 写成 512，驱动就会按 512 B
的粒度去擦，而器件真实粒度是 4 KB —— 擦不干净，写入累积成错误值。

所以这里把可判定的部分全部判掉，判据是**手册给出的常数与彼此之间的
整除/幂关系**，而不是"看起来合理"：

  · JEDEC 容量字节 ⇒ 2^n 字节，必须等于 `total_size`
  · `page_size` / `sector_size` 必须是 2 的幂
  · `page_size` 必须整除 `sector_size`（跨页切分依赖这条）
  · `total_size` 必须是 `sector_size` 的整数倍
  · `block_size_32k` / `block_size_64k` 必须是 `sector_size` 的整数倍
  · 命令码必须齐备且两两不同（同一个码发给两个动作是静默的功能错乱）
  · 写/擦类命令必须配套 `READ_STATUS` 与 `WIP` 位（否则无法等器件就绪）

对应测试：generator/tests/test_device_models.py（含变异验证）。
"""

from __future__ import annotations

import os
from typing import Any

import yaml

from .peripheral_types import SPI_FLASH_TYPES


#: SPI NOR flash 驱动实际会发出的命令。缺任何一个，生成的驱动都无法正确
#: 完成"写前使能 -> 写 -> 等就绪 -> 读回校验"这条最小闭环。
REQUIRED_FLASH_COMMANDS: tuple[str, ...] = (
    "READ_ID",
    "READ_DATA",
    "WRITE_ENABLE",
    "READ_STATUS",
    "PAGE_PROGRAM",
    "SECTOR_ERASE",
    "CHIP_ERASE",
)


def _is_power_of_two(n: Any) -> bool:
    return isinstance(n, int) and n > 0 and (n & (n - 1)) == 0


def validate_device_model(model: dict, model_path: str = "<model>") -> list[str]:
    """校验一个器件模型文件的自洽性，返回错误消息列表（空 = 通过）。

    只校验 SPI NOR Flash 家族 —— 其它外设模型没有这套几何契约，
    别把 flash 的规则套到传感器上。
    """
    errors: list[str] = []
    # 类型取自文件名，而不是文件里的 `type` 字段：后者历史上并不统一
    # （W25Q32.yaml 写的是 "SPI_Flash"，Generic 写的是 "SPI_Flash_Generic"），
    # 而文件名才是 hardware.yaml `type` 的查找键。
    model_type = os.path.splitext(os.path.basename(model_path))[0]
    if model_type not in SPI_FLASH_TYPES:
        return errors

    where = f"[{os.path.basename(model_path)}]"

    # ---------- JEDEC ID 与容量 ----------
    # `jedec_id` 是可选的：`SPI_Flash_Generic` 刻意不绑定具体型号，没有可
    # 声明的 ID。但**声明了就必须自洽** —— 容量字节与 total_size 对不上，
    # 说明其中一个抄错了，而两者都会被拿去生成驱动。
    jedec = model.get("jedec_id")
    if jedec is None:
        pass
    elif (
        not isinstance(jedec, list)
        or len(jedec) != 3
        or not all(isinstance(b, int) and 0 <= b <= 0xFF for b in jedec)
    ):
        errors.append(
            f"{where} jedec_id must be three bytes, e.g. [0x85, 0x60, 0x17] "
            f"(got {jedec!r}). The driver verifies the ID at init, so a wrong "
            f"value makes every board fail to start."
        )
    else:
        capacity_bytes = 1 << jedec[2]
        total_size = model.get("total_size")
        if total_size != capacity_bytes:
            errors.append(
                f"{where} total_size={total_size} does not match the JEDEC "
                f"capacity byte 0x{jedec[2]:02X} (= 2^{jedec[2]} = "
                f"{capacity_bytes} bytes). One of the two is wrong."
            )

    # ---------- 几何 ----------
    page_size = model.get("page_size")
    sector_size = model.get("sector_size")
    total_size = model.get("total_size")

    for name, value in (
        ("page_size", page_size),
        ("sector_size", sector_size),
        ("total_size", total_size),
    ):
        if value is None:
            errors.append(
                f"{where} '{name}' is required: the driver derives its "
                f"transfer splitting and the NVM region layout from it."
            )

    for name, value in (("page_size", page_size), ("sector_size", sector_size)):
        if not _is_power_of_two(value):
            errors.append(
                f"{where} {name}={value} must be a positive power of two."
            )

    if _is_power_of_two(page_size) and _is_power_of_two(sector_size):
        if sector_size % page_size != 0:
            # 两者都是 2 的幂，所以走不到这里的唯一可能就是 page > sector：
            # 这不是"不整除"，而是**两个值写反了**。按后者解释才可行动。
            errors.append(
                f"{where} page_size={page_size} is larger than "
                f"sector_size={sector_size} and does not divide it — the two "
                f"are almost certainly swapped. Page size must be the smaller "
                f"of the two: page-aware programming splits a transfer at page "
                f"boundaries."
            )

    if isinstance(total_size, int) and _is_power_of_two(sector_size):
        if total_size % sector_size != 0:
            errors.append(
                f"{where} total_size={total_size} must be a multiple of "
                f"sector_size={sector_size}."
            )

    for name in ("block_size_32k", "block_size_64k"):
        value = model.get(name)
        if value is None:
            # 主流 SPI NOR（W25Q / P25Q / GD25 全系）都有两种块擦。
            # 作为必填而不是可选，是为了让模板不必写条件分支 ——
            # 条件生成的分支是最容易"某个型号悄悄少生成一段"的地方。
            errors.append(
                f"{where} '{name}' is required (the driver exposes block "
                f"erase for both sizes; model it explicitly)."
            )
            continue
        if not _is_power_of_two(sector_size):
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"{where} {name}={value!r} must be a positive integer.")
        elif value % sector_size != 0:
            errors.append(
                f"{where} {name}={value} must be a multiple of "
                f"sector_size={sector_size}."
            )
        elif value <= sector_size:
            errors.append(
                f"{where} {name}={value} must be larger than "
                f"sector_size={sector_size}: a block erase that is not bigger "
                f"than a sector erase is a modelling mistake."
            )

    # ---------- 命令集 ----------
    commands = model.get("commands")
    if not isinstance(commands, dict):
        errors.append(f"{where} 'commands' is missing or not a mapping.")
        return errors

    for cmd in REQUIRED_FLASH_COMMANDS:
        if cmd not in commands:
            errors.append(
                f"{where} command '{cmd}' is missing. The generated driver "
                f"cannot complete a program/erase cycle without it."
            )

    seen: dict[int, str] = {}
    for name, code in commands.items():
        if not isinstance(code, int) or not 0 <= code <= 0xFF:
            errors.append(
                f"{where} command '{name}' code {code!r} is not a byte."
            )
            continue
        if code in seen:
            errors.append(
                f"{where} command '{name}' reuses code 0x{code:02X} already "
                f"assigned to '{seen[code]}' — the device would execute the "
                f"wrong operation with no error reported."
            )
        else:
            seen[code] = name

    status_bits = model.get("status_bits")
    if not isinstance(status_bits, dict) or "WIP" not in status_bits:
        errors.append(
            f"{where} status_bits.WIP is missing: the driver polls it to know "
            f"when a program/erase has finished (fixed delays are not a "
            f"substitute)."
        )

    # ---------- 时序上限 ----------
    # 轮询超时必须有个来自手册的上界。缺了它，模板只能塞一个魔数进去 ——
    # 那个数字既无出处、也不会随器件更新，器件变慢时表现为"偶发写入失败"。
    timing = model.get("timing")
    if not isinstance(timing, dict):
        errors.append(
            f"{where} 'timing' is missing: the driver derives its WIP poll "
            f"timeout from it, otherwise it has to use a magic constant."
        )
    else:
        for name in (
            "page_program_ms",
            "sector_erase_ms",
            "block_erase_32k_ms",
            "block_erase_64k_ms",
            "chip_erase_ms",
        ):
            value = timing.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                errors.append(
                    f"{where} timing.{name}={value!r} must be a positive "
                    f"integer number of milliseconds."
                )

    return errors


def load_device_model(model_path: str) -> dict:
    """读取模型文件；仅供校验与测试使用（生成管线走 context.load_model）。"""
    with open(model_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate_model_file(model_path: str) -> list[str]:
    """按路径校验模型文件。文件不存在或不是 YAML 时返回一条错误。"""
    if not os.path.exists(model_path):
        return [f"[ERROR] model file '{model_path}' not found."]
    try:
        model = load_device_model(model_path)
    except yaml.YAMLError as exc:
        return [f"[ERROR] model file '{model_path}' is not valid YAML: {exc}"]
    return validate_device_model(model, model_path)
