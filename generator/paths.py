"""
Project path constants.
All hard-coded directory and file paths are defined here so that
other modules reference them via import rather than string literals.
"""

import os

# ---------- Repository root (relative to this file) ----------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------- Source directories ----------
GENERATOR_DIR = os.path.join(_REPO_ROOT, "generator")
STATIC_DIR = os.path.join(_REPO_ROOT, "static")
MODELS_DIR = os.path.join(_REPO_ROOT, "models")
TEMPLATES_DIR = os.path.join(_REPO_ROOT, "templates")
DOCS_DIR = os.path.join(_REPO_ROOT, "docs")
EXAMPLES_DIR = os.path.join(_REPO_ROOT, "examples")
PARSER_DIR = os.path.join(_REPO_ROOT, "parser")

# ---------- Static sub-directories ----------
STATIC_UNITY_DIR = os.path.join(STATIC_DIR, "unity")
STATIC_STM32_DIR = os.path.join(STATIC_DIR, "stm32g0")
STATIC_STM32_HAL_SRC = os.path.join(STATIC_STM32_DIR, "HAL", "Src")

# ---------- Specific files ----------
HIL_RUNNER_PATH = os.path.join(GENERATOR_DIR, "hil_runner.py")
RUN_TESTS_PATH = os.path.join(GENERATOR_DIR, "run_tests.py")
PATCH_CRC_PATH = os.path.join(GENERATOR_DIR, "patch_crc.py")
# FOTA / 引导镜像格式的唯一真源（C 模板与 Python 工具都从这里取值）
FOTA_FORMAT_PATH = os.path.join(GENERATOR_DIR, "data", "fota_format.json")
# YMODEM 传输层格式的唯一真源（设备侧接收器与主机侧发送端都从这里取值）
YMODEM_FORMAT_PATH = os.path.join(GENERATOR_DIR, "data", "ymodem_format.json")
TIMEBASE_SRC = os.path.join(STATIC_STM32_HAL_SRC, "stm32g0xx_hal_timebase_tim.c")

# ---------- Template sub-directories ----------
TEMPLATE_SRC = os.path.join(TEMPLATES_DIR, "src")
TEMPLATE_DRIVERS = os.path.join(TEMPLATES_DIR, "drivers")
TEMPLATE_CONFIG = os.path.join(TEMPLATES_DIR, "config")
TEMPLATE_LINKER = os.path.join(TEMPLATES_DIR, "linker")
TEMPLATE_PROJECT = os.path.join(TEMPLATES_DIR, "project")
TEMPLATE_TEST = os.path.join(TEMPLATES_DIR, "test")
TEMPLATE_APP = os.path.join(TEMPLATES_DIR, "app")
TEMPLATE_BOOTLOADER = os.path.join(TEMPLATES_DIR, "bootloader")
TEMPLATE_VSCODE = os.path.join(TEMPLATES_DIR, "vscode")
TEMPLATE_RTOS = os.path.join(TEMPLATES_DIR, "rtos")
