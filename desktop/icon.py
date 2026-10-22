"""品牌图标渲染（PIL）：圆角渐变底 + 白色文档卡 + 三条文字线 + 一个绿勾。

设计取舍：App 图标要在 24px 也认得出，所以用「文档 + 勾」而不是抽象柱状图——
前者任何尺寸都读作「一份记录」，后者缩小后退化成几个色块。
托盘、安装包、favicon、Android 图标都从这里渲染，避免多套设计。
SVG 版本（网页用）由 scripts/make_logo.py 生成，比例与此一致。
"""

from __future__ import annotations

ACCENT_TOP = (92, 124, 255)
ACCENT_BOTTOM = (47, 73, 179)
ACCENT_MID = (58, 90, 220)
CHECK_GREEN = (52, 199, 110)
WHITE = (255, 255, 255, 255)


def render(size: int = 64):
    from PIL import Image, ImageDraw

    scale = size / 64.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    radius = max(1, int(round(15 * scale)))
    grad = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(
            int(round(ACCENT_TOP[i] + (ACCENT_BOTTOM[i] - ACCENT_TOP[i]) * t)) for i in range(3)
        ) + (255,)
        gdraw.line([(0, y), (size, y)], fill=color)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)

    draw = ImageDraw.Draw(img)

    def box(x0, y0, x1, y1, r, fill):
        draw.rounded_rectangle(
            [x0 * scale, y0 * scale, x1 * scale - 1, y1 * scale - 1],
            radius=max(1, int(round(r * scale))), fill=fill,
        )

    box(14, 9, 50, 55, 6, WHITE)
    for index, width in enumerate((22, 18, 13)):
        top = 18 + index * 9
        box(21, top, 21 + width, top + 4, 2, ACCENT_MID + (255,))

    draw.line(
        [(33 * scale, 45 * scale), (38.5 * scale, 50.5 * scale), (47 * scale, 38 * scale)],
        fill=CHECK_GREEN + (255,),
        width=max(1, int(round(3.8 * scale))),
        joint="curve",
    )
    return img
