# -*- coding: utf-8 -*-
"""托盘图标清晰度验证：证明「交付给通知区域的就是 1:1 物理像素」。

用法（在项目根目录）：:

    python tools/verify_hicon.py

会依次做三件事：

1. 打印 ``SM_CXSMICON`` / ``SM_CXICON`` / DPI —— 说明 pystray 为什么会取错尺寸；
2. 用三条路径各自生成 HICON，回读其位图尺寸与像素矩阵：
   A) pystray 原路径（``LoadImage(0, 0, LR_DEFAULTSIZE)``）
   B) ``LoadImage`` 显式指定 ``cx=cy=SM_CXSMICON``
   C) ``hicon.create_hicon``（本项目现在使用的路径）
3. 逐像素比对 C 与我们渲染出的源图，并统计「中间 alpha 像素数」
   （灰边/羽化的直接量化指标）。
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PIL import Image                                     # noqa: E402

from psbt.tray import hicon, iconart                       # noqa: E402

u32 = ctypes.WinDLL("user32", use_last_error=True)
u32.LoadImageW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                           ctypes.c_int, ctypes.c_int, ctypes.c_uint)
u32.LoadImageW.restype = ctypes.c_void_p
u32.DestroyIcon.argtypes = (ctypes.c_void_p,)
u32.DestroyIcon.restype = ctypes.c_int

IMAGE_ICON = 1
LR_DEFAULTSIZE = 0x40
LR_LOADFROMFILE = 0x10


def intermediate_alpha(img: Image.Image) -> int:
    """统计既不是全透明也不是全不透明的像素数 —— 灰边/羽化的量化指标。"""
    return sum(1 for _r, _g, _b, a in img.convert("RGBA").getdata() if 0 < a < 255)


class _Tee:
    """把输出同时打到控制台和一个 UTF-8 报告文件（Windows 控制台编码不可靠）。"""

    def __init__(self, path: str):
        self._buf = []
        self._path = path

    def write(self, text: str) -> int:
        self._buf.append(text)
        try:
            sys.__stdout__.write(text)
        except Exception:
            pass
        return len(text)

    def flush(self) -> None:
        try:
            sys.__stdout__.flush()
        except Exception:
            pass

    def close(self) -> None:
        with open(self._path, "w", encoding="utf-8") as fh:
            fh.write("".join(self._buf))


def dump(rows, indent="    "):
    for r in rows:
        print(indent + "|" + r + "|")


def main() -> int:
    report_path = os.path.join(ROOT, "artifacts", "hicon_verify.txt")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    tee = _Tee(report_path)
    sys.stdout = tee
    try:
        return _run()
    finally:
        sys.stdout = sys.__stdout__
        tee.close()
        print("报告已写入 %s" % report_path)


def _run() -> int:
    small, large = hicon.icon_metrics()
    print("=" * 78)
    print("一、系统度量（这就是根因所在）")
    print("  SM_CXSMICON = %d    <- 托盘实际使用的小图标尺寸" % small)
    print("  SM_CXICON   = %d    <- LR_DEFAULTSIZE 会取样这个（大图标）" % large)
    print("  屏幕 LOGPIXELSX = %d（100%% 缩放 = 96）" % hicon.screen_dpi())

    size = small
    painter = iconart.IconPainter(size=size)
    src = painter.render(65, False, iconart.MODE_LEVEL)
    print("=" * 78)
    print("二、源图（我们的绘制输出，目标尺寸 %d×%d）" % (size, size))
    print("  尺寸 = %s   中间 alpha 像素 = %d" % (src.size, intermediate_alpha(src)))
    dump(iconart.alpha_matrix(src))

    tmpdir = tempfile.mkdtemp(prefix="psbt_hicon_")
    ico = os.path.join(tmpdir, "icon.ico")
    src.save(ico, format="ICO")

    print("=" * 78)
    print("三、三条路径生成的 HICON 对比")

    results = {}

    # ---- A: pystray 原路径 ----
    h_a = u32.LoadImageW(None, ico, IMAGE_ICON, 0, 0,
                         LR_DEFAULTSIZE | LR_LOADFROMFILE)
    print("\n【A】LoadImage(cx=0, cy=0, LR_DEFAULTSIZE|LR_LOADFROMFILE)")
    print("     = pystray 目前实际使用的调用")
    if h_a:
        size_a = hicon.hicon_bitmap_size(h_a)
        print("     HICON 位图尺寸 = %s" % (size_a,))
        print("     alpha 取值 = %s" % (hicon.hicon_alpha_values(h_a),))
        rows = hicon.hicon_alpha_matrix(h_a)
        if rows:
            dump(rows)
            n = sum(r.count("#") for r in rows)
            n_src = sum(r.count("#") for r in iconart.alpha_matrix(src))
            print("     不透明像素 = %d（源图 %d，放大 %.1f 倍 = %.1f² 面积比）"
                  % (n, n_src, n / float(n_src),
                     (size_a[0] / float(size)) ** 2 if size_a else 0))
        results["A"] = size_a
        u32.DestroyIcon(h_a)
    else:
        print("     加载失败")

    # ---- B: 显式小图标尺寸 ----
    h_b = u32.LoadImageW(None, ico, IMAGE_ICON, small, small, LR_LOADFROMFILE)
    print("\n【B】LoadImage(cx=%d, cy=%d, LR_LOADFROMFILE) —— 显式指定托盘尺寸"
          % (small, small))
    if h_b:
        print("     HICON 位图尺寸 = %s" % (hicon.hicon_bitmap_size(h_b),))
        print("     alpha 取值 = %s" % (hicon.hicon_alpha_values(h_b),))
        results["B"] = hicon.hicon_bitmap_size(h_b)
        u32.DestroyIcon(h_b)

    # ---- C: 我们的实现 ----
    h_c = hicon.create_hicon(src)
    print("\n【C】hicon.create_hicon（CreateIconIndirect 从精确 DIB）"
          " —— 本项目现在使用")
    got = hicon.hicon_bitmap_size(h_c)
    rows_c = hicon.hicon_alpha_matrix(h_c)
    print("     HICON 位图尺寸 = %s" % (got,))
    print("     alpha 取值 = %s" % (hicon.hicon_alpha_values(h_c),))
    if rows_c:
        dump(rows_c)
    results["C"] = got

    print("=" * 78)
    print("四、判定")
    exact = rows_c == iconart.alpha_matrix(src)
    print("  [C] 与源图逐像素一致：%s" % ("是 ✅" if exact else "否 ❌"))
    print("  [C] 尺寸 == SM_CXSMICON(%d)：%s" % (small, "是 ✅" if got == (small, small)
                                              else "否 ❌"))
    print("  [A] 尺寸 == SM_CXICON(%d)：%s   <- 说明 pystray 把图标放大到了大图标尺寸"
          % (large, "是" if results.get("A") == (large, large) else "否"))

    # ---- 多尺寸硬边检查 ----
    print("=" * 78)
    print("五、各 DPI 目标尺寸 × 全部四种状态的硬边检查")
    print("     （要求：每张图都只有 alpha 0 与 255 两种像素，且不透明像素为主色）")
    all_ok = True
    for s in (16, 20, 24, 32, 40, 48):
        geo = iconart.Geometry(s)
        print("  %3dpx：线宽=%d  外框=%dx%d  电量区=%dx%d"
              % (s, geo.sw, geo.body[2], geo.body[3] - geo.body[1],
                 geo.charge[2] - geo.charge[0], geo.charge[3] - geo.charge[1]))
        for label, pct, charging, mode in (
                ("正常 45%", 45, False, iconart.MODE_LEVEL),
                ("无手柄", None, False, iconart.MODE_NONE),
                ("电量未知", None, False, iconart.MODE_UNKNOWN),
                ("告警 20%", 20, False, iconart.MODE_ALERT),
                ("充电中", 80, True, iconart.MODE_LEVEL)):
            img = iconart.IconPainter(size=s).render(pct, charging, mode)
            alphas = iconart.alpha_values(img)
            ok = iconart.has_only_hard_alpha(img)
            all_ok = all_ok and ok
            print("        %-9s alpha=%-13s 灰边像素=%2d  %s"
                  % (label, sorted(alphas)[:4],
                     iconart.intermediate_alpha_count(img),
                     "✅" if ok else "❌"))

    hicon.destroy_hicon(h_c)

    print("=" * 78)
    print("结论：%s" % ("全部通过 ✅" if (exact and all_ok) else "存在失败项 ❌"))
    return 0 if (exact and all_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
