"""桌面版打包入口（PyInstaller 用它做启动脚本）。

开发态等价于 python -m desktop；打包后是可双击的 Workbench.exe。
"""

import multiprocessing
import sys

from desktop.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
