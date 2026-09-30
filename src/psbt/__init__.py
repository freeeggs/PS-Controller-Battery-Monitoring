"""PSBatteryTray —— Windows 10 下 PS4 / PS5 手柄电量托盘监控工具。

包结构::

    psbt/
      version.py            版本与产品信息
      paths.py              %APPDATA% 目录解析
      logs.py               日志初始化（滚动文件 + 可选控制台）
      config.py             用户配置持久化
      winapi.py             少量 Win32 API 封装（ctypes，无第三方依赖）
      autostart.py          HKCU\\...\\Run 开机启动
      theme.py              任务栏 / 弹窗明暗主题探测
      controllers/          手柄检测与电量读取（与 UI 完全解耦）
      tray/                 pystray 托盘图标与动态菜单
      notify/               低电量提醒策略与多通道通知
      app.py                应用装配与主循环
"""

from .version import APP_NAME, APP_VERSION, APP_ID  # noqa: F401

__all__ = ["APP_NAME", "APP_VERSION", "APP_ID"]
