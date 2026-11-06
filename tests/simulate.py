"""在一张"标准答案"长图上模拟滚动截图，跑完整的 capture 流程，再逐像素比对拼接结果。

不动真实屏幕：grab/scroll/鼠标都被替换成对长图的操作。
用法：.venv/bin/python tests/simulate.py [--noise]
"""
import argparse
import io
import shutil
import sys
import types
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import doccap  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--noise", action="store_true", help="每次截图做 JPEG 压缩，模拟远程画面")
ap.add_argument("--px-per-tick", type=int, default=80)
ap.add_argument("--lag", type=float, default=0.2, help="远程桌面对滚动的常规响应延迟（秒）")
ap.add_argument("--spike", type=float, default=0, help="偶发卡顿尖峰的延迟（秒），0 为不模拟")
ap.add_argument("--out", default=None, help="输出目录名（并行跑多个场景时用来区分）")
opt = ap.parse_args()

# ---- 标准答案：真实截图页面 + 插入多段"比一屏还高的空白 + 一句话"
from fixtures import render_doc  # noqa: E402
doc = render_doc(pages=12)
W = doc.shape[1]
pages = np.array_split(doc, 12)  # 切成 12 段，便于在中间插入空白
font = ImageFont.truetype("/System/Library/Fonts/STHeiti Light.ttc", 32)


def sentence(text):
    im = Image.new("RGB", (W, 80), "white")
    ImageDraw.Draw(im).text((160, 20), text, fill="black", font=font)
    return np.asarray(im)


blank = np.full((1800, W, 3), 255, np.uint8)
parts = []
for i, p in enumerate(pages):
    parts.append(p)
    if i in (3, 7):
        parts += [blank, sentence(f"【校验900{i}】本页为空白说明页：只有这一句话"), blank]
parts += [blank, sentence("【校验9999】文档最后一句，后面全是空白"), blank]
truth = np.vstack(parts)

# ---- 模拟视口与时钟：滚动在 lag 秒后才生效，模拟卡顿的远程桌面
region = [228, 206, W // 2, 741]  # 与真实测试时的选区同尺寸（点）；宽度跟随素材
scale = 2
H = region[3] * scale  # 视口宽度直接取页面宽度，与当前选区宽度无关
state = {"pos": 0, "now": 0.0, "pending": []}
park = (region[0] + region[2] / 2, region[1] + region[3] / 2)
rng = np.random.default_rng(7)


def advance(s):
    state["now"] += s
    due = [p for p in state["pending"] if p[0] <= state["now"]]
    state["pending"] = [p for p in state["pending"] if p[0] > state["now"]]
    for _, t in sorted(due):
        state["pos"] = int(np.clip(state["pos"] - t * opt.px_per_tick, 0, truth.shape[0] - H))


def grab(_):
    advance(0.15)  # 一次截图约耗时 0.15 秒
    f = truth[state["pos"]:state["pos"] + H].copy()
    if opt.noise:
        b = io.BytesIO()
        Image.fromarray(f).save(b, "JPEG", quality=75)
        f = np.asarray(Image.open(b).convert("RGB"))
    return f


def scroll(t):
    lag = opt.lag
    if opt.spike and rng.random() < 0.2:  # 20% 的滚动遇到卡顿尖峰
        lag = opt.spike
    state["pending"].append((state["now"] + lag, t))


doccap.grab, doccap.scroll = grab, scroll
doccap.mouse_pos = lambda: park
doccap.move_mouse = lambda x, y: None
doccap.time = types.SimpleNamespace(sleep=advance, time=lambda: state["now"])

name = opt.out or ("_sim_noise" if opt.noise else "_sim")
shutil.rmtree(doccap.OUT_ROOT / name, ignore_errors=True)
args = argparse.Namespace(name=name, delay=0.6, lag_wait=2.0, step=0.6, countdown=0, max_frames=3000,
                          match_thr=12.0, page_ratio=1.414, no_ocr=True, compact=False)
out = doccap.OUT_ROOT / name
doccap.capture(region, out, args)  # 直接调用引擎，不依赖 region.json
doccap.build(out, args)
print(f"模拟耗时：{state['now'] / 60:.1f} 分钟")

# ---- 比对
got = np.vstack([np.asarray(Image.open(p).convert("RGB"))
                 for p in sorted((doccap.OUT_ROOT / name / "pages").glob("page_*.png"))]).astype(np.float32)
tr = truth.astype(np.float32)
print(f"\n标准答案高 {tr.shape[0]}，拼接结果高 {got.shape[0]}")
# 末尾的纯空白不计（工具会丢弃全空白的尾页）
nonblank = np.where(tr.std(axis=(1, 2)) > 0)[0]
end = nonblank[-1] + 1
m = min(end, got.shape[0])
if not opt.noise:
    row_err = np.abs(got[:m] - tr[:m]).mean(axis=(1, 2))
    bad = np.where(row_err > 0.5)[0]
else:
    # 有压缩噪点时逐像素比对没有意义（个别彩色细线行的噪点就会超标）。改为逐段找最佳对齐偏移：
    # 拼接正确时每段都应为 0；错位一行、重复或缺失都会让偏移不为 0
    bad = []
    for lo in range(0, m - 200, 200):
        seg = tr[lo:lo + 200]
        if seg.std() < 1:
            continue  # 纯空白段无法判断对齐
        errs = {k: np.abs(got[lo:lo + 200] - tr[lo + k:lo + k + 200]).mean()
                for k in range(-4, 5) if 0 <= lo + k and lo + k + 200 <= m}
        if min(errs, key=errs.get) != 0:
            bad.append(lo)
print(f"有内容部分 {end} 行，拼接覆盖 {m} 行，{'对齐有偏移的段' if opt.noise else '误差超标的行'}：{len(bad)}"
      + (f"（首个在第 {bad[0]} 行）" if len(bad) else ""))
print("结论：", "通过 ✅" if m == end and not len(bad) else "不通过 ❌")
