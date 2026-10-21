"""桌面版本地服务启动：不许让 uvicorn 接管 logging（打包后这是真实故障源）。"""

import logging
import logging.config

import pytest

from desktop import server as server_mod


def test_启动服务时不调用_dictConfig(tmp_path, monkeypatch):
    """回归测试：uvicorn 的默认日志配置会解析 uvicorn.logging -> click，
    打包后该链路一旦缺失就抛 Unable to configure formatter 'default'。
    我们传 log_config=None，所以整个启动过程不应触碰 logging.config.dictConfig。
    """
    def boom(*args, **kwargs):
        raise AssertionError("uvicorn 不应调用 logging.config.dictConfig")

    monkeypatch.setattr(logging.config, "dictConfig", boom)

    handle = server_mod.start(tmp_path, 0, timeout=30)
    try:
        import httpx

        resp = httpx.get(handle.base_url + "/healthz", timeout=5)
        assert resp.status_code == 200
        assert handle.port > 0
        assert handle.data_dir == tmp_path
    finally:
        handle.stop()


def test_端口选择与占用检测(tmp_path):
    free = server_mod.pick_port()
    assert 1024 < free < 65536
    assert server_mod.port_free(free) is True
    assert server_mod.port_free(0) is False


def test_prepare_env_写入桌面标志(tmp_path, monkeypatch):
    import os

    monkeypatch.delenv("WB_DESKTOP", raising=False)
    server_mod.prepare_env(tmp_path)
    assert os.environ["WB_DESKTOP"] == "1"
    assert os.environ["DATA_DIR"] == str(tmp_path)
