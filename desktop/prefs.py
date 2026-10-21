"""桌面版偏好设置（不依赖 Qt/tk，纯标准库，便于单测）。

存放位置：%APPDATA%\WorkbenchDesktop\prefs.json
数据目录由用户首次启动时选择，之后可在设置页/托盘菜单修改。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

APP_DIR_NAME = "WorkbenchDesktop"
PREFS_NAME = "prefs.json"


def appdata_dir() -> Path:
    """%APPDATA%\WorkbenchDesktop（非 Windows 或缺失时退化为用户主目录）。"""
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home())
    return Path(base) / APP_DIR_NAME


def prefs_path() -> Path:
    return appdata_dir() / PREFS_NAME


@dataclass
class Prefs:
    """桌面壳的全部可调项。字段名与设置页表单一一对应。"""

    data_dir: str = ""
    first_run_done: bool = False
    close_to_tray: bool = True
    autostart: bool = False
    notify: bool = True
    port: int = 0
    window_width: int = 1280
    window_height: int = 860

    def normalized(self) -> "Prefs":
        data = asdict(self)
        data["data_dir"] = str(self.data_dir or "").strip()
        for key in ("close_to_tray", "autostart", "notify", "first_run_done"):
            data[key] = bool(data[key])
        try:
            data["port"] = max(0, min(int(data["port"] or 0), 65535))
        except (TypeError, ValueError):
            data["port"] = 0
        for key, low, high in (("window_width", 900, 4000), ("window_height", 600, 3000)):
            try:
                data[key] = max(low, min(int(data[key]), high))
            except (TypeError, ValueError):
                data[key] = 1280 if key == "window_width" else 860
        return Prefs(**data)


def load(path: Path | None = None) -> Prefs:
    """读取偏好；文件缺失/损坏都返回默认值，绝不抛异常。"""
    target = path or prefs_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Prefs()
    except Exception:
        logger.warning("偏好文件损坏，使用默认值：%s", target, exc_info=True)
        return Prefs()
    if not isinstance(raw, dict):
        return Prefs()
    known = {f for f in Prefs().__dataclass_fields__}
    data = {k: v for k, v in raw.items() if k in known}
    try:
        return Prefs(**data).normalized()
    except Exception:
        logger.warning("偏好字段非法，使用默认值", exc_info=True)
        return Prefs()


def save(prefs: Prefs, path: Path | None = None) -> Path:
    """原子写（先写临时文件再 replace），避免半截 JSON。"""
    target = path or prefs_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(asdict(prefs.normalized()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target
