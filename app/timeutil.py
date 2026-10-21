"""日期 / ISO 周工具（SPEC.md 第 5 节，签名固定）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings

WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def tz() -> ZoneInfo:
    """业务时区对象（默认 Asia/Shanghai，可用 TZ_NAME 覆盖）。"""
    try:
        return ZoneInfo(settings.tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    return datetime.now(tz())


def now_iso() -> str:
    """当前业务时间，ISO8601 字符串（精确到秒）。"""
    return now().replace(microsecond=0).isoformat()


def today() -> date:
    return now().date()


def week_start(d: date) -> date:
    """所属 ISO 周的周一。"""
    return d - timedelta(days=d.weekday())


def week_bounds(offset: int = 0) -> tuple[date, date]:
    """相对本周 offset 周的周一 / 周日。"""
    start = week_start(today()) + timedelta(days=7 * offset)
    return start, start + timedelta(days=6)


def parse_date(value) -> date | None:
    """把 date / datetime / 'YYYY-MM-DD' / ISO8601 字符串统一成 date，失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def fmt_cn(d) -> str:
    """中文短日期：06-04 周三；无法解析时返回空串。"""
    day = parse_date(d)
    if day is None:
        return ""
    return f"{day.month:02d}-{day.day:02d} {WEEKDAYS_CN[day.weekday()]}"


def days_until(due: str | None) -> int | None:
    """距离截止日期的天数：正=未来，0=今天，负=逾期；无截止日期返回 None。"""
    day = parse_date(due)
    if day is None:
        return None
    return (day - today()).days
