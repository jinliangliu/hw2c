"""L6 掉电注入：在主机上把设备侧差分应用层按操作边界"断电"。

为什么这一层值得单独一个文件
----------------------------
掉电安全是差分 OTA 唯一真正危险的地方，而它恰好**不需要真板**就能完整覆盖
（规划 §12 的 L6 因此被称作"性价比最高的一层"）。前提是应用层不依赖真 Flash ——
`drv_fota_delta.{c,h}` 的 I/O 全部经 `fota_delta_backend_t` 注入，因此本测试把
**同一份源码**编到主机上，只把 Flash 换成"行为正确 + 可注入失败"的内存实现。

这与 A9（mock 让测试失效）是相反的：这里没有替身，只有一层被替换的存储介质，
而那一层被实现得比真 Flash 更严格（越界 / 非对齐 / 长度非法一律报错）。

跑什么
------
  1. **渲染** `templates/drivers/fota_delta.{c,h}.j2`（与生成器同一套模板，
     因此链接期/编译期问题在这里也会暴露）；
  2. 用 `delta_tool.build()` 生成一份**真补丁**（压缩路径也走一遍），连带
     新旧镜像一起写成 C 头文件；
  3. 用宿主 gcc 编译「渲染出的驱动 + vendored 解码器 + 测试台」；
  4. 运行测试台：先跑一遍无故障基线，再在**每一次持久化操作**处各注入一次
     掉电，每次检查三条不变量（活动槽不变 / 目标槽 magic 恒擦除 / 重放后正确）。

"测试台有牙齿吗"
----------------
只跑出一个绿色结果说明不了什么 —— 第一版本文件的枚举就曾经**静默退化**成
"每个操作只注入 1 个点"（原因见 harness 里 `g_fault.record` 的注释），而它照样
打印 OK。因此本文件除了主用例，还用**变异测试**证明这个测试台真的会红：
把模板里两处关键顺序改坏，L6 必须抓到。这条比主用例本身更重要。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GENERATOR_DIR = _REPO_ROOT / "generator"
_TEMPLATES_DIR = _REPO_ROOT / "templates"

sys.path.insert(0, str(_REPO_ROOT))

from generator.context.bootloader_context import (  # noqa: E402
    fota_delta_budget,
    fota_format_for_templates,
)
from generator.delta_tool import (  # noqa: E402
    build,
    crc16_ccitt_false,
    parse_envelope,
)

from generator.tests.delta_fixtures import (  # noqa: E402
    SLOT_A_BASE,
    SLOT_B_BASE,
    firmware_like,
    slot_image,
)

_HARNESS = Path(__file__).resolve().parent / "harness" / "fota_delta_l6_harness.c"
_HPATCH_DIR = _REPO_ROOT / "static" / "third_party" / "hpatch_lite"
_TUZ_DIR = _REPO_ROOT / "static" / "third_party" / "tinyuz" / "decompress"

# 槽容量（与 bootloader_context 的默认布局一致：A = 248 KB，B = 256 KB）
_SLOT_A_SIZE = 0x40000 - 0x2000
_SLOT_B_SIZE = 0x40000

# 页面大小 / 缓存：取自与生成器相同的预算函数，避免测试与产品漂移
_BUDGET = fota_delta_budget({"size_kb": 8, "app_a_offset": 0x2000, "app_b_offset": 0x40000})

_OLD_IMAGE_SIZE = 4096                    # 2 页整
_NEW_IMAGE_SIZE = 4352                    # 2.125 页 —— 刻意留一个非整页尾页


def _host_gcc() -> str:
    """宿主 gcc。优先环境变量与 PATH，最后退回本机 mingw-w64。"""
    for cand in (os.environ.get("H2C_GCC_PATH"), shutil.which("gcc")):
        if cand and shutil.which(cand):
            return cand
    for cand in ("C:/mingw64/bin/gcc", "C:/mingw32/bin/gcc"):
        if os.path.exists(cand):
            return cand
    pytest.skip("host gcc not available")


def _driver_context() -> dict:
    """渲染上下文，与生成器保持一致（bootloader_context 的默认布局）。"""
    flash_base = 0x08000000
    boot_config = {
        "size_kb": 8,
        "app_a_offset": 0x2000,
        "app_b_offset": 0x40000,
        "_app_a_start": flash_base + 0x2000,
        "_app_b_start": flash_base + 0x40000,
        "_app_a_size": _SLOT_A_SIZE,
        "_app_b_size": _SLOT_B_SIZE,
    }
    return {
        "boot_config": boot_config,
        "fota_fmt": fota_format_for_templates(),
        "fota_delta_page_size": _BUDGET["page_size"],
        "fota_delta_cache_size": _BUDGET["cache_size"],
        "fota_delta_dict_size": _BUDGET["dict_size"],
    }


def _render_driver(tmp_path: Path, override: dict | None = None) -> tuple:
    """渲染驱动模板。

    `override` 允许把某个模板换成**改坏的源码**（变异测试用）。键是相对于
    templates/ 的路径。未覆盖的模板从仓库里原样取 —— 否则 loader 找不到它们。
    """
    import jinja2

    if override:
        root = tmp_path / "_templates"
        for rel in ("drivers/fota_delta.h.j2", "drivers/fota_delta.c.j2"):
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            text = override.get(rel)
            if text is None:
                text = (_TEMPLATES_DIR / rel).read_text(encoding="utf-8")
            dst.write_text(text, encoding="utf-8")
    else:
        root = _TEMPLATES_DIR

    ctx = _driver_context()
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(root)),
        trim_blocks=True, lstrip_blocks=True,
    )
    out = {}
    for tmpl, name in (("drivers/fota_delta.h.j2", "drv_fota_delta.h"),
                       ("drivers/fota_delta.c.j2", "drv_fota_delta.c")):
        text = env.get_template(tmpl).render(**ctx)
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        out[name] = path
    return out["drv_fota_delta.h"], out["drv_fota_delta.c"]


def _make_vectors() -> tuple:
    """造一对真实镜像并生成真补丁（压缩路径）。

    `new` 比 `old` 大 256 B：既覆盖"新镜像更大"，又让最后一页不是整页 ——
    尾巴上的 `flush_page` 对齐与头部回填是最容易写错的地方。
    """
    old_code = firmware_like(_OLD_IMAGE_SIZE - 208, seed=11)
    new_code = bytearray(firmware_like(_NEW_IMAGE_SIZE - 208, seed=12))
    # 植入一段与旧固件相同的重复块，让差分器有东西可匹配（否则退化成全字面量）
    new_code[64:64 + 128] = old_code[512:512 + 128]
    # 再改动若干字节，模拟真实的一处函数级改动
    for i in range(0, 96, 4):
        new_code[1024 + i] ^= 0x5A
    new_code = bytes(new_code)

    old_image = slot_image(old_code, fw_version=1, slot_base=SLOT_A_BASE)
    new_image = slot_image(new_code, fw_version=2, slot_base=SLOT_B_BASE)
    patch = build(old_image, new_image, fw_version=2, compress="auto")

    assert len(old_image) == _OLD_IMAGE_SIZE
    assert len(new_image) == _NEW_IMAGE_SIZE

    # 压缩路径必须**真的**被走到（规划 §14 增量第 6 条：防"字段写了没人读"）
    env = json.loads(
        (_GENERATOR_DIR / "data" / "fota_format.json").read_text(encoding="utf-8")
    )["delta_envelope"]
    flags_off = env["fields"]["flags"]["offset"]
    assert patch[flags_off] & env["flags_bits"]["compressed"], (
        "测试向量没有走压缩路径 —— L6 就覆盖不到 tinyuz 解压"
    )
    return old_image, new_image, patch


def _emit_vectors_header(path: Path, old_image: bytes, new_image: bytes, patch: bytes) -> None:
    def arr(name: str, blob: bytes) -> str:
        lines = ["static const unsigned char %s[%d] = {" % (name, len(blob))]
        for i in range(0, len(blob), 16):
            row = ", ".join("0x%02X" % b for b in blob[i:i + 16])
            lines.append("    %s," % row)
        lines.append("};")
        return "\n".join(lines)

    parts = [
        "/* 自动生成，请勿手工编辑。见 generator/tests/test_fota_delta_l6.py */",
        "#ifndef __FOTA_L6_VECTORS_H",
        "#define __FOTA_L6_VECTORS_H",
        "",
        "#define FOTA_L6_OLD_IMAGE_SIZE %d" % len(old_image),
        "#define FOTA_L6_NEW_IMAGE_SIZE %d" % len(new_image),
        "#define FOTA_L6_PATCH_SIZE     %d" % len(patch),
        "",
        arr("g_l6_old", old_image),
        arr("g_l6_new", new_image),
        arr("g_l6_patch", patch),
        "",
        "#endif /* __FOTA_L6_VECTORS_H */",
        "",
    ]
    path.write_text("\n".join(parts), encoding="utf-8")


def _run_bench(tmp_path: Path, override: dict | None = None) -> subprocess.CompletedProcess:
    """渲染 → 编译 → 运行 L6 测试台，返回被测进程的结果。"""
    gcc = _host_gcc()
    _hdr, src = _render_driver(tmp_path, override=override)
    old_image, new_image, patch = _make_vectors()
    _emit_vectors_header(tmp_path / "fota_l6_vectors.h", old_image, new_image, patch)

    exe = tmp_path / ("l6.exe" if sys.platform.startswith("win") else "l6")
    cmd = [
        gcc, "-std=c99", "-O1", "-Wall", "-Wextra",
        "-I", str(tmp_path),
        "-I", str(_HPATCH_DIR),
        "-I", str(_TUZ_DIR),
        str(_HARNESS),
        str(src),
        str(_HPATCH_DIR / "hpatch_lite.c"),
        str(_TUZ_DIR / "tuz_dec.c"),
        "-o", str(exe),
    ]
    comp = subprocess.run(cmd, capture_output=True, text=True)
    assert comp.returncode == 0, (
        "L6 测试台编译失败：\n%s\n%s" % (comp.stdout, comp.stderr)
    )
    return subprocess.run([str(exe)], capture_output=True, text=True, cwd=str(tmp_path))


def test_l6_power_loss_injection(tmp_path):
    """在每一次持久化操作处断电：三条不变量都必须成立。"""
    run = _run_bench(tmp_path)
    assert run.returncode == 0, (
        "L6 掉电注入未通过：\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (run.stdout, run.stderr)
    )
    assert "RESULT: OK" in run.stdout, run.stdout

    # ---- 覆盖率必须随"操作可细分程度"增长，而不是恒等于操作数 ----------------
    #
    # 这一条是为了钉住一个真实踩到过的坑：注入循环曾经读到一张被重放改写的
    # 操作表，于是每个操作只注入 1 个点（erase 的 544 个双字前缀全都没测），
    # 而结论依然是 OK。现在要求 注入点数 > 持久化操作数。
    ops_line = [ln for ln in run.stdout.splitlines() if ln.startswith("injecting at ")]
    assert ops_line, run.stdout
    ops = int(ops_line[-1].split("injecting at ")[1].split(" ")[0])

    tail = run.stdout.strip().splitlines()[-1]
    n = int(tail.split("(")[1].split(" ")[0])
    assert n > ops, (
        "只注入了 %d 个点、却有 %d 个持久化操作 —— 枚举退化成了「每操作一点」，"
        "掉电点的前缀覆盖形同虚设：%s" % (n, ops, tail)
    )
    pages = (_NEW_IMAGE_SIZE + _BUDGET["page_size"] - 1) // _BUDGET["page_size"]
    assert n >= pages + 2, (
        "只注入了 %d 个点，少于页数 %d + 2 —— 覆盖不足：%s" % (n, pages, tail)
    )


# ---------------------------------------------------------------------------
# 变异测试：证明这个测试台会红
#
# 下面每个用例都把模板里**一处关键顺序**改坏，然后要求 L6 报错。它们锚定的是
# 模板源码里的具体片段，因此模板一旦重构，锚点断言会先失败、逼着人来更新 ——
# 这是刻意的：一个悄悄失效的变异测试比没有变异测试更糟。
# ---------------------------------------------------------------------------

_DRIVER_C = "drivers/fota_delta.c.j2"

# M1：擦除后**立刻**把 magic 写进目标槽头部（而不是等到 FLUSH）。
# 这直接破坏"掉电后目标槽结构上不可引导"这条核心不变量 ⇒ 必须被 ② 抓到。
_MUT_HEADER_EARLY_ANCHOR = """    if (io->watchdog != NULL) {
        io->watchdog(io->ctx);
    }

    /* ---- 7.3 装配解码器"""

_MUT_HEADER_EARLY_REPL = """    if (io->watchdog != NULL) {
        io->watchdog(io->ctx);
    }
    {   /* MUTATION M1: 头部提前写完，掉电安全的结构性前提被破坏 */
        uint8_t mut[FOTA_HDR_HOLE_LEN];
        memset(mut, 0xFF, sizeof(mut));
        (void)wr_u32(&mut[FOTA_IMG_OFF_MAGIC], FOTA_IMG_MAGIC);
        (void)io->program(io->ctx, dst_slot + FOTA_HDR_HOLE_OFF, mut,
                          FOTA_HDR_HOLE_LEN);
    }

    /* ---- 7.3 装配解码器"""


def test_l6_bench_detects_header_written_early(tmp_path):
    """把镜像头从 FLUSH 阶段提前到擦除之后，L6 必须报"magic 不是擦除态"。"""
    src_text = (_TEMPLATES_DIR / _DRIVER_C).read_text(encoding="utf-8")
    assert src_text.count(_MUT_HEADER_EARLY_ANCHOR) == 1, (
        "变异锚点在模板里不再唯一 —— 模板改过，请同步更新本测试的锚点"
    )
    mutated = src_text.replace(_MUT_HEADER_EARLY_ANCHOR, _MUT_HEADER_EARLY_REPL)

    run = _run_bench(tmp_path, override={_DRIVER_C: mutated})
    assert run.returncode != 0, (
        "L6 没有抓到「头部提前写入」—— 说明不变量 ② 形同虚设：\n%s" % run.stdout
    )
    assert "target header magic is not erased" in run.stdout + run.stderr, (
        run.stdout + run.stderr
    )


# M2：在提交镜像头之前**擅自改动了活动槽**。
# 这破坏"活动槽内容恒不变"（不变量 ①）—— 掉电安全的地基。
# 写成 16 B（= 2 个双字）而不是 8 B 是有意的：操作表只对"可细分"的操作枚举
# 前缀，1 个双字的写只会被枚举到 stop=0（什么都没写）这一处，抓不到。
_MUT_ACTIVE_SLOT_ANCHOR = """    /* FLUSH：整头一次写入。**在这一步之前，目标槽头部恒为 0xFFFFFFFF** ——"""

_MUT_ACTIVE_SLOT_REPL = """    {   /* MUTATION M2: 擅自动活动槽 */
        uint8_t mut[16];
        memset(mut, 0x00, sizeof(mut));
        (void)io->program(io->ctx, src_slot, mut, sizeof(mut));
    }

    /* FLUSH：整头一次写入。**在这一步之前，目标槽头部恒为 0xFFFFFFFF** ——"""


def test_l6_bench_detects_active_slot_corruption(tmp_path):
    """在提交镜像头之前擅动活动槽，L6 必须报活动槽被改。

    注：这一处改动在**无故障基线**里就会生效，因此通常由 case 0 抓到；若基线
    检查被移除，注入循环里的不变量 ① 仍应报 `active slot changed`。故断言
    两者共有的前缀 `active slot`。
    """
    src_text = (_TEMPLATES_DIR / _DRIVER_C).read_text(encoding="utf-8")
    assert src_text.count(_MUT_ACTIVE_SLOT_ANCHOR) == 1, (
        "变异锚点在模板里不再唯一 —— 模板改过，请同步更新本测试的锚点"
    )
    mutated = src_text.replace(_MUT_ACTIVE_SLOT_ANCHOR, _MUT_ACTIVE_SLOT_REPL)

    run = _run_bench(tmp_path, override={_DRIVER_C: mutated})
    assert run.returncode != 0, (
        "L6 没有抓到「活动槽被改动」—— 说明不变量 ① 形同虚设：\n%s" % run.stdout
    )
    assert "active slot" in run.stdout + run.stderr, (run.stdout + run.stderr)


# ---------------------------------------------------------------------------
# 版本号契约：信封里的 fw_version 必须与新镜像头一致，否则设备报 -19
#
# 2026-09-17 真机实测：设备报 `HW2C_FAULT_FOTA_APPLY`，活动槽毫发无损。
# 把真机那份 `drv_fota_delta.c` 编到主机上、用真实产物重放，拿到的是
# `FOTA_DELTA_E_CRC(-19)` —— 而真实原因只是**信封声明的版本号**与**新镜像
# 头部里的版本号**对不上（1 vs 2）。
#
# 机制：设备在 FLUSH 阶段把信封里的 fw_version 写进目标槽镜像头，而镜像 CRC
# 的覆盖区从 magic 起、包含 fw_version 那 4 字节 ⇒ 两者不一致时算出的 CRC
# 与信封里的 new_crc32 必然不同。
#
# 主机侧的护栏（`delta_tool.build()` 拒绝不一致的 fw_version，见
# test_delta_tool.py 的 2b 组）只能证明"我们不再生成这种补丁"。本用例把
# **设备上的后果**也钉住：这样一旦设备侧改成报一个专门的错误码，这里会红，
# 那时才可以把主机侧的措辞与策略放宽。
# ---------------------------------------------------------------------------

_VERSION_GUARD_HARNESS = (
    Path(__file__).resolve().parent / "harness" / "fota_delta_version_guard.c"
)
_VG_VECTORS_H = "fota_version_guard_vectors.h"


def _emit_version_guard_vectors(path, old_image, new_image,
                                patch_ok, patch_bad):
    """把四个向量写成 C 头。数组名统一带 vg_ 前缀，避免与 L6 的向量冲突。"""
    def arr(name, blob):
        lines = ["static const unsigned char %s[%d] = {" % (name, len(blob))]
        for i in range(0, len(blob), 16):
            row = ", ".join("0x%02X" % b for b in blob[i:i + 16])
            lines.append("    %s," % row)
        lines.append("};")
        return "\n".join(lines)

    env_size = json.loads(
        (_GENERATOR_DIR / "data" / "fota_format.json").read_text(encoding="utf-8")
    )["delta_envelope"]["size"]

    parts = [
        "/* 自动生成，请勿手工编辑。见 generator/tests/test_fota_delta_l6.py */",
        "#ifndef __FOTA_VERSION_GUARD_VECTORS_H",
        "#define __FOTA_VERSION_GUARD_VECTORS_H",
        "",
        "#define VG_SLOT_A_BASE      %dUL" % SLOT_A_BASE,
        "#define VG_SLOT_B_BASE      %dUL" % SLOT_B_BASE,
        "#define VG_SLOT_B_SIZE      %dUL" % _SLOT_B_SIZE,
        "#define VG_PAGE_SIZE        %dUL" % _BUDGET["page_size"],
        "#define VG_ENV_SIZE         %d" % env_size,
        "#define VG_OLD_SIZE         %d" % len(old_image),
        "#define VG_NEW_SIZE         %d" % len(new_image),
        "#define VG_PATCH_OK_SIZE    %d" % len(patch_ok),
        "#define VG_PATCH_BAD_SIZE   %d" % len(patch_bad),
        "",
        arr("g_vg_old", old_image),
        arr("g_vg_new", new_image),
        arr("g_vg_patch_ok", patch_ok),
        arr("g_vg_patch_bad", patch_bad),
        "",
        "#endif /* __FOTA_VERSION_GUARD_VECTORS_H */",
        "",
    ]
    path.write_text("\n".join(parts), encoding="utf-8")


def _make_version_guard_patches():
    """造一对**只差信封版本号**的补丁，其余字节完全相同。

    `bad` 的做法：拿 `ok` 的字节，只改信封的 fw_version 那 4 字节，再按真源
    重算信封头 CRC16（它覆盖前 32 字节）—— 于是两个补丁都能通过
    `parse_envelope` 的全部**结构**校验，差异被压到"语义"这一层。
    """
    img = json.loads(
        (_GENERATOR_DIR / "data" / "fota_format.json").read_text(encoding="utf-8")
    )
    env = img["delta_envelope"]
    ver_off = env["fields"]["fw_version"]["offset"]
    crc_off = env["fields"]["hdr_crc16"]["offset"]
    crc_cover = crc_off                  # hdr_crc16 覆盖它之前的全部字节

    old_code = firmware_like(_OLD_IMAGE_SIZE - 208, seed=31)
    new_code = bytearray(firmware_like(_NEW_IMAGE_SIZE - 208, seed=32))
    new_code[32:32 + 96] = old_code[256:256 + 96]
    new_code = bytes(new_code)

    old_image = slot_image(old_code, fw_version=4, slot_base=SLOT_A_BASE)
    new_image = slot_image(new_code, fw_version=5, slot_base=SLOT_B_BASE)
    patch_ok = build(old_image, new_image)              # 默认取新镜像头的版本号

    bad = bytearray(patch_ok)
    # 刻意让版本号的**所有有效字节**都变：这样下面对"差异位置"的断言才有判别力
    # （若只改低字节，差异只剩 1 B，断言几乎形同虚设）。注意 fw_version 是 24 位，
    # 最高字节恒为 0，所以那里的差异最多 3 个字节。
    bad[ver_off:ver_off + 4] = (0x0A0B0C).to_bytes(4, "little")
    c16 = crc16_ccitt_false(bytes(bad[:crc_cover]))
    bad[crc_off:crc_off + 2] = c16.to_bytes(2, "little")
    patch_bad = bytes(bad)

    # 主张：两个补丁的差异**只允许**落在 fw_version 字段与 hdr_crc16 字段内。
    # 换句话说，补丁正文（lite 流）与信封的其余字段逐字节相同 —— 于是两者
    # 唯一可能的语义差异就是版本号。
    ver_range = set(range(ver_off, ver_off + 4))
    crc_range = set(range(crc_off, crc_off + 2))
    diff = {i for i in range(len(patch_ok)) if patch_ok[i] != patch_bad[i]}
    assert diff <= (ver_range | crc_range), (
        "差异跑到了 fw_version / hdr_crc16 之外：%r" % (sorted(diff),))
    assert diff & ver_range, "fw_version 字段根本没变，这个用例就没在测版本号不一致"
    assert diff & crc_range, "hdr_crc16 没重算 —— 坏补丁会先被结构校验拦下，测不到语义层"

    # 两者都必须通过结构校验，否则测的就不是"语义不一致"
    assert parse_envelope(patch_ok)["fw_version"] == 5
    assert parse_envelope(patch_bad)["fw_version"] == 0x0A0B0C
    for tag, blob in (("ok", patch_ok), ("bad", patch_bad)):
        parsed = parse_envelope(blob)
        assert parsed["old_size"] == len(old_image), tag
        assert parsed["new_size"] == len(new_image), tag
    return old_image, new_image, patch_ok, patch_bad


def _run_version_guard(tmp_path, override=None):
    """渲染真实模板 → 编译版本号契约台架 → 运行。"""
    gcc = _host_gcc()
    _hdr, src = _render_driver(tmp_path, override=override)
    old_image, new_image, patch_ok, patch_bad = _make_version_guard_patches()
    _emit_version_guard_vectors(tmp_path / _VG_VECTORS_H,
                                old_image, new_image, patch_ok, patch_bad)

    exe = tmp_path / ("vg.exe" if sys.platform.startswith("win") else "vg")
    cmd = [
        gcc, "-std=c99", "-O1", "-Wall", "-Wextra",
        "-I", str(tmp_path),
        "-I", str(_HPATCH_DIR),
        "-I", str(_TUZ_DIR),
        str(_VERSION_GUARD_HARNESS),
        str(src),
        str(_HPATCH_DIR / "hpatch_lite.c"),
        str(_TUZ_DIR / "tuz_dec.c"),
        "-o", str(exe),
    ]
    comp = subprocess.run(cmd, capture_output=True, text=True)
    assert comp.returncode == 0, (
        "版本号契约台架编译失败：\n%s\n%s" % (comp.stdout, comp.stderr)
    )
    return subprocess.run([str(exe)], capture_output=True, text=True, cwd=str(tmp_path))


def test_l6_version_mismatch_is_a_device_side_crc_failure(tmp_path):
    """只有信封版本号不同 → 设备必须报 -19（而不是成功）。

    这条把「版本号不一致 ⇒ 真机 E_CRC」的因果链钉在**设备侧真实代码**上。
    """
    run = _run_version_guard(tmp_path)
    out = run.stdout + run.stderr
    assert "RESULT: OK" in run.stdout, out
    assert run.returncode == 0, out
    assert "OK_RC=0" in run.stdout, out
    assert "BAD_RC=-19" in run.stdout, (
        "版本号不一致的补丁竟然没被设备侧拒绝，或错误码变了：\n%s" % out
    )


# 变异：把**两道**镜像 CRC 闸门都短路掉。此时"版本号不一致"的补丁会被放行，
# 上一条用例的 BAD_RC 断言必须失效 —— 证明那个 -19 真的来自 CRC 比较。
#
# ⚠️ 必须**同时**关掉两道，这与"测试有没有牙"直接相关：第一次只关掉了 7.5
# 的累积值比较，结果 -19 依旧出现（由 7.6 的 `fota_delta_verify()` 回读复算
# 给出）。若据此判定"断言与实现无关"就会得出完全错误的结论 —— 正确的结论是
# **这一处不变量有两道互相独立的闸门**（一个在内存里、一个回读 Flash）。
_MUT_SKIP_NEW_CRC = [
    # 7.5：解码过程中累积的覆盖区 CRC
    ("    c.crc ^= FOTA_CRC32_FINAL_XOR;\n"
     "    if (c.crc != env->new_crc32) {",
     "    c.crc ^= FOTA_CRC32_FINAL_XOR;\n"
     "    if (0 && c.crc != env->new_crc32) {"),
    # 7.6：回读目标槽再算一遍
    ("    return (crc == expected_crc) ? FOTA_DELTA_OK : FOTA_DELTA_E_CRC;",
     "    return FOTA_DELTA_OK;   /* MUTATION: 回读复核被短路 */"),
]


def test_l6_version_guard_has_teeth(tmp_path):
    """两道 CRC 闸门都短路后，上一条用例的判据必须失效。"""
    src_text = (_TEMPLATES_DIR / _DRIVER_C).read_text(encoding="utf-8")
    mutated = src_text
    for anchor, repl in _MUT_SKIP_NEW_CRC:
        assert mutated.count(anchor) == 1, (
            "变异锚点在模板里不再唯一（可能多了一道/少了一道闸门）—— "
            "模板改过，请同步更新本测试的锚点：\n%s" % anchor
        )
        mutated = mutated.replace(anchor, repl)

    run = _run_version_guard(tmp_path, override={_DRIVER_C: mutated})
    out = run.stdout + run.stderr
    assert "mismatched   rc=0" in run.stdout, (
        "两道 CRC 闸门都短路了，版本号不一致的补丁却仍被拒 —— "
        "说明另有一处判据在起作用（好消息，但请把这个用例的锚点更新到那一处）：\n%s"
        % out
    )
