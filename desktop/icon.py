"""品牌图标渲染（PIL）：圆角渐变底 + 三根上升柱 + 勾。

托盘、安装包、favicon 都从这里取，避免出现两套图标设计。
SVG 版本（网页用）由 scripts/make_logo.py 生成，图形比例与此保持一致。
"""

from __future__ import annotations

ACCENT_TOP = (92, 124, 255)      # #5C7CFF
ACCENT_BOTTOM = (47, 73, 179)    # #2F49B3
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

    def bar(x, y, w, h, rx, alpha):
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            [x * scale, y * scale, (x + w) * scale - 1, (y + h) * scale - 1],
            radius=max(1, int(round(rx * scale))),
            fill=WHITE[:3] + (int(255 * alpha),),
        )
        img.alpha_composite(layer)

    bar(14, 38, 8, 12, 3, 0.82)
    bar(27, 31, 8, 19, 3, 0.92)
    bar(40, 24, 8, 26, 3, 1.0)

    ImageDraw.Draw(img).line(
        [(34 * scale, 17.5 * scale), (38.5 * scale, 22 * scale), (46 * scale, 13.5 * scale)],
        fill=WHITE,
        width=max(1, int(round(4.6 * scale))),
        joint="curve",
    )
    return img
