"""托盘图标与动态菜单（pystray 封装）。

职责边界
--------
本模块只做「把状态画出来 + 把用户操作转成回调」，不碰 HID、不管通知策略。
所有状态通过 ``get_views`` 回调拉取（拉模式），避免上层把状态推来推去。

关键实现点
----------
* 菜单是**动态**的：``pystray.Menu`` 接受一个可调用对象，每次 ``update_menu()``
  重新生成项目，因此「已连接手柄列表」能随插拔/电量变化实时刷新；
* 托盘图标 = **电量最低**的那台手柄（需求要求），用算法保证而不是靠列表顺序；
* 没有手柄时显示「无手柄」图标，绝不残留上一次的电量；
* 图标颜色跟随任务栏明暗主题（浅色任务栏画深色图标，否则图标会「隐身」）；
* 低电量时由 :meth:`TrayApp.flash_alert` 让图标在「正常 / 告警」之间闪烁 ——
  这是不受专注助手影响的提醒通道之一。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import pystray
from PIL import Image

from .. import autostart, winapi
from ..config import Config
from ..controllers.models import ControllerView
from ..logs import get_logger
from ..theme import ThemeWatcher
from ..version import APP_FULL_NAME, APP_NAME, APP_VERSION
from . import hicon, iconart

log = get_logger("tray")

BLINK_INTERVAL = 0.55      # 告警闪烁间隔（秒）
UPDATE_THROTTLE = 0.15     # 最小刷新间隔，防止短时间大量 Shell_NotifyIcon 调用


@dataclass
class TrayCallbacks:
    """托盘需要的上层能力（全部可选，便于单独测试托盘）。"""

    get_views: Callable[[], List[ControllerView]] = lambda: []
    on_rescan: Callable[[], None] = lambda: None
    on_quit: Callable[[], None] = lambda: None
    on_show_info: Callable[[], None] = lambda: None
    on_about: Callable[[], None] = lambda: None
    on_open_logs: Callable[[], None] = lambda: None


class TrayApp:
    """系统托盘图标 + 右键菜单。"""

    def __init__(self, config: Config, callbacks: TrayCallbacks,
                 theme: Optional[ThemeWatcher] = None,
                 throttle: float = UPDATE_THROTTLE):
        self.config = config
        self.cb = callbacks
        self.theme = theme or ThemeWatcher()

        self._lock = threading.RLock()
        self._views: List[ControllerView] = []
        self._alert_active = False
        self._alert_until = 0.0
        self._alert_percent: Optional[int] = None
        self._blink_thread: Optional[threading.Thread] = None
        self._blink_stop = threading.Event()
        self._last_update = 0.0
        self._autostart_detail = "未查询"

        # 刷新节流（可注入，便于测试）+ 尾部刷新定时器
        self._throttle = max(0.0, float(throttle))
        self._trailing: Optional[threading.Timer] = None

        # 上一张真正推送给系统托盘的图标；用于免掉状态未变化时的重复推送
        self._last_icon: Optional[Image.Image] = None
        self._icon_pushes = 0

        # 画笔按「尺寸 + 主题色 + 图标风格」缓存，任一变了才重建（见 _ensure_painter）
        self._painter: Optional[iconart.IconPainter] = None
        self._painter_size = 0
        self._painter_color = None
        self._painter_accent = None
        self._painter_style = None
        with self._lock:
            self._ensure_painter()

        # 用 hicon.ExactSizeIcon 而不是 pystray.Icon：
        # 后者会用 LR_DEFAULTSIZE 把图标按 32×32（SM_CXICON）加载，
        # 托盘再缩回 16×16（SM_CXSMICON），两次重采样把 1 像素宽的
        # 电池外框糊成灰边。详见 hicon 模块头部说明。
        self._icon = hicon.ExactSizeIcon(
            APP_NAME,
            self._painter.render(None, False, iconart.MODE_NONE),
            APP_FULL_NAME,
            menu=pystray.Menu(lambda: tuple(self._menu_items())),
            renderer=self.render_tray_icon,
        )
        hicon.override_is_active()      # 若 pystray 改名导致接管失效，这里会告警

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def run(self, setup: Optional[Callable[[], None]] = None) -> None:
        """**必须在主线程调用**（pystray 要求）。

        .. warning::
           pystray 的 ``run`` 有一处容易踩的坑：**只要你传了自定义 ``setup``，
           它就不再执行「把 ``visible`` 置为 True」的默认逻辑**，图标会安静地
           不出现。所以这里必须显式显示图标。
        """
        log.info("托盘图标启动")
        self._icon.run(setup=lambda icon: self._run_setup(setup))

    def _run_setup(self, setup: Optional[Callable[[], None]]) -> None:
        # setup 由 pystray 在新线程中调用（此时消息循环已就绪），
        # 正好用来启动后台检测。
        try:
            self._icon.visible = True
            log.info("托盘图标已加入通知区域")
        except Exception:
            log.exception("显示托盘图标失败")
        if setup is not None:
            try:
                setup()
            except Exception:
                log.exception("托盘启动回调执行失败")

    def stop(self) -> None:
        log.info("托盘图标退出")
        self._blink_stop.set()
        with self._lock:
            if self._trailing is not None:
                self._trailing.cancel()
                self._trailing = None
        try:
            self._icon.stop()
        except Exception:
            log.exception("停止托盘图标时发生异常")

    # ------------------------------------------------------------------
    # 状态刷新
    # ------------------------------------------------------------------
    def update(self, views: List[ControllerView]) -> None:
        """由检测层回调（任意线程）。

        .. important::
           **节流必须补一次「尾部刷新」**。检测层是**变化驱动**的：只有电量档位、
           充电状态或连接状态真的变化时才会回调。如果在节流窗口内把这次刷新直接
           丢掉，就再也不会有人来补救 —— 图标会永久停在旧状态。

           真机实测过这个 bug（2026-09-21 日志）：手柄接入瞬间，
           「检测到手柄（电量未知）」与「电量更新 percent=85」两条快照在
           **同一毫秒**先后到达（`22:55:56,639` 两条），后者落在 150ms 节流
           窗口内被丢弃，于是图标一直显示「电量未知」直到下次电量变化 ——
           用户看到的就是"明明读到了电量，图标却不显示"。
        """
        now = time.monotonic()
        with self._lock:
            self._views = list(views)
            wait = self._last_update + self._throttle - now
            if wait <= 0:
                self._last_update = now
            else:
                self._schedule_trailing(wait)
                return
        self._refresh()

    def _schedule_trailing(self, delay: float) -> None:
        """在节流窗口结束后补一次刷新（用的是**最新**的 views）。"""
        with self._lock:
            if self._trailing is not None and self._trailing.is_alive():
                return                      # 已排队；它执行时会取到最新状态
            timer = threading.Timer(delay, self._flush_trailing)
            timer.daemon = True
            self._trailing = timer
        timer.start()

    def _flush_trailing(self) -> None:
        with self._lock:
            self._trailing = None
            self._last_update = time.monotonic()
        self._refresh()

    def refresh_now(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        try:
            self._apply_icon()
            self._icon.title = iconart.tooltip_for(
                self.current_views(), detailed=not self.config.hide_tooltip_detail
            )
            self._icon.update_menu()
        except Exception:
            log.exception("刷新托盘界面失败（已忽略，下次刷新会重试）")

    def current_views(self) -> List[ControllerView]:
        with self._lock:
            return list(self._views)

    def render_tray_icon(self) -> Image.Image:
        """生成**当前 DPI 目标尺寸**的托盘图标。

        pystray 每次要新建/重建 HICON 时都会回调它（``hicon.ExactSizeIcon``），
        因此拿到的必然是当前 ``SM_CXSMICON`` 的尺寸；配合
        :func:`psbt.tray.hicon.create_hicon` 的零缩放编码，最终交给通知区域的
        就是「1:1 的物理像素」，不存在任何插值。

        尺寸每次都会重新读 ``SM_CXSMICON``，所以**在设置里改完缩放比例无需重启** ——
        下一次刷新（几秒内）就会自动换成新尺寸的图标。
        """
        with self._lock:
            painter = self._ensure_painter()
            views = list(self._views)
            alert_active = self._alert_active and time.monotonic() < self._alert_until
            alert_percent = self._alert_percent
        if alert_active and alert_percent is not None:
            # 闪烁的高亮相位：直接用告警图标（带感叹号/强调色）
            return painter.render(alert_percent, False, iconart.MODE_ALERT)
        return iconart.icon_for(views, painter)

    def _apply_icon(self) -> None:
        image = self.render_tray_icon()
        # 画笔按 (电量, 充电, 模式, 尺寸, 主题) 缓存，状态没变时会返回**同一个图像
        # 对象**，所以身份比较就能免掉一次无谓的 Shell_NotifyIcon。
        # 这也是「尾部刷新」和看门狗兜底刷新可以放心调用的前提。
        if image is self._last_icon:
            return
        self._last_icon = image
        self._icon.icon = image
        self._icon_pushes += 1

    def on_theme_changed(self) -> None:
        """任务栏明暗主题变化后重新生成图标（否则图标会看不见）。"""
        with self._lock:
            self._ensure_painter()          # 主题色变了会自动重建画笔
        self._refresh()
        log.info("检测到系统主题变化，已重新生成托盘图标")

    def _ensure_painter(self) -> iconart.IconPainter:
        """按当前 DPI + 任务栏主题 + 图标风格取得画笔（任一变化就重建）。

        **尺寸必须严格等于 ``SM_CXSMICON``**：交给 Windows 缩放会糊掉
        1 像素宽的电池外框，所以这里按尺寸重新绘制而不是缩放图片。
        """
        size = winapi.small_icon_size()
        color = self.theme.theme.icon_color
        accent = bool(self.config.icon_accent_color)
        style = self.config.icon_style

        if (self._painter is None or size != self._painter_size
                or color != self._painter_color
                or accent != self._painter_accent
                or style != self._painter_style):
            if self._painter is not None and size != self._painter_size:
                log.info("托盘图标尺寸随 DPI 变化：%dpx → %dpx，已按新尺寸重绘",
                         self._painter_size, size)
            self._painter = iconart.IconPainter(
                primary=color, accent=accent, size=size, style=style)
            self._painter_size = size
            self._painter_color = color
            self._painter_accent = accent
            self._painter_style = style
        return self._painter

    # ------------------------------------------------------------------
    # 通知（通道 3：气泡）
    # ------------------------------------------------------------------
    def notify(self, title: str, message: str) -> bool:
        try:
            self._icon.notify(str(message), str(title))
            return True
        except Exception as exc:
            log.debug("气泡通知发送失败：%s", exc)
            return False

    # ------------------------------------------------------------------
    # 通道 1：图标告警闪烁
    # ------------------------------------------------------------------
    def flash_alert(self, percent: int, seconds: int = 8) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._alert_active = True
            self._alert_percent = percent
            self._alert_until = time.monotonic() + seconds
        if self._blink_thread and self._blink_thread.is_alive():
            return
        self._blink_stop.clear()
        self._blink_thread = threading.Thread(
            target=self._blink_loop, name="psbt-blink", daemon=True
        )
        self._blink_thread.start()

    def _blink_loop(self) -> None:
        log.debug("告警图标开始闪烁")
        try:
            while not self._blink_stop.is_set():
                with self._lock:
                    if time.monotonic() >= self._alert_until:
                        self._alert_active = False
                        break
                    self._alert_active = not self._alert_active
                self._apply_icon()
                if self._blink_stop.wait(BLINK_INTERVAL):
                    break
        except Exception:
            log.exception("告警闪烁线程异常（已忽略）")
        finally:
            with self._lock:
                self._alert_active = False
            try:
                self._apply_icon()
            except Exception:
                pass
            log.debug("告警图标闪烁结束")

    def alert_running(self) -> bool:
        with self._lock:
            return self._alert_active and time.monotonic() < self._alert_until

    # ------------------------------------------------------------------
    # 菜单
    # ------------------------------------------------------------------
    def _menu_items(self):
        views = self.current_views()

        yield pystray.MenuItem(self._status_header(views), self._on_info_clicked, default=True)
        yield pystray.MenuItem("—— 已连接手柄 ——", None)
        if views:
            for view in views:
                text = view.menu_text()
                if view.note:
                    text += " ⚠"
                yield pystray.MenuItem(text, None)
        else:
            yield pystray.MenuItem("未检测到手柄", None)

        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("开机启动", self._toggle_autostart,
                               checked=lambda item: self.autostart_enabled())
        yield pystray.MenuItem("图标风格", pystray.Menu(*self._style_items()))
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("刷新状态并查看详情", self._on_info_clicked)
        yield pystray.MenuItem("打开日志目录", self._on_open_logs)
        yield pystray.MenuItem("关于", self._on_about)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("退出", self._on_quit)

    def _style_items(self):
        """「图标风格」子菜单：win10 直角 / win11 圆角，单选。"""
        for style in iconart.STYLES:
            yield pystray.MenuItem(
                iconart.STYLE_LABELS[style],
                self._style_action(style),
                checked=self._style_checked(style),
                radio=True,
            )

    def _style_action(self, style: str):
        def _action(icon=None, item=None):
            self._set_icon_style(style)
        return _action

    def _style_checked(self, style: str):
        def _checked(item):
            return self.config.icon_style == style
        return _checked

    def _set_icon_style(self, style: str) -> None:
        """切换图标风格：写回配置 + 立即重绘（无需重启）。"""
        if style == self.config.icon_style:
            return
        self.config.icon_style = style
        if not self.config.save():
            log.warning("图标风格写入配置文件失败，本次切换仅在内存生效")
        self._last_icon = None              # 强制推送新风格的图标
        with self._lock:
            self._ensure_painter()
        self._refresh()
        try:
            self._icon.update_menu()        # 让单选勾选状态立刻刷新
        except Exception:
            log.exception("刷新菜单失败（已忽略）")
        log.info("图标风格已切换为：%s", iconart.STYLE_LABELS.get(style, style))

    def _status_header(self, views: List[ControllerView]) -> str:
        if not views:
            return "PS 手柄电量监视器 —— 未检测到手柄"
        lowest = min((v for v in views if v.percent is not None),
                     key=lambda v: v.percent, default=None)
        if lowest is None:
            return "PS 手柄电量监视器 —— %d 台手柄，电量未知" % len(views)
        return "PS 手柄电量监视器 —— %s（最低）" % lowest.battery.percent_text

    # ------------------------------------------------------------------
    # 菜单动作
    # ------------------------------------------------------------------
    def _on_info_clicked(self, icon=None, item=None) -> None:
        try:
            self.cb.on_rescan()
        except Exception:
            log.exception("刷新状态失败")
        try:
            self.cb.on_show_info()
        except Exception:
            log.exception("显示详情失败")

    def _toggle_autostart(self, icon=None, item=None) -> None:
        target = not self.autostart_enabled()
        ok = autostart.set_enabled(target)
        if not ok:
            log.warning("开机启动设置失败（目标状态：%s）", target)
        else:
            self.config.autostart_registered = target
            self.config.save()
        self._refresh()

    def autostart_enabled(self) -> bool:
        enabled, detail = autostart.status_text()
        self._autostart_detail = detail
        return enabled

    def _on_open_logs(self, icon=None, item=None) -> None:
        try:
            self.cb.on_open_logs()
        except Exception:
            log.exception("打开日志目录失败")

    def _on_about(self, icon=None, item=None) -> None:
        try:
            self.cb.on_about()
        except Exception:
            log.exception("显示关于信息失败")

    def _on_quit(self, icon=None, item=None) -> None:
        log.info("用户选择了退出")
        try:
            self.cb.on_quit()
        except Exception:
            log.exception("退出回调异常")
        finally:
            self.stop()


def about_text(manager_available: bool, extra_lines: Optional[List[str]] = None) -> str:
    """「关于」对话框内容：版本、数据来源、精度限制、自启动状态。"""
    from .. import paths
    from ..controllers import backend, parsers

    enabled, detail = autostart.status_text()
    lines = [
        "%s %s" % (APP_FULL_NAME, APP_VERSION),
        "",
        "支持设备：PS5 DualSense / DualSense Edge、PS4 DualShock 4（USB 与蓝牙）",
        "电量来源：手柄 HID 输入报告（" + ("hidapi %s" % backend.library_version()
                                        if manager_available else "hidapi 不可用") + "）",
        "电量精度：约 10 档（每档≈10%），无法获得 1% 精度；",
        "          百分比为区间中点估算值，菜单同时给出「等级 n/10」。",
        "",
        "开机启动：%s" % detail,
        "配置目录：%s" % paths.data_dir(),
        "",
        "XInput 无法读取 PlayStation 手柄电量（XInput 结构里没有电池字段），",
        "因此本工具绕过 XInput，直接读取 HID 报告。",
    ]
    if extra_lines:
        lines.extend(["", *extra_lines])
    return "\n".join(lines)
