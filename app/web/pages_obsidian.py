"""Obsidian 页面（SPEC.md 第 6 节 + 本域任务书）。

只暴露 pages = APIRouter()，路径即页面路径（main.py 不加前缀包含）：
    GET  /obsidian?q=        面板：状态卡 / 重建索引 / 搜索
    POST /obsidian/reindex   重建索引（htmx 返回片段，普通表单 303 回带统计）
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import templating
from app.services import obsidian

logger = logging.getLogger(__name__)

pages = APIRouter()

RESULT_KEYS = ("scanned", "indexed", "skipped", "removed", "errors", "elapsed_ms")


def _is_hx(request: Request) -> bool:
    """htmx 请求返回片段，普通表单请求 303 回页面。"""
    return (request.headers.get("hx-request") or "").lower() == "true"


def _int(value: str | None) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _clamp_limit(value: int) -> int:
    try:
        return max(1, min(int(value), 50))
    except (TypeError, ValueError):
        return 10


@pages.get("/obsidian")
def obsidian_page(request: Request, q: str = "", limit: int = 10):
    stats = obsidian.stats()
    count = _clamp_limit(limit)
    query = (q or "").strip()[:200]

    params = request.query_params
    reindex_result = None
    if params.get("reindexed") == "1":
        reindex_result = {"ok": params.get("ok", "1") != "0", "message": ""}
        for key in RESULT_KEYS:
            reindex_result[key] = _int(params.get(key))

    hits: list[dict] = []
    search_error = ""
    if query:
        try:
            hits = obsidian.search(query, count)
        except Exception:
            logger.exception("页面检索失败：%r", query)
            search_error = "检索失败，请稍后重试或重建索引"

    return templating.render(
        request,
        "obsidian.html",
        stats=stats,
        q=query,
        limit=count,
        hits=hits,
        searched=bool(query),
        search_error=search_error,
        reindex_result=reindex_result,
    )


@pages.post("/obsidian/reindex")
def obsidian_reindex_page(request: Request, q: str = Form("")):
    query = (q or "").strip()[:200]
    try:
        result = obsidian.reindex()
    except Exception:
        logger.exception("页面重建 Obsidian 索引失败")
        result = {
            "ok": False,
            "scanned": 0,
            "indexed": 0,
            "skipped": 0,
            "removed": 0,
            "errors": 1,
            "elapsed_ms": 0,
            "message": "重建索引失败，请查看服务端日志",
        }

    if _is_hx(request):
        return templating.render(
            request,
            "partials/obsidian_status.html",
            stats=obsidian.stats(),
            q=query,
            reindex_result=result,
        )

    params: dict[str, str] = {"reindexed": "1", "ok": "1" if result.get("ok") else "0"}
    for key in RESULT_KEYS:
        params[key] = str(result.get(key, 0))
    if query:
        params["q"] = query
    return RedirectResponse("/obsidian?" + urlencode(params), status_code=303)
