#!/usr/bin/env python3
"""FOTA 主机侧发送端（YMODEM 通道）。

把 `.h2cd` 差分补丁按 **YMODEM batch（CRC 握手）** 发给设备上的
`drv_fota_ymodem` —— 也就是"用任意终端软件内置的文件发送功能升级固件"。

为什么要有这个文件（设备侧已经有一个 YMODEM 实现了）
------------------------------------------------------
它**不是**设备的对端实现，而是三件事的载体：

  1. **现场可用**：手边只有 Tera Term / SecureCRT / ExtraPuTTY / lrzsz 的
     场合，操作员用它们的 YMODEM 发送即可，不需要本仓库的任何工具。这个
     脚本是给"自动化 / CI / 半自动产线"用的等价物 —— 一条命令，不用点菜单。
  2. **跨实现真值**：L5 测试台（`generator/tests/test_fota_ymodem_l5.py`）
     用它构造全部上行字节，让设备侧去解析。若测试台自己在 C 里拼一遍块，
     就变成"自己和自己对答案"：块 CRC 恒过，什么也证明不了。
  3. **CRC 的独立判据**：见下。

CRC 这件事必须说清楚
--------------------
YMODEM 的块尾校验是 **CRC-16/XMODEM（初值 0x0000）**，标准 check 值
`'123456789' → 0x31C3`。本仓库帧协议用的是 **CRC-16/CCITT-FALSE（初值
0xFFFF）**，同一个输入得 `0x29B1`。两者同多项式、只差初值。

复用 `fota_sender.py::crc16()`（CCITT-FALSE）的后果非常隐蔽：本脚本与设备
之间**完全互通**（两侧错得一样），但与任何一个真实的 YMODEM 软件都不通。
所以：

  · 本模块的 `crc16_xmodem()` 按真源里的 poly/init 逐位实现；
  · 构造 `Ymodem` 时**强制自检** `crc16_xmodem(b'123456789')` 等于真源里
    声明的 check 值，并且等于 `binascii.crc_hqx(b'123456789', 0)`；
  · 设备侧 `drv_fota_ymodem.c::ym_crc16()` 是**另一份**独立实现，两侧在
    L5 里比对。

任何一方把初值改成 0xFFFF，这里立刻炸 —— 这是刻意要的失败。

块长与末块补齐
--------------
YMODEM 的块长恒为 128 或 1024，与文件长度无关；最后一块的尾巴用 `0x1A`
（CPMEOF）填满。接收方**必须**按块 0 声明的长度截断，否则补丁尾部会多出
一串 `0x1A` —— 症状是"所有块 CRC 都对，镜像最后一段是坏的"。

用法：
    python -m generator.fota_ymodem_sender COM4 patch.h2cd
    python -m generator.fota_ymodem_sender COM4 patch.h2cd --block 1024 --name fw.h2cd
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import binascii
except ImportError:                                     # pragma: no cover
    binascii = None

# 格式真源。生成出来的工程根目录下也有一份副本（`ymodem_format.json`），
# 优先用工程里那份 —— 它与该工程固件编译时用的是同一份。
_SPEC_CANDIDATES = (
    Path(__file__).resolve().parent / "data" / "ymodem_format.json",
    Path(__file__).resolve().parent.parent / "ymodem_format.json",
    Path.cwd() / "ymodem_format.json",
)

# YMODEM 的标准 check 输入。**这是判据，不是示例**：
# CRC-16/XMODEM 对它是 0x31C3，CRC-16/CCITT-FALSE 对它是 0x29B1。
CHECK_INPUT = b"123456789"


def load_spec() -> dict:
    """读取格式真源。找不到就**直接失败**，不去猜控制字节。"""
    for cand in _SPEC_CANDIDATES:
        if cand.is_file():
            with open(cand, "r", encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(
        "找不到 ymodem_format.json（找过：%s）——YMODEM 的控制字节与块结构"
        "没有第二份定义，本模块拒绝用内置默认值兜底"
        % ", ".join(str(c) for c in _SPEC_CANDIDATES)
    )


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def crc16_xmodem(data: bytes, poly: int = 0x1021, init: int = 0x0000) -> int:
    """CRC-16/XMODEM：不反射、初值 0x0000、无终值异或。

    与设备侧 `drv_fota_ymodem.c::ym_crc16()` 同一算法，但**是两份独立实现**
    —— 这正是跨实现一致性要被验证的那一点。

    ⚠️ 注意它跟 `fota_sender.py::crc16()`（CRC-16/CCITT-FALSE，初值 0xFFFF）
    不是一个东西。不要"顺手复用"。
    """
    crc = init & 0xFFFF
    for b in data:
        crc ^= (b & 0xFF) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ poly) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


def _crc_oracle(data: bytes) -> int:
    """stdlib 的独立算路：`binascii.crc_hqx(data, 0)` 就是 CRC-16/XMODEM。

    与 `crc16_xmodem()` 是两条完全不同的实现路径（一个逐位、一个查表），
    用它对答案，才能证明"我的实现是对的"而不是"我的实现自洽"。
    """
    if binascii is None:                                # pragma: no cover
        raise RuntimeError("binascii 不可用，无法给出独立判据")
    return binascii.crc_hqx(data, 0) & 0xFFFF


# ---------------------------------------------------------------------------
# 组块（纯函数；L5 测试直接复用）
# ---------------------------------------------------------------------------

class Ymodem:
    """按真源构造 YMODEM 字节流。实例持有从 `ymodem_format.json` 解析出的常量。

    构造时做**三项自检**，任何一项不过就直接抛异常（而不是带着它去发数据）：

      1. 块结构算术：`total_128 == 128 + overhead`、`total_1024 == 1024 + overhead`；
      2. CRC 标准 check 值：本模块的实现 == 真源声明的 check 值；
      3. CRC 独立算路：本模块的实现 == `binascii.crc_hqx`。
    """

    def __init__(self, spec: dict | None = None):
        spec = spec if spec is not None else load_spec()
        self.spec = spec

        ctl = spec["control"]
        blk = spec["block"]
        crc = spec["crc16"]
        hdr = spec["header_block"]
        term = spec["terminator_block"]
        hs = spec["handshake"]

        # ---- 控制字节 ----
        # 真源里每个控制字节是 `{"value": N, "value_hex": ...}` —— 数值字段叫
        # `value`。别写成 `int(ctl["soh"])`：那是对 dict 取 int，报的是
        # "argument must be ... not 'dict'"，看不出是取错字段。
        def _ctl(name: str) -> int:
            return int(ctl[name]["value"])

        self.SOH = _ctl("soh")
        self.STX = _ctl("stx")
        self.EOT = _ctl("eot")
        self.ACK = _ctl("ack")
        self.NAK = _ctl("nak")
        self.CAN = _ctl("can")
        self.PAD = _ctl("pad")                      # 0x1A，末块补齐
        self.CRC_REQUEST = _ctl("crc_request")      # 'C'

        # ---- 块结构 ----
        self.size_128 = int(blk["size_128"])
        self.size_1024 = int(blk["size_1024"])
        self.overhead = int(blk["overhead"])
        self.total_128 = int(blk["total_128"])
        self.total_1024 = int(blk["total_1024"])
        self.modulus = int(blk["number_modulus"])
        self.complement_of = int(blk["complement_of"])

        # ---- CRC ----
        self.crc_poly = int(crc["poly"])
        self.crc_init = int(crc["init"])
        self.crc_check = int(crc["check"])
        self.crc_check_input = crc["check_input"].encode("ascii")

        # ---- 块 0 与结束块 ----
        self.hdr_block_no = int(hdr["block_number"])
        self.hdr_max_name = int(hdr["max_name_len"])
        self.term_block_no = int(term["block_number"])
        self.term_payload_value = int(term["payload_value"])
        self.term_size = int(term["size"])

        # ---- 握手与时序 ----
        self.hs_interval_ms = int(hs["interval_ms"])
        self.hs_max_attempts = int(hs["max_attempts"])
        self.block_timeout_ms = int(hs["block_timeout_ms"])
        self.nak_max_retries = int(hs["nak_max_retries"])
        self.can_before_abort = int(hs["can_count_before_abort"])
        self.crc_after_header_ack = int(hs["crc_after_header_ack"])

        self._self_check()

    @property
    def header_ack_response(self) -> bytes:
        """块 0 被 ACK 之后设备会发回的字节：ACK，**以及紧随的 'C'**。

        'C' 由真源的 `crc_after_header_ack` 派生。它是规范里接收方的固定次序，
        不是可选项 —— 早先这里（和设备侧）都漏了它，自研两侧完全自洽、
        主机测试全绿，只有对接外部发送端时才暴露。见 batch_plan 的注释。
        """
        resp = bytes([self.ACK])
        if self.crc_after_header_ack:
            resp += bytes([self.CRC_REQUEST])
        return resp

    # ---- 自检 --------------------------------------------------------------
    def _self_check(self) -> None:
        if self.total_128 != self.size_128 + self.overhead:
            raise ValueError(
                "ymodem_format.json 自相矛盾：total_128(%d) != size_128(%d) + overhead(%d)"
                % (self.total_128, self.size_128, self.overhead)
            )
        if self.total_1024 != self.size_1024 + self.overhead:
            raise ValueError(
                "ymodem_format.json 自相矛盾：total_1024(%d) != size_1024(%d) + overhead(%d)"
                % (self.total_1024, self.size_1024, self.overhead)
            )

        mine = crc16_xmodem(self.crc_check_input, self.crc_poly, self.crc_init)
        if mine != self.crc_check:
            raise ValueError(
                "CRC 与真源声明的 check 值不符：真源说 %r → 0x%04X，算出来 0x%04X。\n"
                "最常见的原因是把初值写成了 0xFFFF（那是本仓库帧协议的 "
                "CRC-16/CCITT-FALSE）——YMODEM 用的是初值 0x0000 的 XMODEM 变体。"
                "两者对 %r 分别是 0x31C3 与 0x29B1，用错的那一侧与所有真实 "
                "YMODEM 软件都不通。" % (self.crc_check_input, self.crc_check,
                                        mine, self.crc_check_input)
            )

        oracle = _crc_oracle(self.crc_check_input)
        if oracle != self.crc_check:
            raise ValueError(
                "真源声明的 check 值 0x%04X 与 stdlib 的独立算路 0x%04X 不符 —— "
                "这不是本模块的实现错，而是真源本身被改成了别的算法（多项式/"
                "初值/反射位），或者声明值抄错了。" % (self.crc_check, oracle)
            )

    # ---- 组块 --------------------------------------------------------------

    def crc16(self, data: bytes) -> int:
        return crc16_xmodem(data, self.crc_poly, self.crc_init)

    def block(self, blk_no: int, payload: bytes) -> bytes:
        """拼一块：起始(1) + 块号(1) + 块号反码(1) + 载荷 + CRC(2, 高字节在前)。

        载荷长度必须是 128 或 1024 —— 块长与文件长度无关，末块的尾巴由调用方
        用 `0x1A` 补齐（`split_payload` 会做）。
        """
        if len(payload) not in (self.size_128, self.size_1024):
            raise ValueError("块载荷长度必须是 %d 或 %d，收到 %d"
                             % (self.size_128, self.size_1024, len(payload)))
        start = self.SOH if len(payload) == self.size_128 else self.STX
        n = blk_no & 0xFF
        crc = self.crc16(payload)
        return (bytes([start, n, (self.complement_of - n) & 0xFF])
                + payload
                + bytes([(crc >> 8) & 0xFF, crc & 0xFF]))

    def split_payload(self, data: bytes, block_size: int = None) -> list:
        """把文件切成整块，**最后一块用 0x1A 补齐**。返回 [(blk_no, payload)]。

        块号从 1 开始（0 留给文件头），按 256 回绕。
        """
        if block_size is None:
            block_size = self.size_1024
        if block_size not in (self.size_128, self.size_1024):
            raise ValueError("block_size 必须是 %d 或 %d" % (self.size_128, self.size_1024))

        out = []
        n = 1
        pos = 0
        total = len(data)
        while pos < total:
            chunk = data[pos:pos + block_size]
            pos += block_size
            if len(chunk) < block_size:
                chunk = chunk + bytes([self.PAD]) * (block_size - len(chunk))
            out.append((n % self.modulus, chunk))
            n += 1
        if not out:
            # 空文件：协议上只发一个文件头就结束。本仓库永远不会有这种补丁，
            # 但把它显式表达出来，胜过让调用方去处理"out 为空"。
            out = []
        return out

    def header_block(self, name: str, size: int, block_size: int = None) -> bytes:
        """块 0：`<文件名> NUL <十进制长度>`，其余用 NUL 补齐。

        ⚠️ **长度字段是必需的**（协议允许省略）。理由：末块用 `0x1A` 补齐，
        不截断就会把一串 `0x1A` 当成补丁正文；而"去掉尾部 0x1A"这种猜法不
        成立 —— 补丁正文里合法地存在 `0x1A`。所以发送端必须声明长度。

        可选字段（mtime / mode / serial）**不发**：lrzsz 与 BSD sb 的约定互不
        兼容，而接收侧一律不看。
        """
        if block_size is None:
            block_size = self.size_128
        raw = name.encode("utf-8")
        if b"\x00" in raw:
            raise ValueError("文件名不能含 NUL")
        if len(raw) > self.hdr_max_name - 1:
            raise ValueError("文件名超过 %d 字节" % (self.hdr_max_name - 1))
        payload = raw + b"\x00" + str(int(size)).encode("ascii")
        if len(payload) > block_size:
            raise ValueError(
                "块 0 装不下 `文件名 NUL 长度`：%d > %d" % (len(payload), block_size)
            )
        payload = payload + b"\x00" * (block_size - len(payload))
        return self.block(self.hdr_block_no, payload)

    def terminator_block(self, block_size: int = None) -> bytes:
        """结束块 = 载荷全 0 的块 0。"""
        if block_size is None:
            block_size = self.term_size
        return self.block(self.term_block_no,
                          bytes([self.term_payload_value]) * block_size)

    # ---- 流 ----------------------------------------------------------------

    def batch_plan(self, name: str, data: bytes, block_size: int = None) -> list:
        """整批的**逐步计划**：[(要发的字节, 期望的应答字节, 说明)]。

        这个结构是刻意的：串口脚本与 L5 测试台用的是同一份期望，于是"设备
        实际答的和协议应该答的"永远在比同一件事。把它写成"一串字节"让两侧
        各自去切，就是同一份契约的两份实现。

        期望值按设备侧 `drv_fota_ymodem.c` 的行为推出（协议本身只规定了
        单块应答，EOT 那两拍在实现之间有分歧，所以拍数写在真源里）：

          · 块 0            → ACK **紧跟一个 'C'**（邀请第一个数据块）
          · 每个数据块       → ACK
          · 第 1 个 EOT      → NAK（给接收方一次"上一拍是不是噪声"的复核机会）
          · 第 2 个 EOT      → ACK，紧跟一个 'C'（邀请结束块）
          · 结束块           → ACK

        ⚠️ 块 0 之后那个 'C' **不是可选项**：规范里接收方的固定次序就是
        "发 'C' → 收块 0 → ACK → 再发 'C' → 收数据块"。早期这里只写 ACK，
        设备侧也漏发，于是自研两侧完全自洽、所有主机测试全绿，直到接上
        真正的第三方发送端（python-ymodem 包：块 0 被 ACK 后等这个 'C' 等
        60 s 才放弃）才暴露。真机实测见
        `docs/reviews/onboard-capture-2026-09-17.txt`。
        """
        if block_size is None:
            block_size = self.size_1024

        plan = []
        plan.append((self.header_block(name, len(data)),
                     self.header_ack_response,
                     "block 0: %s (%d B)" % (name, len(data))))

        blocks = self.split_payload(data, block_size)
        for blk_no, payload in blocks:
            plan.append((self.block(blk_no, payload),
                         bytes([self.ACK]), "data block %d" % blk_no))

        plan.append((bytes([self.EOT]), bytes([self.NAK]), "EOT #1"))
        plan.append((bytes([self.EOT]),
                     bytes([self.ACK, self.CRC_REQUEST]), "EOT #2 + 'C'"))
        plan.append((self.terminator_block(),
                     bytes([self.ACK]), "terminator block"))
        return plan

    def build_batch(self, name: str, data: bytes, block_size: int = None) -> bytes:
        """整批的上行字节（不包含握手 'C'）。L5 的向量就是它。"""
        return b"".join(send for send, _, _ in self.batch_plan(name, data, block_size))

    def expected_responses(self, name: str, data: bytes, block_size: int = None) -> bytes:
        """整批期间设备**应当**发出的全部字节（按顺序拼起来）。"""
        return b"".join(expect for _, expect, _ in self.batch_plan(name, data, block_size))

    # ---- 负例 --------------------------------------------------------------

    def can_can(self) -> bytes:
        """中止信号：连续两个 CAN。协议规定连续两个才算数。"""
        return bytes([self.CAN]) * self.can_before_abort

    def desync_block(self, blk_no: int, payload: bytes, bogus_no: int) -> bytes:
        """一块**CRC 合法但块号错**的块。

        载荷原样不动 ⇒ 块尾 CRC 不变、依然正确（CRC 只覆盖载荷）。只改块号与
        它的反码字节。这一块的作用是把"块号错乱但链路完好"这个状态精确地造
        出来 —— 它正是"重试计数在 CRC 通过之后被清零"那个缺陷唯一能暴露的
        场景（见 drv_fota_ymodem.c 里 ym_block_handle 的说明）。
        """
        b = bytearray(self.block(blk_no, payload))
        b[1] = bogus_no & 0xFF
        b[2] = (self.complement_of - (bogus_no & 0xFF)) & 0xFF
        return bytes(b)

    def desync_plan(self, name: str, data: bytes, block_size: int = None) -> list:
        """块号持续错乱 ⇒ 重试耗尽 ⇒ 设备必须**主动中止**（NAK 若干次后 CAN CAN）。

        期望值按 `ym_nak()` 的实现推：
          第 n 次失败 → NAK，计数 +1；
          计数一旦 **> nak_max_retries** → 同一拍再发 CAN CAN 并交还 UART。
        所以最后一个 ARG 步骤的期望是 `NAK + CAN CAN`（4 字节）。
        """
        if block_size is None:
            block_size = self.size_1024
        blocks = self.split_payload(data, block_size)
        real_no, payload = blocks[0]
        bad = self.desync_block(real_no, payload, (real_no + 1) % self.modulus)

        plan = [(self.header_block(name, len(data)), self.header_ack_response, "block 0")]
        for i in range(self.nak_max_retries):
            plan.append((bad, bytes([self.NAK]),
                         "desynced block, NAK %d/%d" % (i + 1, self.nak_max_retries)))
        plan.append((bad, bytes([self.NAK]) + self.can_can(),
                     "desynced block -> retry exhausted, abort"))
        return plan

    def dup_plan(self, name: str, data: bytes, block_size: int = None) -> list:
        """重复块：主机没收到 ACK，把**同一块**再发一遍。

        正确的行为是"再 ACK 一次但不再处理" —— 丢弃会让主机一直等不到应答，
        重处理会把同一段数据写两遍（暂存区里那一段就成了两块数据的拼接）。
        """
        if block_size is None:
            block_size = self.size_1024
        plan = [(self.header_block(name, len(data)), self.header_ack_response, "block 0")]
        blocks = self.split_payload(data, block_size)
        for i, (blk_no, payload) in enumerate(blocks):
            plan.append((self.block(blk_no, payload), bytes([self.ACK]),
                         "data block %d" % blk_no))
            if i == 0:
                plan.append((self.block(blk_no, payload), bytes([self.ACK]),
                             "data block %d (duplicate: ACK lost)" % blk_no))
        plan.append((bytes([self.EOT]), bytes([self.NAK]), "EOT #1"))
        plan.append((bytes([self.EOT]),
                     bytes([self.ACK, self.CRC_REQUEST]), "EOT #2 + 'C'"))
        plan.append((self.terminator_block(), bytes([self.ACK]), "terminator block"))
        return plan

    def multi_header_plan(self, name: str, data: bytes, second_name: str,
                          block_size: int = None) -> list:
        """传输途中又冒出一个文件头 ⇒ 设备必须拒绝整个批次。

        本设备的"一批"恒等于一个补丁：第二个文件没有语义。当成数据块处理会把
        文件头的**文本**写进暂存区（补丁里就多了一段 ASCII）。
        """
        if block_size is None:
            block_size = self.size_1024
        blocks = self.split_payload(data, block_size)
        blk_no, payload = blocks[0]
        return [
            (self.header_block(name, len(data)), self.header_ack_response, "block 0"),
            (self.block(blk_no, payload), bytes([self.ACK]), "data block %d" % blk_no),
            (self.header_block(second_name, len(data)), self.can_can(),
             "second header mid-transfer -> reject"),
        ]

    def wrong_length_plan(self, name: str, data: bytes, delta: int,
                          block_size: int = None) -> list:
        """块 0 声明的长度与信封里的 patch_size 对不上 ⇒ 设备必须拒绝。

        这是 YMODEM 侧**唯一**能发现"操作员选错了文件"的地方：块 0 的长度来自
        文件名那个界面，信封里的长度来自补丁本体，两处都声明了同一条记录的长
        度，对不上就说明它们不是同一个东西。
        """
        if block_size is None:
            block_size = self.size_1024
        blocks = self.split_payload(data, block_size)
        blk_no, payload = blocks[0]
        return [
            (self.header_block(name, len(data) + delta), self.header_ack_response,
             "block 0 declares %d (real %d)" % (len(data) + delta, len(data))),
            (self.block(blk_no, payload), self.can_can(),
             "first data block -> length mismatch, reject"),
        ]

    def header_only_plan(self, name: str, data: bytes, block_size: int = None) -> list:
        """只发块 0，然后 CAN CAN 中止（模拟操作员在中途按了取消）。"""
        return [
            (self.header_block(name, len(data)), self.header_ack_response, "block 0"),
            (self.can_can(), self.can_can(), "host cancels at block boundary"),
        ]

    def reject_plan(self, name: str, data: bytes, block_size: int = None) -> list:
        """块 0 收下、**第一个数据块就被拒**的批次。

        准入不过、信封长度不符、续传时正文与已落盘前缀不符 —— 这三种都表现为
        "块 0 正常 ACK，第一个数据块直接 CAN CAN"。原因在设备侧是同一句：
        会话在第一块才打开（准入要等信封），失败就 `ym_abort()`。

        ⚠️ 不要用 `batch_plan()` 去发这种记录 ——  那会让测试期望一路 ACK，
        于是**设备明明正确拒绝了，测试却报错**，很容易被误读成"设备坏了"。
        """
        if block_size is None:
            block_size = self.size_1024
        blocks = self.split_payload(data, block_size)
        blk_no, payload = blocks[0]
        return [
            (self.header_block(name, len(data)), self.header_ack_response, "block 0"),
            (self.block(blk_no, payload), self.can_can(),
             "first data block -> session refused, abort"),
        ]


# ---------------------------------------------------------------------------
# 串口 I/O（一层薄壳，不含任何格式知识）
# ---------------------------------------------------------------------------

def send_ymodem(port: str, name: str, data: bytes,
                baud: int = 115200, block_size: int = None,
                verbose: bool = True) -> int:
    """等设备发 'C'，然后按 `batch_plan()` 逐步收发。返回 0 = 成功。

    设备侧主动发 'C'（接收方驱动握手），所以这里**不**先发 'C' —— 发了反而
    会被设备的"等起始字节"逻辑当成噪声丢掉，然后它继续按自己的节拍重发。
    """
    import time

    try:
        import serial                                     # type: ignore
    except ImportError:
        print("需要 pyserial：pip install pyserial", file=sys.stderr)
        return 2

    ym = Ymodem()
    if block_size is None:
        block_size = ym.size_1024

    timeout_s = ym.block_timeout_ms / 1000.0
    naks = 0

    def log(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    with serial.Serial(port, baud, timeout=timeout_s) as sp:
        log("waiting for the device's 'C' (run `fota ymodem` on the board) ...")
        deadline = time.time() + (ym.hs_interval_ms * ym.hs_max_attempts) / 1000.0
        while True:
            b = sp.read(1)
            if b and b[0] == ym.CRC_REQUEST:
                break
            if time.time() > deadline:
                print("设备一直没发 'C' —— 板上是不是没敲 `fota ymodem`？",
                      file=sys.stderr)
                return 1
        log("device is ready ('C' received)")

        plan = ym.batch_plan(name, data, block_size)
        sent_blocks = 0

        for send, expect, note in plan:
            for attempt in range(ym.nak_max_retries):
                sp.write(send)
                got = sp.read(len(expect))
                if got == expect:
                    naks = 0
                    if note.startswith("data block"):
                        sent_blocks += 1
                    log("  %-24s -> %s  (%d/%d blocks)"
                        % (note, got.hex(" "), sent_blocks, len(plan) - 5))
                    break
                if got[:1] == ym.can_can()[:1] * 1 and got == bytes([ym.CAN]):
                    print("设备发了 CAN —— 它中止了这批（原因见板上 `fota status`）",
                          file=sys.stderr)
                    return 1
                naks += 1
                log("  %-24s -> %s (expected %s), retry %d"
                    % (note, got.hex(" ") or "timeout", expect.hex(" "), naks))
            else:
                print("重试 %d 次仍未得到期望应答，放弃" % ym.nak_max_retries,
                      file=sys.stderr)
                return 1

    log("done: %d bytes (%d data blocks)" % (len(data), sent_blocks))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="通过 YMODEM 把 .h2cd 补丁发给设备（先用板上 `fota ymodem` 进入接收）"
    )
    ap.add_argument("port", help="串口，例如 COM4")
    ap.add_argument("patch", help=".h2cd 补丁文件")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--name", default=None,
                    help="块 0 里声明的文件名（默认用补丁的文件名）")
    ap.add_argument("--block", type=int, default=None,
                    help="数据块长度（128 或 1024，默认 1024）")
    args = ap.parse_args(argv)

    path = Path(args.patch)
    if not path.is_file():
        print("找不到补丁：%s" % path, file=sys.stderr)
        return 2
    data = path.read_bytes()
    name = args.name if args.name else path.name

    ym = Ymodem()
    if args.block is not None and args.block not in (ym.size_128, ym.size_1024):
        print("--block 只能是 %d 或 %d" % (ym.size_128, ym.size_1024), file=sys.stderr)
        return 2

    print("YMODEM: %s -> %s  (%d B, %d 个数据块)"
          % (name, args.port, len(data),
             len(ym.split_payload(data, args.block or ym.size_1024))))
    return send_ymodem(args.port, name, data, baud=args.baud, block_size=args.block)


if __name__ == "__main__":
    sys.exit(main())
