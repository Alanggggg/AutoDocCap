"""界面端到端测试：真实窗口 + 模拟的截屏/滚动（在测试文档长图上），走完 开始→悬浮条→生成→复核 全流程。
不动真实屏幕和鼠标；设置与输出写到临时目录。用法：.venv/bin/python tests/ui_e2e.py"""
import os, subprocess, sys, tempfile, threading, time
from pathlib import Path
import numpy as np
from PIL import Image
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.argv = ["app.py"]
import Quartz, webview, app, doccap

tmp = Path(tempfile.mkdtemp())
app.SUPPORT, app.OUT_ROOT = tmp / "support", tmp / "out"
app.SETTINGS, app.PREVIEW = app.SUPPORT / "settings.json", app.SUPPORT / "p.png"
sys.path.insert(0, str(ROOT / "tests"))
from fixtures import render_doc
truth = render_doc(pages=6)
REGION = [300, 200, truth.shape[1] // 2, 700]
H, park, pos = REGION[3] * 2, (300 + REGION[2] / 2, 200 + 350), {"y": 0}
doccap.grab = lambda r: truth[pos["y"]:pos["y"] + H].copy()
def scroll(t): pos["y"] = int(np.clip(pos["y"] - t * 80, 0, truth.shape[0] - H))
doccap.scroll, doccap.mouse_pos, doccap.move_mouse = scroll, (lambda: park), (lambda x, y: None)
doccap.permissions = lambda: {"screen": True, "input": True}

api = app.Api()
api._main = webview.create_window(app.APP, html=(app.BASE / "ui/index.html").read_text(), js_api=api, width=1040, height=760)
# 检查：每次截图时遮罩都必须已经隐藏
seen = {"grabs": 0, "overlay_visible_at_grab": 0}
_grab = doccap.grab
def grab_checked(r):
    seen["grabs"] += 1
    if api._overlay is not None and api._overlay.native is not None and api._overlay.native.isVisible(): seen["overlay_visible_at_grab"] += 1
    return _grab(r)
doccap.grab = grab_checked
done = threading.Event()
orig = api._finish
def fin(r): orig(r); print("RESULT:", {k: r.get(k) for k in ("name", "frames", "reason", "error")}, "pages", len(r.get("pages") or []), flush=True); done.set()
api._finish = fin

def shot(title, name):
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, 0):
        if w.get("kCGWindowOwnerPID") == os.getpid() and w.get("kCGWindowName") == title:
            b = w["kCGWindowBounds"]; print(f"  {title!r} at x={b['X']:.0f} y={b['Y']:.0f} {b['Width']:.0f}x{b['Height']:.0f}")
            subprocess.run(["screencapture", "-x", "-o", "-l", str(w["kCGWindowNumber"]), name]); return True
    print(f"  {title!r} 不在屏幕上"); return False

def run():
    try:
        _run()
    finally:
        api._main.destroy()

def _run():
    time.sleep(3)
    s = dict(app.DEFAULTS, region=REGION, delay=0.2, lag_wait=1.0, poll=0.1, countdown=2, ocr=True)
    app.save_settings(s)  # 区域只认设置文件里的（正常由"框选区域"写入）
    print("start:", api.start("界面测试", s), flush=True)
    time.sleep(1.2); shot("截图进度", "/tmp/e2e_bar_countdown.png")
    ov = api._overlay.native  # 此时正处于倒计时，遮罩已现场新建
    print("倒计时中：遮罩可见", bool(ov.isVisible()), "鼠标穿透", bool(ov.ignoresMouseEvents()), "frame", ov.frame())
    subprocess.run(["screencapture", "-x", "/tmp/e2e_overlay_full.png"])
    # 对位检查：高亮框（橙色）应紧贴截图区域外侧 4~7 点，区域内部不应有橙色框
    im = np.asarray(Image.open("/tmp/e2e_overlay_full.png").convert("RGB")).astype(int)
    x, y, w, h = REGION
    def orange(px, py):
        r, g, b = im[int(py * 2), int(px * 2)]; return r > g > b and r - b > 40  # 按色相判断：截屏时可能正处在闪烁的暗相
    print("对位：左边框", orange(x - 6, y + h / 2), "上边框", orange(x + w / 2, y - 6), "下边框", orange(x + w / 2, y + h + 5),
          "| 区域内侧无框", not orange(x + 3, y + h / 2), not orange(x + w / 2, y + 3))
    time.sleep(8); shot("截图进度", "/tmp/e2e_bar_run.png"); shot(app.APP, "/tmp/e2e_main_hidden.png")
    if not done.wait(60):
        import faulthandler; print("!! 60 秒未完成，各线程当前位置：", flush=True); faulthandler.dump_traceback(all_threads=True)
    time.sleep(2.5)
    shot(app.APP, "/tmp/e2e_review.png")
    print("截图次数", seen["grabs"], "其中遮罩仍可见的次数", seen["overlay_visible_at_grab"])
    d = app.OUT_ROOT / "界面测试"
    print("输出目录内容:", sorted(p.name for p in d.iterdir()) if d.exists() else "（不存在）")
webview.start(run)
