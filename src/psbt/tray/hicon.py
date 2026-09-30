"""把 PIL 图像**零缩放**地变成 HICON，并接管 pystray 的图标创建路径。

根因记录（为什么必须有这个模块）
================================
pystray 的 ``_win32.Icon._assert_icon_handle`` 是这样创建图标的::

    with serialized_image(self.icon, 'ICO') as icon_path:
        self._icon_handle = win32.LoadImage(
            None, icon_path, win32.IMAGE_ICON,
            0, 0,
            win32.LR_DEFAULTSIZE | win32.LR_LOADFROMFILE)

``LR_DEFAULTSIZE`` 的含义是「按系统的**大图标**度量决定尺寸」，即
``SM_CXICON`` —— 1080P / 100% 缩放下等于 **32**；而托盘实际使用的是
``SM_CXSMICON`` = **16**。于是链路变成两次重采样::

    我们的 16×16 源图 --LoadImage 拉伸--> 32×32 HICON --通知区域缩回--> 16×16 绘制

1 像素宽的电池外框经不起两次插值，结果就是灰边、发虚、直线不平滑
（看起来「像被缩放过」）。实测对比见 ``tools/verify_hicon.py``：

* pystray 原路径 → HICON = **32×32**，边缘全是中间 alpha；
* 显式 ``cx=cy=SM_CXSMICON`` → 16×16，与源图逐像素一致；
* :func:`create_hicon` → 16×16，与源图逐像素一致。

因此这里不再让 Windows 决定尺寸，而是**按目标像素尺寸直接造 HICON**。
``CreateIconIndirect`` 只是把一段现成的 32bpp 位图包成图标句柄，
**代码路径里根本没有缩放**，所以物理上不可能引入插值。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Callable, List, Optional, Tuple

from PIL import Image

from ..logs import get_logger

log = get_logger("hicon")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SM_CXICON = 11                  # LR_DEFAULTSIZE 会取这个（大图标 = 32）
SM_CYICON = 12
SM_CXSMICON = 49                # 托盘小图标（= 16 @100%）
SM_CYSMICON = 50

BI_RGB = 0
DIB_RGB_COLORS = 0

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


# ---------------------------------------------------------------------------
# 结构体
# ---------------------------------------------------------------------------
class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


_prototypes_ready = False


def _declare_prototypes() -> None:
    """一次性声明原型。

    64 位下**必须**声明，否则句柄会被 ctypes 按 ``int`` 截断成 32 位，
    得到野句柄（这是本项目早期踩过的坑）。
    """
    global _prototypes_ready
    if _prototypes_ready:
        return

    _user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    _user32.GetSystemMetrics.restype = ctypes.c_int

    _user32.GetDC.argtypes = (wintypes.HWND,)
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    _user32.ReleaseDC.restype = ctypes.c_int

    _user32.CreateIconIndirect.argtypes = (ctypes.POINTER(ICONINFO),)
    _user32.CreateIconIndirect.restype = wintypes.HICON
    _user32.DestroyIcon.argtypes = (wintypes.HICON,)
    _user32.DestroyIcon.restype = wintypes.BOOL
    _user32.GetIconInfo.argtypes = (wintypes.HICON, ctypes.POINTER(ICONINFO))
    _user32.GetIconInfo.restype = wintypes.BOOL

    _gdi32.CreateDIBSection.argtypes = (
        wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
    _gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    _gdi32.CreateBitmap.argtypes = (
        ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p)
    _gdi32.CreateBitmap.restype = wintypes.HBITMAP
    _gdi32.DeleteObject.argtypes = (ctypes.c_void_p,)
    _gdi32.DeleteObject.restype = wintypes.BOOL
    _gdi32.GetObjectW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
    _gdi32.GetObjectW.restype = ctypes.c_int
    _gdi32.GetDeviceCaps.argtypes = (wintypes.HDC, ctypes.c_int)
    _gdi32.GetDeviceCaps.restype = ctypes.c_int
    _gdi32.GetDIBits.argtypes = (
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT)
    _gdi32.GetDIBits.restype = ctypes.c_int

    _kernel32.GetLastError.restype = wintypes.DWORD
    _prototypes_ready = True


# ---------------------------------------------------------------------------
# 尺寸度量（诊断用）
# ---------------------------------------------------------------------------
def icon_metrics() -> Tuple[int, int]:
    """返回 ``(SM_CXSMICON, SM_CXICON)``，即托盘尺寸与大图标尺寸。

    这两个值就是本 bug 的关键：pystray 用后者（32）加载，托盘实际用前者（16）。
    """
    _declare_prototypes()
    return (_user32.GetSystemMetrics(SM_CXSMICON),
            _user32.GetSystemMetrics(SM_CXICON))


def screen_dpi() -> int:
    """主屏 DPI（96 = 100% 缩放）。仅用于日志与诊断。"""
    _declare_prototypes()
    hdc = _user32.GetDC(None)
    try:
        return int(_gdi32.GetDeviceCaps(hdc, 88)) if hdc else 0     # LOGPIXELSX
    finally:
        if hdc:
            _user32.ReleaseDC(None, hdc)


# ---------------------------------------------------------------------------
# 核心：精确尺寸 HICON
# ---------------------------------------------------------------------------
def create_hicon(image: Image.Image) -> int:
    """把 RGBA 图包成 HICON，**尺寸就是图像尺寸，不做任何缩放**。

    :param image: 目标像素尺寸的 RGBA 图（如 16×16 / 20×20 / 32×32）
    :return: HICON 句柄（调用方负责用 :func:`destroy_hicon` 或
        ``DestroyIcon`` 释放）

    实现要点：

    * 颜色位图用 ``CreateDIBSection`` 建 32bpp 顶朝下 DIB，逐像素写入
      **预乘 alpha** 的 BGRA —— 32bpp 图标要求预乘，否则半透明边缘会出现暗边；
    * 掩膜位图用 ``CreateBitmap`` 建同尺寸 1bpp 全零位图（32bpp 图标由 alpha
      决定透明，掩膜只作为占位，这是通行做法）；
    * ``CreateIconIndirect`` 会**复制**这两张位图，所以随后立刻释放我们是正确的。
    """
    _declare_prototypes()

    src = image.convert("RGBA")
    w, h = src.size
    if w != h:
        raise ValueError("托盘图标必须是正方形，当前为 %dx%d" % (w, h))
    if w < 8:
        raise ValueError("图标尺寸过小：%d" % w)

    px = src.load()
    data = (ctypes.c_ubyte * (w * h * 4))()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                r = g = b = 0
            elif a != 255:                       # 预乘 alpha
                r = r * a // 255
                g = g * a // 255
                b = b * a // 255
            o = (y * w + x) * 4
            data[o] = b
            data[o + 1] = g
            data[o + 2] = r
            data[o + 3] = a

    bih = BITMAPINFOHEADER()
    bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bih.biWidth = w
    bih.biHeight = -h                            # 负数 = 顶朝下
    bih.biPlanes = 1
    bih.biBitCount = 32
    bih.biCompression = BI_RGB
    bih.biSizeImage = w * h * 4

    bits = ctypes.c_void_p()
    hdc = _user32.GetDC(None)
    try:
        hbm_color = _gdi32.CreateDIBSection(
            hdc, ctypes.byref(bih), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
    finally:
        if hdc:
            _user32.ReleaseDC(None, hdc)
    if not hbm_color:
        raise ctypes.WinError(ctypes.get_last_error() or 0)
    if not bits:
        _gdi32.DeleteObject(hbm_color)
        raise OSError("CreateDIBSection 未返回像素指针")

    ctypes.memmove(bits, data, w * h * 4)

    stride = ((w + 15) // 16) * 2                # 1bpp 行按 WORD 对齐
    zero = (ctypes.c_ubyte * (stride * h))()
    hbm_mask = _gdi32.CreateBitmap(w, h, 1, 1, zero)
    if not hbm_mask:
        _gdi32.DeleteObject(hbm_color)
        raise ctypes.WinError(ctypes.get_last_error() or 0)

    info = ICONINFO()
    info.fIcon = True
    info.xHotspot = 0
    info.yHotspot = 0
    info.hbmMask = hbm_mask
    info.hbmColor = hbm_color
    hicon = _user32.CreateIconIndirect(ctypes.byref(info))

    _gdi32.DeleteObject(hbm_mask)
    _gdi32.DeleteObject(hbm_color)               # CreateIconIndirect 已复制

    if not hicon:
        raise ctypes.WinError(ctypes.get_last_error() or 0)
    return int(hicon)


def destroy_hicon(hicon: Optional[int]) -> None:
    if hicon:
        _declare_prototypes()
        _user32.DestroyIcon(hicon)


def _read_hicon_pixels(hicon: int):
    """回读 HICON 颜色位图，返回 ``(w, h, 预乘BGRA字节)`` 或 ``None``。"""
    _declare_prototypes()
    info = ICONINFO()
    if not _user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None
    try:
        bm = BITMAP()
        if not _gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(BITMAP),
                                 ctypes.byref(bm)):
            return None
        w, h = int(bm.bmWidth), int(bm.bmHeight)
        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bih.biWidth = w
        bih.biHeight = -h                    # 顶朝下
        bih.biPlanes = 1
        bih.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        hdc = _user32.GetDC(None)
        try:
            got = _gdi32.GetDIBits(hdc, info.hbmColor, 0, h, buf,
                                   ctypes.byref(bih), DIB_RGB_COLORS)
        finally:
            if hdc:
                _user32.ReleaseDC(None, hdc)
        if got != h:
            return None
        return (w, h, buf.raw)
    finally:
        if info.hbmColor:
            _gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            _gdi32.DeleteObject(info.hbmMask)


def hicon_bitmap_size(hicon: int) -> Optional[Tuple[int, int]]:
    """回读 HICON 内部位图的实际像素尺寸 —— 用来证明「没有被缩放」。"""
    data = _read_hicon_pixels(hicon)
    return (data[0], data[1]) if data else None


def hicon_alpha_values(hicon: int) -> Optional[List[int]]:
    """回读 HICON 用到的全部 alpha 取值（降序）。

    这是判断「是否被插值缩放」最直接的指标：

    * 精确绘制 → 只有 ``[255, 0]``；
    * 被拉伸/缩放 → 会出现一批中间值（如 37/74/128/201…），也就是肉眼看到的灰边。
    """
    data = _read_hicon_pixels(hicon)
    if not data:
        return None
    w, h, raw = data
    return sorted({raw[(y * w + x) * 4 + 3] for y in range(h) for x in range(w)},
                  reverse=True)


def hicon_alpha_matrix(hicon: int) -> Optional[List[str]]:
    """回读 HICON 的像素，返回 ``'#'/'.'`` 矩阵（逐像素核对用）。"""
    data = _read_hicon_pixels(hicon)
    if not data:
        return None
    w, h, raw = data
    return ["".join("#" if raw[(y * w + x) * 4 + 3] > 128 else "."
                    for x in range(w)) for y in range(h)]


# ---------------------------------------------------------------------------
# pystray 集成
# ---------------------------------------------------------------------------
try:                                            # pragma: no cover - 平台相关
    import pystray
    _PYSTRAY_ICON = pystray.Icon
    _HAS_OVERRIDE_TARGET = hasattr(_PYSTRAY_ICON, "_assert_icon_handle")
except Exception:                               # pragma: no cover
    pystray = None
    _PYSTRAY_ICON = object
    _HAS_OVERRIDE_TARGET = False


class ExactSizeIcon(_PYSTRAY_ICON):             # type: ignore[misc]
    """pystray 的 ``Icon``，但用「精确尺寸 HICON」替换它的 ICO + LoadImage 路径。

    只覆盖 ``_assert_icon_handle``：pystray 在需要建/重建图标时会调用它
    （``_show`` → 首次显示、``_update_icon`` → 电量刷新、``WM_DISPLAYCHANGE``
    → ``_hide``+``_show``）。菜单、消息循环、任务栏重建等逻辑全部沿用原实现。

    :param renderer: 无参回调，返回**当前 DPI 目标尺寸**的 RGBA 图标。
        pystray 每次重建图标都会重新调用它，所以 DPI 变化后自动拿到正确尺寸。
    """

    def __init__(self, *args, renderer: Optional[Callable[[], Image.Image]] = None,
                 **kwargs):
        self._renderer = renderer
        self._hicon_verified = False
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    def _assert_icon_handle(self) -> None:      # noqa: D401 - 覆盖父类私有方法
        if self._icon_handle:
            return

        image = None
        if self._renderer is not None:
            try:
                image = self._renderer()
            except Exception:
                log.exception("按目标尺寸渲染托盘图标失败，回退 pystray 默认路径")

        if image is None:
            super()._assert_icon_handle()
            return

        self._icon_handle = create_hicon(image)
        self._log_once(image)

    def _log_once(self, image: Image.Image) -> None:
        """首次创建时自证一次：HICON 实际尺寸必须等于图像尺寸。"""
        if self._hicon_verified:
            return
        self._hicon_verified = True
        want = image.size
        got = hicon_bitmap_size(self._icon_handle)
        small, large = icon_metrics()
        if got == want:
            log.info("托盘图标 HICON 精确创建成功：%d×%d（SM_CXSMICON=%d，"
                     "未走 pystray 的 LR_DEFAULTSIZE 路径）", got[0], got[1], small)
        else:
            log.warning("托盘图标 HICON 尺寸异常：期望 %s，实际 %s"
                        "（SM_CXSMICON=%d，SM_CXICON=%d）", want, got, small, large)


def override_is_active() -> bool:
    """我们是否真的接管了 pystray 的图标创建。

    pystray 若改掉了 ``_assert_icon_handle`` 这个名字，覆盖会静默失效，
    图标会退回「被系统缩放」的老路。启动时检查一次并告警，避免悄悄退化。
    """
    if not _HAS_OVERRIDE_TARGET:
        log.warning("当前 pystray（%s）没有 _assert_icon_handle，无法接管图标创建，"
                    "托盘图标可能被系统二次缩放而发虚",
                    getattr(pystray, "__version__", "未知版本"))
        return False
    return True
