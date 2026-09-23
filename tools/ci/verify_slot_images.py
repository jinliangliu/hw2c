#!/usr/bin/env python3
"""CI 判据：每个槽的镜像是不是按**自己的槽基址**链接的。

为什么需要它
------------
应用按槽分别链接：`app_slot_a.ld`（0x08002000）与 `app_slot_b.ld`（0x08040000），
由 `cmake -DHW2C_APP_SLOT=A|B` 选择。镜像头里的 CRC32 **只覆盖文件本身**，
与链接基址无关 ⇒ 把按槽 A 链接的镜像写进槽 B，引导器会"校验成功"地跳过去，
然后跳进槽 A 的地址、崩溃复位、串口零输出。现场看起来像"新模板引入回归"。

判据
----
向量表第二字（Reset_Handler）落在哪个槽的地址区间，就说明它是按哪个槽链接的。
期望区间不写死在脚本里，而是从**链接脚本**（真源）解析 —— 布局改了脚本跟着对，
不需要有人记得同步改常量。

解析不到 / 文件缺失一律**失败退出**，不降级、不静默（FR-13.6 的同一条规矩：
静默出口会把"判据没生效"伪装成"判据通过了"）。

用法（CI 里的工作目录是生成产物根目录）
--------------------------------------
    python ../../tools/ci/verify_slot_images.py \\
        --slot-a build/<proj>.bin --slot-b build_b/<proj>.bin \\
        --linker-a linker/app_slot_a.ld --linker-b linker/app_slot_b.ld \\
        --bootloader-ld linker/bootloader.ld --combined build/combined.bin
"""

import argparse
import re
import struct
import sys

# 向量表布局：第 0 字 = 初始 MSP，第 1 字 = Reset_Handler
_RESET_HANDLER_OFF = 4


def _num(text: str, where: str) -> int:
    """解析链接脚本里的数值：0x08002000 / 248K / 512M / 4096。"""
    s = text.strip().rstrip(",")
    if s.lower().startswith("0x"):
        return int(s, 16)
    m = re.match(r"^(\d+)\s*([KMGkmg]?)$", s)
    if not m:
        raise SystemExit("FAIL: 无法解析 %s 里的数值 %r" % (where, text))
    mult = {"": 1, "K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}
    return int(m.group(1)) * mult[m.group(2).upper()]


def parse_flash_region(ld_path: str) -> tuple:
    """从链接脚本的 MEMORY 块里取 FLASH 的 (ORIGIN, LENGTH)。"""
    try:
        text = open(ld_path, encoding="utf-8", errors="replace").read()
    except OSError as e:
        raise SystemExit("FAIL: 读不到链接脚本 %s: %s" % (ld_path, e))

    m = re.search(
        r"\bFLASH\b[^:{}]*:\s*ORIGIN\s*=\s*([^,]+),\s*LENGTH\s*=\s*([^\s}]+)",
        text,
        re.IGNORECASE,
    )
    if not m:
        raise SystemExit(
            "FAIL: %s 里找不到 FLASH 的 ORIGIN/LENGTH —— 判据无法判断"
            "镜像该落在哪个区间，宁可失败也不猜" % ld_path
        )
    origin = _num(m.group(1), ld_path)
    length = _num(m.group(2), ld_path)
    if length <= 0:
        raise SystemExit("FAIL: %s 里 FLASH LENGTH=%d 不合理" % (ld_path, length))
    return origin, length


def reset_handler(bin_path: str) -> int:
    try:
        with open(bin_path, "rb") as f:
            head = f.read(8)
    except OSError as e:
        raise SystemExit("FAIL: 读不到镜像 %s: %s" % (bin_path, e))
    if len(head) < 8:
        raise SystemExit(
            "FAIL: %s 只有 %d 字节，连向量表前两个字都不够" % (bin_path, len(head))
        )
    return struct.unpack_from("<I", head, _RESET_HANDLER_OFF)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slot-a", required=True, help="槽 A 镜像（build/<proj>.bin）")
    ap.add_argument("--slot-b", required=True, help="槽 B 镜像（build_b/<proj>.bin）")
    ap.add_argument("--linker-a", required=True, help="app_slot_a.ld")
    ap.add_argument("--linker-b", required=True, help="app_slot_b.ld")
    ap.add_argument("--bootloader-ld", required=True, help="bootloader.ld")
    ap.add_argument("--combined", required=True, help="build/combined.bin")
    args = ap.parse_args()

    a_base, a_len = parse_flash_region(args.linker_a)
    b_base, b_len = parse_flash_region(args.linker_b)
    bl_base, bl_len = parse_flash_region(args.bootloader_ld)

    if a_base < b_base + b_len and b_base < a_base + a_len:
        raise SystemExit(
            "FAIL: 两个槽的地址区间重叠（A [0x%08X, 0x%08X) / B [0x%08X, 0x%08X)）—— "
            "差分 OTA 会直接覆盖自己的旧镜像，而旧镜像是还原新镜像的唯一来源"
            % (a_base, a_base + a_len, b_base, b_base + b_len)
        )

    # 区间互不重叠 ⇒ 两个入口必然不同，所以不需要再判 "va == vb"：
    # 那样一条判据在任何输入下都不可能命中，写了等于没写。
    va = reset_handler(args.slot_a)
    vb = reset_handler(args.slot_b)

    print("slot A %s: link 0x%08X..0x%08X, Reset_Handler 0x%08X"
          % (args.slot_a, a_base, a_base + a_len, va))
    print("slot B %s: link 0x%08X..0x%08X, Reset_Handler 0x%08X"
          % (args.slot_b, b_base, b_base + b_len, vb))

    if not (a_base <= va < a_base + a_len):
        raise SystemExit(
            "FAIL: 槽 A 镜像的 Reset_Handler 0x%08X 不在槽 A 区间 "
            "[0x%08X, 0x%08X) —— 它是不是按别的槽/别的基础链接的？"
            % (va, a_base, a_base + a_len)
        )
    if not (b_base <= vb < b_base + b_len):
        raise SystemExit(
            "FAIL: 槽 B 镜像的 Reset_Handler 0x%08X 不在槽 B 区间 "
            "[0x%08X, 0x%08X) —— 给槽 B 种了按槽 A 链接的镜像时就是这个症状："
            "CRC 照旧通过，引导器跳过去才崩。"
            % (vb, b_base, b_base + b_len)
        )

    try:
        import os

        size = os.path.getsize(args.combined)
    except OSError as e:
        raise SystemExit("FAIL: 拿不到 combined.bin: %s" % e)
    if size <= bl_len:
        raise SystemExit(
            "FAIL: %s 只有 %d 字节，还没超过引导器区 %d 字节 —— "
            "应用部分根本没拼进去" % (args.combined, size, bl_len)
        )
    print("combined %s: %d B (> bootloader region %d B)" % (args.combined, size, bl_len))
    print("OK: 两个槽各自按自己的基址链接，combined.bin 已拼装（bootloader @ 0x%08X）"
          % bl_base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
