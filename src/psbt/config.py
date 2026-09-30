"""用户配置：``%APPDATA%\\PSBatteryTray\\config.json``。

设计取舍
--------
* 只有「行为开关」放进配置文件，硬件相关参数（报告偏移等）写死在代码里，
  避免用户误改导致读数错误；
* 配置损坏时不让程序崩溃，回落到默认值并把损坏文件重命名保留；
* 写入使用「临时文件 + 替换」，避免掉电/NAS 环境写出半截 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List

from .logs import get_logger
from .paths import config_path

log = get_logger("config")

# 低电量提醒阈值（百分比，降序）。10% 粒度下分别对应「等级 2 / 1 / 0」。
DEFAULT_THRESHOLDS = [30, 20, 10]


@dataclass
class Config:
    # ---- 扫描 / 轮询 ----
    scan_interval_idle: float = 5.0      # 无手柄时的设备枚举间隔（秒）
    scan_interval_active: float = 3.0    # 有手柄时的设备枚举间隔（秒）
    report_min_gap: float = 0.25         # 同一手柄两次解析的最小间隔（秒）,节流上限 4Hz
    read_timeout_ms: int = 1000          # 单次 HID 读超时
    read_error_limit: int = 5            # 连续读失败多少次后判定为不可读

    # ---- 低电量提醒 ----
    enabled_alerts: bool = True
    low_battery_thresholds: List[int] = field(default_factory=lambda: list(DEFAULT_THRESHOLDS))
    alert_sound: bool = True
    alert_toast: bool = True
    alert_popup: str = "auto"            # auto | always | never
    alert_popup_seconds: int = 10
    alert_blink_seconds: int = 8
    alert_min_gap_seconds: float = 2.0   # 多个手柄同时低电量时，提醒之间的最小间隔

    # ---- 外观 ----
    icon_accent_color: bool = True       # 告警图标是否使用琥珀/红色强调（否则纯单色）
    icon_style: str = "win10"            # 图标风格：win10（直角）/ win11（圆角）
    hide_tooltip_detail: bool = False    # 悬停提示是否隐藏「等级 n/10」

    # ---- 其它 ----
    log_level: str = "INFO"
    autostart_registered: bool = False   # 仅作记录，注册表才是权威来源

    # ------------------------------------------------------------------
    def normalized(self) -> "Config":
        """修正非法值，保证后续代码不必到处做防御。"""
        self.scan_interval_idle = _clamp_float(self.scan_interval_idle, 1.0, 120.0, 5.0)
        self.scan_interval_active = _clamp_float(self.scan_interval_active, 1.0, 120.0, 3.0)
        self.report_min_gap = _clamp_float(self.report_min_gap, 0.05, 5.0, 0.25)
        self.read_timeout_ms = int(_clamp_float(self.read_timeout_ms, 100, 10000, 1000))
        self.read_error_limit = int(_clamp_float(self.read_error_limit, 1, 100, 5))

        thresholds = []
        for v in self.low_battery_thresholds or []:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if 1 <= iv <= 99:
                thresholds.append(iv)
        if not thresholds:
            thresholds = list(DEFAULT_THRESHOLDS)
        self.low_battery_thresholds = sorted(set(thresholds), reverse=True)

        if self.alert_popup not in ("auto", "always", "never"):
            self.alert_popup = "auto"
        self.alert_popup_seconds = int(_clamp_float(self.alert_popup_seconds, 3, 120, 10))
        self.alert_blink_seconds = int(_clamp_float(self.alert_blink_seconds, 0, 120, 8))
        self.alert_min_gap_seconds = _clamp_float(self.alert_min_gap_seconds, 0.0, 60.0, 2.0)

        from .tray.iconart import STYLES, STYLE_WIN10

        if str(self.icon_style) not in STYLES:
            self.icon_style = STYLE_WIN10

        self.log_level = str(self.log_level or "INFO").upper()
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            self.log_level = "INFO"
        return self

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or config_path()
        cfg = cls()
        if not os.path.exists(path):
            log.info("未找到配置文件，使用默认配置：%s", path)
            return cfg.normalized()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("配置根节点必须是对象")
            known = {f.name for f in fields(cls)}
            for k, v in raw.items():
                if k in known:
                    setattr(cfg, k, v)
                else:
                    log.debug("忽略未知配置项 %s", k)
        except Exception as exc:
            log.warning("配置文件解析失败（%s），已改用默认配置", exc)
            _backup_broken(path)
        return cfg.normalized()

    def save(self, path: str | None = None) -> bool:
        path = path or config_path()
        try:
            data = asdict(self.normalized())
            directory = os.path.dirname(path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="config.", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return True
        except Exception as exc:
            log.warning("配置保存失败：%s", exc)
            return False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _backup_broken(path: str) -> None:
    try:
        os.replace(path, path + ".broken")
        log.info("损坏的配置文件已重命名为 %s.broken", path)
    except Exception:
        pass


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(low, min(high, v))
