"""需求文档截图工具 —— 图形界面版。

截图、拼接、OCR 都在 doccap.py（引擎）里，这里只负责窗口和交互：
主窗口（ui/index.html）负责准备与复核；截图期间主窗口隐藏，只在屏幕角落显示悬浮条（ui/bar.html）。

运行：.venv/bin/python app.py
"""

import base64
import io
import json
import shutil
import subprocess
import sys
import threading
import time
import traceback
from argparse import Namespace
from pathlib import Path

import webview
from PIL import Image

import doccap

APP = "需求文档截图工具"
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
SUPPORT = Path.home() / "Library/Application Support" / APP
SETTINGS = SUPPORT / "settings.json"
PREVIEW = SUPPORT / "region_preview.png"
# 不放在桌面或文稿：这两个文件夹可能开着 iCloud 同步，截下来的文档会被传到云上
OUT_ROOT = Path.home() / "需求文档截图输出"

PRESETS = {
    "fast": {"label": "流畅", "delay": 0.4, "lag_wait": 1.5},
    "normal": {"label": "一般", "delay": 0.6, "lag_wait": 2.0},
    "slow": {"label": "卡顿", "delay": 1.5, "lag_wait": 3.0},
}
DEFAULTS = {"preset": "normal", "delay": 0.6, "lag_wait": 2.0, "poll": 0.3, "settle_timeout": 6.0,
            "step": 0.6, "page_ratio": 1.414, "max_frames": 3000, "countdown": 5, "ocr": True,
            "region": None}
BAR_W, BAR_H = 420, 78


def load_settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS.read_text()))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    SUPPORT.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=1))


def data_uri(path, width, quality=82):
    im = Image.open(path).convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def unique_dir(name):
    name = "".join(c for c in name.strip() if c not in '/\\:*?"<>|').strip() or time.strftime("文档_%m%d_%H%M")
    out, n = OUT_ROOT / name, 2
    while out.exists():
        out, n = OUT_ROOT / f"{name}_{n}", n + 1
    return out


def make_opt(s, **over):
    o = {"delay": s["delay"], "lag_wait": s["lag_wait"], "poll": s["poll"],
         "settle_timeout": s["settle_timeout"], "step": s["step"], "countdown": int(s["countdown"]),
         "max_frames": int(s["max_frames"]), "match_thr": 12.0, "page_ratio": s["page_ratio"],
         "no_ocr": not s["ocr"], "restitch": False}
    o.update(over)
    return Namespace(**o)


def bar_position(region):
    """悬浮条放在不与截图区域重叠的屏幕角落；放不下返回 None（悬浮条会被截进去，不能显示）。"""
    x, y, w, h = region
    avoid = (x - 16, y - 16, w + 32, h + 32)

    def hit(a, b):
        return not (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0] or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])

    for sc in webview.screens:
        m = 16
        for cx, cy in [(sc.x + sc.width - BAR_W - m, sc.y + 40), (sc.x + sc.width - BAR_W - m, sc.y + sc.height - BAR_H - m),
                       (sc.x + m, sc.y + 40), (sc.x + m, sc.y + sc.height - BAR_H - m)]:
            if not hit((cx, cy, BAR_W, BAR_H), avoid):
                return int(cx), int(cy)
    return None


def on_main(fn, timeout=2.0):
    """在主线程执行 Cocoa 操作并等它完成。pywebview 的 show/hide 是异步的，
    而遮罩必须确定已经消失才能开始截图，否则会被截进去。"""
    from PyObjCTools import AppHelper
    done = threading.Event()

    def run():
        try:
            fn()
        finally:
            done.set()

    AppHelper.callAfter(run)
    done.wait(timeout)


# NSWindowCollectionBehavior：出现在所有空间 | 切换空间时不跟着滑动 | 不参与 Cmd+` 切换 | 可叠加在全屏应用上
ALL_SPACES = (1 << 0) | (1 << 4) | (1 << 6) | (1 << 8)
LEVEL_OVERLAY, LEVEL_BAR = 1000, 1001  # 高于全屏应用和菜单栏（kCGScreenSaverWindowLevel 附近）


def show_passive(win, click_through=False, level=LEVEL_BAR):
    """显示窗口但不激活本程序：倒计时期间焦点要留给用户去点远程窗口。

    用户通常把文档全屏打开、三指横扫切到它所在的全屏空间，所以窗口要能出现在所有空间、
    叠加在全屏应用之上。"""
    def f():
        nw = win.native
        nw.setCollectionBehavior_(ALL_SPACES)
        nw.setLevel_(level)
        nw.setIgnoresMouseEvents_(click_through)
        nw.orderFrontRegardless()
    on_main(f)


def overlay_mode(on):
    """新版 macOS 只允许"辅助程序"（无 Dock 图标）的窗口叠加到其他程序的全屏空间上。
    截图期间临时切换过去，结束后恢复成普通程序（Dock 图标随之恢复）。"""
    from AppKit import NSApplication
    on_main(lambda: NSApplication.sharedApplication().setActivationPolicy_(1 if on else 0))
    time.sleep(0.5)  # 系统需要一点时间生效；立刻显示窗口仍会被挡在全屏空间之外


def hide_now(win):
    if win is None or win.native is None:
        return
    on_main(lambda: win.native.orderOut_(None))
    time.sleep(0.15)  # 再等窗口服务器把画面合成掉


FLOATING = {
    "bar": dict(title="截图进度", page="ui/bar.html", width=BAR_W, height=BAR_H, shadow=True),
    "overlay": dict(title="倒计时", page="ui/overlay.html", width=800, height=600, shadow=False),
}


def make_floating(kind, api):
    """现场新建悬浮窗。必须在 overlay_mode(True) 之后创建：启动时预先建好的窗口会被系统
    固定在普通桌面空间，之后再改属性也进不了其他程序的全屏空间（实测）。"""
    c = FLOATING[kind]
    w = webview.create_window(c["title"], html=(BASE / c["page"]).read_text(encoding="utf-8"), js_api=api,
                              width=c["width"], height=c["height"], frameless=True, on_top=True, hidden=True,
                              resizable=False, focus=False, transparent=True, shadow=c["shadow"])
    w.events.loaded.wait(5)
    return w


def destroy(win):
    if win is not None:
        try:
            win.destroy()
        except Exception:
            pass


class Api:
    """暴露给界面 JS 调用的方法（window.pywebview.api.xxx）。下划线开头的属性不会暴露。"""

    def __init__(self):
        self._main = None
        self._bar = None
        self._overlay = None
        self._stop = threading.Event()
        self._busy = False
        self._job = None      # 当前查看的输出目录
        self._result = None   # 当前查看的结果

    # ------------------------------------------------ 设置与权限

    def init(self):
        s = load_settings()
        opened = None
        if len(sys.argv) > 2 and sys.argv[1] == "--open":  # 调试用：直接打开某个输出目录进入复核
            opened = self.open_job(sys.argv[2])
        return {"settings": s, "presets": PRESETS, "perms": doccap.permissions(),
                "out_root": str(OUT_ROOT), "default_name": time.strftime("文档_%m%d_%H%M"),
                "preview": data_uri(PREVIEW, 1200) if s.get("region") and PREVIEW.exists() else None,
                "opened": opened}

    def permissions(self):
        return doccap.permissions()

    def request_permission(self, kind):
        doccap.request_permission(kind)
        return doccap.permissions()

    def save_settings(self, s):
        cur = load_settings()
        cur.update({k: s[k] for k in DEFAULTS if k in s and k != "region"})
        save_settings(cur)
        return cur

    def select_region(self, wait=3):
        self._main.hide()
        overlay_mode(True)
        self._bar = make_floating("bar", self)
        try:
            # 文档多半在另一个全屏空间里：先留几秒让用户切过去，再弹出系统的十字框
            pos = (int(webview.screens[0].width) - BAR_W - 16, 40)
            self._bar.resize(BAR_W, BAR_H)
            self._bar.move(*pos)
            show_passive(self._bar)
            for i in range(wait, 0, -1):
                self._call(self._bar, "notice", f"{i} 秒后开始框选", "请先切换到要截图的页面（全屏的话三指横扫过去）")
                time.sleep(1)
            hide_now(self._bar)
            region, sel = doccap.select_region()
        except doccap.CaptureError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"框选失败：{e}"}
        finally:
            destroy(self._bar)
            self._bar = None
            overlay_mode(False)
            self._main.show()
        if region is None:
            return {"cancelled": True}
        SUPPORT.mkdir(parents=True, exist_ok=True)
        Image.fromarray(sel).save(PREVIEW)
        s = load_settings()
        s["region"] = region
        save_settings(s)
        return {"region": region, "preview": data_uri(PREVIEW, 1200)}

    # ------------------------------------------------ 截图

    def start(self, name, settings):
        if self._busy:
            return {"error": "正在处理上一份文档，请稍候。"}
        s = self.save_settings(settings)
        if not s.get("region"):
            return {"error": "请先框选截图区域。"}
        if not all(doccap.permissions().values()):
            return {"error": "请先授予「屏幕录制」和「辅助功能」权限。"}
        out = unique_dir(name)
        pos = bar_position(s["region"])
        self._busy = True
        self._stop.clear()
        threading.Thread(target=self._run, args=(s, out, pos), daemon=True).start()
        return {"ok": True, "name": out.name, "bar": pos is not None}

    def stop_capture(self):
        self._stop.set()
        return True

    def _run(self, s, out, pos):
        opt = make_opt(s)
        result = {"name": out.name, "out": str(out), "kind": "capture"}
        try:
            self._main.hide()
            overlay_mode(True)
            # 先建遮罩、后建悬浮条；层级上悬浮条也更高（LEVEL_BAR > LEVEL_OVERLAY）
            self._overlay = make_floating("overlay", self) if opt.countdown > 0 else None
            self._bar = make_floating("bar", self) if pos else None
            if opt.countdown > 0:
                self._show_overlay(s["region"], opt.countdown)
            if pos:
                self._bar.resize(BAR_W, BAR_H)
                self._bar.move(*pos)
                show_passive(self._bar)
                self._call(self._bar, "reset", {"countdown": opt.countdown})
            time.sleep(0.3)
            result.update(doccap.capture(s["region"], out, opt, on_event=self._emit, stop=self._stop))
        except doccap.CaptureError as e:
            result["error"] = str(e)
        except Exception:
            result["error"] = "截图时出错：\n" + traceback.format_exc(limit=4)
        finally:
            hide_now(self._overlay)
            for w in (self._overlay, self._bar):
                destroy(w)
            self._overlay = self._bar = None
            overlay_mode(False)
            self._main.show()

        frames = list((out / "frames").glob("frame_*.png")) if out.exists() else []
        if not frames:
            shutil.rmtree(out, ignore_errors=True)
            result.setdefault("error", "已取消，没有截到画面。")
            self._finish(result)
            return
        self._job = out
        self._build(out, opt, result)

    def _build(self, out, opt, result):
        try:
            self._call(self._main, "onEngine", {"type": "stage", "msg": "正在拼接和分页…", "stage": "stitch"})
            result.update(doccap.build(out, opt, on_event=self._emit))
            result["frames"] = result.get("frames") or len(list((out / "frames").glob("frame_*.png")))
            meta = {k: v for k, v in result.items() if k not in ("error",)}
            meta["settings"] = {k: getattr(opt, k) for k in vars(opt)}
            (out / "job.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
        except doccap.CaptureError as e:
            result["error"] = str(e)
        except Exception:
            result["error"] = "生成文档时出错：\n" + traceback.format_exc(limit=4)
        self._finish(result)

    def _finish(self, result):
        self._busy = False
        self._result = result if result.get("pages") else self._result
        self._call(self._main, "onDone", result)

    def _show_overlay(self, region, n):
        """倒计时遮罩：铺满截图区域所在的屏幕，压暗区域以外的部分，鼠标点击穿透到下面的远程窗口。

        直接设置原生窗口的 frame：pywebview 的 move 会按"去掉菜单栏的可用区域"换算，
        遮罩会整体下移一个菜单栏的高度，高亮框就对不准截图区域了。
        """
        from AppKit import NSScreen
        cx, cy = region[0] + region[2] / 2, region[1] + region[3] / 2
        main_h = NSScreen.screens()[0].frame().size.height  # Cocoa 坐标以主屏左下角为原点，y 向上
        holder = {}

        def place():
            scr = NSScreen.screens()[0]
            for s in NSScreen.screens():
                f = s.frame()
                top = main_h - (f.origin.y + f.size.height)  # 换算成截图用的左上角坐标
                if f.origin.x <= cx < f.origin.x + f.size.width and top <= cy < top + f.size.height:
                    scr = s
                    break
            f = scr.frame()
            nw = self._overlay.native
            # pywebview 的无边框窗口实为"隐藏标题栏"，系统会把这类窗口限制在菜单栏以下，改成真正的无边框
            nw.setStyleMask_(0)
            nw.setFrame_display_(f, True)
            holder["origin"] = (f.origin.x, main_h - (f.origin.y + f.size.height))

        on_main(place)
        ox, oy = holder.get("origin", (0, 0))
        self._call(self._overlay, "setup", [region[0] - ox, region[1] - oy, region[2], region[3]], n)
        show_passive(self._overlay, click_through=True, level=LEVEL_OVERLAY)

    def _emit(self, ev):
        if ev["type"] == "countdown":
            self._call(self._overlay, "tick", ev["n"])
        elif ev["type"] == "started":
            hide_now(self._overlay)  # 同步等它消失，引擎随后才截第一张图
        self._call(self._main, "onEngine", ev)
        if ev["type"] in ("countdown", "started", "calibrated", "frame", "lag", "warn", "info"):
            self._call(self._bar, "onEngine", ev)

    @staticmethod
    def _call(win, fn, *args):
        params = ", ".join(json.dumps(a, ensure_ascii=False) for a in args)
        try:
            win.evaluate_js(f"window.{fn} && window.{fn}({params})")
        except Exception:
            pass  # 窗口已关闭等情况，不影响引擎继续工作

    # ------------------------------------------------ 复核与导出

    def page_image(self, i, width):
        if not self._result or not (0 <= i < len(self._result["pages"])):
            return None
        return data_uri(self._result["pages"][i], int(width))

    def rebuild(self, page_ratio, restitch):
        if self._busy or not self._job or not self._job.exists():
            return {"error": "当前没有可重新生成的文档。"}
        meta = json.loads((self._job / "job.json").read_text())
        s = dict(load_settings())
        s.update({k: v for k, v in meta.get("settings", {}).items() if k in DEFAULTS})
        s["ocr"] = not meta.get("settings", {}).get("no_ocr", False)
        opt = make_opt(s, page_ratio=float(page_ratio), restitch=bool(restitch))
        result = {k: meta.get(k) for k in ("name", "out", "reason", "timing", "frames")}
        result["kind"] = "rebuild"
        self._busy = True
        threading.Thread(target=self._build, args=(self._job, opt, result), daemon=True).start()
        return {"ok": True}

    def open_path(self, kind):
        if not self._result:
            return False
        target = {"pdf": self._result.get("pdf"), "txt": self._result.get("txt"),
                  "folder": self._result.get("out")}.get(kind)
        if target and Path(target).exists():
            subprocess.run(["open", target] if kind != "folder" else ["open", target])
            return True
        return False

    def open_preview(self):
        if PREVIEW.exists():
            subprocess.run(["open", "-a", "Preview", str(PREVIEW)])
            return True
        return False

    def open_output_root(self):
        OUT_ROOT.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(OUT_ROOT)])
        return True

    def delete_job(self):
        if self._busy or not self._job:
            return False
        shutil.rmtree(self._job, ignore_errors=True)
        self._job = self._result = None
        return True

    def open_job(self, path):
        """打开已有的输出目录进入复核（第二版的"历史记录"也会用到）。"""
        out = Path(path)
        pages = sorted((out / "pages").glob("page_*.png"))
        if not pages:
            return None
        meta = {}
        if (out / "job.json").exists():
            meta = json.loads((out / "job.json").read_text())
        heights = [Image.open(p).size[1] for p in pages]
        pdf, txt = out / f"{out.name}.pdf", out / f"{out.name}.txt"
        self._job = out
        self._result = {"name": out.name, "out": str(out), "kind": "open",
                        "frames": meta.get("frames") or len(list((out / "frames").glob("frame_*.png"))),
                        "reason": meta.get("reason"), "timing": meta.get("timing"),
                        "seams": meta.get("seams", []), "pages": [str(p) for p in pages], "heights": heights,
                        "pdf": str(pdf) if pdf.exists() else None, "txt": str(txt) if txt.exists() else None}
        return self._result


def selftest():
    """打包后自检（不需要任何权限）：真实跑一遍 OCR 与 PDF 生成，供构建流程确认打包完整。"""
    import tempfile
    from PIL import ImageDraw, ImageFont
    with tempfile.TemporaryDirectory() as td:
        img = Image.new("RGB", (1200, 400), "white")
        font = ImageFont.truetype("/System/Library/Fonts/STHeiti Light.ttc", 48)
        ImageDraw.Draw(img).text((60, 150), "自检文字 SELFTEST 2026", fill="black", font=font)
        page = Path(td) / "page_001.png"
        img.save(page)
        texts = doccap.ocr_pages([page], lambda *a, **k: None)
        pdf = Path(td) / "t.pdf"
        doccap.make_pdf([page], texts, 2.0, pdf)
        got = " ".join(t for t, _ in texts[0])
        ok = "SELFTEST" in got and pdf.stat().st_size > 0
        print(f"OCR 结果：{got!r}；PDF {pdf.stat().st_size} 字节；{'自检通过' if ok else '自检失败'}")
        return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    api = Api()
    html = (BASE / "ui/index.html").read_text(encoding="utf-8")
    api._main = webview.create_window(APP, html=html, js_api=api, width=1040, height=760,
                                      min_size=(900, 640), background_color="#F5F5F7")
    # 悬浮条和倒计时遮罩不在这里创建，见 make_floating

    def on_main_closed():
        api._stop.set()
        for w in (api._bar, api._overlay):
            destroy(w)

    api._main.events.closed += on_main_closed
    webview.start()


if __name__ == "__main__":
    main()
