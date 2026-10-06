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
def peek_battery_byte(family: str, transport: Transport,
                      buf: bytes) -> Optional[int]:
    """**只读出一个字节**：这一帧里的电量/状态字节（不做任何解释）。

    给读取循环做节流判断用：电量字节一变就立刻解析，不必等节流窗口
    （见 :meth:`Manager._Reader._loop`）。读不到返回 ``None``。
    """
    size = len(buf)
    if family == FAMILY_DUALSENSE:
        if transport == Transport.BLUETOOTH:
            if buf[0] == DS_REPORT_BT and size >= DS_BT_BATTERY_INDEX + 1:
                return buf[DS_BT_BATTERY_INDEX]
            return None
        if buf[0] == DS_REPORT_USB and size >= DS_USB_BATTERY_INDEX + 1:
            return buf[DS_USB_BATTERY_INDEX]
        return None
    if transport == Transport.BLUETOOTH:
        if buf[0] == 0x11 and size >= DS4_BT_BATTERY_INDEX + 1:
            return buf[DS4_BT_BATTERY_INDEX]
        return None
    if buf[0] == 0x01 and size >= DS4_USB_BATTERY_INDEX + 1:
        return buf[DS4_USB_BATTERY_INDEX]
    return None


def interpret_ds4(raw: int) -> BatteryReport:
    """解释 DualShock 4 的电量字节 ``status[0]``。

    .. important::
       **插线时的低 4 位 ``11`` 不是电量。** 内核 ``hid-playstation.c`` 把它
       注释成"battery is full"，照抄会得到一个**假读数**。真机实测
       （CUH-ZCT2，2026-10-05）：

       * 未插线：``0x08`` → 低 4 位 8 = 约 85%（真实电量）
       * 一插上 USB：立刻变成 ``0x1B``（低 4 位 = 11，bit4 = 插线）
         **并一直保持**，无论电量是 85% 还是别的值
       * 充电期间偶尔会插进来一帧"未插线"的 ``0x08``（充电器脉冲间隙），
         那才是真实的等级

       所以这里对"插线 + 11"只声明**正在充电**、不给等级；等级由真正带等级的
       帧决定，管理器会把上一次真实等级沿用到充电状态里
       （见 :meth:`Manager._update_battery`），既不会假报 100%，也不会闪烁。
    """
    capacity = raw & BATTERY_CAPACITY_MASK
    cable = bool(raw & DS4_CABLE_STATE_BIT)

    if cable:
        if capacity < 10:
            return BatteryReport(capacity, level_to_percent(capacity), ChargeState.CHARGING,
                                 SRC_DS4_USB, raw)
        if capacity == 10:
            return BatteryReport(10, 100, ChargeState.CHARGING, SRC_DS4_USB, raw)
        if capacity == DS4_STATUS_FULL:
            # 内核叫它"已充满"，真机证明那只表示"插着线在充电"。
            # 只报状态、不报等级 —— 否则充电时会显示假的 100%。
            return BatteryReport(None, None, ChargeState.CHARGING, SRC_DS4_USB, raw)
        # 14 电压/温度异常未充电、15 充电错误、以及保留值
        return BatteryReport(None, None, ChargeState.ERROR, SRC_DS4_USB, raw)

    # 未插线：0~10 才是等级；11 及以上是未定义值（不能当成 100%）
    if capacity > 10:
        return BatteryReport(None, None, ChargeState.DISCHARGING, SRC_DS4_USB, raw)
    return BatteryReport(capacity, level_to_percent(capacity), ChargeState.DISCHARGING,
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
    """解析 DualSense (PS5) 输入报告。

    .. important::
       和 DS4 同一个道理：**报告 ID 必须与连接方式一起判断**。
       DualSense 的完整报告在 USB 上是 **0x01**（64B）、蓝牙上是 **0x31**（78B，
       两者电量都落在下标 53）。

       真机踩过的坑（2026-10-05 复现）：蓝牙接入瞬间收到一条 **0x01**、长度
       64 字节的报告，前 6 字节是合法摇杆数据、**下标 8 起全是 0**。旧代码
       没看 transport，直接按 USB 报告读下标 53 = ``0x00`` → "0 档 / 约 5%"
       → 立刻弹了一次**严重低电量**提醒（几分钟后才被真正的 0x31 纠正）。

       所以这里有两道闸：**蓝牙下的 0x01 一律不是完整报告**；**报告体为空的
       0x01 在任何总线上一律不可信**（USB 下也会收到这种空壳，见
       :func:`_body_is_empty`，那是"断开连接时误报 5%"的来源）。
    """
    # 蓝牙完整报告
    if report_id == DS_REPORT_BT and len(buf) >= _DS_MIN_LEN_BT:
        report = interpret_dualsense(buf[DS_BT_BATTERY_INDEX])
        return _with_source(report, SRC_DS_BT, buf, source_hint)
    # 0x01：只有总线明确是 USB、且报告体非空时才算完整报告
    if report_id == DS_REPORT_USB and len(buf) >= _DS_MIN_LEN_USB:
        if transport == Transport.BLUETOOTH:
            return _minimal_report(buf)
        if _body_is_empty(buf):
            return _minimal_report(buf)
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


def _body_is_empty(buf: bytes) -> bool:
    """报告是不是个"空壳"：第 10 字节起全是 0。

    .. important::
       **光看报告 ID 和长度分不出真假**，必须看报告体。三份真机日志里的
       原样字节（都是同一个坑）：

       .. code-block:: text

          # 正常 USB 报告（DualSense）：下标 12 起是陀螺仪/时间戳/计数器
          01 7F 80 7F 7C 00 00 01 08 00 00 00 99 6E 92 8E 00 00 00 00 FF FF ...
          # 连接/断开瞬间的空壳报告（DualSense，USB 与蓝牙都出现过）
          01 80 80 83 7D 08 00 10 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ...
          # 蓝牙精简报告被补齐到 64 字节（DS4）
          01 78 7A 7C 84 08 00 00 00 00 00 00 00 00 00 00 ...

       三种前几个字节都是**合法的摇杆/按键数据**，长度也够（≥31 / ≥64），
       按"ID + 长度"分发全都会被当成完整报告；但后两种的**报告体是空的**。

       空报告体不可能是真实读数：电量下标（DS4=30 / DualSense=53）上全是
       填充的 ``0x00``，照读就是"0 档 / 约 5%"，并立刻误报一次**严重低电量**。
       真正的完整报告里，下标 12 起是陀螺仪与时间戳，**不可能**整段为 0。
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

    # USB（或无法判定连接方式）：0x01 是完整报告，电量在下标 30。
    # 但**报告体为空的 0x01 一律不可信**（连接/断开瞬间的空壳报告），
    # 见 _body_is_empty 的注释。
    if report_id == DS4_REPORT_USB and size >= _DS4_MIN_LEN_USB:
        if _body_is_empty(buf):
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
