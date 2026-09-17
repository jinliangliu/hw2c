"""生成期护栏：YMODEM 通道（`drv_fota_ymodem`）——"真源 → 模板"这一段。

这一层不跑 C 代码，只回答一类问题：**模板里有没有偷偷写死一个本应派生的值？**
（本仓库记为"只在特定配置下暴露"的那一类，2026-09-17 一次修了三个。）

具体守五件事：

  1. **每个控制字节、块长、时序都从 `generator/data/ymodem_format.json` 派生。**
     真源里加一个键却没人消费、或者模板里写回一个字面量，都必须是红的。
  2. **对真源派生常量做窄化转换时不得溢出。**
     这条有前科：模板里曾写成 `(uint8_t)YMODEM_BLK_MODULUS`，而它是 256 ——
     `(uint8_t)256U == 0`，于是那一行变成运行时**除零**（真机上是 HardFault）。
     GCC 只在编译期报一个 `-Wdiv-by-zero` 警告，一份把警告当噪音的构建会带着
     它上板。所以这里按"值装不装得下"逐个检查，而不是靠编译器提醒。
  3. **设备侧有自己的 CRC-16/XMODEM 实现，绝不复用 `fota_crc16()`。**
     YMODEM 用初值 0x0000（`'123456789' → 0x31C3`），帧协议用 0xFFFF
     （`→ 0x29B1`）。复用后者会得到一个"自研主机 ↔ 设备完全互通、与所有真实
     YMODEM 软件都不通"的实现 —— 自测永远绿。判据是标准值。
  4. **宏名与真源键必须一一对应。** 真源是唯一真源，模板是它的投影；映射表
     写错一个字（`block_timeout_ms` 映到别的宏），症状是"某个超时静默用了
     默认值"。没做成宏的键必须**显式登记理由**，不能默默漏掉。
  5. **设备侧硬编码了语义的那几项，改动必须在生成期被拒。**
     例如长度字段的进制：设备用 `'0'..'9'` 逐位解析，真源把 radix 改成 16 时
     设备不会跟着改，而症状是"一切正常、只是收不到东西"。

这些检查全部是纯文本层面的，所以跑得很快；行为正确性由
`generator/tests/test_fota_ymodem_l5.py`（渲染 + 编译 + 运行 + 变异）负责。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(_REPO_ROOT))

from generator.context.bootloader_context import (  # noqa: E402
    load_ymodem_format,
    ymodem_for_templates,
)
from generator.fota_ymodem_sender import (  # noqa: E402
    CHECK_INPUT,
    Ymodem,
    _crc_oracle,
)

_TEMPLATES_DIR = _REPO_ROOT / "templates"
_YM_C = _TEMPLATES_DIR / "drivers" / "drv_fota_ymodem.c.j2"
_YM_H = _TEMPLATES_DIR / "drivers" / "drv_fota_ymodem.h.j2"

# 真源里每个数值键 → 它必须被投影成的宏名。
#
# ⚠️ 这张表与 `_NOT_A_MACRO` **合起来必须是穷举**的（`test_every_numeric_key_is_wired`
# 会拿它们与真源对账），所以真源里新增一个数值键却忘了处理，一定会红。
_MACRO_MAP = {
    ("control", "soh"): "YMODEM_CTL_SOH",
    ("control", "stx"): "YMODEM_CTL_STX",
    ("control", "eot"): "YMODEM_CTL_EOT",
    ("control", "ack"): "YMODEM_CTL_ACK",
    ("control", "nak"): "YMODEM_CTL_NAK",
    ("control", "can"): "YMODEM_CTL_CAN",
    ("control", "pad"): "YMODEM_CTL_PAD",
    ("control", "crc_request"): "YMODEM_CTL_CRC_REQUEST",
    ("block", "size_128"): "YMODEM_BLK_SIZE_SMALL",
    ("block", "size_1024"): "YMODEM_BLK_SIZE_LARGE",
    ("block", "overhead"): "YMODEM_BLK_OVERHEAD",
    ("block", "total_1024"): "YMODEM_BLK_MAX_FRAME",
    ("block", "number_modulus"): "YMODEM_BLK_MODULUS",
    ("block", "complement_of"): "YMODEM_BLK_COMPLEMENT",
    ("crc16", "poly"): "YMODEM_CRC_POLY",
    ("crc16", "init"): "YMODEM_CRC_INIT",
    ("header_block", "block_number"): "YMODEM_HDR_BLOCK_NO",
    ("header_block", "max_name_len"): "YMODEM_HDR_MAX_NAME",
    ("header_block", "size_radix"): "YMODEM_HDR_SIZE_RADIX",
    ("terminator_block", "block_number"): "YMODEM_TERM_BLOCK_NO",
    ("handshake", "interval_ms"): "YMODEM_HS_INTERVAL_MS",
    ("handshake", "max_attempts"): "YMODEM_HS_MAX_ATTEMPTS",
    ("handshake", "block_timeout_ms"): "YMODEM_BLOCK_TIMEOUT_MS",
    ("handshake", "nak_max_retries"): "YMODEM_NAK_MAX_RETRIES",
    ("handshake", "can_count_before_abort"): "YMODEM_CAN_BEFORE_ABORT",
    ("handshake", "eot_nak_before_ack"): "YMODEM_EOT_NAK_BEFORE_ACK",
    ("handshake", "terminator_timeout_ms"): "YMODEM_TERMINATOR_TIMEOUT_MS",
}

# 有意**不**做成宏的数值键，每条都要给出理由。
#
# 为什么要这个表而不是"允许漏"：漏掉一个键时，真源看起来是权威的、模板其实
# 还在用老值，而这种分叉不会自己暴露。要求"要么成宏、要么登记"，把这个决定
# 变成一次显式的人工判断 —— 而且它出现在 diff 里。
_NOT_A_MACRO = {
    ("block", "number_field_size"):
        "折进 YMODEM_BLK_OVERHEAD（设备按字节累积，不需要单独的字段宽）",
    ("block", "crc16_size"):
        "折进 YMODEM_BLK_OVERHEAD，同上",
    ("block", "total_128"):
        "设备侧按 OVERHEAD + size_128 现算；133 只在发送端组块时用",
    ("block", "min_payload"):
        "与 size_128 重复",
    ("block", "max_payload"):
        "与 size_1024 重复；设备侧的单块缓冲用 total_1024",
    ("crc16", "check"):
        "标准 check 值是**测试**的判据（见 test_fota_ymodem_l5.py）；"
        "固件里带上它就是一段永不执行的死代码",
    ("crc16", "xorout"):
        "XMODEM 无终值异或；真源记 0 是为了与其他 CRC 族对照",
    ("header_block", "name_terminator"):
        "设备按 NUL 截断文件名；生成期已钉住它必须是 0",
    ("header_block", "payload_offset_after_name"):
        "设备按解析进度推进下标，不用固定偏移",
    ("terminator_block", "payload_value"):
        "设备用『载荷全零』判定结束块，不比较具体值；生成期已钉住它必须是 0",
    ("terminator_block", "size"):
        "结束块的长度由起始字节（SOH/STX）决定，设备侧不读这个值",
    ("handshake", "crc_after_header_ack"):
        "设备侧**无条件**执行（规范强制的固定次序），所以它不是设备侧的宏；"
        "真源这一项的作用是给主机发送器与 L5 计划提供期望值，"
        "生成期已钉住它必须是 1。设备确实补发了那个 'C' 由 "
        "test_the_header_path_sends_the_crc_request_after_the_ack 与工程内单测钉住",
}


# ---------------------------------------------------------------------------
# 1. 渲染
# ---------------------------------------------------------------------------

def _peripheral() -> dict:
    return {"name": "fota_ymodem", "uart_name": "usart2"}


def _render(tmpl_rel: str) -> str:
    import jinja2

    from generator.context.builder import build_context
    from generator.jinja_filters import register_filters

    hardware = {
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
            {"name": "cli", "type": "Internal_CLI", "uart": "usart2",
             "extra": {"prompt": "yms> "}},
        ],
        "bootloader": {"enabled": True},
    }
    ctx = build_context(hardware, "ymodem-guard")
    assert ctx.get("has_fota_receive") is True

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        trim_blocks=True, lstrip_blocks=True)
    register_filters(env)

    local = dict(ctx)
    local["peripheral"] = _peripheral()
    local["model"] = {"type": "Internal_FOTA"}
    return env.get_template(tmpl_rel).render(**local)


@pytest.fixture(scope="module")
def rendered() -> dict:
    return {
        "c": _render("drivers/drv_fota_ymodem.c.j2"),
        "h": _render("drivers/drv_fota_ymodem.h.j2"),
        "c_src": _YM_C.read_text(encoding="utf-8"),
        "h_src": _YM_H.read_text(encoding="utf-8"),
    }


def _macro_values(header_text: str) -> dict:
    """把渲染出来的 `#define NAME <整数>U [/* 注释 */]` 解析成 {NAME: int}。

    解析不出来的（表达式宏、宏引用）直接跳过 —— 它们不是"被写死的值"，
    正是我们想要的形态。
    """
    out = {}
    for m in re.finditer(
            r"^#define\s+(\w+)\s+(-?(?:0[xX][0-9A-Fa-f]+|\d+))U?\s*"
            r"(?:/\*.*?\*/)?\s*$",
            header_text, re.MULTILINE):
        out[m.group(1)] = int(m.group(2), 0)
    return out


def _strip_c_comments(text: str) -> str:
    """剥掉 C 注释。

    ⚠️ 这一步不是洁癖，是**必需**的：本仓库的模板注释里大量写着"不要这样写"
    的反面示例（`千万不要写成 (uint8_t)YMODEM_BLK_MODULUS`、`不要复用
    fota_crc16()`），这些文字会原样渲染进 C 输出。不剥注释的话，检查会因为
    **文档里那句警告**而误报 —— 而在毫不知情的情况下，最"省事"的修法是把
    警告删掉，于是检查变成永远绿、注释也没了。

    同一课在 `generator/tests/test_template_render.py` 里也上过一遍（注释里
    含函数名会让 `find()` 误命中）。
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


# ---------------------------------------------------------------------------
# 2. 真源 → 模板：一个键都不许漏
# ---------------------------------------------------------------------------

def _numeric_keys(spec: dict) -> set:
    """真源里所有数值型条目 → {(段, 键)}。

    两种形态都算数值：`{"value": N}`（`control` 段）与裸整数（其余段）。
    **bool 不算** —— `refin`/`refout` 是 Python 的 bool，而 `isinstance(True, int)`
    为真，不排掉就会把它们当成"还没处理的数值键"。
    """
    keys = set()
    for section, body in spec.items():
        if not isinstance(body, dict):
            continue
        for key, val in body.items():
            if key.startswith("_"):
                continue
            if isinstance(val, bool):
                continue
            if isinstance(val, int):
                keys.add((section, key))
            elif isinstance(val, dict) and isinstance(val.get("value"), int) \
                    and not isinstance(val.get("value"), bool):
                keys.add((section, key))
    return keys


def test_every_numeric_key_is_wired_or_explicitly_excluded():
    """真源里每个数值键都必须"被投影成宏"或"显式登记为不需要宏"。

    防的是"加了配置项却没人消费"：真源看起来是权威的，模板其实还在用老值。
    `handshake` 段里的 `block_timeout_ms` 与 `terminator_timeout_ms` 名字像、
    位置近 —— 漏掉或映射岔一个是很容易发生的。
    """
    spec = load_ymodem_format()
    in_spec = _numeric_keys(spec)
    handled = set(_MACRO_MAP) | set(_NOT_A_MACRO)

    missing = in_spec - handled
    assert not missing, (
        "这些真源键既没有被投影成宏、也没有登记在 _NOT_A_MACRO 里 —— "
        "新增配置项必须显式决定它怎么进设备（否则它就是一个『看起来能配、"
        "其实不生效』的字段）：%s" % sorted(missing)
    )
    stale = handled - in_spec
    assert not stale, (
        "_MACRO_MAP / _NOT_A_MACRO 里有真源已经不存在的键（表过期了）：%s"
        % sorted(stale)
    )
    # 交叉：同一个键不许同时出现在两张表里（否则"成不成宏"有两个答案）
    both = set(_MACRO_MAP) & set(_NOT_A_MACRO)
    assert not both, "同一个键同时登记成了『成宏』与『不成宏』：%s" % sorted(both)

    assert len(in_spec) >= 35, "真源里的数值键只有 %d 个，是不是读错文件了？" % len(in_spec)


def test_rendered_macros_match_the_source_of_truth(rendered):
    """每个宏的渲染值必须**等于**真源里的值（逐个对账，不是抽查）。"""
    spec = load_ymodem_format()
    macros = _macro_values(rendered["h"])

    for (section, key), macro in sorted(_MACRO_MAP.items()):
        want = int(spec[section][key]["value"]) if isinstance(
            spec[section][key], dict) else int(spec[section][key])
        assert macro in macros, (
            "头文件里没有解析到宏 %s（真源 %s.%s）—— 它是被删了，还是渲染成了"
            "一个我们解析不出的表达式？" % (macro, section, key)
        )
        assert macros[macro] == want, (
            "宏 %s 渲染成了 %d，真源 %s.%s 是 %d —— 模板与真源对不上了"
            % (macro, macros[macro], section, key, want)
        )

    assert len(_MACRO_MAP) >= 25, "映射表只剩 %d 条了？" % len(_MACRO_MAP)


def test_template_macros_are_all_derived(rendered):
    """模板里每个 `#define YMODEM_*` 都必须来自真源表达式或其它宏。

    检查的是**模板源码**（渲染之后 `{{ }}` 已经没了，那时候再查就查不到
    "值是怎么来的"）。写死一个数值（哪怕值碰巧是对的）就把真源的意义抹掉了：
    真源改了、设备不改，而两侧"各自都自洽"。
    """
    text = re.sub(r"\\\n\s*", " ", rendered["h_src"])      # 合并续行
    seen = 0
    for line in text.splitlines():
        m = re.match(r"^#define\s+(YMODEM_\w+)\s+(.*)$", line)
        if not m:
            continue
        name, body = m.group(1), m.group(2).strip()
        seen += 1
        assert "{{" in body or re.search(r"\b(?:FOTA|YMODEM)_\w+", body), (
            "模板里的宏 %s 既不是真源表达式、也不引用其它宏，看起来是写死的：%s"
            % (name, body)
        )
    assert seen >= 25, "模板里只看到 %d 条 YMODEM_* 宏，是不是漏投影了？" % seen


def test_no_control_byte_literal_in_the_driver_template(rendered):
    """`.c` 模板里不许出现"拿字面量当协议控制字节"的比较。

    形如 `== 0x43` / `== 67` 的比较一旦出现，就说明有一处在按记忆里的数值写
    协议，而不是走宏。这种地方改真源时不会一起改。
    """
    hex_bytes = "|".join("0[xX]%02X" % b for b in (1, 2, 4, 6, 0x15, 0x18, 0x1A, 0x43))
    dec_bytes = "|".join(str(b) for b in (4, 6, 21, 24, 26, 67))
    pattern = re.compile(
        r"(?:==|!=)\s*(?:%s|%s)U?\b" % (hex_bytes, dec_bytes))

    src = _strip_c_comments(rendered["c_src"])
    hits = [m.group(0).strip() for m in pattern.finditer(src)]
    assert not hits, (
        "模板里出现了用字面量比协议控制字节的地方：%s\n"
        "控制字节只能通过 YMODEM_CTL_* 宏使用（真源是唯一真源）" % hits
    )

    # 变异验证：把当年那种写法塞进去，检查必须报出来
    mutated = src.replace("if (b == (uint8_t)YMODEM_CTL_EOT) {",
                          "if (b == 0x04) {", 1)
    assert mutated != src, "变异锚点没找到 —— 模板改过，请更新本测试"
    assert [m.group(0).strip() for m in pattern.finditer(mutated)], (
        "字面量检查抓不到 `b == 0x04`（这条检查本身失效了）"
    )


# ---------------------------------------------------------------------------
# 3. 窄化转换不得溢出（这条有前科，见模块 docstring）
# ---------------------------------------------------------------------------

def _narrowing_violations(header_text: str, c_text: str) -> list:
    """找出"把宏窄化到装不下它的类型"的地方。返回 [(宏, 值, 目标类型)]。

    纯函数 —— 于是可以直接喂一段**改坏的源码**给它，证明它真的会报
    （不依赖编译，所以这条护栏本身永远跑得动）。注释先剥掉：模板注释里就写着
    那句"千万不要写成 (uint8_t)YMODEM_BLK_MODULUS"的警告。
    """
    macros = _macro_values(header_text)
    limits = {"uint8_t": 0xFF, "uint16_t": 0xFFFF, "int8_t": 0x7F, "int16_t": 0x7FFF}
    code = _strip_c_comments(c_text)

    bad = []
    for cast, limit in sorted(limits.items()):
        for m in re.finditer(r"\(%s\)\s*(\w+)" % cast, code):
            name = m.group(1)
            if name not in macros:
                continue
            if macros[name] > limit:
                bad.append((name, macros[name], cast))
    return bad


def test_no_narrowing_cast_overflows(rendered):
    """对真源派生常量做窄化转换时，值必须装得下。

    ⚠️ 这一条抓到过一个真缺陷：模板里写过
        g_expect_blk = (uint8_t)((g_expect_blk + 1U) % (uint8_t)YMODEM_BLK_MODULUS);
    而 `YMODEM_BLK_MODULUS` = 256 ⇒ `(uint8_t)256U == 0` ⇒ **运行时除零**
    （真机上是 HardFault）。编译器只给一个 `-Wdiv-by-zero` 警告，很容易被
    淹没在构建输出里。这条检查不看警告、只看"值装不装得下"。
    """
    bad = _narrowing_violations(rendered["h"], rendered["c"])
    assert not bad, (
        "这些宏被窄化到了装不下它的类型（会静默截断，最坏是除零）：\n%s\n"
        "先在宽类型里把运算做完，最后才截断"
        % "\n".join("  (%s)%s = %d 装不下" % (c, n, v) for n, v, c in bad)
    )


def test_narrowing_check_actually_bites(rendered):
    """变异验证：把当年那行缺陷重新写回去，检查必须报出来。

    没有这一条，上面那个检查有可能永远返回空列表而"看起来很绿"。
    """
    buggy = ("    g_expect_blk = (uint8_t)((g_expect_blk + 1U) "
             "% (uint8_t)YMODEM_BLK_MODULUS);\n")
    assert "% (uint8_t)YMODEM_BLK_MODULUS" not in rendered["c"], (
        "渲染出来的源码里还有那个除零写法"
    )
    bad = _narrowing_violations(rendered["h"], buggy)
    assert bad == [("YMODEM_BLK_MODULUS", 256, "uint8_t")], bad

    # 装得下的不许误报：真实渲染出的源码必须是干净的
    assert _narrowing_violations(rendered["h"], rendered["c"]) == []

    # 另一个方向：`(uint16_t)YMODEM_BLK_MAX_FRAME`(1029) 是**合法**的，
    # 不许报 —— 否则这条检查会因为"宁可错杀"而被后人关掉。
    ok = "    uint16_t n = (uint16_t)YMODEM_BLK_MAX_FRAME;\n"
    assert _narrowing_violations(rendered["h"], ok) == []


# ---------------------------------------------------------------------------
# 4. CRC：设备侧不许复用帧协议那份
# ---------------------------------------------------------------------------

ACK_CALL = "ym_ack();"
CRC_CALL = "ym_send_ctl((uint8_t)YMODEM_CTL_CRC_REQUEST);"
TICK_CALL = "g_tick = HAL_GetTick();"


def _body_of(code: str, signature: str) -> str:
    """取出一个函数的完整函数体（按大括号配平）。code 必须已剥掉注释。"""
    i = code.index(signature)
    j = code.index("{", i)
    depth = 0
    for k in range(j, len(code)):
        if code[k] == "{":
            depth += 1
        elif code[k] == "}":
            depth -= 1
            if depth == 0:
                return code[i:k + 1]
    raise AssertionError("大括号不配平：%s" % signature)


def _header_path_step_order(code: str) -> list:
    """块 0 处理函数里三个关键动作的出现次序（缺项用 -1 表示）。

    纯函数，于是可以喂一段**改坏的源码**证明它真的会报 —— 不依赖编译。
    """
    body = _body_of(code, "static void ym_handle_header(")
    return [body.find(ACK_CALL), body.find(TICK_CALL), body.find(CRC_CALL)]


def test_the_header_path_sends_the_crc_request_after_the_ack(rendered):
    """块 0 被 ACK 之后，设备必须**再发一个 'C'**（规范里接收方的固定次序）。

    ⚠️ 这条抓到的缺陷比 CRC 那条更隐蔽：两侧都是自研实现时它**完全自洽** ——
    设备 ACK 完就等数据，自研主机 ACK 完就发数据，渲染层 / L5 台架 / 工程内
    单测**全绿**，那是两个错凑成一个对。只有接上真正的第三方发送端才暴露：
    发送端在块 0 被 ACK 后等的就是这个 'C'（python-ymodem 包等 60 s、
    Tera Term / lrzsz sb 同理），现场表现是"文件发不出去、设备一声不吭"。
    真机实测记录：`docs/reviews/onboard-capture-2026-09-17.txt`。

    三个断言的次序是设计的一部分：先 ACK（告诉发送端"块 0 收下了"），再重启
    数据块超时（邀请开启一个新窗口），最后才是那个 'C'。次序反了的话，发送端
    在等 ACK 时先读到 'C'，会当成"块 0 没被收下"而重发。
    """
    order = _header_path_step_order(_strip_c_comments(rendered["c_src"]))
    ack, tick, crc = order
    assert ack >= 0, "ym_handle_header 里找不到 ym_ack() —— 函数被改写了吗？"
    assert tick >= 0, "块 0 之后没有重启数据块超时（g_tick 未刷新）"
    assert crc >= 0, (
        "块 0 被 ACK 之后**没有**再发 'C'。规范里接收方的次序是"
        "『发 'C' → 收块 0 → ACK → 再发 'C' → 收数据块』，漏了这一句会让所有"
        "外部 YMODEM 发送端（python-ymodem / Tera Term / lrzsz sb）在块 0 之后"
        "等到自己的超时才放弃"
    )
    assert ack < tick < crc, (
        "块 0 的应答次序不对：应当先 ACK、再重启超时、最后发 'C'，实际下标 %r"
        % (order,)
    )

    # 变异验证：把那个 'C' 删掉（= 修复前的状态），检查必须报出来。
    mutated = _strip_c_comments(rendered["c_src"]).replace(CRC_CALL, "", 1)
    assert mutated != _strip_c_comments(rendered["c_src"]), "变异锚点没找到"
    assert _header_path_step_order(mutated)[2] == -1, (
        "去掉那句 'C' 之后本检查居然还是通过的（这条检查本身失效了）"
    )

def test_device_implements_its_own_xmodem_crc(rendered):
    """两块 CRC 必须是**两个**实现，且设备侧那个用初值 0x0000。

    复用的后果：自研主机与设备完全互通（两侧错得一样）、所有自测全绿，但与
    Tera Term / lrzsz sb / ExtraPuTTY 一个都连不上。所以设备侧必须有自己
    的一份，且不许出现 `fota_crc16`。
    """
    src = rendered["c_src"]
    code = _strip_c_comments(src)

    assert "static uint16_t ym_crc16(" in code, "设备侧必须有自己的一份 CRC16"
    assert "fota_crc16" not in code, (
        "YMODEM 侧复用了帧协议的 fota_crc16()（CRC-16/CCITT-FALSE，初值 0xFFFF）"
        "—— 那会让设备与所有真实 YMODEM 软件都不通，而自测全绿"
    )
    # ⚠️ 注释里**允许**提到 `fota_crc16` —— 而且应该提：那条注释解释了为什么
    # 不能复用。所以检查剥掉注释再做，否则最省事的"修法"会变成删掉那句解释。
    assert "fota_crc16" in src, (
        "模板注释里那句『为什么不能复用 fota_crc16』的解释不见了 —— "
        "下一代维护者会重新踩同一个坑"
    )
    # 初值与多项式必须来自真源，不许写死
    assert "YMODEM_CRC_INIT" in code
    assert "YMODEM_CRC_POLY" in code


def test_crc_check_value_is_the_standard_one(rendered):
    """判据是**标准 check 值**，不是"另一侧的实现也这么算"。

        CRC-16/XMODEM       '123456789' → 0x31C3   ← YMODEM
        CRC-16/CCITT-FALSE  '123456789' → 0x29B1   ← 本仓库帧协议
    """
    spec = load_ymodem_format()
    assert int(spec["crc16"]["init"]) == 0x0000
    assert int(spec["crc16"]["poly"]) == 0x1021
    assert int(spec["crc16"]["check"]) == 0x31C3

    ym = Ymodem()
    assert ym.crc16(CHECK_INPUT) == 0x31C3
    assert _crc_oracle(CHECK_INPUT) == 0x31C3, "stdlib 的独立算路不认同"

    # 渲染出的初值也必须是 0（真源→模板这一段不能岔开）
    assert _macro_values(rendered["h"])["YMODEM_CRC_INIT"] == 0x0000
    assert _macro_values(rendered["h"])["YMODEM_CRC_POLY"] == 0x1021


# ---------------------------------------------------------------------------
# 5. 派生上下文：模板拿到的是"扁平化后的真源"
# ---------------------------------------------------------------------------

def test_ymodem_context_is_flat_and_complete():
    """`ymodem_for_templates()` 必须把真源扁平成一维，且自带算术自检。"""
    flat = ymodem_for_templates()
    spec = load_ymodem_format()

    assert flat["ctl_crc_request"] == spec["control"]["crc_request"]["value"]
    assert flat["blk_size_128"] == spec["block"]["size_128"]
    assert flat["blk_size_1024"] == spec["block"]["size_1024"]
    assert flat["crc_init"] == spec["crc16"]["init"]
    assert flat["hdr_size_radix"] == spec["header_block"]["size_radix"]
    # 总长必须是 载荷 + 开销：写成别的值会让单块缓冲装不下最长的一帧
    assert flat["blk_max_frame"] == flat["blk_size_1024"] + flat["blk_overhead"]
    # 注：『模板用到的每个键都在上下文里』由 test_rendered_macros_match_the_source_of_truth
    # 间接保证 —— 缺一个键，Jinja 渲染会直接以 UndefinedError 失败。


@pytest.mark.parametrize("bad_patch,needle", [
    # 总长少 1 字节：单块缓冲装不下最长的块 ⇒ 越界写，只在 1K 块路径上踩内存
    (("block", "total_1024", 1028), "total_1024"),
    # 进制不是 10：设备按 '0'..'9' 解析，改真源不会让设备跟着改
    (("header_block", "size_radix", 16), "size_radix"),
    # 结束块载荷值非 0：设备用『全零』判定，读真源也不会变
    (("terminator_block", "payload_value", 7), "payload_value"),
])
def test_flat_context_rejects_a_self_contradictory_spec(monkeypatch, bad_patch,
                                                        needle):
    """真源自相矛盾 / 与设备实现不一致时，取上下文必须直接失败。

    这类矛盾的症状都很隐蔽：缓冲少几个字节是越界写、进制写错是"收不到东西
    但一切正常"。宁可拒绝生成。
    """
    from generator.context import bootloader_context as bc

    spec = load_ymodem_format()
    bad = {k: (dict(v) if isinstance(v, dict) else v) for k, v in spec.items()}
    section, key, value = bad_patch
    bad[section] = dict(spec[section])
    bad[section][key] = value

    monkeypatch.setattr(bc, "load_ymodem_format", lambda: bad)
    with pytest.raises(ValueError) as ei:
        bc.ymodem_for_templates()
    assert needle in str(ei.value), (
        "错误信息应当指出是 %s 有问题，实际是：%s" % (needle, ei.value)
    )
