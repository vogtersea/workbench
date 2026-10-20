"""Obsidian JSON API（SPEC.md 第 6 节 + 14.4 前端可配置化）。

只暴露 router = APIRouter()，模块内路径不含 /api 前缀（main.py 以 prefix="/api" 包含）：
    GET  /api/obsidian/search?q=&limit=   全文检索
    POST /api/obsidian/reindex            重建只读索引
    GET  /api/obsidian/browse?path=       目录浏览（14.4）
    POST /api/obsidian/vault              {"path": 子路径} 设置当前知识库（14.4）

约定：检索/浏览出错一律降级为 ok=false（不把 500 抛给调用方）；越界路径返回 400 + 可读原因，
绝不回显宿主绝对路径。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.services import obsidian

logger = logging.getLogger(__name__)

router = APIRouter()

# API 只输出纯文本字段：snippet_html 是给页面模板用的，不进 JSON。
RESULT_FIELDS = ("path", "title", "score", "snippet", "line", "tags")


def _plain(hit: dict) -> dict[str, Any]:
    return {key: hit.get(key) for key in RESULT_FIELDS}


async def _read_payload(request: Request) -> dict[str, Any]:
    """同时接受 JSON 与表单编码的请求体；解析失败按空负载处理（不抛 422）。"""
    content_type = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in content_type:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        form = await request.form()
        return {key: form[key] for key in form}
    except Exception:
        logger.warning("请求体解析失败，按空负载处理")
        return {}


@router.get("/obsidian/search")
def obsidian_search(
    q: str = Query("", description="检索关键词（超过 200 字自动截断）"),
    limit: int = Query(5, description="返回条数上限（自动夹到 1..50）"),
) -> Any:
    """全文检索：>=3 字走 FTS5，1-2 字走 LIKE 回退；越界参数夹紧而不是报 422。"""
    query = (q or "").strip()
    available = obsidian.vault_available()
    payload: dict[str, Any] = {
        "ok": True,
        "q": query,
        "available": available,
        "count": 0,
        "results": [],
        "message": "",
    }
    if not query:
        payload["message"] = "缺少 q 参数：用法 /api/obsidian/search?q=关键词"
        return payload
    try:
        hits = obsidian.search(query, limit)
    except Exception:
        logger.exception("Obsidian 检索接口失败：%r", query)
        payload["ok"] = False
        payload["message"] = "检索失败，已降级为空结果"
        return JSONResponse(payload, status_code=200)
    payload["results"] = [_plain(hit) for hit in hits]
    payload["count"] = len(hits)
    if not available:
        payload["message"] = (
            "尚未选择知识库目录，结果来自既有索引"
            if hits
            else "尚未选择知识库目录（或该目录不可读），暂不可检索；请先在设置页选择 vault 子目录并重建索引"
        )
    elif not hits:
        payload["message"] = "没有匹配的笔记；新笔记需先重建索引"
    return payload


@router.post("/obsidian/reindex")
def obsidian_reindex() -> Any:
    """重建只读索引；返回 scanned / indexed / skipped / errors / elapsed_ms。"""
    try:
        return obsidian.reindex()
    except Exception:
        logger.exception("重建 Obsidian 索引失败")
        return JSONResponse(
            {
                "ok": False,
                "scanned": 0,
                "indexed": 0,
                "skipped": 0,
                "removed": 0,
                "errors": 0,
                "elapsed_ms": 0,
                "message": "重建索引失败，请查看服务端日志",
            },
            status_code=200,
        )


@router.get("/obsidian/browse")
def obsidian_browse(
    path: str = Query("", description="相对知识库根目录的子路径，留空表示根目录"),
) -> Any:
    """目录浏览（SPEC 14.4）：返回一层子目录与各自 md 数量；越界路径 400 + 可读原因。"""
    try:
        payload = obsidian.browse(path)
    except Exception:
        logger.exception("Obsidian 目录浏览失败：%r", path)
        payload = {
            "ok": False,
            "available": False,
            "root": "",
            "current": "",
            "abs": "",
            "parent": "",
            "dirs": [],
            "md_count": 0,
            "writable": False,
            "selected": "",
            "message": "目录浏览失败，已降级为空列表，请查看服务端日志",
        }
    return JSONResponse(payload, status_code=200 if payload.get("ok") else 400)


@router.post("/obsidian/vault")
async def obsidian_set_vault(request: Request) -> Any:
    """设置当前知识库目录（SPEC 14.4）：body {"path": "子路径"}，空串表示清空选择。"""
    data = await _read_payload(request)
    raw = data.get("path", "")
    if raw is None:
        raw = ""
    if not isinstance(raw, (str, int, float)):
        return JSONResponse(
            {
                "ok": False,
                "message": "path 必须是相对知识库根目录的字符串子路径",
                "available": obsidian.vault_available(),
            },
            status_code=422,
        )
    try:
        result = obsidian.set_vault_path(str(raw))
    except Exception:
        logger.exception("设置知识库目录失败：%r", raw)
        result = {
            "ok": False,
            "message": "设置知识库目录失败，请查看服务端日志",
            "available": obsidian.vault_available(),
        }
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)
