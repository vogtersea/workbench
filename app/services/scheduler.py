"""APScheduler 每日提醒扫描（SPEC.md 第 5 / 7 节第 4 条）。

main.py 在 lifespan 里调用 start() / shutdown()（签名固定，不得更改）。
设置页改 remind_time 后调用 reschedule() 热更新，无需重启进程。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import timeutil
from app.config import settings
from app.services import settings_store

logger = logging.getLogger(__name__)

JOB_ID = "daily_reminder"
DEFAULT_TIME = "09:00"
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

_scheduler: BackgroundScheduler | None = None


def parse_hhmm(raw: str | None) -> tuple[int, int]:
    """把 "09:00" 解析成 (9, 0)；非法值退化为 (9, 0)。"""
    match = _TIME_RE.match((raw or "").strip())
    if not match:
        return 9, 0
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return 9, 0


def remind_time() -> tuple[int, int]:
    """settings.remind_time 优先，空则用 env REMIND_TIME，再退化到 09:00。"""
    try:
        raw = settings_store.get("remind_time", "")
    except Exception:
        raw = ""
    return parse_hhmm(raw or settings.remind_time or DEFAULT_TIME)


def _trigger() -> CronTrigger:
    hour, minute = remind_time()
    return CronTrigger(hour=hour, minute=minute, timezone=timeutil.tz())


def _routine_owner_ids() -> list[int | None]:
    """需要补齐例行的归属：多用户返回各启用用户 id；开放/单口令模式返回 [None]。"""
    from app.services import users as users_service

    rows = users_service.list_users()
    if not rows:
        return [None]
    return [int(row["id"]) for row in rows if int(row["active"] or 0)]


def _ensure_routines() -> None:
    """每日兜底：扫描提醒之前，为所有启用用户补齐本期例行实例（SPEC 16.5）。

    失败只记日志，不中断当日提醒；legacy / 开放模式退化为 owner_id=None 的全局补齐。
    """
    try:
        from app.services import routines

        owner_ids = _routine_owner_ids()
    except Exception:
        logger.exception("准备例行兜底失败，本次跳过")
        return

    created = 0
    for owner_id in owner_ids:
        try:
            result = routines.ensure_periods(owner_id=owner_id)
            created += int(result.get("created") or 0)
        except Exception:
            logger.exception("补齐本期例行实例失败（owner_id=%s）", owner_id)
    logger.info("例行兜底完成：归属 %s 个，新建本期实例 %s 条", len(owner_ids), created)


def _run_daily() -> None:
    """定时任务主体：任何异常都吞掉并记日志，避免调度器线程崩掉。"""
    from app.services import reminders

    _ensure_routines()
    try:
        result = reminders.send_daily()
        logger.info(
            "每日提醒扫描完成：has_items=%s channel=%s ok=%s",
            result.get("has_items"), result.get("channel"), result.get("ok"),
        )
    except Exception:
        logger.exception("每日提醒扫描失败")


def start() -> None:
    """启动后台调度器（幂等：重复调用不会产生第二个调度器）。"""
    global _scheduler
    if _scheduler is not None:
        try:
            if _scheduler.running:
                return
        except Exception:
            pass
        _scheduler = None

    scheduler = BackgroundScheduler(timezone=timeutil.tz(), daemon=True)
    hour, minute = remind_time()
    scheduler.add_job(
        _run_daily,
        trigger=_trigger(),
        id=JOB_ID,
        name="每日提醒扫描",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("调度器已启动：每天 %02d:%02d（%s）", hour, minute, settings.tz_name)


def shutdown() -> None:
    """安全停止；重复调用 / 未启动均无副作用。"""
    global _scheduler
    current = _scheduler
    _scheduler = None
    if current is None:
        return
    try:
        current.shutdown(wait=False)
    except Exception:
        logger.exception("调度器停止失败（已忽略）")


def next_run() -> str | None:
    """下一次扫描时间 ISO8601；未启动返回 None。"""
    scheduler = _scheduler
    if scheduler is None:
        return None
    try:
        job = scheduler.get_job(JOB_ID)
        run_time = getattr(job, "next_run_time", None) if job is not None else None
        return run_time.isoformat() if run_time is not None else None
    except Exception:
        logger.debug("读取 next_run 失败", exc_info=True)
        return None


def reschedule() -> str | None:
    """按最新 settings.remind_time 热更新触发器；未启动则直接启动。"""
    hour, minute = remind_time()
    scheduler = _scheduler
    if scheduler is None or not getattr(scheduler, "running", False):
        start()
        return next_run()
    try:
        scheduler.reschedule_job(JOB_ID, trigger=_trigger())
        logger.info("调度器已改期：每天 %02d:%02d", hour, minute)
    except Exception:
        logger.exception("热更新触发器失败，改为重建任务")
        try:
            scheduler.add_job(
                _run_daily,
                trigger=_trigger(),
                id=JOB_ID,
                name="每日提醒扫描",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
            )
        except Exception:
            logger.exception("重建调度任务失败")
    return next_run()


def status() -> dict[str, Any]:
    """给设置页展示的调度器状态。"""
    hour, minute = remind_time()
    scheduler = _scheduler
    running = bool(scheduler is not None and getattr(scheduler, "running", False))
    return {
        "running": running,
        "job_id": JOB_ID,
        "remind_time": f"{hour:02d}:{minute:02d}",
        "timezone": settings.tz_name,
        "next_run": next_run(),
    }
