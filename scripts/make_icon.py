"""生成打包与托盘共用的图标（build/workbench.ico）。

图标用代码画（圆角蓝底 + 白色 W），避免仓库里塞二进制资源。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from desktop.tray import _icon_image

    out = ROOT / "build"
    out.mkdir(parents=True, exist_ok=True)
    image = _icon_image()
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    image.save(out / "workbench.ico", format="ICO", sizes=sizes)
    print("图标已生成：%s" % (out / "workbench.ico"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
