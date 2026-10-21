"""PC 布局回归守卫。

真实事故：.container 有 margin:0 auto，而 .app-main 是 flex 纵向容器——
flex 里 auto 外边距会吸收剩余空间、使元素退化成「内容宽度」，于是 --maxw 完全失效，
桌面浏览器里页面只有 574px 宽（1920 窗口），看起来「太瘦」。
修法给 .container 显式 width:100%，本测试守住它。
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "app.css"


def _css() -> str:
    return CSS.read_text(encoding="utf-8")


def test_容器必须有显式宽度否则在_flex_里塌缩成内容宽度():
    css = _css()
    match = re.search(r"\.app-main > \.container\s*\{([^}]*)\}", css)
    assert match, ".app-main > .container 规则不存在"
    body = match.group(1)
    assert "width: 100%" in body, "缺少 width:100%：flex 纵向容器里 auto 外边距会让容器退化为内容宽度"
    assert "flex: 1 0 auto" in body


def test_内容宽度按视口分档():
    css = _css()
    tiers = {1360: "1380px", 1760: "1560px", 2160: "1720px"}
    for min_width, maxw in tiers.items():
        pattern = r"@media \(min-width: %dpx\)\s*\{\s*:root \{\s*--maxw: %s" % (min_width, re.escape(maxw))
        assert re.search(pattern, css), "缺少 %dpx 档位（--maxw: %s）" % (min_width, maxw)


def test_仪表盘分区在宽屏并排且能排满():
    css = _css()
    match = re.search(r"@media \(min-width: 1100px\)\s*\{(.*?)\n\}", css, re.S)
    assert match, "缺少宽屏分区栅格"
    block = match.group(1)
    assert "repeat(6" in block, "应为 6 栏栅格，才能让 3 小 + 2 大正好排满两行"
    assert ".bucket-grid > .bucket { grid-column: span 2; }" in block
    assert ".bucket-grid .bucket-week, .bucket-grid .bucket-none { grid-column: span 3; }" in block


def test_移动端断点未被宽屏规则覆盖():
    css = _css()
    # 新增的宽屏规则必须都是 min-width；max-width 断点只允许既有的 900 / 640
    max_widths = sorted({int(m) for m in re.findall(r"@media \(max-width: (\d+)px\)", css)})
    assert max_widths == [640, 900], "移动端断点被改动：%s" % max_widths
    min_widths = sorted({int(m) for m in re.findall(r"@media \(min-width: (\d+)px\)", css)})
    assert min_widths == [1100, 1280, 1360, 1760, 2160], "宽屏断点异常：%s" % min_widths
