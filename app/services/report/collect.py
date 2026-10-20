"""报告事实采集：把库里的工作与进展整理成报告可用的结构化事实。

职责边界（W-08）：只做读库、归一与区间解析，不拼 Markdown、不拼提示词；
被 prompt / render / __init__ 共用的字段取值与排序小工具也在这里（最底层，无内部依赖）。"""

from __future__ import annotations

import calendar
import logging
from datetime import date
from typing import Any

from app import db, timeutil
from app.services import llm, tasks

logger = logging.getLogger("app.services.reports")

KIND_LABELS: dict[str, str] = {
    "task": "单项工作",
    "week": "周报",
    "month": "月报",
    "half_year": "半年报",
    "year": "年报",
    "custom": "自定义区间",
}


KINDS: tuple[str, ...] = tuple(KIND_LABELS)


MOCK_NOTICE = llm.MOCK_NOTICE


OBSIDIAN_SKIP_NOTE = "本次未使用知识库上下文。"


# 兜底 SQL 只在任务域抛 NotImplementedError 时使用；{owner} 由 _owner_sql 填入归属条件，
# 与任务域契约函数保持同样的口径（owner_id 为 None 时不加任何过滤）。
_TASK_RANGE_FALLBACK = """
SELECT * FROM tasks
WHERE (substr(created_at, 1, 10) BETWEEN ? AND ?
   OR substr(COALESCE(completed_at, ''), 1, 10) BETWEEN ? AND ?
   OR id IN (
        SELECT task_id FROM progress_entries
        WHERE substr(created_at, 1, 10) BETWEEN ? AND ?
      )){owner}
ORDER BY COALESCE(due_date, '9999-12-31') ASC, id ASC
"""


_PROGRESS_RANGE_FALLBACK = """
SELECT pe.* FROM progress_entries pe{join}
WHERE substr(pe.created_at, 1, 10) BETWEEN ? AND ?{owner}
ORDER BY pe.created_at ASC, pe.id ASC
"""


def _owner_sql(owner_id: int | None, alias: str = "") -> tuple[str, str, tuple]:
    """兜底 SQL 的归属片段：返回 (任务表条件, 进展表 join, 参数)（SPEC 15.7）。"""
    if owner_id is None:
        return "", "", ()
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}owner_id = ?", " JOIN tasks t ON t.id = pe.task_id", (int(owner_id),)


class ReportError(ValueError):
    """报告参数或生成流程的可读错误（页面/API 转 400，不是 500 堆栈）。"""


# ---------------------------------------------------------------- 通用小工具


def _field(row: Any, name: str, default: Any = None) -> Any:
    """sqlite3.Row / dict 通用取值：缺列不抛异常。"""
    if row is None:
        return default
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return default


def _in_range(day: date | None, start_day: date, end_day: date) -> bool:
    return day is not None and start_day <= day <= end_day


def _sort_progress(rows: list[Any]) -> list[Any]:
    return sorted(
        rows,
        key=lambda r: (str(_field(r, "created_at") or ""), int(_field(r, "id") or 0)),
    )


# ---------------------------------------------------------------- 区间解析


def resolve_range(kind: str, start=None, end=None) -> tuple[str, str, str]:
    """返回 (start_iso, end_iso, scope_label)。参数非法时抛 ReportError。"""
    key = (kind or "").strip()
    if key not in KIND_LABELS:
        raise ReportError(
            "不支持的报告粒度：%s；可选 %s" % (key or "（空）", "、".join(KINDS))
        )
    today = timeutil.today()

    if key == "task":
        # 单项工作的真实跨度由工作本身决定（见 _task_span），这里只做兜底解析。
        return today.isoformat(), today.isoformat(), KIND_LABELS["task"]

    if key == "custom":
        start_day = timeutil.parse_date(start)
        end_day = timeutil.parse_date(end)
        if start_day is None and end_day is None:
            raise ReportError("自定义区间必须同时填写起始日与结束日（YYYY-MM-DD）")
        if start_day is None:
            raise ReportError("起始日无法识别：%r，请使用 YYYY-MM-DD" % (start,))
        if end_day is None:
            raise ReportError("结束日无法识别：%r，请使用 YYYY-MM-DD" % (end,))
        if start_day > end_day:
            raise ReportError(
                "起始日 %s 晚于结束日 %s，请调整区间"
                % (start_day.isoformat(), end_day.isoformat())
            )
        return (
            start_day.isoformat(),
            end_day.isoformat(),
            "%s ~ %s" % (start_day.isoformat(), end_day.isoformat()),
        )

    if key == "week":
        start_day, end_day = timeutil.week_bounds(0)
        label = "%s 年第 %s 周（%s ~ %s）" % (
            start_day.year,
            start_day.isocalendar()[1],
            start_day.isoformat(),
            end_day.isoformat(),
        )
    elif key == "month":
        start_day = today.replace(day=1)
        end_day = today.replace(day=calendar.monthrange(today.year, today.month)[1])
        label = "%s 年 %s 月（%s ~ %s）" % (
            today.year,
            today.month,
            start_day.isoformat(),
            end_day.isoformat(),
        )
    elif key == "half_year":
        first_half = today.month <= 6
        start_day = date(today.year, 1 if first_half else 7, 1)
        end_day = date(today.year, 6, 30) if first_half else date(today.year, 12, 31)
        label = "%s 年%s（%s ~ %s）" % (
            today.year,
            "上半年" if first_half else "下半年",
            start_day.isoformat(),
            end_day.isoformat(),
        )
    else:  # year
        start_day = date(today.year, 1, 1)
        end_day = date(today.year, 12, 31)
        label = "%s 年（%s ~ %s）" % (
            today.year,
            start_day.isoformat(),
            end_day.isoformat(),
        )

    return start_day.isoformat(), end_day.isoformat(), label


# ---------------------------------------------------------------- 数据采集


def _get_task_row(tid: int, owner_id: int | None = None):
    try:
        row = tasks.get_task(tid, owner_id=owner_id)
    except NotImplementedError:
        logger.info("tasks.get_task 尚未实现，改用等价只读 SQL")
        owner_sql, _, owner_params = _owner_sql(owner_id)
        row = db.one("SELECT * FROM tasks WHERE id = ?" + owner_sql, (tid, *owner_params))
    return row


def _list_progress(tid: int, owner_id: int | None = None) -> list[Any]:
    try:
        rows = list(tasks.list_progress(tid, owner_id=owner_id) or [])
    except NotImplementedError:
        logger.info("tasks.list_progress 尚未实现，改用等价只读 SQL")
        owner_sql, join_sql, owner_params = _owner_sql(owner_id)
        rows = db.query(
            "SELECT pe.* FROM progress_entries pe" + join_sql
            + " WHERE pe.task_id = ?" + owner_sql
            + " ORDER BY pe.created_at ASC, pe.id ASC",
            (tid, *owner_params),
        )
    return _sort_progress(rows)


def _range_task_rows(start_iso: str, end_iso: str, owner_id: int | None = None) -> list[Any]:
    try:
        return list(tasks.tasks_in_range(start_iso, end_iso, owner_id=owner_id) or [])
    except NotImplementedError:
        logger.info("tasks.tasks_in_range 尚未实现，改用等价只读 SQL")
        owner_sql, _, owner_params = _owner_sql(owner_id)
        return db.query(
            _TASK_RANGE_FALLBACK.format(owner=owner_sql),
            (start_iso, end_iso, start_iso, end_iso, start_iso, end_iso, *owner_params),
        )


def _range_progress_rows(start_iso: str, end_iso: str, owner_id: int | None = None) -> list[Any]:
    try:
        return list(tasks.progress_in_range(start_iso, end_iso, owner_id=owner_id) or [])
    except NotImplementedError:
        logger.info("tasks.progress_in_range 尚未实现，改用等价只读 SQL")
        owner_sql, join_sql, owner_params = _owner_sql(owner_id)
        return db.query(
            _PROGRESS_RANGE_FALLBACK.format(owner=owner_sql, join=join_sql),
            (start_iso, end_iso, *owner_params),
        )


def _task_span(row: Any) -> tuple[str, str, str]:
    """单项工作的真实统计跨度：创建日 -> 完成日（未完成则到今天）。"""
    start_day = timeutil.parse_date(_field(row, "created_at")) or timeutil.today()
    end_day = timeutil.parse_date(_field(row, "completed_at")) or timeutil.today()
    if end_day < start_day:
        end_day = start_day
    return (
        start_day.isoformat(),
        end_day.isoformat(),
        "单项工作 #%s" % _field(row, "id"),
    )


def _collect_task(task_id: Any, owner_id: int | None = None) -> dict[str, Any]:
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        raise ReportError("task_id 必须是整数") from None
    row = _get_task_row(tid, owner_id=owner_id)
    if row is None:
        raise ReportError("工作 #%s 不存在（可能已被删除）" % tid)
    return {"task": row, "progress": _list_progress(tid, owner_id=owner_id)}


def _collect_range(
    start_iso: str, end_iso: str, owner_id: int | None = None
) -> dict[str, Any]:
    """区间统计：区间内创建 / 区间内完成 / 区间内有进展（三者并集）。"""
    start_day = timeutil.parse_date(start_iso)
    end_day = timeutil.parse_date(end_iso)
    if start_day is None or end_day is None:
        raise ReportError("统计区间的起止日无法解析：%r ~ %r" % (start_iso, end_iso))

    task_rows = _range_task_rows(start_iso, end_iso, owner_id=owner_id)
    progress_rows = _range_progress_rows(start_iso, end_iso, owner_id=owner_id)

    progress_by_task: dict[int, list[Any]] = {}
    for row in progress_rows:
        tid = _field(row, "task_id")
        if tid is None:
            continue
        if not _in_range(timeutil.parse_date(_field(row, "created_at")), start_day, end_day):
            continue
        progress_by_task.setdefault(int(tid), []).append(row)

    index: dict[int, Any] = {}
    ordered: list[Any] = []
    for row in task_rows:
        tid = _field(row, "id")
        if tid is None or int(tid) in index:
            continue
        index[int(tid)] = row
        ordered.append(row)

    for tid in progress_by_task:
        if tid in index:
            continue
        row = _get_task_row(tid, owner_id=owner_id)
        if row is not None:
            index[tid] = row
            ordered.append(row)

    created: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    progressed: list[dict[str, Any]] = []
    progress_total = 0
    for row in ordered:
        tid = int(_field(row, "id"))
        plist = _sort_progress(progress_by_task.get(tid, []))
        item = {"task": row, "progress": plist, "count": len(plist)}
        progress_total += len(plist)
        if _in_range(timeutil.parse_date(_field(row, "created_at")), start_day, end_day):
            created.append(item)
        if _in_range(timeutil.parse_date(_field(row, "completed_at")), start_day, end_day):
            completed.append(item)
        if plist:
            progressed.append(item)

    return {
        "start_day": start_day,
        "end_day": end_day,
        "ordered": ordered,
        "created": created,
        "completed": completed,
        "progressed": progressed,
        "progress_total": progress_total,
    }
