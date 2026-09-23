"""开机横幅必须打印"实际在跑的固件版本"（FR-15.6）。

真实缺陷背景
------------
横幅原本只打印 `project.version` —— 一个**生成期**常量。而引导器与
`patch_crc.py` 用的是镜像头里的 `fw_version`，那是**构建期**写进去的
（CMake 的 `FW_VERSION`）。两者一旦分叉，设备不报任何错，只是"版本看起来
不对"：

  · 镜像头停留在上次 configure 留下的旧值（`docs/reviews/onboard-capture
    -2026-09-17.txt` §7.3 缺陷 D：改完 task.yaml 重新 configure，横幅报新的、
    镜像头还是旧的 ⇒ 同一块板子上两个版本号）；
  · 直接烧了没走 `patch_crc.py` 的裸 `.bin`（头是擦除态 0xFFFFFFFF）。

这两种情况的症状一模一样："升级看起来没生效"。FR-15.6 因此要求开机读
**自身镜像头**、打印读到的值，并与编译进去的那个值判等 —— 让该失效模式
在第一行日志里自己暴露，而不是留给现场猜。

为什么是渲染级断言
------------------
`main()` 会真起调度器、`boot_app.c` 直接碰 TAMP 寄存器，主机上跑不现实。
钉住的是**渲染产物的结构性质**；行为由真机实验补（烧进去看串口第一行）。

⚠️ 断言容易写成恒真（"文件里有没有 Firmware v"）。本文件的变异脚本
`.workbuddy/tmp/mutate_boot_banner_version.py` 用 6 个变异体验证每条断言
确实会失败，其中含反向变异（把版本换成编译期常量、把判等换成恒真）。
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
_MAIN = "src/main.c.j2"
_BOOT_APP = "bootloader/boot_app.c.j2"

# 差分用的两个版本：断言必须能看出"渲染产物随 project.version 变化"，
# 否则"版本被写死"这件事根本抓不到（FR-14.7 就是这个失效模式）。
_VER_A = 0x010000     # 1.0.0
_VER_B = 0x020304     # 2.3.4


# ---------------------------------------------------------------------------
# 渲染与预处理
# ---------------------------------------------------------------------------

def _strip_c_comments(text: str) -> str:
    """剥掉 C 注释：注释里会写到被禁的旧写法，不剥会误报甚至恒真。"""
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


def _context(with_bootloader: bool, packed: int):
    from generator.context.builder import build_context

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
    }
    if with_bootloader:
        hardware["bootloader"] = {"enabled": True}
    ctx = build_context(hardware, "boot-banner-version-guard")
    ctx["has_log"] = True
    ctx["project_name"] = "guard_proj"
    ctx["project_version"] = "1.0.0"
    ctx["project_version_packed"] = packed
    return ctx


def _render_main(with_bootloader: bool = True, packed: int = _VER_A) -> str:
    return _make_env().get_template(_MAIN).render(
        **_context(with_bootloader, packed))


def _banner_block(with_bootloader: bool = True, packed: int = _VER_A) -> str:
    """横幅里"固件版本自检"这一段的源码（已剥注释）。"""
    code = _strip_c_comments(_render_main(with_bootloader, packed))
    # 起点要落在 `uint32_t img_ver = boot_app_image_version();` 之前：
    # 只从 "Firmware v" 起算会把"版本是从镜像头读来的"这个最关键的事实
    # 排除在区间外，剩下"有没有打印一串数字"这种恒真断言。
    anchors = [p for p in (code.find("img_ver"), code.find("Firmware v")) if p >= 0]
    assert anchors, (
        "main.c 横幅里既没有 img_ver 也没有 `Firmware v` —— 开机版本打印被删掉了，"
        "FR-15.6 不再成立（这条失败是真的，别改成'可选'）")
    i = min(anchors)
    j = code.find("System ready", i)
    end = j if j > 0 else i + 2000
    return code[i:end]


def _render_boot_app() -> str:
    return _strip_c_comments(
        _make_env().get_template(_BOOT_APP).render(**_context(True, _VER_A)))


# ---------------------------------------------------------------------------
# 1. 版本必须来自镜像头，且随 project.version 变化
# ---------------------------------------------------------------------------

def test_banner_reads_the_version_from_the_image_header():
    """有镜像头时，横幅打印的版本必须是**读出来的**，不是编译期字符串。"""
    block = _banner_block()

    assert "boot_app_image_version()" in block, (
        "横幅没有调用 boot_app_image_version() —— 打印的仍是编译期常量，"
        "而镜像头里装的是哪一版就无从证明（缺陷 D 的失效模式）")

    # 打印的必须是读到的那个变量，而不是另一次常量展开
    printed = re.findall(r"log_info\([^;]*?\);", block, flags=re.S)
    assert any("img_ver" in p for p in printed), (
        "log_info 里没有打印 img_ver —— 读是读了，但打出去的还是常量")


def test_banner_version_follows_the_configured_project_version():
    """差分断言：换一个 project.version，横幅里的判据值必须跟着变。"""
    a = _banner_block(packed=_VER_A)
    b = _banner_block(packed=_VER_B)

    assert a != b, (
        "两个 project.version 渲染出的横幅完全相同 —— 版本又被写死了")
    assert "0x010000" in a and "0x020304" in b, (
        "横幅里的一致性别据没跟着 project.version 走："
        f"1.0.0 → {_hexes(a)}，2.3.4 → {_hexes(b)}")
    assert "0x010000" not in b, (
        "2.3.4 的横幅里还留着 1.0.0 的十六进制值 —— 常量没被替换干净")


def _hexes(text: str):
    return sorted(set(re.findall(r"0x[0-9A-Fa-f]{6}", text)))


# ---------------------------------------------------------------------------
# 2. 不得有静默出口
# ---------------------------------------------------------------------------

def test_invalid_image_header_is_reported_not_swallowed():
    """镜像头无效 / 未回填时必须有 log_error，并给出 magic 与槽基址。"""
    block = _banner_block()

    m = re.search(r"if\s*\(\s*img_ver\s*==\s*0xFFFFFFFFUL\s*\)\s*\{(.*?)\n\s*\}",
                  block, flags=re.S)
    assert m, (
        "横幅没有「镜像头无效」分支 —— 烧了未回填的裸 .bin 时会静默打出一串"
        "无意义的版本号，而现场没有任何提示")
    body = m.group(1)
    assert "log_error" in body, (
        "无效分支不打 log_error —— 这个失效模式就没出过设备")
    assert "boot_app_image_magic()" in body, (
        "无效分支没打印 magic 的实际值 —— 现场分不清'没回填'和'偏移错位'")
    assert "boot_app_slot_base()" in body, (
        "无效分支没打印槽基址 —— 分不清镜像被烧到了哪个槽")


def test_header_vs_built_version_mismatch_is_reported():
    """镜像头版本 != 编译进来的版本，必须当场报出**两个**值。"""
    block = _banner_block()

    m = re.search(r"if\s*\(\s*img_ver\s*!=\s*0x[0-9A-Fa-f]{6}UL\s*\)\s*\{(.*?)\n\s*\}",
                  block, flags=re.S)
    assert m, (
        "横幅没有「镜像头版本 != 编译版本」的判据 —— 正是这个判据让"
        "'同一块板子两个版本号'在开机第一行就暴露出来")
    body = m.group(1)
    assert "log_error" in body, "版本不一致只打了 info —— 会被当成正常输出扫过去"
    assert "img_ver" in body, "报错里没有镜像头的实际值"
    assert "0x%06lX" in body, "报错里没有编译版本的实际值（两个值必须都给）"


def test_the_slot_base_is_printed_not_a_slot_letter():
    """打印槽**基址**而不是 A/B 字母。"""
    block = _banner_block()
    assert "boot_app_slot_base()" in block, (
        "横幅没打印槽基址 —— 只打 'A'/'B' 分辨不出"
        "'给槽 B 种了按槽 A 链接的镜像'（那种镜像会带着槽 A 的基址跑起来）")


# ---------------------------------------------------------------------------
# 3. 没有镜像头（未启用 Bootloader）时也要打印版本，且不能引用镜像头
# ---------------------------------------------------------------------------

def test_projects_without_a_bootloader_still_print_a_version():
    block = _banner_block(with_bootloader=False)
    assert "Firmware v" in block, (
        "未启用 Bootloader 的工程开机不打印版本 —— FR-15.6 要求所有工程都打")
    assert "boot_app_" not in block, (
        "未启用 Bootloader 的工程引用了 boot_app_* —— 那个文件不会被编译，"
        "会直接链接失败")


# ---------------------------------------------------------------------------
# 4. boot_app.c：地址只能来自链接脚本，判据只能来自真源
# ---------------------------------------------------------------------------

def test_boot_app_reads_the_header_at_the_linker_provided_address():
    code = _render_boot_app()

    assert "__hw2c_img_header" in code, (
        "boot_app.c 没用链接脚本导出的 __hw2c_img_header —— 地址一旦在 C 侧"
        "自己算，槽 A/B 与向量表长度任何一个变了就会读到错的地方")
    assert re.search(r"0x080[0-9A-Fa-f]{5}", code) is None, (
        "boot_app.c 里出现了硬编码的 flash 地址 —— 槽基址必须由链接器给出")


def test_boot_app_refuses_a_header_that_was_never_patched():
    """magic 不符、或版本字段仍是擦除态，都必须返回 0xFFFFFFFF。"""
    code = _render_boot_app()
    fn = _function_body(code, "boot_app_image_version")

    assert "APP_HEADER_MAGIC" in fn and "0xFFFFFFFF" in fn, (
        "boot_app_image_version() 没有校验 magic —— 读一个不存在的头会返回"
        "一条指令的机器码，横幅会打印出毫无意义的版本号")
    assert fn.count("0xFFFFFFFF") >= 2, (
        "只挡了 magic 没挡擦除态：fw_version 为 0xFFFFFFFF 时会被 mask 成"
        "0xFFFFFF，横幅打 v255.255.255 而不报错 —— 这正是静默出口")


def test_boot_app_magic_constant_comes_from_the_format_source_of_truth():
    """magic 常量必须与 generator/data/fota_format.json 一致（A3 类缺陷）。"""
    from generator.context.bootloader_context import fota_format_for_templates

    fmt = fota_format_for_templates()
    expected = "0x%08XUL" % fmt["img_magic"]
    assert expected in _render_boot_app(), (
        f"boot_app.c 的 magic 不是真源里的 {expected} —— 与 boot_crc.c 分叉"
        "后，App 会判自己无效、引导器却认这个镜像")


def _function_body(code: str, name: str) -> str:
    m = re.search(r"\b%s\s*\(void\)\s*\{(.*?)\n\}" % re.escape(name),
                  code, flags=re.S)
    assert m, f"boot_app.c 里找不到 {name}() 的定义"
    return m.group(1)


# ---------------------------------------------------------------------------
# 5. 链接脚本必须导出这两个符号（App 侧才有得读）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slot", ["a", "b"])
def test_slot_linker_exports_the_header_and_slot_base_symbols(slot):
    """`PROVIDE` 缺一个，App 侧就是链接错误；更糟的是有人用常量替代。"""
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
        loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
        trim_blocks=True, lstrip_blocks=True)
    text = env.get_template("linker/app_slot_%s.ld.j2" % slot).render(
        boot_config=boot_config,
        fota_fmt=fota_format_for_templates(),
        heap_size="0x200",
        stack_size="0x400")

    assert re.search(r"PROVIDE\s*\(\s*__hw2c_img_header\s*=\s*ADDR\(\.app_header\)\s*\)",
                     text), (
        f"app_slot_{slot}.ld 没有 PROVIDE __hw2c_img_header = ADDR(.app_header) "
        "—— App 侧拿不到镜像头地址，只能回头去硬编码")
    assert re.search(r"PROVIDE\s*\(\s*__hw2c_slot_base\s*=\s*ORIGIN\(FLASH\)\s*\)",
                     text), (
        f"app_slot_{slot}.ld 没有 PROVIDE __hw2c_slot_base = ORIGIN(FLASH) "
        "—— 槽基址只能由链接器给出")
