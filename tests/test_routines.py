"""例行工作期次幂等（W-03，SPEC 16.2 / 16.5）。

覆盖：建 daily 模板 -> 连续两次 ensure_periods，同一 (routine_id, period_key)
只有 1 条实例；并直接验证部分唯一索引挡得住重复插入。
"""

from __future__ import annotations

import sqlite3

import pytest

from app import db, timeutil


def _instances(routine_id: int) -> list:
    return db.query(
        "SELECT id, period_key, status FROM tasks WHERE routine_id = ? ORDER BY id",
        (routine_id,),
    )


async def test_连续两次补生成只产生一条实例(client):
    created = await client.post("/api/routines", json={"title": "每日站会", "freq": "daily"})
    assert created.status_code == 201
    routine_id = created.json()["id"]

    first = (await client.post("/api/routines/ensure")).json()
    assert first["routines"] == 1
    assert first["created"] == 1

    second = (await client.post("/api/routines/ensure")).json()
    assert second["created"] == 0
    assert second["skipped"] == 1

    key = timeutil.today().isoformat()
    rows = _instances(routine_id)
    assert len(rows) == 1
    assert rows[0]["period_key"] == key
    assert rows[0]["status"] == "todo"

    detail = (await client.get(f"/api/routines/{routine_id}")).json()
    assert len(detail["history"]) == 1
    assert detail["current"]["period_key"] == key


async def test_同一模板同期的唯一索引生效(client):
    created = await client.post("/api/routines", json={"title": "每周复盘", "freq": "weekly"})
    routine_id = created.json()["id"]
    await client.post("/api/routines/ensure")

    row = db.one("SELECT * FROM tasks WHERE routine_id = ?", (routine_id,))
    now = timeutil.now_iso()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO tasks(title, status, priority, week_start, created_at, updated_at, "
            "routine_id, period_key) VALUES(?,?,?,?,?,?,?,?)",
            ("重复实例", "todo", 2, row["week_start"], now, now, routine_id, row["period_key"]),
        )
    assert len(_instances(routine_id)) == 1
