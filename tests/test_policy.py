# -*- coding: utf-8 -*-
"""低电量提醒策略测试：验证「不重复刷屏」与「跨级再提醒」。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from psbt.controllers.models import (  # noqa: E402
    FAMILY_DUALSENSE,
    BatteryReport,
    ChargeState,
    ControllerView,
    Transport,
)
from psbt.notify.policy import LowBatteryPolicy  # noqa: E402


def view(percent, state=ChargeState.DISCHARGING, key="k1", connected=True):
    level = None if percent is None else max(0, min(10, (percent - 5) // 10))
    return ControllerView(
        key=key,
        family=FAMILY_DUALSENSE,
        display_name="DualSense (PS5)",
        transport=Transport.BLUETOOTH,
        vendor_id=0x054C,
        product_id=0x0CE6,
        battery=BatteryReport(level, percent, state, "test"),
        connected=connected,
    )


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = LowBatteryPolicy([30, 20, 10])

    def test_no_alert_above_threshold(self):
        for percent in (100, 95, 45, 35):
            self.assertIsNone(self.policy.evaluate(view(percent)), "%.0f%% 不应提醒" % percent)

    def test_alert_at_each_band_once(self):
        self.assertIsNotNone(self.policy.evaluate(view(25)))    # 跨 30
        self.assertIsNone(self.policy.evaluate(view(25)))        # 同级不重复
        self.assertIsNone(self.policy.evaluate(view(25)))
        self.assertIsNotNone(self.policy.evaluate(view(15)))    # 跨 20
        self.assertIsNone(self.policy.evaluate(view(15)))
        alert = self.policy.evaluate(view(5))                    # 跨 10
        self.assertIsNotNone(alert)
        self.assertEqual(alert.threshold, 10)
        self.assertEqual(alert.severity, "critical")
        self.assertIsNone(self.policy.evaluate(view(5)))

    def test_exactly_three_alerts_over_full_discharge(self):
        sequence = [95, 85, 75, 65, 55, 45, 35, 25, 25, 15, 15, 5, 5, 5]
        alerts = [self.policy.evaluate(view(p)) for p in sequence]
        fired = [a for a in alerts if a is not None]
        self.assertEqual(len(fired), 3)
        self.assertEqual([a.threshold for a in fired], [30, 20, 10])

    def test_recovery_resets_band(self):
        self.assertIsNotNone(self.policy.evaluate(view(25)))
        self.assertIsNone(self.policy.evaluate(view(45)))     # 回升到 30 以上 → 重置
        self.assertIsNotNone(self.policy.evaluate(view(25)), "恢复后再次跌破应重新提醒")

    def test_charging_resets_band(self):
        self.assertIsNotNone(self.policy.evaluate(view(25)))
        self.assertIsNone(self.policy.evaluate(view(25, ChargeState.CHARGING)))
        self.assertIsNotNone(self.policy.evaluate(view(25)), "断开充电后再次低电量应提醒")

    def test_full_resets_band(self):
        self.assertIsNotNone(self.policy.evaluate(view(15)))
        self.assertIsNone(self.policy.evaluate(view(100, ChargeState.FULL)))
        self.assertIsNotNone(self.policy.evaluate(view(15)))

    def test_disconnect_then_reconnect_triggers_again(self):
        self.assertIsNotNone(self.policy.evaluate(view(25)))
        self.assertIsNone(self.policy.evaluate(view(25, connected=False)))
        self.assertIsNotNone(self.policy.evaluate(view(25)))

    def test_forget_clears_state(self):
        self.assertIsNotNone(self.policy.evaluate(view(25)))
        self.policy.forget("k1")
        self.assertIsNotNone(self.policy.evaluate(view(25)))

    def test_first_sight_low_battery_fires_only_one_alert(self):
        """手柄一插上就是 5%，不应连发 30/20/10 三条提醒。"""
        alert = self.policy.evaluate(view(5))
        self.assertIsNotNone(alert)
        self.assertEqual(alert.threshold, 10)
        self.assertIsNone(self.policy.evaluate(view(5)))
        self.assertEqual(self.policy.armed_bands("k1"), {30, 20, 10})

    def test_unknown_battery_never_alerts(self):
        self.assertIsNone(self.policy.evaluate(view(None)))

    def test_error_state_never_alerts(self):
        self.assertIsNone(self.policy.evaluate(view(None, ChargeState.ERROR)))

    def test_per_controller_state_is_independent(self):
        self.assertIsNotNone(self.policy.evaluate(view(25, key="a")))
        self.assertIsNotNone(self.policy.evaluate(view(25, key="b")))
        self.assertIsNone(self.policy.evaluate(view(25, key="a")))
        self.assertIsNone(self.policy.evaluate(view(25, key="b")))

    def test_custom_thresholds(self):
        policy = LowBatteryPolicy([40, 15])
        self.assertIsNone(policy.evaluate(view(45)))
        self.assertIsNotNone(policy.evaluate(view(35)))     # 跨 40
        self.assertIsNone(policy.evaluate(view(35)))
        self.assertIsNotNone(policy.evaluate(view(5)))      # 跨 15
        self.assertIsNone(policy.evaluate(view(5)))

    def test_disabled_policy(self):
        policy = LowBatteryPolicy([30], enabled=False)
        self.assertIsNone(policy.evaluate(view(5)))

    def test_empty_thresholds_falls_back(self):
        policy = LowBatteryPolicy([])
        self.assertTrue(policy.thresholds)

    def test_alert_text_has_no_fake_precision(self):
        alert = self.policy.evaluate(view(25))
        self.assertIn("约", alert.one_line())


if __name__ == "__main__":
    unittest.main(verbosity=2)
