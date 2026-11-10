#!/usr/bin/env python3
"""来源追溯与安全修复回归测试；只生成合成图，不读取任何真实输出。"""
from argparse import Namespace
import json
from pathlib import Path
import tempfile

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
import app
import doccap

H, W, STEP = 240, 360, 140


def synthetic_document(height=1500):
    """每 40 像素放一组唯一标记，避免规整行距造成错误对齐。"""
    im = Image.new("RGB", (W, height), "white")
    draw = ImageDraw.Draw(im)
    for y in range(8, height - 20, 40):
        draw.rectangle((12, y, W - 12, y + 24), outline=(30, 60, 110), width=2)
        draw.text((22, y + 5), f"SYNTHETIC ROW {y:04d} | {y * 37 % 997:03d}", fill="black")
        draw.line((0, y + 31, (y * 13) % W, y + 31), fill=(170, 70, 50), width=2)
    return np.asarray(im)


def make_job(root, document, starts=(0, STEP, STEP * 2, STEP * 3)):
    frames = root / "frames"
    frames.mkdir(parents=True)
    paths = []
    for i, start in enumerate(starts):
        path = frames / f"frame_{i:04d}.png"
        Image.fromarray(document[start:start + H]).save(path)
        paths.append(path)
    offsets = [None] + [starts[i] - starts[i - 1] for i in range(1, len(starts))]
    (root / "offsets.json").write_text(json.dumps({"scale": 1.0, "offsets": offsets}))
    (root / "job.json").write_text(json.dumps({"name": root.name, "seams": []}))
    return paths, offsets


def snapshot(job):
    files = {str(p.relative_to(job)): p.read_bytes() for p in sorted((job / "frames").glob("frame_*.png"))}
    files["offsets.json"] = (job / "offsets.json").read_bytes()
    return files


def test_trace_and_persistence(base, document):
    job = base / "trace"
    frames, offsets = make_job(job, document)
    heights = [300, 360]
    mapping = doccap.source_map(frames, offsets, heights)
    assert mapping == [[0, 1, 2], [1, 2, 3]], mapping
    # 重叠区仍由前帧贡献；从新内容开始才属于后帧，跨页后按累计页高定位。
    assert doccap.source_frame_at(frames, offsets, heights, 0, 239) == {"frame": 0, "y": 239}
    assert doccap.source_frame_at(frames, offsets, heights, 0, 240) == {"frame": 1, "y": 100}
    assert doccap.source_frame_at(frames, offsets, heights, 0, 299) == {"frame": 1, "y": 159}
    assert doccap.source_frame_at(frames, offsets, heights, 1, 20) == {"frame": 1, "y": 180}
    assert doccap.source_frame_at(frames, offsets, heights, 1, 359) == {"frame": 3, "y": 239}
    # 超过一屏的空白由拼接补出，没有可替换的来源帧。
    assert doccap.source_frame_at(frames, [None, 300], [540], 0, 250) is None
    assert doccap.source_frame_at(frames, [None, 300], [540], 0, 300) == {"frame": 1, "y": 0}

    meta_file = job / "offsets.json"
    meta = json.loads(meta_file.read_text())
    meta.update(suspicious=[1, 2], resolved=[2])
    meta_file.write_text(json.dumps(meta))
    opt = Namespace(page_ratio=1.0, restitch=True, no_ocr=True, compact=False)
    result = doccap.build(job, opt, on_event=lambda ev: None)
    assert result["page_frames"] == doccap.source_map(frames, offsets, result["heights"])
    assert {s["frame"] for s in result["seams"]} == {1, 2}
    assert next(s for s in result["seams"] if s["frame"] == 2)["resolved"]
    assert not next(s for s in result["seams"] if s["frame"] == 1)["resolved"]
    kept = json.loads(meta_file.read_text())
    assert kept["suspicious"] == [1, 2] and kept["resolved"] == [2], "重拼清除了告警状态"

    before = snapshot(job)
    d, err, expected = doccap.pair_shift(frames, kept["offsets"], 1)
    assert d == STEP and err < 1 and expected == STEP
    assert snapshot(job) == before, "pair_shift 不应写文件"

    api = app.Api()
    api._job = job
    preview = api.preview_seam(1)
    assert "error" not in preview and preview["candidate_offset"] == STEP
    assert "match_error" in preview and preview["preview"].startswith("data:image/jpeg;base64,")
    assert snapshot(job) == before, "局部预览不应修改位移或原图"

    opt.restitch = False
    again = doccap.build(job, opt, on_event=lambda ev: None)
    assert not next(s for s in again["seams"] if s["frame"] == 1)["resolved"]
    assert next(s for s in again["seams"] if s["frame"] == 2)["resolved"]

    api._result = again
    marked = api.mark_resolved(1)
    assert all(s["resolved"] for s in marked["seams"])
    assert json.loads(meta_file.read_text())["resolved"] == [1, 2]
    saved_job = json.loads((job / "job.json").read_text())
    assert all(s["resolved"] for s in saved_job["seams"])
    final = doccap.build(job, opt, on_event=lambda ev: None)
    assert all(s["resolved"] for s in final["seams"]), "人工解决状态在再次生成后丢失"
    print("通过：来源映射、局部试修只读、告警保留，且只有人工标记才持久化解除")


def assert_rejected(job, index, image):
    before = snapshot(job)
    result = doccap.assess_replacement(sorted((job / "frames").glob("frame_*.png")),
                                       json.loads((job / "offsets.json").read_text())["offsets"], index, image)
    assert not result["ok"] and result["reasons"], result
    try:
        doccap.replace_frame(job, index, image)
    except doccap.CaptureError:
        pass
    else:
        raise AssertionError("无效试拍竟然被允许替换")
    assert snapshot(job) == before, "拒绝替换后原图或位移发生变化"


def test_replacement(base, document):
    job = base / "replacement"
    frames, offsets = make_job(job, document)
    original = snapshot(job)

    shifted = document[STEP + 6:STEP + 6 + H].copy()
    assessed = doccap.assess_replacement(frames, offsets, 1, shifted)
    assert assessed["ok"], assessed
    assert assessed["offsets"] == {"1": STEP + 6, "2": STEP - 6}, assessed
    assert "接近目标位置" in assessed["direction"]

    # 有轻微位置偏差仍然允许；只有无法与所有邻居可靠衔接时才拒绝。
    api = app.Api()
    api._job = job
    api._result = {"page_frames": [[0, 1, 2, 3]], "heights": [H * 2], "seams": []}
    doccap.ocr_pages = lambda paths, emit: [[("合成定位文字 SYNTHETIC", (0, 0, 1, .1))] for _ in paths]
    doccap.replace_frame(job, 1, shifted)
    assert frames[1].read_bytes() != original["frames/frame_0001.png"]
    changed = json.loads((job / "offsets.json").read_text())["offsets"]
    assert changed == [None, STEP + 6, STEP - 6, STEP]
    manifests = list((job / "backups").glob("frame_0001_*.json"))
    assert len(manifests) == 1 and (job / "backups" / "frame_0001.png").exists() is False
    backup_name = json.loads(manifests[0].read_text())["image"]
    assert (job / "backups" / backup_name).read_bytes() == original["frames/frame_0001.png"]

    # 从前一帧组成的拼接来源打开时，也必须能撤销刚替换的前一帧。
    info = api.source_info(2, 0)
    assert info["can_undo"] and info["undo_frame"] == 1, info
    doccap.undo_replacement(job, info["undo_frame"])
    assert snapshot(job) == original, "撤销没有逐字节恢复原图和原始位移"
    assert not list((job / "backups").glob("frame_0001_*")), "撤销后仍残留本次备份"

    assert_rejected(job, 1, document[1000:1000 + H].copy())  # 错误位置，无法衔接原邻居
    assert_rejected(job, 1, np.full((H, W, 3), 255, dtype=np.uint8))  # 空白图，无有效内容重叠
    assert_rejected(job, 1, document[STEP:STEP + H - 10].copy())  # 尺寸不同
    assert snapshot(job) == original
    print("通过：轻微偏移可替换、错误位置/无重叠/尺寸不符均拒绝且零写入、备份与撤销完整")


def main():
    check = ROOT / ".check"
    check.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="repair-", dir=check) as td:
        document = synthetic_document()
        base = Path(td)
        test_trace_and_persistence(base, document)
        test_replacement(base, document)
    print("全部来源追溯与安全修复测试通过（仅合成素材）")


if __name__ == "__main__":
    main()
