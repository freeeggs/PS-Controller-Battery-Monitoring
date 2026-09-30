#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""提醒弹窗渲染自检：弹出几种级别的弹窗并截图，便于人工核对观感。

用法::

    python tools/verify_popup.py [输出目录]
"""

from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from psbt.notify.popup import ToastPopup            # noqa: E402
from psbt import winapi                             # noqa: E402


def grab(path: str) -> str:
    from PIL import ImageGrab

    image = ImageGrab.grab()
    width, height = image.size
    crop = image.crop((max(0, width - 640), max(0, height - 460), width, height))
    crop.save(path)
    return path


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "artifacts")
    os.makedirs(out_dir, exist_ok=True)

    print("系统通知状态：%s" % winapi.notifications_suppressed()[1])
    print("工作区：%s" % (winapi.work_area(),))

    popup = ToastPopup()
    if not popup.start():
        print("弹窗线程启动失败")
        return 1

    cases = [
        ("critical", "电量严重不足",
         "DualSense 手柄电量约 5%（等级 0/10）。请立即连接 USB 充电，手柄随时可能断开连接。"),
        ("warn", "电量提醒",
         "DualShock 4 手柄电量约 25%（等级 2/10）。手柄电量已低于 30%，建议安排充电。"),
        ("info", "已连接 2 台手柄",
         "DualSense：约 85%（等级 8/10，蓝牙）\nDualShock 4：约 45%（等级 4/10，USB）"),
    ]

    try:
        for index, (severity, title, message) in enumerate(cases):
            popup.show(title, message, severity, seconds=8)
            time.sleep(1.6)
            path = os.path.join(out_dir, "popup_%d_%s.png" % (index + 1, severity))
            grab(path)
            print("已截图：%s" % path)
            time.sleep(0.6)
    finally:
        popup.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
