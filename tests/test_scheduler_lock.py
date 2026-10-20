"""调度器跨进程日锁（W-06）：抢锁语义 + _run_daily 入口门禁。

覆盖：首次抢到 / 同日再抢失败 / 锁落后于今天可再抢 / 锁被占时 _run_daily 不发送 /
抢到时 _run_daily 走到发送 / 同一天跑两次只发送一次。发送函数全程 mock，不联网。
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from app import db, timeutil
from app.services import reminders, routines, scheduler

# 与 _run_daily 用同一套「今天」：锁被本次以外的东西推进到今天，才等于「今天扫过了」。
TODAY = timeutil.today().isoformat()
YESTERDAY = (timeutil.today() - timedelta(days=1)).isoformat()


def _set_lock(value: str) -> None:
    db.execute("UPDATE settings SET value = ? WHERE key = 'reminder_lock'", (value,))


def _lock_value() -> str:
    row = db.one("SELECT value FROM settings WHERE key = 'reminder_lock'")
    return "" if row is None else str(row["value"])


@pytest.fixture(autouse=True)
def _lock_ready(app):
    """依赖 session 级 app 夹具建库；每个用例从「今天还没扫过」的干净锁开始。"""
    db.init_db()
    db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('reminder_lock', '')")
    _set_lock("")
    yield
    db.close_threadlocal()


@pytest.fixture
def sends(monkeypatch):
    """把 send_daily 换成记录器（不读 llm、不发 webhook）；顺带隔离例行补齐。"""
    calls: list[str] = []

    def fake_send_daily():
        calls.append(TODAY)
        return {"has_items": False, "channel": "disabled", "ok": False}

    def fake_ensure_periods(owner_id=None):
        return {"created": 0}

    monkeypatch.setattr(reminders, "send_daily", fake_send_daily)
    monkeypatch.setattr(routines, "ensure_periods", fake_ensure_periods)
    return calls


def test_首次抢锁成功_同一天再抢失败():
    assert scheduler.acquire_daily_lock(TODAY) is True
    assert _lock_value() == TODAY
    assert scheduler.acquire_daily_lock(TODAY) is False
    assert _lock_value() == TODAY


def test_锁落后于今天时可以再抢():
    _set_lock(YESTERDAY)
    assert scheduler.acquire_daily_lock(TODAY) is True
    assert _lock_value() == TODAY


def test_锁被占用时_run_daily_不发送(sends, caplog):
    assert scheduler.acquire_daily_lock(TODAY) is True

    with caplog.at_level(logging.INFO, logger="app.services.scheduler"):
        scheduler._run_daily()

    assert sends == []
    assert any(TODAY in record.getMessage() for record in caplog.records)


def test_抢到锁时_run_daily_走到发送(sends):
    _set_lock(YESTERDAY)

    scheduler._run_daily()

    assert len(sends) == 1
    assert _lock_value() == timeutil.today().isoformat()


def test_同一天_run_daily_跑两次只发送一次(sends):
    scheduler._run_daily()
    scheduler._run_daily()

    assert len(sends) == 1


def test_调度器注册的入口就是_run_daily():
    """提交给 APScheduler 的 job 函数必须是 _run_daily（日锁才覆盖真实入口）。"""
    scheduler.start()
    try:
        job = scheduler._scheduler.get_job(scheduler.JOB_ID)
        assert job is not None
        assert job.func is scheduler._run_daily
    finally:
        scheduler.shutdown()
