"""生成网页用品牌资源：app/static/logo.svg、favicon.ico、logo-512.png。

图形与托盘/安装包一致（desktop/icon.py）；SVG 手写以便任意缩放都清晰。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STATIC = ROOT / "app" / "static"

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64"
     role="img" aria-label="工作志">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#5C7CFF"/>
      <stop offset="1" stop-color="#2F49B3"/>
    </linearGradient>
  </defs>
  <rect x="2" y="2" width="60" height="60" rx="15" fill="url(#g)"/>
  <g fill="#ffffff">
    <rect x="14" y="38" width="8" height="12" rx="3" opacity=".82"/>
    <rect x="27" y="31" width="8" height="19" rx="3" opacity=".92"/>
    <rect x="40" y="24" width="8" height="26" rx="3"/>
  </g>
  <path d="M34 17.5 L38.5 22 L46 13.5" fill="none" stroke="#ffffff" stroke-width="4.6"
        stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


def main() -> int:
    from desktop.icon import render

    STATIC.mkdir(parents=True, exist_ok=True)
    (STATIC / "logo.svg").write_text(SVG, encoding="utf-8")
    print("logo.svg ->", STATIC / "logo.svg")

    render(256).save(STATIC / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    print("favicon.ico ->", STATIC / "favicon.ico")

    render(512).save(STATIC / "logo-512.png")
    print("logo-512.png ->", STATIC / "logo-512.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
