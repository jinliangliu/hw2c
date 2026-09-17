"""boot_crc 的护栏：防止主机测试再次退化成恒真/空转。

背景（2026-09-17 实测缺陷，上板才暴露）
---------------------------------------
引导器 `boot_crc_verify()` 的 STM32 CRC 外设配置写成了 `REV_IN=0b01`
（按字节反转）且**漏掉终值异或 0xFFFFFFFF**。后果是引导器**永远拒绝所有镜像**
（包括刚烧进去、校验和正确的那个）：上电后反复 `soft_reset()`，LED 匀速闪，
`.data` 从未被拷贝。实测同一镜像区域：旧配置算出 `0x119FE772`，而镜像头里
写的是 `0x28843CD6`（= zlib.crc32）。

这个缺陷能活到上板，靠的是**两层同时失效**：

① **恒真断言**：`test_boot_crc.c` 的 `test_verify_crc_match` 把被测代码自己
   算出来的 `CRC->DR` 当期望值写进镜像头。而当时的 mock 的 `DR` 只是个静态
   变量、不做任何运算 ⇒ 无论 REV_IN / REV_OUT / 终值怎么配，两边都一致。
② **整套用例在主机上从不执行**：它们整段包在 `#if UINTPTR_MAX <= UINT32_MAX`
   里，而本机主机 gcc 正是 64 位。

本文件检查：

A. **KAT 常量必须由 zlib 独立算得**，且与主机工具 `patch_crc.stm32_crc32`
   一致 —— 三个实现（C 生成代码、mock 行为模型、主机工具）必须落在同一个值上。
B. **KAT 常量必须真的能区分错误配置**：用 Python 复刻 CRC 核，断言按字节反转、
   漏终值异或等近失配置都算不出这个常量（对护栏本身做变异验证）。
C. **CRC 缝的目标侧展开必须是裸寄存器访问**，不许两侧共用一份软件实现。
D. **C 测试里不许出现**：从设备通路读回期望值、条件编译跳过、旧缺陷的指纹值
   `0x119FE772`。
E. **mock 的 CRC 必须是行为模型**（真的按 CR/POL/INIT 运算），且 INIT/POL 的
   复位值与硅片一致 —— 这是 A 的前提。
F. **Flash 不许有第二条访问缝**：mock 已把 Flash 建到真实地址（`mock_flash_map`），
   生成代码必须继续用裸 `(volatile uint32_t *)` 寻址；再开一条 test-only 通路，
   主机上跑的就不再是目标上跑的那份代码了。
"""

from __future__ import annotations

import json
import re
import struct
import zlib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPO_ROOT / "templates"

_BOOT_CRC = _TEMPLATES / "bootloader" / "boot_crc.c.j2"
_TEST_CRC = _TEMPLATES / "test" / "test_boot_crc.c.j2"
_MOCK_H = _TEMPLATES / "test" / "mock_hal.h.j2"
_MOCK_C = _TEMPLATES / "test" / "mock_hal.c.j2"

# 旧配置（REV_IN=0b01 + 无终值异或）在真实硅片上对那段镜像区域算出的值。
# 它出现在期望值位置上就意味着缺陷回来了。
_BUGGY_CONFIG_FINGERPRINT = 0x119FE772

_VECTORS = ("A", "B", "C")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _strip_c_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


def _macro_int(text: str, name: str) -> int:
    """取 `#define NAME 0x..UL` 的整数值（宏名必须在模板里真实存在）。"""
    match = re.search(r"#define\s+%s\s+(0[xX][0-9A-Fa-f]+|\d+)[UL]*\b" % re.escape(name), text)
    assert match is not None, "模板里找不到宏 %s" % name
    return int(match.group(1), 0)


def _c_function_body(text: str, signature: str) -> str:
    """粗粒度取出函数体：从 `signature` 起匹配到配平的 `}`。"""
    start = text.index(signature)
    brace = text.index("{", start)
    depth = 0
    for idx in range(brace, len(text)):
        if text[idx] == "{":
            depth += 1
        elif text[idx] == "}":
            depth -= 1
            if depth == 0:
                return text[brace:idx + 1]
    raise AssertionError("函数体没有配平的右花括号：%s" % signature)


def _test_branch(text: str, marker: str) -> tuple:
    """返回包含 marker 的 `#ifdef TEST ... #else ... #endif` 的 (TEST 侧, 目标侧)。"""
    for match in re.finditer(r"#ifdef\s+TEST(?P<t>.*?)#else(?P<e>.*?)#endif",
                             text, flags=re.S):
        if marker in match.group("t"):
            return match.group("t"), match.group("e")
    raise AssertionError("找不到包含 %s 的 `#ifdef TEST / #else / #endif` 三段式" % marker)


def _header_spec() -> dict:
    with (_REPO_ROOT / "generator" / "data" / "fota_format.json").open(
            "r", encoding="utf-8") as fh:
        return json.load(fh)["image_header"]


def _kat_region(text: str, vector: str) -> tuple:
    """按 C 测试里声明的 KAT 宏重建 CRC 覆盖区，返回 (region_bytes, 期望CRC)。"""
    size = _macro_int(text, "KAT_%s_SIZE" % vector)
    version = _macro_int(text, "KAT_%s_VER" % vector)
    expected = _macro_int(text, "KAT_%s_CRC" % vector)

    # 注意取 m.group(2)（值）而不是 m.group(1)（W 后面的下标）—— 取错下标会得到
    # 0,1,2 这样"看起来还挺合理"的 payload，于是护栏自己算错却依然自信。
    words = [int(m.group(2), 0) for m in re.finditer(
        r"#define\s+KAT_%s_W(\d+)\s+(0[xX][0-9A-Fa-f]+|\d+)[UL]*\b" % vector, text)]
    assert len(words) * 4 == size, (
        "向量 %s 的 payload 字数（%d）与 image_size（%d）不一致" % (vector, len(words), size)
    )

    magic = _macro_int(text, "HDR_MAGIC")
    # 覆盖范围 = magic || fw_version(24-bit 有效) || payload，与 fota_format.json 的
    # crc_region（start_field=magic, length_addend=8）一致。
    region = struct.pack("<I", magic)
    region += struct.pack("<I", version & 0x00FFFFFF)
    for word in words:
        region += struct.pack("<I", word)

    assert len(region) == size + 8, (
        "覆盖区长度应为 image_size + 8（magic 4B + fw_version 4B + payload），"
        "实得 %d" % len(region)
    )
    return region, expected


# ---------------------------------------------------------------------------
# A. KAT 常量必须由 zlib 独立算得，且与主机工具一致
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vector", _VECTORS)
def test_kat_constant_equals_zlib_crc32(vector):
    """C 测试里的 KAT 期望值必须等于对同一段字节做 zlib.crc32 的结果。

    这条把 C 侧期望值与独立实现的 Python zlib 绑在一起：谁改了 KAT 的
    payload / version / image_size 而忘了改期望值，或者反过来把期望值改成
    被测代码算出来的那个数，这里都会 red。
    """
    text = _TEST_CRC.read_text(encoding="utf-8")
    region, expected = _kat_region(text, vector)

    assert expected == (zlib.crc32(region) & 0xFFFFFFFF), (
        "向量 %s 的期望值 0x%08X 与 zlib.crc32(0x%08X) 不符 —— "
        "KAT 的期望值必须独立算出，不能取自被测代码"
        % (vector, expected, zlib.crc32(region) & 0xFFFFFFFF)
    )


@pytest.mark.parametrize("vector", _VECTORS)
def test_kat_constant_matches_host_tool_stm32_crc32(vector):
    """主机侧 `patch_crc.stm32_crc32`（回填镜像头的那个）必须算出同一个值。

    三者（生成代码 / mock 行为模型 / 主机工具）必须落在同一个数上：主机工具
    把值写进头部、设备侧再算一遍来比对，任何一侧偏移都会让引导器拒绝所有镜像
    —— 而那种故障在主机测试里是看不见的。
    """
    from generator.patch_crc import stm32_crc32

    text = _TEST_CRC.read_text(encoding="utf-8")
    region, expected = _kat_region(text, vector)

    assert stm32_crc32(region) == expected, (
        "generator/patch_crc.py 的 stm32_crc32 与 KAT 期望值不一致（向量 %s）—— "
        "主机回填的 CRC 与设备侧算的不是同一个算法" % vector
    )
    assert stm32_crc32(region) == (zlib.crc32(region) & 0xFFFFFFFF), (
        "stm32_crc32 与 zlib.crc32 不等价（向量 %s）。docstring 里写了这条交叉"
        "核对，但此前**没有任何东西强制它**" % vector
    )


def test_kat_vectors_differ_in_size_and_version():
    """三个向量的 image_size 与 version 必须两两不同。

    只用一个向量的话，CRC 起点偏 4 字节或长度少算 4 字节都可能仍然碰巧对上
    （覆盖区内容相同时值就相同）。取不同长度 + 不同版本号，等于同时钉住
    `crc_region.start_field = magic` 与 `length_addend = 8` 两件事。
    """
    text = _TEST_CRC.read_text(encoding="utf-8")
    sizes = [_macro_int(text, "KAT_%s_SIZE" % v) for v in _VECTORS]
    versions = [_macro_int(text, "KAT_%s_VER" % v) for v in _VECTORS]

    assert len(set(sizes)) == len(sizes), "KAT 向量的 image_size 必须互不相同：%s" % sizes
    assert len(set(versions)) == len(versions), "KAT 向量的版本号必须互不相同：%s" % versions


def test_kat_header_contract_matches_the_spec():
    """C 测试里重写的头部偏移/魔数必须与 fota_format.json 一致。

    测试文件**故意重写一遍**这些常量（不引用被测代码的宏），这样被测代码改
    偏移时测试会失败而不是跟着变 —— 但重写的值本身也得对得上真源。
    """
    text = _TEST_CRC.read_text(encoding="utf-8")
    spec = _header_spec()
    fields = spec["fields"]

    assert _macro_int(text, "HDR_OFF") == spec["offset_in_slot"], (
        "C 测试的 HDR_OFF 与 fota_format.json 的 image_header.offset_in_slot 不一致"
    )
    assert _macro_int(text, "HDR_MAGIC") == fields["magic"]["value"], (
        "C 测试的 HDR_MAGIC 与 fota_format.json 不一致"
    )
    assert _macro_int(text, "HDR_PAYLOAD") == (fields["image_size"]["size"] * 4), (
        "C 测试的 HDR_PAYLOAD 应为头部大小 16（payload 相对头部起点的偏移）"
    )


# ---------------------------------------------------------------------------
# B. KAT 必须真的能区分错误配置（对护栏本身做变异验证）
# ---------------------------------------------------------------------------

def _crc_core(region: bytes, rev_in: int, rev_out: bool, xorout: int,
              poly: int = 0x04C11DB7, init: int = 0xFFFFFFFF) -> int:
    """按 STM32G0 CRC 外设的语义复刻 MSB-first 核（本会话已在真实硅片上对四种
    REV_IN 逐一核对过）。只用于护栏自身的变异验证，不参与产品逻辑。"""

    def bitrev(value: int, bits: int) -> int:
        out = 0
        for _ in range(bits):
            out = (out << 1) | (value & 1)
            value >>= 1
        return out & ((1 << bits) - 1)

    def deform(word: int) -> int:
        if rev_in == 0b01:
            return sum(bitrev((word >> (8 * i)) & 0xFF, 8) << (8 * i) for i in range(4))
        if rev_in == 0b10:
            return (bitrev((word >> 16) & 0xFFFF, 16) << 16) | bitrev(word & 0xFFFF, 16)
        if rev_in == 0b11:
            return bitrev(word, 32)
        return word

    state = init
    for offset in range(0, len(region) - 3, 4):
        state ^= deform(struct.unpack_from("<I", region, offset)[0])
        for _ in range(32):
            state = ((state << 1) ^ poly) & 0xFFFFFFFF if state & 0x80000000 \
                else (state << 1) & 0xFFFFFFFF
    if rev_out:
        state = bitrev(state, 32)
    return state ^ xorout


def test_kat_model_reproduces_zlib_for_every_vector():
    """先证明复刻的核本身是对的（否则下面的近失配置断言毫无意义）。"""
    text = _TEST_CRC.read_text(encoding="utf-8")
    for vector in _VECTORS:
        region, expected = _kat_region(text, vector)
        assert _crc_core(region, rev_in=0b11, rev_out=True, xorout=0xFFFFFFFF) == expected
        assert expected == (zlib.crc32(region) & 0xFFFFFFFF)


@pytest.mark.parametrize("rev_in,rev_out,xorout,why", [
    (0b01, True, 0xFFFFFFFF, "REV_IN=0b01（按字节反转）—— 本次上板缺陷的配置"),
    (0b11, True, 0x00000000, "漏掉终值异或 → 算成了 CRC-32/JAMCRC 一类变体"),
    (0b01, True, 0x00000000, "两个错误同时犯（缺陷当时的实际组合）"),
    (0b00, True, 0xFFFFFFFF, "不反射输入 → CRC-32/MPEG-2 家族"),
    (0b10, True, 0xFFFFFFFF, "按半字反转"),
    (0b11, False, 0xFFFFFFFF, "漏掉 REV_OUT"),
])
def test_kat_distinguishes_near_miss_configurations(rev_in, rev_out, xorout, why):
    """每个看起来差不多的错误配置都必须算不出 KAT 期望值。

    这就是这条 KAT 的**效力证明**：如果哪种近失配置也能算出同样的值，那么
    KAT 对着它也是绿的，护栏名存实亡。
    """
    text = _TEST_CRC.read_text(encoding="utf-8")
    for vector in _VECTORS:
        region, expected = _kat_region(text, vector)
        got = _crc_core(region, rev_in=rev_in, rev_out=rev_out, xorout=xorout)
        assert got != expected, (
            "向量 %s 在错误配置（%s）下也算出了 0x%08X —— 这条 KAT 抓不住它"
            % (vector, why, expected)
        )


# ---------------------------------------------------------------------------
# C. CRC 缝的目标侧必须是裸寄存器访问
# ---------------------------------------------------------------------------

def test_crc_seam_target_side_is_raw_register_access():
    """`HW2C_CRC_FEED/READ` 在目标构建里必须仍是 `CRC->DR` 的读写。

    缝是为了让主机能拦住数据通路（`CRC->DR = x` 是一次裸存储，主机上拦不到），
    不是为了"两边共用一份软件 CRC" —— 后者会让主机测试再次与目标语义脱钩。
    """
    text = _BOOT_CRC.read_text(encoding="utf-8")
    _test_side, target = _test_branch(text, "HW2C_CRC_FEED")

    assert "CRC->DR" in target, "目标侧的 CRC 数据通路不再是 CRC->DR"
    assert re.search(r"CRC->DR\s*=\s*\(uint32_t\)", target), \
        "目标侧的 HW2C_CRC_FEED 必须是一次 CRC->DR 写入"
    assert "mock_" not in target, "目标侧出现了 mock_ 调用 —— 缝漏进了生产代码路径"


def test_boot_crc_reads_flash_only_by_raw_pointer():
    """Flash 读取必须是裸的 `(volatile uint32_t *)` 解引用，不许再开第二条缝。

    mock_hal 已经把 512 KB Flash **建立到它的真实地址**（`mock_flash_map()`），
    所以 `(volatile uint32_t *)0x080020C0` 在 64 位主机上就是有效指针 ——
    根本不需要给生成代码开 test-only 的 Flash 访问缝。一旦开了，主机上跑的
    就不再是目标上跑的那份代码（A9 的形态）。

    曾经的写法是整段用 `#if UINTPTR_MAX <= UINT32_MAX` 排除，等于主机上
    一条都不跑；两种做法都不能再有。
    """
    text = _strip_c_comments(_BOOT_CRC.read_text(encoding="utf-8"))

    assert "mock_flash" not in text, (
        "boot_crc.c 里出现了 mock_flash —— 生产代码不许有 test-only 的 Flash 通路"
    )
    # 5 处读取（magic 探测 ×2、image_size、stored_crc、fw_version）+ 1 处取数据指针
    assert len(re.findall(r"\(volatile uint32_t \*\)", text)) == 6, (
        "boot_crc.c 的 Flash 读取形式变了：应当是 5 处直接解引用 + 1 处取数据指针。"
        "若新增了访问缝，请先确认它不会让主机上跑的不是目标那份代码"
    )


# ---------------------------------------------------------------------------
# D. C 测试里不许出现"从设备通路读回期望值"与"整段跳过"
# ---------------------------------------------------------------------------

def test_c_test_never_takes_its_expected_value_from_the_device_path():
    """期望值绝不能来自被测代码/mock 的 CRC 通路。

    这是本次缺陷的**直接成因**：旧用例写下 `buf[0x44/4] = CRC->DR;`，而当时
    mock 的 `DR` 只是个静态变量 ⇒ 恒真断言。期望值只能来自独立算出的常量。
    """
    text = _strip_c_comments(_TEST_CRC.read_text(encoding="utf-8"))

    for pattern, why in [
        (r"=\s*CRC->DR", "把 CRC->DR 当期望值"),
        (r"=\s*mock_crc_read\s*\(", "把 mock_crc_read() 当期望值"),
        (r"=\s*mock_crc_get_raw\s*\(", "把 mock 的内部状态当期望值"),
    ]:
        assert not re.search(pattern, text), (
            "test_boot_crc.c 里出现了「%s」—— 那是恒真断言：期望值与被测值出自"
            "同一条通路，怎么配都一致" % why
        )

    assert not re.search(r"0x0?119[Ff][Ee]772", text), (
        "test_boot_crc.c 里出现了旧缺陷配置的指纹值 0x%08X —— 期望值里绝不能有它"
        % _BUGGY_CONFIG_FINGERPRINT
    )


def test_c_test_does_not_skip_cases_on_64_bit_hosts():
    """集成用例不许再用 `#if UINTPTR_MAX <= UINT32_MAX` 之类的条件编译跳过。

    本机主机 gcc 就是 64 位 —— 那个条件意味着这些用例一条都不执行，却在源码
    里看起来是覆盖。真实地址映射（`mock_flash_map`）已经把这个问题解决掉。
    """
    # 必须剥注释后再判：文件顶部的说明里**就要**提到那个条件（解释为什么不能
    # 再用它），直接对原文匹配的话，护栏会被自己的文档绊倒。
    text = _strip_c_comments(_TEST_CRC.read_text(encoding="utf-8"))

    assert "UINTPTR_MAX" not in text, (
        "test_boot_crc.c 仍在使用 UINTPTR_MAX 条件编译 —— 64 位主机上会静默跳过"
        "用例。Flash 走真实地址映射即可，不需要排除"
    )
    assert "#if" not in text, (
        "C 测试里出现了条件编译 —— 主机用例必须无条件执行，否则在某台机器上"
        "被跳过这件事不会有任何信号"
    )
    assert "TEST_ASSERT_TRUE(1)" not in text, "存在恒真的 skipped 占位断言"
    assert "skipped" not in text.lower(), "存在被标注为 skipped 的用例"


def test_c_test_builds_its_image_on_the_real_flash_address():
    """用例必须把镜像写到**真实槽位地址**上，并显式检查映射可用。

    - `mock_flash_reset()` / `mock_flash_poke()` 是建镜像的方式；
    - `mock_flash_is_mapped()` 必须被断言 —— 映射失败时直接寻址会段错误，
      那样连崩在哪条用例都看不到。宁可报映射不可用。
    - 槽位地址不要用名字 `FLASH_BASE`：mock_hal.h 已经把它 define 成
      `MOCK_FLASH_BASE`，重定义会编译失败（或更糟，静默用错地址）。
    """
    text = _strip_c_comments(_TEST_CRC.read_text(encoding="utf-8"))

    assert "mock_flash_reset()" in text, "用例没有复位 Flash 存储"
    assert "mock_flash_poke(" in text, "用例没有通过 mock_flash_poke 建镜像"
    assert "mock_flash_is_mapped()" in text, (
        "用例没有断言 Flash 已映射到真实地址 —— 映射失败时随后的直接寻址会段错误"
    )
    assert "0x08002000" in text, "用例没有用 Slot A 的真实基址（0x08002000）"

    defines = set(re.findall(r"#define\s+([A-Za-z_]\w+)", text))
    assert "FLASH_BASE" not in defines, (
        "C 测试重定义了 FLASH_BASE —— 那是 mock_hal.h 里 MOCK_FLASH_BASE 的别名，"
        "重定义会让槽位地址这件事出现两个说法"
    )
    assert not re.search(r"mock_flash_bind\s*\(", text), (
        "C 测试用了 mock_flash_bind —— 那是已被撤掉的第二套 Flash 模型"
    )


# ---------------------------------------------------------------------------
# E. mock 的 CRC 必须是行为模型（A 的前提）
# ---------------------------------------------------------------------------

def test_mock_crc_feed_is_a_behavioural_model_not_storage():
    """`mock_crc_feed()` 必须真的做 CRC 运算，不能只是 `DR = word`。

    如果它退化成一次普通存储，上面所有 KAT 都会变成恒真断言 —— 而且是
    **静默**变绿（测试仍然通过，只是不再说明任何事）。
    """
    text = _strip_c_comments(_MOCK_C.read_text(encoding="utf-8"))
    body = _c_function_body(text, "uint32_t mock_crc_feed(")

    assert re.search(r"for\s*\(\s*uint32_t\s+i\s*=\s*0U?\s*;\s*i\s*<\s*32U?\s*;", body), (
        "mock_crc_feed 里找不到逐位做 32 轮的循环 —— 它可能已经退化成一次普通存储"
    )
    assert "POL" in body, "mock_crc_feed 没有使用多项式（POL）"
    assert "mock_crc_rev_in(" in body, (
        "mock_crc_feed 没有按 REV_IN 变形输入字 —— 那么 REV_IN 配置错也测不出来"
    )


def test_mock_crc_read_applies_rev_out():
    """读回值必须按 REV_OUT 处理，否则漏配 REV_OUT 这类错误抓不到。"""
    text = _strip_c_comments(_MOCK_C.read_text(encoding="utf-8"))
    body = _c_function_body(text, "uint32_t mock_crc_read(")

    assert "CRC_CR_REV_OUT" in body, (
        "mock_crc_read 没有判断 REV_OUT —— 于是 REV_OUT 配错时主机算出的值仍然与"
        "真机不同，而测试全绿"
    )


def test_mock_crc_reset_values_match_silicon():
    """mock 的 INIT / POL 复位值必须与外设复位值一致。

    生成代码**不写**这两个寄存器（用的是外设默认值），所以 mock 若给它 0，
    主机与真机算出的 CRC 必然不同 —— 而这是一处不会有任何其它信号的不一致。
    """
    text = _MOCK_C.read_text(encoding="utf-8")
    body = _c_function_body(text, "void mock_cmsis_reset(")

    assert "MOCK_CRC_INIT_RESET_VALUE" in body, "mock_cmsis_reset 没有恢复 CRC 的 INIT 复位值"
    assert "MOCK_CRC_POL_RESET_VALUE" in body, "mock_cmsis_reset 没有恢复 CRC 的 POL 复位值"

    header = _MOCK_H.read_text(encoding="utf-8")
    assert _macro_int(header, "MOCK_CRC_INIT_RESET_VALUE") == 0xFFFFFFFF, \
        "CRC 外设的 INIT 复位值是 0xFFFFFFFF"
    assert _macro_int(header, "MOCK_CRC_POL_RESET_VALUE") == 0x04C11DB7, \
        "CRC 外设的 POL 复位值是 0x04C11DB7（CRC-32 多项式）"


def test_mock_crc_bits_exist_in_vendored_cmsis():
    """mock 新增的 CRC 位名/位号必须与 vendored CMSIS 一致。

    这类不一致在主机上不会有任何信号（mock 自洽），但生成代码在目标上会编不过，
    或者更糟 —— 判断一个永远为 0 的保留位。
    """
    header = _MOCK_H.read_text(encoding="utf-8")
    cmsis = (_REPO_ROOT / "static" / "stm32g0" / "CMSIS" / "Device" / "ST"
             / "STM32G0xx" / "Include" / "stm32g0b1xx.h").read_text(encoding="utf-8")

    for name, bit in [("CRC_CR_REV_IN_0", 5), ("CRC_CR_REV_IN_1", 6),
                      ("CRC_CR_REV_OUT", 7), ("CRC_CR_RESET", 0)]:
        assert re.search(r"#define\s+%s\s+0x%08XU" % (name, 1 << bit), header), (
            "mock 的 %s 不是 bit%d" % (name, bit)
        )
        assert name in cmsis, "%s 在 vendored CMSIS 里不存在" % name
