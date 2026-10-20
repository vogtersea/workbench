"""设置页路由聚合（原 app/web/pages_settings.py 按子域拆分，W-08）。

职责边界：只建聚合 APIRouter 并把各子域路由挂进来；子路由内部写的是全路径，故不加 prefix。
子模块：shared（通用工具/探测句柄/常量）、users（身份与写权限）、vault（知识库上下文与卡片，
自身不带路由）、general（渲染骨架/系统信息/基础路由）、llm_context（大模型上下文）、
llm（/settings/llm-*）、webhook（推送与提醒配置）。
"""

from fastapi import APIRouter

from . import general, llm, webhook

pages = APIRouter()
pages.include_router(general.router)
pages.include_router(llm.router)
pages.include_router(webhook.router)

#: 兼容既有引用名（main.py 目前用 pages；保留 router 以免后续改名时再动调用方）
router = pages
