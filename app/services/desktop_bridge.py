"""桌面壳桥接层（桌面版专用，NAS/服务器版为空）。

桌面版（pywebview 壳）在启动时通过 set_backend() 注入一个后端对象；应用代码只依赖
这里的接口，不 import desktop 包，因此服务器版行为完全不变：

    backend.state() -> dict                     当前桌面状态（数据目录/托盘/自启/通知/端口）
    backend.update_prefs(**kw) -> dict          改托盘行为、开机自启、系统通知开关
    backend.change_data_dir(path, mode) -> dict 改数据目录（mode: migrate | empty）
    backend.pick_dir() -> str | None            呼出原生目录选择框（托盘菜单用）
    backend.request_restart() -> None            重启桌面应用

backend 为 None 时，桌面专属路由一律 404，设置页也不显示「桌面应用」一节。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_backend: Any = None
_notifier: Any = None


def set_backend(backend: Any) -> None:
    global _backend
    _backend = backend
    logger.info("桌面桥接后端已注入：%s", type(backend).__name__)


def get_backend() -> Any:
    return _backend


def available() -> bool:
    """当前进程是否运行在桌面壳里。"""
    return _backend is not None


def state() -> dict:
    if _backend is None:
        return {}
    try:
        return _backend.state()
    except Exception:
        logger.exception("读取桌面状态失败")
        return {}


def update_prefs(**kw: Any) -> dict:
    if _backend is None:
        return {"ok": False, "error": "非桌面运行环境"}
    try:
        return _backend.update_prefs(**kw)
    except Exception as exc:
        logger.exception("更新桌面设置失败")
        return {"ok": False, "error": str(exc)[:200]}


def change_data_dir(path: str, mode: str) -> dict:
    if _backend is None:
        return {"ok": False, "error": "非桌面运行环境"}
    try:
        return _backend.change_data_dir(path, mode)
    except Exception as exc:
        logger.exception("切换数据目录失败")
        return {"ok": False, "error": str(exc)[:200]}


def set_notifier(fn: Any) -> None:
    """注册系统通知回调：签名 fn(title: str, body: str, url: str | None) -> bool。"""
    global _notifier
    _notifier = fn


def notify(title: str, body: str, url: str | None = None) -> bool:
    """发一条系统通知；非桌面环境或失败都返回 False，调用方不需要区分。"""
    if _notifier is None:
        return False
    try:
        return bool(_notifier(title, body, url))
    except Exception:
        logger.exception("系统通知发送失败")
        return False
