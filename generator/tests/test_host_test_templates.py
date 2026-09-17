"""
主机测试模板护栏（templates/test/*.j2）

这个测试存在的原因
------------------
2026-09-17 在 `output/mhde_mainboard` 上跑主机测试时，16 个用例里有 3 个是坏的，
而且**三个坏的都是"测试模板里写死了一个本应派生的值"**：

  · `test_spi_api.c.j2` 写死 `spi1` / `hspi1`，而 `spi_api.c` 的总线名是从
    `peripherals` 派生的（`p.bus` → `"spi2"`）⇒ 链接期 `undefined reference to 'hspi2'`。
  · `test_led.c.j2` 把图案设到**实例 0**，再用 mock 的"全局最后一次写"去观测。
    但 `led_step()` 按 0..N-1 顺序写**所有**实例，最后写的是实例 N-1 ⇒ 观测串位。
  · `test_btn.c.j2` 包含 `param_registry.c`（有 persistent 参数时会调
    `nvm_param_load/save`），却没有提供持久化层 ⇒ 链接失败。

三者都是**只在特定板子上才暴露**的：总线不是 spi1、LED 多于一个、有 persistent 参数。
其它示例（1 个 LED、SPI 挂在 spi1、无持久化参数）一路全绿，于是"主机测试没问题"
这个结论被维持了很久。所以护栏直接钉住"必须派生"这件事：

  1. SPI 测试的 hspiN 定义集合 == 从 peripherals 派生出的总线集合；
     且渲染结果里不得出现任何未经派生的 hspiN / spi_open 字面量。
  2. LED 测试的探针必须是**最后被写的那个实例**（PROBE_INDEX = COUNT-1），
     极性/名字取自该实例本身，Pattern 设置与观测都用它。
  3. BTN 测试在 `has_persistent_params` 时必须自带语义正确的持久化替身
     （未存过 = NOT_FOUND，存了能读回），并且在 `param_init()` 之前清空。

每条 check_* 都配一个"变异用例"（把渲染结果改坏，check 必须报出来），
否则无法区分"护栏在盯着模板"和"护栏从来不会报警"。

注意：判"某关键字不得出现"之前先剥 C 注释 —— 本文件的说明文字里就写着
`hspi1`、`spi1` 这些词，不剥注释会被自己绊倒（仓库里已经踩过一次）。
"""

from __future__ import annotations

import os
import re

from jinja2 import Environment, FileSystemLoader

from generator.jinja_filters import register_filters
from generator.paths import TEMPLATES_DIR

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_RUN_TESTS = os.path.join(_REPO_ROOT, "generator", "run_tests.py")


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------

def _render(template_name: str, context: dict) -> str:
    """按生成器**同样的**环境参数渲染模板（trim/lstrip_blocks 会影响换行）。"""
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR, encoding="utf-8"),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    register_filters(env)
    return env.get_template(template_name).render(context)


def _strip_c_comments(text: str) -> str:
    """去掉 C 注释，再做"关键字不得出现"的判据。

    注释里出现关键字是常态（本仓库的说明性注释很啰嗦），不剥掉就会出现
    "护栏被自己的说明文字绊倒"或者"注释里的旧名字让护栏误报"。
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


# ---------------------------------------------------------------------------
# 上下文构造（与 hardware.yaml → context 同形）
# ---------------------------------------------------------------------------

def _spi_ctx(buses, cs_label="spi2_nss", cs_pin="PB12"):
    """buses 是真实总线名列表（大小写随意），首元素即"主总线"。"""
    peripherals = [
        {
            "name": f"dev_{i}",
            "type": "SPI_Flash_Generic",
            "bus": b,
            "cs_pin": cs_pin,
            "model": {"interface": "SPI"},
        }
        for i, b in enumerate(buses)
    ]
    pins = [{"id": cs_pin, "label": cs_label, "function": "GPIO_Output"}]
    return {"peripherals": peripherals, "pins": pins}


def _no_spi_ctx():
    return {"peripherals": [], "pins": []}


def _led_ctx(leds):
    """leds: [(id, label, active_level)] —— 顺序即实例表顺序。"""
    return {
        "led_pins": [
            {"id": i, "label": lbl, "active_level": lvl, "function": "GPIO_Output"}
            for i, lbl, lvl in leds
        ]
    }


def _btn_ctx(has_persistent: bool):
    return {"has_params": True, "has_persistent_params": has_persistent}


# 真实板子上的三种形状（都对过生成产物）
MHDE_LEDS = [
    ("PC0", "LED1_B", "low"),
    ("PC1", "LED", "low"),
    ("PC2", "LED1_R", "low"),
    ("PA0", "LED2_G", "low"),
    ("PA1", "LED2_R", "low"),
    ("PC3", "LED2_B", "low"),
]
# 首个与末个极性**不同**的板子：用来暴露"极性取自实例 0 而不是探针"的错误
MIXED_LEDS = [
    ("PC0", "LED_A", "low"),
    ("PC1", "LED_B", "high"),
]


# ---------------------------------------------------------------------------
# 1) SPI 测试模板：总线名必须派生
# ---------------------------------------------------------------------------

_HSPI_DEF_RE = re.compile(r"^SPI_HandleTypeDef\s+(hspi\d+)\s*;", re.M)
_HSPI_ANY_RE = re.compile(r"\bhspi(\d+)\b")
_BUS_PRIMARY_RE = re.compile(r'^#define BUS_PRIMARY\s+"([^"]*)"', re.M)
_SPI_OPEN_LIT_RE = re.compile(r'spi_open\(\s*"([^"]*)"')


def check_spi_test_bus_names(rendered: str, buses) -> list[str]:
    """渲染出的 SPI 测试必须与 `spi_api.c` 的总线派生完全一致。"""
    problems: list[str] = []
    code = _strip_c_comments(rendered)

    expected_handles = {f"hspi{b[-1]}" for b in buses}
    found_handles = set(_HSPI_DEF_RE.findall(code))
    if found_handles != expected_handles:
        problems.append(
            f"hspi 定义集合 {sorted(found_handles)} != 派生的 {sorted(expected_handles)}"
        )

    # 出现任何未派生的 hspiN 都是写死的痕迹（例如板子是 spi2 却引用 hspi1）
    stray = {f"hspi{n}" for n in _HSPI_ANY_RE.findall(code)} - expected_handles
    if stray:
        problems.append(f"出现了未派生的句柄 {sorted(stray)}")

    m = _BUS_PRIMARY_RE.search(code)
    want_primary = buses[0].lower() if buses else ""
    if m is None:
        problems.append("缺少 BUS_PRIMARY 定义")
    elif m.group(1) != want_primary:
        problems.append(f"BUS_PRIMARY = {m.group(1)!r}，应为派生的 {want_primary!r}")

    # 只允许把"保证不存在"的总线名写成字面量；真实总线名必须走宏
    for lit in _SPI_OPEN_LIT_RE.findall(code):
        problems.append(f"spi_open 用了字面量 {lit!r}（必须用 BUS_PRIMARY/BUS_ABSENT）")

    if len(buses) > 1:
        for b in buses[1:]:
            if f"hspi{b[-1]}" not in found_handles:
                problems.append(f"多总线板漏了 hspi{b[-1]}")
    return problems


def test_spi_test_derives_bus_from_peripherals():
    """mhde_mainboard 的实际形状：SPI 挂在 SPI2 上。"""
    rendered = _render("test/test_spi_api.c.j2", _spi_ctx(["spi2"]))
    assert check_spi_test_bus_names(rendered, ["spi2"]) == []
    # 真正钉住的一行：板子是 spi2，就必须定义 hspi2
    assert "SPI_HandleTypeDef hspi2;" in rendered
    assert "hspi1" not in _strip_c_comments(rendered)


def test_spi_test_handles_every_bus():
    rendered = _render("test/test_spi_api.c.j2", _spi_ctx(["spi1", "spi2"]))
    assert check_spi_test_bus_names(rendered, ["spi1", "spi2"]) == []
    assert "SPI_HandleTypeDef hspi1;" in rendered
    assert "SPI_HandleTypeDef hspi2;" in rendered


def test_spi_test_without_devices_emits_no_handle():
    rendered = _render("test/test_spi_api.c.j2", _no_spi_ctx())
    assert check_spi_test_bus_names(rendered, []) == []
    # 没有器件时不该凭空造一个句柄定义
    assert _HSPI_DEF_RE.search(_strip_c_comments(rendered)) is None


def test_check_spi_has_teeth():
    """变异：把派生出来的 hspi2 改回写死的 hspi1，护栏必须报出来。"""
    rendered = _render("test/test_spi_api.c.j2", _spi_ctx(["spi2"]))
    tampered = rendered.replace("hspi2", "hspi1").replace('"spi2"', '"spi1"')
    problems = check_spi_test_bus_names(tampered, ["spi2"])
    assert problems, "把句柄/总线名改回写死值之后护栏没有报警"


# ---------------------------------------------------------------------------
# 2) LED 测试模板：探针必须是最后被写的那个实例
# ---------------------------------------------------------------------------

_PROBE_INDEX_RE = re.compile(r"^#define PROBE_INDEX\s+\(LED_INSTANCE_COUNT\s*-\s*1u\)", re.M)
_PROBE_ON_RE = re.compile(r"^#define PROBE_ON\s+\((\w+)\)", re.M)
_PROBE_OFF_RE = re.compile(r"^#define PROBE_OFF\s+\((\w+)\)", re.M)
_PROBE_NAME_RE = re.compile(r'^#define PROBE_NAME\s+"([^"]*)"', re.M)
_SET_IDX_RE = re.compile(r"led_set_pattern_by_index\(\s*([A-Za-z_0-9]+)\s*,")
_TOGGLE_IDX_RE = re.compile(r"led_toggle_pattern\(\s*([A-Za-z_0-9]+)\s*\)")


def _expected_levels(active_level: str) -> tuple[str, str]:
    low = active_level.lower() == "low"
    return ("GPIO_PIN_RESET", "GPIO_PIN_SET") if low else ("GPIO_PIN_SET", "GPIO_PIN_RESET")


def check_led_test_probe(rendered: str, leds) -> list[str]:
    """被测实例必须是最后被写的那个（mock 只能观测最后一次写）。"""
    problems: list[str] = []
    code = _strip_c_comments(rendered)

    if not leds:
        return problems

    if not _PROBE_INDEX_RE.search(code):
        problems.append("PROBE_INDEX 未固定为 (LED_INSTANCE_COUNT - 1u)")

    last_id, last_label, last_level = leds[-1]
    want_on, want_off = _expected_levels(last_level)

    m_on = _PROBE_ON_RE.search(code)
    m_off = _PROBE_OFF_RE.search(code)
    m_name = _PROBE_NAME_RE.search(code)
    if m_on is None or m_on.group(1) != want_on:
        problems.append(
            f"PROBE_ON = {m_on.group(1) if m_on else None}，应按探针 {last_label}"
            f"({last_level}) 取 {want_on}"
        )
    if m_off is None or m_off.group(1) != want_off:
        problems.append(
            f"PROBE_OFF = {m_off.group(1) if m_off else None}，应按探针 {last_label}"
            f"({last_level}) 取 {want_off}"
        )
    if m_name is None or m_name.group(1) != last_label:
        problems.append(
            f"PROBE_NAME = {m_name.group(1) if m_name else None}，应为最后实例的 {last_label!r}"
        )

    # 图案设置/翻转只许用探针索引（99 是"越界索引被忽略"那条用例）
    allowed = {"PROBE_INDEX", "99"}
    for arg in _SET_IDX_RE.findall(code):
        if arg not in allowed:
            problems.append(f"led_set_pattern_by_index 用了非探针索引 {arg!r}")
    for arg in _TOGGLE_IDX_RE.findall(code):
        if arg not in allowed:
            problems.append(f"led_toggle_pattern 用了非探针索引 {arg!r}")
    return problems


def test_led_test_probes_last_written_instance():
    """mhde_mainboard 的形状：6 个 LED，全部 active_low。"""
    rendered = _render("test/test_led.c.j2", _led_ctx(MHDE_LEDS))
    assert check_led_test_probe(rendered, MHDE_LEDS) == []
    # 真正钉住的一行：图案不能设到实例 0 上再用全局最后一次写观测
    assert re.search(r"led_set_pattern_by_index\(\s*0\s*,", _strip_c_comments(rendered)) is None


def test_led_test_probe_polarity_comes_from_the_probe_not_the_first():
    """首个与末个极性不同的板子：极性必须取自探针实例。"""
    rendered = _render("test/test_led.c.j2", _led_ctx(MIXED_LEDS))
    assert check_led_test_probe(rendered, MIXED_LEDS) == []
    assert _PROBE_NAME_RE.search(rendered).group(1) == "LED_B"
    # 探针是 active_high ⇒ ON 是高电平
    assert _PROBE_ON_RE.search(rendered).group(1) == "GPIO_PIN_SET"
    assert _PROBE_OFF_RE.search(rendered).group(1) == "GPIO_PIN_RESET"


def test_check_led_has_teeth_on_index():
    """变异：把探针换回实例 0（旧写法），护栏必须报出来。"""
    rendered = _render("test/test_led.c.j2", _led_ctx(MHDE_LEDS))
    tampered = rendered.replace("led_set_pattern_by_index(PROBE_INDEX,", "led_set_pattern_by_index(0,")
    problems = check_led_test_probe(tampered, MHDE_LEDS)
    assert problems, "把探针换回实例 0 之后护栏没有报警"


def test_check_led_has_teeth_on_polarity():
    """变异：极性改回"取第一个实例"（旧写法的另一半），护栏必须报出来。"""
    rendered = _render("test/test_led.c.j2", _led_ctx(MIXED_LEDS))
    tampered = rendered.replace(
        "#define PROBE_ON     (GPIO_PIN_SET)", "#define PROBE_ON     (GPIO_PIN_RESET)"
    )
    problems = check_led_test_probe(tampered, MIXED_LEDS)
    assert problems, "极性取错之后护栏没有报警"


# ---------------------------------------------------------------------------
# 3) BTN 测试模板：有持久化参数就必须自带持久化层
# ---------------------------------------------------------------------------

_LOAD_DEF_RE = re.compile(r"^int\s+nvm_param_load\(uint16_t key", re.M)
_SAVE_DEF_RE = re.compile(r"^int\s+nvm_param_save\(uint16_t key", re.M)


def check_btn_test_persistence(rendered: str, has_persistent: bool) -> list[str]:
    """`param_registry.c` 在 has_persistent_params 时会调 nvm_param_*，必须能链接到。"""
    problems: list[str] = []
    code = _strip_c_comments(rendered)
    has_load = _LOAD_DEF_RE.search(code) is not None
    has_save = _SAVE_DEF_RE.search(code) is not None

    if has_persistent and not (has_load and has_save):
        problems.append(
            "有 persistent 参数却没有 nvm_param_load/save 定义"
            " ⇒ 链接期 undefined reference"
        )
    if not has_persistent and (has_load or has_save):
        problems.append("没有 persistent 参数却塞了持久化替身")

    if has_persistent:
        # 替身必须真的置空、且置空在 param_init() 之前，否则持久化值会跨用例串味
        call_reset = "    nvm_stub_reset();"
        call_init = "    param_init();"
        if call_reset not in code:
            problems.append("setUp 未清空替身")
        elif call_init in code and code.index(call_reset) > code.index(call_init):
            problems.append("替身清空发生在 param_init() 之后 ⇒ 加载到的还是上一个用例的值")
        # 替身不能退化成"永远 NOT_FOUND"的空壳：必须有语义自检
        for name in (
            "test_nvm_stub_load_of_absent_key_reports_not_found",
            "test_nvm_stub_save_then_load_round_trips",
        ):
            if f"RUN_TEST({name})" not in code:
                problems.append(f"缺少替身语义自检 {name}")
    return problems


def test_btn_test_links_persistence_layer_when_params_are_persistent():
    rendered = _render("test/test_btn.c.j2", _btn_ctx(True))
    assert check_btn_test_persistence(rendered, True) == []
    assert "int nvm_param_load(uint16_t key" in rendered
    assert "int nvm_param_save(uint16_t key" in rendered


def test_btn_test_without_persistent_params_has_no_stub():
    rendered = _render("test/test_btn.c.j2", _btn_ctx(False))
    assert check_btn_test_persistence(rendered, False) == []
    assert "nvm_param_load" not in _strip_c_comments(rendered)


def test_check_btn_has_teeth():
    """变异：删掉替身定义（回到链接失败的旧样子），护栏必须报出来。"""
    rendered = _render("test/test_btn.c.j2", _btn_ctx(True))
    start = rendered.index("int nvm_param_load(uint16_t key")
    end = rendered.index("}", rendered.index("return NVM_PARAM_OK;", start)) + 1
    tampered = rendered[:start] + rendered[end:]
    problems = check_btn_test_persistence(tampered, True)
    assert problems, "删掉持久化替身之后护栏没有报警"


def test_check_btn_has_teeth_on_reset_ordering():
    """变异：把清空替身挪到 param_init() 之后，护栏必须报出来。"""
    rendered = _render("test/test_btn.c.j2", _btn_ctx(True))
    tampered = rendered.replace("    nvm_stub_reset();\n", "")
    tampered = tampered.replace("    param_init();\n", "    param_init();\n    nvm_stub_reset();\n")
    problems = check_btn_test_persistence(tampered, True)
    assert problems, "清空替身顺序反了之后护栏没有报警"


# ---------------------------------------------------------------------------
# 4) 三个模板都不得把"实例/总线"写死 —— 结构性回归
# ---------------------------------------------------------------------------

def test_no_test_template_hardcodes_a_peripheral_instance():
    """SPI 总线名、LED 实例索引都不得在渲染结果里写死。

    这条是"疾病"层面的护栏：前三条各自盯着一个模板，这条盯着"写死"这件事本身。
    """
    spi_code = _strip_c_comments(_render("test/test_spi_api.c.j2", _spi_ctx(["spi2"])))
    assert not re.search(r"\bhspi1\b", spi_code), "SPI 测试里出现了写死的 hspi1"

    led_code = _strip_c_comments(_render("test/test_led.c.j2", _led_ctx(MHDE_LEDS)))
    assert "PROBE_INDEX" in led_code
    stray = [
        a for a in _SET_IDX_RE.findall(led_code) + _TOGGLE_IDX_RE.findall(led_code)
        if a not in {"PROBE_INDEX", "99"}
    ]
    assert not stray, f"LED 测试里还有写死的实例索引 {stray}（99 是越界用例，允许）"


# ---------------------------------------------------------------------------
# 5) 测试运行器：必须跑完全部用例再汇总
# ---------------------------------------------------------------------------

def check_runner_runs_every_test(text: str) -> list[str]:
    """`generator/run_tests.py` 不得在逐个用例的循环里直接退出。

    旧写法是"遇到首个失败就 sys.exit(1)"，于是一个工程里同时坏了三个用例时，
    只看这个脚本会让人以为"只有一个问题"：修掉第一个，第二个才浮出来。
    排查成本被这个提前退出放大好几倍（2026-09-17 实测）。
    """
    problems: list[str] = []
    if "def main(" not in text or "for test in tests:" not in text:
        return ["run_tests.py 结构变了，护栏需要更新"]

    body = text[text.index("def main("):]
    loop = body[body.index("for test in tests:"):]
    cutoff = loop.index("if args.coverage:") if "if args.coverage:" in loop else len(loop)
    loop = loop[:cutoff]

    if "sys.exit" in loop:
        problems.append("逐个用例的循环里有 sys.exit ⇒ 只会看到第一个失败")
    if "continue" not in loop:
        problems.append("循环里没有 continue ⇒ 编译失败后不会继续跑剩下的用例")
    if "sys.exit(1)" not in body:
        problems.append("没有失败退出码 ⇒ CI 会误判为通过")
    return problems


def test_run_tests_reports_every_failure():
    with open(_RUN_TESTS, encoding="utf-8") as f:
        text = f.read()
    assert check_runner_runs_every_test(text) == []


def test_check_runner_has_teeth():
    """变异：把"首个失败就退出"写回去，护栏必须报出来。"""
    with open(_RUN_TESTS, encoding="utf-8") as f:
        text = f.read()
    tampered = text.replace(
        'results.append((test, "compile-failed"))\n            continue',
        'results.append((test, "compile-failed"))\n            sys.exit(1)',
    )
    assert tampered != text, "变异锚点不存在"
    assert check_runner_runs_every_test(tampered), "提前退出写回去之后护栏没有报警"
