"""冒烟测试：启动界面，等几秒，把本程序的窗口逐个截图后退出。不做任何截图或滚动操作。
用法：.venv/bin/python tests/ui_shot.py 输出前缀 [app.py 的参数...]"""
import subprocess, sys, time
from pathlib import Path
import Quartz
ROOT = Path(__file__).resolve().parent.parent
p = subprocess.Popen([str(ROOT / ".venv/bin/python"), str(ROOT / "app.py"), *sys.argv[2:]],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(float(__import__("os").environ.get("WAIT", 5)))
wins = [w for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, 0)
        if w.get("kCGWindowOwnerPID") == p.pid and w.get("kCGWindowLayer", 1) in (0, 3, 8) and w["kCGWindowBounds"]["Height"] > 40]
for i, w in enumerate(wins):
    b = w["kCGWindowBounds"]
    print(f"window {i}: {w.get('kCGWindowName')!r} onscreen={w.get('kCGWindowIsOnscreen')} {b}")
    subprocess.run(["screencapture", "-x", "-o", "-l", str(w["kCGWindowNumber"]), f"{sys.argv[1]}_{i}.png"])
p.terminate()
try:
    out = p.communicate(timeout=5)[0]
except subprocess.TimeoutExpired:
    p.kill(); out = p.communicate()[0]
print("---- app output ----"); print(out[-3000:])
