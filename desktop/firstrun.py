"""首次启动的数据目录选择（tkinter，标准库，不额外引入 GUI 依赖）。

拿不到图形环境（例如 CI、无桌面会话）时退回默认目录，绝不阻塞启动。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from app import brand

logger = logging.getLogger(__name__)


def default_data_dir() -> Path:
    """默认数据目录：可执行文件同目录的 data\\（便携版语义）。

    打包后必须用 sys.executable 定位：PyInstaller 会把 __file__ 指到临时解包目录（_MEIPASS），
    那里程序退出即删除——若把默认目录定在那儿，用户一选就等于把数据写进一次性目录。
    """
    override = os.environ.get("WB_PROGRAM_DIR")
    if override:
        return Path(override) / "data"
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path(__file__).resolve().parent.parent / "data"


def ask_directory(title: str, initial: Path | None = None) -> str | None:
    """弹目录选择框；用户取消或环境不支持返回 None。"""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except Exception:
        logger.warning("没有 tkinter，跳过目录选择对话框")
        return None

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.title(title)
        root.attributes("-topmost", True)
        if initial:
            messagebox.showinfo(
                title,
                "请选择" + brand.NAME + "的数据目录。\n\n数据库（app.db）与配置都会放在这个目录里，"
                "建议放在非系统盘、且你有写权限的地方。\n\n默认位置：%s" % initial,
                parent=root,
            )
        chosen = filedialog.askdirectory(
            title=title,
            initialdir=str(initial or default_data_dir()),
            mustexist=False,
            parent=root,
        )
        return chosen or None
    except Exception:
        logger.warning("目录选择对话框失败", exc_info=True)
        return None
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass


def ensure_dir(path: str | os.PathLike) -> Path:
    target = Path(path).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return target
