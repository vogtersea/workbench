"""设置页权限层：身份判定与写操作守卫（多用户模式下的管理员校验）。

职责边界（W-08）：只做谁能写，不注册路由；本文件当前没有账号/邀请码路由
（那些页面在 pages_auth.py，不属 W-08 范围）。"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from app import auth, security
from app.services import users

from .shared import logger

# --------------------------------------------------------------------------
# 权限（SPEC 15.8：共享配置由管理员维护，非管理员写操作一律 403）
# --------------------------------------------------------------------------
FORBIDDEN_TEXT = "共享设置（大模型 / 知识库 / Webhook / 每日提醒）由管理员维护，当前账号只能查看。"


def _identity(request: Request) -> dict[str, Any]:
    """身份 + 写权限判定；模板据此决定渲染管理控件还是只读说明。"""
    ident = auth.request_context(request)
    mode = ident["mode"]
    can_manage = mode != security.MODE_MULTI or bool(ident["is_admin"])
    user_count = 0
    if mode == security.MODE_MULTI:
        try:
            user_count = users.count()
        except Exception:
            logger.warning("统计用户数失败", exc_info=True)
    return {**ident, "can_manage": can_manage, "user_count": user_count}


def _forbidden_page(request: Request):
    """非管理员写操作的页面出口：200 之外的 403 页面（不带任何写控件）。"""
    from .general import _render_page  # 延迟导入：general 反向依赖本文件的权限判定

    return _render_page(request, status_code=403, error=FORBIDDEN_TEXT)


def _guard_write(request: Request):
    """管理员 / 单用户模式放行（None），多用户模式下的普通用户返回 403 页面。"""
    if _identity(request)["can_manage"]:
        return None
    logger.info("非管理员尝试修改共享设置：%s %s", request.method, request.url.path)
    return _forbidden_page(request)
