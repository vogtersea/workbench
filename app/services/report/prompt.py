"""报告事实文本与提示词拼装：把结构化事实写成中文事实行与提示词，含 SYSTEM_PROMPT。

职责边界（W-08）：只产字符串，不读库、不落库；render 渲染 MOCK 正文时复用这里的事实行工具。"""

from __future__ import annotations

import re
from typing import Any

from app import db, timeutil

from .collect import KIND_LABELS, OBSIDIAN_SKIP_NOTE, _field, logger

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
    "6. 全文中文，简洁、可核查，不使用夸张修辞。\n"
    "7. 标题不要自己加编号（不要写「一、」「（一）」「1.」），编号由导出程序按公文层级自动生成；\n"
    "   需要分层时用更多 ## 号表示下级标题即可。\n"
    "8. 不要使用项目符号列表（- 或 *），改用「一是……二是……」这类公文句式。"
)


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


def _progress_line(row: Any) -> str:
    content = " ".join(str(_field(row, "content") or "").split()) or "（空内容）"
    when = _ts(_field(row, "created_at"))
    link = str(_field(row, "link") or "").strip()
    prefix = "%s｜" % when if when else ""
    suffix = "（链接：%s）" % link if link else ""
    return "%s%s%s" % (prefix, content, suffix)


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


# ---------------------------------------------------------------- MOCK 渲染


def _join_md(lines: list[str], obsidian_text: str, skip_note: str) -> str:
    if obsidian_text:
        lines += ["", "## 相关知识库笔记（仅作背景参考）", "", obsidian_text]
    if skip_note:
        lines += ["", "---", "", "> %s" % skip_note]
    return "\n".join(lines).strip() + "\n"


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
