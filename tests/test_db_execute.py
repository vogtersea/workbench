"""db.execute() 返回类型显式化（W-10）。

返回类型由 returning_id 决定，不再按 SQL 首词猜测：
returning_id=True 取新行 id，默认取受影响行数；语句自带 RETURNING 时以结果集为准。
语句没有真正插入行（ON CONFLICT DO NOTHING 命中、rowcount 无法判定）时返回 0，
绝不回落到连接上残留的 lastrowid。
"""

from __future__ import annotations

import pytest

from app import db

NOW = "2026-01-05T09:00:00"
PREFIX = "wb-test-w10-"
SETTINGS_KEY = "wb-test-w10"

# 同一条普通 INSERT，只靠 returning_id 切换口径
INSERT_TASK = "INSERT INTO tasks(title, week_start, created_at, updated_at) VALUES(?,?,?,?)"
INSERT_TASK_RETURNING = INSERT_TASK + " RETURNING id"
INSERT_TASK_CTE = (
    "WITH payload(title) AS (SELECT ?) "
    "INSERT INTO tasks(title, week_start, created_at, updated_at) "
    "SELECT title, ?, ?, ? FROM payload"
)
PARAMS = ("2026-01-05", NOW, NOW)


@pytest.fixture(autouse=True)
def _isolate(app):
    """依赖 session 级 app 建好库；用例前后清本线程连接与本用例写入的行。"""
    db.close_threadlocal()
    yield
    db.execute("DELETE FROM tasks WHERE title LIKE ?", (PREFIX + "%",))
    db.execute("DELETE FROM users WHERE username LIKE ?", (PREFIX + "%",))
    db.execute("DELETE FROM settings WHERE key LIKE ?", (SETTINGS_KEY + "%",))
    db.close_threadlocal()


def _task_id(title: str) -> int | None:
    row = db.one("SELECT id FROM tasks WHERE title = ?", (title,))
    return None if row is None else int(row["id"])


def test_普通插入_returning_id_返回新行_id():
    title = PREFIX + "plain-id"
    new_id = db.execute(INSERT_TASK, (title, *PARAMS), returning_id=True)
    assert new_id > 0
    assert _task_id(title) == new_id


def test_同一语句不加_returning_id_返回受影响行数():
    title = PREFIX + "plain-rowcount"
    affected = db.execute(INSERT_TASK, (title, *PARAMS))
    assert affected == 1
    assert _task_id(title) is not None


def test_update_返回受影响行数_零与一():
    title = PREFIX + "update"
    task_id = db.execute(INSERT_TASK, (title, *PARAMS), returning_id=True)

    missed = db.execute("UPDATE tasks SET title = ? WHERE id = ?", (title + "-x", 999999))
    assert missed == 0

    hit = db.execute("UPDATE tasks SET title = ? WHERE id = ?", (title + "-y", task_id))
    assert hit == 1
    assert _task_id(title + "-y") == task_id


def test_insert_returning_以结果集为准_不再被首词判断坑到():
    title = PREFIX + "returning"
    new_id = db.execute(INSERT_TASK_RETURNING, (title, *PARAMS), returning_id=True)
    assert new_id > 0
    assert new_id == _task_id(title)

    # 不声明时口径是受影响行数；结果集必须已被取空，否则收尾 commit 会报
    # "SQL statements in progress"
    title2 = PREFIX + "returning-2"
    assert db.execute(INSERT_TASK_RETURNING, (title2, *PARAMS)) == 1
    assert _task_id(title2) is not None


def test_cte_insert_能正确插入且显式声明才拿得到_id():
    title = PREFIX + "cte"
    default_value = db.execute(INSERT_TASK_CTE, (title, *PARAMS))
    cte_id = _task_id(title)
    assert cte_id is not None
    assert default_value != cte_id  # 带 CTE 的写语句给不出 id，改造前首词判断按 WITH 走行数

    title2 = PREFIX + "cte-2"
    new_id = db.execute(INSERT_TASK_CTE, (title2, *PARAMS), returning_id=True)
    assert new_id > 0
    assert new_id == _task_id(title2)


def test_do_nothing_带_returning_未插入时返回_0():
    username = PREFIX + "conflict"
    insert_user = (
        "INSERT INTO users(username, display_name, password_hash, role, active, created_at) "
        "VALUES(?, ?, 'x', 'user', 1, ?)"
    )
    first = db.execute(insert_user + " RETURNING id", (username, username, NOW), returning_id=True)
    assert first > 0

    # ON CONFLICT DO NOTHING 没有插入：没有返回行就必须是 0，而不是上一次插入残留的 id
    skipped = db.execute(
        insert_user + " ON CONFLICT(username) DO NOTHING RETURNING id",
        (username, username, NOW),
        returning_id=True,
    )
    assert skipped == 0
    row = db.one("SELECT id FROM users WHERE username = ?", (username,))
    assert int(row["id"]) == first


def test_do_nothing_不带_returning_未插入时返回_0():
    key = SETTINGS_KEY + "-noret"
    # 先插入一条，把连接的 lastrowid 顶到一个真实 id 上（这正是改造前会被误返回的值）
    db.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (key, "v1"))

    skipped = db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
        (key, "v2"),
        returning_id=True,
    )
    assert skipped == 0  # 没插入就没有 id，不能是上一次插入残留的值
    assert db.one("SELECT value FROM settings WHERE key = ?", (key,))["value"] == "v1"


def test_do_nothing_不带_returning_未冲突时返回真实_id():
    key = SETTINGS_KEY + "-ok"
    # 同一语句形状在没冲突时确实插入，必须拿到真实 id（证明没把正常插入一并归零）
    new_id = db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
        (key, "v1"),
        returning_id=True,
    )
    assert new_id > 0
    assert db.one("SELECT value FROM settings WHERE key = ?", (key,))["value"] == "v1"
