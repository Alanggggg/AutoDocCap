#!/usr/bin/env python3
"""远程桌面长文档自动截图工具。

流程：选定屏幕上的文档区域 → 自动滚动 + 截图 → 按重叠拼接 → 按 A4 比例分页 → OCR → 可搜索 PDF。

本文件既是引擎（capture / build / select_region，供 app.py 界面调用），也是命令行入口：
    python doccap.py pick                 # 选定截图区域（只需做一次，换窗口位置后重做）
    python doccap.py capture 需求文档A     # 开始自动截图，结束后自动生成 PDF
    python doccap.py build 需求文档A       # 用已截好的帧重新生成分页/PDF（调参用）

引擎通过 on_event 回调报告进度，事件是带 "type" 和 "msg"（给人看的文字）的字典；
出错抛 CaptureError，不直接退出进程。
"""

import argparse
import io
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
REGION_FILE = ROOT / "region.json"
OUT_ROOT = ROOT / "output"


class CaptureError(Exception):
    """可以直接展示给用户的错误。"""


class UserMoved(Exception):
    pass


def print_event(ev):
    """命令行版的事件输出。"""
    if ev["type"] == "countdown":
        print(f"  {ev['n']}...", end="", flush=True)
    elif ev["type"] == "ocr":
        print(f"\r  OCR {ev['n']}/{ev['total']}", end="" if ev["n"] < ev["total"] else "\n", flush=True)
    elif ev.get("msg"):
        print(ev["msg"], flush=True)


# ---------------------------------------------------------------- 屏幕与输入

def mouse_pos():
    import Quartz
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return p.x, p.y


def move_mouse(x, y):
    import Quartz
    ev = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def scroll(ticks):
    """ticks < 0 向下滚。逐格发送，模拟真实滚轮，远程客户端转发更稳定。"""
    import Quartz
    step = -1 if ticks < 0 else 1
    for _ in range(abs(ticks)):
        ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, step)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)


def grab(region):
    x, y, w, h = region
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        r = subprocess.run(["screencapture", "-x", "-t", "png", f"-R{x},{y},{w},{h}", f.name],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise CaptureError(f"截图失败（{r.stderr.strip()}）。请到 系统设置 → 隐私与安全性 → 屏幕与系统录音，"
                               "给本程序（或你使用的终端）打开权限，然后重新打开它。")
        return np.asarray(Image.open(f.name).convert("RGB"))


def permissions():
    import Quartz
    return {"screen": bool(Quartz.CGPreflightScreenCaptureAccess()),
            "input": bool(Quartz.CGPreflightPostEventAccess())}


def request_permission(kind):
    """弹出系统授权请求，并打开对应的系统设置页。"""
    import Quartz
    if kind == "screen":
        Quartz.CGRequestScreenCaptureAccess()
        pane = "Privacy_ScreenCapture"
    else:
        Quartz.CGRequestPostEventAccess()
        pane = "Privacy_Accessibility"
    subprocess.run(["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"])


# ---------------------------------------------------------------- 图像比对

def gray(img):
    return img.astype(np.float32).mean(axis=2)


def _small(g, f=4):
    h, w = g.shape[0] // f * f, g.shape[1] // f * f
    return g[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3))


def _diff(a, b, d):
    """b 相对 a 内容上移 d 行（b[y] == a[y+d]）时重叠部分的平均差。"""
    h = a.shape[0]
    if d >= 0:
        return float(np.abs(a[d:] - b[:h - d]).mean())
    return float(np.abs(a[:h + d] - b[-d:]).mean())


def same(a, b, thr=24, frac=2e-4):
    """画面是否没变。看局部而不是平均：一屏空白里只多出一句短话，平均差几乎为 0，
    但那几行的局部差异很大。阈值高于远程画面的压缩噪点。"""
    d = np.abs(_small(gray(a)) - _small(gray(b)))
    return (d > thr).mean() < frac


def is_blank(img):
    """画面几乎全是纯色行（整屏都落在空白区里）。"""
    return (_small(gray(img)).std(axis=1) < 3).mean() > 0.98


def estimate_shift(a, b, expected=None, allow_negative=False, min_overlap=0.12, f=4, top_k=25, win=16):
    """返回 (位移像素, 匹配误差)。

    先在 1/f 缩略图上全范围搜索，取若干局部最小值，再逐个在原图上精修比较——
    缩略图对细小文字不敏感，只信它的第一名会错。行距规整的内容（列表、表格）会在
    整数行处出现次优匹配；空白区域则处处匹配，这两种情况都用 expected 打破平局。
    expected 附近 ±win 像素逐个精算：远程画面常有半像素错位，正确位置在缩略图上
    未必是局部最小，漏掉它就会被隔一行的错位"顶替"。
    """
    ga, gb = gray(a), gray(b)
    sa, sb = _small(ga, f), _small(gb, f)
    hs = sa.shape[0]
    mo = max(4, int(hs * min_overlap))
    ds = np.arange(-(hs - mo) if allow_negative else 0, hs - mo + 1)
    sc = np.array([_diff(sa, sb, int(d)) for d in ds])
    left = np.r_[np.inf, sc[:-1]]
    right = np.r_[sc[1:], np.inf]
    minima = np.where((sc <= left) & (sc <= right))[0]
    coarse = [int(ds[i]) for i in minima[np.argsort(sc[minima])][:top_k]]

    h = ga.shape[0]
    lim = h - int(h * min_overlap)
    lo = -lim if allow_negative else 0
    todo = {d for c in coarse for d in range(c * f - f, c * f + f + 1)}
    if expected is not None:
        e = int(round(expected))
        todo |= set(range(e - win, e + win + 1))
    fine = {d: _diff(ga, gb, d) for d in todo if lo <= d <= lim}
    best = min(fine.values())
    cands = [d for d, s in fine.items() if s <= best * 1.15 + 0.3]
    if expected is not None:
        d0 = min(cands, key=lambda d: (abs(d - expected), fine[d]))
    else:
        d0 = min(cands, key=lambda d: fine[d])
    return d0, fine[d0]


# ---------------------------------------------------------------- 选区

def select_region():
    """用系统自带的框选（同 Cmd+Shift+4）选区域，再在整屏截图里找到框出的图，换算成坐标。

    返回 (region, 预览图)；用户按 Esc 取消时返回 (None, None)。
    """
    import Quartz
    with tempfile.TemporaryDirectory() as td:
        sel_path = Path(td) / "sel.png"
        subprocess.run(["screencapture", "-i", "-s", "-x", str(sel_path)])
        if not sel_path.exists():
            return None, None
        sel = np.asarray(Image.open(sel_path).convert("RGB"))
    if sel.shape[0] < 60 or sel.shape[1] < 60:
        raise CaptureError("框选的区域太小，请重新框选。")

    _, ids, n = Quartz.CGGetActiveDisplayList(16, None, None)
    for did in ids[:n]:
        b = Quartz.CGDisplayBounds(did)
        bx, by, bw, bh = int(b.origin.x), int(b.origin.y), int(b.size.width), int(b.size.height)
        full = grab([bx, by, bw, bh])
        scale = full.shape[1] / bw
        pos = _locate(full, sel)
        if pos is not None:
            y, x = pos
            region = [round(bx + x / scale), round(by + y / scale),
                      round(sel.shape[1] / scale), round(sel.shape[0] / scale)]
            return region, sel
    raise CaptureError("没能在屏幕上定位到框选的区域（可能框选后画面变化了），请重新框选。")


def _locate(full, sel):
    """在 full 中找 sel 的左上角 (y, x)。同一屏幕两次截图像素基本一致，所以先用
    几行"指纹"做精确查找，再整块容差核对（容忍光标闪烁等少量变化）。"""
    h, w = sel.shape[:2]
    if h > full.shape[0] or w > full.shape[1]:
        return None
    anchors = [int(r) for r in np.argsort(-gray(sel).std(axis=1))[:6]]
    tried = set()
    for r in anchors:
        key = sel[r].tobytes()
        for y in range(r, full.shape[0] - (h - r) + 1):
            row = full[y].tobytes()
            i = row.find(key)
            while i != -1:
                if i % 3 == 0:
                    top, left = y - r, i // 3
                    if (top, left) not in tried and left + w <= full.shape[1]:
                        tried.add((top, left))
                        block = full[top:top + h, left:left + w].astype(np.int16)
                        diff = np.abs(block - sel.astype(np.int16)).max(axis=2)
                        if (diff > 10).mean() < 0.02:
                            return top, left
                i = row.find(key, i + 1)
    return None


def cmd_pick(args):
    def ask(name):
        print(f"\n请在 {args.wait} 秒内把鼠标移到文档区域的【{name}】（只移动，不要点击）")
        for i in range(args.wait, 0, -1):
            print(f"  {i}...", end="", flush=True)
            time.sleep(1)
        x, y = mouse_pos()
        print(f"  记录到 ({x:.0f}, {y:.0f})")
        return x, y

    print("区域应只包含文档正文：不要框进浏览器地址栏、文档顶部固定工具栏、侧边目录。")
    x1, y1 = ask("左上角")
    x2, y2 = ask("右下角")
    region = [int(min(x1, x2)), int(min(y1, y2)), int(abs(x2 - x1)), int(abs(y2 - y1))]
    if region[2] < 100 or region[3] < 100:
        sys.exit("区域太小，请重新选择。")
    REGION_FILE.write_text(json.dumps(region))
    preview = ROOT / "region_preview.png"
    Image.fromarray(grab(region)).save(preview)
    print(f"\n已保存区域 {region}，预览图：{preview}")
    subprocess.run(["open", str(preview)])


def load_region():
    if not REGION_FILE.exists():
        sys.exit("还没有选定区域，请先运行：python doccap.py pick")
    return json.loads(REGION_FILE.read_text())


# ---------------------------------------------------------------- 截图

def capture(region, out, opt, on_event=print_event, stop=None):
    """自动滚动截图，帧存到 out/frames，位移存到 out/offsets.json。

    opt 需要的字段：delay, lag_wait, step, countdown, max_frames, match_thr，
    可选 poll（判断稳定的截图间隔，默认 0.3 秒）、settle_timeout（等稳定的上限，默认 6 秒）。
    stop 为 threading.Event，置位后在下一次检查时停止。
    返回 {"frames", "reason", "timing"}，reason 为 end / moved / stopped / max。
    """
    poll = getattr(opt, "poll", 0.3)
    settle_timeout = getattr(opt, "settle_timeout", 6.0)
    frames_dir = out / "frames"
    if frames_dir.exists() and any(frames_dir.iterdir()):
        raise CaptureError(f"{frames_dir} 已有截图。换个名字，或删掉该目录后重试；只想重新出 PDF 用 build。")
    frames_dir.mkdir(parents=True, exist_ok=True)

    def emit(type_, msg="", **kw):
        on_event({"type": type_, "msg": msg, **kw})

    park = (region[0] + region[2] / 2, region[1] + region[3] / 2)
    reason = {"v": "end"}

    def check_mouse():
        if stop is not None and stop.is_set():
            reason["v"] = "stopped"
            raise UserMoved
        x, y = mouse_pos()
        if abs(x - park[0]) > 25 or abs(y - park[1]) > 25:
            reason["v"] = "moved"
            raise UserMoved

    latencies = []  # 每次滚动后画面开始变化用了多久，用于事后建议 --delay
    stats = {"timeouts": 0, "late": 0}
    flags = {"timeout": False, "scrolled_at": None}

    def settle(ref=None):
        """等画面停止变化（平滑滚动、懒加载、远程传输延迟），至少等 opt.delay 秒。

        ref 为滚动前的画面：记录画面第一次与它不同的时刻，减去发出滚动的时刻即远程的
        响应时间。超时仍不稳定时置 flags["timeout"]，由调用方告警并标为可疑。
        """
        t0 = time.time()
        a = grab(region)
        last_same = t0  # 最近一次确认"还没变"的时刻；变化时刻取它与"首次发现变化"的中点
        changed = ref is None or not same(a, ref)
        if ref is not None and changed:
            note_latency(t0, time.time())
        elif ref is not None:
            last_same = time.time()
        while time.time() - t0 < opt.delay + settle_timeout:
            time.sleep(poll)
            b = grab(region)
            if not changed:
                if same(b, ref):
                    last_same = time.time()
                else:
                    changed = True
                    note_latency(last_same, time.time())
            if same(a, b) and time.time() - t0 >= opt.delay:
                return b
            a = b
        flags["timeout"] = True
        stats["timeouts"] += 1
        emit("warn", f"  ⚠ 等了 {opt.delay + settle_timeout:.0f} 秒画面仍在变化，用最后一张（可能是半渲染画面）")
        return a

    def note_latency(before, after):
        if flags["scrolled_at"] is not None:
            latencies.append(max(0.0, (before + after) / 2 - flags["scrolled_at"]))
            flags["scrolled_at"] = None  # 每次滚动只记一次

    def step(ticks, ref):
        check_mouse()
        scroll(ticks)
        flags["scrolled_at"] = time.time()
        return settle(ref)

    def drain(img, wait):
        """之前有滚动没反应、现在画面动了：远程可能还在执行排队的滚动，等它们都执行完，
        否则会截到两次滚动之间的中间画面。"""
        while True:
            check_mouse()
            time.sleep(wait)
            nxt = settle()
            if same(nxt, img):
                return img
            img = nxt

    def recheck(ref, wait, why):
        """画面没变时边等边看：区分"远程卡了"和"真的没变"。返回新画面或 None。

        等待时长至少为已观测到的最慢响应的 1.5 倍——遇到过一次长卡顿，后面就多等些。
        """
        wait = max(wait, 1.5 * max(latencies, default=0))
        t0 = last = time.time()
        while time.time() - t0 < wait:
            check_mouse()
            time.sleep(poll)
            if not same(grab(region), ref):
                note_latency(last, time.time())
                break
            last = time.time()
        else:
            return None
        img = settle(ref)
        stats["late"] += 1
        emit("lag", f"  远程响应慢：{why}，额外等待 {time.time() - t0:.1f} 秒后画面才更新")
        return img

    emit("info", "请切到远程桌面窗口，在文档正文处单击一下让它获得焦点，把文档滚到最顶部。\n"
                 "开始后不要碰鼠标；中途想停止，动一下鼠标即可（已截内容会保留并生成 PDF）。")
    for i in range(opt.countdown, 0, -1):
        if stop is not None and stop.is_set():
            return {"frames": 0, "reason": "stopped", "timing": None}
        emit("countdown", n=i)
        time.sleep(1)
    emit("started", "")
    move_mouse(*park)
    time.sleep(0.3)

    frames, offsets, suspicious = [], [], []

    def keep(img, d):
        idx = len(frames)
        Image.fromarray(img).save(frames_dir / f"frame_{idx:04d}.png")
        frames.append(img)
        offsets.append(d)
        scale = img.shape[1] / region[2]
        (out / "offsets.json").write_text(json.dumps(
            {"scale": scale, "offsets": offsets, "suspicious": suspicious}))

    try:
        prev = settle()
        H = prev.shape[0]
        # 标定方向和"每格滚动像素"。自然滚动设置可能让方向反过来。
        cal, direction = 2, -1
        img = step(cal * direction, prev)
        if same(prev, img):
            late = recheck(prev, opt.lag_wait, "标定滚动")
            if late is not None:
                img = late
        if same(prev, img):
            direction = 1
            img = step(cal * direction, prev)
            if same(prev, img):
                late = recheck(prev, opt.lag_wait, "标定滚动")
                if late is not None:
                    img = late
            if same(prev, img):
                raise CaptureError("滚动没有反应：确认远程窗口已获得焦点、鼠标位于文档正文内、文档没有滚到底。")
        d, _ = estimate_shift(prev, img, allow_negative=True)
        if d < 0:  # 往上滚了：反转方向，从当前（更靠前的）位置开始即可
            direction = -direction
            keep(img, None)
        else:
            keep(prev, None)
            keep(img, d)
        ppt = max(abs(d) / cal, 1.0)
        ticks = int(np.clip(round(H * opt.step / ppt), 1, 60))
        emit("calibrated", f"标定完成：每格约 {ppt:.0f}px，每次滚 {ticks} 格", ppt=ppt, ticks=ticks)
        prev = frames[-1]

        # 滚了但画面没变，先边等边看，排除远程卡顿。仍然没变时分两种情况：
        # - 上一帧有内容：不可能是"空白区"，只能是到了文末；
        # - 上一帧几乎全白：可能正处在比截图区域还高的空白里，继续滚并记下次数，
        #   等新内容出现时按累计滚动量定位。
        # 卡顿时绝不连续发滚动，否则指令在远程积压后一起执行，位置会失控。
        idle = 0
        recent = []  # 最近几次实测的每格像素，比开头两格的标定准
        while True:
            if len(frames) >= opt.max_frames:
                reason["v"] = "max"
                emit("warn", f"已达到最多 {opt.max_frames} 帧的上限，停止截图。")
                break
            flags["timeout"] = False
            img = step(ticks * direction, prev)
            if same(prev, img):
                late = recheck(prev, opt.lag_wait, f"第 {len(frames) + 1} 帧")
                if late is None and not is_blank(prev):
                    late = recheck(prev, opt.lag_wait * 3, "疑似到达文末")
                    if late is None:
                        emit("info", "已到文档末尾。")
                        break
                if late is not None:
                    img = late
            if same(prev, img):
                idle += 1
                if idle >= 4:
                    late = recheck(prev, opt.lag_wait * 3, "疑似到达文末")
                    if late is None:
                        emit("info", "已到文档末尾。")
                        break
                    img = late
                else:
                    continue
            if idle:
                img = drain(img, opt.lag_wait)
            if recent:
                ppt = float(np.median(recent[-7:]))
            expected = (idle + 1) * ticks * ppt
            gap = idle > 0
            idle = 0
            if expected > H * 0.88:
                # 与上一帧之间隔着整屏以上的空白，没有可比对的重叠，只能按滚动量推算；
                # 无法验证，一律标为可疑
                d, err = int(round(expected)), 0.0
                flags["timeout"] = True
            else:
                d, err = estimate_shift(prev, img, expected=expected)
            if err > opt.match_thr and ticks > 1:
                # 重叠不够或没对上：滚回去，减小步长再来
                back = step(-ticks * direction, img)
                ticks = max(1, ticks // 2)
                if same(prev, back):
                    emit("warn", f"  匹配不佳(误差 {err:.1f})，步长降为 {ticks} 格重试")
                    continue
                emit("warn", f"  警告：回滚未复位，该处拼接可能有误（第 {len(frames)} 帧附近）")
                img, (d, err) = back, estimate_shift(prev, back, expected=ticks * ppt)
            if d <= 0:
                continue
            # 文末最后一滚通常滚不满，位移偏小是正常的，不算可疑
            odd = abs(d - expected) > max(6, 0.015 * expected) and expected <= H * 0.88
            sus = odd or flags["timeout"]
            if sus:
                suspicious.append(len(frames))
            keep(img, d)
            prev = img
            if not gap and not odd:
                recent.append(d / ticks)
            emit("frame", f"  第 {len(frames)} 帧  位移 {d}px  误差 {err:.1f}"
                 + (f"  ⚠ 预期约 {expected:.0f}px，可疑" if odd else "")
                 + ("  ⚠ 画面未稳定，可疑" if flags["timeout"] else ""),
                 n=len(frames), shift=d, err=round(err, 1), suspicious=bool(sus),
                 n_suspicious=len(suspicious), latency=round(latencies[-1], 1) if latencies else None)
    except UserMoved:
        emit("info", "已停止截图。" if reason["v"] == "stopped" else "检测到鼠标移动，已停止截图。")

    if not frames:
        raise CaptureError("没有截到任何画面。")
    timing = report_timing(latencies, stats, opt, emit)
    return {"frames": len(frames), "reason": reason["v"], "timing": timing}


def report_timing(latencies, stats, opt, emit):
    """报告远程桌面的响应速度，并给出 delay 建议值。"""
    if not latencies:
        return None
    lat = np.array(latencies)
    p50, p90 = float(np.median(lat)), float(np.percentile(lat, 90))
    # 按常见响应时间建议；偶发的长卡顿由"边等边看"自动兜住，不必为它拉长每一帧
    suggest = round(max(0.3, p50 + 0.3), 1)
    t = {"mean": round(float(lat.mean()), 2), "p50": round(p50, 2), "p90": round(p90, 2),
         "max": round(float(lat.max()), 2), "n": len(lat), "late": stats["late"],
         "timeouts": stats["timeouts"], "suggest_delay": suggest, "delay": opt.delay}
    msg = (f"远程响应：平均 {t['mean']:.1f} 秒，90% 在 {p90:.1f} 秒内，最慢 {t['max']:.1f} 秒"
           f"（共 {len(lat)} 次滚动）")
    if stats["late"] or stats["timeouts"]:
        msg += f"\n  其中 {stats['late']} 次需要额外等待才更新，{stats['timeouts']} 次等待超时"
    if abs(suggest - opt.delay) >= 0.3:
        msg += (f"\n  建议下次用 --delay {suggest}（本次为 {opt.delay}）"
                + ("；偶发的长卡顿会自动多等，无需按最慢值设置" if p90 > p50 * 2 else ""))
    emit("timing", msg, timing=t)
    return t


def cmd_capture(args):
    region = load_region()
    out = OUT_ROOT / args.name
    try:
        capture(region, out, args)
        print("开始生成文档…")
        build(out, args)
    except CaptureError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit("\n已中断。已截的帧保留在 frames/ 下，可用 build 生成文档。")


# ---------------------------------------------------------------- 拼接、分页

class Paginator:
    """把逐帧追加的行流切成页，在空白行（段落/页间隙）处下刀，避免切断文字。"""

    def __init__(self, width, ratio, pages_dir):
        self.target = int(width * ratio)
        self.min_h = int(self.target * 0.55)
        self.max_h = int(self.target * 1.25)
        self.dir = pages_dir
        self.buf = None
        self.paths = []
        self.added = 0  # 已追加的总行数
        self.starts = []  # 每页在整条长图里的起始行
        self.heights = []

    def add(self, rows):
        self.added += rows.shape[0]
        self.buf = rows if self.buf is None else np.vstack([self.buf, rows])
        while self.buf.shape[0] >= self.max_h:
            cut = self._find_cut()
            self._emit(self.buf[:cut])
            self.buf = self.buf[cut:]

    def finish(self):
        if self.buf is not None and gray(self.buf).std() > 2:
            self._emit(self.buf)
        return self.paths

    def _find_cut(self):
        g = gray(self.buf[self.min_h:self.max_h])
        blank = g.std(axis=1) < 3
        best, best_score = self.max_h, 0.0
        i, n = 0, len(blank)
        span = self.max_h - self.min_h
        while i < n:
            if not blank[i]:
                i += 1
                continue
            j = i
            while j < n and blank[j]:
                j += 1
            mid = self.min_h + (i + j) // 2
            score = (j - i) * (1 - 0.5 * abs(mid - self.target) / span)
            if score > best_score:
                best, best_score = mid, score
            i = j
        return best

    def _emit(self, rows):
        p = self.dir / f"page_{len(self.paths) + 1:03d}.png"
        Image.fromarray(rows).save(p)
        self.starts.append(self.added - self.buf.shape[0])
        self.heights.append(rows.shape[0])
        self.paths.append(p)

    def page_of(self, row):
        return max(1, sum(1 for s in self.starts if s <= row))


def build(out, opt, on_event=print_event):
    """拼接、分页、OCR、生成 PDF。返回 {"pages", "heights", "width", "seams", "pdf", "txt"}，
    seams 为可疑拼接位置 [{"page": 页码, "y": 页内像素行}]。"""
    def emit(type_, msg="", **kw):
        on_event({"type": type_, "msg": msg, **kw})

    frames = sorted((out / "frames").glob("frame_*.png"))
    if not frames:
        raise CaptureError(f"{out / 'frames'} 下没有截图。")
    meta_file = out / "offsets.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {"scale": 2.0, "offsets": []}
    offsets = meta["offsets"]
    if getattr(opt, "restitch", False):
        offsets = restitch(frames, offsets, emit)
        meta["offsets"] = offsets
        meta["suspicious"] = []
        meta_file.write_text(json.dumps(meta))
    suspicious = set(meta.get("suspicious", []))

    pages_dir = out / "pages"
    pages_dir.mkdir(exist_ok=True)
    for old in pages_dir.glob("page_*.png"):
        old.unlink()

    emit("stage", "正在拼接和分页…", stage="stitch")
    prev = np.asarray(Image.open(frames[0]).convert("RGB"))
    H, W = prev.shape[:2]
    pag = Paginator(W, opt.page_ratio, pages_dir)
    pag.add(prev)
    last_d = None
    seam_rows = []  # 可疑拼接在长图中的行号
    for i, fp in enumerate(frames[1:], start=1):
        img = np.asarray(Image.open(fp).convert("RGB"))
        if i < len(offsets) and offsets[i]:
            d = offsets[i]
        else:  # 没有记录位移（例如手动放入的截图），现场估算
            d, _ = estimate_shift(prev, img, expected=last_d)
        if i in suspicious:
            seam_rows.append(pag.added)
        if d > H:  # 中间是整屏以上的空白，用上一帧底部的背景色补齐
            pag.add(np.repeat(prev[-1:], d - H, axis=0))
            pag.add(img)
        elif d > 0:
            pag.add(img[H - d:])
        prev, last_d = img, d
    pages = pag.finish()
    seams = []
    for r in seam_rows:
        p = pag.page_of(r)
        if p <= len(pages):
            seams.append({"page": p, "y": int(r - pag.starts[p - 1])})
    emit("paged", f"分页完成：{len(pages)} 页 → {pages_dir}", pages=len(pages))
    if seams:
        emit("seams", "⚠ 以下页面有可疑拼接，请人工核对是否有重复或缺失的行："
             + "、".join(f"第 {s['page']} 页" for s in seams), seams=seams)

    compact = getattr(opt, "compact", True)
    # 压缩要靠 OCR 判断字的大小、验收清晰度，所以压缩时即使关了"可搜索文字"也要识别，只是不写进 PDF
    texts = ocr_pages(pages, emit) if (compact or not opt.no_ocr) else [[] for _ in pages]
    layer = texts if not opt.no_ocr else [[] for _ in pages]
    pdf = out / f"{out.name}.pdf"
    compress = None
    if compact:
        emit("stage", "正在压缩 PDF…", stage="compress")
        compress = compact_pdf(pages, texts, meta.get("scale", 2.0), pdf, getattr(opt, "pdf_limit_mb", 48), emit,
                               layer=layer)
    else:
        emit("stage", "正在生成 PDF…", stage="pdf")
        make_pdf(pages, layer, meta.get("scale", 2.0), pdf)
    txt = None
    if not opt.no_ocr:
        txt = out / f"{out.name}.txt"
        txt.write_text("\n\n".join(
            f"===== 第 {i} 页 =====\n" + "\n".join(t for t, _ in page)
            for i, page in enumerate(texts, start=1)), encoding="utf-8")
        emit("info", f"文字版：{txt}")
    emit("info", f"PDF：{pdf}")
    return {"pages": [str(p) for p in pages], "heights": pag.heights, "width": W,
            "seams": seams, "pdf": str(pdf), "txt": str(txt) if txt else None,
            "pdf_mb": round(pdf.stat().st_size / 1024 / 1024, 2), "compress": compress}


def restitch(frames, old, emit):
    """用当前算法重算已截帧的位移，以前后帧位移的中位数作预期。整屏以上空白处的位移
    是按滚动量推算的（没有重叠可比），保留原值。"""
    H = Image.open(frames[0]).size[1]
    normal = [d for d in old[1:] if d and d <= H * 0.88]
    new = [None]
    prev = np.asarray(Image.open(frames[0]).convert("RGB"))
    for i, fp in enumerate(frames[1:], start=1):
        emit("restitch", n=i, total=len(frames) - 1)
        img = np.asarray(Image.open(fp).convert("RGB"))
        o = old[i] if i < len(old) else None
        if o and o > H * 0.88:
            d = o
        else:
            near = [x for x in old[max(1, i - 4):i + 5] if x and x <= H * 0.88]
            exp = float(np.median(near or normal)) if (near or normal) else None
            d, err = estimate_shift(prev, img, expected=exp)
            if o and d != o:
                emit("info", f"  第 {i + 1} 帧：位移 {o} → {d}（误差 {err:.1f}）")
        new.append(d)
        prev = img
    return new


def cmd_build(args):
    out = OUT_ROOT / args.name
    if not out.exists():
        sys.exit(f"找不到 {out}")
    try:
        build(out, args)
    except CaptureError as e:
        sys.exit(str(e))


# ---------------------------------------------------------------- OCR 与 PDF

def ocr_pages(paths, emit):
    """macOS Vision 离线识别。返回每页 [(文字, (x, y, w, h) 归一化、左上原点)]。"""
    import objc
    import Vision
    from Foundation import NSURL

    results = []
    for n, p in enumerate(paths, start=1):
        emit("ocr", n=n, total=len(paths))
        with objc.autorelease_pool():  # 在后台线程里跑时需要，否则内存只增不减
            handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
                NSURL.fileURLWithPath_(str(p)), None)
            req = Vision.VNRecognizeTextRequest.alloc().init()
            req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
            req.setRecognitionLanguages_(["zh-Hans", "en-US"])
            req.setUsesLanguageCorrection_(True)
            handler.performRequests_error_([req], None)
            items = []
            for obs in req.results() or []:
                cand = obs.topCandidates_(1)
                if not cand:
                    continue
                b = obs.boundingBox()
                box = (b.origin.x, 1 - b.origin.y - b.size.height, b.size.width, b.size.height)
                items.append((str(cand[0].string()), box))
        items.sort(key=lambda it: (round(it[1][1] * 200), it[1][0]))
        results.append(items)
    return results


def make_pdf(pages, texts, scale, pdf_path, images=None):
    """images 为每页实际嵌入的图片字节（压缩后的）；为 None 时直接嵌入原图。
    页面尺寸和文字层始终按原图计算，所以嵌入图缩小了也不影响版面和搜索定位。"""
    import pymupdf

    doc = pymupdf.open()
    font = pymupdf.Font("china-s")
    for i, (p, items) in enumerate(zip(pages, texts)):
        w, h = Image.open(p).size
        pw, ph = w / scale, h / scale
        page = doc.new_page(width=pw, height=ph)
        if images is None:
            page.insert_image(page.rect, filename=str(p))
        else:
            page.insert_image(page.rect, stream=images[i])
        for text, (x, y, bw, bh) in items:
            fs = bh * ph * 0.9
            tl = font.text_length(text, fontsize=fs)
            if fs < 1 or tl <= 0:
                continue
            origin = pymupdf.Point(x * pw, (y + bh) * ph - bh * ph * 0.15)
            # 隐藏文字层：横向拉伸到与识别框同宽，便于搜索高亮和复制
            page.insert_text(origin, text, fontsize=fs, fontname="china-s", render_mode=3,
                             morph=(origin, pymupdf.Matrix(bw * pw / tl, 1)))
    doc.save(pdf_path, garbage=3, deflate=True)
    doc.close()
    return Path(pdf_path).stat().st_size


# ---------------------------------------------------------------- PDF 压缩

def _box_mean(a, r):
    c = np.pad(a, ((r + 1, r), (r + 1, r)), mode="edge").cumsum(0).cumsum(1)
    return (c[2 * r + 1:, 2 * r + 1:] - c[:-2 * r - 1, 2 * r + 1:]
            - c[2 * r + 1:, :-2 * r - 1] + c[:-2 * r - 1, :-2 * r - 1]) / (2 * r + 1) ** 2


def binarize(g, r=24, gap=45):
    """转 1 位黑白。按局部背景亮度定阈值：浅色背景上比背景暗一截的算字，深色背景上比背景亮
    一截的算字——灰色表头、浅色填色框会变白而字保留，深色底白字也能保住。返回 True=白。
    （实测常见的 Sauvola 自适应阈值会毁掉深色底白字，固定阈值会让中灰底纹整块变黑。）"""
    bg = _box_mean(g, r)
    return np.where(bg >= 110, g >= bg - gap, g > bg + gap)


def _is_photo(g):
    """页面里有大面积"有纹理的中间调"（嵌入的截图、照片），黑白化会把它弄花。"""
    t = 16
    h, w = g.shape[0] // t * t, g.shape[1] // t * t
    tiles = g[:h, :w].reshape(h // t, t, w // t, t)
    mean, std = tiles.mean(axis=(1, 3)), tiles.std(axis=(1, 3))
    textured = (mean > 50) & (mean < 205) & (std > 14)
    return textured.mean() > 0.12


def _encode(g, s, kind):
    """g：原图灰度数组；s：缩放比例；kind：bw（1 位黑白 PNG）或 gray（灰度 JPEG）。"""
    im = Image.fromarray(g.astype(np.uint8))
    if s < 1:
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
    buf = io.BytesIO()
    if kind == "bw":
        Image.fromarray(binarize(np.asarray(im, dtype=np.float32))).convert("1").save(buf, "PNG", optimize=True)
    else:
        im.save(buf, "JPEG", quality=60)
    return buf.getvalue()


def _recall(ref, got):
    """原图认出的字，压缩后还能认出多少（0~1）。
    只数汉字、字母、数字，不看顺序：OCR 对空格、下划线、点号时有时无，对图里零散标签的
    输出顺序也不固定（同一张原图两次识别都可能不同），按序比对会把清晰的页误判为不合格。"""
    from collections import Counter
    a = Counter(c for c in ref if c.isalnum())
    b = Counter(c for c in got if c.isalnum())
    return sum((a & b).values()) / max(1, sum(a.values()))


def _ocr_bytes(data, emit):
    with tempfile.NamedTemporaryFile(suffix=".img") as f:
        f.write(data)
        f.flush()
        return "".join(t for t, _ in ocr_pages([Path(f.name)], lambda *a, **k: None)[0])


def compact_pdf(pages, texts, scale, pdf_path, limit_mb, emit, layer=None):
    """把 PDF 压到 limit_mb 以内，并尽量小：

    1. 每页默认转成原分辨率的 1 位黑白图（实测每页约 45KB，只有灰度 PNG 的 1/10），小字不受影响；
    2. 照片类页面用灰度 JPEG；
    3. 验收：对压缩后的页面再做一次 OCR，认出的字比原图明显少的页改用灰度 JPEG；
    4. 总大小仍超过上限时，按每页最小字高，把大字页逐档降低分辨率（降过的页同样验收）。
    texts 用于分析；layer 是实际写进 PDF 的文字层（关闭"可搜索文字"时为空）。返回压缩报告。
    """
    layer = texts if layer is None else layer
    limit = limit_mb * 1024 * 1024
    n = len(pages)
    gray_of = lambda i: np.asarray(Image.open(pages[i]).convert("L"), dtype=np.float32)
    ref = ["".join(t for t, _ in items) for items in texts]
    # 每页最小字高（原图像素）：只看至少两个字的行，排除识别噪声
    hmin = []
    for p, items in zip(pages, texts):
        H = Image.open(p).size[1]
        hs = [bh * H for t, (_, _, _, bh) in items if len(t.strip()) >= 2 and bh * H >= 6]
        hmin.append(min(hs) if hs else None)

    kind, scl, cache = [], [1.0] * n, {}
    for i in range(n):
        emit("compress", n=i + 1, total=n)
        g = gray_of(i)
        kind.append("gray" if _is_photo(g) else "bw")
        cache[(i, 1.0)] = _encode(g, 1.0, kind[i])

    def verified(i, s):
        """压缩后的页面 OCR 结果与原图足够接近。字很少的页不验（比例不可靠）。"""
        if len(ref[i]) < 20:
            return True
        return _recall(ref[i], _ocr_bytes(cache[(i, s)], emit)) >= 0.93

    fallback = []
    if any(ref):
        for i in range(n):
            emit("verify", n=i + 1, total=n)
            if kind[i] == "bw" and not verified(i, 1.0):
                kind[i] = "gray"
                cache[(i, 1.0)] = _encode(gray_of(i), 1.0, "gray")
                fallback.append(i + 1)

    size = make_pdf(pages, layer, scale, pdf_path, [cache[(i, 1.0)] for i in range(n)])
    floor = None
    # 超限：要求的最小字高逐档降低，只缩字大的页；1 位黑白比灰度更需要像素，所以起点较高
    for target in (24, 20, 17, 14):
        if size <= limit:
            break
        floor = target
        for i in range(n):
            if hmin[i] is None or kind[i] != "bw":
                continue
            s = round(min(1.0, max(0.35, target / hmin[i])), 2)
            if s >= scl[i]:
                continue
            if (i, s) not in cache:
                cache[(i, s)] = _encode(gray_of(i), s, "bw")
            if verified(i, s):
                scl[i] = s
        size = make_pdf(pages, layer, scale, pdf_path, [cache[(i, scl[i])] for i in range(n)])

    report = {"size_mb": round(size / 1024 / 1024, 2), "limit_mb": limit_mb, "over": size > limit,
              "gray_pages": [i + 1 for i in range(n) if kind[i] == "gray"],
              "scaled_pages": [i + 1 for i in range(n) if scl[i] < 1], "min_text_px": floor,
              "verify_fallback": fallback}
    msg = f"PDF {report['size_mb']} MB（上限 {limit_mb} MB）：黑白压缩，"
    msg += (f"{len(report['scaled_pages'])} 页大字页降低了分辨率" if report["scaled_pages"] else "全部保持原分辨率")
    if report["gray_pages"]:
        msg += f"；第 {'、'.join(map(str, report['gray_pages']))} 页含图片或特殊配色，保留灰度"
    emit("compressed", msg, report=report)
    if report["over"]:
        emit("warn", f"⚠ 已压到可读性底线，PDF 仍有 {report['size_mb']} MB，超过 {limit_mb} MB 上限。")
    return report


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(description="远程桌面长文档自动截图 → 可搜索 PDF")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pick", help="选定截图区域")
    p.add_argument("--wait", type=int, default=4, help="每个角的等待秒数")
    p.set_defaults(func=cmd_pick)

    def build_opts(p):
        p.add_argument("--page-ratio", type=float, default=1.414, help="页高/页宽，默认 A4")
        p.add_argument("--no-ocr", action="store_true", help="PDF 不带可搜索文字层")
        p.add_argument("--no-compact", dest="compact", action="store_false", help="不压缩，PDF 嵌入彩色原图")
        p.add_argument("--pdf-limit", dest="pdf_limit_mb", type=float, default=48, help="压缩后 PDF 的大小上限（MB）")

    p = sub.add_parser("capture", help="自动滚动截图并生成 PDF")
    p.add_argument("name", help="文档名，输出到 output/<name>/")
    p.add_argument("--delay", type=float, default=0.6, help="每次滚动后至少等待的秒数（远程卡顿就调大）")
    p.add_argument("--lag-wait", type=float, default=2.0,
                   help="滚动后画面没变时，额外等待多少秒再确认（区分远程卡顿与文末/空白）")
    p.add_argument("--poll", type=float, default=0.3, help="判断画面是否稳定时，两次截图的间隔秒数")
    p.add_argument("--settle-timeout", type=float, default=6.0, help="等画面稳定的最长秒数，超时会标为可疑")
    p.add_argument("--step", type=float, default=0.6, help="每次滚动占区域高度的比例")
    p.add_argument("--countdown", type=int, default=5, help="开始前倒计时秒数")
    p.add_argument("--max-frames", type=int, default=3000)
    p.add_argument("--match-thr", type=float, default=12.0, help="拼接匹配误差阈值")
    build_opts(p)
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("build", help="用已有截图重新生成分页和 PDF")
    p.add_argument("name")
    p.add_argument("--restitch", action="store_true", help="用最新算法重算各帧位移（修正旧版拼错的地方）")
    build_opts(p)
    p.set_defaults(func=cmd_build)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
