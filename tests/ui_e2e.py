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
def scroll(t):
    pos["y"] = int(np.clip(pos["y"] - t * 80, 0, truth.shape[0] - H))
    return time.monotonic()
doccap.scroll, doccap.mouse_pos, doccap.move_mouse = scroll, (lambda: park), (lambda x, y: None)
doccap.permissions = lambda: {"screen": True, "input": True}
app.save_settings(dict(app.DEFAULTS, output_dir=str(app.OUT_ROOT)))  # 窗口初始化前隔离输出路径

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
def fin(r): orig(r); print("RESULT:", {k: r.get(k) for k in ("name", "frames", "reason", "error", "pdf_mb")}, "pages", len(r.get("pages") or []), flush=True); done.set()
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
    shot(app.APP, "/tmp/e2e_prepare.png")  # 临时设置、无选区，仅用于检查主界面布局
    s = dict(app.DEFAULTS, region=REGION, delay=0.2, lag_wait=1.0, poll=0.1, countdown=2, ocr=True,
             output_dir=str(app.OUT_ROOT))
    app.save_settings(s)  # 区域只认设置文件里的（正常由"框选区域"写入）
    ui_path = api._main.evaluate_js("document.querySelector('#out-root').textContent")
    assert ui_path == str(app.OUT_ROOT), ("界面保存位置不正确", ui_path, app.OUT_ROOT)
    print("界面保存位置:", ui_path, flush=True)
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
    # 真实执行一次“长页”重新分页：界面、设置文件和任务元数据都必须保持 2.0
    done.clear()
    api._main.evaluate_js("document.querySelector('#r-ratio').value = '2'; rebuild(false);")
    if not done.wait(60):
        raise AssertionError("长页重新分页超时")
    time.sleep(1)
    page_state = api._main.evaluate_js("({selected: document.querySelector('#r-ratio').value, setting: S.page_ratio, pages: result.pages.length})")
    saved_ratio = app.load_settings()["page_ratio"]
    print("长页重新分页:", page_state, "已保存:", saved_ratio, flush=True)
    assert page_state["selected"] == "2" and page_state["setting"] == 2.0 and saved_ratio == 2.0, "长页选择被重置"
    assert page_state["pages"] < 6, "重新分页没有实际改变页面数量"

    # 来源对照与局部试修：只预览，不正式应用，offsets 必须逐字节不变。
    import json
    job = app.OUT_ROOT / "界面测试"
    offsets_before = (job / "offsets.json").read_bytes()
    api._main.evaluate_js("openSource(1, true)")
    for _ in range(100):
        if api._main.evaluate_js("document.querySelector('#source-dialog').open && !!source"):
            break
        time.sleep(.1)
    source_state = api._main.evaluate_js("""({
      open: document.querySelector('#source-dialog').open,
      mode: source.mode,
      previous: document.querySelector('#source-prev-name').textContent,
      current: document.querySelector('#source-current-name').textContent,
      images: ['#source-prev', '#source-current', '#source-page'].every(s => document.querySelector(s).src.startsWith('data:image/'))
    })""")
    print("拼接来源对照:", source_state, flush=True)
    assert source_state["open"] and source_state["images"] and source_state["mode"] == "seam", "拼接来源对话框或三列图像未显示"
    assert source_state["previous"] == "拼接前帧：frame_0000.png" and source_state["current"] == "拼接后帧：frame_0001.png"
    api._main.evaluate_js("document.querySelector('#source-test').click()")
    for _ in range(100):
        if api._main.evaluate_js("!document.querySelector('#source-preview-wrap').hidden"):
            break
        time.sleep(.1)
    preview_state = api._main.evaluate_js("""({
      shown: !document.querySelector('#source-preview-wrap').hidden,
      candidate: source && source.candidate,
      apply: !document.querySelector('#source-apply').hidden,
      status: document.querySelector('#source-status').textContent
    })""")
    print("局部试修预览:", preview_state, flush=True)
    assert preview_state["shown"] and preview_state["apply"] and isinstance(preview_state["candidate"], int), "局部试修未生成可应用预览"
    assert "匹配误差" in preview_state["status"] and (job / "offsets.json").read_bytes() == offsets_before, "局部预览修改了位移"

    # 三列都必须能打开原始 PNG；缩放操作和关闭大图不能丢失来源页的试修状态。
    page_index = api._main.evaluate_js("cur")
    expected_paths = [job / "frames/frame_0000.png", job / "frames/frame_0001.png",
                      Path(api._result["pages"][page_index])]
    for selector, expected_path in zip(("#source-prev", "#source-current", "#source-zoom-page"), expected_paths):
        api._main.evaluate_js(f"document.querySelector('{selector}').click()")
        for _ in range(100):
            if api._main.evaluate_js("document.querySelector('#zoom-dialog').open && document.querySelector('#zoom-image').naturalWidth > 0"):
                break
            time.sleep(.1)
        zoom = api._main.evaluate_js("""({
          open: document.querySelector('#zoom-dialog').open,
          png: document.querySelector('#zoom-image').src.startsWith('data:image/png;base64,'),
          naturalWidth: document.querySelector('#zoom-image').naturalWidth,
          width: document.querySelector('#zoom-image').getBoundingClientRect().width,
          sourceOpen: document.querySelector('#source-dialog').open,
          candidate: source && source.candidate
        })""")
        expected_width = Image.open(expected_path).width
        print("原始大图:", selector, zoom, "期望宽度", expected_width, flush=True)
        assert zoom["open"] and zoom["png"] and zoom["naturalWidth"] == expected_width, "没有加载原始清晰度 PNG"
        assert zoom["sourceOpen"] and zoom["candidate"] == preview_state["candidate"], "大图查看破坏了来源页状态"
        if selector == "#source-current":
            api._main.evaluate_js("document.querySelector('#zoom-in').click()")
            enlarged = api._main.evaluate_js("document.querySelector('#zoom-image').getBoundingClientRect().width")
            assert enlarged > zoom["width"], "放大按钮无效"
            actual = api._main.evaluate_js("document.querySelector('#zoom-actual').click(); document.querySelector('#zoom-image').getBoundingClientRect().width")
            assert actual == expected_width, "100% 不是实际像素"
            api._main.evaluate_js("document.querySelector('#zoom-fit').click()")
            assert api._main.evaluate_js("document.querySelector('#zoom-image').getBoundingClientRect().width") < actual, "适配窗口无效"
        api._main.evaluate_js("document.querySelector('#zoom-close').click()")
        assert not api._main.evaluate_js("document.querySelector('#zoom-dialog').open"), "大图无法关闭"
    assert (job / "offsets.json").read_bytes() == offsets_before, "查看大图修改了位移"
    api._main.evaluate_js("document.querySelector('#source-dialog').close(); openSource();")
    for _ in range(100):
        if api._main.evaluate_js("source && source.mode === 'content' && document.querySelector('#source-dialog').open"):
            break
        time.sleep(.1)
    content_state = api._main.evaluate_js("""({
      mode: source.mode,
      frame: source.frame,
      first: source.page_frames[0],
      count: document.querySelectorAll('#source-frame-list [data-frame]').length,
      prevHidden: document.querySelector('#source-prev-card').hidden,
      testHidden: document.querySelector('#source-test').hidden
    })""")
    print("页面内容模式:", content_state, flush=True)
    assert content_state["mode"] == "content" and content_state["frame"] == content_state["first"]
    assert content_state["count"] == len(api._result["page_frames"][page_index]) > 1, "本页来源帧列表不完整"
    assert content_state["prevHidden"] and content_state["testHidden"], "内容模式仍混入拼接操作"

    # 点击最终页 80% 高度处，必须命中后端独立计算出的同一帧。
    expected_hit = api.source_at(page_index, .8)
    assert "error" not in expected_hit, expected_hit
    api._main.evaluate_js("""(() => {
      const img = document.querySelector('#source-page'), r = img.getBoundingClientRect();
      img.dispatchEvent(new MouseEvent('click', {clientX: r.left + r.width / 2, clientY: r.top + r.height * .8, bubbles: true}));
    })()""")
    for _ in range(100):
        if api._main.evaluate_js("document.querySelector('#source-status').textContent.includes('已按点击位置')"):
            break
        time.sleep(.1)
    hit_state = api._main.evaluate_js("({frame: source.frame, mode: source.mode, status: document.querySelector('#source-status').textContent})")
    print("最终页点击定位:", hit_state, "期望:", expected_hit, flush=True)
    assert hit_state["frame"] == expected_hit["frame"] and hit_state["mode"] == "content", "最终页点击没有命中正确原始帧"

    # 帧列表仍可手动切换；所有定位操作均不得改位移。
    last_frame = api._result["page_frames"][page_index][-1]
    api._main.evaluate_js(f"document.querySelector('#source-frame-list [data-frame=\"{last_frame}\"]').click()")
    for _ in range(100):
        if api._main.evaluate_js(f"source.frame === {last_frame}"):
            break
        time.sleep(.1)
    assert api._main.evaluate_js("source.frame") == last_frame, "来源帧列表无法切换"
    assert (job / "offsets.json").read_bytes() == offsets_before, "内容定位修改了位移"
    close_state = api._main.evaluate_js("""(() => {
      const b = document.querySelector('#source-close-x').getBoundingClientRect();
      const d = document.querySelector('#source-dialog').getBoundingClientRect();
      return {width: b.width, height: b.height, top: b.top - d.top, right: d.right - b.right};
    })()""")
    assert close_state["width"] == 36 and close_state["height"] == 36 and close_state["top"] < 20 and close_state["right"] < 20, close_state
    api._main.evaluate_js("document.querySelector('#source-close-x').click()")
    assert not api._main.evaluate_js("document.querySelector('#source-dialog').open"), "右上角关闭按钮无效"

    api._main.resize(900, 640)  # 最小窗口：多提示也只能占一行，不能横向溢出
    time.sleep(0.5)
    layout = api._main.evaluate_js("""
      (() => {
        result = {...result, reason: "stopped",
          timing: {n: 8, mean: 1.4, p50: 1.2, p90: 2, delay: .6, suggest_delay: 1.5, late: 2, timeouts: 1},
          compress: {over: false, size_mb: 12, limit_mb: 48, scaled_pages: [2], gray_pages: [3]},
          seams: [{page: 1}, {page: 2}]};
        renderReview();
        const bar = document.querySelector("#notice-bar");
        const checks = {height: bar.getBoundingClientRect().height,
          overflow: bar.scrollWidth > bar.clientWidth,
          footerOverflow: document.querySelector(".rv-foot").scrollWidth > document.querySelector(".rv-foot").clientWidth,
          details: document.querySelectorAll("#notice-panel .notice-detail").length,
          startsClosed: document.querySelector("#notice-panel").hidden,
          summary: bar.textContent};
        document.querySelector("#page-help").click();
        checks.helpOpened = document.querySelector("#page-help-dialog").open;
        document.querySelector("#close-page-help").click();
        checks.helpClosed = !document.querySelector("#page-help-dialog").open;
        document.querySelector(".notice-all").click();
        checks.opened = !document.querySelector("#notice-panel").hidden;
        document.querySelector("#notice-panel [data-go]").click();
        checks.jumpClosed = document.querySelector("#notice-panel").hidden;
        document.querySelector(".notice-all").click();
        document.querySelector("#apply-sg").click();
        checks.adopted = S.preset === "slow";
        checks.applyClosed = document.querySelector("#notice-panel").hidden;
        return checks;
      })()
    """)
    print("紧凑检查栏:", layout, flush=True)
    assert layout["height"] <= 44 and not layout["overflow"] and not layout["footerOverflow"], "检查栏或底部操作区溢出"
    assert layout["details"] == 4 and layout["startsClosed"], "详情未完整收入浮层"
    assert "2 项需要检查" in layout["summary"] and "卡顿" in layout["summary"], "重要摘要未显示"
    assert layout["helpOpened"] and layout["helpClosed"], "分页与拼接说明弹窗失效"
    assert layout["opened"] and layout["jumpClosed"] and layout["adopted"] and layout["applyClosed"], "详情操作失效"
    print("截图次数", seen["grabs"], "其中遮罩仍可见的次数", seen["overlay_visible_at_grab"])
    d = app.OUT_ROOT / "界面测试"
    print("输出目录内容:", sorted(p.name for p in d.iterdir()) if d.exists() else "（不存在）")
webview.start(run)
print("界面事件循环已正常退出", flush=True)
os._exit(0)  # 部分 pywebview 后台线程在窗口销毁后仍可能残留；所有断言和文件写入均已完成
