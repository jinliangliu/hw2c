"""L5：YMODEM 接收通道（drv_fota_ymodem）的主机协议测试。

这一层解决什么问题
------------------
用户敲 `fota ymodem` 之后，主机用任意终端软件的 YMODEM 发送把 `.h2cd` 补丁
发过来。从"终端软件的第一个 'C'"到"设备提交 READY"，中间全是看不见的东西：
块号、块尾 CRC、末块的 0x1A 补齐、EOT 的两拍握手、结束块、CAN 中止、块超时、
重试上限。这些在真机串口日志里只能猜，在主机上可以逐条断言。

**四类只有 YMODEM 才有、且错了不报错的缺陷**，是这个文件存在的理由：

  1. **块尾 CRC 用错算法。** YMODEM 是 CRC-16/XMODEM（初值 0x0000，
     `'123456789' → 0x31C3`），本仓库帧协议是 CRC-16/CCITT-FALSE（初值
     0xFFFF，`→ 0x29B1`）。复用 `fota_crc16()` 之后"自研主机 ↔ 设备"完全互通
     （两侧错得一样），但与 **所有** 真实 YMODEM 软件都不通。判据只能是标准值，
     不能是"另一侧的实现也这么算"。
  2. **末块的 0x1A 补齐没截断。** 症状是"所有块 CRC 都对，但解出来的镜像最后
     一段是坏的"，而收尾的回读 CRC32 会把它报成 PATCH_CRC —— 指不到真凶。
  3. **CAN 在块内也被当中止。** 补丁正文是接近随机的字节流，一段几十 KB 的
     补丁按概率出现若干个 0x18；在块内识别会把正常传输随机打断。
  4. **块号错乱时重试上限打不穿。** 只要有一处"CRC 通过了就清零重试计数"，
     一个 CRC 合法、块号持续不对的主机就会让设备无限 NAK，UART 永远不还给
     CLI —— 而所有应答都是 NAK，看起来完全正常。

为什么要跑**渲染出的模板**，而不是生成工程里的 `test/*.c`
------------------------------------------------------
同帧协议的 L5：这里需要 vendored 解码器（收齐后要调 `fota_delta_apply`）、
Python 侧算好的向量（必须来自**另一份实现**）、以及变异测试。这三样生成工程
的主机测试环境刻意没有。跑同一批模板保证了"生成出来的 `drv_fota_ymodem.c`
长什么样，被测的就是什么样"。

向量为什么必须来自发送端实现
----------------------------
全部上行字节由 `generator/fota_ymodem_sender.py` 按 `ymodem_format.json`
构造，设备侧 `drv_fota_ymodem.c` 解析它们。若台架自己在 C 里拼块、算 CRC，
就变成"自己和自己对答案"。而那个发送端自己也被 stdlib 的独立算路钉住：

    binascii.crc_hqx(b'123456789', 0) == 0x31C3

跑什么
------
  1. 用生成器自己的 `build_context()` 造一个开启 bootloader+UART+CLI 的硬件
     上下文（顺带断言 YMODEM 通道确实被注入 —— 它与 `fota` 同时注入）；
  2. 渲染 `drv_fota_ymodem` / `drv_fota` / `fota_delta` / `fota_meta` /
     `drv_cli.h` / `hw2c_fault` / `mock_hal` 到临时目录；
  3. 用 `delta_fixtures` + `delta_tool.build()` 造一对真实镜像与真补丁；
  4. 用发送端组出 7 个"计划"（正常 1K / 正常 128 / 重复块 / 块号错乱 /
     主机取消 / 长度不符 / 多文件头 / 准入不过 / 正文不符），烘成 C 头文件；
  5. 用宿主 gcc 编译「渲染出的源码 + vendored 解码器 + 台架」并运行；
  6. 另外用**变异测试**把模板里五处关键策略改坏，要求台架必须报错。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES_DIR = _REPO_ROOT / "templates"

sys.path.insert(0, str(_REPO_ROOT))

from generator.context.bootloader_context import (  # noqa: E402
    fota_delta_budget,
    fota_l5_negative_envelopes,
)
from generator.context.builder import build_context  # noqa: E402
from generator.delta_tool import build as build_patch  # noqa: E402
from generator.fota_ymodem_sender import (  # noqa: E402
    CHECK_INPUT,
    Ymodem,
    _crc_oracle,
    crc16_xmodem,
    load_spec,
)
from generator.tests.delta_fixtures import (  # noqa: E402
    SLOT_A_BASE,
    SLOT_B_BASE,
    firmware_like,
    slot_image,
)

_HARNESS = Path(__file__).resolve().parent / "harness" / "fota_ymodem_l5_harness.c"
_HPATCH_DIR = _REPO_ROOT / "static" / "third_party" / "hpatch_lite"
_TUZ_DIR = _REPO_ROOT / "static" / "third_party" / "tinyuz" / "decompress"
_HW2C_CLI_DIR = _REPO_ROOT / "static" / "hw2c_cli"

_SLOT_A_SIZE = 0x40000 - 0x2000
_SLOT_B_SIZE = 0x40000

_OLD_CODE = 2048
_NEW_CODE = 3072

# 续传用例的现场：上次提交到记录的第 648 字节（8 对齐），
# 而"正文不同"这一点被放在记录偏移 148（= 信封 48 + 正文 100）——
# 必须**落在复核对的前缀之内**，否则 fail-fast 抓不到它，用例会退化成
# "全部收完再报 PATCH_CRC"（那样也"通过"了，但测的就不是这条路径）。
_SEED_PREFIX = 648
_CORRUPT_AT = 48 + 100


# ---------------------------------------------------------------------------
# 1. 上下文与模板渲染
# ---------------------------------------------------------------------------

def _cli_peripheral() -> dict:
    return {
        "name": "cli",
        "type": "Internal_CLI",
        "uart": "usart2",
        "extra": {"prompt": "l5> "},
    }


def _hardware() -> dict:
    return {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [
            {"id": "PA2", "function": "USART2_TX", "af": 1},
            {"id": "PA3", "function": "USART2_RX", "af": 1},
            {"id": "PC0", "function": "GPIO_Output", "label": "LED",
             "active_level": "low"},
        ],
        "peripherals": [
            {"name": "usart2", "type": "UART_Serial", "instance": "USART2",
             "interface": "uart", "extra": {"baudrate": 115200}},
            _cli_peripheral(),
        ],
        "bootloader": {"enabled": True},
    }


def _render_sources(tmp_path: Path, override: dict | None = None) -> dict:
    """把 L5 需要的全部模板渲染到 tmp_path。

    `override` 用来把某个模板换成**改坏的源码**（变异测试用），键是相对于
    templates/ 的路径；未覆盖的模板一律从仓库里原样取。
    """
    import jinja2

    from generator.jinja_filters import register_filters

    ctx = build_context(_hardware(), "l5")

    assert ctx.get("has_bootloader") is True
    assert ctx.get("has_uart") is True
    assert ctx.get("has_cli") is True
    assert ctx.get("has_fota_receive") is True, (
        "bootloader+UART+CLI 齐备时 has_fota_receive 必须为真"
    )
    # YMODEM 通道与 fota 同时注入 —— 不做开关。这条断言是"它确实被生成了"
    # 的闸门：没有它，整条 YMODEM 路径可能悄悄从产物里消失，而所有测试
    # 都在测一个不存在的驱动（那正是 FR-14 长期"标着 ✅ 却从未编译过"的形态）。
    assert ctx.get("ymodem"), "ymodem 真源必须已经进了上下文"
    assert int(ctx["ymodem"]["blk_max_frame"]) == 1029, (
        "块缓冲长度必须来自真源（总长 1029 = 1024 + 5）"
    )
    assert ctx.get("cli_name") == "cli"

    if override:
        root = tmp_path / "_templates"
        for rel, text in override.items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8")
        loader = jinja2.ChoiceLoader([
            jinja2.FileSystemLoader(str(root)),
            jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        ])
    else:
        loader = jinja2.FileSystemLoader(str(_TEMPLATES_DIR))

    env = jinja2.Environment(loader=loader, trim_blocks=True, lstrip_blocks=True)
    register_filters(env)

    fota_peri = {"name": "fota"}
    ym_peri = {"name": "fota_ymodem"}
    fota_delta_peri = {"name": "fota_delta"}
    fota_meta_peri = {"name": "fota_meta"}
    iwdg_peri = {"name": "iwdg", "wdg_timeout_ms": 5000}
    cli_peri = _cli_peripheral()

    render_list = [
        ("drivers/drv_fota.h.j2", "drv_fota.h", fota_peri),
        ("drivers/drv_fota.c.j2", "drv_fota.c", fota_peri),
        ("drivers/drv_fota_ymodem.h.j2", "drv_fota_ymodem.h", ym_peri),
        ("drivers/drv_fota_ymodem.c.j2", "drv_fota_ymodem.c", ym_peri),
        ("drivers/fota_delta.h.j2", "drv_fota_delta.h", fota_delta_peri),
        ("drivers/fota_delta.c.j2", "drv_fota_delta.c", fota_delta_peri),
        ("drivers/drv_cli.h.j2", "drv_cli.h", cli_peri),
        ("drivers/fota_meta.h.j2", "drv_fota_meta.h", fota_meta_peri),
        ("drivers/fota_meta.c.j2", "drv_fota_meta.c", fota_meta_peri),
        ("drivers/drv_iwdg.h.j2", "drv_iwdg.h", iwdg_peri),
        ("src/hw2c_fault.h.j2", "hw2c_fault.h", None),
        ("src/hw2c_fault.c.j2", "hw2c_fault.c", None),
        ("test/mock_hal.h.j2", "mock_hal.h", None),
        ("test/mock_hal.c.j2", "mock_hal.c", None),
    ]

    out = {}
    for tmpl, name, peri in render_list:
        local = dict(ctx)
        if peri is not None:
            local["peripheral"] = peri
            local["model"] = {"type": "Internal_FOTA"}
        text = env.get_template(tmpl).render(**local)
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        out[name] = path
    return out


# ---------------------------------------------------------------------------
# 2. 向量
# ---------------------------------------------------------------------------

def _build_patch() -> tuple:
    old_code = firmware_like(_OLD_CODE, seed=303)
    new_code = bytearray(firmware_like(_NEW_CODE, seed=404))
    new_code[128:128 + 256] = old_code[512:512 + 256]

    old_image = slot_image(old_code, fw_version=1, slot_base=SLOT_A_BASE)
    new_image = slot_image(bytes(new_code), fw_version=2, slot_base=SLOT_B_BASE)
    patch = build_patch(old_image, new_image, fw_version=2, compress=False)
    return old_image, new_image, patch


def _c_array(name: str, blob: bytes, per_line: int = 16) -> str:
    lines = ["static const unsigned char %s[%d] = {" % (name, len(blob))]
    for i in range(0, len(blob), per_line):
        row = ", ".join("0x%02X" % b for b in blob[i:i + per_line])
        lines.append("    %s," % row)
    lines.append("};")
    return "\n".join(lines)


def _c_or_empty(name: str, blob: bytes) -> str:
    """长度可以为 0 的数组：C 不允许零长度数组，用一个占位字节。"""
    if not blob:
        return "static const unsigned char %s[1] = { 0 };" % name
    return _c_array(name, blob)


def _c_string(text: str) -> str:
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif 0x20 <= code < 0x7F:
            out.append(ch)
        else:
            out.append("".join("\\%03o" % b for b in ch.encode("utf-8")))
    out.append('"')
    return "".join(out)


def _emit_plan(parts: list, key: str, plan: list) -> None:
    """把一个 [(send, expect, note)] 计划烘成 ym_plan_t。

    ⚠️ 期望值必须由 **Python 侧**给出（它读的是真源与协议），不能由 C 台架
    自己推 —— 那又变成自己和自己对答案了。
    """
    batch = b"".join(s for s, _, _ in plan)
    exp = b"".join(e for _, e, _ in plan)

    snd_off, snd_len, exp_off, exp_len = [], [], [], []
    so = eo = 0
    for s, e, _ in plan:
        snd_off.append(so)
        snd_len.append(len(s))
        exp_off.append(eo)
        exp_len.append(len(e))
        so += len(s)
        eo += len(e)
    assert so == len(batch) and eo == len(exp)

    parts.append(_c_or_empty("g_%s_batch" % key, batch))
    parts.append("")
    parts.append("static const unsigned short g_%s_snd_off[] = {%s};"
                 % (key, ", ".join(str(o) for o in snd_off)))
    parts.append("static const unsigned short g_%s_snd_len[] = {%s};"
                 % (key, ", ".join(str(n) for n in snd_len)))
    parts.append(_c_or_empty("g_%s_exp" % key, exp))
    parts.append("")
    parts.append("static const unsigned short g_%s_exp_off[] = {%s};"
                 % (key, ", ".join(str(o) for o in exp_off)))
    parts.append("static const unsigned short g_%s_exp_len[] = {%s};"
                 % (key, ", ".join(str(n) for n in exp_len)))
    parts.append("static const char *const g_%s_notes[] = {" % key)
    for _, _, note in plan:
        parts.append("    %s," % _c_string(note))
    parts.append("};")
    parts.append("")
    parts.append("static const ym_plan_t g_plan_%s = {" % key)
    parts.append("    g_%s_batch, %du," % (key, len(batch)))
    parts.append("    g_%s_snd_off, g_%s_snd_len," % (key, key))
    parts.append("    g_%s_exp, g_%s_exp_off, g_%s_exp_len," % (key, key, key))
    parts.append("    g_%s_notes, %du" % (key, len(plan)))
    parts.append("};")
    parts.append("")


def _emit_vectors_header(path: Path, old_image: bytes, new_image: bytes,
                         record: bytes, ym: Ymodem) -> None:
    assert _CORRUPT_AT < _SEED_PREFIX, (
        "『正文不符』的位置必须落在复核对的前缀之内，否则 fail-fast 的用例"
        "退化成『全部收完再报 PATCH_CRC』"
    )
    # 末块按块长补齐 ⇒ 记录长度必须**不是**块长的整数倍，否则这条路径根本
    # 没被走到（两条 block_size 都要覆盖，所以两个都要断言）。
    assert len(record) % ym.size_128 != 0, (
        "记录长度 %d 是 128 的整数倍 ⇒ 末块没有 0x1A 补齐可截，测试白跑"
        % len(record)
    )
    assert len(record) % ym.size_1024 != 0, (
        "记录长度 %d 是 1024 的整数倍 ⇒ 1024 块路径的截断没被覆盖" % len(record)
    )

    budget = fota_delta_budget({})
    negs = fota_l5_negative_envelopes({"_app_b_size": _SLOT_B_SIZE})
    over = negs["over_admission"]

    # 准入不过的"记录"：信封来自共享的负例帮助函数，正文按信封声明的
    # patch_size 补齐，这样**长度交叉核对会通过**，唯一违规的就是准入 ——
    # 否则这条用例测的是"长度不符"，与 case 7 重复。
    from generator.context.bootloader_context import load_fota_format
    _fmt = load_fota_format()
    ef = _fmt["delta_envelope"]["fields"]
    env_size = int(_fmt["delta_envelope"]["size"])
    off = int(ef["patch_size"]["offset"])
    env = over["env"]
    patch_size = (env[off] | (env[off + 1] << 8)
                  | (env[off + 2] << 16) | (env[off + 3] << 24))
    over_record = env + bytes(patch_size)
    assert len(over_record) == env_size + patch_size

    # "续传时主机重发了另一份记录"：信封**完全相同**、正文改一个字节。
    # 信封相同是刻意的 —— 续传判定比的就是信封，所以它会判定为"同一条补丁"，
    # 从而走到"逐字节复核前缀"那一步；正文不同则必须在那里被抓住。
    corrupt_record = bytearray(record)
    corrupt_record[_CORRUPT_AT] ^= 0xFF
    corrupt_record = bytes(corrupt_record)
    assert corrupt_record[:env_size] == record[:env_size], "信封必须保持一致"
    assert corrupt_record[_CORRUPT_AT] != record[_CORRUPT_AT]

    name = "fota_demo.h2cd"

    parts = [
        "/* 自动生成，请勿手工编辑。见 generator/tests/test_fota_ymodem_l5.py */",
        "#ifndef __FOTA_YMODEM_VECTORS_H",
        "#define __FOTA_YMODEM_VECTORS_H",
        "",
        "#define YM_ENV_SIZE            %d" % env_size,
        "#define YM_PAGE_SIZE           %d" % budget["page_size"],
        "#define YM_RECORD_SIZE         %d" % len(record),
        "#define YM_RECORD_CORRUPT_AT   %d" % _CORRUPT_AT,
        "#define YM_SEED_PREFIX         %d" % _SEED_PREFIX,
        "#define YM_SLOT_A_BASE         0x%08XUL" % SLOT_A_BASE,
        "#define YM_SLOT_A_SIZE         %dU" % _SLOT_A_SIZE,
        "#define YM_SLOT_B_BASE         0x%08XUL" % SLOT_B_BASE,
        "#define YM_SLOT_B_SIZE         %dU" % _SLOT_B_SIZE,
        "#define YM_OLD_IMAGE_SIZE      %d" % len(old_image),
        "#define YM_NEW_IMAGE_SIZE      %d" % len(new_image),
        "",
        "/* 时序取自真源，台架按它推进 mock 时钟 */",
        "#define YM_HS_INTERVAL_MS       %dU" % ym.hs_interval_ms,
        "#define YM_HS_MAX_ATTEMPTS      %dU" % ym.hs_max_attempts,
        "#define YM_BLOCK_TIMEOUT_MS     %dU" % ym.block_timeout_ms,
        "#define YM_TERMINATOR_TIMEOUT_MS %dU" % int(load_spec()["handshake"]["terminator_timeout_ms"]),
        "#define YM_NAK_MAX_RETRIES      %dU" % ym.nak_max_retries,
        "",
        "/* 准入不过时**期望**的错误码，由生成器共享的负例表给出 ——",
        " * 台架不自己写一遍那个枚举，免得两处各有一个真源。 */",
        "#define YM_EXPECT_OVER_ADMISSION (%s)" % over["expect"],
        "",
        "typedef struct {",
        "    const unsigned char  *batch;",
        "    unsigned int          batch_len;",
        "    const unsigned short *snd_off;",
        "    const unsigned short *snd_len;",
        "    const unsigned char  *exp;",
        "    const unsigned short *exp_off;",
        "    const unsigned short *exp_len;",
        "    const char *const    *notes;",
        "    unsigned int          nsteps;",
        "} ym_plan_t;",
        "",
        _c_array("g_ym_record", record),
        "",
        _c_array("g_ym_old_image", old_image),
        "",
        _c_array("g_ym_new_image", new_image),
        "",
    ]

    _emit_plan(parts, "1k", ym.batch_plan(name, record, ym.size_1024))
    _emit_plan(parts, "128", ym.batch_plan(name, record, ym.size_128))
    _emit_plan(parts, "dup", ym.dup_plan(name, record, ym.size_1024))
    _emit_plan(parts, "desync", ym.desync_plan(name, record, ym.size_1024))
    _emit_plan(parts, "cancel", ym.header_only_plan(name, record))
    _emit_plan(parts, "badlen", ym.wrong_length_plan(name, record, 8))
    _emit_plan(parts, "multi", ym.multi_header_plan(name, record, "second.h2cd"))
    _emit_plan(parts, "overadm", ym.reject_plan("big.h2cd", over_record))
    _emit_plan(parts, "corrupt", ym.reject_plan(name, corrupt_record))

    parts.append("#endif /* __FOTA_YMODEM_VECTORS_H */")
    parts.append("")

    path.write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------
# 3. 编译并运行
# ---------------------------------------------------------------------------

def _host_gcc() -> str:
    for cand in (os.environ.get("H2C_GCC_PATH"), shutil.which("gcc")):
        if cand and shutil.which(cand):
            return cand
    for cand in ("C:/mingw64/bin/gcc", "C:/mingw32/bin/gcc"):
        if os.path.exists(cand):
            return cand
    pytest.skip("host gcc not available")


def _run_bench(tmp_path: Path, override: dict | None = None,
               extra_flags: tuple = ()) -> subprocess.CompletedProcess:
    gcc = _host_gcc()
    srcs = _render_sources(tmp_path, override=override)

    old_image, new_image, record = _build_patch()
    ym = Ymodem()

    # 补丁必须够长：太短的话"多块""块号回绕""续传前缀"这些用例在结构上
    # 构造不出来，会静默退化成"什么也没测到"。
    assert len(record) > _SEED_PREFIX + ym.size_1024, (
        "记录只有 %d B，装不下续传现场（需要 > %d）"
        % (len(record), _SEED_PREFIX + ym.size_1024)
    )
    assert len(ym.split_payload(record, ym.size_1024)) >= 2, (
        "1024 字节块下只有 1 个数据块 ⇒ 多块路径没被覆盖，加大 _NEW_CODE"
    )

    _emit_vectors_header(tmp_path / "fota_ymodem_vectors.h",
                         old_image, new_image, record, ym)

    (tmp_path / "ym_l5_config.h").write_text(
        "/* 自动生成，见 generator/tests/test_fota_ymodem_l5.py */\n"
        "#ifndef __YM_L5_CONFIG_H\n"
        "#define __YM_L5_CONFIG_H\n"
        "/* CLI 头文件名由工程的 CLI 外设名决定（drv_<cli_name>.h） */\n"
        '#define L5_CLI_HEADER "drv_cli.h"\n'
        "#endif\n",
        encoding="utf-8",
    )

    exe = tmp_path / ("yml5.exe" if sys.platform.startswith("win") else "yml5")
    cmd = [
        gcc, "-std=c99", "-O1", "-Wall", "-Wextra", "-DTEST",
        # ⚠️ `-Werror=div-by-zero` 不是洁癖：`(uint8_t)256U` 恰好等于 0，而那
        # 一行曾经真的写成了 `% (uint8_t)YMODEM_BLK_MODULUS`。它只是**警告**，
        # 一份把警告当噪音的构建会带着运行时除零上板（真机上是 HardFault）。
        "-Werror=div-by-zero",
        *extra_flags,
        "-I", str(tmp_path),
        "-I", str(_HPATCH_DIR),
        "-I", str(_TUZ_DIR),
        "-I", str(_HW2C_CLI_DIR),
        str(_HARNESS),
        str(srcs["drv_fota.c"]),
        str(srcs["drv_fota_ymodem.c"]),
        str(srcs["drv_fota_delta.c"]),
        str(srcs["drv_fota_meta.c"]),
        str(srcs["hw2c_fault.c"]),
        str(srcs["mock_hal.c"]),
        str(_HPATCH_DIR / "hpatch_lite.c"),
        str(_TUZ_DIR / "tuz_dec.c"),
        "-o", str(exe),
    ]
    comp = subprocess.run(cmd, capture_output=True, text=True)
    assert comp.returncode == 0, (
        "YMODEM L5 台架编译失败：\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (comp.stdout, comp.stderr)
    )
    return subprocess.run([str(exe)], capture_output=True, text=True,
                          cwd=str(tmp_path))


# ---------------------------------------------------------------------------
# 4. 自检：发送端的 CRC 判据（不依赖 C 编译，先保证真值来源是对的）
# ---------------------------------------------------------------------------

def test_crc_is_xmodem_not_ccitt_false():
    """YMODEM 的块尾 CRC 必须是 CRC-16/XMODEM（初值 0x0000），不是 0xFFFF。

    判据是**标准 check 值**：
        CRC-16/XMODEM       '123456789' → 0x31C3   ← YMODEM 用这个
        CRC-16/CCITT-FALSE  '123456789' → 0x29B1   ← 本仓库帧协议用那个
    再用 stdlib 的独立算路（`binascii.crc_hqx`）对一次答案。
    """
    ym = Ymodem()
    assert ym.crc_init == 0x0000
    assert ym.crc_check == 0x31C3

    mine = crc16_xmodem(CHECK_INPUT, ym.crc_poly, ym.crc_init)
    assert mine == 0x31C3, "自研实现算出的 check 值不是标准值"
    assert _crc_oracle(CHECK_INPUT) == 0x31C3, "stdlib 的独立算路也不认同"

    # 反面对照：把初值换成帧协议那个，结果必须是 0x29B1 ——
    # 这条断言的意义是"两个变体确实不同"，而不是"它们看起来差不多"。
    assert crc16_xmodem(CHECK_INPUT, 0x1021, 0xFFFF) == 0x29B1


def test_sender_rejects_ccitt_false_spec(tmp_path):
    """真源一旦被改成 CCITT-FALSE 的初值，发送端必须**拒绝工作**。

    这条是"两侧各写一份"的闸门：如果有人"顺手"把 `ymodem_format.json` 的
    init 改成 65535 好与帧协议统一，构造 `Ymodem` 会立刻抛异常 —— 而不是
    让设备与发送端一起错、测试全绿、真实终端软件全连不上。

    两条检查分别挡住两种改法（都在构造期，不在发送期）：
      · 只改 init、check 值留着 ⇒ 自研实现算出来与声明的 check 不符；
      · init 与 check **一起**改成自洽的 CCITT-FALSE 值 ⇒ 自研实现自洽了，
        但 stdlib 的独立算路（`binascii.crc_hqx`）不认同 —— 也就是本例。
    第二种才是真正危险的：它是一份"自己跟自己完全一致"的错真源。
    """
    spec = load_spec()
    bad = dict(spec)
    bad["crc16"] = dict(spec["crc16"])
    bad["crc16"]["init"] = 0xFFFF
    bad["crc16"]["check"] = 0x29B1
    bad["crc16"]["check_hex"] = "0x29B1"

    with pytest.raises(ValueError, match="不符"):
        Ymodem(bad)


# ---------------------------------------------------------------------------
# 5. 主用例
# ---------------------------------------------------------------------------

def test_l5_ymodem(tmp_path):
    """握手 / 块解析 / 末块截断 / 重复块 / 中止 / 超时 / 续传：逐条断言。"""
    run = _run_bench(tmp_path)
    assert run.returncode == 0, (
        "YMODEM L5 未通过：\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (run.stdout, run.stderr)
    )
    assert "RESULT: OK" in run.stdout, run.stdout
    # 每个用例都必须真的跑过（防止有人在 main 里注释掉一条）
    for n in range(1, 15):
        assert ("case %d:" % n) in run.stdout, (
            "case %d 没有出现在输出里 —— 台架里的用例被删了？\n%s" % (n, run.stdout)
        )


def test_vectors_exercise_both_block_sizes():
    """向量必须真的覆盖 128 与 1024 两条组块路径，且末块确实有补齐。

    防的是"向量悄悄退化"：记录长度一旦成了块长的整数倍，末块就没有 0x1A
    可以截，那条最贵的截断逻辑就再也没被测过 —— 而所有断言依然全绿。
    """
    _, _, record = _build_patch()
    ym = Ymodem()

    assert len(record) % ym.size_128 != 0
    assert len(record) % ym.size_1024 != 0

    blocks_128 = ym.split_payload(record, ym.size_128)
    blocks_1k = ym.split_payload(record, ym.size_1024)
    # 末块必须**确实**带上了补齐字节，否则这条路径没被走到
    assert blocks_128[-1][1][-1] == ym.PAD
    assert blocks_1k[-1][1][-1] == ym.PAD
    # 末块补齐后仍是满块
    assert len(blocks_128[-1][1]) == ym.size_128
    assert len(blocks_1k[-1][1]) == ym.size_1024
    # 128 字节块应当给出明显更多的块（否则"多块/回绕"覆盖不到）
    assert len(blocks_128) >= 8

    # 块号必须**从 1 开始**、并且能回绕（第 256 个块是 0）
    assert blocks_128[0][0] == 1
    assert set(n for n, _ in blocks_128).issubset(set(range(0, 256)))


# ---------------------------------------------------------------------------
# 6. 变异测试：证明这个测试台会红
#
# 每个用例把模板里**一处关键策略**改坏，然后要求 L5 报错。锚点断言保证
# 模板一旦重构，这些测试会先失败、逼着人来更新 —— 一个悄悄失效的变异测试
# 比没有变异测试更糟。
# ---------------------------------------------------------------------------

_YM_C = "drivers/drv_fota_ymodem.c.j2"
_YM_H = "drivers/drv_fota_ymodem.h.j2"
_FOTA_C = "drivers/drv_fota.c.j2"


def _mutate(tmp_path: Path, rel: str, anchor: str, repl: str,
            why: str) -> subprocess.CompletedProcess:
    text = (_TEMPLATES_DIR / rel).read_text(encoding="utf-8")
    assert text.count(anchor) == 1, (
        "%s 的变异锚点不再唯一（出现 %d 次）—— 模板改过，请同步更新本测试的锚点"
        % (rel, text.count(anchor))
    )
    run = _run_bench(tmp_path, override={rel: text.replace(anchor, repl)})
    assert run.returncode != 0, (
        "L5 没有抓到「%s」：\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (why, run.stdout, run.stderr)
    )
    return run


# M1：末块不再按块 0 声明的长度截断。
# 破坏的是"0x1A 补齐不属于文件"这条 —— 症状是"所有块 CRC 都对，但补丁
# 被回读 CRC32 判成坏的"，错误信息指不到真凶。
_M1_ANCHOR = "    rc = fota_session_feed(payload, take);"
_M1_REPL = "    rc = fota_session_feed(payload, plen);   /* MUTATION M1: 不截断 */"


def test_l5_bench_detects_missing_final_block_truncation(tmp_path):
    """末块不截断，L5 必须报错。"""
    _mutate(tmp_path, _YM_C, _M1_ANCHOR, _M1_REPL,
            "末块的 0x1A 补齐没有被截掉")


# M2：块尾 CRC 通过之后把重试计数清零。
# 这一改让"CRC 合法、块号持续错乱"的链路永远打不穿重试上限 ⇒ 无限 NAK、
# UART 永不交还，而所有应答都是 NAK、看起来完全正常。
_M2_ANCHOR = """    if (ym_crc16(payload, plen) != crc_rx) {
        ym_nak();
        return;
    }"""
_M2_REPL = """    if (ym_crc16(payload, plen) != crc_rx) {
        ym_nak();
        return;
    }
    g_retry = 0U;   /* MUTATION M2: CRC 通过就清零重试计数 */"""


def test_l5_bench_detects_retry_counter_reset_on_crc_pass(tmp_path):
    """CRC 通过就清零重试计数，L5 必须报错。"""
    _mutate(tmp_path, _YM_C, _M2_ANCHOR, _M2_REPL,
            "重试计数在 CRC 通过之后被清零（重试上限打不穿）")


# M3：块尾 CRC 的初值退回 0xFFFF（也就是本仓库帧协议那个变体）。
# 这正是 A3 类缺陷的成因：自研主机与设备完全互通、自测全绿，但与所有真实
# YMODEM 软件都不通。
_M3_ANCHOR = '#define YMODEM_CRC_INIT         {{ "0x%04XU" | format(ymodem.crc_init) }}'
_M3_REPL = '#define YMODEM_CRC_INIT         0xFFFFU   /* MUTATION M3: 用错 CRC 变体 */'


def test_l5_bench_detects_wrong_crc_variant(tmp_path):
    """块尾 CRC 初值改成 0xFFFF，L5 必须报错。"""
    _mutate(tmp_path, _YM_H, _M3_ANCHOR, _M3_REPL,
            "块尾 CRC 用了帧协议那个变体（与所有真实 YMODEM 软件不通）")


# M4：续传时不再逐字节复核已落盘的前缀。
# 这一改不会让任何"正常路径"变红 —— 它会一直传到收尾才由回读 CRC32 发现，
# 也就是说"快速失败"退化成了"多传一遍再失败"。
_M4_ANCHOR = """            if (*(const uint8_t *)(uintptr_t)(g_staging_base + g_stream_pos) != b) {
                g_last_error = FOTA_E_STAGING_LOST;
                return -1;
            }"""
_M4_REPL = """            /* MUTATION M4: 不再复核已落盘的前缀 */"""


def test_l5_bench_detects_missing_prefix_replay_check(tmp_path):
    """续传不再复核前缀，L5 必须报错（fail-fast 退化）。"""
    _mutate(tmp_path, _FOTA_C, _M4_ANCHOR, _M4_REPL,
            "续传时不再逐字节复核已落盘的前缀")


# M5：重复块不再"再 ACK 一次"，而是掉进序号检查被 NAK。
# 后果是：主机每丢一个 ACK 就会重发被 NAK，然后一直重发到超时 —— 一次本来
# 只丢了一个字节的传输变成整体失败。
_M5_ANCHOR = """        && (blk_no == (uint8_t)(g_expect_blk - 1U))) {
        ym_ack();
        return;
    }"""
_M5_REPL = """        && (blk_no == (uint8_t)(g_expect_blk - 1U))) {
        /* MUTATION M5: 重复块不再被就地 ACK */
    }"""


def test_l5_bench_detects_duplicate_block_not_acked(tmp_path):
    """重复块不再被 ACK，L5 必须报错。"""
    _mutate(tmp_path, _YM_C, _M5_ANCHOR, _M5_REPL,
            "重复块被 NAK（丢一个 ACK 就整体失败）")
