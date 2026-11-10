"""AutoDocCap —— 图形界面版。

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
import tempfile
import threading
import time
import traceback
from argparse import Namespace
from pathlib import Path

import webview
from PIL import Image

import doccap

APP = "AutoDocCap"
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
# 品牌改名不迁移本机数据，保留旧设置、选区与历史输出。
SUPPORT = Path.home() / "Library/Application Support" / "需求文档截图工具"
SETTINGS = SUPPORT / "settings.json"
PREVIEW = SUPPORT / "region_preview.png"
# 默认不放在桌面或文稿：这两个文件夹可能开着 iCloud 同步；用户可在界面中另行选择
OUT_ROOT = Path.home() / "AutoDocCap_output"

PRESETS = {
    "fast": {"label": "流畅", "delay": 0.4, "lag_wait": 1.5},
    "normal": {"label": "一般", "delay": 0.6, "lag_wait": 2.0},
    "slow": {"label": "卡顿", "delay": 1.5, "lag_wait": 3.0},
}
DEFAULTS = {"preset": "normal", "delay": 0.6, "lag_wait": 2.0, "poll": 0.3, "settle_timeout": 6.0,
            "step": 0.6, "page_ratio": 1.414, "max_frames": 3000, "countdown": 5, "ocr": True,
            "compact": True, "pdf_limit_mb": 48, "output_dir": str(OUT_ROOT),
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


def array_data_uri(im, width, quality=82):
    im = Image.fromarray(im).convert("RGB") if not isinstance(im, Image.Image) else im.convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def data_uri(path, width, quality=82):
    with Image.open(path) as im:
        return array_data_uri(im, width, quality)


def output_root(settings=None):
    s = load_settings() if settings is None else settings
    try:
        root = Path(s.get("output_dir") or OUT_ROOT).expanduser()
    except TypeError:
        raise doccap.CaptureError("保存位置无效，请重新选择文件夹。")
    if not root.is_absolute():
        raise doccap.CaptureError("保存位置无效，请重新选择文件夹。")
    return root


def prepare_output_root(settings=None):
    """在截图前确认保存位置可写；临时文件立即删除，不留下探测文件。"""
    root = output_root(settings)
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".autodoccap-write-test-", dir=root):
            pass
    except OSError as e:
        raise doccap.CaptureError(f"无法写入保存位置：{root}\n{e}")
    return root


def unique_dir(name, root=None):
    root = output_root() if root is None else Path(root)
    name = "".join(c for c in name.strip() if c not in '/\\:*?"<>|').strip() or time.strftime("文档_%m%d_%H%M")
    out, n = root / name, 2
    while out.exists():
        out, n = root / f"{name}_{n}", n + 1
    return out


def make_opt(s, **over):
    o = {"delay": s["delay"], "lag_wait": s["lag_wait"], "poll": s["poll"],
         "settle_timeout": s["settle_timeout"], "step": s["step"], "countdown": int(s["countdown"]),
         "max_frames": int(s["max_frames"]), "match_thr": 12.0, "page_ratio": s["page_ratio"],
         "no_ocr": not s["ocr"], "restitch": False,
         "compact": bool(s.get("compact", True)), "pdf_limit_mb": float(s.get("pdf_limit_mb", 48))}
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
        self._pending_replacement = None  # 试拍图片只暂存内存，确认前绝不覆盖原图

    # ------------------------------------------------ 设置与权限

    def init(self):
        s = load_settings()
        opened = None
        if len(sys.argv) > 2 and sys.argv[1] == "--open":  # 调试用：直接打开某个输出目录进入复核
            opened = self.open_job(sys.argv[2])
        return {"settings": s, "presets": PRESETS, "perms": doccap.permissions(),
                "out_root": str(output_root(s)), "default_name": time.strftime("文档_%m%d_%H%M"),
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

    def select_output(self):
        try:
            selected = self._main.create_file_dialog(webview.FileDialog.FOLDER)
            if not selected:
                return {"cancelled": True}
            s = load_settings()
            s["output_dir"] = str(prepare_output_root({"output_dir": selected[0]}))
            save_settings(s)
            return {"path": s["output_dir"]}
        except doccap.CaptureError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"选择保存位置失败：{e}"}

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
        try:
            root = prepare_output_root(s)
        except doccap.CaptureError as e:
            return {"error": str(e)}
        out = unique_dir(name, root)
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
        opt.region = s["region"]  # 随任务保存，后续单帧试拍必须沿用原选区
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

    def original_image(self, kind, index):
        """大图查看按需加载原始 PNG，避免把缩略图放大后仍然模糊。"""
        if kind == "frame" and self._job:
            frames = sorted((self._job / "frames").glob("frame_*.png"))
            path = frames[index] if 0 <= index < len(frames) else None
        elif kind == "page" and self._result:
            pages = self._result.get("pages", [])
            path = Path(pages[index]) if 0 <= index < len(pages) else None
        else:
            path = None
        if not path or not path.exists():
            return None
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()

    def source_at(self, page, ratio):
        """最终页点击定位：ratio 是页面可见高度内的纵向比例。"""
        if not self._job or not self._result:
            return {"error": "当前没有可查看的文档。"}
        try:
            heights = self._result["heights"]
            y = float(ratio) * heights[page]
            frames = sorted((self._job / "frames").glob("frame_*.png"))
            meta = json.loads((self._job / "offsets.json").read_text())
            hit = doccap.source_frame_at(frames, meta["offsets"], heights, page, y)
        except (IndexError, TypeError, ValueError, doccap.CaptureError):
            return {"error": "无效的定位位置。"}
        return hit or {"error": "这里是拼接补出的空白，没有对应的原始截图。"}

    def source_info(self, frame=-1, page=0):
        """加载一处拼接或当前页的来源，不重算整份文档。"""
        if not self._job or not self._result:
            return {"error": "当前没有可查看的文档。"}
        frames = sorted((self._job / "frames").glob("frame_*.png"))
        meta = json.loads((self._job / "offsets.json").read_text())
        mapping = self._result.get("page_frames")
        if mapping is None:
            mapping = doccap.source_map(frames, meta["offsets"], self._result["heights"])
        ids = mapping[page] if 0 <= page < len(mapping) else []
        if frame < 0:
            if not ids:
                return {"error": "这一页没有可追溯的原始截图。"}
            frame = ids[0]  # 页面内容模式先选首帧，用户可点击最终页或帧列表切换
        if not 0 <= frame < len(frames):
            return {"error": "无效的原始截图编号。"}
        anchor = ""
        try:
            lines = [t.strip() for t, _ in doccap.ocr_pages([frames[frame]], lambda ev: None)[0] if len(t.strip()) >= 6]
            if lines:
                anchor = max(lines, key=len)[:80]
        except Exception:
            pass  # 原图模糊时可能没有定位文字，图像对照和试拍验证仍可使用
        seam = next((s for s in self._result.get("seams", []) if s.get("frame") == frame), None)
        backups = self._job / "backups"
        undo_frame = next((i for i in (frame, frame - 1) if i >= 0 and backups.exists()
                           and any(backups.glob(f"frame_{i:04d}_*.json"))), None)
        return {"frame": frame, "page": page, "page_frames": ids,
                "prev": data_uri(frames[frame - 1], 700) if frame > 0 else None,
                "current": data_uri(frames[frame], 700),
                "prev_name": frames[frame - 1].name if frame > 0 else None,
                "current_name": frames[frame].name, "anchor": anchor,
                "resolved": bool(seam and seam.get("resolved")), "has_seam": seam is not None,
                "undo_frame": undo_frame, "can_undo": undo_frame is not None}

    def preview_seam(self, frame):
        """局部试修：只计算一对帧并生成预览，不写 offsets.json。"""
        if not self._job:
            return {"error": "当前没有可修复的文档。"}
        frames = sorted((self._job / "frames").glob("frame_*.png"))
        meta = json.loads((self._job / "offsets.json").read_text())
        try:
            d, err, expected = doccap.pair_shift(frames, meta["offsets"], frame)
            prev = Image.open(frames[frame - 1]).convert("RGB")
            img = Image.open(frames[frame]).convert("RGB")
            W, H = prev.size
            if not 0 < d < H:
                return {"error": "局部重算没有得到有效重叠，请改用重截此帧。"}
            strip = min(320, H // 3)
            preview = Image.new("RGB", (W, strip * 2), "white")
            preview.paste(prev.crop((0, H - strip, W, H)), (0, 0))
            preview.paste(img.crop((0, H - d, W, min(H, H - d + strip))), (0, strip))
            return {"frame": frame, "current_offset": meta["offsets"][frame], "candidate_offset": d,
                    "match_error": round(float(err), 2), "expected": expected,
                    "preview": array_data_uri(preview, 900)}
        except (IndexError, TypeError):
            return {"error": "缺少这组截图的位移记录，无法局部试修。"}

    def apply_seam(self, frame, candidate, page_ratio):
        frames = sorted((self._job / "frames").glob("frame_*.png")) if self._job else []
        if not frames:
            return {"error": "当前没有可修复的文档。"}
        meta_file = self._job / "offsets.json"
        meta = json.loads(meta_file.read_text())
        d, _, _ = doccap.pair_shift(frames, meta["offsets"], frame)
        if int(d) != int(candidate):
            return {"error": "原始截图已经变化，请重新局部试修。"}
        meta["offsets"][frame] = int(d)
        meta_file.write_text(json.dumps(meta))
        return self.rebuild(page_ratio, False)

    def mark_resolved(self, frame):
        """只有用户人工核对后才能解除告警，并同时持久化到 offsets 和 job。"""
        if not self._job or not self._result:
            return {"error": "当前没有可处理的文档。"}
        meta_file = self._job / "offsets.json"
        meta = json.loads(meta_file.read_text())
        resolved = set(meta.get("resolved", []))
        resolved.add(int(frame))
        meta["resolved"] = sorted(resolved)
        meta_file.write_text(json.dumps(meta))
        for seam in self._result.get("seams", []):
            if seam.get("frame") == frame:
                seam["resolved"] = True
        job_file = self._job / "job.json"
        job = json.loads(job_file.read_text())
        job["seams"] = self._result["seams"]
        job_file.write_text(json.dumps(job, ensure_ascii=False, indent=1))
        return {"seams": self._result["seams"]}

    def trial_replace(self, frame):
        """倒计时后试拍一帧；结果仅暂存内存，通过 onReplacement 返回预览。"""
        if self._busy or not self._job:
            return {"error": "当前无法试拍，请稍候。"}
        frames = sorted((self._job / "frames").glob("frame_*.png"))
        if not 0 <= frame < len(frames):
            return {"error": "无效的截图编号。"}
        if not doccap.permissions()["screen"]:
            return {"error": "请先授予「屏幕录制」权限。"}
        job = json.loads((self._job / "job.json").read_text())
        region = job.get("settings", {}).get("region") or load_settings().get("region")
        if not region:
            return {"error": "找不到原截图区域，请重新框选后再试拍。"}
        countdown = int(load_settings().get("countdown", 5))
        self._pending_replacement = None
        self._busy = True
        threading.Thread(target=self._run_trial, args=(frame, region, countdown), daemon=True).start()
        return {"ok": True}

    def _run_trial(self, frame, region, countdown):
        result = {"frame": frame}
        try:
            self._main.hide()
            overlay_mode(True)
            self._overlay = make_floating("overlay", self) if countdown > 0 else None
            if countdown > 0:
                self._show_overlay(region, countdown)
                for n in range(countdown, 0, -1):
                    self._call(self._overlay, "tick", n)
                    time.sleep(1)
            hide_now(self._overlay)
            img = doccap.grab(region)
            frames = sorted((self._job / "frames").glob("frame_*.png"))
            meta = json.loads((self._job / "offsets.json").read_text())
            result.update(doccap.assess_replacement(frames, meta["offsets"], frame, img))
            result["preview"] = array_data_uri(img, 700)
            if result["ok"]:
                self._pending_replacement = {"frame": frame, "image": img}
        except Exception as e:
            result.update(ok=False, reasons=[f"试拍失败：{e}"], direction="")
        finally:
            hide_now(self._overlay)
            destroy(self._overlay)
            self._overlay = None
            overlay_mode(False)
            self._main.show()
            self._busy = False
            self._call(self._main, "onReplacement", result)

    def confirm_replace(self, frame, page_ratio):
        if self._busy or not self._pending_replacement or self._pending_replacement["frame"] != frame:
            return {"error": "没有已通过验证的试拍，请先重新试拍。"}
        try:
            doccap.replace_frame(self._job, frame, self._pending_replacement["image"])
        except doccap.CaptureError as e:
            return {"error": str(e)}
        self._pending_replacement = None
        return self.rebuild(page_ratio, False)

    def undo_replace(self, frame, page_ratio):
        if self._busy or not self._job:
            return {"error": "当前无法撤销，请稍候。"}
        try:
            doccap.undo_replacement(self._job, frame)
        except doccap.CaptureError as e:
            return {"error": str(e)}
        self._pending_replacement = None
        return self.rebuild(page_ratio, False)

    def rebuild(self, page_ratio, restitch):
        if self._busy or not self._job or not self._job.exists():
            return {"error": "当前没有可重新生成的文档。"}
        meta = json.loads((self._job / "job.json").read_text())
        s = dict(load_settings())
        s.update({k: v for k, v in meta.get("settings", {}).items() if k in DEFAULTS})
        s["ocr"] = not meta.get("settings", {}).get("no_ocr", False)
        opt = make_opt(s, page_ratio=float(page_ratio), restitch=bool(restitch))
        opt.region = meta.get("settings", {}).get("region") or load_settings().get("region")
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
        try:
            root = prepare_output_root()
            subprocess.run(["open", str(root)])
            return True
        except doccap.CaptureError as e:
            return {"error": str(e)}

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
                        "page_frames": meta.get("page_frames"), "pdf_mb": meta.get("pdf_mb"),
                        "compress": meta.get("compress"),
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
        rep = doccap.compact_pdf([page], texts, 2.0, pdf, 48, lambda *a, **k: None)
        got = " ".join(t for t, _ in texts[0])
        ok = "SELFTEST" in got and pdf.stat().st_size > 0 and not rep["over"]
        print(f"OCR 结果：{got!r}；压缩 PDF {pdf.stat().st_size} 字节；{'自检通过' if ok else '自检失败'}")
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
