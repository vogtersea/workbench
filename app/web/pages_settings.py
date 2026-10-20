"""设置页面（SPEC.md 第 6 节 /settings 系列 + 第 14.4 节前端可配置化）。

    GET  /settings                 设置页（大模型 / 知识库 / 提醒 / Webhook / 扫描 / 系统信息）
    POST /settings                 保存提醒与 Webhook 配置
    POST /settings/test-push       发送测试推送
    POST /settings/scan-now        立即扫描一次
    POST /settings/reindex         重建 Obsidian 索引
    POST /settings/llm-profile     新建或保存大模型配置
    POST /settings/llm-active      切换生效配置
    POST /settings/llm-key         保存或清除密钥
    POST /settings/llm-delete      删除配置
    POST /settings/llm-test        测试连接（htmx 片段优先）
    POST /settings/vault           设置知识库目录
    GET  /settings/browse          目录浏览（htmx 片段）

llm / obsidian 两个服务由并行域开发，本模块对该两域的所有调用都做了「缺函数 + 抛异常」兜底：
功能未落地时卡片显示「该项暂不可用：<原因>」，页面仍然 200，其余卡片全部正常。

htmx 请求返回片段，原生表单提交一律 303 回 /settings。

权限（SPEC 15.8）：大模型 / 知识库 / Webhook / 每日提醒属于全局共享配置，由管理员维护。
多用户模式下非管理员 GET /settings 仍返回 200，但页面上三块的写入控件与「发送测试推送」
「立即扫描」「重建索引」等管理动作全部不渲染；直接 POST 这些写接口一律 403。
单用户（legacy）与开放（open）模式没有用户概念，行为与改造前完全一致。
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import auth, db, security, templating, timeutil
from app.config import settings
from app.services import reminders, scheduler, users
from app.services import settings_store as S

logger = logging.getLogger(__name__)

pages = APIRouter()

#: 下拉框：6 种 Webhook 格式（key 与 SPEC 第 7 节第 5 条一一对应）
WEBHOOK_FORMAT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("generic_json", "通用 JSON —— {\"text\": \"...\"}"),
    ("wecom_text", "企业微信群机器人 —— msgtype=text"),
    ("feishu_text", "飞书自定义机器人 —— msg_type=text"),
    ("serverchan", "Server 酱 —— title + desp"),
    ("telegram", "Telegram Bot —— chat_id + text"),
    ("bark", "Bark（iOS）—— title + body"),
)
FLASH_MESSAGES: dict[str, str] = {
    "test-push": "测试推送已执行，结果见「最近一次测试推送」。",
    "scan-now": "已立即扫描一次，结果见「最近一次扫描」。",
    "reindex": "已触发 Obsidian 索引重建。",
    "llm-profile": "大模型配置已保存，立即生效（无需重启容器）。",
    "llm-active": "已切换生效配置，下一份报告即使用新配置。",
    "llm-key": "密钥已保存（页面不回显明文）。",
    "llm-key-clear": "密钥已清除。",
    "llm-delete": "大模型配置已删除。",
    "llm-test": "已执行连接测试，详细结果见「大模型」卡片内的测试输出。",
    "vault": "知识库目录已更新，建议随后重建一次索引。",
}
COUNT_TABLES = (
    "projects",
    "tasks",
    "progress_entries",
    "reports",
    "settings",
    "reminders_log",
    "notes_index",
)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
LAST_REINDEX_KEY = "last_reindex_json"

#: llm.PRESETS 尚未落地时的兜底预设（只在 llm 域未提供 PRESETS 时使用）。
FALLBACK_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "requires_key": True,
    },
    {
        "key": "openai",
        "label": "OpenAI",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "requires_key": True,
    },
    {
        "key": "kimi",
        "label": "Kimi（Moonshot）",
        "provider": "kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "requires_key": True,
    },
    {
        "key": "ollama",
        "label": "Ollama（本地）",
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "requires_key": False,
    },
    {
        "key": "custom",
        "label": "自定义（OpenAI 兼容）",
        "provider": "custom",
        "base_url": "",
        "model": "",
        "requires_key": True,
    },
)


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def _is_htmx(request: Request) -> bool:
    return (request.headers.get("hx-request") or "").lower() == "true"


def _human_size(num: int) -> str:
    value = float(max(0, num))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} GB"


def _field(obj: Any, *names: str, default: Any = "") -> Any:
    """从 dict / 对象里按顺序取第一个存在的字段（兼容服务端返回 dict 或 dataclass）。"""
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
            continue
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "on", "true", "yes", "y"}


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


def _obsidian():
    from app.services import obsidian

    return obsidian


def _llm():
    from app.services import llm

    return llm


def _obsidian_fn(name: str):
    fn = getattr(_obsidian(), name, None)
    if fn is None:
        raise NotImplementedError(f"obsidian.{name} 尚未实现")
    return fn


def _llm_fn(name: str):
    fn = getattr(_llm(), name, None)
    if fn is None:
        raise NotImplementedError(f"llm.{name} 尚未实现")
    return fn


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


def _load_reindex_result() -> dict[str, Any] | None:
    raw = S.get(LAST_REINDEX_KEY, "")
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# 大模型卡片
# --------------------------------------------------------------------------
def _norm_profile(raw: Any) -> dict[str, Any]:
    pid = str(_field(raw, "id", "slug", default="") or "")
    return {
        "id": pid,
        "name": str(_field(raw, "name", "label", default="") or pid or "未命名配置"),
        "provider": str(_field(raw, "provider", default="") or ""),
        "base_url": str(_field(raw, "base_url", default="") or ""),
        "model": str(_field(raw, "model", default="") or ""),
        "temperature": _field(raw, "temperature", default=""),
        "max_tokens": _field(raw, "max_tokens", default=""),
        "requires_key": bool(_field(raw, "requires_key", default=True)),
        "has_key": bool(_field(raw, "has_key", default=False)),
        "active": bool(_field(raw, "active", default=False)),
    }


def _preset_options() -> list[dict[str, Any]]:
    """llm.PRESETS 有多种写法，统一成模板能吃的列表；取不到时用本地兜底。"""
    raw: Any = None
    try:
        raw = getattr(_llm(), "PRESETS", None)
    except Exception:
        raw = None

    options: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, (str, int)):
                options.append(
                    {
                        "key": str(key),
                        "label": str(value),
                        "provider": str(key),
                        "base_url": "",
                        "model": "",
                        "requires_key": True,
                    }
                )
                continue
            options.append(
                {
                    "key": str(key),
                    "label": str(_field(value, "label", "name", "title", default=str(key)) or key),
                    "provider": str(_field(value, "provider", default=str(key)) or key),
                    "base_url": str(_field(value, "base_url", default="") or ""),
                    "model": str(_field(value, "model", default="") or ""),
                    "requires_key": bool(_field(value, "requires_key", default=True)),
                }
            )
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            key = str(_field(item, "key", "id", default="") or "")
            if not key:
                continue
            options.append(
                {
                    "key": key,
                    "label": str(_field(item, "label", "name", default=key) or key),
                    "provider": str(_field(item, "provider", default=key) or key),
                    "base_url": str(_field(item, "base_url", default="") or ""),
                    "model": str(_field(item, "model", default="") or ""),
                    "requires_key": bool(_field(item, "requires_key", default=True)),
                }
            )
    if options:
        return options
    return [dict(item) for item in FALLBACK_PRESETS]


def _llm_modes() -> str:
    """'mock' / 'live' / ''（未知）。永远不抛异常。"""
    try:
        return "mock" if bool(_llm().is_mock_mode()) else "live"
    except Exception:
        return ""


def _llm_context(
    edit_id: str = "",
    *,
    notice: str = "",
    notice_kind: str = "info",
    selected_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """大模型卡片数据；llm 域缺函数或抛异常时降级为 available=False。"""
    profiles: list[dict[str, Any]] = []
    reason = ""
    try:
        fn = getattr(_llm(), "list_profiles", None)
        if fn is None:
            raise NotImplementedError("llm.list_profiles 尚未实现")
        for item in fn() or []:
            profiles.append(_norm_profile(item))
    except NotImplementedError as exc:
        reason = str(exc)
    except Exception as exc:
        logger.exception("读取大模型配置列表失败")
        reason = f"{type(exc).__name__}: {exc}"

    active: dict[str, Any] | None = None
    if not reason:
        try:
            fn = getattr(_llm(), "active_profile", None)
            if fn is not None:
                raw = fn()
                if raw:
                    active = _norm_profile(raw)
        except NotImplementedError:
            active = None
        except Exception:
            logger.warning("读取生效大模型配置失败", exc_info=True)

    active_id = str((active or {}).get("id") or "")
    if not active_id:
        active_id = next((p["id"] for p in profiles if p["active"]), "")
    if not active_id:
        active_id = S.get("llm_active", "")
    if active is None and active_id:
        active = next((p for p in profiles if p["id"] == active_id), None)

    selected: dict[str, Any] | None = None
    if selected_override is not None:
        selected = _norm_profile(selected_override)
    elif edit_id:
        selected = next((p for p in profiles if p["id"] == edit_id), None)

    return {
        "available": not reason,
        "reason": reason,
        "profiles": profiles,
        "active_id": active_id,
        "active": active,
        "selected": selected,
        "presets": _preset_options(),
        "mode": _llm_modes(),
        "notice": notice,
        "notice_kind": notice_kind,
        "env_base_url": settings.llm_base_url,
        "env_model": settings.llm_model,
        "env_has_key": bool(settings.llm_api_key),
    }


async def _llm_health(profile_id: str = "", override: dict[str, Any] | None = None) -> dict[str, Any]:
    """兼容 health() / health(profile_id, override) 两种签名，并按需 await。"""
    fn = getattr(_llm(), "health", None)
    if fn is None:
        raise NotImplementedError("llm.health 尚未实现")
    kwargs: dict[str, Any] = {}
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if "profile_id" in params and profile_id:
        kwargs["profile_id"] = profile_id
    if "override" in params and override:
        kwargs["override"] = override
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result if isinstance(result, dict) else {}


def _save_key(pid: str, key: str) -> None:
    llm = _llm()
    fn = getattr(llm, "save_api_key", None)
    if fn is not None:
        fn(pid, key)
        return
    from app.services import secrets_store

    if key:
        secrets_store.set_api_key(pid, key)
    else:
        secrets_store.delete_api_key(pid)


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


# --------------------------------------------------------------------------
# 权限（SPEC 15.8：共享配置由管理员维护，非管理员写操作一律 403）
# --------------------------------------------------------------------------
FORBIDDEN_TEXT = "共享设置（大模型 / 知识库 / Webhook / 每日提醒）由管理员维护，当前账号只能查看。"


def _identity(request: Request) -> dict[str, Any]:
    """身份 + 写权限判定；模板据此决定渲染管理控件还是只读说明。"""
    ident = auth.request_context(request)
    mode = ident["mode"]
    can_manage = mode != security.MODE_MULTI or bool(ident["is_admin"])
    user_count = 0
    if mode == security.MODE_MULTI:
        try:
            user_count = users.count()
        except Exception:
            logger.warning("统计用户数失败", exc_info=True)
    return {**ident, "can_manage": can_manage, "user_count": user_count}


def _forbidden_page(request: Request):
    """非管理员写操作的页面出口：200 之外的 403 页面（不带任何写控件）。"""
    return _render_page(request, status_code=403, error=FORBIDDEN_TEXT)


def _guard_write(request: Request):
    """管理员 / 单用户模式放行（None），多用户模式下的普通用户返回 403 页面。"""
    if _identity(request)["can_manage"]:
        return None
    logger.info("非管理员尝试修改共享设置：%s %s", request.method, request.url.path)
    return _forbidden_page(request)


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
    }


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
@pages.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, flash: str = "", edit: str = "", browse: str = ""):
    return _render_page(
        request,
        saved=bool(saved),
        flash=flash,
        edit_id=(edit or "").strip(),
        browse_rel=(browse or "").strip() or None,
    )


@pages.post("/settings", response_class=HTMLResponse)
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


@pages.post("/settings/test-push", response_class=HTMLResponse)
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


@pages.post("/settings/scan-now", response_class=HTMLResponse)
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


@pages.post("/settings/reindex", response_class=HTMLResponse)
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
# 大模型配置（SPEC 14.4 页面路由）
# --------------------------------------------------------------------------
def _profile_payload(
    *,
    pid: str,
    preset: str,
    name: str,
    provider: str,
    base_url: str,
    model: str,
    temperature: str,
    max_tokens: str,
    requires_key: str,
) -> dict[str, Any]:
    provider_value = (provider or "").strip() or (preset or "").strip() or "custom"
    data = {
        "id": pid,
        "name": (name or "").strip(),
        "provider": provider_value,
        "base_url": (base_url or "").strip(),
        "model": (model or "").strip(),
        "temperature": _to_float(temperature, 0.3),
        "max_tokens": _to_int(max_tokens, 2000),
        "requires_key": _truthy(requires_key),
    }
    if not data["name"]:
        data["name"] = data["model"] or provider_value or "未命名配置"
    return data


def _validate_profile(data: dict[str, Any]) -> list[str]:
    """只挡明显非法值；base_url / model 留空表示沿用 .env 兜底（与 llm.upsert_profile 一致）。"""
    errors: list[str] = []
    base = str(data.get("base_url") or "")
    if base and not base.lower().startswith(("http://", "https://")):
        errors.append("base_url 必须以 http:// 或 https:// 开头")
    try:
        temperature = float(data.get("temperature"))
    except (TypeError, ValueError):
        temperature = -1.0
    if not 0.0 <= temperature <= 2.0:
        errors.append("temperature 必须在 0 ~ 2 之间")
    try:
        max_tokens = int(data.get("max_tokens"))
    except (TypeError, ValueError):
        max_tokens = 0
    if not 1 <= max_tokens <= 65536:
        errors.append("max_tokens 必须是 1 ~ 65536 的整数")
    return errors


@pages.post("/settings/llm-profile", response_class=HTMLResponse)
def settings_llm_profile(
    request: Request,
    profile_id: str | None = Form(None),
    preset: str | None = Form(None),
    name: str | None = Form(None),
    provider: str | None = Form(None),
    base_url: str | None = Form(None),
    model: str | None = Form(None),
    temperature: str | None = Form(None),
    max_tokens: str | None = Form(None),
    api_key: str | None = Form(None),
    requires_key: str | None = Form(None),
    action: str | None = Form(None),
):
    denied = _guard_write(request)
    if denied is not None:
        return denied
    pid = (profile_id or "").strip()
    if (action or "").strip() == "new":
        pid = ""
    data = _profile_payload(
        pid=pid,
        preset=preset or "",
        name=name or "",
        provider=provider or "",
        base_url=base_url or "",
        model=model or "",
        temperature=temperature or "",
        max_tokens=max_tokens or "",
        requires_key=requires_key or "",
    )
    errors = _validate_profile(data)
    if errors:
        message = "；".join(errors)
        if _is_htmx(request):
            return _llm_card(request, notice=message, notice_kind="danger", selected_override=data)
        return _render_page(request, error=message, llm_override=data)

    try:
        saved_id = str(_llm_fn("upsert_profile")(data) or pid or "")
        key = (api_key or "").strip()
        if key and saved_id:
            _save_key(saved_id, key)
    except NotImplementedError as exc:
        return _unavailable(request, str(exc), selected_override=data)
    except ValueError as exc:
        return _unavailable(request, str(exc), prefix="保存失败：", selected_override=data)
    except Exception as exc:
        logger.exception("保存大模型配置失败")
        return _unavailable(
            request, f"{type(exc).__name__}: {exc}", prefix="保存失败：", selected_override=data
        )

    if _is_htmx(request):
        return _llm_card(request, notice=FLASH_MESSAGES["llm-profile"], notice_kind="success", edit_id=saved_id)
    target = f"/settings?flash=llm-profile&edit={quote(saved_id)}" if saved_id else "/settings?flash=llm-profile"
    return RedirectResponse(target, status_code=303)


@pages.post("/settings/llm-active", response_class=HTMLResponse)
def settings_llm_active(request: Request, id: str | None = Form(None)):
    denied = _guard_write(request)
    if denied is not None:
        return denied
    pid = (id or "").strip()
    if not pid:
        message = "请选择一个要生效的配置"
        if _is_htmx(request):
            return _llm_card(request, notice=message, notice_kind="danger")
        return _render_page(request, error=message)
    try:
        _llm_fn("set_active")(pid)
    except NotImplementedError as exc:
        return _unavailable(request, str(exc))
    except ValueError as exc:
        return _unavailable(request, str(exc), prefix="切换失败：")
    except Exception as exc:
        logger.exception("切换大模型配置失败")
        return _unavailable(request, f"{type(exc).__name__}: {exc}", prefix="切换失败：")

    if _is_htmx(request):
        return _llm_card(request, notice=FLASH_MESSAGES["llm-active"], notice_kind="success", edit_id=pid)
    return RedirectResponse("/settings?flash=llm-active", status_code=303)


@pages.post("/settings/llm-key", response_class=HTMLResponse)
def settings_llm_key(
    request: Request,
    id: str | None = Form(None),
    api_key: str | None = Form(None),
    action: str | None = Form(None),
):
    denied = _guard_write(request)
    if denied is not None:
        return denied
    pid = (id or "").strip()
    key = (api_key or "").strip()
    if not pid:
        message = "缺少配置 id，无法保存密钥"
        if _is_htmx(request):
            return _llm_card(request, notice=message, notice_kind="danger")
        return _render_page(request, error=message)

    clearing = (action or "").strip() == "clear" or not key
    try:
        _save_key(pid, "" if clearing else key)
    except NotImplementedError as exc:
        return _unavailable(request, str(exc), edit_id=pid)
    except ValueError as exc:
        return _unavailable(request, str(exc), prefix="保存密钥失败：", edit_id=pid)
    except Exception as exc:
        logger.exception("保存大模型密钥失败")
        return _unavailable(
            request, f"{type(exc).__name__}: {exc}", prefix="保存密钥失败：", edit_id=pid
        )

    flash = "llm-key-clear" if clearing else "llm-key"
    if _is_htmx(request):
        return _llm_card(request, notice=FLASH_MESSAGES[flash], notice_kind="success", edit_id=pid)
    return RedirectResponse(f"/settings?flash={flash}&edit={quote(pid)}", status_code=303)


@pages.post("/settings/llm-delete", response_class=HTMLResponse)
def settings_llm_delete(request: Request, id: str | None = Form(None)):
    denied = _guard_write(request)
    if denied is not None:
        return denied
    pid = (id or "").strip()
    if not pid:
        message = "缺少配置 id，无法删除"
        if _is_htmx(request):
            return _llm_card(request, notice=message, notice_kind="danger")
        return _render_page(request, error=message)
    try:
        _llm_fn("delete_profile")(pid)
    except NotImplementedError as exc:
        return _unavailable(request, str(exc))
    except ValueError as exc:
        return _unavailable(request, str(exc), prefix="删除失败：")
    except Exception as exc:
        logger.exception("删除大模型配置失败")
        return _unavailable(request, f"{type(exc).__name__}: {exc}", prefix="删除失败：")

    if _is_htmx(request):
        return _llm_card(request, notice=FLASH_MESSAGES["llm-delete"], notice_kind="success")
    return RedirectResponse("/settings?flash=llm-delete", status_code=303)


@pages.post("/settings/llm-test", response_class=HTMLResponse)
async def settings_llm_test(
    request: Request,
    id: str | None = Form(None),
    preset: str | None = Form(None),
    provider: str | None = Form(None),
    base_url: str | None = Form(None),
    model: str | None = Form(None),
    temperature: str | None = Form(None),
    max_tokens: str | None = Form(None),
    api_key: str | None = Form(None),
):
    """用表单当前值（未保存也生效）试一次；只回片段，不落库。"""
    denied = _guard_write(request)
    if denied is not None:
        return denied
    override: dict[str, Any] = {}
    if (base_url or "").strip():
        override["base_url"] = base_url.strip()
    if (model or "").strip():
        override["model"] = model.strip()
    provider_value = (provider or "").strip() or (preset or "").strip()
    if provider_value:
        override["provider"] = provider_value
    if (temperature or "").strip():
        override["temperature"] = _to_float(temperature, 0.3)
    if (max_tokens or "").strip():
        override["max_tokens"] = _to_int(max_tokens, 2000)
    if (api_key or "").strip():
        override["api_key"] = api_key.strip()

    result: dict[str, Any] = {}
    error = ""
    try:
        result = await _llm_health((id or "").strip(), override or None)
    except NotImplementedError as exc:
        error = str(exc)
    except Exception as exc:
        logger.exception("大模型连接测试失败")
        error = f"{type(exc).__name__}: {exc}"

    if _is_htmx(request):
        return templating.render(
            request, "partials/settings_llm_test.html", result=result, error=error
        )
    return RedirectResponse("/settings?flash=llm-test", status_code=303)


# --------------------------------------------------------------------------
# 知识库目录（SPEC 14.4 页面路由）
# --------------------------------------------------------------------------
@pages.post("/settings/vault", response_class=HTMLResponse)
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


@pages.get("/settings/browse", response_class=HTMLResponse)
def settings_browse(request: Request, path: str = ""):
    denied = _guard_write(request)
    if denied is not None:
        return denied
    rel = (path or "").strip()
    if _is_htmx(request):
        return _vault_browser(request, rel)
    return RedirectResponse(f"/settings?browse={quote(rel)}", status_code=303)
