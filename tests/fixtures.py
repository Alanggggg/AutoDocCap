"""测试素材：直接画出一份"长文档截图"，供 simulate.py / ui_e2e.py 当作屏幕内容使用。

包含标题、段落、行距固定的表格（真实环境里曾因此错位一行）、时序图样式的图形，
每段带唯一校验码。不依赖任何本地截图，在任何 Mac 上都能生成。
"""
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT = "/System/Library/Fonts/STHeiti Light.ttc"  # macOS 自带
WORDS = ["系统应支持", "客户身份证号与手机号的一致性", "需校验账户状态及冻结标志", "交易金额是否超过单笔限额",
         "须记录操作员的岗位权限", "处理结果需在 3 秒内返回前端", "该操作需写入审计日志", "异常情况下应自动回滚",
         "此规则适用于柜面、网银与手机银行", "相关字段需脱敏展示"]


def render_doc(width=1952, pages=6, seed=7):
    """返回 RGB 数组。宽度用 Retina 像素；大约每"页"高 width*1.4。"""
    rng = random.Random(seed)
    f_h1, f_h2 = ImageFont.truetype(FONT, 46), ImageFont.truetype(FONT, 36)
    f_p, f_t = ImageFont.truetype(FONT, 30), ImageFont.truetype(FONT, 26)
    margin = 120
    target = int(width * 1.4 * pages)
    im = Image.new("RGB", (width, target + 2000), "white")
    d = ImageDraw.Draw(im)
    y, code, ch = 80, 0, 0

    def nxt():
        nonlocal code
        code += 1
        return f"【校验{code:04d}】"

    def para(text, font, lh):
        nonlocal y
        line = ""
        for c in text:
            if d.textlength(line + c, font=font) > width - 2 * margin:
                d.text((margin, y), line, fill="black", font=font)
                y, line = y + lh, ""
            line += c
        d.text((margin, y), line, fill="black", font=font)
        y += lh + 24

    while y < target:
        ch += 1
        d.text((margin, y), f"第{ch}章 测试模块需求", fill="black", font=f_h1)
        y += 100
        for sec in range(1, 3):
            d.text((margin, y), f"{ch}.{sec} 功能说明", fill="black", font=f_h2)
            y += 76
            for _ in range(rng.randint(2, 3)):
                para(nxt() + "，".join(rng.sample(WORDS, 4)) + "。", f_p, 54)
            kind = rng.random()
            if kind < 0.5:  # 行距固定的表格
                cols = [margin, margin + 300, margin + 700, margin + 1050, width - margin]
                rows = rng.randint(8, 14)
                for r in range(rows + 1):
                    d.line((cols[0], y + r * 86, cols[-1], y + r * 86), fill=(120, 120, 120), width=2)
                for x in cols:
                    d.line((x, y, x, y + rows * 86), fill=(120, 120, 120), width=2)
                for r in range(rows):
                    vals = [nxt(), rng.choice(["客户号", "证件号码", "账号", "交易金额"]),
                            rng.choice(["CHAR(32)", "DATE", "VARCHAR(64)"]), rng.choice(WORDS)[:12]]
                    for k, v in enumerate(vals):
                        d.text((cols[k] + 16, y + r * 86 + 26), v, fill="black", font=f_t)
                y += rows * 86 + 50
            elif kind < 0.8:  # 时序图样式：等距生命线 + 箭头
                n_act, steps = rng.randint(4, 6), rng.randint(4, 8)
                xs = [margin + (width - 2 * margin) * (i + .5) / n_act for i in range(n_act)]
                for x in xs:
                    d.rectangle((x - 110, y, x + 110, y + 60), outline=(43, 87, 151), fill=(232, 238, 248), width=3)
                    d.line((x, y + 60, x, y + 100 + steps * 70), fill=(150, 150, 150), width=2)
                for s in range(steps):
                    a, b = sorted(rng.sample(range(n_act), 2))
                    yy = y + 110 + s * 70
                    d.line((xs[a], yy, xs[b], yy), fill=(43, 87, 151) if s % 2 == 0 else (192, 57, 43), width=3)
                    d.text(((xs[a] + xs[b]) / 2 - 120, yy - 34), f"{s + 1}. MSG_{rng.randint(100, 999)}_REQ",
                           fill="black", font=f_t)
                y += 140 + steps * 70
                d.text((width / 2 - 200, y), f"图 {ch}-{sec} 报文时序{nxt()}", fill=(68, 68, 68), font=f_t)
                y += 80
        y += 60
    return np.asarray(im.crop((0, 0, width, y + 80)))


if __name__ == "__main__":
    import sys
    Image.fromarray(render_doc()).save(sys.argv[1] if len(sys.argv) > 1 else "fixture.png")
