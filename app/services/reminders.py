"""站内 + Webhook 每日提醒（SPEC.md 第 5 / 7 节第 3~5 条）。

本模块只读写 tasks / reminders_log / settings 三张表，不直接调用任务域
（app.services.tasks）的任何函数；唯一的外部依赖是 routines.ensure_periods，
在扫描前补齐本期例行实例（SPEC 16.5 兜底），失败只记日志不影响提醒本身。

    逾期 due_date < today；今日 due_date = today；
    临期 today < due_date <= today + remind_soon_days。

多用户（SPEC.md 15.7）：scan(owner_id) 只看单个归属；scan_all() 按用户分组；
build_message_multi() 生成含各用户小节的合并日报；send_daily() 在 users 表非空时
自动走合并日报，users 表为空时逐字保持改造前的单用户格式。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app import db, security, timeutil
from app.config import settings
from app.services import settings_store

logger = logging.getLogger(__name__)

#: SPEC 第 7 节第 5 条规定的 6 种 Webhook 格式
WEBHOOK_FORMATS: tuple[str, ...] = (
    "generic_json",
    "wecom_text",
    "feishu_text",
    "serverchan",
    "telegram",
    "bark",
)
WEBHOOK_TIMEOUT = 10.0          # 秒
WEBHOOK_RETRY = 1               # 失败后重试 1 次（共 2 次尝试）
MAX_ITEMS_PER_SECTION = 10      # 每段最多列 10 条
TITLE_PREFIX = "工作台提醒"
PAYLOAD_LIMIT = 4000            # 写库时截断，避免日志表被超长正文撑爆
ROUTINE_TAG = "[例行]"           # 例行实例在日报条目上的前缀（SPEC 16.4）
LAST_SCAN_KEY = "last_scan_json"
LAST_TEST_KEY = "last_test_push_json"


# --------------------------------------------------------------------------
# settings 读取（全部带默认值，绝不抛异常）
# --------------------------------------------------------------------------
def _soon_days() -> int:
    """临期阈值（天），来自 settings.remind_soon_days，非法值退化为 3。"""
    try:
        value = int(float((settings_store.get("remind_soon_days", "3") or "3").strip()))
    except (TypeError, ValueError):
        return 3
    return value if 0 <= value <= 365 else 3


def _webhook_url() -> str:
    """settings 优先，空则回落到环境变量 WEBHOOK_URL。"""
    try:
        url = settings_store.get("webhook_url", "")
    except Exception:
        url = ""
    return (url or settings.webhook_url or "").strip()


def _webhook_format() -> str:
    try:
        fmt = (settings_store.get("webhook_format", "generic_json") or "").strip()
    except Exception:
        fmt = "generic_json"
    return fmt if fmt in WEBHOOK_FORMATS else "generic_json"


def _telegram_chat_id() -> str:
    try:
        return (settings_store.get("telegram_chat_id", "") or "").strip()
    except Exception:
        return ""


def _title() -> str:
    day = timeutil.today()
    return f"{TITLE_PREFIX} {day.isoformat()}"


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------
def _routine_id(row) -> int | None:
    """任务行是否为例行实例（SPEC 16.2：routine_id 非空即本期例行）。"""
    try:
        value = row["routine_id"]
    except (IndexError, KeyError, TypeError):
        return None
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return None


def _row_to_item(row) -> dict[str, Any]:
    due = row["due_date"]
    return {
        "id": int(row["id"]),
        "title": row["title"] or "",
        "status": row["status"] or "",
        "priority": int(row["priority"] or 2),
        "due_date": due,
        "due_time": row["due_time"],
        "days_until": timeutil.days_until(due),
        "week_start": row["week_start"],
        "routine_id": _routine_id(row),
    }


def _ensure_routines(owner_id: int | None = None) -> dict[str, Any] | None:
    """扫描前补齐本期例行实例（SPEC 16.5）；任何异常只记日志，不影响提醒本身。

    owner_id=None 是开放 / 单口令模式的全局补齐；多用户模式逐用户调用。
    """
    try:
        from app.services import routines  # 延迟导入，避免模块级环

        return routines.ensure_periods(owner_id=owner_id)
    except Exception:
        logger.warning(
            "补齐本期例行实例失败（owner_id=%s），已忽略", owner_id, exc_info=True
        )
        return None


def _rows_for(owner_id: int | None) -> list:
    """未完成工作行；owner_id 为 None 时不加归属过滤（单用户模式保持旧行为）。"""
    sql = "SELECT * FROM tasks WHERE status IN ('todo', 'doing') AND archived = 0"
    params: tuple = ()
    if owner_id is not None:
        sql += " AND owner_id = ?"
        params = (int(owner_id),)
    return db.query(sql + " ORDER BY (due_date IS NULL), due_date, priority DESC, id", params)


def _split(rows, soon_days: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    overdue: list[dict[str, Any]] = []
    today_items: list[dict[str, Any]] = []
    soon: list[dict[str, Any]] = []
    for row in rows:
        item = _row_to_item(row)
        days = item["days_until"]
        if days is None:
            continue  # 无 DDL 不进提醒区
        if days < 0:
            overdue.append(item)
        elif days == 0:
            today_items.append(item)
        elif days <= soon_days:
            soon.append(item)
    return overdue, today_items, soon


def _scan_result(owner_id: int | None, today, soon_days: int) -> dict[str, Any]:
    """分桶结果（不含 message，供 scan / scan_all 复用）。"""
    overdue, today_items, soon = _split(_rows_for(owner_id), soon_days)
    return {
        "overdue": overdue,
        "today": today_items,
        "soon": soon,
        "counts": {
            "overdue": len(overdue),
            "today": len(today_items),
            "soon": len(soon),
            "total": len(overdue) + len(today_items) + len(soon),
        },
        "today_date": today.isoformat(),
        "soon_days": soon_days,
        "generated_at": timeutil.now_iso(),
        "tz": settings.tz_name,
        "has_items": bool(overdue or today_items or soon),
    }


def scan(owner_id: int | None = None) -> dict[str, Any]:
    """扫描未完成工作并分三桶。

    owner_id 为 None 时不过滤归属（改造前的行为，单用户模式与开放模式必须一致）；
    多用户模式必须传生效用户 id，绝不返回他人数据。

    返回 {"overdue": [...], "today": [...], "soon": [...],
          "message": str, "has_items": bool,
          "counts": {...}, "today_date": str, "soon_days": int, "generated_at": str}

    扫描前先补齐本期例行实例，未完成的例行自然落入逾期 / 今日 / 临期三桶（SPEC 16.4）。
    """
    _ensure_routines(owner_id)
    result = _scan_result(owner_id, timeutil.today(), _soon_days())
    result["message"] = build_message(result)
    return result


# --------------------------------------------------------------------------
# 多用户：按用户分组扫描（SPEC.md 15.7）
# --------------------------------------------------------------------------
def scan_all(user_ids: list[int] | None = None) -> dict[str, Any]:
    """按用户分组扫描未完成工作。

    user_ids 为空 = 全部启用用户（管理员视角）；给出 id 列表时只看这些用户
    （普通用户只看自己，管理员 ?as= 只看目标用户）。停用用户不参与排期。
    """
    from app.services import users as users_service  # 延迟导入，避免模块级环

    today = timeutil.today()
    soon_days = _soon_days()
    allowed = {int(uid) for uid in user_ids} if user_ids else None
    grouped: list[dict[str, Any]] = []
    totals = {"overdue": 0, "today": 0, "soon": 0, "total": 0}
    user_rows = users_service.list_users()
    if not user_rows:
        # 开放 / 单口令模式没有用户维度，按全局补齐一次（与扫描口径一致）
        _ensure_routines(None)
    for row in user_rows:
        if not int(row["active"] or 0):
            continue
        user_id = int(row["id"])
        if allowed is not None and user_id not in allowed:
            continue
        _ensure_routines(user_id)
        one = _scan_result(user_id, today, soon_days)
        counts = one["counts"]
        grouped.append(
            {
                "user_id": user_id,
                "username": row["username"] or "",
                "display_name": row["display_name"] or row["username"] or "",
                "overdue": one["overdue"],
                "today": one["today"],
                "soon": one["soon"],
                "counts": counts,
            }
        )
        for key in ("overdue", "today", "soon", "total"):
            totals[key] += int(counts.get(key) or 0)

    result: dict[str, Any] = {
        "users": grouped,
        "totals": totals,
        "has_items": bool(totals["total"]),
        "user_count": len(grouped),
        "active_user_count": sum(1 for user in grouped if int(user["counts"]["total"])),
        "today_date": today.isoformat(),
        "soon_days": soon_days,
        "generated_at": timeutil.now_iso(),
        "tz": settings.tz_name,
    }
    result["message"] = build_message_multi(result)
    return result


# --------------------------------------------------------------------------
# 中文纯文本日报
# --------------------------------------------------------------------------
def _relative(days: int | None) -> str:
    if days is None:
        return "无 DDL"
    if days < 0:
        return f"逾期 {-days} 天"
    if days == 0:
        return "今日到期"
    return f"还剩 {days} 天"


def _item_line(index: int, item: dict[str, Any]) -> str:
    title = (item.get("title") or "(未命名)").strip()
    if len(title) > 60:
        title = title[:58] + "…"
    tag = ROUTINE_TAG + " " if item.get("routine_id") else ""
    priority = item.get("priority")
    flag = " [高]" if priority == 3 else (" [低]" if priority == 1 else "")
    due = item.get("due_date") or ""
    ddl = timeutil.fmt_cn(due) or str(due) or "无"
    return f"{index}. {tag}{title}{flag} — DDL {ddl} · {_relative(item.get('days_until'))}"


def _section(header: str, items: list[dict[str, Any]]) -> list[str]:
    lines = [f"■ {header}（{len(items)} 项）"]
    if not items:
        lines.append("（无）")
        return lines
    for index, item in enumerate(items[:MAX_ITEMS_PER_SECTION], start=1):
        lines.append(_item_line(index, item))
    extra = len(items) - MAX_ITEMS_PER_SECTION
    if extra > 0:
        lines.append(f"…另有 {extra} 项未列出")
    return lines


def build_message(scan_result: dict[str, Any] | None = None) -> str:
    """把扫描结果渲染成中文纯文本日报（标题含日期、三段计数、每段最多 10 条）。"""
    result = scan_result if isinstance(scan_result, dict) else scan()
    day = timeutil.parse_date(result.get("today_date")) or timeutil.today()
    counts = result.get("counts") or {}
    soon_days = result.get("soon_days")
    try:
        soon_days = int(soon_days)
    except (TypeError, ValueError):
        soon_days = _soon_days()

    lines: list[str] = [
        f"【{TITLE_PREFIX}】{day.isoformat()} {timeutil.WEEKDAYS_CN[day.weekday()]}",
        "",
        f"逾期 {counts.get('overdue', 0)} 项 · 今日到期 {counts.get('today', 0)} 项 · "
        f"{soon_days} 日内到期 {counts.get('soon', 0)} 项",
        f"（扫描时间 {result.get('generated_at') or timeutil.now_iso()} · 时区 {settings.tz_name}）",
        "",
    ]
    lines += _section("逾期", list(result.get("overdue") or []))
    lines.append("")
    lines += _section("今日到期", list(result.get("today") or []))
    lines.append("")
    lines += _section(f"{soon_days} 日内到期", list(result.get("soon") or []))
    lines.append("")
    if not result.get("has_items"):
        lines.append("今天没有需要提醒的未完成工作。")
        lines.append("")
    lines.append("— 由工作台自动发送")
    return "\n".join(lines)


def _counts_text(counts: dict[str, Any]) -> str:
    """「1 逾期 · 2 今日」；全为 0 时给「0 项」。"""
    parts: list[str] = []
    for key, label in (("overdue", "逾期"), ("today", "今日"), ("soon", "临期")):
        value = int(counts.get(key) or 0)
        if value:
            parts.append(f"{value} {label}")
    return " · ".join(parts) or "0 项"


def build_message_multi(scan_all_result: dict[str, Any] | None = None) -> str:
    """合并日报：标题含日期与总计数，随后每个有待提醒项的用户一个小节（最多 10 条）。

    没有任何用户有待提醒项时只出标题段 + 一句「今天没有需要提醒的未完成工作。」，
    绝不为空用户生成空小节。
    """
    result = scan_all_result if isinstance(scan_all_result, dict) else scan_all()
    day = timeutil.parse_date(result.get("today_date")) or timeutil.today()
    totals = result.get("totals") or {}
    try:
        soon_days = int(result.get("soon_days"))
    except (TypeError, ValueError):
        soon_days = _soon_days()
    pending = [
        user
        for user in (result.get("users") or [])
        if int((user.get("counts") or {}).get("total") or 0)
    ]

    lines: list[str] = [
        f"【{TITLE_PREFIX}】{day.isoformat()} {timeutil.WEEKDAYS_CN[day.weekday()]}",
        "",
        f"逾期 {totals.get('overdue', 0)} 项 · 今日到期 {totals.get('today', 0)} 项 · "
        f"{soon_days} 日内到期 {totals.get('soon', 0)} 项",
        f"（扫描时间 {result.get('generated_at') or timeutil.now_iso()} · 时区 {settings.tz_name}"
        f" · 涉及用户 {len(pending)} 人）",
        "",
    ]
    for user in pending:
        name = user.get("username") or user.get("display_name") or f"用户 {user.get('user_id')}"
        lines.append(f"■ {name}（{_counts_text(user.get('counts') or {})}）")
        items = (
            list(user.get("overdue") or [])
            + list(user.get("today") or [])
            + list(user.get("soon") or [])
        )
        for index, item in enumerate(items[:MAX_ITEMS_PER_SECTION], start=1):
            lines.append(_item_line(index, item))
        extra = len(items) - MAX_ITEMS_PER_SECTION
        if extra > 0:
            lines.append(f"…另有 {extra} 项未列出")
        lines.append("")
    if not pending:
        lines.append("今天没有需要提醒的未完成工作。")
        lines.append("")
    lines.append("— 由工作台自动发送")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Webhook 序列化与推送
# --------------------------------------------------------------------------
def build_payload(message: str, fmt: str | None = None) -> dict[str, Any]:
    """按 SPEC 第 7 节第 5 条序列化 6 种格式。"""
    target = (fmt or _webhook_format()).strip()
    if target not in WEBHOOK_FORMATS:
        target = "generic_json"
    title = _title()
    if target == "generic_json":
        return {"text": message}
    if target == "wecom_text":
        return {"msgtype": "text", "text": {"content": message}}
    if target == "feishu_text":
        return {"msg_type": "text", "content": {"text": message}}
    if target == "serverchan":
        return {"title": title, "desp": message}
    if target == "telegram":
        return {"chat_id": _telegram_chat_id(), "text": message}
    return {"title": title, "body": message}  # bark


def push(message: str, *, kind: str = "daily") -> dict[str, Any]:
    """同步 POST 到 WEBHOOK_URL；超时 10s，失败重试 1 次。

    未配置 URL 时返回 {"ok": False, "channel": "disabled"}，绝不抛异常。
    kind 只用于日志归属，不影响 payload。
    """
    url = _webhook_url()
    fmt = _webhook_format()
    if not url:
        return {"ok": False, "channel": "disabled", "error": "未配置 WEBHOOK_URL，仅站内提醒"}
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "channel": fmt, "error": f"WEBHOOK_URL 不是 http(s) 地址：{url[:60]}"}

    payload = build_payload(message, fmt)
    last_error: str | None = None
    for attempt in range(WEBHOOK_RETRY + 1):
        try:
            response = httpx.post(url, json=payload, timeout=WEBHOOK_TIMEOUT)
            if response.status_code < 400:
                return {"ok": True, "channel": fmt, "error": None}
            last_error = f"HTTP {response.status_code}: {(response.text or '')[:200]}"
        except Exception as exc:  # httpx 超时 / 连接失败 / 序列化异常
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < WEBHOOK_RETRY:
            logger.warning("Webhook 第 %d 次推送失败，重试 1 次：%s", attempt + 1, last_error)
    logger.error("Webhook 推送失败（已重试 1 次）：%s", last_error)
    return {"ok": False, "channel": fmt, "error": last_error}


# --------------------------------------------------------------------------
# 日志与最近一次摘要
# --------------------------------------------------------------------------
def _log(*, kind: str, channel: str, payload: str, ok: bool, error: str | None) -> None:
    """写 reminders_log；日志失败绝不影响主流程。"""
    try:
        db.execute(
            "INSERT INTO reminders_log(sent_at, kind, channel, payload, ok, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                timeutil.now_iso(),
                kind,
                channel or "disabled",
                (payload or "")[:PAYLOAD_LIMIT],
                1 if ok else 0,
                (error or "")[:1000] or None,
            ),
        )
    except Exception:
        logger.exception("写 reminders_log 失败")


def _store_json(key: str, data: dict[str, Any]) -> None:
    try:
        settings_store.set(key, json.dumps(data, ensure_ascii=False))
    except Exception:
        logger.exception("写 settings.%s 失败", key)


def _load_json(key: str) -> dict[str, Any] | None:
    """容错解析：坏数据、空串、非字典一律返回 None。"""
    try:
        raw = settings_store.get(key, "")
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("settings.%s 内容不是合法 JSON，已忽略", key)
        return None
    return data if isinstance(data, dict) else None


def last_scan() -> dict[str, Any] | None:
    """最近一次扫描摘要（解析失败返回 None，设置页据此显示“暂无记录”）。"""
    return _load_json(LAST_SCAN_KEY)


def last_test_push() -> dict[str, Any] | None:
    return _load_json(LAST_TEST_KEY)


def _summary(*, sent_at: str, channel: str, ok: bool, error: str | None,
             scan_result: dict[str, Any], message: str, kind: str) -> dict[str, Any]:
    return {
        "sent_at": sent_at,
        "kind": kind,
        "channel": channel or "disabled",
        "ok": bool(ok),
        "error": error,
        "has_items": bool(scan_result.get("has_items")),
        "counts": scan_result.get("counts") or {},
        "soon_days": scan_result.get("soon_days"),
        "today_date": scan_result.get("today_date"),
        "message_head": (message or "")[:600],
    }


# --------------------------------------------------------------------------
# 每日流程 / 测试推送
# --------------------------------------------------------------------------
def send_daily() -> dict[str, Any]:
    """users 表非空（多用户）走合并日报，否则逐字保持改造前的单用户流程。"""
    if security.users_exist():
        result = _send_daily_multi()
    else:
        result = _send_daily_single()
    _notify_desktop(result)
    return result


def _notify_desktop(result: dict[str, Any]) -> None:
    """桌面版：有提醒项时弹一条系统通知。服务器版没注册 notifier，这里是 no-op。"""
    try:
        from app.services import desktop_bridge

        if not result.get("has_items"):
            return
        counts = result.get("counts") or {}
        body = "逾期 %s / 今日 %s / 临期 %s" % (
            counts.get("overdue", 0),
            counts.get("today", 0),
            counts.get("soon", 0),
        )
        desktop_bridge.notify("工作台 · 每日提醒", body, "/")
    except Exception:
        logger.debug("桌面通知跳过", exc_info=True)


def _send_daily_single() -> dict[str, Any]:
    """改造前的单用户流程，一字未改：scan -> push -> 日志 -> 摘要落 settings。"""
    sent_at = timeutil.now_iso()
    scan_result = scan()
    message = str(scan_result.get("message") or "")
    counts = scan_result.get("counts") or {}

    if not scan_result.get("has_items"):
        channel = _webhook_format() if _webhook_url() else "disabled"
        error = (
            f"无待提醒项（逾期 {counts.get('overdue', 0)} / 今日 {counts.get('today', 0)} / "
            f"临期 {counts.get('soon', 0)}），未推送"
        )
        _log(kind="daily", channel=channel, payload=message, ok=False, error=error)
        summary = _summary(sent_at=sent_at, channel=channel, ok=False, error=error,
                           scan_result=scan_result, message=message, kind="daily")
        _store_json(LAST_SCAN_KEY, summary)
        return {
            "ok": True,
            "has_items": False,
            "sent_at": sent_at,
            "channel": channel,
            "error": None,
            "counts": counts,
            "message": message,
            "note": error,
        }

    push_result = push(message, kind="daily")
    ok = bool(push_result.get("ok"))
    channel = str(push_result.get("channel") or "disabled")
    error = push_result.get("error")
    _log(kind="daily", channel=channel, payload=message, ok=ok, error=error if not ok else None)
    summary = _summary(sent_at=sent_at, channel=channel, ok=ok, error=error if not ok else None,
                       scan_result=scan_result, message=message, kind="daily")
    _store_json(LAST_SCAN_KEY, summary)
    return {
        "ok": ok,
        "has_items": True,
        "sent_at": sent_at,
        "channel": channel,
        "error": error if not ok else None,
        "counts": counts,
        "message": message,
    }


def _send_daily_multi() -> dict[str, Any]:
    """多用户合并日报：scan_all -> 合并消息 -> push -> 日志 -> 摘要（含总计数与用户数）。"""
    sent_at = timeutil.now_iso()
    scan_result = scan_all()
    message = str(scan_result.get("message") or "")
    totals = dict(scan_result.get("totals") or {})
    extra = {
        "mode": "multi",
        "user_count": scan_result.get("user_count"),
        "active_user_count": scan_result.get("active_user_count"),
    }

    if not scan_result.get("has_items"):
        channel = _webhook_format() if _webhook_url() else "disabled"
        error = (
            f"无待提醒项（逾期 {totals.get('overdue', 0)} / 今日 {totals.get('today', 0)} / "
            f"临期 {totals.get('soon', 0)}），未推送"
        )
        _log(kind="daily", channel=channel, payload=message, ok=False, error=error)
        summary = _summary(sent_at=sent_at, channel=channel, ok=False, error=error,
                           scan_result=scan_result, message=message, kind="daily")
        summary.update(extra)
        summary["counts"] = totals
        _store_json(LAST_SCAN_KEY, summary)
        return {
            "ok": True,
            "has_items": False,
            "sent_at": sent_at,
            "channel": channel,
            "error": None,
            "counts": totals,
            "message": message,
            "note": error,
            **extra,
        }

    push_result = push(message, kind="daily")
    ok = bool(push_result.get("ok"))
    channel = str(push_result.get("channel") or "disabled")
    error = push_result.get("error")
    _log(kind="daily", channel=channel, payload=message, ok=ok, error=error if not ok else None)
    summary = _summary(sent_at=sent_at, channel=channel, ok=ok, error=error if not ok else None,
                       scan_result=scan_result, message=message, kind="daily")
    summary.update(extra)
    summary["counts"] = totals
    _store_json(LAST_SCAN_KEY, summary)
    return {
        "ok": ok,
        "has_items": True,
        "sent_at": sent_at,
        "channel": channel,
        "error": error if not ok else None,
        "counts": totals,
        "message": message,
        **extra,
    }


def test_push() -> dict[str, Any]:
    """发送一条测试推送并写 reminders_log（未配置 URL 时也写）。"""
    sent_at = timeutil.now_iso()
    message = "\n".join(
        [
            f"【{TITLE_PREFIX}】测试推送",
            f"发送时间 {sent_at}",
            f"时区 {settings.tz_name}",
            "收到本消息说明 Webhook 配置可用。",
        ]
    )
    result = push(message, kind="test")
    ok = bool(result.get("ok"))
    channel = str(result.get("channel") or "disabled")
    error = result.get("error")
    _log(kind="test", channel=channel, payload=message, ok=ok, error=error if not ok else None)
    outcome = {
        "ok": ok,
        "channel": channel,
        "error": error if not ok else None,
        "sent_at": sent_at,
        "kind": "test",
    }
    _store_json(LAST_TEST_KEY, outcome)
    return {**outcome, "message": message}
