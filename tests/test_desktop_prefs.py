"""桌面版偏好文件：读写、损坏降级、字段归一。"""

import json


from desktop import prefs as prefs_mod


def test_默认值符合桌面约定():
    p = prefs_mod.Prefs()
    assert p.data_dir == ""
    assert p.first_run_done is False
    assert p.close_to_tray is True      # 关窗进托盘：提醒才有机会触发
    assert p.autostart is False         # 开机自启默认关，交给用户自己开
    assert p.notify is True


def test_保存后能原样读回(tmp_path):
    target = tmp_path / "prefs.json"
    p = prefs_mod.Prefs(data_dir="D:/WorkbenchData", first_run_done=True, autostart=True)
    prefs_mod.save(p, target)
    again = prefs_mod.load(target)
    assert again.data_dir == "D:/WorkbenchData"
    assert again.first_run_done is True
    assert again.autostart is True


def test_文件不存在返回默认值(tmp_path):
    assert prefs_mod.load(tmp_path / "nope.json").data_dir == ""


def test_文件损坏返回默认值而不是抛异常(tmp_path):
    target = tmp_path / "prefs.json"
    target.write_text("{ this is not json", encoding="utf-8")
    assert prefs_mod.load(target).data_dir == ""


def test_未知字段被忽略且类型被归一(tmp_path):
    target = tmp_path / "prefs.json"
    target.write_text(
        json.dumps({"data_dir": "  E:/x  ", "port": 99999, "window_width": 10, "未知": 1}),
        encoding="utf-8",
    )
    p = prefs_mod.load(target)
    assert p.data_dir == "E:/x"
    assert p.port == 65535
    assert p.window_width == 900      # 低于下限时抬到最小值
    assert not hasattr(p, "未知")


def test_空数据目录不写脏值(tmp_path):
    target = tmp_path / "prefs.json"
    prefs_mod.save(prefs_mod.Prefs(data_dir="   "), target)
    assert json.loads(target.read_text(encoding="utf-8"))["data_dir"] == ""
