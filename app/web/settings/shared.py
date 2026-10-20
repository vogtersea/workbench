"""设置页共享层：通用取值小工具、llm 与 obsidian 两域的兜底句柄、页面级常量。

职责边界（W-08）：只放被 2 个以上子模块真正复用的东西；页面渲染骨架在 general.py，
知识库上下文在 vault.py，谁用谁 import，避免循环。"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request

from app.services import settings_store as S

logger = logging.getLogger("app.web.pages_settings")

FLASH_MESSAGES: dict[str, str] = {
    "test-push": "测试推送已执行，结果见「最近一次测试推送」。",
    "scan-now": "已立即扫描一次，结果见「最近一次扫描」。",
    "reindex": "已触发 Obsidian 索引重建。",
    "llm-profile": "大模型配置已保存，立即生效（无需重启容器）。",
    "llm-active": "已切换生效配置，下一份报告即使用新配置。",
    "llm-key": "密钥已保存（页面不回显明文）。",
    "llm-key-clear": "密钥已清除。",
    "llm-delete": "大模型配置已删除。",
    "llm-test": "已执行连接测试，详细结果见「大模型」卡片内的测试输出。",
    "vault": "知识库目录已更新，建议随后重建一次索引。",
}


LAST_REINDEX_KEY = "last_reindex_json"


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def _is_htmx(request: Request) -> bool:
    return (request.headers.get("hx-request") or "").lower() == "true"


def _field(obj: Any, *names: str, default: Any = "") -> Any:
    """从 dict / 对象里按顺序取第一个存在的字段（兼容服务端返回 dict 或 dataclass）。"""
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
            continue
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _obsidian():
    from app.services import obsidian

    return obsidian


def _llm():
    from app.services import llm

    return llm


def _obsidian_fn(name: str):
    fn = getattr(_obsidian(), name, None)
    if fn is None:
        raise NotImplementedError(f"obsidian.{name} 尚未实现")
    return fn


def _llm_fn(name: str):
    fn = getattr(_llm(), name, None)
    if fn is None:
        raise NotImplementedError(f"llm.{name} 尚未实现")
    return fn


def _load_reindex_result() -> dict[str, Any] | None:
    raw = S.get(LAST_REINDEX_KEY, "")
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None
