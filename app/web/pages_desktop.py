"""设置页的「桌面应用」一节（桌面版专用；非桌面环境一律 404）。

用普通 form POST，无 JS 依赖，与项目其它页面一致。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.services import desktop_bridge

logger = logging.getLogger(__name__)

pages = APIRouter()


def _require() -> None:
    if not desktop_bridge.available():
        raise HTTPException(status_code=404, detail="桌面设置仅在桌面版可用")


def _back(**params: str) -> RedirectResponse:
    query = "&".join("%s=%s" % (k, v) for k, v in params.items() if v)
    return RedirectResponse("/settings%s" % ("?" + query if query else ""), status_code=303)


@pages.post("/settings/desktop")
def settings_desktop(
    request: Request,
    close_to_tray: str = Form(""),
    autostart: str = Form(""),
    notify: str = Form(""),
):
    """保存托盘行为、开机自启、系统通知三个开关（复选框未勾选时不会提交，故按空串处理）。"""
    _require()
    truthy = {"1", "on", "true", "yes"}
    result = desktop_bridge.update_prefs(
        close_to_tray=close_to_tray.strip().lower() in truthy,
        autostart=autostart.strip().lower() in truthy,
        notify=notify.strip().lower() in truthy,
    )
    if not result.get("ok", True):
        return _back(desktop_error="1")
    return _back(desktop_saved="1")


@pages.post("/settings/desktop/data-dir")
def settings_desktop_data_dir(request: Request, path: str = Form(""), mode: str = Form("migrate")):
    """切换数据目录：mode=migrate 迁移过去，mode=empty 用空库开始。"""
    _require()
    target = (path or "").strip().strip('"')
    if not target:
        return _back(desktop_error="1")
    result = desktop_bridge.change_data_dir(target, "empty" if mode == "empty" else "migrate")
    if not result.get("ok"):
        return _back(desktop_error="1")
    return _back(desktop_restart="1")


@pages.post("/settings/desktop/restart")
def settings_desktop_restart(request: Request):
    """重启桌面应用（切换数据目录后生效）。"""
    _require()
    backend = desktop_bridge.get_backend()
    if backend is not None:
        backend.request_restart()
    return _back(desktop_restart="1")
