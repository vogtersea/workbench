"""设置页知识库域：目录选择上下文、面包屑与知识库卡片渲染。

职责边界（W-08）：只拼知识库相关上下文与卡片，不注册路由（知识库目录的两条路由在
general.py，因为写操作要经过共享的权限与渲染骨架）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request

from app import templating
from app.config import settings
from app.services import settings_store as S

from .shared import _field, _load_reindex_result, _obsidian, _to_int, logger

# --------------------------------------------------------------------------
# 知识库卡片
# --------------------------------------------------------------------------
def _crumbs(rel: str) -> list[dict[str, str]]:
    crumbs = [{"name": "根目录", "rel": ""}]
    parts = [part for part in str(rel or "").strip("/").split("/") if part]
    for index, part in enumerate(parts):
        crumbs.append({"name": part, "rel": "/".join(parts[: index + 1])})
    return crumbs


def _rel_from_root(abs_path: str, root: str) -> str:
    """绝对路径 -> 相对挂载根（服务端只给绝对路径时用它还原面包屑）。"""
    if not abs_path:
        return ""
    try:
        return Path(abs_path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return ""


def _norm_browse(raw: Any) -> dict[str, Any]:
    current = str(_field(raw, "current", "path", default="") or "").strip("/")
    dirs: list[dict[str, Any]] = []
    for item in _field(raw, "dirs", "directories", default=[]) or []:
        name = str(_field(item, "name", default="") or "")
        if not name:
            continue
        rel = str(_field(item, "rel", "path", default="") or "")
        if not rel:
            rel = f"{current}/{name}".strip("/") if current else name
        dirs.append({"name": name, "rel": rel, "md_count": _to_int(_field(item, "md_count", "md", default=0), 0)})
    parent = _field(raw, "parent", default="")
    return {
        "ok": bool(_field(raw, "ok", default=True)),
        "message": str(_field(raw, "message", "error", default="") or ""),
        "current": current,
        "abs": str(_field(raw, "abs", "absolute", default="") or ""),
        "parent": "" if parent is None else str(parent),
        "dirs": dirs,
        "md_count": _to_int(_field(raw, "md_count", "md", default=0), 0),
        "writable": bool(_field(raw, "writable", default=False)),
        "crumbs": _crumbs(current),
    }


def _vault_context(browse_rel: str | None = None) -> dict[str, Any]:
    """知识库卡片数据；obsidian 域缺函数或抛异常时降级为 available=False。"""
    context: dict[str, Any] = {
        "available": False,
        "reason": "",
        "root": str(settings.vault_dir),
        "path": "",
        "path_crumbs": [],
        "browse": None,
        "browse_error": "",
        "notes": 0,
        "indexed_at": "",
        "last_index": S.get("obsidian_last_index", ""),
        "enabled": S.get("obsidian_enabled", "0") == "1",
    }
    ob = _obsidian()

    try:
        fn = getattr(ob, "vault_root", None)
        if fn is not None:
            context["root"] = str(fn() or "") or str(settings.vault_dir)
    except Exception:
        logger.warning("读取知识库挂载根失败", exc_info=True)

    try:
        fn = getattr(ob, "current_rel", None)
        if fn is not None:
            context["path"] = str(fn() or "").strip("/")
        else:
            fn = getattr(ob, "vault_path", None)
            if fn is None:
                raise NotImplementedError("obsidian.vault_path 尚未实现")
            context["path"] = _rel_from_root(str(fn() or ""), context["root"])
    except NotImplementedError as exc:
        context["reason"] = str(exc)
    except Exception as exc:
        logger.exception("读取当前知识库路径失败")
        context["reason"] = f"{type(exc).__name__}: {exc}"

    context["path_crumbs"] = _crumbs(context["path"])

    try:
        fn = getattr(ob, "stats", None)
        if fn is not None:
            stat = fn() or {}
            context["notes"] = _to_int(_field(stat, "notes", default=0), 0)
            context["indexed_at"] = str(_field(stat, "indexed_at", default="") or "")
            if _field(stat, "enabled", default=None) is not None:
                context["enabled"] = bool(_field(stat, "enabled", default=False))
    except Exception:
        logger.warning("读取 Obsidian 索引统计失败", exc_info=True)

    context["available"] = not context["reason"]
    if context["available"]:
        target = context["path"] if browse_rel is None else str(browse_rel or "").strip("/")
        try:
            fn = getattr(ob, "browse", None)
            if fn is None:
                raise NotImplementedError("obsidian.browse 尚未实现")
            browse = _norm_browse(fn(target))
            if browse["ok"]:
                context["browse"] = browse
            else:
                context["browse_error"] = browse["message"] or "目录浏览失败"
        except NotImplementedError as exc:
            context["available"] = False
            context["reason"] = str(exc)
        except Exception as exc:
            logger.exception("浏览知识库目录失败：%r", target)
            context["browse_error"] = f"{type(exc).__name__}: {exc}"
            context["browse"] = None
    return context


def _vault_card(
    request: Request,
    *,
    notice: str = "",
    notice_kind: str = "info",
    browse_rel: str | None = None,
    status_code: int = 200,
):
    return templating.render(
        request,
        "partials/settings_vault_card.html",
        status_code=status_code,
        vault=_vault_context(browse_rel),
        last_reindex=_load_reindex_result(),
        notice=notice,
        notice_kind=notice_kind,
    )


def _vault_browser(request: Request, browse_rel: str | None = None, *, status_code: int = 200):
    return templating.render(
        request,
        "partials/settings_vault_browser.html",
        status_code=status_code,
        vault=_vault_context(browse_rel),
    )
