"""Jinja2 环境、过滤器与渲染入口（SPEC.md 第 5 / 7 节）。

所有页面统一走 render(request, "x.html", **ctx)：本模块自动注入底部状态条
所需的公共上下文（today_cn / llm_mock / auth_enabled / obsidian_available），
页面模板不必重复传参。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import markdown as markdown_lib
from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app import timeutil
from app.config import settings

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
MARKDOWN_EXTENSIONS = ["tables", "fenced_code"]

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

STATUS_LABELS: dict[str, str] = {
    "todo": "待办",
    "doing": "进行中",
    "done": "已完成",
    "cancelled": "已取消",
}
PRIORITY_LABELS: dict[int, str] = {1: "低", 2: "中", 3: "高"}


def md(value: Any) -> Markup:
    """Markdown -> HTML（支持表格与围栏代码块），结果标记为安全。"""
    if not value:
        return Markup("")
    return Markup(markdown_lib.markdown(str(value), extensions=MARKDOWN_EXTENSIONS))


def fmt_cn(value: Any) -> str:
    """06-04 周三。"""
    return timeutil.fmt_cn(value)


def status_label(value: Any) -> str:
    key = str(value or "").strip()
    return STATUS_LABELS.get(key, key)


def priority_label(value: Any) -> str:
    try:
        return PRIORITY_LABELS[int(value)]
    except (TypeError, ValueError, KeyError):
        return str(value or "")


def settings_get(key: str, default: str = "") -> str:
    """模板里读 settings 表：{{ settings_get('webhook_format') }}。"""
    from app.services import settings_store

    try:
        return settings_store.get(key, default)
    except Exception:  # 库尚未就绪时不要让模板渲染 500
        logger.warning("读取 settings 键 %s 失败", key, exc_info=True)
        return default


SIDEBAR_ZERO: dict[str, int] = {"overdue": 0, "today": 0, "routine_open": 0}


def _count(sql: str, params: tuple = ()) -> int:
    from app import db

    row = db.one(sql, params)
    return int(row["n"]) if row is not None else 0


def sidebar_stats(request) -> dict[str, Any]:
    """侧边栏快捷统计：逾期 / 今日 / 例行未完成 + 当前用户（布局改造）。

    归属走 auth.effective_owner_id（None = 开放/单口令模式，不过滤；管理员 ?as= 生效）。
    任何异常都退化为 0，绝不让布局因为统计失败而 500。
    """
    stats: dict[str, Any] = {**SIDEBAR_ZERO, "user": None}
    from app import auth

    try:
        stats["user"] = auth.current_user(request)
        owner_id = auth.effective_owner_id(request, stats["user"])
        today = timeutil.today().isoformat()

        clause, extra = "", ()
        if owner_id is not None:
            clause, extra = " AND t.owner_id = ?", (int(owner_id),)

        active = " FROM tasks t WHERE t.archived = 0 AND t.status IN ('todo', 'doing')"
        stats["overdue"] = _count(
            "SELECT COUNT(*) AS n"
            + active
            + " AND t.due_date IS NOT NULL AND t.due_date < ?"
            + clause,
            (today, *extra),
        )
        stats["today"] = _count(
            "SELECT COUNT(*) AS n" + active + " AND t.due_date = ?" + clause,
            (today, *extra),
        )
        stats["routine_open"] = _count(
            "SELECT COUNT(*) AS n FROM tasks t JOIN routines r ON r.id = t.routine_id"
            " WHERE t.archived = 0 AND t.status IN ('todo', 'doing') AND r.active = 1"
            + clause,
            extra,
        )
    except Exception:
        logger.warning("侧边栏统计失败，退化为 0", exc_info=True)
        stats.update(SIDEBAR_ZERO)
    return stats


templates.env.filters["md"] = md
templates.env.filters["fmt_cn"] = fmt_cn
templates.env.filters["status_label"] = status_label
templates.env.filters["priority_label"] = priority_label
templates.env.globals["settings_get"] = settings_get
templates.env.globals["sidebar_stats"] = sidebar_stats
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["PRIORITY_LABELS"] = PRIORITY_LABELS


def status_context() -> dict[str, Any]:
    """底部状态条数据；下游域未实现（NotImplementedError）时退化为 env 推断值。"""
    from app.services import llm, obsidian

    mock = settings.llm_mock
    try:
        mock = llm.is_mock_mode()
    except NotImplementedError:
        pass
    except Exception:
        logger.debug("llm.is_mock_mode 调用失败，沿用 env 推断", exc_info=True)

    vault = settings.vault_exists
    try:
        vault = obsidian.vault_available()
    except NotImplementedError:
        pass
    except Exception:
        logger.debug("obsidian.vault_available 调用失败，沿用目录探测", exc_info=True)

    return {
        "today_cn": timeutil.fmt_cn(timeutil.today()),
        "llm_mock": bool(mock),
        "auth_enabled": settings.auth_enabled,
        "obsidian_available": bool(vault),
    }


def render(request: Request, name: str, *, status_code: int = 200, **context: Any):
    """渲染模板并注入公共上下文。"""
    ctx: dict[str, Any] = status_context()
    ctx.update(context)
    ctx["request"] = request
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
