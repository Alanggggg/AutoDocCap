"""核对截图结果是否完整：检查 OCR 文本里每个校验码是否恰好出现一次。

用法：.venv/bin/python tests/verify.py output/<文档名>/<文档名>.txt [test_doc/expected_codes.json]
"""
import json
import re
import sys
from collections import Counter

txt = open(sys.argv[1], encoding="utf-8").read()
total = json.load(open(sys.argv[2] if len(sys.argv) > 2 else "test_doc/expected_codes.json"))["total"]

# OCR 常把数字里的 0/1 认成 O/l/I，先纠正再提取
found = Counter()
for m in re.finditer(r"校\s*验\s*([0-9OoIl|]{4})", txt):
    s = m.group(1).translate(str.maketrans("OoIl|", "00111"))
    found[int(s)] += 1

missing = [i for i in range(1, total + 1) if found[i] == 0]
dup = sorted(i for i, n in found.items() if n > 1)


def ranges(xs):
    out, start = [], None
    for i, x in enumerate(xs):
        if start is None:
            start = x
        if i + 1 == len(xs) or xs[i + 1] != x + 1:
            out.append(f"{start:04d}" if start == x else f"{start:04d}-{x:04d}")
            start = None
    return ", ".join(out)


print(f"应有 {total} 个校验码，识别到 {sum(1 for i in range(1, total + 1) if found[i])} 个")
print(f"缺失 {len(missing)} 个：{ranges(missing) or '无'}")
print(f"重复 {len(dup)} 个：{ranges(dup) or '无'}")
print("提示：零星单个缺失多半是 OCR 认错字；连续一段缺失 = 截图丢内容；连续一段重复 = 拼接错位。")
