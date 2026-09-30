"""多通道提醒分发器。

为什么需要「多通道」
------------------
Windows 10 的通知有明确的抑制规则（全屏游戏、演示模式、专注助手、锁屏），
这些状态下 ``Shell_NotifyIcon`` 的气泡通知不会报错但也不会显示。因此本程序
**不把提醒押在单一通道上**，而是分层递进：

+------+--------------------------------+----------------------------+
| 通道 | 实现                            | 受系统抑制影响               |
+======+================================+============================+
| 1    | 托盘图标切换为告警图标并闪烁      | 否（托盘图标始终由我们绘制）  |
| 2    | 系统提示音 winsound.MessageBeep  | 否（专注助手不管声音）        |
| 3    | 气泡通知 Shell_NotifyIcon(NIF_INFO) | **是**（会被专注助手等拦截）|
| 4    | 自绘置顶窗口（popup 模块）        | 否（不经过通知中心）          |
+------+--------------------------------+----------------------------+

通道 1、2、4 在任何情况下都会执行；通道 3 尽力而为。当
``SHQueryUserNotificationState`` 表明系统会抑制通知时，自动升级到通道 4，
并把抑制原因写进日志，便于用户确认「为什么没看到气泡」。

无法绕过的限制
--------------
* **独占全屏**（Exclusive Fullscreen）游戏会覆盖所有窗口，通道 1/2/4 中的
  视觉部分都可能不可见，只剩提示音（若游戏未独占音频设备）；
* 用户在「设置 → 系统 → 通知」里关闭了本程序的通知，通道 3 永远不显示；
* 系统静音或关闭了「声音方案」时通道 2 无声。
以上都已在 README 的「已知限制」中说明。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Optional

from .. import winapi
from ..config import Config
from ..logs import get_logger
from .policy import Alert

log = get_logger("notify")

try:
    import winsound
except ImportError:  # pragma: no cover
    winsound = None


class Notifier:
    """把 Alert 变成用户真正能感知到的提醒。"""

    def __init__(self, config: Config, tray=None, popup=None):
        self.config = config
        self.tray = tray
        self.popup = popup
        self._queue: "queue.Queue[Alert]" = queue.Queue(maxsize=32)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_delivery = 0.0
        self._lock = threading.RLock()
        self.stats = {"toast": 0, "popup": 0, "sound": 0, "suppressed": 0}

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="psbt-notify", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)  # type: ignore[arg-type]
        except queue.Full:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    # ------------------------------------------------------------------
    def submit(self, alert: Alert) -> None:
        """提交一条低电量提醒（非阻塞）。"""
        try:
            self._queue.put_nowait(alert)
            log.info(
                "低电量提醒入队：%s 约%d%%（阈值 %d，%s）",
                alert.name, alert.percent, alert.threshold, alert.severity,
            )
        except queue.Full:
            log.warning("提醒队列已满，丢弃一条提醒（避免堆积刷屏）")

    def notify_simple(self, title: str, message: str, severity: str = "info",
                      use_popup: bool = True) -> None:
        """用于非低电量的提示（关于、错误等）。"""
        self._deliver(title, message, severity, is_low_battery=False,
                      force_popup=use_popup)

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break

            # 多个手柄同时低电量时按最小间隔依次提醒，避免弹窗叠成一团
            gap = self.config.alert_min_gap_seconds
            elapsed = time.monotonic() - self._last_delivery
            if gap > 0 and elapsed < gap:
                self._stop.wait(gap - elapsed)

            try:
                self._deliver(item.title, self._describe(item), item.severity,
                              is_low_battery=True, percent=item.percent)
            except Exception:
                log.exception("发送提醒时发生异常（已忽略）")
            finally:
                self._last_delivery = time.monotonic()

    def _describe(self, alert: Alert) -> str:
        battery = "约 %d%%" % alert.percent
        if alert.level is not None:
            battery += "（等级 %d/10）" % alert.level
        return "%s 手柄电量 %s。%s" % (alert.name, battery, alert.message)

    # ------------------------------------------------------------------
    def _deliver(self, title: str, message: str, severity: str,
                 is_low_battery: bool, percent: Optional[int] = None,
                 force_popup: bool = False) -> None:
        with self._lock:
            suppressed, reason = winapi.notifications_suppressed()
            if suppressed:
                self.stats["suppressed"] += 1

            # --- 通道 1：托盘图标告警闪烁（始终执行）---
            blinked = False
            if is_low_battery and self.tray is not None and percent is not None:
                try:
                    blink = getattr(self.tray, "flash_alert", None)
                    if blink:
                        blink(percent, self.config.alert_blink_seconds)
                        blinked = True
                except Exception:
                    log.exception("托盘图标闪烁失败")

            # --- 通道 2：提示音（始终执行）---
            played = False
            if self.config.alert_sound:
                played = self._play_sound(severity)

            # --- 通道 3：气泡通知 / Toast（尽力而为）---
            toasted = False
            if self.config.alert_toast and self.tray is not None:
                try:
                    toasted = bool(self.tray.notify(title, message))
                except Exception:
                    log.exception("发送气泡通知失败")

            # --- 通道 4：置顶弹窗 ---
            mode = self.config.alert_popup
            need_popup = force_popup or mode == "always" or (mode == "auto" and suppressed)
            popped = False
            if need_popup and self.popup is not None:
                popped = self.popup.show(title, message, severity,
                                         self.config.alert_popup_seconds)

            log.info(
                "提醒已发出[%s] 通道：图标闪烁=%s 声音=%s 通知=%s 弹窗=%s；"
                "系统通知状态：%s",
                severity, blinked, played, toasted, popped, reason,
            )
            if suppressed and not popped and mode == "auto":
                log.warning("当前系统会抑制普通通知，且置顶弹窗不可用，"
                            "用户可能只能通过托盘图标与提示音感知提醒")

    def _play_sound(self, severity: str) -> bool:
        if winsound is None:
            return False
        flags = winsound.MB_ICONHAND if severity == "critical" else winsound.MB_ICONEXCLAMATION
        try:
            winsound.MessageBeep(flags)
            self.stats["sound"] += 1
            return True
        except Exception:
            log.debug("MessageBeep 失败，改用蜂鸣音", exc_info=True)
        try:
            winsound.Beep(880 if severity == "critical" else 660, 220)
            self.stats["sound"] += 1
            return True
        except Exception:
            return False
