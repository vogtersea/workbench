"""测试夹具：必须在导入 app 之前设置 DATA_DIR（config 在导入期即单例化）。"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="wb-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["APP_PASSWORD"] = ""
os.environ["LLM_API_KEY"] = ""          # 强制 MOCK，测试不联网
os.environ["VAULT_DIR"] = os.path.join(_TMP, "vault")

import pytest
from httpx import ASGITransport, AsyncClient

# 用例间要清空的业务表：子表在前，避免外键顺序引发删除失败。
# settings 保留（init_db 播种的默认值）；notes_fts 影子表不在此列（无用例写 Obsidian 索引）。
_DATA_TABLES = (
    "progress_entries",
    "reports",
    "tasks",
    "routines",
    "projects",
    "reminders_log",
    "invites",
    "sessions",
    "users",
)


@pytest.fixture(scope="session")
def app():
    from app.main import create_app
    from app import db

    application = create_app()
    db.init_db()
    return application


@pytest.fixture
def clean_db(app):
    """用例开始前清空业务数据（由 client 夹具串联，纯函数用例不会触发建库）。

    测试共用一个临时库（session 级 app），而 test_scope 会建账号、把鉴权模式从
    open 切到 multi；不隔离的话用例结果会随执行顺序变化。
    """
    from app import db

    with db.cursor() as conn:
        for table in _DATA_TABLES:
            conn.execute(f"DELETE FROM {table}")
    yield


@pytest.fixture
async def client(app, clean_db):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
