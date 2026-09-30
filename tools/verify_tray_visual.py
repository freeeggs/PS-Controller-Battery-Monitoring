#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""可视化验证：启动托盘图标 → 打开 Windows 通知区域溢出面板 → 截图。

Windows 10 默认把新图标折叠进「隐藏的图标」面板，程序无法强制自己常显
（这是系统的设计，不是 bug）。本脚本通过模拟点击托盘上的小箭头展开面板，
从而可以真正**看到**我们的电池图标。

用法::

    python tools/verify_tray_visual.py <截图输出路径>

注意：会短暂移动鼠标并注入一次点击（点的是通知区域的小箭头），
不会修改任何系统设置。
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

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000


def find_chevron() -> tuple:
    """定位通知区域左侧的「显示隐藏的图标」小箭头。

    Windows 任务栏结构：``Shell_TrayWnd`` → ``TrayNotifyWnd`` → 第一个
    ``Button`` 子窗口就是那个小箭头（第二个按钮是输入法指示等）。这里按
    窗口层级精确取坐标，比按像素偏移猜更可靠。
    """
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    tray = user32.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        return (0, 0)
    notify_area = user32.FindWindowExW(tray, None, "TrayNotifyWnd", None)
    if not notify_area:
        return (0, 0)

    children = []
    callback = EnumProc(lambda hwnd, _lparam: (children.append(hwnd), True)[1])
    user32.EnumChildWindows(notify_area, callback, 0)

    for child in children:
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(child, name, 64)
        if name.value == "Button":
            rect = wintypes.RECT()
            user32.GetWindowRect(child, ctypes.byref(rect))
            if rect.right - rect.left >= 12:      # 跳过 0 宽度的占位按钮
                return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    return (0, 0)


def click(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def views():
    return [
        ControllerView(key="a", family=FAMILY_DUALSENSE, display_name="DualSense (PS5)",
                       transport=Transport.BLUETOOTH, vendor_id=0x054C, product_id=0x0CE6,
                       battery=BatteryReport(4, 45, ChargeState.DISCHARGING, "t")),
        ControllerView(key="b", family=FAMILY_DS4, display_name="DualShock 4 (CUH-ZCT2)",
                       transport=Transport.USB, vendor_id=0x054C, product_id=0x09CC,
                       battery=BatteryReport(8, 85, ChargeState.DISCHARGING, "t")),
    ]


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "artifacts", "tray_visual.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    setup_logging("INFO", console=True)

    tray = TrayApp(Config(), TrayCallbacks(get_views=views))
    result = {"ok": False}

    def work():
        from PIL import ImageGrab

        try:
            tray.update(views())
            time.sleep(1.2)
            x, y = find_chevron()
            print("点击通知区域小箭头：(%d, %d)" % (x, y))
            click(x, y)
            time.sleep(1.5)
            image = ImageGrab.grab()
            width, height = image.size
            image.crop((max(0, width - 1200), max(0, height - 340), width, height)).save(out)
            print("已截图：%s" % out)

            # 轮换到「无手柄」状态再截一张，验证图标会回到「无手柄」
            tray.update([])
            time.sleep(1.2)
            image = ImageGrab.grab()
            image.crop((max(0, width - 1200), max(0, height - 340), width, height)).save(
                out.replace(".png", "_none.png"))
            print("已截图（无手柄）：%s" % out.replace(".png", "_none.png"))

            click(x, y)          # 关闭面板
            user32.SetCursorPos(width // 2, height // 2)
            result["ok"] = True
        except Exception:
            import traceback

            traceback.print_exc()
        finally:
            tray.stop()

    tray.run(setup=work)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
