"""深色主题回归守卫。

两个真实事故：
1. 顶栏/卡片头/表头等用硬编码白色，深色下出现成片白条（实测亮像素 19.9% → 0.04%）；
2. 提示条文字写死深色，深色背景下几乎看不见。

本测试守住这两条：浅色变量的值不许硬编码在组件规则里，且深色块必须覆盖浅色的每一个变量。
"""

from __future__ import annotations

import re
from pathlib import Path

CSS_PATH = Path(__file__).resolve().parent.parent / "app" / "static" / "app.css"

#: 深色下会刺眼的浅色字面量（作为背景/描边时）
LIGHT_LITERALS = ("#fff", "#ffffff", "#fcfcfd", "#fafbfc", "#fbfbfc", "#f1f3f5", "rgba(255, 255, 255")


def _css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


COLOR_VALUE = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\(|hsla?\(|white$|black$)")


def _block_decls(css: str, selector: str) -> dict[str, str]:
    """取出某个选择器块内的 --变量名 -> 值。"""
    body = _block_body(css, selector)
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)}


def _color_vars(css: str, selector: str) -> set[str]:
    """只保留颜色类变量：字体、间距、圆角这类与主题无关，不需要深色覆盖。"""
    return {name for name, val in _block_decls(css, selector).items() if COLOR_VALUE.match(val)}


def _block_vars(css: str, selector: str) -> set[str]:
    """取出某个选择器块内定义的 --变量名集合。"""
    return set(_block_decls(css, selector))


def _block_body(css: str, selector: str) -> str:
    start = css.find(selector)
    assert start >= 0, "找不到选择器：%s" % selector
    open_brace = css.index("{", start)
    depth = 0
    end = open_brace
    for i in range(open_brace, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    return css[open_brace:end]


def test_深色块覆盖浅色主题的全部变量():
    css = _css()
    light = _color_vars(css, ":root {")
    assert len(light) >= 15, "颜色变量提取异常，只有 %d 个" % len(light)
    dark = _block_vars(css, ':root[data-theme="dark"] {')
    system_dark = _block_vars(css, ':root:not([data-theme="light"]) {')
    missing = sorted(light - dark)
    assert not missing, "深色主题缺少变量（会退回浅色值，深色下刺眼）：%s" % missing
    missing_sys = sorted(light - system_dark)
    assert not missing_sys, "跟随系统深色块缺少变量：%s" % missing_sys


def test_组件规则里不许硬编码浅色背景():
    css = _css()
    offenders = []
    depth = 0
    in_print = False
    for lineno, line in enumerate(css.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("@media print"):
            in_print = True
        depth += line.count("{") - line.count("}")
        if in_print and depth <= 0 and "}" in line:
            in_print = False
        if in_print or stripped.startswith("--") or "/*" in stripped:
            continue
        if not re.search(r"(background|border-color)\s*:", stripped, re.I):
            continue
        low = stripped.lower()
        if any(lit in low for lit in LIGHT_LITERALS):
            offenders.append("第 %d 行: %s" % (lineno, stripped))
    assert not offenders, "以下规则在深色下会发白，请改用主题变量：\n" + "\n".join(offenders)


def test_提示条文字用变量而不是写死深色():
    css = _css()
    for kind, var in (("info", "info"), ("warn", "warn"), ("danger", "danger"), ("success", "success")):
        match = re.search(r"\.alert-%s\s*\{[^}]*\}" % kind, css)
        assert match, ".alert-%s 不存在" % kind
        rule = match.group(0)
        assert "color: var(--%s)" % var in rule, ".alert-%s 的文字颜色必须用 var(--%s)" % (kind, var)


def test_深色下的文字与背景对比度足够():
    """按 WCAG 相对亮度算对比度，正文与次级文字都要 >= 4.5（AA）。"""
    css = _css()
    dark = _block_vars(css, ':root[data-theme="dark"] {')

    def value(name: str) -> str:
        match = re.search(r'%s:\s*([^;]+);' % re.escape(name), css)
        return match.group(1).strip()

    def luminance(hexcolor: str) -> float:
        hexcolor = hexcolor.strip().lstrip("#")
        channels = []
        for i in (0, 2, 4):
            c = int(hexcolor[i:i + 2], 16) / 255.0
            channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    bg = luminance(value("--surface"))
    for name in ("--text", "--text-muted"):
        fg = luminance(value(name))
        lighter, darker = max(fg, bg), min(fg, bg)
        ratio = (lighter + 0.05) / (darker + 0.05)
        assert ratio >= 4.5, "%s 在深色背景上对比度只有 %.2f，低于 4.5" % (name, ratio)
    assert dark  # 触发上面的变量提取，避免断言被优化掉
