"""开机自启：只写当前用户的 Run 键（HKCU），不需要管理员权限。

命令形如：  "C:\Path\Workbench.exe" --autostart
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "WorkbenchDesktop"


def _command() -> str:
    """开机启动命令：打包后是 exe 本体，开发态是 python -m desktop。"""
    if getattr(sys, "frozen", False):
        return '"%s" --autostart' % Path(sys.executable)
    return '"%s" -m desktop --autostart' % Path(sys.executable)


def enabled() -> bool:
    """当前用户是否已加入开机启动。非 Windows / 无权限一律返回 False。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(str(value).strip())
    except FileNotFoundError:
        return False
    except Exception:
        logger.debug("读取开机启动项失败", exc_info=True)
        return False


def set_enabled(on: bool) -> bool:
    """开关开机自启；返回是否与请求一致（失败返回 False，不抛异常）。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if on:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return enabled() == on
    except Exception:
        logger.warning("写开机启动项失败", exc_info=True)
        return False
