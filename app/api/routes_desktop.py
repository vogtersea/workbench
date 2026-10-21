"""桌面壳专用接口（仅当桌面后端注入时可用；否则一律 404）。

这些接口只监听 127.0.0.1，且只有桌面壳进程会注入后端，所以 NAS 部署时它们等于不存在。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import desktop_bridge

logger = logging.getLogger(__name__)

router = APIRouter()


class PrefsPayload(BaseModel):
    close_to_tray: bool | None = None
    autostart: bool | None = None
    notify: bool | None = None


class DataDirPayload(BaseModel):
    path: str
    mode: str = "migrate"


def _require() -> None:
    if not desktop_bridge.available():
        raise HTTPException(status_code=404, detail="桌面接口不可用（当前不是桌面运行环境）")


@router.get("/desktop/state")
def desktop_state() -> dict:
    _require()
    return desktop_bridge.state()


@router.post("/desktop/prefs")
def desktop_prefs(payload: PrefsPayload) -> dict:
    _require()
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    return desktop_bridge.update_prefs(**data)


@router.post("/desktop/data-dir")
def desktop_data_dir(payload: DataDirPayload) -> dict:
    _require()
    mode = payload.mode if payload.mode in {"migrate", "empty"} else "migrate"
    return desktop_bridge.change_data_dir(payload.path, mode)
