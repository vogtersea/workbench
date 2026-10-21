"""例行工作域：周期算法 + 模板 CRUD + 本期实例生成（SPEC.md 第 16 节）。

跨模块契约（第二轮 UI 域 / 调度与报告域依赖，不得更改）：
    period_of(freq, d)              -> (period_key, period_start, period_end)
    due_date_of(routine, period)    -> date
    ensure_periods(owner_id, today) -> {"created","skipped","routines",...}
    next_due_label(routine)         -> "本周期截止 10-25 周日"
    skip_period(routine_id, today)  -> 被置为 cancelled 的条数

周期一律用 date / calendar / isocalendar 计算，不引第三方库；周从周一开始。
多用户（SPEC 15.7）：读接口带 owner_id 过滤，别人的模板等同于不存在（页面/API 转 404）；
新建模板的 owner_id 取当前生效用户（开放/单口令模式下为 NULL）；
生成的实例复制模板自身的 owner_id，模板为 NULL 则实例也为 NULL。

幂等（SPEC 16.2 / 16.5）：唯一部分索引 uq_tasks_routine_period 兜住并发重复，
ensure_periods 内「先查后插 + 捕获 IntegrityError」双保险；只保证当前周期存在，不回补历史。
"""

from __future__ import annotations

import calendar
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta

from app import db, timeutil
from app.services import tasks

logger = logging.getLogger(__name__)

FREQS: tuple[str, ...] = (
    "daily",
    "weekly",
    "monthly",
    "quarterly",
    "half_year",
    "yearly",
)
FREQ_LABELS: dict[str, str] = {
    "daily": "每日",
    "weekly": "每周",
    "monthly": "每月",
    "quarterly": "每季度",
    "half_year": "每半年",
    "yearly": "每年",
}
DUE_RULES: tuple[str, ...] = ("period_end", "day_of_period")
INSTANCE_STATUS: str = "todo"

BASE_SELECT = "SELECT r.* FROM routines r"
FREQ_ORDER = (
    " ORDER BY r.active DESC, CASE r.freq"
    " WHEN 'daily' THEN 0 WHEN 'weekly' THEN 1 WHEN 'monthly' THEN 2"
    " WHEN 'quarterly' THEN 3 WHEN 'half_year' THEN 4 ELSE 5 END, r.id"
)
INSTANCE_SELECT = """
SELECT t.*, p.name AS project_name, p.color AS project_color,
       r.title AS routine_title, r.freq AS routine_freq
  FROM tasks t
  JOIN routines r ON r.id = t.routine_id
  LEFT JOIN projects p ON p.id = t.project_id
"""


# ---------------------------------------------------------------- 基础清洗

def _day(value=None) -> date:
    """把 date / datetime / 'YYYY-MM-DD' 归一成 date；None 用今天。"""
    if value is None:
        return timeutil.today()
    parsed = timeutil.parse_date(value)
    if parsed is None:
        raise ValueError("日期格式不正确（应为 YYYY-MM-DD）：" + str(value))
    return parsed


def clean_freq(value) -> str:
    """周期取值校验（六种之一，大小写不敏感）。"""
    text = str(value or "").strip().lower()
    if text not in FREQS:
        raise ValueError(
            "周期取值不合法（应为 daily/weekly/monthly/quarterly/half_year/yearly）：" + text
        )
    return text


_clean_freq = clean_freq


def freq_label(freq) -> str:
    text = str(freq or "").strip().lower()
    return FREQ_LABELS.get(text, text)


def _clean_due_rule(value, default: str = "period_end") -> str:
    text = str(value or "").strip().lower() or default
    if text not in DUE_RULES:
        raise ValueError("截止日规则不合法（应为 period_end / day_of_period）：" + text)
    return text


def _clean_due_day(value) -> int | None:
    """截止日是「本周期第几天」（1 基）；空值表示用期末。"""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        number = int(float(text))
    except (TypeError, ValueError):
        raise ValueError("截止日需为 1–31 的整数：" + text) from None
    if number < 1:
        raise ValueError("截止日需为 1–31 的整数：" + text)
    return min(number, 31)


def _clean_time(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).strftime("%H:%M")
        except ValueError:
            continue
    raise ValueError("时间格式不正确（应为 HH:MM）：" + text)


def _clean_priority(value) -> int:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 2
    return number if number in (1, 2, 3) else 2


def _clean_tags(value) -> str:
    parts = [part.strip() for part in str(value or "").split(",")]
    return ",".join(part for part in parts if part)


def _clean_date(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _day(text).isoformat()


def _clean_project(value, owner_id: int | None) -> int | None:
    """项目外键清洗：跨用户的项目 id 一律丢弃（按不属于自己处理）。"""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        project_id = int(text)
    except (TypeError, ValueError):
        return None
    return project_id if tasks.get_project(project_id, owner_id=owner_id) is not None else None


def _owner_clause(owner_id: int | None, alias: str = "r") -> tuple[str, tuple]:
    """归属过滤片段：owner_id 为 None 时返回空，等价于开放/单口令模式的全局查询。"""
    if owner_id is None:
        return "", ()
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}owner_id = ?", (int(owner_id),)


def _owner_value(owner_id: int | None) -> int | None:
    return None if owner_id is None else int(owner_id)


# ---------------------------------------------------------------- 周期算法（SPEC 16.3）

def _month_end(year: int, month: int) -> date:
    """自然月最后一天（闰年 2 月自动为 29，靠 calendar 而非手写判断）。"""
    return date(year, month, calendar.monthrange(year, month)[1])


def period_of(freq: str, d=None) -> tuple[str, date, date]:
    """返回 (period_key, period_start, period_end)；周按 ISO 周（周一起算）。"""
    key_freq = _clean_freq(freq)
    day = _day(d)

    if key_freq == "daily":
        return day.isoformat(), day, day

    if key_freq == "weekly":
        monday = timeutil.week_start(day)
        iso = monday.isocalendar()
        return f"{iso.year}-W{iso.week:02d}", monday, monday + timedelta(days=6)

    if key_freq == "monthly":
        start = date(day.year, day.month, 1)
        return f"{day.year}-{day.month:02d}", start, _month_end(day.year, day.month)

    if key_freq == "quarterly":
        quarter = (day.month - 1) // 3 + 1
        first_month = 3 * quarter - 2
        last_month = 3 * quarter
        return (
            f"{day.year}-Q{quarter}",
            date(day.year, first_month, 1),
            _month_end(day.year, last_month),
        )

    if key_freq == "half_year":
        half = 1 if day.month <= 6 else 2
        first_month = 1 if half == 1 else 7
        last_month = 6 if half == 1 else 12
        return (
            f"{day.year}-H{half}",
            date(day.year, first_month, 1),
            _month_end(day.year, last_month),
        )

    return str(day.year), date(day.year, 1, 1), date(day.year, 12, 31)


_WEEK_KEY = re.compile(r"^(\d{4})-W(\d{1,2})$")
_MONTH_KEY = re.compile(r"^(\d{4})-(\d{2})$")
_QUARTER_KEY = re.compile(r"^(\d{4})-Q([1-4])$")
_HALF_KEY = re.compile(r"^(\d{4})-H([12])$")
_YEAR_KEY = re.compile(r"^(\d{4})$")


def period_bounds(freq: str, period_key) -> tuple[date, date]:
    """由 period_key 反解 (period_start, period_end)，用于展示历史期次。"""
    key_freq = _clean_freq(freq)
    key = str(period_key or "").strip()

    if key_freq == "daily":
        day = _day(key)
        return day, day

    if key_freq == "weekly":
        match = _WEEK_KEY.match(key)
        if not match:
            raise ValueError("周期标识不合法（周应形如 2026-W43）：" + key)
        year, week = int(match.group(1)), int(match.group(2))
        try:
            monday = date.fromisocalendar(year, week, 1)
        except ValueError:
            raise ValueError("周期标识不合法（ISO 周越界）：" + key) from None
        return monday, monday + timedelta(days=6)

    if key_freq == "monthly":
        match = _MONTH_KEY.match(key)
        if not match:
            raise ValueError("周期标识不合法（月应形如 2026-10）：" + key)
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            raise ValueError("周期标识不合法（月份越界）：" + key)
        return date(year, month, 1), _month_end(year, month)

    if key_freq == "quarterly":
        match = _QUARTER_KEY.match(key)
        if not match:
            raise ValueError("周期标识不合法（季度应形如 2026-Q4）：" + key)
        year, quarter = int(match.group(1)), int(match.group(2))
        return date(year, 3 * quarter - 2, 1), _month_end(year, 3 * quarter)

    if key_freq == "half_year":
        match = _HALF_KEY.match(key)
        if not match:
            raise ValueError("周期标识不合法（半年应形如 2026-H2）：" + key)
        year, half = int(match.group(1)), int(match.group(2))
        return (
            date(year, 1 if half == 1 else 7, 1),
            _month_end(year, 6 if half == 1 else 12),
        )

    match = _YEAR_KEY.match(key)
    if not match:
        raise ValueError("周期标识不合法（年应形如 2026）：" + key)
    year = int(match.group(1))
    return date(year, 1, 1), date(year, 12, 31)


def period_display(freq: str, period_key) -> str:
    """周期中文标签，用于历史列表与 JSON 展示。"""
    key = str(period_key or "").strip()
    key_freq = str(freq or "").strip().lower()
    match = _YEAR_KEY.match(key)
    if key_freq == "yearly" and match:
        return match.group(1) + " 年"
    if key_freq == "half_year" and _HALF_KEY.match(key):
        return f"{key[:4]} 年 第{key[-1]}个半年"
    if key_freq == "quarterly" and _QUARTER_KEY.match(key):
        return f"{key[:4]} 年 Q{key[-1]}"
    if key_freq == "monthly" and _MONTH_KEY.match(key):
        return f"{key[:4]} 年 {int(key[5:7])} 月"
    if key_freq == "weekly" and _WEEK_KEY.match(key):
        try:
            monday, _end = period_bounds("weekly", key)
        except ValueError:
            return key
        return f"{monday.isocalendar().year} 年第 {monday.isocalendar().week} 周"
    return key


def _as_period(freq: str, period=None) -> tuple[str, date, date]:
    """把 None / date / period_key / (key, start, end) 统一成三元组。"""
    if period is None:
        return period_of(freq, timeutil.today())
    if isinstance(period, (tuple, list)):
        if len(period) == 3:
            key, start, end = period
            return str(key), _day(start), _day(end)
        raise ValueError("周期参数不合法（应为 (period_key, start, end)）")
    if isinstance(period, (date, datetime)):
        return period_of(freq, period)
    text = str(period).strip()
    try:
        start, end = period_bounds(freq, text)
    except ValueError:
        return period_of(freq, _day(text))
    return text, start, end


def _col(routine, name, default=None):
    """模板既可能是 sqlite3.Row 也可能是 dict，统一取值。"""
    try:
        keys = set(routine.keys())
    except AttributeError:
        keys = set()
    if name in keys:
        return routine[name]
    try:
        return routine.get(name, default)  # type: ignore[union-attr]
    except AttributeError:
        return default


def due_date_of(routine, period=None) -> date:
    """截止日：period_end=期末；day_of_period=周期第 N 天（越界夹到期末）；daily 恒为当天。"""
    freq = _clean_freq(_col(routine, "freq"))
    key, start, end = _as_period(freq, period)
    if freq == "daily":
        return start
    rule = str(_col(routine, "due_rule") or "period_end").strip().lower()
    if rule == "day_of_period":
        raw = _col(routine, "due_day")
        try:
            number = int(raw) if raw not in (None, "") else 1
        except (TypeError, ValueError):
            number = 1
        target = start + timedelta(days=max(1, number) - 1)
        return min(target, end)
    return end


def next_due_label(routine) -> str:
    """UI 展示：本周期截止 10-25 周日。"""
    due = due_date_of(routine, None)
    return "本周期截止 " + timeutil.fmt_cn(due)


# ---------------------------------------------------------------- 模板读取

def list_routines(
    *, owner_id: int | None = None, include_inactive: bool = True
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list = []
    if owner_id is not None:
        clauses.append("r.owner_id = ?")
        params.append(int(owner_id))
    if not include_inactive:
        clauses.append("r.active = 1")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return db.query(BASE_SELECT + where + FREQ_ORDER, tuple(params))


def get_routine(routine_id: int, *, owner_id: int | None = None) -> sqlite3.Row | None:
    """取单个模板；带 owner_id 时别人的模板等同于不存在（返回 None）。"""
    clause, extra = _owner_clause(owner_id)
    return db.one(BASE_SELECT + " WHERE r.id = ?" + clause, (int(routine_id), *extra))


def _require_row(routine_id: int) -> sqlite3.Row:
    row = db.one(BASE_SELECT + " WHERE r.id = ?", (int(routine_id),))
    if row is None:
        raise ValueError("例行模板不存在")
    return row


# ---------------------------------------------------------------- 模板写入

def create_routine(data: dict, *, owner_id: int | None = None) -> int:
    """新建模板；owner_id 取调用方传入的当前生效用户（开放模式为 None）。"""
    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("标题不能为空")
    freq = _clean_freq(data.get("freq"))
    due_rule = _clean_due_rule(data.get("due_rule"))
    due_day = _clean_due_day(data.get("due_day"))
    if due_rule == "period_end":
        due_day = None
    elif due_day is None:
        due_day = 1
    now = timeutil.now_iso()
    active = 1 if str(data.get("active", 1)).strip() not in ("0", "False", "false", "") else 0
    return db.execute(
        """
        INSERT INTO routines(owner_id, title, detail, done_criteria, freq, due_rule, due_day,
                             due_time, priority, project_id, tags, active, start_date,
                             created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _owner_value(owner_id),
            title,
            str(data.get("detail") or ""),
            str(data.get("done_criteria") or ""),
            freq,
            due_rule,
            due_day,
            _clean_time(data.get("due_time")),
            _clean_priority(data.get("priority")),
            _clean_project(data.get("project_id"), owner_id),
            _clean_tags(data.get("tags")),
            active,
            _clean_date(data.get("start_date")),
            now,
            now,
        ),
        returning_id=True,
    )


def update_routine(routine_id: int, data: dict, *, owner_id: int | None = None) -> None:
    """部分更新；未出现的字段保持原值。"""
    current = get_routine(routine_id, owner_id=owner_id)
    if current is None:
        raise ValueError("例行模板不存在")

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
    if "freq" in data:
        sets["freq"] = _clean_freq(data.get("freq"))
    if "due_rule" in data:
        rule = _clean_due_rule(data.get("due_rule"))
        sets["due_rule"] = rule
        if rule == "period_end":
            sets["due_day"] = None
        elif "due_day" not in data:
            sets["due_day"] = _clean_due_day(current["due_day"]) or 1
    if "due_day" in data:
        sets["due_day"] = _clean_due_day(data.get("due_day"))
    if "due_time" in data:
        sets["due_time"] = _clean_time(data.get("due_time"))
    if "priority" in data:
        sets["priority"] = _clean_priority(data.get("priority"))
    if "project_id" in data:
        sets["project_id"] = _clean_project(data.get("project_id"), owner_id)
    if "active" in data:
        sets["active"] = 0 if str(data.get("active")).strip() in ("0", "False", "false", "") else 1
    if "start_date" in data:
        sets["start_date"] = _clean_date(data.get("start_date"))

    if sets.get("due_rule") == "day_of_period" and not sets.get("due_day"):
        sets["due_day"] = _clean_due_day(current["due_day"]) or 1

    sets["updated_at"] = timeutil.now_iso()
    assignments = ", ".join(f"{key} = ?" for key in sets)
    clause, extra = _owner_clause(owner_id, alias="")
    db.execute(
        f"UPDATE routines SET {assignments} WHERE id = ?" + clause,
        (*sets.values(), int(routine_id), *extra),
    )


def set_active(routine_id: int, active: bool, *, owner_id: int | None = None) -> None:
    current = get_routine(routine_id, owner_id=owner_id)
    if current is None:
        raise ValueError("例行模板不存在")
    clause, extra = _owner_clause(owner_id, alias="")
    db.execute(
        "UPDATE routines SET active = ?, updated_at = ? WHERE id = ?" + clause,
        (1 if active else 0, timeutil.now_iso(), int(routine_id), *extra),
    )


def count_instances(routine_id: int) -> int:
    row = db.one("SELECT COUNT(*) AS n FROM tasks WHERE routine_id = ?", (int(routine_id),))
    return int(row["n"]) if row is not None else 0


def delete_routine(
    routine_id: int, *, purge: bool = False, owner_id: int | None = None
) -> dict:
    """默认只置 active=0；purge=True 时删除模板并级联删除其实例（进展随之删除）。"""
    current = get_routine(routine_id, owner_id=owner_id)
    if current is None:
        raise ValueError("例行模板不存在")

    if not purge:
        set_active(routine_id, False, owner_id=owner_id)
        return {
            "id": int(routine_id),
            "purged": False,
            "deactivated": True,
            "deleted_tasks": 0,
            "instances": count_instances(routine_id),
        }

    deleted = count_instances(routine_id)
    clause, extra = _owner_clause(owner_id, alias="")
    with db.cursor() as conn:
        conn.execute(
            "DELETE FROM progress_entries WHERE task_id IN "
            "(SELECT id FROM tasks WHERE routine_id = ?)",
            (int(routine_id),),
        )
        conn.execute("DELETE FROM tasks WHERE routine_id = ?", (int(routine_id),))
        conn.execute("DELETE FROM routines WHERE id = ?" + clause, (int(routine_id), *extra))
    logger.info("例行模板 %s 已彻底删除，级联删除实例 %s 条", routine_id, deleted)
    return {
        "id": int(routine_id),
        "purged": True,
        "deactivated": False,
        "deleted_tasks": deleted,
        "instances": deleted,
    }


# ---------------------------------------------------------------- 实例读取

def current_period_key(routine, today=None) -> str:
    return period_of(_col(routine, "freq"), _day(today))[0]


def current_instance(routine_id: int, today=None) -> sqlite3.Row | None:
    """本期实例（tasks 行）；模板不存在或本期未生成返回 None。"""
    row = db.one(BASE_SELECT + " WHERE r.id = ?", (int(routine_id),))
    if row is None:
        return None
    key = current_period_key(row, today)
    return db.one(
        "SELECT * FROM tasks WHERE routine_id = ? AND period_key = ?",
        (int(routine_id), key),
    )


def list_instances(routine_id: int, *, limit: int = 24) -> list[sqlite3.Row]:
    """历史实例，按周期倒序（最近 limit 期）。"""
    return db.query(
        INSTANCE_SELECT + " WHERE t.routine_id = ? ORDER BY t.period_key DESC, t.id DESC LIMIT ?",
        (int(routine_id), max(1, int(limit))),
    )


def period_instances(*, owner_id: int | None = None, today=None) -> list[dict]:
    """当前周期下所有启用模板的实例（未生成的模板不出现）。"""
    day = _day(today)
    rows = list_routines(owner_id=owner_id, include_inactive=False)
    pairs = [(int(row["id"]), current_period_key(row, day)) for row in rows]
    if not pairs:
        return []
    params: list = [value for pair in pairs for value in pair]
    ors = " OR ".join("(t.routine_id = ? AND t.period_key = ?)" for _ in pairs)
    clause, extra = _owner_clause(owner_id, alias="t")
    found = db.query(
        INSTANCE_SELECT
        + f" WHERE ({ors})"
        + clause
        + " ORDER BY (t.status IN ('todo', 'doing')) DESC, t.due_date, t.id",
        tuple(params) + extra,
    )
    return [serialize_instance(row) for row in found]


# ---------------------------------------------------------------- 生成（SPEC 16.3 / 16.5）

def _instance_payload(routine, key: str, start: date, end: date) -> dict:
    """把模板字段映射成一条本期任务（SPEC 16.3：复制 title/…/tags，due_time 复制）。"""
    due = due_date_of(routine, (key, start, end))
    return {
        "project_id": _col(routine, "project_id"),
        "title": _col(routine, "title"),
        "detail": _col(routine, "detail") or "",
        "done_criteria": _col(routine, "done_criteria") or "",
        "due_date": due.isoformat(),
        # 周期期末落在周日时，week_start 必须取该 due_date 所在 ISO 周的周一
        "week_start": timeutil.week_start(due).isoformat(),
        "due_time": _col(routine, "due_time"),
        "priority": _col(routine, "priority"),
        "tags": _col(routine, "tags") or "",
        "owner_id": _col(routine, "owner_id"),
        "routine_id": int(_col(routine, "id")),
        "period_key": key,
    }


def _insert_instance(payload: dict) -> int:
    now = timeutil.now_iso()
    return db.execute(
        """
        INSERT INTO tasks(project_id, title, detail, done_criteria, due_date, due_time,
                          status, priority, week_start, tags, completed_at, archived,
                          created_at, updated_at, owner_id, routine_id, period_key)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?, ?)
        """,
        (
            payload["project_id"],
            payload["title"],
            payload["detail"],
            payload["done_criteria"],
            payload["due_date"],
            payload["due_time"],
            INSTANCE_STATUS,
            payload["priority"],
            payload["week_start"],
            payload["tags"],
            now,
            now,
            payload["owner_id"],
            payload["routine_id"],
            payload["period_key"],
        ),
        returning_id=True,
    )


def _not_started(routine, key: str, start: date) -> bool:
    """start_date 所属周期晚于当前周期时，本期还不该生成（SPEC 16.2 start_date 语义）。"""
    start_date = timeutil.parse_date(_col(routine, "start_date"))
    if start_date is None:
        return False
    return period_of(_col(routine, "freq"), start_date)[0] != key and (
        period_of(_col(routine, "freq"), start_date)[1] > start
    )


def _ensure_one(routine, day: date) -> str:
    """对单个模板保证本期实例存在；返回 created / skipped / pending。"""
    freq = _col(routine, "freq")
    key, start, end = period_of(freq, day)
    routine_id = int(_col(routine, "id"))

    if _not_started(routine, key, start):
        return "pending"

    # 先查：绝大多数重复调用走这条路径，不产生写操作
    existing = db.one(
        "SELECT id FROM tasks WHERE routine_id = ? AND period_key = ?", (routine_id, key)
    )
    if existing is not None:
        return "skipped"

    payload = _instance_payload(routine, key, start, end)
    try:
        task_id = _insert_instance(payload)
    except sqlite3.IntegrityError:
        # 双保险：并发下另一个进程/线程先插入了同一 (routine_id, period_key)
        logger.info("本期例行已由并发写入创建：routine=%s period=%s", routine_id, key)
        return "skipped"
    logger.info(
        "生成本期例行：routine=%s period=%s task=%s due=%s",
        routine_id,
        key,
        task_id,
        payload["due_date"],
    )
    return "created"


def ensure_periods(*, owner_id: int | None = None, today=None) -> dict:
    """保证当前周期实例存在；幂等，不补历史周期。

    owner_id=None 表示全部模板（调度兜底用）；多用户请求路径传当前生效用户 id。
    """
    day = _day(today)
    rows = list_routines(owner_id=owner_id, include_inactive=False)
    created = skipped = pending = 0
    for routine in rows:
        result = _ensure_one(routine, day)
        if result == "created":
            created += 1
        else:
            skipped += 1
            if result == "pending":
                pending += 1
    return {
        "created": created,
        "skipped": skipped,
        "routines": len(rows),
        "pending": pending,
        "today": day.isoformat(),
    }


def skip_period(routine_id: int, today=None, *, owner_id: int | None = None) -> int:
    """跳过本期：把本期实例置 status='cancelled'（不计逾期、不再提醒）。"""
    routine = get_routine(routine_id, owner_id=owner_id)
    if routine is None:
        raise ValueError("例行模板不存在")
    day = _day(today)
    result = _ensure_one(routine, day)
    if result == "pending":
        return 0
    key = current_period_key(routine, day)
    clause, extra = _owner_clause(owner_id, alias="")
    return db.execute(
        "UPDATE tasks SET status = 'cancelled', archived = 0, completed_at = NULL, "
        "updated_at = ? WHERE routine_id = ? AND period_key = ?" + clause,
        (timeutil.now_iso(), int(routine_id), key, *extra),
    )


# ---------------------------------------------------------------- 序列化

def serialize_instance(row) -> dict:
    """本期/历史实例：复用任务序列化，额外带 routine_id / period_key / 模板标题。"""
    item = tasks.serialize_task(row)
    keys = set(row.keys())
    routine_id = row["routine_id"] if "routine_id" in keys else None
    item["routine_id"] = routine_id
    item["period_key"] = row["period_key"] if "period_key" in keys else None
    item["owner_id"] = row["owner_id"] if "owner_id" in keys else None
    item["is_routine"] = routine_id is not None
    if "routine_title" in keys:
        item["routine_title"] = row["routine_title"]
    if "routine_freq" in keys:
        item["routine_freq"] = row["routine_freq"]
        item["routine_freq_label"] = freq_label(row["routine_freq"])
    return item


def serialize_routine(row, today=None) -> dict:
    """模板 + 本期周期信息 + 本期实例状态（列表与详情共用同一形状）。"""
    day = _day(today)
    freq = str(row["freq"])
    key, start, end = period_of(freq, day)
    due = due_date_of(row, (key, start, end))
    instance = db.one(
        "SELECT * FROM tasks WHERE routine_id = ? AND period_key = ?",
        (int(row["id"]), key),
    )
    tags = str(row["tags"] or "")
    return {
        "id": int(row["id"]),
        "owner_id": row["owner_id"],
        "title": row["title"],
        "detail": row["detail"],
        "done_criteria": row["done_criteria"],
        "freq": freq,
        "freq_label": freq_label(freq),
        "due_rule": row["due_rule"],
        "due_day": row["due_day"],
        "due_time": row["due_time"],
        "priority": int(row["priority"]),
        "project_id": row["project_id"],
        "tags": tags,
        "tag_list": [part for part in tags.split(",") if part],
        "active": int(row["active"]),
        "start_date": row["start_date"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "period_key": key,
        "period_display": period_display(freq, key),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "due_date": due.isoformat(),
        "due_label": timeutil.fmt_cn(due),
        "next_due_label": "本周期截止 " + timeutil.fmt_cn(due),
        "not_started": _not_started(row, key, start),
        "instance": serialize_instance(instance) if instance is not None else None,
        "instance_id": int(instance["id"]) if instance is not None else None,
        "instance_status": instance["status"] if instance is not None else None,
        "instance_done": bool(instance is not None and instance["status"] == "done"),
    }
