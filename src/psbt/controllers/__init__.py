"""手柄检测与电量读取子系统。

与托盘 / 通知层完全解耦：本包只负责「有哪些手柄、电量多少」，通过回调把
不可变快照（:class:`ControllerView`）推给上层。
"""

from .manager import ControllerManager, ManagerEvents  # noqa: F401
from .models import (  # noqa: F401
    BatteryReport,
    ChargeState,
    ControllerView,
    Transport,
)
