"""任务域服务：工作 / 进展 / 项目 / 仪表盘分区（SPEC.md 第 5 节）。

第 5 节列出的函数签名是跨模块契约（reports / reminders / api / web 只调用这些），
不得更改。文末的 serialize_* / week_view / archive_groups / get_progress / delete_task
是本域新增的只读或必要助手，不改变既有签名。

多用户归属（SPEC.md 15.7）：所有函数新增关键字参数 owner_id，默认 None 表示不过滤，
即改造前的全局查询（开放 / 单口令模式必须走这条路）。多用户模式下调用方一律传当前
生效用户的 id；写操作先按归属校验，不属于自己的任务一律当作不存在（页面层转 404）。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

from app import db, timeutil
from app.services import settings_store

STATUSES: tuple[str, ...] = ("todo", "doing", "done", "cancelled")
ACTIVE_STATUSES: tuple[str, ...] = ("todo", "doing")
TERMINAL_STATUSES: tuple[str, ...] = ("done", "cancelled")
BUCKET_KEYS: tuple[str, ...] = ("overdue", "today", "soon", "this_week", "no_due")

BASE_SELECT = """
SELECT t.*, p.name AS project_name, p.color AS project_color
  FROM tasks t
  LEFT JOIN projects p ON p.id = t.project_id
"""
ACTIVE_ORDER = (
    " ORDER BY (t.due_date IS NULL), t.due_date, t.due_time, t.priority DESC, t.id DESC"
)
ARCHIVE_ORDER = (
    " ORDER BY (t.completed_at IS NULL), t.completed_at DESC, t.id DESC"
)


def _owner_clause(owner_id: int | None, alias: str = "t") -> tuple[str, tuple]:
    """归属过滤片段（SPEC 15.7）：owner_id 为 None 时返回空，等价于改造前的全局查询。

    alias 必须与所在 SQL 一致：走 BASE_SELECT 的查询用 "t"，单表 UPDATE / DELETE 传 ""。
    """
    if owner_id is None:
        return "", ()
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}owner_id = ?", (int(owner_id),)


def _owner_value(owner_id: int | None) -> int | None:
    """写入用：None 保持 NULL（兼容历史行），否则落当前生效用户 id。"""
    return None if owner_id is None else int(owner_id)


# ---------------------------------------------------------------- 内部清洗工具

def _soon_days() -> int:
    raw = settings_store.get("remind_soon_days", "3")
    try:
        return max(0, int(float(str(raw).strip())))
    except (TypeError, ValueError):
        return 3


def soon_days() -> int:
    """临期窗口天数（settings.remind_soon_days，默认 3），页面提示与分区共用。"""
    return _soon_days()


def _clean_status(value, default: str = "todo") -> str:
    text = str(value or "").strip().lower()
    if not text:
        text = default
    if text not in STATUSES:
        raise ValueError("状态取值不合法：" + text)
    return text


def _clean_date(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    day = timeutil.parse_date(text)
    if day is None:
        raise ValueError("日期格式不正确（应为 YYYY-MM-DD）：" + text)
    return day.isoformat()


def _clean_time(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return parsed.strftime("%H:%M")
    raise ValueError("时间格式不正确（应为 HH:MM）：" + text)


def _clean_priority(value) -> int:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 2
    return number if number in (1, 2, 3) else 2


def _clean_project(value, owner_id: int | None = None) -> int | None:
    """项目外键清洗：跨用户的项目 id 一律丢弃（按不属于自己处理）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        project_id = int(text)
    except (TypeError, ValueError):
        return None
    row = get_project(project_id, owner_id=owner_id)
    return int(row["id"]) if row is not None else None


def _clean_tags(value) -> str:
    parts = [part.strip() for part in str(value or "").split(",")]
    return ",".join(part for part in parts if part)


def _resolve_week(explicit, due_date) -> str:
    """SPEC 7.1：显式选周 > due_date 所在周周一 > 今天所在周周一。"""
    week = timeutil.parse_date(explicit)
    if week is not None:
        return timeutil.week_start(week).isoformat()
    due = timeutil.parse_date(due_date)
    if due is not None:
        return timeutil.week_start(due).isoformat()
    return timeutil.week_start(timeutil.today()).isoformat()


def _status_effects(status: str) -> dict:
    """SPEC 7.2：done 即销号归档；其余状态一律回到活跃。"""
    if status == "done":
        return {"completed_at": timeutil.now_iso(), "archived": 1}
    return {"completed_at": None, "archived": 0}


def _write_task(task_id: int, sets: dict, owner_id: int | None = None) -> None:
    assignments = ", ".join(f"{key} = ?" for key in sets)
    clause, extra = _owner_clause(owner_id, alias="")
    db.execute(
        f"UPDATE tasks SET {assignments} WHERE id = ?" + clause,
        (*sets.values(), int(task_id), *extra),
    )


# ---------------------------------------------------------------- 工作条目读写

def list_tasks(
    *,
    week_start=None,
    project_id=None,
    status=None,
    archived=0,
    q=None,
    owner_id: int | None = None,
    exclude_routines: bool = False,
    routine_id=None,
) -> list[sqlite3.Row]:
    """按周 / 项目 / 状态 / 归档 / 关键词筛选工作，按紧急度排序。

    exclude_routines=True 只返回一次性工作（routine_id IS NULL），供仪表盘 / 周视图 /
    归档页排除例行实例（SPEC 16.4）；默认 False，既有调用方行为不变。
    routine_id 给定则只看该模板的实例（/routines 明细用），与 exclude_routines 互斥。
    """
    clauses: list[str] = []
    params: list = []

    if owner_id is not None:
        clauses.append("t.owner_id = ?")
        params.append(int(owner_id))
    if exclude_routines:
        clauses.append("t.routine_id IS NULL")
    elif routine_id not in (None, "", 0, "0"):
        try:
            clauses.append("t.routine_id = ?")
            params.append(int(routine_id))
        except (TypeError, ValueError):
            pass
    if archived is not None:
        clauses.append("t.archived = ?")
        params.append(int(archived))
    week = timeutil.parse_date(week_start)
    if week is not None:
        clauses.append("t.week_start = ?")
        params.append(week.isoformat())
    elif week_start:
        clauses.append("t.week_start = ?")
        params.append(str(week_start).strip())
    if project_id not in (None, "", 0, "0"):
        try:
            clauses.append("t.project_id = ?")
            params.append(int(project_id))
        except (TypeError, ValueError):
            pass
    if status:
        wanted = [status] if isinstance(status, str) else list(status)
        wanted = [str(item).strip() for item in wanted if str(item).strip()]
        if wanted:
            clauses.append("t.status IN (" + ", ".join("?" * len(wanted)) + ")")
            params.extend(wanted)
    if q:
        like = "%" + str(q).strip() + "%"
        clauses.append("(t.title LIKE ? OR t.detail LIKE ? OR t.done_criteria LIKE ? OR t.tags LIKE ?)")
        params.extend([like, like, like, like])

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return db.query(BASE_SELECT + where + ACTIVE_ORDER, tuple(params))


def get_task(task_id: int, *, owner_id: int | None = None) -> sqlite3.Row | None:
    """取单条工作；带 owner_id 时别人的工作等同于不存在（返回 None）。"""
    clause, extra = _owner_clause(owner_id)
    return db.one(BASE_SELECT + " WHERE t.id = ?" + clause, (int(task_id), *extra))


def create_task(data: dict, *, owner_id: int | None = None) -> int:
    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("标题不能为空")
    due_date = _clean_date(data.get("due_date"))
    due_time = _clean_time(data.get("due_time"))
    if due_time and not due_date:
        raise ValueError("填写截止时间时必须同时填写截止日期")
    status = _clean_status(data.get("status"))
    week_start = _resolve_week(data.get("week_start"), due_date)
    now = timeutil.now_iso()
    effects = _status_effects(status) if status == "done" else {}
    return db.execute(
        """
        INSERT INTO tasks(project_id, title, detail, done_criteria, due_date, due_time,
                          status, priority, week_start, tags, completed_at, archived,
                          created_at, updated_at, owner_id)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _clean_project(data.get("project_id"), owner_id=owner_id),
            title,
            str(data.get("detail") or ""),
            str(data.get("done_criteria") or ""),
            due_date,
            due_time,
            status,
            _clean_priority(data.get("priority")),
            week_start,
            _clean_tags(data.get("tags")),
            effects.get("completed_at"),
            effects.get("archived", 0),
            now,
            now,
            _owner_value(owner_id),
        ),
        returning_id=True,
    )


def update_task(task_id: int, data: dict, *, owner_id: int | None = None) -> None:
    current = get_task(task_id, owner_id=owner_id)
    if current is None:
        raise ValueError("工作不存在")

    sets: dict = {}
    if "title" in data:
        title = str(data.get("title") or "").strip()
        if not title:
            raise ValueError("标题不能为空")
        sets["title"] = title
    if "detail" in data:
        sets["detail"] = str(data.get("detail") or "")
    if "done_criteria" in data:
        sets["done_criteria"] = str(data.get("done_criteria") or "")
    if "tags" in data:
        sets["tags"] = _clean_tags(data.get("tags"))

    due_date = _clean_date(data["due_date"]) if "due_date" in data else current["due_date"]
    due_time = _clean_time(data["due_time"]) if "due_time" in data else current["due_time"]
    if due_time and not due_date:
        raise ValueError("填写截止时间时必须同时填写截止日期")
    if "due_date" in data:
        sets["due_date"] = due_date
    if "due_time" in data:
        sets["due_time"] = due_time
    if "priority" in data:
        sets["priority"] = _clean_priority(data.get("priority"))
    if "project_id" in data:
        sets["project_id"] = _clean_project(data.get("project_id"), owner_id=owner_id)

    explicit_week = str(data.get("week_start") or "").strip()
    if explicit_week:
        sets["week_start"] = _resolve_week(explicit_week, None)
    elif "due_date" in data and due_date:
        sets["week_start"] = _resolve_week(None, due_date)

    if "status" in data:
        status = _clean_status(data.get("status"))
        sets["status"] = status
        if status != current["status"]:
            sets.update(_status_effects(status))

    sets["updated_at"] = timeutil.now_iso()
    _write_task(task_id, sets, owner_id=owner_id)


def set_status(task_id: int, status: str, *, owner_id: int | None = None) -> None:
    """done -> completed_at=now、archived=1；恢复 todo -> archived=0、completed_at=NULL。"""
    current = get_task(task_id, owner_id=owner_id)
    if current is None:
        raise ValueError("工作不存在")
    status = _clean_status(status)
    sets: dict = {"status": status}
    if status != current["status"]:
        sets.update(_status_effects(status))
    sets["updated_at"] = timeutil.now_iso()
    _write_task(task_id, sets, owner_id=owner_id)


def delete_task(task_id: int, *, owner_id: int | None = None) -> None:
    """删除工作并级联删除其进展（显式两条语句，便于自测核对）。"""
    clause, extra = _owner_clause(owner_id, alias="")
    with db.cursor() as conn:
        conn.execute(
            "DELETE FROM progress_entries WHERE task_id IN "
            "(SELECT id FROM tasks WHERE id = ?" + clause + ")",
            (int(task_id), *extra),
        )
        conn.execute("DELETE FROM tasks WHERE id = ?" + clause, (int(task_id), *extra))


# ---------------------------------------------------------------- 进展

def add_progress(
    task_id: int, content: str, link: str | None = None, *, owner_id: int | None = None
) -> int:
    text = str(content or "").strip()
    if not text:
        raise ValueError("进展内容不能为空")
    if get_task(task_id, owner_id=owner_id) is None:
        raise ValueError("工作不存在")
    now = timeutil.now_iso()
    pid = db.execute(
        "INSERT INTO progress_entries(task_id, content, link, created_at) VALUES(?, ?, ?, ?)",
        (int(task_id), text, (str(link).strip() or None) if link else None, now),
        returning_id=True,
    )
    clause, extra = _owner_clause(owner_id, alias="")
    db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?" + clause,
        (now, int(task_id), *extra),
    )
    return pid


def list_progress(task_id: int, *, owner_id: int | None = None) -> list[sqlite3.Row]:
    """进展通过 task 归属（SPEC 15.3）：带 owner_id 时 join tasks 过滤。"""
    if owner_id is None:
        return db.query(
            "SELECT * FROM progress_entries WHERE task_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (int(task_id),),
        )
    return db.query(
        "SELECT pe.* FROM progress_entries pe JOIN tasks t ON t.id = pe.task_id "
        "WHERE pe.task_id = ? AND t.owner_id = ? "
        "ORDER BY pe.created_at DESC, pe.id DESC",
        (int(task_id), int(owner_id)),
    )


def get_progress(pid: int, *, owner_id: int | None = None) -> sqlite3.Row | None:
    if owner_id is None:
        return db.one("SELECT * FROM progress_entries WHERE id = ?", (int(pid),))
    return db.one(
        "SELECT pe.* FROM progress_entries pe JOIN tasks t ON t.id = pe.task_id "
        "WHERE pe.id = ? AND t.owner_id = ?",
        (int(pid), int(owner_id)),
    )


def delete_progress(pid: int, *, owner_id: int | None = None) -> None:
    if owner_id is None:
        db.execute("DELETE FROM progress_entries WHERE id = ?", (int(pid),))
        return
    db.execute(
        "DELETE FROM progress_entries WHERE id = ? AND task_id IN "
        "(SELECT id FROM tasks WHERE owner_id = ?)",
        (int(pid), int(owner_id)),
    )


# ---------------------------------------------------------------- 仪表盘分区

def dashboard_buckets(
    *, owner_id: int | None = None, exclude_routines: bool = False
) -> dict:
    """SPEC 7.3：仅 status in (todo, doing) 且 archived=0 参与分区。

    exclude_routines=True 时例行实例不参与分区（SPEC 16.4：改在「本期例行」卡片）。
    """
    today = timeutil.today().isoformat()
    soon_window = _soon_days()
    this_week = timeutil.week_start(timeutil.today()).isoformat()

    clause, extra = _owner_clause(owner_id)
    rows = db.query(
        BASE_SELECT
        + " WHERE t.archived = 0 AND t.status IN ('todo', 'doing')"
        + (" AND t.routine_id IS NULL" if exclude_routines else "")
        + clause
        + ACTIVE_ORDER,
        extra,
    )
    buckets: dict[str, list] = {key: [] for key in BUCKET_KEYS}
    for row in rows:
        item = serialize_task(row)
        days_left = item["days_left"]
        if days_left is None:
            buckets["no_due"].append(item)
        elif days_left < 0:
            buckets["overdue"].append(item)
        elif days_left == 0:
            buckets["today"].append(item)
        elif days_left <= soon_window:
            buckets["soon"].append(item)
        if item["week_start"] == this_week:
            buckets["this_week"].append(item)

    result: dict = {key: buckets[key] for key in BUCKET_KEYS}
    result["counts"] = {key: len(buckets[key]) for key in BUCKET_KEYS}
    return result


# ---------------------------------------------------------------- 区间统计

def tasks_in_range(
    start: str, end: str, *, owner_id: int | None = None
) -> list[sqlite3.Row]:
    """区间内创建 / 完成 / 有进展的工作（SPEC 7.6）。"""
    start_day, end_day = str(start)[:10], str(end)[:10]
    clause, extra = _owner_clause(owner_id)
    return db.query(
        BASE_SELECT
        + """
         WHERE (
             substr(t.created_at, 1, 10) BETWEEN ? AND ?
             OR substr(t.completed_at, 1, 10) BETWEEN ? AND ?
             OR EXISTS (
                  SELECT 1 FROM progress_entries pe
                   WHERE pe.task_id = t.id
                     AND substr(pe.created_at, 1, 10) BETWEEN ? AND ?
                )
           )"""
        + clause
        + " ORDER BY t.id",
        (start_day, end_day, start_day, end_day, start_day, end_day, *extra),
    )


def progress_in_range(
    start: str, end: str, *, owner_id: int | None = None
) -> list[sqlite3.Row]:
    start_day, end_day = str(start)[:10], str(end)[:10]
    clause, extra = _owner_clause(owner_id)
    return db.query(
        """
        SELECT pe.*, t.title AS task_title
          FROM progress_entries pe
          JOIN tasks t ON t.id = pe.task_id
         WHERE substr(pe.created_at, 1, 10) BETWEEN ? AND ?"""
        + clause
        + " ORDER BY pe.created_at, pe.id",
        (start_day, end_day, *extra),
    )


# ---------------------------------------------------------------- 项目

def list_projects(include_archived=False, *, owner_id: int | None = None) -> list[sqlite3.Row]:
    """项目列表；带 owner_id 时项目与卡片里的计数都只统计自己的数据。"""
    where = "" if include_archived else " WHERE p.archived = 0"
    clause, extra = _owner_clause(owner_id)
    if owner_id is None:
        params: tuple = ()
    else:
        where = (where + " AND p.owner_id = ?") if where else " WHERE p.owner_id = ?"
        params = (int(owner_id),)
    return db.query(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM tasks t
                 WHERE t.project_id = p.id AND t.archived = 0
                   AND t.status IN ('todo', 'doing')"""
        + clause
        + """) AS active_count,
               (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id"""
        + clause
        + """) AS total_count
          FROM projects p
        """
        + where
        + " ORDER BY p.archived, p.name COLLATE NOCASE",
        (*extra, *extra, *params),
    )


def get_project(project_id: int, *, owner_id: int | None = None) -> sqlite3.Row | None:
    clause, extra = _owner_clause(owner_id, alias="p")
    return db.one(
        "SELECT p.* FROM projects p WHERE p.id = ?" + clause, (int(project_id), *extra)
    )


HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")


def _clean_color(value) -> str:
    text = str(value or "").strip()
    return text if HEX_COLOR.match(text) else "#6366f1"


def create_project(name, description="", color="#6366f1", *, owner_id: int | None = None) -> int:
    project_name = str(name or "").strip()
    if not project_name:
        raise ValueError("项目名不能为空")
    if get_project_by_name(project_name, owner_id=owner_id) is not None:
        raise ValueError("项目名已存在：" + project_name)
    now = timeutil.now_iso()
    try:
        return db.execute(
            "INSERT INTO projects(name, description, color, archived, created_at, updated_at, "
            "owner_id) VALUES(?, ?, ?, 0, ?, ?, ?)",
            (
                project_name,
                str(description or ""),
                _clean_color(color),
                now,
                now,
                _owner_value(owner_id),
            ),
            returning_id=True,
        )
    except sqlite3.IntegrityError:
        # projects.name 全库唯一（SPEC 第 4 节 DDL），同名项目已被他人占用时给同样的可读错误
        raise ValueError("项目名已存在：" + project_name) from None


def get_project_by_name(name: str, *, owner_id: int | None = None) -> sqlite3.Row | None:
    if owner_id is None:
        return db.one("SELECT * FROM projects WHERE name = ?", (str(name or "").strip(),))
    return db.one(
        "SELECT * FROM projects WHERE name = ? AND owner_id = ?",
        (str(name or "").strip(), int(owner_id)),
    )


# ---------------------------------------------------------------- 展示 / JSON 助手

def _due_class(due_date, days_left) -> str:
    if due_date is None or days_left is None:
        return "due-normal"
    if days_left < 0:
        return "due-overdue"
    if days_left == 0:
        return "due-today"
    if days_left <= _soon_days():
        return "due-soon"
    return "due-normal"


def _due_label(due_date, due_time, days_left) -> str:
    if due_date is None:
        return "无截止日期"
    if days_left is None:
        return str(due_date)
    if days_left < 0:
        text = "逾期 " + str(-days_left) + " 天"
    elif days_left == 0:
        text = "今天到期"
    elif days_left == 1:
        text = "明天到期"
    else:
        text = str(days_left) + " 天后"
    return (text + " " + str(due_time)) if due_time else text


def serialize_task(row) -> dict:
    """Row -> 纯 dict（模板、JSON API 共用同一形状，含紧急度与剩余天数）。

    routine_id / is_routine 让页面能区分例行实例与一次性工作（SPEC 16.4）；
    普通任务两者分别为 None / False，既有字段一个不变。
    """
    keys = set(row.keys())

    def col(name, default=None):
        return row[name] if name in keys else default

    due_date = row["due_date"]
    due_time = row["due_time"]
    days_left = timeutil.days_until(due_date)
    tags = str(row["tags"] or "")
    return {
        "id": int(row["id"]),
        "project_id": row["project_id"],
        "project_name": col("project_name"),
        "project_color": col("project_color"),
        "title": row["title"],
        "detail": row["detail"],
        "done_criteria": row["done_criteria"],
        "due_date": due_date,
        "due_time": due_time,
        "due_label": _due_label(due_date, due_time, days_left),
        "due_class": _due_class(due_date, days_left),
        "days_left": days_left,
        "status": row["status"],
        "priority": row["priority"],
        "week_start": row["week_start"],
        "tags": tags,
        "tag_list": [part for part in tags.split(",") if part],
        "completed_at": row["completed_at"],
        "archived": row["archived"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "routine_id": col("routine_id"),
        "is_routine": col("routine_id") not in (None, ""),
    }


def serialize_progress(row) -> dict:
    keys = set(row.keys())
    created = str(row["created_at"] or "")
    return {
        "id": int(row["id"]),
        "task_id": int(row["task_id"]),
        "content": row["content"],
        "link": row["link"],
        "created_at": created,
        "created_label": created[:16].replace("T", " "),
        "task_title": row["task_title"] if "task_title" in keys else None,
    }


def week_view(
    offset: int = 0, *, owner_id: int | None = None, exclude_routines: bool = False
) -> dict:
    """ISO 周视图数据：7 天按 due_date 排布 + 本周无具体日期 + 落到本周外的日期。

    exclude_routines=True 时默认不显示例行实例（SPEC 16.4：?routines=1 才包含）。
    """
    start, end = timeutil.week_bounds(offset)
    rows = list_tasks(
        week_start=start.isoformat(),
        status=ACTIVE_STATUSES,
        archived=0,
        owner_id=owner_id,
        exclude_routines=exclude_routines,
    )
    by_date: dict[str, list] = {}
    no_due: list[dict] = []
    for row in rows:
        item = serialize_task(row)
        if item["due_date"] is None:
            no_due.append(item)
        else:
            by_date.setdefault(item["due_date"], []).append(item)

    today = timeutil.today()
    days = []
    for index in range(7):
        day = start + timedelta(days=index)
        iso = day.isoformat()
        days.append(
            {
                "date": iso,
                "md": f"{day.month:02d}-{day.day:02d}",
                "weekday": timeutil.WEEKDAYS_CN[day.weekday()],
                "is_today": day == today,
                "tasks": by_date.pop(iso, []),
            }
        )
    outside = [item for items in by_date.values() for item in items]
    return {
        "offset": int(offset),
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "start_label": timeutil.fmt_cn(start),
        "end_label": timeutil.fmt_cn(end),
        "today": today.isoformat(),
        "days": days,
        "no_due": no_due,
        "outside_week": outside,
        "counts": {
            "total": len(rows),
            "no_due": len(no_due),
            "outside_week": len(outside),
        },
    }


def list_archived_tasks(
    *, owner_id: int | None = None, exclude_routines: bool = False
) -> list[sqlite3.Row]:
    """归档口径：archived=1 或 status in (done, cancelled)。

    exclude_routines=True 时排除例行实例（SPEC 16.4：归档只保留一次性成果）。
    """
    clause, extra = _owner_clause(owner_id)
    return db.query(
        BASE_SELECT
        + " WHERE (t.archived = 1 OR t.status IN ('done', 'cancelled'))"
        + (" AND t.routine_id IS NULL" if exclude_routines else "")
        + clause
        + ARCHIVE_ORDER,
        extra,
    )


def archive_groups(
    *, owner_id: int | None = None, exclude_routines: bool = False
) -> list[dict]:
    """按完成周分组倒序；无完成时间的归入最后一组。"""
    groups: dict[str, dict] = {}
    for row in list_archived_tasks(owner_id=owner_id, exclude_routines=exclude_routines):
        item = serialize_task(row)
        completed = timeutil.parse_date(row["completed_at"])
        if completed is None:
            key = ""
            label = "未记录完成时间"
        else:
            monday = timeutil.week_start(completed)
            key = monday.isoformat()
            label = f"{monday.isocalendar().year} 年第 {monday.isocalendar().week} 周（{timeutil.fmt_cn(monday)} 起）"
        group = groups.setdefault(key, {"key": key, "label": label, "tasks": []})
        group["tasks"].append(item)
    ordered_keys = sorted([key for key in groups if key], reverse=True)
    if "" in groups:
        ordered_keys.append("")
    return [groups[key] for key in ordered_keys]
