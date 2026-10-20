"""例行工作 JSON API（SPEC.md 16.6）。

约定与其余 API 模块一致：本模块只暴露 router = APIRouter()，路径不含 /api 前缀，
由 main.py 以 include_router(router, prefix="/api") 包含，因此
@router.get("/routines") 最终暴露为 /api/routines。

多用户（SPEC 15.1 / 15.7）：每个端点取 auth.effective_owner_id(request) 作为归属 id，
None 表示开放/单口令模式（不过滤归属，保持改造前行为）；管理员可用 ?as=<user_id> 切换。
跨用户访问一律 404，不暴露存在性。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app import auth
from app.services import routines, tasks

router = APIRouter()


class RoutineCreateIn(BaseModel):
    title: str
    freq: str
    detail: str = ""
    done_criteria: str = ""
    due_rule: str = "period_end"
    due_day: int | None = None
    due_time: str | None = None
    priority: int = 2
    project_id: int | None = None
    tags: str = ""
    active: int = 1
    start_date: str | None = None


class RoutinePatchIn(BaseModel):
    title: str | None = None
    detail: str | None = None
    done_criteria: str | None = None
    freq: str | None = None
    due_rule: str | None = None
    due_day: int | None = None
    due_time: str | None = None
    priority: int | None = None
    project_id: int | None = None
    tags: str | None = None
    active: int | None = None
    start_date: str | None = None


def _owner(request: Request) -> int | None:
    """当前生效归属 id；None = 开放/单口令模式（不过滤，保持改造前行为）。"""
    return auth.effective_owner_id(request)


def _require_routine(request: Request, routine_id: int):
    row = routines.get_routine(routine_id, owner_id=_owner(request))
    if row is None:
        raise HTTPException(status_code=404, detail="例行模板不存在")
    return row


def _history(routine_id: int, limit: int = 24) -> list[dict]:
    return [routines.serialize_instance(row) for row in routines.list_instances(routine_id, limit=limit)]


@router.get("/routines")
def api_list_routines(request: Request, include_inactive: int = Query(1)):
    owner_id = _owner(request)
    rows = routines.list_routines(owner_id=owner_id, include_inactive=bool(include_inactive))
    items = [routines.serialize_routine(row) for row in rows]
    instances = routines.period_instances(owner_id=owner_id)
    return {
        "items": items,
        "count": len(items),
        "instances": instances,
        "instance_count": len(instances),
    }


@router.post("/routines", status_code=201)
def api_create_routine(request: Request, payload: RoutineCreateIn):
    owner_id = _owner(request)
    data = payload.model_dump(exclude_unset=True)
    try:
        routine_id = routines.create_routine(data, owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = routines.get_routine(routine_id, owner_id=owner_id)
    return {"ok": True, "id": routine_id, "routine": routines.serialize_routine(row)}


@router.get("/routines/{routine_id}")
def api_get_routine(request: Request, routine_id: int):
    row = _require_routine(request, routine_id)
    data = routines.serialize_routine(row)
    current = routines.current_instance(routine_id)
    return {
        "routine": data,
        "current": routines.serialize_instance(current) if current is not None else None,
        "history": _history(routine_id),
    }


@router.patch("/routines/{routine_id}")
def api_patch_routine(request: Request, routine_id: int, payload: RoutinePatchIn):
    _require_routine(request, routine_id)
    owner_id = _owner(request)
    data = payload.model_dump(exclude_unset=True)
    try:
        routines.update_routine(routine_id, data, owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = _require_routine(request, routine_id)
    return {"ok": True, "routine": routines.serialize_routine(row)}


@router.delete("/routines/{routine_id}")
def api_delete_routine(request: Request, routine_id: int, purge: int = Query(0)):
    """默认只停用（active=0）；purge=1 才删除模板并级联删除实例，返回删除条数。"""
    _require_routine(request, routine_id)
    owner_id = _owner(request)
    try:
        result = routines.delete_routine(routine_id, purge=bool(purge), owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result}


@router.post("/routines/{routine_id}/skip")
def api_skip_period(request: Request, routine_id: int, today: str | None = Query(None)):
    _require_routine(request, routine_id)
    owner_id = _owner(request)
    try:
        cancelled = routines.skip_period(routine_id, today, owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = _require_routine(request, routine_id)
    return {
        "ok": True,
        "routine_id": routine_id,
        "cancelled": cancelled,
        "period_key": routines.serialize_routine(row)["period_key"],
    }


@router.post("/routines/ensure")
def api_ensure_periods(request: Request, today: str | None = Query(None)):
    """手动补生成（幂等，返回 created/skipped）；today 仅供自测覆盖当期。"""
    owner_id = _owner(request)
    try:
        result = routines.ensure_periods(owner_id=owner_id, today=today)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result}
