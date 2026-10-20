"""Obsidian 知识库目录层：根目录解析、子目录选择、目录浏览与只读文件遍历。

职责边界（W-08）：只管路径与目录，不解析 Markdown、不做检索。
硬约束不变：只对 vault 内文件做 stat / read，绝不写入或删除 vault 内文件。"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import time
from pathlib import Path
from typing import Any

from app.services import settings_store

logger = logging.getLogger("app.services.obsidian")

# 这些目录不是笔记正文，递归时直接跳过（Obsidian 自身元数据 / 版本库 / 回收站）。
SKIP_DIRS = frozenset(
    {".obsidian", ".git", ".hg", ".svn", ".trash", "__pycache__", "node_modules"}
)


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
