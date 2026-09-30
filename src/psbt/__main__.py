"""命令行入口。

用法::

    PSBatteryTray.exe                    正常后台运行（无窗口，直接进托盘）
    PSBatteryTray.exe --autostart        开机启动调用（与正常启动等价，仅记录来源）
    PSBatteryTray.exe --simulate         模拟模式：用内置时间线伪造手柄，便于验收
    PSBatteryTray.exe --simulate=quick   模拟模式的 4 倍速版本
    PSBatteryTray.exe --dump-hid         排障：列出 HID 设备并转储原始报告后退出
    PSBatteryTray.exe --console          源码运行时同时把日志打到控制台
    PSBatteryTray.exe --debug            打开 DEBUG 级日志
    PSBatteryTray.exe --reset-config     恢复默认配置后退出
    PSBatteryTray.exe --version          显示版本
"""

from __future__ import annotations

import argparse
import os
import sys

from . import logs, paths, winapi
from .version import APP_DESC, APP_FULL_NAME, APP_VERSION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="PSBatteryTray",
        description=APP_DESC,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--autostart", action="store_true",
                        help="由开机启动触发（不弹任何提示）")
    parser.add_argument("--simulate", nargs="?", const="demo", metavar="SCENARIO",
                        help="模拟模式，可选 demo / quick")
    parser.add_argument("--dump-hid", type=int, nargs="?", const=20, metavar="SECONDS",
                        help="转储 HID 设备与原始报告（默认 20 秒）后退出")
    parser.add_argument("--console", action="store_true", help="同时输出日志到控制台")
    parser.add_argument("--debug", action="store_true", help="DEBUG 级日志")
    parser.add_argument("--reset-config", action="store_true", help="恢复默认配置后退出")
    parser.add_argument("--version", action="store_true", help="显示版本")
    return parser


def _stream_from_std_handle(which: int):
    """把一个有效的标准句柄包成文本流；失败返回 ``None``。"""
    try:
        import ctypes
        import msvcrt

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetFileType.argtypes = (ctypes.c_void_p,)
        handle = kernel32.GetStdHandle(which)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        ftype = kernel32.GetFileType(ctypes.c_void_p(handle))
        if ftype == 0:                               # FILE_TYPE_UNKNOWN
            return None
        if ftype == 1:                               # FILE_TYPE_CHAR：真控制台
            kernel32.SetConsoleOutputCP(65001)       # 让 UTF-8 中文正常显示
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
        return os.fdopen(fd, "w", encoding="utf-8", buffering=1, errors="replace")
    except Exception:
        return None


def _attach_existing_std_stream() -> bool:
    """若标准输出句柄有效，就沿用它（重定向 / 管道 / 从控制台启动）。

    打包为窗口程序时 PyInstaller 会把 ``sys.stdout`` 置为 ``None``，
    但**标准句柄本身可能依然有效**：调用方做了 ``PSBatteryTray.exe --version >
    out.txt``，或从 cmd 启动继承了父进程的控制台。这种情况下必须直接用该句柄，
    否则输出会跑到新申请的控制台窗口里，调用它的脚本什么也抓不到。

    只有句柄无效（双击启动，Explorer 不提供标准句柄）时才返回 ``False``，
    由调用方去 :func:`attach_console` 申请一个新控制台。
    """
    if sys.stdout is None or not _has_fileno(sys.stdout):
        stream = _stream_from_std_handle(-11)        # STD_OUTPUT_HANDLE
        if stream is None:
            return False
        sys.stdout = stream
    if sys.stderr is None or not _has_fileno(sys.stderr):
        stream = _stream_from_std_handle(-12)        # STD_ERROR_HANDLE
        if stream is not None:
            sys.stderr = stream
    return True


def _has_fileno(stream) -> bool:
    try:
        stream.fileno()
        return True
    except Exception:
        return False


def attach_console() -> None:
    """打包为窗口程序（--noconsole）时，为诊断模式准备好输出目标。

    优先沿用已有标准句柄（重定向/管道/继承的控制台）；
    都没有时才申请一个新控制台 —— 此时双击运行的诊断参数才会弹出一个窗口。
    """
    if not paths.is_frozen():
        return
    if _attach_existing_std_stream():
        return
    try:
        import ctypes

        if ctypes.windll.kernel32.GetConsoleWindow():
            return
        if not ctypes.windll.kernel32.AllocConsole():
            return
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except Exception:
        pass


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # 打包为窗口程序（--noconsole）后 sys.stdout 为 None，print 的输出会被直接
    # 丢弃。所有「需要给用户看输出」的路径都必须先自己申请一个控制台，
    # 否则 --version / --reset-config 会表现为「双击闪一下什么都没发生」。
    if args.version or args.dump_hid is not None or args.reset_config or args.console:
        attach_console()

    if args.version:
        print("%s %s" % (APP_FULL_NAME, APP_VERSION))
        return 0

    if args.dump_hid is not None:
        logs.setup_logging("DEBUG" if args.debug else "INFO", console=args.console)
        logs.install_excepthook()
        from .app import dump_hid

        return dump_hid(int(args.dump_hid))

    if args.reset_config:
        try:
            if os.path.exists(paths.config_path()):
                os.replace(paths.config_path(), paths.config_path() + ".bak")
            print("配置已重置（旧文件备份为 config.json.bak）：%s" % paths.config_path())
        except OSError as exc:
            print("重置失败：%s" % exc)
        return 0

    winapi.set_process_dpi_awareness()
    level = "DEBUG" if args.debug else _config_log_level()
    logs.setup_logging(level, console=args.console)
    logs.install_excepthook()

    from .app import Application

    return Application(args).run()


def _config_log_level() -> str:
    """读取配置里的日志级别（配置文件本身由 Application 加载，这里只 peek）。"""
    try:
        import json

        with open(paths.config_path(), "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("log_level") or "INFO")
    except Exception:
        return "INFO"


if __name__ == "__main__":
    raise SystemExit(main())
