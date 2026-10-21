"""FastAPI 应用工厂：静态资源、路由注册、可选口令鉴权、生命周期钩子。

路由归属见 SPEC.md 第 6 节：
  - app/api/routes_*.py  -> include_router(..., prefix="/api")
  - app/web/pages_*.py   -> include_router(...) 不加前缀
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import db, security
from app.api import (
    routes_auth,
    routes_obsidian,
    routes_reports,
    routes_routines,
    routes_settings,
    routes_tasks,
)
from app import brand
from app.api import routes_desktop
from app.config import APP_DIR
from app.web import (
    pages_auth,
    pages_obsidian,
    pages_reports,
    pages_routines,
    pages_tasks,
)
from app.web import pages_desktop
from app.web.settings import router as settings_router

logger = logging.getLogger("app.main")

PLACEHOLDER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>仪表盘 · 工作志</title>
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <a class="brand" href="/">工作志</a>
    <nav class="nav">
      <a href="/" class="active">仪表盘</a>
      <a href="/week">周视图</a>
      <a href="/projects">项目</a>
      <a href="/archive">归档</a>
      <a href="/reports">报告</a>
      <a href="/settings">设置</a>
    </nav>
  </div>
</header>
<main class="container">
  <div class="alert alert-info">
    阶段一骨架已就绪：应用可启动、数据库已建表、静态资源已就位。仪表盘内容由任务域实现后接管本页。
  </div>
  <div class="card">
    <div class="card-head"><h2 class="card-title">后续交付</h2></div>
    <div class="card-body">
      <ul class="plain-list">
        <li>仪表盘：逾期 / 今日 / 3 日内 / 本周 / 无 DDL 五区 + 快速添加</li>
        <li>周视图、工作详情与进展时间线、归档恢复</li>
        <li>报告中心与 LLM 生成（MOCK 模式可用）</li>
        <li>Reminder 扫描与 Webhook 推送</li>
        <li>Obsidian 只读索引与检索</li>
      </ul>
    </div>
  </div>
</main>
<footer class="statusbar">
  <div class="statusbar-inner">
    <span>骨架版本 0.1.0</span>
  </div>
</footer>
</body>
</html>
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    try:
        from app.services import users

        info = users.bootstrap()
        logger.info("用户初始化完成：%s", info)
    except Exception:
        logger.exception("用户初始化失败（不阻塞启动）")

    try:
        from app.services import scheduler

        scheduler.start()
        logger.info("调度器已启动")
    except NotImplementedError:
        logger.info("调度器尚未实现，本次跳过")
    except Exception:
        logger.exception("调度器启动失败，已跳过（不阻塞启动）")

    try:
        yield
    finally:
        try:
            from app.services import scheduler

            scheduler.shutdown()
        except NotImplementedError:
            pass
        except Exception:
            logger.exception("调度器停止失败")
        # W-05/SPEC 17.2：进程退出前释放本线程缓存的连接（close_threadlocal 幂等）。
        db.close_threadlocal()


def create_app() -> FastAPI:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    app = FastAPI(
        title=brand.FULL_NAME,
        version=brand.VERSION,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # 中间件最先注册：其余路由都在它后面，顺序不影响鉴权覆盖面。
    app.add_middleware(security.AuthMiddleware)
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

    # 静态资源每次向服务端校验：配合模板里的 ?v=asset_v()，避免部署后浏览器继续用旧 app.css
    # （曾导致侧边栏样式不生效、页面排版错乱）。
    @app.middleware("http")
    async def static_no_cache(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    # /healthz 由 main.py 独占；/login、/logout、/setup、/register 由 pages_auth 提供（SPEC 15.6）。
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    # API 路由统一加 /api 前缀：routes_*.py 里写 @router.get("/tasks") 即 /api/tasks。
    app.include_router(routes_auth.router, prefix="/api", tags=["auth"])
    app.include_router(routes_tasks.router, prefix="/api", tags=["tasks"])
    app.include_router(routes_routines.router, prefix="/api", tags=["routines"])
    app.include_router(routes_reports.router, prefix="/api", tags=["reports"])
    app.include_router(routes_settings.router, prefix="/api", tags=["settings"])
    app.include_router(routes_obsidian.router, prefix="/api", tags=["obsidian"])
    # 桌面壳专用接口：非桌面进程没有注入 backend，访问一律 404（SPEC 第 20 章）
    app.include_router(routes_desktop.router, prefix="/api", tags=["desktop"])

    # 页面路由不加前缀，路径即 SPEC 第 6 节的页面路径。
    app.include_router(pages_auth.pages)
    app.include_router(pages_tasks.pages)
    app.include_router(pages_routines.pages)
    app.include_router(pages_reports.pages)
    app.include_router(settings_router)
    app.include_router(pages_obsidian.pages)
    app.include_router(pages_desktop.pages)

    # 阶段一占位：pages_tasks 提供真实仪表盘后，先注册的同路径路由会自动遮蔽本占位，
    # 无需改动 main.py。
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_placeholder() -> HTMLResponse:
        return HTMLResponse(PLACEHOLDER_HTML)

    return app


app = create_app()
