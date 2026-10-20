"""OpenAI 兼容大模型客户端 + 多套配置管理（SPEC.md 第 5 节 / 第 14.2 节，本域整体重写）。

配置层（改造后新增）
--------------------
- profile 列表存 settings 键 llm_profiles（JSON 数组），生效 id 存 llm_active；
- 密钥只存 DATA_DIR/secrets.json（见 secrets_store），绝不进数据库、绝不进 git；
- 生效参数优先级：选中 profile 的非空字段 > settings 覆盖值（llm_base_url / llm_model）
  > 环境变量 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY > 内置默认（DeepSeek）。
  profile 里 base_url / model 留空也按同一顺序回落，绝不把空值直接发出去；
- 环境变量里的 API Key 只回落到「同一 base_url」的 profile，避免把 deepseek 的 key 发给别家。

行为层（语义与改造前保持一致）
------------------------------
- MOCK：requires_key 为真且拿不到密钥时才算 mock；ollama 类本地模型无 key 也算 live；
- generate 失败自动重试 1 次，错误统一转成可读中文 LLMError（含状态码与脱敏后的响应片段）；
- 任何返回值、日志、异常文本都不出现明文密钥，统一展示为 sk-***。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.services import secrets_store, settings_store

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_PROVIDER = "deepseek"
DEFAULT_PROFILE_ID = "default"
DEFAULT_PROFILE_NAME = "默认配置（环境变量）"

PROVIDER = "openai-compatible"
MOCK_PROVIDER = "mock-template"
MOCK_MODEL = "mock-template"
MOCK_NOTICE = "> MOCK 模式：未配置 LLM_API_KEY，本报告为模板渲染，非大模型生成"
KEY_MASK = "sk-***"

RETRY_DELAY = 0.8
SNIPPET_LIMIT = 200
REPLY_SNIPPET_LIMIT = 80
MAX_TOKENS_LIMIT = 65536
MAX_TEMPERATURE = 2.0

KEY_PROFILES = "llm_profiles"
KEY_ACTIVE = "llm_active"
EDITABLE_FIELDS = (
    "name",
    "provider",
    "base_url",
    "model",
    "temperature",
    "max_tokens",
    "requires_key",
)

#: 前端可直接选用的预设（SPEC 14.2：至少 deepseek / openai / kimi / ollama / custom）
PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "provider": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "requires_key": True,
    },
    "openai": {
        "provider": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "requires_key": True,
    },
    "kimi": {
        "provider": "kimi",
        "name": "Kimi（月之暗面）",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "requires_key": True,
    },
    "ollama": {
        "provider": "ollama",
        "name": "Ollama 本地",
        "base_url": "http://host.docker.internal:11434/v1",
        "model": "qwen2.5:7b",
        "requires_key": False,
    },
    "custom": {
        "provider": "custom",
        "name": "自定义（OpenAI 兼容）",
        "base_url": "",
        "model": "",
        "requires_key": True,
    },
}
PROVIDERS: tuple[str, ...] = tuple(PRESETS)

_TRUE_VALUES = {"1", "true", "on", "yes", "y", "checked"}
_FALSE_VALUES = {"0", "false", "off", "no", "n", ""}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


class LLMError(RuntimeError):
    """LLM 调用失败。消息已是可读中文，密钥已脱敏。"""


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    is_mock: bool


# ---------------------------------------------------------------- 小工具


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool_or(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default


def _float_or(
    value: Any, default: float, *, strict: bool = False, field: str = "temperature"
) -> float:
    """转 float 并夹到 0~2；strict 为真时格式错误抛 ValueError（供 API 转 400）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return float(default)
    try:
        number = float(value) if isinstance(value, (int, float)) else float(str(value).strip())
    except (TypeError, ValueError):
        if strict:
            raise ValueError("%s 必须是数字" % field) from None
        return float(default)
    if number != number:  # NaN
        if strict:
            raise ValueError("%s 必须是数字" % field)
        return float(default)
    return max(0.0, min(MAX_TEMPERATURE, number))


def _int_or(
    value: Any, default: int, *, strict: bool = False, field: str = "max_tokens"
) -> int:
    """转 int 并夹到 1~65536；strict 为真时格式错误抛 ValueError。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return int(default)
    try:
        number = int(float(value) if isinstance(value, (int, float, str)) else 0)
    except (TypeError, ValueError):
        if strict:
            raise ValueError("%s 必须是整数" % field) from None
        return int(default)
    return max(1, min(MAX_TOKENS_LIMIT, number))


def _setting(key: str, default: str = "") -> str:
    """读 settings 表的覆盖值；库未就绪时按空处理（不抛异常）。"""
    try:
        return _text(settings_store.get(key, default))
    except Exception:
        logger.debug("读取 settings 键 %s 失败，回退 env 配置", key, exc_info=True)
        return default


def _set_setting(key: str, value: Any) -> None:
    settings_store.set(key, str(value))


def _env_base_url() -> str:
    return (_text(settings.llm_base_url) or DEFAULT_BASE_URL).rstrip("/")


def _env_model() -> str:
    return _text(settings.llm_model) or DEFAULT_MODEL


def _env_api_key() -> str:
    return _text(settings.llm_api_key)


def mask_key(key: str) -> str:
    """密钥的展示形式：有密钥只给 sk-***，没有给空串。"""
    return KEY_MASK if _text(key) else ""


# ---------------------------------------------------------------- profile 存储


def _norm_profile(raw: Any) -> dict | None:
    """把库里的一行 JSON 规范成 profile；脏数据返回 None 直接丢弃。"""
    if not isinstance(raw, dict):
        return None
    pid = _text(raw.get("id"))
    if not pid:
        return None
    provider = _text(raw.get("provider")).lower() or DEFAULT_PROVIDER
    return {
        "id": pid,
        "name": _text(raw.get("name")) or PRESETS.get(provider, {}).get("name") or pid,
        "provider": provider,
        "base_url": _text(raw.get("base_url")).rstrip("/"),
        "model": _text(raw.get("model")),
        "temperature": _float_or(raw.get("temperature"), settings.llm_temperature),
        "max_tokens": _int_or(raw.get("max_tokens"), settings.llm_max_tokens),
        "requires_key": _bool_or(raw.get("requires_key"), True),
    }


def _read_profiles() -> list[dict]:
    raw = _setting(KEY_PROFILES)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("settings.%s 不是合法 JSON，按空处理", KEY_PROFILES)
        return []
    if not isinstance(data, list):
        logger.warning("settings.%s 顶层不是数组，按空处理", KEY_PROFILES)
        return []
    profiles: list[dict] = []
    seen: set[str] = set()
    for item in data:
        profile = _norm_profile(item)
        if profile is None or profile["id"] in seen:
            continue
        seen.add(profile["id"])
        profiles.append(profile)
    return profiles


def _write_profiles(profiles: list[dict]) -> None:
    _set_setting(
        KEY_PROFILES, json.dumps(list(profiles), ensure_ascii=False, sort_keys=True)
    )


def _seed_profile() -> dict:
    """首次访问用的种子 profile：完全由环境变量 / 内置默认生成。"""
    return {
        "id": DEFAULT_PROFILE_ID,
        "name": DEFAULT_PROFILE_NAME,
        "provider": DEFAULT_PROVIDER,
        "base_url": _env_base_url(),
        "model": _env_model(),
        "temperature": _float_or(settings.llm_temperature, 0.3),
        "max_tokens": _int_or(settings.llm_max_tokens, 2000),
        "requires_key": True,
    }


def _profiles() -> list[dict]:
    """读 profile 列表；一个都没有时按环境变量种子落库（不写密钥）。"""
    profiles = _read_profiles()
    if profiles:
        return profiles
    seed = _seed_profile()
    try:
        _write_profiles([seed])
        if not _setting(KEY_ACTIVE):
            _set_setting(KEY_ACTIVE, seed["id"])
        logger.info("首次访问：已生成默认 LLM 配置 %s（来自环境变量）", seed["id"])
    except Exception:
        logger.warning("写入首个 LLM 配置失败，本次仅在内存中生效", exc_info=True)
    return [seed]


def _active_id(profiles: list[dict]) -> str:
    """生效 id；指向不存在的 profile 时自动纠正为第一个。"""
    ids = [profile["id"] for profile in profiles]
    active = _setting(KEY_ACTIVE)
    if active in ids:
        return active
    fallback = ids[0]
    if active != fallback:
        logger.info("llm_active=%r 不在配置列表中，已回落为 %s", active, fallback)
        try:
            _set_setting(KEY_ACTIVE, fallback)
        except Exception:
            logger.debug("纠正 llm_active 失败", exc_info=True)
    return fallback


def _resolve_base_url(profile: dict) -> str:
    """profile 非空 base_url > settings 覆盖 > env > 内置默认。"""
    value = (
        _text(profile.get("base_url"))
        or _setting("llm_base_url")
        or _env_base_url()
        or DEFAULT_BASE_URL
    )
    return value.rstrip("/") or DEFAULT_BASE_URL


def _resolve_model(profile: dict) -> str:
    """profile 非空 model > settings 覆盖 > env > 内置默认。"""
    return (
        _text(profile.get("model"))
        or _setting("llm_model")
        or _env_model()
        or DEFAULT_MODEL
    )


def _key_for(profile: dict, base_url: str) -> str:
    """profile 自己的密钥优先；否则只有同一 base_url 才回落到环境变量密钥。"""
    pid = _text(profile.get("id"))
    stored = secrets_store.get_api_key(pid) if pid else ""
    if stored:
        return stored
    env_key = _env_api_key()
    if env_key and base_url == _env_base_url():
        return env_key
    return ""


def _resolve(profile: dict) -> dict:
    """把 profile 解析成可直接发请求的生效参数。"""
    base_url = _resolve_base_url(profile)
    # api_key 字段只会由 health(override={"api_key": ...}) 临时挂到 profile 上：
    # 一次测试用的密钥，不写 secrets.json、不改任何持久状态，用完即丢。
    key = _text(profile.get("api_key")) or _key_for(profile, base_url)
    return {
        "id": _text(profile.get("id")),
        "name": _text(profile.get("name")) or _text(profile.get("id")),
        "provider": _text(profile.get("provider")).lower() or DEFAULT_PROVIDER,
        "base_url": base_url,
        "model": _resolve_model(profile),
        "api_key": key,
        "has_key": bool(key),
        "temperature": _float_or(profile.get("temperature"), settings.llm_temperature),
        "max_tokens": _int_or(profile.get("max_tokens"), settings.llm_max_tokens),
        "requires_key": _bool_or(profile.get("requires_key"), True),
    }


def _env_only_profile() -> dict:
    """库暂时不可用时的兜底 profile：完全按环境变量。"""
    return {
        "id": "",
        "name": "环境变量",
        "provider": DEFAULT_PROVIDER,
        "base_url": _env_base_url(),
        "model": _env_model(),
        "temperature": _float_or(settings.llm_temperature, 0.3),
        "max_tokens": _int_or(settings.llm_max_tokens, 2000),
        "requires_key": True,
    }


def _decorate(profile: dict, active_id: str) -> dict:
    """给列表/详情补 has_key / key_masked / active（不含明文密钥）。"""
    cfg = _resolve(profile)
    item = dict(profile)
    item["has_key"] = cfg["has_key"]
    item["key_masked"] = KEY_MASK if cfg["has_key"] else ""
    item["active"] = profile["id"] == active_id
    return item


def _slugify(name: str, existing: set[str]) -> str:
    """按名称生成不重复的 slug id。"""
    base = _SLUG_STRIP_RE.sub("-", _text(name).lower()).strip("-")[:32]
    if not base or not _ID_RE.match(base):
        base = "profile"
    candidate = base
    index = 2
    while candidate in existing:
        candidate = "%s-%s" % (base, index)
        index += 1
    return candidate


# ---------------------------------------------------------------- 公开 API：配置管理


def list_profiles() -> list[dict]:
    """全部 profile，每项含 id,name,provider,base_url,model,temperature,max_tokens,requires_key,has_key,active。"""
    profiles = _profiles()
    active = _active_id(profiles)
    return [_decorate(profile, active) for profile in profiles]


def get_profile(pid: str) -> dict | None:
    """按 id 取单个 profile；不存在返回 None。"""
    target = _text(pid)
    if not target:
        return None
    profiles = _profiles()
    active = _active_id(profiles)
    for profile in profiles:
        if profile["id"] == target:
            return _decorate(profile, active)
    return None


def active_profile() -> dict:
    """当前生效的 profile（永远有返回值）。"""
    profiles = _profiles()
    active = _active_id(profiles)
    for profile in profiles:
        if profile["id"] == active:
            return _decorate(profile, active)
    return _decorate(profiles[0], profiles[0]["id"])


def set_active(pid: str) -> None:
    """切换生效 profile；id 不存在抛 ValueError。"""
    target = _text(pid)
    profiles = _profiles()
    if target not in {profile["id"] for profile in profiles}:
        raise ValueError("配置 %s 不存在" % (target or "（空）"))
    _set_setting(KEY_ACTIVE, target)
    logger.info("LLM 生效配置已切换为 %s", target)


def find_profile_id(data: dict) -> str | None:
    """按 id（其次按同名 / 同 slug）定位已有 profile；不存在返回 None。"""
    if not isinstance(data, dict):
        return None
    profiles = _profiles()
    ids = {profile["id"] for profile in profiles}
    pid = _text(data.get("id"))
    if pid:
        return pid if pid in ids else None
    name = _text(data.get("name")).casefold()
    if not name:
        return None
    for profile in profiles:
        if profile["name"].casefold() == name or profile["id"] == name:
            return profile["id"]
    return None


def _values_from(data: dict, base: dict | None) -> dict:
    """把请求里的可编辑字段与 base 合并；未出现的字段沿用 base / 预设。"""
    current = dict(base or {})
    preset_key = _text(data.get("provider")).lower() or _text(current.get("provider")).lower()
    preset = PRESETS.get(preset_key, {})

    def pick(field: str) -> tuple[Any, bool]:
        if field in data and data[field] is not None:
            return data[field], True
        return current.get(field), False

    provider = _text(pick("provider")[0]).lower() or DEFAULT_PROVIDER
    base_url_value, base_url_given = pick("base_url")
    model_value, model_given = pick("model")
    base_url = _text(base_url_value).rstrip("/")
    model = _text(model_value)
    if not base_url and not base_url_given:
        base_url = _text(preset.get("base_url")).rstrip("/")
    if not model and not model_given:
        model = _text(preset.get("model"))
    name = _text(pick("name")[0]) or _text(preset.get("name")) or provider
    return {
        "name": name,
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "temperature": pick("temperature")[0],
        "max_tokens": pick("max_tokens")[0],
        "requires_key": _bool_or(
            pick("requires_key")[0], bool(preset.get("requires_key", True))
        ),
    }


def _check_values(values: dict) -> dict:
    """校验并规范化可编辑字段；非法参数抛 ValueError（API 转 400）。"""
    name = _text(values.get("name"))
    provider = _text(values.get("provider")).lower() or DEFAULT_PROVIDER
    base_url = _text(values.get("base_url")).rstrip("/")
    model = _text(values.get("model"))
    if base_url and not base_url.lower().startswith(("http://", "https://")):
        raise ValueError("接口地址必须是以 http:// 或 https:// 开头的完整 URL")
    return {
        "name": name or provider,
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "temperature": _float_or(
            values.get("temperature"), settings.llm_temperature, strict=True
        ),
        "max_tokens": _int_or(values.get("max_tokens"), settings.llm_max_tokens, strict=True),
        "requires_key": _bool_or(values.get("requires_key"), True),
    }


def upsert_profile(data: dict) -> str:
    """新建或更新 profile（data 不含 api_key），返回 profile id。"""
    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON 对象")
    profiles = _profiles()
    existing_id = find_profile_id(data)
    base = next((profile for profile in profiles if profile["id"] == existing_id), None)

    if base is None and not any(
        _text(data.get(field)) for field in ("name", "provider", "base_url", "model")
    ):
        raise ValueError("至少要提供名称、provider、接口地址、模型之一")

    values = _check_values(_values_from(data, base))

    if base is None:
        raw_id = _text(data.get("id"))
        pid = raw_id or _slugify(values["name"], {profile["id"] for profile in profiles})
        if not _ID_RE.match(pid):
            raise ValueError(
                "配置 id 只能由小写字母、数字、下划线、连字符组成（1~48 字，且不以连字符开头）"
            )
        if any(profile["id"] == pid for profile in profiles):
            raise ValueError("配置 id %s 已存在" % pid)
        profile = {"id": pid, **values}
        profiles.append(profile)
        action = "新建"
    else:
        profile = {"id": base["id"], **values}
        profiles = [profile if item["id"] == base["id"] else item for item in profiles]
        pid = profile["id"]
        action = "更新"

    _write_profiles(profiles)
    if not _setting(KEY_ACTIVE):
        _set_setting(KEY_ACTIVE, pid)
    logger.info("LLM 配置已%s：%s（%s）", action, pid, profile["provider"])
    return pid


def delete_profile(pid: str) -> None:
    """删除 profile 并清理其密钥；删掉最后一个时自动回落到环境变量兜底。"""
    target = _text(pid)
    profiles = _profiles()
    if target not in {profile["id"] for profile in profiles}:
        raise ValueError("配置 %s 不存在" % (target or "（空）"))

    try:
        secrets_store.delete_api_key(target)
    except Exception:
        logger.warning("删除配置 %s 的密钥失败", target, exc_info=True)

    remaining = [profile for profile in profiles if profile["id"] != target]
    active = _setting(KEY_ACTIVE)
    if not remaining:
        seed = _seed_profile()
        _write_profiles([seed])
        _set_setting(KEY_ACTIVE, seed["id"])
        logger.info("已删除最后一个 LLM 配置 %s，回落到环境变量兜底配置", target)
        return

    _write_profiles(remaining)
    if active == target:
        _set_setting(KEY_ACTIVE, remaining[0]["id"])
        logger.info("生效配置 %s 已删除，自动切换为 %s", target, remaining[0]["id"])


def save_api_key(pid: str, key: str) -> None:
    """保存或清除某个 profile 的密钥（空字符串=清除）；只在 secrets.json 落地。"""
    target = _text(pid)
    if not target:
        raise ValueError("保存密钥必须提供配置 id")
    if target not in {profile["id"] for profile in _profiles()}:
        raise ValueError("配置 %s 不存在" % target)
    secret = _text(key)
    if secret:
        secrets_store.set_api_key(target, secret)
        logger.info("已保存配置 %s 的密钥（%s）", target, KEY_MASK)
    else:
        secrets_store.delete_api_key(target)
        logger.info("已清除配置 %s 的密钥", target)


def resolve_config() -> dict:
    """生效参数：profiles(DB) > settings 覆盖 > env > 默认，并带上解析后的密钥。"""
    try:
        profiles = _profiles()
        active = _active_id(profiles)
        profile = next(
            (item for item in profiles if item["id"] == active), profiles[0]
        )
        return _resolve(profile)
    except Exception:
        logger.warning("解析 LLM 生效配置失败，回退环境变量", exc_info=True)
        return _resolve(_env_only_profile())


def _is_mock(cfg: dict) -> bool:
    """requires_key 为真且没有密钥才算 MOCK；本地模型（ollama）无 key 也算 live。"""
    if not cfg.get("requires_key", True):
        return False
    return not _text(cfg.get("api_key"))


def is_mock_mode() -> bool:
    """是否处于 MOCK 模式（不发起任何网络请求）。"""
    try:
        return _is_mock(resolve_config())
    except Exception:
        logger.warning("判断 LLM 模式失败，按 MOCK 处理", exc_info=True)
        return not _env_api_key()


# 兼容改造前模块级函数（其它域若引用，语义不变）
def api_key() -> str:
    return _text(resolve_config().get("api_key"))


def base_url() -> str:
    return _text(resolve_config().get("base_url"))


def model_name() -> str:
    return _text(resolve_config().get("model"))


# ---------------------------------------------------------------- HTTP 调用


def _redact(text: Any, key: str = "") -> str:
    """把密钥从任何外泄文本里抹掉（优先用本次实际使用的 key）。"""
    value = str(text or "")
    secret = _text(key) or _env_api_key()
    if not secret:
        return value
    value = value.replace(secret, KEY_MASK)
    if len(secret) >= 8:
        value = value.replace(secret[:8], KEY_MASK)
    return value


def _snippet(text: Any, limit: int = SNIPPET_LIMIT, key: str = "") -> str:
    flat = " ".join(_redact(text, key).split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _mock_text(cfg: dict, prompt: str, system: str | None) -> str:
    """MOCK 下的诚实占位说明：明确写着这不是模型产出。"""
    return "\n".join(
        [
            MOCK_NOTICE,
            "",
            "当前生效的大模型配置没有可用密钥，本客户端没有发起任何网络请求，也没有可用的模型产出。",
            "",
            "- 配置名称：%s（id=%s）" % (cfg.get("name") or "—", cfg.get("id") or "—"),
            "- 接口地址：%s" % (cfg.get("base_url") or "—"),
            "- 模型名：%s" % (cfg.get("model") or "—"),
            "- system 提示摘要：%s" % _snippet(system or "（未提供）", 120),
            "- 用户提示长度：%s 字符" % len(prompt or ""),
            "",
            "需要报告正文时请调用 reports.generate：MOCK 模式下它用本地模板基于真实入库数据渲染，"
            "并在正文首行标注 MOCK。",
        ]
    )


async def _post_chat(
    cfg: dict,
    messages: list[dict[str, str]],
    *,
    max_tokens: int | None = None,
    timeout: float | None = None,
    retries: int = 1,
) -> dict[str, Any]:
    """POST {base_url}/chat/completions，失败重试 retries 次，返回解析后的 JSON。"""
    secret = _text(cfg.get("api_key"))
    endpoint = (_text(cfg.get("base_url")) or "").rstrip("/")
    if not endpoint:
        raise LLMError("未配置接口地址（base_url）：请在设置页填写大模型配置")
    if cfg.get("requires_key", True) and not secret:
        raise LLMError("未配置 API Key：当前为 MOCK 模式，不能调用真实大模型")

    url = "%s/chat/completions" % endpoint
    payload: dict[str, Any] = {
        "model": cfg.get("model") or DEFAULT_MODEL,
        "messages": messages,
        "temperature": _float_or(cfg.get("temperature"), settings.llm_temperature),
        "max_tokens": _int_or(
            max_tokens if max_tokens is not None else cfg.get("max_tokens"),
            settings.llm_max_tokens,
        ),
    }
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = "Bearer %s" % secret
    timeout_s = float(timeout if timeout is not None else settings.llm_timeout)
    attempts = max(1, retries + 1)
    last_error = "未知错误"

    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            last_error = "请求超时（%g 秒）" % timeout_s
        except httpx.HTTPError as exc:
            last_error = "网络错误 %s：%s" % (
                type(exc).__name__,
                _snippet(str(exc), key=secret),
            )
        else:
            if response.status_code >= 400:
                last_error = "HTTP %s：%s" % (
                    response.status_code,
                    _snippet(response.text, key=secret),
                )
            else:
                try:
                    data = response.json()
                except ValueError:
                    last_error = "响应不是合法 JSON：%s" % _snippet(
                        response.text, key=secret
                    )
                else:
                    if isinstance(data, dict):
                        return data
                    last_error = "响应结构异常（顶层不是对象）：%s" % _snippet(
                        response.text, key=secret
                    )
        if attempt < attempts:
            logger.warning(
                "LLM 调用失败（第 %s/%s 次）：%s；%.1f 秒后重试",
                attempt,
                attempts,
                last_error,
                RETRY_DELAY,
            )
            await asyncio.sleep(RETRY_DELAY)

    retried = "（已重试 %s 次）" % retries if retries else ""
    raise LLMError(
        "大模型调用失败%s：%s｜接口地址 %s" % (retried, _redact(last_error, secret), url)
    )


def _extract_text(data: dict[str, Any], cfg: dict) -> str:
    """解析 choices[0].message.content。"""
    secret = _text(cfg.get("api_key"))
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError(
            "大模型响应缺少 choices：%s"
            % _snippet(json.dumps(data, ensure_ascii=False), key=secret)
        )
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    text = _text(content)
    if not text:
        raise LLMError(
            "大模型返回内容为空：%s"
            % _snippet(json.dumps(data, ensure_ascii=False), key=secret)
        )
    return text


async def generate(prompt: str, system: str | None = None) -> LLMResult:
    """调用 chat/completions 生成文本；MOCK 模式直接返回带声明的说明文本。"""
    cfg = resolve_config()
    if _is_mock(cfg):
        return LLMResult(
            text=_mock_text(cfg, prompt, system),
            provider=MOCK_PROVIDER,
            model=MOCK_MODEL,
            is_mock=True,
        )

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt or ""})

    data = await _post_chat(cfg, messages)
    text = _extract_text(data, cfg)
    model = _text(data.get("model")) or _text(cfg.get("model")) or DEFAULT_MODEL
    return LLMResult(
        text=text, provider=_text(cfg.get("provider")) or PROVIDER, model=model, is_mock=False
    )


# ---------------------------------------------------------------- 连接测试


def _override_api_key(override: dict) -> str:
    """override 里的一次性密钥：本次测试专用，不落盘、不进 secrets.json。"""
    raw = override.get("api_key")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("override.api_key 必须是字符串")
    return raw.strip()


def _pick_profile(profile_id: Any = None, override: Any = None) -> dict:
    """挑一个 profile 供测试：指定 id > 生效 profile；override 做一次性覆盖。"""
    profiles = _profiles()
    active = _active_id(profiles)
    target = _text(profile_id)
    profile: dict | None = None
    if target:
        profile = next((item for item in profiles if item["id"] == target), None)
        if profile is None:
            raise ValueError("配置 %s 不存在" % target)
    if profile is None:
        profile = next((item for item in profiles if item["id"] == active), profiles[0])
    if not override:
        return profile
    if not isinstance(override, dict):
        raise ValueError("override 必须是 JSON 对象")
    values = _check_values(_values_from(override, profile))
    merged = {**profile, **values}
    secret = _override_api_key(override)
    if secret:
        merged["api_key"] = secret
    return merged


def _health_payload(
    mode: str,
    cfg: dict,
    *,
    ok: bool,
    error: str | None,
    latency_ms: int,
    reply_snippet: str = "",
    model: str = "",
) -> dict:
    return {
        "mode": mode,
        "provider": _text(cfg.get("provider")),
        "model": _text(model) or _text(cfg.get("model")),
        "base_url": _text(cfg.get("base_url")),
        "profile_id": _text(cfg.get("id")),
        "ok": ok,
        "error": error,
        "latency_ms": int(latency_ms),
        "reply_snippet": reply_snippet,
    }


async def health(profile_id: Any = None, override: dict | None = None) -> dict:
    """连通性测试；override 支持「不改配置先试一次」。

    override 可含 api_key：非空时用它发起本次测试（不落盘），空/缺省时照旧走已存密钥 / env。
    返回值与错误信息一律脱敏，绝不出现明文密钥。永不抛异常。
    """
    try:
        profile = _pick_profile(profile_id, override)
    except ValueError as exc:
        return _health_payload("mock", {}, ok=False, error=str(exc), latency_ms=0)
    except Exception as exc:  # 库不可用等
        logger.exception("读取 LLM 配置失败")
        return _health_payload(
            "mock", {}, ok=False, error="读取配置失败：%s" % type(exc).__name__, latency_ms=0
        )

    cfg = _resolve(profile)
    if _is_mock(cfg):
        return _health_payload("mock", cfg, ok=True, error=None, latency_ms=0)

    started = time.perf_counter()
    probe_timeout = min(float(settings.llm_timeout or 30), 20.0)
    probe_tokens = max(1, min(_int_or(cfg.get("max_tokens"), 16), 16))
    try:
        data = await _post_chat(
            cfg,
            [{"role": "user", "content": "ping"}],
            max_tokens=probe_tokens,
            timeout=probe_timeout,
            retries=0,
        )
        reply = _extract_text(data, cfg)
    except LLMError as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        return _health_payload(
            "live", cfg, ok=False, error=str(exc), latency_ms=elapsed
        )
    except Exception as exc:  # 兜底：健康检查绝不让接口 500
        logger.exception("LLM 连接测试异常")
        elapsed = int((time.perf_counter() - started) * 1000)
        return _health_payload(
            "live",
            cfg,
            ok=False,
            error="连接测试异常：%s %s"
            % (type(exc).__name__, _snippet(str(exc), key=cfg.get("api_key"))),
            latency_ms=elapsed,
        )

    elapsed = int((time.perf_counter() - started) * 1000)
    return _health_payload(
        "live",
        cfg,
        ok=True,
        error=None,
        latency_ms=elapsed,
        reply_snippet=_snippet(reply, REPLY_SNIPPET_LIMIT),
        model=_text(data.get("model")),
    )
