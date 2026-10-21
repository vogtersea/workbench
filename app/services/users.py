"""用户与邀请码服务（SPEC.md 15.2 / 15.5 / 15.6）。

身份域唯一的数据入口：页面（app/web/pages_auth.py）与 API 都通过本模块读写 users / invites，
密码哈希与校验委托给 app/auth.py，本模块不碰 cookie。

bootstrap() 在 main.py 启动时调用一次，必须幂等：
  1. 建表与加列由 app.db.init_db() 完成（先于本函数执行）。
  2. users 为空且 APP_PASSWORD 非空 -> 建管理员（APP_ADMIN_USER，默认 admin）。
  3. users 为空且 APP_PASSWORD 为空 -> 不建号，保持开放模式，/setup 建首个管理员。
  4. 存在管理员且仍有 owner_id IS NULL 的历史行 -> 一次性归给最早的管理员。
  5. 重复启动不重复建号、不覆盖已有密码。

删除账号的两种数据处置（SPEC 15.12）：默认 mode="transfer" 把名下
tasks/projects/reports 的 owner_id 批量转归（不指定则归给执行删除的管理员）后删账号；
mode="purge" 先统计再连带删除名下数据（显式 DELETE，不依赖 CASCADE）。两者都写审计日志。

管理员护栏（服务层，任何调用路径都生效）：不得让“启用中的管理员”数量归零——
set_active(uid, 0) / set_role(uid, 'user') / delete_user(uid) 在目标为最后一个启用中管理员时
抛 UserError（页面捕获后仍渲染 303 + 可读提示，不会 500）；写语句还额外带条件护栏，
避免读检查与写入之间被并发改坏。存在多个管理员时互相禁用/删除不受影响。
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import sqlite3

from app import auth, db, timeutil
from app.config import settings

logger = logging.getLogger(__name__)

ROLES = ("admin", "user")
ROLE_LABELS = {"admin": "管理员", "user": "成员"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{1,31}$")
MIN_PASSWORD_LENGTH = 6
DEFAULT_ADMIN_USERNAME = "admin"
INVITE_CODE_BYTES = 9
OWNER_TABLES = ("tasks", "projects", "reports")

DELETE_MODES = ("transfer", "purge")
OWNER_TABLE_LABELS = {"tasks": "工作项", "projects": "项目", "reports": "报告"}

LAST_ADMIN_ERRORS = {
    "disable": "不能停用最后一个管理员账号，请先创建或启用另一个管理员",
    "demote": "不能取消最后一个管理员的角色，请先创建或启用另一个管理员",
    "delete": "不能删除最后一个管理员账号，请先创建或启用另一个管理员",
}
# 条件写语句里的兜底护栏：当目标仍是启用中管理员时，必须还存在另一个启用中管理员。
ADMIN_GUARD_SQL = (
    "(role <> 'admin' OR active = 0 OR EXISTS ("
    "SELECT 1 FROM users AS other "
    "WHERE other.role = 'admin' AND other.active = 1 AND other.id <> users.id))"
)


class UserError(ValueError):
    """可读的业务错误：页面直接展示 message。"""


# ---------------------------------------------------------------- 校验
def validate_username(username: str) -> str:
    value = (username or "").strip()
    if not value:
        raise UserError("用户名不能为空")
    if not USERNAME_RE.match(value):
        raise UserError("用户名需 2-32 位，字母或数字开头，可用字母数字与 _ . @ -")
    return value


def validate_password(password: str) -> str:
    value = password or ""
    if len(value) < MIN_PASSWORD_LENGTH:
        raise UserError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
    return value


def validate_role(role: str) -> str:
    value = (role or "user").strip() or "user"
    if value not in ROLES:
        raise UserError("角色只能是管理员或成员")
    return value


# ---------------------------------------------------------------- 用户
def count() -> int:
    row = db.one("SELECT COUNT(*) AS n FROM users")
    return int(row["n"]) if row is not None else 0


def list_users() -> list:
    return db.query(
        "SELECT id, username, display_name, role, active, created_at, last_login_at "
        "FROM users ORDER BY (role = 'admin') DESC, id"
    )


def get(user_id: int):
    return db.one(
        "SELECT id, username, display_name, role, active, created_at, last_login_at "
        "FROM users WHERE id = ?",
        (int(user_id),),
    )


def get_with_hash(user_id: int):
    return db.one("SELECT * FROM users WHERE id = ?", (int(user_id),))


def get_by_username(username: str):
    value = (username or "").strip()
    if not value:
        return None
    return db.one("SELECT * FROM users WHERE username = ?", (value,))


def create_user(
    username: str,
    password: str,
    display_name: str = "",
    role: str = "user",
) -> int:
    uname = validate_username(username)
    raw = validate_password(password)
    role_value = validate_role(role)
    if get_by_username(uname) is not None:
        raise UserError("用户名已存在")
    return db.execute(
        "INSERT INTO users(username, display_name, password_hash, role, active, created_at) "
        "VALUES(?,?,?,?,1,?)",
        (uname, (display_name or "").strip() or uname, auth.hash_password(raw), role_value, timeutil.now_iso()),
        returning_id=True,
    )


def set_password(user_id: int, password: str) -> None:
    raw = validate_password(password)
    if get(user_id) is None:
        raise UserError("用户不存在")
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (auth.hash_password(raw), int(user_id)),
    )
    auth.destroy_user_sessions(user_id)


def is_last_active_admin(user_id: int) -> bool:
    """该用户是否为唯一的启用中管理员（再禁用/降级/删除就会归零）。"""
    row = get(user_id)
    if row is None or row["role"] != "admin" or not row["active"]:
        return False
    others = db.one(
        "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1 AND id <> ?",
        (int(user_id),),
    )
    return int(others["n"]) == 0


def set_active(user_id: int, active: bool, actor_id: int | None = None) -> None:
    target = int(user_id)
    if get(target) is None:
        raise UserError("用户不存在")
    if active:
        db.execute("UPDATE users SET active = 1 WHERE id = ?", (target,))
        return
    if is_last_active_admin(target):
        raise UserError(LAST_ADMIN_ERRORS["disable"])
    if actor_id is not None and target == int(actor_id):
        raise UserError("不能停用自己的账号")
    changed = db.execute(
        "UPDATE users SET active = 0 WHERE id = ? AND " + ADMIN_GUARD_SQL, (target,)
    )
    if not changed:
        raise UserError(LAST_ADMIN_ERRORS["disable"])
    auth.destroy_user_sessions(target)


def set_role(user_id: int, role: str) -> None:
    target = int(user_id)
    value = validate_role(role)
    if get(target) is None:
        raise UserError("用户不存在")
    if value != "admin" and is_last_active_admin(target):
        raise UserError(LAST_ADMIN_ERRORS["demote"])
    changed = db.execute(
        "UPDATE users SET role = ? WHERE id = ? AND (? = 'admin' OR " + ADMIN_GUARD_SQL + ")",
        (value, target, value),
    )
    if not changed:
        raise UserError(LAST_ADMIN_ERRORS["demote"])


def touch_login(user_id: int) -> None:
    db.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?",
        (timeutil.now_iso(), int(user_id)),
    )


def owned_rows(user_id: int) -> dict[str, int]:
    """该用户名下各表的数据行数（删除前提示用）。"""
    result: dict[str, int] = {}
    for table in OWNER_TABLES:
        row = db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE owner_id = ?", (int(user_id),))
        result[table] = int(row["n"]) if row is not None else 0
    return result


def owned_counts() -> dict[int, dict[str, int]]:
    """按用户汇总各表数据行数，供用户管理表格展示。"""
    result: dict[int, dict[str, int]] = {}
    for table in OWNER_TABLES:
        for row in db.query(
            f"SELECT owner_id, COUNT(*) AS n FROM {table} "
            "WHERE owner_id IS NOT NULL GROUP BY owner_id"
        ):
            bucket = result.setdefault(int(row["owner_id"]), {})
            bucket[table] = int(row["n"])
    return result


def describe_counts(counts: dict[str, int]) -> str:
    """{"tasks": 2, "projects": 1} -> "工作项 2 条、项目 1 条"；全零返回“无数据”。"""
    parts = [
        f"{OWNER_TABLE_LABELS.get(table, table)} {num} 条"
        for table, num in counts.items()
        if num
    ]
    return "、".join(parts) if parts else "无数据"


def resolve_transfer_target(
    user_id: int, transfer_to: int | None, actor_id: int | None
) -> int:
    """数据转归目标：缺省取执行删除的管理员；必须是启用中的用户。"""
    raw = transfer_to if transfer_to not in (None, "") else actor_id
    if raw in (None, ""):
        raise UserError("请选择数据转归的用户")
    try:
        to_id = int(raw)
    except (TypeError, ValueError):
        raise UserError("转归目标必须是有效的用户 id")
    if to_id == int(user_id):
        raise UserError("不能把数据转归给即将删除的账号")
    row = get(to_id)
    if row is None:
        raise UserError("转归目标用户不存在")
    if not row["active"]:
        raise UserError("转归目标必须是启用中的用户")
    return to_id


def delete_user(
    user_id: int,
    actor_id: int | None = None,
    *,
    mode: str = "transfer",
    transfer_to: int | None = None,
) -> dict:
    """删除账号：默认把名下数据转归，mode="purge" 才连带删除数据（SPEC 15.12）。"""
    target = int(user_id)
    row = get(target)
    if row is None:
        raise UserError("用户不存在")
    if is_last_active_admin(target):
        raise UserError(LAST_ADMIN_ERRORS["delete"])
    if actor_id is not None and target == int(actor_id):
        raise UserError("不能删除自己的账号")

    mode_value = (mode or "transfer").strip()
    if mode_value not in DELETE_MODES:
        raise UserError("删除模式只能是转归（transfer）或彻底删除（purge）")

    if mode_value == "transfer":
        to_id = resolve_transfer_target(target, transfer_to, actor_id)
        moved: dict[str, int] = {}
        try:
            with db.cursor() as conn:
                for table in OWNER_TABLES:
                    cursor = conn.execute(
                        f"UPDATE {table} SET owner_id = ? WHERE owner_id = ?", (to_id, target)
                    )
                    moved[table] = int(cursor.rowcount or 0)
                removed = conn.execute("DELETE FROM users WHERE id = ?", (target,))
                if not removed.rowcount:
                    raise UserError("用户不存在")
        except sqlite3.IntegrityError as exc:
            raise UserError(
                "转归失败：目标用户名下存在同名项目，请先在「项目」页改名后重试"
            ) from exc
        auth.destroy_user_sessions(target)
        logger.warning(
            "删除用户审计：被删用户=%s(id=%s) 执行者=%s(id=%s) 模式=transfer "
            "转归条数=%s 转归目标=%s",
            row["username"],
            target,
            "system" if actor_id is None else "user",
            actor_id,
            moved,
            to_id,
        )
        return {"mode": "transfer", "moved": moved, "to": to_id}

    deleted: dict[str, int] = {}
    with db.cursor() as conn:
        for table in OWNER_TABLES:
            cursor = conn.execute(f"DELETE FROM {table} WHERE owner_id = ?", (target,))
            deleted[table] = int(cursor.rowcount or 0)
        removed = conn.execute("DELETE FROM users WHERE id = ?", (target,))
        if not removed.rowcount:
            raise UserError("用户不存在")
    auth.destroy_user_sessions(target)
    logger.warning(
        "删除用户审计：被删用户=%s(id=%s) 执行者=%s(id=%s) 模式=purge 删除条数=%s",
        row["username"],
        target,
        "system" if actor_id is None else "user",
        actor_id,
        deleted,
    )
    return {"mode": "purge", "deleted": deleted}


def admin_count() -> int:
    row = db.one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1")
    return int(row["n"]) if row is not None else 0


# ---------------------------------------------------------------- 邀请码
INVITE_STATE_LABELS = {
    "ok": "可用",
    "exhausted": "已用完/已作废",
    "expired": "已过期",
}


def invite_state(invite) -> str:
    if invite is None:
        return "invalid"
    if int(invite["used_count"] or 0) >= int(invite["max_uses"] or 0):
        return "exhausted"
    expires_at = invite["expires_at"]
    if expires_at and str(expires_at) <= timeutil.now_iso():
        return "expired"
    return "ok"


def invite_error(state: str) -> str:
    return {
        "invalid": "邀请码无效",
        "exhausted": "邀请码已用完或已作废",
        "expired": "邀请码已过期",
    }.get(state, "邀请码不可用")


def list_invites() -> list:
    return db.query(
        "SELECT i.*, u.username AS creator_name FROM invites i "
        "LEFT JOIN users u ON u.id = i.created_by ORDER BY i.id DESC"
    )


def get_invite(code: str):
    return db.one("SELECT * FROM invites WHERE code = ?", ((code or "").strip(),))


def create_invite(
    note: str = "",
    max_uses: int = 1,
    expires_days: int | None = None,
    created_by: int | None = None,
) -> dict:
    try:
        uses = int(max_uses)
    except (TypeError, ValueError):
        raise UserError("可用次数必须是整数")
    if uses < 1 or uses > 999:
        raise UserError("可用次数需在 1-999 之间")
    expires_at = None
    if expires_days not in (None, "", 0, "0"):
        try:
            days = int(expires_days)
        except (TypeError, ValueError):
            raise UserError("有效天数必须是整数")
        if days < 1 or days > 3650:
            raise UserError("有效天数需在 1-3650 之间")
        from datetime import timedelta

        expires_at = (timeutil.now() + timedelta(days=days)).replace(microsecond=0).isoformat()
    for _ in range(5):
        code = secrets.token_urlsafe(INVITE_CODE_BYTES)
        if db.one("SELECT 1 FROM invites WHERE code = ?", (code,)) is None:
            break
    else:  # pragma: no cover - 碰撞概率可忽略
        raise UserError("邀请码生成失败，请重试")
    invite_id = db.execute(
        "INSERT INTO invites(code, note, created_by, max_uses, used_count, expires_at, created_at) "
        "VALUES(?,?,?,?,0,?,?)",
        (code, (note or "").strip()[:200], created_by, uses, expires_at, timeutil.now_iso()),
        returning_id=True,
    )
    return {"id": invite_id, "code": code, "max_uses": uses, "expires_at": expires_at}


def revoke_invite(invite_id: int) -> None:
    """作废：把已用次数直接拉满（表结构不含 revoked 列，见 SPEC 15.2）。"""
    row = db.one("SELECT * FROM invites WHERE id = ?", (int(invite_id),))
    if row is None:
        raise UserError("邀请码不存在")
    db.execute("UPDATE invites SET used_count = max_uses WHERE id = ?", (int(invite_id),))


def register_with_invite(
    code: str,
    username: str,
    password: str,
    display_name: str = "",
) -> int:
    """校验邀请码 + 建号 + 扣次数，三步在同一事务内完成。"""
    uname = validate_username(username)
    raw = validate_password(password)
    value = (code or "").strip()
    if not value:
        raise UserError("邀请码不能为空")
    now = timeutil.now_iso()
    with db.cursor() as conn:
        invite = conn.execute("SELECT * FROM invites WHERE code = ?", (value,)).fetchone()
        if invite is None:
            raise UserError(invite_error("invalid"))
        state = invite_state(invite)
        if state != "ok":
            raise UserError(invite_error(state))
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (uname,)).fetchone():
            raise UserError("用户名已存在")
        cursor = conn.execute(
            "INSERT INTO users(username, display_name, password_hash, role, active, created_at) "
            "VALUES(?,?,?, 'user', 1, ?)",
            (uname, (display_name or "").strip() or uname, auth.hash_password(raw), now),
        )
        user_id = int(cursor.lastrowid)
        updated = conn.execute(
            "UPDATE invites SET used_count = used_count + 1 "
            "WHERE id = ? AND used_count < max_uses",
            (int(invite["id"]),),
        )
        if updated.rowcount != 1:
            raise UserError(invite_error("exhausted"))
    return user_id


# ---------------------------------------------------------------- 引导与迁移
def migrate_orphans() -> dict[str, int]:
    """把 owner_id IS NULL 的历史行一次性归给最早的管理员（id 最小）。"""
    admin = db.one("SELECT id, username FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
    if admin is None:
        return {}
    migrated: dict[str, int] = {}
    with db.cursor() as conn:
        for table in OWNER_TABLES:
            cursor = conn.execute(
                f"UPDATE {table} SET owner_id = ? WHERE owner_id IS NULL",
                (int(admin["id"]),),
            )
            if cursor.rowcount:
                migrated[table] = cursor.rowcount
    if migrated:
        logger.info(
            "历史数据归属迁移完成：admin=%s(id=%s) 明细=%s",
            admin["username"],
            admin["id"],
            migrated,
        )
    return migrated


def bootstrap() -> dict:
    """启动时调用一次，幂等。返回结构仅用于日志/自测。"""
    created: str | None = None
    if count() == 0 and settings.app_password:
        username = (os.getenv("APP_ADMIN_USER") or "").strip() or DEFAULT_ADMIN_USERNAME
        try:
            create_user(username, settings.app_password, display_name="管理员", role="admin")
            created = username
            logger.info("首次启动：已按 APP_PASSWORD 创建管理员 %s", username)
        except UserError:
            logger.exception("首次启动创建管理员失败：%s", username)
    elif count() == 0:
        logger.info("users 为空且 APP_PASSWORD 为空：保持开放模式，可用 /setup 创建首个管理员")

    migrated = migrate_orphans()
    purged = auth.purge_expired_sessions()
    return {
        "users": count(),
        "created": created,
        "migrated": migrated,
        "purged_sessions": purged,
    }


def setup_first_admin(username: str, password: str, display_name: str = "") -> int:
    """无用户时创建首个管理员，并把历史行归给它。"""
    if count() > 0:
        raise UserError("系统已初始化，无法再次执行引导")
    user_id = create_user(username, password, display_name=display_name or "管理员", role="admin")
    migrate_orphans()
    logger.info("已创建首个管理员 %s(id=%s)", username, user_id)
    return user_id
