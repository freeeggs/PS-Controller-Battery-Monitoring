"""少量 Win32 API 封装（纯 ctypes，不引入 pywin32 依赖）。

集中放在这里的原因：这些调用都涉及 Windows 版本差异和 64 位指针宽度，
统一处理 argtypes / restype 比散落在各处安全。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Optional, Tuple

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# --------------------------------------------------------------------------
# 通知状态：Windows 自己就是用它来决定「要不要弹通知」的
# --------------------------------------------------------------------------
QUNS_NOT_PRESENT = 1                # 锁定 / 屏保 / 已注销
QUNS_BUSY = 2                       # 全屏应用（非 D3D）或演示中
QUNS_RUNNING_D3D_FULL_SCREEN = 3    # D3D 独占全屏（游戏）
QUNS_PRESENTATION_MODE = 4          # 演示模式
QUNS_ACCEPTS_NOTIFICATIONS = 5      # 可以正常弹通知
QUNS_QUIET_TIME = 6                 # 专注助手 / 免打扰时段
QUNS_APP = 7                        # 处于 Windows Store 应用内

_QUNS_LABEL = {
    QUNS_NOT_PRESENT: "用户不在（锁屏/屏保/未登录）",
    QUNS_BUSY: "全屏应用或演示中",
    QUNS_RUNNING_D3D_FULL_SCREEN: "全屏游戏（Direct3D）中",
    QUNS_PRESENTATION_MODE: "演示模式",
    QUNS_ACCEPTS_NOTIFICATIONS: "可正常显示通知",
    QUNS_QUIET_TIME: "专注助手/免打扰时段",
    QUNS_APP: "Windows 应用全屏中",
}


def query_notification_state() -> Optional[int]:
    """返回 ``SHQueryUserNotificationState`` 的状态值，失败返回 ``None``。"""
    try:
        fn = shell32.SHQueryUserNotificationState
    except AttributeError:  # pragma: no cover - 理论上 Vista+ 都有
        return None
    fn.argtypes = [ctypes.POINTER(ctypes.c_int)]
    fn.restype = ctypes.c_long
    state = ctypes.c_int(0)
    try:
        hr = fn(ctypes.byref(state))
    except OSError:
        return None
    if hr != 0:
        return None
    return state.value


def notifications_suppressed() -> Tuple[bool, str]:
    """判断当前是否处于「系统会抑制通知」的状态。

    返回 ``(是否被抑制, 原因文本)``。

    注意：这里返回的只是**系统层面**的判断（全屏游戏、演示、专注助手等）。
    Windows 10 的「专注助手」在部分配置下允许「优先级通知」通过，此 API
    无法区分，所以被抑制时我们并不放弃提醒，而是走替代通道。
    """
    state = query_notification_state()
    if state is None:
        return False, "无法查询通知状态"
    if state == QUNS_ACCEPTS_NOTIFICATIONS:
        return False, _QUNS_LABEL[state]
    return True, _QUNS_LABEL.get(state, "未知状态 %s" % state)


# --------------------------------------------------------------------------
# 单实例
# --------------------------------------------------------------------------
ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """基于命名互斥体的单实例守卫。

    句柄需要在进程生命周期内一直持有（不能提前关闭），因此这里保留强引用。
    """

    def __init__(self, name: str):
        self._name = name
        self._handle = None
        self.already_running = False

    def acquire(self) -> bool:
        """尝试获取；若已有实例在运行则返回 ``False``。"""
        handle = kernel32.CreateMutexW(None, True, self._name)
        err = ctypes.get_last_error()
        if not handle:
            # 拿不到互斥体时不要阻止程序启动，宁可多开也不要完全不可用
            return True
        self._handle = handle
        if err == ERROR_ALREADY_EXISTS:
            self.already_running = True
            return False
        return True

    def release(self) -> None:
        if self._handle:
            try:
                kernel32.ReleaseMutex(self._handle)
                kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None


# --------------------------------------------------------------------------
# 杂项
# --------------------------------------------------------------------------
def small_icon_size() -> int:
    """托盘小图标边长（随 DPI 变化：16/20/24/32）。"""
    try:
        v = user32.GetSystemMetrics(49)  # SM_CXSMICON
        if v and 8 <= v <= 128:
            return int(v)
    except Exception:
        pass
    return 16


def message_box(text: str, title: str, flags: int = 0x00000040) -> int:
    """MessageBoxW：0x40 = MB_ICONINFORMATION，0x40000 = MB_TOPMOST。"""
    MB_TOPMOST = 0x00040000
    try:
        return int(user32.MessageBoxW(None, str(text), str(title), int(flags) | MB_TOPMOST))
    except Exception:
        return 0


def open_in_explorer(path: str) -> bool:
    """用资源管理器打开目录（UTF-8 路径安全）。"""
    try:
        rc = shell32.ShellExecuteW(None, "open", str(path), None, None, 1)
        return int(rc) > 32
    except Exception:
        return False


def set_process_dpi_awareness() -> None:
    """尽量声明 DPI 感知；失败不影响运行。

    打包版本通过清单文件声明 PerMonitorV2，这里主要服务于源码运行场景。
    """
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def work_area() -> Tuple[int, int, int, int]:
    """主显示器工作区（排除任务栏）矩形 ``(left, top, right, bottom)``。"""
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    SPI_GETWORKAREA = 0x0030
    rect = RECT()
    try:
        ok = user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        if ok:
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


def close_handle(handle) -> None:
    try:
        if handle:
            kernel32.CloseHandle(handle)
    except Exception:
        pass


def pid_alive(pid: int) -> bool:
    """粗略判断进程是否存活（用于「已有实例」提示）。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    try:
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        close_handle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    except Exception:
        return False


def appdata_theme(light_value_name: str = "SystemUsesLightTheme") -> bool:
    """读取 ``AppsUseLightTheme`` / ``SystemUsesLightTheme``，返回是否为浅色。

    读不到时按 Windows 10 默认（深色任务栏）处理。
    """
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            0,
            winreg.KEY_READ,
        )
        try:
            value, _ = winreg.QueryValueEx(key, light_value_name)
        finally:
            winreg.CloseKey(key)
        return bool(int(value))
    except Exception:
        return False


def env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")
