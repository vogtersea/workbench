# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir，产出 dist/Workbench/Workbench.exe。

构建：bash scripts/build_desktop.sh
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = []
hiddenimports += collect_submodules("uvicorn")          # loop/protocol/lifespan 均为动态导入
hiddenimports += collect_submodules("apscheduler")      # 调度器触发器同样是动态导入
hiddenimports += collect_submodules("markdown")
hiddenimports += [
    "click",                     # uvicorn.logging 依赖 click；窗口程序下缺失会报 formatter 配置失败
    "uvicorn.logging",
    "uvicorn.config",
    "uvicorn.server",
    "uvicorn.lifespan.on",
    "uvicorn.protocols.http.h11_impl",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "clr",                       # pythonnet：pywebview 的 Windows 后端依赖
    "pythonnet",
    "pystray._win32",
    "winotify",
    "PIL._tkinter_finder",
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
]

datas = [
    ("app/templates", "app/templates"),   # Jinja2 模板必须随包
    ("app/static", "app/static"),         # css/js 必须随包
]
datas += collect_data_files("webview")
datas += collect_data_files("winotify")

a = Analysis(
    ["run_desktop.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests", "PySide6", "PyQt5", "PyQt6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Workbench",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # 窗口程序：不弹黑框；日志写 %APPDATA%\WorkbenchDesktop\logs
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="build/workbench.ico" if os.path.exists("build/workbench.ico") else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Workbench",
)
