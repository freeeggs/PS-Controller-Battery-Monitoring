"""低电量提醒策略（纯逻辑，无 Windows 依赖，可单元测试）。

为什么阈值要按「区间」而不是「精确百分比」设计
--------------------------------------------
PS4/PS5 手柄的 HID 电量只有 0~10 共约 11 个等级（每级≈10%），
所以「低于 30%」在数据上等价于「等级 ≤ 2」（区间中点 25%）。
若按 1% 去重，就会出现在同一等级内反复触发的问题；本策略因此：

* 以**用户配置的阈值**（默认 30 / 20 / 10）为「台阶」；
* 每个台阶在一次放电周期里**只提醒一次**；
* 电量继续下降、跨入新的更低台阶时**再提醒一次**；
* 电量回升到最高阈值以上、或开始充电、或断开重连后，台阶状态**重置**，
  下次再跌下来可以重新提醒。

这样既保证「不会无限重复弹窗」，也保证「继续降低会再次提醒」。
阈值可在 ``config.json`` 里改（例如改成 40/20/5），策略自动适配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from ..controllers.models import ChargeState, ControllerView

DEFAULT_THRESHOLDS: Sequence[int] = (30, 20, 10)

# 每个阈值对应的提示语
_MESSAGES = {
    10: ("电量严重不足", "请立即连接 USB 充电，手柄随时可能断开连接。"),
    20: ("电量偏低", "建议尽快给手柄充电。"),
    30: ("电量提醒", "手柄电量已低于 30%，建议安排充电。"),
}


@dataclass(frozen=True)
class Alert:
    """一次低电量提醒。"""

    key: str                 # 手柄标识
    name: str                # 手柄名（DualSense / DualShock 4）
    percent: int             # 当前百分比（估算值）
    level: Optional[int]     # 原始等级
    threshold: int           # 本次触发的阈值台阶
    severity: str            # warn | critical
    title: str
    message: str

    def one_line(self) -> str:
        return "%s：%s（约 %d%%）" % (self.name, self.message, self.percent)


@dataclass
class _BatteryState:
    """单个手柄的提醒状态机。"""

    armed: Set[int] = field(default_factory=set)   # 已经提醒过的台阶
    last_percent: Optional[int] = None

    def clear(self) -> None:
        self.armed.clear()
        self.last_percent = None


class LowBatteryPolicy:
    """按阈值台阶去重的低电量提醒策略。"""

    def __init__(self, thresholds: Sequence[int] = DEFAULT_THRESHOLDS,
                 enabled: bool = True):
        values = sorted({int(t) for t in thresholds if 1 <= int(t) <= 99}, reverse=True)
        self.thresholds: List[int] = values or list(DEFAULT_THRESHOLDS)
        self.enabled = enabled
        self._states: Dict[str, _BatteryState] = {}

    # ------------------------------------------------------------------
    def evaluate(self, view: ControllerView) -> Optional[Alert]:
        """传入最新快照，返回需要发出的提醒（无则 ``None``）。"""
        if not self.enabled:
            return None

        state = self._states.setdefault(view.key, _BatteryState())

        if not view.connected:
            state.clear()
            return None

        battery = view.battery

        # 充电 / 已满：视为「恢复」，重置台阶，便于下次从满电开始重新计数
        if battery.state.is_charging:
            state.clear()
            return None

        percent = battery.percent
        if percent is None or battery.state == ChargeState.ERROR:
            # 电量未知或手柄报告异常：不猜、不提醒，避免误报
            state.last_percent = None
            return None

        state.last_percent = percent

        # 回到安全水平：重置，下一次跌下来可以重新提醒
        if percent >= self.thresholds[0]:
            state.armed.clear()
            return None

        crossed = [t for t in self.thresholds if percent < t]
        new_bands = [t for t in crossed if t not in state.armed]
        if not new_bands:
            return None

        # 一次性把本次跨过的所有台阶都标记为已提醒（避免连发多条）
        state.armed.update(crossed)

        threshold = min(new_bands)      # 最严重的那个新台阶
        title, body = _MESSAGES.get(
            threshold,
            ("电量提醒", "手柄电量已低于 %d%%，建议安排充电。" % threshold),
        )
        severity = "critical" if threshold <= min(self.thresholds) else "warn"
        return Alert(
            key=view.key,
            name=view.short_name,
            percent=percent,
            level=battery.level,
            threshold=threshold,
            severity=severity,
            title=title,
            message=body,
        )

    # ------------------------------------------------------------------
    def forget(self, key: str) -> None:
        """手柄断开时清掉状态（重连后按全新设备重新计数）。"""
        self._states.pop(key, None)

    def reset(self) -> None:
        self._states.clear()

    def armed_bands(self, key: str) -> Set[int]:
        return set(self._states.get(key, _BatteryState()).armed)
