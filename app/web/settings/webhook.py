"""设置页推送与提醒域：POST /settings 保存提醒与 Webhook 配置、发送测试推送。

职责边界（W-08）：只处理推送渠道与每日提醒相关的表单与测试动作，不碰大模型与知识库。"""

from __future__ import annotations

import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import templating, timeutil
from app.services import reminders, scheduler
from app.services import settings_store as S

from .general import _render_page
from .shared import _is_htmx, logger
from .users import _guard_write

router = APIRouter()

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@router.post("/settings", response_class=HTMLResponse)
def settings_save(
    request: Request,
    form: str | None = Form(None),
    remind_time: str | None = Form(None),
    remind_soon_days: str | None = Form(None),
    webhook_url: str | None = Form(None),
    webhook_format: str | None = Form(None),
    telegram_chat_id: str | None = Form(None),
    obsidian_enabled: str | None = Form(None),
):
    """只更新本次提交里出现的字段（未出现的保持原值）。"""
    denied = _guard_write(request)
    if denied is not None:
        return denied
    submitted: dict[str, str] = {}
    updates: dict[str, str] = {}
    errors: list[str] = []

    if remind_time is not None:
        raw = (remind_time or "").strip()
        submitted["remind_time"] = raw
        match = TIME_RE.match(raw)
        if match:
            updates["remind_time"] = f"{int(match.group(1)):02d}:{match.group(2)}"
        else:
            errors.append("提醒时间格式必须是 HH:MM（例如 09:00）")

    if remind_soon_days is not None:
        raw = (remind_soon_days or "").strip()
        submitted["remind_soon_days"] = raw
        try:
            days = int(float(raw))
        except (TypeError, ValueError):
            days = -1
        if 0 <= days <= 60:
            updates["remind_soon_days"] = str(days)
        else:
            errors.append("临期天数必须是 0~60 的整数")

    if webhook_format is not None:
        raw = (webhook_format or "").strip()
        submitted["webhook_format"] = raw
        if raw in reminders.WEBHOOK_FORMATS:
            updates["webhook_format"] = raw
        else:
            errors.append("Webhook 格式不在允许的 6 种之内")

    if webhook_url is not None:
        raw = (webhook_url or "").strip()
        submitted["webhook_url"] = raw
        if not raw:
            updates["webhook_url"] = ""
        elif raw.lower().startswith(("http://", "https://")):
            updates["webhook_url"] = raw
        else:
            errors.append("Webhook 地址必须以 http:// 或 https:// 开头")

    if telegram_chat_id is not None:
        raw = (telegram_chat_id or "").strip()
        submitted["telegram_chat_id"] = raw
        updates["telegram_chat_id"] = raw

    if form in {"settings", "obsidian"}:
        submitted["obsidian_enabled"] = obsidian_enabled or "0"
        updates["obsidian_enabled"] = "1" if (obsidian_enabled or "").strip() in {"1", "on", "true"} else "0"

    if errors:
        return _render_page(request, error="；".join(errors), form_override=submitted)

    for key, value in updates.items():
        try:
            S.set(key, value)
        except Exception as exc:
            logger.exception("保存设置 %s 失败", key)
            return _render_page(
                request, error=f"保存 {key} 失败：{type(exc).__name__}: {exc}", form_override=submitted
            )

    if "remind_time" in updates:
        try:
            scheduler.reschedule()
        except Exception:
            logger.exception("提醒时间已保存，但热更新调度器失败")

    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/test-push", response_class=HTMLResponse)
def settings_test_push(request: Request):
    denied = _guard_write(request)
    if denied is not None:
        return denied
    try:
        result = reminders.test_push()
    except Exception as exc:
        logger.exception("测试推送失败")
        result = {
            "ok": False,
            "channel": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "sent_at": timeutil.now_iso(),
            "kind": "test",
        }
    if _is_htmx(request):
        return templating.render(request, "partials/settings_test_result.html", result=result)
    return RedirectResponse("/settings?flash=test-push", status_code=303)
