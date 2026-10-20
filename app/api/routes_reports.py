"""报告域 JSON API（SPEC.md 第 6 节，本域整体重写）。

约定：本模块只暴露 router = APIRouter()，路由路径**不含** /api 前缀——
main.py 以 include_router(router, prefix="/api") 包含。

端点：
    GET    /reports           报告列表（摘要）
    POST   /reports           生成报告（同步阻塞，MOCK 下即时返回）
    GET    /reports/{id}      报告详情（含 content_md）
    DELETE /reports/{id}      删除报告
    GET    /llm/health        LLM 连通性（mock / live）
    GET    /llm/profiles      多套大模型配置列表（含生效项与密钥有无）
    POST   /llm/profiles      新建配置（201）或更新配置（200）
    DELETE /llm/profiles/{pid}  删除配置（生效项被删时自动回落）
    POST   /llm/active        切换生效配置
    POST   /llm/key           保存密钥（key 为空字符串表示清除）
    POST   /llm/test          立即测试连接（支持 override 先试不改配置）
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app import auth
from app.services import llm
from app.services import report as reports

logger = logging.getLogger(__name__)

router = APIRouter()

_TRUE_VALUES = {"1", "true", "on", "yes", "y", "checked"}
_FALSE_VALUES = {"0", "false", "off", "no", "n", ""}


def _owner(request: Request) -> int | None:
    """当前生效归属 id（SPEC 15.7）：None = 开放/单口令模式，不做归属过滤。"""
    return auth.effective_owner_id(request)


def _report_dict(row: Any, *, with_content: bool = True) -> dict:
    data = {
        "id": row["id"],
        "kind": row["kind"],
        "kind_label": reports.KIND_LABELS.get(row["kind"], row["kind"]),
        "scope_label": row["scope_label"],
        "range_start": row["range_start"],
        "range_end": row["range_end"],
        "task_id": row["task_id"],
        "provider": row["provider"],
        "model": row["model"],
        "is_mock": bool(row["is_mock"]),
        "context_notes": reports.parse_context_notes(row["context_notes"]),
        "created_at": row["created_at"],
    }
    if with_content:
        data["content_md"] = row["content_md"]
    else:
        body = row["content_md"] or ""
        data["excerpt"] = body[:160]
        data["content_length"] = len(body)
    return data


async def _read_payload(request: Request) -> dict:
    """同时接受 JSON 与表单编码的请求体。"""
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from None
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        return data
    try:
        form = await request.form()
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析请求体") from None
    return {key: form.get(key) for key in form.keys()}


def _as_int(value: Any, field: str) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="%s 必须是整数" % field) from None


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default


@router.get("/reports")
def list_reports(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict:
    rows = reports.list_reports(limit, owner_id=_owner(request))
    return {
        "count": len(rows),
        "limit": limit,
        "reports": [_report_dict(row, with_content=False) for row in rows],
    }


@router.post("/reports")
async def create_report(request: Request) -> JSONResponse:
    payload = await _read_payload(request)
    kind = str(payload.get("kind") or "").strip()
    task_id = _as_int(payload.get("task_id"), "task_id")
    use_obsidian = _as_bool(payload.get("use_obsidian"), default=True)
    start = payload.get("start") or payload.get("range_start") or None
    end = payload.get("end") or payload.get("range_end") or None

    owner_id = _owner(request)
    try:
        rid = await reports.generate(
            kind,
            start=start,
            end=end,
            task_id=task_id,
            use_obsidian=use_obsidian,
            owner_id=owner_id,
        )
    except reports.ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except llm.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except Exception as exc:
        logger.exception("生成报告失败（kind=%s）", kind)
        raise HTTPException(
            status_code=500, detail="生成报告失败：%s %s" % (type(exc).__name__, exc)
        ) from None

    row = reports.get_report(rid, owner_id=owner_id)
    body = _report_dict(row) if row is not None else {"id": rid}
    body["ok"] = True
    return JSONResponse(body, status_code=201)


@router.get("/reports/{rid}")
def get_report(request: Request, rid: int) -> dict:
    row = reports.get_report(rid, owner_id=_owner(request))
    if row is None:
        raise HTTPException(status_code=404, detail="报告 #%s 不存在" % rid)
    return _report_dict(row)


@router.delete("/reports/{rid}")
def delete_report(request: Request, rid: int) -> dict:
    owner_id = _owner(request)
    row = reports.get_report(rid, owner_id=owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="报告 #%s 不存在" % rid)
    reports.delete_report(rid, owner_id=owner_id)
    return {"ok": True, "deleted": rid}


@router.get("/llm/health")
async def llm_health() -> dict:
    return await llm.health()

# ---------------------------------------------------------------- 大模型配置 API
# SPEC.md 第 14.4 节：多套 profile 的增删改 / 切换 / 保存密钥 / 连接测试。
# 密钥只走 secrets.json，任何响应都只给 has_key 与 sk-***。


def _profile_or_404(pid: Any) -> dict:
    """按 id 取 profile；不存在返回 404（可读中文 detail）。"""
    target = str(pid or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="缺少配置 id")
    profile = llm.get_profile(target)
    if profile is None:
        raise HTTPException(status_code=404, detail="配置 %s 不存在" % target)
    return profile


def _mock_flag() -> bool:
    try:
        return bool(llm.is_mock_mode())
    except Exception:  # 配置解析异常时按 MOCK 上报，不让列表接口 500
        logger.warning("is_mock_mode 调用失败，按 MOCK 上报", exc_info=True)
        return True


@router.get("/llm/profiles")
def list_llm_profiles() -> dict:
    """配置列表 + 生效项 + 预设（前端下拉直接用 presets）。"""
    profiles = llm.list_profiles()
    active = next((item["id"] for item in profiles if item.get("active")), "")
    return {
        "count": len(profiles),
        "active": active,
        "mock": _mock_flag(),
        "profiles": profiles,
        "presets": llm.PRESETS,
    }


@router.post("/llm/profiles")
async def save_llm_profile(request: Request) -> JSONResponse:
    """新建（201）或更新（200）：带 id 或同名已存在即更新。"""
    payload = await _read_payload(request)
    existed = llm.find_profile_id(payload) is not None
    try:
        pid = llm.upsert_profile(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:
        logger.exception("保存大模型配置失败")
        raise HTTPException(
            status_code=500, detail="保存大模型配置失败：%s %s" % (type(exc).__name__, exc)
        ) from None

    body = {
        "ok": True,
        "created": not existed,
        "id": pid,
        "profile": llm.get_profile(pid),
        "mock": _mock_flag(),
    }
    return JSONResponse(body, status_code=200 if existed else 201)


@router.delete("/llm/profiles/{pid}")
def delete_llm_profile(pid: str) -> dict:
    """删除配置；删掉生效项后自动切换到下一个（最后一个则回落环境变量兜底）。"""
    _profile_or_404(pid)
    try:
        llm.delete_profile(pid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None

    profiles = llm.list_profiles()
    active = next((item["id"] for item in profiles if item.get("active")), "")
    return {
        "ok": True,
        "deleted": pid,
        "active": active,
        "profiles": profiles,
        "mock": _mock_flag(),
    }


@router.post("/llm/active")
async def set_llm_active(request: Request) -> dict:
    """切换生效配置。"""
    payload = await _read_payload(request)
    pid = str(payload.get("id") or "").strip()
    _profile_or_404(pid)
    try:
        llm.set_active(pid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {
        "ok": True,
        "active": pid,
        "profile": llm.active_profile(),
        "mock": _mock_flag(),
    }


@router.post("/llm/key")
async def save_llm_key(request: Request) -> dict:
    """保存密钥（空字符串=清除）；响应只回 has_key 与 sk-***，绝不回明文。"""
    payload = await _read_payload(request)
    pid = str(payload.get("id") or "").strip()
    _profile_or_404(pid)
    raw_key = payload.get("key")
    secret = "" if raw_key is None else str(raw_key).strip()
    try:
        llm.save_api_key(pid, secret)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    profile = llm.get_profile(pid) or {}
    has_key = bool(profile.get("has_key"))
    return {
        "ok": True,
        "id": pid,
        "has_key": has_key,
        "key_masked": llm.mask_key("x" if has_key else ""),
        "mock": _mock_flag(),
    }


@router.post("/llm/test")
async def test_llm(request: Request) -> dict:
    """立即试一次连接：{"id": pid?, "override": {...}?}，返回 SPEC 14.2 的 health 结构。"""
    payload = await _read_payload(request)
    pid = str(payload.get("id") or "").strip()
    override = payload.get("override")
    if pid:
        _profile_or_404(pid)
    if override is not None and not isinstance(override, dict):
        raise HTTPException(status_code=400, detail="override 必须是 JSON 对象")
    # override 原样透传给 llm.health：其中 override.api_key 只用于本次测试，
    # 不落盘、不写 secrets.json；这里也绝不打日志（避免密钥进日志）。
    try:
        return await llm.health(profile_id=pid or None, override=override or None)
    except Exception as exc:  # health 自身兜底过，这里只防万一
        logger.exception("大模型连接测试异常")
        return {
            "mode": "live",
            "provider": "",
            "model": "",
            "base_url": "",
            "ok": False,
            "error": "连接测试异常：%s" % type(exc).__name__,
            "latency_ms": 0,
            "reply_snippet": "",
        }
