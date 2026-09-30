#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成程序图标 assets/app.ico（多尺寸）。

用法::

    python tools/make_icons.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from psbt.tray import iconart  # noqa: E402


def main() -> int:
    out_dir = os.path.join(ROOT, "assets")
    os.makedirs(out_dir, exist_ok=True)
    ico = os.path.join(out_dir, "app.ico")
    iconart.save_ico(ico, size=256)
    png = os.path.join(out_dir, "app_preview_256.png")
    iconart.application_icon(256).save(png)
    preview = os.path.join(ROOT, "artifacts", "icon_preview.png")
    os.makedirs(os.path.dirname(preview), exist_ok=True)
    iconart.save_preview(preview)
    print("已生成：%s（%d 字节）" % (ico, os.path.getsize(ico)))
    print("已生成：%s" % png)
    print("已生成：%s" % preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
