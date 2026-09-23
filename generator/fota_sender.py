#!/usr/bin/env python3
"""FOTA 主机侧发送端（Hardware2Code）。

通过串口把 `.h2cd` 差分补丁发给设备上的 `drv_fota` 接收侧状态机。

为什么这个文件的结构是"纯函数 + 一层薄薄的串口 I/O"
------------------------------------------------------
帧的每一个字节都由 `generator/data/fota_format.json` 的 `transport` 段派生，
**不在这里写任何字面量标记或偏移**。设备侧（`templates/drivers/drv_fota.c.j2`）
读同一份真源。

这不是洁癖。上一版这个文件实现的是**已经退役的协议**（START = `0xF0 0x0A`、
分片 CRC 只覆盖 payload），而设备侧在 P3 换成了 `0xA5/0xA4/0xA6` 三帧制、
CRC 覆盖 `seq|len|data`。两侧各写一份的必然结局就是这种"编译全绿、链路静默
失效"——正是本仓库记为 A3 的那一类。所以：

  · 帧构造（`build_*`）是纯函数，不碰串口 —— 于是 L5 协议测试可以直接拿
    它们当**发送端真值**，与设备侧接收实现做跨实现比对；
  · 串口循环（`send_fota`）只负责"发一帧、等一个应答"，不含任何格式知识。

设备侧对应的实现在 `templates/drivers/drv_fota.c.j2`；两侧一起跑在
`generator/tests/test_fota_protocol_l5.py` 里。

用法：
    python fota_sender.py COM4 patch.h2cd [--baud 115200]
"""

import argparse
import json
import struct
import sys
import time
from pathlib import Path

# 格式真源。生成出来的工程根目录下也有一份副本（`fota_format.json`），
# 优先用工程里那份 —— 它与该工程固件编译时用的是同一份。
_SPEC_CANDIDATES = (
    Path(__file__).resolve().parent / "data" / "fota_format.json",
    Path(__file__).resolve().parent.parent / "fota_format.json",
    Path.cwd() / "fota_format.json",
)

MAX_RETRIES = 3
ACK_TIMEOUT = 2.0  # 秒；与真源里的 ack_timeout_ms 同量级


def load_spec() -> dict:
    """读取格式真源。找不到就**直接失败**，不去猜偏移。"""
    for cand in _SPEC_CANDIDATES:
        if cand.is_file():
            with open(cand, "r", encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(
        "找不到 fota_format.json（找过：%s）——帧格式没有第二份定义，"
        "本模块拒绝用内置默认值兜底" % ", ".join(str(c) for c in _SPEC_CANDIDATES)
    )


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE（初值 0xFFFF、不反射、无终值异或）。

    与设备侧 `drv_fota.c::fota_crc16()`、`fota_delta.c::crc16_ccitt_false()`
    同一算法。三处实现不是冗余：这正是"跨实现一致"要被验证的那一点 ——
    主机算、设备验，任何一侧写错都会在 L5 里红。
    """
    crc = 0xFFFF
    for b in data:
        crc ^= (b & 0xFF) << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def crc32(data: bytes) -> int:
    """`finish` 帧里的 patch_crc32。

    与设备侧 `fota_delta_crc32_update()`（反射多项式 0xEDB88320、初值/终值
    异或 0xFFFFFFFF）逐位等价，也就是 `zlib.crc32()`。
    注意它与**镜像头**里的 `crc32`（STM32 硬件 CRC，非反射 0x04C11DB7，
    见 `generator/patch_crc.py`）不是同一个东西 —— 名字像，用途完全不同。
    """
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# 帧构造（纯函数；L5 测试直接复用）
# ---------------------------------------------------------------------------

class Framing:
    """按真源构造帧。实例持有从 `fota_format.json` 解析出的标记与长度。"""

    def __init__(self, spec: dict | None = None):
        spec = spec if spec is not None else load_spec()
        t = spec["transport"]
        self.env_size = int(spec["delta_envelope"]["size"])
        self.marker_start = int(t["start"]["value"])
        self.marker_data = int(t["data"]["value"])
        self.marker_finish = int(t["finish"]["value"])
        self.ack = int(t["ack"]["value"])
        self.nak = int(t["nak"]["value"])
        self.chunk_size = int(t["chunk_size"])
        self.ack_timeout_ms = int(t["ack_timeout_ms"])
        self.start_total = int(t["start"]["total_size"])
        self.finish_total = int(t["finish"]["total_size"])
        self.resp_total = int(t["ack"]["total_size"])

        # 「掉电免重传」的容量上限（FR-14.9）：元数据页能记多少个分片。
        # 与设备侧 `FOTA_MAX_RESUMABLE_BYTES` 同源（都来自 `metadata.journal`）。
        journal = spec["metadata"]["journal"]
        self.meta_slots = int(journal["record_count"]) - int(journal["fixed_slots"])

        # 自检：真源里声明的长度必须与算式一致。写错一处就会让**所有**帧
        # 都少/多几个字节 —— 那是"能发出去、设备永远收不齐"的形态。
        if self.start_total != 1 + self.env_size + 2:
            raise ValueError("transport.start.total_size 与 1 + env_size + 2 不符")
        if self.finish_total != 1 + 4:
            raise ValueError("transport.finish.total_size 与 1 + 4 不符")
        if self.resp_total != 1 + 2:
            raise ValueError("transport.ack.total_size 与 1 + 2 不符")
        if self.marker_start in (self.marker_data, self.marker_finish) \
                or self.marker_data == self.marker_finish:
            raise ValueError("帧标记互不相等是解析器分派的前提")

    # -- 帧 ----------------------------------------------------------------

    def build_start(self, env: bytes) -> bytes:
        if len(env) != self.env_size:
            raise ValueError("信封长度 %d 与真源 %d 不符" % (len(env), self.env_size))
        return bytes([self.marker_start]) + env + struct.pack("<H", crc16(env))

    def build_data(self, seq: int, payload: bytes) -> bytes:
        if not payload or len(payload) > self.chunk_size:
            raise ValueError("分片长度 %d 超出 1..%d" % (len(payload), self.chunk_size))
        body = struct.pack("<HH", seq & 0xFFFF, len(payload)) + payload
        # CRC 覆盖 seq|len|data：与设备侧一致。只覆盖 payload 时，被打坏的
        # seq/len 会静默通过，而正是这两个字段驱动重传与写入定位。
        return bytes([self.marker_data]) + body + struct.pack("<H", crc16(body))

    def build_finish(self, patch_crc32: int) -> bytes:
        return bytes([self.marker_finish]) + struct.pack("<I", patch_crc32 & 0xFFFFFFFF)

    def build_ack(self, next_seq: int) -> bytes:
        return bytes([self.ack]) + struct.pack("<H", next_seq & 0xFFFF)

    def build_nak(self, want_seq: int) -> bytes:
        return bytes([self.nak]) + struct.pack("<H", want_seq & 0xFFFF)

    # -- 整流 --------------------------------------------------------------

    def resume_budget(self) -> tuple:
        """「掉电免重传」的容量上限 `(可用记录条数, 可用字节数)`。

        设备侧会在 START 阶段拒掉越界的补丁（`FOTA_E_META_FULL`）。主机在
        **发出第一个字节之前**就判出同一件事，现场看到的就是一句明确的
        "补丁太大"，而不是"设备不回 ACK、等到超时" —— 后者的原因要翻设备
        日志才知道（FR-14.9）。
        """
        return self.meta_slots, self.meta_slots * self.chunk_size

    def split_chunks(self, patch: bytes) -> list:
        """把补丁切成**数据分片**：跳过前 48 B 信封（信封由 START 帧携带）。

        分片大小是 `chunk_size`，最后一片可以不满 —— 与设备侧
        `g_expected_seq` 的推进方式一致（每片 +1，直到 FINISH）。
        """
        if len(patch) <= self.env_size:
            raise ValueError("补丁只有 %d 字节，连信封都装不下" % len(patch))
        body = patch[self.env_size:]
        return [body[i:i + self.chunk_size]
                for i in range(0, len(body), self.chunk_size)]

    def build_stream(self, patch: bytes) -> bytes:
        """一条完整的发送字节流：START + DATA×n + FINISH。

        没有帧间分隔符 —— 接收端按"标记 → 定长/变长载荷 → 尾校验"推进。
        L5 用它一次性构造输入，于是"发送端与接收端对同一份契约的理解"被
        真正地端到端验证，而不是两侧各自对着自己的实现自洽。
        """
        chunks = self.split_chunks(patch)
        out = bytearray(self.build_start(patch[:self.env_size]))
        for seq, chunk in enumerate(chunks):
            out += self.build_data(seq, chunk)
        out += self.build_finish(crc32(patch))
        return bytes(out)

    def expected_responses(self, patch: bytes) -> bytes:
        """一条**健康**传输应当收到的完整应答流。

        约定：ACK 携带"我要的下一片序号"，所以
            START        → ACK(0)
            DATA k       → ACK(k+1)
            FINISH       → ACK(n)      （n = 分片数）
        也就是序号序列 [0, 1, ..., n, n]。把它烘成向量交给 L5 逐字节比对，
        比在 C 里再写一遍期望逻辑可靠得多。
        """
        n = len(self.split_chunks(patch))
        seqs = list(range(n + 1)) + [n]
        return b"".join(self.build_ack(s) for s in seqs)


# ---------------------------------------------------------------------------
# 串口发送
# ---------------------------------------------------------------------------

def send_fota(port: str, patch_path: str, baud: int = 115200,
              spec: dict | None = None):
    """把补丁发给设备。停等协议：每帧发完等一个应答再发下一帧。"""
    try:
        import serial
    except ImportError:
        print("ERROR: pyserial not installed. Run: pip install pyserial")
        sys.exit(1)

    patch = Path(patch_path).read_bytes()
    fr = Framing(spec)
    chunks = fr.split_chunks(patch)

    # FR-14.9：在发出第一个字节之前判一次"续传上限"。设备侧也会判，但那里
    # 的表现只是"START 没有应答" —— 主机要等满 ACK_TIMEOUT 才知道，且看不
    # 出原因。这里失败，信息才是完整的。
    slots, max_bytes = fr.resume_budget()
    body_bytes = len(patch) - fr.env_size
    if body_bytes > max_bytes:
        print("ERROR: 补丁正文 %d B 超出「掉电免重传」上限 %d B（%d 个分片 × %d B）"
              % (body_bytes, max_bytes, slots, fr.chunk_size))
        print("  设备会在 START 阶段拒收（FOTA_E_META_FULL）：元数据页记不住")
        print("  这么多分片，掉电续传无法保证。改用差分补丁，或放宽提交粒度。")
        sys.exit(1)

    print(f"Patch file: {len(patch)} bytes")
    print(f"Chunks: {len(chunks)} × ≤{fr.chunk_size} bytes")
    print(f"Serial: {port} @ {baud} baud")
    print()

    try:
        ser = serial.Serial(port, baudrate=baud, timeout=ACK_TIMEOUT)
    except serial.SerialException as e:
        print(f"ERROR: Cannot open {port}: {e}")
        sys.exit(1)

    def wait_resp(what: str):
        """读一个应答帧。返回 (kind, seq)；kind ∈ {'ack','nak',None}。"""
        resp = ser.read(fr.resp_total)
        if len(resp) < fr.resp_total:
            print(f"  TIMEOUT waiting {what} ACK")
            return None, None
        kind = "ack" if resp[0] == fr.ack else ("nak" if resp[0] == fr.nak else "?")
        seq = struct.unpack("<H", resp[1:3])[0]
        return kind, seq

    retries = 0

    # ---- START ----
    print("START ... ", end="", flush=True)
    ser.write(fr.build_start(patch[:fr.env_size]))
    kind, seq = wait_resp("START")
    if kind != "ack":
        # 设备在 START 阶段拒绝时**故意不应答**（见 drv_fota.c 的
        # fota_reject_start），所以这里的超时是"补丁被拒"的正常表现。
        print(f"REJECTED/NO-ACK ({kind}) —— 补丁未通过设备的格式或容量准入")
        ser.close()
        sys.exit(2)
    print(f"ACK next_seq={seq}")
    next_seq = seq

    # ---- DATA：停等。NAK 里带的 want_seq 就是设备要的下一片，直接回退到它。
    # 这比"记录 last_good 再 +1"更直接：设备的期望值就是唯一真值。
    while next_seq < len(chunks):
        payload = chunks[next_seq]
        print(f"  DATA {next_seq:4d}  len={len(payload):4d}  ... ", end="", flush=True)
        ser.write(fr.build_data(next_seq, payload))
        kind, seq = wait_resp("DATA")
        if kind == "ack":
            print("ACK")
            next_seq = seq
            retries = 0
        elif kind == "nak":
            print(f"NAK want_seq={seq}")
            next_seq = seq
            retries += 1
            if retries > MAX_RETRIES:
                print(f"ERROR: exceeded max retries ({MAX_RETRIES})")
                ser.close()
                sys.exit(1)
        else:
            print("TIMEOUT/unexpected")
            retries += 1
            if retries > MAX_RETRIES:
                print(f"ERROR: exceeded max retries ({MAX_RETRIES})")
                ser.close()
                sys.exit(1)
        time.sleep(0.01)

    # ---- FINISH ----
    print("FINISH ... ", end="", flush=True)
    ser.write(fr.build_finish(crc32(patch)))
    kind, seq = wait_resp("FINISH")
    if kind == "ack":
        print("ACK —— 设备已校验暂存区，即将应用")
    else:
        print(f"NOT OK ({kind}, want_seq={seq}) —— 暂存区内容与 patch_crc32 不符")
        ser.close()
        sys.exit(1)

    ser.close()


def main():
    parser = argparse.ArgumentParser(
        description='Send a .h2cd differential patch to an STM32G0 device')
    parser.add_argument('port', help='Serial port (e.g., COM4, /dev/ttyUSB0)')
    parser.add_argument('patch', help='Patch file (.h2cd)')
    parser.add_argument('--baud', type=int, default=115200,
                        help='Baud rate (default: 115200)')
    parser.add_argument('--chunk-size', type=int, default=None,
                        help='Override the chunk size from fota_format.json')
    args = parser.parse_args()

    if not Path(args.patch).exists():
        print(f"ERROR: Patch file not found: {args.patch}")
        sys.exit(1)

    if args.chunk_size is not None:
        spec = load_spec()
        spec["transport"]["chunk_size"] = int(args.chunk_size)
        send_fota(args.port, args.patch, args.baud, spec=spec)
    else:
        send_fota(args.port, args.patch, args.baud)


if __name__ == '__main__':
    main()
