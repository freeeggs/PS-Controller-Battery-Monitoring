# -*- coding: utf-8 -*-
"""端到端验证：抓**真实托盘**上的图标像素，反推它当前显示的是哪个状态。

为什么要这么做：单测只能证明「渲染函数输出正确」+「图像对象被推给了 pystray」，
但用户看到的是通知区域里那一小块像素。本工具直接对屏幕做模板匹配，
把「图标实际长什么样」变成可判定的结论 —— 用于验证
「读到电量后图标是否真的跟上」这类端到端问题。

用法（先让 PSBatteryTray.exe 跑起来）::

    python tools/verify_tray_state.py 85

会先展开 Windows 的「隐藏的图标」面板（若图标被折叠），再截图匹配。

性能：用亮度积分图预筛「可能是图标的窗口」，再对候选做提前终止比对，
纯 Python 也能在几秒内跑完 1400×520 的搜索区。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageGrab                       # noqa: E402

from psbt import winapi                                # noqa: E402
from psbt.tray import iconart                          # noqa: E402
from verify_tray_visual import click, find_chevron     # noqa: E402

BRIGHT = 128          # 亮度阈值：任务栏深色底，图标纯白
PAD = 24              # 搜索区外扩


def candidates(expect=None):
    """候选状态 → (前景像素列表, 背景像素列表)。

    **两种风格都列入候选**并把风格写进名称（``win10|电量 75%``），
    这样一次运行就能看出托盘上那 16×16 到底是哪种风格渲染的 ——
    真图标会与真实状态达到一致率 ≈1.0，另一种风格会明显更低。
    """
    size = winapi.small_icon_size()

    states = {}
    if expect is not None:
        e = int(expect)
        for pct in (e, max(0, e - 10), min(100, e + 10)):
            states["电量 %d%%" % pct] = (pct, False, iconart.MODE_LEVEL)
    else:
        for pct in range(100, -1, -5):
            states["电量 %d%%" % pct] = (pct, False, iconart.MODE_LEVEL)
    states["充电中"] = (80, True, iconart.MODE_LEVEL)
    states["无手柄"] = (None, False, iconart.MODE_NONE)
    states["电量未知"] = (None, False, iconart.MODE_UNKNOWN)
    states["告警 25%"] = (25, False, iconart.MODE_ALERT)

    out = {}
    for style in iconart.STYLES:
        painter = iconart.IconPainter(size=size, style=style)
        for name, (pct, charging, mode) in states.items():
            img = painter.render(pct, charging, mode)
            px = img.load()
            fg, bg = [], []
            for y in range(size):
                for x in range(size):
                    (fg if px[x, y][3] > 128 else bg).append((x, y))
            out["%s|%s" % (style, name)] = (fg, bg, size)
    return out


def integral(binary, w, h):
    """2D 前缀和，用于 O(1) 求任意 16×16 窗口的亮像素数。"""
    ii = [[0] * (w + 1) for _ in range(h + 1)]
    for y in range(h):
        row = binary[y]
        prev = ii[y]
        cur = ii[y + 1]
        s = 0
        for x in range(w):
            s += row[x]
            cur[x + 1] = prev[x + 1] + s
    return ii


def window_sum(ii, x, y, w):
    return (ii[y + w][x + w] - ii[y][x + w] - ii[y + w][x] + ii[y][x])


def main() -> int:
    expect = sys.argv[1] if len(sys.argv) > 1 else None

    screen = ImageGrab.grab()
    w, h = screen.size

    x, y = find_chevron()
    panel_open = False
    if x or y:
        click(x, y)
        time.sleep(1.2)
        panel_open = True
        screen = ImageGrab.grab()
        w, h = screen.size

    box = (max(0, w - 1400), max(0, h - 520), w, h)
    crop = screen.crop(box).convert("L")
    cw, ch = crop.size
    data = list(crop.getdata())
    binary = [[1 if data[yy * cw + xx] > BRIGHT else 0 for xx in range(cw)]
              for yy in range(ch)]

    cands = candidates(expect)
    size = winapi.small_icon_size()
    # 预筛窗口必须覆盖**所有**候选的前景像素数：
    # 「电量 100%」的实心块有上百个亮像素，而「无手柄」只有外框 + 一道斜杠，
    # 用单个候选的数量去筛会把另一种状态整片筛掉（踩过：全都匹配不上）。
    counts = [len(fg) for (fg, _bg, _s) in cands.values()]
    n_lo, n_hi = min(counts), max(counts)

    # ---- 预筛：亮像素数落在候选区间内的窗口 ----
    ii = integral(binary, cw, ch)
    spots = []
    lo, hi = int(n_lo * 0.65), int(n_hi * 1.45)
    for oy in range(0, ch - size + 1):
        for ox in range(0, cw - size + 1):
            c = window_sum(ii, ox, oy, size)
            if lo <= c <= hi:
                spots.append((ox, oy))
    print("搜索区 %dx%d，亮像素阈值区间 [%d, %d]，预筛出 %d 个候选位置"
          % (cw, ch, lo, hi, len(spots)))

    # ---- 精确比对（提前终止：先比前景，错太多直接放弃）----
    best = {}
    for name, (fg, bg, _s) in cands.items():
        b_ratio, b_pos = -1.0, None
        for (ox, oy) in spots:
            bad = 0
            tol = 2
            for (dx, dy) in fg:
                if not binary[oy + dy][ox + dx]:
                    bad += 1
                    if bad > tol:
                        break
            if bad > tol:
                continue
            for (dx, dy) in bg:
                if binary[oy + dy][ox + dx]:
                    bad += 1
                    if bad > tol:
                        break
            if bad > tol:
                continue
            ratio = (size * size - bad) / float(size * size)
            if ratio > b_ratio:
                b_ratio, b_pos = ratio, (ox, oy)
        best[name] = (b_ratio, b_pos)

    ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
    print("\n候选状态匹配排名（一致率 = 16×16 窗口内前景/背景完全一致的像素占比）：")
    for name, (ratio, pos) in ranked[:6]:
        print("   %-12s %.4f  位置=%s" % (name, ratio, pos))

    top_name, (top_ratio, top_pos) = ranked[0]
    print("\n→ 托盘图标当前显示：**%s**（一致率 %.4f）" % (top_name, top_ratio))

    if top_pos:
        px, py = top_pos
        pad = PAD
        x0, y0 = max(0, px - pad), max(0, py - pad)
        x1, y1 = min(cw, px + size + pad), min(ch, py + size + pad)
        marked = crop.crop((x0, y0, x1, y1)).convert("RGB")
        # 用红框标出匹配到的那 16×16
        mx0, my0 = px - x0, py - y0
        for yy in range(marked.size[1]):
            for xx in range(marked.size[0]):
                on = (xx in (mx0 - 1, mx0 + size) and my0 - 1 <= yy <= my0 + size) or \
                     (yy in (my0 - 1, my0 + size) and mx0 - 1 <= xx <= mx0 + size)
                if on:
                    marked.putpixel((xx, yy), (255, 0, 0))
        marked = marked.resize((marked.size[0] * 8, marked.size[1] * 8), Image.NEAREST)
        out = os.path.join(ROOT, "artifacts", "tray_state_check.png")
        marked.save(out)
        print("已写出标注图（红框=匹配位置，放大 8×）：%s" % out)

    if panel_open:
        click(x, y)

    if expect:
        ok = ("%s%%" % expect) in top_name
        print("\n期望：电量 %s%%    判定：%s" % (expect, "通过 ✅" if ok else "不通过 ❌"))
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
