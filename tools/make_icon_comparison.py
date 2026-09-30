# -*- coding: utf-8 -*-
"""生成托盘图标「清晰度对比图」：把两条真实链路的结果并排放大给肉眼核对。

左边 = 现在的方案（``hicon.create_hicon``，HICON 恒等于 ``SM_CXSMICON``）；
右边 = 旧方案（pystray 的 ``LoadImage(LR_DEFAULTSIZE)``，先把 16px 拉到 32px，
        再由通知区域缩回 16px）。

放大一律用**最近邻**，不引入任何额外插值，否则图本身就会骗人。

输出：``artifacts/icon_sharpness_compare.png``
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PIL import Image, ImageDraw, ImageFont                # noqa: E402

from psbt.tray import hicon, iconart                        # noqa: E402

u32 = ctypes.WinDLL("user32", use_last_error=True)
u32.LoadImageW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                           ctypes.c_int, ctypes.c_int, ctypes.c_uint)
u32.LoadImageW.restype = ctypes.c_void_p
u32.DestroyIcon.argtypes = (ctypes.c_void_p,)
u32.DestroyIcon.restype = ctypes.c_int

IMAGE_ICON = 1
LR_DEFAULTSIZE = 0x40
LR_LOADFROMFILE = 0x10

TASKBAR = (31, 31, 31, 255)          # Win10 深色任务栏底色
ZOOM = 16
PANEL = 16 * ZOOM                    # 256


def load_font(size: int):
    win = os.environ.get("WINDIR", r"C:\Windows")
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "segoeui.ttf"):
        path = os.path.join(win, "Fonts", name)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def straight_rgba(w, h, raw):
    """HICON 位图是**预乘** BGRA；这里还原成直通 alpha 的 RGBA 图。"""
    img = Image.new("RGBA", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            o = (y * w + x) * 4
            b, g, r, a = raw[o], raw[o + 1], raw[o + 2], raw[o + 3]
            if a and a != 255:
                r = min(255, r * 255 // a)
                g = min(255, g * 255 // a)
                b = min(255, b * 255 // a)
            px[x, y] = (r, g, b, a)
    return img


def hicon_to_image(hicon_handle) -> Image.Image:
    data = hicon._read_hicon_pixels(hicon_handle)
    if not data:
        raise RuntimeError("无法回读 HICON 像素")
    return straight_rgba(*data)


def zoomed(img: Image.Image, bg, zoom=ZOOM) -> Image.Image:
    """合成到背景色后最近邻放大 —— 与托盘上的观感一致。"""
    canvas = Image.new("RGBA", (img.size[0] * zoom, img.size[1] * zoom), bg)
    up = img.resize((img.size[0] * zoom, img.size[1] * zoom), Image.NEAREST)
    canvas.alpha_composite(up)
    return canvas.convert("RGB")


def main() -> int:
    size = hicon.icon_metrics()[0]
    src = iconart.IconPainter(size=size).render(65, False, iconart.MODE_LEVEL)

    tmp = tempfile.mkdtemp(prefix="psbt_cmp_")
    ico = os.path.join(tmp, "icon.ico")
    src.save(ico, format="ICO")

    # ---- 新方案：精确尺寸 HICON ----
    h_new = hicon.create_hicon(src)
    new_img = hicon_to_image(h_new)
    hicon.destroy_hicon(h_new)

    # ---- 旧方案：pystray 的 LR_DEFAULTSIZE（32×32）→ 通知区域缩回 16×16 ----
    h_old = u32.LoadImageW(None, ico, IMAGE_ICON, 0, 0,
                           LR_DEFAULTSIZE | LR_LOADFROMFILE)
    old_big = hicon_to_image(h_old)
    u32.DestroyIcon(h_old)
    old_img = old_big.resize((size, size), Image.BOX)

    # ---- 统计 ----
    def gray_count(img):
        n = 0
        px = img.convert("RGBA").load()
        for y in range(img.size[1]):
            for x in range(img.size[0]):
                _r, _g, _b, a = px[x, y]
                if 0 < a < 255:
                    n += 1
        return n

    font = load_font(22)
    small = load_font(17)
    pad = 24
    gap = 40
    header = 100
    row_h = PANEL + 78
    label_w = 330
    W = pad * 3 + label_w + PANEL + gap + PANEL
    H = header + row_h * 2 + 20

    sheet = Image.new("RGB", (W, H), (245, 245, 246))
    d = ImageDraw.Draw(sheet)

    d.text((pad, 24), "托盘图标清晰度对比（1080P / 100%% 缩放，放大 %d× 最近邻）" % ZOOM,
           font=font, fill=(24, 24, 26))
    d.text((pad, 60), "放大不引入插值；右侧的灰边完全来自 Windows 的两次重采样",
           font=small, fill=(110, 110, 116))

    rows = [
        ("现在的方案  create_hicon", "HICON 精确 %d×%d，零缩放" % (size, size),
         new_img, (28, 104, 40)),
        ("旧方案  pystray LR_DEFAULTSIZE", "HICON 被拉到 32×32，再缩回 %d×%d" % (size, size),
         old_img, (168, 32, 32)),
    ]

    x_panel = pad * 2 + label_w
    for i, (title, subtitle, img, color) in enumerate(rows):
        ry = header + i * row_h
        # 左：文字说明 + 1:1 真实像素
        d.text((pad, ry + 34), title, font=small, fill=color)
        d.text((pad, ry + 58), subtitle, font=small, fill=(120, 120, 126))
        d.text((pad, ry + 124), "1:1 实际像素：", font=small, fill=(90, 90, 96))
        real = Image.new("RGBA", img.size, TASKBAR)
        real.alpha_composite(img)
        sheet.paste(real.convert("RGB"), (pad + 126, ry + 120))

        # 右：×16 放大
        d.text((x_panel, ry + 6), "×%d 放大" % ZOOM, font=small, fill=(90, 90, 96))
        sheet.paste(zoomed(img, TASKBAR), (x_panel, ry + 30))
        d.rectangle([x_panel - 1, ry + 29, x_panel + PANEL, ry + 30 + PANEL],
                    outline=(175, 175, 182))

        cnt = gray_count(img)
        verdict = ("无灰边（alpha 仅 0/255）" if cnt == 0
                   else "有灰边：中间 alpha 像素 %d 个 / 共 %d" % (cnt, size * size))
        d.text((x_panel, ry + 30 + PANEL + 8), verdict, font=small,
               fill=(28, 104, 40) if cnt == 0 else (168, 32, 32))

    out = os.path.join(ROOT, "artifacts", "icon_sharpness_compare.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    sheet.save(out)

    print("新方案 中间 alpha 像素 = %d" % gray_count(new_img))
    print("旧方案 中间 alpha 像素 = %d" % gray_count(old_img))
    print("旧方案 缩放前尺寸 = %s" % (old_big.size,))
    print("已写出 %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
