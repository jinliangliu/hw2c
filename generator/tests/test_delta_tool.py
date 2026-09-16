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
    build,
    crc16_ccitt_false,
    diff_to_covers,
    encode_lite_stream,
    info,
    lite_body,
    pack_lite_header,
    pack_oldpos_delta,
    pack_uvarint,
    parse_envelope,
    split_image,
    verify_host,
)


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


def assert_roundtrip(old: bytes, new: bytes, tmp_path: Path, *, version: int = 3,
                     compress=False) -> bytes:
    """核心判据：old + patch 必须逐字节还原出 new。"""
    patch = build(old, new, fw_version=version, compress=compress)
    verify_host(old, new, patch)                       # L1
    got, _ = apply_oracle(old, patch, tmp_path,
                          with_tuz=(compress is not False))   # L2
    assert got == new, (
        "oracle 还原结果与 new 不一致：len %d vs %d" % (len(got), len(new)))
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
        old = bytes(range(256)) * 4
        patch = build(old, old, fw_version=1, compress=False)
        got, stats = apply_oracle(old, patch, td)
        assert got == old, "oracle 连恒等补丁都还原不了 —— 判据本身失效"
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


def test_envelope_roundtrip_and_field_placement():
    patch = build(b"payload-old", b"payload-new!!", fw_version=0x123456)
    env = parse_envelope(patch)
    assert env["fw_version"] == 0x123456
    assert env["old_size"] == 11
    assert env["new_size"] == 13
    assert env["compressed"] is False
    assert env["auth_len"] == 0
    assert env["patch_size"] == len(patch) - 48


def test_envelope_version_is_covered_by_header_crc():
    """篡改 fw_version 必须让信封头 CRC 失败 —— 否则版本号可被伪造。"""
    patch = bytearray(build(b"a" * 32, b"b" * 32, fw_version=7))
    patch[28] ^= 0x01                      # fw_version 位于偏移 28
    with pytest.raises(ValueError, match="CRC16"):
        parse_envelope(bytes(patch))


def test_envelope_rejects_unknown_flags():
    patch = bytearray(build(b"a" * 32, b"b" * 32, fw_version=7))
    patch[6] |= 0x80                       # flags 位于偏移 6
    struct.pack_into("<H", patch, 32, crc16_ccitt_false(bytes(patch[:32])))
    with pytest.raises(ValueError, match="未知位"):
        parse_envelope(bytes(patch))


def test_info_rejects_truncated_or_short_patch():
    patch = build(b"x" * 64, b"y" * 64, fw_version=1)
    with pytest.raises(ValueError):
        info(patch[:20])                   # 装不下信封
    with pytest.raises(ValueError, match="截断"):
        info(patch[:-1])                   # patch_size 声明与实到不符
    with pytest.raises(ValueError, match="截断"):
        lite_body(patch[:-1])


def test_info_rejects_wrong_lite_magic():
    patch = bytearray(build(b"x" * 64, b"y" * 64, fw_version=1))
    patch[48] = ord("H")                   # 把 'h' 改成 'H'
    with pytest.raises(ValueError, match="hI"):
        info(bytes(patch))


def test_verify_host_rejects_mismatched_old():
    patch = build(b"x" * 64, b"y" * 64, fw_version=1)
    verify_host(b"x" * 64, b"y" * 64, patch)
    with pytest.raises(AssertionError):
        verify_host(b"x" * 63, b"y" * 64, patch)


def test_build_is_deterministic():
    """同一对输入必须产出逐字节相同的补丁（可复现构建）。"""
    a = build(b"hello" * 40, b"hello" * 39 + b"world", fw_version=5)
    b = build(b"hello" * 40, b"hello" * 39 + b"world", fw_version=5)
    assert a == b


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


def test_split_image_extracts_payload_and_version():
    """从合成 slot 镜像里切出 payload：头部偏移不得越界、版本号要读对。"""
    from generator.patch_crc import load_spec
    img = load_spec()["image_header"]
    fields = img["fields"]

    payload = bytes((i * 5 + 1) & 0xFF for i in range(256))
    blob = bytearray(img["payload_offset_in_slot"] + len(payload))
    struct.pack_into("<I", blob, img["offset_in_slot"] + fields["image_size"]["offset"],
                     len(payload))
    struct.pack_into("<I", blob, img["offset_in_slot"] + fields["magic"]["offset"],
                     fields["magic"]["value"])
    struct.pack_into("<I", blob, img["offset_in_slot"] + fields["fw_version"]["offset"], 42)
    blob[img["payload_offset_in_slot"]:] = payload

    got = split_image(bytes(blob))
    assert got["payload"] == payload
    assert got["fw_version"] == 42

    with pytest.raises(ValueError, match="magic"):
        split_image(b"\x00" * 512)


# ---------------------------------------------------------------------------
# 4. L2：oracle 逐字节判据（未压缩基线路径）
# ---------------------------------------------------------------------------

def _firmware_like(n: int, seed: int = 0) -> bytes:
    """造一段"像固件"的数据：有重复结构，但不是纯随机（更接近真实 .bin）。"""
    rnd = random.Random(seed)
    out = bytearray()
    while len(out) < n:
        if rnd.random() < 0.5:
            out += bytes(rnd.getrandbits(8) for _ in range(rnd.randint(4, 64)))
        else:
            out += bytes([rnd.getrandbits(8)]) * rnd.randint(8, 128)
    return bytes(out[:n])


def test_roundtrip_identical(tmp_path):
    data = _firmware_like(4096)
    assert_roundtrip(data, data, tmp_path)


def test_roundtrip_completely_different(tmp_path):
    """零匹配 —— 覆盖 coverCount==0 的收尾路径。"""
    old = bytes([0x00]) * 512
    new = bytes([0xFF]) * 512
    patch = assert_roundtrip(old, new, tmp_path)
    assert len(patch) <= 48 + 16 + len(new), "零匹配时补丁不应超过「信封+字面量」"


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
    """比匹配窗口还短的 payload，以及空 payload —— 差分器应当退化为全字面量。"""
    assert_roundtrip(b"", b"", tmp_path)
    assert_roundtrip(b"", b"short-new", tmp_path)
    assert_roundtrip(b"short-old", b"", tmp_path)
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
    old = _firmware_like(2000, seed=9)
    new = old[:1500] + b"TAIL" * 10
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
    old = _firmware_like(2000, seed=11)
    new = old[:1500] + b"TAIL" * 10
    body = lite_body(build(old, new, fw_version=1, compress=False))

    rc, _ = _run_oracle_raw(old, body[:-8], tmp_path)
    assert rc != 0, "截断的流竟然被解码器接受了"


def test_decoder_silently_ignores_trailing_garbage(tmp_path):
    """尾部垃圾**不会**被发现 —— 结束校验只断言「输入缓存非空」。

    后果：设备不能相信"解码器返回成功"就等于"补丁完整"。垃圾可能来自传输层
    尾部拼接、Nor Flash 残留等，必须在擦页前用 patch_size 卡死。
    """
    old = _firmware_like(2000, seed=12)
    new = old[:1500] + b"TAIL" * 10
    body = lite_body(build(old, new, fw_version=1, compress=False))

    rc, got = _run_oracle_raw(old, body + b"\xde\xad\xbe\xef" * 8, tmp_path)
    assert rc == 0, "预期解码器无视尾部垃圾（这正是薄弱点）"
    assert got == new, "本次内容仍然正确，但解码器没有任何机制保证这一点"


def test_decoder_silently_accepts_midstream_corruption_with_wrong_output(tmp_path):
    """**核心证据**：中段 1 字节翻转后，解码器返回成功，但产出的是错误镜像。

    实测（2000 B old / 1540 B new）：补丁中段翻转 1 字节 ⇒ `hpatch_lite_patch()`
    返回 TRUE，输出长度也对，但内容已错。

    所以 `new_crc32` 不是"锦上添花"，而是**唯一**能拦住这类静默损坏的手段：
    设备必须在把新镜像提交到 Slot 之前复算 CRC32 并比对。同理，`old_crc32`
    保证我们是在正确的旧镜像上做差分。
    """
    old = _firmware_like(2000, seed=13)
    new = old[:1500] + b"TAIL" * 10
    body = bytearray(lite_body(build(old, new, fw_version=1, compress=False)))

    # 找一个「翻转后解码器仍返回成功」的位置（中段，落在 cover 数据里）
    corrupted_positions = []
    for off in range(16, len(body) - 2):
        probe = bytearray(body)
        probe[off] ^= 0xFF
        rc, got = _run_oracle_raw(old, bytes(probe), tmp_path)
        if rc == 0 and got is not None and got != new:
            corrupted_positions.append(off)
            if len(corrupted_positions) >= 3:
                break

    assert corrupted_positions, (
        "未能复现「静默产出错误镜像」—— 若解码器真的变强了，应更新规划 §16.4 的结论，"
        "但在那之前不要把 new_crc32 去掉")

    # 内容完整性只能由信封的 new_crc32 判定
    from generator.patch_crc import stm32_crc32
    off = corrupted_positions[0]
    probe = bytearray(body)
    probe[off] ^= 0xFF
    rc, got = _run_oracle_raw(old, bytes(probe), tmp_path)
    assert rc == 0 and got != new
    assert stm32_crc32(got) != stm32_crc32(new), (
        "new_crc32 竟然没能识别出被损坏的输出 —— 设备侧将无判据可用")
    assert parse_envelope(build(old, new, fw_version=1))["new_crc32"] == stm32_crc32(new)


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
    old = _firmware_like(8000, seed=41)
    new = old[:4000] + b"CHANGED" * 30 + old[4000:]
    patch = build(old, new, fw_version=9, compress=True)

    env = info(patch)
    assert env["compress_type"] == 1, "压缩路径必须把 compressType 写成 1"
    assert env["compressed"] is True
    assert env["lite_uncompress_size"] > 0, "解压后长度必须写进 lite 头，设备据此分配缓冲"

    got, stats = apply_oracle(old, patch, tmp_path, with_tuz=True)
    assert got == new
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

    for old, new in cases:
        n_none = len(build(old, new, fw_version=1, compress=False))
        n_always = len(build(old, new, fw_version=1, compress=True))
        auto = build(old, new, fw_version=1, compress="auto")
        assert len(auto) == min(n_none, n_always), (
            "auto 选了 %d，但可选的更优解是 %d（none=%d always=%d）"
            % (len(auto), min(n_none, n_always), n_none, n_always))
        assert_roundtrip(old, new, tmp_path, compress="auto")


def test_auto_mode_flags_match_the_chosen_inner_format(tmp_path):
    """信封 flags 与 lite 头的 compressType 必须描述**同一条**被选中的路径。"""
    old = _firmware_like(5000, seed=61)
    new = old[:2500] + b"REPLACED-BLOCK" * 20 + old[2500:]
    patch = build(old, new, fw_version=1, compress="auto")
    env = info(patch)                     # info() 内部就会校验两者一致
    assert env["compressed"] == (env["compress_type"] == 1)
    assert_roundtrip(old, new, tmp_path, compress="auto")


def test_build_rejects_unknown_compress_mode():
    with pytest.raises(ValueError):
        build(b"a" * 64, b"b" * 64, fw_version=1, compress="sometimes")


# ---------------------------------------------------------------------------
# 7. CLI 输入识别（自动模式必须"选对并说清"）
# ---------------------------------------------------------------------------

def test_load_payload_auto_handles_both_real_inputs(tmp_path):
    """OTA 现场两种输入都会出现，自动模式必须都能处理，且**说清**选了哪种解释。

    新固件总是裸 `.bin`（linker 直接产出），设备上那份是带 16 B 头部的 slot 镜像。
    猜错方向会让补丁"看起来成功"却与设备上的镜像对不上，所以返回值里必须带
    一句人可读的解释，由 CLI 打印出来。
    """
    from generator.delta_tool import _load_payload
    from generator.patch_crc import load_spec

    img = load_spec()["image_header"]
    fields = img["fields"]
    payload = bytes((i * 3 + 5) & 0xFF for i in range(400))

    # (a) 裸固件
    raw_path = tmp_path / "fw.bin"
    raw_path.write_bytes(payload)
    got, how = _load_payload(str(raw_path), "auto")
    assert got == payload and "payload" in how

    # (b) slot 镜像（带头部）
    blob = bytearray(img["payload_offset_in_slot"] + len(payload))
    struct.pack_into("<I", blob, img["offset_in_slot"] + fields["image_size"]["offset"],
                     len(payload))
    struct.pack_into("<I", blob, img["offset_in_slot"] + fields["magic"]["offset"],
                     fields["magic"]["value"])
    blob[img["payload_offset_in_slot"]:] = payload
    slot_path = tmp_path / "fw_slot.bin"
    slot_path.write_bytes(bytes(blob))
    got, how = _load_payload(str(slot_path), "auto")
    assert got == payload and "slot" in how

    # (c) 强制模式要能覆盖自动判断
    assert _load_payload(str(slot_path), "raw")[0] == bytes(blob)
    with pytest.raises(ValueError, match="magic"):
        _load_payload(str(raw_path), "slot")
    with pytest.raises(ValueError):
        _load_payload(str(raw_path), "有时是裸的")


def test_cli_build_and_info_round_trip(tmp_path):
    """CLI 走一遍 build → info，并确认产物能被 oracle 消费。"""
    from generator.delta_tool import _main

    old = _firmware_like(5000, seed=71)
    new = old[:2500] + b"CLI-TEST-BLOCK" * 15 + old[2500:]
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
    assert got == new


def test_cli_lite_only_output_is_consumable_by_decoder(tmp_path):
    """`--lite-only` 必须吐出纯 lite 流（无信封），这正是 oracle/设备直连调试要的。"""
    from generator.delta_tool import _main

    old = _firmware_like(3000, seed=81)
    new = old[:1500] + b"X" + old[1500:]
    old_p, new_p = tmp_path / "old.bin", tmp_path / "new.bin"
    old_p.write_bytes(old)
    new_p.write_bytes(new)
    lite_p = tmp_path / "p.lite"

    assert _main(["build", "--old", str(old_p), "--new", str(new_p), "-o", str(lite_p),
                  "--version", "1", "--lite-only", "--input-mode", "raw"]) == 0
    body = lite_p.read_bytes()
    assert body[0:2] == b"hI", "输出应当以 lite 魔数开头（而不是信封）"
    assert len(body) == len(lite_body(build(old, new, fw_version=1,
                                           compress=(body[2] == 1))))
