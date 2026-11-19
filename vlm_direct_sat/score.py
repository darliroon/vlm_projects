"""按 annotations.json 给模型预测打分。

hit_in   : 标注框中心落在某个预测框内
hit_near : 标注框中心到最近预测框的距离 <= 0.10（归一化，约 4.8 m）；框内记 0
iou      : 与标注框的最大 IoU
area     : 预测框总面积（越小说明框得越紧；标注框平均约 0.05）
extra    : 未命中标注框的额外预测框个数。这些框不一定是误报（用户每张只标了一处），
           只是"未核实"，没有负例前不能当误报率用。
"""
import glob
import json
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
RET = os.path.join(HERE, "ret")
ANN = json.load(open(os.path.join(HERE, "annotations.json"), encoding="utf-8"))
GT = {k: v["boxes"][0] for k, v in ANN.items() if v["status"] == "event" and v["boxes"]}


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u else 0.0


def dist_to_box(pt, b):
    dx = max(b[0] - pt[0], 0, pt[0] - b[2])
    dy = max(b[1] - pt[1], 0, pt[1] - b[3])
    return (dx * dx + dy * dy) ** 0.5


def score_tile(boxes, gt):
    c = ((gt[0] + gt[2]) / 2, (gt[1] + gt[3]) / 2)
    d = min((dist_to_box(c, b) for b in boxes), default=9)
    hit_in = any(b[0] <= c[0] <= b[2] and b[1] <= c[1] <= b[3] for b in boxes)
    ious = [iou(b, gt) for b in boxes]
    extra = sum(1 for b in boxes if dist_to_box(c, b) > 0.10)
    return dict(hit_in=hit_in, hit_near=d <= 0.10, iou=max(ious, default=0.0),
                area=sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes), extra=extra, n=len(boxes))


def load_boxes(fp):
    if not os.path.exists(fp):
        return None
    p = json.load(open(fp, encoding="utf-8")).get("parsed") or {}
    return [r["bbox"] for r in p.get("regions", []) if len(r.get("bbox", [])) == 4]


def summarize(label, files_by_tile):
    """files_by_tile: {tile: [json path, ...]} 每个路径是一次独立运行。"""
    rows = []
    for t, gt in sorted(GT.items()):
        for fp in files_by_tile.get(t, []):
            b = load_boxes(fp)
            if b is not None:
                rows.append((t, score_tile(b, gt)))
    if not rows:
        print(f"{label:34s} (无数据)")
        return
    n = len(rows)
    per = {t: sum(r["hit_near"] for tt, r in rows if tt == t) for t in {t for t, _ in rows}}
    print(f"{label:34s} n={n:2d}  hit_in={sum(r['hit_in'] for _, r in rows):2d}  "
          f"hit_near={sum(r['hit_near'] for _, r in rows):2d}  meanIoU={st.mean(r['iou'] for _, r in rows):.2f}  "
          f"面积={st.mean(r['area'] for _, r in rows):.2f}  额外框={st.mean(r['extra'] for _, r in rows):.1f}/次  "
          f"逐张hit_near={dict(sorted(per.items()))}")


if __name__ == "__main__":
    tiles = sorted(GT)
    print("标注框平均面积 %.2f" % st.mean((b[2] - b[0]) * (b[3] - b[1]) for b in GT.values()))
    for m in ("gpt-6-astra", "claude-fable-5-2"):
        for mode in ("default", "max"):
            summarize(f"v2 {m.split('-')[0]} {mode}",
                      {t: [os.path.join(RET, "v2", "results", m, mode, f"{t}.json")] for t in tiles})
    for fp in sorted(glob.glob(os.path.join(RET, "v3", "results", "*", "*", "*"))):
        v, m, rep = fp.split(os.sep)[-3:]
        summarize(f"v3 {v} {m.split('-')[0]} {rep}",
                  {t: [os.path.join(fp, f"{t}.json")] for t in tiles})
