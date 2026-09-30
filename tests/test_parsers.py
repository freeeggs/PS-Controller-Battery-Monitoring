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
    buf = bytearray(size)
    buf[0] = report_id
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

    def test_status_11_means_full(self):
        report = self._parse(0x10 | 0x0B, Transport.USB, 64)
        self.assertEqual((report.state, report.percent), (ChargeState.FULL, 100))

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

        long = bytearray(64)
        long[0] = 0x01
        long[30] = 0x09
        report = parsers.parse_report(FAMILY_DS4, Transport.UNKNOWN, bytes(long))
        self.assertEqual(report.level, 9)               # 64B -> 完整报告

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


class ProtocolDocTests(unittest.TestCase):
    def test_describe_protocol_mentions_offsets(self):
        text = parsers.describe_protocol()
        for token in ("30", "32", "53", "54", "11 级", "1%"):
            self.assertIn(token, text)

    def test_hex_dump(self):
        self.assertEqual(parsers.hex_dump(b"\x01\x0a\xff", 8), "01 0A FF")


if __name__ == "__main__":
    unittest.main(verbosity=2)
