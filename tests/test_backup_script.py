"""W-12 备份/恢复脚本的自动化测试（脚本本体：scripts/backup.sh、scripts/restore.sh）。

Windows 开发机没有 sqlite3 命令行也能跑：脚本会回退到 PYTHON 指定的解释器（这里固定指向当前
解释器）。环境里既没有 bash 也没有 sqlite 后端时整组跳过，不污染既有用例的绿。
"""

from __future__ import annotations

import http.server
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKUP_SH = ROOT / "scripts" / "backup.sh"
RESTORE_SH = ROOT / "scripts" / "restore.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None or not BACKUP_SH.is_file() or not RESTORE_SH.is_file(),
    reason="需要 bash 与 scripts/backup.sh、scripts/restore.sh",
)

# verify 抽查的四个关键表；测试用最小 DDL，只关心行数
SCHEMA = (
    "CREATE TABLE tasks(id INTEGER PRIMARY KEY)",
    "CREATE TABLE users(id INTEGER PRIMARY KEY)",
    "CREATE TABLE reports(id INTEGER PRIMARY KEY)",
    "CREATE TABLE routines(id INTEGER PRIMARY KEY)",
)


def make_db(path: Path, tasks: int = 3, users: int = 2, reports: int = 1, routines: int = 0,
            tables: tuple[str, ...] = ("tasks", "users", "reports", "routines")) -> Path:
    con = sqlite3.connect(path)
    for ddl in SCHEMA:
        name = ddl.split()[2].split("(")[0]
        if name in tables:
            con.execute(ddl)
            con.executemany(f"INSERT INTO {name}(id) VALUES (?)", [(i,) for i in range(1, {
                "tasks": tasks, "users": users, "reports": reports, "routines": routines,
            }[name] + 1)])
    con.commit()
    con.close()
    return path


def counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("tasks", "users", "reports", "routines")
        }
    finally:
        con.close()


def script_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """脚本的环境：DATA_DIR/COMPOSE_DIR/BACKUP_DIR 全部指到 tmp_path，PYTHON 指当前解释器。"""
    env = dict(os.environ)
    env.update({
        "DATA_DIR": str(tmp_path / "data"),
        "COMPOSE_DIR": str(tmp_path / "compose"),
        "BACKUP_DIR": str(tmp_path / "backup"),
        "PYTHON": sys.executable,
        "OWNER": "nobody",
        "HEALTH_TIMEOUT": "20",
        "HEALTH_INTERVAL": "1",
        "PORT": "1",
    })
    env.update(extra)
    return env


def run(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=180,
    )


@pytest.fixture
def compose_stub(tmp_path: Path) -> tuple[str, Path]:
    """替身 docker compose：只把被调用的参数写进日志（不碰任何容器）。"""
    log = tmp_path / "compose-calls.log"
    stub = tmp_path / "stub-compose.sh"
    stub.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log.as_posix()}"\n', encoding="utf-8")
    return f"bash {stub}", log


@pytest.fixture
def healthz_server():
    """本地 /healthz 200 端点（restore 的启动后就绪检查）。"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server 约定
            body = b'{"ok":true}'
            self.send_response(200 if self.path == "/healthz" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _prep_data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (tmp_path / "compose").mkdir(parents=True, exist_ok=True)
    return data


# ---------------- backup.sh verify ----------------
def test_verify_ok(tmp_path: Path) -> None:
    db = make_db(_prep_data_dir(tmp_path) / "app.db")
    res = run(BACKUP_SH, "verify", str(db), env=script_env(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "校验通过" in res.stdout
    assert "integrity_check" in res.stdout
    for table, n in (("tasks", 3), ("users", 2), ("reports", 1), ("routines", 0)):
        assert f"{table}" in res.stdout
        assert f"{n} 行" in res.stdout


def test_verify_rejects_missing_file(tmp_path: Path) -> None:
    res = run(BACKUP_SH, "verify", str(tmp_path / "nope.db"), env=script_env(tmp_path))
    assert res.returncode != 0
    assert "FAIL" in res.stdout
    assert "不存在" in res.stdout + res.stderr


def test_verify_rejects_truncated_file(tmp_path: Path) -> None:
    db = make_db(_prep_data_dir(tmp_path) / "app.db", tasks=50)
    truncated = tmp_path / "half.db"
    raw = db.read_bytes()
    truncated.write_bytes(raw[: len(raw) // 2])
    res = run(BACKUP_SH, "verify", str(truncated), env=script_env(tmp_path))
    assert res.returncode != 0
    assert "FAIL" in res.stdout


def test_verify_rejects_missing_table(tmp_path: Path) -> None:
    db = make_db(_prep_data_dir(tmp_path) / "app.db", tables=("tasks", "reports", "routines"))
    res = run(BACKUP_SH, "verify", str(db), env=script_env(tmp_path))
    assert res.returncode != 0
    assert "缺失" in res.stdout


# ---------------- backup.sh 备份 ----------------
def test_backup_creates_verified_snapshot(tmp_path: Path) -> None:
    db = make_db(_prep_data_dir(tmp_path) / "app.db")
    env = script_env(tmp_path, KEEP="3")
    res = run(BACKUP_SH, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    snaps = sorted((tmp_path / "backup").glob("app-*.db"))
    assert len(snaps) == 1
    assert counts(snaps[0]) == counts(db)
    assert run(BACKUP_SH, "verify", str(snaps[0]), env=env).returncode == 0


# ---------------- restore.sh ----------------
def test_restore_dry_run_changes_nothing(tmp_path: Path, compose_stub) -> None:
    db = make_db(_prep_data_dir(tmp_path) / "app.db")
    before = db.read_bytes()
    cmd, log = compose_stub
    env = script_env(tmp_path, COMPOSE_CMD=cmd)
    res = run(RESTORE_SH, str(db), env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "DRY-RUN" in res.stdout
    assert db.read_bytes() == before
    assert not list((tmp_path / "backup").glob("app-before-restore-*.db"))
    assert not log.exists(), "dry-run 不应调用任何容器命令"


def test_restore_yes_refuses_unverified_backup(tmp_path: Path, compose_stub) -> None:
    db = make_db(_prep_data_dir(tmp_path) / "app.db", tasks=50)
    truncated = tmp_path / "half.db"
    raw = db.read_bytes()
    truncated.write_bytes(raw[: len(raw) // 2])
    before = db.read_bytes()
    cmd, log = compose_stub
    res = run(RESTORE_SH, str(truncated), "--yes", env=script_env(tmp_path, COMPOSE_CMD=cmd))
    assert res.returncode != 0
    assert "拒绝恢复" in res.stderr
    assert not log.exists(), "校验未通过时不得碰容器"
    assert db.read_bytes() == before


def test_restore_yes_end_to_end(tmp_path: Path, compose_stub, healthz_server) -> None:
    data = _prep_data_dir(tmp_path)
    live = make_db(data / "app.db")                       # 备份时：3 任务 / 2 用户 / 1 报告
    backup = tmp_path / "backup" / "app-known.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(live, backup)

    con = sqlite3.connect(live)                            # 故意制造差异：+5 任务、删报告
    con.executemany("INSERT INTO tasks(id) VALUES (?)", [(100 + i,) for i in range(5)])
    con.execute("DELETE FROM reports")
    con.commit()
    con.close()
    assert counts(live) == {"tasks": 8, "users": 2, "reports": 0, "routines": 0}

    cmd, log = compose_stub
    env = script_env(tmp_path, COMPOSE_CMD=cmd, PORT=str(healthz_server))
    res = run(RESTORE_SH, str(backup), "--yes", env=env)
    assert res.returncode == 0, res.stdout + res.stderr

    assert counts(live) == {"tasks": 3, "users": 2, "reports": 1, "routines": 0}
    assert live.read_bytes() == backup.read_bytes()
    assert "恢复完成" in res.stdout and "healthz 200" in res.stdout

    calls = log.read_text(encoding="utf-8").split()
    assert "stop" in calls and "up" in calls and "-d" in calls
    assert calls.index("stop") < calls.index("up"), "必须先停容器再起容器"

    snapshots = list((tmp_path / "backup").glob("app-before-restore-*.db"))
    assert len(snapshots) == 1
    assert counts(snapshots[0]) == {"tasks": 8, "users": 2, "reports": 0, "routines": 0}
