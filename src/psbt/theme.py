"""明暗主题探测。

托盘图标是单色位图，浅色任务栏上必须画深色图标、深色任务栏上必须画浅色
图标，否则会看不见。Windows 10 的两个设置互相独立：

* ``SystemUsesLightTheme``  —— 任务栏 / 系统 UI（决定托盘图标颜色）
* ``AppsUseLightTheme``      —— 应用与通知（决定弹窗配色）

而且这两个值**运行时可以随时被用户改**（设置 → 个性化 → 颜色），
所以本模块提供缓存 + 定期复查，主题变化时由 app 重新生成图标。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import winapi

_CHECK_INTERVAL = 1.0  # 秒，轮询注册表的最小间隔
#
# 为什么是 1 秒而不是 30 秒：用户在「设置 → 个性化 → 颜色」里切换亮/暗之后，
# 会**立刻**盯着托盘看。30 秒的探测间隔意味着最长要等半分钟图标才变色，
# 体验上等同于"不跟随"（实测就踩过这个坑：日志里 20:37:02 才报出主题变化）。
# 读一个 HKCU 下的 DWORD 开销是微秒级，1 Hz 轮询完全不值得优化掉。


@dataclass
class Theme:
    taskbar_light: bool = False
    apps_light: bool = False

    @property
    def icon_color(self) -> tuple:
        """托盘图标主色：浅色任务栏用深色图标，反之用白色。"""
        return (26, 26, 26, 255) if self.taskbar_light else (255, 255, 255, 255)


class ThemeWatcher:
    """带缓存的主题监视器（避免每次刷新图标都读注册表）。"""

    def __init__(self) -> None:
        self._theme = Theme()
        self._last_check = 0.0
        self.refresh(force=True)

    @property
    def theme(self) -> Theme:
        return self._theme

    def refresh(self, force: bool = False) -> bool:
        """必要时重新读取注册表；返回 True 表示主题发生了变化。"""
        now = time.monotonic()
        if not force and (now - self._last_check) < _CHECK_INTERVAL:
            return False
        self._last_check = now

        new = Theme(
            taskbar_light=winapi.appdata_theme("SystemUsesLightTheme"),
            apps_light=winapi.appdata_theme("AppsUseLightTheme"),
        )
        if new != self._theme:
            self._theme = new
            return True
        return False
