"""品牌信息单一来源：名字只在这里改，页面/桌面壳/安装包都从这儿取。

产品名与可执行文件名有意分开：显示名是「工作志」，但 exe 仍叫 Workbench.exe，
避免已有快捷方式和 Release 资产名失效。
"""

from __future__ import annotations

NAME = "工作志"
NAME_EN = "Worklog"
TAGLINE = "个人工作流程跟踪"
FULL_NAME = "%s · %s" % (NAME, TAGLINE)
VERSION = "1.2.2"
EXE_NAME = "Workbench.exe"


def title(page: str | None = None) -> str:
    """页面标题：有页面名时是「页面 · 工作志」，否则「工作志 · 个人工作流程跟踪」。"""
    if page:
        return "%s · %s" % (page, NAME)
    return FULL_NAME
