"""提醒 JSON API（SPEC.md 第 6 节 + 15.7 / 15.8）。

    GET  /reminders/preview   扫描并返回分桶结果（不推送、不写日志）
    POST /reminders/test      发一条测试推送，HTTP 永远 200（多用户模式下仅管理员可调用）

归属（SPEC 15.7）：多用户模式一律带生效用户过滤。管理员不带 ?as= 时返回按用户分组的
合并视图（users + totals），带 ?as=<id> 时只看该用户；普通用户永远只看自己。
单用户/开放模式（effective_owner_id 为 None）逐字保持改造前行为。
写操作（/reminders/test）在多用户模式下非管理员返回 403 JSON。

本模块只暴露 router = APIRouter()，路径不含 /api 前缀（main.py 统一加）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import auth, security, timeutil
from app.services import reminders, scheduler, users

logger = logging.getLogger(__name__)

router = APIRouter()

EMPTY_COUNTS = {"overdue": 0, "today": 0, "soon": 0, "total": 0}
FORBIDDEN_MESSAGE = "只有管理员可以执行该操作"


def _forbidden(message: str = FORBIDDEN_MESSAGE) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"ok": False, "error": message, "code": "forbidden", "counts": dict(EMPTY_COUNTS)},
    )


def _is_manage_allowed(ident: dict[str, Any]) -> bool:
    """单用户/开放模式没有用户概念 -> 允许；多用户模式仅管理员允许。"""
    return ident["mode"] != security.MODE_MULTI or bool(ident["is_admin"])


def _flat(result: dict[str, Any]) -> dict[str, Any]:
    """改造前的外层结构（字段名与顺序保持兼容）。"""
    counts = result.get("counts") or EMPTY_COUNTS
    return {
        "ok": True,
        "generated_at": result.get("generated_at") or timeutil.now_iso(),
        "today_date": result.get("today_date"),
        "soon_days": result.get("soon_days"),
        "counts": counts,
        "overdue_count": counts.get("overdue", 0),
        "today_count": counts.get("today", 0),
        "soon_count": counts.get("soon", 0),
        "has_items": bool(result.get("has_items")),
        "overdue": result.get("overdue") or [],
        "today": result.get("today") or [],
        "soon": result.get("soon") or [],
        "message": result.get("message") or "",
    }


def _group_entry(owner_id: int | None, single: dict[str, Any]) -> dict[str, Any]:
    """单用户扫描结果包装成 scan_all 的用户条目，保证两种视角结构一致。

    小节名必须用「数据归属人」而不是登录人：管理员带 ?as=<id> 时归属人是目标用户。
    """
    row = users.get(owner_id) if owner_id is not None else None
    return {
        "user_id": int(row["id"]) if row is not None else 0,
        "username": (row["username"] if row is not None else "") or "",
        "display_name": (row["display_name"] or row["username"] if row is not None else "") or "",
        "overdue": single.get("overdue") or [],
        "today": single.get("today") or [],
        "soon": single.get("soon") or [],
        "counts": single.get("counts") or EMPTY_COUNTS,
    }


def _multi_payload(grouped: dict[str, Any]) -> dict[str, Any]:
    """分组结果 -> 兼容旧字段 + users/totals 的新结构（外层计数为合计）。"""
    users = list(grouped.get("users") or [])
    totals = dict(grouped.get("totals") or EMPTY_COUNTS)
    payload = _flat(
        {
            "overdue": [item for user in users for item in user.get("overdue") or []],
            "today": [item for user in users for item in user.get("today") or []],
            "soon": [item for user in users for item in user.get("soon") or []],
            "counts": totals,
            "today_date": grouped.get("today_date"),
            "soon_days": grouped.get("soon_days"),
            "generated_at": grouped.get("generated_at"),
            "has_items": grouped.get("has_items"),
            "message": grouped.get("message"),
        }
    )
    payload.update(
        {
            "mode": "multi",
            "users": users,
            "totals": totals,
            "user_count": grouped.get("user_count", len(users)),
            "active_user_count": grouped.get("active_user_count", 0),
        }
    )
    return payload


@router.get("/reminders/preview")
def reminders_preview(request: Request) -> Any:
    """预览提醒分桶：管理员看分组合并视图，普通用户只看自己。"""
    try:
        ident = auth.request_context(request)
        raw_as = (request.query_params.get("as") or "").strip()
        if ident["mode"] != security.MODE_MULTI:
            # 单用户 / 开放模式：不加任何新字段，返回值与改造前逐字一致
            return _flat(reminders.scan())
        if ident["is_admin"] and not raw_as:
            return _multi_payload(reminders.scan_all())
        single = reminders.scan(ident["owner_id"])
        payload = _flat(single)
        payload.update(
            {
                "mode": "multi",
                "users": [_group_entry(ident["owner_id"], single)],
                "totals": payload["counts"],
                "user_count": 1,
                "active_user_count": 1 if payload["has_items"] else 0,
            }
        )
        return payload
    except Exception as exc:
        logger.exception("提醒预览失败")
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "counts": dict(EMPTY_COUNTS),
            "overdue_count": 0,
            "today_count": 0,
            "soon_count": 0,
            "has_items": False,
            "overdue": [],
            "today": [],
            "soon": [],
            "message": "",
        }


@router.post("/reminders/test")
def reminders_test(request: Request) -> Any:
    """测试推送：未配置 Webhook 时返回 ok=false + channel=disabled，HTTP 仍为 200。"""
    if not _is_manage_allowed(auth.request_context(request)):
        logger.info("非管理员尝试测试推送，已拒绝")
        return _forbidden()
    try:
        result = reminders.test_push()
    except Exception as exc:
        logger.exception("测试推送失败")
        result = {
            "ok": False,
            "channel": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "sent_at": timeutil.now_iso(),
            "message": "",
        }
    return {
        "ok": bool(result.get("ok")),
        "channel": result.get("channel") or "disabled",
        "error": result.get("error"),
        "sent_at": result.get("sent_at"),
        "message": result.get("message") or "",
        "scheduler": scheduler.status(),
        "last_scan": reminders.last_scan(),
    }
