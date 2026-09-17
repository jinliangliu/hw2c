"""FOTA / 引导镜像格式的一致性测试。

本文件守着三件事，每一件都对应一个曾经真实发生过的缺陷类型：

1. **格式真源与实现一致** —— `generator/data/fota_format.json` 是唯一真源，
   链接脚本模板与 `boot_crc.c.j2` 里的常量必须与它对齐。
   （历史缺陷 A3：C 模板与 Python 工具各写一份头部布局，字段序不一致，
   引导器永远搜不到 magic。）

2. **`patch_crc.py` 原位回填，绝不改变镜像长度** ——
   历史缺陷 B1：它曾在 0xCC 插入 4 字节版本号，使镜像整体后移，
   而向量表与字面量池中的绝对地址不会跟着变 ⇒ 镜像不可运行。
   这条测试用合成镜像把「长度不变 + 向量表未被触碰」钉死。

3. **CRC 覆盖 fw_version** —— fw_version 是「哪个槽更新」的判据，
   若不被 CRC 覆盖，改版本号即可绕过完整性校验。
"""

import json
import re
import struct
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GENERATOR_DIR = _REPO_ROOT / "generator"
_TEMPLATES_DIR = _REPO_ROOT / "templates"

sys.path.insert(0, str(_REPO_ROOT))

from generator.patch_crc import load_spec, patch_firmware, stm32_crc32  # noqa: E402


# ---------------------------------------------------------------------------
# 真源加载
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def spec():
    return load_spec(str(_GENERATOR_DIR / "data" / "fota_format.json"))


@pytest.fixture(scope="module")
def img(spec):
    return spec["image_header"]


def _render_slot_linker(slot: str) -> str:
    """把 app_slot_{a,b}.ld.j2 **渲染出来**再断言。

    为什么必须渲染而不是对模板源码做正则：模板里的偏移现在写成
    `{{ fota_fmt.img_hdr_off }}`，源码上看不出它到底是多少。只有渲染后
    才能确认落进链接脚本的是真源里的那个数（= A3 的正面防线）。
    """
    import jinja2

    from generator.context.bootloader_context import fota_format_for_templates

    flash_base = 0x08000000
    boot_config = {
        "size_kb": 8,
        "app_a_offset": 0x2000,
        "app_b_offset": 0x40000,
        "_app_a_start": flash_base + 0x2000,
        "_app_a_end": flash_base + 0x40000,
        "_app_b_start": flash_base + 0x40000,
        "_app_b_end": flash_base + 0x80000,
        "_app_a_size": 0x40000 - 0x2000,
        "_app_b_size": 0x80000 - 0x40000,
    }
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        trim_blocks=True, lstrip_blocks=True,
    )
    return env.get_template("linker/app_slot_%s.ld.j2" % slot).render(
        boot_config=boot_config,
        fota_fmt=fota_format_for_templates(),
        heap_size="0x200",
        stack_size="0x400",
    )


# ---------------------------------------------------------------------------
# 1. 真源自身的自洽性
# ---------------------------------------------------------------------------

def test_magic_values_reproduce_declared_on_flash_bytes(img, spec):
    """magic 的数值必须能按小端还原出 on_flash_bytes。

    这条防的是「注释说 H2Ck、内存里却是 kCAH」这类字节序误读 ——
    现存的 APP_HEADER_MAGIC 正是这种情况（数值 0x4841436B，内存呈现 kCAH），
    所以真源里同时记了数值与内存字节，二者必须互相推得出来。
    """
    for name, section in (("image_header", img), ("delta_envelope", spec["delta_envelope"])):
        field = section["fields"]["magic"]
        packed = struct.pack("<I", field["value"]).hex()
        assert packed == field["on_flash_bytes"], (
            "%s.magic 数值 0x%08X 小端展开为 %s，真源声明的是 %s"
            % (name, field["value"], packed, field["on_flash_bytes"])
        )


def test_header_field_offsets_are_contiguous_and_inside(img):
    """字段必须首尾相接、不重叠、不越界。"""
    fields = sorted(img["fields"].items(), key=lambda kv: kv[1]["offset"])
    cursor = 0
    for name, f in fields:
        assert f["offset"] == cursor, "字段 %s 起始 %d，期望 %d（与前一字段不连续）" % (
            name, f["offset"], cursor)
        cursor += f["size"]
    assert cursor == img["size"]


def test_crc_region_covers_magic_and_version(img):
    """CRC 必须从 magic 起算，从而覆盖 fw_version。"""
    fields = img["fields"]
    region = img["crc_region"]
    magic_off = img["offset_in_slot"] + fields["magic"]["offset"]
    version_off = img["offset_in_slot"] + fields["fw_version"]["offset"]

    assert region["start_offset_in_slot"] == magic_off
    # magic 与 fw_version 都落在覆盖区间内
    assert region["start_offset_in_slot"] <= magic_off
    assert region["start_offset_in_slot"] <= version_off
    # 覆盖长度 = image_size + addend，其中 addend 必须恰好是 magic+version 的 8 字节
    assert region["length_addend"] == fields["magic"]["size"] + fields["fw_version"]["size"]


# ---------------------------------------------------------------------------
# 2. 链接脚本模板与 boot_crc 都对齐真源
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slot", ["a", "b"])
def test_slot_linker_declares_header_of_exact_size(slot, img):
    """**渲染后**的 `.app_header` 段必须恰好声明 img['size'] 个 LONG，
    且 magic 落在真源规定的那个字上。

    多一个少一个都会让 .text 的起始地址与真源不符 —— 而真源正是
    boot_crc / patch_crc 两侧共用的偏移依据。
    """
    text = _render_slot_linker(slot)
    block = re.search(r"\.app_header\s*:\s*\{(.*?)\}\s*>FLASH", text, re.S)
    assert block, "app_slot_%s.ld 中找不到 .app_header 段" % slot

    longs = re.findall(r"\bLONG\s*\(", block.group(1))
    expected = img["size"] // 4
    assert len(longs) == expected, (
        ".app_header 声明了 %d 个 LONG，真源要求 %d 个（头部 %d 字节）"
        % (len(longs), expected, img["size"])
    )

    # magic 必须出现在它该在的那个 LONG 上，且值等于真源
    magic_index = img["fields"]["magic"]["offset"] // 4
    values = [int(m.group(1).strip(), 0)
              for m in re.finditer(r"\bLONG\s*\(\s*([^)]*)\)", block.group(1))]
    assert values[magic_index] == img["fields"]["magic"]["value"], (
        "第 %d 个 LONG 是 0x%08X，真源的 magic 是 0x%08X"
        % (magic_index, values[magic_index], img["fields"]["magic"]["value"])
    )
    assert values.count(img["fields"]["magic"]["value"]) == 1, (
        "magic 常量在 .app_header 里出现了不止一次")


def test_boot_crc_constants_match_spec(img):
    """boot_crc.c.j2 的 #define 必须与真源一致。"""
    src = (_TEMPLATES_DIR / "bootloader" / "boot_crc.c.j2").read_text(encoding="utf-8")

    def define(name):
        m = re.search(r"#define\s+%s\s+([0-9A-Fa-fxX]+)" % re.escape(name), src)
        assert m, "boot_crc.c.j2 缺少 #define %s" % name
        return int(m.group(1), 0)

    assert define("APP_HEADER_MAGIC") == img["fields"]["magic"]["value"]
    assert define("APP_HEADER_OFFSET") == img["offset_in_slot"]
    assert define("APP_HEADER_SIZE") == img["size"]
    assert define("APP_HEADER_MAGIC_OFF") == img["fields"]["magic"]["offset"]
    assert define("APP_HEADER_VERSION_OFF") == img["fields"]["fw_version"]["offset"]
    assert define("APP_HEADER_PAYLOAD_OFF") == img["payload_offset_in_slot"] - img["offset_in_slot"]
    assert define("APP_CRC_REGION_OFF") == (
        img["crc_region"]["start_offset_in_slot"] - img["offset_in_slot"])
    assert define("APP_CRC_LENGTH_ADDEND") == img["crc_region"]["length_addend"]
    assert define("APP_VERSION_MASK") == img["version_mask"]


def test_boot_read_fw_version_reads_explicit_field():
    """版本号必须读显式字段，不得再读 magic+4 这种「靠插入凑出来」的位置。"""
    src = (_TEMPLATES_DIR / "bootloader" / "boot_crc.c.j2").read_text(encoding="utf-8")
    body = src[src.index("uint32_t boot_read_fw_version"):]
    assert "APP_HEADER_VERSION_OFF" in body, (
        "boot_read_fw_version 未使用显式的版本字段偏移"
    )


def test_generator_copies_format_spec_into_output():
    """生成器必须把真源复制到 output/，否则 patch_crc.py 会退回默认值。"""
    src = (_GENERATOR_DIR / "generate.py").read_text(encoding="utf-8")
    assert "FOTA_FORMAT_PATH" in src and "fota_format.json" in src, (
        "generate.py 没有把 fota_format.json 复制到 output 目录"
    )


# ---------------------------------------------------------------------------
# 3. patch_crc.py 的行为（B1 回归）
# ---------------------------------------------------------------------------

_SLOT_BASE = 0x08002000


def _make_image(img, payload_len=256, first_word=0xB580AF00):
    """合成一个布局正确的镜像：向量表 + 16B 头部占位 + payload。"""
    hdr_at = img["offset_in_slot"]
    payload_at = img["payload_offset_in_slot"]

    vector = bytearray(hdr_at)
    # 向量表 [0] = 初始 MSP，[1] = Reset_Handler（Thumb 位）...指向 payload 首字
    struct.pack_into("<I", vector, 0, 0x20024000)
    struct.pack_into("<I", vector, 4, _SLOT_BASE + payload_at + 1)

    header = bytearray(img["size"])
    struct.pack_into("<I", header, img["fields"]["magic"]["offset"],
                     img["fields"]["magic"]["value"])

    payload = bytearray([(first_word >> (8 * i)) & 0xFF for i in range(4)])
    payload += bytes((i * 7 + 3) & 0xFF for i in range(payload_len - 4))

    return bytes(vector + header + payload)


def test_patch_crc_does_not_change_image_length(tmp_path, img):
    """B1 回归：回填后长度必须不变。

    长度一变，镜像整体位移，而向量表与字面量池里的绝对地址不会跟着变。
    """
    src = tmp_path / "app.bin"
    dst = tmp_path / "app_crc.bin"
    src.write_bytes(_make_image(img))

    res = patch_firmware(str(src), str(dst), version=7, spec=None)

    assert res["size_before"] == res["size_after"], "回填改变了镜像长度"
    assert dst.stat().st_size == src.stat().st_size


def test_patch_crc_keeps_vector_table_intact(tmp_path, img):
    """B1 回归：向量表逐字节不变，且向量表 [1] 仍指向真实首指令。

    这正是旧实现失败的地方：插入 4 字节后，向量表指向处变成了版本号。
    """
    src = tmp_path / "app.bin"
    dst = tmp_path / "app_crc.bin"
    original = _make_image(img)
    src.write_bytes(original)

    patch_firmware(str(src), str(dst), version=7)
    patched = dst.read_bytes()

    hdr_at = img["offset_in_slot"]
    assert patched[:hdr_at] == original[:hdr_at], "向量表被改写了"

    reset_vector = struct.unpack_from("<I", patched, 4)[0] & ~1
    entry_off = reset_vector - _SLOT_BASE
    payload_at = img["payload_offset_in_slot"]
    assert entry_off == payload_at, (
        "向量表 [1] 指向 0x%X，但 payload 起始在 0x%X" % (entry_off, payload_at))
    assert patched[entry_off:entry_off + 4] == original[entry_off:entry_off + 4], (
        "向量表指向处已不是真实首指令")


def test_patch_crc_writes_fields_in_place(tmp_path, img):
    """头部各字段落在真源规定的偏移上，CRC 与独立重算一致。"""
    src = tmp_path / "app.bin"
    dst = tmp_path / "app_crc.bin"
    src.write_bytes(_make_image(img))

    res = patch_firmware(str(src), str(dst), version=0x123456)
    data = dst.read_bytes()

    hdr_at = img["offset_in_slot"]
    fields = img["fields"]

    image_size, crc32 = struct.unpack_from("<II", data, hdr_at)
    assert image_size == len(data) - img["payload_offset_in_slot"]
    assert struct.unpack_from("<I", data, hdr_at + fields["magic"]["offset"])[0] \
        == fields["magic"]["value"]
    assert struct.unpack_from("<I", data, hdr_at + fields["fw_version"]["offset"])[0] \
        == 0x123456

    region = img["crc_region"]
    start = region["start_offset_in_slot"]
    end = start + image_size + region["length_addend"]
    assert crc32 == stm32_crc32(data[start:end])
    assert res["crc32"] == crc32


def test_patch_crc_crc_covers_fw_version(tmp_path, img):
    """改动 fw_version 必须使 CRC 失效 —— 否则版本号可被伪造。"""
    src = tmp_path / "app.bin"
    dst = tmp_path / "app_crc.bin"
    src.write_bytes(_make_image(img))
    patch_firmware(str(src), str(dst), version=7)
    data = bytearray(dst.read_bytes())

    hdr_at = img["offset_in_slot"]
    image_size = struct.unpack_from("<I", data, hdr_at)[0]
    region = img["crc_region"]
    start = region["start_offset_in_slot"]
    end = start + image_size + region["length_addend"]

    stored = struct.unpack_from("<I", data, hdr_at + 4)[0]
    version_off = hdr_at + img["fields"]["fw_version"]["offset"]
    data[version_off] ^= 0x01
    assert stm32_crc32(bytes(data[start:end])) != stored, (
        "篡改 fw_version 后 CRC 仍然相同 —— 版本号没有被覆盖")


def test_patch_crc_rejects_misaligned_payload(tmp_path, img):
    """payload 长度非 4 的倍数必须拒绝，而不是静默漏算尾部。"""
    src = tmp_path / "app.bin"
    src.write_bytes(_make_image(img, payload_len=254))
    with pytest.raises(ValueError, match="4 的倍数"):
        patch_firmware(str(src), str(tmp_path / "out.bin"), version=1)


def test_patch_crc_rejects_missing_magic(tmp_path, img):
    """没有头部 magic 时必须报错，不得产出一个看似成功的镜像。"""
    blob = bytearray(_make_image(img))
    hdr_at = img["offset_in_slot"] + img["fields"]["magic"]["offset"]
    struct.pack_into("<I", blob, hdr_at, 0xDEADBEEF)
    src = tmp_path / "nohdr.bin"
    src.write_bytes(bytes(blob))
    with pytest.raises(ValueError, match="magic"):
        patch_firmware(str(src), str(tmp_path / "out.bin"), version=1)


def test_patch_crc_rejects_header_at_the_wrong_slot_offset(tmp_path, img):
    """头部偏移与真源不符必须**拒绝**，而不是"搜到哪儿就按哪儿写"。

    背景（2026-09-16 实测）：`.isr_vector` 在 STM32G0B1 上只有 47 项 =
    188 B = 0xBC，链接脚本里 `.app_header` 紧随其后 → 落在 0xBC。
    旧实现用 magic 的实际位置反推 header_offset，于是它**顺着**接受了 0xBC，
    而引导器 / fota_delta 都按 0xC0 读 ⇒ 在板上拒掉每一个镜像，
    但生成与编译一路绿灯。本测试把这条静默路径钉死。
    """
    # 把整个头部（含 magic）前移 4 字节，模拟向量表比预期短
    blob = bytearray(_make_image(img))
    hdr_at = img["offset_in_slot"]
    shifted = bytes(blob[hdr_at:hdr_at + img["size"]])
    del blob[hdr_at:hdr_at + img["size"]]
    blob[hdr_at - 4:hdr_at - 4] = shifted
    src = tmp_path / "shifted.bin"
    src.write_bytes(bytes(blob))

    with pytest.raises(ValueError, match="槽内偏移"):
        patch_firmware(str(src), str(tmp_path / "out.bin"), version=1)


# ---------------------------------------------------------------------------
# 4. 链接脚本把头部**钉在**真源偏移上（不是"紧随向量表"）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slot", ["a", "b"])
def test_slot_linker_pins_header_at_the_spec_offset(slot, img):
    """仅声明 `.app_header` 是不够的，必须把它的**位置**钉在真源偏移上。

    只检查"段里有 4 个 LONG"会漏掉真正的故障模式：段本身是对的，
    但它被放在了错误的偏移上（紧随长度未知的向量表）。所以这里断言
    (1) `.isr_vector` 里显式把位置计数器推到 `ORIGIN(FLASH) + offset_in_slot`，
    (2) 存在校验位置与大小的链接期 ASSERT。
    """
    tmpl = (_TEMPLATES_DIR / "linker" / ("app_slot_%s.ld.j2" % slot)).read_text(encoding="utf-8")

    # (1) 位置钉死。允许写成字面量或经 fota_fmt 展开的占位符；这里只要求
    #     "把 . 推到 ORIGIN(FLASH) 加一个常量"这个结构存在。
    m = re.search(r"\.\s*=\s*ORIGIN\(FLASH\)\s*\+\s*([^;]+);", tmpl)
    assert m, (
        "app_slot_%s.ld.j2 没有把 .isr_vector 的结束位置推到 ORIGIN(FLASH) + 头部偏移；"
        "头部会落在向量表的自然长度上（STM32G0B1 实测 0xBC，而非真源的 0x%X）"
        % (slot, img["offset_in_slot"]))
    expr = m.group(1).strip()
    assert "fota_fmt.img_hdr_off" in expr or str(img["offset_in_slot"]) in expr, (
        "头部偏移表达式 %r 既不是真源占位符也不等于真源值" % expr)

    # (2) 链接期断言必须存在 —— 否则位置/大小再次漂移时无人报警。
    assert re.search(r"ASSERT\s*\(\s*ADDR\(\.app_header\)", tmpl), (
        "app_slot_%s.ld.j2 缺少对头部**位置**的链接期 ASSERT" % slot)
    assert re.search(r"ASSERT\s*\(\s*SIZEOF\(\.app_header\)", tmpl), (
        "app_slot_%s.ld.j2 缺少对头部**大小**的链接期 ASSERT" % slot)
