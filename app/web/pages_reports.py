"""报告域页面（SPEC.md 第 6 节，本域整体重写）。

    GET  /reports              报告中心：生成表单 + 历史列表
    POST /reports              触发生成（同步阻塞），成功 303 到 /reports/{id}
    GET  /reports/{id}         详情：Markdown 渲染 + 打印 + 复制原文
    GET  /reports/{id}/export.docx  下载 Word（W-01），受 EXPORT_DOCX 开关控制
    POST /reports/{id}/delete  删除报告，303 回 /reports

多用户（SPEC 15.1 / 15.7 / 15.9）：归属性由 auth.effective_owner_id(request) 决定；
None 表示开放/单口令模式（不传 owner_id，保持改造前行为）。别人的报告一律 404。
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app import auth, db, templating
from app.config import settings
from app.services import export, llm, reports, tasks
from app.web.pages_tasks import as_query, owner_switch_context

logger = logging.getLogger(__name__)

pages = APIRouter()

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_TRUE_VALUES = {"1", "true", "on", "yes", "checked"}

_TASK_OPTIONS_FALLBACK = """
SELECT id, title, status FROM tasks{owner}
ORDER BY (archived = 1) ASC, COALESCE(due_date, '9999-12-31') ASC, id DESC
LIMIT 200
"""


def _owner(request: Request) -> int | None:
    """当前生效归属 id；None = 开放/单口令模式（不过滤，保持改造前行为）。"""
    return auth.effective_owner_id(request)


def _task_options(owner_id: int | None = None) -> list:
    """工作下拉：优先走任务域契约函数，未实现时退化为只读 SQL。"""
    rows: list = []
    try:
        rows = list(tasks.list_tasks(archived=0, owner_id=owner_id) or []) + list(
            tasks.list_tasks(archived=1, owner_id=owner_id) or []
        )
    except NotImplementedError:
        logger.info("tasks.list_tasks 尚未实现，报告页下拉改用只读 SQL")
        rows = db.query(
            _TASK_OPTIONS_FALLBACK.format(
                owner="" if owner_id is None else " WHERE owner_id = ?"
            ),
            () if owner_id is None else (int(owner_id),),
        )
    except Exception:
        logger.warning("读取工作列表失败，报告页下拉置空", exc_info=True)
        rows = []

    unique: list = []
    seen: set = set()
    for row in rows:
        try:
            tid = row["id"]
        except (IndexError, KeyError, TypeError):
            continue
        if tid in seen:
            continue
        seen.add(tid)
        unique.append(row)
    return unique


def _form_state(
    kind: str = "week",
    task_id: object = "",
    start: str = "",
    end: str = "",
    use_obsidian: bool = True,
) -> dict:
    return {
        "kind": (kind or "week").strip(),
        "task_id": str(task_id or "").strip(),
        "start": (start or "").strip(),
        "end": (end or "").strip(),
        "use_obsidian": bool(use_obsidian),
    }


def _render_page(
    request: Request,
    *,
    status_code: int = 200,
    error: str = "",
    form: dict | None = None,
):
    owner_id = _owner(request)
    return templating.render(
        request,
        "reports.html",
        status_code=status_code,
        error=error,
        form=form or _form_state(),
        reports_list=reports.list_reports(50, owner_id=owner_id),
        task_options=_task_options(owner_id),
        kinds=list(reports.KIND_LABELS.items()),
        kind_labels=reports.KIND_LABELS,
        owner_switch=owner_switch_context(request),
        as_q=as_query(request),
    )


@pages.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    return _render_page(request)


@pages.post("/reports", response_class=HTMLResponse)
async def reports_create(
    request: Request,
    kind: str = Form("week"),
    task_id: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
    use_obsidian: str = Form(""),
):
    use_context = use_obsidian.strip().lower() in _TRUE_VALUES
    tid: int | None = None
    raw_task = (task_id or "").strip()
    if raw_task:
        try:
            tid = int(raw_task)
        except ValueError:
            return _render_page(
                request,
                status_code=400,
                error="请选择有效的工作",
                form=_form_state(kind, raw_task, start, end, use_context),
            )

    owner_id = _owner(request)
    try:
        rid = await reports.generate(
            (kind or "").strip(),
            start=(start or "").strip() or None,
            end=(end or "").strip() or None,
            task_id=tid,
            use_obsidian=use_context,
            owner_id=owner_id,
        )
    except reports.ReportError as exc:
        return _render_page(
            request,
            status_code=400,
            error=str(exc),
            form=_form_state(kind, raw_task, start, end, use_context),
        )
    except llm.LLMError as exc:
        return _render_page(
            request,
            status_code=502,
            error=str(exc),
            form=_form_state(kind, raw_task, start, end, use_context),
        )
    except Exception as exc:
        logger.exception("生成报告失败（kind=%s）", kind)
        return _render_page(
            request,
            status_code=500,
            error="生成报告失败：%s %s" % (type(exc).__name__, exc),
            form=_form_state(kind, raw_task, start, end, use_context),
        )

    return RedirectResponse("/reports/%s" % rid, status_code=303)


@pages.get("/reports/{rid}", response_class=HTMLResponse)
def report_detail(request: Request, rid: int):
    row = reports.get_report(rid, owner_id=_owner(request))
    if row is None:
        return templating.render(
            request, "report_detail.html", status_code=404, report=None, rid=rid,
            kind_labels=reports.KIND_LABELS, notes=[],
        )
    return templating.render(
        request,
        "report_detail.html",
        report=row,
        rid=rid,
        notes=reports.parse_context_notes(row["context_notes"]),
        kind_labels=reports.KIND_LABELS,
        as_q=as_query(request),
        export_enabled=getattr(settings, "export_docx", True),
    )


@pages.get("/reports/{rid}/export.docx")
def report_export_docx(request: Request, rid: int):
    """下载公文格式 Word。开关关闭时 404，越权/不存在一律 303 回列表。"""
    if not getattr(settings, "export_docx", True):
        raise HTTPException(status_code=404, detail="Word 导出已关闭")

    row = reports.get_report(rid, owner_id=_owner(request))
    if row is None:
        return RedirectResponse("/reports", status_code=303)

    data = export.build_docx(row["content_md"], row["scope_label"])
    day = (row["created_at"] or "")[:10]
    filename = "%s%s.docx" % (row["scope_label"], "-" + day if day else "")
    headers = {
        "Content-Disposition": (
            "attachment; filename=report-%d.docx; filename*=UTF-8''%s"
            % (rid, quote(filename))
        )
    }
    return Response(content=data, media_type=DOCX_MIME, headers=headers)


@pages.post("/reports/{rid}/delete")
def report_delete(request: Request, rid: int):
    owner_id = _owner(request)
    if reports.get_report(rid, owner_id=owner_id) is None:
        return _render_page(
            request,
            status_code=404,
            error="报告 #%s 不存在或已被删除" % rid,
            form=_form_state(),
        )
    reports.delete_report(rid, owner_id=owner_id)
    return RedirectResponse("/reports", status_code=303)
