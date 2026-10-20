"""LLM 密钥存储（SPEC.md 第 14.2 节）。

密钥绝不进数据库、绝不进 git：只落在 DATA_DIR/secrets.json，结构化：
    {"llm_api_keys": {"<profile_id>": "sk-..."}}

写入一律「临时文件 -> chmod 0600 -> os.replace」的原子替换，
中途崩溃不会留下半截文件，也不会短暂出现一个低权限之外的可见文件。
本模块只负责存取，展示层要的 has_key / sk-*** 由 llm.py 负责。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

FILENAME = "secrets.json"
API_KEYS_KEY = "llm_api_keys"
FILE_MODE = 0o600


def path() -> Path:
    """secrets.json 的绝对路径（DATA_DIR 由环境变量决定，每次现取，不缓存）。"""
    return settings.data_dir / FILENAME


def _empty() -> dict[str, Any]:
    return {API_KEYS_KEY: {}}


def _clean_keys(raw: Any) -> dict[str, str]:
    """只保留「非空 id -> 非空字符串」的键值对，其余一律丢弃。"""
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            continue
        pid = str(key or "").strip()
        secret = value.strip()
        if pid and secret:
            clean[pid] = secret
    return clean


def load() -> dict:
    """读取 secrets.json；文件不存在 / 内容损坏 / 权限不足都返回空结构，绝不抛异常。"""
    target = path()
    try:
        text = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return _empty()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        logger.warning("secrets.json 不是合法 JSON，按空处理：%s", target)
        return _empty()
    if not isinstance(data, dict):
        logger.warning("secrets.json 顶层不是对象，按空处理：%s", target)
        return _empty()
    return {API_KEYS_KEY: _clean_keys(data.get(API_KEYS_KEY))}


def save(data: dict) -> None:
    """原子写 secrets.json 并置 0600：临时文件写完 -> chmod -> os.replace。"""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {API_KEYS_KEY: _clean_keys((data or {}).get(API_KEYS_KEY))}

    handle, tmp_name = tempfile.mkstemp(
        prefix=".secrets-", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp_path, FILE_MODE)
        except OSError:
            logger.warning("secrets.json 临时文件 chmod 失败：%s", tmp_path, exc_info=True)
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(target, FILE_MODE)
    except OSError:
        logger.warning("secrets.json chmod 失败：%s", target, exc_info=True)


def get_api_key(profile_id: str) -> str:
    """取某个 profile 的密钥；没有就返回空字符串。"""
    pid = str(profile_id or "").strip()
    if not pid:
        return ""
    return load()[API_KEYS_KEY].get(pid, "")


def set_api_key(profile_id: str, key: str) -> None:
    """写入密钥；key 为空等价于删除该 profile 的密钥。"""
    pid = str(profile_id or "").strip()
    if not pid:
        raise ValueError("保存密钥必须提供 profile id")
    secret = str(key or "").strip()
    if not secret:
        delete_api_key(pid)
        return
    data = load()
    data[API_KEYS_KEY][pid] = secret
    save(data)


def delete_api_key(profile_id: str) -> None:
    """删除某个 profile 的密钥；本来就不存在时静默通过。"""
    pid = str(profile_id or "").strip()
    if not pid:
        return
    data = load()
    if pid not in data[API_KEYS_KEY]:
        return
    data[API_KEYS_KEY].pop(pid, None)
    save(data)


def has_api_key(profile_id: str) -> bool:
    """是否存在可用密钥（只读探测，供列表展示用）。"""
    return bool(get_api_key(profile_id))
