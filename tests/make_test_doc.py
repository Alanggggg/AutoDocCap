"""生成用于测试 doccap 的长文档（docx + html），每段/每行带唯一校验码，供 verify.py 核对。

用法：.venv/bin/python tests/make_test_doc.py [输出目录，默认 test_doc/]
"""
import html
import json
import random
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from PIL import Image, ImageDraw, ImageFont

random.seed(20261105)
out = Path(sys.argv[1] if len(sys.argv) > 1 else "test_doc")
out.mkdir(parents=True, exist_ok=True)

MODULES = ["客户信息管理", "账户开立", "账户变更", "账户销户", "柜面转账", "跨行汇款", "定期存款", "理财产品购买",
           "贷款申请", "贷款审批", "还款管理", "信用卡申请", "额度调整", "反洗钱监测", "风险评级", "电子回单",
           "对账管理", "权限管理", "日志审计", "报表统计"]
SUBJ = ["系统", "柜员", "客户经理", "审批人员", "后台服务", "核心系统", "前置系统", "监管报送接口"]
VERB = ["应支持", "需校验", "须记录", "应在页面展示", "需调用", "应拦截", "须二次确认", "应异步通知"]
OBJ = ["客户身份证号与手机号的一致性", "账户状态及冻结标志", "交易金额是否超过单笔限额", "操作员的岗位权限",
       "客户风险等级与产品风险等级的匹配关系", "当日累计交易笔数", "印鉴卡影像比对结果", "跨行清算通道的可用状态",
       "授权柜员的复核意见", "交易流水号的唯一性"]
TAIL = ["若校验不通过，应给出明确的错误码与提示语，并禁止提交。", "处理结果需在 3 秒内返回前端。",
        "相关字段需脱敏展示，仅显示前三位与后四位。", "该操作需写入审计日志，保留期限不少于五年。",
        "异常情况下应自动回滚并通知运维值班人员。", "此规则适用于所有渠道，包括柜面、网银与手机银行。"]

ACTORS = ["柜面前端", "前置系统", "ESB总线", "核心系统", "反洗钱系统", "人行清算", "短信平台", "影像平台"]
MSGS = [("开户", "ACCT_OPEN"), ("账户查询", "ACCT_QRY"), ("转账", "TRF_EXEC"), ("限额校验", "LMT_CHK"),
        ("身份核查", "ID_VERIFY"), ("黑名单筛查", "AML_SCREEN"), ("清算", "CLR_SEND"), ("通知", "SMS_PUSH"),
        ("冲正", "TXN_REVERSE"), ("影像调阅", "IMG_FETCH")]
SELF = ["校验报文签名", "检查交易幂等", "组装 8583 报文", "记录交易流水", "计算手续费", "落地审计日志"]
FONT = "/System/Library/Fonts/STHeiti Light.ttc"
img_dir = out / "images"
img_dir.mkdir(exist_ok=True)


def seq_diagram(path):
    """画一张报文调用时序图：请求实线、应答虚线、自调用折线。"""
    actors = [ACTORS[0]] + sorted(random.sample(ACTORS[1:], random.randint(3, 5)), key=ACTORS.index)
    steps, n = [], 1
    for _ in range(random.randint(1, 2)):  # 每轮：逐级下发请求，再逐级原路返回应答
        name, msg = random.choice(MSGS)
        depth = random.randint(2, len(actors) - 1)
        for a in range(depth):
            steps.append((a, a + 1, f"{n}. {name}请求 {msg}_REQ", False))
            n += 1
        if random.random() < 0.7:
            steps.append((depth, depth, f"{n}. {random.choice(SELF)}", False))
            n += 1
        for a in range(depth, 0, -1):
            steps.append((a, a - 1, f"{n}. {name}应答 {msg}_RSP (RetCode=0000)", True))
            n += 1
    W, col, top, row = 1600, 1600 // len(actors), 110, 56
    H = top + row * len(steps) + 90
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f_box = ImageFont.truetype(FONT, 24)
    f_msg = ImageFont.truetype(FONT, 18 if len(actors) <= 5 else 15)

    def label_at(xy, text, anchor):
        box = d.textbbox(xy, text, font=f_msg, anchor=anchor)
        d.rectangle((box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2), fill="white")
        d.text(xy, text, fill="#222", font=f_msg, anchor=anchor)
    xs = [col * i + col // 2 for i in range(len(actors))]
    for x, name in zip(xs, actors):
        for y0 in (20, H - 70):
            d.rectangle((x - 95, y0, x + 95, y0 + 50), outline="#2b5797", fill="#e8eef8", width=2)
            d.text((x, y0 + 25), name, fill="#123", font=f_box, anchor="mm")
        for y in range(70, H - 70, 14):  # 生命线
            d.line((x, y, x, y + 7), fill="#999", width=2)
    for i, (a, b, label, dashed) in enumerate(steps):
        y = top + row * i + 30
        if a == b:  # 自调用
            x = xs[a]
            d.line((x, y - 12, x + 60, y - 12, x + 60, y + 12, x + 8, y + 12), fill="#333", width=2)
            d.polygon([(x, y + 12), (x + 14, y + 6), (x + 14, y + 18)], fill="#333")
            label_at((x + 70, y), label, "lm")
            continue
        x1, x2 = xs[a], xs[b]
        if dashed:
            step = 12 if x2 > x1 else -12
            for x in range(x1, x2, step):
                d.line((x, y, x + step // 2, y), fill="#c0392b", width=2)
        else:
            d.line((x1, y, x2, y), fill="#2b5797", width=2)
        s = 1 if x2 > x1 else -1
        d.polygon([(x2, y), (x2 - 14 * s, y - 7), (x2 - 14 * s, y + 7)], fill="#c0392b" if dashed else "#2b5797")
        label_at(((x1 + x2) // 2, y - 8), label, "ms")
    im.save(path)


blocks = []  # (类型, 内容)
code = 0
fig = 0


def nxt():
    global code
    code += 1
    return f"【校验{code:04d}】"


def para():
    s = "".join(f"{random.choice(SUBJ)}{random.choice(VERB)}{random.choice(OBJ)}，" for _ in range(random.randint(2, 4)))
    return nxt() + s.rstrip("，") + "。" + random.choice(TAIL)


for ci, mod in enumerate(MODULES, start=1):
    blocks.append(("h1", f"第{ci}章 {mod}模块需求"))
    for si in range(1, random.randint(2, 3) + 1):
        blocks.append(("h2", f"{ci}.{si} 功能说明"))
        for _ in range(random.randint(2, 4)):
            blocks.append(("p", para()))
        if random.random() < 0.6:
            rows = [["编号", "字段名称", "类型", "是否必填", "说明"]]
            for r in range(random.randint(5, 9)):
                rows.append([nxt(), random.choice(["客户号", "证件号码", "账号", "交易金额", "币种", "渠道码", "机构号"]),
                             random.choice(["CHAR(32)", "DECIMAL(18,2)", "VARCHAR(64)", "DATE"]),
                             random.choice(["是", "否"]), random.choice(TAIL)[:14]])
            blocks.append(("table", rows))
        if random.random() < 0.45:
            fig += 1
            p = img_dir / f"seq_{fig:02d}.png"
            seq_diagram(p)
            blocks.append(("seq", (p, f"图 {ci}-{si} {mod}报文调用时序{nxt()}")))
        if random.random() < 0.5:
            blocks.append(("ul", [nxt() + random.choice(OBJ) + "需满足监管要求" for _ in range(random.randint(3, 5))]))
        if random.random() < 0.35:
            blocks.append(("small", "备注：" + para()))
    if ci % 4 == 0:  # 稀疏页：一页只有一句话
        blocks.append(("break", None))
        blocks.append(("p", nxt() + f"本页为空白说明页：第{ci}章结束，以下进入下一部分。"))
        blocks.append(("break", None))

(out / "expected_codes.json").write_text(json.dumps({"total": code}))

# ---- docx
doc = Document()
st = doc.styles["Normal"]
st.font.name = "宋体"
st.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
st.font.size = Pt(11)
doc.add_heading("某银行综合业务系统需求规格说明书（测试用）", 0)
for kind, c in blocks:
    if kind == "h1":
        doc.add_heading(c, 1)
    elif kind == "h2":
        doc.add_heading(c, 2)
    elif kind == "p":
        doc.add_paragraph(c)
    elif kind == "small":
        doc.add_paragraph().add_run(c).font.size = Pt(8)
    elif kind == "ul":
        for it in c:
            doc.add_paragraph(it, style="List Bullet")
    elif kind == "table":
        t = doc.add_table(rows=len(c), cols=len(c[0]))
        t.style = "Table Grid"
        for r, row in enumerate(c):
            for k, v in enumerate(row):
                t.cell(r, k).text = v
    elif kind == "seq":
        doc.add_picture(str(c[0]), width=Inches(6.2))
        cap = doc.add_paragraph(c[1])
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif kind == "break":
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
doc.save(out / "测试需求文档.docx")

# ---- html（模拟浏览器里的在线文档）
e = html.escape
parts = []
for kind, c in blocks:
    if kind in ("h1", "h2"):
        parts.append(f"<{kind}>{e(c)}</{kind}>")
    elif kind == "p":
        parts.append(f"<p>{e(c)}</p>")
    elif kind == "small":
        parts.append(f"<p class=small>{e(c)}</p>")
    elif kind == "ul":
        parts.append("<ul>" + "".join(f"<li>{e(i)}</li>" for i in c) + "</ul>")
    elif kind == "table":
        parts.append("<table>" + "".join(
            "<tr>" + "".join(f"<{'th' if r == 0 else 'td'}>{e(v)}</{'th' if r == 0 else 'td'}>" for v in row) + "</tr>"
            for r, row in enumerate(c)) + "</table>")
    elif kind == "seq":
        parts.append(f'<figure><img src="images/{c[0].name}"><figcaption>{e(c[1])}</figcaption></figure>')
    elif kind == "break":
        parts.append("<div class=gap></div>")
(out / "测试需求文档.html").write_text(f"""<!doctype html><meta charset=utf-8>
<title>测试需求文档</title>
<style>
body{{background:#f0f1f3;margin:0;font:16px/1.8 "PingFang SC","Songti SC",sans-serif;color:#222}}
.doc{{max-width:860px;margin:24px auto;background:#fff;padding:48px 64px;box-shadow:0 1px 4px #0002}}
h1{{font-size:24px;margin-top:40px}} h2{{font-size:19px}} .small{{font-size:11px;color:#555}}
table{{border-collapse:collapse;width:100%;margin:12px 0}} td,th{{border:1px solid #999;padding:4px 8px;font-size:14px}}
th{{background:#f5f5f5}} .gap{{height:900px}}
figure{{margin:16px 0;text-align:center}} figure img{{width:100%;border:1px solid #ddd}}
figcaption{{font-size:14px;color:#444;margin-top:6px}}
</style>
<div class=doc><h1 style="font-size:30px">某银行综合业务系统需求规格说明书（测试用）</h1>
{chr(10).join(parts)}
</div>""", encoding="utf-8")
print(f"校验码共 {code} 个，时序图 {fig} 张 → {out.resolve()}")
