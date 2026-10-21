"""桌面专属接口：未注入后端时 404，注入后可用（服务器版行为不受影响）。"""

import pytest

from app.services import desktop_bridge


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.prefs = {
            "version": "1.0.0",
            "data_dir": "D:/Data",
            "port": 1234,
            "close_to_tray": True,
            "autostart": False,
            "notify": True,
            "launch_mode": "开发态",
        }

    def state(self):
        return dict(self.prefs, ok=True)

    def update_prefs(self, **changes):
        self.calls.append(("prefs", changes))
        self.prefs.update(changes)
        return {"ok": True, "state": self.state()}

    def change_data_dir(self, path, mode):
        self.calls.append(("data_dir", path, mode))
        return {"ok": True, "restart_required": True, "data_dir": path}

    def request_restart(self):
        self.calls.append(("restart",))


@pytest.fixture()
def backend():
    desktop_bridge.set_backend(FakeBackend())
    yield desktop_bridge.get_backend()
    desktop_bridge.set_backend(None)


def test_无桌面后端时接口一律404(app):
    from fastapi.testclient import TestClient

    desktop_bridge.set_backend(None)
    with TestClient(app) as http:
        assert http.get("/api/desktop/state").status_code == 404
        assert http.post("/api/desktop/prefs", json={"notify": True}).status_code == 404
        assert http.post("/api/desktop/data-dir", json={"path": "x"}).status_code == 404
        assert http.post("/settings/desktop", data={}).status_code == 404


def test_注入后端后状态与偏好可用(app, backend):
    from fastapi.testclient import TestClient

    with TestClient(app) as http:
        state = http.get("/api/desktop/state")
        assert state.status_code == 200
        assert state.json()["data_dir"] == "D:/Data"

        resp = http.post("/api/desktop/prefs", json={"notify": False, "close_to_tray": False})
        assert resp.status_code == 200
        assert resp.json()["state"]["notify"] is False

        assert ("prefs", {"notify": False, "close_to_tray": False}) in backend.calls


def test_数据目录接口透传模式(app, backend):
    from fastapi.testclient import TestClient

    with TestClient(app) as http:
        resp = http.post("/api/desktop/data-dir", json={"path": "E:/新目录", "mode": "empty"})
        assert resp.status_code == 200
        assert resp.json()["restart_required"] is True
        assert backend.calls[-1] == ("data_dir", "E:/新目录", "empty")


def test_设置页表单保存后跳转(app, backend):
    from fastapi.testclient import TestClient

    with TestClient(app, follow_redirects=False) as http:
        resp = http.post("/settings/desktop", data={"close_to_tray": "1", "notify": "1"})
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/settings")
        assert backend.prefs["close_to_tray"] is True


def test_设置页出现桌面卡片(app, backend):
    from fastapi.testclient import TestClient

    with TestClient(app) as http:
        html = http.get("/settings").text
    assert "桌面应用" in html
    assert "desktop-data-dir" in html
    assert "关闭按钮时最小化到托盘" in html
