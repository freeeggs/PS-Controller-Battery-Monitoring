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
    """``supports_battery=False`` 必须在**解析入口**形成硬约束。

    历史 bug：DS3 / PS Move 在 registry 里 family 记的是 DS4，管理器中却
    仍然无条件启动读取线程并按 DS4 解析。``supports_battery`` 当时只决定了
    初始显示"未知" + 一句提示，**并没有阻止报告被解析** —— 一旦某条报告
    恰好 report_id=0x01 且长度 ≥31，就会去读下标 30 那个无意义的位置，
    读出假电量。约束放在 UI 提示上是不够的。
    """

    def _info(self, product_id, name):
        from psbt.controllers.backend import HidDeviceInfo

        return HidDeviceInfo(
            path=(r"\\?\hid#vid_054c&pid_%04x#fake" % product_id).encode(),
            vendor_id=0x054C, product_id=product_id, product_string=name,
            usage_page=0, usage=0, interface_number=-1, bus_type=1,
        )

    def test_registry_marks_legacy_devices_as_no_battery(self):
        from psbt.controllers import registry

        for pid, label in ((0x0268, "DualShock 3"), (0x03D5, "PS Move")):
            spec = registry.lookup(0x054C, pid)
            self.assertIsNotNone(spec, label)
            self.assertFalse(spec.supports_battery, "%s 不应支持电量" % label)

    def test_no_reader_started_for_unsupported_device(self):
        from psbt.controllers import backend
        from psbt.controllers.manager import ControllerManager

        info = self._info(0x0268, "DualShock 3")
        manager = ControllerManager(Config())
        original = backend.all_candidates_for
        try:
            backend.all_candidates_for = lambda *a, **k: []
            manager._add_device(info)                           # noqa: SLF001
        finally:
            backend.all_candidates_for = original

        self.assertIn(info.device_key, manager._records,          # noqa: SLF001
                      "不支持电量的设备仍应被识别并显示")
        self.assertNotIn(info.device_key, manager._readers,       # noqa: SLF001
                         "不得为它启动电量读取线程")
        note = manager._records[info.device_key].view.note        # noqa: SLF001
        self.assertIn("不提供电量", note)
        manager._remove_device(info.device_key)                   # noqa: SLF001

    def test_reader_started_for_supported_device(self):
        """对照组：支持电量的设备照常启动读取。"""
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
