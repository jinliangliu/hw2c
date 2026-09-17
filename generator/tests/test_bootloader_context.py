"""Tests for generator/context/bootloader_context.py"""

from generator.context.bootloader_context import (build_boot_config,
                                                  inject_bootloader_drivers,
                                                  get_boot_led_pin)


def test_build_boot_config_disabled():
    """Bootloader disabled returns empty config and False"""
    config, enabled = build_boot_config({})
    assert enabled == False
    assert config == {}


def test_build_boot_config_enabled_minimal():
    """Bootloader enabled with minimal config"""
    config, enabled = build_boot_config({"enabled": True})
    assert enabled == True
    assert config["size_kb"] == 8
    assert config["app_a_offset"] == 0x2000
    assert config["app_b_offset"] == 0x40000
    assert config["wdg_timeout_ms"] == 5000
    assert config["max_retries"] == 3


def test_build_boot_config_custom_values():
    """Bootloader with custom values preserves them"""
    config, enabled = build_boot_config({
        "enabled": True,
        "size_kb": 16,
        "app_a_offset": 0x4000,
        "wdg_timeout_ms": 8000,
        "max_retries": 5
    })
    assert config["size_kb"] == 16
    assert config["app_a_offset"] == 0x4000
    assert config["wdg_timeout_ms"] == 8000
    assert config["max_retries"] == 5


def test_build_boot_config_calculates_iwdg_reload():
    """Bootloader computes iwdg_reload_value"""
    config, enabled = build_boot_config({"enabled": True, "wdg_timeout_ms": 5000})
    # 5000ms / 8ms per tick = 625, within 12-bit range
    assert config["iwdg_reload_value"] == 625


def test_inject_bootloader_drivers_no_bootloader():
    """No bootloader means no injections"""
    result = inject_bootloader_drivers(False, False, {}, "")
    assert result["has_fota"] == False
    assert len(result["drivers_additions"]) == 0
    assert len(result["hal_additions"]) == 0


def test_inject_bootloader_drivers_with_bootloader_no_uart():
    """有引导器但没串口：注入 IWDG + 差分应用（fota_delta）。

    `fota_delta` 只依赖引导器，**不依赖 UART** —— 差分应用要往另一个槽写
    Flash，有引导器才有第二个槽；接收通道是另一件事（P3）。把两者绑在一起
    会得到一个"有引导器却写不了另一个槽"的工程。
    """
    result = inject_bootloader_drivers(True, False, {"wdg_timeout_ms": 5000}, "")
    assert result["has_fota"] == False          # 整条 OTA 传输链路仍不可用
    assert result["has_fota_receive"] == False
    names = [d["name"] for d in result["drivers_additions"]]
    assert names == ["iwdg", "fota_delta"]


def test_inject_bootloader_drivers_with_fota():
    """有引导器 + UART：仍然只有 IWDG + 差分应用。

    接收侧（`drv_fota`）在 P3 重写之前**不注入**：它引用了 `drv_uart.c` 里的
    static 函数，跨翻译单元不可见，从来编不过（此前无示例开启 bootloader，
    所以从未被发现）。所以 `has_fota_receive` 恒假，驱动表里也没有它。
    """
    result = inject_bootloader_drivers(True, True, {"wdg_timeout_ms": 5000}, "uart1")
    assert result["has_fota"] == True           # bootloader + uart ⇒ OTA 链路有条件
    assert result["has_fota_receive"] == False  # 但接收驱动尚未就绪
    names = [d["name"] for d in result["drivers_additions"]]
    assert names == ["iwdg", "fota_delta"]
    assert "fota" not in names, "接收驱动仍未就绪，不应出现在驱动表里"


def test_fota_delta_driver_carries_decoder_include_paths():
    """vendored 解码器的包含路径必须由驱动自己带上。

    否则模板会渲染出 `#include "hpatch_lite.h"` 而 CMake 不知道该去哪儿找 ——
    错误发生在编译期，且只在这个驱动存在时出现。
    """
    result = inject_bootloader_drivers(True, True, {"wdg_timeout_ms": 5000}, "uart1")
    drv = next(d for d in result["drivers_additions"] if d["name"] == "fota_delta")
    incs = " ".join(drv["includes"])
    assert "hpatch_lite" in incs
    assert "tinyuz" in incs


def test_bootloader_adds_flash_hal_sources():
    """差分应用要擦写 Flash，HAL 的 flash 源文件必须被带上。"""
    result = inject_bootloader_drivers(True, False, {"wdg_timeout_ms": 5000}, "")
    assert "stm32g0xx_hal_flash.c" in result["hal_additions"]
    assert "stm32g0xx_hal_flash_ex.c" in result["hal_additions"]


def test_get_boot_led_pin_found():
    """LED pin extracted from pins list when label == 'LED'"""
    pins = [{"id": "PA5", "function": "GPIO_Output", "label": "LED"}]
    result = get_boot_led_pin(pins)
    assert result["boot_led_port"] == "GPIOA"
    assert result["boot_led_pin_num"] == 5
    assert result["boot_led_rcc_enable"] == "RCC_IOPENR_GPIOAEN"


def test_get_boot_led_pin_fallback():
    """No LED pin in list → fallback to GPIOC / pin 0"""
    pins = [{"id": "PB3", "function": "GPIO_Input", "label": "BUTTON"}]
    result = get_boot_led_pin(pins)
    assert result["boot_led_port"] == "GPIOC"
    assert result["boot_led_pin_num"] == 0
    assert result["boot_led_rcc_enable"] == "RCC_IOPENR_GPIOCEN"


def test_get_boot_led_pin_empty():
    """Empty pins list → fallback"""
    result = get_boot_led_pin([])
    assert result["boot_led_port"] == "GPIOC"
    assert result["boot_led_pin_num"] == 0


def test_build_boot_config_computes_slot_addresses():
    """Default config computes correct slot addresses for 512KB flash"""
    config, enabled = build_boot_config({"enabled": True})
    assert config["_app_a_start"] == 0x08002000
    assert config["_app_a_end"]   == 0x08040000
    assert config["_app_b_start"] == 0x08040000
    assert config["_app_b_end"]   == 0x08080000
    assert config["_app_a_size"]  == 0x3E000   # 248KB
    assert config["_app_b_size"]  == 0x40000   # 256KB


def test_build_boot_config_slot_addresses_custom():
    """Custom offsets produce correct computed addresses"""
    config, enabled = build_boot_config({
        "enabled": True,
        "size_kb": 16,
        "app_a_offset": 0x4000,
        "app_b_offset": 0x30000,
    })
    assert config["_app_a_start"] == 0x08004000
    assert config["_app_a_end"]   == 0x08030000
    assert config["_app_b_start"] == 0x08030000
    assert config["_app_b_end"]   == 0x08080000
    assert config["_app_a_size"]  == 0x2C000
    assert config["_app_b_size"]  == 0x50000


if __name__ == "__main__":
    test_build_boot_config_disabled()
    test_build_boot_config_enabled_minimal()
    test_build_boot_config_custom_values()
    test_build_boot_config_calculates_iwdg_reload()
    test_inject_bootloader_drivers_no_bootloader()
    test_inject_bootloader_drivers_with_bootloader_no_uart()
    test_inject_bootloader_drivers_with_fota()
    test_fota_delta_driver_carries_decoder_include_paths()
    test_bootloader_adds_flash_hal_sources()
    test_get_boot_led_pin_found()
    test_get_boot_led_pin_fallback()
    test_get_boot_led_pin_empty()
    test_build_boot_config_computes_slot_addresses()
    test_build_boot_config_slot_addresses_custom()
    print("All bootloader_context tests passed.")
