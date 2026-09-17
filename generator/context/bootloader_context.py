"""
bootloader_context.py
Bootloader / FOTA / IWDG / LED pin configuration helpers.
"""

import json
import os

from ..paths import FOTA_FORMAT_PATH


def load_fota_format() -> dict:
    """读取格式真源（`generator/data/fota_format.json`）。"""
    with open(FOTA_FORMAT_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fota_format_for_templates() -> dict:
    """把格式真源压成 C 模板直接可用的扁平常量。

    **为什么必须在生成期做这一步**：C 模板里但凡出现一个字面量偏移
    （`0xC0` / `16` / `48`），它就与 Python 侧构成了第二份定义 —— 两侧一旦改动
    不同步，补丁就会"生成成功、装上砖机"。A3 类缺陷正是这样产生的。
    所以 C 头文件里的每个偏移都由 JSON 算出，改格式只需改一处。

    返回值里的 `hdr_hole_off` / `hdr_hole_len` 是设备**事后回填**的那两个字段
    （`image_size` + `crc32`）：差分流把它们写成了 0xFFFFFFFF 占位，设备编程时
    必须跳过这 8 字节，留到 FLUSH 阶段单独写入（Flash 只能 1→0）。
    """
    spec = load_fota_format()
    img = spec["image_header"]
    env = spec["delta_envelope"]
    f = img["fields"]
    ef = env["fields"]

    return {
        # ---- 镜像头 ----
        "img_hdr_off": img["offset_in_slot"],
        "img_hdr_size": img["size"],
        "img_payload_off": img["payload_offset_in_slot"],
        "img_magic": f["magic"]["value"],
        "img_version_mask": img["version_mask"],
        "img_off_image_size": f["image_size"]["offset"],
        "img_off_crc32": f["crc32"]["offset"],
        "img_off_magic": f["magic"]["offset"],
        "img_off_fw_version": f["fw_version"]["offset"],
        "crc_region_start": img["crc_region"]["start_offset_in_slot"],
        "crc_region_addend": img["crc_region"]["length_addend"],
        # 事后回填的 8 字节窗口（相对槽起点）
        "hdr_hole_off": img["offset_in_slot"] + f["image_size"]["offset"],
        "hdr_hole_len": f["crc32"]["offset"] + f["crc32"]["size"]
                        - f["image_size"]["offset"],
        # ---- 信封 ----
        "env_size": env["size"],
        "env_magic": ef["magic"]["value"],
        "env_format_ver": ef["format_ver"]["value"],
        "env_flags_compressed": env["flags_bits"]["compressed"],
        "env_off_magic": ef["magic"]["offset"],
        "env_off_format_ver": ef["format_ver"]["offset"],
        "env_off_flags": ef["flags"]["offset"],
        "env_off_old_size": ef["old_size"]["offset"],
        "env_off_old_crc32": ef["old_crc32"]["offset"],
        "env_off_new_size": ef["new_size"]["offset"],
        "env_off_new_crc32": ef["new_crc32"]["offset"],
        "env_off_patch_size": ef["patch_size"]["offset"],
        "env_off_fw_version": ef["fw_version"]["offset"],
        "env_off_hdr_crc16": ef["hdr_crc16"]["offset"],
        "env_off_auth_len": ef["auth_len"]["offset"],
    }


def fota_delta_budget(boot_config: dict) -> dict:
    """差分应用的 RAM 预算（可由 YAML 覆盖，默认值见下）。

    默认值的依据：
      * `delta_page_size = 2048` —— STM32G0 双 bank 模式的页大小，擦写粒度的下界；
      * `delta_cache_size = 2048` —— 交给 HPatchLite 当 `temp_cache`。它会被对半分：
        下半做 old→new 拷贝的暂存，上半做补丁输入缓存。太小会让拷贝步长变碎
        （次数变多，不影响正确性）；太大白占 RAM。
      * `delta_dict_size = 4096` —— tinyuz 回溯窗口**上限**，同时是静态分配的
        解压缓冲大小。真实固件对实测：1K/4K/16K 的补丁大小几乎一样，
        4 KB 已拿到 16 KB 的全部收益（`generator/delta_tool.py::build()`）。
        实际使用的值以补丁流开头声明的为准，必须 ≤ 本上限。
    """
    return {
        'page_size': int(boot_config.get('delta_page_size', 2048)),
        'cache_size': int(boot_config.get('delta_cache_size', 2048)),
        'dict_size': int(boot_config.get('delta_dict_size', 4096)),
    }


def get_boot_led_pin(pins: list) -> dict:
    """
    Extract LED pin info from YAML pins list.

    Searches for pin with label == "LED".  Falls back to GPIOC / pin 0 if
    no LED pin is declared in the hardware YAML.

    Args:
        pins: list of pin dicts, each with 'id', 'label', 'function'.

    Returns:
        dict with keys: boot_led_port, boot_led_pin_num, boot_led_rcc_enable.
    """
    led_pin = None
    for p in pins:
        if p.get('label') == 'LED':
            led_pin = p
            break

    if led_pin:
        pin_id = led_pin['id']          # e.g. "PA5"
        port_letter = pin_id[1]          # 'A'
        pin_num = int(pin_id[2:])        # 5
    else:
        port_letter = 'C'
        pin_num = 0

    return {
        'boot_led_port': f'GPIO{port_letter}',
        'boot_led_pin_num': pin_num,
        'boot_led_rcc_enable': f'RCC_IOPENR_GPIO{port_letter}EN',
    }


def build_boot_config(bootloader_raw: dict,
                      mcu_flash_kb: int = 512) -> tuple:
    """
    Parse bootloader raw config, set defaults, and compute linker-level
    slot addresses.

    Args:
        bootloader_raw: raw bootloader dict from hardware YAML.
        mcu_flash_kb:  total on-chip Flash size in KiB (default 512 for
                       STM32G0B1RE).

    Returns:
        (boot_config, has_bootloader) tuple.
    """
    has_bootloader = bootloader_raw.get('enabled', False)
    boot_config = dict(bootloader_raw) if has_bootloader else {}
    if has_bootloader:
        boot_config.setdefault('size_kb', 8)
        boot_config.setdefault('app_a_offset', 0x2000)
        boot_config.setdefault('app_b_offset', 0x40000)
        boot_config.setdefault('crc_method', 'crc32_hw')
        boot_config.setdefault('boot_flag_src', 'tamp_bkp')
        boot_config.setdefault('max_retries', 3)
        boot_config.setdefault('wdg_timeout_ms', 5000)

        # Compute IWDG reload value: prescaler /256, LSI ~32kHz → 8ms per tick
        # Clamp to 12-bit range [1, 0xFFF]
        wdg_timeout_ms = boot_config['wdg_timeout_ms']
        boot_config['iwdg_reload_value'] = max(1, min(int(wdg_timeout_ms / 8), 0xFFF))

        # ---- Compute linker-script slot addresses (all derived from config) ----
        flash_base = 0x08000000
        ao = boot_config['app_a_offset']
        bo = boot_config['app_b_offset']
        flash_bytes = mcu_flash_kb * 1024

        boot_config['_app_a_start'] = flash_base + ao
        boot_config['_app_a_end']   = flash_base + bo
        boot_config['_app_b_start'] = flash_base + bo
        boot_config['_app_b_end']   = flash_base + flash_bytes

        # Convenience: slot sizes for C code
        boot_config['_app_a_size'] = bo - ao
        boot_config['_app_b_size'] = flash_bytes - bo

    return (boot_config, has_bootloader)


def inject_bootloader_drivers(has_bootloader: bool, has_uart: bool,
                               boot_config: dict, uart_name: str) -> dict:
    """
    Auto-inject IWDG driver (bootloader) and FOTA drivers (bootloader + UART).

    Args:
        has_bootloader: whether bootloader is enabled.
        has_uart: whether any UART peripheral is present.
        boot_config: bootloader config dict with defaults already applied.
        uart_name: name of the primary UART peripheral for FOTA.

    Returns:
        dict with drivers_additions (list), has_fota (bool), hal_additions (list).
    """
    drivers_additions = []
    has_fota = False
    hal_additions = []

    # IWDG driver is auto-injected when bootloader is enabled
    if has_bootloader:
        drivers_additions.append({
            'name': 'iwdg',
            'template': 'drivers/drv_iwdg.c.j2',
            'header_template': 'drivers/drv_iwdg.h.j2',
            'model': {'type': 'Internal_IWDG'},
            'peripheral': {
                'name': 'iwdg',
                'wdg_timeout_ms': boot_config.get('wdg_timeout_ms', 5000)
            }
        })

    # ── 差分应用（规划 P2'）────────────────────────────────────────────────
    # 只在 bootloader 开启时注入：它要往**另一个槽**写 Flash，没有引导器的工程
    # 既没有第二个槽，也没有回滚兜底。
    #
    # 不依赖 UART —— 接收归接收，应用归应用。把两者绑在一起是本仓库踩过的
    # 一个教训的变体：耦合越紧，越容易在只生成一半时得到一个"看起来能编、
    # 实际用不了"的工程。
    if has_bootloader:
        drivers_additions.append({
            'name': 'fota_delta',
            'template': 'drivers/fota_delta.c.j2',
            'header_template': 'drivers/fota_delta.h.j2',
            'model': {'type': 'Internal_FOTA'},
            'peripheral': {'name': 'fota_delta'},
            # 解码器与解压器都是 vendored 源码，需要各自的包含路径
            'includes': [
                '$(HARDWARE2CODE_STATIC)/../third_party/hpatch_lite',
                '$(HARDWARE2CODE_STATIC)/../third_party/tinyuz/decompress',
            ],
        })
        hal_additions.extend(['stm32g0xx_hal_flash.c', 'stm32g0xx_hal_flash_ex.c'])

    # ⚠️ 接收侧（`drv_fota`）**暂不注入**：它当前**编不过**，而且协议本身要按
    # 规划 §10 重写（START/FINISH 帧、尾仓暂存、BKP5R..BKP9R），那是 P3 的工作。
    #
    # 为什么之前没人发现：它调用 `UART_IsRxComplete` / `UART_StartRx_IT` /
    # `UART_GetRxCount` / `UART_SendByte`，而这四个函数是 `drv_uart.c` 里的
    # **static** 函数 —— 跨翻译单元根本不可见；它 include 的还是
    # `drv_<uart_name>.h`（如 `drv_usart2.h`），与 `drv_uart` 也不是同一个文件。
    # 更要紧的是：此前**没有任何示例开启 bootloader**，所以这个文件从未被渲染、
    # 从未被编译，缺陷也就从未暴露（fota_demo 一出现就立刻暴露了）。
    #
    # 现在把 `has_bootloader` 的工程做成"能编译、能验证引导与差分应用"的状态，
    # 接收路径等 P3 重写后再打开，而不是继续留一个编不过的文件在树里。
    has_fota = has_bootloader and has_uart

    return {
        'drivers_additions': drivers_additions,
        'has_fota': has_fota,
        # 接收侧协议尚未就绪：驱动与其单测都先不生成
        'has_fota_receive': False,
        'hal_additions': hal_additions
    }
