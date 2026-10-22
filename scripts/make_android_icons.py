"""生成 Android 应用图标（多密度 mipmap PNG）。

图形与桌面版/网页共用 desktop/icon.py 的同一套画法（圆角渐变底 + 三根上升柱 + 勾），
不在仓库里塞二进制设计稿。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DENSITIES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}


def main() -> int:
    from desktop.icon import render

    base = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "android/app/src/main/res"
    for density, size in DENSITIES.items():
        target = Path(base) / ("mipmap-" + density)
        target.mkdir(parents=True, exist_ok=True)
        img = render(size)
        img.save(target / "ic_launcher.png")
        img.save(target / "ic_launcher_round.png")
        print("  %-10s %dx%d" % (density, size, size))
    # 应用商店/关于页用的大图
    store = ROOT / "android" / "icon-512.png"
    render(512).save(store)
    print("  512x512 ->", store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
