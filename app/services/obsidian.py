"""Obsidian 只读索引与检索：实现已拆到 obsidian_vault / obsidian_index / obsidian_search，本模块是转发层（W-08）。

拆分前这里是 937 行的单文件：目录层（路径解析与浏览）、索引层（解析与重建）、
检索层（FTS5 检索与上下文）各一个模块，本文件只做 re-export。
本文件是 obsidian 域**稳定的公开门面**（不是临时兼容层，请勿删除）：
pages_obsidian.py、routes_obsidian.py、templating.py、report 域都通过 from app.services import obsidian
取用 browse/reindex/search/stats/vault_* 等符号。三个子模块只被本文件引用，外部一律走这个门面。
"""

from app.services.obsidian_index import (
    LIST_KEYS as LIST_KEYS,
    indexed_note_count as indexed_note_count,
    reindex as reindex,
    stats as stats,
)
from app.services.obsidian_search import (
    CONTEXT_HEADER as CONTEXT_HEADER,
    MAX_LIMIT as MAX_LIMIT,
    MAX_QUERY as MAX_QUERY,
    SNIPPET_RADIUS as SNIPPET_RADIUS,
    build_context as build_context,
    search as search,
)
from app.services.obsidian_vault import (
    COUNT_CACHE_MAX as COUNT_CACHE_MAX,
    COUNT_CACHE_TTL as COUNT_CACHE_TTL,
    DIR_SCAN_LIMIT as DIR_SCAN_LIMIT,
    SKIP_DIRS as SKIP_DIRS,
    VAULT_REL_KEY as VAULT_REL_KEY,
    VAULT_ROOT_DEFAULT as VAULT_ROOT_DEFAULT,
    browse as browse,
    current_rel as current_rel,
    set_vault_path as set_vault_path,
    vault_available as vault_available,
    vault_path as vault_path,
    vault_root as vault_root,
)
