"""应用数据目录解析。

所有可变数据都放在 ``%APPDATA%\\PSBatteryTray`` 下，卸载时删除该目录即可，
不会在程序目录里留下散落文件（也不需要在 Program Files 下写权限）。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache

from .version import APP_NAME


def is_frozen() -> bool:
    """是否由 PyInstaller 打包运行。"""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


@lru_cache(maxsize=1)
def data_dir() -> str:
    """返回并创建应用数据目录。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        # 极少数受限环境下退回到临时目录，保证程序仍可运行
        import tempfile

        path = os.path.join(tempfile.gettempdir(), APP_NAME)
        os.makedirs(path, exist_ok=True)
    return path


def log_dir() -> str:
    path = os.path.join(data_dir(), "logs")
    os.makedirs(path, exist_ok=True)
    return path


def config_path() -> str:
    return os.path.join(data_dir(), "config.json")


def executable_path() -> str:
    """当前可执行文件路径（打包后为 exe，源码运行为 python 解释器）。"""
    return os.path.abspath(sys.executable)


def command_line() -> list[str]:
    """用于写入开机启动的完整命令行前缀（不含 --autostart）。

    * 打包运行：``["C:\\path\\PSBatteryTray.exe"]``
    * 源码运行：``["C:\\...\\pythonw.exe", "C:\\...\\run.py"]``
    """
    if is_frozen():
        return [executable_path()]
    exe = executable_path()
    # 尽量使用 pythonw.exe，避免源码运行时弹出控制台窗口
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    # src/psbt/paths.py -> src/psbt -> src -> 项目根目录
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    entry = os.path.join(project_root, "run.py")
    return [exe, entry]
