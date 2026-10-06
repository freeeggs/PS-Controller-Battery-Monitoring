# -*- coding: utf-8 -*-
"""图标绘制、配置、自启动与模拟器的测试。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from psbt import autostart  # noqa: E402
from psbt import theme  # noqa: E402

#: 注册表测试专用的值名。**绝不能**用 autostart.VALUE_NAME，
#: 否则会读写用户真实的自启动配置（有 test_test_value_name_is_not_the_real_one 兜底）。
TEST_VALUE_NAME = "PSBatteryTray__TestOnly"
from psbt.config import Config  # noqa: E402
from psbt.controllers.models import (  # noqa: E402
    FAMILY_DS4,
    FAMILY_DUALSENSE,
    UNKNOWN_BATTERY,
    BatteryReport,
    ChargeState,
    ControllerView,
    Transport,
)
from psbt.controllers.simulator import SCENARIOS, SimulatedManager  # noqa: E402
from psbt.tray import hicon, iconart, trayapp  # noqa: E402


class IconArtTests(unittest.TestCase):
    def test_fill_is_monotonic(self):
        painter = iconart.IconPainter(size=32)
        widths = []
        for percent in range(100, -1, -10):
            img = painter.render(percent, False, iconart.MODE_LEVEL)
            px = img.load()
            widths.append(sum(1 for x in range(32) if px[x, int(32 * 0.45)][3] > 200))
        # 每档应严格不增（100% 最宽，0% 最窄）
        for a, b in zip(widths, widths[1:]):
            self.assertGreaterEqual(a, b, "填充宽度必须随电量单调不增")

    def test_100_percent_is_full_and_0_is_empty(self):
        painter = iconart.IconPainter(size=32)
        full = painter.render(100, False, iconart.MODE_LEVEL).load()
        empty = painter.render(0, False, iconart.MODE_LEVEL).load()
        row = int(32 * 0.45)
        full_pixels = sum(1 for x in range(32) if full[x, row][3] > 200)
        empty_pixels = sum(1 for x in range(32) if empty[x, row][3] > 200)
        self.assertGreater(full_pixels, empty_pixels)

    def test_all_variants_render_at_all_tray_sizes(self):
        for size in (16, 20, 24, 32):
            painter = iconart.IconPainter(size=size)
            for mode in (iconart.MODE_LEVEL, iconart.MODE_NONE, iconart.MODE_UNKNOWN,
                         iconart.MODE_ALERT):
                img = painter.render(40, False, mode)
                self.assertEqual(img.size, (size, size))
                self.assertEqual(img.mode, "RGBA")
                self.assertIsNotNone(img.getbbox(), "%s 模式在 %dpx 下不能是空白图" % (mode, size))

    def test_none_and_level_differ(self):
        painter = iconart.IconPainter(size=32)
        a = painter.render(None, False, iconart.MODE_NONE).tobytes()
        b = painter.render(50, False, iconart.MODE_LEVEL).tobytes()
        self.assertNotEqual(a, b)

    def test_icon_for_picks_lowest_battery(self):
        painter = iconart.IconPainter(size=32)
        views = [
            _view("a", 80), _view("b", 45), _view("c", 20),
        ]
        lowest_icon = iconart.icon_for(views, painter).tobytes()
        self.assertEqual(lowest_icon, painter.render(20, False, iconart.MODE_LEVEL).tobytes())

    def test_icon_for_no_controller_is_none_icon(self):
        painter = iconart.IconPainter(size=32)
        self.assertEqual(iconart.icon_for([], painter).tobytes(),
                         painter.render(None, False, iconart.MODE_NONE).tobytes())

    def test_icon_for_unknown_battery_uses_unknown_icon(self):
        painter = iconart.IconPainter(size=32)
        views = [ControllerView(key="x", family=FAMILY_DS4, display_name="DualShock 4",
                               transport=Transport.USB, vendor_id=0x054C, product_id=0x09CC)]
        self.assertEqual(iconart.icon_for(views, painter).tobytes(),
                         painter.render(None, False, iconart.MODE_UNKNOWN).tobytes())

    def test_alert_mode_uses_accent_color(self):
        painter = iconart.IconPainter(size=32, accent=True)
        img = painter.render(5, False, iconart.MODE_ALERT)
        colors = {c for _count, c in img.getcolors(4096) or []}
        self.assertTrue(any(c[0] > 180 and c[1] < 80 for c in colors), "10% 告警应为红色系")

    def test_tooltip_truncated(self):
        views = [_view("k%d" % i, 50) for i in range(12)]
        text = iconart.tooltip_for(views)
        self.assertLessEqual(len(text), 127, "Windows 托盘提示有 127 字符上限")

    def test_tooltip_no_controller(self):
        self.assertIn("未检测到手柄", iconart.tooltip_for([]))

    def test_application_icon_is_multisize_ico(self):
        import io

        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app.ico")
            iconart.save_ico(path)
            with Image.open(path) as im:
                sizes = sorted(im.ico.sizes())
            self.assertIn((16, 16), sizes)
            self.assertIn((256, 256), sizes)
        del io


class IconSharpnessTests(unittest.TestCase):
    """托盘图标锐利度回归：锁定几何、硬边、像素对齐，以及 HICON 不被缩放。"""

    def test_16px_matches_user_reference_matrix(self):
        """1080P/100% 下必须**逐像素**等于用户提供的基准图（电量 65%）。"""
        img = iconart.IconPainter(size=16).render(65, False, iconart.MODE_LEVEL)
        self.assertEqual(iconart.alpha_matrix(img),
                         list(iconart.REFERENCE_16_AT_65))

    def test_16px_reference_has_no_intermediate_alpha(self):
        """基准图只有 0/255 两种 alpha —— 有任何中间值就说明混进了抗锯齿。"""
        img = iconart.IconPainter(size=16).render(65, False, iconart.MODE_LEVEL)
        alphas = {a for _r, _g, _b, a in img.convert("RGBA").getdata()}
        self.assertEqual(alphas, {0, 255})

    def test_every_size_is_hard_edged(self):
        """所有目标 DPI 尺寸下都只能有 0/255 两种 alpha（无羽化、无灰边）。"""
        for size in (16, 20, 24, 32, 40, 48, 64):
            painter = iconart.IconPainter(size=size)
            for mode in (iconart.MODE_LEVEL, iconart.MODE_ALERT, iconart.MODE_UNKNOWN):
                img = painter.render(45, False, mode)
                self.assertTrue(iconart.has_only_hard_alpha(img),
                                "%dpx / %s 出现了中间 alpha（抗锯齿）" % (size, mode))

    def test_frame_stroke_is_symmetric_at_every_size(self):
        """外框四周必须等宽：不允许出现「左边 1px、右边 2px」的歪框。"""
        for size in (16, 20, 24, 28, 32, 40, 48, 64):
            geo = iconart.Geometry(size)
            bx0, by0, bx1, by1 = geo.body
            left, right = geo.sw, geo.sw
            top, bottom = geo.sw, geo.sw
            self.assertEqual(left, right)
            self.assertEqual(top, bottom)
            # 内腔必须严格在外框内部，且电量块与内腔之间的留白 = 一个线宽
            self.assertGreater(geo.cavity[2] - geo.cavity[0], 0)
            self.assertGreater(geo.charge[2] - geo.charge[0], 0)
            self.assertEqual(geo.charge[0] - geo.cavity[0], geo.sw)
            self.assertEqual(geo.charge[1] - geo.cavity[1], geo.sw)
            self.assertEqual(geo.cavity[2] - geo.charge[2], geo.sw)
            self.assertEqual(geo.cavity[3] - geo.charge[3], geo.sw)
            # 正极必须在外框右侧、垂直居中
            self.assertEqual(geo.nub[0], bx1)
            self.assertEqual(geo.nub[2], size)
            self.assertLessEqual(abs((geo.nub[1] + geo.nub[3]) - (by0 + by1)), 1)

    def test_stroke_width_scales_with_dpi(self):
        """线宽随尺寸取整，不做插值：16px→1、32px→2、48px→3。"""
        self.assertEqual(iconart.Geometry(16).sw, 1)
        self.assertEqual(iconart.Geometry(20).sw, 1)
        self.assertEqual(iconart.Geometry(24).sw, 2)
        self.assertEqual(iconart.Geometry(32).sw, 2)
        self.assertEqual(iconart.Geometry(48).sw, 3)
        self.assertEqual(iconart.Geometry(64).sw, 4)

    def test_fill_widths_are_monotonic_and_cover_11_levels(self):
        """11 档电量必须对应 0~10 格宽，且严格单调不减（不出现倒吸）。"""
        for size in (16, 20, 24, 32):
            geo = iconart.Geometry(size)
            widths = [geo.fill_box(u)[2] - geo.fill_box(u)[0] for u in range(11)]
            self.assertEqual(widths[0], 0, "0% 不应有填充")
            self.assertEqual(widths[-1], geo.charge[2] - geo.charge[0],
                             "100% 必须填满电量区")
            for a, b in zip(widths, widths[1:]):
                self.assertLessEqual(a, b, "填充宽度必须随电量单调不减")

    def test_hicon_is_exact_tray_size_not_default_icon_size(self):
        """HICON 尺寸必须 == SM_CXSMICON，而不是 SM_CXICON。

        pystray 原来用 ``LoadImage(..., 0, 0, LR_DEFAULTSIZE)`` 会拿到
        ``SM_CXICON``（32）的图，通知区域再缩回 ``SM_CXSMICON``（16），
        两次重采样必然糊。这里把它锁死。
        """
        small, large = hicon.icon_metrics()
        img = iconart.IconPainter(size=small).render(65, False, iconart.MODE_LEVEL)
        h = hicon.create_hicon(img)
        try:
            self.assertEqual(hicon.hicon_bitmap_size(h), (small, small))
            if small != large:      # 100% 缩放下 16 != 32，正好验证没有走默认尺寸
                self.assertNotEqual(hicon.hicon_bitmap_size(h), (large, large))
        finally:
            hicon.destroy_hicon(h)

    def test_hicon_pixels_match_source_exactly(self):
        """回读 HICON 像素，必须与源图逐像素一致（证明零缩放、零插值）。"""
        img = iconart.IconPainter(size=16).render(65, False, iconart.MODE_LEVEL)
        h = hicon.create_hicon(img)
        try:
            self.assertEqual(hicon.hicon_alpha_matrix(h),
                             iconart.alpha_matrix(img))
        finally:
            hicon.destroy_hicon(h)

    def test_hicon_rejects_non_square(self):
        from PIL import Image as _Image
        with self.assertRaises(ValueError):
            hicon.create_hicon(_Image.new("RGBA", (16, 10), (255, 255, 255, 255)))

    def test_charging_shows_real_level_not_bolt(self):
        """充电时图标必须显示**真实电量**，而不是画满块 + 闪电。

        早先充电会画满块并抠出闪电（跟随 Windows 系统电池图标），但闪电盖住了
        电量条，用户没法一眼看出充到多少 —— 已改为与放电时同一套电量填充。
        """
        for size in (16, 20, 24, 32):
            painter = iconart.IconPainter(size=size)
            for percent in (5, 45, 85, 100):
                charging = painter.render(percent, True, iconart.MODE_LEVEL)
                plain = painter.render(percent, False, iconart.MODE_LEVEL)
                self.assertEqual(
                    charging.tobytes(), plain.tobytes(),
                    "%dpx / %d%%：充电与放电的图标必须完全一致" % (size, percent))

    def test_charging_20_percent_is_a_short_fill_that_grows(self):
        """充电时填充宽度应随电量单调增长（直观反映充了多少）。"""
        painter = iconart.IconPainter(size=16)
        widths = []
        for percent in (5, 15, 25, 45, 65, 85, 100):
            img = painter.render(percent, True, iconart.MODE_LEVEL)
            row = iconart.alpha_matrix(img)[7]          # 电量块所在行
            widths.append(row.count("#"))
        for a, b in zip(widths, widths[1:]):
            self.assertLessEqual(a, b, "充电时填充宽度也必须随电量单调不减")
        self.assertLess(widths[0], widths[-1], "5%% 与 100%% 必须有明显差异")
        # 该行 = 左外框 1 + 电量块 u + 右外框 1 + 正极 2 ⇒ 满档比 0 档宽 10
        self.assertEqual(widths[-1] - widths[0], 10,
                         "16px 下 0 档到满档的电量块宽度应相差 10 格")

    def test_none_and_unknown_states_use_pure_primary_color(self):
        """「无手柄」「电量未知」必须与正常状态**同色**：只有透明与主色两种像素。

        这两个状态早先用整体半透明（alpha 105 / 200）表达"未激活"，在深色任务栏
        上看起来就是发灰。现在改由**图形**区分（斜杠 / 横杠），颜色一律为主色。
        """
        for primary in (iconart.COLOR_LIGHT, iconart.COLOR_DARK):
            painter = iconart.IconPainter(primary=primary, size=16)
            for mode in (iconart.MODE_NONE, iconart.MODE_UNKNOWN):
                img = painter.render(None, False, mode).convert("RGBA")
                self.assertEqual(iconart.alpha_values(img), {0, 255},
                                 "%s 的 alpha 必须是纯二值（不得有半透明）" % mode)
                rgb = {px[:3] for px in img.getdata() if px[3] == 255}
                self.assertEqual(rgb, {primary[:3]},
                                 "%s 的不透明像素必须全是主色 %s，实际 %s"
                                 % (mode, primary[:3], sorted(rgb)))

    def test_all_four_modes_share_the_same_alpha_set(self):
        """四种状态的 alpha 集合必须完全一致，避免任何状态"发灰"。"""
        painter = iconart.IconPainter(size=32)
        sets = {mode: iconart.alpha_values(painter.render(45, False, mode))
                for mode in (iconart.MODE_LEVEL, iconart.MODE_NONE,
                             iconart.MODE_UNKNOWN, iconart.MODE_ALERT)}
        for mode, values in sets.items():
            self.assertEqual(values, {0, 255}, "%s 的 alpha 集合=%s" % (mode, values))


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = Config().normalized()
        self.assertEqual(cfg.low_battery_thresholds, [30, 20, 10])
        self.assertGreaterEqual(cfg.scan_interval_idle, cfg.scan_interval_active,
                                "无手柄时应比有手柄时更省电")

    def test_thresholds_sorted_and_deduped(self):
        cfg = Config().normalized()
        cfg.low_battery_thresholds = [10, 30, 20, 30, "x", 0, 200]
        cfg = cfg.normalized()
        self.assertEqual(cfg.low_battery_thresholds, [30, 20, 10])

    def test_invalid_values_clamped(self):
        cfg = Config()
        cfg.scan_interval_idle = -5
        cfg.report_min_gap = "abc"
        cfg.log_level = "chatty"
        cfg.alert_popup = "sometimes"
        cfg = cfg.normalized()
        self.assertGreater(cfg.scan_interval_idle, 0)
        self.assertGreater(cfg.report_min_gap, 0)
        self.assertEqual(cfg.log_level, "INFO")
        self.assertEqual(cfg.alert_popup, "auto")

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            cfg = Config()
            cfg.alert_popup_seconds = 25
            self.assertTrue(cfg.save(path))
            loaded = Config.load(path)
            self.assertEqual(loaded.alert_popup_seconds, 25)

    def test_broken_config_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{ this is not json")
            cfg = Config.load(path)
            self.assertEqual(cfg.low_battery_thresholds, [30, 20, 10])
            self.assertTrue(os.path.exists(path + ".broken"))

    def test_unknown_keys_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"unknown_key": 1, "alert_popup_seconds": 7}, fh)
            cfg = Config.load(path)
            self.assertEqual(cfg.alert_popup_seconds, 7)


class AutostartTests(unittest.TestCase):
    """自启动读写测试。

    .. danger::
       下面几个 test 会**真的读写注册表**，因此一律使用 :data:`TEST_VALUE_NAME`
       这个一次性值名，并在 finally 里 ``restore_value()`` 精确还原。
       历史教训：曾经直接用程序自己的值名 ``PSBatteryTray`` 读写用户的
       ``HKCU\\...\\Run``，还原时又调 ``enable()``（写入当前构建的命令而非原值），
       于是**跑测试 / 跑 build.ps1 就会把用户的自定义自启动命令覆盖掉**。
    """

    def test_command_contains_program_and_flag(self):
        cmd = autostart.build_command()
        self.assertIn("--autostart", cmd)
        exe = autostart.command_line()[0]
        self.assertIn(exe, cmd.replace('"', ""))

    def test_paths_with_spaces_are_quoted(self):
        cmd = autostart.build_command([r"C:\Program Files\PSBatteryTray\PSBatteryTray.exe"])
        self.assertTrue(cmd.startswith('"C:\\Program Files\\PSBatteryTray\\PSBatteryTray.exe"'))
        self.assertTrue(cmd.endswith("--autostart"))

    def test_roundtrip_registry(self):
        """在**专用测试值名**下走一遍真实的注册表读写。

        .. danger::
           **不要用程序自己的值名去测**。曾经这里用 ``PSBatteryTray`` 直接读写
           用户的 ``HKCU\\...\\Run``，而还原用的是 ``enable()`` —— 那是"写入当前
           构建的命令"，不是"恢复原值"，所以用户自定义的自启动命令（旧路径、
           附加参数）会被静默覆盖。``build.ps1`` 默认会跑测试，等于**每次构建
           都可能改掉用户的开机启动配置**。

           现在改为：一次性测试值名 + ``restore_value()`` 精确还原。
        """
        if not autostart.is_available():
            self.skipTest("非 Windows 环境")
        original = autostart.current_value(TEST_VALUE_NAME)
        try:
            self.assertTrue(autostart.enable(TEST_VALUE_NAME))
            self.assertTrue(autostart.is_enabled(TEST_VALUE_NAME))
            self.assertTrue(autostart.points_to_current_build(TEST_VALUE_NAME))
            self.assertTrue(autostart.disable(TEST_VALUE_NAME))
            self.assertFalse(autostart.is_enabled(TEST_VALUE_NAME))
        finally:
            autostart.restore_value(original, TEST_VALUE_NAME)

    def test_disable_when_absent_is_ok(self):
        if not autostart.is_available():
            self.skipTest("非 Windows 环境")
        original = autostart.current_value(TEST_VALUE_NAME)
        try:
            autostart.disable(TEST_VALUE_NAME)
            self.assertTrue(autostart.disable(TEST_VALUE_NAME))
        finally:
            autostart.restore_value(original, TEST_VALUE_NAME)

    def test_restore_value_writes_back_exact_text(self):
        """还原必须是"原样写回"，而不是写成当前构建的命令行。"""
        if not autostart.is_available():
            self.skipTest("非 Windows 环境")
        custom = r'"D:\Old Version\PSBatteryTray.exe" --autostart --debug'
        original = autostart.current_value(TEST_VALUE_NAME)
        try:
            self.assertTrue(autostart.write_raw(custom, TEST_VALUE_NAME))
            self.assertEqual(autostart.current_value(TEST_VALUE_NAME), custom)
            self.assertTrue(autostart.enable(TEST_VALUE_NAME))     # 模拟被覆盖
            self.assertNotEqual(autostart.current_value(TEST_VALUE_NAME), custom)
            self.assertTrue(autostart.restore_value(custom, TEST_VALUE_NAME))
            self.assertEqual(autostart.current_value(TEST_VALUE_NAME), custom,
                             "必须精确还原原始字符串，不能用 enable() 顶替")
        finally:
            autostart.restore_value(original, TEST_VALUE_NAME)

    def test_restore_value_none_removes_entry(self):
        """原来不存在的项，还原后必须仍然不存在。"""
        if not autostart.is_available():
            self.skipTest("非 Windows 环境")
        original = autostart.current_value(TEST_VALUE_NAME)
        try:
            autostart.enable(TEST_VALUE_NAME)
            self.assertTrue(autostart.is_enabled(TEST_VALUE_NAME))
            self.assertTrue(autostart.restore_value(None, TEST_VALUE_NAME))
            self.assertFalse(autostart.is_enabled(TEST_VALUE_NAME))
        finally:
            autostart.restore_value(original, TEST_VALUE_NAME)

    def test_test_value_name_is_not_the_real_one(self):
        """护栏：测试值名绝不能等于程序自己的值名，否则又会污染真实配置。"""
        self.assertNotEqual(TEST_VALUE_NAME, autostart.VALUE_NAME,
                            "测试必须使用专用值名，不能碰用户真实的自启动项")


class SimulatorTests(unittest.TestCase):
    def test_full_scenario_produces_expected_events(self):
        """逐步驱动整条时间线，验证连接/断开/告警次数与快照内容。"""
        from psbt.controllers.manager import ManagerEvents
        from psbt.notify.policy import LowBatteryPolicy

        snapshots = []
        connected = []
        disconnected = []
        policy = LowBatteryPolicy([30, 20, 10])
        alerts = []

        events = ManagerEvents(
            on_snapshot=lambda views: snapshots.append(list(views)),
            on_connected=connected.append,
            on_disconnected=disconnected.append,
        )
        timeline = SCENARIOS["quick"]()
        manager = SimulatedManager(Config(), events, timeline=timeline)

        def on_snapshot(views):
            snapshots.append(list(views))
            for view in views:
                alert = policy.evaluate(view)
                if alert is not None:
                    alerts.append(alert)

        manager.events.on_snapshot = on_snapshot

        manager.start()
        try:
            for _ in range(len(timeline) + 2):
                manager.rescan_now()
        finally:
            manager.stop()

        self.assertGreaterEqual(len(connected), 3, "应至少发生 3 次连接（含重连）")
        self.assertGreaterEqual(len(disconnected), 2, "应至少发生 2 次断开")
        self.assertTrue(any(len(s) == 2 for s in snapshots), "应出现过 2 台手柄同时在线的快照")
        self.assertTrue(any(len(s) == 0 for s in snapshots), "应出现过无手柄的快照")
        self.assertGreaterEqual(len(alerts), 3, "低电量提醒应至少触发 3 次")
        self.assertEqual(sorted({a.threshold for a in alerts}), [10, 20, 30],
                         "30/20/10 三个台阶都应各自触发过")

    def test_simulator_never_repeats_same_band(self):
        """时间线里 2% 档重复出现两次，只应提醒一次。"""
        from psbt.controllers.manager import ManagerEvents
        from psbt.notify.policy import LowBatteryPolicy

        policy = LowBatteryPolicy([30, 20, 10])
        alerts = []

        def on_snapshot(views):
            for view in views:
                alert = policy.evaluate(view)
                if alert is not None:
                    alerts.append(alert)

        timeline = [
            (0, ("connect", "dualsense", 2)),
            (1, ("set", "dualsense", 2)),
            (2, ("set", "dualsense", 2)),
            (3, ("set", "dualsense", 1)),
            (4, ("set", "dualsense", 1)),
        ]
        manager = SimulatedManager(Config(), ManagerEvents(on_snapshot=on_snapshot),
                                   timeline=timeline)
        manager.start()
        try:
            for _ in range(len(timeline) + 2):
                manager.rescan_now()
        finally:
            manager.stop()

        self.assertEqual(len(alerts), 2, "等级 2 与等级 1 各提醒一次，重复等级不再提醒")
        self.assertEqual([a.threshold for a in alerts], [30, 20])

    def test_lowest_battery_is_first_in_snapshot(self):
        manager = SimulatedManager(Config(), timeline=[(0, ("connect", "ds4", 8))])
        manager.start()
        try:
            time.sleep(0.2)
            manager._connect(FAMILY_DUALSENSE, 2)   # noqa: SLF001
            views = manager.snapshot()
            self.assertEqual(len(views), 2)
            self.assertEqual(views[0].percent, 25, "快照应按电量升序，最低的排第一")
        finally:
            manager.stop()


def _view(key, percent, family=FAMILY_DUALSENSE):
    return ControllerView(
        key=key, family=family, display_name="DualSense (PS5)",
        transport=Transport.BLUETOOTH, vendor_id=0x054C, product_id=0x0CE6,
        battery=BatteryReport((percent - 5) // 10, percent, ChargeState.DISCHARGING, "t"),
    )


def _unknown_view(key, family=FAMILY_DUALSENSE):
    """刚接入、还没读到电量时的视图（这正是真机上先到达的那一条快照）。"""
    return ControllerView(
        key=key, family=family, display_name="DualSense (PS5)",
        transport=Transport.USB, vendor_id=0x054C, product_id=0x0CE6,
        battery=UNKNOWN_BATTERY,
    )


class IconStyleTests(unittest.TestCase):
    """两套图标风格：win10 直角 / win11 圆角。"""

    def test_win10_is_the_reference_anchor(self):
        """win10 风格必须与用户给定的基准图逐像素一致（不能因加风格而跑偏）。"""
        img = iconart.IconPainter(size=16, style=iconart.STYLE_WIN10).render(
            65, False, iconart.MODE_LEVEL)
        self.assertEqual(tuple(iconart.alpha_matrix(img)),
                         iconart.REFERENCE_16_AT_65)

    def test_win11_corners_are_rounded(self):
        """win11 的四角必须是切掉的，win10 的四角必须是满的。"""
        a = iconart.alpha_matrix(iconart.IconPainter(
            size=16, style=iconart.STYLE_WIN10).render(65, False, iconart.MODE_LEVEL))
        b = iconart.alpha_matrix(iconart.IconPainter(
            size=16, style=iconart.STYLE_WIN11).render(65, False, iconart.MODE_LEVEL))
        self.assertNotEqual(a, b, "两种风格必须画得不一样")
        # 外框左上/右上/左下/右下四个角的像素
        for (row, col) in ((3, 0), (3, 13), (12, 0), (12, 13)):
            self.assertEqual(a[row][col], "#", "win10 的角应是直角")
            self.assertEqual(b[row][col], ".", "win11 的角应被切掉")

    def test_win11_geometry_matches_win10(self):
        """除四角外，两种风格应当完全一致 —— 电量表现方式不能变。"""
        a = iconart.alpha_matrix(iconart.IconPainter(
            size=16, style=iconart.STYLE_WIN10).render(65, False, iconart.MODE_LEVEL))
        b = iconart.alpha_matrix(iconart.IconPainter(
            size=16, style=iconart.STYLE_WIN11).render(65, False, iconart.MODE_LEVEL))
        for y, (ra, rb) in enumerate(zip(a, b)):
            if y in (3, 12):                       # 只有最上/最下一行有区别
                continue
            self.assertEqual(ra, rb, "第 %d 行不应有差异" % y)

    def test_both_styles_keep_hard_alpha(self):
        """圆角**不得**引入抗锯齿：两种风格、各尺寸、各状态都只有 0/255。"""
        for style in iconart.STYLES:
            for size in (16, 20, 24, 32, 40, 48):
                painter = iconart.IconPainter(size=size, style=style)
                for (pct, mode) in ((5, iconart.MODE_LEVEL), (45, iconart.MODE_LEVEL),
                                    (95, iconart.MODE_LEVEL), (None, iconart.MODE_NONE),
                                    (None, iconart.MODE_UNKNOWN), (15, iconart.MODE_ALERT)):
                    img = painter.render(pct, False, mode)
                    self.assertEqual(iconart.alpha_values(img), {0, 255},
                                     "%s/%dpx/%s 出现了中间 alpha" % (style, size, mode))

    def test_win11_fill_still_monotonic(self):
        """圆角不影响电量表达：填充宽度仍随档位单调不减。"""
        painter = iconart.IconPainter(size=16, style=iconart.STYLE_WIN11)
        widths = []
        for pct in (5, 25, 45, 65, 85, 100):
            row = iconart.alpha_matrix(painter.render(pct, False, iconart.MODE_LEVEL))[7]
            widths.append(row.count("#"))
        for x, y in zip(widths, widths[1:]):
            self.assertLessEqual(x, y, "win11 填充宽度也必须单调不减")
        self.assertEqual(widths[-1] - widths[0], 10)

    def test_radius_grows_with_size(self):
        """圆角半径随像素尺寸取整增大，且不超过半高/半宽。"""
        radii = [iconart.Geometry(s, iconart.STYLE_WIN11).radius
                 for s in (16, 20, 24, 32, 48)]
        self.assertEqual(radii, sorted(radii), "半径应随尺寸单调不减：%s" % radii)
        for s, r in zip((16, 20, 24, 32, 48), radii):
            geo = iconart.Geometry(s, iconart.STYLE_WIN11)
            self.assertGreaterEqual(r, 1)
            self.assertLessEqual(r, geo.gh // 2)
        self.assertEqual(iconart.Geometry(16, iconart.STYLE_WIN10).radius, 0)

    def test_config_style_defaults_and_sanitises(self):
        cfg = Config()
        self.assertEqual(cfg.icon_style, iconart.STYLE_WIN10)
        self.assertEqual(Config(icon_style="win11").normalized().icon_style,
                         iconart.STYLE_WIN11)
        self.assertEqual(Config(icon_style="bogus").normalized().icon_style,
                         iconart.STYLE_WIN10)

    def test_menu_exposes_style_switch(self):
        """右键菜单里必须有「图标风格」子菜单，且勾选状态跟随配置。"""
        app = trayapp.TrayApp(Config(), trayapp.TrayCallbacks(), throttle=0.0)
        try:
            items = list(app._menu_items())                # noqa: SLF001
            style_item = [i for i in items if i.text == "图标风格"]
            self.assertEqual(len(style_item), 1, "菜单里应有一个「图标风格」项")
            sub = style_item[0].submenu
            self.assertIsNotNone(sub, "「图标风格」应是子菜单")
            labels = [i.text for i in sub.items]
            for style in iconart.STYLES:
                self.assertIn(iconart.STYLE_LABELS[style], labels)
        finally:
            app.stop()

    def test_switching_style_repaints_icon(self):
        """切换风格后图标必须立刻换成新风格（并把选择写回配置）。"""
        cfg = Config()
        app = trayapp.TrayApp(cfg, trayapp.TrayCallbacks(), throttle=0.0)
        try:
            app.update([_view("k", 65)])
            before = _icon_bytes(app)
            want_win11 = iconart.IconPainter(
                primary=app._painter.primary, size=app._painter.size,      # noqa: SLF001
                style=iconart.STYLE_WIN11).render(65, False, iconart.MODE_LEVEL).tobytes()
            self.assertNotEqual(before, want_win11, "两种风格的图标本就不同")

            app._set_icon_style(iconart.STYLE_WIN11)       # noqa: SLF001
            self.assertEqual(cfg.icon_style, iconart.STYLE_WIN11)
            self.assertEqual(_icon_bytes(app), want_win11,
                             "切换后托盘图标应变成 win11 风格")
        finally:
            app.stop()


class ThemeFollowTests(unittest.TestCase):
    """运行时切换系统亮/暗主题，图标必须及时跟随。"""

    def test_check_interval_is_short(self):
        """探测间隔必须远小于人眼耐心 —— 30 秒在体验上等同于「不跟随」。"""
        self.assertLessEqual(theme._CHECK_INTERVAL, 2.0)   # noqa: SLF001

    def test_watcher_detects_change(self):
        """注册表值翻转后，refresh() 必须报告变化并把颜色换掉。"""
        original = theme.winapi.appdata_theme
        state = {"light": False}
        # 先装桩再构造，保证初始缓存与桩一致
        theme.winapi.appdata_theme = lambda name: state["light"]
        try:
            w = theme.ThemeWatcher()
            w._last_check = 0.0                            # noqa: SLF001
            self.assertFalse(w.refresh(), "值没变不应报告变化")

            state["light"] = True                          # 相当于用户切到亮色
            w._last_check = 0.0                            # noqa: SLF001
            self.assertTrue(w.refresh(), "亮暗翻转必须被探测到")
            self.assertTrue(w.theme.taskbar_light)
            self.assertEqual(w.theme.icon_color, (26, 26, 26, 255),
                             "浅色任务栏必须换成深色图标")

            state["light"] = False
            w._last_check = 0.0                            # noqa: SLF001
            self.assertTrue(w.refresh(), "切回暗色同样要被探测到")
            self.assertEqual(w.theme.icon_color, (255, 255, 255, 255))
        finally:
            theme.winapi.appdata_theme = original

    def test_color_flips_with_theme(self):
        light = theme.Theme(taskbar_light=True)
        dark = theme.Theme(taskbar_light=False)
        self.assertEqual(light.icon_color[:3], (26, 26, 26))
        self.assertEqual(dark.icon_color[:3], (255, 255, 255))


class StaleBatteryTests(unittest.TestCase):
    """超过 stale 超时后，旧电量不得继续作为当前读数展示。

    README 承诺"读不到就说读不到、不残留旧数字"。但早先的实现只往 note 里
    加了一句话，``view.battery`` 原封不动 —— "约 85% + 一句警告"依然是
    一个过期读数，用户会照着它决定要不要充电。
    """

    def _reader_with(self, age_seconds, battery=None):
        import time
        from psbt.controllers.manager import ControllerManager, _Reader
        from psbt.controllers.models import (
            FAMILY_DS4, BatteryReport, ChargeState, ControllerView,
            MutableController, Transport,
        )

        manager = ControllerManager(Config())
        view = ControllerView(
            key="k1", family=FAMILY_DS4, display_name="DualShock 4",
            transport=Transport.BLUETOOTH, vendor_id=0x054C, product_id=0x09CC,
            battery=battery or BatteryReport(8, 85, ChargeState.DISCHARGING, "test"),
        )
        record = MutableController(view=view)
        record.last_report_ts = time.monotonic() - age_seconds
        manager._records[view.key] = record       # noqa: SLF001
        return manager, record, _Reader(manager, record)

    def test_old_reading_is_invalidated(self):
        manager, record, reader = self._reader_with(120.0)
        reader._check_stale_battery()             # noqa: SLF001
        self.assertIsNone(record.view.percent,
                          "超时后不得再展示旧的 85%")
        self.assertIsNone(record.view.level)
        self.assertIn("长时间未收到数据", record.view.note)

    def test_fresh_reading_is_kept(self):
        manager, record, reader = self._reader_with(1.0)
        reader._check_stale_battery()             # noqa: SLF001
        self.assertEqual(record.view.percent, 85, "刚收到的读数必须保留")
        self.assertEqual(record.view.note, "")

    def test_stale_is_not_repeatedly_republished(self):
        manager, record, reader = self._reader_with(120.0)
        published = []
        manager.events.on_snapshot = published.append
        reader._check_stale_battery()             # noqa: SLF001
        first = len(published)
        self.assertGreaterEqual(first, 1)
        reader._check_stale_battery()             # noqa: SLF001
        self.assertEqual(len(published), first,
                         "已是未知+同一提示时不应重复推送")

    def test_valid_report_after_stale_clears_note(self):
        """数据恢复后必须既恢复读数、又清掉那条提示。"""
        from psbt.controllers.models import BatteryReport, ChargeState

        manager, record, reader = self._reader_with(120.0)
        reader._check_stale_battery()             # noqa: SLF001
        self.assertIn("长时间未收到数据", record.view.note)

        manager._update_battery(                   # noqa: SLF001
            record.view.key,
            BatteryReport(6, 65, ChargeState.DISCHARGING, "test"), b"", note="")
        self.assertEqual(record.view.percent, 65)
        self.assertEqual(record.view.note, "",
                         "拿到有效报告后必须能清掉旧提示")

    def test_update_battery_none_keeps_existing_note(self):
        """note=None 表示"不动提示"，与 note="" 明确区分。"""
        from psbt.controllers.models import BatteryReport, ChargeState

        manager, record, reader = self._reader_with(120.0)
        reader._check_stale_battery()             # noqa: SLF001
        kept = record.view.note
        manager._update_battery(                   # noqa: SLF001
            record.view.key,
            BatteryReport(7, 75, ChargeState.DISCHARGING, "test"), b"")
        self.assertEqual(record.view.note, kept, "note=None 不应改动提示")
        self.assertEqual(record.view.percent, 75)


class CompositeDeviceTests(unittest.TestCase):
    """复合设备（USB 无线适配器）不得把一个手柄显示成好几台。

    真机数据（DualSense 走 USB 适配器）：一个手柄暴露 4 个 HID 集合::

        up=1  u=5   Game Pad          ← 真正的主集合（能读到电量）
        up=1  u=6   Keyboard          ┐ 触控板附带的
        up=12 u=1   Consumer Control  ├ 三个集合，
        up=1  u=2   Mouse             ┘ 永远不会带电量

    它们分属 ``MI_03`` / ``MI_04`` 两个接口，HID 实例段也各不相同
    （``&0000`` / ``&0001`` / ``&0002``），所以按 HID 路径分组会得到 4 台设备 ——
    界面上就是"一台 75% + 三台电量未知"。
    """

    #: 真机抓到的 4 条路径（原样保留，回归测试直接用它们）
    REAL_PATHS = [
        (r"\\?\HID#VID_054C&PID_0CE6&MI_03#9&22b58c18&0&0000"
         r"#{4d1e55b2-f16f-11cf-88cb-001111000030}", 0x01, 0x05, 3),
        (r"\\?\HID#VID_054C&PID_0CE6&MI_04&Col01#9&6a2d811&0&0000"
         r"#{4d1e55b2-f16f-11cf-88cb-001111000030}\KBD", 0x01, 0x06, 4),
        (r"\\?\HID#VID_054C&PID_0CE6&MI_04&Col02#9&6a2d811&0&0001"
         r"#{4d1e55b2-f16f-11cf-88cb-001111000030}", 0x0C, 0x01, 4),
        (r"\\?\HID#VID_054C&PID_0CE6&MI_04&Col03#9&6a2d811&0&0002"
         r"#{4d1e55b2-f16f-11cf-88cb-001111000030}", 0x01, 0x02, 4),
    ]

    def _infos(self):
        from psbt.controllers.backend import HidDeviceInfo

        return [
            HidDeviceInfo(
                path=p.encode(), vendor_id=0x054C, product_id=0x0CE6,
                product_string="DualSense Wireless Controller",
                usage_page=up, usage=u, interface_number=iface, bus_type=1,
            )
            for (p, up, u, iface) in self.REAL_PATHS
        ]

    def test_touchpad_collections_are_not_controllers(self):
        from psbt.controllers import backend

        verdicts = [(i.usage_page, i.usage, backend.is_controller_collection(i))
                    for i in self._infos()]
        self.assertTrue(verdicts[0][2], "Game Pad 集合必须被认作手柄")
        self.assertFalse(verdicts[1][2], "键盘集合不是手柄")
        self.assertFalse(verdicts[2][2], "消费类集合不是手柄")
        self.assertFalse(verdicts[3][2], "鼠标集合不是手柄")

    def test_bluetooth_ds4_undefined_collection_is_kept(self):
        """蓝牙 DS4 报的是 up=0 u=0（未定义），不能被筛掉。"""
        from psbt.controllers.backend import HidDeviceInfo, is_controller_collection

        info = HidDeviceInfo(path=b"x", vendor_id=0x054C, product_id=0x09CC,
                             usage_page=0, usage=0, bus_type=2)
        self.assertTrue(is_controller_collection(info))

    def test_controller_candidates_drops_phantoms(self):
        from psbt.controllers import backend

        cands = backend.controller_candidates(self._infos())
        self.assertEqual(len(cands), 1, "只剩主集合")
        self.assertEqual((cands[0].usage_page, cands[0].usage), (0x01, 0x05))

    def test_controller_candidates_never_empties_a_device(self):
        """一个集合都不像手柄时不许把设备筛没（宁可保留原样）。"""
        from psbt.controllers import backend

        infos = [i for i in self._infos() if i.usage_page == 0x0C]
        cands = backend.controller_candidates(infos)
        self.assertEqual(len(cands), 1)

    def test_physical_device_id_merges_interfaces(self):
        """4 个集合必须归到同一个物理设备。"""
        from psbt.controllers import physid

        ids = {physid.physical_device_id(i.path) for i in self._infos()}
        if ids == {None}:
            self.skipTest("本机取不到物理设备 ID（cfgmgr32 不可用或非 Windows）")
        self.assertEqual(len(ids), 1, "同一手柄的多个接口应归为一台：%s" % ids)
        self.assertTrue(list(ids)[0].upper().startswith("USB\\"))

    def test_device_key_groups_by_physical_device(self):
        """device_key 必须一致 —— 这是界面只显示一台的关键。"""
        from psbt.controllers import backend, physid
        from psbt.controllers.models import Transport

        infos = self._infos()
        if physid.physical_device_id(infos[0].path) is None:
            self.skipTest("本机取不到物理设备 ID")
        keys = {i.device_key for i in infos}
        self.assertEqual(len(keys), 1, "4 个集合应共用一个 device_key：%s" % keys)

    def test_device_key_falls_back_to_instance_segment(self):
        """MAC 与物理设备都拿不到时，回退到 HID 实例段（行为与旧版一致）。"""
        from psbt.controllers import backend, physid

        infos = self._infos()
        orig_phys = physid.physical_device_id
        orig_mac = backend.controller_mac
        try:
            physid.physical_device_id = lambda path: None
            backend.controller_mac = lambda info: None
            backend.clear_mac_cache()
            keys = [i.device_key for i in infos]
        finally:
            physid.physical_device_id = orig_phys
            backend.controller_mac = orig_mac
            backend.clear_mac_cache()
        self.assertEqual(len(set(keys)), 4, "回退后每集合各成一组（旧行为）")
        for k in keys:
            self.assertIn("&", k.split("#")[-1])

    def test_mac_is_resolved_once_per_physical_device(self):
        """★ MAC 必须**按物理设备**解析，不是按集合。

        曾经的实现按设备路径缓存，于是可能出现"主集合读到了 MAC、触控板集合
        没读到" —— 同一个手柄算出两个分组键，1.5 修掉的"一个手柄显示成好几台"
        就会回来。这里让除主集合外的路径都打不开，四个集合仍必须共用同一个 key。

        .. note::
           物理设备 ID 显式打桩：真实路径对应的是**上一次**插拔的 devnode，
           机器状态一变结果就不同，测试不能依赖它。
        """
        from psbt.controllers import backend, physid

        infos = self._infos()
        payload = bytes.fromhex("09 4E B3 82 56 27 0C 08 25 00 DA 47 64 28 AF 44 00 00 00 00")
        opened = []

        class _FakeHandle:
            def __init__(self, info):
                self.info = info

            def open(self):
                # 只有主游戏控制器集合（MI_03）能打开
                ok = b"MI_03" in self.info.path
                opened.append(ok)
                return ok

            def get_feature_report(self, report_id, length=64):
                return payload if report_id == 0x09 else None

            def close(self):
                pass

        orig_handle = backend.HidHandle
        orig_phys = physid.physical_device_id
        try:
            backend.HidHandle = _FakeHandle
            physid.physical_device_id = lambda path: r"USB\VID_054C&PID_0CE6\STUB"
            backend.clear_mac_cache()
            keys = {i.device_key for i in infos}
        finally:
            backend.HidHandle = orig_handle
            physid.physical_device_id = orig_phys
            backend.clear_mac_cache()
        self.assertEqual(len(keys), 1,
                         "四个集合必须共用一个 key：%s" % keys)
        self.assertIn("MAC:0c275682b34e", keys.pop())

    def test_hid_instance_id_parsing(self):
        from psbt.controllers import physid

        self.assertEqual(
            physid.hid_instance_id(self.REAL_PATHS[1][0]),
            r"HID\VID_054C&PID_0CE6&MI_04&Col01\9&6a2d811&0&0000")
        self.assertEqual(
            physid.hid_instance_id(rb"\\?\HID#VID_054C&PID_09CC#7&abc&0&0000#{g}"),
            "HID\\VID_054C&PID_09CC\\7&abc&0&0000")
        self.assertIsNone(physid.hid_instance_id("not-a-hid-path"))
        self.assertIsNone(physid.hid_instance_id(b""))

    def test_hub_and_interface_nodes_are_not_devices(self):
        from psbt.controllers import physid

        # 接口节点
        self.assertFalse(physid._is_physical_usb_device(          # noqa: SLF001
            r"USB\VID_054C&PID_0CE6&MI_04\8&1DF12F96&1&0004"))
        # HUB 节点
        self.assertFalse(physid._is_physical_usb_device(          # noqa: SLF001
            r"USB\ROOT_HUB30\5&201A262C&0&0"))
        self.assertFalse(physid._is_physical_usb_device(          # noqa: SLF001
            r"USB\VID_1A40&PID_0101\6&28CF390B&0&12"))
        # 蓝牙节点
        self.assertFalse(physid._is_physical_usb_device(          # noqa: SLF001
            r"BTHENUM\{00001124-0000-1000-8000-00805f9b34fb}_VID&0002054c_PID&09cc\7&a&0&0000"))
        # 真设备
        self.assertTrue(physid._is_physical_usb_device(           # noqa: SLF001
            r"USB\VID_054C&PID_0CE6\1C784B8B409B0000"))

    def test_bluetooth_parent_chain_never_reaches_the_radio(self):
        """★ 关键防线：蓝牙设备的父链上出现非 USB 节点时必须立刻停手。

        否则会一路爬到**蓝牙适配器**（它本身挂在 USB 总线上），
        把同一台机器的所有蓝牙手柄并成一台 —— 比多几个幽灵条目严重得多。
        """
        from psbt.controllers import physid

        self.assertFalse(physid._is_usb_node(                     # noqa: SLF001
            r"BTHENUM\{00001124-0000-1000-8000-00805f9b34fb}_VID&0002054c_PID&09cc\7&a&0&0000"))
        self.assertFalse(physid._is_usb_node(r"BTHLE\DEV_1234\7&a&0&0000"))   # noqa: SLF001
        self.assertTrue(physid._is_usb_node(                      # noqa: SLF001
            r"USB\VID_8087&PID_0026\5&1&0&1"))


class SameControllerTwoTransportsTests(unittest.TestCase):
    """同一只手柄同时出现在两个总线上，只能显示一条。

    真机场景（用户反馈）：手柄**蓝牙连着时插上 USB 线**，Windows 会同时暴露
    两条 HID 设备，两条都能打开——

    .. code-block:: text

       12:54:54  检测到手柄：DualSense [蓝牙 / 054C:0CE6#\\?\\HID]
       12:55:01  检测到手柄：DualSense [USB / 054C:0CE6#USB\\VID_054C&PID_0CE6\\6&1B0A3985&0&2]

    ——界面里同一只手柄出现两条。两条的分组键天然不同，能跨总线对齐它们的
    只有**手柄自己的 MAC**：蓝牙侧 hidapi 的 ``serial_number`` 就是 MAC，
    USB 侧 ``serial_number`` 是空的、得读配对信息特性报告。

    下面的字节都是真机抓到的原样数据（DualSense，USB 与蓝牙同时连着）。
    """

    #: 真机特性报告 0x09（USB 侧读到的，MAC 小端序存在 [1..6]）
    REAL_PAIRING = bytes.fromhex("09 4E B3 82 56 27 0C 08 25 00 DA 47 64 28 AF 44 00 00 00 00")
    #: 蓝牙侧 hidapi 给的 serial_number
    REAL_BT_SERIAL = "0c275682b34e"
    #: USB 侧真实路径（serial 为空）
    USB_PATH = (r"\\?\HID#VID_054C&PID_0CE6&MI_03#8&e44178f&0&0000"
                r"#{4d1e55b2-f16f-11cf-88cb-001111000030}").encode()
    #: 蓝牙侧真实路径
    BT_PATH = (r"\\?\HID#{00001124-0000-1000-8000-00805f9b34fb}"
               r"_VID&0002054c_PID&0ce6#9&1934ba94&1&0000"
               r"#{4d1e55b2-f16f-11cf-88cb-001111000030}").encode()

    def setUp(self):
        from psbt.controllers import backend

        self.backend = backend
        # 保存原对象：下面的替身会覆盖模块级名字，必须还原，
        # 否则会污染后面的测试（读取线程会拿到不会 read 的假句柄）。
        self._orig_handle = backend.HidHandle
        backend.clear_mac_cache()

    def tearDown(self):
        self.backend.HidHandle = self._orig_handle
        self.backend.clear_mac_cache()

    def _info(self, path, bus_type, serial=""):
        from psbt.controllers.backend import HidDeviceInfo

        return HidDeviceInfo(
            path=path, vendor_id=0x054C, product_id=0x0CE6,
            product_string="DualSense Wireless Controller",
            serial=serial, usage_page=1, usage=5, interface_number=-1,
            bus_type=bus_type)

    def _stub_feature_reads(self):
        """把打开设备/读特性报告换成返回真机字节，测试不碰硬件。"""
        payload = self.REAL_PAIRING
        opened = []

        class _FakeHandle:
            def __init__(self, info):
                self.info = info

            def open(self):
                opened.append(self.info.path)
                return True

            def get_feature_report(self, report_id, length=64):
                return payload if report_id == 0x09 else None

            def close(self):
                pass

        self.backend.HidHandle = _FakeHandle
        return opened

    # ---- 纯函数 ----------------------------------------------------------
    def test_normalize_mac_variants(self):
        from psbt.controllers import physid

        for text in ("0c275682b34e", "0C275682B34E", "0c:27:56:82:b3:4e",
                     "0C-27-56-82-B3-4E", "  0c275682b34e  "):
            self.assertEqual(physid.normalize_mac(text), "0c275682b34e", text)
        for bad in ("", None, "0c275682b3", "not-a-mac", "0c275682b34e00"):
            self.assertIsNone(physid.normalize_mac(bad), bad)

    def test_mac_from_feature_report_is_little_endian(self):
        """特性报告里的 MAC 是**小端序**，必须反过来读。

        内核结构体注释：``u8 mac_address[6]; /* Note: stored in little
        endian order. */`` —— 真机字节 4E B3 82 56 27 0C 对应
        0C:27:56:82:B3:4E。
        """
        from psbt.controllers import physid

        self.assertEqual(physid.mac_from_feature_report(self.REAL_PAIRING),
                         "0c275682b34e")
        self.assertIsNone(physid.mac_from_feature_report(b""))
        self.assertIsNone(physid.mac_from_feature_report(b"\x09\x01\x02"))

    # ---- 采集 MAC --------------------------------------------------------
    def test_mac_from_bluetooth_serial(self):
        info = self._info(self.BT_PATH, bus_type=2, serial=self.REAL_BT_SERIAL)
        self.assertEqual(self.backend.controller_mac(info), "0c275682b34e")

    def test_mac_from_usb_feature_report(self):
        """USB 侧 serial 为空，靠特性报告取 MAC。"""
        self._stub_feature_reads()
        info = self._info(self.USB_PATH, bus_type=1, serial="")
        self.assertEqual(self.backend.controller_mac(info), "0c275682b34e")

    def test_mac_read_is_cached_per_path(self):
        opened = self._stub_feature_reads()
        info = self._info(self.USB_PATH, bus_type=1, serial="")
        self.backend.controller_mac(info)
        self.backend.controller_mac(info)
        self.assertEqual(len(opened), 1, "同一条路径只该读一次")

    # ---- ★ 核心：两条总线必须归成一台 -----------------------------------
    def test_usb_and_bt_share_one_device_key(self):
        """★ 用户报的 bug：蓝牙 + USB 同时连着，界面出现两条。

        两个集合的分组键必须相同 —— 这正是界面只显示一条的关键。
        """
        self._stub_feature_reads()
        usb = self._info(self.USB_PATH, bus_type=1, serial="")
        bt = self._info(self.BT_PATH, bus_type=2, serial=self.REAL_BT_SERIAL)
        self.assertEqual(usb.device_key, bt.device_key,
                         "同一只手柄的两条总线必须共用一个 device_key")
        self.assertIn("MAC:0c275682b34e", usb.device_key)

    def test_different_controllers_are_not_merged(self):
        """护栏：两只**不同**的手柄不能被并成一台。"""
        payload = bytearray(self.REAL_PAIRING)
        payload[1:7] = bytes.fromhex("11 22 33 44 55 66")      # 另一台手柄
        self._stub_feature_reads()
        self.backend.HidHandle.get_feature_report = (
            lambda self_, rid, length=64: bytes(payload) if rid == 0x09 else None)
        a = self._info(self.USB_PATH, bus_type=1, serial="")
        b = self._info(self.BT_PATH, bus_type=2, serial="aabbccddeeff")
        self.assertNotEqual(a.device_key, b.device_key)

    def test_falls_back_when_mac_unavailable(self):
        """MAC 取不到时退回原来的分组方式，不能崩、也不能变成同一台。"""
        class _DeadHandle:
            def __init__(self, info):
                pass

            def open(self):
                return False

            def close(self):
                pass

        self.backend.HidHandle = _DeadHandle
        usb = self._info(self.USB_PATH, bus_type=1, serial="")
        bt = self._info(self.BT_PATH, bus_type=2, serial="")
        self.assertIsNone(self.backend.controller_mac(usb))
        self.assertIsNone(self.backend.controller_mac(bt))
        self.assertNotEqual(usb.device_key, bt.device_key)
        self.assertNotIn("MAC:", usb.device_key)

    def test_usb_is_preferred_for_dualsense(self):
        """DualSense 两条总线都在时**确定性地 USB 优先**。

        否则每轮扫描都可能认为"HID 路径变了"并重启读取线程，界面会抖。
        真机实测 DualSense 走 USB 充电时能给出正确等级（如"65% 充电中"）。
        """
        self._stub_feature_reads()
        usb = self._info(self.USB_PATH, bus_type=1, serial="")
        bt = self._info(self.BT_PATH, bus_type=2, serial=self.REAL_BT_SERIAL)
        for order in ([usb, bt], [bt, usb]):          # 两种输入顺序结果一致
            picked = self.backend.controller_candidates(order)
            self.assertEqual(picked[0].transport.label, "USB")
            self.assertEqual(len(picked), 2, "蓝牙那条要留作兜底")

    def test_bluetooth_is_preferred_for_ds4(self):
        """DS4 反过来：**蓝牙优先**。

        真机实测（CUH-ZCT2）：它一插上 USB，USB 报告的 status[0] 就恒定报
        0x1B（"充电中"标记，不含等级）；真实等级只在**蓝牙**链路那些"未插线"
        的帧里出现。走 USB 的话充电期间就永远看不到电量。
        """
        from psbt.controllers.backend import HidDeviceInfo

        def ds4(path, bus_type, serial=""):
            return HidDeviceInfo(
                path=path, vendor_id=0x054C, product_id=0x09CC,
                product_string="DualShock 4 (CUH-ZCT2)", serial=serial,
                usage_page=1, usage=5, interface_number=-1, bus_type=bus_type)

        usb = ds4(self.USB_PATH, 1)
        bt = ds4(self.BT_PATH, 2, serial="28c13c8852c4")
        for order in ([usb, bt], [bt, usb]):
            picked = self.backend.controller_candidates(order)
            self.assertEqual(picked[0].transport.label, "蓝牙")
            self.assertEqual(len(picked), 2, "USB 那条要留作兜底")


class DS4ChargingCarryForwardTests(unittest.TestCase):
    """插线后读到"不带电量的充电帧"：沿用上一次真实等级。

    用户症状：DS4 插上 USB 后电量从 **85% 变成 100%**。
    真机序列（13:29:07 → 13:29:14）：

    =========================  ==================  ==============
    时间                        报告 status[0]      旧行为
    =========================  ==================  ==============
    13:29:07 蓝牙               0x08                85%（真实）
    13:29:13 蓝牙               0x1B                100% ✗
    13:29:14 USB                0x1B                100% ✗
    =========================  ==================  ==============

    0x1B（低 4 位 = 11）只是"插着线在充电"的标记，不含等级。修好之后：
    等级沿用上一次真实读数（85%），状态改为充电中 —— 既不假报 100%，
    也不会在"未知"与真实等级之间跳（这种帧每秒几百条）。
    """

    def _manager_with_ds4(self):
        from psbt.controllers import backend, registry
        from psbt.controllers.manager import ControllerManager
        from psbt.controllers.models import FAMILY_DS4, MutableController, ControllerView

        info = backend.HidDeviceInfo(
            path=b"\\\\?\\HID#VID_054C&PID_09CC&MI_03#8&4662d51&0&0000#{g}",
            vendor_id=0x054C, product_id=0x09CC, product_string="DualShock 4",
            usage_page=1, usage=5, interface_number=-1, bus_type=1)
        manager = ControllerManager(Config())
        view = ControllerView(
            key=info.device_key, family=FAMILY_DS4,
            display_name=registry.lookup(0x054C, 0x09CC).name,
            transport=info.transport, vendor_id=0x054C, product_id=0x09CC)
        manager._records[view.key] = MutableController(view=view)   # noqa: SLF001
        return manager, view.key

    def _feed(self, manager, key, raw):
        from psbt.controllers import parsers

        manager._update_battery(                                    # noqa: SLF001
            key, parsers.interpret_ds4(raw), bytes([raw]))

    def test_85_percent_survives_plugging_in_the_cable(self):
        manager, key = self._manager_with_ds4()
        self._feed(manager, key, 0x08)                  # 未插线：真实 85%
        view = manager._records[key].view               # noqa: SLF001
        self.assertEqual(view.percent, 85)

        self._feed(manager, key, 0x1B)                  # 插线：无等级的充电帧
        view = manager._records[key].view               # noqa: SLF001
        self.assertEqual(view.percent, 85, "不得被改写成 100%")
        self.assertEqual(view.battery.state.value, "charging")

    def test_unknown_stays_unknown_without_history(self):
        """从没读到过真实等级时，充电帧就显示未知（不编一个 100%）。"""
        manager, key = self._manager_with_ds4()
        self._feed(manager, key, 0x1B)
        view = manager._records[key].view               # noqa: SLF001
        self.assertIsNone(view.percent)
        self.assertEqual(view.battery.state.value, "charging")

    def test_real_charging_level_overrides_carried_value(self):
        """带等级的充电帧要照常更新（如 0x18 = 充电中 85%）。"""
        manager, key = self._manager_with_ds4()
        self._feed(manager, key, 0x1B)
        self._feed(manager, key, 0x09)                  # 未插线 95%
        self._feed(manager, key, 0x18)                  # 插线 + 等级 8
        view = manager._records[key].view               # noqa: SLF001
        self.assertEqual(view.percent, 85)
        self.assertEqual(view.battery.state.value, "charging")

    def test_no_alert_from_the_charging_code(self):
        """护栏：0x1B 不得让策略认为"电量耗尽"。"""
        from psbt.controllers.models import ControllerView
        from psbt.notify.policy import LowBatteryPolicy

        manager, key = self._manager_with_ds4()
        self._feed(manager, key, 0x1B)
        view = manager._records[key].view               # noqa: SLF001
        policy = LowBatteryPolicy([30, 20, 10])
        self.assertIsNone(policy.evaluate(view))


class ParseThrottleTests(unittest.TestCase):
    """节流不能让"稍纵即逝的电量帧"被漏掉。

    DS4 插线后每秒几百帧都报"充电中"标记（不带等级），**真正的等级只在充电器
    脉冲间隙那几帧里出现**（间隔几十秒、只持续几毫秒）。读取循环按 4Hz 节流
    （`report_min_gap`），整段错过就会导致充电期间一直显示"电量未知"。
    所以电量字节一变就必须立刻解析。
    """

    def test_peek_battery_byte_per_family_and_transport(self):
        from psbt.controllers import parsers
        from psbt.controllers.models import FAMILY_DS4, FAMILY_DUALSENSE, Transport

        ds4_usb = bytes([0x01] + [0] * 29 + [0x1B] + [0] * 34)
        ds4_bt = bytes([0x11, 0xC0, 0x00] + [0] * 29 + [0x08, 0]) + bytes(46)
        ds_minimal = bytes([0x01] + [0] * 9)                  # 蓝牙精简报告
        self.assertEqual(parsers.peek_battery_byte(FAMILY_DS4, Transport.USB, ds4_usb), 0x1B)
        self.assertEqual(parsers.peek_battery_byte(FAMILY_DS4, Transport.BLUETOOTH, ds4_bt), 0x08)
        self.assertIsNone(parsers.peek_battery_byte(FAMILY_DS4, Transport.BLUETOOTH, ds_minimal))

        ds5 = bytearray(78)
        ds5[0] = 0x31
        ds5[54] = 0x16
        self.assertEqual(parsers.peek_battery_byte(FAMILY_DUALSENSE, Transport.BLUETOOTH,
                                                   bytes(ds5)), 0x16)

    def test_level_frame_is_parsed_without_waiting_for_throttle(self):
        """★ 真等级帧必须马上被解析，哪怕还在节流窗口内。

        构造的数据流：3 帧"充电中标记" → **1 帧真等级** → 大量标记帧。
        若没有"字节一变就解析"这条，那唯一一帧真等级会落在 0.25s 节流窗口里
        被丢掉，之后全是标记帧（不带等级）→ 永远读不到 85%。
        """
        import time

        from psbt.controllers import backend
        from psbt.controllers.manager import ControllerManager, _Reader
        from psbt.controllers.models import (FAMILY_DS4, ControllerView,
                                            MutableController, Transport)

        def frame(status):
            return bytes([0x11, 0xC0, 0x00] + [0] * 29 + [status, 0x00]) + bytes(46)

        stream = [frame(0x1B)] * 3 + [frame(0x08)] + [frame(0x1B)] * 100000

        class _FakeHandle:
            def __init__(self, info):
                self.info = info
                self.is_open = True
                self._it = iter(stream)

            def open(self):
                return True

            def read(self, size, timeout):
                try:
                    return next(self._it)
                except StopIteration:
                    time.sleep(0.05)
                    return b""

            def close(self):
                self.is_open = False

        manager = ControllerManager(Config())
        view = ControllerView(
            key="ds4", family=FAMILY_DS4, display_name="DualShock 4 (CUH-ZCT2)",
            transport=Transport.BLUETOOTH, vendor_id=0x054C, product_id=0x09CC)
        record = MutableController(view=view, candidate_paths=[b"fake"])
        manager._records[view.key] = record               # noqa: SLF001

        orig = backend.HidHandle
        reader = None
        try:
            backend.HidHandle = _FakeHandle
            reader = _Reader(manager, record)
            reader.start()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and record.view.battery.percent is None:
                time.sleep(0.02)
        finally:
            if reader is not None:
                reader.shutdown()
            backend.HidHandle = orig
        self.assertEqual(record.view.battery.percent, 85,
                         "夹在节流窗口内的真等级帧不得被漏掉")


class ReaderRobustnessTests(unittest.TestCase):
    """读取线程的日志辅助函数必须吃得下 hidapi 的 bytes 路径。"""

    def test_short_accepts_bytes_and_str(self):
        """``_short`` 同时支持 bytes 与 str。

        回归：hidapi 返回的 ``path`` 是 **bytes**，而 ``view.key`` 是 str。
        早先 ``_short`` 里写的是 ``"#" in key``，传 bytes 进来会抛
        ``TypeError``；而它在"所有 HID 集合都打不开"这条路径上被调用 ——
        本该只记一行 debug 日志再退避重试，实际却让读取线程直接崩掉，
        那台手柄从此不再被读取（设备被 DS4Windows / Steam Input 独占时
        很容易走到这条路径）。
        """
        from psbt.controllers.manager import _short

        self.assertEqual(_short(b"\\\\?\\hid#vid_054c&pid_09cc#7&abc"), "7&abc")
        self.assertEqual(_short("054C:09CC#7&abc"), "7&abc")
        self.assertEqual(_short(b"nohashpath"), "nohashpa")
        self.assertEqual(_short(""), "")


class UnsupportedDeviceTests(unittest.TestCase):
    """只支持 PS4 / PS5 手柄**本体**。

    1.6 起移除了三类设备：PS3 的 DualShock 3、PS Move、以及 PS4 官方无线
    适配器 (CUH-ZWA1)。前两者协议里根本没有电量字段（只能识别不能读），
    适配器已停产且手头没有硬件可验证。

    移除后，"识别但不读电量"那套分支（``supports_battery``）也随之删掉了，
    所以这些 PID 现在**完全不会被识别**，不会出现在界面里。
    """

    #: 明确不支持的设备（PID -> 说明）
    REMOVED = (
        (0x0268, "DualShock 3 (PS3)"),
        (0x03D5, "PS Move 控制器"),
        (0x0BA0, "DualShock 4 无线适配器 (CUH-ZWA1)"),
    )

    def _info(self, product_id, name):
        from psbt.controllers.backend import HidDeviceInfo

        return HidDeviceInfo(
            path=(r"\\?\hid#vid_054c&pid_%04x#fake" % product_id).encode(),
            vendor_id=0x054C, product_id=product_id, product_string=name,
            usage_page=1, usage=5, interface_number=-1, bus_type=1,
        )

    def test_registry_only_knows_ps4_and_ps5_controllers(self):
        from psbt.controllers import registry

        known = {pid for (_vid, pid) in registry.known_pairs()}
        self.assertEqual(known, {0x05C4, 0x09CC, 0x0CE6, 0x0DF2},
                         "只应保留 PS4/PS5 手柄本体")

    def test_removed_devices_are_not_recognized(self):
        from psbt.controllers import registry

        for pid, label in self.REMOVED:
            self.assertIsNone(registry.lookup(0x054C, pid),
                              "%s 不应再被收录" % label)

    def test_removed_device_is_not_tracked(self):
        """不在表里的设备连识别都不会识别 —— 不进记录、不开线程。"""
        from psbt.controllers import backend
        from psbt.controllers.manager import ControllerManager

        for pid, label in self.REMOVED:
            info = self._info(pid, label)
            manager = ControllerManager(Config())
            manager._add_device(info)                             # noqa: SLF001
            self.assertNotIn(info.device_key, manager._records,   # noqa: SLF001
                             "%s 不应被跟踪" % label)
            self.assertNotIn(info.device_key, manager._readers,   # noqa: SLF001
                             "%s 不应启动读取线程" % label)

    def test_no_dead_supports_battery_field(self):
        """护栏：那个已经没人用的 `supports_battery` 字段不该偷偷回来。"""
        from psbt.controllers import registry

        spec = registry.lookup(0x054C, 0x0CE6)
        self.assertFalse(hasattr(spec, "supports_battery"),
                         "字段已随设备精简一并删除")

    def test_reader_started_for_supported_device(self):
        """对照：支持电量的设备照常启动读取。"""
        from psbt.controllers import backend
        from psbt.controllers.manager import ControllerManager

        info = self._info(0x09CC, "DualShock 4 (CUH-ZCT2)")
        manager = ControllerManager(Config())
        original = backend.all_candidates_for
        try:
            backend.all_candidates_for = lambda *a, **k: []
            manager._add_device(info)                           # noqa: SLF001
            self.assertIn(info.device_key, manager._readers)      # noqa: SLF001
        finally:
            backend.all_candidates_for = original
            manager._remove_device(info.device_key)               # noqa: SLF001


class ScanFailureTests(unittest.TestCase):
    """枚举失败必须与「真的没有设备」区分开。

    历史 bug：``enumerate_all()`` 在异常时返回 ``[]``，与"枚举成功但 0 台设备"
    压成同一个值，于是管理器把一次临时枚举错误当成**所有手柄同时拔出**——
    关闭读取线程、清空界面、重置告警去重，下一轮又全部重新连上。
    """

    def _fake_hid(self, behaviour):
        class _Fake:
            def __init__(self, fn):
                self._fn = fn

            def enumerate(self):
                return self._fn()

            def __getattr__(self, name):        # 其它接口调用一律不关心
                raise AttributeError(name)

        return _Fake(behaviour)

    def test_enumerate_failure_returns_none(self):
        from psbt.controllers import backend

        original = backend._hid          # noqa: SLF001
        try:
            backend._hid = self._fake_hid(lambda: (_ for _ in ()).throw(OSError("boom")))
            self.assertIsNone(backend.enumerate_all(),
                              "枚举失败必须是 None，不能是空列表")
            self.assertIsNone(backend.find_playstation_devices())
        finally:
            backend._hid = original      # noqa: SLF001

    def test_enumerate_success_empty_is_empty_list(self):
        from psbt.controllers import backend

        original = backend._hid          # noqa: SLF001
        try:
            backend._hid = self._fake_hid(lambda: [])
            self.assertEqual(backend.enumerate_all(), [],
                             "枚举成功但没有设备应当是空列表")
            self.assertEqual(backend.find_playstation_devices(), [])
        finally:
            backend._hid = original      # noqa: SLF001

    def _manager_with_one_device(self):
        from psbt.controllers import backend
        from psbt.controllers.manager import ControllerManager
        from psbt.controllers.models import (
            FAMILY_DS4, ControllerView, Transport, MutableController,
        )

        manager = ControllerManager(Config())
        view = ControllerView(
            key="fake-ds4", family=FAMILY_DS4, display_name="DualShock 4 (fake)",
            transport=Transport.BLUETOOTH, vendor_id=0x054C, product_id=0x09CC,
        )
        manager._records[view.key] = MutableController(view=view)   # noqa: SLF001
        original = backend.find_playstation_devices
        return manager, original

    def test_scan_failure_does_not_unplug_devices(self):
        from psbt.controllers import backend

        manager, original = self._manager_with_one_device()
        try:
            backend.find_playstation_devices = lambda: None     # 枚举失败
            manager._scan_once()                                # noqa: SLF001
            self.assertIn("fake-ds4", manager._records,         # noqa: SLF001
                          "枚举失败时不得移除已跟踪的设备")
            self.assertEqual(manager.scan_failures, 1)
            self.assertFalse(manager.is_scan_degraded)

            for _ in range(3):
                manager._scan_once()                            # noqa: SLF001
            self.assertIn("fake-ds4", manager._records)         # noqa: SLF001
            self.assertTrue(manager.is_scan_degraded,
                            "连续失败到阈值应标记为降级（设备列表可能过期）")
        finally:
            backend.find_playstation_devices = original

    def test_scan_success_still_reconciles(self):
        """对照组：枚举成功且确实没有设备时，仍然要正常判定为拔出。"""
        from psbt.controllers import backend

        manager, original = self._manager_with_one_device()
        try:
            backend.find_playstation_devices = lambda: []
            manager._scan_once()                                # noqa: SLF001
            self.assertNotIn("fake-ds4", manager._records,      # noqa: SLF001
                             "枚举成功且无设备时才应判定拔出")
            self.assertEqual(manager.scan_failures, 0)
        finally:
            backend.find_playstation_devices = original

    def test_scan_recovery_resets_counter(self):
        from psbt.controllers import backend

        manager, original = self._manager_with_one_device()
        try:
            backend.find_playstation_devices = lambda: None
            manager._scan_once()                                # noqa: SLF001
            self.assertEqual(manager.scan_failures, 1)
            backend.find_playstation_devices = lambda: []
            manager._scan_once()                                # noqa: SLF001
            self.assertEqual(manager.scan_failures, 0, "恢复后计数器必须归零")
        finally:
            backend.find_playstation_devices = original


def _icon_bytes(app):
    return app._icon.icon.tobytes()                      # noqa: SLF001

def _wait_icon(app, timeout, pred, interval=0.02):
    """轮询等图标满足条件（含尾部刷新/定时器带来的延迟），返回最终像素。"""
    deadline = time.time() + timeout
    data = _icon_bytes(app)
    while time.time() < deadline and not pred(data):
        time.sleep(interval)
        data = _icon_bytes(app)
    return data


class TrayRefreshTests(unittest.TestCase):
    """托盘刷新节流**不得吞掉**状态更新。

    真机 bug 复现（2026-09-21 日志）：手柄接入瞬间，
    「已连接（电量未知）」与「电量更新 percent=85」两条快照在**同一毫秒**
    先后到达（`22:55:56,639` 两条），后者落在 150ms 节流窗口内被直接丢弃。
    而快照是**变化驱动**的 —— 电量不变就不会再有回调，于是图标永久停在
    「电量未知」，用户看到的就是"明明读到了电量，图标却不显示"。
    """

    def _make(self, throttle):
        return trayapp.TrayApp(Config(), trayapp.TrayCallbacks(), throttle=throttle)

    def test_throttled_update_is_not_lost(self):
        app = self._make(throttle=0.30)
        try:
            app.update([_unknown_view("k")])
            first = app._icon.icon            # noqa: SLF001
            # 同一毫秒到达的电量更新：必然落进节流窗口
            app.update([_view("k", 85)])
            self.assertIs(app._icon.icon, first, "前置条件：这次更新确实被节流了")

            deadline = time.time() + 4.0
            while time.time() < deadline and app._icon.icon is first:
                time.sleep(0.02)
            self.assertIsNot(app._icon.icon, first,
                             "被节流的电量更新被永久丢弃，图标停在旧状态")
            want = app._painter.render(85, False, iconart.MODE_LEVEL)
            self.assertEqual(app._icon.icon.tobytes(), want.tobytes(),
                             "图标最终应显示 85% 对应的电量块")
        finally:
            app.stop()

    def test_icon_follows_connect_read_disconnect(self):
        """完整生命周期：无手柄 → 已连接(未知) → 读到电量 → 断开，图标都要跟上。

        注意中间态允许被节流跳过（150ms 内就被取代，不值得绘制），
        但**最终状态必须落到图标上**。
        """
        app = self._make(throttle=0.05)
        try:
            none_img = _icon_bytes(app)                  # 初始：无手柄

            app.update([_unknown_view("k")])
            unknown_img = _wait_icon(app, 2.0, lambda b: b != none_img)
            self.assertNotEqual(unknown_img, none_img, "应切换到「电量未知」")

            app.update([_view("k", 85)])
            want = app._painter.render(85, False, iconart.MODE_LEVEL).tobytes()
            self.assertEqual(_wait_icon(app, 2.0, lambda b: b == want), want,
                             "读到电量后图标必须显示对应电量块")

            app.update([])
            self.assertEqual(_wait_icon(app, 2.0, lambda b: b == none_img), none_img,
                             "断开后必须回到「无手柄」")
        finally:
            app.stop()

    def test_identical_state_does_not_touch_shell(self):
        """状态没变时不应重复调用 Shell_NotifyIcon（省掉无谓的托盘重绘）。"""
        app = self._make(throttle=0.0)
        try:
            app.update([_view("k", 65)])
            pushes = app._icon_pushes            # noqa: SLF001
            self.assertGreaterEqual(pushes, 1)
            time.sleep(0.05)
            app.update([_view("k", 65)])
            app.refresh_now()
            self.assertEqual(app._icon_pushes, pushes,
                             "状态未变化却重新推送了图标")
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
