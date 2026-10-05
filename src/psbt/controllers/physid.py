r"""把 HID 集合的路径映射到「它属于哪个物理设备」。

为什么需要这个模块
------------------
一个手柄在 Windows 上会暴露**多个 HID 顶层集合**，例如 DualSense::

    \\?\HID#VID_054C&PID_0CE6&MI_03#9&22b58c18&0&0000#{...}        ← 主游戏控制器
    \\?\HID#VID_054C&PID_0CE6&MI_04&Col01#9&6a2d811&0&0000#{...}\KBD ← 触控板:键盘
    \\?\HID#VID_054C&PID_0CE6&MI_04&Col02#9&6a2d811&0&0001#{...}    ← 触控板:消费类
    \\?\HID#VID_054C&PID_0CE6&MI_04&Col03#9&6a2d811&0&0002#{...}    ← 触控板:鼠标

注意后三个的**接口段**（``MI_04&Col01/02/03``）不同，**实例段**（``&0000/0001/0002``）
也不同 —— 它们其实是同一个物理设备的不同接口，但在设备树里是各自独立的
devnode。只按 HID 路径的实例段分组，就会把**一个手柄显示成四台**：
一台读到电量、另外三台"电量未知"。

（这是真机实测出来的：USB 无线适配器 / 复合设备最容易触发。
原来的 ``_device_instance`` 只处理了 ``&Col`` 出现在实例段的情况，这里不是。）

做法
----
沿设备树的**父设备链**往上走，找到第一个不带 ``&MI_`` 的 USB 设备 —— 它就是这
组集合共同的物理设备::

    HID\...MI_03\...        ┐
    HID\...MI_04&Col01\...  ├─→ USB\VID_054C&PID_0CE6\1C784B8B409B0000  ← 手柄本体
    HID\...MI_04&Col02\...  │        ↑ USB\VID_1A40&PID_0101\...（HUB，不能再往上）
    HID\...MI_04&Col03\...  ┘

那串实例（``1C784B8B409B0000``）是设备自己的序列号，**手柄之间互不相同**，
所以两只同型号手柄仍然各自成组，不会并成一个。

用不到 / 拿不到时（蓝牙连接、cfgmgr32 不可用、设备树查询失败）返回 ``None``，
调用方回退到原来的按 HID 实例段分组 —— 行为与旧版一致，不会更差。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Dict, Optional

from ..logs import get_logger

log = get_logger("physid")

_CR_SUCCESS = 0
_MAX_DEPTH = 6              # HID → USB 接口 → USB 设备 → HUB → ROOT_HUB，留足余量

_cfgmgr = None
_cfgmgr_broken = False
_cache: Dict[bytes, Optional[str]] = {}


def _load():
    """加载并定型 cfgmgr32；不可用时返回 None（只尝试一次，失败后不再重试）。"""
    global _cfgmgr, _cfgmgr_broken
    if _cfgmgr is not None or _cfgmgr_broken:
        return _cfgmgr
    try:
        cm = ctypes.WinDLL("cfgmgr32", use_last_error=True)
        cm.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                          ctypes.c_wchar_p, wintypes.ULONG]
        cm.CM_Locate_DevNodeW.restype = wintypes.ULONG
        cm.CM_Get_Parent.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                     wintypes.DWORD, wintypes.ULONG]
        cm.CM_Get_Parent.restype = wintypes.ULONG
        cm.CM_Get_Device_ID_Size.argtypes = [ctypes.POINTER(wintypes.ULONG),
                                             wintypes.DWORD, wintypes.ULONG]
        cm.CM_Get_Device_ID_Size.restype = wintypes.ULONG
        cm.CM_Get_Device_IDW.argtypes = [wintypes.DWORD, ctypes.c_wchar_p,
                                         wintypes.ULONG, wintypes.ULONG]
        cm.CM_Get_Device_IDW.restype = wintypes.ULONG
        _cfgmgr = cm
    except Exception as exc:                    # 非 Windows / 异常环境
        log.debug("cfgmgr32 不可用，物理设备分组将回退到 HID 实例段：%s", exc)
        _cfgmgr_broken = True
    return _cfgmgr


def hid_instance_id(path) -> Optional[str]:
    """把 HID 设备路径转成设备树认得的实例 ID。

    ``\\\\?\\HID#VID_054C&PID_0CE6&MI_04&Col01#9&6a2d811&0&0000#{guid}\\KBD``
    -> ``HID\\VID_054C&PID_0CE6&MI_04&Col01\\9&6a2d811&0&0000``
    """
    try:
        text = (path.decode("utf-8", "replace")
                if isinstance(path, (bytes, bytearray)) else str(path))
    except Exception:
        return None
    parts = [p for p in text.split("#") if p]
    if len(parts) < 3:
        return None
    if not parts[0].rstrip("\\?").lower().endswith("hid"):
        return None
    return "HID\\%s\\%s" % (parts[1], parts[2])


def _device_id(cm, devinst: int) -> Optional[str]:
    size = wintypes.ULONG(0)
    if cm.CM_Get_Device_ID_Size(ctypes.byref(size), devinst, 0) != _CR_SUCCESS:
        return None
    buf = ctypes.create_unicode_buffer(size.value + 2)
    if cm.CM_Get_Device_IDW(devinst, buf, size.value + 1, 0) != _CR_SUCCESS:
        return None
    return buf.value


def _is_physical_usb_device(device_id: str) -> bool:
    """父设备链上，哪一个才算"手柄本体"。

    ``USB\\VID_054C&PID_0CE6\\1C784B8B409B0000``          -> 是（不带 &MI_）
    ``USB\\VID_054C&PID_0CE6&MI_04\\8&1DF12F96&1&0004``   -> 否（这是接口）
    ``USB\\VID_1A40&PID_0101\\6&28CF390B&0&12``           -> 否（这是 USB HUB）
    """
    if not _is_usb_node(device_id):
        return False
    parts = device_id.split("\\")
    if len(parts) < 3:
        return False
    hardware_id = parts[1].upper()
    if "&MI_" in hardware_id:
        return False                 # 复合设备的某个接口，不是设备本体
    # HUB 本身也是个"不带 &MI_ 的 USB 节点"（ROOT_HUB30、各种 HUB 芯片）。
    # 正常情况爬不到它（真设备的节点在它前面），这里是防线，避免把
    # 整台机器上的设备都归到同一个 HUB 上。
    if "HUB" in hardware_id or hardware_id.startswith("VID_1A40"):
        return False
    return True


def _is_usb_node(device_id: str) -> bool:
    return bool(device_id) and device_id.upper().startswith("USB\\")


def physical_device_id(path) -> Optional[str]:
    """返回该 HID 集合所属**物理设备**的实例 ID；拿不到返回 ``None``。

    结果按路径缓存（重插会换路径，缓存自然失效）。

    .. important::
       **只允许在 USB 枚举器内往上爬。** 一旦某个祖先是别的枚举器
       （``BTHENUM`` / ``BTHLE`` / ``ROOT_HUB30`` / ``HDAUDIO`` …）就立刻停止并
       返回 ``None`` —— 否则蓝牙手柄会一路爬到**蓝牙适配器**上（它本身往往挂在
       USB 总线上），把同一台机器上的所有蓝牙手柄并成一个设备，那是比"多出
       几个幽灵条目"严重得多的错误。宁可退回旧的分组方式。
    """
    key = path if isinstance(path, (bytes, bytearray)) else str(path).encode()
    if key in _cache:
        return _cache[key]

    result: Optional[str] = None
    cm = _load()
    if cm is not None:
        instance = hid_instance_id(path)
        if instance:
            devinst = wintypes.DWORD(0)
            if cm.CM_Locate_DevNodeW(ctypes.byref(devinst), instance, 0) == _CR_SUCCESS:
                current = devinst.value
                for _ in range(_MAX_DEPTH):
                    parent = wintypes.DWORD(0)
                    if cm.CM_Get_Parent(ctypes.byref(parent), current, 0) != _CR_SUCCESS:
                        break
                    parent_id = _device_id(cm, parent.value)
                    if not parent_id:
                        break
                    if not _is_usb_node(parent_id):
                        break            # 出了 USB 枚举器，立刻停手（见上面的警告）
                    if _is_physical_usb_device(parent_id):
                        result = parent_id
                        break
                    current = parent.value

    _cache[key] = result
    if result:
        log.debug("HID 路径归组到物理设备 %s", result)
    return result


def clear_cache() -> None:
    """清空缓存（测试用；或设备拓扑大改后调用）。"""
    _cache.clear()


# ===========================================================================
# 手柄自己的 MAC：跨总线（USB / 蓝牙）识别"同一台手柄"
# ===========================================================================
# 需求：手柄蓝牙连着时插上 USB 线，Windows 会**同时**暴露两条 HID 设备
# （蓝牙一条、USB 一条），分组键不同 → 界面里同一只手柄出现两条。
#
# 能跨总线对齐两者的只有**手柄自己的 MAC**。内核 hid-playstation.c 就是这么
# 拿的，两条来源：
#
#   if (hdev->bus == BUS_USB) {
#           DUALSHOCK4_GET_REPORT(0x12, buf, 16);
#           memcpy(mac, &buf[1], 6);          /* 特性报告里 */
#   } else {
#           sscanf(hdev->uniq, "%02hhx:%02hhx:...");   /* 蓝牙：设备唯一串 */
#   }
#
# 真机验证（DualSense，USB 与蓝牙同时连着）：
#   特性报告 0x09 = 09 4E B3 82 56 27 0C 08 25 ...
#   → 反转（内核注明是小端） = 0C 27 56 82 B3 4E
#   → 蓝牙侧 hidapi.serial_number = '0c275682b34e'   完全一致 ✓
#   → 设备树实例 BTHENUM\DEV_0C275682B34E            完全一致 ✓

#: 家族 -> (配对信息特性报告 ID, 读取长度)
PAIRING_REPORT = {
    0x0CE6: (0x09, 20),      # DualSense
    0x0DF2: (0x09, 20),      # DualSense Edge
    0x05C4: (0x12, 16),      # DualShock 4 (CUH-ZCT1)
    0x09CC: (0x12, 16),      # DualShock 4 (CUH-ZCT2)
}


def normalize_mac(text) -> Optional[str]:
    """把各种写法归一成 12 位小写十六进制：``0c275682b34e``。

    接受 ``0c:27:56:82:b3:4e`` / ``0C-27-56-82-B3-4E`` / ``0C275682B34E``。
    不是 12 位十六进制就返回 ``None``（例如空串 —— Windows 上 USB 连接的
    手柄 serial_number 常常是空的，那正是需要读特性报告的原因）。
    """
    if not text:
        return None
    digits = "".join(c for c in str(text).lower() if c in "0123456789abcdef")
    return digits if len(digits) == 12 else None


def mac_from_feature_report(payload: Optional[bytes]) -> Optional[str]:
    """从配对信息特性报告里取出 MAC。

    **MAC 在报告里是小端序**（内核结构体注释：``stored in little endian order``），
    所以要倒着读。真机字节::

        09 4E B3 82 56 27 0C 08 25 00 ...
        ^^ 报告 ID
           ^^^^^^^^^^^^^^^ 倒序 -> 0c275682b34e
    """
    if not payload or len(payload) < 7:
        return None
    return "".join("%02x" % b for b in reversed(payload[1:7]))
