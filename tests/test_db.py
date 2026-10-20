"""db 层：连接复用（W-05）、日锁默认值（W-06）、登录节流表（W-07）。

覆盖：同线程复用单连接 / 事务异常回滚后连接仍可用 / 跨线程连接隔离 /
init_db 幂等并补齐 login_attempts 与 reminder_lock / 4 线程各 50 条并发写入无锁冲突。
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from app import db

NOW = "2026-01-05T09:00:00"
INSERT_TASK = (
    "INSERT INTO tasks(title, week_start, created_at, updated_at) VALUES(?,?,?,?)"
)


@pytest.fixture(autouse=True)
def _isolate_threadlocal(app):
    """依赖 session 级 app 夹具建好库；用例前后清掉本线程缓存连接。"""
    db.close_threadlocal()
    yield
    db.close_threadlocal()


def test_同一线程三次查询只建一条连接(monkeypatch):
    created: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        created.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", counting_connect)

    for _ in range(3):
        assert db.query("SELECT 1 AS v")[0]["v"] == 1

    assert len(created) == 1
    assert db.connect() is created[0]

    # 显式释放后再查询才需要新开连接（close_threadlocal 的语义）
    db.close_threadlocal()
    db.query("SELECT 1 AS v")
    assert len(created) == 2


def test_cursor_异常回滚且同连接仍可读():
    marker = "wb-test-rollback"

    with pytest.raises(RuntimeError, match="boom"):
        with db.cursor() as conn:
            conn.execute(INSERT_TASK, (marker, "2026-01-05", NOW, NOW))
            raise RuntimeError("boom")

    assert db.query("SELECT id FROM tasks WHERE title = ?", (marker,)) == []
    # 回滚没有污染连接：同线程后续读写照常
    assert db.one("SELECT COUNT(*) AS c FROM tasks WHERE title = ?", (marker,))["c"] == 0


def test_跨线程连接隔离且各自可写():
    main_conn = db.connect()
    seen: dict[str, sqlite3.Connection] = {}
    errors: list[str] = []

    def worker(name: str) -> None:
        try:
            first = db.connect()
            assert first is db.connect(), "同线程内 connect() 应复用"
            seen[name] = first
            db.execute(INSERT_TASK, (f"wb-test-thread-{name}", "2026-01-05", NOW, NOW))
        except BaseException as exc:  # noqa: BLE001 - 线程内异常要带回主线程断言
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            db.close_threadlocal()

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    try:
        assert errors == []
        assert set(seen) == {"a", "b"}
        assert main_conn is not seen["a"]
        assert main_conn is not seen["b"]
        assert seen["a"] is not seen["b"]
        rows = db.query(
            "SELECT title FROM tasks WHERE title LIKE 'wb-test-thread-%' ORDER BY title"
        )
        assert [row["title"] for row in rows] == ["wb-test-thread-a", "wb-test-thread-b"]
    finally:
        db.execute("DELETE FROM tasks WHERE title LIKE 'wb-test-thread-%'")


def test_init_db_幂等并补齐节流表与日锁默认值():
    # 模拟「既有库」：两张 W-06 / W-07 新增物都不存在
    with db.cursor() as conn:
        conn.execute("DROP TABLE IF EXISTS login_attempts")
        conn.execute("DELETE FROM settings WHERE key = 'reminder_lock'")
    assert db.one("SELECT value FROM settings WHERE key = 'reminder_lock'") is None

    db.init_db()
    db.init_db()  # 幂等：第二次不报错、不重复建

    tables = {row["name"] for row in db.query("SELECT name FROM sqlite_master")}
    assert "login_attempts" in tables
    assert "idx_login_attempts" in tables
    assert db.one("SELECT value FROM settings WHERE key = 'reminder_lock'")["value"] == ""

    # 节流表按方案可写、可按 (username, source, at) 计数
    db.execute(
        "INSERT INTO login_attempts(username, source, at) VALUES(?,?,?)",
        ("alice", "10.0.0.1", NOW),
    )
    row = db.one(
        "SELECT COUNT(*) AS c FROM login_attempts WHERE username = ? AND source = ? AND at > ?",
        ("alice", "10.0.0.1", "2026-01-05T08:55:00"),
    )
    assert row["c"] == 1
    db.execute("DELETE FROM login_attempts")


def test_四线程各写五十条无数据库锁():
    prefix = "wb-test-concurrent-"
    before = db.one("SELECT COUNT(*) AS c FROM tasks")["c"]
    errors: list[str] = []
    barrier = threading.Barrier(4)

    def worker(n: int) -> None:
        try:
            barrier.wait(timeout=10)  # 尽量让四条写入流真正重叠
            for i in range(50):
                db.execute(INSERT_TASK, (f"{prefix}{n}-{i}", "2026-01-05", NOW, NOW))
        except BaseException as exc:  # noqa: BLE001 - 线程内异常要带回主线程断言
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            db.close_threadlocal()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    try:
        assert errors == []
        assert db.one("SELECT COUNT(*) AS c FROM tasks WHERE title LIKE ?", (prefix + "%",))[
            "c"
        ] == 200
        assert db.one("SELECT COUNT(*) AS c FROM tasks")["c"] == before + 200
    finally:
        db.execute("DELETE FROM tasks WHERE title LIKE ?", (prefix + "%",))
