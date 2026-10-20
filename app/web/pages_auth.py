"""身份页面路由（SPEC.md 15.6 页面部分）。

    GET/POST /login                 多用户：用户名+密码；单口令模式：沿用旧表单
    POST     /logout                删会话 + 清 cookie
    GET/POST /setup                 仅当 users 为空时可用，创建首个管理员
    GET/POST /register?code=        邀请码注册，成功后自动登录
    GET      /admin/users           用户列表（管理员）
    POST     /admin/users           创建用户
    POST     /admin/users/{id}/password | /toggle | /delete
    GET      /admin/invites         邀请码列表 + 新建表单
    POST     /admin/invites         新建邀请码
    POST     /admin/invites/{id}/revoke  作废

本模块只暴露 pages = APIRouter()；/healthz 仍由 main.py 独占。
"""

from __future__ import annotations

import hmac
import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import auth, security, templating
from app.config import settings
from app.services import users as users_service
from app.services.users import UserError

logger = logging.getLogger(__name__)

pages = APIRouter()

MESSAGE_PAGE = (
    '{% extends "base.html" %}'
    "{% block title %}提示{% endblock %}"
    "{% block content %}{% include 'partials/auth_message.html' %}{% endblock %}"
)


# ---------------------------------------------------------------- 公共工具
def safe_next(target: str | None) -> str:
    """只允许站内相对路径，避免开放重定向。"""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def _redirect(url: str, *, msg: str = "", error: str = "") -> RedirectResponse:
    params = []
    if msg:
        params.append("msg=" + quote(msg, safe=""))
    if error:
        params.append("error=" + quote(error, safe=""))
    if params:
        url = f"{url}{'&' if '?' in url else '?'}" + "&".join(params)
    return RedirectResponse(url, status_code=303)


def _flash(request: Request) -> dict:
    return {
        "msg": (request.query_params.get("msg") or "").strip(),
        "error": (request.query_params.get("error") or "").strip(),
    }


def _message(
    request: Request,
    *,
    title: str,
    detail: str,
    status_code: int,
    cta_label: str = "返回首页",
    cta_href: str = "/",
) -> HTMLResponse:
    """用 partials/auth_message.html + base.html 渲染提示页（不新增整页模板）。"""
    template = templating.templates.env.from_string(MESSAGE_PAGE)
    context = templating.status_context()
    context.update(
        {
            "request": request,
            "title": title,
            "detail": detail,
            "cta_label": cta_label,
            "cta_href": cta_href,
        }
    )
    return HTMLResponse(template.render(**context), status_code=status_code)


def _require_admin(request: Request):
    """返回 (user, None) 或 (None, 403 页面)。开放/单口令模式的本地用户视同管理员。"""
    user = auth.current_user(request)
    if user is None or auth.is_admin(user):
        return user, None
    return None, _message(
        request,
        title="无权访问",
        detail="用户管理仅限管理员使用。",
        status_code=403,
        cta_label="返回仪表盘",
        cta_href="/",
    )


# ---------------------------------------------------------------- 登录
@pages.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next_url: str = Query("/", alias="next")):
    mode = security.auth_mode()
    if mode == security.MODE_OPEN:
        return RedirectResponse("/", status_code=303)
    user = auth.current_user(request)
    if user is not None:
        return RedirectResponse(safe_next(next_url), status_code=303)
    if mode == security.MODE_LEGACY and security.is_authenticated(
        request.headers.get("cookie")
    ):
        return RedirectResponse(safe_next(next_url), status_code=303)
    return templating.render(
        request, "login.html", next_url=safe_next(next_url), error="", mode=mode
    )


@pages.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next_url: str = Form("/", alias="next"),
):
    mode = security.auth_mode()
    target = safe_next(next_url)

    if mode == security.MODE_OPEN:
        return RedirectResponse("/", status_code=303)

    if mode == security.MODE_LEGACY:
        if hmac.compare_digest(password.strip(), settings.app_password):
            response = RedirectResponse(target, status_code=303)
            security.set_session_cookie(response)
            return response
        return templating.render(
            request,
            "login.html",
            next_url=target,
            error="口令不正确，请重试",
            mode=mode,
            status_code=401,
        )

    username = username.strip()
    source = auth.source_of(request)
    if auth.login_blocked(username, source):
        wait = auth.retry_after(username, source)
        return templating.render(
            request,
            "login.html",
            next_url=target,
            username=username,
            error=f"失败次数过多，请 {wait} 秒后再试",
            mode=mode,
            status_code=429,
        )

    account = users_service.get_by_username(username)
    ok = bool(
        account
        and account["active"]
        and auth.verify_password(password, account["password_hash"])
    )
    if not ok:
        attempts = auth.record_failure(username, source)
        blocked = attempts >= auth.MAX_LOGIN_FAILURES
        message = f"用户名或密码不正确（第 {attempts} 次）"
        if blocked:
            message = f"失败次数过多，请 {auth.retry_after(username, source)} 秒后再试"
        logger.info("登录失败 username=%s source=%s attempts=%s", username, source, attempts)
        return templating.render(
            request,
            "login.html",
            next_url=target,
            username=username,
            error=message,
            mode=mode,
            status_code=429 if blocked else 401,
        )

    auth.clear_failures(username, source)
    token = auth.create_session(account["id"], ua=request.headers.get("user-agent", ""))
    users_service.touch_login(account["id"])
    response = RedirectResponse(target, status_code=303)
    security.set_session_cookie(response, token)
    logger.info("登录成功 username=%s source=%s", username, source)
    return response


@pages.post("/logout")
def logout(request: Request):
    token = security.read_session_cookie(request.headers.get("cookie"))
    auth.destroy_session(token)
    response = RedirectResponse(
        "/login" if security.auth_mode() != security.MODE_OPEN else "/", status_code=303
    )
    security.clear_session_cookie(response)
    return response


# ---------------------------------------------------------------- 引导
@pages.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if security.users_exist():
        return RedirectResponse("/", status_code=303)
    return templating.render(request, "setup.html", error="", username="", display_name="")


@pages.post("/setup", response_class=HTMLResponse)
def setup_submit(
    request: Request,
    username: str = Form(""),
    display_name: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
):
    if security.users_exist():
        return RedirectResponse("/", status_code=303)
    try:
        if password != password2:
            raise UserError("两次输入的密码不一致")
        user_id = users_service.setup_first_admin(username, password, display_name)
    except UserError as exc:
        return templating.render(
            request,
            "setup.html",
            error=str(exc),
            username=username,
            display_name=display_name,
            status_code=400,
        )
    token = auth.create_session(user_id, ua=request.headers.get("user-agent", ""))
    users_service.touch_login(user_id)
    response = RedirectResponse("/", status_code=303)
    security.set_session_cookie(response, token)
    logger.info("引导完成：首个管理员 %s(id=%s)", username, user_id)
    return response


# ---------------------------------------------------------------- 邀请码注册
@pages.get("/register", response_class=HTMLResponse)
def register_page(request: Request, code: str = Query("")):
    if not security.users_exist():
        return RedirectResponse("/setup", status_code=303)
    code = code.strip()
    invite = users_service.get_invite(code)
    state = users_service.invite_state(invite)
    error = "" if state == "ok" else users_service.invite_error(state)
    return templating.render(
        request,
        "register.html",
        code=code,
        error=error,
        username="",
        display_name="",
        status_code=400 if error else 200,
    )


@pages.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    code: str = Form(""),
    username: str = Form(""),
    display_name: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
):
    code = code.strip()
    try:
        if password != password2:
            raise UserError("两次输入的密码不一致")
        user_id = users_service.register_with_invite(code, username, password, display_name)
    except UserError as exc:
        return templating.render(
            request,
            "register.html",
            code=code,
            error=str(exc),
            username=username,
            display_name=display_name,
            status_code=400,
        )
    auth.clear_failures(username, auth.source_of(request))
    token = auth.create_session(user_id, ua=request.headers.get("user-agent", ""))
    users_service.touch_login(user_id)
    response = RedirectResponse("/", status_code=303)
    security.set_session_cookie(response, token)
    logger.info("邀请码注册成功：%s(id=%s)", username, user_id)
    return response


# ---------------------------------------------------------------- 用户管理
@pages.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request):
    user, denied = _require_admin(request)
    if denied is not None:
        return denied
    return templating.render(
        request,
        "admin_users.html",
        users=users_service.list_users(),
        counts=users_service.owned_counts(),
        me=user,
        role_labels=users_service.ROLE_LABELS,
        **_flash(request),
    )


@pages.post("/admin/users")
def admin_users_create(
    request: Request,
    username: str = Form(""),
    display_name: str = Form(""),
    password: str = Form(""),
    role: str = Form("user"),
):
    user, denied = _require_admin(request)
    if denied is not None:
        return denied
    try:
        users_service.create_user(username, password, display_name, role)
    except UserError as exc:
        return _redirect("/admin/users", error=str(exc))
    return _redirect("/admin/users", msg=f"已创建用户 {username.strip()}")


@pages.post("/admin/users/{user_id}/password")
def admin_users_password(
    request: Request,
    user_id: int,
    password: str = Form(""),
):
    user, denied = _require_admin(request)
    if denied is not None:
        return denied
    try:
        users_service.set_password(user_id, password)
    except UserError as exc:
        return _redirect("/admin/users", error=str(exc))
    return _redirect("/admin/users", msg="密码已重置，该用户需要重新登录")


@pages.post("/admin/users/{user_id}/toggle")
def admin_users_toggle(request: Request, user_id: int):
    user, denied = _require_admin(request)
    if denied is not None:
        return denied
    target = users_service.get(user_id)
    if target is None:
        return _redirect("/admin/users", error="用户不存在")
    activate = not bool(target["active"])
    if not activate and user is not None and int(user["id"]) == int(user_id):
        return _redirect("/admin/users", error="不能停用自己的账号")
    if not activate and target["role"] == "admin" and users_service.admin_count() <= 1:
        return _redirect("/admin/users", error="至少保留一个启用状态的管理员")
    try:
        users_service.set_active(user_id, activate, actor_id=int(user["id"]) if user else None)
    except UserError as exc:
        return _redirect("/admin/users", error=str(exc))
    return _redirect("/admin/users", msg=f"已{'启用' if activate else '停用'} {target['username']}")


@pages.post("/admin/users/{user_id}/delete")
def admin_users_delete(
    request: Request,
    user_id: int,
    confirm: str = Form("0"),
    mode: str = Form("transfer"),
    transfer_to: str = Form(""),
):
    """删除账号：mode=transfer 转归名下数据（默认），mode=purge 连带删除（需 confirm=1）。"""
    user, denied = _require_admin(request)
    if denied is not None:
        return denied
    target = users_service.get(user_id)
    if target is None:
        return _redirect("/admin/users", error="用户不存在")
    if user is not None and int(user["id"]) == int(user_id):
        return _redirect("/admin/users", error="不能删除自己的账号")
    mode_value = (mode or "transfer").strip()
    if mode_value not in users_service.DELETE_MODES:
        return _redirect("/admin/users", error="删除模式只能是转归（transfer）或彻底删除（purge）")
    owned = users_service.owned_rows(user_id)
    total = sum(owned.values())
    if mode_value == "purge" and confirm.strip() != "1":
        return _redirect(
            "/admin/users",
            error=(
                f"彻底删除将同时删除 {target['username']} 名下的 {total} 条数据"
                f"（{users_service.describe_counts(owned)}），请勾选确认框后重试"
            ),
        )
    raw_to = transfer_to.strip()
    to_id = int(raw_to) if raw_to.isdigit() else None
    try:
        result = users_service.delete_user(
            user_id,
            actor_id=int(user["id"]) if user else None,
            mode=mode_value,
            transfer_to=to_id,
        )
    except UserError as exc:
        return _redirect("/admin/users", error=str(exc))
    if result["mode"] == "purge":
        return _redirect(
            "/admin/users",
            msg=f"已删除用户 {target['username']} 及其名下 {sum(result['deleted'].values())} 条数据",
        )
    to_row = users_service.get(result["to"])
    to_name = (to_row["display_name"] or to_row["username"]) if to_row is not None else "目标用户"
    return _redirect(
        "/admin/users",
        msg=f"已删除用户 {target['username']}，{sum(result['moved'].values())} 条数据已转归给 {to_name}",
    )


# ---------------------------------------------------------------- 邀请码管理
@pages.get("/admin/invites", response_class=HTMLResponse)
def admin_invites_page(request: Request):
    user, denied = _require_admin(request)
    if denied is not None:
        return denied
    rows = users_service.list_invites()
    return templating.render(
        request,
        "admin_invites.html",
        invites=rows,
        states={row["id"]: users_service.invite_state(row) for row in rows},
        state_labels=users_service.INVITE_STATE_LABELS,
        me=user,
        **_flash(request),
    )


@pages.post("/admin/invites")
def admin_invites_create(
    request: Request,
    note: str = Form(""),
    max_uses: str = Form("1"),
    expires_days: str = Form(""),
):
    user, denied = _require_admin(request)
    if denied is not None:
        return denied
    try:
        invite = users_service.create_invite(
            note=note,
            max_uses=max_uses,
            expires_days=expires_days,
            created_by=int(user["id"]) if user else None,
        )
    except UserError as exc:
        return _redirect("/admin/invites", error=str(exc))
    return _redirect("/admin/invites", msg=f"已创建邀请码 {invite['code']}")


@pages.post("/admin/invites/{invite_id}/revoke")
def admin_invites_revoke(request: Request, invite_id: int):
    user, denied = _require_admin(request)
    if denied is not None:
        return denied
    try:
        users_service.revoke_invite(invite_id)
    except UserError as exc:
        return _redirect("/admin/invites", error=str(exc))
    return _redirect("/admin/invites", msg="邀请码已作废")
