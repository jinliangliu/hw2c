"""差分 OTA 测试用的镜像夹具（不是产品代码）。

为什么需要它
------------
差分域是**整个 slot 镜像**（向量表 + 头部 + 代码），所以任何喂给
`delta_tool.build()` 的数据都必须是「结构合法的 slot 镜像」：头部 magic 在
`0xC0`、`image_size` 与 `crc32` 与实际内容自洽。随手截一段随机字节当输入
是**不合法**的（`build()` 会 fail-closed 拒绝），也不再能代表真实输入。

因此单元测试必须从「一段代码区」出发，把它包成合法的 slot 镜像 —— 这正是
本模块的职责。

为什么向量表要按 slot 基址生成
------------------------------
`boot_jump_to_app()` 用 `SCB->VTOR = slot_base`，向量表里的**绝对地址**因此
是 slot 相关的。如果夹具把两个槽的向量表生成得一模一样，就掩盖了 A→B 更新
中最容易出问题的那部分差异（这是实施中真实踩到的缺陷，见 `split_image()`）。
所以这里按 `slot_base` 生成条目，让测试与现场一致。
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from generator.delta_tool import image_crc32, split_image  # noqa: E402

# 与 generator/context/bootloader_context.py 的默认值一致
SLOT_A_BASE = 0x08002000
SLOT_B_BASE = 0x08040000

RAM_TOP = 0x20024000          # STM32G0B1RE：144 KB SRAM 顶端
VECTOR_BYTES = 192            # 48 项 × 4 B = 0xC0，头部紧随其后


def firmware_like(n: int, seed: int = 0) -> bytes:
    """造一段「像固件」的代码区：混合重复块与随机字节。

    纯随机数据会让块匹配器完全失效（差分退化成全字面量），测不出真实压缩率；
    全重复数据又会让 cover 数量少到覆盖不到解析分支。混合两者才有代表性。
    """
    r = random.Random(seed)
    out = bytearray()
    while len(out) < n:
        if r.random() < 0.5:
            out += bytes(r.getrandbits(8) for _ in range(r.randint(4, 64)))
        else:
            out += bytes([r.getrandbits(8)]) * r.randint(8, 128)
    return bytes(out[:n])


def vector_table(slot_base: int) -> bytes:
    """按 slot 基址生成 192 B 向量表（48 项）。

    条目里放**绝对地址** —— 这正是两个槽之间必然不同、且必须被差分吸收的部分。
    """
    words = [RAM_TOP]
    for i in range(1, VECTOR_BYTES // 4):
        # 末尾或上 Thumb 位，和真实的异常向量一样
        words.append((slot_base + 0x0D0 + i * 4) | 1)
    return b"".join(w.to_bytes(4, "little") for w in words)


def slot_image(code: bytes, *, fw_version: int = 1,
               slot_base: int = SLOT_A_BASE) -> bytes:
    """把代码区包成结构合法的 slot 镜像：`[向量表 192B][头部 16B][代码]`。

    头部按 `fota_format.json` 的布局填写，并按 CRC 覆盖区（从 magic 起，含
    fw_version 与代码区）算出 `crc32` —— 与 `patch_crc.py` 对真实产物做的事
    完全一致。所以夹具镜像可以直接当 `delta_tool` 的输入。
    """
    img = _image_header_spec()
    fields = img["fields"]

    vec = vector_table(slot_base)
    if len(vec) != img["offset_in_slot"]:
        raise AssertionError("向量表长度 %d 与头部偏移 %d 不一致"
                             % (len(vec), img["offset_in_slot"]))

    hdr = bytearray(img["size"])
    hdr[fields["image_size"]["offset"]:fields["image_size"]["offset"] + 4] = \
        len(code).to_bytes(4, "little")
    hdr[fields["crc32"]["offset"]:fields["crc32"]["offset"] + 4] = b"\x00" * 4
    hdr[fields["magic"]["offset"]:fields["magic"]["offset"] + 4] = \
        fields["magic"]["value"].to_bytes(4, "little")
    hdr[fields["fw_version"]["offset"]:fields["fw_version"]["offset"] + 4] = \
        fw_version.to_bytes(4, "little")

    blob = bytearray(vec) + hdr + bytearray(code)

    # 回填 CRC：覆盖区从 magic 开始（排除 image_size / crc32 自身）
    start = img["crc_region"]["start_offset_in_slot"]
    length = len(code) + img["crc_region"]["length_addend"]
    crc_off = img["offset_in_slot"] + fields["crc32"]["offset"]
    blob[crc_off:crc_off + 4] = b"\x00" * 4
    from generator.patch_crc import stm32_crc32
    crc = stm32_crc32(bytes(blob[start:start + length]))
    blob[crc_off:crc_off + 4] = crc.to_bytes(4, "little")
    return bytes(blob)


def slot_pair(code_old: bytes, code_new: bytes, *, fw_version: int = 2,
              old_base: int = SLOT_A_BASE,
              new_base: int = SLOT_B_BASE) -> tuple:
    """生成一对 (旧镜像, 新镜像)。默认模拟「活动槽 A → 目标槽 B」。"""
    return (slot_image(code_old, fw_version=fw_version - 1, slot_base=old_base),
            slot_image(code_new, fw_version=fw_version, slot_base=new_base))


def code_of(image: bytes) -> bytes:
    """取出镜像里的代码区（测试里改代码用）。"""
    return split_image(image)["code"]


def expected_target(new_image: bytes) -> bytes:
    """设备应用补丁后、**尚未回填头部**时应得的字节。

    与 `new_image` 的差别仅在头部那两个**事后回填**的字段：差分流里它们恒为
    0xFFFFFFFF，由设备在 FLUSH 阶段写入真值。所以逐字节比对的期望值就是
    占位版本。
    """
    from generator.delta_tool import apply_header_placeholders
    return apply_header_placeholders(new_image)


def finalize_image(applied: bytes) -> bytes:
    """模拟设备 FLUSH 阶段的头部回填，得到**最终**镜像。

    刻意**不读**头部里的 `image_size` 字段来定位代码区长度，而是从镜像总长反推：
    `code_size = len(applied) - payload_offset`。设备侧也是这么做的 —— 它从信封的
    `new_size` 推出代码区长度，从而完全不依赖头部里那个尚未写入的字段。
    （这正是 CRC 覆盖区从 magic 起、排除 `image_size`/`crc32` 的收益。）
    """
    img = _image_header_spec()
    fields = img["fields"]
    hdr_off = img["offset_in_slot"]
    payload_off = img["payload_offset_in_slot"]
    code_size = len(applied) - payload_off
    if code_size <= 0:
        raise ValueError("镜像长度 %d 不足以容纳头部" % len(applied))

    out = bytearray(applied)
    start, length = (hdr_off + img["crc_region"]["start_offset_in_slot"] - hdr_off,
                     code_size + img["crc_region"]["length_addend"])
    crc = patch_crc32_of(out[start:start + length])
    out[hdr_off + fields["image_size"]["offset"]:
        hdr_off + fields["image_size"]["offset"] + 4] = code_size.to_bytes(4, "little")
    out[hdr_off + fields["crc32"]["offset"]:
        hdr_off + fields["crc32"]["offset"] + 4] = crc.to_bytes(4, "little")
    return bytes(out)


def patch_crc32_of(region: bytes) -> int:
    from generator.patch_crc import stm32_crc32
    return stm32_crc32(region)


def _image_header_spec() -> dict:
    from generator.delta_tool import _fmt
    return _fmt()["image_header"]


def patch_crc32(image: bytes) -> int:
    """镜像的 CRC（与引导器覆盖同一段），供测试断言回填值。"""
    return image_crc32(image)
