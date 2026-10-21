"""报告生成服务（原 app/services/reports.py 按子域拆分，W-08）。

职责边界：对外 API 层——generate 编排（采集、提示词、渲染、落库）与报告读写，
并显式 re-export 拆分前的全部公开符号，pages_reports.py 与 routes_reports.py 一行都不用改。
子模块：collect（事实采集）、prompt（事实文本与提示词）、render（Markdown 渲染）。"""

from __future__ import annotations

import json
from typing import Any

from app import db, timeutil
from app.services import llm

# 公开符号显式 re-export：保持既有 from app.services import reports 用法不变
from .collect import (
    KINDS as KINDS,
    logger as logger,
    KIND_LABELS as KIND_LABELS,
    MOCK_NOTICE as MOCK_NOTICE,
    OBSIDIAN_SKIP_NOTE as OBSIDIAN_SKIP_NOTE,
    ReportError as ReportError,
    _collect_range as _collect_range,
    _collect_task as _collect_task,
    _field as _field,
    _task_span as _task_span,
    resolve_range as resolve_range,
)
from .prompt import (
    PRIORITY_TEXT as PRIORITY_TEXT,
    ROUTINE_FREQ_TEXT as ROUTINE_FREQ_TEXT,
    STATUS_TEXT as STATUS_TEXT,
    SYSTEM_PROMPT as SYSTEM_PROMPT,
    _keywords as _keywords,
    _obsidian_context as _obsidian_context,
    _range_prompt as _range_prompt,
    _task_prompt as _task_prompt,
)
from .render import (
    _mock_range_md as _mock_range_md,
    _mock_task_md as _mock_task_md,
)

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
        returning_id=True,
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


def content_of(row: Any) -> str:
    """报告正文：改过就用改后的，否则用生成时的原文（W-11）。

    行里没有 content_md_edited 字段（老库未迁移）时按未编辑处理。
    """
    if row is None:
        return ""
    try:
        edited = row["content_md_edited"]
    except (KeyError, IndexError):
        edited = None
    return edited if edited else (row["content_md"] or "")


def save_content(rid: int, content: str, *, owner_id: int | None = None) -> bool:
    """保存页内编辑的正文；content 为空串表示恢复原文（置 NULL）。返回是否命中该报告。"""
    try:
        report_id = int(rid)
    except (TypeError, ValueError):
        return False
    value = (content or "").strip()
    sql = "UPDATE reports SET content_md_edited = ? WHERE id = ?"
    params: list[Any] = [value or None, report_id]
    if owner_id is not None:
        sql += " AND owner_id = ?"
        params.append(int(owner_id))
    return db.execute(sql, tuple(params)) == 1


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
