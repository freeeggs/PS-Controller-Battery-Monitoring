"""PS4 / PS5 手柄 HID 输入报告解析。

权威依据
========
下面的字节偏移、位域定义**逐条对照 Linux 内核驱动**
``drivers/hid/hid-playstation.c``（由索尼工程师参与维护，是 PS 手柄 HID
协议事实上的公开规范，hid-sony.c 已废弃）：

DualShock 4 (PS4)
-----------------
::

    #define DS4_INPUT_REPORT_USB              0x01    // 64 字节
    #define DS4_INPUT_REPORT_BT_MINIMAL       0x01    // 10 字节（无电量字段）
    #define DS4_INPUT_REPORT_BT               0x11    // 78 字节
    #define DS4_STATUS0_BATTERY_CAPACITY      GENMASK(3, 0)   // 低 4 位 = 电量等级
    #define DS4_STATUS0_CABLE_STATE           BIT(4)          // bit4 = 已插入 USB
    #define DS4_BATTERY_STATUS_FULL           11

.. warning::
   **报告 ID ``0x01`` 的含义取决于连接方式**，只看 ID 会读错位置。驱动原文注释::

       DualShock4 in USB uses the full HID report for reportID 1, but
       Bluetooth uses a minimal HID report for reportID 1 and reports
       the full report using reportID 17.

   即 **0x01 只在 USB 上是完整报告**（电量在下标 30）；**蓝牙上的 0x01 是精简
   报告、没有电量字段**，蓝牙的完整报告是 0x11（电量在下标 32）。
   详见 :func:`_parse_ds4`。

结构体布局（``__packed``）：:

    struct dualshock4_input_report_common {
        u8 x,y, rx,ry, buttons[3], z,rz;   // 偏移 0..8
        __le16 sensor_timestamp;           // 9..10
        u8 sensor_temperature;             // 11
        __le16 gyro[3];                    // 12..17
        __le16 accel[3];                   // 18..23
        u8 reserved2[5];                   // 24..28
        u8 status[2];                      // 29..30   <-- status[0] 偏移 29
        u8 reserved3;
    };
    struct dualshock4_input_report_usb { u8 report_id; common; ... };
    struct dualshock4_input_report_bt  { u8 report_id; u8 reserved[2]; common; ... };

由此推出电量字节在**原始缓冲区**中的下标（hidapi 读到的首字节就是报告 ID）：

============================  ==========================  ======
连接方式                       报告                         下标
============================  ==========================  ======
USB   (0x01)                  1 + 29                       **30**
蓝牙  (0x11)                  3 + 29                       **32**
蓝牙  (0x01, 10 字节精简报告)  报告本身只到 offset 9        无电量字段
============================  ==========================  ======

DualSense (PS5)
---------------
::

    #define DS_INPUT_REPORT_USB       0x01    // 64 字节
    #define DS_INPUT_REPORT_BT        0x31    // 78 字节
    #define DS_STATUS0_BATTERY_CAPACITY   GENMASK(3, 0)   // 低 4 位 = 电量等级
    #define DS_STATUS0_CHARGING          GENMASK(7, 4)   // 高 4 位 = 充电状态

结构体布局（``__packed``，共 63 字节）：::

    struct dualsense_input_report {
        u8 x,y, rx,ry, z,rz, seq_number;   // 0..6
        u8 buttons[4];                     // 7..10
        u8 reserved[4];                    // 11..14
        __le16 gyro[3];                    // 15..20
        __le16 accel[3];                   // 21..26
        __le32 sensor_timestamp;           // 27..30
        u8 reserved2;                      // 31
        struct dualsense_touch_point points[2];  // 32..39（每点 4 字节）
        u8 reserved3[12];                  // 40..51
        u8 status[3];                      // 52..54  <-- status[0] 偏移 52
        u8 reserved4[8];
    };

驱动中 ``ds_report = (struct dualsense_input_report *)&data[1]``（USB）、
``&data[2]``（蓝牙），因此电量字节下标：

=================  ==========  ======
连接方式            报告         下标
=================  ==========  ======
USB               0x01(64B)    **53**
蓝牙              0x31(78B)    **54**
=================  ==========  ======

电量精度限制（重要，不要在 UI 上假装精确）
=========================================
PS4 / PS5 手柄通过 HID 只暴露**约 11 个档位**的电量:

* DualSense 驱动注释原文：``Each unit of battery data corresponds to 10%
  0 = 0-9%, 1 = 10-19%, .. and 10 = 100%``
* DualShock 4 同理，低 4 位取值 0~10，另有两个特殊值（11=已充满、
  14/15=电压或温度异常/充电错误）。

也就是说：

1. **不存在**任何公开 API（XInput / HID / 索尼消费级 SDK）能给出
   PS4/PS5 手柄的 1% 精度电量。XInput 的 ``XInputGetBatteryInformation``
   只对 Xbox 手柄有效，对 DS4/DualSense 一律返回 0。
2. 因此本程序把「原始等级」原样保留（``level``），百分比只是按与 Linux
   驱动一致的**区间中点**换算（``level * 10 + 5``，满级=100%），
   UI 文案统一加「约」并附带 ``等级 n/10``，让用户清楚精度边界。
3. 电量每 10% 才会跳变一次，所以「低于 30%」的实际含义是「等级 ≤ 2」。
   低电量阈值去重逻辑据此设计（见 ``notify/policy.py``）。

本文件不做任何猜测：报告长度不足、报告 ID 不匹配时返回 ``None``，
由上层决定是重试还是标记为「电量未知」，绝不伪造数值。
"""

from __future__ import annotations

from typing import Optional

from .models import (
    FAMILY_DS4,
    FAMILY_DUALSENSE,
    BatteryReport,
    ChargeState,
    Transport,
)

# ---------------------------------------------------------------------------
# 常量（对照 hid-playstation.c）
# ---------------------------------------------------------------------------
BATTERY_CAPACITY_MASK = 0x0F        # GENMASK(3,0)
CHARGING_STATUS_SHIFT = 4           # GENMASK(7,4)

# DS4
DS4_REPORT_USB = 0x01
DS4_REPORT_BT_MINIMAL = 0x01
DS4_REPORT_BT = 0x11
DS4_USB_BATTERY_INDEX = 30
DS4_BT_BATTERY_INDEX = 32
DS4_CABLE_STATE_BIT = 0x10          # BIT(4)
DS4_STATUS_FULL = 11                # DS4_BATTERY_STATUS_FULL
DS4_BT_MINIMAL_SIZE = 10

# DualSense
DS_REPORT_USB = 0x01
DS_REPORT_BT = 0x31
DS_USB_BATTERY_INDEX = 53
DS_BT_BATTERY_INDEX = 54

_DS4_MIN_LEN_USB = DS4_USB_BATTERY_INDEX + 1        # 31
_DS4_MIN_LEN_BT = DS4_BT_BATTERY_INDEX + 1          # 33
_DS_MIN_LEN_USB = DS_USB_BATTERY_INDEX + 1          # 54
_DS_MIN_LEN_BT = DS_BT_BATTERY_INDEX + 1            # 55

# 数据来源标记（写进 BatteryReport.source，便于日志排查）
SRC_DS4_USB = "ds4.usb.input0x01[30]"
SRC_DS4_BT = "ds4.bt.input0x11[32]"
SRC_DS4_BT_MINIMAL = "ds4.bt.input0x01(精简,无电量字段)"
SRC_DS_USB = "dualsense.usb.input0x01[53]"
SRC_DS_BT = "dualsense.bt.input0x31[54]"


# ---------------------------------------------------------------------------
# 等级 -> 百分比（与 Linux 驱动 dualsense_parse_report / dualshock4_parse_report 一致）
# ---------------------------------------------------------------------------
def level_to_percent(level: int) -> int:
    """把 0~10 的原始等级换算成区间中点百分比。

    驱动原文：``battery_capacity = min(battery_data * 10 + 5, 100)``
    即 0->5%, 1->15%, ... 9->95%, 10->100%。
    """
    if level >= 10:
        return 100
    if level <= 0:
        return 5
    return min(level * 10 + 5, 100)


# ---------------------------------------------------------------------------
# 解释函数
# ---------------------------------------------------------------------------
def interpret_ds4(raw: int) -> BatteryReport:
    """解释 DualShock 4 的电量字节 ``status[0]``。"""
    capacity = raw & BATTERY_CAPACITY_MASK
    cable = bool(raw & DS4_CABLE_STATE_BIT)

    if cable:
        if capacity < 10:
            return BatteryReport(capacity, level_to_percent(capacity), ChargeState.CHARGING,
                                 SRC_DS4_USB, raw)
        if capacity == 10:
            return BatteryReport(10, 100, ChargeState.CHARGING, SRC_DS4_USB, raw)
        if capacity == DS4_STATUS_FULL:
            return BatteryReport(10, 100, ChargeState.FULL, SRC_DS4_USB, raw)
        # 14 / 15 及其它未定义值：电压或温度异常、充电错误
        return BatteryReport(None, None, ChargeState.ERROR, SRC_DS4_USB, raw)

    # 未插线（蓝牙或纯无线 USB 适配器供电）
    level = min(capacity, 10)
    return BatteryReport(level, level_to_percent(level), ChargeState.DISCHARGING,
                         SRC_DS4_USB, raw)


def interpret_dualsense(raw: int) -> BatteryReport:
    """解释 DualSense 的电量字节 ``status[0]``。"""
    capacity = raw & BATTERY_CAPACITY_MASK
    status = (raw >> CHARGING_STATUS_SHIFT) & 0x0F

    if status in (0x0, 0x1):
        level = min(capacity, 10)
        state = ChargeState.CHARGING if status == 0x1 else ChargeState.DISCHARGING
        return BatteryReport(level, level_to_percent(level), state, SRC_DS_USB, raw)
    if status == 0x2:
        return BatteryReport(10, 100, ChargeState.FULL, SRC_DS_USB, raw)

    # 0xa 电压/温度超范围、0xb 温度异常、0xf 充电错误、其余保留值。
    # 内核驱动在这些情况下把电量置 0；我们**不**置 0，否则会误触发「电量耗尽」
    # 告警。这里返回「电量未知 + 状态异常」，由 UI 明确显示异常而不是假数字。
    return BatteryReport(None, None, ChargeState.ERROR, SRC_DS_USB, raw)


# ---------------------------------------------------------------------------
# 报告解析入口
# ---------------------------------------------------------------------------
def parse_report(
    family: str,
    transport: Transport,
    data,
    *,
    source_hint: str = "",
) -> Optional[BatteryReport]:
    """解析一条输入报告。

    :param family: :data:`FAMILY_DS4` 或 :data:`FAMILY_DUALSENSE`
    :param transport: 连接方式（hidapi ``bus_type``）；``UNKNOWN`` 时按报告 ID 推断
    :param data: hidapi ``read()`` 返回的字节序列（首字节为报告 ID）
    :returns: :class:`BatteryReport`；无法解析时 ``None``
    """
    if not data:
        return None
    buf = bytes(data)
    report_id = buf[0]

    if family == FAMILY_DUALSENSE:
        return _parse_dualsense(transport, report_id, buf, source_hint)
    return _parse_ds4(transport, report_id, buf, source_hint)


def _parse_dualsense(transport: Transport, report_id: int, buf: bytes,
                     source_hint: str) -> Optional[BatteryReport]:
    # 蓝牙
    if report_id == DS_REPORT_BT and len(buf) >= _DS_MIN_LEN_BT:
        report = interpret_dualsense(buf[DS_BT_BATTERY_INDEX])
        return _with_source(report, SRC_DS_BT, buf, source_hint)
    # USB
    if report_id == DS_REPORT_USB and len(buf) >= _DS_MIN_LEN_USB:
        report = interpret_dualsense(buf[DS_USB_BATTERY_INDEX])
        return _with_source(report, SRC_DS_USB, buf, source_hint)
    # 报告 ID 与长度都对不上：交给调用方记录原始报告
    return None


def _minimal_report(buf: bytes) -> BatteryReport:
    """蓝牙精简报告：设备在传数据，但协议里**没有电量字段**。

    返回「等级未知」而不是 ``None``，让上层知道「设备活着、只是读不到电量」，
    从而可以提示用户并尝试切到完整报告模式。

    .. warning::
       这里**绝不能**退化成 0 档 / 约 5% —— 那会立刻误触发一次低电量提醒。
    """
    return BatteryReport(
        None, None, ChargeState.UNKNOWN, SRC_DS4_BT_MINIMAL, buf[0] if buf else None
    )


def _looks_like_padded_minimal(buf: bytes) -> bool:
    """报告是不是「蓝牙精简报告被补齐到 64 字节」。

    只在**无法判定连接方式**时用作保险。判据：第 10 字节往后全是 0。

    真机证据（DS4 CUH-ZCT2 蓝牙接入瞬间的第一条报告）::

        01 78 7A 7C 84 08 00 00 00 00 ... 00     (64B，下标 30 是填充的 0x00)

    精简报告只有前 10 字节有意义（摇杆 + 按键），补齐部分必然是 0；
    而真正的 USB 完整报告里，偏移 9 起是数据包计数器、12 起是陀螺仪/加速度计，
    这些字段**不可能**整段为 0。
    """
    return len(buf) >= 11 and not any(buf[10:])


def _parse_ds4(transport: Transport, report_id: int, buf: bytes,
               source_hint: str) -> Optional[BatteryReport]:
    """解析 DualShock 4 输入报告。

    .. important::
       **报告 ID 必须与连接方式一起判断**，只看 ID 会读错位置。内核驱动
       ``hid-playstation.c`` 的原文注释::

           DualShock4 in USB uses the full HID report for reportID 1, but
           Bluetooth uses a minimal HID report for reportID 1 and reports
           the full report using reportID 17.

       即 **0x01 在 USB 上是完整报告（电量在下标 30），在蓝牙上却是精简报告、
       协议里没有电量字段**；蓝牙的完整报告是 **0x11**（电量在下标 32）。

       真机踩过这个坑：某些蓝牙栈会把 10 字节的精简报告**补齐到 64 字节**，
       长度一"合格"就被当成 USB 完整报告去读下标 30 —— 那里是填充的 ``0x00``，
       于是解析出「0 档 / 约 5%」，接入瞬间误报一次低电量提醒，
       十几秒后才被真正的 0x11 报告纠正。
    """
    size = len(buf)

    if transport == Transport.BLUETOOTH:
        # 蓝牙：只有 0x11(≥78B) 携带电量；0x01 一律按精简报告处理（无论长度）
        if report_id == DS4_REPORT_BT and size >= _DS4_MIN_LEN_BT:
            report = interpret_ds4(buf[DS4_BT_BATTERY_INDEX])
            return _with_source(report, SRC_DS4_BT, buf, source_hint)
        if report_id == DS4_REPORT_BT_MINIMAL:
            return _minimal_report(buf)
        return None

    # USB（或无法判定连接方式）：0x01 是完整报告，电量在下标 30
    #
    # 连接方式为 UNKNOWN 时多一道保险：报告体若为空（第 10 字节起全 0），
    # 按蓝牙精简报告处理。原因见 _looks_like_padded_minimal 的注释 ——
    # 蓝牙栈会把 10 字节精简报告补齐到 64 字节，只看 ID 和长度会读出假的 5%。
    if report_id == DS4_REPORT_USB and size >= _DS4_MIN_LEN_USB:
        if transport == Transport.UNKNOWN and _looks_like_padded_minimal(buf):
            # 总线未知时的保险：这条报告体是空的，极可能就是蓝牙精简报告被
            # 补齐到 64 字节。宁可显示"电量未知"，也不能再次踩到本项目已经
            # 明确修过的"读下标 30 得到假的 0 档 / 约 5%"。
            # （返回的 source 会标成精简报告，日志里一眼能看出来。）
            return _minimal_report(buf)
        report = interpret_ds4(buf[DS4_USB_BATTERY_INDEX])
        return _with_source(report, SRC_DS4_USB, buf, source_hint)
    # 个别蓝牙栈会把 bus_type 报成 USB；此时只能靠长度兜底区分 0x01 的两种含义
    if report_id == DS4_REPORT_BT and size >= _DS4_MIN_LEN_BT:
        report = interpret_ds4(buf[DS4_BT_BATTERY_INDEX])
        return _with_source(report, SRC_DS4_BT, buf, source_hint)
    if report_id == DS4_REPORT_BT_MINIMAL and size <= DS4_BT_MINIMAL_SIZE + 2:
        return _minimal_report(buf)
    return None


def _with_source(report: BatteryReport, source: str, buf: bytes,
                 source_hint: str) -> BatteryReport:
    """把真实来源（USB/蓝牙 + 下标）写回报告对象。"""
    src = source
    if source_hint:
        src = "%s|%s" % (source, source_hint)
    from dataclasses import replace

    return replace(report, source=src)


def battery_index_for(family: str, transport: Transport) -> Optional[int]:
    """给定家族与连接方式，返回期望的电量字节下标（排障用）。"""
    if family == FAMILY_DUALSENSE:
        if transport == Transport.BLUETOOTH:
            return DS_BT_BATTERY_INDEX
        return DS_USB_BATTERY_INDEX
    if transport == Transport.BLUETOOTH:
        return DS4_BT_BATTERY_INDEX
    return DS4_USB_BATTERY_INDEX


def describe_protocol() -> str:
    """协议说明文本（写入日志 / 关于对话框），便于用户核对。"""
    return (
        "PS4 DualShock 4 : USB 报告 0x01(64B) 电量字节下标 30；"
        "蓝牙报告 0x11(78B) 下标 32；蓝牙精简报告 0x01(10B) 不含电量。"
        "低 4 位=等级 0~10，bit4=是否插线，11=已充满，14/15=异常。\n"
        "PS5 DualSense   : USB 报告 0x01(64B) 电量字节下标 53；"
        "蓝牙报告 0x31(78B) 下标 54。低 4 位=等级 0~10，高 4 位=充电状态"
        "（0 放电/1 充电/2 充满/0xa,0xb 异常/0xf 错误）。\n"
        "精度：两者都只有约 11 级（每级≈10%），无法获得 1% 精度；"
        "百分比为区间中点估算值。"
    )


def hex_dump(data, limit: int = 32) -> str:
    """把报告转成便于日志阅读的十六进制串。"""
    buf = bytes(data)[:limit]
    text = " ".join("%02X" % b for b in buf)
    if len(bytes(data)) > limit:
        text += " ..."
    return text
