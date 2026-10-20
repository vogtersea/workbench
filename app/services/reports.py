"""报告生成与查询（SPEC.md 第 5 / 7 节，本域整体重写）。

粒度：task / week / month / half_year / year / custom。

事实来源只走任务域契约函数（get_task / list_progress / tasks_in_range /
progress_in_range）；任务域尚未实现（NotImplementedError）时退化为等价的只读 SQL，
保证本域自测可以独立进行，任务域落地后自动改走契约函数。

- 真实模式：结构化事实 -> 中文 Prompt -> llm.generate -> Markdown 落库（is_mock=0）。
- MOCK 模式：不调用大模型，用本地确定性模板基于真实入库数据渲染（is_mock=1），
  正文首行固定插入 SPEC 第 5 节的 MOCK 声明。
"""

from __future__ import annotations

import calendar
import json
import logging
import re
from datetime import date
from typing import Any

from app import db, timeutil
from app.services import llm, tasks

logger = logging.getLogger(__name__)

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
STATUS_TEXT = {"todo": "待办", "doing": "进行中", "done": "已完成", "cancelled": "已取消"}
PRIORITY_TEXT = {1: "低", 2: "中", 3: "高"}

SYSTEM_PROMPT = (
    "你是一位严谨的中文工作总结助手。必须遵守以下规则：\n"
    "1. 只允许使用用户消息「数据」部分明确给出的事实，禁止编造数据、日期、数字、人名或事件。\n"
    "2. 数据不足的小节直接写「本期数据不足，暂无法总结」，不要猜测，也不要用空话填充。\n"
    "3. 输出 Markdown 正文，小节标题用二级标题（##），不要整篇包在代码块里。\n"
    "4. 固定小节，标题与顺序必须完全一致：## 概述 / ## 完成情况 / ## 关键进展 / "
    "## 问题与风险 / ## 下阶段计划。\n"
    "5. 「相关知识库笔记」只作背景参考，不得当作本期工作事实写入报告。\n"
    "6. 全文中文，简洁、可核查，不使用夸张修辞。"
)

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


def _status_text(value: Any) -> str:
    key = str(value or "").strip()
    return STATUS_TEXT.get(key, key or "未知")


def _priority_text(value: Any) -> str:
    try:
        return PRIORITY_TEXT.get(int(value), "P%s" % value)
    except (TypeError, ValueError):
        return "未指定"


def _ts(value: Any) -> str:
    """ISO8601 -> 2025-06-03 09:00（只做展示层裁剪，不改语义）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) >= 16 and text[10] in "T ":
        return "%s %s" % (text[:10], text[11:16])
    return text[:16]


def _due_text(row: Any) -> str:
    due = str(_field(row, "due_date") or "").strip()
    if not due:
        return "（无 DDL）"
    time_part = str(_field(row, "due_time") or "").strip()
    return ("%s %s" % (due, time_part)).strip()


def _tags_text(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return "（无标签）"
    parts = [p.strip() for p in re.split(r"[,，]", text) if p.strip()]
    return "、".join(parts) if parts else text


ROUTINE_FREQ_TEXT: dict[str, str] = {
    "daily": "每日",
    "weekly": "每周",
    "monthly": "每月",
    "quarterly": "每季度",
    "half_year": "每半年",
    "yearly": "每年",
}


def _is_routine(row: Any) -> bool:
    """例行实例判定：tasks.routine_id 非空（SPEC 16.2）。"""
    value = _field(row, "routine_id")
    return value not in (None, "")


def _routine_freq(row: Any) -> str:
    """取该实例所属模板的 freq（模板已被删除时返回空串）。"""
    try:
        freq_row = db.one(
            "SELECT freq FROM routines WHERE id = ?", (int(_field(row, "routine_id")),)
        )
    except (TypeError, ValueError):
        return ""
    except Exception:
        logger.warning("读取例行模板周期失败", exc_info=True)
        return ""
    return str(_field(freq_row, "freq") or "").strip()


def _routine_note(row: Any) -> str:
    """例行实例的标注文本（SPEC 16.4：「（例行 · 每周）」）；一次性工作返回空串。"""
    if not _is_routine(row):
        return ""
    freq = _routine_freq(row)
    label = ROUTINE_FREQ_TEXT.get(freq, freq)
    return "（例行 · %s）" % label if label else "（例行）"


def _routine_counts(items: list[dict[str, Any]]) -> tuple[int, int]:
    """(例行条数, 一次性条数)，供区间统计区分两类完成量。"""
    routine = sum(1 for item in items if _is_routine(item["task"]))
    return routine, len(items) - routine


def _in_range(day: date | None, start_day: date, end_day: date) -> bool:
    return day is not None and start_day <= day <= end_day


def _sort_progress(rows: list[Any]) -> list[Any]:
    return sorted(
        rows,
        key=lambda r: (str(_field(r, "created_at") or ""), int(_field(r, "id") or 0)),
    )


def _progress_line(row: Any) -> str:
    content = " ".join(str(_field(row, "content") or "").split()) or "（空内容）"
    when = _ts(_field(row, "created_at"))
    link = str(_field(row, "link") or "").strip()
    prefix = "%s｜" % when if when else ""
    suffix = "（链接：%s）" % link if link else ""
    return "%s%s%s" % (prefix, content, suffix)


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


# ---------------------------------------------------------------- 事实文本


def _task_fact_lines(row: Any) -> list[str]:
    return [
        "工作编号：#%s" % _field(row, "id"),
        "标题：%s%s" % (_field(row, "title") or "（无标题）", _routine_note(row)),
        "状态：%s" % _status_text(_field(row, "status")),
        "优先级：%s" % _priority_text(_field(row, "priority")),
        "所属周（周一）：%s" % (_field(row, "week_start") or "—"),
        "DDL：%s" % _due_text(row),
        "创建时间：%s" % (_ts(_field(row, "created_at")) or "—"),
        "完成时间：%s" % (_ts(_field(row, "completed_at")) or "—"),
        "标签：%s" % _tags_text(_field(row, "tags")),
        "详情：%s" % (_field(row, "detail") or "（未填写）"),
        "完成条件：%s" % (_field(row, "done_criteria") or "（未填写）"),
    ]


def _task_overview(row: Any, progress: list[Any]) -> str:
    return "工作「%s」创建于 %s，当前状态为「%s」，优先级「%s」，共记录 %s 条进展。" % (
        _field(row, "title") or "（无标题）",
        _ts(_field(row, "created_at")) or "未知时间",
        _status_text(_field(row, "status")),
        _priority_text(_field(row, "priority")),
        len(progress),
    )


def _task_completion(row: Any) -> str:
    status = str(_field(row, "status") or "")
    if status == "done":
        verdict = "已完成并归档（完成时间：%s）。" % (
            _ts(_field(row, "completed_at")) or "未记录"
        )
    elif status == "cancelled":
        verdict = "该工作已取消，不再推进。"
    else:
        verdict = "尚未完成，当前状态「%s」。" % _status_text(status)
    return "\n".join(
        [
            "- 完成条件：%s" % (_field(row, "done_criteria") or "（未填写）"),
            "- 当前状态：%s" % _status_text(status),
            "- 完成时间：%s" % (_ts(_field(row, "completed_at")) or "—"),
            "- 判定：%s" % verdict,
        ]
    )


def _task_progress_section(progress: list[Any]) -> str:
    if not progress:
        return "暂无进展记录。"
    return "\n".join(
        "%s. %s" % (idx, _progress_line(row))
        for idx, row in enumerate(progress, start=1)
    )


def _task_risks(row: Any, progress: list[Any]) -> str:
    risks: list[str] = []
    status = str(_field(row, "status") or "")
    due = timeutil.parse_date(_field(row, "due_date"))
    if due is None:
        risks.append("未登记 DDL：无法进入逾期/临期提醒。")
    elif status in {"todo", "doing"}:
        delta = (due - timeutil.today()).days
        if delta < 0:
            risks.append("已逾期 %s 天（DDL %s）。" % (-delta, due.isoformat()))
        elif delta == 0:
            risks.append("DDL 就是今天。")
    if not str(_field(row, "done_criteria") or "").strip():
        risks.append("完成条件未填写，收尾缺少可核对的标准。")
    if not progress:
        risks.append("没有任何进展记录，过程不可追溯。")
    if status == "cancelled":
        risks.append("状态为已取消，需确认相关工作是否仍需推进。")
    if not risks:
        risks.append("基于入库字段未发现结构性风险（DDL、完成条件、进展均已登记）。")
    return "\n".join("- %s" % item for item in risks)


def _task_plan(row: Any, progress: list[Any]) -> str:
    status = str(_field(row, "status") or "")
    steps: list[str] = []
    if status in {"todo", "doing"}:
        steps.append("按完成条件推进，达成后切到「已完成」并在归档区核对。")
        steps.append("下一次动作补一条进展记录，保持过程可追溯。")
    elif status == "done":
        steps.append("无需后续动作；如需复盘，可在归档区查阅历史进展。")
    elif status == "cancelled":
        steps.append("若确定重启，先改回「待办」并重新评估 DDL 与完成条件。")
    if timeutil.parse_date(_field(row, "due_date")) is None:
        steps.append("补充 DDL，让它进入逾期/临期提醒。")
    if not str(_field(row, "done_criteria") or "").strip():
        steps.append("补齐完成条件，明确可核对的收尾标准。")
    if not steps:
        steps.append("保持现状，按周复盘。")
    return "\n".join("- %s" % item for item in steps)


def _range_completion(data: dict[str, Any]) -> str:
    created, completed = data["created"], data["completed"]
    routine_done, plain_done = _routine_counts(completed)
    lines = [
        "- 新建工作：%s 项" % len(created),
        "- 完成工作：%s 项" % len(completed),
        "- 例行完成 %s 条 / 一次性完成 %s 条" % (routine_done, plain_done),
        "- 有进展工作：%s 项" % len(data["progressed"]),
        "- 进展条目：%s 条" % data["progress_total"],
        "",
        "### 本期完成",
        "",
    ]
    if completed:
        for item in completed:
            row = item["task"]
            lines.append(
                "- [%s] %s%s（完成于 %s）"
                % (
                    _priority_text(_field(row, "priority")),
                    _field(row, "title") or "（无标题）",
                    _routine_note(row),
                    str(_field(row, "completed_at") or "")[:10] or "—",
                )
            )
    else:
        lines.append("- 本期没有完成的工作。")

    done_ids = {int(_field(i["task"], "id")) for i in completed}
    pending = [i for i in created if int(_field(i["task"], "id")) not in done_ids]
    lines += ["", "### 本期新建但未完成", ""]
    if pending:
        for item in pending:
            row = item["task"]
            lines.append(
                "- %s%s（状态：%s，DDL：%s）"
                % (
                    _field(row, "title") or "（无标题）",
                    _routine_note(row),
                    _status_text(_field(row, "status")),
                    _due_text(row),
                )
            )
    else:
        lines.append("- 本期新建的工作全部完成。")
    return "\n".join(lines)


def _range_progress(progressed: list[dict[str, Any]]) -> str:
    if not progressed:
        return "本期没有任何进展记录。"
    chunks: list[str] = []
    for item in progressed:
        row = item["task"]
        chunks.append(
            "### %s（%s 条）"
            % (_field(row, "title") or "（无标题）", item["count"])
        )
        chunks.append("")
        chunks += ["- %s" % _progress_line(p) for p in item["progress"]]
        chunks.append("")
    return "\n".join(chunks).strip()


def _range_risks(data: dict[str, Any]) -> str:
    today = timeutil.today()
    overdue: list[tuple[str, int]] = []
    no_due: list[str] = []
    for row in data["ordered"]:
        if str(_field(row, "status") or "") not in {"todo", "doing"}:
            continue
        due = timeutil.parse_date(_field(row, "due_date"))
        title = str(_field(row, "title") or "（无标题）")
        if due is None:
            no_due.append(title)
        elif due < today:
            overdue.append((title, (today - due).days))

    risks: list[str] = []
    if overdue:
        detail = "、".join("%s（逾期 %s 天）" % (t, d) for t, d in overdue[:8])
        tail = " 等 %s 项" % len(overdue) if len(overdue) > 8 else ""
        risks.append("逾期未完成 %s 项：%s%s" % (len(overdue), detail, tail))
    if no_due:
        risks.append(
            "无 DDL 的未完成工作 %s 项：%s" % (len(no_due), "、".join(no_due[:8]))
        )
    if not data["progressed"]:
        risks.append("本期没有任何进展记录，过程数据缺失。")
    if not risks:
        risks.append("基于入库字段未发现结构性风险。")
    return "\n".join("- %s" % item for item in risks)


def _range_plan(data: dict[str, Any]) -> str:
    done_ids = {int(_field(i["task"], "id")) for i in data["completed"]}
    pending = [
        i["task"] for i in data["created"] if int(_field(i["task"], "id")) not in done_ids
    ]
    steps: list[str] = []
    if pending:
        names = "、".join(str(_field(r, "title") or "（无标题）") for r in pending[:10])
        tail = " 等" if len(pending) > 10 else ""
        steps.append("结转本期未完成的 %s 项工作：%s%s。" % (len(pending), names, tail))
    else:
        steps.append("本期新建工作已全部完成，下期以新计划为主。")
    if any(timeutil.parse_date(_field(r, "due_date")) is None for r in pending):
        steps.append("为无 DDL 的结转项补上截止日期，让它进入提醒。")
    steps.append("下期继续按「完成条件」逐项核对，避免只记录进展不销号。")
    return "\n".join("- %s" % item for item in steps)


# ---------------------------------------------------------------- MOCK 渲染


def _join_md(lines: list[str], obsidian_text: str, skip_note: str) -> str:
    if obsidian_text:
        lines += ["", "## 相关知识库笔记（仅作背景参考）", "", obsidian_text]
    if skip_note:
        lines += ["", "---", "", "> %s" % skip_note]
    return "\n".join(lines).strip() + "\n"


def _mock_task_md(
    row: Any, progress: list[Any], obsidian_text: str, skip_note: str
) -> str:
    title = (_field(row, "title") or "工作 #%s" % _field(row, "id")) + _routine_note(row)
    lines = [
        MOCK_NOTICE,
        "",
        "# 单项工作总结：%s" % title,
        "",
        "- 粒度：%s" % KIND_LABELS["task"],
        "- 工作编号：#%s" % _field(row, "id"),
        "- 当前状态：%s" % _status_text(_field(row, "status")),
        "- 优先级：%s" % _priority_text(_field(row, "priority")),
        "- 所属周（周一）：%s" % (_field(row, "week_start") or "—"),
        "- DDL：%s" % _due_text(row),
        "- 完成时间：%s" % (_ts(_field(row, "completed_at")) or "—"),
        "- 标签：%s" % _tags_text(_field(row, "tags")),
        "- 报告生成时间：%s" % (_ts(timeutil.now_iso()) or "—"),
        "",
        "## 概述",
        "",
        _task_overview(row, progress),
        "",
        "## 完成情况",
        "",
        _task_completion(row),
        "",
        "## 关键进展",
        "",
        _task_progress_section(progress),
        "",
        "## 问题与风险",
        "",
        _task_risks(row, progress),
        "",
        "## 下阶段计划",
        "",
        _task_plan(row, progress),
    ]
    return _join_md(lines, obsidian_text, skip_note)


def _mock_range_md(
    kind: str,
    scope_label: str,
    data: dict[str, Any],
    obsidian_text: str,
    skip_note: str,
) -> str:
    start_iso = data["start_day"].isoformat()
    end_iso = data["end_day"].isoformat()
    lines = [
        MOCK_NOTICE,
        "",
        "# %s：%s" % (KIND_LABELS[kind], scope_label),
        "",
        "- 粒度：%s" % KIND_LABELS[kind],
        "- 统计区间：%s ~ %s（含首尾）" % (start_iso, end_iso),
        "- 数据口径：区间内创建 / 区间内完成 / 区间内有进展（三者取并集）",
        "- 报告生成时间：%s" % (_ts(timeutil.now_iso()) or "—"),
        "",
        "## 概述",
        "",
        "本期（%s ~ %s）共新建 %s 项工作、完成 %s 项，%s 项工作登记了进展，"
        "进展条目合计 %s 条。"
        % (
            start_iso,
            end_iso,
            len(data["created"]),
            len(data["completed"]),
            len(data["progressed"]),
            data["progress_total"],
        ),
        "",
        "## 完成情况",
        "",
        _range_completion(data),
        "",
        "## 关键进展",
        "",
        _range_progress(data["progressed"]),
        "",
        "## 问题与风险",
        "",
        _range_risks(data),
        "",
        "## 下阶段计划",
        "",
        _range_plan(data),
    ]
    return _join_md(lines, obsidian_text, skip_note)


# ---------------------------------------------------------------- Prompt


def _range_task_block(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    chunks: list[str] = []
    for idx, item in enumerate(items, start=1):
        row = item["task"]
        chunks.append(
            "### %s）%s%s"
            % (idx, _field(row, "title") or "（无标题）", _routine_note(row))
        )
        chunks.append("- 状态：%s｜优先级：%s" % (
            _status_text(_field(row, "status")),
            _priority_text(_field(row, "priority")),
        ))
        chunks.append("- 所属周（周一）：%s" % (_field(row, "week_start") or "—"))
        chunks.append("- DDL：%s" % _due_text(row))
        chunks.append("- 创建时间：%s" % (_ts(_field(row, "created_at")) or "—"))
        chunks.append("- 完成时间：%s" % (_ts(_field(row, "completed_at")) or "—"))
        chunks.append("- 标签：%s" % _tags_text(_field(row, "tags")))
        chunks.append("- 详情：%s" % (_field(row, "detail") or "（未填写）"))
        chunks.append("- 完成条件：%s" % (_field(row, "done_criteria") or "（未填写）"))
        chunks.append("")
    return "\n".join(chunks).strip()


def _range_progress_block(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    chunks: list[str] = []
    for idx, item in enumerate(items, start=1):
        row = item["task"]
        chunks.append("### %s）%s（区间内 %s 条进展）" % (
            idx,
            _field(row, "title") or "（无标题）",
            item["count"],
        ))
        chunks += ["- %s" % _progress_line(p) for p in item["progress"]]
        chunks.append("")
    return "\n".join(chunks).strip()


def _task_prompt(
    row: Any, progress: list[Any], start_iso: str, end_iso: str, obsidian_text: str
) -> str:
    lines = [
        "# 报告任务",
        "- 粒度：%s" % KIND_LABELS["task"],
        "- 统计区间：%s ~ %s（含首尾）" % (start_iso, end_iso),
        "- 生成时间：%s" % (_ts(timeutil.now_iso()) or "—"),
        "",
        "## 数据｜工作事实",
        *_task_fact_lines(row),
        "",
        "## 数据｜全部进展（共 %s 条，按时间正序）" % len(progress),
    ]
    if progress:
        lines += ["%s. %s" % (i, _progress_line(p)) for i, p in enumerate(progress, 1)]
    else:
        lines.append("（无进展记录）")
    if obsidian_text:
        lines += [
            "",
            "## 相关知识库笔记（仅作背景参考，不得当作本期事实）",
            "",
            obsidian_text,
        ]
    lines += ["", "请严格依据以上事实，按固定小节输出 Markdown 报告。"]
    return "\n".join(lines)


def _range_prompt(
    kind: str, scope_label: str, data: dict[str, Any], obsidian_text: str
) -> str:
    start_iso = data["start_day"].isoformat()
    end_iso = data["end_day"].isoformat()
    lines = [
        "# 报告任务",
        "- 粒度：%s" % KIND_LABELS[kind],
        "- 统计范围：%s" % scope_label,
        "- 统计区间：%s ~ %s（含首尾）" % (start_iso, end_iso),
        "- 生成时间：%s" % (_ts(timeutil.now_iso()) or "—"),
        "",
        "## 数据汇总",
        "- 区间内新建：%s 项" % len(data["created"]),
        "- 区间内完成：%s 项" % len(data["completed"]),
        "- 例行完成 %s 条 / 一次性完成 %s 条" % _routine_counts(data["completed"]),
        "- 区间内有进展：%s 项，共 %s 条进展"
        % (len(data["progressed"]), data["progress_total"]),
        "",
        "## 数据 1｜区间内创建的工作（%s 项）" % len(data["created"]),
        _range_task_block(data["created"]) or "（无）",
        "",
        "## 数据 2｜区间内完成的工作（%s 项）" % len(data["completed"]),
        _range_task_block(data["completed"]) or "（无）",
        "",
        "## 数据 3｜区间内有进展的工作（%s 项，合计 %s 条进展）"
        % (len(data["progressed"]), data["progress_total"]),
        _range_progress_block(data["progressed"]) or "（无）",
    ]
    if obsidian_text:
        lines += [
            "",
            "## 相关知识库笔记（仅作背景参考，不得当作本期事实）",
            "",
            obsidian_text,
        ]
    lines += ["", "请严格依据以上事实，按固定小节输出 Markdown 报告。"]
    return "\n".join(lines)


# ---------------------------------------------------------------- Obsidian


def _keywords(*groups: list[Any]) -> list[str]:
    picked: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if len(text) < 2 or text in picked or len(picked) >= 12:
            return
        picked.append(text)

    for group in groups:
        for raw in group:
            for chunk in re.split(r"[\s,，、;；/|\\·:：\-—()（）\[\]【】]+", str(raw or "")):
                add(chunk)
    return picked


def _obsidian_context(
    keywords: list[str], use: bool
) -> tuple[str, list[dict], str]:
    """返回 (上下文文本, 命中列表, 未使用时的说明)。任何异常都优雅跳过。"""
    if not use or not keywords:
        return "", [], OBSIDIAN_SKIP_NOTE
    try:
        from app.services import obsidian
    except Exception:
        logger.warning("Obsidian 模块不可用，报告跳过知识库上下文", exc_info=True)
        return "", [], OBSIDIAN_SKIP_NOTE
    try:
        text, hits = obsidian.build_context(keywords, limit=5, budget=2000)
    except NotImplementedError:
        logger.info("Obsidian 域尚未实现，报告跳过知识库上下文")
        return "", [], OBSIDIAN_SKIP_NOTE
    except Exception:
        logger.warning("Obsidian 上下文构建失败，已跳过", exc_info=True)
        return "", [], OBSIDIAN_SKIP_NOTE

    clean_hits = [h for h in (hits or []) if isinstance(h, dict)]
    clean_text = str(text or "").strip()
    if not clean_text or not clean_hits:
        return "", [], OBSIDIAN_SKIP_NOTE
    return clean_text, clean_hits, ""


# ---------------------------------------------------------------- 主流程


def _assemble_live(doc_title: str, model_text: str, skip_note: str) -> str:
    lines = ["# %s" % doc_title, "", (model_text or "").strip()]
    if skip_note:
        lines += ["", "---", "", "> %s" % skip_note]
    return "\n".join(lines).strip() + "\n"


async def generate(
    kind: str,
    *,
    start=None,
    end=None,
    task_id=None,
    use_obsidian=True,
    owner_id: int | None = None,
) -> int:
    """生成报告并落库，返回 reports.id。参数非法抛 ReportError，调用失败抛 llm.LLMError。

    owner_id 为当前生效用户（SPEC 15.7）：事实采集只看他自己的数据，新报告写入该归属。
    """
    key = (kind or "").strip()
    if key not in KIND_LABELS:
        raise ReportError(
            "不支持的报告粒度：%s；可选 %s" % (key or "（空）", "、".join(KINDS))
        )

    task_value: int | None = None
    if key == "task":
        data = _collect_task(task_id, owner_id=owner_id)
        row = data["task"]
        task_value = int(_field(row, "id"))
        start_iso, end_iso, scope_label = _task_span(row)
        keywords = _keywords(
            [_field(row, "title")],
            [_field(row, "tags")],
            [str(_field(p, "content") or "")[:60] for p in data["progress"]],
        )
        obsidian_text, obs_hits, skip_note = _obsidian_context(
            keywords, bool(use_obsidian)
        )
        doc_title = "单项工作总结：%s" % (_field(row, "title") or "（无标题）")
        progress = data["progress"]

        def render_mock() -> str:
            return _mock_task_md(row, progress, obsidian_text, skip_note)

        def build_prompt() -> str:
            return _task_prompt(row, progress, start_iso, end_iso, obsidian_text)

    else:
        start_iso, end_iso, scope_label = resolve_range(key, start, end)
        data = _collect_range(start_iso, end_iso, owner_id=owner_id)
        titles = [_field(i["task"], "title") for i in data["created"]]
        titles += [_field(i["task"], "title") for i in data["progressed"]]
        contents: list[str] = []
        for item in data["progressed"]:
            contents += [str(_field(p, "content") or "")[:60] for p in item["progress"]]
        keywords = _keywords(titles, contents)
        obsidian_text, obs_hits, skip_note = _obsidian_context(
            keywords, bool(use_obsidian)
        )
        doc_title = "%s：%s" % (KIND_LABELS[key], scope_label)

        def render_mock() -> str:
            return _mock_range_md(key, scope_label, data, obsidian_text, skip_note)

        def build_prompt() -> str:
            return _range_prompt(key, scope_label, data, obsidian_text)

    notes = [
        {"path": str(h.get("path") or ""), "title": str(h.get("title") or "")}
        for h in obs_hits
    ]
    notes_json = json.dumps(notes, ensure_ascii=False) if notes else None

    if llm.is_mock_mode():
        content = render_mock()
        provider, model, is_mock = llm.MOCK_PROVIDER, llm.MOCK_MODEL, 1
    else:
        result = await llm.generate(build_prompt(), system=SYSTEM_PROMPT)
        content = _assemble_live(doc_title, result.text, skip_note)
        provider, model, is_mock = result.provider, result.model, 0

    rid = db.execute(
        "INSERT INTO reports(kind, scope_label, range_start, range_end, task_id, "
        "content_md, provider, model, is_mock, context_notes, created_at, owner_id) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            scope_label,
            start_iso,
            end_iso,
            task_value,
            content,
            provider,
            model,
            is_mock,
            notes_json,
            timeutil.now_iso(),
            None if owner_id is None else int(owner_id),
        ),
    )
    if not rid:
        raise ReportError("报告正文已生成，但写入数据库失败")
    logger.info("报告 #%s 已生成（kind=%s，is_mock=%s）", rid, key, is_mock)
    return int(rid)


def list_reports(limit: int = 50, *, owner_id: int | None = None) -> list[Any]:
    try:
        size = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        size = 50
    if owner_id is None:
        return db.query(
            "SELECT * FROM reports ORDER BY created_at DESC, id DESC LIMIT ?", (size,)
        )
    return db.query(
        "SELECT * FROM reports WHERE owner_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (int(owner_id), size),
    )


def get_report(rid: int, *, owner_id: int | None = None) -> Any:
    try:
        report_id = int(rid)
    except (TypeError, ValueError):
        return None
    if owner_id is None:
        return db.one("SELECT * FROM reports WHERE id = ?", (report_id,))
    return db.one(
        "SELECT * FROM reports WHERE id = ? AND owner_id = ?", (report_id, int(owner_id))
    )


def delete_report(rid: int, *, owner_id: int | None = None) -> None:
    try:
        report_id = int(rid)
    except (TypeError, ValueError):
        return None
    if owner_id is None:
        db.execute("DELETE FROM reports WHERE id = ?", (report_id,))
        return None
    db.execute(
        "DELETE FROM reports WHERE id = ? AND owner_id = ?", (report_id, int(owner_id))
    )
    return None


def parse_context_notes(raw: Any) -> list[dict]:
    """把 reports.context_notes（JSON 数组）解析成 list[dict]；坏数据返回空列表。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]
