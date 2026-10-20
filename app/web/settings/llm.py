"""设置页大模型域路由：配置保存、生效切换、密钥读写、删除与连接测试。

职责边界（W-08）：只管 /settings/llm-* 这几条写操作与其校验；上下文拼装在 llm_context.py。"""

from __future__ import annotations

import inspect
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import templating

from .general import _llm_card, _render_page, _unavailable
from .shared import FLASH_MESSAGES, _is_htmx, _llm, _llm_fn, _to_int, logger
from .users import _guard_write

router = APIRouter()

def _to_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "on", "true", "yes", "y"}


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


@router.post("/settings/llm-profile", response_class=HTMLResponse)
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


@router.post("/settings/llm-active", response_class=HTMLResponse)
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


@router.post("/settings/llm-key", response_class=HTMLResponse)
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


@router.post("/settings/llm-delete", response_class=HTMLResponse)
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


@router.post("/settings/llm-test", response_class=HTMLResponse)
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
