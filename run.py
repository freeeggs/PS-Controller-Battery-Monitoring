#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""源码方式运行入口（开发调试 / 源码模式开机启动使用）。

生产使用请直接运行 ``Release/PSBatteryTray.exe``，无需 Python 环境。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from psbt.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
