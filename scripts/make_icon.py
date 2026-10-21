"""生成打包用图标（build/workbench.ico），图形与托盘、网页完全一致。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from desktop.icon import render

    out = ROOT / "build"
    out.mkdir(parents=True, exist_ok=True)
    render(256).save(
        out / "workbench.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("图标已生成：%s" % (out / "workbench.ico"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
