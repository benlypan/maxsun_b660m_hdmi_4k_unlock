#!/usr/bin/env python3
"""
patch_vbt.py — 解除 Intel VBT 中 HDMI hdmi_max_data_rate 限制

按 i915 内核
intel_vbt_defs.h 描述的 VBT/BDB 结构解析, 与 intel_vbt_decode 对齐。

输入是一段 VBT 二进制 (以 b"$VBT " 开头), 不是整个 BIOS。
适用场景:
  - Linux dump: /sys/kernel/debug/dri/0/i915_vbt
  - 已经从 BIOS 抽出来的 Raw section body.bin
  - 任何独立 VBT 二进制

用法:
    python3 patch_vbt.py <vbt.bin> [-o <out.bin>] [--no-decode] [--dry-run]

----- VBT 结构 (i915 vbt_header) -----
    0x00..0x14  signature   "$VBT TIGERLAKE      " 等
    0x14..0x16  version
    0x16..0x18  header_size (恒 0x0030)
    0x18..0x1A  vbt_size    整 VBT 长度 (含 BDB)
    0x1A        vbt_checksum (要求 sum(:vbt_size) % 256 == 0)
    0x1B        reserved
    0x1C..0x20  bdb_offset  (= 0x30)
    0x20..0x30  AIM offsets

----- BDB header (在 vbt + bdb_offset 处) -----
    0x00..0x10  "BIOS_DATA_BLOCK "
    0x10..0x12  version
    0x12..0x14  header_size
    0x14..0x18  bdb_size

----- BDB block header (loop) -----
    0x00        block_id  (8-bit)
    0x01..0x03  block_size (16-bit LE)
    0x03..      block_data

----- BDB Block 2 (General Definitions) data -----
    byte[0]    crt_ddc_gmbus_addr
    byte[1]    flags (DPMS / boot CRT bits)
    byte[2..4] boot_display
    byte[4]    child_dev_size  ← 关键: 每条 child_device_config 长度
    byte[5..]  child_device_config[N], 每条 child_dev_size 字节

----- child_device_config byte 7 -----
    bits 0-4  hdmi_level_shifter_value
    bits 5-7  hdmi_max_data_rate
              0 = 平台默认 (无 VBT 限制)
              1 = 297 MHz   ← 厂商常用的限制
              2 = 165 MHz
              3 = 594 MHz
              4 = 340 MHz
              5 = 300 MHz

注: 对非 HDMI/DVI 子设备, byte 7 的语义被 i915 忽略 (devtype 决定),
    所以全量清零 hdmi_max_data_rate 是安全的。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

VBT_SIG = b"$VBT "
BDB_SIG = b"BIOS_DATA_BLOCK "
GENERAL_DEFS_BLOCK_ID = 2
HDMI_BYTE_OFFSET_IN_CHILD = 7  # bits 5-7 = hdmi_max_data_rate


@dataclass
class VBTHeader:
    signature: str
    version: int
    header_size: int
    vbt_size: int
    checksum: int
    bdb_offset: int


@dataclass
class ChildDevice:
    abs_offset: int           # 整 VBT 内绝对偏移
    handle: int
    device_type: int
    dvo_port: int
    byte7_offset: int         # = abs_offset + 7
    byte7: int


def parse_vbt_header(buf: bytes) -> VBTHeader:
    if len(buf) < 0x30 or not buf.startswith(VBT_SIG):
        raise ValueError(f"不是 VBT (缺 $VBT 签名 / 文件过小, len={len(buf)})")
    header_size = int.from_bytes(buf[0x16:0x18], "little")
    vbt_size = int.from_bytes(buf[0x18:0x1A], "little")
    if header_size != 0x30:
        raise ValueError(f"VBT header_size 异常 (期望 0x30, 实际 {header_size:#x})")
    if not (0 < vbt_size <= len(buf)):
        raise ValueError(f"VBT vbt_size 异常 ({vbt_size}, 文件 {len(buf)} 字节)")
    return VBTHeader(
        signature=bytes(buf[:0x14]).decode("ascii", errors="replace").rstrip(),
        version=int.from_bytes(buf[0x14:0x16], "little"),
        header_size=header_size,
        vbt_size=vbt_size,
        checksum=buf[0x1A],
        bdb_offset=int.from_bytes(buf[0x1C:0x20], "little"),
    )


def find_general_definitions_block(buf: bytes, hdr: VBTHeader) -> tuple[int, int]:
    """返回 (block_data_offset, block_size). block_data 从 block header 之后开始。"""
    bdb_off = hdr.bdb_offset
    if bdb_off + 0x18 > len(buf) or buf[bdb_off:bdb_off + 16] != BDB_SIG:
        raise ValueError(f"在 vbt+{bdb_off:#x} 处没找到 BDB 签名")
    bdb_header_size = int.from_bytes(buf[bdb_off + 0x12:bdb_off + 0x14], "little")
    bdb_size = int.from_bytes(buf[bdb_off + 0x14:bdb_off + 0x18], "little")
    p = bdb_off + bdb_header_size
    end = bdb_off + bdb_size
    while p + 3 <= end:
        block_id = buf[p]
        block_size = int.from_bytes(buf[p + 1:p + 3], "little")
        if block_id == GENERAL_DEFS_BLOCK_ID:
            return p + 3, block_size
        p = p + 3 + block_size
    raise ValueError("BDB 内未找到 Block 2 (General Definitions)")


def parse_child_devices(buf: bytes, hdr: VBTHeader) -> tuple[int, list[ChildDevice]]:
    """解析 BDB Block 2, 返回 (child_dev_size, [ChildDevice...])"""
    block_data, block_size = find_general_definitions_block(buf, hdr)
    if block_size < 5:
        raise ValueError(f"Block 2 太短 ({block_size} 字节)")
    child_dev_size = buf[block_data + 4]
    if child_dev_size < 8:
        raise ValueError(f"child_dev_size={child_dev_size}, 至少要 8 才有 byte 7")
    first_child = block_data + 5
    end = block_data + block_size
    children: list[ChildDevice] = []
    p = first_child
    while p + child_dev_size <= end:
        # i915 child_device_config layout (TGL+/ADL):
        #   0x00 handle (u16)
        #   0x02 device_type (u16)
        #   0x04 dvo_port (在 ADL/TGL 这套结构里位置不同, 我们仅用 byte 7 改 HDMI 位)
        # 为了显示方便, 同时尝试解出 dvo_port (典型偏移 0x10):
        handle = int.from_bytes(buf[p:p + 2], "little")
        devtype = int.from_bytes(buf[p + 2:p + 4], "little")
        dvo_port = buf[p + 0x10] if child_dev_size > 0x10 else 0
        byte7_off = p + HDMI_BYTE_OFFSET_IN_CHILD
        children.append(ChildDevice(
            abs_offset=p,
            handle=handle,
            device_type=devtype,
            dvo_port=dvo_port,
            byte7_offset=byte7_off,
            byte7=buf[byte7_off],
        ))
        p += child_dev_size
    return child_dev_size, children


def patch_buffer(buf: bytearray, children: list[ChildDevice]) -> tuple[list[str], int]:
    """对每个 child 清掉 byte 7 高 3 位 (hdmi_max_data_rate -> 0), 重算 checksum.

    返回 (改动说明列表, 实际改了几个 byte)。
    """
    notes: list[str] = []
    changed = 0
    for c in children:
        old = buf[c.byte7_offset]
        new = old & 0x1F
        if old != new:
            buf[c.byte7_offset] = new
            changed += 1
            notes.append(
                f"  child @ vbt+{c.abs_offset:#06x} "
                f"handle={c.handle:#06x} devtype={c.device_type:#06x} dvo=0x{c.dvo_port:02x}: "
                f"byte7 {old:#04x} -> {new:#04x}  "
                f"(hdmi_max_data_rate {old >> 5} -> 0, lvl_shifter={old & 0x1F})"
            )
    if changed == 0:
        notes.append("  所有 child_device 的 hdmi_max_data_rate 已是 0, 无需修改")

    vbt_size = int.from_bytes(buf[0x18:0x1A], "little")
    old_cs = buf[0x1A]
    buf[0x1A] = 0
    s = sum(buf[:vbt_size]) & 0xFF
    new_cs = (-s) & 0xFF
    buf[0x1A] = new_cs
    if new_cs != old_cs:
        notes.append(f"  byte@0x1A (checksum): {old_cs:#04x} -> {new_cs:#04x}")
    else:
        notes.append(f"  byte@0x1A (checksum): {old_cs:#04x} 不变")

    final = sum(buf[:vbt_size]) & 0xFF
    if final != 0:
        raise RuntimeError(f"checksum 计算后 sum%256 = {final} (应为 0)")
    notes.append("  ✅ sum(:vbt_size) % 256 == 0")
    return notes, changed


def try_decode(path: Path, label: str) -> None:
    decoder = shutil.which("intel_vbt_decode")
    if not decoder:
        print(f"      ⚠️  没找到 intel_vbt_decode (intel-gpu-tools), 跳过 {label} 解码")
        return
    r = subprocess.run([decoder, f"--file={path}"], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"      ❌ intel_vbt_decode {label} 失败 (rc={r.returncode}):")
        for line in (r.stdout + r.stderr).splitlines()[:10]:
            print(f"         {line}")
        return
    print(f"      [intel_vbt_decode] {label} VBT 解码成功")
    cur_dvo = None
    for line in r.stdout.splitlines():
        s = line.strip()
        if "DVO Port:" in s or "DVO port:" in s:
            cur_dvo = s.split(":", 1)[1].strip()
        if "HDMI max data rate" in s:
            rate = s.split(":", 1)[1].strip()
            tag = f" (dvo_port={cur_dvo})" if cur_dvo else ""
            print(f"        HDMI max data rate{tag}: {rate}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="解除 VBT 中所有 child_device 的 HDMI hdmi_max_data_rate 限制",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="按 BDB Block 2 解析定位每条 child_device_config, 不依赖硬偏移。",
    )
    ap.add_argument("vbt", help="待 patch 的 VBT 二进制文件")
    ap.add_argument("-o", "--output", help="输出路径 (默认 <vbt>.patched)")
    ap.add_argument("--no-decode", action="store_true",
                    help="跳过 intel_vbt_decode 验证步骤")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析显示, 不写文件")
    args = ap.parse_args()

    src = Path(args.vbt).resolve()
    if not src.is_file():
        sys.exit(f"❌ 找不到文件: {src}")
    out = Path(args.output).resolve() if args.output else src.with_suffix(src.suffix + ".patched")

    raw = src.read_bytes()
    print(f"[1/4] 读取 {src.name} ({len(raw)} 字节)")
    try:
        hdr = parse_vbt_header(raw)
    except ValueError as e:
        sys.exit(f"❌ {e}")
    print(f"      signature : {hdr.signature!r}")
    print(f"      version   : {hdr.version}")
    print(f"      vbt_size  : {hdr.vbt_size} ({hdr.vbt_size:#x})")
    print(f"      checksum  : {hdr.checksum:#04x}  (当前 sum%256 = {sum(raw[:hdr.vbt_size]) & 0xFF})")
    print(f"      bdb_offset: {hdr.bdb_offset:#x}")

    try:
        child_dev_size, children = parse_child_devices(raw, hdr)
    except ValueError as e:
        sys.exit(f"❌ 解析 BDB Block 2 失败: {e}")
    print(f"      BDB Block 2: child_dev_size={child_dev_size}, 共 {len(children)} 个 child_device")
    for c in children:
        rate = c.byte7 >> 5
        rate_label = {0: "platform max", 1: "297 MHz", 2: "165 MHz",
                      3: "594 MHz", 4: "340 MHz", 5: "300 MHz"}.get(rate, f"reserved({rate})")
        print(f"        @{c.abs_offset:#06x} handle={c.handle:#06x} "
              f"devtype={c.device_type:#06x} dvo=0x{c.dvo_port:02x} "
              f"byte7={c.byte7:#04x} -> hdmi_max_data_rate={rate_label}")

    if not args.no_decode:
        try_decode(src, "原始")

    print(f"[2/4] Patch {len(children)} 个 child_device 的 byte 7 高 3 位 ...")
    buf = bytearray(raw)
    notes, changed = patch_buffer(buf, children)
    for n in notes:
        print(n)

    if args.dry_run:
        print(f"[3/4] --dry-run, 不写文件; 本次将改 {changed} 处 byte")
        return 0

    print(f"[3/4] 写入 {out}")
    out.write_bytes(buf)

    if not args.no_decode:
        try_decode(out, "patched")

    print(f"[4/4] 完成 ✅  {out}  (改了 {changed} 处 byte + 1 处 checksum)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
