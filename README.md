# 工作台项目

个人工作流程跟踪系统：手动登记每周工作（详情 / 完成条件 / DDL），随时追加进展；未完成自动提醒，
完成后销号归档；数据落本地 SQLite；单镜像 Docker 部署到 NAS；接大模型生成单项与任意区间的
工作总结；只读接入 Obsidian 知识库增强报告。

- 技术栈：Python 3.12 · FastAPI · Jinja2 服务端渲染 · htmx（本地 vendored）· SQLite · APScheduler · httpx
- 无 CDN、无前端构建链、无 ORM；密钥只从环境变量 / .env 读取，绝不写库
- 契约文件：SPEC.md（唯一权威）

---

## 1. 功能概览

| 模块 | 能力 |
| --- | --- |
| 仪表盘 / | 逾期 / 今日 / N 日内 / 本周 / 无 DDL 五区 + 快速添加 |
| 周视图 /week | ISO 周按天分组，支持上周/本周/下周 |
| 工作详情 /tasks/{id} | 详情、完成条件、进展时间线、状态切换、编辑、删除 |
| 归档 /archive | 已完成 / 已取消，按完成周分组，可一键恢复 |
| 项目 /projects | 项目列表与新建（颜色标识） |
| 报告 /reports | 单项 / 周 / 月 / 半年 / 年 / 任意区间总结，Markdown 渲染、打印、复制原文 |
| 提醒（站内 + Webhook） | 每日定时扫描，逾期 / 今日 / 临期三桶；6 种 Webhook 格式；reminders_log 留痕 |
| 设置 /settings | 扫描时刻、临期阈值、Webhook 地址与格式、Telegram chat_id；系统信息；最近一次扫描摘要；测试推送 / 立即扫描 |
| Obsidian（只读） | vault 索引 + 全文检索，作为报告的“相关知识库笔记”背景参考 |
| 部署 | Dockerfile（非 root + HEALTHCHECK）、docker-compose、备份脚本、冒烟测试脚本 |

提醒分区口径（SPEC 第 7 节第 3 条，仅统计 status 为 todo/doing 且 archived=0 的工作）：

- 逾期：due_date < 今天
- 今日：due_date = 今天
- 临期：今天 < due_date <= 今天 + remind_soon_days
- 无 DDL：due_date 为空（不进提醒区）

---

## 2. 架构与数据模型

### 2.1 分层

    app/
      config.py            环境变量汇总（TZ / DATA_DIR / 密钥 / LLM / Webhook）
      timeutil.py          业务时区、ISO 周、DDL 天数
      db.py                SQLite 连接（Row 工厂 / WAL / foreign_keys）+ DDL + init_db()
      security.py          可选口令鉴权中间件（APP_PASSWORD 为空即跳过）
      templating.py        Jinja2 环境、过滤器（md / fmt_cn / status_label / priority_label）
      main.py              app 工厂、路由注册、lifespan（启动/停止调度器）
      services/            业务层：tasks / reports / llm / obsidian / reminders / scheduler / settings_store
      api/                 JSON API（router，统一挂 /api 前缀）
      web/                 页面路由（pages，无前缀，Jinja2 渲染）
      templates/           base.html + 各页面 + partials/ 片段
      static/              app.css / app.js / htmx.min.js / settings.css
    scripts/               backup.sh（VACUUM INTO 备份）、smoke_test.py（冒烟测试）
    Dockerfile  docker-compose.yml  .env.example  .dockerignore

请求流：浏览器 → AuthMiddleware（可选口令）→ 页面路由（app/web/pages_*.py）→ 服务层 →
db（SQLite）。所有变更都可用原生 form method=post 完成；htmx 仅在追加进展、状态切换、
测试推送三处做局部刷新，服务端按 HX-Request 头决定返回片段还是 303 重定向。

### 2.2 数据模型（SQLite，DATA_DIR/app.db）

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| projects | name(唯一) / color / archived | 项目 |
| tasks | title / detail / done_criteria / due_date / due_time / status / priority / week_start / tags / completed_at / archived | 工作；status 取 todo/doing/done/cancelled；priority 1低 2中 3高；week_start 为所属 ISO 周周一 |
| progress_entries | task_id(FK, CASCADE) / content / link | 进展条目 |
| reports | kind / scope_label / range_start / range_end / task_id / content_md / provider / model / is_mock / context_notes | 报告 |
| settings | key(主键) / value | 非密钥配置（remind_time、remind_soon_days、webhook_url、webhook_format、telegram_chat_id、last_scan_json…） |
| reminders_log | sent_at / kind / channel / payload / ok / error | 每次推送/扫描的留痕 |
| notes_index + notes_fts | path(唯一) / title / tags / body / mtime | Obsidian 只读索引（FTS5，trigram 不可用时降级 unicode61） |

约定：SQLite 使用 WAL + foreign_keys=ON；时间一律 ISO8601 字符串，业务时区 TZ_NAME；
status=done 即 archived=1 且写 completed_at，立刻离开所有活跃视图。

### 2.3 提醒链路

    APScheduler CronTrigger(remind_time, TZ_NAME)
        └─ reminders.send_daily()
              ├─ scan()           只读 tasks 表分三桶（不依赖任务域代码）
              ├─ build_message()  中文纯文本日报：标题含日期 + 三段计数 + 每段最多 10 条
              ├─ push()           按 webhook_format 序列化 → httpx POST（超时 10s，失败重试 1 次）
              ├─ 写 reminders_log（无待提醒项也写一行，ok=0 + 原因）
              └─ settings.last_scan_json ← 扫描摘要（设置页“最近一次扫描”数据源）

---

## 3. 本地运行

    # 1) 安装依赖（建议独立虚拟环境，不要污染系统 Python）
    python -m venv .venv
    .venv/Scripts/python.exe -m pip install -r requirements.txt      # Windows
    .venv/bin/python -m pip install -r requirements.txt              # Linux / macOS

    # 2) 配置（全部可省略，省略即用默认值）
    cp .env.example .env

    # 3) 启动（首次启动自动建库 + 生成 DATA_DIR/.secret）
    .venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080

    # 4) 验证
    curl http://127.0.0.1:8080/healthz          # {"ok":true}
    curl http://127.0.0.1:8080/api/reminders/preview

常用自测：

    # 冒烟测试（全链路，逐项 PASS/FAIL/SKIP）
    .venv/Scripts/python.exe scripts/smoke_test.py --base http://127.0.0.1:8080

    # 隔离数据目录跑一次（不污染 data/app.db）
    DATA_DIR=/tmp/wb-smoke .venv/Scripts/python.exe -m uvicorn app.main:app --port 8090

APP_PASSWORD 非空时，除 /healthz、/login、/static/* 外全部需要登录；会话 cookie
wb_session = hex(hmac_sha256(APP_SECRET, APP_PASSWORD))。

---

## 4. Docker 构建与启动

    # 构建（单镜像 python:3.12-slim，非 root 运行，内置 HEALTHCHECK）
    docker build -t workbench:latest .

    # 准备目录与环境文件
    mkdir -p data vault backup
    cp .env.example .env      # 按需填写 LLM_API_KEY / APP_PASSWORD / WEBHOOK_URL …

    # 启动
    docker compose up -d
    docker compose ps         # STATUS 里出现 (healthy) 表示 /healthz 通过
    docker compose logs -f workbench

    # 升级（改了代码或依赖后）
    docker compose build && docker compose up -d

镜像要点（与 SPEC 第 8 节一致）：

- FROM python:3.12-slim，pip install --no-cache-dir -r requirements.txt
- 非 root：useradd --uid 10001 workbench + USER workbench
- EXPOSE 8080；CMD uvicorn app.main:app --host 0.0.0.0 --port 8080
- HEALTHCHECK 用容器内 python 打 http://127.0.0.1:8080/healthz（不额外装 curl）
- 容器内已装 tzdata（zoneinfo 解析 Asia/Shanghai）与 sqlite3（可直接跑备份脚本）

---

## 5. NAS 部署步骤

整体思路：把仓库放到 NAS 的固定目录，data/ 与 vault/ 用宿主机目录映射进容器，
.env 放宿主机，容器只负责跑应用；备份由宿主机 cron 调 scripts/backup.sh。

### 5.1 群晖 DSM（Container Manager / Docker）

1. 套件中心安装 Container Manager；SSH 登录 NAS（或开 Terminal & SNMP）。
2. 目录准备（示例：卷 1 的 docker/workbench）：

       mkdir -p /volume1/docker/workbench/{data,vault,backup}
       cd /volume1/docker/workbench
       # 方式 A：git clone 仓库到当前目录；方式 B：把仓库文件用 File Station 上传到当前目录
       cp .env.example .env && vi .env
       chown -R 10001:10001 data backup      # 容器内 uid 10001，属主对不上会写不进库

3. 启动：

       docker compose up -d          # 老版本用 docker-compose up -d

4. 目录映射（compose 里的相对路径，等价绝对路径写法）：

       ./data  →  /volume1/docker/workbench/data  →  容器 /data   （读写，SQLite）
       ./vault →  /volume1/docker/workbench/vault →  容器 /vault  （只读，Obsidian）

   Container Manager 图形界面等价配置：新增项目 → 选择 docker-compose.yml → 确认
   “卷”里两条映射与“环境”里的 TZ=Asia/Shanghai；端口写法是「宿主端口 → 容器 8080」
   （建议把左侧改成非标准端口，降低被批量扫描发现的概率；容器内端口不要动）。
5. 反向代理（可选）：DSM 套件 反向代理服务器 加规则 https://wb.example.com → http://127.0.0.1:8080，
   并启用 HSTS；外网暴露前务必设置 APP_PASSWORD。

### 5.2 QNAP QTS（Container Station）

1. App Center 安装 Container Station；控制台开启 SSH。
2. 目录准备（示例：/share/Container/workbench）：

       mkdir -p /share/Container/workbench/{data,vault,backup}
       cd /share/Container/workbench
       cp .env.example .env && vi .env
       chown -R 10001:10001 data backup

3. 启动：docker compose up -d。Container Station 里也能看到名为 workbench 的容器，
   端口映射为「宿主端口 → 容器 8080」、环境变量 TZ=Asia/Shanghai、卷映射 data → /data（读写）与
   vault → /vault（只读）应与 compose 一致。
4. 访问 http://NAS:<映射端口>（容器内仍监听 8080；宿主端口可改 compose 左侧或 .env 的 APP_PORT）。
5. 自动启动：compose 已设 restart: unless-stopped，NAS 重启后容器会自动拉起。

### 5.3 其他 NAS / 群晖旧内核注意

- 只支持 x86_64 / arm64 官方镜像；32 位 ARM 的旧机型无法运行。
- 磁盘建议放 SSD 或“存储池 1”的固定路径，不要用休眠盘（SQLite WAL 对休眠不友好）。
- 时间必须正确（NTP 开启），否则“今天”与定时扫描时点会漂移。

---

## 6. 环境变量表

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| DATA_DIR | ./data | 数据目录，数据库为 DATA_DIR/app.db（Docker 内固定 /data） |
| TZ_NAME | Asia/Shanghai | 业务时区，影响“今天”、扫描时刻、DDL 逾期天数 |
| PORT | 8080 | 容器内监听端口（uvicorn 命令行参数，供脚本/文档引用） |
| APP_PORT | 8080 | 宿主机对外端口（compose 映射左侧；建议改成非标准端口） |
| APP_PASSWORD | 空 | 非空即启用口令登录；空 = 不鉴权（内网自用） |
| APP_SECRET | 空 | 会话签名密钥；空时首次启动生成并落盘 DATA_DIR/.secret |
| VAULT_DIR | /vault | Obsidian vault 只读目录；不存在则禁用该功能 |
| LLM_BASE_URL | https://api.deepseek.com/v1 | OpenAI 兼容 base（末尾不带 /） |
| LLM_API_KEY | 空 | 空即 MOCK 模式（模板渲染 + 醒目 MOCK 声明） |
| LLM_MODEL | deepseek-chat | 模型名 |
| LLM_TIMEOUT | 120 | 单次请求超时（秒） |
| LLM_MAX_TOKENS | 2000 | 单次生成最大 token |
| LLM_TEMPERATURE | 0.3 | 采样温度 |
| REMIND_TIME | 09:00 | 每日扫描时刻（设置页里的值优先） |
| WEBHOOK_URL | 空 | 空即只做站内提醒 |

密钥（APP_PASSWORD / APP_SECRET / LLM_API_KEY）只从环境或 .env 读，绝不写库、绝不进 git；
settings 表只存非密钥项（提醒时间、临期天数、Webhook 地址与格式、Telegram chat_id 等）。

---

## 7. 大模型接入（全部在网页里完成）

打开「设置 → 大模型」卡片即可，**不需要改 .env，也不需要重启容器**。

### 7.1 三步配好

1. 「新建配置」→ 选预设（DeepSeek / OpenAI / Kimi / Ollama / 自定义），base_url 与 model 自动填好，可按需修改。
2. 填 API Key → 点「保存密钥」。页面只显示「已保存 / 未设置」，**永不回显明文**。
3. 点「测试连接」→ 显示 模式 / 厂商 / 模型 / 延迟 / 回复片段；确认可用后点「设为生效配置」。

可以并存多套配置（例如「DeepSeek 正式」与「Ollama 本地」），下拉切换立即生效，不用重启。

### 7.2 预设对照

| 预设 | base_url | 默认模型 | 需要 Key |
|---|---|---|---|
| DeepSeek | https://api.deepseek.com/v1 | deepseek-chat | 是 |
| OpenAI | https://api.openai.com/v1 | gpt-4o-mini | 是 |
| Kimi | https://api.moonshot.cn/v1 | moonshot-v1-8k | 是 |
| Ollama | http://host.docker.internal:11434/v1 | qwen2.5:7b | 否 |
| 自定义 | 自行填写 | 自行填写 | 自行勾选 |

Ollama 注意：容器内访问宿主机服务时，Linux 上 host.docker.internal 不一定存在，直接填 NAS 内网 IP，
例如 http://192.168.1.10:11434/v1（先在宿主机 ollama serve && ollama pull qwen2.5:7b）。

### 7.3 密钥存在哪

DATA_DIR/secrets.json，权限 600，**不进数据库、不进 git**。
所以「复制 app.db 备份」不会把 API Key 一起带走。

### 7.4 环境变量兜底（可选）

一套配置都不建时，仍读取 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（用于纯命令行场景）。
前端配置优先级高于环境变量；切换配置后立即生效。

### 7.5 MOCK 模式

需要 Key 的配置没有 Key 时进入 MOCK：报告用真实入库数据做确定性模板渲染，正文首行固定插入
“MOCK 模式：未配置 LLM_API_KEY，本报告为模板渲染，非大模型生成”，页脚显示 MOCK 徽标。
Ollama 这类不需要 Key 的配置不会被误判为 MOCK。

报告生成是同步阻塞请求（30~60s 常见），按钮有 loading 态并提示“生成中，请勿关闭页面”。
排查：设置页「测试连接」，或 curl /api/llm/health。

---

## 8. Obsidian 知识库接入（全部在网页里完成）

只读接入，绝不写回 vault。

    # compose（已内置）：只读挂载一个"根目录"，具体库在网页里选
    volumes:
      - ./data:/data
      - ./vaults:/hostvaults:ro

    # 宿主上对应的目录（群晖示例）
    /volume1/docker/workbench/vaults/         # 把库放这里的任意子目录

步骤（不需要改 .env，也不需要改 compose）：

1. 把你的 Obsidian 库同步/复制到宿主机的 `workbench/vaults/` 下，例如
   `workbench/vaults/我的知识库/`（Obsidian Sync、Syncthing、直接拷贝都可以）。
2. 打开网页「设置 → 知识库」：目录列表会显示每个子目录及其 Markdown 篇数，
   点进去逐级浏览，选定后点「选为当前库」。
3. 同一张卡上点「重建索引」，页面会显示扫描/更新/跳过/清理的真实数字。
4. 之后生成工作总结时会自动检索该库并作为「相关知识库笔记」附在数据区。

说明：

- 路径安全：只接受根目录内的相对子路径；`../`、绝对路径、指向根外的软链接一律拒绝。
- 换库：在同一张卡里选另一个目录再重建索引，旧库的索引行会被自动清理，检索只命中当前库。
- 未选择知识库时不会报错：报告照常生成，仅在正文末尾注明「本次未使用知识库上下文」。
- 索引落在 notes_index + notes_fts（FTS5 / trigram，不可用时自动降级 unicode61）。
- 报告生成时以「相关知识库笔记」附在数据区，并在 Prompt 里声明仅作背景参考、禁止据此编造事实。

---

## 9. Webhook 六种平台配置示例

在 /settings 选择“Webhook 格式”并填地址（或写环境变量 WEBHOOK_URL）。
每日扫描触发一次推送；设置页“发送测试推送”可立即验证。

| 格式 | 目标平台 | 请求体（SPEC 第 7 节第 5 条） |
| --- | --- | --- |
| generic_json | 自建服务 / n8n / 本地脚本 | {"text": msg} |
| wecom_text | 企业微信群机器人 | {"msgtype":"text","text":{"content":msg}} |
| feishu_text | 飞书自定义机器人 | {"msg_type":"text","content":{"text":msg}} |
| serverchan | Server 酱（微信） | {"title":title,"desp":msg} |
| telegram | Telegram Bot | {"chat_id":...,"text":msg} |
| bark | Bark（iOS 推送） | {"title":title,"body":msg} |

### 9.1 企业微信（wecom_text）

    # 群设置 → 添加群机器人 → 复制 Webhook
    WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx
    # 设置页：格式选“企业微信群机器人”

### 9.2 飞书（feishu_text）

    WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx
    # 自定义机器人安全设置里若开了“签名校验”，本项目的 payload 不含签名，请改用“IP 白名单”或关键词

### 9.3 Server 酱（serverchan）

    WEBHOOK_URL=https://sctapi.ftqq.com/SCTxxxxxxxxxxxxxxxx.send
    # 正文过长时 Server 酱会截断，本项目每段最多列 10 条正是为了控制长度

### 9.4 Telegram（telegram）

    # BotFather 建 bot 拿 token，chat_id 用 @channel 或数字 id（getUpdates 可查）
    WEBHOOK_URL=https://api.telegram.org/bot123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxx/sendMessage
    # 设置页：格式选 Telegram，并填 chat_id（例如 123456789 或 @mychannel）
    # 内网无法直连时可在 NAS 上配代理或改用自建中转（generic_json 转发）

### 9.5 Bark（bark）

    # iOS 安装 Bark，App 里拿到 device key
    WEBHOOK_URL=https://api.day.app/YourDeviceKey
    # 格式选 Bark：{"title":..., "body":...}

### 9.6 通用 JSON（generic_json）

    # 任何能收 POST JSON 的服务；本地自测可用 20 行 echo 服务
    WEBHOOK_URL=http://127.0.0.1:9091/hook

留空 WEBHOOK_URL 时：只写 reminders_log（channel=disabled、ok=0、error 写明未推送原因），
页面不报错，设置页“最近一次扫描”照常显示计数与时间。

推送行为：同步 httpx.post，超时 10s，失败重试 1 次；结果（成功/失败/原因、payload 前 4000 字符）写
reminders_log，可在设置页与 /api/reminders/preview、/api/reminders/test 侧观察。

---

## 10. 备份与恢复

### 10.1 备份（在线、一致性快照）

    # 默认：./data/app.db → ./backup/app-YYYYmmdd-HHMMSS.db，保留最近 14 份
    scripts/backup.sh

    # NAS 上指定目录与保留份数
    DATA_DIR=/volume1/docker/workbench/data \
    BACKUP_DIR=/volume1/docker/workbench/backup \
    KEEP=30 scripts/backup.sh

    # 群晖任务计划（每天 03:30）
    # /volume1/docker/workbench/scripts/backup.sh >> /volume1/docker/workbench/backup/backup.log 2>&1

要点：

- 使用 sqlite3 "VACUUM INTO '文件'"：单个读事务内的一致性快照，应用写入时也可安全执行，
  不需要停服（脚本顶部注释里说明了为什么默认不 sleep，以及 BACKUP_PRE_SLEEP 的用途）。
- 备份后用 PRAGMA integrity_check 自检；非 ok 会在日志里 WARN。
- 轮转按修改时间倒序，超过 KEEP（默认 14）份自动删除。
- 容器内也已装 sqlite3，可 docker compose exec workbench bash scripts/backup.sh（注意 /data 挂载）。

### 10.2 恢复（从一个 app.db 文件恢复）

    # 1) 停服务，避免半途写入
    docker compose stop workbench          # 本地运行则 Ctrl-C

    # 2) 备份当前库（留后路），清掉 WAL/SHM 残留
    cd data
    mv app.db app.db.broken 2>/dev/null || true
    rm -f app.db-wal app.db-shm

    # 3) 放入备份文件（重命名成 app.db）
    cp ../backup/app-20251020-033000.db app.db

    # 4) 校验文件本身可用（可选）
    sqlite3 app.db 'PRAGMA integrity_check;'      # 期望输出 ok
    sqlite3 app.db 'SELECT COUNT(*) FROM tasks;'

    # 5) 起服务，页面抽查数据
    cd .. && docker compose start workbench

注意：

- 一定要同时删除 app.db-wal / app.db-shm，否则旧 WAL 可能覆盖恢复出来的数据。
- 数据库属主必须与容器 uid 一致：chown -R 10001:10001 data。
- 只想回滚某几条数据时，可以恢复到一个临时 DATA_DIR（DATA_DIR=/tmp/wb-tmp）后用
  sqlite3 / DB Browser 手工比对导出，再改回线上库。

---

## 11. 自动化冒烟测试

    # 本地开发默认 8080；容器部署请指向宿主机映射端口
    .venv/Scripts/python.exe scripts/smoke_test.py
    .venv/Scripts/python.exe scripts/smoke_test.py --base http://192.168.1.10:8080 --verbose

覆盖项（每项独立 try/except，单点失败不中断）：

| 编号 | 检查 |
| --- | --- |
| a | GET /healthz → 200 且 {"ok":true} |
| b | 建项目 → GET /api/projects 回读 |
| c | 建 5 条边界工作（昨天 / 今天 / +3 / +4 / 无 DDL） |
| d | GET /api/dashboard 四区计数断言（且 +4 不得落进 soon） |
| e | 追加进展 → 回读命中 |
| f | 标记完成 → 出现在 /archive → 恢复后离开归档 |
| g | 生成 task 报告且正文含 MOCK 声明或足够长度 |
| h | 生成 custom 报告 |
| i | GET /api/llm/health 含 mode |
| j | Obsidian 搜索（vault 未就绪 → SKIP，不算失败） |
| k | GET /settings → 200 |

退出码：全 PASS/SKIP 为 0，任一 FAIL 为 1（可直接接 CI 或 NAS 定时任务）。
注意：脚本使用固定标记“冒烟-HHMMSS”建数据，多次运行会在库里留下可识别的测试数据，
建议对独立的测试 DATA_DIR 跑，或跑完手工清理。

---

## 12. 主要接口速查

页面（Jinja2，无前缀）：

    GET  /                       仪表盘
    GET  /week?offset=0          ISO 周视图
    GET  /tasks/new   POST /tasks
    GET  /tasks/{id}  POST /tasks/{id}/edit|status|progress|delete
    POST /progress/{pid}/delete
    GET  /archive    GET /projects   POST /projects
    GET  /reports    POST /reports   GET /reports/{id}   POST /reports/{id}/delete
    GET  /settings   POST /settings
    POST /settings/test-push   POST /settings/reindex   POST /settings/scan-now
    GET  /login      POST /login     POST /logout
    GET  /healthz

JSON API（前缀 /api）：

    GET/POST /tasks    GET/PATCH/DELETE /tasks/{id}    POST /tasks/{id}/progress
    GET /dashboard     GET /week?offset=0     GET /projects
    GET/POST /reports  GET/DELETE /reports/{id}
    GET /llm/health    GET /obsidian/search?q=    POST /obsidian/reindex
    GET /reminders/preview                        POST /reminders/test

POST /settings/scan-now 为本项目设置页“立即扫描一次”按钮的入口（扫描 → 有 Webhook 时推送 →
写日志 → 刷新“最近一次扫描”）。

---

## 13. 常见问题

Q1. 时间不对 / 定时扫描没在 09:00 触发？
先确认容器 TZ=Asia/Shanghai（compose 已设）、.env 里 TZ_NAME=Asia/Shanghai，再确认宿主机 NTP 正常。
业务时区取自 TZ_NAME（zoneinfo），镜像内已装 tzdata；本地 Windows 若报 ZoneInfoNotFoundError，
pip install tzdata 即可。设置页改“每日扫描时刻”会立即热更新调度器，无需重启；next_run 可在
设置页“最近一次扫描”卡片右上角看到。

Q2. 端口被占用？
compose 默认是「宿主 8080 → 容器 8080」，可用 .env 的 APP_PORT 覆盖。冲突时改左侧，例如 ports: "18080:8080"，
容器内端口不要动。本地开发直接换端口：uvicorn app.main:app --port 8081。查占用：
Windows netstat -ano | findstr :8080；Linux/NAS lsof -i:8080 或 ss -lntp。

Q3. 数据目录/数据库权限报错（attempt to write a readonly database、unable to open database file）？
容器以 uid 10001 运行，宿主机目录必须让它可写：

    chown -R 10001:10001 data backup
    # 群晖/QNAP 若不用 chown：把 data 目录权限放到 0777（仅内网自用可接受）

另外不要把 data/ 放在只读挂载（:ro）里——只有 vault 是只读。

Q4. 中文路径能跑吗？
可以。项目内部统一 UTF-8；Windows 上建议用 bash（如 Git Bash / MSYS2）并 cd "/d/工作目录/工作台项目"
这种 POSIX 风格路径，避免某些工具把中文路径当成乱码。.env、DATA_DIR、VAULT_DIR 都支持中文，
但 Docker 卷映射建议用纯英文路径（部分 NAS 的 compose 解析器对非 ASCII 路径不友好）。

Q5. Webhook 一直失败？
先在设置页点“发送测试推送”看具体 error：HTTP 401/404 多为地址或 key 错；ConnectTimeout
多为 NAS 不能直连外网（Telegram / 飞书）；HTTP 45003（企业微信）是内容过长。
失败会重试 1 次并写 reminders_log，页面不会报错。留空地址时 channel=disabled 属正常。

Q6. 报告全是“MOCK”标记？
说明 LLM_API_KEY 为空，属预期行为（模板渲染 + MOCK 声明）。填 key 后重启或改设置页
llm_base_url / llm_model 覆盖项，再 curl /api/llm/health 确认 mode=live。

Q7. 数据要不要备份？怎么定时？
见第 10 节。推荐：群晖“任务计划”→ 每天 03:30 执行 scripts/backup.sh，
BACKUP_DIR 指到会被 Cloud Sync / Hyper Backup 同步的目录，这样就有异地副本。
恢复务必先停容器，并清掉 app.db-wal / app.db-shm。

Q8. 想清空重来？
停服务 → 删除 data/app.db* → 重启（自动重新建表并写入默认 settings）。backup/ 里的历史快照不受影响。

---

## 14. 多用户与账号管理

### 14.1 两种模式

| 模式 | 触发条件 | 行为 |
|---|---|---|
| 单用户（旧） | 数据库里没有任何用户账号 | 沿用单一访问口令（APP_PASSWORD），与早期版本完全一致 |
| 多用户 | 存在至少一个用户账号 | 登录用「用户名 + 密码」；工作项、进展、报告按用户隔离 |

首次启动多用户版时，若 APP_PASSWORD 非空，系统会自动创建一个管理员
（用户名取 APP_ADMIN_USER，默认 admin，密码即 APP_PASSWORD），
并把此前没有归属的历史数据一次性划归该管理员 —— 不需要手工搬数据。

若 APP_PASSWORD 为空且没有任何账号，应用保持旧的开放/单口令模式；
此时访问 /setup 可以手动创建第一个管理员。

### 14.2 权限

- 普通用户：只能看到与修改自己的工作项、进展、报告；访问他人资源一律 404（不暴露是否存在）。
- 管理员：可以查看全部用户的数据。仪表盘 / 归档 / 报告页顶部有「查看用户」下拉，
  选择后 URL 带 ?as=<用户id>，页面与接口都会以该用户身份渲染。
- 共享配置（大模型、知识库、Webhook、每日扫描时刻）由管理员维护；
  普通用户打开设置页只看到「由管理员维护」的只读说明，直接调写接口返回 403。

### 14.3 账号与邀请码

1. 管理员进入「用户管理」（顶栏用户名旁的入口）：
   - 新建账号：用户名 / 显示名 / 初始密码 / 角色（普通用户或管理员）
   - 重置密码、启用/停用、删除
2. 邀请码：在「用户管理 → 邀请码」里创建，可设最大使用次数与有效期；
   把链接 http://<NAS>:<映射端口>/register?code=<邀请码> 发给对方，对方自助注册并自动登录。
3. 停用账号会让其会话立即失效（下一次请求即被踢回登录页）。

### 14.4 删除账号时的数据处置

删除账号必须选择名下数据怎么办，默认不删数据：

- 转归（默认）：把工作项 / 项目 / 报告批量转到目标账号（默认执行删除的管理员），数据一条不少。
- 彻底删除：连带删除名下数据，页面上会先显示将被删除的条数并要求勾选确认。

系统不会允许删除或停用「最后一个启用中的管理员」，避免把自己锁在门外。

### 14.5 安全说明

- 密码使用 PBKDF2-HMAC-SHA256（200000 次迭代、随机盐）存储，不可逆。
- 会话保存在数据库，30 天过期；停用账号即删除其会话。
- 同一账号 5 分钟内连续 5 次密码错误会被临时限流（429）。
- 大模型 API Key 仍只存放于 DATA_DIR/secrets.json（600 权限），不进数据库、不进 git。
