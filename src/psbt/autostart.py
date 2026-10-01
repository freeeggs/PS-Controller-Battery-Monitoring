"""开机启动（Windows 推荐做法：当前用户 Run 键）。

为什么用 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``
---------------------------------------------------------------
* 微软官方文档 *Run and RunOnce Registry Keys* 明确把该键列为首选的自启动机制；
* 属于「当前用户」范围，**不需要管理员权限**，也不会触发 UAC；
* 相比之下计划任务（Task Scheduler）需要 COM/XML、行为更重，容易被安全软件
  当成可疑行为；启动文件夹（shell:startup）对打包单文件 exe 还多一个快捷方式
  依赖。因此这里选 Run 键，并保证：
  - 值名固定（``PSBatteryTray``），开关操作幂等；
  - 路径写的是**当前 exe 的绝对路径**，程序被移动/改名后重新开启即可修正；
  - 关闭时只删除自己的值，绝不触碰其它软件的自启动项。

已知限制
--------
用户在「任务管理器 → 启动」里把本项手动禁用后，Run 键的值仍然存在，
但 Windows 会在 ``...\\Explorer\\StartupApproved\\Run`` 里记录禁用状态而
不再启动。本模块会读取该状态并给出提示（仅提示，不自动改写）。
"""

from __future__ import annotations

import subprocess
from typing import List, Optional, Tuple

from .logs import get_logger
from .paths import command_line
from .version import APP_ID

log = get_logger("autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = APP_ID

try:
    import winreg
except ImportError:  # pragma: no cover - 非 Windows
    winreg = None  # type: ignore


def build_command(argv: Optional[List[str]] = None) -> str:
    """生成写入注册表的命令行（含 ``--autostart`` 标记）。

    :param argv: 覆盖默认命令行前缀（便于测试含空格路径的引号处理）
    """
    parts = list(argv if argv is not None else command_line()) + ["--autostart"]
    return subprocess.list2cmdline(parts)


def is_available() -> bool:
    return winreg is not None


def current_value(value_name: str = VALUE_NAME) -> Optional[str]:
    """返回注册表中当前记录的自启动命令行。

    :param value_name: 值名。默认是本程序自己的项；**测试必须传专用名字**，
        否则会读到/写到用户真实的自启动配置（详见 tests 里的说明）。
    """
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.debug("读取自启动项失败：%s", exc)
        return None


def restore_value(text: Optional[str], value_name: str = VALUE_NAME) -> bool:
    """把某个值**精确**写回为 ``text``；``text`` 为 ``None`` 表示删除该项。

    这是给"改完了要还原"用的：**不能用 enable() 代替**，因为 enable() 写的是
    当前构建的命令行，会把用户原来的自定义命令（旧路径、附加参数）永久覆盖掉。
    """
    if winreg is None:
        return False
    if text is None:
        return _delete(value_name)
    return write_raw(text, value_name)


def write_raw(text: str, value_name: str = VALUE_NAME) -> bool:
    """写入一行**原文**命令行（不追加 ``--autostart``）。"""
    if winreg is None:
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, str(text))
        return True
    except OSError as exc:
        log.error("写入自启动项失败：%s", exc)
        return False


def is_enabled(value_name: str = VALUE_NAME) -> bool:
    return current_value(value_name) is not None


def points_to_current_build(value_name: str = VALUE_NAME) -> bool:
    """注册表里的路径是否与当前运行的程序一致。"""
    value = current_value(value_name)
    if not value:
        return False
    first = command_line()[0].lower()
    return first in value.lower()


def enable(value_name: str = VALUE_NAME) -> bool:
    """写入自启动项。成功返回 ``True``。"""
    if winreg is None:
        return False
    cmd = build_command()
    if not write_raw(cmd, value_name):
        return False
    log.info("已开启开机启动：%s = %s", value_name, cmd)
    return True


def disable(value_name: str = VALUE_NAME) -> bool:
    """删除自启动项（不存在也算成功）。"""
    return _delete(value_name)


def _delete(value_name: str) -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, value_name)
        log.info("已关闭开机启动：%s", value_name)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.error("删除自启动项失败：%s", exc)
        return False


def set_enabled(enabled: bool) -> bool:
    return enable() if enabled else disable()


def blocked_by_task_manager() -> bool:
    """是否被任务管理器「启动」页禁用（仅用于提示、记录日志）。

    StartupApproved 下每条记录为 12 字节二进制，首字节最低位为 1 表示禁用。
    读不到或格式不认识时一律返回 ``False``（不做误判）。
    """
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    try:
        data = bytes(value)
        return bool(data) and bool(data[0] & 0x01)
    except Exception:
        return False


def status_text() -> Tuple[bool, str]:
    """返回 ``(是否已启用, 人类可读说明)``，供菜单 / 关于对话框使用。"""
    value = current_value()
    if value is None:
        return False, "未设置"
    if not points_to_current_build():
        return True, "已启用，但路径指向旧版本（建议关闭后重新开启）"
    if blocked_by_task_manager():
        return True, "已启用，但被「任务管理器 → 启动」禁用，Windows 不会自动运行"
    return True, "已启用"


def other_entries() -> List[Tuple[str, str]]:
    """列出 Run 键里其它名字含 ``ps``/``battery`` 的项，便于发现重复冲突。"""
    if winreg is None:
        return []
    found: List[Tuple[str, str]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            count = winreg.QueryInfoKey(key)[1]
            for i in range(count):
                name, value, _ = winreg.EnumValue(key, i)
                if name == VALUE_NAME:
                    continue
                low = name.lower()
                if "ps" in low and "batt" in low or "psbattery" in low or "ps-battery" in low:
                    found.append((name, str(value)))
    except OSError:
        pass
    return found
