"""报告 Markdown 渲染：MOCK 模式下确定性的模板渲染（各小节与正文拼装）。

职责边界（W-08）：只把事实渲染成 Markdown，不读库、不写库、不拼提示词。"""

from __future__ import annotations

from typing import Any

from app import timeutil

from .collect import KIND_LABELS, MOCK_NOTICE, _field
from .prompt import (
    _due_text,
    _join_md,
    _priority_text,
    _progress_line,
    _routine_counts,
    _routine_note,
    _status_text,
    _tags_text,
    _ts,
)

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
        risks.append("未登记截止日期：无法进入逾期/临期提醒。")
    elif status in {"todo", "doing"}:
        delta = (due - timeutil.today()).days
        if delta < 0:
            risks.append("已逾期 %s 天（截止日期 %s）。" % (-delta, due.isoformat()))
        elif delta == 0:
            risks.append("截止日期就是今天。")
    if not str(_field(row, "done_criteria") or "").strip():
        risks.append("完成条件未填写，收尾缺少可核对的标准。")
    if not progress:
        risks.append("没有任何进展记录，过程不可追溯。")
    if status == "cancelled":
        risks.append("状态为已取消，需确认相关工作是否仍需推进。")
    if not risks:
        risks.append("基于入库字段未发现结构性风险（截止日期、完成条件、进展均已登记）。")
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
        steps.append("若确定重启，先改回「待办」并重新评估截止日期与完成条件。")
    if timeutil.parse_date(_field(row, "due_date")) is None:
        steps.append("补充截止日期，让它进入逾期/临期提醒。")
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
                "- %s%s（状态：%s，截止日期：%s）"
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
            "无截止日期的未完成工作 %s 项：%s" % (len(no_due), "、".join(no_due[:8]))
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
        steps.append("为无截止日期的结转项补上截止日期，让它进入提醒。")
    steps.append("下期继续按「完成条件」逐项核对，避免只记录进展不销号。")
    return "\n".join("- %s" % item for item in steps)


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
        "- 截止日期：%s" % _due_text(row),
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
