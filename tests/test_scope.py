"""跨用户数据隔离（W-03，SPEC 15.1 / 15.7）。

覆盖：A 登录建任务，B 登录后访问同一任务一律 404（不是 403，不泄露存在性）；
B 的工作列表里也看不到 A 的数据，越权写不落库。
"""

from __future__ import annotations

from app import db
from app.services import users

ALICE = ("alice", "alice-pass-1")
BOB = ("bob", "bob-pass-1")


async def _login(client, username: str, password: str) -> None:
    """走真实登录表单拿 wb_session cookie；多用户模式下这是唯一入口。"""
    response = await client.post(
        "/login", data={"username": username, "password": password, "next": "/"}
    )
    assert response.status_code == 303, response.text
    assert "wb_session" in client.cookies


async def test_他人任务一律404(client):
    alice_id = users.create_user(*ALICE)
    users.create_user(*BOB)

    await _login(client, *ALICE)
    created = await client.post("/api/tasks", json={"title": "A 的私有任务"})
    assert created.status_code == 201
    task_id = created.json()["id"]
    assert (await client.get(f"/api/tasks/{task_id}")).status_code == 200

    owner = db.one("SELECT owner_id FROM tasks WHERE id = ?", (task_id,))
    assert owner["owner_id"] == alice_id

    await _login(client, *BOB)

    listed = (await client.get("/api/tasks")).json()
    assert task_id not in [item["id"] for item in listed["items"]]

    assert (await client.get(f"/api/tasks/{task_id}")).status_code == 404
    assert (
        await client.patch(f"/api/tasks/{task_id}", json={"status": "done"})
    ).status_code == 404
    assert (
        await client.post(f"/api/tasks/{task_id}/progress", json={"content": "越权写入"})
    ).status_code == 404

    # 越权写全部落空：任务仍是 A 的、状态未变
    row = db.one("SELECT owner_id, status FROM tasks WHERE id = ?", (task_id,))
    assert row["owner_id"] == alice_id
    assert row["status"] == "todo"
