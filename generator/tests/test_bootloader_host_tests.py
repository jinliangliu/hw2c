"""bootloader 主机测试可用性：让"生成出来的 test/ 编不过"这类缺陷无法再悄悄出现。

背景（真实缺陷，2026-09-16 首次带 bootloader 的示例生成时暴露）
----------------------------------------------------------------
`boot_crc.c` / `boot_jump.c` / `boot_nvm.c` 曾经**无条件** `#include "stm32g0xx.h"`。
而主机单测（`output/<demo>/test/run_tests.py`）是把被测源码 `#include` 进测试文件
并链接 `mock_hal.c` 的 —— `mock_hal.h` 已经用 `typedef int32_t IRQn_Type;` 占掉了
CMSIS 的名字，两者必然冲突：

    error: conflicting types for 'IRQn_Type'
    fatal error: core_cm0plus.h: No such file or directory

结果是：`test_boot_crc` / `test_boot_jump` / `test_boot_nvm` **从来没有编译通过过**，
`run_tests.py` 对任何带 bootloader 的工程都是失败的。这与 A9（mock 让测试失效）
是同一类问题 —— 测试没跑，却没人知道，于是"有测试"变成了虚假的安全感。

真正的根因不是"缺 mock"，而是**漏了一处约定**：本项目所有源码都必须遵守

    #ifdef TEST
    #include "mock_hal.h"     // 寄存器模型（含可复位静态存储）
    #else
    #include "stm32g0xx_hal.h" / "stm32g0xx.h"
    #endif

本文件把这个约定、以及"mock 必须建模 bootloader 真正解引用的每一个寄存器块"
这两件事变成可执行的断言。第二条尤其重要：`#define IWDG ((void *)0)` 曾经让
`IWDG->KR = 0xAAAA` 直接编不过，而这类"少建一个寄存器模型"的疏漏不会有任何
其它信号。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPO_ROOT / "templates"

_BOOTLOADER_SOURCES = ("boot_crc.c.j2", "boot_jump.c.j2", "boot_nvm.c.j2")

# 主机单测环境里由 mock 提供的"伪寄存器"（不是 mock_hal.h 里的指针变量）。
# GPIOx 在 mock_hal.h 里是 `#define GPIOA ((GPIO_TypeDef *)0)` 形式，单独处理。
_MOCK_REGEX = re.compile(r"extern\s+volatile\s+\w+\s*\*\s*([A-Za-z_]\w*)\s*;")
_GPIO_MACRO = re.compile(r"#define\s+(GPIO[A-F])\b")


def _strip_c_comments(text: str) -> str:
    """剥掉 C 注释。

    必要性：注释里出现的 `RCC->CSR` 之类的例子会污染"代码解引用了哪些寄存器"的
    判断，让断言变成误报来源（本项目已因同类问题踩过一次坑）。
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def _mock_register_names() -> set:
    """mock_hal.h.j2 里建模为指针的寄存器块名。"""
    text = (_TEMPLATES / "test" / "mock_hal.h.j2").read_text(encoding="utf-8")
    names = set(_MOCK_REGEX.findall(text))
    names |= set(_GPIO_MACRO.findall(text))
    return names


def _pointer_derefs(text: str) -> set:
    """代码里 `NAME->field` 形式的寄存器块名（已去注释与 Jinja 标签）。"""
    text = re.sub(r"\{[%{].*?[%}]\}", " ", text, flags=re.S)
    return set(re.findall(r"\b([A-Z][A-Z0-9_]*)\s*->", _strip_c_comments(text)))


# ---------------------------------------------------------------------------
# 约定 1：源码必须走 TEST/mock 分支
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", _BOOTLOADER_SOURCES)
def test_bootloader_source_uses_the_test_mock_convention(name):
    """bootloader 源码不允许无条件包含真实 CMSIS 头。"""
    text = (_TEMPLATES / "bootloader" / name).read_text(encoding="utf-8")

    assert '#include "mock_hal.h"' in text, (
        "%s 没有在 TEST 构建里改用 mock_hal.h —— 主机单测会因 CMSIS/mock 的 "
        "IRQn_Type 冲突而编不过" % name
    )

    # stm32g0xx.h 只允许出现在 #ifdef TEST ... #else ... #endif 的 #else 分支里
    branch = re.search(r"#ifdef\s+TEST(?P<test>.*?)#else(?P<target>.*?)#endif",
                       text, flags=re.S)
    assert branch is not None, (
        "%s 里找不到 `#ifdef TEST / #else / #endif` 三段式分支" % name
    )
    assert "stm32g0xx.h" not in branch.group("test"), (
        "%s 在 TEST 分支里包含了真实 CMSIS 头" % name
    )
    assert "stm32g0xx.h" in branch.group("target"), (
        "%s 的目标构建分支没有包含 stm32g0xx.h" % name
    )
    # 整份文件里 stm32g0xx.h 只应出现一次（就在那个 #else 分支里）
    assert text.count('#include "stm32g0xx.h"') == 1, (
        "%s 里 stm32g0xx.h 出现了 %d 次 —— 应只在目标分支出现一次"
        % (name, text.count('#include "stm32g0xx.h"'))
    )


# ---------------------------------------------------------------------------
# 约定 2：mock 必须建模 bootloader 解引用的每一个寄存器块
# ---------------------------------------------------------------------------

def test_mock_models_every_register_block_the_bootloader_dereferences():
    """把"少建一个寄存器模型"变成编译期之前就能发现的失败。

    `#define IWDG ((void *)0)` 是个典型：它能满足 `hiwdg.Instance = IWDG;`，
    却让 bootloader 的 `IWDG->KR = 0xAAAA;` 无法编译 —— 而这一点只有在真的
    去编译 bootloader 的主机测试时才会暴露。
    """
    mock_regs = _mock_register_names()
    missing = {}
    for name in _BOOTLOADER_SOURCES:
        text = (_TEMPLATES / "bootloader" / name).read_text(encoding="utf-8")
        for reg in sorted(_pointer_derefs(text)):
            if reg not in mock_regs:
                missing.setdefault(reg, []).append(name)

    assert not missing, (
        "bootloader 解引用了 mock_hal.h 未建模的寄存器块：%s。"
        "请在 templates/test/mock_hal.h.j2 里补上寄存器结构 + "
        "`extern volatile <X>_TypeDef *<X>;`，并在 mock_hal.c.j2 里给它静态存储、"
        "在 mock_cmsis_reset() 里清零。" % missing
    )


def test_mock_cmsis_reset_clears_every_modelled_register_block():
    """每个建模出来的寄存器块都必须在 mock_cmsis_reset() 里被清零。

    否则用例之间会带着上一条用例写进去的备份寄存器/状态位，产生**顺序相关**
    的偶发失败 —— 那类失败最难查。
    """
    header = (_TEMPLATES / "test" / "mock_hal.h.j2").read_text(encoding="utf-8")
    impl = (_TEMPLATES / "test" / "mock_hal.c.j2").read_text(encoding="utf-8")

    modelled = set(_MOCK_REGEX.findall(header))
    assert modelled, "mock_hal.h.j2 里没有解析到任何寄存器指针声明"

    not_reset = [n for n in sorted(modelled)
                 if ("mock_%s_regs" % n.lower()) not in impl]
    assert not not_reset, (
        "mock_cmsis_reset() 没有清零这些寄存器块（缺 mock_<name>_regs 存储）：%s"
        % not_reset
    )


# ---------------------------------------------------------------------------
# 约定 3：run_tests.py 的 include 路径必须真实存在
# ---------------------------------------------------------------------------

def test_run_tests_static_include_paths_exist():
    """主机测试的 `-I../../../static/...` 路径必须指向真实存在的目录。

    `-ICMSIS/Core/Include` 曾长期写错（vendored 的 CMSIS Core 头直接在
    `CMSIS/Core/` 下）。gcc 对不存在的 `-I` 目录**不报错**，所以这类错误会一直
    潜伏，直到有人真的去 include 那个头文件时才以"xxx.h: No such file"的形式
    冒出来 —— 排查时很容易误判成"依赖没装"。
    """
    text = (_REPO_ROOT / "generator" / "run_tests.py").read_text(encoding="utf-8")
    paths = re.findall(r'"(-I)(\.\./\.\./\.\./static/[^"]+)"', text)
    assert paths, "run_tests.py 里没有解析到 static 下的 include 路径"

    bad = []
    for _flag, rel in paths:
        # output/<demo>/test/ 是工作目录，所以 ../../../ 指向仓库根
        target = _REPO_ROOT / rel.replace("../../../", "")
        if not target.is_dir():
            bad.append(rel)
    assert not bad, "run_tests.py 里的 include 目录不存在：%s" % bad


def test_run_tests_include_dirs_covering_cmsis_core():
    """CMSIS Core 头的真实位置必须被 include 到（回归护栏，见上条注释）。"""
    text = (_REPO_ROOT / "generator" / "run_tests.py").read_text(encoding="utf-8")
    assert "-I../../../static/stm32g0/CMSIS/Core" in text, (
        "run_tests.py 没有 include `static/stm32g0/CMSIS/Core`（注意：该 vendored "
        "目录下没有 Include/ 子目录）"
    )
    core = _REPO_ROOT / "static" / "stm32g0" / "CMSIS" / "Core"
    if core.is_dir():
        assert (core / "core_cm0plus.h").exists(), (
            "CMSIS Core 目录存在但没有 core_cm0plus.h —— vendored 布局变了，"
            "请同步更新 generator/run_tests.py 与本测试"
        )
