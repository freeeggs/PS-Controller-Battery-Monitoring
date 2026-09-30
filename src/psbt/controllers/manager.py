"""手柄状态管理器：设备枚举 + 每个手柄一个读取线程。

并发模型
--------
* **1 个扫描线程**：周期性调用 hidapi ``enumerate()``，这是「手柄是否还在线」
  的唯一权威判据 —— 插拔、重连都由它发现。
* **每个手柄 1 个读取线程**：阻塞在 ``hid_read_timeout`` 上（默认 1s），
  有报告就解析、没报告就超时返回，几乎不消耗 CPU；读失败则退避重试并轮换
  HID 集合路径（同一个手柄 Windows 会暴露多个集合，某些集合不可读）。

资源与稳定性
------------
* 读取线程与扫描线程全部 daemon 化，退出时统一 ``stop()`` + join，句柄显式关闭，
  不会因为反复插拔累积线程/句柄（hidapi 每个句柄自带一个 Windows 后台读线程）。
* 设备消失时立刻回收：停线程 → 关句柄 → 删除记录 → 推送新快照；
  同时清掉电量缓存，保证「未连接手柄时不会误显示上一次的电量」。
* 任何单设备异常都被限制在该设备的线程内（try/except + 退避），
  不会影响其它手柄，也不会让进程崩溃。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional

from ..config import Config
from ..logs import get_logger
from . import backend, parsers, registry
from .models import (
    BatteryReport,
    ChargeState,
    ControllerView,
    MutableController,
    Transport,
    UNKNOWN_BATTERY,
)

log = get_logger("manager")

MAX_TRACKED_DEVICES = 12        # 安全阀：异常情况下也不会无限开线程
OPEN_RETRY_DELAY = 3.0          # 所有集合都打不开时的重试间隔
READ_ERROR_BACKOFF = 1.0        # 读失败后的退避
FULL_MODE_DELAY = 3.0           # DS4 蓝牙精简报告 —— 多久后尝试切完整模式


@dataclass
class ManagerEvents:
    """上层订阅的回调集合（全部可选，回调在后台线程中被调用）。"""

    on_snapshot: Optional[Callable[[List[ControllerView]], None]] = None
    on_connected: Optional[Callable[[ControllerView], None]] = None
    on_disconnected: Optional[Callable[[ControllerView], None]] = None


class ControllerManager:
    """手柄检测与电量读取的总入口。"""

    def __init__(self, config: Config, events: Optional[ManagerEvents] = None):
        self.config = config
        self.events = events or ManagerEvents()

        self._lock = threading.RLock()
        self._records: Dict[str, MutableController] = {}
        self._readers: Dict[str, "_Reader"] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._scan_thread: Optional[threading.Thread] = None
        self._last_snapshot: List[ControllerView] = []

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> bool:
        if not backend.is_available():
            log.error("hidapi 不可用，无法检测手柄：%s", backend.import_error())
            return False
        if self._scan_thread and self._scan_thread.is_alive():
            return True
        self._stop.clear()
        self._scan_thread = threading.Thread(
            target=self._scan_loop, name="psbt-scan", daemon=True
        )
        self._scan_thread.start()
        log.info(
            "手柄检测已启动（hidapi %s，扫描间隔 %.1fs/%.1fs）",
            backend.library_version(),
            self.config.scan_interval_idle,
            self.config.scan_interval_active,
        )
        return True

    def stop(self, timeout: float = 4.0) -> None:
        log.info("正在停止手柄检测…")
        self._stop.set()
        self._wake.set()

        if self._scan_thread and self._scan_thread.is_alive():
            self._scan_thread.join(timeout=timeout)

        with self._lock:
            readers = list(self._readers.values())
            records = list(self._records.values())
            self._readers.clear()
            self._records.clear()

        # 先关句柄再 join：close() 会让阻塞中的 hid_read 立刻返回，避免白等超时
        for reader in readers:
            reader.shutdown()
        for reader in readers:
            reader.join(timeout=timeout)
            if reader.is_alive():
                log.warning("读取线程未能及时退出：%s", reader.name)
        del records
        log.info("手柄检测已停止")

    # ------------------------------------------------------------------
    # 对外查询
    # ------------------------------------------------------------------
    def snapshot(self) -> List[ControllerView]:
        """返回当前所有已连接手柄的不可变快照（按电量升序，最低的在前）。"""
        with self._lock:
            views = [rec.view for rec in self._records.values()]
        return sorted(views, key=_sort_key)

    def is_running(self) -> bool:
        return bool(self._scan_thread and self._scan_thread.is_alive())

    def rescan_now(self) -> None:
        """请求立即重新扫描（菜单里「重新扫描」用）。"""
        self._wake.set()

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception:
                log.exception("扫描手柄时发生异常（已忽略，将继续扫描）")

            with self._lock:
                has_device = bool(self._records)
            interval = (
                self.config.scan_interval_active if has_device else self.config.scan_interval_idle
            )
            self._wake.wait(interval)
            self._wake.clear()

    def _scan_once(self) -> None:
        found = backend.find_playstation_devices()
        present: Dict[str, backend.HidDeviceInfo] = {item.device_key: item for item in found}

        with self._lock:
            known = set(self._records.keys())
        current = set(present.keys())

        # --- 新增 / 重连 ---
        for key in sorted(current - known):
            if len(self._snapshot_keys()) >= MAX_TRACKED_DEVICES:
                log.warning("同时跟踪的手柄数量已达上限 %d，忽略 %s", MAX_TRACKED_DEVICES, key)
                continue
            self._add_device(present[key])

        # --- 拔出 ---
        for key in sorted(known - current):
            self._remove_device(key, reason="设备已从系统中消失")

        # --- 同一设备路径变化（换了 HID 集合/端口）→ 重启读取线程 ---
        with self._lock:
            changed = [
                key for key in (current & known)
                if self._records[key].view.connected
                and key in self._readers
                and self._readers[key].path != present[key].path
            ]
        for key in changed:
            log.info("设备 %s 的 HID 路径发生变化，重启读取线程", key)
            self._remove_device(key, reason="HID 路径变化", silent=True)
            self._add_device(present[key])

        with self._lock:
            log.debug("扫描完成：设备 %d 台，跟踪 %d 台，读取线程 %d 个",
                      len(present), len(self._records), len(self._readers))

    def _snapshot_keys(self) -> List[str]:
        with self._lock:
            return list(self._records.keys())

    # ------------------------------------------------------------------
    # 设备增删
    # ------------------------------------------------------------------
    def _add_device(self, info: backend.HidDeviceInfo) -> None:
        spec = registry.lookup(info.vendor_id, info.product_id)
        if spec is None:
            return

        candidates = backend.all_candidates_for(info.vendor_id, info.product_id, info.device_key)
        if not candidates:
            candidates = [info]

        view = ControllerView(
            key=info.device_key,
            family=spec.family,
            display_name=spec.name,
            transport=info.transport,
            vendor_id=info.vendor_id,
            product_id=info.product_id,
            serial=info.serial,
            battery=UNKNOWN_BATTERY if spec.supports_battery else BatteryReport(),
            connected=True,
            note="" if spec.supports_battery else "该型号不提供电量数据",
        )
        record = MutableController(view=view, candidate_paths=[c.path for c in candidates])
        record.opened_ts = time.monotonic()

        reader = _Reader(self, record)
        with self._lock:
            self._records[record.view.key] = record
            self._readers[record.view.key] = reader
        reader.start()

        log.info(
            "检测到手柄：%s [%s / %s] 集合候选 %d 个",
            view.display_name, view.transport.label, view.key, len(candidates),
        )
        self._emit(self.events.on_connected, view)
        self._publish()

    def _remove_device(self, key: str, reason: str = "", silent: bool = False) -> None:
        with self._lock:
            record = self._records.pop(key, None)
            reader = self._readers.pop(key, None)
        if reader is not None:
            reader.shutdown()
            reader.join(timeout=2.0)
        if record is None:
            return
        log.info("手柄已断开：%s（%s）", record.view.display_name, reason or "未知原因")
        if not silent:
            self._emit(self.events.on_disconnected, record.view)
        self._publish()

    # ------------------------------------------------------------------
    # 状态更新（由读取线程调用）
    # ------------------------------------------------------------------
    def _update_battery(self, key: str, battery: BatteryReport, raw: bytes,
                        note: str = "") -> None:
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            old = record.view.battery
            record.last_raw = raw
            record.view = replace(record.view, battery=battery, note=note or record.view.note)
            changed = (
                old.level != battery.level
                or old.percent != battery.percent
                or old.state != battery.state
            )
        if changed:
            log.info(
                "电量更新 %s：level=%s percent=%s state=%s source=%s raw=%s",
                key, battery.level, battery.percent, battery.state.value,
                battery.source, parsers.hex_dump(raw, 48),
            )
            self._publish()

    def _update_note(self, key: str, note: str) -> None:
        with self._lock:
            record = self._records.get(key)
            if record is None or record.view.note == note:
                return
            record.view = replace(record.view, note=note)
        self._publish()

    def _publish(self) -> None:
        views = self.snapshot()
        with self._lock:
            if _same_views(self._last_snapshot, views):
                return
            self._last_snapshot = views
        self._emit(self.events.on_snapshot, views)

    def _emit(self, callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            log.exception("上层回调执行失败（已忽略）")


class _Reader(threading.Thread):
    """单个手柄的读取线程。"""

    def __init__(self, manager: ControllerManager, record: MutableController):
        super().__init__(name="psbt-read-%s" % _short(record.view.key), daemon=True)
        self.manager = manager
        self.record = record
        self._stop = threading.Event()
        self.handle: Optional[backend.HidHandle] = None
        self.path: bytes = record.current_path
        self._min_gap = manager.config.report_min_gap
        self._timeout = manager.config.read_timeout_ms
        self._error_limit = manager.config.read_error_limit
        self._minimal_reports = 0

    # -- 生命周期 --
    def shutdown(self) -> None:
        self._stop.set()
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    # -- 主循环 --
    def run(self) -> None:
        log.debug("读取线程启动：%s", self.name)
        try:
            self._loop()
        except Exception:
            log.exception("读取线程异常退出：%s", self.name)
        finally:
            handle, self.handle = self.handle, None
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
            log.debug("读取线程结束：%s", self.name)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.handle is None or not self.handle.is_open:
                if not self._open():
                    self._stop.wait(OPEN_RETRY_DELAY)
                    continue

            try:
                data = self.handle.read(256, self._timeout)
            except OSError as exc:
                self._on_read_error(exc)
                continue
            except Exception:
                log.exception("读取 HID 时发生未知异常（%s）", self.name)
                self._stop.wait(READ_ERROR_BACKOFF)
                continue

            if not data:
                self._check_stale_battery()
                continue

            self.record.read_errors = 0
            self.record.total_reports += 1
            self.record.last_report_ts = time.monotonic()

            now = time.monotonic()
            if (now - self.record.last_parse_ts) >= self._min_gap:
                self.record.last_parse_ts = now
                self._parse(data)
            else:
                # 手柄（尤其蓝牙）报告速率可达 250Hz，节流到 ~4Hz 足够，
                # 又不至于让 Python 层空转。
                self._stop.wait(self._min_gap)

    # -- HID 打开 / 轮换集合 --
    def _open(self) -> bool:
        candidates = self.record.candidate_paths or [self.path]
        count = len(candidates)
        for offset in range(count):
            index = (self.record.path_index + offset) % count
            path = candidates[index]
            info = backend.HidDeviceInfo(
                path=path,
                vendor_id=self.record.view.vendor_id,
                product_id=self.record.view.product_id,
                product_string=self.record.view.display_name,
                serial=self.record.view.serial,
                bus_type=1 if self.record.view.transport == Transport.USB else (
                    2 if self.record.view.transport == Transport.BLUETOOTH else 0
                ),
            )
            handle = backend.HidHandle(info)
            if handle.open():
                self.handle = handle
                self.path = path
                self.record.path_index = index
                self.record.view = replace(self.record.view, transport=info.transport)
                return True
            log.debug("HID 集合候选 %d/%d 打开失败：%s", offset + 1, count, _short(path))
        self.manager._update_note(  # noqa: SLF001 - 同包内协作
            self.record.view.key, "无法打开 HID 设备（可能被其它程序独占）"
        )
        log.warning("手柄 %s 的所有 HID 集合都打不开，%0.1fs 后重试",
                    self.record.view.display_name, OPEN_RETRY_DELAY)
        return False

    def _on_read_error(self, exc: OSError) -> None:
        self.record.read_errors += 1
        log.debug("读取失败 %d/%d（%s）：%s",
                  self.record.read_errors, self._error_limit, self.name, exc)
        if self.record.read_errors >= self._error_limit:
            log.info("手柄 %s 的当前 HID 集合连续 %d 次不可读，尝试下一个集合",
                     self.record.view.display_name, self.record.read_errors)
            self.record.read_errors = 0
            self.record.path_index += 1
            handle, self.handle = self.handle, None
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        else:
            self._stop.wait(READ_ERROR_BACKOFF)

    # -- 解析 --
    def _parse(self, data: bytes) -> None:
        view = self.record.view
        try:
            report = parsers.parse_report(view.family, view.transport, data)
        except Exception:
            log.exception("解析报告失败（已忽略本条报告）family=%s len=%d",
                          view.family, len(data))
            return

        if report is None:
            log.debug("无法识别的报告：id=0x%02X len=%d data=%s",
                      data[0] if data else -1, len(data), parsers.hex_dump(data, 24))
            return

        if report.source.startswith(parsers.SRC_DS4_BT_MINIMAL):
            self._handle_minimal_report()
            return

        self._minimal_reports = 0
        self.manager._update_battery(  # noqa: SLF001 - 同包内协作
            view.key, report, data, note=""
        )

    def _handle_minimal_report(self) -> None:
        """DS4 蓝牙精简报告：没有电量字段，尝试让手柄切到完整报告模式。"""
        self._minimal_reports += 1
        view = self.record.view
        if not self.record.tried_full_mode:
            self.record.tried_full_mode = True
            log.info("手柄 %s 正在使用蓝牙精简报告（10 字节），"
                     "尝试请求完整报告模式…", view.display_name)
            if self.handle is not None:
                backend.request_ds4_full_report_mode(self.handle, view.transport)

        if self._minimal_reports in (1, 20, 200):
            self.manager._update_note(  # noqa: SLF001
                view.key,
                "蓝牙精简报告模式，无法读取电量；建议改用 USB 或手柄附带的无线适配器",
            )
        if self._minimal_reports == 20:
            log.warning(
                "手柄 %s 连续收到 %d 条蓝牙精简报告，仍未获得电量数据。"
                "该模式下协议不携带电量字段。",
                view.display_name, self._minimal_reports,
            )

    def _check_stale_battery(self) -> None:
        """长时间收不到任何报告时提示用户，但不据此判定拔出（由枚举决定）。"""
        if self.record.last_report_ts == 0.0:
            return
        idle = time.monotonic() - self.record.last_report_ts
        if idle > 30.0 and not self.record.view.note:
            self.manager._update_note(  # noqa: SLF001
                self.record.view.key, "长时间未收到数据（可能已休眠或断开）"
            )


# ---------------------------------------------------------------------------
def _sort_key(view: ControllerView):
    """排序：有读数的在前、电量低的在前，保证菜单顺序稳定。"""
    percent = view.percent
    return (0 if percent is not None else 1, percent if percent is not None else 999,
            view.display_name)


def _same_views(a: List[ControllerView], b: List[ControllerView]) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if (x.key, x.battery.level, x.battery.percent, x.battery.state, x.note,
                x.connected) != (y.key, y.battery.level, y.battery.percent,
                                 y.battery.state, y.note, y.connected):
            return False
    return True


def _short(key: str) -> str:
    return key.split("#")[-1][:8] if "#" in key else key[:8]


def lowest_battery_view(views: List[ControllerView]) -> Optional[ControllerView]:
    """托盘图标要显示的那台手柄：**电量最低**的那台。"""
    known = [v for v in views if v.percent is not None]
    if not known:
        return views[0] if views else None
    return min(known, key=lambda v: (v.percent, v.display_name))


def charge_state_of(views: List[ControllerView]) -> ChargeState:
    if not views:
        return ChargeState.UNKNOWN
    return lowest_battery_view(views).battery.state
