#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""托盘图标自检：验证图标能否被 shell 正确注册、菜单文本是否正确、气泡是否发出。

用法::

    python tools/verify_tray.py [输出目录]

检查项
------
1. 生成的图标能被 ``LoadImage`` 加载成 HICON（pystray 用的正是这条路径）；
2. ``Shell_NotifyIconGetRect`` 返回成功 —— 说明图标已真正注册到任务栏
   （若图标被折叠到「隐藏的图标」里，返回的矩形为空，这在 Windows 10 上属正常）；
3. 动态菜单文本（含多手柄、无手柄两种情况）；
4. 气泡通知调用是否成功。
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
from psbt.logs import get_logger, setup_logging       # noqa: E402
from psbt.tray import iconart                         # noqa: E402
from psbt.tray.trayapp import TrayApp, TrayCallbacks  # noqa: E402

setup_logging("INFO", console=True)
log = get_logger("verify")

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("guidItem", ctypes.c_byte * 16),
    ]


def shell_notify_icon_get_rect(hwnd, uid):
    ident = NOTIFYICONIDENTIFIER()
    ident.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
    ident.hWnd = hwnd
    ident.uID = uid
    rect = wintypes.RECT()
    rc = shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(rect))
    return int(rc), (rect.left, rect.top, rect.right, rect.bottom)


def check_load_image(out_dir: str) -> bool:
    """用 pystray 内部同款方式把图标变成 HICON，确认图标管线可用。"""
    import tempfile

    from psbt.tray.iconart import IconPainter, MODE_LEVEL

    image = IconPainter(size=256).render(65, False, MODE_LEVEL)
    fd, path = tempfile.mkstemp(suffix=".ico")
    os.close(fd)
    try:
        image.save(path, format="ICO")
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                      wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                      wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HANDLE
        handle = user32.LoadImageW(None, path, 1, 0, 0,
                                   0x00000040 | 0x00000010)   # LR_DEFAULTSIZE | LR_LOADFROMFILE
        ok = bool(handle)
        if ok:
            user32.DestroyIcon(handle)
        return ok
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def fake_views(mode: str):
    if mode == "none":
        return []
    return [
        ControllerView(key="a", family=FAMILY_DUALSENSE, display_name="DualSense (PS5)",
                       transport=Transport.BLUETOOTH, vendor_id=0x054C, product_id=0x0CE6,
                       battery=BatteryReport(8, 85, ChargeState.DISCHARGING, "t")),
        ControllerView(key="b", family=FAMILY_DS4, display_name="DualShock 4 (CUH-ZCT2)",
                       transport=Transport.USB, vendor_id=0x054C, product_id=0x09CC,
                       battery=BatteryReport(4, 45, ChargeState.DISCHARGING, "t")),
    ]


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)

    print("\n[1] 图标 → HICON 管线：%s" % ("通过" if check_load_image(out_dir) else "失败"))

    config = Config()
    state = {"views": fake_views("multi")}
    tray = TrayApp(config, TrayCallbacks(get_views=lambda: state["views"]))

    results = {}

    def verify(icon):
        try:
            time.sleep(0.8)
            tray.update(state["views"])
            time.sleep(0.6)

            hwnd = getattr(icon, "_hwnd", None)
            uid = id(icon)
            rc, rect = shell_notify_icon_get_rect(hwnd, uid)
            results["rect"] = (rc, rect)
            print("[2] Shell_NotifyIconGetRect: hr=0x%08X rect=%s" % (rc, rect))
            print("    -> 图标已注册到任务栏" if rc == 0 else "    -> 未注册（异常）")
            if rc == 0 and rect[2] > rect[0]:
                print("    -> 图标当前可见于任务栏通知区域")
            else:
                print("    -> 图标被折叠进「隐藏的图标」弹出面板（Windows 10 默认行为）")

            print("[3] 多手柄菜单文本：")
            for item in tray._menu_items():      # noqa: SLF001 - 自检工具，直接读内部
                print("      %s" % (item.text if item.text else "(分隔线)"))

            print("[4] 无手柄菜单文本：")
            state["views"] = fake_views("none")
            tray.update([])
            time.sleep(0.4)
            for item in tray._menu_items():      # noqa: SLF001
                if item.text:
                    print("      %s" % item.text)

            print("[5] 气泡通知调用：%s" % ("成功" if tray.notify(
                "PS 手柄电量监视器", "这是一条测试通知（验证托盘气泡通道）") else "失败"))

            time.sleep(1.0)
        except Exception:
            import traceback

            traceback.print_exc()
        finally:
            tray.stop()

    tray.run(setup=lambda: verify(tray._icon))   # noqa: SLF001
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
