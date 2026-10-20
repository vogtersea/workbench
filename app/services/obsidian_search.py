"""Obsidian 检索层：FTS5 子串检索、LIKE 回退、片段高亮与报告用的上下文拼装。

职责边界（W-08）：只读 notes_index 与 notes_fts，不遍历文件、不写索引。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from markupsafe import Markup, escape

from app import db
from app.services import settings_store

from app.services.obsidian_vault import logger

# 命中位置前后各取 60 字 -> 约 120 字上下文。
SNIPPET_RADIUS = 60


MAX_LIMIT = 50


MAX_QUERY = 200


CONTEXT_HEADER = "## 相关知识库笔记（仅作背景参考，不得据此编造事实）"


_SPACE_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #
def _fts_tokenizer() -> str:
    try:
        value = settings_store.get("fts_tokenizer", "trigram").strip()
        return value or "trigram"
    except Exception:
        return "trigram"


def _normalize(q: str, limit: int) -> tuple[str, int]:
    query = (q or "").strip()[:MAX_QUERY]
    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = 5
    return query, max(1, min(count, MAX_LIMIT))


def _fts_candidates(query: str, want: int) -> list[str]:
    """FTS5 子串候选；分词器不支持 / 查询出错时返回空列表交给 LIKE 回退。"""
    match = '"' + query.replace('"', '""') + '"'
    try:
        rows = db.query(
            "SELECT path, bm25(notes_fts) AS rank FROM notes_fts "
            "WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, want),
        )
    except Exception:
        logger.warning("FTS 查询失败，降级为 LIKE 回退：%r", query, exc_info=True)
        return []
    return [row["path"] for row in rows]


def _like_candidates(query: str, want: int) -> list[str]:
    like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        rows = db.query(
            "SELECT path FROM notes_index "
            "WHERE title LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' "
            "OR body LIKE ? ESCAPE '\\' "
            "ORDER BY (CASE WHEN title LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END) ASC, "
            "length(body) ASC LIMIT ?",
            (like, like, like, like, want),
        )
    except Exception:
        logger.warning("LIKE 回退查询失败：%r", query, exc_info=True)
        return []
    return [row["path"] for row in rows]


def _load_rows(paths: list[str]) -> dict[str, Any]:
    if not paths:
        return {}
    placeholders = ",".join("?" * len(paths))
    try:
        rows = db.query(
            "SELECT path, title, tags, frontmatter, body FROM notes_index "
            f"WHERE path IN ({placeholders})",
            tuple(paths),
        )
    except Exception:
        logger.warning("读取笔记正文失败", exc_info=True)
        return {}
    return {row["path"]: row for row in rows}


def _collapse(text: str) -> str:
    return _SPACE_RE.sub(" ", text or "").strip()


def _frontmatter_lines(raw_json: Any) -> int:
    try:
        return int(json.loads(raw_json or "{}").get("fm_lines") or 0)
    except Exception:
        return 0


def _make_snippet(body: str, title: str, query: str, fm_lines: int) -> tuple[str, int, int]:
    """返回 (纯文本摘要, 命中偏移, 文件内行号)。"""
    text = body or ""
    lowered = text.lower()
    needle = query.lower()
    offset = lowered.find(needle) if needle else -1
    if offset < 0:
        plain = _collapse(text)
        snippet = plain[: 2 * SNIPPET_RADIUS]
        if len(plain) > 2 * SNIPPET_RADIUS:
            snippet += "…"
        return snippet, -1, fm_lines + 1
    start = max(0, offset - SNIPPET_RADIUS)
    end = min(len(text), offset + len(query) + SNIPPET_RADIUS)
    snippet = _collapse(text[start:end])
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    line = text.count("\n", 0, offset) + 1 + fm_lines
    return snippet, offset, line


def _highlight(snippet: str, query: str) -> Markup:
    """把摘要里的命中词包成 <mark>（其余内容做 HTML 转义）。"""
    if not snippet:
        return Markup("")
    needle = (query or "").strip()
    if not needle:
        return escape(snippet)
    lowered = snippet.lower()
    needle_lower = needle.lower()
    parts: list[Markup] = []
    cursor = 0
    while True:
        found = lowered.find(needle_lower, cursor)
        if found < 0:
            parts.append(escape(snippet[cursor:]))
            break
        parts.append(escape(snippet[cursor:found]))
        parts.append(
            Markup('<mark class="obs-mark">')
            + escape(snippet[found : found + len(needle)])
            + Markup("</mark>")
        )
        cursor = found + len(needle)
    return Markup("").join(parts)


def _score(title: str, tags: str, body: str, query: str) -> float:
    """越相关分越高：标题命中 3 分、标签命中 1 分，正文命中按次数与位置加权。"""
    needle = query.lower()
    score = 0.0
    if needle and needle in (title or "").lower():
        score += 3.0
    if needle and needle in (tags or "").lower():
        score += 1.0
    lowered = (body or "").lower()
    if needle:
        score += min(lowered.count(needle), 20) * 0.2
        first = lowered.find(needle)
        if first >= 0:
            score += 1.0 / (1.0 + first / 200.0)
    return round(score, 4)


def search(q: str, limit: int = 5) -> list[dict]:
    """全文检索：>=3 字走 FTS5（trigram），1-2 字或 FTS 无结果走 LIKE 回退。

    返回 [{"path","title","score","snippet","snippet_html","line","offset","tags"}]，
    score 越大越相关；异常时返回空列表（降级，不抛 500）。
    """
    query, count = _normalize(q, limit)
    if not query:
        return []
    try:
        candidates: list[str] = []
        if len(query) >= 3:
            candidates = _fts_candidates(query, count * 4)
        if not candidates:
            candidates = _like_candidates(query, count * 4)
        rows = _load_rows(candidates)
        hits: list[dict] = []
        for path in candidates:
            row = rows.get(path)
            if row is None:
                continue
            fm_lines = _frontmatter_lines(row["frontmatter"])
            snippet, offset, line = _make_snippet(
                row["body"] or "", row["title"] or "", query, fm_lines
            )
            hits.append(
                {
                    "path": path,
                    "title": row["title"] or Path(path).stem,
                    "score": _score(
                        row["title"] or "", row["tags"] or "", row["body"] or "", query
                    ),
                    "snippet": snippet,
                    "snippet_html": _highlight(snippet, query),
                    "line": line,
                    "offset": offset,
                    "tags": [tag for tag in (row["tags"] or "").split(",") if tag],
                }
            )
        hits.sort(key=lambda hit: (-hit["score"], hit["path"]))
        return hits[:count]
    except Exception:
        logger.exception("Obsidian 检索失败，降级为空结果：%r", query)
        return []


def build_context(
    keywords: list[str], limit: int = 5, budget: int = 2000
) -> tuple[str, list[dict]]:
    """把多个关键词的检索结果拼成受字数上限约束的 Markdown 背景片段。"""
    words = [str(word).strip() for word in (keywords or []) if str(word or "").strip()]
    if not words:
        return "", []
    _, count = _normalize("", limit)
    try:
        budget = max(0, int(budget))
    except (TypeError, ValueError):
        budget = 2000
    if budget <= len(CONTEXT_HEADER):
        return "", []

    best: dict[str, dict] = {}
    for word in words:
        for hit in search(word, count):
            previous = best.get(hit["path"])
            if previous is None or hit["score"] > previous["score"]:
                best[hit["path"]] = hit
    if not best:
        return "", []

    ranked = sorted(best.values(), key=lambda hit: (-hit["score"], hit["path"]))[:count]
    lines = [CONTEXT_HEADER]
    used = len(CONTEXT_HEADER)
    included: list[dict] = []
    for hit in ranked:
        prefix = "- " + hit["path"] + " — " + hit["title"] + "\n  "
        abstract = _collapse(hit.get("snippet", "")) or Path(hit["path"]).stem
        room = budget - used - 1 - len(prefix)
        if room < 24:
            break
        if len(abstract) > room:
            abstract = abstract[: max(0, room - 1)].rstrip() + "…"
        block = prefix + abstract
        lines.append(block)
        used += 1 + len(block)
        included.append(hit)
    if not included:
        return "", []
    return "\n".join(lines), included
