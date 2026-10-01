"""应用装配与生命周期。

线程模型（自下而上）
-------------------
::

    主线程                     -> tray.Icon.run()   （pystray 要求主线程跑消息循环）
      └─ setup 线程             -> 启动检测层 / 通知层 / 看门狗
    扫描线程 (psbt-scan)       -> 每 3~5s 枚举 HID 设备
    读取线程 (psbt-read-<id>)  -> 每台手柄一个，阻塞读报告
    通知线程 (psbt-notify)     -> 串行分发提醒（带最小间隔）
    闪烁线程 (psbt-blink)      -> 低电量时让托盘图标闪烁
    弹窗线程 (psbt-popup)      -> 自绘置顶提醒窗的消息循环
    看门狗线程 (psbt-watchdog) -> 主题变化检测 / 定期自检

所有后台线程均为 daemon，退出时按「先置停止标志 → 关句柄解锁 → join」的顺序
收敛，保证反复启停不残留线程与 HID 句柄。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from typing import List, Optional

from . import autostart, logs, paths, winapi
from .config import Config
from .controllers.manager import ControllerManager, ManagerEvents
from .controllers.models import ControllerView
from .controllers.simulator import SCENARIOS, SimulatedManager
from .logs import get_logger
from .notify.notifier import Notifier
from .notify.policy import LowBatteryPolicy
from .notify.popup import ToastPopup
from .theme import ThemeWatcher
from .tray import iconart
from .tray.trayapp import TrayApp, TrayCallbacks, about_text
from .version import APP_FULL_NAME, APP_VERSION

log = get_logger("app")

WATCHDOG_INTERVAL = 30.0
THEME_POLL_INTERVAL = 1.0    # 主题复查节奏（读注册表，微秒级；见 _start_watchdog）


class Application:
    """把各层组装起来并管理生命周期。"""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.config = Config.load()
        if args.debug:
            self.config.log_level = "DEBUG"
        self._single_instance: Optional[winapi.SingleInstance] = None
        self._theme = ThemeWatcher()
        self._popup: Optional[ToastPopup] = None
        self._tray: Optional[TrayApp] = None
        self._notifier: Optional[Notifier] = None
        self._manager = None
        self._policy = LowBatteryPolicy(self.config.low_battery_thresholds,
                                        enabled=self.config.enabled_alerts)
        self._watchdog: Optional[threading.Thread] = None
        self._shutdown_lock = threading.RLock()
        self._quitting = False

    # ------------------------------------------------------------------
    def run(self) -> int:
        from .version import MUTEX_NAME

        self._single_instance = winapi.SingleInstance(MUTEX_NAME)
        if not self._single_instance.acquire():
            log.warning("已有实例在运行，本次启动退出")
            if not self.args.autostart:
                winapi.message_box(
                    "%s 已经在运行中。\n请查看系统托盘（可能被折叠在「隐藏的图标」里）。"
                    % APP_FULL_NAME,
                    APP_FULL_NAME,
                )
            return 0

        log.info("=" * 62)
        log.info("%s %s 启动（Python %s, 打包运行=%s）",
                 APP_FULL_NAME, APP_VERSION, sys.version.split()[0], paths.is_frozen())
        log.info("配置文件：%s", paths.config_path())
        log.info("日志目录：%s", paths.log_dir())
        self._log_environment()

        self._popup = ToastPopup()
        self._tray = TrayApp(
            self.config,
            TrayCallbacks(
                get_views=lambda: self._manager.snapshot() if self._manager else [],
                on_rescan=self._on_rescan,
                on_quit=self.shutdown,
                on_show_info=self._show_status_popup,
                on_about=self._show_about,
                on_open_logs=lambda: winapi.open_in_explorer(paths.log_dir()),
            ),
            theme=self._theme,
        )
        self._notifier = Notifier(self.config, tray=self._tray, popup=self._popup)

        try:
            # pystray 要求在主线程运行
            self._tray.run(setup=self._start_backend)
        except KeyboardInterrupt:
            log.info("收到中断信号，正在退出")
            self.shutdown()
        except Exception:
            log.exception("托盘主循环异常退出")
            return 1
        finally:
            self.shutdown()

        log.info("已退出")
        return 0

    # ------------------------------------------------------------------
    def _start_backend(self) -> None:
        """由 pystray 在工作线程中调用：启动检测与通知。"""
        log.info("后台服务启动中…")

        events = ManagerEvents(
            on_snapshot=self._on_snapshot,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
        )

        if self.args.simulate:
            factory = SCENARIOS.get(self.args.simulate, SCENARIOS["demo"])
            self._manager = SimulatedManager(self.config, events, timeline=factory())
        else:
            self._manager = ControllerManager(self.config, events)

        started = self._manager.start()
        if not started:
            log.error("手柄检测启动失败：%s",
                      "hidapi 不可用" if not self.args.simulate else "未知原因")
            self._tray.refresh_now()
            return

        self._notifier.start()
        self._popup.start()
        self._start_watchdog()
        self._tray.refresh_now()
        log.info("后台服务已就绪（双击托盘图标或右键查看详情）")

    def _start_watchdog(self) -> None:
        if self._watchdog and self._watchdog.is_alive():
            return

        def _loop():
            """两个不同节奏的职责合在一个线程里。

            * **主题跟随：每 ``THEME_POLL_INTERVAL`` 复查一次。** 用户在
              「设置 → 个性化 → 颜色」里一切换就会立刻回来看托盘，30 秒的
              间隔在体验上等同于"不跟随"（实测踩过）。读一个 HKCU 的
              DWORD 是微秒级开销，1 Hz 完全可以接受。
            * **看门狗兜底：每 ``WATCHDOG_INTERVAL`` 一次。** 托盘重绘、
              菜单重建、检测层重启检查都不便宜，没必要跟着 1 Hz 跑。
            """
            elapsed = 0.0
            while not self._quitting:
                time.sleep(THEME_POLL_INTERVAL)
                elapsed += THEME_POLL_INTERVAL
                try:
                    if self._theme.refresh():
                        self._tray.on_theme_changed()
                except Exception:
                    log.exception("主题复查异常（已忽略）")

                if elapsed < WATCHDOG_INTERVAL:
                    continue
                elapsed = 0.0
                try:
                    # 兜底自愈：检测层的快照是「变化驱动」的，万一某次更新因为
                    # 异常/竞态没能落到图标上，就会一直停在旧状态。这里定期用
                    # 最新快照重绘一次（状态未变化时是空操作，不会真的推送）。
                    self._tray.refresh_now()
                    if self._manager and not self._manager.is_running() and not self.args.simulate:
                        log.warning("检测层已停止，尝试重启")
                        self._manager.start()
                except Exception:
                    log.exception("看门狗异常（已忽略）")

        self._watchdog = threading.Thread(target=_loop, name="psbt-watchdog", daemon=True)
        self._watchdog.start()

    # ------------------------------------------------------------------
    # 检测层回调（后台线程）
    # ------------------------------------------------------------------
    def _on_snapshot(self, views: List[ControllerView]) -> None:
        try:
            self._tray.update(views)
        except Exception:
            log.exception("刷新托盘失败")
        for view in views:
            try:
                alert = self._policy.evaluate(view)
            except Exception:
                log.exception("低电量策略评估失败")
                continue
            if alert is not None:
                self._notifier.submit(alert)

    def _on_connected(self, view: ControllerView) -> None:
        self._policy.forget(view.key)   # 重连后按全新设备重新计数
        self._policy.evaluate(view)

    def _on_disconnected(self, view: ControllerView) -> None:
        self._policy.forget(view.key)

    # ------------------------------------------------------------------
    # 托盘动作
    # ------------------------------------------------------------------
    def _on_rescan(self) -> None:
        if self._manager:
            self._manager.rescan_now()

    def _views(self) -> List[ControllerView]:
        return self._manager.snapshot() if self._manager else []

    def _show_status_popup(self) -> None:
        views = self._views()
        if not views:
            body = ("未检测到手柄。\n\n"
                    "请确认手柄已通过 USB 或蓝牙连接，并且已被 Windows 识别为"
                    "「Wireless Controller / DualSense Wireless Controller」。")
            title = "PS 手柄电量监视器"
            severity = "info"
        else:
            lines = []
            for view in views:
                line = "%s：%s（%s，%s）" % (
                    view.short_name, view.battery.percent_text,
                    view.battery.level_text, view.transport.label,
                )
                if view.battery.state.is_charging:
                    line += " · %s" % view.battery.state.label
                if view.note:
                    line += "\n    ⚠ %s" % view.note
                lines.append(line)
            lowest = min((v for v in views if v.percent is not None),
                         key=lambda v: v.percent, default=None)
            if lowest is not None and len(views) > 1:
                lines.append("")
                lines.append("托盘图标显示电量最低的手柄：%s" % lowest.short_name)
            lines.append("")
            lines.append("说明：PS4/PS5 手柄 API 只有约 10 档电量，"
                         "百分比为区间估算值，不是 1% 精度。")
            title = "已连接 %d 台手柄" % len(views)
            body = "\n".join(lines)
            severity = "info"

        if self._popup is None or not self._popup.show(title, body, severity, 14):
            winapi.message_box(body, title)

    def _show_about(self) -> None:
        extra = []
        others = autostart.other_entries()
        if others:
            extra.append("检测到其它疑似同类自启动项（未做任何修改）：")
            extra.extend("  %s = %s" % (name, value) for name, value in others)
        winapi.message_box(about_text(self._manager is not None, extra), APP_FULL_NAME)

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._quitting:
                return
            self._quitting = True
        log.info("正在退出…")
        if self._notifier is not None:
            try:
                self._notifier.shutdown()
            except Exception:
                log.exception("停止通知层时发生异常")
        if self._manager is not None:
            try:
                self._manager.stop()
            except Exception:
                log.exception("停止检测层时发生异常")
        if self._popup is not None:
            try:
                self._popup.stop()
            except Exception:
                log.exception("停止提醒弹窗时发生异常")
        if self._single_instance:
            self._single_instance.release()
        log.info("清理完成")

    # ------------------------------------------------------------------
    def _log_environment(self) -> None:
        from .controllers import backend, parsers

        log.info("hidapi：%s", backend.library_version() if backend.is_available()
                 else "不可用（%s）" % backend.import_error())
        log.info("系统通知状态：%s", winapi.notifications_suppressed()[1])
        log.info("任务栏主题：%s", "浅色" if self._theme.theme.taskbar_light else "深色")
        enabled, detail = autostart.status_text()
        log.info("开机启动：%s（%s）", "已启用" if enabled else "未启用", detail)
        log.debug("协议说明：%s", parsers.describe_protocol())


# ---------------------------------------------------------------------------
def dump_hid(seconds: int = 20) -> int:
    """排障模式：列出所有 HID 设备并转储索尼手柄的原始输入报告。

    实机验证的关键工具：把输出文件发给开发者即可定位「为什么读不到电量」。
    """
    from .controllers import backend, parsers

    out_path = os.path.join(paths.log_dir(), "hid_dump_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
    lines: List[str] = []

    def emit(text: str) -> None:
        lines.append(text)
        try:
            print(text)
        except Exception:
            pass

    emit("%s %s HID 诊断转储" % (APP_FULL_NAME, APP_VERSION))
    emit("hidapi：%s" % (backend.library_version() if backend.is_available() else "不可用"))
    emit("协议参考：\n%s" % parsers.describe_protocol())
    emit("")
    emit("=== 全部 HID 设备 ===")
    all_devices = backend.enumerate_all()
    if all_devices is None:
        emit("  （枚举失败：hidapi 调用出错，与「没有设备」不是一回事）")
    for info in all_devices or []:
        emit("  %04X:%04X  %-40s up=%-5s usage=%-4s bus=%-2s  %s" % (
            info.vendor_id, info.product_id, (info.product_string or "")[:40],
            info.usage_page, info.usage, info.bus_type, info.path.decode("utf-8", "replace"),
        ))
    emit("")
    emit("=== 索尼手柄候选 ===")
    candidates = backend.find_playstation_devices()
    if candidates is None:
        emit("  （枚举失败，本次结果未知；请稍后重试）")
        return
    if not candidates:
        emit("  （未发现索尼 HID 设备）")
    for info in candidates:
        emit("  %s" % info)

    if not candidates:
        emit("")
        emit("结论：系统当前没有识别到 PS4/PS5 手柄。")
        emit("排查建议：")
        emit("  1) 蓝牙连接时先在「设置 → 蓝牙和其它设备」里确认手柄已配对并显示为已连接；")
        emit("  2) 手柄指示灯应从闪烁变为常亮/呼吸；")
        emit("  3) 若被 DS4Windows / Steam Input 等工具独占，请先退出它们再试。")
    else:
        emit("")
        emit("=== 原始输入报告（%d 秒）===" % seconds)
        handles = []
        for info in candidates:
            handle = backend.HidHandle(info)
            if handle.open():
                handles.append((info, handle))
        deadline = time.monotonic() + max(1, seconds)
        seen: dict = {}
        try:
            while time.monotonic() < deadline and handles:
                for info, handle in handles:
                    try:
                        data = handle.read(256, 300)
                    except OSError as exc:
                        emit("  [%s] 读取失败：%s" % (info.product_string, exc))
                        continue
                    if not data:
                        continue
                    signature = (info.device_key, len(data))
                    count = seen.get(signature, 0)
                    seen[signature] = count + 1
                    if count < 4:      # 每种报告只打印前几条
                        kind = "%s / %s" % (info.product_string, info.transport.label)
                        emit("  [%s] len=%3d id=0x%02X %s" % (
                            kind, len(data), data[0], parsers.hex_dump(data, 40)))
                        report = parsers.parse_report(
                            _guess_family(info.product_id), info.transport, data
                        )
                        if report is not None:
                            emit("        -> 解析：level=%s percent=%s state=%s source=%s" % (
                                report.level, report.percent, report.state.value, report.source))
                        else:
                            emit("        -> 解析：无法识别（报告 ID/长度与已知协议不匹配）")
        finally:
            for _info, handle in handles:
                handle.close()

        emit("")
        emit("=== 每种报告的条数统计 ===")
        for (key, length), count in sorted(seen.items()):
            emit("  %s len=%d 共 %d 条" % (key, length, count))

    emit("")
    emit("转储结束。完整内容已保存到：%s" % out_path)
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except OSError:
        pass
    winapi.open_in_explorer(paths.log_dir())
    return 0


def _guess_family(product_id: int) -> str:
    from .controllers.models import FAMILY_DS4, FAMILY_DUALSENSE

    return FAMILY_DUALSENSE if product_id in (0x0CE6, 0x0DF2) else FAMILY_DS4
