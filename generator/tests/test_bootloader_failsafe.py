"""引导器失效保护：两槽镜像都坏时必须停下并给外部指示，不得无限乒乓（FR-14.8）。

真实缺陷（2026-09-22，槽 B 注入 1 字节翻转后实测）
--------------------------------------------------
`boot_main.c.j2` 阶段 6 原本这样判断"两个槽都试过了"：

    boot_nvm_swap_active_slot();
    if ((slot == BOOT_SLOT_A && boot_nvm_get_active_slot() == BOOT_SLOT_A) ||
        (slot == BOOT_SLOT_B && boot_nvm_get_active_slot() == BOOT_SLOT_B)) {
        led_error_blink();      /* SOS */
    }
    soft_reset();

而 `boot_nvm_swap_active_slot()` 的定义就是**取反**：

    TAMP->BKP1R = (current == BOOT_SLOT_A) ? BOOT_SLOT_B : BOOT_SLOT_A;

于是 swap 之后 `get_active_slot()` 必定不等于先前的 `slot`，条件**恒为假**，
`led_error_blink()` 是一段永不执行的死代码。两槽都坏时的真实行为是 A↔B
无限软复位乒乓 —— 灯一直在闪，设备永远起不来，而现场没有任何办法区分
"还在重试"和"彻底没救"。

单槽回滚（上一轮实测通过）走的是同一段代码，所以"能回滚"完全掩盖了
"两槽都坏时停不下来"。这类缺陷只有把两槽同时弄坏才暴露。

修法：CRC 失败时**直接校验另一个槽**，另一槽可用才切槽软复位，都坏则进 SOS。
`boot_crc_verify()` 是纯函数、无副作用、跨复位不需要任何持久状态 —— 因此
不需要占用 STM32G0B1 最后一个空闲的备份寄存器（BKP4R），也不必回答
"谁在什么时候清除这个标志"。

为什么是渲染级断言
------------------
`boot_main.c` 无条件 `#include "stm32g0xx.h"` 且 `main()` 会真跳转，
主机上编译运行它不现实（给它加 `#ifdef TEST` 分支要顺带 mock 掉
`boot_jump_to_app` 的跳转副作用，代价大于收益）。所以这里钉住的是
**渲染产物的结构性质**，由 `docs/reviews/` 之外的真机实验补行为验证。

⚠️ 这里的断言容易写成恒真（比如只检查"文件里有没有 led_error_blink"）。
变异脚本 `.workbuddy/tmp/mutate_boot_failsafe_guard.py` 用 5 个变异体验证
本文件确实会失败，其中两条是**反向**变异（无条件 SOS、验错槽位）——
只检查"有没有 SOS"的断言抓不到它们。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_TEMPLATES = _REPO_ROOT / "templates"
_BOOT_MAIN = "bootloader/boot_main.c.j2"

# 分析区间的起点：`uint32_t app_addr;`（阶段 5 选槽位地址）到引导结束。
# 之所以不从"阶段 6"开始：另一槽地址的选择必须能与当前槽对照着看，
# 只盯阶段 6 无法断言"另一槽 ≠ 当前槽"。改名会让所有断言 fail —— 那是信号。
_STAGE_ANCHOR = "uint32_t app_addr;"


# ---------------------------------------------------------------------------
# 渲染与预处理
# ---------------------------------------------------------------------------

def _strip_c_comments(text: str) -> str:
    """剥掉 C 注释。

    必要性：注释里会解释"旧写法错在哪"，里面满是被禁的写法（`swap_active_slot`
    之后比较槽位）。不剥注释，断言要么误报、要么（更糟）因为总能匹配到而恒真。
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def _render_boot_main() -> str:
    import jinja2

    from generator.context.builder import build_context
    from generator.jinja_filters import register_filters

    hardware = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [
            {"id": "PC0", "function": "GPIO_Output", "label": "LED",
             "active_level": "low"},
        ],
        "peripherals": [
            {"name": "usart2", "type": "UART_Serial", "instance": "USART2",
             "interface": "uart", "extra": {"baudrate": 115200}},
        ],
        "bootloader": {"enabled": True},
    }
    ctx = build_context(hardware, "boot-failsafe-guard")

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
        trim_blocks=True, lstrip_blocks=True)
    register_filters(env)
    return env.get_template(_BOOT_MAIN).render(**ctx)


def _stage6() -> str:
    """渲染结果里"槽位地址选择 → 校验 → 启动或恢复"这一段（已剥注释）。"""
    code = _strip_c_comments(_render_boot_main())
    i = code.find(_STAGE_ANCHOR)
    assert i >= 0, (
        f"boot_main.c.j2 里找不到「{_STAGE_ANCHOR}」—— 引导尾部改了结构，"
        "本文件的断言需要跟着更新，而不是删掉")
    return code[i:]


def _assignment(code: str, name: str) -> str:
    """取出 `name = ...;` 这一句的表达式文本。"""
    m = re.search(r"\b%s\s*=\s*([^;]+);" % re.escape(name), code)
    assert m, f"阶段 6 里找不到 `{name}` 的赋值"
    return m.group(1)


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------

def test_the_other_slot_is_really_crc_checked():
    """活动槽 CRC 失败后，必须真的去校验**另一个**槽，而不是只切过去碰运气。"""
    s6 = _stage6()

    # 两次校验：一次是当前槽（阶段 5），一次是另一槽（阶段 6）
    assert s6.count("boot_crc_verify(") >= 2, (
        "阶段 6 只有一次 boot_crc_verify —— 另一槽没被校验，"
        "「两槽都坏」就成了无法判定的事，只能靠切过去再试一次")

    # "另一个槽"必须与当前槽相反：A 的三元分支要给 B 的地址
    other = _assignment(s6, "other_addr")
    assert "APP_B_START" in other, (
        f"`other_addr` 里没有 APP_B_START（实际: {other.strip()}）—— "
        "若两边都指向同一个槽，『两槽都坏』会退化成『这一槽坏了就 SOS』")
    assert "BOOT_SLOT_A" in other, (
        f"`other_addr` 没有按 slot 选择（实际: {other.strip()}）—— "
        "写死一个槽会让另一个槽永远得不到校验")

    # 当前槽的地址选择必须与之相反，否则两条路径重合
    app = _assignment(s6, "app_addr")
    assert "APP_A_START" in app, (
        f"`app_addr` 里没有 APP_A_START（实际: {app.strip()}）")


def test_sos_is_guarded_by_the_other_slots_crc_result():
    """SOS 的判据必须是 CRC 结果，而不是"切槽后活动槽是否变回原值"（恒假）。"""
    s6 = _stage6()

    pos = s6.find("led_error_blink(")
    assert pos >= 0, "阶段 6 里没有 led_error_blink() —— SOS 信号消失了"

    cond = s6.rfind("if (", 0, pos)
    assert cond >= 0, (
        "led_error_blink() 前面没有任何 if —— 变成无条件 SOS，"
        "单槽损坏时也会停机，回滚能力就没了")
    assert "boot_crc_verify" in s6[cond:pos], (
        "SOS 的判据不是 CRC 结果（实际条件: "
        + " ".join(s6[cond:pos].split())[:120] + "）")

    # 旧缺陷的特征：拿 swap 之后的活动槽与先前的 slot 比。swap 恒取反 ⇒ 恒假。
    assert "boot_nvm_get_active_slot()" not in s6, (
        "阶段 6 又在拿 swap 之后的活动槽做判断 —— swap 的定义就是取反，"
        "这种自比较恒为假，SOS 永远进不去")


def test_sos_stops_instead_of_swapping_first():
    """两槽都坏时必须**停在** SOS：不许先切槽、也不许再软复位。"""
    s6 = _stage6()

    sos = s6.find("led_error_blink(")
    assert sos >= 0
    swap = s6.find("boot_nvm_swap_active_slot(")
    assert swap >= 0, "阶段 6 里没有切槽 —— 另一槽可用时也回不去了"

    assert sos < swap, (
        "切槽发生在 SOS 之前 —— 两槽都坏时会先把活动槽翻到另一个坏槽再停机，"
        "现场取证看到的『最后尝试的槽』就是错的，且与原缺陷的乒乓只差一次复位")

    # SOS 必须是终点：它所在分支之内不许再安排复位。
    # 注意 soft_reset() 出现在分支**之外**是合法的（那条是"另一槽可用"的路径）。
    close = s6.find("}", sos)
    assert close > sos, "led_error_blink() 所在分支没有闭合 —— 模板结构异常"
    assert "soft_reset()" not in s6[sos:close], (
        "SOS 分支内部还有 soft_reset() —— 说明作者预期 SOS 会返回，"
        "那它就不是『停止』，而是又一次乒乓的起点")


def test_a_single_bad_slot_still_swaps_and_retries():
    """反向护栏：不能为了让 SOS 可达而把回滚本身弄丢。"""
    s6 = _stage6()

    assert "boot_nvm_swap_active_slot()" in s6, "另一槽可用时不切槽 ⇒ 单槽损坏无法回滚"
    assert "soft_reset()" in s6, "切槽后不软复位 ⇒ 引导器不会重入阶段 5 校验新槽"
    assert "boot_jump_to_app(" in s6, "CRC 通过时不跳转 App ⇒ 正常路径断了"
