"""v3 prompt 不变，只改输入图尺寸：256(原图) / 512，对比已有的 768 (results3/tight)。
结果: results6/s<size>/<model>/r<k>/<idx>.json ；同时记录 prompt_tokens，看端点是否在服务端缩放。
"""
import base64
import io
import json
import os
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ms3 import PROMPT, parse  # noqa: E402
from mscall import BASE, HERE, KEY, RET, TILES  # noqa: E402
from score import GT, load_boxes, score_tile  # noqa: E402

MODELS = ("gpt-6-astra", "claude-fable-5-2")
TILE_IDS = sorted(GT)
REPS = (1, 2, 3)
SIZES = (256, 512, 1024)
V3_PROMPT = PROMPT.format(grid_note="")  # 与 ms3 的 tight 变体完全一致


def b64(path, size):
    im = Image.open(path).convert("RGB")
    if im.width != size:
        im = im.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def call(model, data):
    if model == "gpt-6-astra":
        p = {"model": model, "max_tokens": 24000, "reasoning_effort": "xhigh",
             "messages": [{"role": "user", "content": [
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64," + data}},
                 {"type": "text", "text": V3_PROMPT}]}]}
        r = requests.post(BASE + "/chat/completions", json=p, headers={"Authorization": "Bearer " + KEY}, timeout=900)
        if r.status_code != 200:
            return None, None, f"HTTP {r.status_code}: {r.text[:300]}"
        j = r.json()
        u = j.get("usage") or {}
        return (j["choices"][0]["message"].get("content") or "",
                {"prompt": u.get("prompt_tokens"), "completion": u.get("completion_tokens")}, None)
    p = {"model": model, "max_tokens": 32000, "thinking": {"type": "enabled", "budget_tokens": 16000},
         "messages": [{"role": "user", "content": [
             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
             {"type": "text", "text": V3_PROMPT}]}]}
    r = requests.post(BASE + "/messages", json=p,
                      headers={"x-api-key": KEY, "anthropic-version": "2023-06-01"}, timeout=900)
    if r.status_code != 200:
        return None, None, f"HTTP {r.status_code}: {r.text[:300]}"
    j = r.json()
    u = j.get("usage") or {}
    return ("".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text"),
            {"prompt": u.get("input_tokens"), "completion": u.get("output_tokens")}, None)


def job(a):
    size, model, rep, idx = a
    fp = os.path.join(RET, "v6", "results", f"s{size}", model, f"r{rep}", f"{idx}.json")
    if os.path.exists(fp):
        return a, "cached"
    t0 = time.time()
    try:
        text, usage, err = call(model, b64(os.path.join(TILES, f"{idx}.png"), size))
    except Exception as e:  # noqa: BLE001
        text, usage, err = None, None, f"EXC {e}"
    out = {"_latency_s": round(time.time() - t0, 1), "_usage": usage, "_error": err, "_raw": text}
    if text:
        try:
            out["parsed"] = parse(text)
        except Exception as e:  # noqa: BLE001
            out["_parse_error"] = str(e)
    if err is None:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        json.dump(out, open(fp, "w"), ensure_ascii=False, indent=1)
    return a, f"{out['_latency_s']}s {usage} err={err} parse={'ok' if out.get('parsed') else out.get('_parse_error', 'none')}"


def path_for(size, model, rep, t):
    if size == 768:
        return os.path.join(RET, "v3", "results", "tight", model, f"r{rep}", f"{t}.json")
    return os.path.join(RET, "v6", "results", f"s{size}", model, f"r{rep}", f"{t}.json")


def report():
    for m in MODELS:
        for size in (256, 512, 768, 1024):
            rows, ptok = [], []
            for r in REPS:
                for t in TILE_IDS:
                    fp = path_for(size, m, r, t)
                    if not os.path.exists(fp):
                        continue
                    d = json.load(open(fp))
                    rows.append((t, score_tile(load_boxes(fp) or [], GT[t])))
                    if (d.get("_usage") or {}).get("prompt"):
                        ptok.append(d["_usage"]["prompt"])
            if not rows:
                continue
            per = {t: sum(s["hit_near"] for tt, s in rows if tt == t) for t in TILE_IDS}
            print(f"{m.split('-')[0]:6s} {size:4d}px n={len(rows):2d} hit_near={sum(s['hit_near'] for _, s in rows):2d} "
                  f"hit_in={sum(s['hit_in'] for _, s in rows):2d} meanIoU={st.mean(s['iou'] for _, s in rows):.2f} "
                  f"面积={st.mean(s['area'] for _, s in rows):.3f} 框数/次={st.mean(s['n'] for _, s in rows):.1f} "
                  f"空={sum(s['n'] == 0 for _, s in rows)} 输入token={int(st.mean(ptok)) if ptok else '-'} 逐张={per}")


if __name__ == "__main__":
    if sys.argv[1] == "run":
        jobs = [(s, m, r, t) for s in SIZES for m in MODELS for r in REPS for t in TILE_IDS]
        with ThreadPoolExecutor(10) as ex:
            for a, msg in ex.map(job, jobs):
                print(*a, msg, flush=True)
    else:
        report()
