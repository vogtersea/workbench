"""Android 启动器：在后台线程跑 uvicorn，返回端口。

与桌面壳同理（desktop/server.py），但更精简：
Android 上由 Java 侧负责窗口与 WebView，这里只起 HTTP 服务。
"""

import logging
import os
import socket
import threading

LOG = logging.getLogger("launcher")


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start(data_dir: str) -> int:
    os.makedirs(data_dir, exist_ok=True)
    os.environ["DATA_DIR"] = data_dir
    os.environ["APP_PASSWORD"] = ""
    os.environ["TZ_NAME"] = "Asia/Shanghai"
    os.environ.setdefault("WB_MOBILE", "1")

    from app.main import create_app  # 必须放在环境变量之后

    port = _pick_port()
    application = create_app()

    import uvicorn

    config = uvicorn.Config(application, host="127.0.0.1", port=port,
                            log_level="warning", access_log=False, lifespan="on",
                            log_config=None)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="worklog-http", daemon=True).start()
    LOG.warning("本地服务已启动：127.0.0.1:%d（数据目录 %s）", port, data_dir)
    return port
