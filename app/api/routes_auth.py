"""身份 JSON API（SPEC.md 15.6）。

    GET /me   当前用户 {id, username, display_name, role, active, as_id}
              另附 mode / is_admin 两个辅助字段：mode=open|legacy 表示未启用多用户，
              此时 id/as_id 为 null（下游按“不做归属过滤”处理，保持旧行为）。

本模块只暴露 router = APIRouter()，路径不含 /api 前缀（main.py 统一加）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app import auth, security

router = APIRouter()

OPEN_USER = {
    "id": None,
    "username": "",
    "display_name": "本地用户",
    "role": "admin",
    "active": 1,
}


@router.get("/me")
def me(request: Request) -> dict:
    user = auth.current_user(request)
    mode = security.auth_mode()
    payload = dict(OPEN_USER if user is None else user)
    payload["as_id"] = auth.effective_owner_id(request, user)
    payload["mode"] = mode
    payload["is_admin"] = auth.is_admin(user) or user is None
    return payload
