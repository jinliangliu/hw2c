#!/usr/bin/env python3
"""
固件镜像后处理：回填头部（image_size / CRC32 / fw_version）。

固件镜像布局（Slot 内，字段定义以 generator/data/fota_format.json 为唯一真源）:

  offset 0x00 - 0xBF:  向量表
  offset 0xC0 - 0xCF:  镜像头部 16 B
      0xC0  image_size  (u32)  payload(=code) 字节数
      0xC4  crc32       (u32)  CRC-32/ISO-HDLC（≡ zlib.crc32，非 MPEG-2；见 stm32_crc32）
      0xC8  magic       (u32)  0x4841436B
      0xCC  fw_version  (u32)  24-bit 有效
  offset 0xD0 - end:   payload（代码 + 数据）

CRC 覆盖 [0xC8, 0xD0 + image_size)，即 magic + fw_version + payload。
把 fw_version 纳入覆盖范围是刻意的：它是「哪个槽更新」的判据，不被 CRC 覆盖
就可以被改而校验通过。

⚠️ 本脚本**原位回填，绝不插入字节**。历史上它曾在 0xCC 插入 4 字节版本号，
使整个镜像后移 4 字节，而向量表与字面量池中的绝对地址不会跟着变
（向量表 [1] 仍指向 0x080020CC，真实入口已到 0xD0）⇒ 镜像不可运行。
详见 docs/plans/differential-ota.md §1.2 的 B1。

用法:
    python patch_crc.py firmware.bin -o output.bin [--version 1]
    python patch_crc.py firmware.bin --in-place
"""

import argparse
import json
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 格式真源
# ---------------------------------------------------------------------------

_SPEC_FILENAME = "fota_format.json"


def load_spec(explicit: str = None) -> dict:
    """定位并加载 fota_format.json。

    本脚本会被生成器单独复制到 output/<demo>/ 下独立运行，所以真源也必须
    随它一起复制；同时保留从仓库内直接运行时的查找路径。
    """
    here = Path(__file__).resolve().parent
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates += [
        here / _SPEC_FILENAME,                    # output/<demo>/fota_format.json
        here / "data" / _SPEC_FILENAME,           # generator/data/fota_format.json
        here.parent / "generator" / "data" / _SPEC_FILENAME,
    ]
    for cand in candidates:
        if cand.is_file():
            with cand.open("r", encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(
        "找不到格式真源 %s；已尝试:\n  %s" % (_SPEC_FILENAME, "\n  ".join(str(c) for c in candidates))
    )


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------

def stm32_crc32(data: bytes) -> int:
    """STM32 硬件 CRC32（与 STM32G0 CRC 外设一致）。

    ⚠️ 算法是 **CRC-32/ISO-HDLC（≡ zlib.crc32 ≡ Ethernet CRC）**，不是
    CRC-32/MPEG-2 —— 早期注释写的 MPEG-2 是错的，两者结果不同：

        多项式  0x04C11DB7（反射形式 0xEDB88320）
        初值    0xFFFFFFFF
        REV_IN  每个字节（反射）
        REV_OUT 是（最终整体反射，等价于终值异或 0xFFFFFFFF）
        校验值  crc32(b"123456789") == 0xCBF43926

    MPEG-2 变体是「不反射、终值不异或」，配置错会让引导器拒绝**所有**镜像，
    而且很容易误判成"硬件 CRC 外设有问题"。

    必须与 templates/bootloader/boot_crc.c.j2 中 CRC 外设的 REV_IN / REV_OUT
    配置保持等价 —— 否则引导器会拒绝所有镜像。
    交叉核对：`stm32_crc32(x) == zlib.crc32(x) & 0xFFFFFFFF`。
    """
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc >>= 1
    return crc ^ 0xFFFFFFFF


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def patch_firmware(input_path: str, output_path: str = None, version: int = 0,
                   spec: str = None) -> dict:
    """搜索 magic 定位头部，原位回填 image_size / CRC32 / fw_version。

    Returns:
        结果字典，供测试与调用方断言（键：image_size / crc32 / version /
        header_offset / payload_offset / size_before / size_after）。
    """
    fmt = load_spec(spec)["image_header"]
    fields = fmt["fields"]
    magic_value = fields["magic"]["value"]
    magic_off_in_hdr = fields["magic"]["offset"]
    hdr_size = fmt["size"]
    crc_region = fmt["crc_region"]
    version_mask = fmt["version_mask"]

    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError("输入固件不存在: %s" % input_path)

    data = bytearray(input_path.read_bytes())
    size_before = len(data)

    if size_before < fmt["payload_offset_in_slot"]:
        raise ValueError("固件太小（%d B），至少需要 %d B 才能容纳头部"
                         % (size_before, fmt["payload_offset_in_slot"]))

    # --- 定位 magic（按 u32 数值比较，不按肉眼认字符串）---
    magic_offset = None
    search_max = min(fmt["search_max"], size_before - 4)
    for off in range(0, search_max, fmt["search_align"]):
        if struct.unpack_from("<I", data, off)[0] == magic_value:
            magic_offset = off
            break
    if magic_offset is None:
        raise ValueError(
            "前 %d 字节内未找到头部 magic 0x%08X；请确认链接脚本包含 .app_header 段。"
            % (fmt["search_max"], magic_value))

    header_offset = magic_offset - magic_off_in_hdr
    if header_offset < 0:
        raise ValueError("magic 位于 0x%X，其前无足够空间容纳头部" % magic_offset)

    # --- 头部位置必须**恰好**是真源声明的槽内偏移（fail-closed）---
    #
    # 为什么不能"搜到哪儿就按哪儿写"：这个脚本曾经用 magic 的实际位置反推
    # header_offset，于是任何位移都被它**顺着**接受了。真正的事故是这样发生的
    # （2026-09-16 实测）：`.isr_vector` 在 STM32G0B1 上只有 47 项 = 188 B = 0xBC，
    # 链接脚本里 `.app_header` 紧随其后 → 落在 0xBC，而不是真源写的 0xC0。
    # 本脚本照 0xBC 回填，"成功"了；而引导器 / fota_delta 按 `slot_base + 0xC0`
    # 读取 ⇒ magic 对不上 ⇒ **在板上拒掉每一个镜像**。
    # 生成与编译都一路绿灯，缺陷只在机器上显形 —— 这正是最贵的一类。
    if header_offset != fmt["offset_in_slot"]:
        raise ValueError(
            "镜像头位于槽内偏移 0x%X，但格式真源声明的是 0x%X（generator/data/fota_format.json"
            " 的 image_header.offset_in_slot）。\n"
            "这几乎总是链接脚本的问题：`.app_header` 必须被**钉在**该偏移上，"
            "不能依赖向量表自然长度（STM32G0B1 实测为 0xBC）。\n"
            "引导器与 fota_delta 都按 0x%X 读取，偏移不符会让它们拒掉所有镜像。"
            % (header_offset, fmt["offset_in_slot"], fmt["offset_in_slot"]))

    payload_offset = header_offset + hdr_size
    version_offset = header_offset + fields["fw_version"]["offset"]
    crc_region_start = header_offset + (crc_region["start_offset_in_slot"] - fmt["offset_in_slot"])

    payload = bytes(data[payload_offset:])
    image_size = len(payload)
    if image_size == 0:
        raise ValueError("payload 为空（头部之后没有数据）")
    if image_size % 4 != 0:
        raise ValueError(
            "payload 长度 %d 不是 4 的倍数；硬件 CRC 按 32-bit 字推进，"
            "长度不整除会静默漏算尾部。请检查链接脚本与 objcopy 输出。" % image_size)

    # --- 回填版本号（原位，不插字节）---
    struct.pack_into("<I", data, version_offset, version & version_mask)

    # --- CRC 覆盖 [magic, payload 末尾) ---
    crc_len = image_size + crc_region["length_addend"]
    crc_region_end = crc_region_start + crc_len
    if crc_region_end > len(data):
        raise ValueError("CRC 区间 [0x%X, 0x%X) 超出镜像长度 %d"
                         % (crc_region_start, crc_region_end, len(data)))
    crc_value = stm32_crc32(bytes(data[crc_region_start:crc_region_end]))

    # --- 回填 image_size + CRC32 ---
    struct.pack_into("<II", data, header_offset, image_size, crc_value)

    # --- 硬性断言：长度必须与输入一致 ---
    # 长度变化 = 发生了插入/删除 = 绝对地址全部失配（B1）。这是不可协商的不变量。
    if len(data) != size_before:
        raise AssertionError(
            "内部错误：回填改变了镜像长度（%d -> %d）。原位回填不允许改变长度。"
            % (size_before, len(data)))

    dest = output_path or input_path
    Path(dest).write_bytes(data)

    return {
        "image_size": image_size,
        "crc32": crc_value,
        "version": version & version_mask,
        "header_offset": header_offset,
        "payload_offset": payload_offset,
        "crc_region": (crc_region_start, crc_region_end),
        "size_before": size_before,
        "size_after": len(data),
        "output": str(dest),
    }


def main():
    parser = argparse.ArgumentParser(description="STM32G0 固件头部回填（CRC32 + 版本号）")
    parser.add_argument("input", help="输入固件 .bin 文件")
    parser.add_argument("-o", "--output", help="输出文件路径（默认覆盖输入）")
    parser.add_argument("--in-place", action="store_true", help="原位修改（同没有 -o）")
    parser.add_argument("--version", type=lambda x: int(x, 0), default=0,
                        help="固件版本号（24-bit，如 1 或 0x01）")
    parser.add_argument("--spec", help="格式真源 fota_format.json 的路径（可选）")
    args = parser.parse_args()

    output = args.output if args.output else (args.input if args.in_place else None)
    if output is None:
        parser.error("请指定 -o <output> 或 --in-place")

    try:
        res = patch_firmware(args.input, output, args.version, args.spec)
    except (FileNotFoundError, ValueError, AssertionError) as exc:
        print("[ERROR] %s" % exc)
        sys.exit(1)

    print("Header  found at offset 0x%X" % res["header_offset"])
    print("Payload at offset 0x%X" % res["payload_offset"])
    print("Version:    0x%06X" % res["version"])
    print("Image size: %d bytes (payload)" % res["image_size"])
    print("CRC32:      0x%08X  over [0x%X, 0x%X)" % (res["crc32"], *res["crc_region"]))
    print("Total:      %d bytes (unchanged)" % res["size_after"])
    print("Output:     %s" % res["output"])


if __name__ == "__main__":
    main()
