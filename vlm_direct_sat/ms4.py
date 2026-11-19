"""v4: 多次采样 + 局部放大复核 + 投票。

阶段 1: 复用 v3 prompt，每个(模型,切片)跑 3 次 (results3/tight/<model>/r1..r3)。
阶段 2: 对每个候选框，以其中心裁出 0.5x0.5 的局部区域放大，叠上候选框，让模型复核并重给框
        (可以回答 none)。结果: results4/<model>/r<k>/<idx>.json
阶段 3: 把复核后的框映射回整图坐标，按中心距离聚类，至少 2/3 次运行一致才保留。

用法: python3 ms4.py run      # 调用模型
      python3 ms4.py report   # 打分 + 出对比图
"""
import base64
import io
import json
import os
import re
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ms3  # noqa: E402
from mscall import HERE, RET, TILES  # noqa: E402
from score import GT, iou, load_boxes, score_tile  # noqa: E402

MODELS = ("gpt-6-astra", "claude-fable-5-2")
TILE_IDS = sorted(GT)
REPS = (1, 2, 3)
WIN = 0.5          # 裁剪窗口边长（占整图比例）
CLUSTER_D = 0.12   # 聚类：中心距离阈值（归一化）
MIN_VOTES = 2

REFINE_PROMPT = """这是从一张荷兰高速公路正射航拍图（18.7 cm/像素）中裁出并放大的局部区域，约 24 m × 24 m，上北下南。图中黄色细框是上一轮给出的、疑似"车道数发生变化的过渡点"的候选位置。这个候选位置可能偏离，甚至落在非道路区域（草地、泥土、路堤、中央分隔带）上。

任务：
1. 判断候选框附近是否确有车道线层面的过渡：新的车道虚线开始出现或终止；斜纹（人字形/斜条）缓冲区收窄的末端与行车道虚线、边线相接处；两条实线之间路面渐宽或渐窄并伴随车道线出现或消失。
2. 如果有，给出过渡点所在位置的紧框。框必须落在铺装路面或路面标线上，不能落在草地、泥土、阴影、分隔带上。框的宽和高通常在 0.15~0.5（相对本裁剪图）。位置可以与黄框不同，以你在图上看到的真实标线为准。
3. 如果候选框附近没有真正的过渡，回答 none，不要为了迎合候选框而硬找。

先简要分析，最后用一个 ```json 代码块输出：
{"verdict": "transition" 或 "none", "bbox": [x0, y0, x1, y1] 或 null, "evidence": "一句话依据"}
坐标相对本裁剪图，归一化到 0~1，(0,0) 在左上角，x 向右，y 向下。"""


def crop_b64(idx, box):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    x0 = min(max(cx - WIN / 2, 0), 1 - WIN)
    y0 = min(max(cy - WIN / 2, 0), 1 - WIN)
    im = Image.open(os.path.join(TILES, f"{idx}.png")).convert("RGB")
    W = im.width
    S = 768
    c = im.crop((round(x0 * W), round(y0 * W), round((x0 + WIN) * W), round((y0 + WIN) * W))).resize((S, S), Image.LANCZOS)
    d = ImageDraw.Draw(c)
    d.rectangle([(box[0] - x0) / WIN * S, (box[1] - y0) / WIN * S,
                 (box[2] - x0) / WIN * S, (box[3] - y0) / WIN * S], outline=(255, 255, 0), width=2)
    buf = io.BytesIO()
    c.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode(), (x0, y0)


def parse_refine(text):
    for m in reversed(re.findall(r"```json\s*(\{.*?\})\s*```", text or "", re.S)):
        try:
            return json.loads(m)
        except ValueError:
            pass
    return None


def refine_job(a):
    model, rep, idx = a
    fp = os.path.join(RET, "v4", "results", model, f"r{rep}", f"{idx}.json")
    if os.path.exists(fp):
        return a, "cached"
    boxes = load_boxes(os.path.join(RET, "v3", "results", "tight", model, f"r{rep}", f"{idx}.json")) or []
    cands, kept = [], []
    for b in boxes[:3]:
        b64, (x0, y0) = crop_b64(idx, b)
        t0 = time.time()
        try:
            text, usage, err = ms3.call(model, b64, REFINE_PROMPT)
        except Exception as e:  # noqa: BLE001
            text, usage, err = None, None, f"EXC {e}"
        if err:
            return a, f"ERR {err}"  # 失败不落盘，便于重跑
        r = parse_refine(text)
        rec = {"orig": b, "verdict": (r or {}).get("verdict"), "evidence": (r or {}).get("evidence"),
               "_latency_s": round(time.time() - t0, 1), "_usage": usage, "_raw": text}
        if r and r.get("verdict") == "transition" and r.get("bbox") and len(r["bbox"]) == 4:
            bb = [min(1, max(0, float(v))) for v in r["bbox"]]
            full = [x0 + bb[0] * WIN, y0 + bb[1] * WIN, x0 + bb[2] * WIN, y0 + bb[3] * WIN]
            rec["refined_full"] = [round(v, 4) for v in full]
            kept.append({"bbox": rec["refined_full"]})
        cands.append(rec)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    json.dump({"candidates": cands, "parsed": {"regions": kept}}, open(fp, "w"), ensure_ascii=False, indent=1)
    return a, f"{len(boxes)} 候选 -> 保留 {len(kept)}"


def vote(runs):
    """runs: [[box,...], ...] 每次运行的框列表。返回投票通过的框(簇内平均)。"""
    items = [(ri, b) for ri, bs in enumerate(runs) for b in bs]
    clusters = []
    for ri, b in items:
        c = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        for cl in clusters:
            m = cl["c"]
            if ((c[0] - m[0]) ** 2 + (c[1] - m[1]) ** 2) ** 0.5 <= CLUSTER_D:
                cl["m"].append((ri, b))
                cl["c"] = (st.mean((x[1][0] + x[1][2]) / 2 for x in cl["m"]), st.mean((x[1][1] + x[1][3]) / 2 for x in cl["m"]))
                break
        else:
            clusters.append({"m": [(ri, b)], "c": c})
    out = []
    for cl in clusters:
        if len({ri for ri, _ in cl["m"]}) >= MIN_VOTES:
            out.append([round(st.mean(x[1][k] for x in cl["m"]), 4) for k in range(4)])
    return out


def get(model, stage, tile, rep):
    fp = (os.path.join(RET, "v3", "results", "tight", model, f"r{rep}", f"{tile}.json") if stage == "raw"
          else os.path.join(RET, "v4", "results", model, f"r{rep}", f"{tile}.json"))
    return load_boxes(fp) or []


def run():
    jobs = [("tight", m, r, t) for m in MODELS for r in REPS for t in TILE_IDS]
    with ThreadPoolExecutor(10) as ex:
        for a, msg in ex.map(ms3.job, jobs):
            if msg != "cached":
                print("stage1", *a, msg, flush=True)
    jobs = [(m, r, t) for m in MODELS for r in REPS for t in TILE_IDS]
    with ThreadPoolExecutor(10) as ex:
        for a, msg in ex.map(refine_job, jobs):
            print("refine", *a, msg, flush=True)


def stats(label, rows):
    n = len(rows)
    print(f"{label:34s} n={n:2d} hit_near={sum(s['hit_near'] for s in rows):2d} hit_in={sum(s['hit_in'] for s in rows):2d} "
          f"meanIoU={st.mean(s['iou'] for s in rows):.2f} 面积={st.mean(s['area'] for s in rows):.3f} "
          f"框数/次={st.mean(s['n'] for s in rows):.1f} 空={sum(s['n'] == 0 for s in rows)} "
          f"额外框/次={st.mean(s['extra'] for s in rows):.1f}")


def report():
    for m in MODELS:
        short = m.split("-")[0]
        raw = [score_tile(get(m, "raw", t, r), GT[t]) for t in TILE_IDS for r in REPS]
        ref = [score_tile(get(m, "ref", t, r), GT[t]) for t in TILE_IDS for r in REPS]
        voted_raw = {t: vote([get(m, "raw", t, r) for r in REPS]) for t in TILE_IDS}
        voted_ref = {t: vote([get(m, "ref", t, r) for r in REPS]) for t in TILE_IDS}
        stats(f"{short} 原始(3次x5张)", raw)
        stats(f"{short} 仅放大复核(3次x5张)", ref)
        stats(f"{short} 仅投票(原始框)", [score_tile(voted_raw[t], GT[t]) for t in TILE_IDS])
        stats(f"{short} 复核+投票", [score_tile(voted_ref[t], GT[t]) for t in TILE_IDS])
        print("   逐张 复核+投票 hit_near:",
              {t: score_tile(voted_ref[t], GT[t])["hit_near"] for t in TILE_IDS},
              " 框数:", {t: len(voted_ref[t]) for t in TILE_IDS})
        json.dump({"raw_vote": voted_raw, "refined_vote": voted_ref},
                  open(os.path.join(RET, "v4", f"v4_final_{short}.json"), "w"), indent=1)
    # 对比图: 每行一张切片
    S = 340
    cols = [("GT", None)] + [(f"{m.split('-')[0]} raw x3", (m, "raw")) for m in MODELS] + [(f"{m.split('-')[0]} refined+vote", (m, "final")) for m in MODELS]
    sheet = Image.new("RGB", ((S + 6) * len(cols), (S + 6) * len(TILE_IDS)), "white")
    palette = [(255, 120, 0), (255, 220, 0), (255, 0, 200)]
    for r, t in enumerate(TILE_IDS):
        base = Image.open(os.path.join(TILES, f"{t}.png")).convert("RGB").resize((S, S), Image.LANCZOS)
        for c, (label, spec) in enumerate(cols):
            im = base.copy()
            d = ImageDraw.Draw(im)
            if spec is None:
                d.rectangle([v * S for v in GT[t]], outline=(0, 255, 0), width=3)
            else:
                m, kind = spec
                d.rectangle([v * S for v in GT[t]], outline=(0, 255, 0), width=1)
                if kind == "raw":
                    for k, rep in enumerate(REPS):
                        for b in get(m, "raw", t, rep):
                            d.rectangle([v * S for v in b], outline=palette[k], width=2)
                else:
                    fin = json.load(open(os.path.join(RET, "v4", f"v4_final_{m.split('-')[0]}.json")))["refined_vote"][t]
                    for b in fin:
                        d.rectangle([v * S for v in b], outline=(0, 200, 255), width=4)
            d.rectangle([0, 0, S, 14], fill=(0, 0, 0))
            d.text((3, 2), f"{t} {label}", fill="white")
            sheet.paste(im, (c * (S + 6), r * (S + 6)))
    sheet.save(os.path.join(RET, "v4", "v4_compare.png"))
    print("saved v4_compare.png")


if __name__ == "__main__":
    {"run": run, "report": report}[sys.argv[1]]()
