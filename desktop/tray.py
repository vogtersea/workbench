"""系统托盘：关窗不退出、右键改设置、显式退出。

pystray 的图标菜单在独立线程里跑；所有动作都通过回调回到 app.py 执行，
避免托盘线程直接碰 GUI 对象。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


def _icon_image():
    """生成托盘图标（PIL 画一个圆角方块 + 字母 W），无需外部图片资源。"""
    from PIL import Image, ImageDraw, ImageFont

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=14, fill=(70, 98, 214, 255))
    try:
        font = ImageFont.truetype("segoeui.ttf", 36)
    except Exception:
        font = ImageFont.load_default()
    text = "W"
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(
        ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
        text,
        font=font,
        fill=(255, 255, 255, 255),
    )
    return img


class Tray:
    """托盘图标封装；start() 后由 pystray 线程接管。"""

    def __init__(
        self,
        *,
        title: str,
        on_open: Callable[[], None],
        on_settings: Callable[[], None],
        on_change_dir: Callable[[], None],
        on_toggle_autostart: Callable[[], bool],
        on_quit: Callable[[], None],
        is_autostart: Callable[[], bool],
        is_close_to_tray: Callable[[], bool],
        on_toggle_close_to_tray: Callable[[], bool],
    ):
        self._title = title
        self._on_open = on_open
        self._on_settings = on_settings
        self._on_change_dir = on_change_dir
        self._on_toggle_autostart = on_toggle_autostart
        self._on_quit = on_quit
        self._is_autostart = is_autostart
        self._is_close_to_tray = is_close_to_tray
        self._on_toggle_close_to_tray = on_toggle_close_to_tray
        self._icon = None
        self._thread: threading.Thread | None = None

    # ---- pystray 菜单 ----
    def _menu(self):
        import pystray

        return pystray.Menu(
            pystray.MenuItem("打开工作台", lambda *_: self._on_open(), default=True),
            pystray.MenuItem("设置", lambda *_: self._on_settings()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("更改数据目录…", lambda *_: self._on_change_dir()),
            pystray.MenuItem(
                "关窗时最小化到托盘",
                lambda *_: self._on_toggle_close_to_tray(),
                checked=lambda _item: self._is_close_to_tray(),
            ),
            pystray.MenuItem(
                "开机自动启动",
                lambda *_: self._on_toggle_autostart(),
                checked=lambda _item: self._is_autostart(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda *_: self._on_quit()),
        )

    def start(self) -> bool:
        try:
            import pystray

            self._icon = pystray.Icon(
                "workbench", _icon_image(), self._title, menu=self._menu()
            )
            self._thread = threading.Thread(
                target=self._icon.run, name="workbench-tray", daemon=True
            )
            self._thread.start()
            logger.info("托盘图标已启动")
            return True
        except Exception:
            logger.warning("托盘启动失败（不影响主窗口使用）", exc_info=True)
            return False

    def refresh(self) -> None:
        """刷新菜单勾选状态（pystray 会在下次弹菜单时重新求值，这里只更新提示）。"""
        try:
            if self._icon is not None:
                self._icon.update_menu()
        except Exception:
            logger.debug("刷新托盘菜单失败", exc_info=True)

    def stop(self) -> None:
        try:
            if self._icon is not None:
                self._icon.stop()
        except Exception:
            logger.debug("停止托盘失败", exc_info=True)
