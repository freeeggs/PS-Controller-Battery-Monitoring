"""手柄数据模型。

所有对外暴露的都是**不可变**对象：后台读取线程产生新实例，UI 线程只读，
从根上排除跨线程状态撕裂。可变状态（句柄、错误计数、已提醒等级）只存在于
``manager`` 内部。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Transport(str, Enum):
    """连接方式。来自 hidapi 的 ``bus_type``。"""

    USB = "usb"
    BLUETOOTH = "bluetooth"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            Transport.USB: "USB",
            Transport.BLUETOOTH: "蓝牙",
            Transport.UNKNOWN: "未知接口",
        }[self]

    @classmethod
    def from_bus_type(cls, bus_type: int) -> "Transport":
        # hidapi: 1 = HID_API_BUS_USB, 2 = HID_API_BUS_BLUETOOTH, 0 = unknown
        if bus_type == 1:
            return cls.USB
        if bus_type == 2:
            return cls.BLUETOOTH
        return cls.UNKNOWN


class ChargeState(str, Enum):
    """充电状态（含义对照 Linux 内核 hid-playstation 驱动）。"""

    DISCHARGING = "discharging"
    CHARGING = "charging"
    FULL = "full"
    ERROR = "error"        # 电压/温度异常、充电错误
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            ChargeState.DISCHARGING: "放电中",
            ChargeState.CHARGING: "充电中",
            ChargeState.FULL: "已充满",
            ChargeState.ERROR: "充电异常",
            ChargeState.UNKNOWN: "状态未知",
        }[self]

    @property
    def is_charging(self) -> bool:
        return self in (ChargeState.CHARGING, ChargeState.FULL)


# 手柄家族
FAMILY_DS4 = "dual-shock-4"
FAMILY_DUALSENSE = "dualsense"


@dataclass(frozen=True)
class BatteryReport:
    """一次解析出的电量信息。

    :param level: HID 报告里的**原始等级**（0~10），``None`` 表示 API 未提供。
        PS4/PS5 手柄只有约 11 级电量，这是硬件/协议层面的限制，不是本程序的
        解析疏漏，详见 ``parsers`` 模块顶部说明。
    :param percent: 由原始等级换算的百分比（区间中点，与 Linux 驱动一致）。
    :param state: 充电状态。
    :param source: 数据来源描述（写入日志，便于排查兼容性问题）。
    :param raw_byte: 原始电量字节，排障时可对照。
    """

    level: Optional[int] = None
    percent: Optional[int] = None
    state: ChargeState = ChargeState.UNKNOWN
    source: str = "unknown"
    raw_byte: Optional[int] = None

    @property
    def has_reading(self) -> bool:
        return self.level is not None or self.percent is not None

    @property
    def percent_text(self) -> str:
        """用于 UI 的百分比文本。刻意带「约」——精度只有 10%。"""
        if self.percent is None:
            return "电量未知"
        return "约 %d%%" % self.percent

    @property
    def level_text(self) -> str:
        if self.level is None:
            return "等级 ?/10"
        return "等级 %d/10" % self.level


UNKNOWN_BATTERY = BatteryReport()


@dataclass(frozen=True)
class ControllerView:
    """给上层（托盘菜单 / 通知策略）使用的只读快照。"""

    key: str                    # 稳定标识，跨重连保持一致
    family: str                 # FAMILY_DS4 / FAMILY_DUALSENSE
    display_name: str           # 例如 "DualSense (PS5)"
    transport: Transport
    vendor_id: int
    product_id: int
    serial: str = ""
    battery: BatteryReport = UNKNOWN_BATTERY
    connected: bool = True
    note: str = ""              # 例如「蓝牙精简报告，暂无法读取电量」

    # ---- 展示辅助 ----
    @property
    def short_name(self) -> str:
        return "DualSense" if self.family == FAMILY_DUALSENSE else "DualShock 4"

    @property
    def percent(self) -> Optional[int]:
        return self.battery.percent

    @property
    def level(self) -> Optional[int]:
        return self.battery.level

    def menu_text(self) -> str:
        """托盘菜单里的一行，例如 ``DualSense —— 约 85%（等级 8/10）``。"""
        name = self.short_name
        if not self.battery.has_reading:
            return "%s —— 电量未知（%s）" % (name, self.battery.state.label if self.battery.state != ChargeState.UNKNOWN else "等待数据")
        parts = ["%s —— %s" % (name, self.battery.percent_text)]
        extra = self.battery.level_text
        if self.battery.state.is_charging:
            extra += "，" + self.battery.state.label
        parts.append("（%s）" % extra)
        return "".join(parts)

    def tooltip_line(self) -> str:
        if not self.battery.has_reading:
            return "%s: 电量未知" % self.short_name
        text = "%s: %s" % (self.short_name, self.battery.percent_text)
        if self.battery.state.is_charging:
            text += " (%s)" % self.battery.state.label
        return text


@dataclass
class MutableController:
    """仅 manager 内部使用的可变记录。"""

    view: ControllerView
    candidate_paths: list = field(default_factory=list)
    path_index: int = 0
    last_report_ts: float = 0.0
    last_parse_ts: float = 0.0
    read_errors: int = 0
    total_reports: int = 0
    last_raw: bytes = b""
    tried_full_mode: bool = False
    opened_ts: float = 0.0

    @property
    def current_path(self) -> bytes:
        if not self.candidate_paths:
            return b""
        return self.candidate_paths[min(self.path_index, len(self.candidate_paths) - 1)]
