"""桌面桥接后端的实现：设置读写、数据目录切换、重启。

被 app/services/desktop_bridge.set_backend() 注入；所有方法都不抛异常给 Web 层，
失败一律返回 {"ok": False, "error": ...}，由页面提示用户。
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from desktop import __version__, autostart, prefs as prefs_mod

logger = logging.getLogger(__name__)


class DesktopBackend:
    def __init__(self, prefs: prefs_mod.Prefs, handle, *, on_change=None, on_restart=None):
        self._prefs = prefs
        self._handle = handle
        self._on_change = on_change
        self._on_restart = on_restart
        self._lock = threading.Lock()
        self.window = None

    # ---------------- 状态 ----------------
    def state(self) -> dict:
        return {
            "ok": True,
            "version": __version__,
            "data_dir": self._prefs.data_dir,
            "port": self._handle.port if self._handle else 0,
            "base_url": self._handle.base_url if self._handle else "",
            "close_to_tray": self._prefs.close_to_tray,
            "autostart": self._prefs.autostart,
            "notify": self._prefs.notify,
            "launch_mode": "安装版" if getattr(sys, "frozen", False) else "开发态（python -m desktop）",
        }

    # ---------------- 设置 ----------------
    def update_prefs(self, **changes) -> dict:
        with self._lock:
            data = asdict(self._prefs)
            for key, value in changes.items():
                if key in data and value is not None:
                    data[key] = value
            updated = prefs_mod.Prefs(**data).normalized()

            if bool(updated.autostart) != bool(self._prefs.autostart):
                ok = autostart.set_enabled(updated.autostart)
                if not ok:
                    return {"ok": False, "error": "写开机启动项失败（可能被安全软件拦截）"}

            prefs_mod.save(updated)
            self._prefs = updated
            notify_change = self._on_change
        if callable(notify_change):
            try:
                notify_change(updated)
            except Exception:
                logger.debug("变更回调失败", exc_info=True)
        return {"ok": True, "state": self.state()}

    # ---------------- 数据目录 ----------------
    def change_data_dir(self, path: str, mode: str = "migrate") -> dict:
        target = Path(str(path or "").strip().strip('"')).expanduser()
        if not str(target):
            return {"ok": False, "error": "目录为空"}
        try:
            target = target.resolve()
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return {"ok": False, "error": "无法创建目录：%s" % str(exc)[:120]}

        current = Path(self._prefs.data_dir or self._handle.data_dir)
        if target == current.resolve():
            return {"ok": False, "error": "与当前数据目录相同"}

        if mode == "migrate":
            source = current / "app.db"
            if not source.exists():
                return {"ok": False, "error": "当前目录没有 app.db，无法迁移"}
            try:
                temp = target / "app.db.migrating"
                shutil.copy2(source, temp)
                conn = sqlite3.connect(str(temp))
                try:
                    check = conn.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    conn.close()
                if check != "ok":
                    temp.unlink(missing_ok=True)
                    return {"ok": False, "error": "复制后的数据库校验失败：%s" % check[:80]}
                os.replace(temp, target / "app.db")
                for extra in (".secret", "secrets.json"):
                    src = current / extra
                    if src.exists():
                        shutil.copy2(src, target / extra)
            except Exception as exc:
                logger.exception("迁移数据目录失败")
                return {"ok": False, "error": "迁移失败：%s" % str(exc)[:120]}

        with self._lock:
            data = asdict(self._prefs)
            data["data_dir"] = str(target)
            updated = prefs_mod.Prefs(**data).normalized()
            prefs_mod.save(updated)
            self._prefs = updated
        return {
            "ok": True,
            "restart_required": True,
            "data_dir": str(target),
            "note": "旧目录已保留，确认新目录可用后可自行删除",
        }

    # ---------------- 重启 ----------------
    def request_restart(self) -> None:
        """重启桌面进程（数据目录切换后生效）。"""
        logger.info("用户请求重启应用")
        if callable(self._on_restart):
            try:
                self._on_restart()
                return
            except Exception:
                logger.exception("重启回调失败，走默认重启路径")
        try:
            if getattr(sys, "frozen", False):
                args = [sys.executable] + [a for a in sys.argv[1:] if a != "--autostart"]
            else:
                args = [sys.executable, "-m", "desktop"] + [a for a in sys.argv[1:] if a != "--autostart"]
            subprocess.Popen(args, close_fds=True)
        except Exception:
            logger.exception("拉起新进程失败")
        finally:
            os._exit(0)
