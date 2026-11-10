#!/usr/bin/env python3
"""复核页响应建议的三种状态；只测合成数据，不读取真实截图。"""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "ui/index.html").read_text(encoding="utf-8")
assert "const advice = timingAdvice(t, PRESETS, S.preset);" in HTML, "复核页未接入响应评估"
assert 'advice.apply ? `<button class="btn" id="apply-sg">采用</button>`' in HTML, "仅推荐状态应显示采用按钮"

node = shutil.which("node")
if not node:
    raise SystemExit("需要 Node.js 来直接执行界面中的 timingAdvice 函数")

script = r"""
const fs = require("fs");
const assert = require("assert");
const html = fs.readFileSync(process.argv[1], "utf8");
const match = html.match(/\/\* timing-advice-start[^*]*\*\/([\s\S]*?)\/\* timing-advice-end \*\//);
assert(match, "找不到响应评估函数");
eval(match[1]);
const presets = {
  fast: {label: "流畅", delay: .4, lag_wait: 1.5},
  normal: {label: "一般", delay: .6, lag_wait: 2},
  slow: {label: "卡顿", delay: 1.5, lag_wait: 3}
};
let a = timingAdvice(null, presets, "normal");
assert.equal(a.kind, "insufficient");
assert.match(a.text, /0 次有效滚动样本/);
assert.equal(a.apply, undefined);
a = timingAdvice({n: 2}, presets, "normal");
assert.equal(a.kind, "insufficient");
assert.match(a.text, /2 次有效滚动样本/);
a = timingAdvice({n: 5, p50: .3, p90: .6, delay: .6, suggest_delay: .6, late: 0, timeouts: 0}, presets, "normal");
assert.equal(a.kind, "suitable");
assert.match(a.text, /当前「一般」档合适/);
assert.equal(a.apply, undefined);
a = timingAdvice({n: 8, p50: 1.2, p90: 2, delay: .6, suggest_delay: 1.5, late: 2, timeouts: 1}, presets, "normal");
assert.equal(a.kind, "recommend");
assert.match(a.text, /建议下次使用 「卡顿」档/);
assert.match(a.text, /2 次需要额外等待，1 次等待超时/);
assert.deepEqual(a.apply, {preset: "slow", delay: 1.5, lag_wait: 3});
a = timingAdvice({n: 4, p50: .6, p90: 1, delay: .4, suggest_delay: .9, late: 0, timeouts: 0}, presets, "fast");
assert.equal(a.kind, "recommend");
assert.deepEqual(a.apply, {preset: "custom", delay: .9});
a = timingAdvice({n: 4, p50: .7, p90: 1, delay: 1, suggest_delay: 1, late: 0, timeouts: 0}, presets, "custom");
assert.equal(a.kind, "suitable");
assert.match(a.text, /当前自定义等待 1 秒合适/);
a = timingAdvice({n: 6, p50: .04, p90: .08, delay: .4, suggest_delay: .3, late: 0, timeouts: 0}, presets, "fast");
assert.equal(a.kind, "suitable");
assert.match(a.text, /响应中位数 <0.1 秒，90% 在 <0.1 秒内/);
a = timingAdvice({n: 6, p50: .1, p90: .12, delay: .4, suggest_delay: .4, late: 0, timeouts: 0}, presets, "fast");
assert.match(a.text, /响应中位数 0.1 秒，90% 在 0.1 秒内/);
assert.doesNotMatch(a.text, /<0.1/);
console.log("响应评估测试通过：无数据、样本不足、当前合适、推荐档位、自定义推荐、自定义合适、快速响应上限显示");
"""
result = subprocess.run([node, "-e", script, str(ROOT / "ui/index.html")], capture_output=True, text=True, timeout=20)
print(result.stdout, end="")
if result.returncode:
    print(result.stderr, end="")
    raise SystemExit(result.returncode)
