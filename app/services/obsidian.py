"""Obsidian 只读索引与检索（SPEC.md 第 5 节）。

硬约束：
- 只读：本模块只对 vault 内文件做 stat / read，绝不写入、移动、删除 vault 内任何文件；
- 索引落在 notes_index（元数据 + 正文）与 notes_fts（trigram FTS5），单个事务批量提交；
- 检索优先 FTS5 子串匹配（>=3 字），1-2 字中文短词退化为 notes_index 的 LIKE 回退；
- 任何检索/索引异常都降级为空结果并记日志，绝不把 500 抛给界面。

目录选择（SPEC.md 14.2，前端可配置化）：
- vault_root() 是容器里的只读挂载根（env VAULT_ROOT，默认 /hostvaults）；
- vault_path() 是用户在网页里选中的子目录（settings.vault_path，相对根目录的 posix 路径），
  未选择时为空串 -> vault_path() 返回 None、vault_available() 为 False、reindex 返回 ok=false；
- 根目录本身用 "." 表示；空字符串按 SPEC 14.3 的「空=未选择」语义表示清空选择。

notes_index.frontmatter 存 JSON（schema 冻结，不能加列）：
    {"raw": frontmatter 原文, "fields": {k: v}, "links": [..], "embeds": [..], "fm_lines": n}
links / embeds 是 [[双链]] 与 ![[嵌入]] 的目标名（去掉 |别名 与 #标题 之后）。
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import time
from pathlib import Path
from typing import Any

from markupsafe import Markup, escape

from app import db, timeutil
from app.services import settings_store

logger = logging.getLogger(__name__)

# 这些目录不是笔记正文，递归时直接跳过（Obsidian 自身元数据 / 版本库 / 回收站）。
SKIP_DIRS = frozenset(
    {".obsidian", ".git", ".hg", ".svn", ".trash", "__pycache__", "node_modules"}
)
# 命中位置前后各取 60 字 -> 约 120 字上下文。
SNIPPET_RADIUS = 60
MAX_LIMIT = 50
MAX_QUERY = 200
# 用逗号分隔的 frontmatter 键（Obsidian 里 tags / aliases 既可是列表也可是行内逗号写法）。
LIST_KEYS = frozenset({"tags", "tag", "aliases", "alias", "cssclasses"})
CONTEXT_HEADER = "## 相关知识库笔记（仅作背景参考，不得据此编造事实）"
# 只读挂载根（SPEC 14.2）：env VAULT_ROOT，默认 /hostvaults。
VAULT_ROOT_DEFAULT = "/hostvaults"
# settings 保存「当前选中的相对子路径」；空串 = 未选择（SPEC 14.3）。
VAULT_REL_KEY = "vault_path"
# 单目录 md 计数上限：超大库也不能把浏览接口拖到超时。
DIR_SCAN_LIMIT = 5000
COUNT_CACHE_TTL = 30.0
COUNT_CACHE_MAX = 512
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
# 目录 md 计数缓存：绝对路径 -> (失效时刻, 计数)；超上限整体清空，避免无界增长。
_count_cache: dict[str, tuple[float, int]] = {}

_FENCE = re.compile(r"^---\s*$")
# \x60 即反引号：正文里的行内代码标记不能算作标签字符。
_TAG_RE = re.compile(
    r"(?<![\w#/&])#([0-9A-Za-z_\u4e00-\u9fff][^\s#\[\]()（）【】，。；、,.;:!?\"'\x60]*)"
)
_LINK_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
_CODE_FENCE_RE = re.compile(r"\x60\x60\x60.*?\x60\x60\x60|~~~.*?~~~", re.S)
_SPACE_RE = re.compile(r"\s+")

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
# vault 根目录 / 当前选择（SPEC.md 14.2）
# --------------------------------------------------------------------------- #
def vault_root() -> str:
    """只读挂载根：env VAULT_ROOT，默认 /hostvaults；目录不存在也原样返回。"""
    raw = (os.getenv("VAULT_ROOT") or "").strip()
    return str(Path(raw or VAULT_ROOT_DEFAULT).expanduser())


def _root_resolved() -> Path | None:
    """根目录 resolve 后的绝对路径；不存在 / 权限异常返回 None。"""
    try:
        root = Path(vault_root())
        return root.resolve() if root.is_dir() else None
    except OSError:
        logger.warning("vault 根目录探测失败：%s", vault_root(), exc_info=True)
        return None


def _norm_rel(text: str) -> str:
    """规范化相对路径：统一分隔符、折叠 . 与 a/../b；根目录本身返回空串。"""
    if not text:
        return ""
    cleaned = posixpath.normpath(text)
    return "" if cleaned == "." else cleaned.lstrip("/")


def _rel_of(target: Path) -> str:
    """绝对路径 -> 相对根目录的 posix 路径（根目录本身返回空串）。"""
    root = _root_resolved()
    if root is None:
        return ""
    try:
        return _norm_rel(target.relative_to(root).as_posix())
    except ValueError:
        return ""


def _resolve_within(rel: Any) -> tuple[Path | None, str]:
    """把相对路径解析成根目录内的绝对路径。

    返回 (绝对路径, 空串) 或 (None, 可读中文原因)；原因里绝不回显宿主绝对路径。
    绝对路径、盘符、.. 逃逸与 symlink 逃逸（resolve 之后再校验）一律拒绝。
    """
    root = _root_resolved()
    if root is None:
        return None, "知识库根目录不可用：请确认已把目录只读挂载到 VAULT_ROOT（默认 /hostvaults）"
    text = str(rel if rel is not None else "").strip().replace("\\", "/")
    if "\x00" in text:
        return None, "路径含非法字符"
    if text.startswith("/") or _DRIVE_RE.match(text):
        return None, "只接受相对知识库根目录的子路径，不接受绝对路径"
    cleaned = _norm_rel(text)
    if cleaned == ".." or cleaned.startswith("../"):
        return None, "路径超出知识库根目录，已拒绝"
    try:
        candidate = (root / cleaned).resolve()
    except OSError:
        return None, "路径无法解析"
    if not _is_within(candidate, root):
        return None, "路径超出知识库根目录，已拒绝"
    return candidate, ""


def _stored_rel() -> str:
    """settings 里保存的原始相对路径（"." = 根目录本身，空串 = 未选择）。"""
    try:
        return str(settings_store.get(VAULT_REL_KEY, "") or "").strip()
    except Exception:
        logger.debug("读取 vault_path 设置失败", exc_info=True)
        return ""


def current_rel() -> str:
    """当前相对路径：未选择返回空串，根目录本身返回 "."。"""
    raw = _stored_rel()
    if not raw:
        return ""
    return posixpath.normpath(raw.replace("\\", "/"))


def vault_path() -> str | None:
    """当前选中的知识库绝对路径；未选择 / 不存在 / 不可读一律 None。"""
    rel = _stored_rel()
    if not rel:
        return None
    target, _reason = _resolve_within(rel)
    if target is None:
        return None
    try:
        if not target.is_dir() or not os.access(target, os.R_OK | os.X_OK):
            return None
    except OSError:
        return None
    return str(target)


def vault_available() -> bool:
    """已选择知识库且目录可读时为 True（页面状态条、设置页都读它）。"""
    return vault_path() is not None


def _vault_state(*, ok: bool, message: str) -> dict[str, Any]:
    """设置/清空知识库后的统一返回体。

    失败时只回相对信息：越界请求绝不回显宿主绝对路径（成功时才给出 abs / root）。
    """
    chosen = vault_path()
    state: dict[str, Any] = {
        "ok": ok,
        "message": message,
        "path": current_rel(),
        "available": chosen is not None,
    }
    if ok:
        state["abs"] = chosen or ""
        state["root"] = vault_root()
    return state


def set_vault_path(rel: Any) -> dict[str, Any]:
    """校验并保存当前知识库目录（相对根目录的子路径，空串=清空选择）。

    只做 listdir/stat 只读校验，绝不写入 vault；越界/不存在/不可读都返回 ok=False + 可读原因。
    """
    text = str(rel if rel is not None else "").strip()
    if not text:
        try:
            settings_store.set(VAULT_REL_KEY, "")
        except Exception:
            logger.exception("清空 vault_path 设置失败")
            return _vault_state(ok=False, message="保存失败：数据库写入异常，请查看服务端日志")
        return _vault_state(ok=True, message="已清空知识库选择（未选择时索引与检索不可用）")

    target, reason = _resolve_within(text)
    if target is None:
        return _vault_state(ok=False, message=reason)
    try:
        if not target.is_dir():
            return _vault_state(ok=False, message="该路径不是目录，请选择知识库所在的目录")
        os.listdir(target)  # 只读校验：确认目录可枚举
    except OSError:
        return _vault_state(ok=False, message="目录不可读，请检查只读挂载权限")
    stored = _rel_of(target) or "."  # 根目录本身用 "." 表示
    try:
        settings_store.set(VAULT_REL_KEY, stored)
    except Exception:
        logger.exception("保存 vault_path 设置失败")
        return _vault_state(ok=False, message="保存失败：数据库写入异常，请查看服务端日志")
    label = stored if stored != "." else "（根目录）"
    return _vault_state(ok=True, message=f"已切换知识库目录：{label}")


def _dir_within(path: Path, allowed: Path | None) -> bool:
    """目录自身 resolve 后是否仍在知识库根目录内（挡住 symlink / junction 逃逸）。"""
    if allowed is None:
        return True
    try:
        return _is_within(path.resolve(), allowed)
    except OSError:
        return False


def _count_md(directory: Path) -> int:
    """递归统计 .md 数量；单目录最多扫 DIR_SCAN_LIMIT 个文件，结果缓存 COUNT_CACHE_TTL 秒。"""
    key = str(directory)
    now = time.monotonic()
    cached = _count_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    count = 0
    scanned = 0
    stopped = False
    allowed = _root_resolved()
    try:
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not _skip_dir(name) and _dir_within(Path(dirpath) / name, allowed)
            )
            for name in filenames:
                scanned += 1
                if scanned > DIR_SCAN_LIMIT:
                    stopped = True
                    break
                if name.lower().endswith(".md"):
                    count += 1
            if stopped:
                break
    except OSError:
        logger.warning("统计 md 数量失败：%s", directory, exc_info=True)
    if stopped:
        logger.info("目录 %s 超过 %d 个文件，md 计数为上界值", directory, DIR_SCAN_LIMIT)
    if len(_count_cache) >= COUNT_CACHE_MAX:
        _count_cache.clear()
    _count_cache[key] = (now + COUNT_CACHE_TTL, count)
    return count


def browse(rel: Any = "") -> dict[str, Any]:
    """列出当前目录的一级子目录与各自 md 数量（SPEC 14.2）；异常降级为空列表，不抛 500。"""
    payload: dict[str, Any] = {
        "ok": True,
        "available": False,
        "root": vault_root(),
        "current": "",
        "abs": "",
        "parent": "",
        "dirs": [],
        "md_count": 0,
        "writable": False,
        "selected": current_rel(),
        "message": "",
    }
    try:
        target, reason = _resolve_within(rel)
        if target is None:
            # 越界请求不回显宿主路径：只给可读原因。
            payload.update(ok=False, available=vault_available(), message=reason, root="")
            return payload
        try:
            if not target.is_dir():
                payload.update(ok=False, message="该路径不是目录")
                return payload
        except OSError:
            payload.update(ok=False, message="目录不可读，请检查只读挂载权限")
            return payload
        current = _rel_of(target)
        payload.update(available=True, current=current, abs=str(target))
        payload["parent"] = "" if not current else posixpath.dirname(current)
        try:
            payload["md_count"] = _count_md(target)
        except Exception:
            logger.warning("统计当前目录 md 数量失败：%s", target, exc_info=True)
        try:
            with os.scandir(target) as entries:
                candidates = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            logger.warning("目录不可读，无法列出子目录：%s", target, exc_info=True)
            payload["message"] = "目录不可读，无法列出子目录"
            return payload
        root = Path(payload["root"]).resolve()
        dirs: list[dict[str, Any]] = []
        for entry in candidates:
            if _skip_dir(entry.name):
                continue
            try:
                if not entry.is_dir():  # 跟随 symlink 之后仍须是目录
                    continue
                resolved = Path(entry.path).resolve()
            except OSError:
                logger.warning("子目录无法解析，已跳过：%s", entry.name, exc_info=True)
                continue
            if not _is_within(resolved, root):
                logger.warning("子目录经符号链接逃出知识库根目录，已跳过：%s", entry.name)
                continue
            dirs.append(
                {"name": entry.name, "rel": _rel_of(resolved), "md_count": _count_md(resolved)}
            )
        payload["dirs"] = dirs
        if not dirs and not payload["message"]:
            payload["message"] = "该目录下没有可浏览的子目录"
        return payload
    except Exception:
        logger.exception("目录浏览失败，降级为空列表：%r", rel)
        payload.update(ok=False, dirs=[], md_count=0, message="目录浏览失败，已降级为空列表")
        return payload


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def _iter_notes(root: Path) -> list[Path]:
    """递归收集 vault 内 .md 文件（resolve 后仍在 vault 内，符号链接逃逸一律忽略）。"""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not _skip_dir(name))
        for name in sorted(filenames):
            if not name.lower().endswith(".md"):
                continue
            path = Path(dirpath) / name
            try:
                resolved = path.resolve()
            except OSError:
                logger.warning("解析笔记路径失败，已跳过：%s", path)
                continue
            if not _is_within(resolved, root):
                logger.warning("笔记经符号链接逃出 vault，已跳过：%s", path)
                continue
            if not resolved.is_file():
                continue
            files.append(resolved)
    return files


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
