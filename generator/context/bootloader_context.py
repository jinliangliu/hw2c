"""
bootloader_context.py
Bootloader / FOTA / IWDG / LED pin configuration helpers.
"""

import json
import os

from ..paths import FOTA_FORMAT_PATH, YMODEM_FORMAT_PATH


def load_fota_format() -> dict:
    """读取格式真源（`generator/data/fota_format.json`）。"""
    with open(FOTA_FORMAT_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_ymodem_format() -> dict:
    """读取 YMODEM 真源（`generator/data/ymodem_format.json`）。"""
    with open(YMODEM_FORMAT_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def ymodem_for_templates() -> dict:
    """把 YMODEM 真源压成 C 模板直接可用的扁平常量。

    与 `fota_transport_for_templates()` 同样的理由：模板里但凡出现一个字面量
    控制字节（`0x43` / `0x18`），它就与 Python 侧构成第二份定义。

    这里额外做两条**生成期**自检，它们挡住的是整整一类"能编过、但对不上任何
    真实终端软件"的实现：

      · 块长算式自洽（`total == 1 + 2 + size + 2`）—— 写错一处，所有块都
        少/多几个字节，症状是"发得出去、永远收不完"；
      · CRC 的 check 值必须真是 CRC-16/XMODEM（用 stdlib 独立复算）。
        判据是**标准**，不是"另一侧的实现也这么算" —— 复用 `fota_crc16`
        （CCITT-FALSE）时两侧自洽、与所有真实 YMODEM 软件不通，正是这条
        自检要拦下的形态。
    """
    import binascii

    spec = load_ymodem_format()
    ctl = spec["control"]
    blk = spec["block"]
    crc = spec["crc16"]
    hs = spec["handshake"]
    hdr = spec["header_block"]
    term = spec["terminator_block"]

    for size_key, total_key in (("size_128", "total_128"),
                                ("size_1024", "total_1024")):
        want = 1 + blk["number_field_size"] * 2 + blk[size_key] + blk["crc16_size"]
        if blk[total_key] != want:
            raise ValueError(
                "ymodem block.%s (%d) != 1 + 2*number_field_size + %s + crc16_size (%d)"
                % (total_key, blk[total_key], size_key, want))

    if blk["overhead"] != 1 + blk["number_field_size"] * 2 + blk["crc16_size"]:
        raise ValueError("ymodem block.overhead 与字段尺寸之和不符")

    got = binascii.crc_hqx(crc["check_input"].encode("ascii"), crc["init"])
    if got != crc["check"]:
        raise ValueError(
            "ymodem crc16.check 声明 %s，但用 %s(init=%#x) 独立复算是 %#x —— "
            "真源写错了，或者有人把它改成了 CCITT-FALSE（初值 0xFFFF）"
            % (hex(crc["check"]), crc["python_oracle"], crc["init"], got))

    # 注：『YMODEM 的 CRC 必须与帧协议那一侧的 CRC-16/CCITT-FALSE 不同』这条
    # 判据放在 `generator/tests/test_fota_ymodem.py` 里 —— 帧协议的初值没有
    # 写进 fota_format.json（对 C 侧是常量 0xFFFF，对 Python 侧是实现细节），
    # 在这里断言会变成"对着一个没被真源记录的数字比大小"，看着严格实则空转。

    out = {
        # ---- 控制字节 ----
        "ctl_soh": ctl["soh"]["value"],
        "ctl_stx": ctl["stx"]["value"],
        "ctl_eot": ctl["eot"]["value"],
        "ctl_ack": ctl["ack"]["value"],
        "ctl_nak": ctl["nak"]["value"],
        "ctl_can": ctl["can"]["value"],
        "ctl_pad": ctl["pad"]["value"],
        "ctl_crc_request": ctl["crc_request"]["value"],
        # ---- 块 ----
        "blk_size_128": blk["size_128"],
        "blk_size_1024": blk["size_1024"],
        "blk_overhead": blk["overhead"],
        "blk_total_128": blk["total_128"],
        "blk_total_1024": blk["total_1024"],
        "blk_complement_of": blk["complement_of"],
        "blk_number_modulus": blk["number_modulus"],
        "blk_max_payload": blk["max_payload"],
        # 接收侧单块缓冲：1 个起始字节 + 2 个块号字节 + 最大载荷 + 2 个 CRC 字节
        "blk_max_frame": blk["total_1024"],
        # ---- CRC ----
        "crc_poly": crc["poly"],
        "crc_init": crc["init"],
        "crc_check": crc["check"],
        # ---- 文件头块 ----
        "hdr_block_number": hdr["block_number"],
        "hdr_max_name_len": hdr["max_name_len"],
        "hdr_size_radix": hdr["size_radix"],
        # ---- 结束块 ----
        "term_block_number": term["block_number"],
        "term_block_size": term["size"],
        # ---- 握手时序 ----
        "hs_interval_ms": hs["interval_ms"],
        "hs_max_attempts": hs["max_attempts"],
        "hs_block_timeout_ms": hs["block_timeout_ms"],
        "hs_nak_max_retries": hs["nak_max_retries"],
        "hs_can_before_abort": hs["can_count_before_abort"],
        "hs_eot_nak_before_ack": hs["eot_nak_before_ack"],
        "hs_terminator_timeout_ms": hs["terminator_timeout_ms"],
    }

    # 分派前提：三个互不相等的起始字节（SOH/STX/EOT）才能只靠首字节决定去向。
    # EOT 与 SOH/STX 撞车时，"发送结束"会被当成"一个块开始了"。
    marks = [out["ctl_soh"], out["ctl_stx"], out["ctl_eot"]]
    if len(set(marks)) != len(marks):
        raise ValueError("ymodem SOH/STX/EOT 撞车: %r" % (marks,))

    # ---- 设备侧**硬编码**了语义、因而必须在生成期钉住的那几项 ----
    #
    # 这几项设备实现里用的是"结构性的 0/十进制数字字符"，没法（也不值得）
    # 做成运行时可配置。真源改动它们时，设备不会跟着改，而症状是静默的：
    # 比如 radix 改成 16 之后，块 0 的长度字段里 `A`..`F` 会被当成"数字结束"，
    # 于是一个 0x1F4 这样的长度被解析成 0 —— 随后一切正常，只是收不到东西。
    # 所以在**生成期**就拒掉，而不是留到设备上。
    if int(hdr["size_radix"]) != 10:
        raise ValueError(
            "ymodem header_block.size_radix 必须是 10（设备侧用 '0'..'9' 逐位解析）："
            "真源写的是 %r" % (hdr["size_radix"],))
    if int(hdr["name_terminator"]) != 0:
        raise ValueError(
            "ymodem header_block.name_terminator 必须是 0（设备侧按 NUL 截断文件名）："
            "真源写的是 %r" % (hdr["name_terminator"],))
    if int(term["payload_value"]) != 0:
        raise ValueError(
            "ymodem terminator_block.payload_value 必须是 0（设备侧用『载荷全零』"
            "判定结束块，不比较具体值）：真源写的是 %r" % (term["payload_value"],))

    # 块 0 被 ACK 之后再发一个 'C' —— 规范里接收方的固定次序，**不是**开关。
    #
    # 设备侧无条件执行这一步（协议强制），所以真源这一项也必须是 1：改成 0 不会
    # 让设备跟着改，只会让**主机侧发送器与 L5 台架的期望值**少掉那个 'C'，于是
    # 测试变成"要求设备漏发" —— 恰恰是本通道上最隐蔽的那类缺陷（两侧都自研时
    # 完全自洽，对接任何外部发送端才暴露）。必须在生成期拒掉。
    if int(hs["crc_after_header_ack"]) != 1:
        raise ValueError(
            "ymodem handshake.crc_after_header_ack 必须是 1：规范里接收方在 ACK 块 0 "
            "之后必须再发一个 'C' 邀请数据块，设备侧无条件执行。真源写成 %r 只会让"
            "主机侧期望值错，不会让设备漏发 —— 见设备模板 ym_handle_header() 的注释。"
            % (hs["crc_after_header_ack"],))
    return out


def fota_format_for_templates() -> dict:
    """把格式真源压成 C 模板直接可用的扁平常量。

    **为什么必须在生成期做这一步**：C 模板里但凡出现一个字面量偏移
    （`0xC0` / `16` / `48`），它就与 Python 侧构成了第二份定义 —— 两侧一旦改动
    不同步，补丁就会"生成成功、装上砖机"。A3 类缺陷正是这样产生的。
    所以 C 头文件里的每个偏移都由 JSON 算出，改格式只需改一处。

    返回值里的 `hdr_hole_off` / `hdr_hole_len` 是**由设备独占写入**的头部窗口
    ——即**整个 16 B 镜像头**。差分流天然带着镜像头的全部字节（新镜像原样参与
    差分），但设备一个字节都不采信：`image_size`/`crc32` 必须等代码区全部生成
    后才能算出（Flash 只能 1→0），而 magic/fw_version 干脆由设备自己写，
    这样"中途掉电"永远不可能留下一个 magic 合法的目标槽。
    """
    spec = load_fota_format()
    img = spec["image_header"]
    env = spec["delta_envelope"]
    f = img["fields"]
    ef = env["fields"]

    out = {
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
        # 设备**独占**写入的头部窗口（相对槽起点）——整个 16 B 镜像头。
        #
        # 为什么是整头而不是只留 image_size+crc32 那 8 字节：
        # 差分流天然包含镜像头的全部字节（新镜像原样参与差分），若让 magic 从
        # 流里进来，那么"应用中途掉电"就可能留下一个 **magic 合法** 的目标槽 ——
        # 它到底能不能被引导，就完全依赖"引导路径上每一处都必须先验 CRC"这个
        # 跨子系统的隐含假设。把整头改成设备在 FLUSH 阶段独占写入后，掉电状态下
        # 目标槽头部恒为擦除态（0xFFFFFFFF）⇒ magic 恒非法 ⇒ **结构上不可能
        # 被当成可引导镜像**，与模块内部其它检查无关。
        #
        # 这也正是 drv_fota_delta.h 里对掉电语义的既有承诺（"目标槽的镜像头
        # magic 尚未写入（它属于 FLUSH 阶段）"）—— 此前代码没做到，本项修齐。
        "hdr_hole_off": img["offset_in_slot"],
        "hdr_hole_len": img["size"],
        # 头部里落在 CRC 覆盖区内的**尾段**（magic + fw_version）。
        # 覆盖区自 magic 起、payload 起止；这一段由设备按信封内容喂入 CRC，
        # 不再依赖流里的字节。
        "hdr_tail_off": img["crc_region"]["start_offset_in_slot"],
        "hdr_tail_len": img["payload_offset_in_slot"]
                        - img["crc_region"]["start_offset_in_slot"],
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

    # ---- 不变量：设备独占写入的窗口必须**恰好覆盖整个镜像头** ----
    #
    # 这条不变量的作用不是"防止算错"，而是把上面那段论证固化下来：一旦有人把
    # hdr_hole 改回"只留 image_size+crc32"，掉电安全性就从"结构上不可能引导"
    # 退化成"依赖引导器先验 CRC"，而那种退化不会有任何编译或测试上的信号。
    hole = out["hdr_hole_off"]
    assert hole == img["offset_in_slot"], (
        "设备独占写入的头部窗口起点 %d 与镜像头偏移 %d 不一致"
        % (hole, img["offset_in_slot"]))
    assert out["hdr_hole_len"] == img["size"], (
        "设备独占写入的头部窗口长度 %d 与镜像头大小 %d 不一致 —— "
        "整头必须由设备在 FLUSH 阶段写入，见本函数的注释"
        % (out["hdr_hole_len"], img["size"]))
    # 头部尾段必须与 CRC 覆盖区的前 hdr_tail_len 字节完全重合
    assert out["hdr_tail_off"] == img["crc_region"]["start_offset_in_slot"]
    assert (out["hdr_tail_off"] + out["hdr_tail_len"]
            == img["payload_offset_in_slot"]), (
        "头部尾段 [%d, %d) 未恰好填满到 payload 起点 %d"
        % (out["hdr_tail_off"], out["hdr_tail_off"] + out["hdr_tail_len"],
           img["payload_offset_in_slot"]))

    return out


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


def fota_align_up(value: int, align: int) -> int:
    """向上取整到 `align` 的倍数。`align` 必须是正数。"""
    if align <= 0:
        raise ValueError("align must be positive, got %r" % (align,))
    return ((int(value) + align - 1) // align) * align


def fota_staging_geometry(slot_size: int, page_size: int, env_size: int,
                          patch_size: int, new_size: int) -> dict:
    """尾仓暂存（规划 §7.2）的几何与准入判定。

    **为什么准入公式不能只用 `new_size`**：擦除是按**整页**做的，设备侧
    `io->erase(dst_slot, new_size)` 实际擦掉 `align_up(new_size, page_size)` 字节。
    若在准入里只算 `new_size`，边界情况（例如 new_size 恰为整页数）会少算一页，
    于是应用阶段把尾仓的**首页**给擦了 —— 补丁被毁，而所有校验仍然通过，
    失败会以"CRC 不符"的形式出现在很远的地方。这是典型的"边界差一页"缺陷，
    所以这里用**实际擦除量**而不是声明量。

    Returns:
        dict，含 `ok`（准入通过与否）、`reason`（不通过的原因，便于写进日志）、
        `image_bytes`（新镜像实际占用，= 擦除量）、`staged_total`（暂存区占用，
        8 字节对齐）、`staging_off`（暂存区相对槽基址的偏移）、`headroom`（余量）。
        不通过时 `staging_off` 为 None —— 让调用方无法"忽略 ok 硬用偏移"。
    """
    staged_raw = int(env_size) + int(patch_size)
    staged_total = fota_align_up(staged_raw, 8)
    image_bytes = fota_align_up(new_size, page_size)

    out = {
        'image_bytes': image_bytes,
        'staged_total': staged_total,
        'staged_raw': staged_raw,
        'staging_off': None,
        'headroom': int(slot_size) - image_bytes - staged_total,
        'ok': False,
        'reason': '',
    }

    if patch_size <= 0:
        out['reason'] = 'patch_size is zero'
        return out
    if new_size <= 0:
        out['reason'] = 'new_size is zero'
        return out
    if slot_size % 8 != 0:
        # 暂存区基址 = slot_size - staged_total，两者都要 8 字节对齐才能
        # 满足 STM32G0 的双字编程前置条件。槽容量不是 8 的倍数说明配置本身
        # 有问题（正常是 2 KB 的倍数），宁可直接拒绝。
        out['reason'] = 'slot_size %d is not a multiple of 8' % (slot_size,)
        return out

    staging_off = int(slot_size) - staged_total
    if staging_off < 0:
        out['reason'] = 'staged patch (%d B) does not fit in the slot' % (staged_total,)
        return out
    if image_bytes + staged_total > int(slot_size):
        out['reason'] = ('image %d B + staged %d B > slot %d B'
                         % (image_bytes, staged_total, slot_size))
        return out
    if staging_off % 8 != 0:
        out['reason'] = 'staging offset %d is not 8-byte aligned' % (staging_off,)
        return out

    out['staging_off'] = staging_off
    out['ok'] = True
    return out


# FOTA 元数据的持久状态取值（写进 Flash 记录里的 `state` 字段）。
#
# 刻意与 drv_fota 的运行期枚举 `fota_state_t` 取值对齐：两套取值不重合时，
# "把持久状态赋给运行期状态"这类误用会静默变成另一个语义（v1 §9.1 的
# 位图设计就吃过这个亏）。但**它们仍然是两个东西**，不要互相赋值 ——
# 引导器只认 Flash 里那一份。
FOTA_META_STATE_IDLE = 0
FOTA_META_STATE_RECEIVING = 1
FOTA_META_STATE_READY = 2      # 收齐且 CRC 通过，等应用
FOTA_META_STATE_APPLYING = 3
FOTA_META_STATE_DONE = 4       # 已应用，等引导器确认
FOTA_META_STATE_ERROR = 5


def fota_meta_for_templates(boot_config: dict) -> dict:
    """Flash 元数据页与记录的布局，压成模板可直接用的常量。

    **为什么不是在 C 模板里写死**：页基址由 `bootloader.size_kb` 决定，
    记录字段偏移来自 `fota_format.json`。两处各写一份时，症状是"改了一个
    配置之后元数据莫名其妙读不出来"，而且只在特定 size_kb 上出现。

    记录里**只放无法从别处重算的字段**：
      · `slot`   —— 暂存区地址由它算出，而信封本身就在暂存区里（鸡生蛋）；
      · `staged` —— 已提交字节数。补丁正文里可以合法出现 0xFF，
                     所以扫描暂存区**推不出**"写到哪了"；
      · `state`  —— 决策本身。
    `new_crc32` / `patch_size` 不存：它们写在暂存区开头那 48 B 信封里，
    而 START 阶段本来就要逐字段比对来帧信封与暂存信封（判定续传），
    再存一份只会在两边不一致时制造歧义。
    """
    import struct

    spec = load_fota_format()['metadata']
    fields = spec['fields']
    magic = int(fields['magic']['value'])

    # 与 image_header 同一约定：value 是 ASCII 字节的 LE 解释。校验它是为了
    # 让"手改 JSON 时把 hex 与 decimal 改岔"立刻失败，而不是变成一个
    # 谁都认不出来的魔数。
    expect = struct.unpack('<I', b'FOTM')[0]
    if magic != expect:
        raise ValueError(
            "metadata.fields.magic 的 value/hex 与 'FOTM' 不一致："
            "value=%d hex=%s，应为 %d (0x%08X)"
            % (magic, fields['magic'].get('value_hex'), expect, expect))

    # 没有引导器 ⇒ 没有引导器区 ⇒ 没有元数据页。返回空 dict 而不是一堆 0：
    # 那几份消费它的模板（`fota_meta.h/.c`）只在**驱动被注入时**才会被渲染，
    # 而注入条件是 has_bootloader —— 所以空 dict 不会漏进任何产物。
    # 反过来给一堆 0 才是危险的：万一哪天真被渲染到，"页基址 0x0"会编过、
    # 会在运行期去擦地址 0（Flash 基址），症状离原因极远。
    if not boot_config.get('_meta_page_base'):
        return {}

    # 没有引导器 ⇒ 没有引导器区 ⇒ 没有元数据页。返回空 dict 而不是一堆 0：
    # 那几份消费它的模板（`fota_meta.h/.c`）只在**驱动被注入时**才会被渲染，
    # 而注入条件是 has_bootloader —— 所以空 dict 不会漏进任何产物。
    # 反过来给一堆 0 才是危险的：万一哪天真被渲染到，"页基址 0x0"会编过、
    # 会在运行期去擦地址 0（Flash 基址），症状离原因极远。
    if not boot_config.get('_meta_page_base'):
        return {}

    page_base = int(boot_config['_meta_page_base'])
    page_size = int(boot_config['_meta_page_size'])
    rec_size = int(spec['record_size'])
    rec_count = page_size // rec_size

    if rec_size <= 0 or rec_count < 2:
        raise ValueError(
            "元数据页 %d B 装不下至少 2 条 %d B 记录：日志式存储至少要能"
            "『留着上一条、写下一条』，否则每次追加都得先擦页"
            % (page_size, rec_size))

    # 页尾余数（2048 % 24 = 8）**明确不参与日志**。这一点要让调用方看得见：
    # 悄悄把余数当半条记录扫描，会在页刚好写满时读出一段 0xFF 组成的
    # "记录"，其 magic 恰好为 0xFFFFFFFF 而 seq 也是 0xFFFFFFFF。
    unused_tail = page_size - rec_count * rec_size

    return {
        'meta_magic': magic,
        'meta_page_base': page_base,
        'meta_page_size': page_size,
        'meta_record_size': rec_size,
        'meta_record_count': rec_count,
        'meta_unused_tail': unused_tail,
        # 字段偏移（来自真源，模板里不写字面量）
        'meta_off_magic': int(fields['magic']['offset']),
        'meta_off_seq': int(fields['seq']['offset']),
        'meta_off_state': int(fields['state']['offset']),
        'meta_off_slot': int(fields['slot']['offset']),
        'meta_off_staged': int(fields['staged']['offset']),
        'meta_off_crc16': int(fields['crc16']['offset']),
        # CRC16 只覆盖 crc16 字段之前的部分 —— 否则校验值把自己也算进去，
        # 变成一个没有不动点的方程。
        'meta_crc16_len': int(fields['crc16']['offset']),
        'st_idle': FOTA_META_STATE_IDLE,
        'st_receiving': FOTA_META_STATE_RECEIVING,
        'st_ready': FOTA_META_STATE_READY,
        'st_applying': FOTA_META_STATE_APPLYING,
        'st_done': FOTA_META_STATE_DONE,
        'st_error': FOTA_META_STATE_ERROR,
        # 0 保留给"没有待启动槽"。与 boot_nvm 的 BOOT_SLOT_A/B（0/1）**故意
        # 不同**：0 与 1 复用是最容易写错的一处 —— 元数据被清零后若 0 被
        # 解读成"槽 A"，引导器就会在一次擦除后去引导空槽。
        'slot_none': 0,
        'slot_a': 1,
        'slot_b': 2,
    }


def _crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE。与设备侧 fota_crc16()/fota_delta.c 的同一算法。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else ((crc << 1) & 0xFFFF)
    return crc


def fota_l5_negative_envelopes(boot_config: dict) -> dict:
    """L5 负例用的信封：**每一份都只违反一条规则**。

    为什么在生成期把字节算好、写死进 C 测试，而不是在 C 里现场拼：
    信封里的 `hdr_crc16` 必须与设备侧算法一致，在 C 里再实现一遍 CRC16
    就成了"同一事实两份实现" —— 正是本仓库反复吃亏的那类缺陷。让 Python
    算（`_crc16_ccitt_false`）而让 C 校验，就顺带得到了**跨实现交叉验证**：
    两侧不一致时 L5 会立刻红，而不是等到真板上才发现。

    "只违反一条"是这份向量唯一的设计要求。两份负例同时违反两条规则时，
    测试只能证明"某一条起了作用"，无法证明"被宣称的那一条起了作用" ——
    于是改错规则照样全绿（这正是 A9 的形态）。

    另外，每份负例都附带**正确的传输层 CRC16**（覆盖整条 48 B）。这样
    START 帧的传输级校验会通过、测试真的走到格式级分支；否则所有负例都会
    停在传输级，看似"全都拒绝了"，其实只测了一条规则。

    Returns:
        dict：键是短名（`too_big` / `over_admission` / `bad_magic` /
        `bad_hdr_crc16` / `auth_set` / `unknown_flag`），值是
        `{'env': bytes, 'crc16': int, 'expect': str, 'why': str}`。
        `expect` 是**设备侧应报的错误码符号名**（如 `FOTA_DELTA_E_ENV_MAGIC`），
        由 C 编译器去解析 —— Python 侧因此不需要知道那些枚举的数值，
        也就不会出现"两处各写一份错误码表"。`why` 写明它违反了哪一条。
    """
    spec = load_fota_format()
    env_spec = spec['delta_envelope']
    ef = env_spec['fields']
    env_size = env_spec['size']
    img = spec['image_header']

    page = int(boot_config.get('delta_page_size', 2048))
    slot_b = boot_config.get('_app_b_size') or 0x40000

    # 一份"处处合法"的底稿。`old_*` 的值在这里不重要：真正用到它们的
    # `fota_delta_apply()` 只有在补丁被完整收下之后才会看到这些数。
    def build(new_size, *, flags=0, magic=None, auth_len=0,
              hdr_crc16=None, patch_sz=64):
        buf = bytearray(env_size)

        def put_u32(off, val):
            buf[off:off + 4] = int(val).to_bytes(4, 'little')

        def put_u16(off, val):
            buf[off:off + 2] = int(val).to_bytes(2, 'little')

        put_u32(ef['magic']['offset'],
                ef['magic']['value'] if magic is None else magic)
        put_u16(ef['format_ver']['offset'], ef['format_ver']['value'])
        put_u16(ef['flags']['offset'], flags)
        put_u32(ef['old_size']['offset'], img['payload_offset_in_slot'] + 4096)
        put_u32(ef['old_crc32']['offset'], 0x11111111)
        put_u32(ef['new_size']['offset'], new_size)
        put_u32(ef['new_crc32']['offset'], 0x22222222)
        put_u32(ef['patch_size']['offset'], patch_sz)
        put_u32(ef['fw_version']['offset'], 7)
        put_u16(ef['auth_len']['offset'], auth_len)
        put_u16(ef['hdr_crc16']['offset'],
                _crc16_ccitt_false(bytes(buf[:ef['hdr_crc16']['offset']]))
                if hdr_crc16 is None else hdr_crc16)
        return bytes(buf)

    # 镜像必须装得下头部，否则连"合法性"都无从谈起
    min_size = int(img['payload_offset_in_slot']) + page
    normal = min_size + 4096

    # `over_admission` 要卡在**两条规则的缝隙**里，所以边界值必须现算：
    #   · 格式层用 `new_size > SLOT_B_SIZE` 粗筛 ⇒ new_size 不能超过槽容量；
    #   · 传输层用 `align_up(new_size, page) + staged_total` 精筛。
    # 于是取 `new_size = slot_b - staged_total - 100`（100 只是个小于一页的
    # 余量）：**声明值**看着装得下，但按整页对齐后就装不下。
    #
    # ⚠️ 用 `slot_b - page` 之类的"差不多"值是不行的：那个值其实装得下补丁，
    # 用例会走在正向路径上，准入公式改坏了也不会红 —— 一个看似在测准入、
    # 实际什么也没测的用例。
    patch_sz = 64
    staged_total = fota_align_up(env_size + patch_sz, 8)
    over_admission_size = slot_b - staged_total - 100

    cases = {
        # ── 格式层（fota_delta_parse_env）应当拒绝的 ──────────────────────
        'bad_magic':      (build(normal, magic=ef['magic']['value'] ^ 1),
                           'FOTA_DELTA_E_ENV_MAGIC', 'magic 不是 H2CD 的魔数'),
        'bad_hdr_crc16':  (build(normal, hdr_crc16=0x0000),
                           'FOTA_DELTA_E_ENV_CRC16', '前 32 B 的头 CRC16 被写坏'),
        'auth_set':       (build(normal, auth_len=16),
                           'FOTA_DELTA_E_AUTH', '声明了签名但本版不支持'),
        'unknown_flag':   (build(normal, flags=0x0002),
                           'FOTA_DELTA_E_ENV_FLAGS', 'flags 里出现未知位'),
        'too_big':        (build(slot_b + page),
                           'FOTA_DELTA_E_TOO_BIG', 'new_size 超过槽容量（格式层的粗筛）'),
        # ── 格式层通过、必须由**传输层准入公式**拦住的 ──────────────────
        'over_admission': (build(over_admission_size),
                           'FOTA_E_STAGING_FULL',
                           '声明长度装得下、按整页对齐后装不下（准入公式）'),
    }

    out = {}
    for name, (env, expect, why) in cases.items():
        out[name] = {
            'env': env,
            'crc16': _crc16_ccitt_false(env),
            'expect': expect,
            'why': why,
        }
    return out


def fota_transport_for_templates() -> dict:
    """传输层（drv_fota）与主机侧发送端共用的帧约定，取自格式真源。

    真源里写的是"一帧长什么样"，这里换成 C 模板直接能用的常量。任何在模板里
    出现的字面量（`0xA5` / `51` / `1024`）都会与 Python 侧构成第二份定义 ——
    A3 类缺陷的成因就是这个，所以一个都不留。
    """
    spec = load_fota_format()
    t = spec['transport']
    env = spec['delta_envelope']

    start = t['start']
    data = t['data']
    finish = t['finish']

    # data 帧的两个定长字段（seq / len）与尾校验的相对位置
    data_hdr = 0
    for fld in data['fields']:
        if fld['name'] in ('seq', 'len'):
            data_hdr += fld['size']
    crc16_size = 2
    for fld in data['fields']:
        if fld['name'] == 'crc16':
            crc16_size = fld['size']

    out = {
        'frame_start': start['value'],
        'frame_data': data['value'],
        'frame_finish': finish['value'],
        'ack': t['ack']['value'],
        'nak': t['nak']['value'],
        'chunk_size': t['chunk_size'],
        'ack_timeout_ms': t['ack_timeout_ms'],
        'start_total': start['total_size'],
        'finish_total': finish['total_size'],
        'data_hdr_size': data_hdr,
        'data_crc16_size': crc16_size,
        'ack_total': t['ack']['total_size'],
        'resp_seq_size': 2,
        # 一次能收下的最长帧（DATA）与最短帧（ACK/NAK）—— 接收缓冲区按最大帧开
        'max_frame': data_hdr + t['chunk_size'] + crc16_size + 1,
        'min_frame': 1 + t['ack']['total_size'],
    }
    # 帧标记必须互不相等，否则解析器无法只靠首字节分派；这是"格式正确性"里
    # 唯一能在此处静态检查的一条，其余由 L5 主机测试覆盖。
    marks = [out['frame_start'], out['frame_data'], out['frame_finish']]
    if len(set(marks)) != len(marks):
        raise ValueError("transport frame markers collide: %r" % (marks,))
    if out['start_total'] != env['size'] + crc16_size + t['frame_marker_size']:
        raise ValueError("transport START total_size disagrees with the envelope size")
    return out


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

        # ---- 元数据页：引导器区的**最后一页**（规划 §9.1 的落地替代方案）----
        #
        # 原方案把 FOTA 元数据放进 TAMP 的 BKP5R..BKP9R，前提是 §9.1 那句
        # "G0 的 TAMP 有 BKP0R..BKP31R"。**这句是错的**：RM0444 §31.1 写明
        # "5 backup registers"，§31.6.8 的偏移公式是 0x100 + 4*x, x = 0..4,
        # vendored 的 stm32g0b1xx.h 里 TAMP_TypeDef 也正好止于 BKP4R。
        # 而 BKP0R..BKP3R 归 boot_nvm、BKP4R 归 boot_main 的旧 FOTA 标志 ——
        # 一个不剩。写不存在的寄存器在编译期就报错，绕过编译则静默不持久。
        #
        # 换成 Flash 页还有个更根本的好处：备份域没有 V_BAT 电池时 VDD 一掉
        # 就丢，而 §7.2 "掉电免重传"正是靠这份元数据；Flash 只在擦除时丢。
        meta_page = int(load_fota_format()['metadata']['page_size'])
        delta_page = int(boot_config.get('delta_page_size', meta_page))
        if delta_page != meta_page:
            raise ValueError(
                "delta_page_size=%d 与 fota_format.json 的 metadata.page_size=%d "
                "不一致：两者都是'器件的擦除粒度'，必须相等"
                % (delta_page, meta_page))

        boot_size = int(boot_config['size_kb']) * 1024
        boot_code_size = boot_size - meta_page
        # 引导器代码必须明显小于"区域 - 一页"，否则这个配置本身就不成立。
        # 1 KB 是个很低的下限（实测引导器约 1.6 KB，见链接期 ASSERT），
        # 这里只在配置层面拦住荒谬的值；真正的护栏是链接脚本的 ASSERT。
        if boot_code_size < 1024:
            raise ValueError(
                "bootloader.size_kb=%s 太小：扣掉 %d B 元数据页后只剩 %d B 给引导器代码"
                % (boot_config['size_kb'], meta_page, boot_code_size))

        boot_config['_meta_page_size'] = meta_page
        boot_config['_meta_page_base'] = flash_base + boot_size - meta_page
        boot_config['_boot_code_size'] = boot_code_size

    return (boot_config, has_bootloader)


def inject_bootloader_drivers(has_bootloader: bool, has_uart: bool,
                               boot_config: dict, uart_name: str,
                               has_cli: bool = False) -> dict:
    """
    Auto-inject IWDG driver (bootloader) and FOTA drivers (bootloader + UART).

    Args:
        has_bootloader: whether bootloader is enabled.
        has_uart: whether any UART peripheral is present.
        boot_config: bootloader config dict with defaults already applied.
        uart_name: name of the primary UART peripheral for FOTA.
        has_cli: whether a CLI driver exists (it owns the UART byte stream).

    Returns:
        dict with drivers_additions (list), has_fota (bool), has_fota_receive
        (bool), hal_additions (list).
    """
    drivers_additions = []
    has_fota = False
    has_fota_receive = False
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

        # ── 元数据存储（引导器与 App **共用同一份**）────────────────────────
        # 它是"引导器的启动决策"与"App 的传输进度"之间的唯一共享状态，
        # 两侧必须对布局有完全一致的理解。所以只生成**一份** .c，放在
        # src/drivers/ 下，引导器的 CMake 用 ../src/drivers/fota_meta.c 引用它
        # —— 生成两份就等于给"两侧布局悄悄分叉"留了门。
        drivers_additions.append({
            'name': 'fota_meta',
            'template': 'drivers/fota_meta.c.j2',
            'header_template': 'drivers/fota_meta.h.j2',
            'model': {'type': 'Internal_FOTA'},
            'peripheral': {'name': 'fota_meta'},
        })

    # ── 接收侧（规划 P3）────────────────────────────────────────────────────
    # P3 之前这里恒为 False：`drv_fota` 当时编不过（它调用的是 `drv_uart.c`
    # 里并不存在的 `UART_StartRx_IT` 等接口），而且协议本身要按 §10 重写。
    # 现在它已被重写为帧解析 + 尾仓暂存 + 元数据 + 应用编排，可以真的接进去了。
    #
    # ⚠️ 前置条件：**必须有 CLI**。理由见 drv_cli.h 里 `cli_rx_sink_t` 的说明 ——
    # UART 的字节流在 App 里只有一个消费者（CLI 的行编辑器），FOTA 靠
    # `fota recv` 命令显式接管它。没有 CLI 就没有字节来源，硬生成只会得到一个
    # "编译通过、永远收不到东西"的工程 —— 正是本项目反复踩的那类坑。
    # 因此这里**显式要求** has_cli，而不是默认开启。
    if has_bootloader and has_uart and has_cli:
        drivers_additions.append({
            'name': 'fota',
            'template': 'drivers/drv_fota.c.j2',
            'header_template': 'drivers/drv_fota.h.j2',
            'model': {'type': 'Internal_FOTA'},
            'peripheral': {
                'name': 'fota',
                'uart_name': uart_name,
            },
        })
        has_fota_receive = True

        # ── YMODEM 传输通道（同一条接收链路的第二种"怎么把字节送进来"）──────
        #
        # 与 `fota` **同时注入、不做开关**。理由不是省事：
        #   · 它不改变接收语义，只换一种传输；关掉它并不能省下任何 Flash
        #     （模板是同一个接收链路的一部分），只省下约 1 KB 单块缓冲；
        #   · 而给它加一个 `bootloader.ymodem: false` 之类的开关，等于制造一种
        #     **只在部分示例里被编译/被测试**的配置 —— 本仓库最贵的一类缺陷
        #     （模板里写死一个本应派生的值、只在特定硬件配置下暴露）正是这么来的。
        #     无条件生成，意味着每个开了引导器的示例都会在生成期编译它、
        #     在主机测试里跑它。
        drivers_additions.append({
            'name': 'fota_ymodem',
            'template': 'drivers/drv_fota_ymodem.c.j2',
            'header_template': 'drivers/drv_fota_ymodem.h.j2',
            'model': {'type': 'Internal_FOTA'},
            'peripheral': {
                'name': 'fota_ymodem',
                'uart_name': uart_name,
            },
        })

    has_fota = has_bootloader and has_uart

    return {
        'drivers_additions': drivers_additions,
        'has_fota': has_fota,
        'has_fota_receive': has_fota_receive,
        'hal_additions': hal_additions
    }
