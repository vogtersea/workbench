"""任务状态机与仪表盘分区（W-03）。

覆盖：经 JSON API 建任务 -> 改状态 done（SPEC 7.2 销号归档，立刻离开活跃视图）
-> 恢复 todo（回到活跃视图）；另一条用例守状态机对非法取值返回 400 且不落库。
"""

from __future__ import annotations

BUCKETS = ("overdue", "today", "soon", "this_week", "no_due")


def _bucket_ids(dashboard: dict) -> set[int]:
    """五分区里出现过的任务 id（counts 是计数，不参与）。"""
    return {item["id"] for key in BUCKETS for item in dashboard[key]}


async def test_done_归档离开仪表盘_恢复后回到待办(client):
    created = await client.post("/api/tasks", json={"title": "写测试用例"})
    assert created.status_code == 201
    task_id = created.json()["id"]
    assert created.json()["task"]["status"] == "todo"
    assert created.json()["task"]["archived"] == 0

    # 无 DDL 的工作落在 no_due（SPEC 7.3）
    dashboard = (await client.get("/api/dashboard")).json()
    assert task_id in _bucket_ids(dashboard)

    done = await client.patch(f"/api/tasks/{task_id}", json={"status": "done"})
    assert done.status_code == 200
    payload = done.json()["task"]
    assert payload["status"] == "done"
    assert payload["archived"] == 1
    assert payload["completed_at"]

    dashboard = (await client.get("/api/dashboard")).json()
    assert task_id not in _bucket_ids(dashboard)
    active = (await client.get("/api/tasks?archived=0")).json()
    assert task_id not in [item["id"] for item in active["items"]]
    archived = (await client.get("/api/tasks?archived=1")).json()
    assert task_id in [item["id"] for item in archived["items"]]

    restored = await client.patch(f"/api/tasks/{task_id}", json={"status": "todo"})
    assert restored.status_code == 200
    payload = restored.json()["task"]
    assert payload["status"] == "todo"
    assert payload["archived"] == 0
    assert payload["completed_at"] is None

    dashboard = (await client.get("/api/dashboard")).json()
    assert task_id in _bucket_ids(dashboard)


async def test_非法状态返回400且不落库(client):
    created = await client.post("/api/tasks", json={"title": "校验状态机"})
    task_id = created.json()["id"]

    rejected = await client.patch(f"/api/tasks/{task_id}", json={"status": "unknown"})
    assert rejected.status_code == 400
    assert "状态" in rejected.json()["detail"]

    detail = (await client.get(f"/api/tasks/{task_id}")).json()
    assert detail["task"]["status"] == "todo"
    assert detail["task"]["archived"] == 0
