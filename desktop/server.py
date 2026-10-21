"""在后台线程跑 uvicorn，仅监听 127.0.0.1（不对外暴露端口）。

关键顺序：先设置环境变量（DATA_DIR / WB_DESKTOP 等），再 import app.main——
app.config 在导入期就单例化读取环境，晚于此步的修改不会生效。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def pick_port() -> int:
    """让系统分配一个空闲端口（绑 0 端口再释放）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_free(port: int) -> bool:
    if not port:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", int(port)))
            return True
        except OSError:
            return False


@dataclass
class ServerHandle:
    base_url: str
    port: int
    data_dir: Path
    _stop: object = None

    def stop(self) -> None:
        try:
            if callable(self._stop):
                self._stop()
        except Exception:
            logger.debug("停止本地服务失败", exc_info=True)


def prepare_env(data_dir: Path, *, extra: dict[str, str] | None = None) -> None:
    """设置 app.config 读取的全部桌面相关环境变量（必须在 import app 之前）。"""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["WB_DESKTOP"] = "1"
    # 桌面版默认开放模式（单机单用户，无需登录）；想启用多用户可在应用内初始化管理员
    os.environ.setdefault("APP_PASSWORD", "")
    os.environ.setdefault("TZ_NAME", "Asia/Shanghai")
    os.environ.setdefault("EXPORT_DOCX", "1")
    for key, value in (extra or {}).items():
        os.environ[key] = str(value)


def wait_ready(base_url: str, timeout: float = 40.0) -> bool:
    """轮询 /healthz 直到 200；用于在开窗口前确认服务已就绪。"""
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(base_url + "/healthz", timeout=1.5)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def start(data_dir: Path, port: int = 0, *, host: str = "127.0.0.1", timeout: float = 40.0) -> ServerHandle:
    """启动本地服务并返回句柄；失败抛 RuntimeError（调用方负责提示用户）。"""
    import uvicorn

    prepare_env(Path(data_dir))

    from app.main import create_app  # 必须在上面的环境变量之后导入

    chosen = int(port) if port and port_free(int(port)) else pick_port()
    application = create_app()
    config = uvicorn.Config(
        application,
        host=host,
        port=chosen,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="workbench-http", daemon=True)
    thread.start()

    base_url = "http://%s:%d" % (host, chosen)
    if not wait_ready(base_url, timeout=timeout):
        try:
            server.should_exit = True
        except Exception:
            pass
        raise RuntimeError("本地服务在 %.0f 秒内没有就绪：%s" % (timeout, base_url))

    logger.info("本地服务已就绪：%s（数据目录 %s）", base_url, data_dir)
    return ServerHandle(base_url=base_url, port=chosen, data_dir=Path(data_dir), _stop=lambda: setattr(server, "should_exit", True))
