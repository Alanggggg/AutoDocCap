"""验证遮罩和悬浮条能否显示在"另一个程序的全屏空间"里。
另起一个进程打开纯白窗口并全屏（模拟全屏打开的文档），本进程分别用"旧设置"和"新设置"显示遮罩并截屏比对。"""
import os, subprocess, sys, tempfile, time
from pathlib import Path
import numpy as np
from PIL import Image
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.argv = ["app.py"]
import webview, app

HELPER = '''
import webview, time, threading
w = webview.create_window("全屏测试文档", html="<body style='margin:0;background:#fff'></body>", width=900, height=600)
def go():
    time.sleep(1.0); w.toggle_fullscreen(); time.sleep(16); w.destroy()
webview.start(go)
'''
REGION = [300, 200, 600, 400]
api = app.Api()
host = webview.create_window("host", html="<body></body>", width=200, height=100, hidden=True)

def check(tag):
    f = tempfile.mktemp(suffix=".png"); subprocess.run(["screencapture", "-x", f])
    im = np.asarray(Image.open(f).convert("L")).astype(int); os.remove(f)
    at = lambda x, y: int(im[int(y * 2), int(x * 2)])
    out, inside = at(120, 700), at(REGION[0] + 300, REGION[1] + 200)
    bx, by = int(webview.screens[0].width) - app.BAR_W - 16 + 60, 40 + 39
    print(f"[{tag}] 区域外像素 {out}（应约 97=变暗，255=没遮罩）  区域内 {inside}（应约 255）  悬浮条处 {at(bx, by)}（应约 30）", flush=True)
    return out < 140 and inside > 230

def run():
    try:
        helper = subprocess.Popen([sys.executable, "-c", HELPER])
        time.sleep(4.5)  # 等全屏动画结束
        # 与 app._run 相同的流程：切辅助模式 → 现场新建窗口 → 显示
        app.overlay_mode(True)
        api._overlay = app.make_floating("overlay", api); api._bar = app.make_floating("bar", api)
        api._show_overlay(REGION, 5)
        api._bar.resize(app.BAR_W, app.BAR_H); api._bar.move(int(webview.screens[0].width) - app.BAR_W - 16, 40)
        app.show_passive(api._bar)
        time.sleep(1.2); new_ok = check("新设置")
        app.hide_now(api._overlay); app.hide_now(api._bar); app.overlay_mode(False)
        print("结论：新设置在全屏空间里可见", new_ok, flush=True)
        helper.terminate()
    finally:
        for w in (api._overlay, api._bar, host): app.destroy(w)
webview.start(run)
