"""工作台桌面版（Windows，pywebview 壳）。

设计要点：
- 复用 app/ 下全部业务代码与模板，桌面壳只负责：启动本地服务、开窗口、托盘、系统通知、开机自启。
- 数据目录与桌面开关存 %APPDATA%\WorkbenchDesktop\prefs.json；数据库仍由 app 层管理。
- 桌面能力通过 app/services/desktop_bridge.py 注入，服务器版完全不受影响。
"""

__version__ = "1.0.0"
