"""置顶提醒弹窗（纯 Win32 实现，不依赖 tkinter / win10toast）。

为什么需要它
------------
Windows 10 在以下情况会**直接丢弃**普通 Toast / 气泡通知：

* 全屏运行 D3D 游戏（``QUNS_RUNNING_D3D_FULL_SCREEN``）
* 演示模式（``QUNS_PRESENTATION_MODE``）
* 系统静默时段（``QUNS_QUIET_TIME``，指首次登录 / 系统升级后的第一个小时）
* 锁定屏幕 / 屏保（``QUNS_NOT_PRESENT``）

这些状态下 ``Shell_NotifyIcon(NIF_INFO)`` 不报错，但用户什么都看不到。
因此本程序除了「尽力发通知」，还会在右下角弹出一个**自绘的置顶窗口**：
它不经过通知中心，是绕过系统通知抑制的可靠兜底手段。

.. note::
   上面这一组状态来自 ``SHQueryUserNotificationState``。**它检测不到
   专注助手（Focus Assist）** —— 专注助手不在该 API 的枚举里（早先把
   ``QUNS_QUIET_TIME`` 当成"专注助手"是错的）。专注助手确实会吞掉气泡
   通知，但被吞掉时通道 1/2（图标闪烁、提示音）照样触发，提醒不会整个丢失；
   想让置顶窗口**始终**弹出，把配置 ``alert_popup`` 设为 ``"always"``。

实现要点
--------
* 独立线程 + 自己的消息循环，不与 pystray 的托盘窗口互相阻塞；
* ``WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE``：置顶、不进
  Alt+Tab/任务栏、**不抢焦点**（游戏/打字时不会被打断）；
* ``WM_MOUSEACTIVATE`` 返回 ``MA_NOACTIVATE``，点击也不激活；
* 定时器自动关闭，点击任意处立即关闭；
* GDI ``DrawTextW`` 绘制，字号随显示器 DPI 缩放，配色跟随系统明暗主题。

已知限制
--------
* 独占全屏（Exclusive Fullscreen）游戏会覆盖一切窗口，此时弹窗同样看不到；
  这类场景退回「更换托盘图标 + 提示音」，详见 :mod:`psbt.notify.notifier`；
* 窗口不参与 Windows 11 的圆角/云母效果；在 Windows 10 上观感与系统一致。
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Dict, Optional, Tuple

from .. import winapi
from ..logs import get_logger

log = get_logger("popup")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 64 位下句柄是 8 字节，**必须**显式声明 restype，否则 ctypes 会按 int 截断！
LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


# ---- 常量 ----
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = wintypes.HWND(-1)
WM_PAINT = 0x000F
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_MOUSEACTIVATE = 0x0021
WM_APP = 0x8000
WM_SHOW = WM_APP + 17
WM_HIDE = WM_APP + 18
MA_NOACTIVATE = 3
DT_LEFT = 0x0000
DT_WORDBREAK = 0x0010
DT_NOPREFIX = 0x0800
DT_SINGLELINE = 0x0020
DT_VCENTER = 0x0004
DT_END_ELLIPSIS = 0x00008000
FW_NORMAL = 400
FW_BOLD = 700
LOGPIXELSY = 90
DEFAULT_CHARSET = 1
CLEARTYPE_QUALITY = 5
TRANSPARENT = 1
IDC_ARROW = 32512

PALETTE = {
    "light": ((0xF3, 0xF3, 0xF3), (0xC8, 0xC8, 0xC8), (0x10, 0x10, 0x10), (0x4A, 0x4A, 0x4A)),
    "dark": ((0x25, 0x25, 0x25), (0x3A, 0x3A, 0x3A), (0xFF, 0xFF, 0xFF), (0xC8, 0xC8, 0xC8)),
}
ACCENT = {
    "info": (0x00, 0x78, 0xD7),
    "warn": (0xFF, 0xB9, 0x00),
    "critical": (0xE8, 0x11, 0x23),
}


def _declare_prototypes() -> None:
    """显式声明参数与返回类型 —— 64 位下句柄/指针绝不能走默认 int。"""
    P = ctypes.POINTER

    user32.RegisterClassW.argtypes = [P(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.WORD

    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
    user32.DefWindowProcW.restype = LRESULT

    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL

    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.restype = None

    user32.GetMessageW.argtypes = [P(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [P(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [P(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT

    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL

    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL

    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL

    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT,
                                ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    user32.KillTimer.restype = wintypes.BOOL

    user32.BeginPaint.argtypes = [wintypes.HWND, P(PAINTSTRUCT)]
    user32.BeginPaint.restype = wintypes.HDC
    user32.EndPaint.argtypes = [wintypes.HWND, P(PAINTSTRUCT)]
    user32.EndPaint.restype = wintypes.BOOL

    user32.FillRect.argtypes = [wintypes.HDC, P(wintypes.RECT), wintypes.HBRUSH]
    user32.FillRect.restype = ctypes.c_int
    user32.FrameRect.argtypes = [wintypes.HDC, P(wintypes.RECT), wintypes.HBRUSH]
    user32.FrameRect.restype = ctypes.c_int

    user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                                 P(wintypes.RECT), wintypes.UINT]
    user32.DrawTextW.restype = ctypes.c_int

    user32.GetClientRect.argtypes = [wintypes.HWND, P(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.InvalidateRect.argtypes = [wintypes.HWND, P(wintypes.RECT), wintypes.BOOL]
    user32.InvalidateRect.restype = wintypes.BOOL

    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int

    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.LoadCursorW.restype = wintypes.HANDLE

    gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.CreateFontW.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    gdi32.SetTextColor.restype = wintypes.COLORREF
    gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetBkMode.restype = ctypes.c_int
    gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.GetDeviceCaps.restype = ctypes.c_int

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE


_declare_prototypes()


def _rgb(color: Tuple[int, int, int]) -> int:
    return color[0] | (color[1] << 8) | (color[2] << 16)


class ToastPopup:
    """右下角置顶提醒窗（单例，内部自带线程）。"""

    _CLASS_NAME = "PSBatteryTrayToast"

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._hwnd: Optional[wintypes.HWND] = None
        self._wndproc = WNDPROC(self._wnd_proc)      # 必须保持强引用
        self._content: Dict[str, object] = {}
        self._fonts: Dict[tuple, int] = {}
        self._stack_offset = 0

    # ------------------------------------------------------------------
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="psbt-popup", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            log.warning("提醒弹窗线程启动超时（提醒将退回声音与图标方式）")
            return False
        return True

    def stop(self) -> None:
        self._stop.set()
        hwnd = self._hwnd
        if hwnd:
            try:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    def show(self, title: str, message: str, severity: str = "info",
             seconds: int = 10) -> bool:
        """显示提醒；可从任意线程调用。"""
        if not self.start():
            return False
        hwnd = self._hwnd
        if not hwnd:
            return False
        with self._lock:
            self._content = {
                "title": str(title),
                "message": str(message),
                "severity": severity if severity in ACCENT else "info",
                "seconds": max(2, int(seconds)),
            }
        try:
            user32.PostMessageW(hwnd, WM_SHOW, 0, 0)
        except Exception as exc:
            log.debug("投递弹窗消息失败：%s", exc)
            return False
        return True

    def hide(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            try:
                user32.PostMessageW(hwnd, WM_HIDE, 0, 0)
            except Exception:
                pass

    def is_available(self) -> bool:
        return bool(self._hwnd)

    # ------------------------------------------------------------------
    # 弹窗线程
    # ------------------------------------------------------------------
    def _run(self) -> None:
        try:
            self._create_window()
        except Exception:
            log.exception("创建提醒弹窗失败")
            self._ready.set()
            return
        self._ready.set()

        msg = wintypes.MSG()
        while not self._stop.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        log.debug("提醒弹窗线程结束")

    def _create_window(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = None
        wc.hCursor = user32.LoadCursorW(None, ctypes.cast(IDC_ARROW, wintypes.LPCWSTR))
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = self._CLASS_NAME
        user32.RegisterClassW(ctypes.byref(wc))       # 已注册时返回 0，无需处理

        w, h = self._window_size()
        hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            self._CLASS_NAME, "PSBatteryTray 提醒", WS_POPUP,
            0, 0, w, h, None, None, hinst, None,
        )
        if not hwnd:
            raise OSError("CreateWindowExW 失败，错误码 %d" % ctypes.get_last_error())
        self._hwnd = hwnd
        log.debug("提醒弹窗已创建 hwnd=%s 尺寸=%dx%d", hwnd, w, h)

    def _dpi_scale(self) -> float:
        try:
            hdc = user32.GetDC(None)
            dpi = gdi32.GetDeviceCaps(hdc, LOGPIXELSY)
            user32.ReleaseDC(None, hdc)
            if dpi:
                return max(1.0, min(3.0, dpi / 96.0))
        except Exception:
            pass
        return 1.0

    def _window_size(self) -> Tuple[int, int]:
        s = self._dpi_scale()
        return int(384 * s), int(94 * s)

    # ------------------------------------------------------------------
    # 消息处理
    # ------------------------------------------------------------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam) -> int:
        try:
            if msg == WM_SHOW:
                self._on_show(hwnd)
                return 0
            if msg == WM_HIDE:
                self._dismiss(hwnd)
                return 0
            if msg == WM_PAINT:
                self._on_paint(hwnd)
                return 0
            if msg == WM_TIMER:
                self._dismiss(hwnd)
                return 0
            if msg == WM_LBUTTONUP:
                self._dismiss(hwnd)
                return 0
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE       # 不抢焦点
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._delete_fonts()
                self._hwnd = None
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("弹窗消息处理异常 msg=0x%X", msg)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _dismiss(self, hwnd) -> None:
        user32.ShowWindow(hwnd, SW_HIDE)
        user32.KillTimer(hwnd, 1)
        self._stack_offset = 0

    def _on_show(self, hwnd) -> None:
        with self._lock:
            content = dict(self._content)
        seconds = int(content.get("seconds", 10))
        w, h = self._window_size()
        left, top, right, bottom = winapi.work_area()
        margin = int(12 * self._dpi_scale())
        x = max(left, right - w - margin)
        y = max(top, bottom - h - margin - self._stack_offset)
        # 同时弹出多条时向上叠放，避免互相遮挡
        self._stack_offset = min(self._stack_offset + h + 8, h * 3)

        user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)
        user32.InvalidateRect(hwnd, None, True)
        user32.KillTimer(hwnd, 1)
        user32.SetTimer(hwnd, 1, seconds * 1000, None)
        log.info("已弹出提醒窗口：%s（%d 秒）", content.get("title", ""), seconds)

    # ------------------------------------------------------------------
    def _on_paint(self, hwnd) -> None:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        try:
            self._draw(hwnd, hdc)
        except Exception:
            log.exception("绘制提醒窗口失败")
        finally:
            user32.EndPaint(hwnd, ctypes.byref(ps))

    def _draw(self, hwnd, hdc) -> None:
        with self._lock:
            content = dict(self._content)
        title = str(content.get("title", ""))
        message = str(content.get("message", ""))
        severity = str(content.get("severity", "info"))

        rect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        w, h = rect.right, rect.bottom

        theme = "light" if winapi.appdata_theme("AppsUseLightTheme") else "dark"
        bg, border, title_color, body_color = PALETTE[theme]
        s = self._dpi_scale()

        brush = gdi32.CreateSolidBrush(_rgb(bg))
        user32.FillRect(hdc, ctypes.byref(rect), brush)
        gdi32.DeleteObject(brush)

        # 左侧强调色竖条：信息分级一眼可见
        bar_w = int(5 * s)
        bar = wintypes.RECT(0, 0, bar_w, h)
        brush = gdi32.CreateSolidBrush(_rgb(ACCENT.get(severity, ACCENT["info"])))
        user32.FillRect(hdc, ctypes.byref(bar), brush)
        gdi32.DeleteObject(brush)

        # 直角边框，贴合 Win10 观感
        frame = gdi32.CreateSolidBrush(_rgb(border))
        user32.FrameRect(hdc, ctypes.byref(rect), frame)
        gdi32.DeleteObject(frame)

        gdi32.SetBkMode(hdc, TRANSPARENT)

        pad = int(14 * s)
        text_left = bar_w + pad
        text_right = w - pad

        title_font = self._font(-int(15 * s), FW_BOLD)
        old_font = gdi32.SelectObject(hdc, title_font)
        gdi32.SetTextColor(hdc, _rgb(title_color))
        r = wintypes.RECT(text_left, pad, text_right, pad + int(22 * s))
        user32.DrawTextW(hdc, title, -1, ctypes.byref(r),
                         DT_LEFT | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX | DT_END_ELLIPSIS)

        body_font = self._font(-int(13 * s), FW_NORMAL)
        gdi32.SelectObject(hdc, body_font)
        gdi32.SetTextColor(hdc, _rgb(body_color))
        r = wintypes.RECT(text_left, pad + int(24 * s), text_right, h - pad)
        user32.DrawTextW(hdc, message, -1, ctypes.byref(r),
                         DT_LEFT | DT_WORDBREAK | DT_NOPREFIX)

        gdi32.SelectObject(hdc, old_font)

    def _font(self, height: int, weight: int) -> int:
        key = (height, weight)
        handle = self._fonts.get(key)
        if handle:
            return handle
        handle = gdi32.CreateFontW(
            height, 0, 0, 0, weight, 0, 0, 0, DEFAULT_CHARSET,
            0, 0, CLEARTYPE_QUALITY, 0, "Segoe UI",
        )
        self._fonts[key] = handle
        return handle

    def _delete_fonts(self) -> None:
        for handle in self._fonts.values():
            try:
                gdi32.DeleteObject(handle)
            except Exception:
                pass
        self._fonts.clear()
