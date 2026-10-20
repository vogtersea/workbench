"""鉴权门禁（SPEC.md 第 7 节第 10 条 + 15.1 / 15.4）。

三种模式由 users 表与 APP_PASSWORD 共同决定：
  multi   有用户 -> cookie wb_session 必须是 sessions 表里未过期且 active=1 的会话
  legacy  无用户且 APP_PASSWORD 非空 -> 沿用改造前口令模式，
          cookie 值 = hex(hmac_sha256(APP_SECRET, APP_PASSWORD))
  open    无用户且 APP_PASSWORD 为空 -> 完全不鉴权（内网自用，保持旧行为）

白名单：/healthz、/static/*、/favicon.ico、/login、/logout、/setup、/register。
本模块只管"能不能进门"，用户是谁、数据归谁见 app/auth.py。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from http.cookies import SimpleCookie
from urllib.parse import quote

from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app import db
from app.config import settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "wb_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30
PUBLIC_PATHS = frozenset(
    {"/healthz", "/login", "/logout", "/favicon.ico", "/setup", "/register"}
)
PUBLIC_PREFIXES = ("/static/",)
NO_STATE_PREFIXES = ("/static/",)

MODE_MULTI = "multi"
MODE_LEGACY = "legacy"
MODE_OPEN = "open"


def users_exist() -> bool:
    """users 表是否有账号；表不存在（首启前）也按无用户处理。"""
    try:
        return db.one("SELECT 1 FROM users LIMIT 1") is not None
    except Exception:  # 库尚未初始化
        logger.debug("users 表不可读，按无用户处理", exc_info=True)
        return False


def auth_mode() -> str:
    if users_exist():
        return MODE_MULTI
    return MODE_LEGACY if settings.app_password else MODE_OPEN


def auth_enabled() -> bool:
    """是否启用任何形式的门禁（multi 或 legacy）。"""
    return auth_mode() != MODE_OPEN


def session_token() -> str:
    """单口令模式的会话 cookie 值，用 APP_SECRET 对 APP_PASSWORD 做 HMAC-SHA256。"""
    digest = hmac.new(
        settings.app_secret.encode("utf-8"),
        settings.app_password.encode("utf-8"),
        hashlib.sha256,
    )
    return digest.hexdigest()


def read_session_cookie(header: str | None) -> str:
    if not header:
        return ""
    jar = SimpleCookie()
    try:
        jar.load(header)
    except Exception:
        return ""
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel is not None else ""


def is_authenticated(cookie_header: str | None) -> bool:
    """兼容旧调用：多用户模式看会话表，单口令模式看 HMAC，开放模式恒真。"""
    mode = auth_mode()
    if mode == MODE_OPEN:
        return True
    given = read_session_cookie(cookie_header)
    if not given:
        return False
    if mode == MODE_LEGACY:
        return hmac.compare_digest(given, session_token())
    from app import auth  # 延迟导入，避免 security <-> auth 模块环

    return auth.lookup_session(given) is not None


def set_session_cookie(response, token: str | None = None) -> None:
    """token 为空时按单口令模式写入（保持改造前的 cookie 值）。"""
    response.set_cookie(
        COOKIE_NAME,
        token if token else session_token(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        # HTTPS 反向代理部署时由 SECURE_COOKIE=1 打开；直连 HTTP 置 1 会登录后立即掉线（预期）
        secure=settings.secure_cookie,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _cookie_header(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"cookie":
            return value.decode("latin-1")
    return None


def _login_redirect(scope: Scope) -> RedirectResponse:
    target = scope.get("path") or "/"
    query = (scope.get("query_string") or b"").decode("latin-1")
    if query:
        target = f"{target}?{query}"
    return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)


class AuthMiddleware:
    """纯 ASGI 中间件：解析身份写入 scope state，再决定放行还是跳登录页。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or "/"
        if path.startswith(NO_STATE_PREFIXES):
            await self.app(scope, receive, send)
            return

        from app import auth  # 延迟导入，避免模块环

        token = read_session_cookie(_cookie_header(scope))
        user = auth.lookup_session(token) if token else None
        mode = auth_mode()
        state = scope.setdefault("state", {})
        state["user"] = user
        state["auth_mode"] = mode

        if user is not None or is_public_path(path) or mode == MODE_OPEN:
            await self.app(scope, receive, send)
            return
        if mode == MODE_LEGACY and token and hmac.compare_digest(token, session_token()):
            await self.app(scope, receive, send)
            return
        await _login_redirect(scope)(scope, receive, send)
