"""Windows 系统通知（winotify）。非 Windows / 未安装依赖时静默降级。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

APP_ID = "工作台"


def available() -> bool:
    try:
        import winotify  # noqa: F401

        return True
    except Exception:
        return False


def toast(title: str, body: str, url: str | None = None) -> bool:
    """弹一条通知；点击可打开 url（通常是本地服务地址）。失败返回 False。"""
    try:
        from winotify import Notification

        note = Notification(app_id=APP_ID, title=title, msg=body, duration="short")
        if url:
            note.set_audio(None, None) if False else None
            note.add_actions(label="打开工作台", launch=url)
        note.show()
        return True
    except Exception:
        logger.debug("系统通知发送失败", exc_info=True)
        return False
