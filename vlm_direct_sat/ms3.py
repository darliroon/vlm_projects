"""v3: 依据人工标注归纳的 prompt。变体 tight(原图) / grid(叠 10x10 编号网格)。
结果: ret/v3/results/<variant>/<model>/<rep>/<idx>.json ；用 score.py 打分。
"""
import json
import os
import re
import sys
import time
import base64
import io
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mscall import BASE, HERE, KEY, RET, TILES  # noqa: E402

PROMPT = """这是荷兰高速/干线公路的正射航拍图（卫片），地面分辨率约 18.7 cm/像素，图幅约 48 m × 48 m，上北下南。{grid_note}

任务：找出【车道数发生变化的那个过渡点】。人工标注者关心的是车道线层面的细小过渡，而不是整条匝道、整块导流区或整段道路。

从人工标注样本归纳出的三种典型样式：
A. 新车道出现或消失：车行道一侧原本只有路缘/边线，下游开始出现一列新的车道分隔虚线，路面随之变宽（或反过来，虚线终止、车道并入）。虚线"开始出现"或"终止"的那一小段就是目标。
B. 斜纹（人字形/斜条）缓冲区的收窄末端：斜纹区逐渐变窄，它与行车道虚线、边线相接的末端就是目标，不要框整块斜纹区。
C. 车道渐变加宽/变窄：两条实线之间的空间沿道路方向渐变，内部出现或消失车道线。
这些过渡常常很细微，只表现为一小段虚线的起止或边线的外移。

不算：互相平行、宽度恒定的车道；桥梁跨越但不相连的道路；仅由车辆、阴影造成的错觉。

框的要求：
- 紧框住"过渡发生的那一小段"，宽和高通常在 0.1~0.35（归一化）之间，不要框住整块斜纹区或整段道路；
- 每张图最多 3 个框，宁少勿滥；没有就返回空数组；
- 坐标归一化到 0~1，(0,0) 在左上角，x 向右，y 向下。

先简要分析，最后用一个 ```json 代码块给出结果：
{{"has_event": true/false,
  "regions": [{{"bbox": [x0, y0, x1, y1], "type": "A|B|C|other", "evidence": "一句话依据"}}]}}"""

GRID_NOTE = "图上叠加了半透明黄色网格，把图分成 10×10 个格子，列 A~J 从左到右，行 1~10 从上到下，每格边长 0.1。网格只用于定位，不是路面标线。"


def make_img(path, variant, size=768):
    im = Image.open(path).convert("RGB").resize((size, size), Image.LANCZOS)
    if variant == "grid":
        d = ImageDraw.Draw(im, "RGBA")
        c = size // 10
        for k in range(1, 10):
            d.line([(k * c, 0), (k * c, size)], fill=(255, 255, 0, 90), width=1)
            d.line([(0, k * c), (size, k * c)], fill=(255, 255, 0, 90), width=1)
        for r in range(10):
            for col in range(10):
                d.text((col * c + 3, r * c + 2), f"{'ABCDEFGHIJ'[col]}{r + 1}", fill=(255, 255, 0, 200))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def call(model, b64, prompt):
    if model == "gpt-6-astra":
        p = {"model": model, "max_tokens": 24000, "reasoning_effort": "xhigh",
             "messages": [{"role": "user", "content": [
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
                 {"type": "text", "text": prompt}]}]}
        r = requests.post(BASE + "/chat/completions", json=p, headers={"Authorization": "Bearer " + KEY}, timeout=900)
        if r.status_code != 200:
            return None, None, f"HTTP {r.status_code}: {r.text[:300]}"
        j = r.json()
        u = j.get("usage") or {}
        return (j["choices"][0]["message"].get("content") or "",
                {"completion": u.get("completion_tokens"),
                 "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens")}, None)
    p = {"model": model, "max_tokens": 32000, "thinking": {"type": "enabled", "budget_tokens": 16000},
         "messages": [{"role": "user", "content": [
             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
             {"type": "text", "text": prompt}]}]}
    r = requests.post(BASE + "/messages", json=p,
                      headers={"x-api-key": KEY, "anthropic-version": "2023-06-01"}, timeout=900)
    if r.status_code != 200:
        return None, None, f"HTTP {r.status_code}: {r.text[:300]}"
    j = r.json()
    u = j.get("usage") or {}
    return ("".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text"),
            {"completion": u.get("output_tokens"),
             "reasoning": (u.get("output_tokens_details") or {}).get("thinking_tokens")}, None)


def parse(text):
    for m in reversed(re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)):
        try:
            return json.loads(m)
        except ValueError:
            pass
    m = re.search(r"\{[^{}]*\"regions\".*\}", text, re.S)
    return json.loads(m.group(0)) if m else None


def job(a):
    variant, model, rep, idx = a
    fp = os.path.join(RET, "v3", "results", variant, model, f"r{rep}", f"{idx}.json")
    if os.path.exists(fp):
        return a, "cached"
    prompt = PROMPT.format(grid_note=GRID_NOTE if variant == "grid" else "")
    t0 = time.time()
    try:
        text, usage, err = call(model, make_img(os.path.join(TILES, f"{idx}.png"), variant), prompt)
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


if __name__ == "__main__":
    idxs = ["0102", "0254", "0256", "0334", "0404"]
    jobs = [(v, m, r, i) for r in (1, 2) for v in ("tight", "grid")
            for m in ("gpt-6-astra", "claude-fable-5-2") for i in idxs]
    with ThreadPoolExecutor(10) as ex:
        for a, msg in ex.map(job, jobs):
            print(*a, msg, flush=True)
