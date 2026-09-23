"""异常诊断链路的渲染级护栏（FR-15.7）。

真实缺陷背景
------------
"设备重启了"这件事原本只有一个错误码可看（`hw2c_fault_last()`）。现场拿到
一个数字，分不清是初始化失败还是跑飞、更不知道在哪条指令上。FR-15.7 要求
异常时打出**当时在跑的模块/任务**和**故障点地址**，并且这个记录要活过复位
（`.noinit`），下次开机由 `main()` 报出来。

这条链路横跨 8 个模板，任何一环掉链子都不会报错，只会"打印出来的东西少了
几个字段"—— 典型的静默退化：

  · `hw2c_fault.c` 自己留一份记录、`hw2c_diag.c` 再留一份 ⇒ 两个 magic、
    两个真源，读数互相矛盾（这是把记录收口到 diag 的原因）；
  · HardFault handler 不是 naked ⇒ 编译器先动了栈指针，"栈帧在哪"就说不清，
    取到的 PC 指错地方却看不出错；
  · 照抄 M3/M4 的 HardFault 打印代码 ⇒ 引用 CFSR/HFSR/BFAR/MMFAR，在 M0+
    上**根本编译不过**（core_cm0plus.h 的 SCB 只到 SHCSR）；
  · 只编 `hw2c_fault.c` 不编 `hw2c_diag.c` ⇒ 链接期缺符号，而且只在"某个
    示例恰好没开某个功能"时冒出来；
  · 删掉 `.noinit` 段 ⇒ 记录变成普通 .bss，复位即清零，"上次为什么复位"
    永远查不到。

为什么是渲染级断言
------------------
地址取值（naked 汇编 + `__builtin_return_address`）和实际复位行为在主机上跑
不了：`__builtin_return_address` 在 x86 上返回的是主机地址，`.noinit` 在主机
进程里也不会跨"复位"存活。这里钉住的是**渲染产物的结构性质**；行为由
`templates/test/test_hw2c_diag.c.j2`（主机）与真机实验（烧进去看串口）补。

⚠️ 断言容易写成恒真（"文件里有没有 hw2c_diag"）。本文件的变异脚本
`.workbuddy/tmp/mutate_diag_guardrails.py` 用 16 个变异体验证每条断言确实会
失败，其中含反向变异（把 PC 偏移改错、把 log_ready 判据颠倒）。
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

_FAULT = "src/hw2c_fault.c.j2"
_DIAG = "src/hw2c_diag.c.j2"
_DIAG_H = "src/hw2c_diag.h.j2"
_IT = "src/stm32g0xx_it.c.j2"
_FREERTOS_CFG = "config/FreeRTOSConfig.h.j2"
_CLI = "drivers/drv_cli.c.j2"
_FOTA = "drivers/drv_fota.c.j2"
_EVENT = "src/event_mgr.c.j2"
_MAIN = "src/main.c.j2"
_LOG_H = "drivers/drv_log.h.j2"
_LOG_C = "drivers/drv_log.c.j2"


# ---------------------------------------------------------------------------
# 渲染与预处理
# ---------------------------------------------------------------------------

def _strip_c_comments(text: str) -> str:
    """剥掉 C 注释：注释里明明白白写着被禁的旧写法（CFSR/HFSR/…），不剥会
    让"不许引用"类断言永远失败、也会让"必须有"类断言恒真。"""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def _make_env():
    import jinja2

    from generator.jinja_filters import register_filters

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
        trim_blocks=True, lstrip_blocks=True)
    register_filters(env)
    return env


def _context():
    from generator.context.builder import build_context

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
             "extra": {"prompt": "cli> "}},
            {"name": "fota", "type": "Internal_FOTA", "uart": "usart2"},
        ],
        "bootloader": {"enabled": True},
    }
    ctx = build_context(hardware, "diag-guardrails")
    ctx["peripheral"] = {"name": "cli", "uart_name": "usart2"}
    ctx["model"] = {"type": "Internal_CLI"}
    ctx["has_log"] = True
    ctx["project_name"] = "guard"
    ctx["project_version"] = "1.0.0"
    ctx["project_version_packed"] = 0x010000
    return ctx


def _render(name: str) -> str:
    """渲染并剥注释后的源码。"""
    return _strip_c_comments(_make_env().get_template(name).render(**_context()))


def _raw(name: str) -> str:
    """模板原文（用于 CMake / Python 这类非 C 文件）。"""
    return (_TEMPLATES / name).read_text(encoding="utf-8")


def _func_body(code: str, signature: str) -> str:
    """取一个函数体的源码（大括号配平，不看缩进）。

    不能"截到下一个空行"：函数体里到处是空行，那样取到的只有第一行声明，
    断言就变成了"这一行里有没有它" —— 一堆恒真。
    """
    i = code.find(signature)
    assert i >= 0, f"渲染产物里找不到 {signature!r} —— 结构变了，护栏要跟着改"
    j = code.find("{", i)
    assert j >= 0, f"{signature!r} 后面没有 '{{'"
    depth = 0
    for k in range(j, len(code)):
        if code[k] == "{":
            depth += 1
        elif code[k] == "}":
            depth -= 1
            if depth == 0:
                return code[j:k]
    raise AssertionError(f"{signature!r} 的大括号没有配平")


def _macro_body(code: str, signature: str) -> str:
    """取一个多行宏的体（续行符 '\\' 结束为止）。"""
    i = code.find(signature)
    assert i >= 0, f"渲染产物里找不到宏 {signature!r}"
    lines = code[i:].splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        out.append(line)
        if not line.rstrip().endswith("\\"):
            break
    return "\n".join(out)


def _window(code: str, anchor: str, size: int = 900) -> str:
    """从 anchor 起取一段源码（用于"一个块"而不是"一个函数"的场景）。"""
    i = code.find(anchor)
    assert i >= 0, f"渲染产物里找不到 {anchor!r}"
    return code[i:i + size]


# ---------------------------------------------------------------------------
# 1. hw2c_fault：记录委托给 diag，且只有一个真源
# ---------------------------------------------------------------------------

def test_fault_trap_records_through_diag_before_resetting():
    """trap 必须**先记账再复位**：不记账的话下次开机什么都没有。"""
    code = _render(_FAULT)
    body = _func_body(code, "static void fault_trap_impl(")

    order = [body.find("__disable_irq()"),
             body.find("hw2c_safe_state()"),
             body.find("hw2c_diag_on_fault("),
             body.find("NVIC_SystemReset()")]
    assert all(i >= 0 for i in order), (
        f"fault_trap_impl() 缺步骤（disable_irq/safe_state/diag_on_fault/"
        f"reset 的索引 = {order}）—— 诊断不是可选的装饰，漏一步就少一样证据")
    assert order == sorted(order), (
        f"执行顺序错了：{order}。记账必须在复位之前，安全态必须在记账之前"
        "（先断电再记录，否则记录期间执行机构还是带电的）")


def test_fault_keeps_a_single_source_of_truth_for_the_record():
    """记录实体只能有一份：fault 里不许再出现第二个 magic / 第二条记录。"""
    code = _render(_FAULT)

    assert "HW2C_FAULT_MAGIC" not in code, (
        "hw2c_fault.c 又定义了自己的 magic —— 两处各自记账，diag 报的地址和"
        "fault 报的错误码会来自两次不同的故障，读数互相矛盾")
    assert "g_fault_record" not in code, (
        "hw2c_fault.c 又留了一份 .noinit 记录 —— 这就是被收口掉的那个双真源")

    assert "hw2c_diag_last_code()" in _func_body(code, "hw2c_fault_last(void)"), (
        "hw2c_fault_last() 不再委托 hw2c_diag_last_code() —— 调用方读到的错误"
        "码和 diag 打出来的地址不是同一笔记录")


def test_fault_trap_must_not_be_inlined():
    """`hw2c_fault_trap()` 被内联后，`__builtin_return_address(0)` 取到的就是
    "调用者的调用者" —— 一个指错地方却看不出错的读数。"""
    code = _render(_FAULT)

    assert "HW2C_NOINLINE void hw2c_fault_trap(" in code, (
        "hw2c_fault_trap() 不是 noinline —— 一旦被内联，它记下的 LR 会指向"
        "上一层的上一层的某条指令，现场按这个地址查代码只会越查越糊涂")
    assert "__builtin_return_address(0)" in _func_body(
        code, "HW2C_NOINLINE void hw2c_fault_trap(uint32_t code)"), (
        "trap 没有取自己的返回地址 —— 非异常路径（断言失败、初始化失败）就"
        "拿不到故障点地址，只能打 lr=0x00000000")


def test_fault_exposes_a_context_entry_for_exception_paths():
    """异常路径要能把栈帧里的 PC/LR/PSR 直接交给 trap。"""
    code = _render(_FAULT)

    assert "void hw2c_fault_trap_ctx(uint32_t code, uint32_t pc, uint32_t lr, uint32_t psr)" in code, (
        "没有 hw2c_fault_trap_ctx() —— HardFault/NMI 拿到的栈帧地址无处可交，"
        "会被当成 lr=0 的普通陷阱，'故障点地址'这项需求就落空了")


# ---------------------------------------------------------------------------
# 2. 异常入口：naked 汇编取 PC/LR/xPSR
# ---------------------------------------------------------------------------

def test_exception_entry_reads_pc_lr_psr_from_the_stack_frame():
    """栈帧偏移写错（PC=24/LR=20/xPSR=28）会得到一串"像地址但不是故障点"
    的数 —— 比没有更糟，因为它看起来是有证据的。"""
    code = _render(_IT)
    macro = _macro_body(code, "#define HW2C_EXC_ENTRY_ASM(_c_entry)")

    for off, what in (("#20", "LR"), ("#24", "PC"), ("#28", "xPSR")):
        assert off in macro, (
            f"汇编入口没有取偏移 {off}（{what}）—— 打印出来的 {what} 会是错的")
    assert "psp" in macro and "msp" in macro, (
        "入口没有判 MSP/PSP —— 异常发生在任务里时栈帧在 PSP 上，按 MSP 取"
        "地址得到的是主栈里的无关数据")
    assert "tst" in macro, (
        "入口没有用 EXC_RETURN 的 bit2 选栈 —— 固定按 MSP 取，在 FreeRTOS"
        "任务里 HardFault 时会读到完全不相干的地址")


def test_handlers_are_naked_and_forward_the_frame():
    """不是 naked 就别谈"栈帧在哪"：第一条 C 语句之前编译器可能已经动过栈指针。"""
    code = _render(_IT)

    for handler, entry, code_name in (
            ("NMI_Handler", "hw2c_nmi_entry", "HW2C_FAULT_NMI"),
            ("HardFault_Handler", "hw2c_hardfault_entry", "HW2C_FAULT_HARDFAULT")):
        # 属性必须抓在**这个** handler 的声明上：只看"前面 120 字符里有没有
        # naked"会把上一个 handler 的属性算进来 —— 删掉 HardFault 的 naked
        # 也能蒙混过关（变异验证抓到过）。
        m = re.search(
            r"(?:__attribute__\(\((\w+)\)\)\s+)?void\s+%s\s*\(void\)\s*\{(.*?)\}"
            % handler, code, flags=re.S)
        assert m, f"渲染产物里找不到 {handler}()"
        assert m.group(1) == "naked", (
            f"{handler} 不是 __attribute__((naked))（找到的是 {m.group(1)!r}）"
            " —— 编译器生成的序言会动栈指针，后面按固定偏移取到的 PC/LR 就"
            "指错地方了")
        assert "HW2C_EXC_ENTRY_ASM(%s)" % entry in m.group(2), (
            f"{handler} 不再跳进 {entry}() —— 栈帧里的地址没有被交出去")
        assert f"hw2c_fault_trap_ctx({code_name}, pc, lr, psr)" in code, (
            f"{entry}() 没有把 {code_name} 和 pc/lr/psr 交给 trap —— 故障码"
            "和地址会来自两笔不同的记录")


def test_no_m3_m4_fault_status_registers_anywhere():
    """反向护栏：M0+ 的 SCB 没有这些寄存器，引用了就编译不过。"""
    banned = ("CFSR", "HFSR", "BFAR", "MMFAR")

    for path in sorted(_TEMPLATES.glob("src/*.j2")) + \
            sorted(_TEMPLATES.glob("drivers/*.j2")) + \
            sorted(_TEMPLATES.glob("bootloader/*.j2")):
        code = _strip_c_comments(path.read_text(encoding="utf-8"))
        for reg in banned:
            assert not re.search(r"\b%s\b" % reg, code), (
                f"{path.relative_to(_REPO_ROOT)} 引用了 {reg} —— M0+ 的 "
                "SCB_Type 只定义到 SHCSR，照抄 M3/M4 的故障打印代码在本工程"
                "编译不过。故障点地址只有 PC/LR/xPSR")


# ---------------------------------------------------------------------------
# 3. 任务名：切换时缓存，故障时不要再问内核
# ---------------------------------------------------------------------------

def test_task_switch_hook_caches_the_task_name():
    """hook 掉了 ⇒ task 永远是 '<none>'，"当时在跑哪个任务"这项需求蒸发。"""
    code = _render(_FREERTOS_CFG)

    m = re.search(r"#define\s+traceTASK_SWITCHED_IN\(\)\s+(\S+)", code)
    assert m, (
        "FreeRTOSConfig.h 没有定义 traceTASK_SWITCHED_IN() —— 任务名永远缓存"
        "不下来，故障记录里 task 一栏恒为 <none>")
    assert m.group(1) == "hw2c_diag_task_switched_in()", (
        f"traceTASK_SWITCHED_IN() 展开成 {m.group(1)}，不是诊断的缓存入口")
    assert re.search(r"#define\s+INCLUDE_pcTaskGetName\s+1\b", code), (
        "INCLUDE_pcTaskGetName 不是 1 —— pcTaskGetName() 不会被编译进内核，"
        "缓存任务名那条语句直接链接失败")


# ---------------------------------------------------------------------------
# 4. 关键生成点登记 tag
# ---------------------------------------------------------------------------

def test_cli_wrapper_enters_and_leaves_a_scope():
    """CLI 是最常见的"当时在跑什么"。只 enter 不 leave ⇒ 第一个命令的 tag
    一直挂着，之后的故障记录全部指错模块。"""
    code = _render(_CLI)
    macro = _macro_body(code, "#define CLI_HW2C_WRAPPER(name)")

    assert "hw2c_diag_enter(\"cli:\" #name, 0U)" in macro, (
        "CLI wrapper 没有登记 tag —— 故障记录里看不出当时在执行哪条命令")
    assert "hw2c_diag_leave()" in macro, (
        "CLI wrapper 没有 leave —— tag 会一直挂着，之后任何故障都被记成"
        "'发生在第一条命令里'")
    assert macro.index("hw2c_diag_enter") < macro.index("cmd_##name") < \
        macro.index("hw2c_diag_leave"), (
        "enter / 执行 / leave 的顺序不对 —— 命令体跑飞时 tag 还没挂上，或者"
        "异常返回路径绕过了 leave")


def test_every_registered_cli_command_is_wrapped():
    """注册了却没包 wrapper ⇒ 那条命令跑飞时 tag 是上一条命令的。"""
    code = _render(_CLI)

    wrapped = set(re.findall(r"^CLI_HW2C_WRAPPER\((\w+)\)", code, flags=re.M))
    registered = set(re.findall(r"cli_register_command\(\"(\w+)\"", code))
    registered |= set(re.findall(r"\{\"(\w+)\",\s+\"", code))

    unwrapped = {c for c in registered if c not in wrapped}
    assert not unwrapped, (
        f"这些命令进了命令表却没有 CLI_HW2C_WRAPPER：{sorted(unwrapped)} —— "
        "它们跑飞时诊断记到的是别的命令的 tag")


def test_cli_reset_notes_a_software_reset_before_resetting():
    """`reset` 是**唯一**能在没有异常的情况下验证整条链路的命令。"""
    code = _render(_CLI)
    body = _func_body(code, "static void cmd_reset(")

    assert "hw2c_diag_note(HW2C_FAULT_SOFT_RESET)" in body, (
        "cmd_reset 没有登记软件复位 —— 用户敲 reset 重启后，开机上报会说"
        "'没有故障'，而真相是'有人要求重启'")
    assert body.index("hw2c_diag_note") < body.index("NVIC_SystemReset"), (
        "登记发生在复位之后 —— 永远执行不到，diag 记录恒为上次的内容")


def test_cli_exposes_a_diag_command_with_last_and_crash():
    """`diag crash` 是整条链路**可复现的注入点**：没有它，"HardFault 时会不会
    打出地址和模块"只能靠运气碰。"""
    code = _render(_CLI)
    body = _func_body(code, "static void cmd_diag(")

    assert re.search(r"\{\"diag\",\s+\"", code), "命令表里没有 diag"
    assert "hw2c_diag_format_last(" in body and "\"last\"" in body, (
        "`diag last` 没有走 hw2c_diag_format_last() —— 打印的内容与开机上报"
        "的那行不是同一个真源，两处会分叉")
    assert "volatile" in body and "log_flush()" in body, (
        "`diag crash` 没有先 log_flush() 就去触发异常 —— HardFault 之后 TXE "
        "中断不再被服务，'我要崩了'这句话根本出不了串口")


def test_event_dispatch_registers_the_event_id():
    """事件 id 放进 detail ⇒ "哪个事件的处理逻辑跑飞了"不用猜。"""
    code = _render(_EVENT)
    task = _func_body(code, "void EventMgr_Task(void *pvParameters)")

    assert "hw2c_diag_enter(\"event\", (uint32_t)evt.id)" in task, (
        "事件派发没有登记 event id —— 派发回调里跑飞时，记录只会说"
        "'在 event 里'，看不出是哪个事件")
    assert task.index("hw2c_diag_enter") < task.index("switch(evt.id)") < \
        task.index("hw2c_diag_leave"), (
        "登记/派发/退出的顺序不对 —— 要么 run 之前没挂上，要么 leave 没包住"
        "整个派发过程")


def test_fota_state_changes_go_through_one_instrumented_function():
    """17 处直接写 `g_state`、靠人记得每处都登记 tag ⇒ 漏一个阶段就是
    "看起来有证据、指向的阶段却不对"。收口到一个函数才杜绝这件事。"""
    code = _render(_FOTA)

    setter = _func_body(code, "static void fota_set_state(fota_state_t s)")
    assert "hw2c_diag_enter(\"fota\"" in setter, (
        "fota_set_state() 没有登记 tag —— FOTA 各阶段的故障都记不到阶段号")

    # `(?!=)` 是为了把 `g_state == X` / `g_state != X` 这类比较排除掉：不加的
    # 话计数里混进 9 个比较，"有直写绕过收口函数"这件事根本看不出来。
    writes = re.findall(r"\bg_state\s*=(?!=)", code)
    assert len(writes) == 2, (
        f"渲染产物里 `g_state =` 出现了 {len(writes)} 次（期望 2：一处声明、"
        "一处 fota_set_state 内部）—— 有直写绕过了收口函数，那个阶段就不会"
        "被登记")


# ---------------------------------------------------------------------------
# 5. 开机上报
# ---------------------------------------------------------------------------

def test_boot_reports_the_previous_fault_with_its_context():
    """只报错码的话，"设备反复重启"在现场仍然只剩一个数字。"""
    code = _render(_MAIN)
    block = _window(code, "uint32_t prev_fault = hw2c_fault_last()")

    assert "hw2c_diag_report_last()" in block, (
        "开机上报没有把诊断快照打出去 —— 只报错码，看不出模块/任务/地址")
    m = re.search(r'log_error\(\s*"previous reset:[^"]*"\s*,\s*\(unsigned long\)prev_fault',
                  block, flags=re.S)
    assert m, (
        "故障码没有自己单独成一行 —— 要么没打，要么把它和诊断行拼在一起"
        "（会顶穿日志行宽被静默截断）。必须是 'previous reset: fault=0x%08lX …' "
        "外加 prev_fault 实参")
    assert "hw2c_fault_clear()" in block, (
        "上报后没有清闩锁 —— 同一笔故障会在之后的每次启动里反复上报，"
        "掩盖新的故障")
    assert block.index("hw2c_diag_report_last()") < block.index("hw2c_fault_clear()"), (
        "先清闩锁再上报 —— hw2c_diag_report_last() 会因为 code==0 直接返回，"
        "开机横幅里什么都看不到（静默失效）")


def test_diag_log_lines_are_split_so_the_log_buffer_cannot_truncate_them():
    """完整诊断行最长 149 B，而 drv_log 的行缓冲只给正文留 94 B。

    合成一行会被 msg_len 钳位**静默截断** —— 不报错、不打点，真机上只剩
    "count=" 这种半截尾巴（docs/reviews/onboard-capture-2026-09-23-diag.txt）。
    """
    code = _render(_DIAG)
    fault = _func_body(code, "void hw2c_diag_on_fault(")
    report = _func_body(code, "void hw2c_diag_report_last(void)")

    for name, body in (("hw2c_diag_on_fault", fault),
                       ("hw2c_diag_report_last", report)):
        assert "HW2C_DIAG_LOG_LINE_LEN" in body, (
            f"{name} 用的是 HW2C_DIAG_LINE_LEN(160) 的缓冲 —— 那条长行放进日志"
            "必然被截断，缓冲必须按日志行宽预算 HW2C_DIAG_LOG_LINE_LEN 开")
        assert "hw2c_diag_format_fault(" in body, f"{name} 没有输出故障点行"
        assert "hw2c_diag_format_ctx(" in body, f"{name} 没有输出上下文行"
        assert body.count("log_output(") >= 2, (
            f"{name} 只打了一条日志 —— 全部字段塞进一行会被静默截断")

    # 上下文行不许再被套上 "previous reset: "：89 + 16 会顶穿 94 B 的正文上限。
    # 必须查**每一处**——只查第一处的话，"给第二行也加上前缀"这种改法抓不到。
    prefixed = [m.group(1) for m in
                re.finditer(r'"previous reset:\s*%s"\s*,\s*(\w+)', report)]
    for name in prefixed:
        assert name == "fault_line", (
            f"'previous reset: ' 前缀套在了 {name} 上 —— 上下文行最长 "
            "89 B，再加 16 B 前缀会顶穿 94 B 的正文上限，尾部字段被静默截断")


def test_log_buffer_is_big_enough_for_the_diagnostic_line_budget():
    """HW2C_DIAG_LOG_LINE_LEN 是"正文"预算，必须真能塞进 drv_log 的行缓冲。

    两者分居两个模板，谁改了另一个都不会报错 —— 这就是它们必须对账的原因。
    """
    log_c = _render(_LOG_C)
    diag_h = _render(_DIAG_H)

    m = re.search(r"#define\s+LOG_MSG_BUF_SIZE\s+(\d+)", log_c)
    assert m, "drv_log.c 里找不到 LOG_MSG_BUF_SIZE —— 常量改名了，护栏要跟着改"
    msg_buf = int(m.group(1))

    m = re.search(r"#define\s+HW2C_DIAG_LOG_LINE_LEN\s+(\d+)", diag_h)
    assert m, "hw2c_diag.h 里找不到 HW2C_DIAG_LOG_LINE_LEN"
    budget = int(m.group(1))

    # 时间戳 + 级别前缀 "[YYYY-MM-DD HH:MM:SS.mmm] [ERR] " = 32 B，CRLF = 2 B
    body = msg_buf - 32 - 2
    assert budget <= body, (
        f"诊断行预算 {budget} B 超过日志正文 {body} B "
        f"(LOG_MSG_BUF_SIZE={msg_buf} − 前缀 32 − CRLF 2) —— "
        "诊断行会被 drv_log 静默截断")

    # 开机上报还要在故障行前面加 "previous reset: "(16)，那一格也要留出来。
    assert budget >= 16 + 65, (
        f"诊断行预算 {budget} B 放不下 'previous reset: '(16) + 故障行(65)")


# ---------------------------------------------------------------------------
# 6. 日志不可用时不得二次 HardFault
# ---------------------------------------------------------------------------

def test_log_ready_gate_exists_and_is_armed_by_log_init():
    """故障可能发生在 log_init() 之前：那时往还没 lwrb_init() 的环形缓冲里
    写字节 = 二次 HardFault，连"我要崩了"都出不去。"""
    assert "int log_ready(void);" in _render(_LOG_H), (
        "drv_log.h 没有声明 log_ready() —— 故障路径无从判断日志能不能用")
    assert "log_ringbuf_ready = 1" in _func_body(_render(_LOG_C), "void log_init(void)"), (
        "log_init() 没有置位可用标志 —— log_ready() 恒为 0，故障日志一行都不打")


def test_diag_never_touches_the_log_before_asking_whether_it_is_ready():
    code = _render(_DIAG)
    fault = _func_body(code, "void hw2c_diag_on_fault(")

    assert "log_ready()" in fault, (
        "打日志前没有问 log_ready() —— 故障发生在 log_init() 之前时会二次 "
        "HardFault，连记录本身都可能来不及写完")
    assert fault.index("log_ready()") < fault.index("log_output("), (
        "判据在 log_output() 之后 —— 等于没判")
    assert fault.index("diag_store(") < fault.index("log_ready()"), (
        "先判日志再记账 —— 日志不可用时记录就丢了，.noinit 也就没意义了")


def test_diag_header_is_self_contained_for_stdint_types():
    """对外暴露 uint32_t 的头文件必须自带 <stdint.h>：指望调用方先包含，
    会以 unknown type name 的形式在某个示例上炸出来。"""
    header = _render(_DIAG_H)
    assert "#include <stdint.h>" in header, (
        "hw2c_diag.h 暴露 stdint 类型却没包含 <stdint.h>")


# ---------------------------------------------------------------------------
# 7. 构建接线：编 fault 的地方必须编 diag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmake_tmpl", [
    "project/CMakeLists.txt.j2",
    "test/CMakeLists_sil.txt.j2",
])
def test_diag_is_compiled_wherever_fault_is_compiled(cmake_tmpl):
    """只编 fault 不编 diag = 链接期缺符号，而且只在"某个示例恰好没开某个
    功能"时才冒出来 —— 所以两个构建清单都要盯。"""
    text = _raw(cmake_tmpl)

    assert re.search(r"^\s*\S*hw2c_fault\.c\s*$", text, flags=re.M), (
        f"{cmake_tmpl} 里连 hw2c_fault.c 都没有 —— 清单结构变了，护栏要跟着改")
    # 必须查"独占一行的源清单项"：只查 "hw2c_diag.c" 这个子串的话，注释里
    # 提到它也算命中，删掉真正的那一行照样全绿（变异验证抓到过）。
    assert re.search(r"^\s*\S*hw2c_diag\.c\s*$", text, flags=re.M), (
        f"{cmake_tmpl} 编译 hw2c_fault.c 却没有把 hw2c_diag.c 列进源清单 —— "
        "fault 把记录委托给了 diag，缺它会在链接期炸")


def test_sil_build_can_see_the_vendored_ring_buffer():
    """hw2c_diag.c 在开了日志的工程里会包含 drv_log.h → lwrb.h。SIL 的
    include 路径里没有 vendor 的 lwrb，就是一条 `fatal error: lwrb.h:
    No such file`，而且只在**开了日志**的工程上冒出来。"""
    text = _raw("test/CMakeLists_sil.txt.j2")

    assert "static/third_party/lwrb" in text, (
        "SIL 的 include_directories 里没有 vendor 的 lwrb —— hw2c_diag.c "
        "（has_log 时包含 drv_log.h）会直接编译失败")
    # 深度必须是 4 级：sil → test → <project> → output → 仓库根。
    # 少一级会指到 output/static/... 那个不存在又不报错的地方。
    assert re.search(r"\$\{CMAKE_SOURCE_DIR\}/(?:\.\./){4}static/third_party/lwrb",
                     text), (
        "lwrb 的路径层级不对 —— CMake 里多写/少写一层 `../` 不会报错，只会"
        "指向一个不存在的目录，然后在真正 include 时才炸")


def test_generator_ships_the_diag_module_and_its_host_test():
    """模板不在发货清单里 = 生成出来的工程根本没有这个文件。"""
    gen = (_REPO_ROOT / "generator" / "generate.py").read_text(encoding="utf-8")

    for entry in ("\"src/hw2c_diag.h.j2\"", "\"src/hw2c_diag.c.j2\"",
                  "\"test/test_hw2c_diag.c.j2\""):
        assert entry in gen, (
            f"generate.py 的清单里没有 {entry} —— 新工程不会生成这个文件")


@pytest.mark.parametrize("harness", [
    "generator/tests/test_fota_protocol_l5.py",
    "generator/tests/test_fota_ymodem_l5.py",
])
def test_l5_harnesses_compile_the_diag_module(harness):
    """L5 台架编译的是渲染出的真实源码：少了 diag.c 它就跑不起来。"""
    text = (_REPO_ROOT / harness).read_text(encoding="utf-8")

    # 两条都要盯，而且都要盯"具体那一项"：只查 "hw2c_diag.c" 这个子串的话，
    # 渲染清单里的 "src/hw2c_diag.c.j2" 本身就含它 —— gcc 清单漏了也照样
    # 通过（变异验证抓到过）。
    assert '"src/hw2c_diag.c.j2"' in text, f"{harness} 没有渲染 hw2c_diag.c.j2"
    assert re.search(r"srcs\[\"hw2c_diag\.c\"\]", text), (
        f"{harness} 的 gcc 编译清单里没有 hw2c_diag.c —— 台架会在链接期缺符号")


# ---------------------------------------------------------------------------
# 8. .noinit 段：记录活过复位的物理前提
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ld_tmpl", [
    "linker/app_slot_a.ld.j2",
    "linker/app_slot_b.ld.j2",
    "linker/bootloader.ld.j2",
    "linker/STM32G0B1RETx_FLASH.ld.j2",
])
def test_every_linker_script_keeps_a_noinit_section(ld_tmpl):
    """段被删/被合并进 .bss ⇒ 启动代码把它清零，"上次为什么复位"永远查不到。"""
    text = _raw(ld_tmpl)

    assert ".noinit" in text, f"{ld_tmpl} 没有 .noinit 段"
    assert "NOLOAD" in text, (
        f"{ld_tmpl} 的 .noinit 不是 NOLOAD —— 它会被算进可加载镜像，上次"
        "故障记录随烧录一起被覆盖掉")
