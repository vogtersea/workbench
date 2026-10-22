# 工作志 · Android 版

> **测试版（beta）**：功能可用，但未做 release 签名，后台提醒尚未接入系统闹钟，
> 仅在 Android 14 模拟器上验证过启动与页面导航。
> 日常记录可用；重要数据请同时保留桌面版或 NAS 版。

纯本地：CPython + FastAPI 打进 APK，服务只监听 `127.0.0.1`，界面用 WebView 显示，
与桌面版、NAS 版共用同一份 `app/` 代码与模板。数据存在 App 私有目录的 SQLite。

## 前置环境（一次性）

| 组件 | 说明 |
|---|---|
| JDK 17 | Chaquopy 与 AGP 8.4 要求 |
| Android SDK | 需 `platforms;android-34` 与 `build-tools;34.0.0` |
| Gradle 8.7 | 未用 wrapper，需自行提供 `gradle` 命令 |

环境变量：`ANDROID_HOME`、`JAVA_HOME`，可选 `GRADLE`。

## 构建

```bash
bash scripts/build_apk.sh
```

产物：`dist/worklog-<版本>-debug.apk`

脚本会先把仓库根的 `app/` 同步进 `android/app/src/main/python/app/` 再构建——
**那份副本是构建产物、已被 gitignore**，因此不存在「两份代码不一致」的问题。

## 设计要点（三个必须设置的项）

1. `setWebViewClient`：不设置的话，WebView 会把每次导航交给系统浏览器，
   表现为「点按钮就跳到浏览器」，应用内无法使用。
2. `setWebChromeClient`：JS 的 `confirm()` 没有它会静默返回 false，
   而删除确认用的正是 `confirm()`。
3. `onBackPressed`：让返回键在 WebView 内后退，而不是直接退出。

## 依赖版本约束（踩过的坑）

| 包 | 约束 | 原因 |
|---|---|---|
| `pydantic` | `<2` | v2 核心 `pydantic-core` 是 Rust 扩展，Android 上无法编译 |
| `fastapi` | `==0.110.0` | 需兼容 pydantic v1，且依赖 starlette ≥0.29（新版 `TemplateResponse` 签名） |
| `tzdata` | 必装 | Android 没有系统时区库，`zoneinfo("Asia/Shanghai")` 否则抛异常 |
| `python-docx` | 可装 | lxml 有 Android 原生库，Word 导出可用（APK 因此约 +5 MB） |

## 已知限制

- 当前只出 **debug 包**（debug 签名），正式分发需自行配置 release keystore
- 后台提醒尚未接入 `AlarmManager`，APScheduler 常驻线程在 Android 上会被系统回收
- 仅在 Android 14（API 34）x86_64 模拟器上验证过启动与页面导航
