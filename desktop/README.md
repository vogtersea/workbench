# 工作志 · 桌面版（Windows）

不依赖 Docker、不需要服务器：双击运行，本地起一个只监听 127.0.0.1 的服务，用 WebView2 窗口显示界面。
业务代码与模板全量复用，功能与 NAS 版一致。

## 下载现成的

<https://github.com/vogtersea/workbench/releases/latest>

- Workbench-Setup.exe —— 安装包（按用户安装，不需要管理员）
- Workbench-portable-v1.0.x.zip —— 解压即用（x 为当前版本号）
- SHA256SUMS.txt —— 校验和

## 构建

    .venv/Scripts/python.exe -m pip install -r requirements-desktop.txt
    bash scripts/build_desktop.sh

产物：

| 产物 | 说明 |
|---|---|
| dist/Workbench/Workbench.exe | 免安装目录版，解压即用（启动 1–2 秒） |
| dist/Workbench-Setup.exe | Inno Setup 安装包（按用户安装到 %LOCALAPPDATA%\Programs，不需要管理员） |

Inno Setup 6 未安装时脚本会跳过安装包，只出免安装目录。

## 首次启动

弹窗让你选**数据目录**（数据库与配置都放在这里），建议放非系统盘。之后可在
设置页「桌面应用」一节或托盘菜单里更改。

## 桌面专属能力

| 能力 | 位置 |
|---|---|
| 关窗最小化到托盘（定时提醒继续跑） | 设置页 / 托盘菜单 |
| 开机自动启动（写 HKCU Run 键，无需管理员） | 设置页 / 托盘菜单 |
| 每日提醒弹 Windows 系统通知 | 设置页 |
| 更改数据目录（弹窗二选一：迁移 / 空库开始） | 设置页 / 托盘菜单 |

数据目录迁移会先复制 app.db 并做 integrity_check 校验，失败不动现有数据；旧目录一律保留。

## 排障

日志：%APPDATA%\WorkbenchDesktop\logs\desktop.log
偏好：%APPDATA%\WorkbenchDesktop\prefs.json（损坏会自动回退默认值，不会启动失败）

单实例：重复启动会提示「已在运行」，不会起第二个进程。
