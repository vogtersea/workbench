"""SQLite 连接、DDL 与初始化（SPEC.md 第 4 / 5 节）。

所有其它模块只通过 connect / cursor / query / one / execute 访问数据库；
函数签名是跨模块契约，不得更改。

连接生命周期（W-05）：连接按线程缓存，cursor() 只负责事务边界（提交 / 回滚），
不再每语句开关连接。FastAPI 同步端点跑在 anyio 线程池里，线程会被复用，所以进程内
最多存在「线程池大小」条连接（默认上限 40）；线程退出时 threading.local 的槽位随线程
销毁，连接对象失去引用后由 GC 关闭。需要确定性释放时调用 close_threadlocal()
（测试 teardown / 应用关闭钩子）。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS projects (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL UNIQUE,
      description TEXT NOT NULL DEFAULT '',
      color TEXT NOT NULL DEFAULT '#6366f1',
      archived INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
      title TEXT NOT NULL,
      detail TEXT NOT NULL DEFAULT '',
      done_criteria TEXT NOT NULL DEFAULT '',
      due_date TEXT,
      due_time TEXT,
      status TEXT NOT NULL DEFAULT 'todo',
      priority INTEGER NOT NULL DEFAULT 2,
      week_start TEXT NOT NULL,
      tags TEXT NOT NULL DEFAULT '',
      completed_at TEXT, archived INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_week ON tasks(week_start)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, archived)",
    """
    CREATE TABLE IF NOT EXISTS progress_entries (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      content TEXT NOT NULL, link TEXT,
      created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_progress_task ON progress_entries(task_id)",
    """
    CREATE TABLE IF NOT EXISTS reports (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL,
      scope_label TEXT NOT NULL,
      range_start TEXT, range_end TEXT,
      task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
      content_md TEXT NOT NULL,
      provider TEXT, model TEXT,
      is_mock INTEGER NOT NULL DEFAULT 0,
      context_notes TEXT,
      created_at TEXT NOT NULL
    )
    """,
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS reminders_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      sent_at TEXT NOT NULL, kind TEXT NOT NULL,
      channel TEXT NOT NULL, payload TEXT,
      ok INTEGER NOT NULL DEFAULT 0, error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notes_index (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      path TEXT NOT NULL UNIQUE, title TEXT, tags TEXT,
      frontmatter TEXT, body TEXT, mtime REAL, indexed_at TEXT
    )
    """,
    # ---- 多用户（SPEC.md 15.2）----
    """
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL UNIQUE,
      display_name TEXT NOT NULL DEFAULT '',
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'user',
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL,
      last_login_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invites (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      code TEXT NOT NULL UNIQUE,
      note TEXT NOT NULL DEFAULT '',
      created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
      max_uses INTEGER NOT NULL DEFAULT 1,
      used_count INTEGER NOT NULL DEFAULT 0,
      expires_at TEXT,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
      token TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      ua TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at)",
    # ---- 登录节流（W-07）：落在库里，跨重启仍生效，内存不留状态 ----
    """
    CREATE TABLE IF NOT EXISTS login_attempts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL,
      source TEXT NOT NULL,
      at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_login_attempts "
    "ON login_attempts(username, source, at)",
    # ---- 例行工作（SPEC.md 16.2）----
    """
    CREATE TABLE IF NOT EXISTS routines (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
      title TEXT NOT NULL,
      detail TEXT NOT NULL DEFAULT '',
      done_criteria TEXT NOT NULL DEFAULT '',
      freq TEXT NOT NULL,
      due_rule TEXT NOT NULL DEFAULT 'period_end',
      due_day INTEGER,
      due_time TEXT,
      priority INTEGER NOT NULL DEFAULT 2,
      project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
      tags TEXT NOT NULL DEFAULT '',
      active INTEGER NOT NULL DEFAULT 1,
      start_date TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_routines_owner ON routines(owner_id)",
)

# 归属列迁移（SPEC.md 15.3）：表名 -> 索引名。列可为 NULL，兼容历史行。
OWNER_TARGETS: tuple[tuple[str, str], ...] = (
    ("tasks", "idx_tasks_owner"),
    ("projects", "idx_projects_owner"),
    ("reports", "idx_reports_owner"),
)

# 例行工作列与唯一索引（SPEC.md 16.2）：tasks 新增 routine_id / period_key，可空以兼容历史行。
ROUTINE_TARGETS: tuple[tuple[str, str], ...] = (
    ("tasks", "routine_id INTEGER REFERENCES routines(id) ON DELETE CASCADE"),
    ("tasks", "period_key TEXT"),
)
ROUTINE_INDEXES: tuple[str, ...] = (
    # 部分唯一索引：普通任务 routine_id 为 NULL，互不冲突；同一模板同一期只允许一条实例
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_routine_period "
    "ON tasks(routine_id, period_key) WHERE routine_id IS NOT NULL",
)

NOTES_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
    "path UNINDEXED, title, tags, body, tokenize='{tokenizer}')"
)
FTS_TOKENIZERS = ("trigram", "unicode61")

# 抢占式日锁的默认值（W-06）：value 形如 2026-10-20。抢锁逻辑属于 services/scheduler.py
# （UPDATE settings SET value = ? WHERE key = 'reminder_lock' AND value < ?），db 只负责
# 让这一行存在——缺行时那条 UPDATE 影响行数恒为 0，谁都抢不到锁。
DEFAULT_SETTINGS_EXTRA: tuple[tuple[str, str], ...] = (("reminder_lock", ""),)

_local = threading.local()


def connect() -> sqlite3.Connection:
    """返回本线程缓存的连接（W-05）：Row 工厂、WAL、foreign_keys=ON、busy_timeout=5000。

    首次调用才真正 open，同线程后续调用直接复用。
    不要改成全局单连接 + check_same_thread=False：并发写会退化成锁竞争。
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    _local.conn = conn
    return conn


def close_threadlocal() -> None:
    """关闭本线程缓存的连接并清空槽位；本线程尚无连接时是空操作。

    调用点：测试 teardown（用例间不串连接）、应用关闭钩子。线程池线程被回收时即便
    不调用也不泄漏句柄：threading.local 的槽位随线程销毁，连接交给 GC 关闭。
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        return
    _local.conn = None
    conn.close()


@contextmanager
def cursor() -> Iterator[sqlite3.Connection]:
    """事务上下文：正常提交，异常回滚。连接按线程复用，不再关闭（W-05）。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def query(sql: str, params=()) -> list[sqlite3.Row]:
    with cursor() as conn:
        return conn.execute(sql, params).fetchall()


def one(sql: str, params=()) -> sqlite3.Row | None:
    with cursor() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params=()) -> int:
    """写语句：INSERT/REPLACE 返回 lastrowid，其余返回受影响行数。"""
    verb = (sql.lstrip().split(None, 1) or [""])[0].upper()
    with cursor() as conn:
        cur = conn.execute(sql, params)
        value = cur.lastrowid if verb in {"INSERT", "REPLACE"} else cur.rowcount
        return int(value or 0)


def _ensure_owner_columns() -> list[str]:
    """幂等补齐 owner_id 列与索引，返回本次新增列的表名。"""
    added: list[str] = []
    for table, index in OWNER_TARGETS:
        info = query(f"PRAGMA table_info({table})")
        if not info:
            logger.warning("表 %s 不存在，跳过 owner_id 迁移", table)
            continue
        if any(row["name"] == "owner_id" for row in info):
            continue
        with cursor() as conn:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN owner_id INTEGER "
                "REFERENCES users(id) ON DELETE CASCADE"
            )
        added.append(table)
        logger.info("表 %s 已新增归属列 owner_id", table)

    for table, index in OWNER_TARGETS:
        with cursor() as conn:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {table}(owner_id)")
    return added


def _ensure_routine_columns() -> list[str]:
    """幂等补齐 tasks.routine_id / period_key，并建例行期次唯一索引（SPEC.md 16.2）。"""
    info = query("PRAGMA table_info(tasks)")
    if not info:
        logger.warning("表 tasks 不存在，跳过例行列迁移")
        return []
    existing = {row["name"] for row in info}
    added: list[str] = []
    for table, definition in ROUTINE_TARGETS:
        name = definition.split()[0]
        if name in existing:
            continue
        with cursor() as conn:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
        existing.add(name)
        added.append(name)
        logger.info("表 %s 已新增例行列 %s", table, name)

    for statement in ROUTINE_INDEXES:
        with cursor() as conn:
            conn.execute(statement)
    return added


def _ensure_fts() -> str:
    """确保 notes_fts 存在，返回实际使用的分词器。trigram 不可用时降级 unicode61。"""
    row = one("SELECT sql FROM sqlite_master WHERE name = 'notes_fts'")
    if row is not None:
        match = re.search(r"tokenize\s*=\s*'([^']+)'", row["sql"] or "")
        return match.group(1) if match else "unknown"
    for tokenizer in FTS_TOKENIZERS:
        try:
            with cursor() as conn:
                conn.execute(NOTES_FTS_DDL.format(tokenizer=tokenizer))
            return tokenizer
        except sqlite3.OperationalError:
            logger.warning("notes_fts 使用 %s 分词器建表失败，尝试下一档", tokenizer)
    logger.error("notes_fts 建表失败：当前 sqlite 不含 fts5 支持")
    return "unavailable"


def init_db() -> None:
    """幂等建表 + 记录实际 fts 分词器 + 写入默认 settings。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with cursor() as conn:
        for statement in SCHEMA:
            conn.execute(statement)

    added_owner = _ensure_owner_columns()
    added_routine = _ensure_routine_columns()
    tokenizer = _ensure_fts()
    with cursor() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('fts_tokenizer', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (tokenizer,),
        )
        for key, value in DEFAULT_SETTINGS_EXTRA:
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value)
            )

    from app.services import settings_store  # 延迟导入，避免 db <-> settings_store 循环

    settings_store.seed_defaults()
    logger.info(
        "数据库就绪：%s（fts_tokenizer=%s%s%s）",
        settings.db_path,
        tokenizer,
        f"，新增归属列 {','.join(added_owner)}" if added_owner else "",
        f"，新增例行列 {','.join(added_routine)}" if added_routine else "",
    )
