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
