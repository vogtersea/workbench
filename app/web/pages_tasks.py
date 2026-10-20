"""任务域页面路由（SPEC.md 第 6 节页面表）。

约定：本模块只暴露 pages = APIRouter()，路径即 SPEC 的页面路径（main.py 不加前缀）。
所有变更都能用原生 form method=post 完成；追加进展与状态切换在 HX-Request 时返回片段。

多用户（SPEC 15.1 / 15.7 / 15.9）：
  - 每个 handler 用 auth.effective_owner_id(request) 取归属 id；None 表示开放/单口令模式，
    此时不传 owner_id，行为与改造前完全一致。
  - 别人的任务在 multi 模式下等同于不存在（404），不区分 403。
  - 管理员在多用户模式下才有「查看用户」下拉（?as=<id>），并把该参数串到本页的链接/表单上。
"""

from __future__ import annotations

import inspect
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app import auth, db, security, timeutil
from app.services import routines, tasks, users
from app.templating import render

pages = APIRouter()

STATUS_OPTIONS = (
    ("todo", "待办"),
    ("doing", "进行中"),
    ("done", "完成"),
    ("cancelled", "取消"),
)
PRIORITY_OPTIONS = ((3, "高"), (2, "中"), (1, "低"))


# ---------------------------------------------------------------- 身份 / 归属

def _owner(request: Request) -> int | None:
    """当前生效的数据归属 id；None = 开放/单口令模式（不过滤，保持改造前行为）。"""
    return auth.effective_owner_id(request)


def as_query(request: Request) -> str:
    """管理员 ?as= 的串联后缀（'' 或 '?as=<id>'）：非管理员、看自己时一律为空。"""
    user = auth.current_user(request)
    if not auth.is_admin(user):
        return ""
    owner = auth.effective_owner_id(request, user)
    if owner is None or int(owner) == int(user["id"]):
        return ""
    return "?as=%d" % int(owner)


def owner_switch_context(request: Request) -> dict | None:
    """「查看用户」下拉的数据（SPEC 15.9）：仅多用户模式的管理员可见，否则 None。"""
    user = auth.current_user(request)
    if not auth.is_admin(user) or security.auth_mode() != security.MODE_MULTI:
        return None
    current = auth.effective_owner_id(request, user)
    options = [dict(row) for row in users.list_users() if int(row["active"])]
    if current is not None and not any(int(row["id"]) == int(current) for row in options):
        options = [dict(row) for row in users.list_users()]
    return {"users": options, "current": current, "self_id": int(user["id"])}


# ---------------------------------------------------------------- 内部工具

def _is_hx(request: Request) -> bool:
    return request.headers.get("hx-request", "").strip().lower() == "true"


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _time_ok(text: str) -> bool:
    try:
        datetime.strptime(text, "%H:%M")
        return True
    except ValueError:
        return False


def _form_values(
    title="",
    detail="",
    done_criteria="",
    due_date="",
    due_time="",
    priority="2",
    project_id="",
    week_start="",
    tags="",
    status="todo",
) -> dict[str, str]:
    return {
        "title": title or "",
        "detail": detail or "",
        "done_criteria": done_criteria or "",
        "due_date": due_date or "",
        "due_time": due_time or "",
        "priority": str(priority or "2"),
        "project_id": str(project_id or ""),
        "week_start": week_start or "",
        "tags": tags or "",
        "status": status or "todo",
    }


def _values_from_task(row) -> dict[str, str]:
    return _form_values(
        title=row["title"] or "",
        detail=row["detail"] or "",
        done_criteria=row["done_criteria"] or "",
        due_date=row["due_date"] or "",
        due_time=row["due_time"] or "",
        priority=str(row["priority"]),
        project_id=str(row["project_id"]) if row["project_id"] else "",
        week_start=row["week_start"] or "",
        tags=row["tags"] or "",
        status=row["status"] or "todo",
    )


def _validate(values: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """返回 (总错误列表, 字段错误)。错误信息直接进 .error-text。"""
    errors: list[str] = []
    field_errors: dict[str, str] = {}

    if not values["title"].strip():
        field_errors["title"] = "标题不能为空"
        errors.append(field_errors["title"])

    due_date = values["due_date"].strip()
    due_time = values["due_time"].strip()
    week_start = values["week_start"].strip()

    if due_date and timeutil.parse_date(due_date) is None:
        field_errors["due_date"] = "截止日期格式不正确，应为 YYYY-MM-DD"
        errors.append(field_errors["due_date"])
    if due_time and not due_date:
        field_errors["due_time"] = "填写截止时间时必须同时填写截止日期"
        errors.append(field_errors["due_time"])
    elif due_time and not _time_ok(due_time):
        field_errors["due_time"] = "截止时间格式不正确，应为 HH:MM"
        errors.append(field_errors["due_time"])
    if week_start and timeutil.parse_date(week_start) is None:
        field_errors["week_start"] = "所属周格式不正确，应为 YYYY-MM-DD"
        errors.append(field_errors["week_start"])
    if values["status"] not in tasks.STATUSES:
        field_errors["status"] = "状态取值不合法"
        errors.append(field_errors["status"])
    priority = values["priority"].strip()
    if priority and priority not in ("1", "2", "3"):
        field_errors["priority"] = "优先级只能是 1 / 2 / 3"
        errors.append(field_errors["priority"])

    return errors, field_errors


def _payload(values: dict[str, str]) -> dict:
    return {
        "title": values["title"].strip(),
        "detail": values["detail"],
        "done_criteria": values["done_criteria"],
        "due_date": values["due_date"].strip(),
        "due_time": values["due_time"].strip(),
        "priority": values["priority"].strip() or "2",
        "project_id": values["project_id"].strip(),
        "week_start": values["week_start"].strip(),
        "tags": values["tags"],
        "status": values["status"].strip() or "todo",
    }


def _render_form(
    request: Request,
    *,
    values: dict[str, str],
    errors: list[str],
    field_errors: dict[str, str],
    form_action: str,
    page_title: str,
    submit_label: str,
    cancel_url: str,
    status_code: int = 200,
):
    return render(
        request,
        "task_form.html",
        values=values,
        errors=errors,
        field_errors=field_errors,
        form_action=form_action + as_query(request),
        page_title=page_title,
        submit_label=submit_label,
        cancel_url=cancel_url,
        projects=tasks.list_projects(owner_id=_owner(request)),
        status_options=STATUS_OPTIONS,
        priority_options=PRIORITY_OPTIONS,
        as_q=as_query(request),
        status_code=status_code,
    )


def _render_detail(
    request: Request,
    task_id: int,
    *,
    error: str = "",
    notice: str = "",
    status_code: int = 200,
):
    owner_id = _owner(request)
    row = tasks.get_task(task_id, owner_id=owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工作不存在")
    as_q = as_query(request)
    return render(
        request,
        "task_detail.html",
        task=tasks.serialize_task(row),
        progress=[
            tasks.serialize_progress(p)
            for p in tasks.list_progress(task_id, owner_id=owner_id)
        ],
        values=_values_from_task(row),
        field_errors={},
        projects=tasks.list_projects(owner_id=owner_id),
        status_options=STATUS_OPTIONS,
        priority_options=PRIORITY_OPTIONS,
        error=error,
        notice=notice,
        as_q=as_q,
        status_code=status_code,
    )


def _render_progress(request: Request, task_id: int, *, error: str = ""):
    owner_id = _owner(request)
    row = tasks.get_task(task_id, owner_id=owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工作不存在")
    # htmx 只在 2xx 时替换目标，因此片段响应固定 200，错误写在片段里
    return render(
        request,
        "partials/tasks_progress.html",
        task=tasks.serialize_task(row),
        progress=[
            tasks.serialize_progress(p)
            for p in tasks.list_progress(task_id, owner_id=owner_id)
        ],
        error=error,
        as_q=as_query(request),
    )


def _render_projects(
    request: Request,
    *,
    error: str = "",
    form: dict[str, str] | None = None,
    status_code: int = 200,
):
    owner_id = _owner(request)
    form = form or {"name": "", "description": "", "color": "#6366f1"}
    items = []
    for project in tasks.list_projects(owner_id=owner_id):
        items.append(
            {
                "project": project,
                "tasks": [
                    tasks.serialize_task(row)
                    for row in tasks.list_tasks(
                        project_id=project["id"],
                        status=tasks.ACTIVE_STATUSES,
                        archived=0,
                        owner_id=owner_id,
                    )
                ],
            }
        )
    return render(
        request,
        "projects.html",
        items=items,
        total=len(items),
        error=error,
        form=form,
        as_q=as_query(request),
        status_code=status_code,
    )


# ---------------------------------------------------------------- 例行过滤（SPEC 16.4）

ROUTINE_CARD_LIMIT = 5


def _accepts(fn, name: str) -> bool:
    """服务层函数是否已声明某个关键字开关。

    并行开发期间 tasks 域的 exclude_routines 可能尚未落地：按签名探测，服务层支持就只走
    服务层（绝不重复剔除），未支持时由本页兜底，两种路径结果一致。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


def _routine_ids(owner_id: int | None) -> set[int]:
    """当前归属下例行实例的 tasks.id（仅在服务层未支持开关时用于页面兜底）。"""
    clause, params = "", ()
    if owner_id is not None:
        clause, params = " AND owner_id = ?", (int(owner_id),)
    rows = db.query("SELECT id FROM tasks WHERE routine_id IS NOT NULL" + clause, params)
    return {int(row["id"]) for row in rows}


def _routine_badges(task_ids) -> dict[int, str]:
    """task_id -> 「例行 · 每周」：例行实例就是 tasks 行，serialize_task 不带模板字段。"""
    ids = sorted({int(value) for value in task_ids})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.query(
        "SELECT t.id, r.freq FROM tasks t JOIN routines r ON r.id = t.routine_id"
        f" WHERE t.id IN ({marks})",
        tuple(ids),
    )
    return {int(row["id"]): "例行 · " + routines.freq_label(row["freq"]) for row in rows}


def _mark_routine_items(items) -> None:
    """给列表里混着的例行实例补徽标（只有 ?routines=1 时才需要）。"""
    badges = _routine_badges([item["id"] for item in items])
    for item in items:
        badge = badges.get(int(item["id"]))
        if badge:
            item["is_routine"] = True
            item["routine_badge"] = badge


def _buckets_without_routines(owner_id: int | None) -> dict:
    """仪表盘五分区排除例行实例：例行统一在「本期例行」卡片里看（SPEC 16.4）。"""
    if _accepts(tasks.dashboard_buckets, "exclude_routines"):
        return tasks.dashboard_buckets(owner_id=owner_id, exclude_routines=True)
    buckets = tasks.dashboard_buckets(owner_id=owner_id)
    ids = _routine_ids(owner_id)
    if ids:
        for key in tasks.BUCKET_KEYS:
            buckets[key] = [item for item in buckets[key] if int(item["id"]) not in ids]
        buckets["counts"] = {key: len(buckets[key]) for key in tasks.BUCKET_KEYS}
    return buckets


def _week_items(view: dict) -> list[dict]:
    """周视图里出现的所有工作行（天格 + 本周无日期 + 周外）。"""
    return (
        [item for day in view["days"] for item in day["tasks"]]
        + list(view["no_due"])
        + list(view["outside_week"])
    )


def _week_data(offset: int, owner_id: int | None, include_routines: bool) -> dict:
    """周视图数据：默认排除例行实例，?routines=1 时包含并带上例行徽标（SPEC 16.4）。"""
    if _accepts(tasks.week_view, "exclude_routines"):
        view = tasks.week_view(offset, owner_id=owner_id, exclude_routines=not include_routines)
    else:
        view = tasks.week_view(offset, owner_id=owner_id)
        ids = _routine_ids(owner_id) if not include_routines else set()
        if ids:
            for day in view["days"]:
                day["tasks"] = [item for item in day["tasks"] if int(item["id"]) not in ids]
            view["no_due"] = [item for item in view["no_due"] if int(item["id"]) not in ids]
            view["outside_week"] = [
                item for item in view["outside_week"] if int(item["id"]) not in ids
            ]
            view["counts"] = {
                "total": sum(len(day["tasks"]) for day in view["days"])
                + len(view["no_due"])
                + len(view["outside_week"]),
                "no_due": len(view["no_due"]),
                "outside_week": len(view["outside_week"]),
            }
    if include_routines:
        _mark_routine_items(_week_items(view))
    return view


def _week_url(offset: int, show_routines: bool, as_q: str) -> str:
    """周视图链接：保留 offset、例行开关与管理员 ?as=。"""
    query = f"offset={offset}" + ("&routines=1" if show_routines else "")
    return "/week?" + query + (("&" + as_q.lstrip("?")) if as_q else "")


def _archive_groups(owner_id: int | None) -> list[dict]:
    """归档只保留一次性成果，例行历史在 /routines/{id}（SPEC 16.4）。"""
    if _accepts(tasks.archive_groups, "exclude_routines"):
        return tasks.archive_groups(owner_id=owner_id, exclude_routines=True)
    groups = tasks.archive_groups(owner_id=owner_id)
    ids = _routine_ids(owner_id)
    if not ids:
        return groups
    kept = []
    for group in groups:
        items = [item for item in group["tasks"] if int(item["id"]) not in ids]
        if items:
            kept.append({**group, "tasks": items})
    return kept


def _routine_state(item: dict) -> tuple[str, str]:
    """卡片状态徽标：未完成清单里只有待办/进行中，待办过了截止日显示逾期。"""
    if item["status"] == "doing":
        return "进行中", "badge-doing"
    due = str(item.get("due_date") or "")[:10]
    if due and due < timeutil.today().isoformat():
        return "逾期", "badge-overdue"
    return "未开始", "badge-todo"


def _routine_card(owner_id: int | None, as_q: str) -> dict:
    """「本期例行」卡片数据：未完成 n / 共 m + 前 5 条（SPEC 16.4）。"""
    instances = routines.period_instances(owner_id=owner_id)
    unfinished = [item for item in instances if item["status"] in tasks.ACTIVE_STATUSES]
    items = []
    for item in unfinished[:ROUTINE_CARD_LIMIT]:
        state, state_class = _routine_state(item)
        due = str(item.get("due_date") or "")[:10]
        items.append(
            {
                "id": item["id"],
                "routine_id": item["routine_id"],
                "title": item["title"],
                "freq_label": item.get("routine_freq_label") or "例行",
                "state": state,
                "state_class": state_class,
                "due_cn": timeutil.fmt_cn(due) if due else "未设截止",
                "due_time": item.get("due_time"),
                "detail_url": f"/routines/{int(item['routine_id'])}{as_q}",
            }
        )
    return {
        "open": len(unfinished),
        "total": len(instances),
        "rows": items,
        "url": "/routines" + as_q,
    }


# ---------------------------------------------------------------- 仪表盘

@pages.get("/")
def dashboard(request: Request):
    owner_id = _owner(request)
    routines.ensure_periods(owner_id=owner_id)  # SPEC 16.5：渲染前生成本期例行的实例
    buckets = _buckets_without_routines(owner_id)
    monday = timeutil.week_start(timeutil.today())
    as_q = as_query(request)
    return render(
        request,
        "dashboard.html",
        buckets=buckets,
        counts=buckets["counts"],
        active_total=sum(buckets["counts"].values()),
        soon_days=tasks.soon_days(),
        this_week_start=monday.isoformat(),
        this_week_label=timeutil.fmt_cn(monday),
        priority_options=PRIORITY_OPTIONS,
        routine_card=_routine_card(owner_id, as_q),
        owner_switch=owner_switch_context(request),
        as_q=as_q,
    )


# ---------------------------------------------------------------- 周视图

@pages.get("/week")
def week_page(
    request: Request,
    offset: int = Query(0),
    routines_flag: int = Query(0, alias="routines"),
):
    owner_id = _owner(request)
    safe_offset = max(-104, min(104, offset))
    include_routines = bool(routines_flag)
    routines.ensure_periods(owner_id=owner_id)  # SPEC 16.5：渲染前生成本期例行的实例
    view = _week_data(safe_offset, owner_id, include_routines)
    nav_label = {-1: "上一周", 0: "本周", 1: "下一周"}.get(safe_offset, f"{safe_offset:+d} 周")
    as_q = as_query(request)
    return render(
        request,
        "week.html",
        view=view,
        offset=safe_offset,
        nav_label=nav_label,
        is_current_week=(safe_offset == 0),
        show_routines=include_routines,
        toggle_label="隐藏例行" if include_routines else "显示例行",
        prev_url=_week_url(safe_offset - 1, include_routines, as_q),
        this_url=_week_url(0, include_routines, as_q),
        next_url=_week_url(safe_offset + 1, include_routines, as_q),
        toggle_url=_week_url(safe_offset, not include_routines, as_q),
        as_q=as_q,
    )


# ---------------------------------------------------------------- 新建 / 编辑

@pages.get("/tasks/new")
def new_task_page(request: Request, project_id: int | None = Query(None)):
    return _render_form(
        request,
        values=_form_values(project_id=str(project_id) if project_id else ""),
        errors=[],
        field_errors={},
        form_action="/tasks",
        page_title="新建工作",
        submit_label="创建",
        cancel_url="/",
    )


@pages.post("/tasks")
def create_task_page(
    request: Request,
    title: str = Form(""),
    detail: str = Form(""),
    done_criteria: str = Form(""),
    due_date: str = Form(""),
    due_time: str = Form(""),
    priority: str = Form("2"),
    project_id: str = Form(""),
    week_start: str = Form(""),
    tags: str = Form(""),
    status: str = Form("todo"),
):
    values = _form_values(
        title, detail, done_criteria, due_date, due_time, priority, project_id, week_start, tags, status
    )
    errors, field_errors = _validate(values)
    if errors:
        return _render_form(
            request,
            values=values,
            errors=errors,
            field_errors=field_errors,
            form_action="/tasks",
            page_title="新建工作",
            submit_label="创建",
            cancel_url="/",
            status_code=400,
        )
    task_id = tasks.create_task(_payload(values), owner_id=_owner(request))
    return _redirect(f"/tasks/{task_id}")


@pages.get("/tasks/{task_id}")
def task_detail_page(request: Request, task_id: int, error: str = Query(""), notice: str = Query("")):
    return _render_detail(request, task_id, error=error, notice=notice)


@pages.post("/tasks/{task_id}/edit")
def update_task_page(
    request: Request,
    task_id: int,
    title: str = Form(""),
    detail: str = Form(""),
    done_criteria: str = Form(""),
    due_date: str = Form(""),
    due_time: str = Form(""),
    priority: str = Form("2"),
    project_id: str = Form(""),
    week_start: str = Form(""),
    tags: str = Form(""),
    status: str = Form("todo"),
):
    owner_id = _owner(request)
    if tasks.get_task(task_id, owner_id=owner_id) is None:
        raise HTTPException(status_code=404, detail="工作不存在")
    values = _form_values(
        title, detail, done_criteria, due_date, due_time, priority, project_id, week_start, tags, status
    )
    errors, field_errors = _validate(values)
    if errors:
        return _render_form(
            request,
            values=values,
            errors=errors,
            field_errors=field_errors,
            form_action=f"/tasks/{task_id}/edit",
            page_title="编辑工作",
            submit_label="保存",
            cancel_url=f"/tasks/{task_id}",
            status_code=400,
        )
    try:
        tasks.update_task(task_id, _payload(values), owner_id=owner_id)
    except ValueError as exc:
        return _render_detail(request, task_id, error=str(exc), status_code=400)
    return _redirect(f"/tasks/{task_id}")


# ---------------------------------------------------------------- 状态切换

@pages.post("/tasks/{task_id}/status")
def set_status_page(request: Request, task_id: int, status: str = Form("")):
    owner_id = _owner(request)
    if tasks.get_task(task_id, owner_id=owner_id) is None:
        raise HTTPException(status_code=404, detail="工作不存在")
    try:
        tasks.set_status(task_id, status, owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _is_hx(request):
        return render(
            request,
            "partials/tasks_statusbar.html",
            task=tasks.serialize_task(tasks.get_task(task_id, owner_id=owner_id)),
            status_options=STATUS_OPTIONS,
            as_q=as_query(request),
        )
    return _redirect(f"/tasks/{task_id}")


# ---------------------------------------------------------------- 进展

@pages.post("/tasks/{task_id}/progress")
def add_progress_page(
    request: Request,
    task_id: int,
    content: str = Form(""),
    link: str = Form(""),
):
    owner_id = _owner(request)
    if tasks.get_task(task_id, owner_id=owner_id) is None:
        raise HTTPException(status_code=404, detail="工作不存在")
    text = content.strip()
    if _is_hx(request):
        error = ""
        if not text:
            error = "进展内容不能为空"
        else:
            tasks.add_progress(task_id, text, link.strip() or None, owner_id=owner_id)
        return _render_progress(request, task_id, error=error)
    if not text:
        return _render_detail(request, task_id, error="进展内容不能为空", status_code=400)
    tasks.add_progress(task_id, text, link.strip() or None, owner_id=owner_id)
    return _redirect(f"/tasks/{task_id}#progress")


@pages.post("/progress/{pid}/delete")
def delete_progress_page(request: Request, pid: int):
    owner_id = _owner(request)
    entry = tasks.get_progress(pid, owner_id=owner_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="进展不存在")
    task_id = int(entry["task_id"])
    tasks.delete_progress(pid, owner_id=owner_id)
    return _redirect(f"/tasks/{task_id}#progress")


@pages.post("/tasks/{task_id}/delete")
def delete_task_page(request: Request, task_id: int):
    owner_id = _owner(request)
    if tasks.get_task(task_id, owner_id=owner_id) is None:
        raise HTTPException(status_code=404, detail="工作不存在")
    tasks.delete_task(task_id, owner_id=owner_id)
    return _redirect("/archive")


# ---------------------------------------------------------------- 归档

@pages.get("/archive")
def archive_page(request: Request):
    groups = _archive_groups(_owner(request))
    return render(
        request,
        "archive.html",
        groups=groups,
        total=sum(len(group["tasks"]) for group in groups),
        owner_switch=owner_switch_context(request),
        as_q=as_query(request),
    )


# ---------------------------------------------------------------- 项目

@pages.get("/projects")
def projects_page(request: Request):
    return _render_projects(request)


@pages.post("/projects")
def create_project_page(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    color: str = Form("#6366f1"),
):
    try:
        tasks.create_project(name, description, color, owner_id=_owner(request))
    except ValueError as exc:
        return _render_projects(
            request,
            error=str(exc),
            form={"name": name, "description": description, "color": color or "#6366f1"},
            status_code=400,
        )
    return _redirect("/projects")
