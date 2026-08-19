#!/usr/bin/env python3
"""
Hardware2Code Generator
读取硬件 YAML，生成 STM32G0 + FreeRTOS 工程（或 HIL 测试固件）。
"""

import argparse
import difflib
import json
import logging
import os
import subprocess
import sys
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import jinja2
import yaml
from jinja2 import Environment, FileSystemLoader

from .context.builder import build_context
from .validator import validate_hardware
from .jinja_filters import register_filters
from .merger import CSTCodeMerger
from .models import HardwareModel
from .mcu_database import MCUDatabase
from .paths import STATIC_UNITY_DIR, STATIC_STM32_DIR, HIL_RUNNER_PATH, RUN_TESTS_PATH, PATCH_CRC_PATH, TIMEBASE_SRC, TEMPLATES_DIR
from .registry import load_backend, get_default_backend
from .validators.pin_conflict_validator import validate_pin_conflicts
from .allocators.pin_allocator import PinAllocator
from .schemas.hardware import PinConfig

logger = logging.getLogger("hw2c")

# Global merger instance for preserving USER CODE blocks
_code_merger = CSTCodeMerger()

# File extensions that support USER CODE block merging
_MERGE_EXTENSIONS = {".c", ".h"}


def _setup_logging(verbose: bool = False):
    """Configure logging with level based on --verbose flag."""
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    logging.root.handlers.clear()
    logging.root.addHandler(handler)
    logging.root.setLevel(level)
    logger.setLevel(level)


def _write_file(out_path: str, content: str, dry_run: bool, show_diff: bool,
                merge_user_code: bool = True):
    """Write rendered content to file. In dry-run mode, skip writing.
    In diff mode, show unified diff against existing file.
    When merge_user_code is True and target exists, preserves USER CODE blocks.
    """
    rel_path = out_path

    # ---------- USER CODE merging ----------
    ext = os.path.splitext(out_path)[1]
    if merge_user_code and ext in _MERGE_EXTENSIONS:
        try:
            original = content
            content = _code_merger.merge(out_path, content)
            if content != original:
                logger.debug(f"Merged USER CODE blocks in: {rel_path}")
        except Exception as e:
            logger.warning(f"USER CODE merge failed for {rel_path}: {e}")

    if dry_run:
        logger.info(f"[DRY-RUN] Would generate: {rel_path}")
        return

    if show_diff and os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8") as f:
            old_content = f.read()
        if old_content != content:
            diff = difflib.unified_diff(
                old_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{os.path.basename(out_path)}",
                tofile=f"b/{os.path.basename(out_path)}",
            )
            diff_text = "".join(diff)
            if diff_text:
                logger.info(f"Diff for {rel_path}:\n{diff_text}")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    logger.info(f"Generated: {rel_path}")


def load_yaml(file_path: str) -> dict:
    """加载硬件描述 YAML 文件"""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"YAML file not found: '{file_path}'")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parsing error in '{file_path}': {e}")
    except OSError as e:
        raise RuntimeError(f"Error loading YAML file '{file_path}': {e}")


def render_hil_project(env: Environment, context: dict, output_dir: str,
                       dry_run: bool = False, show_diff: bool = False):
    """渲染 HIL 测试固件工程"""
    if not dry_run:
        os.makedirs(os.path.join(output_dir, "src", "drivers"), exist_ok=True)
        os.makedirs(os.path.join(output_dir, "config"), exist_ok=True)
        os.makedirs(os.path.join(output_dir, "linker"), exist_ok=True)

    # 复制 Unity 源文件
    unity_src = STATIC_UNITY_DIR
    if os.path.exists(unity_src):
        if not dry_run:
            shutil.copytree(unity_src, os.path.join(output_dir, "unity"), dirs_exist_ok=True)
        logger.info("Copied Unity to unity/")
    else:
        logger.warning("static/unity not found. HIL tests will not compile.")

    # 生成 HIL 主固件
    hil_main = env.get_template("test/hil_test.c.j2")
    rendered = hil_main.render(context)
    _write_file(os.path.join(output_dir, "src", "main.c"), rendered, dry_run, show_diff)

    # 生成必要的标准文件
    standard = {
        "config/stm32g0xx_hal_conf.h.j2": os.path.join(output_dir, "config", "stm32g0xx_hal_conf.h"),
        "linker/STM32G0B1RETx_FLASH.ld.j2": os.path.join(output_dir, "linker", "STM32G0B1RETx_FLASH.ld"),
        "project/CMakeLists.txt.j2": os.path.join(output_dir, "CMakeLists.txt"),
    }
    for tmpl, out in standard.items():
        template = env.get_template(tmpl)
        rendered = template.render(context)
        _write_file(out, rendered, dry_run, show_diff)

    # Copy static toolchain file
    toolchain_src = os.path.join(TEMPLATES_DIR, "project", "toolchain.cmake")
    if os.path.exists(toolchain_src):
        if not dry_run:
            shutil.copy(toolchain_src, os.path.join(output_dir, "toolchain.cmake"))
        logger.info("Copied toolchain.cmake")

    # 复制 hil_runner.py
    hil_runner_src = HIL_RUNNER_PATH
    if os.path.exists(hil_runner_src):
        if not dry_run:
            shutil.copy(hil_runner_src, os.path.join(output_dir, "hil_runner.py"))
        logger.info("Copied hil_runner.py")
    else:
        logger.warning("generator/hil_runner.py not found")

    logger.info(f"HIL project '{os.path.basename(output_dir)}' generated successfully.")


def render_templates(env: Environment, context: dict, output_dir: str,
                     dry_run: bool = False, show_diff: bool = False):
    """渲染所有模板并写入输出目录，包括测试框架"""
    if not dry_run:
        os.makedirs(os.path.join(output_dir, "src", "drivers"), exist_ok=True)
        os.makedirs(os.path.join(output_dir, "config"), exist_ok=True)
        os.makedirs(os.path.join(output_dir, "linker"), exist_ok=True)
        os.makedirs(os.path.join(output_dir, "test", "unity"), exist_ok=True)

    # ---------- 标准模板 ----------
    standard_templates = {
        "src/main.c.j2": os.path.join(output_dir, "src", "main.c"),
        "src/gpio.c.j2": os.path.join(output_dir, "src", "gpio.c"),
        "src/sleep.c.j2": os.path.join(output_dir, "src", "sleep.c"),
        "src/stm32g0xx_it.c.j2": os.path.join(output_dir, "src", "stm32g0xx_it.c"),
        "config/FreeRTOSConfig.h.j2": os.path.join(output_dir, "config", "FreeRTOSConfig.h"),
        "config/stm32g0xx_hal_conf.h.j2": os.path.join(output_dir, "config", "stm32g0xx_hal_conf.h"),
        "linker/STM32G0B1RETx_FLASH.ld.j2": os.path.join(output_dir, "linker", "STM32G0B1RETx_FLASH.ld"),
        "project/CMakeLists.txt.j2": os.path.join(output_dir, "CMakeLists.txt"),
    }

    for template_name, out_path in standard_templates.items():
        template = env.get_template(template_name)
        rendered = template.render(context)
        _write_file(out_path, rendered, dry_run, show_diff)

    # Copy static toolchain file
    toolchain_src = os.path.join(TEMPLATES_DIR, "project", "toolchain.cmake")
    if os.path.exists(toolchain_src):
        if not dry_run:
            shutil.copy(toolchain_src, os.path.join(output_dir, "toolchain.cmake"))
        logger.info("Copied toolchain.cmake")

    # ---------- 事件管理器 ----------
    event_mgr_templates = {
        "src/event_mgr.h.j2": os.path.join(output_dir, "src", "event_mgr.h"),
        "src/event_mgr.c.j2": os.path.join(output_dir, "src", "event_mgr.c"),
    }
    for tmpl_name, out_path in event_mgr_templates.items():
        template = env.get_template(tmpl_name)
        rendered = template.render(context)
        _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- 日志子系统（全局，非外设驱动） ----------
    if context.get("has_log"):
        log_templates = {
            "drivers/drv_log.h.j2": os.path.join(output_dir, "src", "drivers", "drv_log.h"),
            "drivers/drv_log.c.j2": os.path.join(output_dir, "src", "drivers", "drv_log.c"),
        }
        for tmpl_name, out_path in log_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- 外设驱动 ----------
    drivers = context.get("drivers", [])
    boot_config = context.get("boot_config", {})
    for drv in drivers:
        driver_ctx = dict(context)
        driver_ctx["peripheral"] = drv["peripheral"]
        driver_ctx["model"] = drv["model"]
        driver_ctx["boot_config"] = boot_config

        if drv.get("header_template"):
            template = env.get_template(drv["header_template"])
            rendered = template.render(**driver_ctx)
            out_path = os.path.join(output_dir, "src", "drivers", f"drv_{drv['name']}.h")
            _write_file(out_path, rendered, dry_run, show_diff)

        if drv.get("template"):
            template = env.get_template(drv["template"])
            rendered = template.render(**driver_ctx)
            out_path = os.path.join(output_dir, "src", "drivers", f"drv_{drv['name']}.c")
            _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- POSIX-style driver API (when corresponding peripheral exists) ----------
    if context.get("has_uart"):
        posix_uart_templates = {
            "drivers/posix/uart_api.h.j2": os.path.join(output_dir, "src", "drivers", "uart_api.h"),
            "drivers/posix/uart_api.c.j2": os.path.join(output_dir, "src", "drivers", "uart_api.c"),
        }
        for tmpl_name, out_path in posix_uart_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    if context.get("has_adc"):
        posix_adc_templates = {
            "drivers/posix/adc_api.h.j2": os.path.join(output_dir, "src", "drivers", "adc_api.h"),
            "drivers/posix/adc_api.c.j2": os.path.join(output_dir, "src", "drivers", "adc_api.c"),
        }
        for tmpl_name, out_path in posix_adc_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    if context.get("has_i2c"):
        posix_i2c_templates = {
            "drivers/posix/i2c_api.h.j2": os.path.join(output_dir, "src", "drivers", "i2c_api.h"),
            "drivers/posix/i2c_api.c.j2": os.path.join(output_dir, "src", "drivers", "i2c_api.c"),
        }
        for tmpl_name, out_path in posix_i2c_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    if context.get("has_spi"):
        posix_spi_templates = {
            "drivers/posix/spi_api.h.j2": os.path.join(output_dir, "src", "drivers", "spi_api.h"),
            "drivers/posix/spi_api.c.j2": os.path.join(output_dir, "src", "drivers", "spi_api.c"),
        }
        for tmpl_name, out_path in posix_spi_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- Attitude estimation module (when an IMU is present) ----------
    if context.get("has_mpu6050"):
        attitude_templates = {
            "app/attitude.h.j2": os.path.join(output_dir, "src", "attitude.h"),
            "app/attitude.c.j2": os.path.join(output_dir, "src", "attitude.c"),
        }
        for tmpl_name, out_path in attitude_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- Fall detection module (when a fall_detect component exists) ----------
    if context.get("has_fall_detect"):
        fall_templates = {
            "app/fall_detect.h.j2": os.path.join(output_dir, "src", "fall_detect.h"),
            "app/fall_detect.c.j2": os.path.join(output_dir, "src", "fall_detect.c"),
        }
        for tmpl_name, out_path in fall_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- FOC math module (when a FOC motor peripheral exists) ----------
    if context.get("has_foc"):
        foc_templates = {
            "app/foc_math.h.j2": os.path.join(output_dir, "src", "foc_math.h"),
            "app/foc_math.c.j2": os.path.join(output_dir, "src", "foc_math.c"),
        }
        for tmpl_name, out_path in foc_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- PID math module (when a pid_ctrl component exists) ----------
    if context.get("has_pid_ctrl"):
        pid_templates = {
            "app/pid_math.h.j2": os.path.join(output_dir, "src", "pid_math.h"),
            "app/pid_math.c.j2": os.path.join(output_dir, "src", "pid_math.c"),
            "app/pid_autotune.h.j2": os.path.join(output_dir, "src", "pid_autotune.h"),
            "app/pid_autotune.c.j2": os.path.join(output_dir, "src", "pid_autotune.c"),
        }
        for tmpl_name, out_path in pid_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    # GPIO POSIX API always generated (all boards have GPIO)
    posix_gpio_templates = {
        "drivers/posix/gpio_api.h.j2": os.path.join(output_dir, "src", "drivers", "gpio_api.h"),
        "drivers/posix/gpio_api.c.j2": os.path.join(output_dir, "src", "drivers", "gpio_api.c"),
    }
    for tmpl_name, out_path in posix_gpio_templates.items():
        template = env.get_template(tmpl_name)
        rendered = template.render(context)
        _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- Component Registry ----------
    if context.get("has_components"):
        registry_templates = {
            "src/component_registry.h.j2": os.path.join(output_dir, "src", "component_registry.h"),
            "src/component_registry.c.j2": os.path.join(output_dir, "src", "component_registry.c"),
        }
        for tmpl_name, out_path in registry_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

        # ---------- Pub/Sub message bus ----------
        if context.get("has_topics"):
            bus_templates = {
                "src/component_bus.h.j2": os.path.join(output_dir, "src", "component_bus.h"),
                "src/component_bus.c.j2": os.path.join(output_dir, "src", "component_bus.c"),
            }
            for tmpl_name, out_path in bus_templates.items():
                template = env.get_template(tmpl_name)
                rendered = template.render(context)
                _write_file(out_path, rendered, dry_run, show_diff)

        # ---------- Param Registry ----------
        if context.get("has_params"):
            param_templates = {
                "src/param_registry.h.j2": os.path.join(output_dir, "src", "param_registry.h"),
                "src/param_registry.c.j2": os.path.join(output_dir, "src", "param_registry.c"),
            }
            for tmpl_name, out_path in param_templates.items():
                template = env.get_template(tmpl_name)
                rendered = template.render(context)
                _write_file(out_path, rendered, dry_run, show_diff)

        # ---------- Component scaffold .c files ----------
        periph_type_map = {}
        for p in context.get("peripherals", []):
            periph_type_map[p.get("name", "").lower()] = p.get("type", "")

        for comp in context.get("components", []):
            driver_name = comp.get("driver", "").lower()
            ptype = periph_type_map.get(driver_name, "")
            if "UART" in ptype or "uart" in driver_name:
                driver_type = "uart"
            elif "GPIO" in ptype:
                driver_type = "gpio"
            elif "ADC" in ptype:
                driver_type = "adc"
            elif "I2C" in ptype or "i2c" in driver_name:
                driver_type = "i2c"
            elif "SPI" in ptype or "spi" in driver_name:
                driver_type = "spi"
            else:
                driver_type = "unknown"

            comp_ctx = dict(context)
            comp_ctx["comp_name"] = comp["name"]
            comp_ctx["comp_type"] = comp.get("type", "unknown")
            comp_ctx["driver"] = comp.get("driver", "")
            comp_ctx["driver_type"] = driver_type
            comp_ctx["period_ms"] = comp.get("period_ms", 100)
            comp_ctx["description"] = comp.get("config", {}).get("description", "")
            comp_ctx["comp_config"] = comp.get("config", {})

            # Use specialized template for led/btn components
            comp_type = comp.get("type", "")
            if comp_type == "led":
                comp_template = env.get_template("app/led_component.c.j2")
                comp_name_out = "led_component"
            elif comp_type == "btn":
                comp_template = env.get_template("app/btn_component.c.j2")
                comp_name_out = "btn_component"
            elif comp_type == "modbus":
                comp_template = env.get_template("app/modbus_component.c.j2")
                comp_name_out = comp["name"] + "_component"
            elif comp_type == "mpu6050":
                comp_template = env.get_template("app/mpu6050_component.c.j2")
                comp_name_out = comp["name"] + "_component"
            elif comp_type == "fall_detect":
                comp_template = env.get_template("app/fall_detect_component.c.j2")
                comp_name_out = comp["name"] + "_component"
            elif comp_type == "foc_motor":
                comp_template = env.get_template("app/foc_motor_component.c.j2")
                comp_name_out = comp["name"] + "_component"
            elif comp_type == "knob":
                comp_template = env.get_template("app/knob_component.c.j2")
                comp_name_out = comp["name"] + "_component"
            elif comp_type == "pid_ctrl":
                comp_template = env.get_template("app/pid_ctrl_component.c.j2")
                comp_name_out = comp["name"] + "_component"
            else:
                comp_template = env.get_template("app/component.c.j2")
                comp_name_out = comp["name"] + "_component"
            comp_rendered = comp_template.render(comp_ctx)
            comp_out = os.path.join(output_dir, "src", f"{comp_name_out}.c")
            _write_file(comp_out, comp_rendered, dry_run, show_diff)

    # ---------- Telemetry ----------
    if context.get("has_telemetry"):
        telemetry_templates = {
            "src/telemetry.h.j2": os.path.join(output_dir, "src", "telemetry.h"),
            "src/telemetry.c.j2": os.path.join(output_dir, "src", "telemetry.c"),
        }
        for tmpl_name, out_path in telemetry_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- Power Manager ----------
    if context.get("has_power_mgr"):
        power_mgr_templates = {
            "src/power_mgr.h.j2": os.path.join(output_dir, "src", "power_mgr.h"),
            "src/power_mgr.c.j2": os.path.join(output_dir, "src", "power_mgr.c"),
        }
        for tmpl_name, out_path in power_mgr_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- LED Component ----------
    if context.get("has_led_component"):
        led_comp_templates = {
            "src/led_component.h.j2": os.path.join(output_dir, "src", "led_component.h"),
        }
        for tmpl_name, out_path in led_comp_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- Button Component ----------
    if context.get("has_btn_component"):
        btn_comp_templates = {
            "src/btn_component.h.j2": os.path.join(output_dir, "src", "btn_component.h"),
        }
        for tmpl_name, out_path in btn_comp_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- Modbus Component ----------
    if context.get("has_modbus_component"):
        mb_comp_templates = {
            "src/modbus_component.h.j2": os.path.join(output_dir, "src", "modbus_component.h"),
        }
        for tmpl_name, out_path in mb_comp_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- 业务状态机 ----------
    if context.get("has_behavior"):
        state_machine_templates = {
            "app/statemachine.h.j2": os.path.join(output_dir, "src", "statemachine.h"),
            "app/statemachine.c.j2": os.path.join(output_dir, "src", "statemachine.c"),
        }
        for tmpl_name, out_path in state_machine_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- 测试框架 ----------
    test_dir = os.path.join(output_dir, "test")
    if not dry_run:
        os.makedirs(test_dir, exist_ok=True)

    # 复制 Unity
    unity_src = STATIC_UNITY_DIR
    if os.path.exists(unity_src):
        if not dry_run:
            shutil.copytree(unity_src, os.path.join(test_dir, "unity"), dirs_exist_ok=True)
        logger.info("Copied Unity framework to test/unity/")
    else:
        logger.warning("static/unity not found. Tests will not compile without Unity.")

    # Mock HAL
    mock_templates = {
        "test/mock_hal.h.j2": os.path.join(test_dir, "mock_hal.h"),
        "test/mock_hal.c.j2": os.path.join(test_dir, "mock_hal.c"),
    }
    for tmpl_name, out_path in mock_templates.items():
        template = env.get_template(tmpl_name)
        rendered = template.render(context)
        _write_file(out_path, rendered, dry_run, show_diff)

    # 测试用例模板（根据外设动态生成）
    test_templates = {
        "test/test_gpio.c.j2": os.path.join(test_dir, "test_gpio.c"),
    }

    if context.get("has_rtc"):
        test_templates["test/test_rtc.c.j2"] = os.path.join(test_dir, "test_rtc.c")
        test_templates["test/test_event_mgr.c.j2"] = os.path.join(test_dir, "test_event_mgr.c")
        test_templates["test/test_rtc_timers.c.j2"] = os.path.join(test_dir, "test_rtc_timers.c")

    for p in context.get("peripherals", []):
        if p.get("type") in ("I2C_Sensor_MPU6050", "SPI_Sensor_MPU6500"):
            test_templates["test/test_mpu6050.c.j2"] = os.path.join(test_dir, "test_mpu6050.c")
            break

    for p in context.get("peripherals", []):
        if p.get("type") == "I2C_EEPROM":
            test_templates["test/test_eeprom.c.j2"] = os.path.join(test_dir, "test_eeprom.c")
            break

    if context.get("has_spi_flash"):
        test_templates["test/test_spi_flash.c.j2"] = os.path.join(test_dir, "test_spi_flash.c")

    if context.get("has_pwm"):
        test_templates["test/test_pwm.c.j2"] = os.path.join(test_dir, "test_pwm.c")

    # test_adc exercises the standalone ADC driver (drv_adc), which is only
    # generated for an explicit Internal_ADC peripheral.  has_adc also covers
    # Internal_TempSensor (which reuses the ADC core but not drv_adc), so it
    # must not be used as the gate here.
    if any(p.get("type") == "Internal_ADC"
           for p in context.get("peripherals", [])):
        test_templates["test/test_adc.c.j2"] = os.path.join(test_dir, "test_adc.c")

    if context.get("has_uart"):
        test_templates["test/test_uart.c.j2"] = os.path.join(test_dir, "test_uart.c")
    if context.get("has_i2c"):
        test_templates["test/test_i2c_api.c.j2"] = os.path.join(test_dir, "test_i2c_api.c")
    if context.get("has_spi"):
        test_templates["test/test_spi_api.c.j2"] = os.path.join(test_dir, "test_spi_api.c")
    if context.get("has_mpu6050"):
        test_templates["test/test_attitude.c.j2"] = os.path.join(test_dir, "test_attitude.c")
    if context.get("has_fall_detect"):
        test_templates["test/test_fall_detect.c.j2"] = os.path.join(test_dir, "test_fall_detect.c")
    if context.get("has_foc"):
        test_templates["test/test_foc_math.c.j2"] = os.path.join(test_dir, "test_foc_math.c")
    if context.get("has_pid_ctrl"):
        test_templates["test/test_pid_math.c.j2"] = os.path.join(test_dir, "test_pid_math.c")
        test_templates["test/test_pid_autotune.c.j2"] = os.path.join(test_dir, "test_pid_autotune.c")
    if context.get("has_ir"):
        test_templates["test/test_ir.c.j2"] = os.path.join(test_dir, "test_ir.c")
    if context.get("has_cellular"):
        test_templates["test/test_cellular.c.j2"] = os.path.join(test_dir, "test_cellular.c")
    if context.get("has_rs485"):
        test_templates["test/test_rs485.c.j2"] = os.path.join(test_dir, "test_rs485.c")
    if context.get("has_modbus"):
        test_templates["test/test_modbus.c.j2"] = os.path.join(test_dir, "test_modbus.c")
    if context.get("has_mqtt"):
        test_templates["test/test_mqtt.c.j2"] = os.path.join(test_dir, "test_mqtt.c")
    if context.get("has_cli"):
        test_templates["test/test_cli.c.j2"] = os.path.join(test_dir, "test_cli.c")
    if context.get("has_substate"):
        test_templates["test/test_substate.c.j2"] = os.path.join(test_dir, "test_substate.c")

    # Bootloader unit tests
    if context.get("has_bootloader"):
        test_templates["test/test_boot_crc.c.j2"] = os.path.join(test_dir, "test_boot_crc.c")
        test_templates["test/test_boot_nvm.c.j2"] = os.path.join(test_dir, "test_boot_nvm.c")
        test_templates["test/test_boot_jump.c.j2"] = os.path.join(test_dir, "test_boot_jump.c")

    # FOTA unit tests
    if context.get("has_fota"):
        test_templates["test/test_fota_protocol.c.j2"] = os.path.join(test_dir, "test_fota_protocol.c")
        test_templates["test/test_fota_bspatch.c.j2"] = os.path.join(test_dir, "test_fota_bspatch.c")

    # 状态机测试
    if context.get("has_behavior"):
        if context.get("behavior", {}).get("regions") is not None:
            test_templates["test/test_parallel.c.j2"] = os.path.join(test_dir, "test_parallel.c")
        else:
            test_templates["test/test_statemachine.c.j2"] = os.path.join(test_dir, "test_statemachine.c")

    if context.get("has_led_component"):
        test_templates["test/test_led.c.j2"] = os.path.join(test_dir, "test_led.c")

    if context.get("has_btn_component"):
        test_templates["test/test_btn.c.j2"] = os.path.join(test_dir, "test_btn.c")

    for tmpl_name, out_path in test_templates.items():
        template = env.get_template(tmpl_name)
        rendered = template.render(context)
        _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- SIL test framework (host/x86 build) ----------
    if context.get("has_components") or context.get("has_topics") or context.get("has_params"):
        sil_dir = os.path.join(test_dir, "sil")
        if not dry_run:
            os.makedirs(sil_dir, exist_ok=True)

        # Generate posix_mock (mock POSIX driver interfaces)
        posix_mock_templates = {
            "test/posix_mock.h.j2": os.path.join(sil_dir, "posix_mock.h"),
            "test/posix_mock.c.j2": os.path.join(sil_dir, "posix_mock.c"),
        }
        for tmpl_name, out_path in posix_mock_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

        # Generate SIL component test
        sil_test_out = os.path.join(sil_dir, "test_component_sil.c")
        template = env.get_template("test/test_component_sil.c.j2")
        rendered = template.render(context)
        _write_file(sil_test_out, rendered, dry_run, show_diff)

        # Copy Unity to SIL dir
        if os.path.exists(unity_src):
            if not dry_run:
                shutil.copytree(unity_src, os.path.join(sil_dir, "unity"), dirs_exist_ok=True)
            logger.info("Copied Unity framework to test/sil/unity/")

        # Generate SIL CMakeLists
        sil_cmake_out = os.path.join(sil_dir, "CMakeLists.txt")
        template = env.get_template("test/CMakeLists_sil.txt.j2")
        rendered = template.render(context)
        _write_file(sil_cmake_out, rendered, dry_run, show_diff)

    # 复制 run_tests.py
    run_tests_script = RUN_TESTS_PATH
    if os.path.exists(run_tests_script):
        if not dry_run:
            shutil.copy(run_tests_script, os.path.join(test_dir, "run_tests.py"))
        logger.info("Copied run_tests.py to test/")
    else:
        logger.warning("run_tests.py not found in generator/. Tests will not be executable via cmake --build build --target test.")

    # ---------- .vscode 编辑器配置 ----------
    vscode_dir = os.path.join(output_dir, ".vscode")
    if not dry_run:
        os.makedirs(vscode_dir, exist_ok=True)

    vscode_templates = {
        "vscode/settings.json.j2": ".vscode/settings.json",
        "vscode/tasks.json.j2": ".vscode/tasks.json",
        "vscode/launch.json.j2": ".vscode/launch.json",
        "vscode/c_cpp_properties.json.j2": ".vscode/c_cpp_properties.json",
        "vscode/extensions.json.j2": ".vscode/extensions.json",
    }
    for tmpl_name, rel_out in vscode_templates.items():
        template = env.get_template(tmpl_name)
        rendered = template.render(context)
        out_path = os.path.join(output_dir, rel_out)
        _write_file(out_path, rendered, dry_run, show_diff)

    # ---------- 复制 HAL Timebase 文件到工程 ----------
    # ALWAYS needed: HAL_InitTick() uses TIM14 unconditionally in main.c
    timebase_src = TIMEBASE_SRC
    timebase_dst = os.path.join(output_dir, "src", "stm32g0xx_hal_timebase_tim.c")
    if os.path.exists(timebase_src):
        if not dry_run:
            shutil.copy2(timebase_src, timebase_dst)
        logger.info("Copied stm32g0xx_hal_timebase_tim.c to src/")
    else:
        logger.warning(f"{timebase_src} not found")

    # ---------- Bootloader ----------
    if context.get("has_bootloader"):
        boot_dir = os.path.join(output_dir, "bootloader")
        if not dry_run:
            os.makedirs(boot_dir, exist_ok=True)

        # Bootloader 链接脚本
        boot_ld = env.get_template("linker/bootloader.ld.j2")
        rendered = boot_ld.render(context)
        _write_file(os.path.join(output_dir, "linker", "bootloader.ld"), rendered, dry_run, show_diff)

        # App Slot A 链接脚本
        slot_a_ld = env.get_template("linker/app_slot_a.ld.j2")
        rendered = slot_a_ld.render(context)
        _write_file(os.path.join(output_dir, "linker", "app_slot_a.ld"), rendered, dry_run, show_diff)

        # App Slot B 链接脚本
        slot_b_ld = env.get_template("linker/app_slot_b.ld.j2")
        rendered = slot_b_ld.render(context)
        _write_file(os.path.join(output_dir, "linker", "app_slot_b.ld"), rendered, dry_run, show_diff)

        # Bootloader 核心源文件
        boot_templates = {
            "bootloader/boot_main.c.j2": "main.c",
            "bootloader/boot_nvm.c.j2": "boot_nvm.c",
            "bootloader/boot_nvm.h.j2": "boot_nvm.h",
            "bootloader/boot_crc.c.j2": "boot_crc.c",
            "bootloader/boot_crc.h.j2": "boot_crc.h",
            "bootloader/boot_jump.c.j2": "boot_jump.c",
            "bootloader/boot_jump.h.j2": "boot_jump.h",
        }
        for tmpl_name, fname in boot_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            out_path = os.path.join(boot_dir, fname)
            _write_file(out_path, rendered, dry_run, show_diff)

        # Bootloader CMakeLists
        boot_cmake = env.get_template("project/bootloader_CMakeLists.txt.j2")
        rendered = boot_cmake.render(context)
        _write_file(os.path.join(boot_dir, "CMakeLists.txt"), rendered, dry_run, show_diff)

        # Copy toolchain to bootloader dir too (it references ../linker/bootloader.ld)
        toolchain_src = os.path.join(TEMPLATES_DIR, "project", "toolchain.cmake")
        if os.path.exists(toolchain_src) and not dry_run:
            shutil.copy(toolchain_src, os.path.join(boot_dir, "toolchain.cmake"))

        # App 端启动标记文件
        boot_app_templates = {
            "bootloader/boot_app.h.j2": os.path.join(output_dir, "src", "boot_app.h"),
            "bootloader/boot_app.c.j2": os.path.join(output_dir, "src", "boot_app.c"),
        }
        for tmpl_name, out_path in boot_app_templates.items():
            template = env.get_template(tmpl_name)
            rendered = template.render(context)
            _write_file(out_path, rendered, dry_run, show_diff)

        # 复制 CRC 后处理脚本
        crc_script = PATCH_CRC_PATH
        if os.path.exists(crc_script):
            if not dry_run:
                shutil.copy(crc_script, os.path.join(output_dir, "patch_crc.py"))
            logger.info("Copied patch_crc.py")


def _run_compile_check(staging_dir: str, verbose: bool = False) -> tuple[bool, str]:
    """Run cmake --build in staging directory to verify generated code compiles.

    Returns:
        (success: bool, output: str) - combined stdout+stderr from cmake.
    """
    logger.info("Running compile check: cmake configure + build ...")
    try:
        # Step 1: Configure
        build_dir = os.path.join(staging_dir, "build")
        result_cfg = subprocess.run(
            ["cmake", "-B", build_dir,
             "-DCMAKE_TOOLCHAIN_FILE=toolchain.cmake",
             "-G", "Ninja"],
            cwd=staging_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result_cfg.returncode != 0:
            output = result_cfg.stdout + result_cfg.stderr
            for line in output.splitlines():
                logger.error(f"[cmake configure] {line}")
            return False, output

        # Step 2: Build
        result_build = subprocess.run(
            ["cmake", "--build", build_dir, "-j"],
            cwd=staging_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result_build.stdout + result_build.stderr
        success = result_build.returncode == 0

        if verbose or not success:
            for line in output.splitlines():
                if verbose:
                    logger.debug(f"[cmake build] {line}")
                elif not success:
                    logger.error(f"[cmake build] {line}")

        if success:
            logger.info("Compile check PASSED")
        else:
            logger.error(f"Compile check FAILED (exit code {result_build.returncode})")

        return success, output
    except FileNotFoundError:
        logger.warning("Skipping compile check: 'cmake' or 'ninja' not found in PATH")
        return True, ""  # skip check if cmake not available
    except subprocess.TimeoutExpired:
        logger.error("Compile check timed out after 120s")
        return False, "TIMEOUT"
    except Exception as e:
        logger.error(f"Compile check error: {e}")
        return False, str(e)


def _write_generation_log(staging_dir: str, context: dict, yaml_path: str,
                          generated_files: list[str]) -> None:
    """Write generation.log into the staging directory for debugging."""
    log_path = os.path.join(staging_dir, "generation.log")
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "yaml_input": yaml_path,
        "context_keys": sorted(context.keys()),
        "generated_files": sorted(generated_files),
    }
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        logger.debug(f"Generation log written to {log_path}")
    except OSError as e:
        logger.warning(f"Failed to write generation.log: {e}")


def _collect_staging_files(staging_dir: str) -> list[str]:
    """Walk staging directory and return relative paths of all generated files."""
    files = []
    for root, _, filenames in os.walk(staging_dir):
        for fn in filenames:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, staging_dir)
            files.append(rel)
    return files


def _atomic_commit(staging_dir: str, target_dir: str) -> None:
    """Move staging directory content to target directory atomically.

    Creates a .bak backup of the existing target before overwriting.
    """
    if os.path.exists(target_dir):
        backup_dir = target_dir + ".bak"
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)
        shutil.move(target_dir, backup_dir)
        logger.info(f"Backed up existing to {backup_dir}")

    shutil.copytree(staging_dir, target_dir, dirs_exist_ok=True)
    logger.info(f"Atomic commit: {staging_dir} -> {target_dir}")


def generate_project(
    yaml_path: str,
    output_dir: str,
    hil_mode: bool = False,
    dry_run: bool = False,
    show_diff: bool = False,
    force: bool = False,
    target: str = "stm32",
    validate_pins: bool = True,
    allocate_pins: bool = True,
    pin_db: Optional[str] = None,
    task_yaml_path: Optional[str] = None,
    bind_yaml_path: Optional[str] = None,
    components_yaml_path: Optional[str] = None,
    pubsub_yaml_path: Optional[str] = None,
    params_yaml_path: Optional[str] = None,
    validate_fn: Callable[[dict], list] = validate_hardware,
    build_context_fn: Callable[[dict, str, bool], dict] = build_context,
    load_yaml_fn: Callable[[str], dict] = load_yaml,
) -> None:
    banner = "=" * 60
    logger.info(f"\n{banner}")
    logger.info("Hardware2Code Generator v2.0")
    logger.info(banner)
    logger.info(f"Input file:  {yaml_path}")
    logger.info(f"Output dir:  {output_dir}")
    logger.info(f"Target MCU:  {target}")
    logger.info(f"HIL mode:    {'Yes' if hil_mode else 'No'}")
    logger.info(f"Dry run:     {'Yes' if dry_run else 'No'}")
    if show_diff:
        logger.info(f"Diff mode:   Yes")
    if force:
        logger.info(f"Force mode:  Yes (skip compile check)")
    logger.info(f"{banner}")

    # ---------- Load target backend ----------
    try:
        backend = load_backend(target)
    except ValueError as e:
        logger.critical(str(e))
        sys.exit(1)

    logger.info(f"Backend:     {backend.get_mcu_family()} ({target})")

    # ---------- Atomic write: generate to temp dir first ----------
    actual_output = output_dir
    tmp_build_dir: Optional[str] = None

    if not dry_run and not show_diff:
        tmp_build_dir = tempfile.mkdtemp(prefix="hw2c_build_")
        output_dir = tmp_build_dir
        logger.info(f"Atomic mode: building to {tmp_build_dir}")
    elif dry_run:
        output_dir = os.path.join(tempfile.gettempdir(), "hw2c_dryrun", os.path.basename(output_dir))
        logger.info(f"Dry-run output: {output_dir}")

    try:
        # ---------- YAML loading and merging ----------
        hw_raw = load_yaml_fn(yaml_path)
        logger.info("[OK] Hardware YAML loaded successfully")

        # Load optional task and bind YAMLs
        task_raw = None
        bind_raw = None
        if task_yaml_path:
            try:
                task_raw = load_yaml_fn(task_yaml_path)
                logger.info("[OK] Task YAML loaded: %s", task_yaml_path)
            except (FileNotFoundError, ValueError) as e:
                logger.warning("Task YAML not found or parse error: %s", e)

        if bind_yaml_path:
            try:
                bind_raw = load_yaml_fn(bind_yaml_path)
                logger.info("[OK] Bind YAML loaded: %s", bind_yaml_path)
            except (FileNotFoundError, ValueError) as e:
                logger.warning("Bind YAML not found or parse error: %s", e)

        components_raw = None
        if components_yaml_path:
            try:
                components_raw = load_yaml_fn(components_yaml_path)
                logger.info("[OK] Components YAML loaded: %s", components_yaml_path)
            except (FileNotFoundError, ValueError) as e:
                logger.warning("Components YAML not found or parse error: %s", e)

        pubsub_raw = None
        if pubsub_yaml_path:
            try:
                pubsub_raw = load_yaml_fn(pubsub_yaml_path)
                logger.info("[OK] Pub/Sub YAML loaded: %s", pubsub_yaml_path)
            except (FileNotFoundError, ValueError) as e:
                logger.warning("Pub/Sub YAML not found or parse error: %s", e)

        params_raw = None
        if params_yaml_path:
            try:
                params_raw = load_yaml_fn(params_yaml_path)
                logger.info("[OK] Params YAML loaded: %s", params_yaml_path)
            except (FileNotFoundError, ValueError) as e:
                logger.warning("Params YAML not found or parse error: %s", e)

        # Merge via mapper (handles backward compat)
        from generator.mapper import merge as merge_yamls
        merged = merge_yamls(
            yaml.dump(hw_raw, default_flow_style=False, sort_keys=False),
            yaml.dump(task_raw, default_flow_style=False, sort_keys=False) if task_raw else "",
            yaml.dump(bind_raw, default_flow_style=False, sort_keys=False) if bind_raw else "",
        )
        logger.info("[OK] Three-layer merge complete")

        # Cross-layer validation: bind → hardware/task reference integrity
        if bind_raw:
            from generator.validator import validate_bind_cross_refs
            cross_errors = validate_bind_cross_refs(hw_raw, task_raw, bind_raw, components_raw)
            if cross_errors:
                _report_validation_errors(cross_errors)
                sys.exit(1)
            else:
                logger.info("[OK] Bind cross-reference validation passed")

        # Pydantic model validation
        hw_model = HardwareModel.model_validate(hw_raw)
        hw = hw_model.model_dump(exclude_none=True)
        logger.info("[OK] Pydantic model validation passed")

        # Inject merged software fields into hw (needed by business
        # validation AND context building — behavior actions must be
        # validated at generation time, not at runtime).
        if "app_tasks" in merged:
            hw["app_tasks"] = merged["app_tasks"]
        if "behavior" in merged:
            hw["behavior"] = merged["behavior"]
        if "periodic_events" in merged:
            hw["periodic_events"] = merged["periodic_events"]
        if "bind_routings" in merged:
            hw["bind_routings"] = merged["bind_routings"]
        if pubsub_raw and "topics" in pubsub_raw:
            hw["topics"] = pubsub_raw["topics"]

        # Cross-field business logic validation
        errors = validate_fn(hw)
        if errors:
            _report_validation_errors(errors)
            sys.exit(1)
        else:
            logger.info("[OK] Hardware validation passed")

        # ---------- MCU database loading ----------
        mcu_db = None
        if hasattr(hw_model, 'mcu') and hw_model.mcu:
            try:
                if pin_db:
                    mcu_db = MCUDatabase.from_mcu_name(hw_model.mcu.part, data_dir=pin_db)
                else:
                    mcu_db = MCUDatabase.from_mcu_name(hw_model.mcu.part)
            except FileNotFoundError:
                logger.warning(
                    "MCU database not found for %s. Pin validation/allocation skipped. "
                    "Use --pin-db to specify a custom database path.",
                    hw_model.mcu.part,
                )

        # ---------- MCU pin conflict validation ----------
        if validate_pins and mcu_db is not None:
            pin_errors = validate_pin_conflicts(hw_model, mcu_db)
            if pin_errors:
                logger.warning("")
                for err in pin_errors:
                    logger.warning("  %s", err)
                logger.error(
                    "Pin validation found %d error(s). "
                    "Use --no-validate-pins to bypass.", len(pin_errors)
                )
                sys.exit(1)
            else:
                logger.info("[OK] Pin conflict validation passed")
        elif not validate_pins:
            logger.info("Pin validation skipped (--no-validate-pins)")

        # ---------- MCU pin auto-allocation ----------
        if allocate_pins and mcu_db is not None:
            allocator = PinAllocator(mcu_db)
            allocated = allocator.allocate_all(hw_model)
            if allocated:
                for pin_dict in allocated:
                    hw_model.pins.append(PinConfig(**pin_dict))
                # Re-dump hw dict with newly allocated pins
                hw = hw_model.model_dump(exclude_none=True)
                logger.info("[OK] Pin auto-allocation assigned %d pin(s)", len(allocated))
            else:
                logger.info("Pin auto-allocation: nothing to allocate")
        elif not allocate_pins:
            logger.info("Pin allocation skipped (--no-allocate-pins)")

        # Context building
        project_name = os.path.basename(actual_output) or "hw2code"
        context = build_context_fn(hw, project_name, hil_mode)

        # Inject component data into context for registry generation
        if components_raw:
            context["components"] = components_raw.get("components", [])
        else:
            context["components"] = []
        context["has_components"] = bool(context.get("components"))
        context["has_foc"] = any(
            p.get("type") == "FOC_Motor"
            for p in context.get("peripherals", [])
        )
        context["has_fall_detect"] = any(
            c.get("type") == "fall_detect" for c in context.get("components", [])
        )
        context["has_led_component"] = any(
            c.get("type") == "led" for c in context.get("components", [])
        )
        context["has_btn_component"] = any(
            c.get("type") == "btn" for c in context.get("components", [])
        )
        context["has_modbus_component"] = any(
            c.get("type") == "modbus" for c in context.get("components", [])
        )
        context["has_pid_ctrl"] = any(
            c.get("type") == "pid_ctrl" for c in context.get("components", [])
        )

        # Pre-compute LED/BTN pin lists for component templates
        all_pins = context.get("pins", [])
        context["led_pins"] = [p for p in all_pins
                               if p.get("label", "").startswith("LED")]
        context["btn_pins"] = [p for p in all_pins
                               if "BUTTON" in p.get("label", "")
                               or "BTN" in p.get("label", "")]

        # Inject pubsub data into context for bus generation
        if pubsub_raw:
            context["topics"] = pubsub_raw.get("topics", [])
        else:
            context["topics"] = []
        context["has_topics"] = bool(context.get("topics"))

        # Inject params data into context for param registry generation
        if params_raw:
            context["params"] = params_raw.get("params", [])
        else:
            context["params"] = []
        context["has_params"] = bool(context.get("params"))

        logger.info("[OK] Context built successfully")

        # Template environment - use backend's template dirs for multi-level override
        template_dirs = backend.get_template_dirs()
        logger.debug(f"Template search paths: {template_dirs}")
        env = Environment(
            loader=FileSystemLoader(template_dirs, encoding='utf-8'),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        register_filters(env)
        logger.info("[OK] Template environment initialized")

        # Rendering
        if hil_mode:
            render_hil_project(env, context, output_dir, dry_run, show_diff)
        else:
            render_templates(env, context, output_dir, dry_run, show_diff)

        # ---------- Write generation.log to staging ----------
        if tmp_build_dir and not dry_run:
            generated_files = _collect_staging_files(tmp_build_dir)
            _write_generation_log(tmp_build_dir, context, yaml_path, generated_files)

        # ---------- Compile check ----------
        if tmp_build_dir and not dry_run and not force:
            compile_ok, compile_output = _run_compile_check(
                tmp_build_dir, verbose=logger.isEnabledFor(logging.DEBUG)
            )
            if not compile_ok:
                logger.critical("Compile check failed. Staging directory preserved for inspection.")
                logger.critical(f"Temp dir: {tmp_build_dir}")
                # Do NOT cleanup temp dir on compile failure - preserve for debugging
                sys.exit(1)

        # ---------- Print dry-run file tree ----------
        if dry_run and not show_diff:
            staging_files = _collect_staging_files(output_dir)
            logger.info(f"\nGenerated file tree ({len(staging_files)} files):")
            for f in sorted(staging_files):
                logger.info(f"  {f}")

        # ---------- Atomic commit: copy temp to target ----------
        if tmp_build_dir:
            logger.info("Committing atomic build...")
            _atomic_commit(tmp_build_dir, actual_output)

    except FileNotFoundError as e:
        logger.critical(str(e))
        _cleanup_temp(tmp_build_dir)
        sys.exit(1)
    except ValueError as e:
        logger.critical(str(e))
        _cleanup_temp(tmp_build_dir)
        sys.exit(1)
    except (yaml.YAMLError, OSError, ValueError) as e:
        logger.error(str(e))
        _cleanup_temp(tmp_build_dir)
        sys.exit(1)
    except jinja2.TemplateNotFound as e:
        logger.error(f"Template file not found: {e}")
        _cleanup_temp(tmp_build_dir)
        sys.exit(1)
    except jinja2.TemplateError as e:
        logger.error(f"Jinja2 template error: {e}")
        _cleanup_temp(tmp_build_dir)
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        logger.debug("", exc_info=True)
        _cleanup_temp(tmp_build_dir)
        sys.exit(1)
    finally:
        if tmp_build_dir and os.path.exists(tmp_build_dir):
            shutil.rmtree(tmp_build_dir)
            logger.debug(f"Cleaned up temp dir: {tmp_build_dir}")

    logger.info(f"\n{banner}")
    logger.info(f"SUCCESS! Project '{project_name}' generated in '{actual_output}'")
    logger.info(banner)
    logger.info("\nNext steps:")
    logger.info(f"  1. cmake -B build -G Ninja -DCMAKE_TOOLCHAIN_FILE=toolchain.cmake")
    logger.info(f"  2. cmake --build build")
    logger.info(f"  3. cmake --build build --target flash")
    logger.info(f"\nTo run tests:")
    logger.info(f"  cd {actual_output}/test")
    logger.info(f"  python run_tests.py")


def _cleanup_temp(tmp_dir: Optional[str]) -> None:
    """Remove temporary build directory on error, keeping workspace clean."""
    if tmp_dir and os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
        logger.info(f"Cleaned up failed build dir: {tmp_dir}")


def _report_validation_errors(errors: list) -> None:
    """Print validation errors organized by severity."""
    banner = "=" * 60
    logger.info(f"\n{banner}")
    logger.info("VALIDATION RESULTS")
    logger.info(banner)

    critical_errors = [e for e in errors if e['severity'] == 'CRITICAL']
    regular_errors = [e for e in errors if e['severity'] == 'ERROR']
    warnings = [e for e in errors if e['severity'] == 'WARNING']
    infos = [e for e in errors if e['severity'] == 'INFO']

    if critical_errors:
        logger.info("\n[CRITICAL] Fatal errors (cannot continue):")
        for err in critical_errors:
            logger.critical(f"  {err['message']}")
    if regular_errors:
        logger.info("\n[ERROR] Errors (recommended to fix):")
        for err in regular_errors:
            logger.error(f"  {err['message']}")
    if warnings:
        logger.info("\n[WARNING] Warnings:")
        for warn in warnings:
            logger.warning(f"  {warn['message']}")
    if infos:
        logger.info("\n[INFO] Information:")
        for info in infos:
            logger.info(f"  {info['message']}")

    if critical_errors or regular_errors:
        logger.info(f"\n{banner}")
        logger.info(f"Found {len(critical_errors)} critical, {len(regular_errors)} errors, "
                      f"{len(warnings)} warnings")
        logger.info("Please fix the errors and try again.")
        logger.info(banner)


def generate(hardware_yaml: str, output_dir: str, hil_mode: bool = False,
             dry_run: bool = False, show_diff: bool = False, force: bool = False,
             target: str = "stm32", validate_pins: bool = True,
             allocate_pins: bool = True, pin_db: Optional[str] = None,
             task_yaml: Optional[str] = None, bind_yaml: Optional[str] = None,
             components_yaml_path: Optional[str] = None,
             pubsub_yaml_path: Optional[str] = None,
             params_yaml_path: Optional[str] = None):
    """Backward-compatible wrapper around generate_project with default deps."""
    return generate_project(hardware_yaml, output_dir, hil_mode, dry_run,
                            show_diff, force, target=target,
                            validate_pins=validate_pins,
                            allocate_pins=allocate_pins, pin_db=pin_db,
                            task_yaml_path=task_yaml, bind_yaml_path=bind_yaml,
                            components_yaml_path=components_yaml_path,
                            pubsub_yaml_path=pubsub_yaml_path,
                            params_yaml_path=params_yaml_path)


def main():
    parser = argparse.ArgumentParser(
        description="Hardware2Code Generator - Generate STM32G0 + FreeRTOS projects from YAML"
    )
    parser.add_argument("-i", "--input", required=True, help="Path to hardware YAML file")
    parser.add_argument("-o", "--output", required=True, help="Output directory for generated project")
    parser.add_argument("--hil", action="store_true", help="Generate HIL test firmware")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate to temp dir, show file tree (no disk write to target)")
    parser.add_argument("--diff", action="store_true",
                        help="Show unified diff against existing target files")
    parser.add_argument("--force", action="store_true",
                        help="Skip compile check, force overwrite target directory")
    parser.add_argument("--target", type=str, default="stm32",
                        help="Target MCU backend (default: stm32)")
    parser.add_argument("--no-validate-pins", action="store_true",
                        help="Disable pin conflict validation")
    parser.add_argument("--no-allocate-pins", action="store_true",
                        help="Disable automatic pin allocation")
    parser.add_argument("--pin-db", type=str, default=None,
                        help="Path to custom MCU pin database directory")
    parser.add_argument("--task", type=str, default=None,
                        help="Path to task YAML file (task.yaml)")
    parser.add_argument("--bind", type=str, default=None,
                        help="Path to bind YAML file (bind.yaml)")
    parser.add_argument("--components", type=str, default=None,
                        help="Path to components YAML file (components.yaml)")
    parser.add_argument("--pubsub", type=str, default=None,
                        help="Path to pub/sub YAML file (pubsub.yaml)")
    parser.add_argument("--params", type=str, default=None,
                        help="Path to params YAML file (params.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug-level logging")
    args = parser.parse_args()

    _setup_logging(verbose=args.verbose)

    if not args.dry_run:
        _apply_vendored_patches()

    generate(args.input, args.output, args.hil,
             dry_run=args.dry_run, show_diff=args.diff, force=args.force,
             target=args.target, validate_pins=not args.no_validate_pins,
             allocate_pins=not args.no_allocate_pins, pin_db=args.pin_db,
             task_yaml=args.task, bind_yaml=args.bind,
             components_yaml_path=args.components,
             pubsub_yaml_path=args.pubsub,
             params_yaml_path=args.params)


def _apply_vendored_patches() -> None:
    """Apply hw2c patches to vendored submodules at generation time.

    FreeRTOS ARMv6-M port (V11): ulCriticalNesting is initialized to the
    poison value 0xAAAAAAAA.  taskENTER_CRITICAL/taskEXIT_CRITICAL used
    before xPortStartScheduler() (heap_4 malloc from pre-scheduler
    xQueueCreate/xTaskCreate) can then never reach nesting 0 and leave
    PRIMASK=1, freezing HAL_GetTick()-based waits (LSE startup, I2C NACK
    timeout) and hanging boot.  Revert to V10 semantics (0).

    The patch lives in the parent repo (applied on every generation)
    because the submodule points at the upstream FreeRTOS repository,
    which cannot accept local commits.  Idempotent.
    """
    port_c = os.path.join(STATIC_STM32_DIR, "FreeRTOS-Kernel",
                          "portable", "GCC", "ARM_CM0", "port.c")
    if not os.path.exists(port_c):
        logger.warning("FreeRTOS ARM_CM0 port.c not found at %s; patch skipped", port_c)
        return
    with open(port_c, "r", encoding="utf-8") as fh:
        text = fh.read()
    poison = "= 0xaaaaaaaaUL;"
    if poison in text:
        text = text.replace(
            poison,
            "= 0UL;   /* hw2c: V10 semantics (poison value wedges PRIMASK "
            "in pre-scheduler critical sections) */",
            1,
        )
        with open(port_c, "w", encoding="utf-8") as fh:
            fh.write(text)
        logger.info("Applied FreeRTOS ARM_CM0 port.c patch (ulCriticalNesting -> 0)")
    else:
        logger.info("FreeRTOS ARM_CM0 port.c already patched")


if __name__ == "__main__":
    main()
