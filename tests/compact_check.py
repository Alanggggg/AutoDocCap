"""PDF 压缩测试：30 页模拟远程截图（含底纹、反白字、小字），比较原图 PDF 与压缩 PDF，并检查搜索与超限处理。
用法：.venv/bin/python tests/compact_check.py"""
import io, re, sys, tempfile, time
from pathlib import Path
import numpy as np, pymupdf
from PIL import Image, ImageDraw, ImageFont
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import doccap
from fixtures import render_doc, FONT

td = Path(tempfile.mkdtemp())
doc = Image.fromarray(render_doc(pages=30)); d = ImageDraw.Draw(doc); W = doc.width
for y in range(3000, doc.height - 400, 9000):  # 每隔一段插入难处理的格式
    d.rectangle((120, y, W - 120, y + 80), fill=(140, 140, 140)); d.text((140, y + 25), "中灰底纹 MIDGRAY", fill="black", font=ImageFont.truetype(FONT, 26))
    d.rectangle((120, y + 90, W - 120, y + 170), fill=(50, 50, 50)); d.text((140, y + 115), "反白文字 INVERTED", fill="white", font=ImageFont.truetype(FONT, 26))
    d.text((140, y + 190), "极小字号 TINY 0123456789 时序报文 MSG_REQ", fill=(60, 60, 60), font=ImageFont.truetype(FONT, 16))
doc = np.asarray(doc)
ph = int(W * 1.414); pages = []
for i, y in enumerate(range(0, doc.shape[0] - ph, ph)):
    b = io.BytesIO(); Image.fromarray(doc[y:y + ph]).save(b, "JPEG", quality=80)  # 逐页加远程画面噪点（JPEG 有高度上限）
    p = td / f"page_{i + 1:03d}.png"; Image.open(b).convert("RGB").save(p); pages.append(p)
print(f"{len(pages)} 页，开始 OCR…", flush=True)
texts = doccap.ocr_pages(pages, lambda *a, **k: None)
quiet = lambda ev: print("   ", ev["msg"]) if ev.get("msg") else None
emit = lambda *a, **k: quiet({"msg": a[1] if len(a) > 1 else ""}) if a and a[0] in ("compressed", "warn") else None

orig = doccap.make_pdf(pages, texts, 2.0, td / "orig.pdf")
print(f"原图 PDF：{orig / 1048576:.1f} MB（每页 {orig / len(pages) / 1024:.0f} KB）")
for limit in (48, 0.6):
    t = time.time()
    r = doccap.compact_pdf(pages, texts, 2.0, td / f"c{limit}.pdf", limit, emit)
    pdf = pymupdf.open(td / f"c{limit}.pdf")
    hits = sum(len(pg.search_for("校验")) for pg in pdf)
    print(f"上限 {limit} MB → {r['size_mb']} MB，降分辨率 {len(r['scaled_pages'])} 页，灰度页 {r['gray_pages']}，"
          f"验收回退 {r['verify_fallback']}，超限 {r['over']}，PDF 中可搜到'校验' {hits} 处，用时 {time.time() - t:.0f}s")
# 抽一页带难处理格式的区域，输出压缩后的样子供肉眼检查
pdf = pymupdf.open(td / "c48.pdf"); pix = pdf[1].get_pixmap(dpi=144)
Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save("/tmp/compact_page.png")
