# -*- coding: utf-8 -*-
"""HID 报告解析测试。

所有测试数据都是**手工构造的合成报告**：由于开发环境没有 PS4/PS5 实机，
这里验证的是「给定报告字节 → 解析结果」的映射是否与 Linux 内核
hid-playstation.c 的定义一致。实机验证需在目标机器上跑 ``--dump-hid``。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from psbt.controllers import parsers  # noqa: E402
from psbt.controllers.models import (  # noqa: E402
    FAMILY_DS4,
    FAMILY_DUALSENSE,
    ChargeState,
    Transport,
)


def make_report(report_id: int, size: int, index: int, value: int) -> bytes:
    """构造一条**像真实完整报告**的输入报告。

    .. note::
       不能只填电量字节就交差：真实报告的陀螺仪/时间戳区（下标 12 起）必然
       有数据，而"除了头部和电量字节以外全 0"恰好是**连接/断开瞬间空壳报告**
       的特征，会被解析层正确拒绝（见 ``parsers._body_is_empty``）。
       这里填一段递增数据，让合成报告具备真实报告的形态。
    """
    buf = bytearray(size)
    buf[0] = report_id
    for i in range(12, min(size, 24)):
        buf[i] = (0x40 + i) & 0xFF          # 模拟陀螺仪/时间戳
    buf[index] = value
    return bytes(buf)


class DS4ParseTests(unittest.TestCase):
    def _parse(self, raw, transport, size):
        index = 30 if transport == Transport.USB else 32
        rid = 0x01 if transport == Transport.USB else 0x11
        data = make_report(rid, size, index, raw)
        return parsers.parse_report(FAMILY_DS4, transport, data)

    def test_usb_discharging_levels(self):
        for level in range(0, 10):
            report = self._parse(level, Transport.USB, 64)
            self.assertEqual(report.level, level)
            self.assertEqual(report.percent, level * 10 + 5)
            self.assertEqual(report.state, ChargeState.DISCHARGING)

    def test_usb_full_level(self):
        report = self._parse(10, Transport.USB, 64)
        self.assertEqual((report.level, report.percent), (10, 100))

    def test_bt_offsets_are_different_from_usb(self):
        # 蓝牙报告 0x11 的电量字节在下标 32（USB 是 30）—— 这是最容易写错的地方
        data = bytearray(78)
        data[0] = 0x11
        data[30] = 0x03      # 这个位置在蓝牙报告里不是电量
        data[32] = 0x07      # 这才是电量
        report = parsers.parse_report(FAMILY_DS4, Transport.BLUETOOTH, bytes(data))
        self.assertEqual(report.level, 7)

    def test_cable_state_means_charging(self):
        report = self._parse(0x05 | 0x10, Transport.USB, 64)
        self.assertEqual(report.state, ChargeState.CHARGING)
        self.assertEqual(report.percent, 55)

    def test_cable_with_level_10_is_charging_100(self):
        report = self._parse(0x10 | 0x0A, Transport.USB, 64)
        self.assertEqual((report.state, report.percent), (ChargeState.CHARGING, 100))

    def test_status_11_means_charging_not_full(self):
        """插线时低 4 位 11 —— 内核叫它"已充满"，**但真机证明它只是"充电中"**。

        2026-10-05 实测（用户反馈"接 USB 误报 100"）：DS4 一插上线就固定报
        0x1B（低 4 位 = 11），而当时真实电量只有 85%（充电器脉冲间隙那几帧报
        0x08，那才是真实等级）。照内核映射成 100% 会给出假读数，所以这里只
        声明"充电中"、不给等级；等级由管理器沿用上一次真实读数，
        详见 DS4ChargingCarryForwardTests。
        """
        report = self._parse(0x10 | 0x0B, Transport.USB, 64)
        self.assertEqual(report.state, ChargeState.CHARGING)
        self.assertIsNone(report.percent, "不能报成 100%")

    def test_status_14_15_are_error_not_zero(self):
        for raw in (0x10 | 0x0E, 0x10 | 0x0F):
            report = self._parse(raw, Transport.USB, 64)
            self.assertEqual(report.state, ChargeState.ERROR)
            self.assertIsNone(report.percent, "异常状态不能报成 0%，否则会误触发耗电告警")

    def test_bt_minimal_report_has_no_battery(self):
        data = bytes([0x01] + [0] * 9)      # 10 字节精简报告
        report = parsers.parse_report(FAMILY_DS4, Transport.BLUETOOTH, data)
        self.assertIsNotNone(report)
        self.assertIsNone(report.level)
        self.assertIn("无电量字段", report.source)

    def test_short_report_returns_none(self):
        data = make_report(0x01, 20, 19, 5)   # 长度不足 31
        self.assertIsNone(parsers.parse_report(FAMILY_DS4, Transport.USB, data))

    def test_unknown_report_id_returns_none(self):
        data = make_report(0x99, 64, 30, 5)
        self.assertIsNone(parsers.parse_report(FAMILY_DS4, Transport.USB, data))

    def test_empty_data(self):
        self.assertIsNone(parsers.parse_report(FAMILY_DS4, Transport.USB, b""))

    # ------------------------------------------------------------------
    # 真机回归：蓝牙上的 0x01 不是 USB 完整报告（2026-09-21 实测）
    # ------------------------------------------------------------------
    # 数据来自真机日志：DS4(CUH-ZCT2) 用蓝牙接入时先到的第一条报告。
    # 它被蓝牙栈补齐到 64 字节，若按 USB 下标去读，下标 30 是填充的 0x00，
    # 会解析成「0 档 / 约 5%」并误报一次低电量提醒。
    REAL_BT_MINIMAL_PADDED = bytes.fromhex(
        "01 78 7A 7C 84 08 00 00 00 00 00 00 00 00 00 00"
        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    )
    # 随后到达的真实蓝牙完整报告（0x11），电量等级 6（约 65%）
    REAL_BT_FULL = bytes.fromhex(
        "11 C0 00 78 7A 7D 83 08 00 94 00 00 77 D2 04 FB"
        "FF 01 00 0A 00 82 02 E4 1E 53 06 00 00 00 00 00"
        "06 00 00 00 00 80 00 00 00 80 00 00 00 00 80 00"
        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
        "00 00 00 00 00 00 00 00 00 00 00"
    )

    def test_bt_padded_minimal_report_is_not_usb_full(self):
        """蓝牙上被补齐到 64 字节的 0x01 仍必须按精简报告处理。"""
        self.assertGreaterEqual(len(self.REAL_BT_MINIMAL_PADDED), 64)
        self.assertEqual(self.REAL_BT_MINIMAL_PADDED[30], 0x00,
                         "下标 30 就是那个会被误读成 0 档的填充字节")
        report = parsers.parse_report(
            FAMILY_DS4, Transport.BLUETOOTH, self.REAL_BT_MINIMAL_PADDED)
        self.assertIsNotNone(report)
        self.assertIsNone(report.level, "不能解析出 0 档")
        self.assertIsNone(report.percent, "不能报成约 5%，否则会误触发低电量提醒")
        self.assertIn("无电量字段", report.source)
        self.assertTrue(report.source.startswith(parsers.SRC_DS4_BT_MINIMAL),
                        "上层据此走「尝试切完整模式」分支")

    def test_real_bt_full_report_still_parses(self):
        """真正的蓝牙完整报告（0x11）必须照常解析出电量。"""
        report = parsers.parse_report(FAMILY_DS4, Transport.BLUETOOTH,
                                      self.REAL_BT_FULL)
        self.assertEqual(report.level, 6)
        self.assertEqual(report.percent, 65)
        self.assertEqual(report.state, ChargeState.DISCHARGING)

    def test_usb_full_0x01_unchanged(self):
        """USB 上的 0x01 是完整报告，行为不能变（下标 30）。"""
        data = bytearray(64)
        data[0] = 0x01
        data[30] = 0x07
        report = parsers.parse_report(FAMILY_DS4, Transport.USB, bytes(data))
        self.assertEqual(report.level, 7)
        self.assertEqual(report.source, parsers.SRC_DS4_USB)

    def test_unknown_transport_falls_back_to_length(self):
        """连接方式未知时靠长度区分 0x01 的两种含义。"""
        short = bytes([0x01] + [0] * 9)                 # 10B -> 精简报告
        report = parsers.parse_report(FAMILY_DS4, Transport.UNKNOWN, short)
        self.assertIsNone(report.level)
        self.assertIn("无电量字段", report.source)

        # 64B 且报告体被填充（非全零）-> 认定为真正的 USB 完整报告
        long = bytearray(64)
        long[0] = 0x01
        long[9] = 0x2A          # 数据包计数器
        long[30] = 0x09
        report = parsers.parse_report(FAMILY_DS4, Transport.UNKNOWN, bytes(long))
        self.assertEqual(report.level, 9)

    def test_unknown_transport_with_padded_minimal_is_not_usb(self):
        """总线未知时，被补齐的蓝牙精简报告也不能被当成 USB 报告。

        这是本项目明确修过的 bug（接入瞬间误报约 5%）。当初只按
        ``Transport.BLUETOOTH`` 分支修好了；``Transport.UNKNOWN`` 这条路径
        曾经仍会把补齐后的 64 字节 0x01 当 USB 报告读下标 30，等于把同一个
        坑重新引入。这里用真机抓到的原始字节锁死。
        """
        report = parsers.parse_report(
            FAMILY_DS4, Transport.UNKNOWN, self.REAL_BT_MINIMAL_PADDED)
        self.assertIsNotNone(report)
        self.assertIsNone(report.level, "不能解析出 0 档")
        self.assertIsNone(report.percent, "不能报成约 5%")
        self.assertIn("无电量字段", report.source)
        self.assertTrue(report.source.startswith(parsers.SRC_DS4_BT_MINIMAL))

    def test_explicit_usb_transport_with_populated_body_parses(self):
        """总线明确是 USB、报告体也有数据 → 正常解析。"""
        data = bytearray(64)
        data[0] = 0x01
        data[12] = 0x99           # 陀螺仪/时间戳区非零 = 真实报告
        data[30] = 0x05
        report = parsers.parse_report(FAMILY_DS4, Transport.USB, bytes(data))
        self.assertEqual(report.level, 5)
        self.assertEqual(report.source, parsers.SRC_DS4_USB)

    def test_bt_padded_minimal_does_not_trigger_low_battery_alert(self):
        """端到端锁住原始症状：接入瞬间**不再误报**低电量提醒。

        旧逻辑把补齐的 64 字节 `0x01` 当成 USB 完整报告 → 读出 0 档（约 5%）
        → 立刻触发一次 critical 提醒。这里直接验证「解析结果不会让策略报警」。
        """
        from psbt.controllers.models import ControllerView
        from psbt.notify.policy import LowBatteryPolicy

        report = parsers.parse_report(
            FAMILY_DS4, Transport.BLUETOOTH, self.REAL_BT_MINIMAL_PADDED)
        view = ControllerView(
            key="ds4", family=FAMILY_DS4, display_name="DualShock 4 (CUH-ZCT2)",
            transport=Transport.BLUETOOTH, vendor_id=0x054C, product_id=0x09CC,
            battery=report,
        )
        policy = LowBatteryPolicy([30, 20, 10])
        self.assertIsNone(policy.evaluate(view),
                          "蓝牙精简报告不得触发低电量提醒")
        self.assertIsNone(view.percent, "图标也不应显示任何电量百分比")

        # 对照：若按旧逻辑误读成 0 档，策略确实会报警 —— 证明这个测试有区分力
        wrong = ControllerView(
            key="ds4", family=FAMILY_DS4, display_name="DualShock 4 (CUH-ZCT2)",
            transport=Transport.BLUETOOTH, vendor_id=0x054C, product_id=0x09CC,
            battery=parsers.interpret_ds4(self.REAL_BT_MINIMAL_PADDED[30]),
        )
        self.assertEqual(wrong.percent, 5, "旧逻辑会读成 0 档 / 约 5%")
        self.assertIsNotNone(LowBatteryPolicy([30, 20, 10]).evaluate(wrong),
                             "按旧逻辑确实会误报 —— 这正是被修掉的 bug")


class DualSenseParseTests(unittest.TestCase):
    def _parse(self, raw, transport, size):
        index = 53 if transport == Transport.USB else 54
        rid = 0x01 if transport == Transport.USB else 0x31
        data = make_report(rid, size, index, raw)
        return parsers.parse_report(FAMILY_DUALSENSE, transport, data)

    def test_usb_offsets(self):
        report = self._parse(0x09, Transport.USB, 64)
        self.assertEqual(report.level, 9)
        self.assertEqual(report.percent, 95)
        self.assertEqual(report.state, ChargeState.DISCHARGING)

    def test_bt_offset_is_54(self):
        data = bytearray(78)
        data[0] = 0x31
        data[53] = 0x01     # USB 下标，蓝牙下应为 0
        data[54] = 0x18     # 充电中 + 等级 8
        report = parsers.parse_report(FAMILY_DUALSENSE, Transport.BLUETOOTH, bytes(data))
        self.assertEqual(report.level, 8)
        self.assertEqual(report.state, ChargeState.CHARGING)

    def test_charging_status_nibble(self):
        report = self._parse(0x17, Transport.USB, 64)
        self.assertEqual(report.state, ChargeState.CHARGING)
        self.assertEqual(report.level, 7)
        self.assertEqual(report.percent, 75)

    def test_full_status(self):
        report = self._parse(0x20, Transport.USB, 64)
        self.assertEqual(report.state, ChargeState.FULL)
        self.assertEqual(report.percent, 100)

    def test_error_statuses(self):
        for raw in (0xA5, 0xB5, 0xF5):
            report = self._parse(raw, Transport.USB, 64)
            self.assertEqual(report.state, ChargeState.ERROR)
            self.assertIsNone(report.level)

    def test_unknown_status_is_error_not_crash(self):
        report = self._parse(0x45, Transport.USB, 64)   # 状态位 0x4，保留值
        self.assertEqual(report.state, ChargeState.ERROR)

    def test_short_report_returns_none(self):
        self.assertIsNone(parsers.parse_report(
            FAMILY_DUALSENSE, Transport.BLUETOOTH, make_report(0x31, 40, 39, 5)))


class PercentConversionTests(unittest.TestCase):
    def test_level_to_percent_matches_kernel_driver(self):
        # 内核注释：0 = 0-9%, 1 = 10-19%, ... 10 = 100%
        self.assertEqual(parsers.level_to_percent(0), 5)
        self.assertEqual(parsers.level_to_percent(1), 15)
        self.assertEqual(parsers.level_to_percent(9), 95)
        self.assertEqual(parsers.level_to_percent(10), 100)
        self.assertEqual(parsers.level_to_percent(99), 100)

    def test_only_11_distinct_percentages(self):
        values = {parsers.level_to_percent(level) for level in range(0, 11)}
        self.assertEqual(len(values), 11, "等级到百分比必须是双射，不能伪造 1% 精度")

    def test_thresholds_map_to_levels(self):
        """验证「低于 30%」在 10% 粒度下等价于「等级 ≤ 2」。"""
        self.assertLess(parsers.level_to_percent(2), 30)
        self.assertGreaterEqual(parsers.level_to_percent(3), 30)
        self.assertLess(parsers.level_to_percent(1), 20)
        self.assertGreaterEqual(parsers.level_to_percent(2), 20)
        self.assertLess(parsers.level_to_percent(0), 10)


class StubReportTests(unittest.TestCase):
    """"空壳" 0x01 报告：看着合法、其实读不出电量，绝不能报成 5%。

    字节全部**原样抄自真机日志**（日志只 dump 前 48 字节，其余补 0；
    反正这些报告的后半部分本来就是 0）。三份日志、三次误报，同一个坑：

    * 2026-10-05 12:36:25  DualSense 蓝牙接入 → 误报 critical（用户报的）
    * 2026-10-04 23:26:10  DualSense USB      → 误报（用户报的"断开时"）
    * 2026-09-21 21:0x     DS4 蓝牙精简报告被补齐 → 误报
    """

    #: DualSense 蓝牙接入瞬间（raw=01 80 80 83 7D 08 00 10 00 ...）
    DS_STUB_BT = bytes.fromhex(
        "01 80 80 83 7D 08 00 10 00 00 00 00 00 00 00 00"
        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00")

    #: DualSense 走 USB 收到的空壳（raw=01 7F 7F 7F 7F 00 00 00 08 00 ...）
    DS_STUB_USB = bytes.fromhex(
        "01 7F 7F 7F 7F 00 00 00 08 00 00 00 00 00 00 00"
        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
        "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00")

    #: DualSense 正常 USB 报告（raw=01 7F 80 7F 7C 00 00 01 08 00 00 00 99 ...）。
    #: 日志只 dump 前 48 字节，电量下标 53 按其当时读出的 level=7 补 0x07
    #: （DS5 电量字节的低 4 位才是等级，高 4 位是充电状态）。
    DS_REAL_USB = bytes.fromhex(
        "01 7F 80 7F 7C 00 00 01 08 00 00 00 99 6E 92 8E"
        "00 00 00 00 FF FF 0D 00 A3 1F 63 04 85 68 47 AA"
        "04 80 00 00 00 80 00 00 00 00 09 09 00 00 00 00"
        "00 00 00 00 00 07 00 00 00 00 00 00 00 00 00 00")

    def test_fixtures_have_battery_at_index_53(self):
        self.assertEqual(len(self.DS_STUB_BT), 64)
        self.assertEqual(len(self.DS_REAL_USB), 64)
        self.assertEqual(self.DS_STUB_BT[53], 0x00)
        self.assertEqual(self.DS_REAL_USB[53], 0x07, "真报告电量字节")

    def test_dualsense_bt_stub_is_not_a_reading(self):
        report = parsers.parse_report(
            FAMILY_DUALSENSE, Transport.BLUETOOTH, self.DS_STUB_BT)
        self.assertIsNotNone(report)
        self.assertIsNone(report.level, "不能读出 0 档")
        self.assertIsNone(report.percent, "不能报成约 5%")

    def test_dualsense_usb_stub_is_not_a_reading(self):
        """★ 用户报的"断开连接时误报 5%"：USB 上也会收到空壳。"""
        report = parsers.parse_report(
            FAMILY_DUALSENSE, Transport.USB, self.DS_STUB_USB)
        self.assertIsNotNone(report)
        self.assertIsNone(report.level)
        self.assertIsNone(report.percent)

    def test_dualsense_real_usb_report_still_parses(self):
        """对照：真报告必须照常读出电量（不能因为加闸把正常工作也挡了）。"""
        report = parsers.parse_report(
            FAMILY_DUALSENSE, Transport.USB, self.DS_REAL_USB)
        self.assertEqual(report.level, 7)
        self.assertEqual(report.percent, 75)
        self.assertEqual(report.source, parsers.SRC_DS_USB)

    def test_ds4_usb_stub_is_not_a_reading(self):
        """DS4 在 USB 上收到空壳同样不可信。"""
        report = parsers.parse_report(FAMILY_DS4, Transport.USB, self.DS_STUB_USB)
        self.assertIsNone(report.level)
        self.assertIsNone(report.percent)

    def test_no_stub_triggers_a_low_battery_alert(self):
        """端到端锁住症状：三种空壳都不得触发低电量提醒。

        这是用户实际感受到的问题（"误报一个 5%"= 弹出严重低电量提醒）。
        """
        from psbt.controllers.models import ControllerView
        from psbt.notify.policy import LowBatteryPolicy

        policy = LowBatteryPolicy([30, 20, 10])
        cases = [
            (FAMILY_DUALSENSE, Transport.BLUETOOTH, self.DS_STUB_BT, 0x0CE6),
            (FAMILY_DUALSENSE, Transport.USB, self.DS_STUB_USB, 0x0CE6),
            (FAMILY_DS4, Transport.USB, self.DS_STUB_USB, 0x09CC),
            (FAMILY_DS4, Transport.BLUETOOTH, self.DS_STUB_BT, 0x09CC),
            (FAMILY_DS4, Transport.UNKNOWN, self.DS_STUB_USB, 0x09CC),
        ]
        for family, transport, buf, pid in cases:
            report = parsers.parse_report(family, transport, buf)
            view = ControllerView(
                key="k", family=family, display_name="C", transport=transport,
                vendor_id=0x054C, product_id=pid, battery=report)
            self.assertIsNone(
                policy.evaluate(view),
                "空壳报告不得触发提醒：%s/%s" % (family, transport.label))
            self.assertIsNone(view.percent,
                              "图标也不应显示百分比：%s/%s" % (family, transport.label))


class DS4ChargingCodeTests(unittest.TestCase):
    """DS4 插上 USB 后固定报"低 4 位 = 11" —— 那不是电量，不能当成 100%。

    真机实测（CUH-ZCT2，2026-10-05；用户反馈"接入 usb 会误报 100"）：

    * 未插线：``0x08`` → 低 4 位 8 → 约 85%（真实电量）
    * 一插上 USB：**立刻**变成 ``0x1B`` 并**一直保持**（实测 8 秒 4003 帧 USB +
      1876 帧蓝牙，无一例外），与真实电量无关
    * 充电期间偶尔插进来一帧"未插线"的 ``0x08``（充电器脉冲间隙），
      那才是真实等级

    内核 ``hid-playstation.c`` 把"插线时 11"注释成 "battery is full"，照抄就会
    在 85% 时显示 100%。
    """

    def test_cable_plugged_eleven_is_not_a_level(self):
        r = parsers.interpret_ds4(0x1B)
        self.assertIsNone(r.level, "11 不是电量")
        self.assertIsNone(r.percent, "不能报成 100%")
        self.assertEqual(r.state, ChargeState.CHARGING, "但确实是充电中")

    def test_cable_clear_real_level(self):
        r = parsers.interpret_ds4(0x08)
        self.assertEqual((r.level, r.percent), (8, 85))
        self.assertEqual(r.state, ChargeState.DISCHARGING)

    def test_cable_plugged_with_real_level_still_works(self):
        """插线 + 0~10 是真等级（内核规则），照常解析。"""
        r = parsers.interpret_ds4(0x18)
        self.assertEqual((r.level, r.percent), (8, 85))
        self.assertEqual(r.state, ChargeState.CHARGING)

    def test_cable_plugged_ten_is_100(self):
        r = parsers.interpret_ds4(0x1A)
        self.assertEqual((r.percent, r.state), (100, ChargeState.CHARGING))

    def test_cable_clear_undefined_is_not_100(self):
        """未插线时的 11 及以上是未定义值，也不能当成 100%。"""
        for raw in (0x0B, 0x0C, 0x0D, 0x0E, 0x0F):
            self.assertIsNone(parsers.interpret_ds4(raw).percent, hex(raw))

    def test_errors_stay_errors(self):
        """14 电压/温度异常、15 充电错误 → 异常，不是 100%。"""
        for raw in (0x1E, 0x1F):
            r = parsers.interpret_ds4(raw)
            self.assertEqual(r.state, ChargeState.ERROR, hex(raw))
            self.assertIsNone(r.percent)


class ProtocolDocTests(unittest.TestCase):
    def test_describe_protocol_mentions_offsets(self):
        text = parsers.describe_protocol()
        for token in ("30", "32", "53", "54", "11 级", "1%"):
            self.assertIn(token, text)

    def test_hex_dump(self):
        self.assertEqual(parsers.hex_dump(b"\x01\x0a\xff", 8), "01 0A FF")


if __name__ == "__main__":
    unittest.main(verbosity=2)
