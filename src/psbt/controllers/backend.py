"""HID 访问后端（hidapi 薄封装）。

为什么是 hidapi 而不是其它方案
------------------------------
* **XInput**：``XINPUT_GAMEPAD`` 里没有电池字段；``XInputGetBatteryInformation``
  只对 Xbox 手柄有效，对 DS4/DualSense 返回 BATTERY_TYPE_DISCONNECTED。
  所以 XInput 从原理上就拿不到 PlayStation 手柄电量。
* **WMI / SetupAPI**：Windows 只把它当普通 HID 游戏控制器，拿不到索尼私有
  的 status 字节。
* **hidapi**：直接打开 HID 集合、读原始输入报告，是 DS4Windows、DualSenseX、
  Steam Input 等所有成熟方案共同的做法，也是唯一稳妥的路子。

本模块职责仅限于「枚举 / 打开 / 读 / 写 feature report / 关闭」，
不做任何协议解释（那是 :mod:`psbt.controllers.parsers` 的事）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..logs import get_logger
from . import physid, registry
from .models import FAMILY_DUALSENSE, Transport

log = get_logger("hid")

try:
    import hid as _hid  # hidapi 的 Python 绑定，import 名就是 hid
    _HID_IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover - 只在缺依赖时发生
    _hid = None
    _HID_IMPORT_ERROR = str(exc)


def is_available() -> bool:
    return _hid is not None


def import_error() -> Optional[str]:
    return _HID_IMPORT_ERROR


def library_version() -> str:
    if _hid is None:
        return "n/a"
    try:
        return _hid.version_str()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HidDeviceInfo:
    """一个 HID 顶层集合（Windows 上一个物理手柄会暴露多个集合）。"""

    path: bytes
    vendor_id: int
    product_id: int
    product_string: str = ""
    manufacturer: str = ""
    serial: str = ""
    usage_page: int = 0
    usage: int = 0
    interface_number: int = -1
    bus_type: int = 0

    @property
    def transport(self) -> Transport:
        return Transport.from_bus_type(self.bus_type)

    @property
    def device_key(self) -> str:
        """同一物理设备的所有集合共用一个 key；**同一手柄跨总线也用同一个 key**。

        优先级：

        1. **手柄自己的 MAC**（见 :func:`controller_mac`）—— 唯一能跨总线对齐的
           标识。手柄蓝牙连着时插上 USB 线，Windows 会同时暴露两条设备，
           只有 MAC 相同才能认出"这是同一只手柄"，从而不显示成两条。
        2. 父 USB 设备的实例 ID（见 :mod:`physid`）—— 复合设备会把一个手柄
           暴露成多个接口，各接口的 HID 实例段互不相同，只按 HID 路径分组
           会把**一个手柄显示成好几台**。
        3. HID 实例段 —— 都拿不到时的兜底，与旧行为一致。
        """
        mac = controller_mac(self)
        if mac:
            return "%04X:%04X#MAC:%s" % (self.vendor_id, self.product_id, mac)
        anchor = physid.physical_device_id(self.path) or _device_instance(self.path)
        return "%04X:%04X#%s" % (self.vendor_id, self.product_id, anchor)

    def __str__(self) -> str:
        return "%s (%04X:%04X %s up=%s u=%s bus=%s)" % (
            self.product_string or "HID",
            self.vendor_id,
            self.product_id,
            self.transport.label,
            self.usage_page,
            self.usage,
            self.bus_type,
        )


def _device_instance(path: bytes) -> str:
    """从 Windows HID 路径里取出「设备实例」段，用于稳定标识。

    ``\\\\?\\HID#VID_054C&PID_09CC#8&2657b139&0&0000#{guid}``
    -> ``8&2657b139&0&0000``
    不同集合（Col01/Col02 或结尾的 \\KBD）会归一到同一实例。
    """
    try:
        text = path.decode("utf-8", "replace") if isinstance(path, (bytes, bytearray)) else str(path)
    except Exception:
        return str(path)
    text = text.split("#{")[0]
    parts = [p for p in text.split("#") if p]
    if len(parts) >= 3:
        return parts[2].split("&Col")[0]
    return text


def enumerate_all() -> Optional[List[HidDeviceInfo]]:
    """枚举所有 HID 设备。

    :returns: 设备列表；**枚举失败时返回 ``None``（而不是空列表）**。

    .. important::
       ``None`` 与 ``[]`` 是两种完全不同的状态，调用方必须区分：

       * ``[]``   —— 枚举成功，当前确实一台设备都没有
       * ``None`` —— 这次枚举失败，系统里有什么**未知**

       早先两者都返回 ``[]``，结果一次临时的 SetupAPI / hidapi 抖动会被上层
       当成"所有手柄都被拔出"：读取线程全关、界面清空、告警去重状态被重置，
       下一轮又全部重新连上。设备没动，UI 却抖了一轮，还可能破坏告警去重。
    """
    if _hid is None:
        return None
    try:
        raw = _hid.enumerate()
    except Exception as exc:
        log.debug("hid.enumerate 失败：%s", exc)
        return None
    result: List[HidDeviceInfo] = []
    for d in raw:
        try:
            result.append(
                HidDeviceInfo(
                    path=d.get("path") or b"",
                    vendor_id=int(d.get("vendor_id") or 0),
                    product_id=int(d.get("product_id") or 0),
                    product_string=str(d.get("product_string") or ""),
                    manufacturer=str(d.get("manufacturer_string") or ""),
                    serial=str(d.get("serial_number") or ""),
                    usage_page=int(d.get("usage_page") or 0),
                    usage=int(d.get("usage") or 0),
                    interface_number=int(d.get("interface_number") if d.get("interface_number") is not None else -1),
                    bus_type=int(d.get("bus_type") or 0),
                )
            )
        except Exception:
            continue
    return result


def _collection_rank(info: HidDeviceInfo) -> int:
    """同一物理设备的多个 HID 集合里，哪个最可能是「主游戏控制器」。"""
    if info.usage_page == 0x01 and info.usage == 0x05:      # Generic Desktop / Game Pad
        return 0
    if info.usage_page == 0x01 and info.usage == 0x04:      # Generic Desktop / Joystick
        return 1
    if info.usage_page == 0x01 and info.usage in (0x00, 0x08):  # undefined / Multi-axis
        return 2
    if info.usage_page == 0x01:                              # 键盘/鼠标集合
        return 5
    return 8


def find_playstation_devices() -> Optional[List[HidDeviceInfo]]:
    """枚举已知的 PS4 / PS5 手柄，并按「最可能是主集合」排序。

    返回的列表已按物理设备分组去重：同一手柄只保留最靠前的候选，
    但调用方仍可通过 :func:`candidates_for` 拿到备用集合路径。

    :returns: ``None`` 表示**本轮枚举失败、结果未知**（不是"没有设备"）。
        调用方**不得**据此判定设备已拔出，详见 :func:`enumerate_all`。
    """
    if _hid is None:
        return None

    found = enumerate_all()
    if found is None:
        return None

    groups: Dict[str, List[HidDeviceInfo]] = {}
    unknown_sony: List[HidDeviceInfo] = []

    for info in found:
        if not registry.is_sony(info.vendor_id):
            continue
        spec = registry.lookup(info.vendor_id, info.product_id)
        if spec is None:
            unknown_sony.append(info)
            continue
        groups.setdefault(info.device_key, []).append(info)

    if unknown_sony:
        seen = sorted({"%04X:%04X %s" % (i.vendor_id, i.product_id, i.product_string) for i in unknown_sony})
        log.debug("发现未收录的索尼 HID 设备（不会参与电量显示）：%s", "; ".join(seen))

    result: List[HidDeviceInfo] = []
    for key in sorted(groups):
        candidates = controller_candidates(groups[key])
        result.append(candidates[0])
    return result


def _transport_rank(info: HidDeviceInfo) -> int:
    """同一只手柄同时出现在多个总线上时，该优先用哪条链路。

    目的首先是**确定性**：两条链路都在线时若排序不稳，每轮扫描都会认为
    "HID 路径变了"并重启读取线程，界面会跟着抖。

    优先级按**实测证据**定，不是一刀切：

    * **DualSense**：USB 优先。真机实测插着 USB 充电时 USB 报告能给出正确
      等级（如"65% 充电中"），有线链路也更稳。
    * **DualShock 4**：**蓝牙优先**。真机实测（CUH-ZCT2）它一插上 USB，
      USB 报告的低 4 位就**恒定**报 11（"充电中"标记，不含等级，见
      :func:`parsers.interpret_ds4`）；真实等级只在**蓝牙**链路那些
      "未插线"的帧里出现（充电器脉冲间隙）。走蓝牙才能显示真实电量。

    另一条链路仍留在候选列表里，当前链路打不开时会自动回落
    （见 :func:`all_candidates_for`）。
    """
    spec = registry.lookup(info.vendor_id, info.product_id)
    family = spec.family if spec else ""
    if family == FAMILY_DUALSENSE:
        order = (Transport.USB, Transport.BLUETOOTH)
    else:
        order = (Transport.BLUETOOTH, Transport.USB)
    return order.index(info.transport) if info.transport in order else len(order)


def candidates_for(infos: List[HidDeviceInfo]) -> List[HidDeviceInfo]:
    """把同一物理设备的多个集合排好序，最优的候选排最前。"""
    return sorted(infos, key=lambda i: (_transport_rank(i), _collection_rank(i),
                                        i.usage_page, i.usage))


def is_controller_collection(info: HidDeviceInfo) -> bool:
    """这个 HID 集合有可能**是游戏手柄**吗。

    用的是**黑名单**（只排除"确定不是手柄"的集合），不是白名单 —— 错杀会让
    一台正常手柄从界面上整个消失，代价远大于多留一个可疑集合（后者由
    :func:`_collection_rank` 排序处理，主集合仍然排最前）。

    要排除的是触控板附带的集合。真机实测 DualSense 走 USB 适配器时::

        up=1  u=5   Game Pad           ← 主集合，保留
        up=1  u=6   Keyboard           ← 排除
        up=12 u=1   Consumer Control   ← 排除
        up=1  u=2   Mouse              ← 排除

    注意**不能**用白名单（只认 Game Pad）：蓝牙连接的 DS4 报的是
    ``up=0 u=0``（hidapi 没能给出 usage），白名单会把它一起筛掉。
    """
    if info.usage_page == 0x01:                      # Generic Desktop
        # Pointer / Mouse / Keyboard / Keypad 确定不是游戏手柄
        return info.usage not in (0x01, 0x02, 0x06, 0x07)
    if info.usage_page in (0x0B, 0x0C):              # Telephony / Consumer
        return False
    return True                                      # 其余（含 up=0 的未知）放行


def controller_candidates(infos: List[HidDeviceInfo]) -> List[HidDeviceInfo]:
    """优先挑"像手柄"的集合；**一个都不像时宁可不筛**（不能把设备弄丢）。"""
    picked = [i for i in infos if is_controller_collection(i)]
    return candidates_for(picked or infos)


def all_candidates_for(vendor_id: int, product_id: int, device_key: str) -> List[HidDeviceInfo]:
    """重新枚举并取出指定物理设备的所有可用集合路径（备用候选）。

    枚举失败（``None``）时返回空列表 —— 调用方本来就有"没有候选就退回
    当前这条"的兜底逻辑，这里不需要区分"失败"与"为空"。
    """
    infos = [
        i for i in (enumerate_all() or [])
        if i.vendor_id == vendor_id and i.product_id == product_id and i.device_key == device_key
    ]
    # 备用候选同样只取"像手柄"的集合：触控板的键鼠集合即使能打开也读不到电量，
    # 让主集合失败后回落到它们只会得到无意义的读数。
    return controller_candidates(infos)


# ---------------------------------------------------------------------------
class HidHandle:
    """打开着的 HID 句柄。

    hidapi 在 Windows 上为每个句柄开一个后台读线程，因此：
    * 句柄必须显式 ``close()``，否则会泄漏线程；
    * ``read()`` 抛 ``OSError`` 表示「这个集合读不了」（例如只有 feature
      report 的集合），**不等于**设备已拔出 —— 设备是否在线由 ``enumerate``
      决定，这一点是本程序稳定性的关键。
    """

    def __init__(self, info: HidDeviceInfo):
        self.info = info
        self._dev = None
        self._lock = threading.Lock()
        self.opened_at = 0.0

    # -- 生命周期 --
    def open(self) -> bool:
        if _hid is None:
            return False
        try:
            dev = _hid.device()
            dev.open_path(self.info.path)
        except Exception as exc:
            log.debug("打开 HID 失败 %s：%s", self.info, exc)
            return False
        self._dev = dev
        self.opened_at = time.monotonic()
        log.info("已打开 HID 句柄：%s path=%s", self.info, _short_path(self.info.path))
        return True

    def close(self) -> None:
        dev, self._dev = self._dev, None
        if dev is None:
            return
        try:
            dev.close()
        except Exception as exc:
            log.debug("关闭 HID 句柄异常（忽略）：%s", exc)

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    # -- 读写 --
    def read(self, max_length: int = 256, timeout_ms: int = 1000):
        """读一条输入报告；超时返回 ``b""``，读不了抛 ``OSError``。"""
        dev = self._dev
        if dev is None:
            return b""
        try:
            data = dev.read(max_length, timeout_ms)
        except OSError:
            raise
        except Exception as exc:
            raise OSError(str(exc))
        if not data:
            return b""
        return bytes(data)

    def get_feature_report(self, report_id: int, length: int = 64) -> Optional[bytes]:
        dev = self._dev
        if dev is None:
            return None
        try:
            with self._lock:
                data = dev.get_feature_report(report_id, length)
            return bytes(data) if data else None
        except Exception as exc:
            log.debug("读取 feature report 0x%02X 失败：%s", report_id, exc)
            return None

    def send_feature_report(self, payload: bytes) -> bool:
        dev = self._dev
        if dev is None:
            return False
        try:
            with self._lock:
                dev.send_feature_report(list(payload))
            return True
        except Exception as exc:
            log.debug("发送 feature report 失败：%s", exc)
            return False


def _short_path(path: bytes) -> str:
    try:
        text = path.decode("utf-8", "replace") if isinstance(path, (bytes, bytearray)) else str(path)
    except Exception:
        return repr(path)
    return text.split("{")[0]


# ---------------------------------------------------------------------------
# DS4 蓝牙「完整报告模式」
# ---------------------------------------------------------------------------
# 现象：部分 DS4 固件/主机组合下，蓝牙连接只发 10 字节的精简报告（0x01），
# 报告里根本没有电量字段，因此界面会一直显示「电量未知」。
# Linux 内核驱动 hid-sony/hid-playstation 的做法是向手柄请求一次
# **feature report 0x02（校准数据）**，DS4 收到后即切换到 78 字节完整报告
# 模式（0x11），电量随之可用。该请求是只读的，对新手柄也是合法操作。
DS4_FEATURE_REPORT_CALIBRATION = 0x02
DS4_FEATURE_REPORT_CALIBRATION_SIZE = 37
DS4_FEATURE_REPORT_BT_CALIBRATION = 0x05
DS4_FEATURE_REPORT_BT_CALIBRATION_SIZE = 41


def request_ds4_full_report_mode(handle: HidHandle, transport: Transport) -> bool:
    """尝试让 DS4 从精简报告切到完整报告模式，成功与否都只影响日志。"""
    ok = False
    if transport == Transport.BLUETOOTH:
        payload = handle.get_feature_report(
            DS4_FEATURE_REPORT_BT_CALIBRATION, DS4_FEATURE_REPORT_BT_CALIBRATION_SIZE
        )
        if payload:
            log.info("DS4 蓝牙：已请求 feature report 0x05（校准），%d 字节", len(payload))
            ok = True
    else:
        payload = handle.get_feature_report(
            DS4_FEATURE_REPORT_CALIBRATION, DS4_FEATURE_REPORT_CALIBRATION_SIZE
        )
        if payload:
            log.info("DS4：已请求 feature report 0x02（校准），%d 字节", len(payload))
            ok = True
    if not ok:
        log.info("DS4：切换完整报告模式的请求未成功（手柄可能不支持，继续等待输入报告）")
    return ok


def dualsense_firmware_info(handle: HidHandle) -> Optional[bytes]:
    """读取 DualSense 固件信息（0x20，64 字节）—— 仅用于排障日志。"""
    return handle.get_feature_report(0x20, 64)


# ---------------------------------------------------------------------------
# 手柄自己的 MAC：跨总线识别"同一台手柄"
# ---------------------------------------------------------------------------
# 为什么需要：手柄**蓝牙连着时插上 USB 线**，Windows 会同时暴露两条 HID 设备
# （蓝牙一条、USB 一条），两者的分组键天然不同 → 界面里同一只手柄出现两条。
# 能跨总线对齐的只有手柄自己的 MAC。
#
# 两条来源（内核 hid-playstation.c 就是这么做的）：
#   * 蓝牙：设备唯一串，对应 hidapi 的 serial_number，本身就是 MAC；
#   * USB ：serial_number 常常是空的，得读**配对信息特性报告**
#           （DualSense = 0x09 / DualShock 4 = 0x12），MAC 在小端序的 [1..6]。
#
# 真机验证（DualSense，USB 与蓝牙同时连着）：
#   特性报告 0x09 = 09 4E B3 82 56 27 0C ...  → 反转 → 0c275682b34e
#   蓝牙侧 serial_number = '0c275682b34e'              一致 ✓

_mac_cache: Dict[object, Optional[str]] = {}
_MAC_MISS = object()


def _collections_of(info: HidDeviceInfo) -> List[HidDeviceInfo]:
    """同一个**物理设备**下的所有集合，按"最像手柄"的顺序排。

    为什么要一起看：一个手柄会暴露多个 HID 集合（主游戏控制器 + 触控板的
    键鼠集合）。配对信息特性报告只有主集合（或其中一部分）能读，
    只拿手上这一个集合去读，可能"恰好读不到"。
    """
    try:
        all_dev = enumerate_all() or []
    except Exception:
        return [info]
    anchor = physid.physical_device_id(info.path)
    group = [d for d in all_dev
             if d.vendor_id == info.vendor_id and d.product_id == info.product_id
             and (physid.physical_device_id(d.path) or d.path) == (anchor or info.path)]
    return candidates_for(group) or [info]


def _read_pairing_mac(info: HidDeviceInfo) -> Optional[str]:
    entry = physid.PAIRING_REPORT.get(info.product_id)
    if entry is None:
        return None
    report_id, length = entry
    handle = HidHandle(info)
    try:
        if not handle.open():
            return None
        payload = handle.get_feature_report(report_id, length)
    except Exception as exc:                       # 读不到不算错误，只是没标识
        log.debug("读取配对信息特性报告失败（%s）：%s", info, exc)
        return None
    finally:
        try:
            handle.close()
        except Exception:
            pass
    mac = physid.mac_from_feature_report(payload)
    if mac:
        log.debug("从特性报告 0x%02X 取到手柄 MAC：%s", report_id, mac)
    return mac


def controller_mac(info: HidDeviceInfo) -> Optional[str]:
    """返回手柄自己的 MAC（12 位小写十六进制），拿不到返回 ``None``。

    .. important::
       **缓存单位是"物理设备"，不是单个集合。** 一个手柄的多个集合必须共用
       同一个结果 —— 否则会出现"主集合拿到了 MAC、触控板集合没拿到"，
       同一个手柄算出两个分组键，1.5 修掉的"一个手柄显示成好几台"就会回来。

    取法（内核 hid-playstation.c 的两条来源）：
    1. 设备唯一串 —— 蓝牙连接时 ``serial_number`` 本身就是 MAC；
    2. 配对信息特性报告 —— USB 连接时 ``serial_number`` 是空的，
       DualSense 用 ``0x09`` / DualShock 4 用 ``0x12``，MAC 在小端序的 ``[1..6]``。

    第 2 步会按"最像手柄"的顺序**逐个集合尝试**，避免恰好拿到一个读不了的集合。
    """
    anchor = physid.physical_device_id(info.path) or info.path
    cached = _mac_cache.get(anchor, _MAC_MISS)
    if cached is not _MAC_MISS:
        return cached                                  # type: ignore[return-value]

    mac = None
    for cand in _collections_of(info):
        mac = physid.normalize_mac(cand.serial)
        if mac:
            break
    if mac is None:
        for cand in _collections_of(info):
            mac = _read_pairing_mac(cand)
            if mac:
                break

    if len(_mac_cache) > 64:                           # 防止无界增长
        _mac_cache.clear()
    _mac_cache[anchor] = mac
    return mac


def clear_mac_cache() -> None:
    """清空 MAC 缓存（测试用）。"""
    _mac_cache.clear()


# ---------------------------------------------------------------------------
def dump_report_descriptor(info: HidDeviceInfo, limit: int = 512) -> str:
    """读取 HID 报告描述符（排障用），失败返回空串。"""
    if _hid is None:
        return ""
    handle = HidHandle(info)
    if not handle.open():
        return ""
    try:
        dev = handle._dev  # noqa: SLF001 - 排障辅助，容忍访问内部
        with handle._lock:
            data = dev.get_report_descriptor()
        return bytes(data)[:limit].hex(" ") if data else ""
    except Exception as exc:
        log.debug("读取报告描述符失败：%s", exc)
        return ""
    finally:
        handle.close()
