"""生成图标预览图（人工核对观感用）。

用法::

    python tools/icon_preview.py [输出png]
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from psbt.tray import iconart  # noqa: E402


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts", "icon_preview.png"
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    iconart.save_preview(out)
    print("已生成图标预览：", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
