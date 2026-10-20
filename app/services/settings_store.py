"""settings 表读写（SPEC.md 第 5 节）。

settings 只存非密钥项：密钥一律留在环境变量 / .env。
"""

from __future__ import annotations

from app import db

DEFAULT_SETTINGS: dict[str, str] = {
    "remind_time": "09:00",
    "remind_soon_days": "3",
    "webhook_url": "",
    "webhook_format": "generic_json",
    "telegram_chat_id": "",
    "llm_base_url": "",
    "llm_model": "",
    "obsidian_enabled": "0",
    "obsidian_last_index": "",
    "fts_tokenizer": "trigram",
    "last_scan_json": "",
}


def get(key: str, default: str = "") -> str:
    row = db.one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return default
    return row["value"]


def set(key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def all() -> dict[str, str]:
    rows = db.query("SELECT key, value FROM settings ORDER BY key")
    return {row["key"]: row["value"] for row in rows}


def seed_defaults() -> None:
    """只补齐缺失的键，绝不覆盖已有值。"""
    with db.cursor() as conn:
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value)
            )
