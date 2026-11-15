# 更新记录 / Changelog

本文件记录 **1.4 → 1.9** 的变更。逐条中英对照，完整细节（含真机字节证据）
见 [`docs/USAGE.md` 版本历史](docs/USAGE.md#版本历史)。

This file covers releases **1.4 → 1.9**, in Chinese/English pairs. Full details
(including raw on-device byte evidence) are in
[`docs/USAGE.md`](docs/USAGE.md#版本历史).

> 本阶段的主线是一条：**干掉各种"假电量"**。所有修复的判据都来自真机日志里的
> 原始字节，而不是协议文档 —— 其中一条文档（Linux 内核）的映射被真机推翻。
>
> The theme of this stretch: **eliminating false battery readings.** Every fix is
> backed by raw bytes captured on real hardware rather than protocol docs — one
> doc (the Linux kernel) turned out to be wrong on real hardware.

---

## 1.9 — 2026-11-15

**中文**：删掉左键的详情悬浮窗（与悬停提示重复）；新增连接/断开后的"稳定等待"，
挡住偶发的假电量。

**English**: Removed the left-click details window (redundant with the tooltip);
added a settle window after connect/disconnect to block occasional false readings.

- 左键单击**只刷新**，不再弹窗；菜单「刷新状态并查看详情」→「刷新状态」。
  *Left-click now only refreshes, no window; menu item renamed to "Refresh".*
- 新增 `settle_seconds`（默认 5 秒）：刚连接/重连的头几秒不采信电量读数，
  之后正常显示；`0` 关闭。
  *New `settle_seconds` (default 5 s): battery readings are ignored for a few seconds after connect/reconnect; set `0` to disable.*
- 单元测试 156 → **162**。
  *Unit tests: 156 → 162.*

## 1.8 — 2026-10-05

**中文**：DS4 插上 USB 后电量误报 100%（内核把"插线时低 4 位 11"定义为"已充满"，
真机证明那只是"充电中"标记）。

**English**: Fixed the DualShock 4 falsely reporting 100% once the USB cable is
plugged in — the kernel defines "low nibble 11 while the cable is connected" as
*battery full*, but real hardware shows it only means *charging*.

- 插线 + 低 4 位 `11` → 只声明"充电中"，不再报 100%；未插线的 `11` 及以上也不再当 100%。
  *Cable connected + low nibble `11` → reports "charging" only, no longer 100%; the same value without a cable is no longer 100% either.*
- 充电中不带等级的帧沿用上一次真实等级，界面不再每秒跳几百次。
  *Charging frames that carry no level keep the last real reading, instead of flickering hundreds of times per second.*
- 两条链路同时在线时按型号选链路：**DS4 走蓝牙、DualSense 走 USB**（另一条兜底）。
  *With both links up, the link is chosen per model: Bluetooth for the DS4, USB for the DualSense (the other stays as fallback).*
- 电量字节一变就立刻解析，避免漏掉罕见的真值帧（DS4 充电时真等级间隔几十秒、只持续几毫秒）。
  *A changed battery byte triggers an immediate parse, so rare real-value frames aren't missed.*
- MAC 采集改为按**物理设备**缓存，避免"主集合读到、触控板集合没读到"导致同一手柄算出两个 key。
  *MAC lookup is now cached per physical device, so one controller can't end up with two keys.*
- 单元测试 142 → **156**。
  *Unit tests: 142 → 156.*

## 1.7 — 2026-10-05

**中文**：手柄蓝牙连着时插上 USB 线，同一只手柄不再显示成两条。

**English**: Plugging in the USB cable while a controller is connected over
Bluetooth no longer shows the same controller twice.

- 用**手柄自己的 MAC** 作跨总线标识：蓝牙取设备唯一串，USB 读配对信息特性报告
  （DualSense `0x09` / DualShock 4 `0x12`，MAC 小端序）。
  *Identifies a controller across transports by its own MAC: the device unique string over Bluetooth, the pairing-info feature report over USB (DualSense `0x09` / DualShock 4 `0x12`, MAC stored little-endian).*
- 候选顺序确定化，避免两条链路都在线时反复重启读取线程。
  *Deterministic candidate ordering, so the reader isn't restarted repeatedly while both links are up.*
- 单元测试 133 → **142**。
  *Unit tests: 133 → 142.*

## 1.6 — 2026-10-05

**中文**：修掉蓝牙接入 / USB 断开瞬间的"误报 5%"，并按你的要求精简设备支持。

**English**: Fixed the false "5%" reading on Bluetooth connect and on
disconnect, and trimmed device support as requested.

- 两道闸：**蓝牙下的 `0x01` 一律不算完整报告**；**报告体为空（下标 10 起全 0）的
  `0x01` 在任何总线上都不可信**。
  *Two guards: a `0x01` report over Bluetooth is never a full report; a `0x01` whose body is empty (all zero from index 10) is untrusted on any bus.*
- 只保留 PS4 / PS5 手柄本体：移除 DualShock 3 (`0x0268`)、PS Move (`0x03D5`)、
  官方无线适配器 CUH-ZWA1 (`0x0BA0`)，连带删掉"识别但不读电量"那套分支。
  *Only PS4/PS5 controllers are supported now: DualShock 3 (`0x0268`), PS Move (`0x03D5`) and the CUH-ZWA1 wireless adapter (`0x0BA0`) were removed, along with the "recognise but don't read" code path.*
- 单元测试 125 → **133**。
  *Unit tests: 125 → 133.*

## 1.5 — 2026-10-05

**中文**：USB 适配器 / 复合设备下，一个手柄被显示成好几台（一台有电量 +
几台"电量未知"）。

**English**: Under a USB adapter or composite device, one controller appeared as
several entries (one with a battery, the rest "unknown").

- 分组键改为**父 USB 设备的实例 ID**（一个手柄的多个 HID 集合归成一台）。
  *The grouping key is now the parent USB device instance ID, merging the several HID collections of one controller.*
- 过滤掉触控板附带的键盘 / 鼠标 / 消费类集合（用黑名单，宁可放过不错杀）。
  *Touchpad keyboard/mouse/consumer collections are filtered out (via a denylist, preferring to keep a doubtful one over killing a real controller).*
- 向上找父设备时只允许停在 USB 枚举器内，避免蓝牙手柄被并到**蓝牙适配器**上。
  *The parent walk stops inside the USB enumerator, so Bluetooth controllers can't be merged into the Bluetooth adapter.*
- 单元测试 115 → **125**。
  *Unit tests: 115 → 125.*

## 1.4 — 2026-10-01

**中文**：针对一份仓库级 code review 的集中修复 —— 8 条意见逐条核实后修掉 7 条，
并额外发现 1 条 review 未提到的崩溃路径。

**English**: Focused fixes after a repository-wide code review — 7 of the 8
points were confirmed and fixed, plus one crash path the review had missed.

- 测试会改**用户真实的开机启动项**（还原时写入的是当前构建的命令而非原值）。
  *Tests used to modify the user's real autostart entry (restoring it wrote the current build's command rather than the original value).*
- 枚举失败被误判成"所有手柄同时拔出" → 枚举改为三态（失败 / 空 / 有设备）。
  *An enumeration failure was mistaken for "every controller unplugged" → enumeration is now three-state (failed / empty / devices).*
- 超过 30 秒收不到报告仍显示旧电量 → 超时后读数切为**未知**。
  *Stale readings persisted past 30 seconds → the reading now becomes unknown on timeout.*
- 警告文字无法被清除（空串被当成"保留旧提示"）。
  *A warning note could never be cleared (an empty string was treated as "keep the old note").*
- 更正 `QUNS_QUIET_TIME` 的语义：它是"首次登录后第一个小时"，**不是专注助手**；
  `SHQueryUserNotificationState` 检测不到专注助手。
  *Corrected the semantics of `QUNS_QUIET_TIME`: it means "first hour after logon", not Focus Assist; `SHQueryUserNotificationState` cannot detect Focus Assist.*
- 所有 HID 集合都打不开时读取线程直接崩溃（`bytes` 路径被判成 `str`）。
  *The reader thread crashed when every HID collection failed to open (a `bytes` path was treated as `str`).*
- 单元测试 96 → **115**。
  *Unit tests: 96 → 115.*

---

## 统计 / Stats

| 版本 Version | 日期 Date | 单元测试 Unit tests |
|---|---|---|
| 1.4 | 2026-10-01 | 115 |
| 1.5 | 2026-10-05 | 125 |
| 1.6 | 2026-10-05 | 133 |
| 1.7 | 2026-10-05 | 142 |
| 1.8 | 2026-10-05 | 156 |
