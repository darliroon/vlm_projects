"""阶段 C：把候选点的 sat.png + vec.png 送模型，让模型给出"矢量哪里不对"的判断。

模型输出结构化结论（不是画框）：
  verdict: ok | wrong_branch | missing_lane | wrong_break | wrong_count | wrong_geometry | unreadable

每个候选独立调用，结果落 ret/<ver>/works/<model>/s<size>_<tag>/r<k>/<cid>.json，可用 --report 汇总。

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

import hashlib

import numpy as np
import requests
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import geo  # noqa: E402
import render  # noqa: E402

import paths  # noqa: E402

CROPS = paths.p("crops")          # ret/<ver>/crops，--ver 可改
WORK = paths.p("works")           # ret/<ver>/works，--ver 可改
ANNOTS = os.path.join(paths.RET, "human_annots.json")   # 人工标注跨版本共用


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


# ------------------------------------------------------------------ prompt v1
# 与 v0 只差图例这几句（卫片上不再烧入编号和引线，编号只留在纯矢量图上）；
# 目标、六类问题、判断要点、输出格式逐字不变。不往 prompt 里加车道坐标。
PROMPT_V1 = """第一张图是正射卫星影像（图上红线是我们已有的车道中心线矢量，白色小方块是每条线的端点，黄色十字是待核对的位置；这张图上没有编号）。
第二张图是同一视角的纯矢量图：红色=待核实的车道中心线，灰色=周边已有车道中心线，白色小方块=每条线的端点（也是矢量断开或相接的地方）。图幅 {size} 像素约 {meters} 米见方，{orient}。

注意图上的标注：黄色十字=待核对位置；第二张图上的黄色短虚线+黄色框+L 编号=仅仅是标签引线，**不是**车道线，不要把它当成路面结构。两张图的视角和坐标完全相同，每条红线的编号请到第二张图上查。

我们的目标：核对【红色矢量】是否与影像上的真实车道一致，重点看黄十字附近的端点和分合流处。

请逐一检查红色中心线（L 编号见第二张图），判断下列哪一类问题（可多选，若无问题就是 ok）：

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

ORIENT_ROT = "已旋转使主要道路接近竖直，上不作方向参考"
ORIENT_NORTH = "未做旋转，图的上方是正北"

# few-shot：人工在查看器里标的判例（human_annots.json），以"图 + 人工结论"的形式放在正题之前
FEWSHOT_HEAD = """下面先给出 {n} 处已经由人工核对过的判例，每处两张图（卫星影像、纯矢量图）加人工结论，图例与后面正题相同。请从这些判例里体会我们关心的是哪类问题、判断标准是什么、改法该怎么写。人工只标了他有把握的位置，判例里没提到的红线不代表没有问题。"""
FEWSHOT_ITEM = """判例 {i} 的人工结论（坐标是这处判例图上的像素）：
{rows}"""
FEWSHOT_TAIL = """判例到此结束。下面两张图是这次真正要核对的位置，请只对这两张图作答。"""

ZH = {"missing_lane": "该续未续", "wrong_break": "本该断开却连上了", "wrong_branch": "分支/合流错误",
      "wrong_count": "车道数不符", "ok": "这里没问题"}


def frame_of(meta, size):
    """这张图的坐标系：有了它，模型给的 where 才能换算回整幅卫片坐标。"""
    return dict(center_xy=meta["center_xy"], crop_px=meta["crop_px"], size=size,
                rotation_exact=meta.get("rotation_exact", meta["rotation_deg"]))


def to_local(xy_global, meta, size):
    """整幅卫片像素坐标 -> 某张裁剪图（送模型尺寸）里的像素坐标。"""
    f = frame_of(meta, size)
    p = render.rot([xy_global], f["rotation_exact"], *f["center_xy"], f["crop_px"])[0]
    return p * (size / f["crop_px"])


def _nearest_lane(p, meta, size, max_m=8.0):
    """离 p 最近的红线（距离超过 max_m 就认为不在任何红线上）。"""
    sc = size / meta["crop_px"]
    best = (None, 1e18)
    for L in meta["lanes"]:
        P = np.asarray(L["pts"], float) * sc
        for a, b in zip(P[:-1], P[1:]):
            v = b - a
            l2 = float((v * v).sum())
            t = 0.0 if not l2 else max(0.0, min(1.0, float(((p - a) * v).sum()) / l2))
            d = float(np.linalg.norm(p - (a + t * v)))
            if d < best[1]:
                best = (f"L{L['k']}", d)
    return best[0] if best[1] * geo.G / sc <= max_m else None


def load_annots():
    if not os.path.exists(ANNOTS):
        return {}
    by = {}
    for a in json.load(open(ANNOTS, encoding="utf-8")).get("rows", []):
        if a.get("xy_global"):
            by.setdefault(a["cid"], []).append(a)
    return by


def pick_fewshot(cid, n, crops, annots=None):
    """给候选 cid 挑 n 处判例：排除它自己和图幅与它重叠的；贪心覆盖尽量多的问题类型，其次标注条数多的优先。"""
    annots = load_annots() if annots is None else annots

    def center(c):
        return np.array(json.load(open(os.path.join(crops, c, "meta.json")))["center_xy"])

    # 相邻候选的图幅会互相重叠：判例如果和被测位置看的是同一段路，等于把答案给了模型。
    # 所以除了排除自己，还排除中心距离小于 1.5 倍图幅的候选（任意旋转角下都不重叠）。
    me = json.load(open(os.path.join(crops, cid, "meta.json")))
    far = 1.5 * me["crop_px"]
    pool = {c: a for c, a in annots.items()
            if c != cid and os.path.exists(os.path.join(crops, c, "meta.json"))
            and np.linalg.norm(center(c) - np.array(me["center_xy"])) >= far}
    chosen, seen = [], set()
    while pool and len(chosen) < n:
        best = max(sorted(pool), key=lambda c: (len({a["type"] for a in pool[c]} - seen),
                                                len(pool[c])))
        chosen.append(best)
        seen |= {a["type"] for a in pool.pop(best)}
    return chosen, annots


def fewshot_rows(ex_cid, annots, crops, size):
    """一处判例的人工结论文字；落在这张图幅之外的标注用不上，跳过。"""
    meta = json.load(open(os.path.join(crops, ex_cid, "meta.json")))
    rows = []
    for a in sorted(annots[ex_cid], key=lambda a: a.get("seq", 0)):
        p = to_local(a["xy_global"], meta, size)
        if not (0 <= p[0] <= size and 0 <= p[1] <= size):
            continue
        lane = _nearest_lane(p, meta, size)
        r = (f"- 位置 ({int(round(p[0]))},{int(round(p[1]))})，"
             f"{'最近的红线 ' + lane if lane else '不在任何红线上'}："
             f"{a['type']}（{ZH.get(a['type'], a['type'])}）。")
        if a.get("fix"):
            r += f"改法：{a['fix'].strip()}"
        if a.get("note"):
            r += f"（说明：{a['note'].strip()}）"
        rows.append(r)
    return rows


def b64(im, size):
    im = im.convert("RGB")
    if im.width != size:
        im = im.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def prompt_for(meta, version="v0", size=None):
    size = size or meta["crop_px"]
    if version == "v0":
        # v0 原样保留（包括它把 {size} 填成裁剪像素数的做法），保证基线可复现
        return PROMPT.format(size=meta["crop_px"], meters=meta["ground_m"])
    return PROMPT_V1.format(size=size, meters=meta["ground_m"],
                            orient=ORIENT_ROT if meta.get("rotated", True) else ORIENT_NORTH)


def build_content(cid, crops, size, version, n_fewshot=0, annots=None):
    """组装一次调用的内容块：[("image", b64) | ("text", str), ...]，以及这次调用的记录信息。"""
    d = os.path.join(crops, cid)
    meta = json.load(open(os.path.join(d, "meta.json")))
    blocks, ex_used = [], []
    if n_fewshot:
        chosen, annots = pick_fewshot(cid, n_fewshot, crops, annots)
        items = []
        for ex in chosen:
            rows = fewshot_rows(ex, annots, crops, size)
            if rows:
                items.append((ex, rows))
        if items:
            blocks.append(("text", FEWSHOT_HEAD.format(n=len(items))))
            for i, (ex, rows) in enumerate(items, 1):
                for fn in ("sat.png", "vec.png"):
                    blocks.append(("image", b64(Image.open(os.path.join(crops, ex, fn)), size)))
                blocks.append(("text", FEWSHOT_ITEM.format(i=i, rows="\n".join(rows))))
                ex_used.append(ex)
            blocks.append(("text", FEWSHOT_TAIL))
    for fn in ("sat.png", "vec.png"):
        blocks.append(("image", b64(Image.open(os.path.join(d, fn)), size)))
    blocks.append(("text", prompt_for(meta, version, size)))
    info = dict(prompt_version=version, crops=os.path.basename(crops), fewshot_cids=ex_used,
                frame=frame_of(meta, size),
                input_sha256=hashlib.sha256(json.dumps(blocks).encode()).hexdigest())
    return blocks, info


def call(model, blocks, timeout=900):
    if model.startswith("claude"):
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                "data": v}} if t == "image"
                   else {"type": "text", "text": v} for t, v in blocks]
        # 这个模型的思考是常开的，只接受 adaptive；v0 当时传的
        # {"type": "enabled", "budget_tokens": 8000} 现在会被网关以 400 拒绝。
        p = {"model": model, "max_tokens": 16000,
             "thinking": {"type": "adaptive"},
             "messages": [{"role": "user", "content": content}]}
        r = requests.post(BASE + "/messages", json=p,
                          headers={"x-api-key": KEY, "anthropic-version": "2023-06-01"},
                          timeout=timeout)
        if r.status_code != 200:
            return None, None, f"HTTP {r.status_code}: {r.text[:300]}"
        j = r.json()
        txt = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        if j.get("stop_reason") not in (None, "end_turn"):
            # max_tokens（输出被截断）/ refusal 等：不当成功结果落盘
            return txt, j.get("usage"), f"stop_reason={j.get('stop_reason')}"
        return txt, j.get("usage"), None
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + v}}
               if t == "image" else {"type": "text", "text": v} for t, v in blocks]
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


def run_dir(model, size, tag=None):
    """结果目录：works/<model>/s<size>[_<tag>]/ 。不带 tag 的就是 v0。"""
    return os.path.join(WORK, model, f"s{size}" + (f"_{tag}" if tag else ""))


def job(a):
    cid, model, rep, size, cfg = a
    a = (cid, model, rep, size)
    fp = os.path.join(run_dir(model, size, cfg["tag"]), f"r{rep}", f"{cid}.json")
    if os.path.exists(fp):
        return a, "cached"
    if not os.path.exists(os.path.join(cfg["crops"], cid, "meta.json")):
        return a, "no meta"
    blocks, info = build_content(cid, cfg["crops"], size, cfg["prompt"], cfg["fewshot"])
    if cfg["dry"]:
        # 不发请求，只把"模型将会收到的东西"落盘供人检查（图片用哈希代替）
        dp = os.path.join(WORK, "_dryrun", os.path.basename(run_dir(model, size, cfg["tag"])),
                          f"{cid}.json")
        os.makedirs(os.path.dirname(dp), exist_ok=True)
        json.dump(dict(cid=cid, model=model, **info,
                       content=[dict(type=t, text=v) if t == "text" else
                                dict(type=t, png_b64_len=len(v),
                                     sha256=hashlib.sha256(v.encode()).hexdigest()[:16])
                                for t, v in blocks]),
                  open(dp, "w"), ensure_ascii=False, indent=1)
        n_img = sum(1 for t, _ in blocks if t == "image")
        n_ch = sum(len(v) for t, v in blocks if t == "text")
        return a, f"dry-run: {n_img} 张图, 文字 {n_ch} 字符, 判例 {info['fewshot_cids']}"
    t0 = time.time()
    txt, usage, err = None, None, None
    for attempt in range(5):
        try:
            txt, usage, err = call(model, blocks)
        except Exception as e:  # noqa: BLE001
            txt, usage, err = None, None, f"EXC {e}"
        # 503 / 429 / 超时都是临时性错误，退避重试
        if err is None:
            break
        if not re.search(r"HTTP (429|5\d\d)|EXC .*(Timeout|Connection|reset)", err):
            break
        time.sleep(20 * (attempt + 1))
        t0 = time.time()
    out = {"cid": cid, "model": model, "size": size, "rep": rep, "tag": cfg["tag"], **info,
           "latency_s": round(time.time() - t0, 1), "usage": usage, "error": err, "raw": txt}
    if txt:
        out["parsed"] = parse(txt)
    if err is None:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        json.dump(out, open(fp, "w"), ensure_ascii=False, indent=1)
    v = (out.get("parsed") or {}).get("verdicts")
    summ = ",".join(f"{x.get('lane')}:{x.get('problem')}" for x in v) if v else out.get("error")
    return a, f"{out['latency_s']}s {summ}"


def run(model, size, reps, only=None, workers=6, tag=None, crops=None, prompt="v0",
        fewshot=0, dry=False):
    C = json.load(open(os.path.join(HERE, "candidates.json")))["cands"]
    cids = [c["cid"] for c in C]
    if only:
        cids = [c for c in cids if c in set(only)]
    cfg = dict(tag=tag, crops=(paths.p(crops) if crops else CROPS), prompt=prompt,
               fewshot=fewshot, dry=dry)
    if size is None and cids:
        # 默认按裁剪图的原生尺寸送给模型，不重采样
        size = json.load(open(os.path.join(cfg["crops"], cids[0], "meta.json")))["crop_px"]
    if prompt == "v0" and fewshot:
        sys.exit("few-shot 判例里的车道名要靠 v1 的图例来指认，请配合 --prompt v1 使用")
    jobs = [(c, model, r, size, cfg) for r in range(1, reps + 1) for c in cids]
    with ThreadPoolExecutor(workers) as ex:
        for a, msg in ex.map(job, jobs):
            print(*a, msg, flush=True)


def report(models=None, size=None):
    import collections
    rows = []
    for root, _, files in os.walk(WORK):
        if "_dryrun" in os.path.relpath(root, WORK).split(os.sep):
            continue
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
    ap.add_argument("--size", type=int,
                    help="送模型前把图缩放到多少像素；不给就按裁剪图原生尺寸送（不重采样）")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--only")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--tag", help="结果目录后缀：works/<model>/s<size>_<tag>/ ；不给就是 v0 的目录")
    ap.add_argument("--crops", help="读哪个裁剪图目录（相对 ret/<ver>/），默认 crops")
    ap.add_argument("--ver", help="产物版本目录 ret/<ver>/，默认 v1（也可设环境变量 VLM_RET_VERSION）")
    ap.add_argument("--prompt", choices=["v0", "v1"], default="v0")
    ap.add_argument("--fewshot", type=int, default=0,
                    help="每次调用附带几处人工判例（来自 human_annots.json，自动排除被测候选本身）")
    ap.add_argument("--dry-run", action="store_true",
                    help="不调用模型，只把将要发送的内容写到 works/_dryrun/ 供检查")
    a = ap.parse_args()
    if a.ver:
        paths.use(a.ver)
        CROPS, WORK = paths.p("crops"), paths.p("works")
    if a.cmd == "run":
        run(a.model, a.size, a.reps, a.only.split(",") if a.only else None, a.workers,
            tag=a.tag, crops=a.crops, prompt=a.prompt, fewshot=a.fewshot, dry=a.dry_run)
    else:
        report()
