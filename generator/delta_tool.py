#!/usr/bin/env python3
"""差分 OTA 的**写侧**（自研）。

为什么自研
----------
见 docs/plans/differential-ota.md §16.7 裁决 1：HPatchLite 只发布解码器
（4 个文件），写侧在 `HDiff/Diff` 模块，本项目**不使用**。因此
「差分块匹配 + HPatchLite lite 流编码 + 信封」三件都由本文件负责；
tinyuz 编码见裁决 2（`generator/tinyuz_enc.py`）。

正确性判据
----------
HPatchLite 没有格式规范文档，唯一权威是**解码器源码本身**。所以本文件的
正确性判据不是「与某个参考实现逐位一致」，而是：

    oracle_apply(old, build(old, new)) == new      （逐字节）

其中 `oracle_apply` 由 `tools/hpatch_oracle/` 用同一份 vendored 源码编译而成。
下面所有格式细节都注明了它对应的解码器代码位置（`hpatch_lite.c:<行>`），便于复核。

编码器的自由度
--------------
压缩器/编码器的合法输出**不唯一**。我们的编码器只需满足「可被解码器还原」，
**不需要**与上游 `hdiff` / `tuz_enc` 逐字节一致。因此本实现一律取最保守的
构造：varint 最短编码、cover 严格按 newPos 递增、绝不产生越界 old 读取。

用法
----
    python -m generator.delta_tool build --old old.bin --new new.bin -o p.h2cd --version 3
    python -m generator.delta_tool info p.h2cd
    python -m generator.delta_tool build ... --input-mode raw    # 输入没有镜像头

差分域 = **整个 slot 镜像**（向量表 + 头部 + 代码）。不含向量表会让目标槽在更新后
启动旧固件或静默放弃跳转 —— 详见 `split_image()`。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

try:                                              # 包内导入
    from .patch_crc import load_spec, stm32_crc32
except ImportError:                               # 直接以脚本方式运行
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from generator.patch_crc import load_spec, stm32_crc32


# ===========================================================================
# 0. 常量（布局全部来自格式真源，不在此另立一份）
# ===========================================================================

def _fmt() -> dict:
    return load_spec()


def _env() -> dict:
    return _fmt()["delta_envelope"]


def _env_fields() -> dict:
    return _env()["fields"]


# --- 控制流常量（与解码器里的字面量一致） ---------------------------------
HPI_K_HEAD_SIZE = 4            # hpatch_lite_types.h: (2+1+1)
HPI_COMPRESS_TYPE_NO = 0       # hpi_compressType_no
HPI_COMPRESS_TYPE_TUZ = 1      # hpi_compressType_tuz
HPI_VERSION_CODE = 1           # hpatch_lite.c:118 kHPatchLite_versionCode

# tag 位含义（hpatch_lite.c:164-172）
_TAG_BIT_NOT_NEED_SUBDIFF = 1 << 7    # isNotNeedSubDiff
_TAG_BIT_OLD_POS_NEGATIVE = 1 << 6
_TAG_BIT_OLD_POS_NEXT = 1 << 5


# ===========================================================================
# 1. CRC-16/CCITT-FALSE
#    用于信封头校验。它与 image_header 的 CRC32 用途不同：它挡的是"头部被改"
#    这种廉价错误，不是内容完整性（内容完整性由 old/new crc32 负责）。
# ===========================================================================

def crc16_ccitt_false(data: bytes, crc: int = 0xFFFF) -> int:
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


# ===========================================================================
# 2. HPatchLite lite 流的编码原语
#
#    解码侧对应 static/third_party/hpatch_lite/hpatch_lite.c:
#      _cache_unpackUInt(self, v, isNext)    :53
#          while (isNext) { b = read1(); v = (v<<7)|(b&127); isNext = b>>7; }
#      _hpi_readSize(buf, len)               :108  v = sum(buf[i] << 8*i)  —— 小端
# ===========================================================================

def pack_uvarint(value: int) -> bytes:
    """7 位一组、**最高位组先出**，除末组外每字节 bit7=1 作续接位。

    与 `_cache_unpackUInt(..., v=0, isNext=1)` 对应：循环体至少执行一次，
    因此 0 必须编成 b'\\x00'（而不是空串）。
    """
    if value < 0:
        raise ValueError("pack_uvarint 不接受负数: %d" % value)
    groups = []
    v = value
    while True:
        groups.append(v & 0x7F)
        v >>= 7
        if v == 0:
            break
    groups.reverse()
    last = len(groups) - 1
    return bytes(g | (0x80 if i < last else 0x00)
                 for i, g in enumerate(groups))


def pack_oldpos_delta(delta: int) -> Tuple[int, bytes]:
    """把 oldPos 增量的**绝对值**编成 (tag 低 5 位, 续接字节)。

    解码（hpatch_lite.c:166）：
        cover_oldPos = _cache_unpackUInt(&diff, tag&31, tag&(1<<5))
    即低 5 位作为**最高位组**直接放在 tag 里，续接由 tag bit5 触发，
    之后每读一字节再补 7 位、由该字节自己的 bit7 决定是否继续。
    因此「先写进 tag 的 5 位 + 后续 k 字节」共承载 5+7k 位 ⇒ 取最小 k 使
    delta < 2**(5+7k)。
    """
    if delta < 0:
        raise ValueError("pack_oldpos_delta 的入参应为绝对值: %d" % delta)
    k = 0
    while delta >= (1 << (5 + 7 * k)):
        k += 1
    tag5 = delta >> (7 * k)
    if tag5 >= 32:
        raise AssertionError("内部错误：tag5=%d 超出 5 位" % tag5)
    out = bytearray()
    for i in range(k - 1, -1, -1):
        out.append(((delta >> (7 * i)) & 0x7F) | (0x80 if i > 0 else 0x00))
    return tag5, bytes(out)


def _size_width(value: int) -> int:
    """newSize / uncompressSize 字段的最小字节宽（1..4）。

    注意 0 也必须占 1 字节 —— 宽度 0 是"该字段不出现"的专用语义
    （解码器 hpatch_lite.c:135 读 0 字节 ⇒ uncompressSize=0）。
    """
    if value < 0:
        raise ValueError("size 不能为负")
    width = 1
    while value >= (1 << (8 * width)) and width < 4:
        width += 1
    return width


def pack_lite_header(compress_type: int, new_size: int,
                     uncompress_size: int = 0) -> bytes:
    """HPatchLite 明文头。对应 `hpatch_lite_open()`（hpatch_lite.c:116-137）：

        buf[0]='h'  buf[1]='I'  buf[2]=compressType
        buf[3] = [7:6]versionCode | [5:3]uncompressSize 字节宽 | [2:0]newSize 字节宽
        随后 newSize（小端，宽度见上）、uncompressSize（小端，宽度 0 ⇒ 不占字节）

    `uncompress_size` 是**补丁流解压后的字节数**（不是新固件大小）：上游
    `-c-tuz` 压缩的是 cover 数据流本身。未压缩路径传 0。
    """
    if compress_type not in (HPI_COMPRESS_TYPE_NO, HPI_COMPRESS_TYPE_TUZ):
        raise ValueError("不支持的 compress_type=%d" % compress_type)

    new_w = _size_width(new_size)
    unc_w = _size_width(uncompress_size) if uncompress_size else 0
    if unc_w > 7 or new_w > 7:
        raise ValueError("字段宽度超出 3 位编码能力")

    b3 = ((HPI_VERSION_CODE & 0x3) << 6) | ((unc_w & 0x7) << 3) | (new_w & 0x7)
    # ⚠️ 魔数是**不对称**的 `'h'`（小写）+ `'I'`（大写），见 hpatch_lite.c:126
    #    _SAFE_CHECK((lenn==hpi_kHeadSize)&(buf[0]=='h')&(buf[1]=='I')&...)
    #    写成 "HI" 会被解码器当成头不合法而整体拒绝。
    out = bytearray(b"hI" + bytes([compress_type, b3]))
    out += new_size.to_bytes(new_w, "little")
    if unc_w:
        out += uncompress_size.to_bytes(unc_w, "little")
    return bytes(out)


# ===========================================================================
# 3. Cover 与差分
# ===========================================================================

@dataclass
class Cover:
    """一段「从旧数据取，写到新镜像」的映射。

    copy=True  → 纯拷贝，不消费 diff 字节（tag bit7 = 1）
    copy=False → 与 diff 字节逐字节相加（tag bit7 = 0）
    """
    new_pos: int
    old_pos: int
    length: int
    copy: bool
    diff: bytes = field(default=b"")


# 差分器参数。取保守值，先把正确性跑通，再谈比率。
_BLOCK = 12           # 块匹配窗口
_MIN_GAIN = 12        # 值得为一个 cover 付出控制开销的最小长度
_MAX_CANDIDATES = 32  # 同一哈希值的候选上限，界定最坏耗时


class _RollingHash:
    """多项式滚动哈希。

    自实现而非用内置 `hash()`，是为了跨进程/跨版本稳定（内置 str/bytes
    哈希有随机化种子）。位宽固定 32 位并显式取模，避免大整数拖慢速度。
    """

    _BASE = 131
    _MASK = 0xFFFFFFFF

    def __init__(self, block: int):
        self.block = block
        self._pow = pow(self._BASE, block - 1, 1 << 32)

    def hash_at(self, data: bytes, pos: int) -> int:
        h = 0
        for k in range(self.block):
            h = ((h * self._BASE) + data[pos + k]) & self._MASK
        return h

    def roll(self, h: int, data: bytes, pos: int) -> int:
        """从窗口 [pos-1, pos-1+block) 滚到 [pos, pos+block)。"""
        return ((h - data[pos - 1] * self._pow) * self._BASE
                + data[pos + self.block - 1]) & self._MASK


def _index_old(old: bytes, block: int) -> Dict[int, List[int]]:
    """建立 哈希 → old 位置列表 的索引（每桶最多 `_MAX_CANDIDATES` 个）。"""
    idx: Dict[int, List[int]] = {}
    n = len(old)
    if n < block:
        return idx
    rh = _RollingHash(block)
    h = rh.hash_at(old, 0)
    idx[h] = [0]
    for i in range(1, n - block + 1):
        h = rh.roll(h, old, i)
        bucket = idx.get(h)
        if bucket is None:
            idx[h] = [i]
        elif len(bucket) < _MAX_CANDIDATES:
            bucket.append(i)
    return idx


def diff_to_covers(old: bytes, new: bytes, *, block: int = _BLOCK,
                   min_gain: int = _MIN_GAIN) -> List[Cover]:
    """滚动哈希块匹配，把 new 表示成一串 cover。

    未覆盖处就是"间隙"，由 `encode_lite_stream` 作为字面量写出。

    为什么不用后缀数组（规划里的 B3）：那个实现对 256 KB 输入会产生
    数十 GB 中间量。滚动哈希是 O(n)，规模上安全。

    ⚠️ **正确性要点**：`is_copy` 由 `seg_old == seg_new` 的**实际字节比较**
    决定，而不是由"哈希命中"推断。哈希碰撞最多让匹配变短（效率损失），
    绝不可能产出错误 cover —— 因为碰撞时两侧字节不等，会走 CDIFF 分支
    逐字节存差值，解码后仍精确还原 new。
    """
    covers: List[Cover] = []
    n_old, n_new = len(old), len(new)
    if n_old < block or n_new < block:
        return covers

    idx = _index_old(old, block)
    rh = _RollingHash(block)

    i = 0
    limit = n_new - block
    h_pos = 0
    h = rh.hash_at(new, 0)

    while i <= limit:
        # 让 h 追上 i（总共最多 roll n_new 次，摊销 O(1)）
        while h_pos < i:
            h = rh.roll(h, new, h_pos + 1)
            h_pos += 1

        best_len = 0
        best_old = -1

        for j in idx.get(h, ()):
            max_len = min(n_old - j, n_new - i)
            ln = block
            while ln < max_len and old[j + ln] == new[i + ln]:
                ln += 1
            if ln > best_len:
                best_len = ln
                best_old = j

        if best_len < max(block, min_gain):
            i += 1
            continue

        j = best_old
        seg_old = old[j:j + best_len]
        seg_new = new[i:i + best_len]
        if seg_old == seg_new:
            diff_bytes = b""
            is_copy = True
        else:
            mismatches = sum(1 for a, b in zip(seg_old, seg_new) if a != b)
            # CDIFF 的代价与"间隙"相同（都是 length 字节），但差值多为 0，
            # 压缩路径下收益明显。不满足这个比例说明匹配本身不值得。
            if mismatches * 4 > best_len:
                i += 1
                continue
            diff_bytes = bytes((b - a) & 0xFF for a, b in zip(seg_old, seg_new))
            is_copy = False

        covers.append(Cover(new_pos=i, old_pos=j, length=best_len,
                            copy=is_copy, diff=diff_bytes))
        i += best_len

    return covers


# ===========================================================================
# 4. lite 流组装
# ===========================================================================

def encode_lite_stream(old: bytes, new: bytes, covers: Sequence[Cover]) -> bytes:
    """把 covers 编成 HPatchLite 补丁正文（**不含** 4 字节明文头）。

    解码器逐 cover 的读取顺序（hpatch_lite.c:157-185）：
        coverCount(varint)                      :155  —— 循环外，只读一次
        ┌ 每轮：
        │   cover_length(varint)                :163
        │   tag(1B)  →  oldPos 增量（5 位 + 续接字节）  :164-172
        │   cover_newPos 增量(varint，相对 newPosBack)  :173-174
        │   if newPosBack < cover_newPos:  间隙字面量（裸字节）  :178-179
        │   length 个 diff 裸字节（仅当 tag bit7 == 0）        :82-100
        └ newPosBack = cover_newPos + length;  oldPosBack = cover_oldPos + length

    ⚠️ **尾部收束的不变量**：解码器结尾断言 `newSize == newPosBack`，而字面量
    只能通过 `newPosBack < cover_newPos` 这条路径写出。所以：

      * 若 new 非空，**必须至少有一个 cover**，哪怕它是零长度的；
      * 若 new 以"间隙"结尾（后面没有真实 cover），必须补一个零长度 cover 来
        把这批字面量送出去。

    解码器 `_SAFE_CHECK((cover_length>0)|(coverCount==0))`（:183）正是为这种
    零长度收尾留的口子：只允许**最后一个** cover 长度为 0。本函数在入口处把
    收尾 cover 显式补进列表，从而不再需要分支处理。
    """
    work = list(covers)

    # --- 规范化：确保收尾有一个 cover 承载尾部字面量 ---------------------
    if work:
        tail_new = work[-1].new_pos + work[-1].length
        tail_old = work[-1].old_pos + work[-1].length
    else:
        tail_new, tail_old = 0, 0
    if tail_new < len(new):
        # new_pos 直接放到末尾 ⇒ 该 cover 的 newPos 增量恰好等于尾部字面量长度；
        # old_pos 与 oldPosBack 相同 ⇒ 增量为 0（长度 0，read_old 永不被调用）。
        work.append(Cover(new_pos=len(new), old_pos=tail_old, length=0, copy=True))

    if len(new) > 0 and not work:
        raise AssertionError("内部错误：new 非空却没有可用 cover")

    body = bytearray()
    body += pack_uvarint(len(work))

    new_pos_back = 0
    old_pos_back = 0

    for idx_c, c in enumerate(work):
        if len(c.diff) not in (0, c.length):
            raise AssertionError("diff 字节数 %d 与 cover 长度 %d 不符"
                                 % (len(c.diff), c.length))
        # 解码器 :177-183 的断言，先在写侧挡住，报错信息更可读
        if c.new_pos < new_pos_back:
            raise AssertionError("cover#%d 的 new_pos=%d 小于 newPosBack=%d"
                                 % (idx_c, c.new_pos, new_pos_back))
        if c.length and c.old_pos + c.length > len(old):
            raise AssertionError("cover#%d 越界读取旧数据（old_pos=%d len=%d old=%d）"
                                 % (idx_c, c.old_pos, c.length, len(old)))
        if c.length == 0 and idx_c != len(work) - 1:
            raise AssertionError("零长度 cover 只允许出现在最后（解码器 :183 断言）")

        gap = c.new_pos - new_pos_back

        body += pack_uvarint(c.length)

        # --- tag + oldPos 增量 ---
        delta = c.old_pos - old_pos_back
        tag5, ext = pack_oldpos_delta(abs(delta))
        tag = tag5
        if ext:
            tag |= _TAG_BIT_OLD_POS_NEXT
        if delta < 0:
            tag |= _TAG_BIT_OLD_POS_NEGATIVE
        if c.copy:
            tag |= _TAG_BIT_NOT_NEED_SUBDIFF
        body.append(tag)
        body += ext

        # --- newPos 增量（相对 newPosBack）---
        body += pack_uvarint(gap)

        # --- 间隙字面量 ---
        if gap:
            body += new[new_pos_back:c.new_pos]

        # --- diff 字面量 ---
        if not c.copy:
            body += c.diff

        new_pos_back = c.new_pos + c.length
        old_pos_back = c.old_pos + c.length

    if new_pos_back != len(new):
        raise AssertionError("组装后 newPosBack=%d 与 newSize=%d 不符"
                             % (new_pos_back, len(new)))
    return bytes(body)


# ===========================================================================
# 5. 信封
# ===========================================================================

def pack_envelope(*, old_size: int, old_crc32: int, new_size: int, new_crc32: int,
                  patch_size: int, fw_version: int, compressed: bool,
                  auth_len: int = 0) -> bytes:
    """按真源布局打包 48 字节信封，并回填 hdr_crc16。"""
    env = _env()
    fields = env["fields"]
    buf = bytearray(env["size"])

    def put(name: str, value: int) -> None:
        f = fields[name]
        buf[f["offset"]:f["offset"] + f["size"]] = int(value).to_bytes(
            f["size"], "little", signed=bool(f.get("signed", False)))

    flags = env["flags_bits"]["compressed"] if compressed else 0

    put("magic", fields["magic"]["value"])
    put("format_ver", fields["format_ver"]["value"])
    put("flags", flags)
    put("old_size", old_size)
    put("old_crc32", old_crc32)
    put("new_size", new_size)
    put("new_crc32", new_crc32)
    put("patch_size", patch_size)
    put("fw_version", fw_version)
    put("auth_len", auth_len)
    # hdr_crc16 覆盖其之前的全部字节（含 magic 与 fw_version ⇒ 版本被篡改可检出）
    crc16 = crc16_ccitt_false(bytes(buf[:fields["hdr_crc16"]["offset"]]))
    put("hdr_crc16", crc16)
    return bytes(buf)


def parse_envelope(blob: bytes) -> dict:
    """解析并校验信封；任何不一致都抛 ValueError（fail-closed）。"""
    env = _env()
    fields = env["fields"]
    if len(blob) < env["size"]:
        raise ValueError("补丁太短（%d B），装不下 %d B 的信封" % (len(blob), env["size"]))

    def get(name: str) -> int:
        f = fields[name]
        return int.from_bytes(blob[f["offset"]:f["offset"] + f["size"]], "little")

    if get("magic") != fields["magic"]["value"]:
        raise ValueError("信封 magic 不匹配（期望 0x%08X，实际 0x%08X）"
                         % (fields["magic"]["value"], get("magic")))
    if get("format_ver") != fields["format_ver"]["value"]:
        raise ValueError("信封 format_ver=%d 不受支持" % get("format_ver"))

    stored = get("hdr_crc16")
    calc = crc16_ccitt_false(bytes(blob[:fields["hdr_crc16"]["offset"]]))
    if stored != calc:
        raise ValueError("信封头 CRC16 不匹配（存 0x%04X，算 0x%04X）" % (stored, calc))

    flags = get("flags")
    known = env["flags_bits"]["compressed"]
    if flags & ~known:
        raise ValueError("信封 flags=0x%X 含未知位，拒绝" % flags)

    return {
        "format_ver": get("format_ver"),
        "flags": flags,
        "compressed": bool(flags & known),
        "old_size": get("old_size"),
        "old_crc32": get("old_crc32"),
        "new_size": get("new_size"),
        "new_crc32": get("new_crc32"),
        "patch_size": get("patch_size"),
        "fw_version": get("fw_version"),
        "auth_len": get("auth_len"),
    }


# ===========================================================================
# 6. 顶层
# ===========================================================================

def split_image(image: bytes) -> dict:
    """从 slot 镜像里取出**差分域**与头部信息。

    ⚠️ 差分域 = **整个 slot 镜像** `[0, payload_off + code_size)`，**不是**代码区。

    为什么必须含向量表（这是实施中发现并修正的一个真实缺陷）：

    `boot_jump_to_app()` 的做法是 `SCB->VTOR = slot_base`，再从 `slot+0` / `slot+4`
    取初始 SP 与 Reset_Handler（见 `templates/bootloader/boot_jump.c.j2`）。
    也就是说**目标槽的向量表必须已经是新固件的**。若补丁只搬运 `slot+0xD0` 之后的
    代码区，目标槽 `[0, 0xC0)` 会保留上一次的向量表，后果是：

    * 该槽曾经装过固件 → SP/入口指向**旧版本**的代码 → 更新后启动的仍是旧固件
      （静默失效，比崩溃更难发现）；
    * 该槽从未用过（全 0xFF）→ `boot_jump` 的安全检查
      `(app_entry & 0xFFE00000) != 0x08000000` 命中，跳转被静默放弃。

    两种都不是我们想要的。把向量表纳入差分域后，A→B 更新时向量表里的绝对地址
    差异由差分器自然吸收（规划 §8.1 已预期："差异集中在向量表 192 B 与承载绝对
    地址的字面量池"）。

    头部里只有 `image_size` / `crc32` 两个字段是**事后回填**的（见
    `apply_header_placeholders`），它们在差分流里以 0xFFFFFFFF 占位；设备侧
    跳过这 8 字节不编程，留到 FLUSH 阶段单独写入（Flash 只能 1→0）。
    """
    img = _fmt()["image_header"]
    fields = img["fields"]
    magic = fields["magic"]["value"]
    search = min(img["search_max"], max(0, len(image) - 4))
    magic_off = None
    for off in range(0, search, img["search_align"]):
        if int.from_bytes(image[off:off + 4], "little") == magic:
            magic_off = off
            break
    if magic_off is None:
        raise ValueError("镜像里找不到头部 magic，请确认是经 patch_crc.py 处理过的 slot 镜像")

    hdr_off = magic_off - fields["magic"]["offset"]
    if hdr_off < 0:
        raise ValueError("头部 magic 出现在偏移 %d，无法容纳 %d 字节的头部"
                         % (magic_off, img["size"]))
    payload_off = hdr_off + img["size"]
    code_size = int.from_bytes(image[hdr_off:hdr_off + 4], "little")
    if code_size == 0xFFFFFFFF:
        raise ValueError("头部 image_size 仍是 0xFFFFFFFF 占位值，说明该文件只是"
                         "linker 产物，尚未经 patch_crc.py 回填")
    if code_size == 0 or payload_off + code_size > len(image):
        raise ValueError("镜像头里的 image_size=%d 不合法" % code_size)

    region = _crc_region(img, hdr_off, code_size)
    ver_off = hdr_off + fields["fw_version"]["offset"]
    return {
        # 差分域：向量表 + 头部 + 代码，首尾连续，没有洞
        "image": image[:payload_off + code_size],
        "code": image[payload_off:payload_off + code_size],
        "code_size": code_size,
        "payload_offset": payload_off,
        "hdr_offset": hdr_off,
        "fw_version": int.from_bytes(image[ver_off:ver_off + 4], "little"),
        "crc32_field": int.from_bytes(
            image[hdr_off + fields["crc32"]["offset"]:
                  hdr_off + fields["crc32"]["offset"] + 4], "little"),
        "crc_region": region,
    }


def _crc_region(img: dict, hdr_off: int, code_size: int) -> Tuple[int, int]:
    """镜像 CRC 覆盖区 `(start, length)`，取值全部来自格式真源。

    起点是 **magic** 而非 payload 起点：`image_size` 与 `crc32` 两个字段被刻意排除，
    这样 CRC 可以在回填这两者**之前**算出，也就与「是否已回填」无关。
    设备侧因此可以在应用完成后先算 CRC、再回填，两者自洽。
    """
    start = hdr_off + img["crc_region"]["start_offset_in_slot"] - img["offset_in_slot"]
    length = code_size + img["crc_region"]["length_addend"]
    return start, length


def image_crc32(image: bytes) -> int:
    """slot 镜像的 CRC32 —— 与引导器 `boot_crc_verify()` 覆盖的是**同一段**。

    于是信封里的 `new_crc32` 可以直接当作要回填进镜像头的 `crc32` 字段值，
    不需要第二套算法或第二份定义。
    """
    parts = split_image(image)
    start, length = parts["crc_region"]
    if start + length > len(image):
        raise ValueError("CRC 覆盖区 [%d,+%d) 超出镜像长度 %d"
                         % (start, length, len(image)))
    return stm32_crc32(image[start:start + length])


def apply_header_placeholders(image: bytes) -> bytes:
    """把镜像头里两个**事后回填**的字段置为 0xFFFFFFFF。

    原因：Flash 只能 1→0。`image_size` 与 `crc32` 要等 payload 全部落盘后才能确定，
    但设备是按页编程的 —— 若差分流已经把真值写进那一页，就没有第二次写入的机会。
    置成 0xFFFFFFFF 后，设备可以「整页编程（该双字留 0xFF）→ 单独补写该双字」。

    这不影响 `new_crc32`：CRC 覆盖区从 magic 开始，本就排除这两个字段，
    所以无论是否占位，算出来的值相同。
    """
    img = _fmt()["image_header"]
    fields = img["fields"]
    parts = split_image(image)
    off = parts["hdr_offset"]
    out = bytearray(image)
    for name in ("image_size", "crc32"):
        o = off + fields[name]["offset"]
        out[o:o + 4] = b"\xff\xff\xff\xff"
    return bytes(out[:parts["payload_offset"] + parts["code_size"]])


def _make_lite(old: bytes, new: bytes, covers: Sequence[Cover],
               compress, dict_size: int) -> bytes:
    """产出 lite 流（头 + 正文），按 `compress` 决定是否压缩。

    `compress` 三态：
      False    → 强制不压（`compress_type=0`，必须始终可用的基线路径）
      True     → 强制压（`compress_type=1`）
      "auto"   → **两者都算，取更小的那个**（默认）

    为什么要有 "auto"：实测（2026-09-16，真实固件对）
        base        89,172 B 新镜像：不压 1,837 B → 压 1,522 B   （压更好）
        modbus_demo 89,988 B 新镜像：不压 3,845 B → 压 3,498 B   （压更好）
        knob_demo   88,876 B 新镜像：不压    70 B → 压    79 B   （**压更差**）
    补丁越小时 tinyuz 的固定开销（4 B dict_size + 控制字）占比越高，会反超收益。
    "auto" 让"默认开压缩"不产生任何回退；设备侧本来就必须同时支持两条路径
    （裁决 2 的 P1' 出口条件），所以两种取值都不增加设备复杂度。
    信封里的 `flags.compressed` 与 lite 头的 `compressType` 由**实际选中的那条**
    决定，两者必然一致（`info()` 也会校验）。
    """
    plain = encode_lite_stream(old, new, covers)
    uncompressed = pack_lite_header(HPI_COMPRESS_TYPE_NO, len(new), 0) + plain
    if compress is False:
        return uncompressed

    from .tinyuz_enc import compress_lite_body
    compressed = (pack_lite_header(HPI_COMPRESS_TYPE_TUZ, len(new), len(plain))
                  + compress_lite_body(plain, dict_size=dict_size))
    if compress is True:
        return compressed
    if compress != "auto":
        raise ValueError("compress 取值应为 False / True / 'auto'，实际 %r" % (compress,))
    return compressed if len(compressed) < len(uncompressed) else uncompressed


def build(old_image: bytes, new_image: bytes, *, fw_version: int,
          compress="auto", block: int = _BLOCK, min_gain: int = _MIN_GAIN,
          dict_size: int = 4096) -> bytes:
    """生成完整补丁（48 B 信封 + lite 流）。

    两个入参是**完整 slot 镜像**（含向量表与头部），不是代码区 —— 理由见
    `split_image()` 的文档：不含向量表会让更新后启动旧固件或静默放弃跳转。

    新旧两侧的处理**刻意不对称**：

    * `old` 原样参与差分 —— 它必须与设备上活动槽的实际字节逐字节一致，
      否则差分器匹配不到，补丁会白白变大甚至错位；
    * `new` 先经 `apply_header_placeholders()` 把 `image_size`/`crc32` 置 0xFFFFFFFF ——
      这两个字段由设备事后回填，先占位才能保证"页内那个双字写入时仍是擦除态"。

    `compress=False` 走 `compress_type=0`，是**必须始终可用**的基线路径
    （规划 §16.6：先用它把 L2 跑绿，再叠加 tinyuz）。

    `dict_size` 是 tinyuz 的回溯窗口上限，也直接决定设备侧 RAM
    （RAM = dict_size + cache_size）。实测（真实固件对，2026-09-16）：

        base         不压 1837 → 1K 1522 · 4K 1522 · 16K 1522
        modbus_demo  不压 3845 → 1K 3546 · 4K 3498 · 16K 3498

    ⇒ **4 KB 已经拿到 16 KB 的全部收益**，1 KB 只差 1.4%。取 4096 可以把
    设备侧 RAM 从 16 KB 降到 4 KB。这与规划 §16.7 的实测结论一致：
    差分流对 dict_size 不敏感。
    """
    # 入参自检：头部里的 crc32 字段必须与按覆盖区算出来的一致。
    # 不一致说明镜像没经 patch_crc.py 回填（或回填用的是另一套算法），
    # 此时生成的补丁在设备侧必然过不了 new_crc32 复核 —— 与其让设备在
    # 擦完页之后才发现，不如在主机侧就拒绝。
    for tag, img in (("old", old_image), ("new", new_image)):
        parts = split_image(img)
        start, length = parts["crc_region"]
        want = stm32_crc32(img[start:start + length])
        if parts["crc32_field"] != want:
            raise ValueError(
                "%s 镜像头部 crc32=0x%08X 与按覆盖区算出的 0x%08X 不符："
                "该镜像未经 patch_crc.py 回填，或回填算法不一致"
                % (tag, parts["crc32_field"], want))

    old = split_image(old_image)["image"]
    new = apply_header_placeholders(new_image)

    covers = diff_to_covers(old, new, block=block, min_gain=min_gain)
    lite = _make_lite(old, new, covers, compress, dict_size)

    # 压缩与否以**实际选中的 lite 头**为准，避免 flags 与内层格式不一致
    is_compressed = lite[2] == HPI_COMPRESS_TYPE_TUZ

    envelope = pack_envelope(
        old_size=len(old),
        old_crc32=image_crc32(old_image),
        new_size=len(new),
        new_crc32=image_crc32(new_image),
        patch_size=len(lite),
        fw_version=fw_version,
        compressed=is_compressed,
    )
    return envelope + lite


def lite_body(patch: bytes) -> bytes:
    """取出补丁里的 lite 流（丢掉 48 B 信封），供 oracle 之类只认 lite 的消费者使用。

    ⚠️ 必须校验长度：`patch_size` 是补丁**自报**的大小，若实到字节更少就是被
    截断。这里不做检查的话，截断的补丁会一路传到解码器，而 HPatchLite 的结束
    校验（`_cache_success_finish()` = 「输入缓存非空」）发现不了尾部缺失 ⇒
    静默产出错误的镜像。这是设备侧同样要守的一条。
    """
    env = parse_envelope(patch)
    off = _env()["size"]
    body = patch[off:off + env["patch_size"]]
    if len(body) != env["patch_size"]:
        raise ValueError("补丁被截断：声明 patch_size=%d，实到 %d 字节（文件共 %d）"
                         % (env["patch_size"], len(body), len(patch)))
    return body


def info(patch: bytes) -> dict:
    """解析补丁，返回信封 + lite 头的合并视图。"""
    env = parse_envelope(patch)
    body = lite_body(patch)

    if len(body) < HPI_K_HEAD_SIZE:
        raise ValueError("lite 流不足 %d 字节" % HPI_K_HEAD_SIZE)
    if body[0:2] != b"hI":
        raise ValueError("lite 流魔数应为 b'hI'，实际 %r" % body[0:2])

    compress_type = body[2]
    b3 = body[3]
    if (b3 >> 6) != HPI_VERSION_CODE:
        raise ValueError("lite 流 versionCode=%d 不受支持" % (b3 >> 6))
    new_w = b3 & 0x7
    unc_w = (b3 >> 3) & 0x7
    pos = HPI_K_HEAD_SIZE
    if pos + new_w + unc_w > len(body):
        raise ValueError("lite 头声明的字段宽度 %d+%d 超出流长度" % (new_w, unc_w))
    lite_new_size = int.from_bytes(body[pos:pos + new_w], "little")
    pos += new_w
    lite_unc = int.from_bytes(body[pos:pos + unc_w], "little") if unc_w else 0
    pos += unc_w

    if lite_new_size != env["new_size"]:
        raise ValueError("lite 头 newSize=%d 与信封 new_size=%d 不符"
                         % (lite_new_size, env["new_size"]))
    if (compress_type != HPI_COMPRESS_TYPE_NO) != env["compressed"]:
        raise ValueError("lite 头 compressType=%d 与信封 compressed=%s 不符"
                         % (compress_type, env["compressed"]))

    env.update({
        "compress_type": compress_type,
        "lite_new_size": lite_new_size,
        "lite_uncompress_size": lite_unc,
        "lite_body_offset": pos,
        "lite_body_size": len(body) - pos,
    })
    return env


def verify_host(old_image: bytes, new_image: bytes, patch: bytes) -> dict:
    """主机侧自洽性检查（**不含** oracle）：信封、lite 头、CRC 必须自相一致。

    这是"补丁与这对 old/new 相配"的必要条件，但不是充分条件 —— 只有 oracle
    逐字节比对才能证明 cover 编码正确。两者都要跑。
    """
    env = info(patch)
    old = split_image(old_image)["image"]
    new = apply_header_placeholders(new_image)
    if env["old_size"] != len(old):
        raise AssertionError("信封 old_size=%d 与实际旧镜像差分域=%d 不符"
                             % (env["old_size"], len(old)))
    if env["old_crc32"] != image_crc32(old_image):
        raise AssertionError("信封 old_crc32 与旧镜像 CRC 不符")
    if env["new_size"] != len(new):
        raise AssertionError("信封 new_size=%d 与实际新镜像差分域=%d 不符"
                             % (env["new_size"], len(new)))
    if env["new_crc32"] != image_crc32(new_image):
        raise AssertionError("信封 new_crc32 与新镜像 CRC 不符")
    return env


# ===========================================================================
# 7. CLI
# ===========================================================================

def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _load_image(path: str) -> Tuple[bytes, str]:
    """读入一个 **slot 镜像** 并取出差分域，返回 (域, 说明)。

    输入必须是**已经 patch_crc.py 回填过头部**的镜像（生成目录里的
    `*_crc.bin`），不能是 linker 刚产出的裸 `.bin`：后者的 `image_size`/`crc32`
    还是 `0xFFFFFFFF` 占位。这不是刁难 —— 差分域与 CRC 都依赖头部里的
    `image_size`，拿占位值去算会得到一份"看起来正常、装上就砖"的补丁。
    所以这里一律 fail-closed，并把该走的那一步写进错误信息。
    """
    data = _read(path)
    try:
        parts = split_image(data)
    except ValueError as exc:
        raise ValueError(
            "%s 不是合法的 slot 镜像：%s\n"
            "（裸 .bin 需先经 generator/patch_crc.py 回填头部 —— 生成流程里的 "
            "*_crc.bin 就是这一步的产物）" % (path, exc)) from exc
    return parts["image"], ("slot 镜像：向量表 %d B + 头部 %d B + 代码 %d B = 差分域 %d B"
                            % (parts["hdr_offset"],
                               parts["payload_offset"] - parts["hdr_offset"],
                               parts["code_size"], len(parts["image"])))


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="差分 OTA 写侧（自研）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="生成差分补丁")
    p_build.add_argument("--old", required=True,
                         help="旧 slot 镜像（必须是 patch_crc.py 回填过的那份）")
    p_build.add_argument("--new", required=True,
                         help="新 slot 镜像（同上；注意新旧应在**不同槽基址**上链接）")
    p_build.add_argument("-o", "--output", required=True)
    p_build.add_argument("--version", type=lambda x: int(x, 0), required=True,
                         help="升级后的固件版本号")
    p_build.add_argument("--compress-mode", choices=("none", "always", "auto"),
                         default="auto",
                         help="none=只用 compress_type=0；always=强制 tinyuz；"
                              "auto(默认)=两条都算、取更小的那个")
    p_build.add_argument("--dict-size", type=int, default=4096,
                         help="tinyuz 回溯窗口上限，直接决定设备侧 RAM 用量")
    p_build.add_argument("--lite-only", action="store_true",
                         help="只输出 lite 流（丢掉信封），供 oracle 使用")

    p_info = sub.add_parser("info", help="查看补丁信息")
    p_info.add_argument("patch")

    args = parser.parse_args(argv)

    if args.cmd == "build":
        mode = {"none": False, "always": True, "auto": "auto"}[args.compress_mode]
        old, old_how = _load_image(args.old)
        new, new_how = _load_image(args.new)
        patch = build(old, new, fw_version=args.version, compress=mode,
                      dict_size=args.dict_size)
        d = verify_host(old, new, patch)
        out = lite_body(patch) if args.lite_only else patch
        with open(args.output, "wb") as fh:
            fh.write(out)
        print("旧镜像: %d B 差分域   (%s)" % (len(old), old_how))
        print("新镜像: %d B 差分域   (%s)" % (len(new), new_how))
        print("补丁:   %d B  (%.2f%% of new)" % (len(patch), len(patch) * 100.0 / max(1, len(new))))
        print("压缩:   %s (compress_type=%d)" % (d["compressed"], d["compress_type"]))
        print("输出:   %s%s" % (args.output, "（lite 流，无信封）" if args.lite_only else ""))
        return 0

    if args.cmd == "info":
        d = info(_read(args.patch))
        for k in ("format_ver", "flags", "compressed", "old_size", "old_crc32",
                  "new_size", "new_crc32", "patch_size", "fw_version", "auth_len",
                  "compress_type", "lite_new_size", "lite_uncompress_size",
                  "lite_body_size"):
            v = d[k]
            print("%-22s %s" % (k, ("0x%08X" % v) if isinstance(v, int) and k.endswith("crc32") else v))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(_main())
