"""已知 PlayStation 手柄的 VID / PID 表。

全部来自索尼官方 USB 分配（Vendor ID 0x054C），并对照 Linux 内核
``drivers/hid/hid-playstation.c`` 与 ``hid-ids.h`` 中的条目。

注意：**XInput 完全无法读取 PlayStation 手柄电量**（XInput 的
``XINPUT_GAMEPAD`` 结构里根本没有电池字段，``XInputGetBatteryInformation``
只对 Xbox 手柄有效）。这也是为什么必须绕过 XInput、直接走 HID 报告。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .models import FAMILY_DS4, FAMILY_DUALSENSE

SONY_VENDOR_ID = 0x054C


@dataclass(frozen=True)
class DeviceSpec:
    family: str
    name: str


# 只收录 PS4 / PS5 手柄本体。
# 曾经还收录过 PS3 的 DualShock 3、PS Move、以及 PS4 官方无线适配器
# （CUH-ZWA1, 0x0BA0）—— 前两者协议里根本没有电量字段（只能识别不能读），
# 适配器已停产且我们手头没有硬件可验证。1.6 起一并移除，代码里那套
# "识别但不读电量"的分支也随之删掉。
KNOWN_DEVICES: Dict[Tuple[int, int], DeviceSpec] = {
    # ---- DualShock 4 (PS4) ----
    (SONY_VENDOR_ID, 0x05C4): DeviceSpec(FAMILY_DS4, "DualShock 4 (CUH-ZCT1)"),
    (SONY_VENDOR_ID, 0x09CC): DeviceSpec(FAMILY_DS4, "DualShock 4 (CUH-ZCT2)"),
    # ---- DualSense (PS5) ----
    (SONY_VENDOR_ID, 0x0CE6): DeviceSpec(FAMILY_DUALSENSE, "DualSense (PS5)"),
    (SONY_VENDOR_ID, 0x0DF2): DeviceSpec(FAMILY_DUALSENSE, "DualSense Edge (PS5)"),
}

# 关键报告 ID / 大小常量（对照 hid-playstation.c）
DS4_INPUT_REPORT_USB = 0x01
DS4_INPUT_REPORT_USB_SIZE = 64
DS4_INPUT_REPORT_BT_MINIMAL = 0x01
DS4_INPUT_REPORT_BT_MINIMAL_SIZE = 10
DS4_INPUT_REPORT_BT = 0x11
DS4_INPUT_REPORT_BT_SIZE = 78

DS_INPUT_REPORT_USB = 0x01
DS_INPUT_REPORT_USB_SIZE = 64
DS_INPUT_REPORT_BT = 0x31
DS_INPUT_REPORT_BT_SIZE = 78


def lookup(vendor_id: int, product_id: int) -> Optional[DeviceSpec]:
    return KNOWN_DEVICES.get((int(vendor_id), int(product_id)))


def is_sony(vendor_id: int) -> bool:
    return int(vendor_id) == SONY_VENDOR_ID


def known_pairs() -> List[Tuple[int, int]]:
    return list(KNOWN_DEVICES.keys())


def expected_report_ids(family: str, transport_label: str) -> Tuple[int, ...]:
    """给日志/排障用的「期望报告 ID」提示。"""
    if family == FAMILY_DUALSENSE:
        return (DS_INPUT_REPORT_USB,) if transport_label == "USB" else (DS_INPUT_REPORT_BT,)
    return (DS4_INPUT_REPORT_USB,) if transport_label == "USB" else (DS4_INPUT_REPORT_BT_MINIMAL, DS4_INPUT_REPORT_BT)
