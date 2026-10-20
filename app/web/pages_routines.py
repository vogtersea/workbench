"""例行工作页面路由（SPEC.md 16.4 / 16.5 / 16.6）。

约定与其余页面模块一致：本模块只暴露 pages = APIRouter()，路径即 SPEC 的页面路径
（main.py 不加前缀）。所有操作都能用原生 form 完成；「完成 / 跳过」按钮在 HX-Request 时
返回片段 partials/routines_instance_row.html，普通提交 303 回原页。

多用户（SPEC 15.1 / 15.7 / 16.8）：每个 handler 用 auth.effective_owner_id(request) 取归属 id，
None 表示开放 / 单口令模式（不过滤，保持改造前行为）；别人的模板等同于不存在（404）；
管理员 ?as=<user_id> 一路透传到 ensure_periods / 查询 / 表单 action / 回跳地址。
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app import auth, timeutil
from app.services import routines, tasks
from app.templating import render
from app.web.pages_tasks import as_query, owner_switch_context

pages = APIRouter()

PRIORITY_OPTIONS = ((3, "高"), (2, "中"), (1, "低"))
FREQ_CHOICES = tuple((freq, routines.freq_label(freq)) for freq in routines.FREQS)
DUE_RULE_CHOICES = (("period_end", "周期末"), ("day_of_period", "周期第 N 天"))
DUE_DAY_MAX = 366
HISTORY_LIMIT = 24
STATUS_BADGES: dict[str, tuple[str, str]] = {
    "todo": ("未开始", "badge-todo"),
    "doing": ("进行中", "badge-doing"),
    "done": ("已完成", "badge-done"),
    "cancelled": ("已跳过", "badge-cancelled"),
}


# ---------------------------------------------------------------- 身份 / 归属

def _owner(request: Request) -> int | None:
    """当前生效的数据归属 id；None = 开放/单口令模式（不过滤，保持改造前行为）。"""
    return auth.effective_owner_id(request)


def _is_hx(request: Request) -> bool:
    return request.headers.get("hx-request", "").strip().lower() == "true"


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _with_as(url: str, as_q: str) -> str:
    """把管理员 ?as= 串到链接上（已带 as= 时不重复）。"""
    if not as_q or "as=" in url:
        return url
    return url + ("&" if "?" in url else "?") + as_q.lstrip("?")


def _notice_url(url: str, text: str, as_q: str) -> str:
    """回跳地址附带一条提示文案（中文按 percent-encoding 放进 Location）。"""
    return _with_as(url + ("&" if "?" in url else "?") + "notice=" + quote(text), as_q)


def _back(next_url: str, default: str, as_q: str) -> str:
    """表单回跳目标：只接受本域路径，避免开放重定向。"""
    target = str(next_url or "").strip()
    if not target.startswith("/routines"):
        target = default
    return _with_as(target, as_q)


# ---------------------------------------------------------------- 状态徽标

def _is_overdue(due_date) -> bool:
    text = str(due_date or "")[:10]
    return bool(text) and text < timeutil.today().isoformat()


def _state_of(item: dict) -> tuple[str, str]:
    """(中文状态, badge 修饰类)：模板 dict 看 instance_status，实例 dict 看 status。"""
    status = item.get("instance_status") or item.get("status")
    if not status:
        return ("未开始", "badge-muted") if item.get("not_started") else ("未生成", "badge-muted")
    if status == "doing":
        return STATUS_BADGES["doing"]
    if status in tasks.ACTIVE_STATUSES and _is_overdue(item.get("due_date")):
        return ("逾期", "badge-overdue")
    return STATUS_BADGES.get(status, (str(status), "badge-muted"))


def _routine_row(item: dict, as_q: str) -> dict:
    """模板列表一行：状态徽标 + 详情/跳过/启停/删除的目标地址。"""
    state, state_class = _state_of(item)
    routine_id = item["id"]
    return {
        **item,
        "state": state,
        "state_class": state_class,
        "detail_url": _with_as(f"/routines/{routine_id}", as_q),
        "skip_url": _with_as(f"/routines/{routine_id}/skip", as_q),
        "toggle_url": _with_as(f"/routines/{routine_id}/toggle", as_q),
        "delete_url": _with_as(f"/routines/{routine_id}/delete", as_q),
    }


def _instance_row(item: dict, as_q: str, freq: str = "") -> dict:
    """「本期例行任务」一行：完成/跳过按钮的地址、频率徽标与截止日中文标签。"""
    state, state_class = _state_of(item)
    freq_key = item.get("routine_freq") or freq
    label = routines.freq_label(freq_key)
    due = str(item.get("due_date") or "")[:10]
    period_key = item.get("period_key")
    return {
        **item,
        "state": state,
        "state_class": state_class,
        "freq_label": label,
        "routine_badge": ("例行 · " + label) if label else "例行",
        "due_cn": timeutil.fmt_cn(due) if due else "未设截止",
        "period_display": routines.period_display(freq_key, period_key) if period_key else "",
        "task_url": _with_as(f"/tasks/{item['id']}", as_q),
        "template_url": _with_as(f"/routines/{item['routine_id']}", as_q),
        "done_url": _with_as(f"/routines/instance/{item['id']}/done", as_q),
        "skip_url": _with_as(f"/routines/{item['routine_id']}/skip", as_q),
    }


# ---------------------------------------------------------------- 表单值与校验

def _form_values(
    title="",
    detail="",
    done_criteria="",
    freq="weekly",
    due_rule="period_end",
    due_day="",
    due_time="",
    priority="2",
    project_id="",
    tags="",
) -> dict[str, str]:
    return {
        "title": title or "",
        "detail": detail or "",
        "done_criteria": done_criteria or "",
        "freq": str(freq or "weekly"),
        "due_rule": str(due_rule or "period_end"),
        "due_day": "" if due_day in (None, "") else str(due_day),
        "due_time": due_time or "",
        "priority": str(priority if priority not in (None, "") else "2"),
        "project_id": "" if project_id in (None, "") else str(project_id),
        "tags": tags or "",
    }


def _values_from_row(row) -> dict[str, str]:
    return _form_values(
        title=row["title"],
        detail=row["detail"],
        done_criteria=row["done_criteria"],
        freq=row["freq"],
        due_rule=row["due_rule"],
        due_day=row["due_day"],
        due_time=row["due_time"],
        priority=row["priority"],
        project_id=row["project_id"],
        tags=row["tags"],
    )


def _time_ok(text: str) -> bool:
    try:
        datetime.strptime(text, "%H:%M")
        return True
    except ValueError:
        return False


def _validate(values: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """返回 (总错误列表, 字段错误)；服务层仍会二次校验（SPEC 16.3）。"""
    errors: list[str] = []
    field_errors: dict[str, str] = {}

    if not values["title"].strip():
        field_errors["title"] = "标题不能为空"
        errors.append(field_errors["title"])
    if values["freq"] not in routines.FREQS:
        field_errors["freq"] = "周期取值不合法"
        errors.append(field_errors["freq"])
    if values["due_rule"] not in routines.DUE_RULES:
        field_errors["due_rule"] = "截止日规则不合法"
        errors.append(field_errors["due_rule"])
    if values["due_rule"] == "day_of_period":
        raw = values["due_day"].strip()
        number = 0
        if raw:
            try:
                number = int(float(raw))
            except ValueError:
                number = 0
        if not 1 <= number <= DUE_DAY_MAX:
            field_errors["due_day"] = f"第几天需为 1–{DUE_DAY_MAX} 的整数"
            errors.append(field_errors["due_day"])
    if values["due_time"].strip() and not _time_ok(values["due_time"].strip()):
        field_errors["due_time"] = "截止时间格式不正确，应为 HH:MM"
        errors.append(field_errors["due_time"])
    if values["project_id"].strip() and not values["project_id"].strip().isdigit():
        field_errors["project_id"] = "项目取值不合法"
        errors.append(field_errors["project_id"])

    return errors, field_errors


def _payload(values: dict[str, str]) -> dict:
    return {
        "title": values["title"].strip(),
        "detail": values["detail"],
        "done_criteria": values["done_criteria"],
        "freq": values["freq"].strip(),
        "due_rule": values["due_rule"].strip(),
        "due_day": values["due_day"].strip() or None,
        "due_time": values["due_time"].strip() or None,
        "priority": values["priority"].strip() or "2",
        "project_id": values["project_id"].strip() or None,
        "tags": values["tags"],
    }


# ---------------------------------------------------------------- 渲染

def _list_context(
    request: Request,
    *,
    values: dict[str, str] | None = None,
    errors: list[str] | None = None,
    field_errors: dict[str, str] | None = None,
    error: str = "",
    notice: str = "",
) -> dict:
    """列表页上下文：模板按 freq 分组 + 本期例行任务清单（SPEC 16.4）。"""
    owner_id = _owner(request)
    routines.ensure_periods(owner_id=owner_id)  # SPEC 16.5：渲染前惰性生成本期实例
    as_q = as_query(request)
    items = [
        _routine_row(routines.serialize_routine(row), as_q)
        for row in routines.list_routines(owner_id=owner_id)
    ]
    instances = [
        _instance_row(item, as_q) for item in routines.period_instances(owner_id=owner_id)
    ]
    groups = []
    for freq in routines.FREQS:
        same = [item for item in items if item["freq"] == freq]
        if same:
            groups.append({"freq": freq, "label": routines.freq_label(freq), "rows": same})
    return {
        "groups": groups,
        "total": len(items),
        "active_total": sum(1 for item in items if item["active"]),
        "instances": instances,
        "open_count": sum(1 for item in instances if item["status"] in tasks.ACTIVE_STATUSES),
        "values": values or _form_values(),
        "errors": list(errors or []),
        "field_errors": field_errors or {},
        "freq_choices": FREQ_CHOICES,
        "due_rule_choices": DUE_RULE_CHOICES,
        "priority_options": PRIORITY_OPTIONS,
        "due_day_max": DUE_DAY_MAX,
        "projects": tasks.list_projects(owner_id=owner_id),
        "owner_switch": owner_switch_context(request),
        "as_q": as_q,
        "error": error,
        "notice": notice,
    }


def _render_list(
    request: Request,
    *,
    values: dict[str, str] | None = None,
    errors: list[str] | None = None,
    field_errors: dict[str, str] | None = None,
    error: str = "",
    notice: str = "",
    status_code: int = 200,
):
    return render(
        request,
        "routines.html",
        status_code=status_code,
        **_list_context(
            request,
            values=values,
            errors=errors,
            field_errors=field_errors,
            error=error,
            notice=notice,
        ),
    )


def _render_detail(
    request: Request,
    routine_id: int,
    *,
    values: dict[str, str] | None = None,
    errors: list[str] | None = None,
    field_errors: dict[str, str] | None = None,
    error: str = "",
    notice: str = "",
    status_code: int = 200,
):
    """模板详情 + 编辑表单 + 最近 24 期历史（SPEC 16.4）。"""
    owner_id = _owner(request)
    row = routines.get_routine(routine_id, owner_id=owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="例行模板不存在")
    routines.ensure_periods(owner_id=owner_id)  # SPEC 16.5
    row = routines.get_routine(routine_id, owner_id=owner_id) or row
    as_q = as_query(request)
    routine = _routine_row(routines.serialize_routine(row), as_q)
    history = []
    for instance in routines.list_instances(routine_id, limit=HISTORY_LIMIT):
        data = routines.serialize_instance(instance)
        state, state_class = _state_of(data)
        history.append(
            {
                **data,
                "state": state,
                "state_class": state_class,
                "period_display": routines.period_display(routine["freq"], data["period_key"]),
                "is_current": data["period_key"] == routine["period_key"],
                "completed_label": str(data.get("completed_at") or "")[:16].replace("T", " "),
                "due_cn": timeutil.fmt_cn(data["due_date"]) if data.get("due_date") else "",
            }
        )
    return render(
        request,
        "routine_detail.html",
        status_code=status_code,
        routine=routine,
        current_row=(
            _instance_row(routine["instance"], as_q, routine["freq"])
            if routine["instance"]
            else None
        ),
        history=history,
        instance_count=routines.count_instances(routine_id),
        values=values or _values_from_row(row),
        errors=list(errors or []),
        field_errors=field_errors or {},
        freq_choices=FREQ_CHOICES,
        due_rule_choices=DUE_RULE_CHOICES,
        priority_options=PRIORITY_OPTIONS,
        due_day_max=DUE_DAY_MAX,
        projects=tasks.list_projects(owner_id=owner_id),
        owner_switch=owner_switch_context(request),
        as_q=as_q,
        error=error,
        notice=notice,
    )


# ---------------------------------------------------------------- 列表 / 新建

@pages.get("/routines")
def routines_page(request: Request, error: str = Query(""), notice: str = Query("")):
    return _render_list(request, error=error, notice=notice)


@pages.post("/routines")
def create_routine_page(
    request: Request,
    title: str = Form(""),
    detail: str = Form(""),
    done_criteria: str = Form(""),
    freq: str = Form("weekly"),
    due_rule: str = Form("period_end"),
    due_day: str = Form(""),
    due_time: str = Form(""),
    priority: str = Form("2"),
    project_id: str = Form(""),
    tags: str = Form(""),
):
    values = _form_values(
        title, detail, done_criteria, freq, due_rule, due_day, due_time, priority, project_id, tags
    )
    errors, field_errors = _validate(values)
    if not errors:
        try:
            routine_id = routines.create_routine(_payload(values), owner_id=_owner(request))
        except ValueError as exc:
            errors = [str(exc)]
        else:
            return _redirect(_with_as(f"/routines/{routine_id}", as_query(request)))
    return _render_list(
        request, values=values, errors=errors, field_errors=field_errors, status_code=400
    )


# ---------------------------------------------------------------- 详情 / 编辑

@pages.get("/routines/{routine_id}")
def routine_detail_page(request: Request, routine_id: int, error: str = Query(""), notice: str = Query("")):
    return _render_detail(request, routine_id, error=error, notice=notice)


@pages.post("/routines/{routine_id}/edit")
def update_routine_page(
    request: Request,
    routine_id: int,
    title: str = Form(""),
    detail: str = Form(""),
    done_criteria: str = Form(""),
    freq: str = Form("weekly"),
    due_rule: str = Form("period_end"),
    due_day: str = Form(""),
    due_time: str = Form(""),
    priority: str = Form("2"),
    project_id: str = Form(""),
    tags: str = Form(""),
    next_url: str = Form("", alias="next"),
):
    owner_id = _owner(request)
    if routines.get_routine(routine_id, owner_id=owner_id) is None:
        raise HTTPException(status_code=404, detail="例行模板不存在")
    values = _form_values(
        title, detail, done_criteria, freq, due_rule, due_day, due_time, priority, project_id, tags
    )
    errors, field_errors = _validate(values)
    if not errors:
        try:
            routines.update_routine(routine_id, _payload(values), owner_id=owner_id)
        except ValueError as exc:
            errors = [str(exc)]
        else:
            as_q = as_query(request)
            return _redirect(
                _notice_url(
                    _back(next_url, f"/routines/{routine_id}", as_q), "模板已保存", as_q
                )
            )
    return _render_detail(
        request,
        routine_id,
        values=values,
        errors=errors,
        field_errors=field_errors,
        status_code=400,
    )


# ---------------------------------------------------------------- 启停 / 删除

@pages.post("/routines/{routine_id}/toggle")
def toggle_routine_page(
    request: Request,
    routine_id: int,
    active: str = Form("1"),
    next_url: str = Form("", alias="next"),
):
    owner_id = _owner(request)
    want_active = str(active).strip().lower() not in ("0", "false", "off", "", "no")
    try:
        routines.set_active(routine_id, want_active, owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    as_q = as_query(request)
    text = "已启用模板，下个周期起照常生成" if want_active else "已停用模板（历史实例保留）"
    return _redirect(_notice_url(_back(next_url, "/routines", as_q), text, as_q))


@pages.post("/routines/{routine_id}/delete")
def delete_routine_page(
    request: Request,
    routine_id: int,
    purge: str = Form("0"),
    confirm: str = Form("0"),
    next_url: str = Form("", alias="next"),
):
    """默认只停用；purge=1 且 confirm=1 才连同历史实例彻底删除（SPEC 16.6）。"""
    owner_id = _owner(request)
    if routines.get_routine(routine_id, owner_id=owner_id) is None:
        raise HTTPException(status_code=404, detail="例行模板不存在")
    purge_now = str(purge).strip().lower() in ("1", "true", "on", "yes")
    confirmed = str(confirm).strip().lower() in ("1", "true", "on", "yes")
    if purge_now and not confirmed:
        count = routines.count_instances(routine_id)
        return _render_detail(
            request,
            routine_id,
            error=f"彻底删除需二次确认：将连同 {count} 条历史实例一起删除。模板未删除。",
            status_code=400,
        )
    try:
        result = routines.delete_routine(routine_id, purge=purge_now, owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    as_q = as_query(request)
    if result["purged"]:
        text = f"已彻底删除模板，连同 {result['deleted_tasks']} 条历史实例"
    else:
        text = "已停用模板（历史实例保留，可随时重新启用）"
    return _redirect(_notice_url(_back(next_url, "/routines", as_q), text, as_q))


# ---------------------------------------------------------------- 跳过本期 / 完成本期

@pages.post("/routines/{routine_id}/skip")
def skip_period_page(request: Request, routine_id: int, next_url: str = Form("", alias="next")):
    owner_id = _owner(request)
    try:
        cancelled = routines.skip_period(routine_id, owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    as_q = as_query(request)
    if _is_hx(request):
        instance = routines.current_instance(routine_id)
        routine = routines.get_routine(routine_id, owner_id=owner_id)
        if instance is None or routine is None:
            raise HTTPException(status_code=404, detail="例行模板不存在")
        return render(
            request,
            "partials/routines_instance_row.html",
            row=_instance_row(routines.serialize_instance(instance), as_q, routine["freq"]),
            as_q=as_q,
        )
    text = "已跳过本期（不计逾期、不再提醒）" if cancelled else "本期暂不生成（起始周期未到）"
    return _redirect(_notice_url(_back(next_url, f"/routines/{routine_id}", as_q), text, as_q))


@pages.post("/routines/instance/{task_id}/done")
def complete_instance_page(request: Request, task_id: int, next_url: str = Form("", alias="next")):
    """一键完成本期实例：复用 tasks.set_status 状态机，303 回原页或返回 htmx 片段。"""
    owner_id = _owner(request)
    row = tasks.get_task(task_id, owner_id=owner_id)
    if row is None or row["routine_id"] is None:
        raise HTTPException(status_code=404, detail="例行实例不存在")
    tasks.set_status(task_id, "done", owner_id=owner_id)
    as_q = as_query(request)
    if _is_hx(request):
        fresh = tasks.get_task(task_id, owner_id=owner_id)
        routine = routines.get_routine(int(row["routine_id"]), owner_id=owner_id)
        if fresh is None:
            raise HTTPException(status_code=404, detail="例行实例不存在")
        data = routines.serialize_instance(fresh)
        return render(
            request,
            "partials/routines_instance_row.html",
            row=_instance_row(data, as_q, routine["freq"] if routine is not None else ""),
            as_q=as_q,
        )
    return _redirect(_back(next_url, "/routines", as_q))
