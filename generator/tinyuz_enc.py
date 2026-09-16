#!/usr/bin/env python3
"""tinyuz 压缩器（**自研写侧**，对应 docs/plans/differential-ota.md §16.7 裁决 2）。

为什么自研
----------
裁决 2 决定「`-c-tuz` 默认开」，而 tinyuz 上游只发布了 `decompress/`，压缩器在
`compress/`（C++）。本项目不把 C++ 侧纳入构建，所以这里按**解码器源码**重新实现
一份 Python 压缩器。它只需满足「可被 vendored 解码器还原」，**不必**与上游
`tuz_enc.cpp` 逐字节一致 —— 压缩流格式本身不唯一。

唯一权威判据与 HPatchLite 相同：
    oracle_apply(old, build(old, new, compress=True)) == new   （逐字节）

═══════════════════════════════════════════════════════════════════════════════
格式摘要（逐条对应 decompress/tuz_dec.c 的行号）
═══════════════════════════════════════════════════════════════════════════════

流 = [dict_size: 4 B 小端] + [位/字节交错的主体]

**主体不是纯位流，也不是纯字节流，而是两者交错**，这是本格式最容易写错的地方。
编码器的写入模型（镜像 tuz_enc_private/tuz_enc_code.cpp 的 `TTuzCode`）：

    code 是一个字节数组。位按 LSB-first 攒进「当前位字节」：
        outType(bit):  若 type_count==0 → 在**数组末尾**追加一个 0 字节并记下它的
                       下标 types_index；把 bit 写到 code[types_index] 的第
                       type_count 位；type_count 满 8 归零。
    outDictPos(byte): **直接在数组末尾追加一个裸字节** —— 此时若 type_count!=0，
                       追加位置在 types_index 之后，而后续的 outType 仍会写回
                       code[types_index]。于是数组里出现
                           [ … 位字节(未写满) ][ dictPos 裸字节 ] …
                       这个顺序正是解码器要的：它先把位字节读进 `types` 累加器
                       （一次读满 8 位），用到 dictPos 时再 `_cache_read_1byte`
                       读紧随其后的裸字节，之后继续消费 `types` 里**剩下的高位位**。
    字面量（literalLine 与大段 data）同样以裸字节追加在数组末尾。

    要点：位字节的**高位可以"后写"**，因为解码器是把整个字节一次性读走的 ——
    所以"先写低 3 位、插一个裸字节、再补高 5 位"是合法的。

    ⚠️ 因此**不能**用「先攒位、最后统一字节打包」的常规实现 —— 裸字节的插入
    顺序会与位字节交错，必须完全照抄上面的写入顺序。

控制流（hpatch_lite 那套 + tinyuz 自己的类型位）
    type bit  0 = dict（拷贝/控制），1 = data（1 个字面字节紧随）
    dict 分支：type(0) → outDictLen(len) → [若 isHaveData_back: 1 位 isSamePos]
              → 若 !isSamePos: outDictPos(pos)
    pos == 0 是**控制字**，其 len 取值为 ctrl 类型：
        1 = literalLine（随后是 pos_len 与裸字面量）
        2 = clipEnd（流内分片结束，继续读）
        3 = streamEnd（流结束）
    长度与 pos 的高位都用同一套「分组 + 续接位」变长编码（见 `pack_len`）。

═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

# --- 常量，全部取自 tuz_types_private.h / tuz_types.h ----------------------
K_DICT_SIZE_SAVED_BYTES = 4      # tuz_kDictSizeSavedBytes（因为 kMaxOfDictSize=1<<30）
K_MAX_TYPE_BIT_COUNT = 8         # tuz_kMaxTypeBitCount
K_MIN_LITERAL_LEN = 15           # tuz_kMinLiteralLen
K_MIN_DICT_MATCH_LEN = 2         # tuz_kMinDictMatchLen
K_BIG_POS_FOR_LEN = (1 << 11) + (1 << 9) + (1 << 7) - 1     # 2687

K_CODE_TYPE_DICT = 0
K_CODE_TYPE_DATA = 1

CTRL_LITERAL_LINE = 1
CTRL_CLIP_END = 2
CTRL_STREAM_END = 3

# 设备侧 RAM = dict_size + cache_size，所以 dict_size 必须有硬上限。
# 差分流对 dict 大小不敏感（规划 §16.7 的实测），4 KB 足够。
MAX_DICT_SIZE = 1 << 20
# 实测（真实固件对）：4 KB 已经拿到 16 KB 的全部收益，1 KB 只差 1.4%。
# 这个值直接等于设备侧多占的 RAM，所以取 4096 而不是上游默认的 1 MB。
DEFAULT_DICT_SIZE = 4096


# ===========================================================================
# 1. 变长「长度/pos 高位」编码 —— 与解码器 `_def_unpack_len` 完全互逆
# ===========================================================================

def _group_count(value: int, pack_bit: int) -> Tuple[int, int]:
    """返回 (组数, 需减掉的偏移量 dec)。

    对应 tuz_enc_code.cpp 的 `_getOutCount`：解码是
        v = 0
        loop: low = 读 readBit(=pack_bit+1) 位
              v = (v << pack_bit) + (low & ((1<<pack_bit)-1))
              if not (low & (1<<pack_bit)): return v
              v += 1
    展开后 v = P + dec，其中 dec = Σ_{j=1..count-1} 2^(j*pack_bit)，
    ``P < 2**(count*pack_bit)``。各区间的取值范围首尾相接，故 value≥0 总可表示。
    """
    count = 1
    v = value
    while True:
        m = 1 << (count * pack_bit)
        if v < m:
            break
        v -= m
        count += 1
    return count, value - v


# ===========================================================================
# 2. 编码器状态机（严格镜像 TTuzCode）
# ===========================================================================

class TuzCode:
    """tinyuz 位/字节交错写入器。方法名与上游 TTuzCode 一一对应，便于复核。"""

    def __init__(self, is_need_literal_line: bool = True):
        self.code = bytearray()
        self.is_need_literal_line = is_need_literal_line
        self.types_index = 0
        self.type_count = 0
        self.dict_pos_back = 1
        self.dict_size_max = 1              # _dict_size_max 初值 tuz_kMinOfDictSize
        self.is_have_data_back = False
        self.dict_size_pos: Optional[int] = None

    # --- 基础写入 ---

    def out_type(self, bit: int) -> None:
        """把 1 位写进「当前位字节」，必要时在数组末尾开一个新的位字节。"""
        if self.type_count == 0:
            self.types_index = len(self.code)
            self.code.append(0)
        self.code[self.types_index] |= (bit & 1) << self.type_count
        self.type_count += 1
        if self.type_count == K_MAX_TYPE_BIT_COUNT:
            self.type_count = 0

    def out_raw(self, byte: int) -> None:
        """追加一个**裸字节**（dictPos / 字面量走这条路），不动位累加器。"""
        self.code.append(byte & 0xFF)

    def out_raw_bytes(self, data: bytes) -> None:
        self.code += data

    def out_len(self, value: int, pack_bit: int) -> None:
        """变长长度：pack_bit 位一组，组内低位在前，末尾跟一个续接位。"""
        count, dec = _group_count(value, pack_bit)
        v = value - dec
        c = count
        while c:
            c -= 1
            for i in range(pack_bit):
                self.out_type((v >> (c * pack_bit + i)) & 1)
            self.out_type(1 if c > 0 else 0)

    def out_dict_len(self, value: int) -> None:
        self.out_len(value, 1)

    def out_dict_pos_len(self, value: int) -> None:
        self.out_len(value, 2)

    def out_dict_size(self, dict_size: int) -> None:
        self.dict_size_pos = len(self.code)
        for _ in range(K_DICT_SIZE_SAVED_BYTES):
            self.out_raw(dict_size & 0xFF)
            dict_size >>= 8
        if dict_size:
            raise ValueError("dict_size 超出 %d 字节能表示的范围" % K_DICT_SIZE_SAVED_BYTES)

    def out_dict_pos(self, pos: int) -> None:
        """dict_pos 是「距离 - 1」。<128 一个字节；否则低 7 位带标记 + pos_len 高位。"""
        is_out_len = 1 if pos >= (1 << 7) else 0
        if is_out_len:
            pos -= (1 << 7)
        self.out_raw((pos & ((1 << 7) - 1)) | (is_out_len << 7))
        if is_out_len:
            self.out_dict_pos_len(pos >> 7)

    # --- 语义写入 ---

    def out_ctrl(self, ctrl: int) -> None:
        self.out_type(K_CODE_TYPE_DICT)
        self.out_dict_len(ctrl)
        if self.is_have_data_back:
            self.out_type(0)
        self.out_dict_pos(0)               # dict_pos==0 表示控制字

    def out_ctrl_types_end(self) -> None:
        """解码器在 ctrl 处会丢弃位累加器的残留位（tuz_dec.c:250/400）。"""
        self.type_count = 0
        self.dict_pos_back = 1
        self.is_have_data_back = False

    def out_data(self, data: bytes) -> None:
        """字面量。≥15 字节走 literalLine（一次控制 + 一次长度 + 裸字节），
        否则逐字节「1 位 type + 1 裸字节」。"""
        n = len(data)
        if n == 0:
            return
        if self.is_need_literal_line and n >= K_MIN_LITERAL_LEN:
            self.out_ctrl(CTRL_LITERAL_LINE)
            self.out_dict_pos_len(n - K_MIN_LITERAL_LEN)
            self.out_raw_bytes(data)
        else:
            for b in data:
                self.out_type(K_CODE_TYPE_DATA)
                self.out_raw(b)
        self.is_have_data_back = True

    def out_dict(self, match_len: int, dict_pos: int) -> None:
        """一段「从 dict 拷贝 match_len 字节，距离 = dict_pos + 1」。"""
        if match_len < K_MIN_DICT_MATCH_LEN:
            raise ValueError("match_len=%d 小于 %d" % (match_len, K_MIN_DICT_MATCH_LEN))

        self.out_type(K_CODE_TYPE_DICT)
        saved_dict_pos = dict_pos + 1                  # 0 留给控制字
        if saved_dict_pos > self.dict_size_max:
            self.dict_size_max = saved_dict_pos

        is_same_pos = 1 if self.dict_pos_back == saved_dict_pos else 0
        is_saved_same_pos = 1 if (is_same_pos and self.is_have_data_back) else 0

        ln = match_len - K_MIN_DICT_MATCH_LEN
        if not is_saved_same_pos and saved_dict_pos > K_BIG_POS_FOR_LEN:
            # 解码器会把这个 1 加回来；长度 2 的匹配在大位移下无从编码
            if match_len < K_MIN_DICT_MATCH_LEN + 1:
                raise ValueError("大位移下 match_len=%d 无法编码" % match_len)
            ln -= 1

        self.out_dict_len(ln)
        if self.is_have_data_back:
            self.out_type(is_saved_same_pos)
        if not is_saved_same_pos:
            self.out_dict_pos(saved_dict_pos)

        self.is_have_data_back = False
        self.dict_pos_back = saved_dict_pos

    def out_ctrl_stream_end(self) -> None:
        self.out_ctrl(CTRL_STREAM_END)
        self.out_ctrl_types_end()


# ===========================================================================
# 3. 匹配查找（LZ77，允许重叠匹配）
# ===========================================================================

_MIN_MATCH = 4              # 比 tuz_kMinDictMatchLen 大，减少控制开销
_HASH_BITS = 15
_HASH_SIZE = 1 << _HASH_BITS
_CHAIN_DEPTH = 24           # 每个位置的候选上限，界定最坏耗时


def _hash4(data: bytes, pos: int) -> int:
    v = data[pos] | (data[pos + 1] << 8) | (data[pos + 2] << 16) | (data[pos + 3] << 24)
    return ((v * 2654435761) & 0xFFFFFFFF) >> (32 - _HASH_BITS)


def compress_body(data: bytes, *, dict_size: int = DEFAULT_DICT_SIZE,
                  is_need_literal_line: bool = True) -> bytes:
    """把 `data` 压成 tinyuz 流。

    ⚠️ `is_need_literal_line` 必须与**设备侧解码器**的 `tuz_isNeedLiteralLine`
    编译开关一致。上游 README 的警告正是这一点：设成 0 还按 1 编码（或反之）
    得到的不是"压缩率变差"，而是**流无法解码**。这里默认 1（= 上游默认）。
    """
    if dict_size < 1 or dict_size > MAX_DICT_SIZE:
        raise ValueError("dict_size=%d 超出 [1, %d]" % (dict_size, MAX_DICT_SIZE))

    code = TuzCode(is_need_literal_line=is_need_literal_line)
    n = len(data)

    if n == 0:
        code.out_dict_size(1)               # dict_size 必须 >0，取最小值
        code.out_ctrl_stream_end()
        return _with_header_first(code)

    head = [-1] * _HASH_SIZE
    prev = [-1] * n

    def insert(pos: int) -> None:
        h = _hash4(data, pos)
        prev[pos] = head[h]
        head[h] = pos

    def find_best(pos: int) -> Tuple[int, int]:
        """返回 (match_len, distance)；找不到返回 (0, 0)。"""
        if pos + _MIN_MATCH > n:
            return 0, 0
        limit = min(dict_size, pos)          # 距离不得超出已产出字节数，也不得超 dict
        if limit < 1:
            return 0, 0
        h = _hash4(data, pos)
        cand = head[h]
        best_len, best_dist = 0, 0
        depth = _CHAIN_DEPTH
        while cand >= 0 and depth:
            dist = pos - cand
            if dist > limit:
                break                        # 链是按位置递减的，再往前只会更远
            # 先比首字节能挡掉绝大多数候选
            if data[cand] == data[pos]:
                max_len = n - pos
                ln = 0
                while ln < max_len and data[cand + ln] == data[pos + ln]:
                    ln += 1
                if ln > best_len:
                    best_len, best_dist = ln, dist
            cand = prev[cand]
            depth -= 1
        if best_len < _MIN_MATCH:
            return 0, 0
        return best_len, best_dist

    lit_start = 0
    pos = 0
    while pos < n:
        if pos + _MIN_MATCH <= n:
            mlen, dist = find_best(pos)
        else:
            mlen, dist = 0, 0

        if mlen:
            if pos > lit_start:
                code.out_data(data[lit_start:pos])
            code.out_dict(mlen, dist - 1)
            for k in range(pos, min(pos + mlen, n - _MIN_MATCH + 1)):
                insert(k)
            pos += mlen
            lit_start = pos
        else:
            if pos + _MIN_MATCH <= n:
                insert(pos)
            pos += 1

    if lit_start < n:
        code.out_data(data[lit_start:n])

    # dict_size 必须 ≥ 实际用到的最大距离；用 1 兜底（解码器拒绝 dict_size==0）
    code.out_dict_size(max(1, code.dict_size_max))
    code.out_ctrl_stream_end()
    return _with_header_first(code)


def _with_header_first(code: "TuzCode") -> bytes:
    """把 `out_dict_size()` 写下的 4 字节挪到流首。

    上游的做法是「先整体压缩到内存 → 拿到 getCurDictSizeMax() → 从头部输出」。
    这里等价处理：`out_dict_size()` 用 `out_raw` 追加，落点可能在已写内容之间
    （因为它是 `_dict_size_max` 的**结果**，只能等压完才知道），所以压完后把它
    摘出来前置。其余字节的相对顺序保持不变。
    """
    raw = bytearray(code.code)
    if code.dict_size_pos is None:
        raise AssertionError("out_dict_size() 未被调用，流里没有 dict_size")
    p = code.dict_size_pos
    hdr = bytes(raw[p:p + K_DICT_SIZE_SAVED_BYTES])
    if len(hdr) != K_DICT_SIZE_SAVED_BYTES:
        raise AssertionError("dict_size 字段不完整")
    del raw[p:p + K_DICT_SIZE_SAVED_BYTES]
    return hdr + bytes(raw)


def compress_lite_body(plain: bytes, *, dict_size: int = DEFAULT_DICT_SIZE) -> bytes:
    """`delta_tool` 的接入点：把 lite 补丁正文压成 tinyuz 流。"""
    return compress_body(plain, dict_size=dict_size, is_need_literal_line=True)
