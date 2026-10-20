"""多用户鉴权：密码哈希、会话、当前用户、登录节流、角色判定（SPEC.md 15.4 / 15.6）。

本模块只做“身份”，不做业务归属：
  - hash_password / verify_password   密码存储格式 pbkdf2_sha256 / iters / salt_hex / hash_hex，四段用 $ 连接
  - create_session / lookup_session / destroy_session
  - current_user(request)              当前登录用户（dict 或 None）
  - is_admin / effective_owner_id      角色判断与 ?as= 解析（供任务域/报告域调用）
  - login_blocked / record_failure / clear_failures / retry_after   进程内节流

单用户兼容：users 表为空时不产生任何会话，current_user() 返回 None，
security 模块退化为改造前的 APP_PASSWORD 口令门禁。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from datetime import timedelta

from app import db, security, timeutil

logger = logging.getLogger(__name__)

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
SEPARATOR = "$"
SESSION_TTL_SECONDS = 30 * 24 * 3600
MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300


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
_failures: dict[tuple[str, str], list[float]] = {}
_failures_lock = threading.Lock()


def _prune(bucket: list[float], now: float) -> list[float]:
    return [stamp for stamp in bucket if now - stamp < LOGIN_WINDOW_SECONDS]


def _key(username: str, source: str) -> tuple[str, str]:
    return ((username or "").strip().lower(), source or "")


def login_blocked(username: str, source: str) -> bool:
    """同一用户名+来源 5 分钟内失败达到 5 次 -> True（应返回 429）。"""
    key = _key(username, source)
    now = time.time()
    with _failures_lock:
        bucket = _prune(_failures.get(key, []), now)
        if bucket:
            _failures[key] = bucket
        else:
            _failures.pop(key, None)
        return len(bucket) >= MAX_LOGIN_FAILURES


def record_failure(username: str, source: str) -> int:
    key = _key(username, source)
    now = time.time()
    with _failures_lock:
        bucket = _prune(_failures.get(key, []), now)
        bucket.append(now)
        _failures[key] = bucket
        return len(bucket)


def clear_failures(username: str, source: str) -> None:
    with _failures_lock:
        _failures.pop(_key(username, source), None)


def retry_after(username: str, source: str) -> int:
    """距离窗口内最早一次失败过期还有多少秒（无失败为 0）。"""
    key = _key(username, source)
    now = time.time()
    with _failures_lock:
        bucket = _prune(_failures.get(key, []), now)
        if not bucket:
            return 0
        oldest = min(bucket)
    return max(1, int(LOGIN_WINDOW_SECONDS - (now - oldest)))


def reset_throttle() -> None:
    """测试用：清空全部节流计数。"""
    with _failures_lock:
        _failures.clear()
