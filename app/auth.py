"""多用户鉴权：密码哈希、会话、当前用户、登录节流、角色判定（SPEC.md 15.4 / 15.6）。

本模块只做“身份”，不做业务归属：
  - hash_password / verify_password   密码存储格式 pbkdf2_sha256 / iters / salt_hex / hash_hex，四段用 $ 连接
  - create_session / lookup_session / destroy_session
  - current_user(request)              当前登录用户（dict 或 None）
  - is_admin / effective_owner_id      角色判断与 ?as= 解析（供任务域/报告域调用）
  - login_blocked / record_failure / clear_failures / retry_after   登录节流（落库，SPEC 17.3）

单用户兼容：users 表为空时不产生任何会话，current_user() 返回 None，
security 模块退化为改造前的 APP_PASSWORD 口令门禁。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta

from app import db, security, timeutil

logger = logging.getLogger(__name__)

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
SEPARATOR = "$"
SESSION_TTL_SECONDS = 30 * 24 * 3600
MAX_FAILURES = 5
WINDOW_SECONDS = 300
# 旧名兼容位：app/web/pages_auth.py:174 仍在读 MAX_LOGIN_FAILURES（值等同 MAX_FAILURES），
# 该文件不在 W-07 的授权范围，故只保留别名、不改调用方。
MAX_LOGIN_FAILURES = MAX_FAILURES


# ---------------------------------------------------------------- 密码
def hash_password(password: str) -> str:
    """生成四段存储串：pbkdf2_sha256 / 迭代次数 / salt_hex / hash_hex。"""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return SEPARATOR.join(
        (PBKDF2_ALGORITHM, str(PBKDF2_ITERATIONS), salt.hex(), digest.hex())
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码；存储串损坏一律按失败处理。"""
    parts = str(stored or "").split(SEPARATOR, 3)
    if len(parts) != 4:
        return False
    algorithm, iterations_text, salt_hex, hash_hex = parts
    if algorithm != PBKDF2_ALGORITHM:
        return False
    try:
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (TypeError, ValueError):
        return False
    if iterations <= 0:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(digest, expected)


# ---------------------------------------------------------------- 会话
def _expires_at(ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    deadline = timeutil.now() + timedelta(seconds=ttl_seconds)
    return deadline.replace(microsecond=0).isoformat()


def create_session(user_id: int, ua: str | None = None) -> str:
    """写入 sessions 并返回 cookie 值。"""
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO sessions(token, user_id, created_at, expires_at, ua) VALUES(?,?,?,?,?)",
        (token, int(user_id), timeutil.now_iso(), _expires_at(), (ua or "")[:200] or None),
    )
    return token


def lookup_session(token: str) -> dict | None:
    """校验会话：行存在 + 未过期 + 用户 active=1；否则 None。"""
    if not token:
        return None
    row = db.one(
        "SELECT s.token, s.user_id, s.expires_at, u.username, u.display_name, u.role, u.active "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
        (token,),
    )
    if row is None or not row["active"]:
        return None
    if str(row["expires_at"] or "") <= timeutil.now_iso():
        return None
    return {
        "id": int(row["user_id"]),
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "active": int(row["active"]),
    }


def destroy_session(token: str) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))


def destroy_user_sessions(user_id: int) -> int:
    return db.execute("DELETE FROM sessions WHERE user_id = ?", (int(user_id),))


def purge_expired_sessions() -> int:
    return db.execute("DELETE FROM sessions WHERE expires_at <= ?", (timeutil.now_iso(),))


# ---------------------------------------------------------------- 当前用户
def current_user(request) -> dict | None:
    """当前登录用户；中间件已解析时直接读 scope state，否则自行查会话。"""
    scope = getattr(request, "scope", None) or {}
    state = scope.get("state") or {}
    if "user" in state:
        return state["user"]
    headers = getattr(request, "headers", None) or {}
    return lookup_session(security.read_session_cookie(headers.get("cookie")))


def is_admin(user) -> bool:
    return bool(user) and str(user.get("role") or "") == "admin"


def source_of(request) -> str:
    """登录节流的来源标识：客户端地址（不信任 X-Forwarded-For，避免绕过）。"""
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "unknown")


def effective_owner_id(request, user=None) -> int | None:
    """当前生效的数据归属 id（供任务/报告/提醒域过滤）。

    开放/单口令模式用户为 None -> 返回 None（不做归属过滤，保持旧行为）；
    管理员可用 ?as=<user_id> 切换，非法 id 回落到自己。
    """
    user = user if user is not None else current_user(request)
    if not user:
        return None
    own_id = int(user["id"])
    if not is_admin(user):
        return own_id
    params = getattr(request, "query_params", None)
    raw = params.get("as") if params is not None else None
    if not raw:
        return own_id
    try:
        candidate = int(raw)
    except (TypeError, ValueError):
        return own_id
    row = db.one("SELECT id FROM users WHERE id = ?", (candidate,))
    return int(row["id"]) if row is not None else own_id


def request_context(request) -> dict:
    """页面/API 通用身份上下文，避免各域重复解析 ?as=。"""
    user = current_user(request)
    return {
        "user": user,
        "is_admin": is_admin(user),
        "owner_id": effective_owner_id(request, user),
        "mode": security.auth_mode(),
    }


# ---------------------------------------------------------------- 登录节流
# 状态全部落在 login_attempts（SPEC 17.3）：重启不清零、多 worker 一致、内存零占用。
def _cutoff() -> str:
    """窗口起点：now - 300s，去微秒后 isoformat（与 now_iso() 同格式，可直接字符串比较）。"""
    return (timeutil.now() - timedelta(seconds=WINDOW_SECONDS)).replace(
        microsecond=0
    ).isoformat()


def _parse_at(value) -> datetime | None:
    """宽松解析入库时间戳；损坏行按「无记录」处理。"""
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _key(username: str, source: str) -> tuple[str, str]:
    return ((username or "").strip().lower(), source or "")


def login_blocked(username: str, source: str) -> bool:
    """同一用户名+来源 5 分钟内失败达到 5 次 -> True（应返回 429）。"""
    row = db.one(
        "SELECT COUNT(*) AS c FROM login_attempts "
        "WHERE username = ? AND source = ? AND at > ?",
        (_key(username, source)[0], source, _cutoff()),
    )
    return int(row["c"]) >= MAX_FAILURES


def record_failure(username: str, source: str) -> int:
    """写入一条失败记录，返回本次记录后该 (username, source) 在 300 秒窗口内的失败次数。

    插入、清理窗口外旧行、取计数在同一个事务里完成，并复用同一个 cutoff，
    避免「插入了但计数还没算上」的不一致；调用方（web/pages_auth.py）用它判断当次是否已达上限。
    """
    key_username = _key(username, source)[0]
    cutoff = _cutoff()
    with db.cursor() as conn:
        conn.execute(
            "INSERT INTO login_attempts(username, source, at) VALUES(?,?,?)",
            (key_username, source, timeutil.now_iso()),
        )
        conn.execute("DELETE FROM login_attempts WHERE at <= ?", (cutoff,))
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM login_attempts "
            "WHERE username = ? AND source = ? AND at > ?",
            (key_username, source, cutoff),
        ).fetchone()
    return int(row["c"])


def clear_failures(username: str, source: str) -> None:
    db.execute(
        "DELETE FROM login_attempts WHERE username = ? AND source = ?",
        (_key(username, source)[0], source),
    )


def retry_after(username: str, source: str) -> int:
    """距离窗口内最早一次失败过期还有多少秒（无失败为 0）。"""
    row = db.one(
        "SELECT at FROM login_attempts "
        "WHERE username = ? AND source = ? AND at > ? ORDER BY at LIMIT 1",
        (_key(username, source)[0], source, _cutoff()),
    )
    if row is None:
        return 0
    oldest = _parse_at(row["at"])
    if oldest is None:
        return 0
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timeutil.tz())
    remaining = WINDOW_SECONDS - (timeutil.now() - oldest).total_seconds()
    return max(1, min(WINDOW_SECONDS, int(remaining)))


def reset_throttle() -> None:
    """测试用：清空全部节流计数（落库后等价于清空 login_attempts）。"""
    db.execute("DELETE FROM login_attempts")
