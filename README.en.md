# PS Controller Battery Monitor (PSBatteryTray)

[中文说明](README.md) · **English**

A tiny Windows tray utility that shows the **real battery level of PS4 (DualShock 4)
and PS5 (DualSense) controllers**. Runs in the notification area and warns you when
the charge gets low.

Single-file executable — **no install, no admin rights, no Python required.**

![icon states](docs/images/icon-states.png)

## Features

- **Real battery level** — the hardware reports 11 steps (10% each), so it says
  "about 75% (level 7/10)" instead of faking precision.
- **Multiple controllers** — the icon shows the one with the **lowest** charge.
- **Low-battery alerts** at 30% / 20% / 10%, sent over **several channels**
  (toast + icon blink + sound + topmost popup). A toast can be silently
  swallowed (Focus Assist, fullscreen games, presentation mode), so no single
  channel is trusted — the blink and the sound still fire even if the toast
  never appears.
- **Two icon styles** — Windows 10 (square) / Windows 11 (rounded), switchable
  from the right-click menu.
- **Follows your theme** — dark taskbar → white icon; theme changes are picked up
  within ~2 seconds.
- **Honest about failure** — shows a dash when the level can't be read rather than
  a stale number, and "unknown" for out-of-range values instead of a bogus 0%.
- Writes only to `%APPDATA%\PSBatteryTray`.

## Supported devices

| Device | USB | Bluetooth |
|---|---|---|
| DualShock 4 (PS4) | ✅ | ✅ |
| DualSense (PS5) | ✅ | not tested |

> Over Bluetooth a DualShock 4 may only send the short "minimal" report, which by
> protocol has no battery field. The app detects this, tells you, and tries to
> switch the controller to full-report mode — it never invents a number.

## Quick start

1. Download `PSBatteryTray.exe`
2. Run it — there is no main window, it goes straight to the tray
3. Optional: right-click → *Start with Windows*

If SmartScreen blocks it (the binary is unsigned), click *More info → Run anyway*.

## Usage

**Right-click** the tray icon for per-controller levels, autostart, icon style,
log folder and exit. **Left-click** for a details window.

## Command line

```bat
PSBatteryTray.exe --simulate       :: simulated timeline, no controller needed
PSBatteryTray.exe --dump-hid 30    :: dump raw HID reports for 30s (diagnostics)
PSBatteryTray.exe --reset-config   :: restore default config
PSBatteryTray.exe --version        :: print version
```

## Build from source

```bat
git clone https://github.com/freeeggs/PS-Controller-Battery-Monitoring
cd PS-Controller-Battery-Monitoring
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
build\build.ps1
```

Output: `Release\PSBatteryTray.exe`. Tests: `python tests\run_tests.py` (96 tests).

## Documentation

| Document | Contents |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | **Releases 1.4 → 1.8, Chinese/English pairs** |
| [docs/USAGE.md](docs/USAGE.md) | Detailed usage, full config reference, known issues, changelog (**Chinese**) |
| [docs/TECHNICAL_FEASIBILITY.md](docs/TECHNICAL_FEASIBILITY.md) | Protocol notes, byte-offset derivations, feasibility study (**Chinese**) |

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with Sony. "PlayStation", "DualShock" and "DualSense" are trademarks
of Sony Interactive Entertainment Inc.
