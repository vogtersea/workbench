"""报告页内二次编辑（W-11）：改后正文生效、可恢复原文、越权不可改。"""

import io

import pytest

from app import db
from app.services import report as reports
from app.services import users

ORIGINAL = "# 原始标题\n\n原始正文"


@pytest.fixture()
def owner(clean_db):
    """建一个属主账号；clean_db 保证用例之间互不影响。"""
    return users.create_user(
        username="owner", display_name="Owner", password="owner-pass", role="admin"
    )


@pytest.fixture()
def report_id(owner):
    rid = db.execute(
        "INSERT INTO reports(kind, scope_label, content_md, is_mock, owner_id, created_at) "
        "VALUES(?,?,?,?,?,?)",
        ("week", "测试周报", ORIGINAL, 1, owner, "2026-10-21T10:00:00+08:00"),
        returning_id=True,
    )
    return rid


def test_未编辑时正文取原文(report_id):
    row = reports.get_report(report_id)
    assert reports.content_of(row) == ORIGINAL


def test_保存编辑后正文取改后版本(report_id):
    assert reports.save_content(report_id, "# 改后标题\n\n改后正文") is True
    row = reports.get_report(report_id)
    assert reports.content_of(row) == "# 改后标题\n\n改后正文"
    assert row["content_md"] == ORIGINAL  # 原始生成内容不被覆盖


def test_空内容保存等于恢复原文(report_id):
    reports.save_content(report_id, "改过的")
    assert reports.content_of(reports.get_report(report_id)) == "改过的"
    assert reports.save_content(report_id, "   ") is True
    row = reports.get_report(report_id)
    assert row["content_md_edited"] is None
    assert reports.content_of(row) == ORIGINAL


def test_越权不可改也查不到(owner, report_id):
    other = users.create_user(
        username="other", display_name="Other", password="other-pass", role="user"
    )
    assert reports.get_report(report_id, owner_id=other) is None
    assert reports.save_content(report_id, "黑客改的", owner_id=other) is False
    assert reports.content_of(reports.get_report(report_id, owner_id=owner)) == ORIGINAL


def test_导出用的是改后正文(app, owner, report_id):
    from fastapi.testclient import TestClient
    from docx import Document

    reports.save_content(report_id, "# 导出的应该是这一版\n\n正文内容")
    with TestClient(app) as http:
        login = http.post("/login", data={"username": "owner", "password": "owner-pass"})
        assert login.status_code in (200, 303)
        resp = http.get("/reports/%d/export.docx" % report_id)
    assert resp.status_code == 200
    assert resp.content[:4] == b"PK\x03\x04"
    text = "".join(p.text for p in Document(io.BytesIO(resp.content)).paragraphs)
    assert "导出的应该是这一版" in text
    assert "原始标题" not in text
