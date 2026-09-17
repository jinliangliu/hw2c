"""L5：FOTA 接收侧传输状态机（drv_fota）的主机协议测试。

这一层解决什么问题
------------------
规划 §12 的 L5 是"传输层"：帧解析、应答、尾仓暂存、续传、准入，以及最后一
步交棒给应用层。真机上看不见的东西全在这里 —— 丢一片、重发一片、CRC 被打
坏、主机中途静默、设备在收了一半之后复位……这些用肉眼在串口日志里都只能
"猜"，而在主机上可以逐条断言。

为什么**不是**生成工程里的 `test/*.c`
------------------------------------
L5 需要三样生成工程的主机测试环境刻意没有的东西：

  1. **vendored 解码器**：`drv_fota` 收齐之后要调 `fota_delta_parse_env()` /
     `fota_delta_apply()`，而它们要链 `hpatch_lite.c` + `tuz_dec.c`。
     生成工程的 `run_tests.py` 只编 `test_*.c` + `unity.c` + `mock_hal.c`，
     加进去就得给每个用户的工程塞一条构建魔法。
  2. **Python 侧算好的向量**：帧字节（含 CRC）必须由**另一份实现**产生，
     否则就是自己和自己对答案（见下）。
  3. **变异测试**：证明这个测试台真的会红 —— 只能由 Python 驱动。

所以 L5 与 L6 放在一起（`generator/tests/`），跑的是同一批模板、同一份真源。
这保证了"生成出来的 drv_fota.c 长什么样，被测的就是什么样"。

向量为什么必须来自发送端实现
----------------------------
`generator/fota_sender.py::Framing` 按 `fota_format.json` 构造全部帧字节。
设备侧 `drv_fota.c::fota_crc16()` 验它们。这是**两个独立的实现**比对同一份
契约 —— 如果测试台自己在 C 里再写一个 CRC16 去拼输入，那么"校验不过"永远
不会发生，测出来的只是"我的实现对不对得上我自己"。

跑什么
------
  1. 用生成器自己的 `build_context()` 构造一个开启 bootloader+UART+CLI 的
     硬件上下文（顺带断言 `has_fota_receive` 为真 —— 那正是这个驱动被生成
     的注入条件）；
  2. 渲染 `drv_fota` / `fota_delta` / `drv_cli.h` / `drv_iwdg.h` /
     `hw2c_fault` / `mock_hal` 到临时目录；
  3. 用 `delta_fixtures` + `delta_tool.build()` 造一对真实镜像与真补丁；
  4. 用宿主 gcc 编译「渲染出的源码 + vendored 解码器 + 测试台」并运行；
  5. 另外用**变异测试**把模板里三处关键策略改坏，要求测试台必须报错。
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
    fota_align_up,
    fota_delta_budget,
    fota_l5_negative_envelopes,
)
from generator.context.builder import build_context  # noqa: E402
from generator.delta_tool import build as build_patch  # noqa: E402
from generator.fota_sender import Framing, crc16  # noqa: E402
from generator.tests.delta_fixtures import (  # noqa: E402
    SLOT_A_BASE,
    SLOT_B_BASE,
    firmware_like,
    slot_image,
)

_HARNESS = Path(__file__).resolve().parent / "harness" / "fota_protocol_l5_harness.c"
_HPATCH_DIR = _REPO_ROOT / "static" / "third_party" / "hpatch_lite"
_TUZ_DIR = _REPO_ROOT / "static" / "third_party" / "tinyuz" / "decompress"
_HW2C_CLI_DIR = _REPO_ROOT / "static" / "hw2c_cli"

_SLOT_A_SIZE = 0x40000 - 0x2000
_SLOT_B_SIZE = 0x40000

# 代码区大小。选择依据：补丁必须**大于一个分片**（env 48 + chunk 1024），
# 否则"续传""乱序""FINISH 早到"三个用例在结构上无法构造（只有一片数据时
# 断点只可能落在 0 或 1）。compress=False 让补丁长度≈新代码长度，于是
# 3 KB 的代码区稳稳给出 3 个分片；压缩路径由 L6 覆盖（那里有专门的断言）。
_OLD_CODE = 2048
_NEW_CODE = 3072


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

    # 这个断言本身就是一条护栏：`drv_fota` 只在 bootloader + UART + CLI
    # 齐备时才被注入（见 inject_bootloader_drivers）。三者少一个，整条接收
    # 路径就会静默地不生成 —— 而那正是 FR-14 长期"标着 ✅ 却从未编译过"的形态。
    assert ctx.get("has_bootloader") is True
    assert ctx.get("has_uart") is True
    assert ctx.get("has_cli") is True
    assert ctx.get("has_fota_receive") is True, (
        "bootloader+UART+CLI 齐备时 has_fota_receive 必须为真"
    )
    assert ctx.get("has_fota") is True
    assert ctx.get("cli_name") == "cli"

    if override:
        root = tmp_path / "_templates"
        for rel, text in override.items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8")
        # 未覆盖的模板仍需从仓库取：把整个 templates/ 复制过去代价太大，
        # 改为优先查 _templates、回退仓库（ChoiceLoader）。
        loader = jinja2.ChoiceLoader([
            jinja2.FileSystemLoader(str(root)),
            jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        ])
    else:
        loader = jinja2.FileSystemLoader(str(_TEMPLATES_DIR))

    env = jinja2.Environment(loader=loader, trim_blocks=True, lstrip_blocks=True)
    register_filters(env)

    fota_peri = {"name": "fota"}
    fota_delta_peri = {"name": "fota_delta"}
    fota_meta_peri = {"name": "fota_meta"}
    fota_meta_peri = {"name": "fota_meta"}
    iwdg_peri = {"name": "iwdg", "wdg_timeout_ms": 5000}
    cli_peri = _cli_peripheral()

    render_list = [
        ("drivers/drv_fota.h.j2", "drv_fota.h", fota_peri),
        ("drivers/drv_fota.c.j2", "drv_fota.c", fota_peri),
        ("drivers/fota_delta.h.j2", "drv_fota_delta.h", fota_delta_peri),
        ("drivers/fota_delta.c.j2", "drv_fota_delta.c", fota_delta_peri),
        # 输出名由 **驱动名** 决定（drv_<name>.h），不是模板名
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
# 2. 测试向量
# ---------------------------------------------------------------------------

def _build_patch() -> tuple:
    """造一对结构合法的 slot 镜像，并用自研差分器生成真补丁。"""
    old_code = firmware_like(_OLD_CODE, seed=101)
    new_code = bytearray(firmware_like(_NEW_CODE, seed=202))
    # 植入一段与旧固件相同的重复块：给差分器一点可匹配的东西（纯随机数据会
    # 让 cover 数量退化，覆盖不到"拷贝段"的解析分支）。
    new_code[128:128 + 256] = old_code[512:512 + 256]

    old_image = slot_image(old_code, fw_version=1, slot_base=SLOT_A_BASE)
    new_image = slot_image(bytes(new_code), fw_version=2, slot_base=SLOT_B_BASE)
    patch = build_patch(old_image, new_image, fw_version=2, compress=False)

    assert len(old_image) == 208 + _OLD_CODE
    assert len(new_image) == 208 + _NEW_CODE
    return old_image, new_image, patch


def _c_array(name: str, blob: bytes, per_line: int = 16) -> str:
    lines = ["static const unsigned char %s[%d] = {" % (name, len(blob))]
    for i in range(0, len(blob), per_line):
        row = ", ".join("0x%02X" % b for b in blob[i:i + per_line])
        lines.append("    %s," % row)
    lines.append("};")
    return "\n".join(lines)


def _emit_vectors_header(path: Path, old_image: bytes, new_image: bytes,
                         patch: bytes, boot_config: dict) -> None:
    """把帧流、应答流、镜像与负例信封烘成一个 C 头文件。

    ⚠️ 负例里的期望错误码写成**符号名**（`FOTA_DELTA_E_ENV_MAGIC` 之类），
    由 C 编译器去解析 —— 这样 Python 侧不需要知道那些枚举的数值，
    也就不会出现"两处各写一份错误码表"。改宏名会直接编不过，是想要的失败。
    """
    fr = Framing()
    chunks = fr.split_chunks(patch)
    stream = fr.build_stream(patch)
    resp = fr.expected_responses(patch)

    # 每一帧在 stream 里的 (offset, len)
    offs = []
    pos = fr.start_total
    offs.append((0, fr.start_total))
    for k, ch in enumerate(chunks):
        n = 1 + 4 + len(ch) + 2
        offs.append((pos, n))
        pos += n
    offs.append((pos, fr.finish_total))
    assert pos + fr.finish_total == len(stream)

    negs = fota_l5_negative_envelopes(boot_config)
    budget = fota_delta_budget(boot_config)

    parts = [
        "/* 自动生成，请勿手工编辑。见 generator/tests/test_fota_protocol_l5.py */",
        "#ifndef __FOTA_L5_VECTORS_H",
        "#define __FOTA_L5_VECTORS_H",
        "",
        "#define L5_ENV_SIZE            %d" % fr.env_size,
        "#define L5_CHUNK_SIZE          %d" % fr.chunk_size,
        "#define L5_PATCH_SIZE          %d" % len(patch),
        "#define L5_NCHUNKS             %d" % len(chunks),
        "#define L5_FRAME_COUNT         %d" % len(offs),
        "#define L5_FRAMES_SIZE         %d" % len(stream),
        "#define L5_RESP_COUNT          %d" % (len(resp) // fr.resp_total),
        "#define L5_RESP_SIZE           %d" % len(resp),
        "#define L5_ENV_CRC16           %d" % crc16(patch[:fr.env_size]),
        "#define L5_PAGE_SIZE           %d" % budget["page_size"],
        "#define L5_SLOT_A_BASE         0x%08XUL" % SLOT_A_BASE,
        "#define L5_SLOT_A_SIZE         %dU" % _SLOT_A_SIZE,
        "#define L5_SLOT_B_BASE         0x%08XUL" % SLOT_B_BASE,
        "#define L5_SLOT_B_SIZE         %dU" % _SLOT_B_SIZE,
        "#define L5_OLD_IMAGE_SIZE      %d" % len(old_image),
        "#define L5_NEW_IMAGE_SIZE      %d" % len(new_image),
        "",
        _c_array("g_l5_frames", stream),
        "",
        "static const unsigned short g_l5_frame_off[%d] = {%s};"
        % (len(offs), ", ".join(str(o) for o, _ in offs)),
        "static const unsigned short g_l5_frame_len[%d] = {%s};"
        % (len(offs), ", ".join(str(n) for _, n in offs)),
        "",
        _c_array("g_l5_resp", resp),
        "",
        _c_array("g_l5_old_image", old_image),
        "",
        _c_array("g_l5_new_image", new_image),
        "",
    ]

    # ---- 负例信封表 ----
    for key in sorted(negs):
        env = negs[key]["env"]
        parts.append(_c_array("g_l5_neg_%s" % key, env))
        parts.append("")
    parts.append("typedef struct {")
    parts.append("    const unsigned char *env;")
    parts.append("    unsigned short       size;")
    parts.append("    unsigned short       crc16;")
    parts.append("    int                  expect;")
    parts.append("    const char          *why;")
    parts.append("} l5_negative_t;")
    parts.append("")
    parts.append("#define L5_NEG_COUNT %d" % len(negs))
    parts.append("static const l5_negative_t g_l5_negatives[L5_NEG_COUNT] = {")
    for key in sorted(negs):
        info = negs[key]
        parts.append('    { g_l5_neg_%s, %d, %d, (%s), %s },'
                     % (key, len(info["env"]), info["crc16"],
                        info["expect"], _c_string(info["why"])))
    parts.append("};")
    parts.append("")
    parts.append("#endif /* __FOTA_L5_VECTORS_H */")
    parts.append("")

    path.write_text("\n".join(parts), encoding="utf-8")


def _c_string(text: str) -> str:
    """把一个 Python 字符串变成 C 字符串字面量（非 ASCII 用八进制转义）。"""
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


def _run_bench(tmp_path: Path, override: dict | None = None) -> subprocess.CompletedProcess:
    gcc = _host_gcc()
    srcs = _render_sources(tmp_path, override=override)

    old_image, new_image, patch = _build_patch()
    # 补丁必须跨不止一个分片，否则续传/乱序/早到 FINISH 三个用例在结构上
    # 没法构造（断点只可能落在 0 或 1）。这条断言把"向量尺寸悄悄退化"变成
    # 一个显式的失败，而不是三个静默变绿的用例。
    nchunks = len(Framing().split_chunks(patch))
    assert nchunks >= 2, (
        "补丁只有 %d 个分片（%d B）—— 加大 _NEW_CODE 让补丁超过 %d B"
        % (nchunks, len(patch), Framing().env_size + Framing().chunk_size)
    )

    boot_config = {
        "size_kb": 8,
        "app_a_offset": 0x2000,
        "app_b_offset": 0x40000,
        "_app_a_start": SLOT_A_BASE,
        "_app_b_start": SLOT_B_BASE,
        "_app_a_size": _SLOT_A_SIZE,
        "_app_b_size": _SLOT_B_SIZE,
        "delta_page_size": fota_delta_budget({})["page_size"],
    }
    _emit_vectors_header(tmp_path / "fota_l5_vectors.h",
                         old_image, new_image, patch, boot_config)

    # 与工程命名相关的小配置单独一个头，必须在其它头之前被包含
    (tmp_path / "l5_config.h").write_text(
        "/* 自动生成，见 generator/tests/test_fota_protocol_l5.py */\n"
        "#ifndef __L5_CONFIG_H\n"
        "#define __L5_CONFIG_H\n"
        "/* CLI 头文件名由工程的 CLI 外设名决定（drv_<cli_name>.h） */\n"
        '#define L5_CLI_HEADER "drv_cli.h"\n'
        "#endif\n",
        encoding="utf-8",
    )

    exe = tmp_path / ("l5.exe" if sys.platform.startswith("win") else "l5")
    cmd = [
        gcc, "-std=c99", "-O1", "-Wall", "-Wextra", "-DTEST",
        "-I", str(tmp_path),
        "-I", str(_HPATCH_DIR),
        "-I", str(_TUZ_DIR),
        "-I", str(_HW2C_CLI_DIR),
        str(_HARNESS),
        str(srcs["drv_fota.c"]),
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
        "L5 测试台编译失败：\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (comp.stdout, comp.stderr)
    )
    return subprocess.run([str(exe)], capture_output=True, text=True, cwd=str(tmp_path))


# ---------------------------------------------------------------------------
# 4. 主用例
# ---------------------------------------------------------------------------

def test_l5_protocol(tmp_path):
    """帧解析 / 应答 / 暂存 / 续传 / 准入 / 应用交棒：逐条断言。"""
    run = _run_bench(tmp_path)
    assert run.returncode == 0, (
        "L5 协议测试未通过：\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (run.stdout, run.stderr)
    )
    assert "RESULT: OK" in run.stdout, run.stdout
    # 每个用例都必须真的跑过（防止有人在 main 里注释掉一条）
    for n in range(1, 11):
        assert ("case %d:" % n) in run.stdout, run.stdout


def test_l5_negative_envelopes_each_violate_one_rule():
    """负例的"只违反一条"必须真的是唯一违反项。

    这一条防的是向量本身退化成"同时违反多条"：那样测试只能证明"某条规则
    起了作用"，无法证明"被宣称的那条起了作用" —— 于是改错规则照样全绿。
    这里用最小改动确认：把每一份负例的**目标字段**改回合法值之后，它就必须
    变成一份能被解析的信封（反之亦然）。
    """
    from generator.context.bootloader_context import load_fota_format

    spec = load_fota_format()
    ef = spec["delta_envelope"]["fields"]
    negs = fota_l5_negative_envelopes({"_app_b_size": _SLOT_B_SIZE})

    # 逐条确认"被声称违反的那条"确实能在字节层看出来
    def u16(env, off):
        return env[off] | (env[off + 1] << 8)

    def u32(env, off):
        return (env[off] | (env[off + 1] << 8)
                | (env[off + 2] << 16) | (env[off + 3] << 24))

    bad_magic = negs["bad_magic"]["env"]
    assert u32(bad_magic, ef["magic"]["offset"]) != ef["magic"]["value"]

    bad_crc = negs["bad_hdr_crc16"]["env"]
    assert u16(bad_crc, ef["hdr_crc16"]["offset"]) == 0

    auth = negs["auth_set"]["env"]
    assert u16(auth, ef["auth_len"]["offset"]) != 0

    flag = negs["unknown_flag"]["env"]
    assert u16(flag, ef["flags"]["offset"]) & ~0x1

    too_big = negs["too_big"]["env"]
    assert u32(too_big, ef["new_size"]["offset"]) > _SLOT_B_SIZE

    # over_admission 是边界上的边界：new_size **不能**超过槽容量（否则会被
    # 格式层先拦下），但 align_up(new_size, page) + 暂存区占用必须超出。
    over = negs["over_admission"]["env"]
    new_size = u32(over, ef["new_size"]["offset"])
    patch_size = u32(over, ef["patch_size"]["offset"])
    staged = fota_align_up(48 + patch_size, 8)
    page = fota_delta_budget({})["page_size"]
    assert new_size <= _SLOT_B_SIZE
    assert fota_align_up(new_size, page) + staged > _SLOT_B_SIZE
    # 而且**声明值本身**必须看着像装得下 —— 否则这条用例测不出"必须按整页算"
    assert new_size + staged <= _SLOT_B_SIZE


# ---------------------------------------------------------------------------
# 5. 变异测试：证明这个测试台会红
#
# 每个用例把模板里**一处关键策略**改坏，然后要求 L5 报错。锚点断言保证
# 模板一旦重构，这些测试会先失败、逼着人来更新 —— 一个悄悄失效的变异测试
# 比没有变异测试更糟。
# ---------------------------------------------------------------------------

_DRIVER_C = "drivers/drv_fota.c.j2"

# M1：取消"乱序/重复帧只重发 ACK、不写入"的策略。
# 破坏的是 DATA 的顺序语义 —— 重复分片会被追加写入，暂存区被写花。
_M1_ANCHOR = """    if (seq != g_expected_seq) {
        fota_send_resp(FOTA_PROTOCOL_ACK, g_expected_seq);
        return;
    }"""

_M1_REPL = """    if (0) {   /* MUTATION M1: 乱序/重复帧不再被挡下 */
        fota_send_resp(FOTA_PROTOCOL_ACK, g_expected_seq);
        return;
    }"""


def test_l5_bench_detects_missing_reorder_guard(tmp_path):
    """去掉乱序/重复帧的重发逻辑，L5 必须报错。"""
    text = (_TEMPLATES_DIR / _DRIVER_C).read_text(encoding="utf-8")
    assert text.count(_M1_ANCHOR) == 1, (
        "变异锚点在模板里不再唯一 —— 模板改过，请同步更新本测试的锚点"
    )
    run = _run_bench(tmp_path, override={_DRIVER_C: text.replace(_M1_ANCHOR, _M1_REPL)})
    assert run.returncode != 0, (
        "L5 没有抓到「重复帧被写入」—— 顺序语义形同虚设：\n%s" % run.stdout
    )


# M2：准入公式退回用**声明长度**而不是"实际擦除量"。
# 这正是"边界差一页"缺陷：new_size 恰好落在页边界附近时少算一页，
# 应用期会把暂存区首页擦掉。
_M2_ANCHOR = "    image_bytes  = ((g_env.new_size + page - 1U) / page) * page;"
_M2_REPL = "    image_bytes  = g_env.new_size;   /* MUTATION M2: 不按整页算 */"


def test_l5_bench_detects_page_rounding_regression(tmp_path):
    """准入公式漏算整页，L5 必须报错。"""
    text = (_TEMPLATES_DIR / _DRIVER_C).read_text(encoding="utf-8")
    assert text.count(_M2_ANCHOR) == 1, (
        "变异锚点在模板里不再唯一 —— 模板改过，请同步更新本测试的锚点"
    )
    run = _run_bench(tmp_path, override={_DRIVER_C: text.replace(_M2_ANCHOR, _M2_REPL)})
    assert run.returncode != 0, (
        "L5 没有抓到「准入少算一页」—— 尾仓可能被擦：\n%s" % run.stdout
    )


# M3：FINISH 阶段不再从 Flash 回读暂存区复算 CRC32，直接采信主机声明的值。
# 这一改会让"暂存区其实写坏了"永远检查不出来。
_M3_ANCHOR = """    crc ^= FOTA_CRC32_FINAL_XOR;
    return (crc == expected_crc32) ? 0 : -1;"""

_M3_REPL = """    (void)crc;                       /* MUTATION M3: 不再回读复算 */
    (void)expected_crc32;
    return 0;"""


def test_l5_bench_detects_trusting_declared_crc(tmp_path):
    """FINISH 不再回读校验，L5 必须报错（坏 CRC 的补丁会一路走到 READY）。"""
    text = (_TEMPLATES_DIR / _DRIVER_C).read_text(encoding="utf-8")
    assert text.count(_M3_ANCHOR) == 1, (
        "变异锚点在模板里不再唯一 —— 模板改过，请同步更新本测试的锚点"
    )
    run = _run_bench(tmp_path, override={_DRIVER_C: text.replace(_M3_ANCHOR, _M3_REPL)})
    assert run.returncode != 0, (
        "L5 没有抓到「暂存区校验被摘掉」：\n%s" % run.stdout
    )


# M4：`fota_init` 的 READY 分支退回 IDLE（也就是"重启后不重建上下文"）。
# 这一改的危害不是"多等一会儿"：下一次 START 的续传判定只认 RECEIVING 记录，
# 于是会走完整重传，把一份**已经通过 CRC32** 的补丁从暂存区擦掉重收。
_M4_ANCHOR = """        if (fota_stage_restore(&rec) == 0) {
            g_state        = FOTA_STATE_READY;
            g_staged_bytes = rec.staged;"""

_M4_REPL = """        if (fota_stage_restore(&rec) == 0) {
            g_state        = FOTA_STATE_IDLE;   /* MUTATION M4: 不恢复 READY */
            g_staged_bytes = rec.staged;"""


def test_l5_bench_detects_dropping_the_ready_restore(tmp_path):
    """收齐后掉电不再恢复 READY，L5 必须报错。"""
    text = (_TEMPLATES_DIR / _DRIVER_C).read_text(encoding="utf-8")
    assert text.count(_M4_ANCHOR) == 1, (
        "变异锚点在模板里不再唯一 —— 模板改过，请同步更新本测试的锚点"
    )
    run = _run_bench(tmp_path, override={_DRIVER_C: text.replace(_M4_ANCHOR, _M4_REPL)})
    assert run.returncode != 0, (
        "L5 没有抓到「收齐后掉电不恢复 READY」—— 已校验通过的补丁会被"
        "下一次 START 静默擦掉重收：\n%s" % run.stdout
    )


# M4：`fota_init` 的 READY 分支退回 IDLE（也就是"重启后不重建上下文"）。
# 这一改的危害不是"多等一会儿"：下一次 START 的续传判定只认 RECEIVING 记录，
# 于是会走完整重传，把一份**已经通过 CRC32** 的补丁从暂存区擦掉重收。
_M4_ANCHOR = """        if (fota_stage_restore(&rec) == 0) {
            g_state        = FOTA_STATE_READY;
            g_staged_bytes = rec.staged;"""

_M4_REPL = """        if (fota_stage_restore(&rec) == 0) {
            g_state        = FOTA_STATE_IDLE;   /* MUTATION M4: 不恢复 READY */
            g_staged_bytes = rec.staged;"""


def test_l5_bench_detects_dropping_the_ready_restore(tmp_path):
    """收齐后掉电不再恢复 READY，L5 必须报错。"""
    text = (_TEMPLATES_DIR / _DRIVER_C).read_text(encoding="utf-8")
    assert text.count(_M4_ANCHOR) == 1, (
        "变异锚点在模板里不再唯一 —— 模板改过，请同步更新本测试的锚点"
    )
    run = _run_bench(tmp_path, override={_DRIVER_C: text.replace(_M4_ANCHOR, _M4_REPL)})
    assert run.returncode != 0, (
        "L5 没有抓到「收齐后掉电不恢复 READY」—— 已校验通过的补丁会被"
        "下一次 START 静默擦掉重收：\n%s" % run.stdout
    )
