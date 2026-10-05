#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""右键菜单可视化验证：展开「隐藏的图标」→ 右键我们的图标 → 截图菜单。

用法::

    python tools/verify_menu_visual.py [输出png]

依赖 ``Shell_NotifyIconGetRect`` 定位图标位置（图标在溢出面板里时同样有效），
然后注入一次右键点击并截图。不会修改任何系统设置。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from psbt.config import Config                        # noqa: E402
from psbt.controllers.models import (                 # noqa: E402
    FAMILY_DS4,
    FAMILY_DUALSENSE,
    BatteryReport,
    ChargeState,
    ControllerView,
    Transport,
)
from psbt.logs import setup_logging                   # noqa: E402
from psbt.tray.trayapp import TrayApp, TrayCallbacks  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

LEFTDOWN, LEFTUP = 0x0002, 0x0004
RIGHTDOWN, RIGHTUP = 0x0008, 0x0010


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("guidItem", ctypes.c_byte * 16),
    ]


def icon_rect(hwnd, uid):
    ident = NOTIFYICONIDENTIFIER()
    ident.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
    ident.hWnd = hwnd
    ident.uID = uid
    rect = wintypes.RECT()
    rc = shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(rect))
    return int(rc), (rect.left, rect.top, rect.right, rect.bottom)


def click(x, y, button="left"):
    down, up = (RIGHTDOWN, RIGHTUP) if button == "right" else (LEFTDOWN, LEFTUP)
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    user32.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(up, 0, 0, 0, 0)


def chevron_pos():
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    area = user32.FindWindowExW(tray, None, "TrayNotifyWnd", None)
    kids = []
    cb = EnumProc(lambda h, l: (kids.append(h), True)[1])
    user32.EnumChildWindows(area, cb, 0)
    for kid in kids:
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(kid, buf, 64)
        if buf.value == "Button":
            r = wintypes.RECT()
            user32.GetWindowRect(kid, ctypes.byref(r))
            if r.right - r.left >= 12:
                return ((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    return (0, 0)


def demo_views():
    """**假数据**，用于看菜单排版（与真实设备无关）。"""
    return [
        ControllerView(key="a", family=FAMILY_DUALSENSE, display_name="DualSense (PS5)",
                       transport=Transport.BLUETOOTH, vendor_id=0x054C, product_id=0x0CE6,
                       battery=BatteryReport(8, 85, ChargeState.DISCHARGING, "t")),
        ControllerView(key="b", family=FAMILY_DS4, display_name="DualShock 4 (CUH-ZCT2)",
                       transport=Transport.USB, vendor_id=0x054C, product_id=0x09CC,
                       battery=BatteryReport(4, 45, ChargeState.DISCHARGING, "t")),
    ]


class _RealViews:
    """接**真实设备**：起一个真 Manager，等它出快照，然后读它的 snapshot。

    用途：验证界面里到底列出了几台手柄（例如"复合设备是否把一个手柄
    显示成好几台"）。用假数据是看不出这类问题的。
    """

    def __init__(self):
        from psbt.controllers.manager import ControllerManager

        self.manager = ControllerManager(Config())
        self.views = []
        self.manager.events.on_snapshot = self._on_snapshot

    def _on_snapshot(self, views):
        self.views = list(views)

    def start(self) -> bool:
        return self.manager.start()

    def wait(self, seconds: float) -> None:
        import time as _t

        deadline = _t.monotonic() + seconds
        while _t.monotonic() < deadline and not self.views:
            _t.sleep(0.2)

    def stop(self) -> None:
        self.manager.stop()

    def __call__(self):
        return self.views


def main() -> int:                                    # noqa: C901
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_real = "--real" in sys.argv
    out = args[0] if args else os.path.join(ROOT, "artifacts", "tray_menu.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    setup_logging("INFO", console=True)

    real = _RealViews() if use_real else None
    if real is not None:
        if not real.start():
            print("hidapi 不可用，无法使用 --real 模式")
            return 2
        real.wait(8.0)
        print("真实设备快照：%d 台" % len(real.views))
        for v in real.views:
            print("   %s" % v.menu_text())

    source = real if real is not None else demo_views
    tray = TrayApp(Config(), TrayCallbacks(get_views=source))

    def work():
        from PIL import ImageGrab

        try:
            tray.update(source())
            time.sleep(1.0)

            # 直接把「右键」消息投递给托盘窗口 —— 这正是 shell 在用户右键图标时
            # 做的事（WM_NOTIFY + lParam=WM_RBUTTONUP），比注入鼠标事件更稳定，
            # 也不受「图标被折叠进溢出面板」影响。
            from pystray._util import win32 as pystray_win32   # noqa: PLC2701

            hwnd = getattr(tray._icon, "_hwnd", None)          # noqa: SLF001
            user32.SetCursorPos(700, 420)                      # 菜单在此处弹出
            time.sleep(0.2)
            cb = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                    ctypes.c_size_t, ctypes.c_ssize_t)
            post = user32.PostMessageW
            post.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
            post.restype = wintypes.BOOL
            del cb
            post(hwnd, pystray_win32.WM_NOTIFY, 0, pystray_win32.WM_RBUTTONUP)
            print("已向托盘窗口投递 WM_NOTIFY/WM_RBUTTONUP，hwnd=%s" % hwnd)
            time.sleep(1.2)

            image = ImageGrab.grab()
            image.save(out.replace(".png", "_full.png"))
            image.crop((120, 60, 860, 580)).save(out)
            print("已截图：%s（完整屏幕另存 %s）" % (out, out.replace(".png", "_full.png")))

            # 菜单是模态的：用 ESC 关闭，避免误点桌面上的图标
            user32.keybd_event(0x1B, 0, 0, 0)
            user32.keybd_event(0x1B, 0, 2, 0)
            time.sleep(0.5)

            # 再截一张「无手柄」时的菜单
            tray.update([])
            time.sleep(0.6)
            user32.SetCursorPos(700, 420)
            time.sleep(0.2)
            post(hwnd, pystray_win32.WM_NOTIFY, 0, pystray_win32.WM_RBUTTONUP)
            time.sleep(1.2)
            image = ImageGrab.grab()
            image.crop((120, 60, 860, 580)).save(out.replace(".png", "_none.png"))
            print("已截图（无手柄）：%s" % out.replace(".png", "_none.png"))
            user32.keybd_event(0x1B, 0, 0, 0)
            user32.keybd_event(0x1B, 0, 2, 0)
            user32.SetCursorPos(600, 600)
        except Exception:
            import traceback

            traceback.print_exc()
        finally:
            tray.stop()

    tray.run(setup=work)
    if real is not None:
        real.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
