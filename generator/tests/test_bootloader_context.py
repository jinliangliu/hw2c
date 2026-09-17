"""Tests for generator/context/bootloader_context.py"""

import pytest

from generator.context.bootloader_context import (FOTA_META_STATE_ERROR,
                                                  FOTA_META_STATE_IDLE,
                                                  build_boot_config,
                                                  fota_align_up,
                                                  fota_delta_budget,
                                                  fota_format_for_templates,
                                                  fota_meta_for_templates,
                                                  fota_staging_geometry,
                                                  fota_transport_for_templates,
                                                  get_boot_led_pin,
                                                  inject_bootloader_drivers)

# 示例（examples/fota_demo）用的引导器配置：8 KB 引导器区、512 KB Flash、
# 槽 A 从 0x2000 起、槽 B 从 0x40000 起。元数据页基址由它算出。
_BOOT_CONFIG, _ = build_boot_config({"enabled": True})


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
    # fota_meta 与 fota_delta 一起跟着**引导器**注入（而不是跟着 UART）：
    # 它是"引导器的启动决策"与"应用进度"之间的共享状态，绑在一起才自洽。
    assert names == ["iwdg", "fota_delta", "fota_meta"]


def test_inject_bootloader_drivers_with_fota():
    """有引导器 + UART 但**没有 CLI**：只有 IWDG + 差分应用，不含接收驱动。

    理由不是"接收驱动还没写好"，而是**字节来源**：`drv_fota` 的输入是 CLI
    正在消费的那条 UART 字节流，FOTA 靠 `fota recv` 显式接管它（见
    `cli_rx_sink_t`）。没有 CLI 就没有字节来源，硬生成只会得到一个"编译通过、
    永远收不到东西"的工程 —— 那正是本项目反复踩的那类坑。所以这里仍然
    不注入，但原因换了。真正的原因由下一个用例正面钉住。
    """
    result = inject_bootloader_drivers(True, True, {"wdg_timeout_ms": 5000}, "uart1")
    assert result["has_fota"] == True           # bootloader + uart ⇒ OTA 链路有条件
    assert result["has_fota_receive"] == False  # 但没有 CLI ⇒ 没有字节来源
    names = [d["name"] for d in result["drivers_additions"]]
    assert names == ["iwdg", "fota_delta", "fota_meta"]
    assert "fota" not in names, "没有 CLI 时不应注入接收驱动"


def test_inject_bootloader_drivers_with_cli_injects_receiver():
    """引导器 + UART + CLI ⇒ 接收驱动（P3 完成之后才成立）。

    这是 P3 的**注入条件**，也是"这条路径第一次真的进入构建"的判据：
    在此之前 `has_fota_receive` 恒假，`drv_fota.c` 从未被渲染、从未被编译，
    于是 FR-14 长期标着 ✅ 而整条接收链路根本不存在。
    """
    result = inject_bootloader_drivers(True, True, {"wdg_timeout_ms": 5000}, "uart1",
                                       has_cli=True)
    assert result["has_fota_receive"] == True
    names = [d["name"] for d in result["drivers_additions"]]
    assert names == ["iwdg", "fota_delta", "fota_meta", "fota"]
    fota = next(d for d in result["drivers_additions"] if d["name"] == "fota")
    assert fota["template"] == "drivers/drv_fota.c.j2"
    assert fota["header_template"] == "drivers/drv_fota.h.j2"


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


def test_fota_transport_frame_sizes_agree_with_the_format_source():
    """传输层的帧长必须与格式真源自洽。

    写错一处就会让**所有**帧都少/多几个字节 —— 形态是"能发出去、设备永远
    收不齐"，而且编译单元测试全绿。所以把算式变成断言。
    """
    t = fota_transport_for_templates()
    fmt = fota_format_for_templates()

    assert t["start_total"] == 1 + fmt["env_size"] + 2
    assert t["data_hdr_size"] == 4          # seq(2) + len(2)
    assert t["data_crc16_size"] == 2
    assert t["max_frame"] == t["data_hdr_size"] + t["chunk_size"] + 2 + 1
    assert len({t["frame_start"], t["frame_data"], t["frame_finish"]}) == 3, (
        "帧标记互不相等是「只靠首字节分派」的前提"
    )
    assert t["ack_timeout_ms"] > 0


def test_fota_meta_record_layout_tiles_exactly():
    """记录内的字段必须**无缝无重叠**地铺满 record_size。

    少了这条，"字段偏移写错一个"的表现是"元数据跨复位后莫名其妙变了"：
    读出来的 state 其实是 staged 的高两字节，而且所有取值都还落在合法范围内，
    所以任何"看值域"的检查都拦不住。断言铺满 + 与 CRC16 的覆盖长度自洽，
    就把这类错位变成一次生成期失败。
    """
    m = fota_meta_for_templates(_BOOT_CONFIG)
    fmt = fota_format_for_templates()

    offs = sorted([
        (m["meta_off_magic"], 4), (m["meta_off_seq"], 4),
        (m["meta_off_state"], 4), (m["meta_off_slot"], 4),
        (m["meta_off_staged"], 4), (m["meta_off_crc16"], 2),
    ])
    cursor = 0
    for off, size in offs:
        assert off == cursor, "字段偏移有空洞或重叠：%r" % (offs,)
        cursor += size
    assert cursor <= m["meta_record_size"], (
        "记录的字段加起来 %d B，超过 record_size %d B"
        % (cursor, m["meta_record_size"])
    )

    # CRC16 只覆盖校验字段之前的部分：覆盖到自己就成了没有不动点的方程
    assert m["meta_crc16_len"] == m["meta_off_crc16"]
    # magic 必须落在记录的第一个字节，扫描时才能靠"首字仍是 0xFF"判空槽
    assert m["meta_off_magic"] == 0

    # 双字编程粒度：一条记录必须是 8 B 的整数倍，否则最后一次 HAL_FLASH_Program
    # 会跨出记录边界、写进下一条记录的头部。
    assert m["meta_record_size"] % 8 == 0
    # 每个 32 bit 字段都必须落在 4 字节边界上（fota_meta_rd32 直接按 u32 取值）
    for key in ("meta_off_magic", "meta_off_seq", "meta_off_state",
                "meta_off_slot", "meta_off_staged"):
        assert m[key] % 4 == 0, "%s=%d 未按 4 字节对齐" % (key, m[key])

    # 真源里算出来的 magic 必须能对上 "FOTM"（防止手改 JSON 时 hex/dec 改岔）
    assert m["meta_magic"] == int.from_bytes(b"FOTM", "little")
    assert m["st_error"] == FOTA_META_STATE_ERROR
    assert m["st_idle"] == FOTA_META_STATE_IDLE
    # 0 必须保留给"没有待启动槽"：清零的记录读出来就是"无事发生"
    assert m["slot_none"] == 0
    assert len({m["slot_none"], m["slot_a"], m["slot_b"]}) == 3
    assert fmt["env_size"] > 0          # 顺手确认真源能加载


def test_fota_meta_page_is_the_last_page_of_the_bootloader_region():
    """元数据页 = 引导器区的**最后一页**，且不与代码区重叠。

    引导器代码区（`_boot_code_size`）必须恰好是"区域大小 - 一页"，链接脚本
    就是按它写 FLASH 的 LENGTH。算错一格的表现是：元数据页在运行期被
    `fota_meta_append()` 擦掉时，擦的是引导器**正在执行的代码** ——
    设备升级到一半就再也起不来了。
    """
    cfg = _BOOT_CONFIG
    flash_base = 0x08000000
    page = cfg["_meta_page_size"]

    assert cfg["_meta_page_base"] == flash_base + cfg["size_kb"] * 1024 - page
    assert cfg["_boot_code_size"] == cfg["size_kb"] * 1024 - page
    assert cfg["_boot_code_size"] + page == cfg["size_kb"] * 1024
    # 代码区右侧边界必须正好等于元数据页基址（中间不留缝、也不重叠）
    assert flash_base + cfg["_boot_code_size"] == cfg["_meta_page_base"]
    # 实测引导器约 1.6 KB（见 bootloader.ld 的 ASSERT），8 KB 配置下余量充足
    assert cfg["_boot_code_size"] >= 4096

    m = fota_meta_for_templates(cfg)
    assert m["meta_page_base"] == cfg["_meta_page_base"]
    # 页尾余数明确不参与日志：把它当半条记录扫描会读出一段全 0xFF 的"记录"
    assert m["meta_record_count"] * m["meta_record_size"] \
        + m["meta_unused_tail"] == m["meta_page_size"]
    # 日志至少要能"留着上一条、写下一条"，否则每次追加都得先擦页
    assert m["meta_record_count"] >= 2


def test_fota_meta_page_size_must_match_the_erase_granularity():
    """元数据页大小就是器件的擦除粒度，必须与 delta_page_size 一致。

    两处各写一个"页大小"时，症状是"一半按新粒度、一半按旧粒度"，
    而两个值都合法、都能编过。
    """
    with pytest.raises(ValueError) as ei:
        build_boot_config({"enabled": True, "delta_page_size": 1024})
    assert "delta_page_size" in str(ei.value)


def test_bootloader_config_rejects_a_region_too_small_for_the_metadata_page():
    """size_kb 装不下"一页元数据 + 一点代码"时必须在配置期就失败。"""
    with pytest.raises(ValueError) as ei:
        build_boot_config({"enabled": True, "size_kb": 2})
    assert "size_kb" in str(ei.value)


def test_fota_align_up():
    assert fota_align_up(0, 8) == 0
    assert fota_align_up(1, 8) == 8
    assert fota_align_up(8, 8) == 8
    assert fota_align_up(4304, 2048) == 6144


def test_fota_staging_geometry_uses_the_erased_size_not_the_declared_one():
    """准入必须按"实际擦除量"算 —— 这是"边界差一页"缺陷的判据。

    构造一个**恰好**卡在边界上的用例：声明长度本身装得下，但按整页对齐后
    装不下。若实现退回用 `new_size`，这个用例会翻成 ok，而真实设备会在应用
    阶段把暂存区的首页擦掉。
    """
    page = 2048
    env_size = 48
    patch_size = 64
    staged = fota_align_up(env_size + patch_size, 8)
    slot = 0x40000

    new_size = slot - staged - 100          # 声明值装得下
    geo = fota_staging_geometry(slot, page, env_size, patch_size, new_size)
    assert geo["ok"] is False, (
        "align_up(%d, %d) + %d = %d > %d，准入必须拒绝"
        % (new_size, page, staged, fota_align_up(new_size, page) + staged, slot)
    )
    assert geo["image_bytes"] == fota_align_up(new_size, page), (
        "image_bytes 必须是擦除量（按整页），不是声明值"
    )

    # 反向：留出一页的余量就必须通过
    ok = fota_staging_geometry(slot, page, env_size, patch_size, slot - staged - page - 8)
    assert ok["ok"] is True
    assert ok["staging_off"] % 8 == 0


def test_fota_delta_budget_can_be_overridden_by_yaml():
    """预算可由 YAML 覆盖，默认值必须与真源宣称的一致。"""
    assert fota_delta_budget({}) == {
        "page_size": 2048, "cache_size": 2048, "dict_size": 4096,
    }
    assert fota_delta_budget({"delta_dict_size": 16384})["dict_size"] == 16384


if __name__ == "__main__":
    test_build_boot_config_disabled()
    test_build_boot_config_enabled_minimal()
    test_build_boot_config_custom_values()
    test_build_boot_config_calculates_iwdg_reload()
    test_inject_bootloader_drivers_no_bootloader()
    test_inject_bootloader_drivers_with_bootloader_no_uart()
    test_inject_bootloader_drivers_with_fota()
    test_inject_bootloader_drivers_with_cli_injects_receiver()
    test_fota_delta_driver_carries_decoder_include_paths()
    test_bootloader_adds_flash_hal_sources()
    test_get_boot_led_pin_found()
    test_get_boot_led_pin_fallback()
    test_get_boot_led_pin_empty()
    test_build_boot_config_computes_slot_addresses()
    test_build_boot_config_slot_addresses_custom()
    test_fota_transport_frame_sizes_agree_with_the_format_source()
    test_fota_meta_bitfields_can_hold_every_state_and_slot()
    test_fota_meta_registers_are_distinct()
    test_fota_align_up()
    test_fota_staging_geometry_uses_the_erased_size_not_the_declared_one()
    test_fota_delta_budget_can_be_overridden_by_yaml()
    print("All bootloader_context tests passed.")
