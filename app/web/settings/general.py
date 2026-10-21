"""设置页基础域：页面渲染骨架、系统信息、基础设置页与扫描/索引/知识库目录路由。

职责边界（W-08）：_page_context 与 _render_page 是四个子域共用的渲染骨架；
本文件同时承载 GET/POST /settings、立即扫描、重建索引与知识库目录这几条路由。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db, templating, timeutil
from app.config import settings
from app.services import reminders, scheduler
from app.services import settings_store as S

from .llm_context import _llm_context
from .shared import (
    FLASH_MESSAGES,
    LAST_REINDEX_KEY,
    _field,
    _is_htmx,
    _load_reindex_result,
    _obsidian_fn,
    logger,
)
from .users import _guard_write, _identity
from .vault import _vault_browser, _vault_card, _vault_context

router = APIRouter()

#: 下拉框：6 种 Webhook 格式（key 与 SPEC 第 7 节第 5 条一一对应）
WEBHOOK_FORMAT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("generic_json", "通用 JSON —— {\"text\": \"...\"}"),
    ("wecom_text", "企业微信群机器人 —— msgtype=text"),
    ("feishu_text", "飞书自定义机器人 —— msg_type=text"),
    ("serverchan", "Server 酱 —— title + desp"),
    ("telegram", "Telegram Bot —— chat_id + text"),
    ("bark", "Bark（iOS）—— title + body"),
)


COUNT_TABLES = (
    "projects",
    "tasks",
    "progress_entries",
    "reports",
    "settings",
    "reminders_log",
    "notes_index",
)


def _human_size(num: int) -> str:
    value = float(max(0, num))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} GB"


def _probe_llm_mock() -> tuple[bool, str]:
    """llm.is_mock_mode() 可能还是 stub（NotImplementedError），必须容错。"""
    try:
        from app.services import llm

        return bool(llm.is_mock_mode()), ""
    except NotImplementedError:
        return settings.llm_mock, "llm 域尚未实现，按 LLM_API_KEY 是否为空推断"
    except Exception as exc:
        return settings.llm_mock, f"{type(exc).__name__}: {exc}"


def _probe_vault() -> tuple[bool, str]:
    """obsidian.vault_available() 同样可能还是 stub。"""
    try:
        from app.services import obsidian

        return bool(obsidian.vault_available()), ""
    except NotImplementedError:
        return settings.vault_exists, "obsidian 域尚未实现，按 VAULT_DIR 是否存在推断"
    except Exception as exc:
        return settings.vault_exists, f"{type(exc).__name__}: {exc}"


def _system_info() -> dict[str, Any]:
    db_path = settings.db_path
    size = 0
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            size += candidate.stat().st_size
        except OSError:
            pass
    counts: dict[str, int | None] = {}
    for table in COUNT_TABLES:
        try:
            row = db.one(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = int(row["n"]) if row is not None else 0
        except Exception:
            counts[table] = None

    llm_mock, llm_note = _probe_llm_mock()
    vault_ok, vault_note = _probe_vault()
    return {
        "data_dir": str(settings.data_dir),
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "db_size": _human_size(size),
        "db_size_bytes": size,
        "tz_name": settings.tz_name,
        "today": timeutil.today().isoformat(),
        "now": timeutil.now_iso(),
        "auth_enabled": settings.auth_enabled,
        "llm_mock": llm_mock,
        "llm_model": S.get("llm_model", "") or settings.llm_model,
        "llm_base_url": S.get("llm_base_url", "") or settings.llm_base_url,
        "llm_note": llm_note,
        "vault_dir": str(settings.vault_dir),
        "vault_available": vault_ok,
        "vault_note": vault_note,
        "counts": counts,
        "db_path_obj": db_path,
    }


def _current_values() -> dict[str, str]:
    values = S.all()
    remind = (values.get("remind_time") or settings.remind_time or "09:00").strip()
    return {
        "remind_time": remind,
        "remind_soon_days": (values.get("remind_soon_days") or "3").strip(),
        "webhook_url": values.get("webhook_url", ""),
        "webhook_format": values.get("webhook_format") or "generic_json",
        "telegram_chat_id": values.get("telegram_chat_id", ""),
        "obsidian_enabled": values.get("obsidian_enabled") or "0",
    }


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _page_context(
    *,
    edit_id: str = "",
    browse_rel: str | None = None,
    notice: str = "",
) -> dict[str, Any]:
    values = _current_values()
    return {
        "form": values,
        "format_options": WEBHOOK_FORMAT_OPTIONS,
        "system": _system_info(),
        "last_scan": reminders.last_scan(),
        "last_test": reminders.last_test_push(),
        "last_reindex": _load_reindex_result(),
        "scheduler": scheduler.status(),
        "env_webhook_url": settings.webhook_url,
        "obsidian_last_index": S.get("obsidian_last_index", ""),
        "llm": _llm_context(edit_id),
        "vault": _vault_context(browse_rel),
        "notice": notice,
        "notice_kind": "info",
        # 桌面版才有的状态（数据目录/托盘/自启/通知）；服务器版为 {}
        "desktop_state": _desktop_state(),
    }


def _desktop_state() -> dict[str, Any]:
    """桌面版状态；服务器版返回空字典（模板据此隐藏整块）。"""
    from app.services import desktop_bridge

    return desktop_bridge.state() if desktop_bridge.available() else {}


def _render_page(
    request: Request,
    *,
    saved: bool = False,
    flash: str = "",
    error: str = "",
    form_override: dict[str, str] | None = None,
    status_code: int = 200,
    edit_id: str = "",
    browse_rel: str | None = None,
    llm_override: dict[str, Any] | None = None,
    vault_override: dict[str, Any] | None = None,
):
    context = _page_context(edit_id=edit_id, browse_rel=browse_rel)
    if llm_override is not None:
        context["llm"] = _llm_context(
            edit_id, selected_override=llm_override, notice=context["llm"]["notice"]
        )
    if vault_override is not None:
        context["vault"] = vault_override
    if form_override:
        context["form"] = {**context["form"], **form_override}
    ident = _identity(request)
    context.update(
        {
            "saved": saved,
            "flash": flash,
            "flash_message": FLASH_MESSAGES.get(flash, ""),
            "error": error,
            "can_manage": ident["can_manage"],
            "auth_mode": ident["mode"],
            "user_count": ident["user_count"],
        }
    )
    return templating.render(request, "settings.html", status_code=status_code, **context)


def _llm_card(
    request: Request,
    *,
    notice: str = "",
    notice_kind: str = "info",
    edit_id: str = "",
    selected_override: dict[str, Any] | None = None,
    status_code: int = 200,
):
    return templating.render(
        request,
        "partials/settings_llm_card.html",
        status_code=status_code,
        llm=_llm_context(edit_id, notice=notice, notice_kind=notice_kind, selected_override=selected_override),
    )


def _unavailable(
    request: Request, message: str, *, card: str = "llm", prefix: str = "该项暂不可用：", **kwargs
):
    """失败统一出口：htmx 返回卡片片段，普通请求返回整页错误提示。

    prefix 为空即普通操作失败（参数非法 / 服务拒绝），只有域未落地时才用「该项暂不可用」。
    """
    text = f"{prefix}{message}"
    kind = "warn" if prefix else "danger"
    if _is_htmx(request):
        if card == "vault":
            return _vault_card(request, notice=text, notice_kind=kind, **kwargs)
        return _llm_card(request, notice=text, notice_kind=kind, **kwargs)
    if card == "vault":
        return _render_page(request, error=text, **{k: v for k, v in kwargs.items() if k in {"browse_rel"}})
    return _render_page(request, error=text)


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, flash: str = "", edit: str = "", browse: str = ""):
    return _render_page(
        request,
        saved=bool(saved),
        flash=flash,
        edit_id=(edit or "").strip(),
        browse_rel=(browse or "").strip() or None,
    )


@router.post("/settings/scan-now", response_class=HTMLResponse)
def settings_scan_now(request: Request):
    denied = _guard_write(request)
    if denied is not None:
        return denied
    try:
        result = reminders.send_daily()
    except Exception as exc:
        logger.exception("立即扫描失败")
        result = {
            "ok": False,
            "channel": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "sent_at": timeutil.now_iso(),
            "counts": {},
            "has_items": False,
        }
    if _is_htmx(request):
        ident = _identity(request)
        return templating.render(
            request,
            "partials/settings_scan_summary.html",
            last_scan=reminders.last_scan(),
            scan_now=result,
            auth_mode=ident["mode"],
            user_count=ident["user_count"],
        )
    return RedirectResponse("/settings?flash=scan-now", status_code=303)


@router.post("/settings/reindex", response_class=HTMLResponse)
def settings_reindex(request: Request):
    denied = _guard_write(request)
    if denied is not None:
        return denied
    ok = False
    error = ""
    result: dict[str, Any] = {}
    try:
        result = _obsidian_fn("reindex")() or {}
        ok = bool(_field(result, "ok", default=True))
    except NotImplementedError as exc:
        error = str(exc)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("Obsidian 索引失败")

    outcome = {"ok": ok, "error": error, "result": result, "at": timeutil.now_iso()}
    try:
        S.set(LAST_REINDEX_KEY, json.dumps(outcome, ensure_ascii=False))
    except Exception:
        logger.exception("记录索引结果失败")

    if _is_htmx(request):
        return templating.render(request, "partials/settings_reindex_result.html", outcome=outcome)
    return RedirectResponse("/settings?flash=reindex", status_code=303)


# --------------------------------------------------------------------------
# 知识库目录（SPEC 14.4 页面路由）
# --------------------------------------------------------------------------
@router.post("/settings/vault", response_class=HTMLResponse)
def settings_vault(request: Request, path: str | None = Form(None)):
    denied = _guard_write(request)
    if denied is not None:
        return denied
    rel = (path or "").strip().strip("/")
    try:
        result = _obsidian_fn("set_vault_path")(rel) or {}
    except NotImplementedError as exc:
        return _unavailable(request, str(exc), card="vault", browse_rel=rel)
    except Exception as exc:
        logger.exception("设置知识库目录失败：%r", rel)
        text = f"设置知识库失败：{type(exc).__name__}: {exc}"
        if _is_htmx(request):
            return _vault_card(request, notice=text, notice_kind="danger", browse_rel=rel)
        return _render_page(request, error=text)

    ok = bool(_field(result, "ok", default=True))
    message = str(_field(result, "message", "error", default="") or "")
    if not ok:
        text = f"设置知识库失败：{message or '未知原因'}"
        if _is_htmx(request):
            return _vault_card(request, notice=text, notice_kind="danger", browse_rel=rel)
        return _render_page(request, error=text)

    notice = message or FLASH_MESSAGES["vault"]
    if _is_htmx(request):
        return _vault_card(request, notice=notice, notice_kind="success", browse_rel=rel)
    return RedirectResponse("/settings?flash=vault", status_code=303)


@router.get("/settings/browse", response_class=HTMLResponse)
def settings_browse(request: Request, path: str = ""):
    denied = _guard_write(request)
    if denied is not None:
        return denied
    rel = (path or "").strip()
    if _is_htmx(request):
        return _vault_browser(request, rel)
    return RedirectResponse(f"/settings?browse={quote(rel)}", status_code=303)
