"""生成模拟长文档并切成带重叠和噪点的帧，用于离线测试拼接/分页/OCR。"""
import io, random, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

out = Path(sys.argv[1]); (out / "frames").mkdir(parents=True, exist_ok=True)
random.seed(1)
W, H = 1400, 900
font = ImageFont.truetype("/System/Library/Fonts/STHeiti Light.ttc", 30)
lines = []
for i in range(1, 160):
    if i % 12 == 0: lines.append(None)  # 段落空行
    lines.append(f"{i}. 系统应支持客户在柜面办理账户变更业务，需校验身份证号与手机号 ID{i:04d}")
doc = Image.new("RGB", (W, 60 + 52 * len(lines)), "white")
d = ImageDraw.Draw(doc)
for k, t in enumerate(lines):
    if t: d.text((60, 30 + 52 * k), t, fill="black", font=font)
# 一张表格，测试非空白区域
d.rectangle((60, 2000, 1300, 2600), outline="black", width=2)
for y in range(2000, 2600, 60): d.line((60, y, 1300, y), fill="black")
Image.fromarray(np.asarray(doc)).save(out / "truth.png")
arr = np.asarray(doc)
y = 0; n = 0; shifts = []
while True:
    f = arr[y:y + H]
    if f.shape[0] < H: break
    buf = io.BytesIO(); Image.fromarray(f).save(buf, "JPEG", quality=80)  # 模拟远程压缩噪点
    Image.open(buf).save(out / "frames" / f"frame_{n:04d}.png"); n += 1
    if y == arr.shape[0] - H: break
    ny = min(y + random.randint(450, 600), arr.shape[0] - H); shifts.append(ny - y); y = ny
import json; json.dump(shifts, open(out/"truth_shifts.json","w"))
print("frames", n, "doc height", arr.shape[0])
