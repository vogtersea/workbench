"""设置页大模型上下文：预设选项、模式列表与大模型卡片上下文拼装。

职责边界（W-08）：只拼上下文（读配置、探测 llm 域能力），不渲染页面、不注册路由。"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.services import settings_store as S

from .shared import _field, _llm, logger

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
