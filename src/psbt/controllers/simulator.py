"""模拟手柄（无实机时的端到端验证手段）。

用途
----
* 在没有 PS4/PS5 手柄的机器上验证托盘菜单、图标刷新、低电量提醒去重、
  断开/重连、多手柄「最低电量优先」等全部逻辑；
* 作为回归测试：脚本化时间线是确定性的，每次结果可复现。

通过 ``PSBatteryTray.exe --simulate``（或 ``--simulate=quick``）启用，
与真实检测层**互斥**：模拟模式下不会打开任何 HID 设备。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional, Tuple

from ..config import Config
from ..logs import get_logger
from .manager import ManagerEvents, _same_views, _sort_key  # noqa: PLC2701 - 同包协作
from .models import (
    FAMILY_DS4,
    FAMILY_DUALSENSE,
    BatteryReport,
    ChargeState,
    ControllerView,
    Transport,
)
from .parsers import level_to_percent

log = get_logger("simulator")

# 时间线动作
Action = Tuple[float, Optional[tuple]]


def _levels_to_percent(level: int) -> int:
    return level_to_percent(level)


def demo_timeline(scale: float = 1.0) -> List[Action]:
    """完整演示时间线，覆盖验收清单中的每一项。"""
    raw = [
        (5, ("connect", "dualsense", 9)),
        (10, ("set", "dualsense", 8)),
        (15, ("set", "dualsense", 7)),
        (20, ("set", "dualsense", 6)),
        (25, ("connect", "ds4", 5)),
        (30, ("set", "dualsense", 5)),
        (35, ("set", "dualsense", 4)),
        (40, ("set", "dualsense", 3)),
        (45, ("set", "dualsense", 2)),    # < 30% → 提醒 1
        (52, ("set", "dualsense", 2)),    # 同一等级 → 不应重复提醒
        (60, ("set", "dualsense", 1)),    # < 20% → 提醒 2
        (68, ("set", "dualsense", 1)),    # 同一等级 → 不应重复提醒
        (74, ("set", "dualsense", 0)),    # < 10% → 提醒 3
        (82, ("charge", "dualsense", 1)),  # 充电 → 状态重置
        (90, ("disconnect", "dualsense")),  # 断开 → 从菜单移除
        (96, ("connect", "dualsense", 2)),  # 重连 → 应重新提醒
        (104, ("set", "ds4", 1)),         # 另一台手柄也进入低电量
        (112, ("set", "dualsense", 9)),   # 恢复 → 重置
        (118, ("disconnect", "ds4")),
        (122, ("set", "dualsense", 2)),   # 再次跌破 → 再次提醒
        (128, ("disconnect", "dualsense")),  # 全部断开 → 托盘回到「无手柄」
        (132, None),
    ]
    return [(t * scale, a) for t, a in raw]


SCENARIOS = {"demo": demo_timeline, "quick": lambda: demo_timeline(0.25)}


class SimulatedManager:
    """与 :class:`ControllerManager` 接口一致的模拟实现。"""

    def __init__(self, config: Config, events: Optional[ManagerEvents] = None,
                 timeline: Optional[List[Action]] = None):
        self.config = config
        self.events = events or ManagerEvents()
        self._timeline = timeline if timeline is not None else demo_timeline()
        self._lock = threading.RLock()
        self._views: List[ControllerView] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_time = 0.0
        self._index = 0
        self._counter = 0

    # ---- 与真实管理器一致的接口 ----
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._start_time = time.monotonic()
        self._index = 0
        self._thread = threading.Thread(target=self._loop, name="psbt-sim", daemon=True)
        self._thread.start()
        log.warning("已进入【模拟模式】：不会读取真实手柄，时间线共 %d 步",
                    len(self._timeline))
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        log.info("模拟模式已停止")

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self) -> List[ControllerView]:
        with self._lock:
            return list(self._views)

    def rescan_now(self) -> None:
        """手动推进模拟时间线一步（测试与人工演练用）。"""
        self._apply_next_action(force=True)

    # ---- 内部 ----
    def _loop(self) -> None:
        while not self._stop.is_set():
            self._apply_next_action()
            self._stop.wait(0.5)

    def _elapsed(self) -> float:
        return time.monotonic() - self._start_time

    def _apply_next_action(self, force: bool = False) -> None:
        """处理到期的动作。

        ``force=True`` 时只推进一步（便于测试逐步驱动），
        正常定时循环则把当前所有到期动作一次处理完。
        """
        now = self._elapsed()
        changed = False
        while self._index < len(self._timeline):
            at, action = self._timeline[self._index]
            if not force and at > now:
                break
            self._index += 1
            if action is not None:
                changed |= self._do_action(action)
            if force:
                break
        if changed:
            self._publish()

    def _do_action(self, action: tuple) -> bool:
        kind = action[0]
        if kind == "connect":
            _, family, level = action
            return self._connect(family, level)
        if kind == "set":
            _, family, level = action
            return self._set(family, level)
        if kind == "charge":
            _, family, level = action
            return self._set(family, level, charging=True)
        if kind == "disconnect":
            return self._disconnect(action[1])
        return False

    def _key(self, family: str) -> str:
        suffix = "sim-dualsense" if family == FAMILY_DUALSENSE else "sim-ds4"
        return "054C:%s#SIM%s" % ("0CE6" if family == FAMILY_DUALSENSE else "09CC", suffix)

    def _connect(self, family: str, level: int) -> bool:
        key = self._key(family)
        with self._lock:
            if any(v.key == key for v in self._views):
                return False
            duplicate_index = sum(1 for v in self._views if v.family == family)
            view = ControllerView(
                key=key,
                family=family,
                display_name=("DualSense (PS5)" if family == FAMILY_DUALSENSE
                              else "DualShock 4 (CUH-ZCT2)"),
                transport=(Transport.BLUETOOTH if family == FAMILY_DUALSENSE
                           else Transport.USB),
                vendor_id=0x054C,
                product_id=0x0CE6 if family == FAMILY_DUALSENSE else 0x09CC,
                serial="SIM-%02d" % self._counter,
                battery=BatteryReport(level, _levels_to_percent(level),
                                      ChargeState.DISCHARGING, "simulator"),
                connected=True,
            )
            self._views.append(view)
            self._views.sort(key=_sort_key)
        self._counter += 1
        del duplicate_index
        log.info("【模拟】手柄已连接：%s 等级 %d", view.display_name, level)
        self._emit(self.events.on_connected, view)
        return True

    def _set(self, family: str, level: int, charging: bool = False) -> bool:
        key = self._key(family)
        with self._lock:
            for index, view in enumerate(self._views):
                if view.key != key:
                    continue
                state = (ChargeState.CHARGING if charging and level < 10
                         else ChargeState.FULL if charging else ChargeState.DISCHARGING)
                battery = BatteryReport(level, _levels_to_percent(level), state, "simulator")
                if (view.battery.level, view.battery.state) == (battery.level, battery.state):
                    return False
                from dataclasses import replace

                self._views[index] = replace(view, battery=battery)
                self._views.sort(key=_sort_key)
                log.info("【模拟】%s 电量 → 等级 %d（约 %d%%，%s）",
                         view.display_name, level, battery.percent or 0, state.label)
                return True
        return False

    def _disconnect(self, family: str) -> bool:
        key = self._key(family)
        with self._lock:
            for index, view in enumerate(self._views):
                if view.key == key:
                    self._views.pop(index)
                    log.info("【模拟】手柄已断开：%s", view.display_name)
                    self._emit(self.events.on_disconnected, view)
                    return True
        return False

    def _publish(self) -> None:
        views = self.snapshot()
        self._emit(self.events.on_snapshot, views)

    def _emit(self, callback: Optional[Callable], *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            log.exception("模拟器回调执行失败")


__all__ = ["SimulatedManager", "demo_timeline", "SCENARIOS", "_same_views"]
