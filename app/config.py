"""环境变量配置汇总（SPEC.md 第 3 节）。

密钥只从环境 / .env 读取，绝不写库、绝不进 git。
DATA_DIR 保持为 Path，业务代码统一用 settings.data_dir / settings.db_path。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

load_dotenv(ROOT_DIR / ".env")


def _str(name: str, default: str) -> str:
    """读字符串环境变量；未设置或空白则用默认值。"""
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int(name: str, default: int) -> int:
    try:
        return int(float(_str(name, str(default))))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except (TypeError, ValueError):
        return default


def _flag(name: str, default: bool = False) -> bool:
    """布尔开关：1/true/on/yes 视为真（大小写不敏感）。"""
    value = _str(name, "1" if default else "0").strip().lower()
    return value in {"1", "true", "on", "yes"}


def _path(name: str, default: str, base: Path | None = None) -> Path:
    path = Path(_str(name, default)).expanduser()
    if base is not None and not path.is_absolute():
        path = base / path
    return path


def _load_secret(data_dir: Path) -> str:
    """APP_SECRET：优先环境变量，其次 DATA_DIR/.secret（不存在则生成 32 字节 hex 落盘）。"""
    env_secret = _str("APP_SECRET", "")
    if env_secret:
        return env_secret
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_file = data_dir / ".secret"
    try:
        existing = secret_file.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        existing = ""
    if existing:
        return existing
    secret = secrets.token_hex(32)
    secret_file.write_text(secret + "\n", encoding="utf-8")
    try:
        secret_file.chmod(0o600)
    except OSError:
        pass
    return secret


@dataclass(frozen=True)
class Settings:
    """SPEC.md 第 3 节全部环境变量。"""

    data_dir: Path
    tz_name: str
    app_password: str
    app_secret: str
    vault_dir: Path
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout: int
    llm_max_tokens: int
    llm_temperature: float
    remind_time: str
    webhook_url: str
    port: int
    # HTTPS 反代下置 1：会话 Cookie 带 Secure，浏览器才不会明文回传（W-02）
    secure_cookie: bool = False
    # docx 导出总开关（W-01）；关闭后导出路由 404、页面不显示按钮
    export_docx: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def auth_enabled(self) -> bool:
        """APP_PASSWORD 非空才启用口令登录。"""
        return bool(self.app_password)

    @property
    def llm_mock(self) -> bool:
        """LLM_API_KEY 为空即 MOCK 模式。"""
        return not self.llm_api_key

    @property
    def vault_exists(self) -> bool:
        return self.vault_dir.is_dir()


def load_settings() -> Settings:
    data_dir = _path("DATA_DIR", "./data", base=ROOT_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        tz_name=_str("TZ_NAME", "Asia/Shanghai"),
        app_password=_str("APP_PASSWORD", ""),
        app_secret=_load_secret(data_dir),
        vault_dir=_path("VAULT_DIR", "/vault"),
        llm_base_url=_str("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        llm_api_key=_str("LLM_API_KEY", ""),
        llm_model=_str("LLM_MODEL", "deepseek-chat"),
        llm_timeout=_int("LLM_TIMEOUT", 120),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 2000),
        llm_temperature=_float("LLM_TEMPERATURE", 0.3),
        remind_time=_str("REMIND_TIME", "09:00"),
        webhook_url=_str("WEBHOOK_URL", ""),
        port=_int("PORT", 8080),
        secure_cookie=_flag("SECURE_COOKIE", False),
        export_docx=_flag("EXPORT_DOCX", True),
    )


settings = load_settings()


def get_settings() -> Settings:
    """返回模块级配置单例。"""
    return settings
