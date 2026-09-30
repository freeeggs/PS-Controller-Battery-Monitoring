# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（单文件、无控制台、带图标与版本信息）。

用法::

    pyinstaller --clean --noconfirm build/psbattery.spec

产物：``Release/PSBatteryTray.exe``（单文件，无需 Python 环境）。

要点说明
--------
* ``console=False``：托盘程序不能有控制台窗口；诊断模式（``--dump-hid``）
  会自己 ``AllocConsole``；
* 显式排除 tkinter（本机 Python 未提供，且本程序用自绘 Win32 弹窗代替）、
  numpy 等体积大又用不到的库；
* ``hidapi`` 的原生扩展 ``hid.cp313-win_amd64.pyd`` 由 PyInstaller 自动收集，
  这里再加一道 hiddenimports 保险；
* pystray 的 Windows 后端是运行时按平台选择的，静态分析看不到，必须显式声明；
* 通过 manifest 声明 PerMonitorV2 DPI 感知与 asInvoker 权限。
"""

import os

# 注意：spec 文件由 PyInstaller 用 exec() 执行，命名空间里**没有** __file__，
# 但 PyInstaller 会注入 SPECPATH（spec 文件所在目录）。早期版本写成
# os.path.abspath(__file__) 会直接 NameError: name '__file__' is not defined。
SPEC_DIR = os.path.abspath(SPECPATH)
ROOT = os.path.dirname(SPEC_DIR)

VERSION_FILE = os.path.join(SPEC_DIR, "version_info.txt")
MANIFEST = os.path.join(ROOT, "assets", "app.manifest")
ICON = os.path.join(ROOT, "assets", "app.ico")

block_cipher = None

hidden = [
    "hid",
    "pystray._win32",
    "pystray._util.win32",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "winreg",
    "winsound",
]

excludes = [
    "tkinter", "unittest", "pydoc", "doctest", "pdb", "test", "lib2to3",
    "numpy", "scipy", "pandas", "matplotlib", "setuptools", "pip",
    "pystray._gtk", "pystray._appindicator", "pystray._darwin", "pystray._xorg",
    "PIL.ImageQt", "PIL.ImageTk",
]

a = Analysis(
    [os.path.join(ROOT, "run.py")],
    pathex=[os.path.join(ROOT, "src"), ROOT],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="PSBatteryTray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # 不压缩：避免被杀软误报、也避免解压开销
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON if os.path.exists(ICON) else None,
    version=VERSION_FILE if os.path.exists(VERSION_FILE) else None,
    manifest=MANIFEST if os.path.exists(MANIFEST) else None,
)
