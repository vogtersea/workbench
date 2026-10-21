# 工作台项目 · TECHNICAL SPEC（子代理共享契约）

> 本文件是唯一权威契约。所有实现必须与此一致；如需偏离，先在本文件登记再实施。
> 项目根目录：D:/Users/suzhe/Documents/deepseek-harness/工作台项目

## 0. 目标与非目标
个人工作流程跟踪系统：手动添加每周工作（详情 / 完成条件 / DDL），随时追加进展；未完成提醒，完成后销号归档；
数据落本地 SQLite；Docker 部署在 NAS；接入大模型生成单项工作与任意区间的工作总结；只读接入 Obsidian 知识库增强报告。

非目标（v1 明确不做）：多用户与权限体系、移动端专门适配、附件/图片上传、docx/pdf 导出、Obsidian 写回。

## 1. 技术栈（已拍定，不得更换）
- Python 3.12，FastAPI，uvicorn，Jinja2 服务端渲染，htmx（本地 vendored）渐进增强
- SQLite（stdlib sqlite3，raw SQL，WAL，foreign_keys=ON）
- APScheduler（BackgroundScheduler）做每日提醒扫描
- httpx（LLM 调用 + Webhook 推送）
- markdown（Markdown 转 HTML 渲染）
- python-dotenv（读取根目录 .env）
- 禁止：CDN 引用、前端构建链（npm/webpack/vite）、ORM（SQLAlchemy/Alembic）

## 2. 目录结构（文件归属见任务书；未列出的文件不得创建）

    SPEC.md  README.md  .env.example  .gitignore  requirements.txt
    Dockerfile  docker-compose.yml
    scripts/backup.sh  scripts/smoke_test.py
    app/__init__.py
    app/config.py               环境变量配置
    app/timeutil.py             日期 / ISO 周工具
    app/db.py                   连接、DDL、初始化
    app/security.py             可选口令鉴权中间件
    app/templating.py           Jinja 环境与过滤器
    app/main.py                 app 工厂、路由注册、startup
    app/services/__init__.py
    app/services/settings_store.py
    app/services/tasks.py
    app/services/llm.py
    app/services/reports.py
    app/services/obsidian.py
    app/services/reminders.py
    app/services/scheduler.py
    app/api/__init__.py
    app/api/routes_tasks.py  routes_reports.py  routes_settings.py  routes_obsidian.py
    app/web/__init__.py
    app/web/pages_tasks.py  pages_reports.py  pages_settings.py  pages_obsidian.py
    app/templates/base.html  login.html
    app/templates/dashboard.html  week.html  task_detail.html  archive.html  projects.html
    app/templates/reports.html  report_detail.html  settings.html
    app/templates/partials/  （片段目录）
    app/static/app.css  app.js  htmx.min.js

## 3. 环境变量（app/config.py 汇总，写入 .env.example）

    | 变量 | 默认 | 说明 |
    | DATA_DIR | ./data | 数据目录，DB 在 DATA_DIR/app.db |
    | TZ_NAME | Asia/Shanghai | 业务时区 |
    | APP_PASSWORD | 空 | 非空则启用口令登录；空=不鉴权（内网自用） |
    | APP_SECRET | 空 | 会话签名密钥；为空时首次启动自动生成并落 DATA_DIR/.secret |
    | VAULT_DIR | /vault | Obsidian vault 只读目录；不存在则禁用该功能 |
    | LLM_BASE_URL | https://api.deepseek.com/v1 | OpenAI 兼容 base |
    | LLM_API_KEY | 空 | 空即 MOCK 模式 |
    | LLM_MODEL | deepseek-chat | 模型名 |
    | LLM_TIMEOUT | 120 | 秒 |
    | LLM_MAX_TOKENS | 2000 | |
    | LLM_TEMPERATURE | 0.3 | |
    | REMIND_TIME | 09:00 | 每日扫描时刻 |
    | WEBHOOK_URL | 空 | 空即仅站内提醒 |
    | PORT | 8080 | |

密钥只从环境 / .env 读，绝不写库、绝不进 git。settings 表只存非密钥项。

## 4. 数据库 DDL（app/db.py，幂等 CREATE IF NOT EXISTS）

    CREATE TABLE IF NOT EXISTS projects (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL UNIQUE,
      description TEXT NOT NULL DEFAULT '',
      color TEXT NOT NULL DEFAULT '#6366f1',
      archived INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
      title TEXT NOT NULL,
      detail TEXT NOT NULL DEFAULT '',
      done_criteria TEXT NOT NULL DEFAULT '',
      due_date TEXT,
      due_time TEXT,
      status TEXT NOT NULL DEFAULT 'todo',
      priority INTEGER NOT NULL DEFAULT 2,
      week_start TEXT NOT NULL,
      tags TEXT NOT NULL DEFAULT '',
      completed_at TEXT, archived INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_week ON tasks(week_start);
    CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date);
    CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, archived);
    CREATE TABLE IF NOT EXISTS progress_entries (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      content TEXT NOT NULL, link TEXT,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_progress_task ON progress_entries(task_id);
    CREATE TABLE IF NOT EXISTS reports (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL,
      scope_label TEXT NOT NULL,
      range_start TEXT, range_end TEXT,
      task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
      content_md TEXT NOT NULL,
      provider TEXT, model TEXT,
      is_mock INTEGER NOT NULL DEFAULT 0,
      context_notes TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS reminders_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      sent_at TEXT NOT NULL, kind TEXT NOT NULL,
      channel TEXT NOT NULL, payload TEXT,
      ok INTEGER NOT NULL DEFAULT 0, error TEXT
    );
    CREATE TABLE IF NOT EXISTS notes_index (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      path TEXT NOT NULL UNIQUE, title TEXT, tags TEXT,
      frontmatter TEXT, body TEXT, mtime REAL, indexed_at TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
      path UNINDEXED, title, tags, body, tokenize='trigram'
    );

字段语义补充：
- tasks.status 取值 todo | doing | done | cancelled；priority 1低 2中 3高
- week_start 是该工作所属 ISO 周的周一（YYYY-MM-DD）
- due_date 可空表示无 DDL；due_time 为 HH:MM 可空
- tags 逗号分隔、不含空格
- 若 trigram 分词器不可用则降级 unicode61（初始化时 try/except，把实际分词器写入 settings 键 fts_tokenizer）
- 时间一律 ISO8601 字符串，业务时区 TZ_NAME

## 5. 关键函数契约（签名固定，跨模块依赖这些）

### app/db.py

    def connect() -> sqlite3.Connection   # row_factory=Row, WAL, foreign_keys=ON
    @contextmanager
    def cursor() -> Iterator[sqlite3.Connection]
    def query(sql, params=()) -> list[sqlite3.Row]
    def one(sql, params=()) -> sqlite3.Row | None
    def execute(sql, params=(), *, returning_id: bool = False) -> int
                                          # 显式声明返回类型（W-10，v0.3.0 起）：
                                          #   returning_id=True -> 新行 id（未插入则 0）
                                          #   默认 -> 受影响行数 rowcount
                                          # 不再按 SQL 首词猜测；语句自带 RETURNING 时以结果集为权威值
    def init_db() -> None                 # 幂等建表 + 默认 settings

### app/timeutil.py

    def now_iso() -> str
    def today() -> date
    def week_start(d: date) -> date                          # 周一
    def week_bounds(offset: int = 0) -> tuple[date, date]    # 相对本周的周一/周日
    def fmt_cn(d) -> str                                     # 06-04 周三
    def days_until(due: str | None) -> int | None            # 正=未来, 负=逾期

### app/services/settings_store.py

    def get(key: str, default: str = "") -> str
    def set(key: str, value: str) -> None
    def all() -> dict[str, str]
    def seed_defaults() -> None

默认 settings 键：remind_time=09:00、remind_soon_days=3、webhook_url=（空）、webhook_format=generic_json、
telegram_chat_id=、llm_base_url=（空则可选覆盖 env）、llm_model=（空则用 env）、obsidian_enabled=0、
obsidian_last_index=、fts_tokenizer=trigram、last_scan_json=（最近一次扫描摘要）。

### app/services/tasks.py（任务域实现，其它域只调用）

    def list_tasks(*, week_start=None, project_id=None, status=None, archived=0, q=None) -> list[Row]
    def get_task(task_id: int) -> Row | None
    def create_task(data: dict) -> int
    def update_task(task_id: int, data: dict) -> None
    def set_status(task_id: int, status: str) -> None
        # done 时 completed_at=now, archived=1；恢复 todo 时 archived=0, completed_at=NULL
    def add_progress(task_id: int, content: str, link: str | None = None) -> int
    def list_progress(task_id: int) -> list[Row]
    def delete_progress(pid: int) -> None
    def dashboard_buckets() -> dict
        # {"overdue":[..],"today":[..],"soon":[..],"this_week":[..],"no_due":[..],"counts":{..}}
    def tasks_in_range(start: str, end: str) -> list[Row]
    def progress_in_range(start: str, end: str) -> list[Row]
    def list_projects(include_archived=False) -> list[Row]
    def create_project(name, description="", color="#6366f1") -> int

### app/services/llm.py

    @dataclass
    class LLMResult: text: str; provider: str; model: str; is_mock: bool
    def is_mock_mode() -> bool
    async def generate(prompt: str, system: str | None = None) -> LLMResult
    async def health() -> dict
        # {"mode":"mock|live","model":..,"ok":bool,"error":str|None,"latency_ms":int}

MOCK 必须基于真实入库数据做确定性模板渲染，并在正文首行插入一行醒目的 MOCK 声明：
  > MOCK 模式：未配置 LLM_API_KEY，本报告为模板渲染，非大模型生成

### app/services/obsidian.py

    def vault_available() -> bool
    def reindex() -> dict
        # {"scanned":n,"indexed":n,"skipped":n,"errors":n,"elapsed_ms":int}
    def search(q: str, limit: int = 5) -> list[dict]        # [{path,title,score,snippet}]
    def build_context(keywords: list[str], limit: int = 5, budget: int = 2000) -> tuple[str, list[dict]]

### app/services/reports.py

    async def generate(kind: str, *, start=None, end=None, task_id=None, use_obsidian=True) -> int
    def list_reports(limit: int = 50) -> list[Row]
    def get_report(rid: int) -> Row | None
    def delete_report(rid: int) -> None
    def resolve_range(kind: str, start=None, end=None) -> tuple[str, str, str]   # (start_iso, end_iso, scope_label)

### app/services/reminders.py 与 app/services/scheduler.py

    def scan() -> dict            # {"overdue":[..],"today":[..],"soon":[..],"message":str,"has_items":bool}
    def build_message(scan_result) -> str
    def push(message: str) -> dict          # {"ok":bool,"channel":str,"error":str|None}
    def send_daily() -> dict                # scan -> push -> 写 reminders_log
    def test_push() -> dict
    def start() -> None                     # 启动调度器
    def shutdown() -> None
    def next_run() -> str | None

## 6. HTTP 接口

### 页面路由（app/web/pages_*.py，Jinja2 渲染；每个模块暴露 pages = APIRouter()）

    | 方法 | 路径 | 说明 |
    | GET | / | 仪表盘：逾期 / 今日 / 3日内 / 本周 / 无DDL 五区 + 快速添加 |
    | GET | /week?offset=0 | ISO 周视图，按天分组，含上周/本周/下周导航 |
    | GET | /tasks/new | 新建工作表单 |
    | POST | /tasks | 创建（成功后 303 到 /tasks/{id}） |
    | GET | /tasks/{id} | 详情：信息、完成条件、进展时间线、状态切换 |
    | POST | /tasks/{id}/edit | 编辑 |
    | POST | /tasks/{id}/status | 状态切换（表单字段 status） |
    | POST | /tasks/{id}/progress | 追加进展（content, link） |
    | POST | /progress/{pid}/delete | 删除进展 |
    | POST | /tasks/{id}/delete | 删除工作 |
    | GET | /archive | 已完成 / 已取消归档区，按完成周分组，可恢复 |
    | GET | /projects | 项目列表 |
    | POST | /projects | 新建项目 |
    | GET | /reports | 报告中心：生成表单 + 历史列表 |
    | POST | /reports | 触发生成，303 到 /reports/{id} |
    | GET | /reports/{id} | 报告详情（Markdown 渲染 + 打印按钮 + 复制原文） |
    | POST | /reports/{id}/delete | 删除报告 |
    | GET | /settings | 设置页 |
    | POST | /settings | 保存提醒 / Webhook / Obsidian 配置 |
    | POST | /settings/test-push | 测试推送 |
    | POST | /settings/reindex | 重建 Obsidian 索引 |
    | GET | /login | 登录页 |
    | POST | /login | 提交口令 |
    | POST | /logout | 退出 |
    | GET | /healthz | {"ok": true}（不鉴权） |

### JSON API（app/api/routes_*.py，每个模块暴露 router = APIRouter()）

    GET/POST /api/tasks
    GET/PATCH/DELETE /api/tasks/{id}
    POST /api/tasks/{id}/progress
    GET /api/dashboard
    GET /api/week?offset=0
    GET /api/projects
    GET /api/reports        POST /api/reports
    GET /api/reports/{id}   DELETE /api/reports/{id}
    GET /api/llm/health
    GET /api/obsidian/search?q=      POST /api/obsidian/reindex
    GET /api/reminders/preview       POST /api/reminders/test

## 7. 行为规则
1. 周归属：表单可显式选周；未选则取 due_date 所在周的周一，再退化为今天所在周的周一。
2. 销号归档：status=done 即 archived=1 且 completed_at=now，立刻离开所有活跃视图；/archive 可一键恢复。
3. 提醒分区（仅 status in (todo, doing) 且 archived=0）：
   逾期 due_date < today；今日 due_date = today；临期 today < due_date <= today + remind_soon_days；
   本周 week_start = 本周一；无DDL due_date IS NULL。
4. 每日扫描：APScheduler 在 remind_time 触发 send_daily()；无待提醒项则不推送但写日志。
   WEBHOOK_URL 为空则只写站内，设置页展示最近一次扫描结果。
5. Webhook 格式（webhook_format 取值）：
   generic_json -> {"text": msg}
   wecom_text   -> {"msgtype":"text","text":{"content":msg}}
   feishu_text  -> {"msg_type":"text","content":{"text":msg}}
   serverchan   -> {"title":title,"desp":msg}
   telegram     -> {"chat_id":..., "text":msg}   （bot token 已在 URL 内）
   bark         -> {"title":title,"body":msg}
   超时 10s，失败重试 1 次，结果写 reminders_log。
6. 报告粒度：task（单工作，含全部进展）/ week / month / half_year / year / custom（必填起止日）。
   区间统计包含：区间内创建、区间内完成、区间内有进展的工作。
7. 报告 Prompt：中文，强制 Markdown，固定小节为 概述 / 完成情况 / 关键进展 / 问题与风险 / 下阶段计划；
   只允许使用输入数据中的事实，禁止编造；Obsidian 上下文作为“相关知识库笔记”附在数据区并声明仅作背景参考。
8. 报告生成是同步阻塞请求（可能 30-60s）：按钮需 loading 态并提示“生成中，请勿关闭页面”。
9. htmx 渐进增强：所有变更必须用原生 form method=post 可工作；htmx 仅在追加进展、状态切换、测试推送三处做局部刷新。
   服务端按 HX-Request 头决定返回片段还是 303 重定向。
10. 鉴权：APP_PASSWORD 为空则完全跳过；非空则除 /healthz、/login、/static/* 外全部要求会话 cookie wb_session
    （值 = hmac_sha256(APP_SECRET, APP_PASSWORD) 的 hex），失效跳 /login?next=...
11. UI 全中文；无 CDN；响应式（桌面优先，窄屏单列可用）。
12. 不引入新依赖：requirements.txt 只允许 fastapi、uvicorn[standard]、jinja2、httpx、apscheduler、
    python-multipart、python-dotenv、markdown。

## 8. 部署
- 单镜像 python:3.12-slim；pip install --no-cache-dir -r requirements.txt；非 root 用户运行；
  EXPOSE 8080；HEALTHCHECK 打 /healthz；CMD 为 uvicorn app.main:app --host 0.0.0.0 --port 8080。
- compose：./data:/data（读写，SQLite）、./vault:/vault:ro（可选）、env_file: .env、
  restart: unless-stopped、TZ=Asia/Shanghai。
- 备份：停写即复制 data/app.db；scripts/backup.sh 用 sqlite3 VACUUM INTO 生成
  backup/app-YYYYmmdd-HHMMSS.db，保留最近 14 份。

## 9. 验收口径（子代理自测 + 调度者复核）
1. python -m uvicorn app.main:app --port 8080 可启动，/healthz 返回 200，库文件生成。
2. 全链路：新建项目 -> 新建本周工作（详情 / 完成条件 / DDL）-> 追加进展 -> 完成 -> 出现在 /archive -> 可恢复。
3. 周视图与五个仪表盘分区计数正确（构造边界数据：昨天 / 今天 / +3天 / +4天 / 无DDL）。
4. 报告：task 与 custom 各生成一份，MOCK 水印存在，落库可在列表看到，详情页渲染正常。
5. Webhook：本地 echo 服务收到符合所选格式的 JSON；未配 URL 时页面不报错。
6. Obsidian：临时 vault（2 个 md + frontmatter + 双链）索引后 search 命中，报告 context_notes 非空。
7. docker compose config 通过（本机无 Docker，仅静态校验 YAML）。

## 10. 实现偏差与调度者裁决登记（交付后补记）

1. API 前缀：各 api 模块内路径不含 /api，由 main.py include_router(prefix="/api") 统一加，全站一致。
2. /healthz、/login、/logout 由 main.py 独占实现；SPEC 第 6 节把它们列在页面路由表，实际注册位置不同但行为一致。
3. / 仪表盘由 pages_tasks 提供；阶段一注册的占位 / 已被下游路由遮蔽（实测验证）。
4. resolve_range("task") 无法用 (kind,start,end) 表达单项工作跨度：generate 在 task 粒度改用该工作自身
   创建日 -> 完成日（未完成则到今天）覆盖 range_start/range_end，scope_label 记为「单项工作 #<id>」。判定可接受。
5. settings 表新增两个非密钥键：last_test_push_json、last_reindex_json（供设置页回显）；未触及 obsidian_last_index。
6. requirements.txt 锁定 apscheduler>=3.10,<4（BackgroundScheduler 属 3.x API）。
7. 短词检索：FTS5 trigram 对 1-2 字查询无结果，obsidian.search 在 len(q)<3 时回退 notes_index 的 LIKE 查询
   （已实测 1 字「迁」与 2 字「数仓」均可命中）。
8. 本机无 Docker：镜像构建未实测；compose 与 Dockerfile 以 PyYAML 解析 + 逐条对齐 SPEC 第 8 节核对。
   Dockerfile 内 apt 安装 tzdata（zoneinfo 解析 Asia/Shanghai）与 sqlite3（容器内可直接跑 backup.sh）。
9. scripts/backup.sh 读取 DATA_DIR / BACKUP_DIR / KEEP / DB_PATH；VACUUM INTO 依赖 sqlite3 CLI（镜像内已装）。
   Windows 本地验证须使用盘符路径；POSIX 子系统路径需要先做转换。
10. 验收环境为 starlette 1.x + fastapi 0.150，TestClient 对 httpx 有弃用告警，不影响功能。

## 11. 验收结论（调度者独立执行，非子代理自述）

- 独立验收脚本 31 项断言（另含 3 个周视图 offset 断言）全绿；唯一一次「失败」是验收脚本自身对
  webhook 日志的二次转义判断写错，解析嵌套 JSON 后确认 payload 正确（keys=['text']）。
- 子代理交付的 scripts/smoke_test.py 在全新库上 11/11 PASS、退出码 0。
- 数据层：8 张业务表 + notes_fts 虚拟表，WAL，FTS5 trigram；15 个共享文件 sha256 在四域并行开发期间零变更。
- 端到端：建项目 -> 建工作（详情/完成条件/DDL）-> 追加进展 -> 完成 -> 归档 -> 恢复 -> 周视图三页 -> 报告(MOCK) ->
  自定义区间 -> 反向区间 400 -> Obsidian 索引 3 篇 + 中文检索命中 -> 提醒三桶 1/1/1 -> Webhook 实收 -> 设置保存。
- live 模式：以本地假 OpenAI 服务验证真实调用链（POST /v1/chat/completions + Bearer + 中文防编造 system prompt），
  落库报告 is_mock=0；未配置 key 时 MOCK 首行声明不可省略。
- 部署物：compose 7 项结构断言通过；backup.sh 实跑产出 integrity=ok 快照并正确拒绝非法 KEEP / 缺失数据库。
- 默认参数启动（无任何环境变量，端口 8080，VAULT_DIR 不存在）：8 个页面全部 200，/obsidian 正确显示未挂载提示。

## 12. NAS 实际部署记录（2026-10-20）

- 目标：<数据卷>/docker/workbench（x86_64 Linux，Docker Engine 29.x，Compose v2 语法）
- 环境限制与处理：
  1. Docker Hub 的 auth/registry 端点在该网络不可达（v4/v6 双栈均超时），但环境内已有 HTTP 代理：改为先由 daemon 拉取 python:3.12-slim，再用传统构建器
     DOCKER_BUILDKIT=0 docker build 构建，绕开 BuildKit 不继承代理的问题。
  2. PyPI 官方源在该网络约 13 kB/s 且会挂起。Dockerfile 因此新增可选构建参数 PIP_INDEX_URL
     （默认空 = 官方源不变），本次构建传入清华镜像；不改变默认行为，删除该 ARG 即可完全复原。
  3. 目标机若对 SFTP 子系统做 chroot，可改用 SSH 通道 + tar 流传输（sha256 校验一致）。
- 挂载与安全：./data:/data 读写（容器内 uid 10001 写入，宿主目录 777），./vault:/vault:ro；
  .env 权限 600；APP_PASSWORD 已启用（未登录 303 转登录页，错误口令 401）。
- 回退：workbench/rollback.sh 执行 docker compose -p workbench down 与 down --rmi local，
  故意不删除 data/；彻底移除需人工执行并先备份 app.db。

## 13. 宿主端口可配置（2026-10-20）

- 对外端口与容器端口解耦：容器内始终监听 8080（PORT、EXPOSE、HEALTHCHECK 均不动），
  compose 写为 "${APP_PORT:-8080}:8080"，可用 .env 的 APP_PORT 覆盖为任意宿主端口。
- 理由：非标准高位端口可显著降低被批量扫描与自动化探测命中的概率，且无需改镜像。
- 验证：宿主端口正常监听（0.0.0.0 与 [::]），/healthz 200，登录 303、页面 200；容器内 8080 不直接对外。

## 14. 前端可配置化改造（大模型 + Obsidian，2026-10-20）

### 14.1 目标
用户不再编辑 .env 或 docker-compose.yml 就能完成：大模型多套配置的增删改/切换/测试连接，以及
Obsidian 知识库目录的浏览与选择。改完立即生效，无需重启容器。

### 14.2 新增共享模块契约
app/services/secrets_store.py（新文件，只存密钥，不进数据库、不进 git）：
    def load() -> dict                     # 读取 DATA_DIR/secrets.json，不存在返回 {}
    def save(data: dict) -> None           # 原子写（临时文件+rename），权限 0600
    def get_api_key(profile_id: str) -> str
    def set_api_key(profile_id: str, key: str) -> None
    def delete_api_key(profile_id: str) -> None
结构化：{"llm_api_keys": {profile_id: "sk-..."}}；文件路径 = DATA_DIR/secrets.json。

app/services/llm.py 扩展（保留原有 generate/is_mock_mode/health 语义）：
    PRESETS: dict                          # deepseek / openai / kimi / ollama / custom
    def list_profiles() -> list[dict]      # 每项含 id,name,provider,base_url,model,temperature,max_tokens,requires_key,has_key,active
    def get_profile(pid: str) -> dict | None
    def active_profile() -> dict
    def set_active(pid: str) -> None
    def upsert_profile(data: dict) -> str  # data 不含 api_key；id 为空则新建（slug 命名）
    def delete_profile(pid: str) -> None   # 同时删除其密钥；删掉最后一个时自动回落到 env 兜底
    def save_api_key(pid: str, key: str) -> None
    def resolve_config() -> dict           # 生效参数：profiles(DB) > env > 默认
    async def health(profile_id=None, override: dict | None = None) -> dict
        # 返回 {"mode":"mock|live","provider":..,"model":..,"base_url":..,"ok":bool,"error":..,"latency_ms":int,"reply_snippet":..}
    is_mock_mode(): 生效配置 requires_key 为真且没有 api_key 时才算 mock；ollama 类本地模型无 key 也算 live。
存储：settings 键 llm_profiles = JSON 数组；llm_active = 生效 profile id；密钥全部在 secrets.json。

app/services/obsidian.py 扩展：
    def vault_root() -> str                # 只读挂载根，env VAULT_ROOT 默认 /hostvaults
    def vault_path() -> str | None         # 当前选择的目录绝对路径（settings.vault_path）
    def set_vault_path(rel: str) -> dict   # 校验存在/可读/不越界，写入 settings.vault_path，返回状态
    def browse(rel: str = "") -> dict
        # {"current":"相对路径","abs":"绝对路径","parent":"上级相对路径或空",
        #  "dirs":[{"name","rel","md_count"}],"md_count":n,"writable":false}
    vault_available() 改为：settings.vault_path 存在且可读 → True；否则 False。
    路径遍历防护：解析后必须仍在 vault_root 之内（symlink 也要 resolve 后校验）。

### 14.3 新增 settings 键
llm_profiles（JSON 数组）、llm_active（profile id）、vault_path（相对 vault_root 的子路径，空=未选择）。

### 14.4 新增 HTTP 接口（不得改动 main.py 的注册方式：仍由 routes_reports / routes_obsidian 承载）
app/api/routes_reports.py（JSON）：
    GET  /api/llm/profiles            列表 + active
    POST /api/llm/profiles            新建或更新（body: id?,name,provider,base_url,model,temperature,max_tokens,requires_key）
    DELETE /api/llm/profiles/{pid}
    POST /api/llm/active              {"id": pid}
    POST /api/llm/key                 {"id": pid, "key": "sk-..."}（key 为空字符串表示删除）
    POST /api/llm/test                {"id": pid?, "override": {...}?} 立即试一次并返回 health 结构
app/api/routes_obsidian.py（JSON）：
    GET  /api/obsidian/browse?path=   目录浏览
    POST /api/obsidian/vault          {"path": "子路径"} 设置当前知识库
页面路由（app/web/pages_settings.py 承载，模板在 settings.html）：
    POST /settings/llm-profile       保存/新建配置（表单）
    POST /settings/llm-active        切换生效配置
    POST /settings/llm-key           保存或清除密钥
    POST /settings/llm-delete        删除配置
    POST /settings/llm-test          测试连接（htmx 片段优先）
    POST /settings/vault             设置知识库目录
    GET  /settings/browse            目录浏览（htmx 片段）

### 14.5 部署变更
docker-compose.yml 卷映射由 ./vault:/vault:ro 改为 ./vaults:/hostvaults:ro（只读），
env VAULT_ROOT 默认 /hostvaults。用户的库同步/复制到宿主机 workbench/vaults/ 下任一子目录即可被前端浏览选择。

### 14.6 文件归属（并行开发，互不越界）
- LLM 域：app/services/llm.py、app/services/secrets_store.py、app/api/routes_reports.py
- Obsidian 域：app/services/obsidian.py、app/api/routes_obsidian.py
- 设置 UI 域：app/web/pages_settings.py、app/templates/settings.html、app/templates/partials/settings_*.html、app/api/routes_settings.py
冻结：main.py、config.py、db.py、timeutil.py、templating.py、security.py、base.html、app.css、app.js、tasks.py、reminders.py、scheduler.py、reports.py

### 14.7 验收口径
1. 未配置 key：GET /api/llm/profiles 返回预设列表或空，is_mock_mode() 为 True，报告首行 MOCK 声明不变。
2. 新建 profile（deepseek 预设 + 假 base_url 指向本地假 OpenAI 服务）→ POST /api/llm/key 填 key →
   POST /api/llm/test 返回 ok=true 且 mode=live → 生成报告 is_mock=0；再从 UI 切到 ollama profile → 立即变 live（无 key 也可）。
3. 删除 active profile 后自动回落，页面不报错。
4. GET /api/obsidian/browse 能列出 vaults 下的目录与每个目录的 md 数量；POST /api/obsidian/vault 设置后
   reindex 只索引该目录；设置 ../ 之类越界路径被拒绝（返回可读错误）。
5. /settings 页面含「大模型」与「知识库」两张卡，无需编辑任何文件即可完成配置；htmx 测试连接返回片段。

### 14.8 实现裁决与部署后的补充约定（2026-10-20）

1. vault_path 语义（实现时发现的契约冲突，裁决如下）：
   空字符串 = 未选择知识库；根目录本身用 "." 表示。UI 在根目录隐藏「设为当前库」按钮并给提示。
2. llm.health 的 override 额外支持 api_key（一次性密钥）：仅用于本次测试请求，不写 secrets.json、
   不改变任何持久状态、不进日志；为空或缺省时退回已存密钥 / 环境变量 / MOCK 判定。
   routes_reports 的 POST /api/llm/test 原样透传，不打日志。
3. 设置页表单字段名（供后续维护与自动化测试使用）：
   POST /settings/llm-profile  name, preset, provider, base_url, model, temperature, max_tokens, requires_key, id(空=新建)
   POST /settings/llm-key      id, api_key, action(save|clear)
   POST /settings/llm-active   id
   POST /settings/llm-delete   id
   POST /settings/llm-test     id, 可选 base_url/model/api_key 覆盖
   POST /settings/vault        path（相对 /hostvaults 的子路径）
   GET  /settings/browse       ?path=（htmx 片段）
4. 部署记录（NAS 实测）：镜像重建后容器 healthy；/data/secrets.json 在 Linux 容器内为 0o600 (-rw-------)，
   Windows 开发机因 NTFS 不保存 POSIX 权限位，本地只能看到 666，属平台限制而非缺陷。
5. 前端完整链路已在本地与 NAS 双端验证：表单建配置 → 存密钥 → 设为生效 → 测试连接 → 报告 is_mock=0 →
   浏览目录 → 选库 → 重建索引 → 检索只命中当前库 → 删除配置自动回落。

## 15. 多用户模式（2026-10-20，开发中，未部署）

### 15.1 权限模型
- 角色：admin / user。管理员可查看与操作全部用户数据；普通用户只能看/改自己的。
- 隔离方式：所有业务行携带 owner_id；查询一律按 owner_id 过滤；跨用户访问返回 404（不暴露存在性）。
- 管理员查看他人数据用查询参数 ?as=<user_id>（页面与 JSON API 都支持），非法 id 回落到自己。
- 兼容：users 表为空时沿用旧的单口令模式（APP_PASSWORD 门禁），行为与改造前一致。

### 15.2 新增表（app/db.py，幂等 CREATE IF NOT EXISTS）
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL UNIQUE,
      display_name TEXT NOT NULL DEFAULT '',
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'user',
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL,
      last_login_at TEXT
    );
    CREATE TABLE IF NOT EXISTS invites (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      code TEXT NOT NULL UNIQUE,
      note TEXT NOT NULL DEFAULT '',
      created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
      max_uses INTEGER NOT NULL DEFAULT 1,
      used_count INTEGER NOT NULL DEFAULT 0,
      expires_at TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessions (
      token TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      ua TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at);

### 15.3 归属列（ALTER TABLE ADD COLUMN，可为 NULL 以兼容历史行）
- tasks.owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE
- projects.owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE
- reports.owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE
- progress_entries 通过 task 归属，不单独加列。
- 迁移函数：init_db() 内检测列是否存在（PRAGMA table_info），缺失则 ALTER TABLE ADD COLUMN 并建索引
  idx_tasks_owner / idx_projects_owner / idx_reports_owner。

### 15.4 密码与会话
- 哈希：hashlib.pbkdf2_hmac('sha256', pwd_bytes, salt_bytes, 200000)；存储格式
  pbkdf2_sha256$200000$<salt_hex>$<hash_hex>；校验用 hmac.compare_digest。
- 会话：登录成功生成 secrets.token_urlsafe(32) 写入 sessions（默认 30 天），cookie 名 wb_session，
  HttpOnly、SameSite=Lax、Path=/；不设 Secure（内网 http）。
- 每次请求校验：session 未过期 + users.active=1，否则视为未登录。
- 登录节流：同一 用户名+来源 5 分钟内失败 5 次 → 返回 429 并提示稍后再试（进程内计数即可）。
- 退出登录：删除该 session 行。

### 15.5 引导与迁移（app/db.py 或 app/services/users.py 的 bootstrap()，启动时调用一次，幂等）
1. 建表与加列（幂等）。
2. 若 users 为空且 APP_PASSWORD 非空：创建管理员，username=APP_ADMIN_USER（默认 admin）、
   password=APP_PASSWORD、role=admin、display_name=管理员。
3. 若 users 为空且 APP_PASSWORD 为空：不创建账号，应用保持旧的开放模式；/setup 页面允许创建首个管理员。
4. 若存在管理员且仍有 owner_id IS NULL 的历史行：一次性 UPDATE 归给最早的管理员（id 最小），并打印日志。
5. 重复启动不得重复创建账号、不得覆盖已有密码。

### 15.6 路由（新增，注意 main.py 由身份域统一接线）
页面：
    GET/POST /login          多用户模式：用户名 + 密码；单用户模式：沿用旧口令表单
    POST     /logout
    GET/POST /setup          仅当 users 为空时可用；创建首个管理员后跳 /
    GET/POST /register?code= 邀请码注册（code 无效/过期/超次 → 可读错误）
    GET      /admin/users    管理员：用户列表
    POST     /admin/users    创建用户（username/display_name/password/role）
    POST     /admin/users/{id}/password   重置密码
    POST     /admin/users/{id}/toggle     启用/停用（不得停用自己）
    POST     /admin/users/{id}/delete     删除（不得删除自己；有关联数据时要求先确认参数 confirm=1）
    GET      /admin/invites  邀请码列表 + 新建表单
    POST     /admin/invites  新建（note/max_uses/expires_days）
    POST     /admin/invites/{id}/revoke   作废
JSON：
    GET /api/me              当前用户 {id, username, display_name, role, active, as_id}

### 15.7 业务查询改造
- app/services/tasks.py 全部函数新增关键字参数 owner_id: int，必须按 owner_id 过滤；
  admin 视角传目标用户 id（?as=）或 None 表示全部（仅 admin 内部统计可用）。
  对外页面/API 一律传当前生效用户的 id，禁止无归属过滤的查询。
  同理 projects / progress（通过 join tasks）。
- app/services/reports.py 生成与列表按 owner_id 过滤；reports.owner_id 写入当前用户。
- 写操作校验归属：非本人的任务/报告，GET 与写一律 404。
- 提醒：reminders.scan(owner_id=None) 保持兼容；新增 scan_all() 返回按用户分组的列表；
  build_message_multi() 生成含「用户名」小节的合并日报；单用户模式下格式与改造前一致。

### 15.8 共享资源与设置页
- 全局（管理员维护，普通用户只读或不可见）：LLM 配置与密钥、Obsidian vault 选择、Webhook 地址与格式、
  每日扫描时刻。
- 每用户：工作项、进展、报告、仪表盘视图。
- 普通用户打开 /settings 时不显示「大模型」「知识库」「Webhook 保存」控件，改为一行说明「由管理员维护」；
  其余（如个人展示名）可用。
- 非管理员的写操作一律 403。

### 15.9 UI
- base.html 顶栏右侧：当前用户名 + 角色徽标 + 「退出」；管理员额外显示「用户管理」入口。
- 管理员在仪表盘/归档/报告页顶部有「查看用户」下拉（?as=<id>），默认自己。
- 登录页改造为用户名 + 密码；保留单用户模式的旧表单兼容。

### 15.10 文件归属（两轮并行）
第一轮（身份与表结构，其余域依赖它）：app/db.py、app/auth.py（新）、app/services/users.py（新）、
app/api/routes_auth.py（新）、app/web/pages_auth.py（新）、app/main.py、app/security.py、
app/templates/login.html、register.html、setup.html、admin_users.html、admin_invites.html、
app/templates/base.html、app/templates/partials/auth_*.html、app/static/auth.css
第二轮（并发）：
  任务与报告域：app/services/tasks.py、app/services/reports.py、app/api/routes_tasks.py、app/api/routes_reports.py、
    app/web/pages_tasks.py、app/web/pages_reports.py、对应模板与 partials
  提醒与设置域：app/services/reminders.py、app/services/scheduler.py、app/api/routes_settings.py、
    app/web/pages_settings.py、app/templates/settings.html、partials/settings_*.html
冻结（本轮所有人不得改）：app/config.py、app/timeutil.py、app/templating.py、app/services/settings_store.py、
app/services/llm.py、app/services/secrets_store.py、app/services/obsidian.py、app/api/routes_obsidian.py、
requirements.txt、Dockerfile、docker-compose.yml、app/static/app.css、app/static/app.js、app/static/htmx.min.js

### 15.11 验收口径
1. 首次启动（APP_PASSWORD 非空）：自动创建 admin，历史行 owner_id 归 admin；重复启动不重复建号。
2. admin 登录 → 建普通用户 A/B → A 登录只见自己的数据；A 访问 B 的任务 id → 404。
3. 邀请码：管理员建码 → 未登录访问 /register?code= 成功注册并自动登录；码用过即失效（max_uses）。
4. 管理员用 ?as=<A> 能看 A 的仪表盘与报告；普通用户带 ?as= 无效，仍只看自己。
5. 停用 A 后 A 的会话立即失效（下一次请求跳登录）；管理员不能停用/删除自己。
6. 提醒：多用户下 send_daily 生成含各用户小节的合并日报；单用户模式格式不变。
7. 设置页：普通用户看不到大模型/知识库/Webhook 写控件；直接 POST 那些接口返回 403。
8. 单用户兼容：users 为空且 APP_PASSWORD 为空时，行为与改造前完全一致（开放访问）。

### 15.12 删除用户的两种数据处置（2026-10-20 追加，用户要求）

删除账号时必须显式选择名下数据怎么办，默认不删数据：

1. 转归（transfer，推荐默认）：把该用户的 tasks / projects / reports 的 owner_id 批量改成目标用户
   （默认是执行删除的管理员本人；管理员可选其他启用中的账号），然后删除账号行。
   历史归属不可恢复地改到目标账号名下，数据条数不变。
2. 彻底删除（purge）：连同名下的 tasks / projects / reports 一起删除（依赖 ON DELETE CASCADE）。
   必须在页面上二次确认，并在确认文案里显示将被删除的条数。

接口与实现约定：
- users.delete_user(uid, mode='transfer'|'purge', transfer_to=None) -> dict
    mode='transfer'：transfer_to 为空时取当前管理员；目标必须是启用中的用户；返回
    {"mode":"transfer","moved":{"tasks":n,"projects":n,"reports":n},"to":user_id}
    mode='purge'：返回 {"mode":"purge","deleted":{"tasks":n,"projects":n,"reports":n}}
- 仍受「最后一个启用管理员」保护：不能删除最后一个管理员，也不能把数据转归给已停用账号。
- HTTP：POST /admin/users/{id}/delete 表单字段 mode=transfer|purge、transfer_to=<用户id，可空>、confirm=1
    mode=transfer 不需要 confirm；mode=purge 必须 confirm=1，否则 303 回列表并提示待删条数。
- 页面：删除按钮旁提供下拉（转归给… / 彻底删除），选择后在按钮文案里回显将执行的动作。
- 服务端必须记录一条审计日志（logger.warning），包含被删用户、模式、条数、目标用户。
- 自测要点：user X 名下有 3 tasks + 1 report，transfer → 这 4 行 owner_id 变成管理员且行数不变；
  另一个 user Y 用 purge → 其名下行被删除且 jobs 行数随之减少；两种都不能删除最后一个管理员。

## 16. 例行工作（周期性固定工作，2026-10-20 开发中）

### 16.1 概念
例行工作 = 一直存在的固定周期事项（每天交日报、每周五交周报、每月月末报表、每季度复盘、半年总结、年度归档）。
用它建模板；系统按周期自动生成「本期任务」，每期独立完成，历史可回溯。
本期任务就是普通 tasks 行（带 routine_id + period_key），因此天然复用进展、提醒、报告、统计。

### 16.2 新表 routines（app/db.py，幂等）
    CREATE TABLE IF NOT EXISTS routines (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
      title TEXT NOT NULL,
      detail TEXT NOT NULL DEFAULT '',
      done_criteria TEXT NOT NULL DEFAULT '',
      freq TEXT NOT NULL,                 -- daily|weekly|monthly|quarterly|half_year|yearly
      due_rule TEXT NOT NULL DEFAULT 'period_end',   -- period_end | day_of_period
      due_day INTEGER,                    -- due_rule=day_of_period 时的第几天（1 基）
      due_time TEXT,                      -- HH:MM 可选
      priority INTEGER NOT NULL DEFAULT 2,
      project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
      tags TEXT NOT NULL DEFAULT '',
      active INTEGER NOT NULL DEFAULT 1,
      start_date TEXT,                    -- 从该日期所属周期开始，空=创建当天
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
tasks 新增两列（ALTER TABLE，可空）：
    routine_id INTEGER REFERENCES routines(id) ON DELETE CASCADE
    period_key TEXT      -- daily=2026-10-20, weekly=2026-W43, monthly=2026-10, quarterly=2026-Q4,
                         -- half_year=2026-H2, yearly=2026
唯一约束（防重复生成，部分索引）：
    CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_routine_period ON tasks(routine_id, period_key)
      WHERE routine_id IS NOT NULL;
普通任务 routine_id 为 NULL，不受影响。

### 16.3 周期与截止日算法（app/services/routines.py）
    period_of(freq, d) -> (period_key, period_start, period_end)   # ISO 周一起算；季度/半年按自然年
    due_date_of(routine, period) -> date
        due_rule='period_end'  → period_end
        due_rule='day_of_period' → period_start + (due_day-1)，超出 period_end 则夹到 period_end
        daily 恒为当天
    ensure_periods(owner_id=None, today=None) -> dict
        对每个 active 模板：算出当前 period_key；若 (routine_id, period_key) 不存在则创建一条本期任务：
        title/detail/done_criteria/priority/project_id/tags 复制模板；due_date=due_date_of；due_time 复制；
        status='todo'；week_start 取 due_date 所在 ISO 周周一；owner_id 与模板一致。
        返回 {"created":n,"skipped":n,"routines":n}。幂等，不补历史周期（只保证当前周期存在）。
    next_due_label(routine) -> str   # 给 UI 显示「本周期截止 10-25 周日」
    skip_period(routine_id, today=None) -> int   # 把本期实例置 status='cancelled'（跳过，不计逾期）

### 16.4 视图与各页取舍
- 新页面 /routines：模板列表（按 freq 分组）+「本期例行任务」清单（可勾完成）+ 新建/编辑/停用/删除。
- /routines/{id}：模板详情 + 历史（最近 24 期，含状态与完成时间）+ 编辑表单 + 跳过本期。
- 仪表盘 /：五分区**排除**例行实例（避免每日例行刷屏）；新增一张「本期例行」卡片，
  显示「未完成 n / 共 m」与前 5 条，链接到 /routines。
- 周视图 /week：默认排除例行实例；带 ?routines=1 时包含（页面上给一个「显示例行」开关）。
- 归档 /archive：排除例行实例（用户选择：归档只保留一次性成果）；例行的历史在 /routines/{id} 看。
- 提醒：**包含**未完成的本期例行（它们本来就是逾期/今日到期的任务）。
- 报告：包含例行实例，并在数据里标注「例行（weekly）」便于模型区分。
- 例行实例完成后 archived=1（复用现有状态机），但归档查询按 routine_id IS NULL 过滤。

### 16.5 生成时机
- 惰性：渲染 /、/week、/routines 前调用 ensure_periods(owner_id=当前用户)（管理员 ?as= 时对目标用户）。
- 兜底：每日调度任务在扫描提醒之前，对所有启用用户各调一次 ensure_periods()。
- 两者都幂等；唯一索引兜住并发重复。

### 16.6 接口
JSON（app/api/routes_routines.py，main.py 注册）：
    GET    /api/routines               列表（含本期状态）
    POST   /api/routines               新建
    GET    /api/routines/{id}          详情 + 历史
    PATCH  /api/routines/{id}          修改
    DELETE /api/routines/{id}          删除（默认仅停用；?purge=1 才级联删除实例）
    POST   /api/routines/{id}/skip     跳过本期
    POST   /api/routines/ensure        手动补生成（幂等，返回 created/skipped）
页面（app/web/pages_routines.py）：
    GET /routines、POST /routines、GET /routines/{id}、POST /routines/{id}/edit、
    POST /routines/{id}/toggle、POST /routines/{id}/delete、POST /routines/{id}/skip
模板：routines.html、routine_detail.html、partials/routines_*.html

### 16.7 文件归属与轮次
第一轮（周期内核，其余域依赖）：app/db.py、app/services/routines.py（新）、app/api/routes_routines.py（新）、
app/main.py（注册路由）。
第二轮并发：
  UI 域：app/web/pages_routines.py（新）、routines.html、routine_detail.html、partials/routines_*.html、
    app/web/pages_tasks.py（仪表盘/周视图/归档的例行过滤与「本期例行」卡片）、dashboard.html、week.html、
    archive.html、partials/tasks_*.html（仅与例行相关处）、base.html（导航加「例行工作」）
  调度与报告域：app/services/reminders.py（扫描前 ensure）、app/services/scheduler.py（每日兜底）、
    app/services/reports.py（例行标注）、app/services/tasks.py（查询支持 exclude_routines/include_routines）
冻结：app/auth.py、app/security.py、app/services/users.py、app/services/llm.py、app/services/secrets_store.py、
app/services/obsidian.py、app/api/routes_auth.py、app/api/routes_reports.py、app/api/routes_settings.py、
app/api/routes_obsidian.py、app/web/pages_auth.py、app/web/pages_reports.py、app/web/pages_settings.py、
登录/注册/admin 模板、app.css、app.js、app/static/auth.css、requirements.txt、Dockerfile、docker-compose.yml

### 16.8 验收口径
1. 建「每周五交周报」（weekly, day_of_period=5）→ ensure 后生成 1 条本期任务，due_date 是本周周五，period_key 形如 2026-W43。
2. 重复 ensure 不重复生成（created 0/skipped 1），唯一索引生效。
3. 六种 freq 的 period_key 与 due_date 正确：daily=当天；weekly 周五；monthly 用 31 号在 2 月被夹到月末；
   quarterly 第 1 天/期末；half_year 与 yearly 同理；跨年周（12-29~01-04）period_key 取 ISO 周。
4. 本期任务完成后出现在 /routines/{id} 历史，但**不**出现在 /archive。
5. 仪表盘不显示例行实例，改在「本期例行」卡片；/week 默认不显示，?routines=1 显示。
6. 未完成的本期例行出现在提醒三桶与合并日报里。
7. 跳过本期 → 状态 cancelled、不计逾期、不再提醒。
8. 多用户：alice 的例行对 bob 不可见；管理员 ?as=alice 可生成/查看。
9. 停用模板后不再生成新实例，已有实例保留。

## 17. 运行约束与容量假设（P1 起生效）

### 17.1 worker 数与定时推送

APScheduler 的 BackgroundScheduler 在每个进程内各跑一份，因此定时推送天然按进程复制。
从 P1 起用 SQLite 的 settings.reminder_lock 做**抢占式日锁**：扫描任务开始前先尝试把
reminder_lock 的值推进到今天，单条 UPDATE 原子，只有抢到的进程继续执行。

- 默认部署仍是 **单 worker**（Dockerfile CMD 未加 --workers），这是最省心的形态；
- 若要加 --workers N（N>1），**必须**确认日锁已生效（P1 之后默认生效），否则会重复推送；
- 抢锁失败是正常路径，不记错误日志，只记一条 info。

### 17.2 数据库连接

P1 起改为**线程本地连接复用**：同一线程复用一条连接，不再每次查询 open/close。
WAL 模式 + busy_timeout=5000 提供并发保障。

- 只读查询不进显式事务；写操作通过 cursor() 上下文提交/回滚；
- 线程退出时连接会残留，需由 close_threadlocal() 释放（测试 teardown 与进程退出路径调用）；
- 单进程最坏连接数约等于 anyio 线程池大小（默认 40），SQLite 单写多读可承受。

### 17.3 登录节流

失败次数落库到 login_attempts（username + source + at，5 次 / 5 分钟窗口），
重启不清零、多 worker 一致；每次记录顺带清理过期行，随机用户名不会撑大内存。

## 18. P1 改造记录与偏差（v0.2.0）

### 18.1 W-08 拆分：与方案文件清单的偏差

方案 W-08 给出的文件清单是**估算**，实测体量装不下，故按「单文件 ≤ 400 行」这一目标调整：

| 域 | 方案清单 | 实际清单 | 实际行数 | 偏差原因 |
|---|---|---|---|---|
| settings | __init__/general/llm/webhook/users（5） | 另加 shared、llm_context、vault（8） | 19/96/49/164/357/202/313/131 | 原文件有效代码 1176 行；users 域在本文件里只有权限判定（49 行）、webhook 只有 131 行，两域装不满；general+llm 合计 1012 行，无法塞进 2×400 |
| report | __init__/collect/prompt/render（4） | 一致 | 207/307/310/307 | 无偏差。generate 与落库留在 __init__，render 只负责渲染 |
| obsidian | 方案未列 | obsidian.py 门面 + vault/index/search | 36/353/376/268 | 937 行塞不进 1 个模块；按目录层/索引层/检索层三拆，依赖为单向 |

### 18.2 拆分中的三处结构性发现

1. **权限层与渲染骨架存在真实循环依赖**：写权限校验调用 403 页渲染，而渲染骨架所在模块的写路由又要权限校验。
   解法是把渲染函数的 import 放进 403 处理函数体内（全仓唯一一处 AST 差异，行为等价）。
   彻底消环需要把「403 页渲染」从渲染骨架中抽出，属改逻辑，留给后续。
2. **logger 名字是隐性契约**：三个原模块的 getLogger(__name__) 名字（app.services.reports / app.services.obsidian /
   app.web.pages_settings）被保留为显式常量，避免线上日志过滤与告警规则静默失效。
3. **obsidian.py 是稳定门面而非兼容层**：pages/routes/templating/report 域都通过它取符号，三个子模块只被它引用。

### 18.3 W-07 的一处方案偏差

方案写 record_failure 返回 0，但调用方 pages_auth.py 把它当作「窗口内第几次失败」，用于提示文案与当次 429 判定。
判定：方案的 return 0 是无意义占位，调用方的计数语义才是真契约，故改为在单事务内 INSERT → 清理过期行 → 返回计数。
结果：第 5 次失败当次即返回 429，提示不再出现「第 0 次」。

### 18.4 部署记录

- 时点：2026-10-21；接入方式：外网 IPv6（2408:821a:e139:8c0:dc53:1ff8:557:f214）
- 回滚点：workbench:prev6；数据备份：backup/app-20261021-093130.db
- 验证：9 页 200；/settings 11 path / 12 method 与拆分前一致；docx 导出 200；
  login_attempts 表与 reminder_lock 键就位；tasks=2 users=1 routines=0 reports=0 未变
