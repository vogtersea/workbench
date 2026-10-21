"""桌面桥接后端：状态、偏好、数据目录切换（含迁移校验与拒绝路径）。"""

import sqlite3

import pytest

from desktop import prefs as prefs_mod
from desktop.bridge_impl import DesktopBackend


class FakeHandle:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.base_url = "http://127.0.0.1:12345"
        self.port = 12345
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把偏好文件指到临时目录，避免污染真实 %APPDATA%。"""
    prefs_file = tmp_path / "prefs.json"
    monkeypatch.setattr(prefs_mod, "prefs_path", lambda: prefs_file)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    handle = FakeHandle(data_dir)
    prefs = prefs_mod.Prefs(data_dir=str(data_dir), first_run_done=True)
    return DesktopBackend(prefs, handle), data_dir, tmp_path


def test_state_字段齐全(env):
    backend, data_dir, _ = env
    state = backend.state()
    assert state["ok"] is True
    assert state["data_dir"] == str(data_dir)
    assert state["port"] == 12345
    assert {"close_to_tray", "autostart", "notify", "version", "launch_mode"} <= set(state)


def test_更新偏好会落盘(env):
    backend, _, tmp_path = env
    result = backend.update_prefs(close_to_tray=False, notify=False)
    assert result["ok"] is True
    saved = prefs_mod.load(tmp_path / "prefs.json")
    assert saved.close_to_tray is False
    assert saved.notify is False


def test_开机自启写入失败时整体失败(env, monkeypatch):
    backend, _, _ = env
    monkeypatch.setattr("desktop.autostart.set_enabled", lambda on: False)
    result = backend.update_prefs(autostart=True)
    assert result["ok"] is False
    assert "开机启动" in result["error"]


def test_迁移数据目录会复制并校验(env):
    backend, data_dir, tmp_path = env
    db = data_dir / "app.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES(1)")
    conn.commit()
    conn.close()

    target = tmp_path / "newdata"
    result = backend.change_data_dir(str(target), "migrate")
    assert result["ok"] is True
    assert result["restart_required"] is True
    assert (target / "app.db").exists()
    conn = sqlite3.connect(str(target / "app.db"))
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    conn.close()
    assert (data_dir / "app.db").exists()          # 旧目录保留
    assert prefs_mod.load(tmp_path / "prefs.json").data_dir == str(target)


def test_空库模式不复制任何数据(env):
    backend, data_dir, tmp_path = env
    conn = sqlite3.connect(str(data_dir / "app.db"))
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()
    target = tmp_path / "fresh"
    result = backend.change_data_dir(str(target), "empty")
    assert result["ok"] is True
    assert not (target / "app.db").exists()


def test_同一目录被拒绝(env):
    backend, data_dir, _ = env
    result = backend.change_data_dir(str(data_dir), "migrate")
    assert result["ok"] is False
    assert "相同" in result["error"]


def test_迁移但源库不存在时报错(env):
    backend, _, tmp_path = env
    result = backend.change_data_dir(str(tmp_path / "x"), "migrate")
    assert result["ok"] is False
    assert "app.db" in result["error"]
