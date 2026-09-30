"""托盘图标绘制：Win10 风格**单色电池**（按参考图重绘的几何）。

几何基准
========
基准网格 **16 格宽 × 10 格高**（= 1080P / 100% 缩放下的 16×10 像素）。
逻辑格上的几何是半开区间 ``[x0, y0, x1, y1)``::

    (0,0)                     (14,0)  (16,0)
      +-------------------------+
      |                         |        <- 外框 14×10，线宽 1 格
      |    +---------------+    |  +--+  <- 正极 2×4，垂直居中
      |    |  电量块 10×6  |    |  |  |
      |    +---------------+    |  +--+
      +-------------------------+
    (0,10)                   (14,10)

    BODY   = (0, 0, 14, 10)   外壳外沿
    CAVITY = (1, 1, 13, 9)    内腔（外壳减去 1 格线宽）
    CHARGE = (2, 2, 12, 8)    电量块活动区 = 内腔**四周再内缩 1 格**
    NUB    = (14, 3, 16, 7)   正极

关键设计点：**电量块与外框之间有一圈 1 格的连续留白**（``CAVITY`` → ``CHARGE``
的内缩）。这不是「在电量条中间画一条线」，而是整个电量区域相对外框向内缩进，
四周留白等宽；电量块本身仍是纯实心几何形状。

电量块满格 10 格宽 × 6 格高，横向 1 格 = 10%，与手柄的 11 档电量天然对齐。

清晰度策略
==========
托盘图标最终只有 16~32 **物理**像素，任何「先画大图再缩到 16」的做法都会把
1 格宽的线条糊成灰边。所以本模块：

1. **直接在目标像素网格上计算几何**，不生成中间大图、不做重采样、不做超采样
   （见 :class:`Geometry`）。所有矩形的边界都是整数设备像素，因此每条水平/垂直
   边都**精确落在像素边界上**，天然没有抗锯齿过渡；
2. 图形**先分别生成单色掩膜、再着色合成**，不同颜色的块（白色外壳 / 琥珀告警块）
   永远不会在合成时互相污染；
3. 线宽 ``sw`` 随尺寸取整（16px→1、24/32px→2、40/48px→3…），既保证任何 DPI 下
   线宽都是整数像素，又保证外框**四周等宽**（不会出现一边 1px 一边 2px 的歪框）；
4. 尺寸由调用方按系统 DPI 传入（``winapi.small_icon_size()`` → 16/20/24/32），
   不写死 16。**并且**在 ``hicon.py`` 里用目标尺寸直接生成 HICON，杜绝由
   Windows 做二次缩放（这正是上一版发虚的真正原因，详见 ``hicon.py`` 的说明）。

为什么不用系统自带电池图标
==========================
``battery.dll`` / ``shell32.dll`` 里的电池图标属于未公开内部资源：
``ExtractIcon`` 只能拿到固定几档姿态的位图，无法表达 11 级电量、无法改色、
跨系统版本还可能不存在。因此自绘，形状上严格贴合参考图比例。
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# 逻辑几何（单位：格）
# ---------------------------------------------------------------------------
UNIT = 16                       # 基准宽度（格）
GLYPH_H = 10                    # 基准高度（格）
FRAME = 1                       # 外壳线宽（格）

BODY = (0, 0, 14, 10)           # 外壳外沿
CAVITY = (1, 1, 13, 9)          # 内腔（外壳减线宽）
CHARGE = (2, 2, 12, 8)          # 电量块活动区（内腔四周内缩 1 格）
NUB = (14, 3, 16, 7)            # 正极
FILL_UNITS = 10                 # 电量块满格宽度 = 10 格 = 每 10% 一格
CHARGE_ROWS = 6                 # 电量块高度（格）

MASTER = 256                    # 程序图标（exe / 关于框）主尺寸

# ---------------------------------------------------------------------------
# 颜色
# ---------------------------------------------------------------------------
COLOR_LIGHT = (255, 255, 255, 255)     # 深色任务栏 → 白色图形
COLOR_DARK = (24, 24, 24, 255)         # 浅色任务栏 → 深色图形
ACCENT_WARN = (255, 185, 0, 255)       # ≤30% 告警（琥珀）
ACCENT_CRITICAL = (232, 17, 35, 255)   # ≤10% 告警（红）
ICON_BLUE = (32, 90, 184, 255)         # 程序图标底色（仅 exe/关于框使用）

STYLE_WIN10 = "win10"
STYLE_WIN11 = "win11"
STYLES = (STYLE_WIN10, STYLE_WIN11)
STYLE_LABELS = {STYLE_WIN10: "Windows 10", STYLE_WIN11: "Windows 11"}

# 圆角半径（以 16 格为基准描述，实际按目标尺寸换算并取整）。
# 2.4 是在 16px 下切掉 1 个角像素、32px 下切掉 3 个 —— 与 Win11 实机观感一致
# （参考截图上 30px 宽的字形，角上是 2 像素的台阶过渡）。
RADIUS_MASTER = 2.4

MODE_LEVEL = "level"
MODE_NONE = "none"
MODE_UNKNOWN = "unknown"
MODE_ALERT = "alert"

# ---------------------------------------------------------------------------
# 叠加图案（坐标都相对**所在区域**，单位仍是格）
# ---------------------------------------------------------------------------
# 告警感叹号（相对电量块）：竖条 4 行 + 空 1 行 + 点 1 行，居中 2 格宽
_EXCL_BAR = (4, 0, 6, 4)
_EXCL_DOT = (4, 5, 6, 6)
# 「电量未知」的横杠（相对电量块）
_UNKNOWN_DASH = (2, 2, 9, 4)
# 「无手柄」的斜杠（相对内腔，左下 → 右上）
_SLASH_FROM = (0, 7)
_SLASH_TO = (11, 1)

# 用户提供的基准图（1080P / 100% 缩放下的 16×16 源文件）逐像素内容，
# 对应电量 65%（6/10 格）。放在这里做回归锚点：以后任何改动都必须
# 仍然逐像素等于它，否则就是几何被改歪了。
REFERENCE_16_AT_65 = (
    "................",
    "................",
    "................",
    "##############..",
    "#............#..",
    "#.######.....#..",
    "#.######.....###",
    "#.######.....###",
    "#.######.....###",
    "#.######.....###",
    "#.######.....#..",
    "#............#..",
    "##############..",
    "................",
    "................",
    "................",
)


# ---------------------------------------------------------------------------
# 整数像素几何
# ---------------------------------------------------------------------------
def _round_half(v: float) -> int:
    """四舍五入（远离零方向）。刻意不用 ``round``：它是银行家舍入。"""
    return int(v + 0.5) if v >= 0 else -int(-v + 0.5)


class Geometry:
    """把 16×10 的逻辑格几何映射到**任意目标像素尺寸**，边界全为整数像素。

    这是本模块「锐利」的核心：**不是缩放图片，而是按目标尺寸重新计算几何**。

    * ``sw = round(size/16)``：线宽随尺寸取整（16px→1、24/32px→2、40/48px→3…）。
      外框四周用同一个 ``sw``，所以永远是**对称等宽**的干净直角框；
    * 外沿宽按 14/16、高按 10/16 取整；正极高按 4/16 取整并垂直居中；
    * 电量块活动区 = 外沿向内缩 **2 个线宽**（= 外框 1 层 + 留白 1 层）；
    * 叠加图案（感叹号 / 横杠 / 斜杠）在各自区域内按格等分取整，
      因此永远贴合区域、永远不产生半像素。

    16px 下这套映射退化为「1 格 = 1 像素」，与用户提供的基准图逐像素一致
    （由 :data:`REFERENCE_16_AT_65` 与单测锁定）。
    """

    __slots__ = ("size", "sw", "gh", "oy", "body", "nub", "cavity", "charge",
                 "style", "radius")

    def __init__(self, size: int, style: str = STYLE_WIN10):
        self.size = int(size)
        s = self.size
        if s < 8:
            raise ValueError("图标尺寸过小：%d（最小 8）" % s)

        self.style = style
        self.sw = max(1, _round_half(s / float(UNIT)))
        self.gh = max(3 * self.sw, _round_half(GLYPH_H * s / float(UNIT)))
        self.oy = _round_half((s - self.gh) / 2.0)

        body_w = max(5 * self.sw, _round_half(14 * s / float(UNIT)))
        body_w = min(body_w, s - 1)             # 至少留 1px 给正极
        bx0, by0, bx1, by1 = 0, self.oy, body_w, self.oy + self.gh
        self.body = (bx0, by0, bx1, by1)

        # 圆角半径：win10 为 0（直角），win11 按尺寸取整（仍落在整数像素上）
        if style == STYLE_WIN11:
            r = _round_half(RADIUS_MASTER * s / float(UNIT))
            self.radius = max(1, min(r, self.gh // 2, body_w // 2))
        else:
            self.radius = 0

        nub_h = max(self.sw, _round_half(4 * s / float(UNIT)))
        nub_y0 = by0 + _round_half((self.gh - nub_h) / 2.0)
        self.nub = (bx1, nub_y0, s, nub_y0 + nub_h)

        sw = self.sw
        self.cavity = (bx0 + sw, by0 + sw, bx1 - sw, by1 - sw)
        self.charge = (bx0 + 2 * sw, by0 + 2 * sw, bx1 - 2 * sw, by1 - 2 * sw)

    # -- 圆角辅助 --------------------------------------------------------
    @property
    def is_round(self) -> bool:
        return self.radius > 0

    def inner_radius(self) -> int:
        """内腔的圆角半径（外半径减去一个线宽）。"""
        return max(0, self.radius - self.sw)

    def fill_radius(self) -> int:
        """电量块的圆角半径（比内腔再小一个线宽）。"""
        return max(0, self.radius - 2 * self.sw)

    # -- 电量块内的格坐标 → 设备像素 -------------------------------------
    def charge_x(self, k: int) -> int:
        """电量块内第 ``k`` 格（0..10）的横向设备坐标（单调不减）。"""
        x0, x1 = self.charge[0], self.charge[2]
        return x0 + _round_half(k * (x1 - x0) / float(FILL_UNITS))

    def charge_y(self, k: int) -> int:
        """电量块内第 ``k`` 行（0..6）的纵向设备坐标（单调不减）。"""
        y0, y1 = self.charge[1], self.charge[3]
        return y0 + _round_half(k * (y1 - y0) / float(CHARGE_ROWS))

    def charge_box(self, x0: int, y0: int, x1: int, y1: int) -> Tuple[int, int, int, int]:
        return (self.charge_x(x0), self.charge_y(y0),
                self.charge_x(x1), self.charge_y(y1))

    def fill_box(self, units: int) -> Tuple[int, int, int, int]:
        """电量块实际填充区域（左对齐，宽度 = units 格）。"""
        u = max(0, min(FILL_UNITS, int(units)))
        y0, y1 = self.charge[1], self.charge[3]
        return (self.charge_x(0), y0, self.charge_x(u), y1)

    # -- 内腔内的格坐标 → 设备像素（斜杠用）------------------------------
    def cavity_x(self, k: int) -> int:
        x0, x1 = self.cavity[0], self.cavity[2]
        return x0 + _round_half(k * (x1 - x0) / 12.0)

    def cavity_y(self, k: int) -> int:
        y0, y1 = self.cavity[1], self.cavity[3]
        return y0 + _round_half(k * (y1 - y0) / 8.0)

    def cavity_box(self, x0: int, y0: int, x1: int, y1: int) -> Tuple[int, int, int, int]:
        return (self.cavity_x(x0), self.cavity_y(y0),
                self.cavity_x(x1), self.cavity_y(y1))


# ---------------------------------------------------------------------------
# 掩膜绘制
# ---------------------------------------------------------------------------
def _line_pixels(x0: int, y0: int, x1: int, y1: int) -> List[Tuple[int, int]]:
    """Bresenham：返回从 (x0,y0) 到 (x1,y1) 的 1 格宽像素台阶。

    用「像素台阶」而不是抗锯齿直线，是为了和整体的像素化风格一致 ——
    在小尺寸下一条抗锯齿斜线会变成一条灰雾，台阶反而更清晰。
    """
    pts: List[Tuple[int, int]] = []
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    while True:
        pts.append((x, y))
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy
    return pts


def _fill(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int],
          val: int = 255) -> None:
    """填充半开矩形 ``[x0,y0,x1,y1)``（Pillow 的 rectangle 是闭区间，需 -1）。"""
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return                            # 宽度为 0 时什么都不画（如 0% 电量）
    d.rectangle((x0, y0, x1 - 1, y1 - 1), fill=val)


def _rounded_pixels(box: Tuple[int, int, int, int], radius: int,
                    corners: Tuple[bool, bool, bool, bool] = (True, True, True, True)):
    """生成**圆角矩形**的半开区域 ``[x0,y0,x1,y1)`` 内的像素坐标。

    圆角用「像素级圆形裁剪」实现 —— 每个角只做整像素的取舍，**没有任何
    抗锯齿**，因此渲染出来的 alpha 仍然只有 0 / 255，符合本模块的硬边约定。

    :param radius: 圆角半径（整数像素）；``0`` 时退化为普通矩形
    :param corners: (左上, 右上, 右下, 左下) 是否倒圆角 —— 正极只圆右侧用得到
    """
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return
    w, h = x1 - x0, y1 - y0
    r = max(0, int(radius))
    r = min(r, w // 2, h // 2)

    if r <= 0:
        for y in range(y0, y1):
            for x in range(x0, x1):
                yield x, y
        return

    # 每个角的裁剪方块与圆心（圆心在距两条边各 r 处）
    blocks = (
        (corners[0], x0, x0 + r, y0, y0 + r, x0 + r, y0 + r),
        (corners[1], x1 - r, x1, y0, y0 + r, x1 - r, y0 + r),
        (corners[2], x1 - r, x1, y1 - r, y1, x1 - r, y1 - r),
        (corners[3], x0, x0 + r, y1 - r, y1, x0 + r, y1 - r),
    )
    rr = r * r
    for y in range(y0, y1):
        for x in range(x0, x1):
            keep = True
            for (on, bx0, bx1, by0, by1, cx, cy) in blocks:
                if on and bx0 <= x < bx1 and by0 <= y < by1:
                    dx = (x + 0.5) - cx
                    dy = (y + 0.5) - cy
                    if dx * dx + dy * dy > rr:
                        keep = False
                        break
            if keep:
                yield x, y


def _fill_round(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], radius: int,
                val: int = 255,
                corners: Tuple[bool, bool, bool, bool] = (True, True, True, True)) -> None:
    """按圆角矩形填充（逐像素落点，无抗锯齿）。"""
    for (x, y) in _rounded_pixels(box, radius, corners):
        d.rectangle((x, y, x, y), fill=val)


def _fill_units(percent: Optional[int]) -> int:
    """百分比 → 电量块格数（0 ~ 10）。

    手柄只上报 11 档（``percent = level * 10 + 5``，满档 100），所以这里做的是
    **逆向还原档位**而不是线性插值 —— 保证 5%→0 格、15%→1 格 …… 95%→9 格、
    100%→10 格，一档一格，不伪造不存在的中间精度。
    """
    if percent is None:
        return 0
    p = max(0, min(100, int(percent)))
    if p <= 0:
        return 0
    if p >= 100:
        return FILL_UNITS
    return max(0, min(FILL_UNITS, int((p - 5) / 10.0 + 0.5)))


def _render_masks(size: int, units: int, mode: str, charging: bool,
                  style: str = STYLE_WIN10) -> Tuple[Image.Image, Image.Image]:
    """在**目标像素尺寸**的画布上直接绘制，返回两个二值掩膜 ``(base, accent)``。

    * ``base``   —— 用主色（白/黑）绘制的部分：外壳、正极、电量块、斜杠等；
    * ``accent`` —— 用强调色绘制的部分：告警时的电量块。

    两个掩膜天然不重叠（电量块与外框之间隔着留白），因此合成时不会互相污染。

    两种风格**共用同一套几何**（:class:`Geometry`），唯一区别是 ``STYLE_WIN11``
    会把外框 / 内腔 / 电量块 / 正极的角**按整数像素倒圆**（``radius > 0``），
    其余尺寸、线宽、格数完全一致 —— 这样两种风格的电量表现方式不会跑偏。

    .. note::
       ``charging`` 参数**保留但已不影响图形**：充电时也按真实电量填充
       （早先会画满块 + 抠闪电，但闪电会盖住电量条，看不出充到多少）。
       保留参数是为了不动调用方签名；充电状态由悬浮提示与菜单文字表达。

    .. note::
       这里**没有任何缩放/超采样**：坐标全部由 :class:`Geometry` 算成整数像素，
       直接落在像素网格上，所以边缘只有「画」与「不画」两种状态。
    """
    del charging                        # 见上：充电不再改变图形
    geo = Geometry(size, style)
    base = Image.new("L", (size, size), 0)
    accent = Image.new("L", (size, size), 0)
    db = ImageDraw.Draw(base)
    da = ImageDraw.Draw(accent)

    bx0, by0, bx1, by1 = geo.body
    sw = geo.sw

    if geo.is_round:
        # ---- 圆角外壳：外圆角矩形「减去」内圆角矩形 = 等宽圆角描边 ----
        outer = set(_rounded_pixels(geo.body, geo.radius))
        inner = set(_rounded_pixels(geo.cavity, geo.inner_radius()))
        for (x, y) in outer - inner:
            db.rectangle((x, y, x, y), fill=255)
        # ---- 圆角正极：只倒右边的两个角，左边与壳体相接 ----
        _fill_round(db, geo.nub, max(1, geo.radius // 2),
                    corners=(False, True, True, False))
    else:
        # ---- 直角外壳：四条等宽边 ----
        _fill(db, (bx0, by0, bx1, by0 + sw))              # 上
        _fill(db, (bx0, by1 - sw, bx1, by1))              # 下
        _fill(db, (bx0, by0, bx0 + sw, by1))              # 左
        _fill(db, (bx1 - sw, by0, bx1, by1))              # 右
        # ---- 正极 ----
        _fill(db, geo.nub)

    if mode == MODE_NONE:
        for (x, y) in _line_pixels(*_SLASH_FROM, *_SLASH_TO):
            _fill(db, geo.cavity_box(x, y, x + 1, y + 1))
        return base, accent

    if mode == MODE_UNKNOWN:
        if geo.is_round:
            _fill_round(db, geo.charge_box(*_UNKNOWN_DASH), geo.fill_radius())
        else:
            _fill(db, geo.charge_box(*_UNKNOWN_DASH))
        return base, accent

    if mode == MODE_ALERT:
        # 告警：电量块画满并着色强调色，再从块里抠出感叹号（抠成透明，
        # 于是「外框—留白—块」的层次依然清晰）。
        if geo.is_round:
            _fill_round(da, geo.charge, geo.fill_radius())
        else:
            _fill(da, geo.charge)
        _fill(da, geo.charge_box(*_EXCL_BAR), val=0)
        _fill(da, geo.charge_box(*_EXCL_DOT), val=0)
        return base, accent

    # ---- 正常电量 ----
    # 充电时**照样显示真实电量**：不再画满块 + 抠闪电。闪电会盖住电量条，
    # 用户没法一眼看出充到了多少 —— 而这才是托盘图标最该回答的问题。
    # 充电状态改由悬浮提示与菜单文字表达（"充电中 / 已充满"）。
    if units > 0:
        if geo.is_round:
            _fill_round(db, geo.fill_box(units), geo.fill_radius())
        else:
            _fill(db, geo.fill_box(units))

    return base, accent


def alpha_matrix(img: Image.Image) -> List[str]:
    """把图标还原成 ``'#'/'.'`` 矩阵，用于逐像素核对与回归测试。"""
    px = img.load()
    w, h = img.size
    return ["".join("#" if px[x, y][3] > 128 else "." for x in range(w))
            for y in range(h)]


def has_only_hard_alpha(img: Image.Image) -> bool:
    """图标是否**只有**「透明(0)」与「主色不透明(255)」两种像素。

    四种状态（正常 / 无手柄 / 电量未知 / 告警）都必须是纯二值 alpha：
    任何第 3 种取值都意味着出现了羽化、灰边或整体半透明，在深色任务栏上
    会直接表现为「发灰」。本函数就是这条硬约束的判据。
    """
    return alpha_values(img) <= {0, 255}


def alpha_values(img: Image.Image) -> set:
    """图标中用到的全部 alpha 取值（诊断用）。"""
    return {a for _r, _g, _b, a in img.convert("RGBA").getdata()}


def intermediate_alpha_count(img: Image.Image) -> int:
    """半透明像素数（0 < alpha < 255）—— 灰边/羽化的量化指标。

    按设计本指标**恒为 0**（见 :func:`has_only_hard_alpha`）；
    一旦不为 0，说明图标被插值缩放过，或有人重新引入了整体半透明。
    """
    return sum(1 for _r, _g, _b, a in img.convert("RGBA").getdata() if 0 < a < 255)


def _paste(out: Image.Image, mask: Image.Image, color) -> None:
    """把纯色按掩膜贴到 RGBA 画布上（先画掩膜后着色，避免颜色互相污染）。"""
    layer = Image.new("RGBA", out.size, tuple(color))
    out.paste(layer, (0, 0), mask)


# ---------------------------------------------------------------------------
# 画笔
# ---------------------------------------------------------------------------
class IconPainter:
    """按 (电量, 是否充电, 模式, 风格) 生成托盘图标，内部带缓存。

    :param primary: 主色（跟任务栏明暗主题走）
    :param accent: 是否在低电量时使用醒目强调色
    :param size: **目标像素边长**，应由 ``winapi.small_icon_size()`` 按 DPI 给出
    :param style: :data:`STYLE_WIN10`（直角）或 :data:`STYLE_WIN11`（圆角）

    .. note::
       ``charging`` 只用于**低电量告警判定**（充电中不告警），
       **不改变图标图形** —— 充电时同样显示真实电量。
    """

    def __init__(self, primary: Tuple[int, int, int, int] = COLOR_LIGHT,
                 accent: bool = True, size: int = MASTER,
                 style: str = STYLE_WIN10):
        self.primary = tuple(primary)
        self.accent = accent
        self.size = int(size)
        self.style = style
        self._cache: Dict[tuple, Image.Image] = {}

    # ------------------------------------------------------------------
    def render(self, percent: Optional[int] = None, charging: bool = False,
               mode: str = MODE_LEVEL) -> Image.Image:
        key = (percent, charging, mode, self.style)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        img = self._draw(percent, charging, mode)
        if len(self._cache) > 96:       # 防止长时间运行缓存无限增长
            self._cache.clear()
        self._cache[key] = img
        return img

    # ------------------------------------------------------------------
    def _alert_color(self, percent: Optional[int]):
        if not self.accent:
            return self.primary
        return ACCENT_CRITICAL if (percent is not None and percent <= 10) else ACCENT_WARN

    def _draw(self, percent: Optional[int], charging: bool, mode: str) -> Image.Image:
        # 四种模式一律使用**同一个主色**（深色任务栏 → 纯白 #FFFFFF，
        # 浅色任务栏 → 主题深色），不做任何整体透明度调整。
        #
        # 为什么「无手柄 / 电量未知」也不降低不透明度：
        # 半透明会引入第三种 alpha 值（曾用 105 / 200），在深色任务栏上看着就是
        # 「发灰」，破坏「只有透明(0) 与主色(255) 两种像素」的硬边约定。
        # 这两种状态改由**图形**区分（斜杠 / 横杠），颜色与正常状态完全一致。
        base_color = self.primary
        accent_color = self._alert_color(percent) if mode == MODE_ALERT else None

        base_mask, accent_mask = _render_masks(
            self.size, _fill_units(percent), mode, charging, self.style
        )
        out = Image.new("RGBA", (self.size, self.size), (0, 0, 0, 0))
        _paste(out, base_mask, base_color)
        if accent_color is not None:
            _paste(out, accent_mask, accent_color)
        return out


# ---------------------------------------------------------------------------
# 状态 → 图标
# ---------------------------------------------------------------------------
def icon_for(views, painter: IconPainter, alert: bool = False) -> Image.Image:
    """根据当前手柄列表给出托盘图标。

    规则（对应需求「托盘图标优先显示电量最低的那个手柄」）：

    * 没有手柄 → 无手柄图标（不会残留任何一次的电量）；
    * 有手柄但都读不到电量 → 未知图标；
    * 否则取**电量最低**的那台；``alert=True`` 时使用告警变体（闪烁用）。
    """
    if not views:
        return painter.render(None, False, MODE_NONE)
    known = [v for v in views if v.percent is not None]
    if not known:
        return painter.render(None, False, MODE_UNKNOWN)
    lowest = min(known, key=lambda v: (v.percent, v.display_name))
    charging = lowest.battery.state.is_charging
    mode = MODE_ALERT if (alert and not charging) else MODE_LEVEL
    return painter.render(lowest.percent, charging, mode)


def tooltip_for(views, detailed: bool = True) -> str:
    """托盘悬浮提示。Windows 托盘提示上限 127 字符，这里主动截断。"""
    if not views:
        return "PS 手柄电量监视器\n未检测到手柄"
    lines = ["PS 手柄电量监视器"]
    for v in views:
        text = v.tooltip_line()
        if not detailed and v.battery.level is not None:
            text = "%s: 约 %d%%" % (v.short_name, v.battery.percent or 0)
        lines.append(text)
    if len(views) > 1:
        lowest = min((v for v in views if v.percent is not None),
                     key=lambda v: v.percent, default=None)
        if lowest is not None:
            lines.append("图标显示：%s（最低）" % lowest.short_name)
    text = "\n".join(lines)
    return text[:126]


# ---------------------------------------------------------------------------
# 程序图标（exe / 关于对话框）
# ---------------------------------------------------------------------------
def application_icon(size: int = MASTER) -> Image.Image:
    """程序图标：蓝色方块底 + 白色电池。

    注意：这是**唯一**带底色、带圆角的地方 —— 它出现在资源管理器、任务栏、
    Alt+Tab 这些需要品牌识别度的位置，而托盘图标是纯单色直角。
    电池字形本身与托盘图标完全同一套几何，保证观感统一。
    """
    s = int(size)
    if s <= 16:
        glyph_size = s                      # 16px 下没有留白空间，直接满铺
    else:
        glyph_size = max(14, min(s - 2, _round_half(s * 0.82)))
    margin = (s - glyph_size) // 2

    glyph = Image.new("RGBA", (glyph_size, glyph_size), (0, 0, 0, 0))
    base_mask, _accent = _render_masks(glyph_size, _fill_units(65), MODE_LEVEL, False)
    _paste(glyph, base_mask, (255, 255, 255, 255))

    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, s - 1, s - 1],
                           radius=max(1, _round_half(s * 0.16)), fill=ICON_BLUE)
    img.alpha_composite(glyph, dest=(margin, margin))
    return img


# ---------------------------------------------------------------------------
# ICO 输出（自己写容器，保证每个 DPI 子项都是**按目标尺寸重绘**的锐利位图）
# ---------------------------------------------------------------------------
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _bmp_entry(im: Image.Image) -> bytes:
    """把 RGBA 图编码成 ICO 内嵌的 32bpp BITMAPINFOHEADER + BGRA + AND 掩膜。"""
    w, h = im.size
    header = struct.pack("<IiiHHIIiiII",
                         40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    px = im.convert("RGBA").load()
    rows = []
    for y in range(h - 1, -1, -1):          # BMP 是自下而上存储
        row = bytearray()
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                r = g = b = 0
            elif a != 255:                  # 32bpp 图标要求预乘 alpha
                r = r * a // 255
                g = g * a // 255
                b = b * a // 255
            row += bytes((b, g, r, a))      # BGRA
        rows.append(bytes(row))
    xor = b"".join(rows)
    stride = ((w + 31) // 32) * 4           # AND 掩膜每行按 4 字节对齐
    return header + xor + (b"\x00" * (stride * h))


def _write_ico(path: str, images: Sequence[Tuple[int, Image.Image]]) -> None:
    offset = 6 + 16 * len(images)
    entries, blobs = [], []
    for size, im in images:
        blob = _bmp_entry(im)
        entries.append((size, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, len(images)))
        for size, length, off in entries:
            dim = 0 if size >= 256 else size     # 256 用 0 表示
            fh.write(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, length, off))
        for blob in blobs:
            fh.write(blob)


def save_ico(path: str, size: int = MASTER) -> None:
    """保存多尺寸 .ico。

    刻意**不**用 Pillow 的 ICO 写出：它是从一张大图逐档缩放，会把 1 像素宽的
    外框糊成灰边。这里逐个尺寸重新绘制（每个尺寸都是硬边），再自己组装容器，
    这样 100% / 125% / 150% / 200% 下拿到的都是对应尺寸的锐利位图。
    失败时回落到 Pillow 写出，保证不因为图标问题中断打包。
    """
    sizes = [s for s in ICO_SIZES if s <= int(size)]
    if not sizes:
        sizes = [int(size)]
    try:
        _write_ico(path, [(s, application_icon(s)) for s in sizes])
    except Exception:
        application_icon(size).save(path, format="ICO")


# ---------------------------------------------------------------------------
# 预览图（人工核对用）
# ---------------------------------------------------------------------------
def _states(painter: IconPainter):
    out = [painter.render(pct, False, MODE_LEVEL) for pct in range(100, -1, -10)]
    out.append(painter.render(None, False, MODE_NONE))
    out.append(painter.render(None, False, MODE_UNKNOWN))
    out.append(painter.render(80, True, MODE_LEVEL))
    out.append(painter.render(30, False, MODE_ALERT))
    out.append(painter.render(20, False, MODE_ALERT))
    out.append(painter.render(5, False, MODE_ALERT))
    return out


def save_preview(path: str) -> None:
    """按**真实托盘尺寸**渲染并放大，用于人工核对观感。

    直接在 64px 渲染再缩放会骗人 —— 托盘里实际只有 16px（100% DPI）
    到 32px（200% DPI）。所以按 16/20/24/32 渲染、**最近邻**放大，
    并铺上深色/浅色任务栏底色检查对比度。两种风格各出一组。
    """
    zoom = 6
    cell = 32 * zoom + 10
    rows = []
    for style in STYLES:
        for color, bg in ((COLOR_LIGHT, (31, 31, 31, 255)),
                          (COLOR_DARK, (243, 243, 243, 255))):
            for size in (16, 20, 24, 32):
                rows.append((bg, IconPainter(primary=color, accent=True, size=size,
                                             style=style)))

    rendered = [(bg, _states(p)) for bg, p in rows]
    cols = max(len(s) for _, s in rendered)
    sheet = Image.new("RGBA", (cell * cols + 8, cell * len(rendered) + 8),
                      (140, 140, 140, 255))
    for r, (bg, states) in enumerate(rendered):
        for c, im in enumerate(states):
            x, y = 4 + c * cell, 4 + r * cell
            sheet.alpha_composite(Image.new("RGBA", (cell - 8, cell - 8), bg),
                                  dest=(x, y))
            up = im.resize((im.size[0] * zoom, im.size[1] * zoom), Image.NEAREST)
            sheet.alpha_composite(up, dest=(x + (cell - 8 - up.size[0]) // 2,
                                            y + (cell - 8 - up.size[1]) // 2))
    sheet.save(path)
