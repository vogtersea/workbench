"""桌面版入口：单实例 → 选数据目录 → 起本地服务 → 开窗口 → 托盘常驻。

启动顺序很重要：
1) 先确定数据目录并写入环境变量
2) 再 import/启动 app（app.config 在导入期读取环境）
3) 最后注入桌面桥接后端，让设置页/托盘能读写桌面状态
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from desktop import __version__, firstrun, notify, prefs as prefs_mod, server as server_mod, tray as tray_mod

logger = logging.getLogger("desktop")

MUTEX_NAME = "WorkbenchDesktop.SingleInstance"
WINDOW_TITLE = "工作台"


def _setup_logging() -> Path:
    """日志写到 %APPDATA%\WorkbenchDesktop\logs\desktop.log，排障时直接看这个文件。"""
    log_dir = prefs_mod.appdata_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "desktop.log"
    handlers = [logging.FileHandler(log_file, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_file


def _single_instance():
    """Windows 命名互斥体；已有实例时返回 None（调用方应提示并退出）。"""
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, wintypes.BOOL(False), MUTEX_NAME)
        already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        return None if already else handle
    except Exception:
        logger.debug("单实例检测不可用，继续启动", exc_info=True)
        return True


def _resolve_data_dir(prefs: prefs_mod.Prefs, *, interactive: bool = True) -> Path:
    """首次启动让用户选目录；已有配置直接用。取消选择时退回默认目录。"""
    if prefs.data_dir and prefs.first_run_done:
        return firstrun.ensure_dir(prefs.data_dir)

    default = firstrun.default_data_dir()
    chosen = firstrun.ask_directory("选择工作台数据目录", default) if interactive else None
    target = firstrun.ensure_dir(chosen or default)

    prefs.data_dir = str(target)
    prefs.first_run_done = True
    prefs_mod.save(prefs)
    logger.info("数据目录已确定：%s（首次运行）", target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workbench", description="工作台桌面版")
    parser.add_argument("--autostart", action="store_true", help="开机自启模式（不弹首次对话框、最小化到托盘）")
    parser.add_argument("--data-dir", default="", help="临时覆盖数据目录（排障用）")
    args, _unknown = parser.parse_known_args(argv)

    log_file = _setup_logging()
    logger.info("工作台桌面版 v%s 启动（python %s）", __version__, sys.version.split()[0])

    if _single_instance() is None:
        logger.info("已有实例在运行，本次启动退出")
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(WINDOW_TITLE, "工作台已经在运行了。\n请在系统托盘里找到它的图标。")
            root.destroy()
        except Exception:
            pass
        return 0

    prefs = prefs_mod.load()
    data_dir = (
        firstrun.ensure_dir(args.data_dir)
        if args.data_dir
        else _resolve_data_dir(prefs, interactive=not args.autostart)
    )

    try:
        handle = server_mod.start(data_dir, prefs.port)
    except Exception as exc:
        logger.exception("本地服务启动失败")
        _fatal("本地服务启动失败：%s\n\n日志：%s" % (exc, log_file))
        return 2

    # ---- 注入桌面桥接后端 ----
    from app.services import desktop_bridge
    from desktop.bridge_impl import DesktopBackend

    state = {"tray": None, "window": None}

    def on_prefs_change(updated: prefs_mod.Prefs) -> None:
        if state["tray"] is not None:
            state["tray"].refresh()

    backend = DesktopBackend(prefs, handle, on_change=on_prefs_change)
    desktop_bridge.set_backend(backend)
    desktop_bridge.set_notifier(
        lambda title, body, url=None: notify.toast(title, body, url) if prefs.notify else False
    )

    # ---- 窗口 ----
    import webview

    window = webview.create_window(
        WINDOW_TITLE,
        handle.base_url,
        width=prefs.window_width,
        height=prefs.window_height,
        min_size=(900, 600),
        confirm_close=False,
    )
    backend.window = window
    state["window"] = window

    def show_window() -> None:
        try:
            window.show()
            window.restore()
        except Exception:
            logger.debug("显示窗口失败", exc_info=True)

    def open_settings() -> None:
        try:
            window.load_url(handle.base_url + "/settings")
            show_window()
        except Exception:
            logger.debug("打开设置页失败", exc_info=True)

    def change_dir_dialog() -> None:
        """托盘菜单「更改数据目录」：选目录后询问是否迁移。"""
        chosen = firstrun.ask_directory("选择新的数据目录", Path(prefs.data_dir or data_dir))
        if not chosen:
            return
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            migrate = messagebox.askyesno(
                "切换数据目录",
                "要把现有数据迁移到新目录吗？\n\n是：复制现有 app.db 过去（旧目录保留）\n否：新目录从空库开始",
                parent=root,
            )
            root.destroy()
        except Exception:
            migrate = True
        result = backend.change_data_dir(chosen, "migrate" if migrate else "empty")
        if result.get("ok"):
            _info("数据目录已切换，重启后生效。")
            backend.request_restart()
        else:
            _info("切换失败：%s" % result.get("error", "未知错误"))

    def toggle_autostart() -> bool:
        backend.update_prefs(autostart=not prefs.autostart)
        return prefs.autostart

    def toggle_close_to_tray() -> bool:
        backend.update_prefs(close_to_tray=not prefs.close_to_tray)
        return prefs.close_to_tray

    def quit_app() -> None:
        logger.info("用户从托盘退出")
        try:
            if state["tray"] is not None:
                state["tray"].stop()
        finally:
            handle.stop()
            os._exit(0)

    tray = tray_mod.Tray(
        title=WINDOW_TITLE,
        on_open=show_window,
        on_settings=open_settings,
        on_change_dir=change_dir_dialog,
        on_toggle_autostart=toggle_autostart,
        on_quit=quit_app,
        is_autostart=lambda: bool(prefs.autostart),
        is_close_to_tray=lambda: bool(prefs.close_to_tray),
        on_toggle_close_to_tray=toggle_close_to_tray,
    )
    state["tray"] = tray
    tray.start()

    def on_closing() -> bool:
        """返回 False 取消关闭：关窗只是收进托盘，定时扫描继续跑（提醒才有意义）。"""
        if not prefs.close_to_tray:
            return True
        try:
            window.hide()
        except Exception:
            logger.debug("隐藏窗口失败", exc_info=True)
        _info("工作台已最小化到系统托盘，定时提醒仍在后台运行。")
        return False

    try:
        window.events.closing += on_closing
    except Exception:
        logger.debug("注册关闭事件失败", exc_info=True)

    logger.info("窗口即将打开：%s", handle.base_url)
    try:
        webview.start(
            gui="edgechromium" if sys.platform.startswith("win") else None,
            private_mode=False,
            storage_path=str(prefs_mod.appdata_dir() / "webview"),
            debug=bool(os.environ.get("WB_DESKTOP_DEBUG")),
        )
    finally:
        try:
            tray.stop()
        finally:
            handle.stop()
    logger.info("桌面版退出")
    return 0


def _info(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo(WINDOW_TITLE, message, parent=root)
        root.destroy()
    except Exception:
        logger.info(message)


def _fatal(message: str) -> None:
    _info(message)
