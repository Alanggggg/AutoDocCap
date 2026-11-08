#!/usr/bin/env python3
"""可选保存位置：默认兼容、选择持久化、写入检查、任务路径。仅使用临时目录。"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app


class FakeWindow:
    selected = None

    def create_file_dialog(self, kind):
        assert kind == app.webview.FileDialog.FOLDER
        return [str(self.selected)] if self.selected else None


class FakeThread:
    last = None

    def __init__(self, target, args, daemon):
        self.target, self.args, self.daemon = target, args, daemon
        FakeThread.last = self

    def start(self):
        self.started = True


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    originals = {k: getattr(app, k) for k in ("SUPPORT", "SETTINGS", "PREVIEW", "OUT_ROOT", "DEFAULTS", "bar_position")}
    original_thread, original_permissions = app.threading.Thread, app.doccap.permissions
    try:
        app.SUPPORT = tmp / "support"
        app.SETTINGS = app.SUPPORT / "settings.json"
        app.PREVIEW = app.SUPPORT / "preview.png"
        app.OUT_ROOT = tmp / "default"
        app.DEFAULTS = dict(app.DEFAULTS, output_dir=str(app.OUT_ROOT))
        app.bar_position = lambda region: None
        app.threading.Thread = FakeThread
        app.doccap.permissions = lambda: {"screen": True, "input": True}

        # 旧设置没有 output_dir 时仍使用原默认目录。
        app.save_settings({"preset": "normal"})
        assert app.output_root() == app.OUT_ROOT

        api = app.Api()
        api._main = FakeWindow()
        assert api.select_output() == {"cancelled": True}
        selected = tmp / "chosen"
        api._main.selected = selected
        result = api.select_output()
        assert result == {"path": str(selected)}, result
        assert app.output_root() == selected
        assert not list(selected.glob(".autodoccap-write-test-*")), "写入检查留下了临时文件"

        (selected / "同名文档").mkdir()
        assert app.unique_dir("同名文档", selected) == selected / "同名文档_2"

        blocked = tmp / "not-a-directory"
        blocked.write_text("test")
        try:
            app.prepare_output_root({"output_dir": str(blocked)})
        except app.doccap.CaptureError as e:
            assert "无法写入保存位置" in str(e)
        else:
            raise AssertionError("不可写路径没有被拦截")

        settings = dict(app.load_settings(), region=[0, 0, 100, 100])
        app.save_settings(settings)  # 选区由框选流程写入设置，普通表单保存会故意排除它
        result = api.start("路径验证", settings)
        assert result.get("ok"), result
        task_out = FakeThread.last.args[1]
        assert task_out == selected / "路径验证", task_out
        assert FakeThread.last.started
        print("保存位置测试通过：旧默认、取消选择、选择持久化、重名、写入检查、任务输出路径")
    finally:
        for key, value in originals.items():
            setattr(app, key, value)
        app.threading.Thread = original_thread
        app.doccap.permissions = original_permissions
