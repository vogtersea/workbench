"""登录节流落库（W-07 / SPEC 17.3）：窗口计数、过期清理、来源与用户名隔离、重试等待。

覆盖：5 次失败后阻塞 / 清除后不阻塞 / 同名不同来源隔离 / 不同用户名隔离 /
窗口外记录不计入 / retry_after 取值 / 记录失败时顺带清过期行 / 用户名归一化。

隔离说明：conftest 的 clean_db 不认识 login_attempts（它只清业务表），所以这里自带
autouse 夹具清表；库仍是 conftest 那份临时 DATA_DIR，不碰 data/。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import auth, db, timeutil

USER = "alice"
SRC = "10.0.0.1"


@pytest.fixture(autouse=True)
def _clean_attempts(app):
    """依赖 session 级 app 夹具建好库；用例前后清空 login_attempts，保证互相隔离。"""
    db.execute("DELETE FROM login_attempts")
    yield
    db.execute("DELETE FROM login_attempts")


def _count() -> int:
    return int(db.one("SELECT COUNT(*) AS c FROM login_attempts")["c"])


def _stamp(seconds_ago: int) -> str:
    """窗口内 / 外的 at 值，精度与 timeutil.now_iso() 一致（去微秒）。"""
    return (timeutil.now() - timedelta(seconds=seconds_ago)).replace(
        microsecond=0
    ).isoformat()


def test_连续五次失败后阻塞():
    for _ in range(auth.MAX_FAILURES - 1):
        auth.record_failure(USER, SRC)
    assert auth.login_blocked(USER, SRC) is False  # 第 5 次之前不阻塞
    auth.record_failure(USER, SRC)
    assert auth.login_blocked(USER, SRC) is True  # 第 6 次：达到 5 次失败即阻塞


def test_记录失败返回窗口内计数():
    """record_failure 的返回值是「本次记录后窗口内失败次数」，调用方据此当次判 429。"""
    counts = [auth.record_failure(USER, SRC) for _ in range(auth.MAX_FAILURES)]
    assert counts == [1, 2, 3, 4, 5]
    assert counts[-1] >= auth.MAX_FAILURES  # 第 5 次当次即达上限
    assert auth.login_blocked(USER, SRC) is True


def test_四次失败后清除即不再阻塞():
    for _ in range(4):
        auth.record_failure(USER, SRC)
    assert _count() == 4
    auth.clear_failures(USER, SRC)
    assert _count() == 0
    assert auth.login_blocked(USER, SRC) is False
    assert auth.retry_after(USER, SRC) == 0


def test_同一用户名不同来源互不影响():
    for _ in range(auth.MAX_FAILURES):
        auth.record_failure(USER, SRC)
    assert auth.login_blocked(USER, SRC) is True
    assert auth.login_blocked(USER, "10.0.0.2") is False


def test_不同用户名互不影响():
    for _ in range(auth.MAX_FAILURES):
        auth.record_failure(USER, SRC)
    assert auth.login_blocked(USER, SRC) is True
    assert auth.login_blocked("bob", SRC) is False


def test_窗口外记录不计入():
    for _ in range(auth.MAX_FAILURES):
        db.execute(
            "INSERT INTO login_attempts(username, source, at) VALUES(?,?,?)",
            (USER, SRC, _stamp(auth.WINDOW_SECONDS + 60)),
        )
    assert _count() == auth.MAX_FAILURES
    assert auth.login_blocked(USER, SRC) is False
    assert auth.retry_after(USER, SRC) == 0


def test_重试等待秒数():
    assert auth.retry_after(USER, SRC) == 0  # 无记录
    auth.record_failure(USER, SRC)
    wait = auth.retry_after(USER, SRC)
    assert 1 <= wait <= auth.WINDOW_SECONDS


def test_记录失败时顺带清理过期行():
    db.execute(
        "INSERT INTO login_attempts(username, source, at) VALUES(?,?,?)",
        (USER, SRC, _stamp(auth.WINDOW_SECONDS + 60)),
    )
    assert _count() == 1
    auth.record_failure(USER, SRC)
    assert _count() == 1  # 过期行被删掉，只剩刚写的一条
    assert auth.login_blocked(USER, SRC) is False


def test_用户名大小写与空白归一():
    for _ in range(auth.MAX_FAILURES):
        auth.record_failure("  Alice  ", SRC)
    assert auth.login_blocked("alice", SRC) is True
    assert db.one("SELECT username FROM login_attempts LIMIT 1")["username"] == "alice"
