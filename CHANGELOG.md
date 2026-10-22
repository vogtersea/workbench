# CHANGELOG

格式：每个版本记录「改了什么 / 影响哪些接口 / 是否需要迁移」。
一类改动一个提交，提交信息用 type(scope): 摘要；每个 P 阶段结束打一个语义化 tag。

## [v0.3.0] — 2026-10-21

P2：把「返回值靠猜」与「备份没验证过」两个隐患清掉。

**改了什么**

- W-10 execute() 显式返回类型：签名改为 execute(sql, params, *, returning_id=False)。
  全量审计 32 处调用点，8 处「返回值当新行 id 用」的 INSERT 显式声明 returning_id=True。
  返回值判定改用运行期属性（结果集 / rowcount / total_changes），不再按 SQL 首词猜测：
    INSERT ... RETURNING 以结果集为权威值（不取空会抛 SQL statements in progress）
    ON CONFLICT DO NOTHING 命中时返回 0，不再回落到上一次插入残留的 lastrowid
    WITH ... INSERT（rowcount 为 -1）仍能拿到真实 id
- W-12 备份验证与恢复：
    backup.sh verify <备份>：文件头 / PRAGMA integrity_check / 四表行数，缺表即失败
    restore.sh <备份>：默认 dry-run，--yes 才执行；执行前强制 verify；五步走
      （停容器 -> VACUUM INTO 留现场 -> 清 WAL + 原子替换 -> up -d 等 healthz -> 复核行数）
      失败时打印原因 + 照抄式回退指引
    轮转 glob 收窄为 app-[0-9]*.db：app-before-restore-*.db 等救命快照永不自动删除
- 演练结论：3 任务/2 用户/1 报告 -> 备份 -> 破坏成 8/2/0 -> 恢复后与备份 sha256 完全一致、行数回到 3/2/1；
  截断备份 verify 与 restore 均拒绝且库未被改动。

**影响哪些接口**

- db.execute 签名变更（新增仅关键字参数），不传 returning_id 时行为与旧版一致（返回 rowcount）
- 若你有外部脚本依赖旧版「按首词猜测」的返回类型，需要显式声明 returning_id=True
- 新增运维脚本 scripts/restore.sh；backup.sh 增加 verify 子命令，原有备份用法不变
- HTTP 接口零变化

**是否需要迁移**

- 无需迁移。不影响数据库结构。

## [v0.3.1] — 2026-10-21

W-11：报告页内二次编辑（方案 P2 最后一项）。

**改了什么**

- reports 表新增可空列 content_md_edited（启动时幂等 ALTER，无需手工迁移）
- 报告详情页新增「编辑正文」区：textarea + 保存正文；清空并保存即恢复原文
- 编辑过的报告在标题旁显示「已修改」标记；页面正文、复制原文、导出 Word 三处统一取改后版本
- 原始生成内容 content_md 永不被覆盖，随时可回退

**影响哪些接口**

- 新增 POST /reports/{id}/edit（越权/不存在 404）
- GET /reports/{id} 额外提供 content（改后正文）与 edited（是否改过）给模板
- GET /reports/{id}/export.docx 改为导出改后正文
- db 层新增 reports.content_of / save_content 两个函数

**是否需要迁移**

- 无需手工迁移：应用启动时 init_db() 检测到缺列会自动 ALTER TABLE 补上，既有报告 content_md_edited 为 NULL，行为与以前一致。

## [v1.0.0] — 2026-10-21

桌面版（Windows）：不依赖 Docker、不需要服务器的原生窗口客户端。

**改了什么**

- 新增 desktop/ 桌面壳：pywebview + WebView2 窗口，进程内起本地服务（仅监听 127.0.0.1）
- 首次启动弹窗选择数据目录；设置页新增「桌面应用」一节，托盘菜单同步提供
- 关窗最小化到托盘（定时扫描继续跑）、开机自启（写 HKCU Run，无需管理员）、
  每日提醒弹 Windows 系统通知、数据目录切换（弹窗二选一：迁移 / 空库开始）
- 桌面能力通过 app/services/desktop_bridge.py 注入；服务器版没有后端时相关接口一律 404、
  设置页不渲染该节，NAS 部署行为完全不变
- 打包：PyInstaller onedir + Inno Setup 安装包（按用户安装，不需要管理员）

**影响哪些接口**

- 新增桌面专属接口 /api/desktop/state、/api/desktop/prefs、/api/desktop/data-dir（仅桌面版可用）
- 新增设置页路由 POST /settings/desktop、/settings/desktop/data-dir、/settings/desktop/restart
- 新增环境变量 WB_DESKTOP（桌面壳自动设置）
- 新增依赖文件 requirements-desktop.txt（服务器部署不需要）
- 其余接口与数据库结构零变化

**是否需要迁移**

- 无需迁移：桌面版首次启动会新建空库；若指向已有 NAS 数据目录，直接复用同一份 app.db。

## [v1.0.1] — 2026-10-21

桌面版修复：安装后启动失败（Unable to configure formatter 'default'），以及打包态默认数据目录错误。

**改了什么**

- desktop/server.py：uvicorn.Config 传 log_config=None。uvicorn 默认会调 logging.config.dictConfig
  并解析 uvicorn.logging.DefaultFormatter（该模块 import click）；打包成窗口程序后这条链一旦在用户机器上
  缺失或被安全软件拦截，就会抛 ValueError: Unable to configure formatter 'default'，
  并被我们包装成「本地服务启动失败」。日志本就由 basicConfig + 文件处理器接管，不需要 uvicorn 插手。
- desktop/firstrun.py：默认数据目录改用 sys.executable。打包后 __file__ 指向 PyInstaller 临时解包目录
  （退出即删），原先的算法会把「默认数据目录」建议成一次性目录，用户接受就等于把数据写进临时目录。
- workbench-desktop.spec：显式补 click、uvicorn.logging/config/server、lifespan.on、h11_impl
  等 hiddenimports 作为双保险。
- tests/test_desktop_server.py：新增回归测试，把 logging.config.dictConfig 换成抛异常，
  断言服务仍能启动——去掉修复该用例立刻失败（已实证）。

**影响哪些接口**

- 无对外接口变化；仅桌面壳内部启动路径变化。

**是否需要迁移**

- 无需迁移。若旧版已经把数据目录选到了临时目录，换到新版后重选一次正式目录即可（旧目录内容会随系统清理消失）。

## [v1.0.2] — 2026-10-21

品牌与界面重做：改名「工作志」、新 logo、浅色/深色双主题。

**改了什么**

- 品牌：产品名 工作台 → 工作志，副标题「个人工作流程跟踪」；新增 app/brand.py 作为名字/标语/版本
  的唯一来源（页面标题、侧边栏、窗口标题、托盘、通知、安装包显示名全部取自这里）
- 可执行文件名与 Release 资产名保持不变（Workbench.exe），避免已有快捷方式、桌面路径、脚本失效
- 新图标：圆角渐变底 + 三根上升柱 + 勾（进度累积），代码生成 SVG/ICO/PNG 多尺寸，16px 下可辨认；
  页面 favicon、登录页品牌区、桌面壳窗口与安装包统一使用
- 深色模式：跟随系统 + 顶栏手动切换（记住选择），首屏内联脚本避免白屏闪烁；打印强制浅色
- 界面精修：品牌区（logo + 名称 + 标语）、侧边栏活动项左侧强调条、卡片悬停阴影、
  表格行悬停、focus-visible 焦点环、按钮/徽标过渡、图标按钮与主题切换控件

**影响哪些接口**

- 新增 app/brand.py（NAME / TAGLINE / VERSION / FULL_NAME / title()）
- 新增静态资源 /static/logo.svg、/static/favicon.ico、/static/logo-512.png、/static/theme.js
- 模板上下文新增 BRAND 对象（Jinja 全局）
- 新增环境无关的 UI 行为：localStorage 键 wb-theme
- 后端接口与数据库结构零变化

**是否需要迁移**

- 无需迁移。已部署的 NAS 版拉取新镜像后自动获得新品牌与深色模式；数据不受影响。

## [v1.0.3] — 2026-10-21

深色模式修复：原先有 7 处成片白条，提示条文字在深色下几乎不可见。

**改了什么**

- 新增组件级主题变量并在深色下整套覆盖：--topbar-bg（顶栏原来是 rgba 白）、--head-bg
  （卡片头/脚、表格头、正文表头、时间轴头原来是 #fcfcfd / #fafbfc）、--hover-bg（行与导航悬停）、
  --on-accent / --on-danger（实心按钮文字，深色下改为深色字）、--border-accent|info|success|warn|danger
  （徽标与提示条描边原来是浅色硬编码）、--badge-neutral-bg|fg
- 提示条（alert）文字由写死的深蓝/深褐/深红/深绿改为 var(--info|warn|danger|success)，
  否则深色背景下是深字压深底
- badge-mock 由 #fff4d6 亮黄底改为 warn 主题色；badge-cancelled 改用中性变量
- 前端新增 ?theme=dark|light 强制主题（截图、嵌入、系统不支持深色的设备可用）
- 新增 tests/test_dark_theme.py 四项守卫：深色必须覆盖浅色全部颜色变量、组件规则禁用浅色字面量、
  alert 文字必须用变量、正文与次级文字对比度 >= 4.5（WCAG AA）

**实测**

深色下整页高亮像素（亮度 > 200）占比：修复前 19.91% → 修复后 0.04%（9 个页面均在 0.04%~0.09%）。

**影响哪些接口**

- 无后端接口变化；仅 CSS 变量与 theme.js 行为
- 新增 URL 参数 ?theme=dark|light（不改页面内容，只改 <html data-theme>）

**是否需要迁移**

- 无需迁移。

## [v1.0.4] — 2026-10-21

报告 Word 导出按公文排版约定重做。

**改了什么**

- 补全页眉距 1.5cm、页脚距 1.75cm（原先未设置）
- 首行缩进改用 w:firstLineChars=200（真正的「2 字符」），不再只写固定磅值
- 四级标题按规范对应字体与自动编号：
  一级 黑体三号「一、」／二级 楷体_GB2312 三号「（一）」／
  三级 仿宋_GB2312 三号「1.」／四级 仿宋_GB2312 三号「（1）」
- 编号自动生成且子级重置；标题自带编号时以自带为准并同步计数器（不会出现「二、」后面接「一、」）
- 所有标题强制不加粗（含模型输出里的 ** 标记会被剥掉）
- 提示词新增两条约束：标题不要自己写编号（避免与自动编号重复）、不用项目符号改用「一是……二是……」

**实测**

generated docx 用真实 Word（Office16）打开：页边距 3.5/3.3/2.7/2.7cm、页眉 1.5、页脚 1.75、
正文 仿宋_GB2312 16pt 不加粗、行距 29.5pt（固定值）、首行缩进 32pt（=2 字符），PDF 导出正常。
新增 tests/test_export_format.py 9 项断言（直接校验 docx XML 属性，不是看外观）。

**影响哪些接口**

- 无接口变化；build_docx 签名不变
- 导出文件的格式变化：见上

**是否需要迁移**

- 无需迁移。历史报告重新导出即得新格式。

## [v1.0.5] — 2026-10-21

界面文案纠正：不再用 DDL 这个缩写，统一为「截止日期」。

**改了什么**

- 仪表盘第五区：无 DDL → 无截止日期；副标题「没有待定日期的工作」→「没有登记截止日期的工作」
  （原副标题语义不通：待定日期 ≠ 截止日期）
- 工作列表、归档、工作详情、例行详情表头、周视图小节标题、设置页说明全部改为「截止日期」
- 每日提醒正文：— DDL {日期} → — 截止日期 {日期}；无截止日期的项不再显示缩写
- 报告生成：提示词与 MOCK 模板里的 DDL 全部改为截止日期，避免模型把缩写写进公文
- README / SPEC 的口径同步（SQL 的 DDL 即 Data Definition Language 保留原样）

**影响哪些接口**

- 无接口与数据库变化（列名仍是 due_date）

**是否需要迁移**

- 无需迁移。

## [v1.0.6] — 2026-10-21

PC 端布局修复：桌面浏览器里页面只有 574px 宽（1920 窗口），看起来太瘦。

**根因（一直存在的问题，不只是宽屏）**

.container 设了 margin: 0 auto 做居中，而 .app-main 是 flex 纵向容器。flex 布局里
auto 外边距会吸收剩余空间、使元素放弃拉伸，于是 .container 宽度退化成「内容宽度」，
--maxw（1180px）从未真正生效——实测 1920 窗口下内容只有 574px。

**改了什么**

- .app-main > .container 增加 width: 100%（一行修复，是根因所在）
- 内容宽度按视口分档：默认 1180 → ≥1360 为 1380 → ≥1760 为 1560 → ≥2160 为 1720
- 仪表盘五个分区在 ≥1100px 用 6 栏栅格：逾期/今日/临期各占 2 栏（一行三个），
  本周/无截止日期各占 3 栏（一行两个），正好两行排满，不留空档
- ≥1280px 细节：表格字号与单元格留白加大、表单每列最小宽度 220px、页面标题 22px、
  周视图日列最小高度 190px
- 移动端断点（900 / 640）保持原样，未做任何改动

**实测**

内容宽度：1920 窗口 574px → 1560px；1440 → 1190px；2560 → 1720px；1024 → 774px（侧栏外可用宽度）。
用无头 Edge 读取计算样式核对，并对 1920 / 1440 / 1152 / 1024 / 820 / 390 逐个截图核对，移动端布局无回退。

**影响哪些接口**

- 无接口与数据库变化；仅 CSS 与 dashboard 模板加了一层 .bucket-grid 容器

**是否需要迁移**

- 无需迁移。

## [v1.0.7] — 2026-10-21

用已清理的源码重新构建：v1.0.1 ~ v1.0.6 的二进制含清理前的注释，本次全部重做。

**背景**

报告导出的排版参数来自一份内部规范，早期把规范的文件名与部门写进了代码注释与 CHANGELOG。
源码已在 v1.0.6 之后清理（当前文件与 git 历史均为 0 命中），但**已构建的二进制仍是清理前的源码**，
PyInstaller 会把模块常量压进 exe 内的 PYZ，解包可见。本次用清理后的源码重新打包并部署。

**改了什么**

- 版本号 1.0.7，行为无变化（与 v1.0.6 功能一致，仅源码清洁度不同）
- 旧版本（v1.0.1 ~ v1.0.6）的二进制资产已撤下

**是否需要迁移**

- 无需迁移；建议直接用 v1.0.7 的安装包替换旧版。

## [v1.1.0] — 2026-10-22

新增 Android 版（**测试版 beta**）：纯本地 APK（内嵌 CPython + FastAPI，WebView 显示，数据存手机本地）。

**改了什么**

- 新增 android/ 工程（Gradle + Chaquopy 16.1）：App 内启动本地 HTTP 服务，仅监听 127.0.0.1
- 新增 scripts/build_apk.sh：把仓库 app/ 同步进 Android 工程后构建，产物 dist/worklog-<版本>-debug.apk
- app/templating.py 增加 TemplateResponse 双签名兼容（桌面 starlette 1.9 与 Android starlette 0.36 共用一份代码）
- 依赖约束：pydantic<2（v2 核心是 Rust 扩展，Android 无法编译）、fastapi==0.110.0、tzdata

**实测**

- Android 14 模拟器：能安装、能启动、页面正常渲染、点击导航留在应用内（未跳浏览器）
- 数据落地：files/app.db + WAL 在 App 私有目录

**影响哪些接口**

- 后端接口与数据库结构零变化；Android 版复用同一套模板与路由

**是否需要迁移**

- 无需迁移。手机端数据独立存于 App 私有目录，与 NAS/桌面互不影响。

## [未发布]

（方案剩余项：W-11 报告页内二次编辑。W-10 / W-12 已随 v0.3.0 发布。）

## [v0.2.0] — 2026-10-21

P1：运行可靠性与工程结构。

**改了什么**

- W-05 连接复用：数据库连接改为线程本地复用，不再每次查询 open/close。
  报告页单次渲染的连接创建次数由 32 降到 2（降 94%），WAL + busy_timeout 不变。
- W-06 调度日锁：用 settings.reminder_lock 实现抢占式日锁，多个 worker 同时只允许一路扫描推送。
  实测 3 个独立进程同时执行，只有 1 路发送。抢锁异常时 fail-closed（宁可不推，也不重复推）。
- W-07 登录节流落库：失败次数从进程内 dict 改为 login_attempts 表（5 次 / 300 秒窗口），
  重启不清零、多 worker 一致；每次记录顺带清理过期行，随机用户名不会撑大内存。
- W-08 大文件拆分：settings 域 1220 行拆成 8 个模块（最大 357）、报告域 1056 行拆成 4 个（最大 310）、
  Obsidian 域 937 行拆成 3 个（最大 376）。旧的 pages_settings.py 与 reports.py 转发层已删除，
  obsidian.py 保留为稳定门面。
- W-09 提交与版本纪律：CHANGELOG、语义化 tag、SPEC 第 17 章运行约束、按主题拆分提交。

**影响哪些接口**

- 对外 HTTP 接口零变化（拆分前后 12 条 /settings 路由、11 条 API、20 条写路径逐条比对一致）
- 函数签名零变化（178 条顶层语句 AST 逐条比对，仅 1 处延迟 import 以解循环依赖）
- 新增内部模块路径：app/services/report/*、app/services/obsidian_{vault,index,search}.py、app/web/settings/*
- 若你有外部脚本 import app.web.pages_settings 或 app.services.reports，需改为新路径

**是否需要迁移**

- 无需手工迁移。init_db() 会自动建 login_attempts 表并播种 reminder_lock；
  既有库启动即补齐，不改动任何既有数据。

## [v0.1.0] — 2026-10-20

首个可用基线。

**改了什么**

- 任务：手工录入、详情、完成条件、DDL、持续追加进展；完成即销号归档，归档可恢复
- 例行工作：日/周/月/季/半年/年六种周期，自动生成当期实例，可跳过；同一模板同期唯一
- 多用户：管理员/成员两级，邀请码注册，归属隔离（跨用户一律 404），管理员可 ?as= 代办，
  删除用户可选「转移给管理员（默认）」或「彻底清除」；最后一个管理员不可停用/降级/删除
- 报告：单条 / 周 / 月 / 季 / 半年 / 年 / 自定义区间，大模型多套配置可在前端增删切换与测试连接，
  未配置密钥时走确定性 MOCK 模板
- Obsidian：只读索引与全文搜索（trigram + LIKE 回退）
- 前端：服务端渲染 + htmx（本地打包，无 CDN）、左侧常驻侧边栏、窄屏抽屉
- 部署：Docker + docker compose，数据卷本地持久化，宿主端口由 APP_PORT 配置

**P0 改进（本次一并纳入基线）**

- W-01 报告导出 Word（docx）：公文版式（页边距 3.5/3.3/2.7/2.7cm、正文仿宋_GB2312 16pt、
  固定行距 29.5 磅、首行缩进 32pt），EXPORT_DOCX 可关闭
- W-02 会话 Cookie 支持 Secure：SECURE_COOKIE=1（HTTPS 反代），直连 HTTP 必须为 0
- W-03 测试与 CI：pytest 用例、ruff 配置、GitHub Actions
- W-04 许可：GPL-3.0

**影响哪些接口**

- 新增 GET /reports/{id}/export.docx（附件下载，越权 303 回列表）
- 新增环境变量 SECURE_COOKIE、EXPORT_DOCX、APP_PORT
- 其余既有接口签名不变

**是否需要迁移**

- 无。首次启动 init_db() 会自动建表；已有库无需手工操作
