"""Obsidian 索引层：frontmatter、标签、双链解析与全量重建（notes_index + notes_fts）。

职责边界（W-08）：把笔记文件解析成索引行并落库；目录遍历委托 obsidian_vault，
分词器探测用检索层 obsidian_search。"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from app import db, timeutil
from app.services import settings_store

from app.services.obsidian_search import _fts_tokenizer
from app.services.obsidian_vault import (
    _iter_notes,
    _root_resolved,
    current_rel,
    logger,
    vault_path,
    vault_root,
)

# 用逗号分隔的 frontmatter 键（Obsidian 里 tags / aliases 既可是列表也可是行内逗号写法）。
LIST_KEYS = frozenset({"tags", "tag", "aliases", "alias", "cssclasses"})


_FENCE = re.compile(r"^---\s*$")


# \x60 即反引号：正文里的行内代码标记不能算作标签字符。
_TAG_RE = re.compile(
    r"(?<![\w#/&])#([0-9A-Za-z_\u4e00-\u9fff][^\s#\[\]()（）【】，。；、,.;:!?\"'\x60]*)"
)


_LINK_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")


_CODE_FENCE_RE = re.compile(r"\x60\x60\x60.*?\x60\x60\x60|~~~.*?~~~", re.S)


_UPSERT_NOTE = (
    "INSERT INTO notes_index(path, title, tags, frontmatter, body, mtime, indexed_at) "
    "VALUES(?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(path) DO UPDATE SET title = excluded.title, tags = excluded.tags, "
    "frontmatter = excluded.frontmatter, body = excluded.body, mtime = excluded.mtime, "
    "indexed_at = excluded.indexed_at"
)


_DELETE_FTS = "DELETE FROM notes_fts WHERE path = ?"


_INSERT_FTS = "INSERT INTO notes_fts(path, title, tags, body) VALUES(?, ?, ?, ?)"


# --------------------------------------------------------------------------- #
# 解析：frontmatter / 标题 / 标签 / 双链
# --------------------------------------------------------------------------- #
def _split_frontmatter(text: str) -> tuple[str, str, int]:
    """切出 (frontmatter 原文, 正文, frontmatter 占用的行数)。"""
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or not _FENCE.match(lines[0].strip()):
        return "", text, 0
    for index in range(1, len(lines)):
        if _FENCE.match(lines[index].strip()):
            raw = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            return raw, body, index + 1
    return "", text, 0


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def _parse_value(key: str, value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        parts = [_unquote(part) for part in value[1:-1].split(",")]
        return [part for part in parts if part]
    if key.lower() in LIST_KEYS and ("," in value or "，" in value):
        return [part for part in (_unquote(p) for p in re.split(r"[,，]", value)) if part]
    return _unquote(value)


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    """手写极简 YAML：key: value / [a, b] 行内列表 / 紧随其后的 - 块列表。"""
    fields: dict[str, Any] = {}
    pending: str | None = None
    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") or stripped == "-":
            if pending is None:
                continue
            item = _unquote(stripped[1:].strip())
            if not item:
                continue
            current = fields.get(pending)
            if isinstance(current, list):
                current.append(item)
            else:
                fields[pending] = ([current] if current else []) + [item]
            continue
        key, sep, value = stripped.partition(":")
        if not sep or not key.strip():
            continue
        key = key.strip()
        value = value.strip()
        if not value:
            pending = key
            fields.setdefault(key, "")
        else:
            pending = None
            fields[key] = _parse_value(key, value)
    return fields


def _strip_code(text: str) -> str:
    """去掉围栏代码块：代码里的 # 与 [[ ]] 不算标签/双链。"""
    return _CODE_FENCE_RE.sub(" ", text)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [part for part in re.split(r"[,，\s]+", text) if part]


def _extract_tags(fields: dict[str, Any], body: str) -> list[str]:
    tags: list[str] = []
    for key in ("tags", "tag"):
        if key in fields:
            tags.extend(_as_list(fields[key]))
    for match in _TAG_RE.finditer(_strip_code(body)):
        tags.append(match.group(1).rstrip("/"))
    seen: dict[str, None] = {}
    for tag in tags:
        tag = tag.strip().strip(",")
        if tag:
            seen.setdefault(tag.replace(" ", "-"), None)
    return list(seen)


def _extract_links(body: str) -> tuple[list[str], list[str]]:
    """返回 (全部双链目标, 其中 ![[嵌入]] 的目标)。"""
    links: dict[str, None] = {}
    embeds: dict[str, None] = {}
    for match in _LINK_RE.finditer(_strip_code(body)):
        target = match.group(2).split("|")[0].split("#")[0].strip().strip("/")
        if not target:
            continue
        links.setdefault(target, None)
        if match.group(1) == "!":
            embeds.setdefault(target, None)
    return list(links), list(embeds)


def _heading_title(body: str) -> str:
    for line in _strip_code(body).split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            title = re.sub(r"\s+#+\s*$", "", stripped[2:].strip()).strip()
            if title:
                return title
    return ""


def _parse_note(rel_path: str, text: str) -> dict[str, Any]:
    """把一个 .md 文件解析成 notes_index 行所需的全部字段。"""
    raw, body, header_lines = _split_frontmatter(text)
    fields = _parse_frontmatter(raw)
    links, embeds = _extract_links(body)
    tags = _extract_tags(fields, body)
    title = ""
    if "title" in fields:
        candidates = _as_list(fields["title"])
        title = candidates[0] if candidates else ""
    if not title:
        title = _heading_title(body)
    if not title:
        title = Path(rel_path).stem
    payload = {
        "raw": raw,
        "fields": fields,
        "links": links,
        "embeds": embeds,
        "fm_lines": header_lines,
    }
    return {
        "path": rel_path,
        "title": title,
        "tags": ",".join(tags),
        "tags_list": tags,
        "frontmatter": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        # 正文按原样保存：保留 frontmatter 之后所有换行，行号 = fm_lines + 正文行号。
        "body": body,
        "fm_lines": header_lines,
    }


# --------------------------------------------------------------------------- #
# 建索引
# --------------------------------------------------------------------------- #
def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _empty_result(message: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "scanned": 0,
        "indexed": 0,
        "skipped": 0,
        "removed": 0,
        "errors": 0,
        "elapsed_ms": 0,
        "message": message,
    }


def reindex() -> dict:
    """增量重建 vault 索引。

    返回 {"scanned", "indexed", "skipped", "errors", "elapsed_ms"}（另附 ok/removed/message）。
    mtime 未变且 notes_fts 已有对应行的笔记直接跳过；已删除文件的索引行与 FTS 行一并清理。
    """
    started = time.perf_counter()
    selected = vault_path()
    if not selected:
        result = _empty_result(
            "尚未选择知识库目录：请先在「设置 → 知识库」里选择 vault 子目录，再重建索引"
        )
        result["elapsed_ms"] = int(round((time.perf_counter() - started) * 1000))
        return result

    root = Path(selected).resolve()
    files = _iter_notes(root)
    existing: dict[str, float] = {}
    fts_paths: set[str] = set()
    try:
        existing = {
            row["path"]: float(row["mtime"] or 0)
            for row in db.query("SELECT path, mtime FROM notes_index")
        }
        fts_paths = {row["path"] for row in db.query("SELECT path FROM notes_fts")}
    except Exception:  # 库尚未建表等情况：按全量索引处理
        logger.warning("读取既有索引失败，本次按全量索引处理", exc_info=True)

    stats = {"scanned": len(files), "indexed": 0, "skipped": 0, "removed": 0, "errors": 0}
    indexed_at = timeutil.now_iso()
    seen: set[str] = set()

    with db.cursor() as conn:
        for path in files:
            rel = _rel(root, path)
            seen.add(rel)
            try:
                mtime = float(path.stat().st_mtime)
            except OSError:
                stats["errors"] += 1
                logger.warning("读取笔记状态失败：%s", path, exc_info=True)
                continue
            if (
                existing.get(rel) is not None
                and abs(existing[rel] - mtime) <= 0.001
                and rel in fts_paths
            ):
                stats["skipped"] += 1
                continue
            try:
                note = _parse_note(rel, path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                stats["errors"] += 1
                logger.warning("解析笔记失败：%s", path, exc_info=True)
                continue
            conn.execute(
                _UPSERT_NOTE,
                (
                    note["path"],
                    note["title"],
                    note["tags"],
                    note["frontmatter"],
                    note["body"],
                    mtime,
                    indexed_at,
                ),
            )
            conn.execute(_DELETE_FTS, (rel,))
            conn.execute(_INSERT_FTS, (rel, note["title"], note["tags"], note["body"]))
            stats["indexed"] += 1

        for rel in [path for path in existing if path not in seen]:
            conn.execute("DELETE FROM notes_index WHERE path = ?", (rel,))
            conn.execute(_DELETE_FTS, (rel,))
            stats["removed"] += 1

        # FTS 与主表对齐：清掉任何孤儿行（补漏「先 DELETE 同 path 再 INSERT」之外的残留）。
        conn.execute("DELETE FROM notes_fts WHERE path NOT IN (SELECT path FROM notes_index)")

    result: dict[str, Any] = {"ok": True}
    result.update(stats)
    result["elapsed_ms"] = int(round((time.perf_counter() - started) * 1000))
    result["message"] = (
        f"扫描 {stats['scanned']} 篇，更新 {stats['indexed']} 篇，"
        f"跳过 {stats['skipped']} 篇，清理 {stats['removed']} 篇，失败 {stats['errors']} 篇"
    )
    try:
        settings_store.set("obsidian_last_index", indexed_at)
        settings_store.set("obsidian_enabled", "1")
    except Exception:
        logger.warning("写入 obsidian 索引状态失败", exc_info=True)
    return result


# --------------------------------------------------------------------------- #
# 页面用的状态快照
# --------------------------------------------------------------------------- #
def stats() -> dict[str, Any]:
    """状态卡数据：vault 路径 / 笔记数 / 上次索引时间 / 分词器。永不抛异常。"""
    chosen = vault_path()
    info: dict[str, Any] = {
        "available": chosen is not None,
        "vault": chosen or vault_root(),
        "vault_resolved": chosen or "",
        "root": vault_root(),
        "rel": current_rel(),
        "root_available": _root_resolved() is not None,
        "notes": 0,
        "fts_rows": 0,
        "indexed_at": "",
        "last_index": "",
        "tokenizer": _fts_tokenizer(),
        "enabled": False,
    }
    try:
        row = db.one("SELECT COUNT(*) AS n, MAX(indexed_at) AS ts FROM notes_index")
        if row is not None:
            info["notes"] = int(row["n"] or 0)
            info["indexed_at"] = row["ts"] or ""
        fts_row = db.one("SELECT COUNT(*) AS n FROM notes_fts")
        if fts_row is not None:
            info["fts_rows"] = int(fts_row["n"] or 0)
    except Exception:
        logger.warning("读取 Obsidian 索引统计失败", exc_info=True)
    try:
        info["last_index"] = settings_store.get("obsidian_last_index", "")
        info["enabled"] = settings_store.get("obsidian_enabled", "0") == "1"
    except Exception:
        logger.debug("读取 obsidian settings 失败", exc_info=True)
    return info


def indexed_note_count() -> int:
    """notes_index 行数（页面 / 报告域可复用）。"""
    try:
        row = db.one("SELECT COUNT(*) AS n FROM notes_index")
        return int(row["n"] or 0) if row is not None else 0
    except Exception:
        return 0
