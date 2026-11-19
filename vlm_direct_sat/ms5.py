"""v5: 不投票。对 v3 原始框做"放大后只修位置、不许否决"的复核。

输入: results3/tight/<model>/r{1,2,3}/<idx>.json  (已有的原始框)
输出: results5/<model>/r<k>/<idx>.json
对比: 同一次运行的 原始框 vs 复核框，逐次打分，不聚合。
"""
import json
import os
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ms3  # noqa: E402
from ms4 import MODELS, REPS, TILE_IDS, WIN, crop_b64, parse_refine  # noqa: E402
from mscall import HERE, RET, TILES  # noqa: E402
from score import GT, load_boxes, score_tile  # noqa: E402

PROMPT = """这是从一张荷兰高速公路正射航拍图（18.7 cm/像素）中裁出并放大的局部区域，约 24 m × 24 m，上北下南。图中黄色细框是上一轮给出的"车道数发生变化的过渡点"的大致位置。这个位置不一定准，可能偏几米，偏到草地、泥土、路堤或中央分隔带上。

任务：在这张放大图里，找到离黄框最近的那一处真实的车道线过渡，并给出一个紧框把它框住。过渡指下面三种之一：
A. 一列车道虚线的开始或终止处（路面随之变宽或变窄）；
B. 斜纹（人字形/斜条）缓冲区收窄的末端，与行车道虚线或边线相接的地方；
C. 两条实线之间路面渐宽或渐窄，并伴随车道线的出现或消失。

要求：
- 必须给出一个框，不要放弃。以你在放大图上看到的真实标线为准，位置可以与黄框不同；
- 框必须压在铺装路面或路面标线上，不能落在草地、泥土、阴影、分隔带上；
- 框的宽和高都不要小于 0.2（相对本裁剪图），也不要大于 0.6，要把那一小段标线连同它前后的少量路面都包进来。

先简要分析，最后用一个 ```json 代码块输出：
{"bbox": [x0, y0, x1, y1], "evidence": "一句话依据"}
坐标相对本裁剪图，归一化到 0~1，(0,0) 在左上角，x 向右，y 向下。"""


def job(a):
    model, rep, idx = a
    fp = os.path.join(RET, "v5", "results", model, f"r{rep}", f"{idx}.json")
    if os.path.exists(fp):
        return a, "cached"
    boxes = load_boxes(os.path.join(RET, "v3", "results", "tight", model, f"r{rep}", f"{idx}.json")) or []
    cands, kept = [], []
    for b in boxes[:3]:
        b64, (x0, y0) = crop_b64(idx, b)
        t0 = time.time()
        try:
            text, usage, err = ms3.call(model, b64, PROMPT)
        except Exception as e:  # noqa: BLE001
            text, usage, err = None, None, f"EXC {e}"
        if err:
            return a, f"ERR {err}"
        r = parse_refine(text) or {}
        rec = {"orig": b, "evidence": r.get("evidence"), "_latency_s": round(time.time() - t0, 1),
               "_usage": usage, "_raw": text}
        bb = r.get("bbox")
        if bb and len(bb) == 4:
            bb = [min(1, max(0, float(v))) for v in bb]
            full = [round(v, 4) for v in (x0 + bb[0] * WIN, y0 + bb[1] * WIN, x0 + bb[2] * WIN, y0 + bb[3] * WIN)]
            rec["refined_full"] = full
            kept.append({"bbox": full})
        cands.append(rec)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    json.dump({"candidates": cands, "parsed": {"regions": kept}}, open(fp, "w"), ensure_ascii=False, indent=1)
    return a, f"{len(boxes)} 候选 -> 复核框 {len(kept)}"


def get(model, stage, tile, rep):
    p = (os.path.join("v3", "results", "tight") if stage == "raw" else os.path.join("v5", "results"))
    fp = os.path.join(RET, p, model, f"r{rep}", f"{tile}.json")
    return load_boxes(fp) or []


def line(label, rows):
    print(f"{label:30s} n={len(rows):2d} hit_near={sum(s['hit_near'] for s in rows):2d} hit_in={sum(s['hit_in'] for s in rows):2d} "
          f"meanIoU={st.mean(s['iou'] for s in rows):.2f} 面积={st.mean(s['area'] for s in rows):.3f} 空={sum(s['n'] == 0 for s in rows)}")


def report():
    for m in MODELS:
        sh = m.split("-")[0]
        for stage, name in (("raw", "原始"), ("ref", "复核(不许否决)")):
            line(f"{sh} {name}", [score_tile(get(m, stage, t, r), GT[t]) for t in TILE_IDS for r in REPS])
        # 逐张: 原始 -> 复核 的命中变化 (3 次里命中几次)
        print("   逐张 命中次数(3次) 原始->复核:",
              {t: (sum(score_tile(get(m, 'raw', t, r), GT[t])['hit_near'] for r in REPS),
                   sum(score_tile(get(m, 'ref', t, r), GT[t])['hit_near'] for r in REPS)) for t in TILE_IDS})
    S = 340
    cols = [("GT", None)] + [(f"{m.split('-')[0]} raw(虚线)/refined(实线)", m) for m in MODELS]
    sheet = Image.new("RGB", ((S + 6) * len(cols), (S + 6) * len(TILE_IDS)), "white")
    pal = [(255, 120, 0), (255, 220, 0), (255, 0, 200)]
    for r_, t in enumerate(TILE_IDS):
        base = Image.open(os.path.join(TILES, f"{t}.png")).convert("RGB").resize((S, S), Image.LANCZOS)
        for c, (label, m) in enumerate(cols):
            im = base.copy()
            d = ImageDraw.Draw(im)
            d.rectangle([v * S for v in GT[t]], outline=(0, 255, 0), width=3 if m is None else 1)
            if m:
                for k, rep in enumerate(REPS):
                    for b in get(m, "ref", t, rep):
                        d.rectangle([v * S for v in b], outline=pal[k], width=3)
            d.rectangle([0, 0, S, 14], fill=(0, 0, 0))
            d.text((3, 2), f"{t} {label}", fill="white")
            sheet.paste(im, (c * (S + 6), r_ * (S + 6)))
    sheet.save(os.path.join(RET, "v5", "v5_compare.png"))
    print("saved v5_compare.png")


if __name__ == "__main__":
    if sys.argv[1] == "run":
        jobs = [(m, r, t) for m in MODELS for r in REPS for t in TILE_IDS]
        with ThreadPoolExecutor(10) as ex:
            for a, msg in ex.map(job, jobs):
                print("refine", *a, msg, flush=True)
    else:
        report()
