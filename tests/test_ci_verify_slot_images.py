"""tools/ci/verify_slot_images.py 的判据有效性测试。

为什么不是"跑一遍正常路径就完事"
--------------------------------
这个脚本的全部价值在于**抓得住**错误：CRC 只覆盖文件本身、与链接基址无关，
所以"给槽 B 种了按槽 A 链接的镜像"在校验环节看不出来。一条只跑正向用例的
测试，在判据被写坏（区间算反、解析失败就 pass）时照样全绿 —— 那是最糟的
一种绿。所以这里每个反例都必须真的返回非零。
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "ci" / "verify_slot_images.py"

SLOT_A_LD = textwrap.dedent("""
    MEMORY
    {
      FLASH (rx)  : ORIGIN = 0x08002000, LENGTH = 248K
      RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 144K
    }
""")

SLOT_B_LD = textwrap.dedent("""
    MEMORY
    {
      FLASH (rx)  : ORIGIN = 0x08040000, LENGTH = 256K
      RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 144K
    }
""")

BOOT_LD = textwrap.dedent("""
    MEMORY
    {
      FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 8K
      RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 144K
    }
""")


def _bin(reset_handler: int, extra: int = 0) -> bytes:
    """造一个只含向量表前两字的镜像：MSP + Reset_Handler (+ 填充)。"""
    import struct
    return struct.pack("<II", 0x20004000, reset_handler) + b"\x00" * extra


def _layout(tmp_path, reset_a, reset_b, combined_size=40960,
            a_ld=SLOT_A_LD, b_ld=SLOT_B_LD, boot_ld=BOOT_LD):
    (tmp_path / "linker").mkdir(exist_ok=True)
    (tmp_path / "linker" / "app_slot_a.ld").write_text(a_ld, encoding="utf-8")
    (tmp_path / "linker" / "app_slot_b.ld").write_text(b_ld, encoding="utf-8")
    (tmp_path / "linker" / "bootloader.ld").write_text(boot_ld, encoding="utf-8")
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build_b").mkdir(exist_ok=True)
    (tmp_path / "build" / "demo.bin").write_bytes(_bin(reset_a, 64))
    (tmp_path / "build_b" / "demo.bin").write_bytes(_bin(reset_b, 64))
    (tmp_path / "build" / "combined.bin").write_bytes(b"\x00" * combined_size)
    return [
        "--slot-a", "build/demo.bin",
        "--slot-b", "build_b/demo.bin",
        "--linker-a", "linker/app_slot_a.ld",
        "--linker-b", "linker/app_slot_b.ld",
        "--bootloader-ld", "linker/bootloader.ld",
        "--combined", "build/combined.bin",
    ]


def _run(tmp_path, argv):
    return subprocess.run([sys.executable, str(SCRIPT)] + argv,
                          cwd=str(tmp_path), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)


def test_accepts_two_images_linked_for_their_own_slot(tmp_path):
    """正控：两个槽各自按自己的基址链接 ⇒ 通过。"""
    argv = _layout(tmp_path, 0x08005DE5, 0x08043DE5)  # 真机上实测到的两个入口
    p = _run(tmp_path, argv)
    assert p.returncode == 0, p.stdout
    assert "OK" in p.stdout


def test_rejects_slot_b_holding_a_slot_a_image(tmp_path):
    """核心反例：槽 B 里放的是按槽 A 链接的镜像（CRC 看不出的那一种）。"""
    argv = _layout(tmp_path, 0x08005DE5, 0x08005DE5)
    p = _run(tmp_path, argv)
    assert p.returncode != 0, "判据放过了『槽 B 装了槽 A 版本』—— 这正是它唯一要抓的东西"
    assert "槽 B" in p.stdout


def test_two_slots_with_the_same_entry_point_is_caught_by_the_slot_b_check(tmp_path):
    """两个镜像入口一模一样 ⇒ 至少有一个不在自己的槽里，必须失败。

    （区间互不重叠时"入口相同"本身不可能出现，所以这里命中槽 B 那条判据，
    而不是某条永远不可达的 `va == vb`。）
    """
    argv = _layout(tmp_path, 0x08005DE5, 0x08005DE5)
    p = _run(tmp_path, argv)
    assert p.returncode != 0
    assert "槽 B" in p.stdout


def test_rejects_image_outside_any_slot(tmp_path):
    """入口落在 flash 之外（比如按 0 基址链接）也要被抓住。"""
    argv = _layout(tmp_path, 0x00000101, 0x08043DE5)
    p = _run(tmp_path, argv)
    assert p.returncode != 0
    assert "槽 A" in p.stdout


def test_missing_flash_region_in_linker_script_fails_loudly(tmp_path):
    """链接脚本解析不出 FLASH ⇒ 必须失败，不许降级成"跳过检查"。

    静默出口会把"判据没生效"伪装成"判据通过了"。
    """
    broken = "MEMORY\n{\n  ROM (rx) : ORIGIN = 0x00000000, LENGTH = 64K\n}\n"
    argv = _layout(tmp_path, 0x08005DE5, 0x08043DE5, a_ld=broken)
    p = _run(tmp_path, argv)
    assert p.returncode != 0, "解析失败却放行了 —— 这就是静默出口"
    assert "FLASH" in p.stdout


def test_rejects_combined_image_without_the_app(tmp_path):
    """combined.bin 只有引导器区大小 ⇒ 说明 app 没拼进去。"""
    argv = _layout(tmp_path, 0x08005DE5, 0x08043DE5, combined_size=8192)
    p = _run(tmp_path, argv)
    assert p.returncode != 0
    assert "combined" in p.stdout


def test_rejects_overlapping_slot_regions(tmp_path):
    """两个槽完全同基址 ⇒ 差分 OTA 会直接覆盖自己的旧镜像。"""
    argv = _layout(tmp_path, 0x08005DE5, 0x08005DE9, b_ld=SLOT_A_LD)
    p = _run(tmp_path, argv)
    assert p.returncode != 0
    assert "重叠" in p.stdout


def test_rejects_partially_overlapping_slot_regions(tmp_path):
    """只是相交（不是完全重合）也要抓 —— 部分重叠更容易被写成"看起来能跑"。"""
    partial = SLOT_B_LD.replace("0x08040000", "0x08010000")  # [0x08010000, 0x08050000)
    argv = _layout(tmp_path, 0x08005DE5, 0x08013000, b_ld=partial)
    p = _run(tmp_path, argv)
    assert p.returncode != 0
    assert "重叠" in p.stdout


def test_missing_image_file_fails_loudly(tmp_path):
    argv = _layout(tmp_path, 0x08005DE5, 0x08043DE5)
    (tmp_path / "build_b" / "demo.bin").unlink()
    p = _run(tmp_path, argv)
    assert p.returncode != 0
    assert "读不到" in p.stdout
