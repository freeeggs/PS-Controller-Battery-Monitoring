"""日志初始化。

打包版本没有控制台，日志是排查兼容性问题的唯一手段，因此：
* 写入 ``%APPDATA%\\PSBatteryTray\\logs\\psbt.log``，单文件 512KB、滚动保留 3 份；
* ``--console`` 时额外输出到标准输出，方便源码调试；
* 文件日志始终 UTF-8，避免中文乱码。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from .paths import log_dir

_LOGGER_NAME = "psbt"
_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_configured = False


def setup_logging(level: str = "INFO", console: bool = False) -> logging.Logger:
    """配置并返回根 logger。重复调用是幂等的。"""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    fmt = logging.Formatter(_FORMAT)

    try:
        logfile = os.path.join(log_dir(), "psbt.log")
        fh = logging.handlers.RotatingFileHandler(
            logfile, maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        # 文件始终按 DEBUG 记录，详细程度由 logger 级别控制，
        # 这样运行中调整级别无需重建 handler。
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)
    except OSError:
        pass

    if console:
        sh = logging.StreamHandler(stream=sys.stdout)
        sh.setFormatter(fmt)
        sh.setLevel(logging.DEBUG)
        logger.addHandler(sh)

    if not logger.handlers:  # 极端情况下至少不丢日志
        logger.addHandler(logging.NullHandler())

    _configured = True
    return logger


def get_logger(name: str = "") -> logging.Logger:
    """获取子 logger，统一挂在 ``psbt`` 命名空间下。"""
    return logging.getLogger("%s.%s" % (_LOGGER_NAME, name) if name else _LOGGER_NAME)


def install_excepthook() -> None:
    """把未捕获异常写进日志，避免打包后「闪退且无任何线索」。"""
    log = get_logger("excepthook")

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log.error("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _hook

    try:
        import threading

        def _thread_hook(args):
            if issubclass(args.exc_type, SystemExit):
                return
            log.error(
                "线程 %s 未捕获异常",
                getattr(args.thread, "name", "?"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

        threading.excepthook = _thread_hook
    except Exception:  # pragma: no cover - Python < 3.8 不存在
        pass
