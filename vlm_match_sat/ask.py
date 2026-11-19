"""阶段 C：把候选点的 sat.png + vec.png 送模型，让模型给出"矢量哪里不对"的判断。

模型输出结构化结论（不是画框）：
  verdict: ok | wrong_branch | missing_lane | wrong_break | wrong_count | wrong_geometry | unreadable

每个候选独立调用，结果落 works/<cid>.json，可用 --report 汇总。

用法
----
python ask.py run --model claude-fable-5-2 --size 1024
python ask.py run --model gpt-6-astra   --size 1024 --reps 3
python ask.py report
"""
import argparse
import base64
import io
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
import paths  # noqa: E402

CROPS = paths.p("crops")
WORK = paths.p("works")


def load_env(path=os.path.join(HERE, "..", ".env")):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()
BASE = ENV["OPENAI_BASE_URL"].rstrip("/")
KEY = ENV["OPENAI_API_KEY"]

# ------------------------------------------------------------------ prompt
PROMPT = """第一张图是正射卫星影像（图上红线是我们已有的车道中心线矢量，黄色十字是待核对的位置）。
第二张图是同一视角的纯矢量图：红色=待核实的车道中心线，灰色=周边已有车道中心线，白色小方块=每条线的端点（也是矢量断开或相接的地方）。图幅 {size} 像素约 {meters} 米见方，已旋转使主要道路接近竖直，上不作方向参考。

注意图上的标注：黄色十字=待核对位置；黄色短虚线+黄色框+L 编号=仅仅是标签引线，**不是**车道线，不要把它当成路面结构。

我们的目标：核对【红色矢量】是否与影像上的真实车道一致，重点看黄十字附近的端点和分合流处。

请逐一检查红色中心线（L 编号标在线上），判断下列哪一类问题（可多选，若无问题就是 ok）：

A. missing_lane（该续未续）：影像上这条车道明明继续延伸，矢量却在此断开了。
B. wrong_break（本该断开却连上了）：影像上这里车道终止/车道线不存在（例如撞上导流区、路肩尽头、被中央分隔带/护栏截断），矢量却在这里结尾或拐进了不该进的地方。
C. wrong_branch（分支/合流错误）：影像上的分/合流形态与矢量不符——例如应有分叉却没有、不该分叉却分叉了、合流方向接反、有车道在分合流处"穿越"了导流区。
D. wrong_count（车道数不符）：穿过某个断面的车道数与影像上的行车车道数不一致（多一条或少一条）。
E. ok（就这一段没问题）：端点/连接是合理的，影像与矢量一致。
F. unreadable：影像在此处看不清（云、阴影、无影像黑块、车辆遮挡）。

判断要点：
- 看路面本身：车道分隔线（虚线/实线）、导流区人字形斜纹、车道线起止的尖端 (nose)、实线变虚线的位置。
- 端点落在导流区里、压着斜纹区、或跨过实体分隔带 → 偏向 B。
- 端点附近影像上车行道仍在延续、只是我们的线停了 → 偏向 A。
- 一条线在分合流处穿过斜纹区/直接横切 → 偏向 C。
- 不要因为"两条线之间有空隙"就判 A：同向车道本来就是并行的，空隙是正常的车道间距。

请只输出一个 JSON，不要其他文字：
{{
 "verdicts": [
   {{"lane": "L12",
     "problem": "missing_lane|wrong_break|wrong_branch|wrong_count|ok|unreadable",
     "confidence": 0.0~1.0,
     "where": [x, y],          // 问题发生的像素位置（本图坐标系，原点左上）
     "evidence": "一句话看什么判断的（例如：斜纹区尖端在线上方 3 车道处，线穿过了斜纹）",
     "fix": "一句话该怎么改（例如：向南延长约 40 m 到车道线消失处）"}}
 ],
 "overall": "一句话总结这一处的主要问题",
 "severity": "high|medium|low|none"
}}"""


def b64(im, size):
    im = im.convert("RGB")
    if im.width != size:
        im = im.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def prompt_for(meta):
    return PROMPT.format(size=meta["crop_px"], meters=meta["ground_m"])


def call(model, imgs, prompt, timeout=900):
    if model.startswith("claude"):
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                "data": imgs[0]}}]
        if len(imgs) > 1:
            content.append({"type": "image", "source": {"type": "base64",
                                                        "media_type": "image/png",
                                                        "data": imgs[1]}})
        content.append({"type": "text", "text": prompt})
        p = {"model": model, "max_tokens": 16000,
             "thinking": {"type": "enabled", "budget_tokens": 8000},
             "messages": [{"role": "user", "content": content}]}
        r = requests.post(BASE + "/messages", json=p,
                          headers={"x-api-key": KEY, "anthropic-version": "2023-06-01"},
                          timeout=timeout)
        if r.status_code != 200:
            return None, None, f"HTTP {r.status_code}: {r.text[:300]}"
        j = r.json()
        txt = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        return txt, j.get("usage"), None
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + imgs[0]}}]
    if len(imgs) > 1:
        content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + imgs[1]}})
    content.append({"type": "text", "text": prompt})
    p = {"model": model, "max_tokens": 16000, "reasoning_effort": "high",
         "messages": [{"role": "user", "content": content}]}
    r = requests.post(BASE + "/chat/completions", json=p,
                      headers={"Authorization": "Bearer " + KEY}, timeout=timeout)
    if r.status_code != 200:
        return None, None, f"HTTP {r.status_code}: {r.text[:300]}"
    j = r.json()
    return j["choices"][0]["message"].get("content") or "", j.get("usage"), None


def parse(text):
    if not text:
        return None
    for m in reversed(re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)):
        try:
            return json.loads(m)
        except ValueError:
            pass
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except ValueError:
            return None
    return None


def job(a):
    cid, model, rep, size = a
    fp = os.path.join(WORK, model, f"s{size}", f"r{rep}", f"{cid}.json")
    if os.path.exists(fp):
        return a, "cached"
    d = os.path.join(CROPS, cid)
    mf = os.path.join(d, "meta.json")
    if not os.path.exists(mf):
        return a, "no meta"
    meta = json.load(open(mf))
    imgs = [b64(Image.open(os.path.join(d, "sat.png")), size),
            b64(Image.open(os.path.join(d, "vec.png")), size)]
    t0 = time.time()
    txt, usage, err = None, None, None
    for attempt in range(5):
        try:
            txt, usage, err = call(model, imgs, prompt_for(meta))
        except Exception as e:  # noqa: BLE001
            txt, usage, err = None, None, f"EXC {e}"
        # 503 / 429 / 超时都是临时性错误，退避重试
        if err is None:
            break
        if not re.search(r"HTTP (429|5\d\d)|EXC .*(Timeout|Connection|reset)", err):
            break
        time.sleep(20 * (attempt + 1))
        t0 = time.time()
    out = {"cid": cid, "model": model, "size": size, "rep": rep,
           "latency_s": round(time.time() - t0, 1), "usage": usage, "error": err, "raw": txt}
    if txt:
        out["parsed"] = parse(txt)
    if err is None:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        json.dump(out, open(fp, "w"), ensure_ascii=False, indent=1)
    v = (out.get("parsed") or {}).get("verdicts")
    summ = ",".join(f"{x.get('lane')}:{x.get('problem')}" for x in v) if v else out.get("error")
    return a, f"{out['latency_s']}s {summ}"


def run(model, size, reps, only=None, workers=6):
    C = json.load(open(os.path.join(HERE, "candidates.json")))["cands"]
    cids = [c["cid"] for c in C]
    if only:
        cids = [c for c in cids if c in set(only)]
    jobs = [(c, model, r, size) for r in range(1, reps + 1) for c in cids]
    with ThreadPoolExecutor(workers) as ex:
        for a, msg in ex.map(job, jobs):
            print(*a, msg, flush=True)


def report(models=None, size=None):
    import collections
    rows = []
    for root, _, files in os.walk(WORK):
        for f in files:
            if not f.endswith(".json"):
                continue
            d = json.load(open(os.path.join(root, f), encoding="utf-8"))
            if models and d["model"] not in models:
                continue
            if size and d["size"] != size:
                continue
            rows.append(d)
    print("records", len(rows))
    k = collections.Counter((r["model"], r["size"]) for r in rows)
    print(k)
    # verdict 分布（按 lane 计）
    cnt = collections.Counter()
    for r in rows:
        for v in ((r.get("parsed") or {}).get("verdicts") or []):
            cnt[(r["model"], v.get("problem"))] += 1
    for key in sorted(cnt):
        print(f"  {key[0]:22s} {str(key[1]):14s} {cnt[key]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "report"])
    ap.add_argument("--model", default="claude-fable-5-2")
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--only")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--ver", help="产物版本目录 ret/<ver>/，默认 v0")
    a = ap.parse_args()
    if a.ver:
        paths.use(a.ver)
    if a.cmd == "run":
        run(a.model, a.size, a.reps, a.only.split(",") if a.only else None, a.workers)
    else:
        report()
