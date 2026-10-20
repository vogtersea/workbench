"""任务域 JSON API（SPEC.md 第 6 节）。

约定：本模块只暴露 router = APIRouter()，路径**不含** /api 前缀——
main.py 以 include_router(router, prefix="/api") 包含，因此
@router.get("/tasks") 最终暴露为 /api/tasks。

多用户（SPEC 15.1 / 15.7）：每个端点取 auth.effective_owner_id(request) 作为归属 id，
None 表示开放/单口令模式（不传 owner_id，保持改造前行为）。跨用户访问一律 404，
不暴露存在性。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app import auth
from app.services import tasks

router = APIRouter()


class TaskCreateIn(BaseModel):
    title: str
    detail: str = ""
    done_criteria: str = ""
    due_date: str | None = None
    due_time: str | None = None
    priority: int = 2
    project_id: int | None = None
    week_start: str | None = None
    tags: str = ""
    status: str = "todo"


class TaskPatchIn(BaseModel):
    title: str | None = None
    detail: str | None = None
    done_criteria: str | None = None
    due_date: str | None = None
    due_time: str | None = None
    priority: int | None = None
    project_id: int | None = None
    week_start: str | None = None
    tags: str | None = None
    status: str | None = None


class ProgressIn(BaseModel):
    content: str
    link: str | None = None


def _owner(request: Request) -> int | None:
    """当前生效归属 id；None = 开放/单口令模式（不过滤，保持改造前行为）。"""
    return auth.effective_owner_id(request)


def _require_task(request: Request, task_id: int):
    row = tasks.get_task(task_id, owner_id=_owner(request))
    if row is None:
        raise HTTPException(status_code=404, detail="工作不存在")
    return row


@router.get("/tasks")
def api_list_tasks(
    request: Request,
    week_start: str | None = Query(None),
    project_id: int | None = Query(None),
    status: str | None = Query(None),
    archived: int = Query(0, description="0=仅活跃；1=仅归档；-1=全部"),
    q: str | None = Query(None),
):
    statuses = None
    if status and status.strip() and status.strip() != "all":
        statuses = [part.strip() for part in status.split(",") if part.strip()]
    archived_filter = None if archived < 0 else archived
    rows = tasks.list_tasks(
        week_start=week_start,
        project_id=project_id,
        status=statuses,
        archived=archived_filter,
        q=q,
        owner_id=_owner(request),
    )
    items = [tasks.serialize_task(row) for row in rows]
    return {"items": items, "count": len(items)}


@router.post("/tasks", status_code=201)
def api_create_task(request: Request, payload: TaskCreateIn):
    data = payload.model_dump(exclude_unset=True)
    owner_id = _owner(request)
    try:
        task_id = tasks.create_task(data, owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = tasks.get_task(task_id, owner_id=owner_id)
    return {"ok": True, "id": task_id, "task": tasks.serialize_task(row)}


@router.get("/tasks/{task_id}")
def api_get_task(request: Request, task_id: int):
    row = _require_task(request, task_id)
    progress = [
        tasks.serialize_progress(p)
        for p in tasks.list_progress(task_id, owner_id=_owner(request))
    ]
    return {
        "task": tasks.serialize_task(row),
        "progress": progress,
        "progress_count": len(progress),
    }


@router.patch("/tasks/{task_id}")
def api_patch_task(request: Request, task_id: int, payload: TaskPatchIn):
    _require_task(request, task_id)
    owner_id = _owner(request)
    data = payload.model_dump(exclude_unset=True)
    try:
        tasks.update_task(task_id, data, owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "task": tasks.serialize_task(_require_task(request, task_id))}


@router.delete("/tasks/{task_id}")
def api_delete_task(request: Request, task_id: int):
    _require_task(request, task_id)
    owner_id = _owner(request)
    progress_deleted = len(tasks.list_progress(task_id, owner_id=owner_id))
    tasks.delete_task(task_id, owner_id=owner_id)
    return {"ok": True, "id": task_id, "progress_deleted": progress_deleted}


@router.post("/tasks/{task_id}/progress", status_code=201)
def api_add_progress(request: Request, task_id: int, payload: ProgressIn):
    _require_task(request, task_id)
    owner_id = _owner(request)
    try:
        pid = tasks.add_progress(task_id, payload.content, payload.link, owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "id": pid,
        "task_id": task_id,
        "progress": [
            tasks.serialize_progress(p)
            for p in tasks.list_progress(task_id, owner_id=owner_id)
        ],
    }


@router.get("/dashboard")
def api_dashboard(request: Request):
    return tasks.dashboard_buckets(owner_id=_owner(request))


@router.get("/week")
def api_week(request: Request, offset: int = Query(0)):
    return tasks.week_view(max(-104, min(104, offset)), owner_id=_owner(request))


@router.get("/projects")
def api_projects(request: Request, include_archived: int = Query(0)):
    rows = tasks.list_projects(
        include_archived=bool(include_archived), owner_id=_owner(request)
    )
    return {"items": [dict(row) for row in rows], "count": len(rows)}
