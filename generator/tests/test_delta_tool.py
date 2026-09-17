"""差分 OTA 写侧（`generator/delta_tool.py`）的测试。

分两层，缺一不可
----------------
**L1 结构层**（任何机器都跑）：信封 / lite 头的自洽性、CRC 覆盖、fail-closed
边界。它快，但**证明不了 cover 编码正确** —— 它只检查我们写出来的字节
「格式上像不像」，不检查解码器能否还原出新镜像。

**L2 oracle 层**（有主机 C 编译器时跑）：把 `build()` 的产物交给
**vendored 的 HPatchLite 解码器**去消费，判据是逐字节相等：

    oracle_apply(old, build(old, new)) == new

这是唯一权威判据。为什么必须有它：HPatchLite 没有格式规范文档，只有解码器
源码；而「自己写的编码器 + 自己写的解码器」互相验证是**同源错误**的重灾区
（这正是 A9「mock 让测试失效」的教训）。所以 L2 绝不能用「Python 再实现一遍
解码器」来替代。

⚠️ L2 不能被静默跳过。若本机有编译器但 oracle 编译失败，`_build_oracle()`
会抛异常而不是返回 None ⇒ 测试**失败**而不是跳过。没有编译器时才 skip，
且 skip 的理由必须写明「L2 判据不可用」。
"""

import functools
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORACLE_SRC = _REPO_ROOT / "tools" / "hpatch_oracle" / "oracle_apply.c"
_HPATCH_DIR = _REPO_ROOT / "static" / "third_party" / "hpatch_lite"
_TUZ_DIR = _REPO_ROOT / "static" / "third_party" / "tinyuz" / "decompress"
_ORACLE_OUT = _REPO_ROOT / "build" / "oracle"

sys.path.insert(0, str(_REPO_ROOT))

from generator.delta_tool import (  # noqa: E402
    HPI_COMPRESS_TYPE_NO,
    HPI_VERSION_CODE,
    apply_header_placeholders,
    build,
    crc16_ccitt_false,
    diff_to_covers,
    encode_lite_stream,
    image_crc32,
    info,
    lite_body,
    pack_lite_header,
    pack_oldpos_delta,
    pack_uvarint,
    parse_envelope,
    split_image,
    verify_host,
)
from generator.tests.delta_fixtures import (  # noqa: E402
    SLOT_A_BASE,
    SLOT_B_BASE,
    VECTOR_BYTES,
    expected_target,
    finalize_image,
    firmware_like as _firmware_like,
    slot_image,
)
from generator.patch_crc import load_spec  # noqa: E402


def _posix(p) -> str:
    """给 Windows 原生程序传路径必须用 C:/ 形式（`/c/...` 会被当成非法路径）。"""
    return str(p).replace("\\", "/")


# ---------------------------------------------------------------------------
# oracle 构建（L2 的前置）
# ---------------------------------------------------------------------------

def _find_cc():
    for cand in (
        os.environ.get("CC"),
        shutil.which("cc"),
        shutil.which("gcc"),
        shutil.which("clang"),
        "C:/mingw64/bin/gcc.exe",
    ):
        if cand and Path(cand).exists():
            return cand
    return None


@functools.lru_cache(maxsize=4)
def _build_oracle(with_tuz: bool):
    """编译 oracle。返回可执行文件路径；**没有编译器**时返回 None。

    「没有编译器」（环境问题 ⇒ 可跳过）与「编译失败」（缺陷 ⇒ 必须失败）
    是两件事，这里刻意用「抛异常」区分开。
    """
    cc = _find_cc()
    if cc is None:
        return None

    _ORACLE_OUT.mkdir(parents=True, exist_ok=True)
    exe = _ORACLE_OUT / ("oracle_apply%s.exe" % ("_tuz" if with_tuz else ""))

    # oracle_apply.c 只提供 I/O 回调，解码器本体必须一起编进来
    cmd = [cc, "-O1", "-std=c99", "-Wall", "-Wextra", "-o", _posix(exe),
           _posix(_ORACLE_SRC), _posix(_HPATCH_DIR / "hpatch_lite.c"),
           "-I", _posix(_HPATCH_DIR)]
    if with_tuz:
        cmd += ["-D_ORACLE_WITH_TUZ", "-I", _posix(_TUZ_DIR),
                _posix(_TUZ_DIR / "tuz_dec.c")]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "oracle 编译失败（这是缺陷，不是环境问题）:\n%s\n%s"
            % (" ".join(cmd), proc.stderr))
    return exe


def _oracle_or_skip(with_tuz: bool = False) -> Path:
    exe = _build_oracle(with_tuz)
    if exe is None:
        pytest.skip("本机没有主机 C 编译器，无法构建 oracle（L2 权威判据不可用）")
    return exe


def apply_oracle(old: bytes, patch: bytes, tmp_path: Path,
                 with_tuz: bool = False):
    """驱动 vendored 解码器，返回 (输出字节, 统计字典)。`patch` 是完整补丁。"""
    exe = _oracle_or_skip(with_tuz)
    old_f = tmp_path / "old.bin"
    pat_f = tmp_path / "patch.lite"
    out_f = tmp_path / "out.bin"
    old_f.write_bytes(old)
    pat_f.write_bytes(lite_body(patch))

    proc = subprocess.run([_posix(exe), _posix(old_f), _posix(pat_f), _posix(out_f)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, (
        "oracle 应用失败（rc=%d）\nstdout: %s\nstderr: %s"
        % (proc.returncode, proc.stdout, proc.stderr))

    stats = {}
    for line in proc.stdout.splitlines():
        if line.startswith("ORACLE_STAT"):
            for kv in line.split()[1:]:
                k, _, v = kv.partition("=")
                stats[k] = int(v)
    assert stats, "oracle 没有输出 ORACLE_STAT 统计行，测试无法据此断言: %r" % proc.stdout
    return out_f.read_bytes(), stats


def assert_roundtrip(old_code: bytes, new_code: bytes, tmp_path: Path, *,
                     version: int = 3, compress=False) -> bytes:
    """核心判据：old + patch 必须逐字节还原出 new。

    入参是**代码区**（不是整镜像）。差分域是整个 slot 镜像，所以这里先把两段
    代码包成合法的 slot 镜像 —— 且刻意放在**不同的槽基址**上（A→B），因为真实
    的 A/B 更新正是这样：两个槽的向量表与绝对地址都不同，这是差分必须吸收的
    差异。若夹具把两侧生成得一样，就测不出这一类问题。

    比对基准是 `expected_target(new)` 而不是 `new`：头部里 `image_size`/`crc32`
    两个字段由设备事后回填，差分流里恒为 0xFFFFFFFF（见
    `delta_tool.apply_header_placeholders`）。真值另由单独用例断言。
    """
    old = slot_image(old_code, fw_version=version - 1, slot_base=SLOT_A_BASE)
    new = slot_image(new_code, fw_version=version, slot_base=SLOT_B_BASE)
    patch = build(old, new, fw_version=version, compress=compress)
    verify_host(old, new, patch)                       # L1
    got, _ = apply_oracle(old, patch, tmp_path,
                          with_tuz=(compress is not False))   # L2
    assert got == expected_target(new), (
        "oracle 还原结果与期望镜像不一致：len %d vs %d" % (len(got), len(expected_target(new))))
    return patch


# ---------------------------------------------------------------------------
# 0. oracle 自身可用性（防止 skip 掩盖编译失败）
# ---------------------------------------------------------------------------

def test_oracle_builds_and_is_self_evidently_functional():
    """有编译器时 oracle 必须编得出来，并且真的能干活（不是空跑）。"""
    if _find_cc() is None:
        pytest.skip("本机没有主机 C 编译器")
    exe = _build_oracle(False)
    assert exe is not None and exe.exists()

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        old = slot_image(bytes(range(256)) * 4)
        patch = build(old, old, fw_version=1, compress=False)
        got, stats = apply_oracle(old, patch, td)
        assert got == expected_target(old), "oracle 连恒等补丁都还原不了 —— 判据本身失效"
        assert stats.get("tuz_calls", 0) == 0, "未压缩路径不应触发解压"


# ---------------------------------------------------------------------------
# 1. L1：编码原语
# ---------------------------------------------------------------------------

def _decode_uvarint(buf: bytes, pos: int) -> tuple:
    """按解码器 `_cache_unpackUInt(&d, 0, 1)` 的语义解码（hpatch_lite.c:53）。"""
    v = 0
    is_next = 1
    while is_next:
        b = buf[pos]
        pos += 1
        v = (v << 7) | (b & 127)
        is_next = b >> 7
    return v, pos


@pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 16383, 16384,
                                   0xFFFF, 0x1FFFFF, 0x7FFFFFFF])
def test_uvarint_roundtrips_through_decoder_semantics(value):
    out = pack_uvarint(value)
    assert _decode_uvarint(out, 0) == (value, len(out))
    assert out[-1] & 0x80 == 0, "末字节不应带续接位"
    assert all(b & 0x80 for b in out[:-1]), "中间字节必须带续接位"


def _decode_oldpos(tag: int, ext: bytes) -> int:
    """按 hpatch_lite.c:166 解码 oldPos 增量的绝对值。"""
    v = tag & 31
    is_next = tag & (1 << 5)
    pos = 0
    while is_next:
        b = ext[pos]
        pos += 1
        v = (v << 7) | (b & 127)
        is_next = b >> 7
    assert pos == len(ext), "oldPos 续接字节数不匹配"
    return v


@pytest.mark.parametrize("delta", [0, 1, 31, 32, 33, 4095, 4096, 0x3FFFFF, 0x7FFFFFF])
def test_oldpos_delta_roundtrips(delta):
    tag5, ext = pack_oldpos_delta(delta)
    assert tag5 < 32
    tag = tag5 | ((1 << 5) if ext else 0)      # bit5 恰好表示「有续接字节」
    assert _decode_oldpos(tag, ext) == delta


def test_lite_header_magic_is_asymmetric_hI():
    """魔数是 'h' 小写 + 'I' 大写。写成 "HI" 会让解码器直接拒绝整个流。"""
    hdr = pack_lite_header(HPI_COMPRESS_TYPE_NO, 100)
    assert hdr[0:2] == b"hI"
    assert hdr[0] == ord("h") and hdr[1] == ord("I")


@pytest.mark.parametrize("new_size", [0, 1, 255, 256, 65535, 65536, 0xFFFFFF])
def test_lite_header_encodes_new_size_per_decoder(new_size):
    hdr = pack_lite_header(HPI_COMPRESS_TYPE_NO, new_size)
    b3 = hdr[3]
    assert (b3 >> 6) == HPI_VERSION_CODE
    new_w = b3 & 7
    assert (b3 >> 3) & 7 == 0, "未压缩路径不应带 uncompressSize 字段"
    assert int.from_bytes(hdr[4:4 + new_w], "little") == new_size


def test_lite_header_carries_uncompress_size_when_compressed():
    hdr = pack_lite_header(1, 0x1234, 0x5678)
    new_w = hdr[3] & 7
    unc_w = (hdr[3] >> 3) & 7
    pos = 4
    assert int.from_bytes(hdr[pos:pos + new_w], "little") == 0x1234
    pos += new_w
    assert int.from_bytes(hdr[pos:pos + unc_w], "little") == 0x5678
    assert len(hdr) == 4 + new_w + unc_w


# ---------------------------------------------------------------------------
# 2. L1：信封
# ---------------------------------------------------------------------------

def test_crc16_ccitt_false_check_value():
    assert crc16_ccitt_false(b"123456789") == 0x29B1


def _mk(old_code: bytes, new_code: bytes, ver: int = 7):
    """把两段代码包成 A→B 的一对 slot 镜像（L1 用例的通用入口）。"""
    return (slot_image(old_code, fw_version=ver - 1, slot_base=SLOT_A_BASE),
            slot_image(new_code, fw_version=ver, slot_base=SLOT_B_BASE))


def _l2_pair(n: int, seed: int):
    """造一对 A→B 镜像：新代码 = 旧代码前 (n-500) 字节 + 一段新尾部。"""
    old_code = _firmware_like(n, seed=seed)
    new_code = old_code[:n - 500] + b"TAIL" * 10
    return (slot_image(old_code, fw_version=1, slot_base=SLOT_A_BASE),
            slot_image(new_code, fw_version=2, slot_base=SLOT_B_BASE))


def test_envelope_roundtrip_and_field_placement():
    old, new = _mk(b"payload-old", b"payload-new!!")
    patch = build(old, new, fw_version=0x123456)
    env = parse_envelope(patch)
    assert env["fw_version"] == 0x123456
    assert env["old_size"] == len(old), "old_size 是**差分域**长度（含向量表与头部）"
    assert env["new_size"] == len(new)
    assert env["compressed"] is False
    assert env["auth_len"] == 0
    assert env["patch_size"] == len(patch) - 48


def test_envelope_version_is_covered_by_header_crc():
    """篡改 fw_version 必须让信封头 CRC 失败 —— 否则版本号可被伪造。"""
    patch = bytearray(build(*_mk(b"a" * 32, b"b" * 32), fw_version=7))
    patch[28] ^= 0x01                      # fw_version 位于偏移 28
    with pytest.raises(ValueError, match="CRC16"):
        parse_envelope(bytes(patch))


def test_envelope_rejects_unknown_flags():
    patch = bytearray(build(*_mk(b"a" * 32, b"b" * 32), fw_version=7))
    patch[6] |= 0x80                       # flags 位于偏移 6
    struct.pack_into("<H", patch, 32, crc16_ccitt_false(bytes(patch[:32])))
    with pytest.raises(ValueError, match="未知位"):
        parse_envelope(bytes(patch))


def test_info_rejects_truncated_or_short_patch():
    patch = build(*_mk(b"x" * 64, b"y" * 64), fw_version=1)
    with pytest.raises(ValueError):
        info(patch[:20])                   # 装不下信封
    with pytest.raises(ValueError, match="截断"):
        info(patch[:-1])                   # patch_size 声明与实到不符
    with pytest.raises(ValueError, match="截断"):
        lite_body(patch[:-1])


def test_info_rejects_wrong_lite_magic():
    patch = bytearray(build(*_mk(b"x" * 64, b"y" * 64), fw_version=1))
    patch[48] = ord("H")                   # 把 'h' 改成 'H'
    with pytest.raises(ValueError, match="hI"):
        info(bytes(patch))


def test_verify_host_rejects_mismatched_old():
    """旧镜像不匹配必须以两种方式被拒：

    * 结构上不成立（长度与头部矛盾）→ `split_image` 直接拒绝；
    * 结构成立但内容不对（例如拿了另一个槽的镜像）→ 信封字段自检失败。

    第二种才是 OTA 现场最容易犯的错：把"为 Slot B 链接的镜像"当成设备上
    Slot A 的那份。此时补丁"看起来生成成功"，装上却对不上。
    """
    old, new = _mk(b"x" * 64, b"y" * 64)
    patch = build(old, new, fw_version=1)
    verify_host(old, new, patch)

    with pytest.raises(ValueError):
        verify_host(old[:-1], new, patch)           # 结构不成立

    other_slot = slot_image(b"x" * 64, fw_version=0, slot_base=SLOT_B_BASE)
    assert other_slot != old, "两槽镜像本该不同（向量表里的绝对地址不同）"
    with pytest.raises(AssertionError):
        verify_host(other_slot, new, patch)         # 结构成立但内容不对


def test_build_is_deterministic():
    """同一对输入必须产出逐字节相同的补丁（可复现构建）。"""
    old, new = _mk(b"hello" * 40, b"hello" * 39 + b"world", ver=5)
    assert build(old, new, fw_version=5) == build(old, new, fw_version=5)


# ---------------------------------------------------------------------------
# 3. L1：差分器与流组装的结构不变量
# ---------------------------------------------------------------------------

def test_covers_are_monotonic_and_in_bounds():
    rnd = random.Random(7)
    old = bytes(rnd.getrandbits(8) for _ in range(4096))
    new = bytearray(old)
    new[100:140] = b"\xAA" * 40
    new[2000:2000] = b"INSERT" * 4
    new = bytes(new)

    back_new = 0
    for c in diff_to_covers(old, new):
        assert c.new_pos >= back_new, "new_pos 必须单调不减"
        assert c.old_pos + c.length <= len(old), "不得越界读旧数据"
        assert c.length > 0, "差分器不应产出零长度 cover"
        assert c.diff == b"" or len(c.diff) == c.length
        assert c.copy == (c.diff == b"")
        back_new = c.new_pos + c.length


def _walk_stream(body: bytes):
    """按解码器语义走一遍正文，返回每个 cover 的 (length, tag, gap)。"""
    count, pos = _decode_uvarint(body, 0)
    out = []
    for _ in range(count):
        ln, pos = _decode_uvarint(body, pos)
        tag = body[pos]
        pos += 1
        is_next = tag & (1 << 5)
        while is_next:
            b = body[pos]
            pos += 1
            is_next = b >> 7
        gap, pos = _decode_uvarint(body, pos)
        pos += gap                                  # 间隙字面量
        if not (tag >> 7):
            pos += ln                               # diff 字面量
        out.append((ln, tag, gap))
    return count, out, pos


def test_encode_lite_stream_appends_zero_length_tail_cover():
    """new 以「间隙」结尾时，必须补一个零长度 cover 来承载尾部字面量。

    否则解码器走完 coverCount 后 newPosBack < newSize，末尾断言失败。
    这对应解码器 :183 的 `_SAFE_CHECK((cover_length>0)|(coverCount==0))`：
    零长度 cover 只允许出现在最后。

    这里刻意构造「尾部 24 字节不与 old 共享任何块」的最小场景，好让 gap
    恰好等于那 24 字节；若尾部之外还有未匹配区，gap 会合并成一段（见下一
    条测试用不变量覆盖那种情况）。
    """
    old = b"ABCDEFGHIJ" * 20                                      # 200 B
    new = old + bytes(range(1, 25))                               # + 24 B 全新
    body = encode_lite_stream(old, new, diff_to_covers(old, new))
    count, covers, end = _walk_stream(body)

    assert count == len(covers) == 2, "应恰好是「1 个真 cover + 1 个收尾 cover」"
    assert covers[0][0] == 200, "首个 cover 应吃掉全部 200 字节相同前缀"
    assert covers[-1][0] == 0, "末 cover 必须为零长度"
    assert covers[-1][2] == 24, "零长度 cover 前应恰好是那 24 字节尾部字面量"
    assert end == len(body), "正文尾部不应有多余字节"


def test_encode_lite_stream_consumes_entire_new_exactly_once():
    """不变量：沿流走一遍，newPos 的累计前进量必须恰好等于 len(new)。

    这条比「断言某个具体 gap」更耐改：无论差分器把多少字节划成字面量，
    「字面量 + cover 长度」的总和都必须精确覆盖新镜像，不多不少。
    """
    old = b"\x00" * 32 + b"\x11" * 100
    new = b"\x00" * 32 + b"\x22" * 100 + b"\x99" * 24      # 只有前缀 32 B 能匹配
    body = encode_lite_stream(old, new, diff_to_covers(old, new))
    count, covers, end = _walk_stream(body)

    assert count == len(covers)
    assert covers[-1][0] == 0, "末 cover 必须为零长度（承载收尾字面量）"

    advanced = sum(gap + ln for ln, _tag, gap in covers)
    assert advanced == len(new), (
        "流只覆盖了 %d 字节，new 有 %d 字节" % (advanced, len(new)))
    assert end == len(body)


def test_encode_lite_stream_has_cover_even_when_nothing_matches():
    """new 与 old 毫无共同块时仍必须至少有 1 个 cover。

    字面量只能通过 `newPosBack < cover_newPos` 写出，所以
    「coverCount==0 且 new 非空」是**不可表示**的状态 —— 早期实现正是在这里
    产出了解码器无法消费的流。
    """
    old = b"\x00" * 64
    new = b"\xff" * 64
    covers = diff_to_covers(old, new)
    assert covers == [], "本用例前提是差分器找不到任何匹配"

    body = encode_lite_stream(old, new, covers)
    count, walked, end = _walk_stream(body)
    assert count == 1, "必须补出一个零长度 cover 来承载全部 64 字节字面量"
    assert walked[0][0] == 0 and walked[0][2] == 64
    assert end == len(body)


def test_split_image_extracts_the_delta_domain_not_just_the_code():
    """差分域必须**含向量表**，且版本号要读对。

    这是实施中修正的一个真实缺陷：若差分域只取代码区，目标槽 `[0, 0xC0)` 的
    向量表就没人写，更新后会启动旧固件或静默放弃跳转（详见 split_image 文档）。
    """
    code = bytes((i * 5 + 1) & 0xFF for i in range(256))
    blob = slot_image(code, fw_version=42, slot_base=SLOT_A_BASE)

    got = split_image(blob)
    assert got["code"] == code
    assert got["code_size"] == len(code)
    assert got["fw_version"] == 42
    # 关键断言：差分域从**偏移 0** 开始（含向量表），而不是从代码区开始
    assert got["image"][:VECTOR_BYTES] == blob[:VECTOR_BYTES]
    assert len(got["image"]) == len(blob), "差分域应覆盖到代码区末尾、且不含尾部填充"

    # 尾部填充不参与差分
    padded = blob + b"\xff" * 64
    assert split_image(padded)["image"] == got["image"]

    with pytest.raises(ValueError, match="magic"):
        split_image(b"\x00" * 512)

    # linker 刚产出的裸 .bin：magic 在，但 image_size 还是 0xFFFFFFFF 占位
    raw = bytearray(blob)
    img = load_spec()["image_header"]
    raw[img["offset_in_slot"]:img["offset_in_slot"] + 4] = b"\xff" * 4
    with pytest.raises(ValueError, match="占位"):
        split_image(bytes(raw))


# ---------------------------------------------------------------------------
# 4. L2：oracle 逐字节判据（未压缩基线路径）
# ---------------------------------------------------------------------------

# `_firmware_like` 现在来自 delta_fixtures（它同时提供 slot 镜像夹具）。


def test_roundtrip_identical(tmp_path):
    data = _firmware_like(4096)
    assert_roundtrip(data, data, tmp_path)


def test_roundtrip_completely_different(tmp_path):
    """零匹配 —— 覆盖 coverCount==0 的收尾路径。"""
    patch = assert_roundtrip(bytes([0x00]) * 512, bytes([0xFF]) * 512, tmp_path)
    # 上界 = 信封 + lite 头 + 新镜像全部字面量。镜像比代码区多出向量表(192B)+头部(16B)，
    # 而两个槽的向量表本就不同，所以这部分也必然是字面量 —— 必须算进去。
    ceiling = 48 + 16 + (VECTOR_BYTES + 16 + 512)
    assert len(patch) <= ceiling, "零匹配时补丁不应超过「信封+头+全部字面量」"


def test_roundtrip_insertion_shifts_tail(tmp_path):
    """中段插入会移动其后所有字节 —— 差分器必须改用位移后的 old 位置。"""
    old = _firmware_like(3000, seed=1)
    new = old[:1000] + b"NEWLY-INSERTED-BLOCK" * 3 + old[1000:]
    patch = assert_roundtrip(old, new, tmp_path)
    assert len(patch) < len(new) // 4, "插入型改动应被高度压缩"


def test_roundtrip_deletion(tmp_path):
    old = _firmware_like(3000, seed=2)
    new = old[:1000] + old[1500:]
    patch = assert_roundtrip(old, new, tmp_path)
    assert len(patch) < len(new) // 4


def test_roundtrip_tail_gap_and_head_gap(tmp_path):
    """尾部/头部字面量 —— 分别覆盖「需要收尾 cover」与「首个 cover 有前导间隙」。"""
    old = _firmware_like(2000, seed=3)
    body = old[500:1500]

    assert_roundtrip(old, b"PREFIX-ONLY-IN-NEW" * 2 + body, tmp_path)      # 头部间隙
    assert_roundtrip(old, body + b"SUFFIX-ONLY-IN-NEW" * 2, tmp_path)      # 尾部间隙


def test_roundtrip_uses_backward_oldpos_jump(tmp_path):
    """构造一个必须向**前**回跳 oldPos 的新镜像，覆盖负数增量编码。"""
    block = _firmware_like(400, seed=4)
    old = block + _firmware_like(400, seed=5) + block       # 同一块出现两次
    new = block + b"x" + block                              # 第二处指向更早的位置
    assert_roundtrip(old, new, tmp_path)


def test_roundtrip_block_aligned_and_short_payloads(tmp_path):
    """比匹配窗口还短的代码区 —— 差分器应当退化为全字面量。

    注：代码区长度 0 是**非法镜像**（`split_image` 明确拒绝），所以这里用 1 字节
    作下界。这不是遗漏：真实固件不可能没有代码。
    """
    assert_roundtrip(b"\x01", b"\x01", tmp_path)
    assert_roundtrip(b"\x01", b"short-new", tmp_path)
    assert_roundtrip(b"short-old", b"\x02", tmp_path)
    assert_roundtrip(b"abcdefgh", b"abcdefgh", tmp_path)          # 恰短于 block(12)
    assert_roundtrip(b"abcdefgh", b"abcdZZZZ", tmp_path)


def test_roundtrip_version_is_masked_into_envelope(tmp_path):
    patch = assert_roundtrip(b"a" * 64, b"a" * 63 + b"b", tmp_path, version=0xABCDEF)
    assert parse_envelope(patch)["fw_version"] == 0xABCDEF


@pytest.mark.parametrize("seed", range(6))
def test_roundtrip_fuzz_random_mutations(tmp_path, seed):
    """随机变异模糊测试：随机改几处、随机插入、随机截断。"""
    rnd = random.Random(1000 + seed)
    old = _firmware_like(8000, seed=seed)
    new = bytearray(old)

    for _ in range(rnd.randint(1, 6)):
        op = rnd.choice(("flip", "insert", "delete", "block"))
        pos = rnd.randrange(0, max(1, len(new)))
        if op == "flip" and pos < len(new):
            new[pos] = (new[pos] + rnd.randint(1, 255)) & 0xFF
        elif op == "insert":
            new[pos:pos] = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(1, 64)))
        elif op == "delete" and pos < len(new):
            del new[pos:pos + rnd.randint(1, 64)]
        elif op == "block" and pos < len(new):
            new[pos:pos] = b"\x5A" * rnd.randint(12, 200)

    new = bytes(new)
    assert_roundtrip(old, new, tmp_path)


def test_roundtrip_rejects_truncated_patch(tmp_path):
    """截断必须被**信封层**拦下，而不是指望解码器。

    HPatchLite 的结束校验 `_cache_success_finish()` = 「输入缓存非空」，
    它在解码器不需要更多字节时什么也发现不了。真正的第一道防线是
    `patch_size`：设备在擦任何一页之前先核对「实到字节数 == 声明字节数」。
    """
    old, new = _l2_pair(2000, seed=9)
    patch = build(old, new, fw_version=1)
    verify_host(old, new, patch)

    with pytest.raises(ValueError, match="截断"):
        lite_body(patch[:-8])


# ---------------------------------------------------------------------------
# 5. L2：解码器**做不到**什么（信封为什么必须存在）
#
# 这一组是实测记录，不是猜测。它们证明了「48 B 信封 + new_crc32」不是冗余，
# 而是整条链路唯一的内容完整性判据。规划 §16.4 据此把信封从"可选"升为"必须"。
# ---------------------------------------------------------------------------

def _run_oracle_raw(old: bytes, body: bytes, tmp_path: Path):
    """直接喂一段 lite 正文给 oracle，返回 (rc, 输出字节或 None)。"""
    exe = _oracle_or_skip(False)
    old_f, pat_f, out_f = tmp_path / "old.bin", tmp_path / "p.lite", tmp_path / "o.bin"
    old_f.write_bytes(old)
    pat_f.write_bytes(body)
    if out_f.exists():
        out_f.unlink()
    proc = subprocess.run([_posix(exe), _posix(old_f), _posix(pat_f), _posix(out_f)],
                          capture_output=True, text=True)
    return proc.returncode, (out_f.read_bytes() if out_f.exists() else None)


def test_decoder_rejects_truncated_stream(tmp_path):
    """缺字节导致解码器需要更多输入 ⇒ 被 EOF 抓住并拒绝。"""
    old, new = _l2_pair(2000, seed=11)
    body = lite_body(build(old, new, fw_version=1, compress=False))

    rc, _ = _run_oracle_raw(old, body[:-8], tmp_path)
    assert rc != 0, "截断的流竟然被解码器接受了"


def test_decoder_silently_ignores_trailing_garbage(tmp_path):
    """尾部垃圾**不会**被发现 —— 结束校验只断言「输入缓存非空」。

    后果：设备不能相信"解码器返回成功"就等于"补丁完整"。垃圾可能来自传输层
    尾部拼接、Nor Flash 残留等，必须在擦页前用 patch_size 卡死。
    """
    old, new = _l2_pair(2000, seed=12)
    body = lite_body(build(old, new, fw_version=1, compress=False))

    rc, got = _run_oracle_raw(old, body + b"\xde\xad\xbe\xef" * 8, tmp_path)
    assert rc == 0, "预期解码器无视尾部垃圾（这正是薄弱点）"
    assert got == expected_target(new), "本次内容仍然正确，但解码器没有任何机制保证这一点"


def test_decoder_silently_accepts_midstream_corruption_with_wrong_output(tmp_path):
    """**核心证据**：中段 1 字节翻转后，解码器返回成功，但产出的是错误镜像。

    实测（整镜像域）：补丁中间翻转 1 字节 ⇒ `hpatch_lite_patch()` 返回 TRUE、
    输出长度也对，但内容已错。

    所以 `new_crc32` 不是"锦上添花"，而是**唯一**能拦住这类静默损坏的手段：
    设备必须在把新镜像提交到 Slot 之前复算 CRC32 并比对。同理，`old_crc32`
    保证我们是在正确的旧镜像上做差分。

    本用例刻意只挑**落在 CRC 覆盖区内**的损坏位置 —— 覆盖区之外的那一段
    另有其问题，由下一个用例单独记录（它是个真实缺口，不是本用例的课题）。
    """
    img = load_spec()["image_header"]
    crc_start = img["crc_region"]["start_offset_in_slot"]

    old, new = _l2_pair(2000, seed=13)
    want = expected_target(new)
    env = parse_envelope(build(old, new, fw_version=1, compress=False))
    body = bytearray(lite_body(build(old, new, fw_version=1, compress=False)))

    # 找一个「翻转后解码器仍返回成功、且只有 CRC 覆盖区之内发生变化」的位置。
    # 必须限定在覆盖区内，否则测的就不是 new_crc32 的能力了。
    # 同时要求 magic 完好，否则镜像根本解析不出来（那是"结构损坏"，不是本用例）。
    hit = None
    for off in range(16, len(body) - 2):
        probe = bytearray(body)
        probe[off] ^= 0xFF
        rc, got = _run_oracle_raw(old, bytes(probe), tmp_path)
        if rc != 0 or got is None or got == want:
            continue
        if got[:crc_start + 4] != want[:crc_start + 4]:
            continue                      # 改动落在了覆盖区之外（或破坏了 magic）
        hit = (off, got)
        break

    assert hit is not None, (
        "未能复现「静默产出错误镜像」—— 若解码器真的变强了，应更新规划 §16.4 的结论，"
        "但在那之前不要把 new_crc32 去掉")

    off, got = hit
    assert image_crc32(finalize_image(got)) != env["new_crc32"], (
        "new_crc32 竟然没能识别出被损坏的输出 —— 设备侧将无判据可用")
    assert env["new_crc32"] == image_crc32(new)


def test_crc_coverage_excludes_the_vector_table_a_real_gap(tmp_path):
    """**实测到的缺口（记录下来，不是"通过"）**：向量表不在任何校验覆盖区内。

    CRC 覆盖区从 `magic`（`0xC8`）起，而影像开头 `[0, 0xC0)` 的**192 B 向量表
    不在其中**。于是：

    * 补丁流在向量表区间被损坏（传输层 CRC16 未覆盖到的场景：暂存区 bit-rot、
      我们自己的编码器在这一段出错）时，解码器返回成功、`new_crc32` 也一致；
    * `boot_crc_verify()` 同样看不见这一段（它验的就是同一个覆盖区）；
    * 而 `boot_jump_to_app()` 恰恰**只**依赖这一段（`slot+0` 取初始 SP、
      `slot+4` 取 Reset_Handler）。

    后果：设备会把一个向量表被破坏的槽标记为"校验通过"并跳进去。

    本用例把这个缺口**钉死**，以免日后有人以为 CRC 覆盖了整份镜像。
    处置见 `fota_delta.c.j2` 的向量表合法性检查，以及规划 §17.5 的后续项。
    """
    img = load_spec()["image_header"]
    crc_start = img["crc_region"]["start_offset_in_slot"]

    old, new = _l2_pair(2000, seed=17)
    want = expected_target(new)
    env = parse_envelope(build(old, new, fw_version=1, compress=False))
    body = bytearray(lite_body(build(old, new, fw_version=1, compress=False)))

    invisible = None
    for off in range(16, len(body) - 2):
        probe = bytearray(body)
        probe[off] ^= 0xFF
        rc, got = _run_oracle_raw(old, bytes(probe), tmp_path)
        if rc != 0 or got is None or got == want:
            continue
        if got[:crc_start] == want[:crc_start]:
            continue                      # 覆盖区之内 → 归上一个用例管
        invisible = got
        break

    assert invisible is not None, (
        "没有找到落在向量表区间的损坏样本 —— 若解码器行为变了，本缺口可能已不存在，"
        "请复核后更新规划 §17.5 与 fota_delta 的向量表检查是否还有必要")
    # 缺口的确切含义：内容已错，但 CRC 判据认为"没问题"
    assert finalize_image(invisible) != new, "向量表确实被改坏了"
    assert image_crc32(finalize_image(invisible)) == env["new_crc32"], (
        "预期 CRC 无法发现向量表损坏（这正是缺口的定义）；"
        "若这里失败说明覆盖区已经扩大到含向量表，请把本用例改成正向断言")


# ---------------------------------------------------------------------------
# 6. tinyuz 压缩路径（自研编码器）
#
# 规划 §16.6 的验收第 6 条：**必须断言解压路径真的被执行**，否则
# `compress_type` 会退化成"只写不读"的字段 —— 与 A9「mock 让测试失效」同类。
# oracle 的 `tuz_calls` 计数就是这件事的直接证据。
# ---------------------------------------------------------------------------

def test_uvarint_group_count_matches_decoder_semantics():
    """`outLen` 的分组数必须与解码器 `_def_unpack_len` 互逆。

    判据：用「组数 + 偏移」还原出原值，并检查 P 落在该组数的可表示区间内。
    这是长度编码最容易写错的地方（偏移是 Σ2^(j*pack_bit) 而不是 Σ2^j）。
    """
    from generator.tinyuz_enc import _group_count

    for pack_bit in (1, 2):
        for value in (0, 1, 2, 3, 4, 7, 8, 15, 16, 127, 128, 1000, 65535, 200000):
            count, dec = _group_count(value, pack_bit)
            p = value - dec
            assert 0 <= p < (1 << (count * pack_bit)), (
                "pack_bit=%d value=%d ⇒ count=%d dec=%d 但 P=%d 超出 %d 位"
                % (pack_bit, value, count, dec, p, count * pack_bit))
            # 组数必须是最小的那个（否则编码不是最紧的）
            if count > 1:
                prev_dec = dec - (1 << ((count - 1) * pack_bit))
                assert value - prev_dec >= (1 << ((count - 1) * pack_bit)), (
                    "pack_bit=%d value=%d 本可以用 %d 组" % (pack_bit, value, count - 1))


def test_compress_body_puts_dict_size_at_stream_head():
    """dict_size 必须落在流首（4 B 小端），且必须落在合法区间。"""
    from generator.tinyuz_enc import MAX_DICT_SIZE, compress_body

    for data in (b"", b"a", b"abcdefgh" * 40, bytes(range(256)) * 8):
        blob = compress_body(data, dict_size=4096)
        dict_size = int.from_bytes(blob[:4], "little")
        assert 1 <= dict_size <= MAX_DICT_SIZE, (
            "dict_size=%d 非法（解码器会拒绝 0，且上限 %d）" % (dict_size, MAX_DICT_SIZE))
        # ⚠️ 这条同时钉死一个真实踩过的坑：4 个 dict_size 字节是用 out_raw 追加的，
        #    落点可能在已写内容之间 —— 必须被摘出来前置，否则解码器读到的是乱码。
        assert dict_size <= 4096, "编码器不得产出超过请求值的回溯窗口"


def test_compress_body_is_deterministic():
    data = _firmware_like(5000, seed=21)
    from generator.tinyuz_enc import compress_body
    assert compress_body(data) == compress_body(data)


def test_compress_body_rejects_bad_dict_size():
    from generator.tinyuz_enc import MAX_DICT_SIZE, compress_body
    with pytest.raises(ValueError):
        compress_body(b"abc", dict_size=0)
    with pytest.raises(ValueError):
        compress_body(b"abc", dict_size=MAX_DICT_SIZE + 1)


def test_roundtrip_compressed_identical_and_shifted(tmp_path):
    """压缩路径的两种典型形态：完全一致（全 cover）与中段插入（带位移）。"""
    data = _firmware_like(6000, seed=31)
    assert_roundtrip(data, data, tmp_path, compress=True)

    old = _firmware_like(6000, seed=32)
    new = old[:2000] + b"INSERTED-BLOCK--" * 5 + old[2000:]
    assert_roundtrip(old, new, tmp_path, compress=True)


@pytest.mark.parametrize("seed", range(4))
def test_roundtrip_compressed_fuzz(tmp_path, seed):
    rnd = random.Random(2000 + seed)
    old = _firmware_like(9000, seed=seed)
    new = bytearray(old)
    for _ in range(rnd.randint(1, 5)):
        pos = rnd.randrange(0, max(1, len(new)))
        op = rnd.choice(("flip", "insert", "delete", "block"))
        if op == "flip" and pos < len(new):
            new[pos] ^= rnd.randint(1, 255)
        elif op == "insert":
            new[pos:pos] = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(1, 90)))
        elif op == "delete" and pos < len(new):
            del new[pos:pos + rnd.randint(1, 90)]
        elif op == "block" and pos < len(new):
            new[pos:pos] = b"\x77" * rnd.randint(12, 300)
    assert_roundtrip(old, bytes(new), tmp_path, compress=True)


def test_compressed_path_actually_runs_the_decompressor(tmp_path):
    """**规划 §16.6 验收第 6 条**：解压必须真的被执行。

    `compress_type` 是"只写不读"字段的典型风险点 —— 编码器写了 1，设备却没走
    解压分支，测试只看"打补丁成功"是发现不了的。oracle 的 `tuz_calls` 计数是
    直接证据。
    """
    old_code = _firmware_like(8000, seed=41)
    new_code = old_code[:4000] + b"CHANGED" * 30 + old_code[4000:]
    old = slot_image(old_code, fw_version=8, slot_base=SLOT_A_BASE)
    new = slot_image(new_code, fw_version=9, slot_base=SLOT_B_BASE)
    patch = build(old, new, fw_version=9, compress=True)

    env = info(patch)
    assert env["compress_type"] == 1, "压缩路径必须把 compressType 写成 1"
    assert env["compressed"] is True
    assert env["lite_uncompress_size"] > 0, "解压后长度必须写进 lite 头，设备据此分配缓冲"

    got, stats = apply_oracle(old, patch, tmp_path, with_tuz=True)
    assert got == expected_target(new)
    assert stats["tuz_calls"] >= 1, (
        "解压回调一次都没被调用 —— compress_type 成了只写不读的字段")
    assert stats["uncompress_size"] == env["lite_uncompress_size"]


def test_auto_mode_never_worse_than_either_single_mode(tmp_path):
    """`compress="auto"` 必须取两条路径里更小的那个，因此永不回退。

    实测依据（真实固件对）：补丁很小时 tinyuz 的固定开销会反超收益
    （knob_demo：不压 70 B vs 压 79 B），所以"默认开压缩"不能是无条件压。
    """
    cases = [
        (_firmware_like(4000, seed=51), _firmware_like(4000, seed=52)),   # 完全换掉
        (_firmware_like(4000, seed=53), _firmware_like(4000, seed=53)),   # 完全一致
    ]
    old_id = _firmware_like(4000, seed=54)
    cases.append((old_id, old_id[:2000] + b"XYZ" + old_id[2000:]))        # 微小改动

    for old_code, new_code in cases:
        old = slot_image(old_code, fw_version=1, slot_base=SLOT_A_BASE)
        new = slot_image(new_code, fw_version=2, slot_base=SLOT_B_BASE)
        n_none = len(build(old, new, fw_version=1, compress=False))
        n_always = len(build(old, new, fw_version=1, compress=True))
        auto = build(old, new, fw_version=1, compress="auto")
        assert len(auto) == min(n_none, n_always), (
            "auto 选了 %d，但可选的更优解是 %d（none=%d always=%d）"
            % (len(auto), min(n_none, n_always), n_none, n_always))
        assert_roundtrip(old_code, new_code, tmp_path, compress="auto")


def test_auto_mode_flags_match_the_chosen_inner_format(tmp_path):
    """信封 flags 与 lite 头的 compressType 必须描述**同一条**被选中的路径。"""
    old_code = _firmware_like(5000, seed=61)
    new_code = old_code[:2500] + b"REPLACED-BLOCK" * 20 + old_code[2500:]
    old = slot_image(old_code, fw_version=1, slot_base=SLOT_A_BASE)
    new = slot_image(new_code, fw_version=2, slot_base=SLOT_B_BASE)
    patch = build(old, new, fw_version=1, compress="auto")
    env = info(patch)                     # info() 内部就会校验两者一致
    assert env["compressed"] == (env["compress_type"] == 1)
    assert_roundtrip(old_code, new_code, tmp_path, compress="auto")


def test_build_rejects_unknown_compress_mode():
    old, new = _mk(b"a" * 64, b"b" * 64)
    with pytest.raises(ValueError):
        build(old, new, fw_version=1, compress="sometimes")


# ---------------------------------------------------------------------------
# 7. CLI 输入识别（自动模式必须"选对并说清"）
# ---------------------------------------------------------------------------

def test_load_image_requires_a_header_filled_slot_image(tmp_path):
    """输入必须是 **patch_crc.py 回填过头部**的 slot 镜像，且错误信息要指明该走哪一步。

    为什么必须 fail-closed：差分域与 CRC 都依赖头部里的 `image_size`。拿 linker
    刚产出的裸 `.bin`（`image_size` 还是 0xFFFFFFFF 占位）去算，会得到一份
    「看起来生成成功、装上就砖」的补丁 —— 这比直接报错危险得多。
    """
    from generator.delta_tool import _load_image

    code = bytes((i * 3 + 5) & 0xFF for i in range(400))
    good = slot_image(code, fw_version=3, slot_base=SLOT_A_BASE)
    good_p = tmp_path / "fw_crc.bin"
    good_p.write_bytes(good)

    got, how = _load_image(str(good_p))
    assert got == good, "差分域就是整个 slot 镜像"
    assert "slot 镜像" in how, "必须回报一句人可读的解释（CLI 会打印出来）"

    # 裸 linker 产物：magic 在，但 image_size 是占位值
    raw = bytearray(good)
    img = load_spec()["image_header"]
    raw[img["offset_in_slot"]:img["offset_in_slot"] + 4] = b"\xff" * 4
    raw_p = tmp_path / "fw_raw.bin"
    raw_p.write_bytes(bytes(raw))
    with pytest.raises(ValueError, match="占位"):
        _load_image(str(raw_p))

    # 完全不是镜像
    junk_p = tmp_path / "junk.bin"
    junk_p.write_bytes(b"\x00" * 512)
    with pytest.raises(ValueError, match="magic"):
        _load_image(str(junk_p))

    # 错误信息必须指出「先经 patch_crc.py」—— 否则使用者只会看到"格式不对"
    with pytest.raises(ValueError, match="patch_crc"):
        _load_image(str(junk_p))


def test_cli_build_and_info_round_trip(tmp_path):
    """CLI 走一遍 build → info，并确认产物能被 oracle 消费。"""
    from generator.delta_tool import _main

    old_code = _firmware_like(5000, seed=71)
    new_code = old_code[:2500] + b"CLI-TEST-BLOCK" * 15 + old_code[2500:]
    old = slot_image(old_code, fw_version=1, slot_base=SLOT_A_BASE)
    new = slot_image(new_code, fw_version=2, slot_base=SLOT_B_BASE)
    old_p, new_p = tmp_path / "old.bin", tmp_path / "new.bin"
    old_p.write_bytes(old)
    new_p.write_bytes(new)
    out_p = tmp_path / "p.h2cd"

    rc = _main(["build", "--old", str(old_p), "--new", str(new_p),
                "-o", str(out_p), "--version", "0x2A"])
    assert rc == 0 and out_p.exists()

    patch = out_p.read_bytes()
    env = info(patch)
    assert env["fw_version"] == 0x2A
    assert _main(["info", str(out_p)]) == 0

    # 用 info() 判断内层格式：patch[48] 是 'h'，不是 compress_type
    got, _ = apply_oracle(old, patch, tmp_path,
                          with_tuz=(env["compress_type"] == 1))
    assert got == expected_target(new)


def test_cli_refuses_raw_linker_output(tmp_path):
    """CLI 必须拒绝未经 patch_crc.py 的裸 .bin，并把该走的那一步写进提示。"""
    from generator.delta_tool import _main

    raw = bytearray(slot_image(b"code" * 100))
    img = load_spec()["image_header"]
    raw[img["offset_in_slot"]:img["offset_in_slot"] + 4] = b"\xff" * 4
    p = tmp_path / "raw.bin"
    p.write_bytes(bytes(raw))

    with pytest.raises(ValueError, match="patch_crc"):
        _main(["build", "--old", str(p), "--new", str(p),
               "-o", str(tmp_path / "x.h2cd"), "--version", "1"])


def test_cli_lite_only_output_is_consumable_by_decoder(tmp_path):
    """`--lite-only` 必须吐出纯 lite 流（无信封），这正是 oracle/设备直连调试要的。"""
    from generator.delta_tool import _main

    old_code = _firmware_like(3000, seed=81)
    new_code = old_code[:1500] + b"X" + old_code[1500:]
    old = slot_image(old_code, fw_version=1, slot_base=SLOT_A_BASE)
    new = slot_image(new_code, fw_version=2, slot_base=SLOT_B_BASE)
    old_p, new_p = tmp_path / "old.bin", tmp_path / "new.bin"
    old_p.write_bytes(old)
    new_p.write_bytes(new)
    lite_p = tmp_path / "p.lite"

    assert _main(["build", "--old", str(old_p), "--new", str(new_p), "-o", str(lite_p),
                  "--version", "1", "--lite-only"]) == 0
    body = lite_p.read_bytes()
    assert body[0:2] == b"hI", "输出应当以 lite 魔数开头（而不是信封）"
    assert len(body) == len(lite_body(build(old, new, fw_version=1,
                                           compress=(body[2] == 1))))
