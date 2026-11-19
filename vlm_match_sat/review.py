"""阶段 D：人工核对台 + 把模型的"单点判断"回填到矢量上。

两个用途
--------
1. review.py sheet   —— 生成联系表（每个候选一张缩略图，标注模型结论），
   供人快速标"对/错"。这是唯一可靠的评测方式：没有真值前，模型结论只能靠人抽查。
2. review.py apply   —— 把 verdict 里的 fix 落成 lana 级建议，输出 proposals.json，
   每条形如 {lane, action: extend|trim|delete|relink|recount, at: [x,y], note}

用法
----
python review.py sheet --model claude-fable-5-2
python review.py apply --model claude-fable-5-2 --min-conf 0.5
python review.py stats
"""
import argparse
import glob
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
import paths  # noqa: E402

CROPS = paths.p("crops")
WORK = paths.p("works")


def _font(sz):
    for p in ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz)
            except OSError:
                continue
    return ImageFont.load_default()


def load_results(model, size, rep=1, size_dir=None):
    """size_dir 形如 's1024'；不给时按 f's{size}' 拼"""
    sd = size_dir or f"s{size}"
    out = {}
    for fp in glob.glob(os.path.join(WORK, model, sd, f"r{rep}", "*.json")):
        d = json.load(open(fp, encoding="utf-8"))
        out[d["cid"]] = d
    return out


COLORS = {"ok": (60, 200, 90), "missing_lane": (255, 60, 60),
          "wrong_break": (255, 160, 40), "wrong_branch": (200, 60, 255),
          "wrong_count": (60, 160, 255), "unreadable": (140, 140, 140)}


def sheet(model, size, rep=1, cols=4, cell=520):
    C = json.load(open(os.path.join(HERE, "candidates.json")))["cands"]
    R = load_results(model, size, rep)
    rows = (len(C) + cols - 1) // cols
    pad, hdr = 8, 92
    Wt, Ht = cols * (cell + pad) + pad, rows * (cell + hdr + pad) + pad
    sheet_im = Image.new("RGB", (Wt, Ht), (25, 25, 28))
    dr = ImageDraw.Draw(sheet_im)
    f1, f2, f3 = _font(20), _font(16), _font(15)
    for i, c in enumerate(C):
        r, col = divmod(i, cols)
        x, y = pad + col * (cell + pad), pad + r * (cell + hdr + pad)
        fp = os.path.join(CROPS, c["cid"], "sat.png")
        if not os.path.exists(fp):
            continue
        im = Image.open(fp).convert("RGB").resize((cell, cell), Image.LANCZOS)
        # 把模型说的 where 点画上去
        res = R.get(c["cid"]) or {}
        parsed = res.get("parsed") or {}
        m = json.load(open(os.path.join(CROPS, c["cid"], "meta.json")))
        s = cell / m["crop_px"]
        d2 = ImageDraw.Draw(im, "RGBA")
        nl = 0
        for v in parsed.get("verdicts", []):
            prob = v.get("problem", "?")
            if prob == "ok":
                continue
            nl += 1
            w = v.get("where")
            cc = COLORS.get(prob, (255, 255, 255))
            if isinstance(w, list) and len(w) == 2:
                px, py = w[0] * s, w[1] * s
                rr = 16
                d2.ellipse([px - rr, py - rr, px + rr, py + rr], outline=cc + (255,), width=3)
                d2.line([px - rr - 8, py, px + rr + 8, py], fill=cc + (200,), width=2)
                d2.line([px, py - rr - 8, px, py + rr + 8], fill=cc + (200,), width=2)
                d2.text((px + rr + 4, py - 22), f"{v.get('lane')}", font=f3, fill=cc + (255,))
        sheet_im.paste(im, (x, y))
        sev = parsed.get("severity", "-")
        dr.text((x + 4, y + cell + 4), f"{c['cid']}  [{c['kind']}]", font=f1, fill=(255, 255, 255))
        dr.text((x + 4, y + cell + 30), f"sev={sev}  problems={nl}", font=f2,
                fill=(255, 210, 80) if nl else (120, 200, 120))
        ov = (parsed.get("overall") or res.get("error") or "-")
        # 按格宽折行，最多 3 行，避免溢到相邻格子
        max_w = cell - 8
        line, lines = "", []
        for ch in ov:
            if dr.textlength(line + ch, font=f3) > max_w:
                lines.append(line)
                line = ch
                if len(lines) == 3:
                    break
            else:
                line += ch
        if line and len(lines) < 3:
            lines.append(line)
        if len(lines) == 3 and dr.textlength(ov, font=f3) > max_w * 3:
            lines[-1] = lines[-1][:-1] + "…"
        for k, ln in enumerate(lines):
            dr.text((x + 4, y + cell + 50 + k * 18), ln, font=f3, fill=(190, 190, 190))
    out = paths.p(f"sheet_{model}_s{size}_r{rep}.png")
    sheet_im.save(out)
    print("wrote", out, sheet_im.size)


ACTION = {"missing_lane": "extend", "wrong_break": "trim_or_relink",
          "wrong_branch": "relink", "wrong_count": "recount"}


def apply(model, size, rep=1, min_conf=0.5):
    """把 verdict 变成 lane 级建议（不改数据，只出 proposals）"""
    C = {c["cid"]: c for c in json.load(open(os.path.join(HERE, "candidates.json")))["cands"]}
    R = load_results(model, size, rep)
    props = []
    for cid, res in sorted(R.items()):
        p = res.get("parsed") or {}
        meta = json.load(open(os.path.join(CROPS, cid, "meta.json")))
        id_by_k = {f"L{L['k']}": L for L in meta["lanes"]}
        for v in p.get("verdicts", []):
            lane_key = v.get("lane")
            prob = v.get("problem")
            conf = float(v.get("confidence") or 0)
            if prob in (None, "ok", "unreadable") or conf < min_conf:
                continue
            L = id_by_k.get(lane_key)
            props.append(dict(
                cid=cid, lane_key=lane_key, lane_id=(L or {}).get("id"),
                road=(L or {}).get("road"),
                action=ACTION.get(prob, prob), problem=prob, confidence=conf,
                where_local=v.get("where"), evidence=v.get("evidence"), fix=v.get("fix"),
                severity=p.get("severity"),
            ))
    out = paths.p(f"proposals_{model}_s{size}_r{rep}.json")
    json.dump(props, open(out, "w"), ensure_ascii=False, indent=1)
    print("wrote", out, len(props))
    # 按 action 统计
    import collections
    print(collections.Counter(p["action"] for p in props))
    print("涉及不同 Lane:", len({p["lane_id"] for p in props if p["lane_id"]}))
    return props


def cross_check(model, size, rep=1, min_conf=0.4):
    """跨候选汇总：同一条 Lane 被多个候选重复指出 → 信号远比模型自述的 confidence 可靠。

    一个 Lane 若在 2 个以上相邻候选里都被判"有问题"，几乎不可能是模型的偶然误判。
    输出 consensus.json，按"被指出的次数"排序。
    """
    C = {c["cid"]: c for c in json.load(open(os.path.join(HERE, "candidates.json")))["cands"]}
    R = load_results(model, size, rep)
    agg = {}
    for cid, res in R.items():
        p = res.get("parsed") or {}
        meta = json.load(open(os.path.join(CROPS, cid, "meta.json")))
        id_by_k = {f"L{L['k']}": L for L in meta["lanes"]}
        for v in p.get("verdicts", []):
            prob = v.get("problem")
            if prob in (None, "ok", "unreadable"):
                continue
            if float(v.get("confidence") or 0) < min_conf:
                continue
            L = id_by_k.get(v.get("lane"))
            if not L:
                continue
            a = agg.setdefault(L["id"], dict(lane_id=L["id"], road=L["road"], n=0,
                                             kinds=set(), cids=[], evids=[], fixes=[]))
            a["n"] += 1
            a["kinds"].add(prob)
            a["cids"].append(cid)
            a["evids"].append(v.get("evidence"))
            a["fixes"].append(v.get("fix"))
    out = []
    for a in agg.values():
        a["kinds"] = sorted(a["kinds"])
        a["road_xy"] = None
        out.append(a)
    out.sort(key=lambda a: (-a["n"], a["lane_id"]))
    fp = paths.p(f"consensus_{model}_s{size}_r{rep}.json")
    json.dump(out, open(fp, "w"), ensure_ascii=False, indent=1)
    print(f"wrote {fp}  lanes={len(out)}")
    mult = [a for a in out if a["n"] >= 2]
    print(f"被 >=2 个候选指出: {len(mult)} 条 lane")
    for a in mult[:15]:
        print(f"  lane {a['lane_id']} road {a['road']} x{a['n']} {a['kinds']}")
    return out


def stats():
    import collections
    for model in sorted(os.listdir(WORK)):
        md = os.path.join(WORK, model)
        if not os.path.isdir(md):
            continue
        for size in sorted(os.listdir(md)):
            for rep in sorted(os.listdir(os.path.join(md, size))):
                # size 已经是目录名（如 "s1024"），直接当 size_dir 用，
                # 不能再拼一次 "s"，否则会去找 ss1024
                R = load_results(model, None, int(rep.lstrip("r")), size_dir=size)
                if not R:
                    continue
                cnt = collections.Counter()
                sev = collections.Counter()
                nlanes = 0
                for d in R.values():
                    p = d.get("parsed") or {}
                    sev[p.get("severity", "ERR" if d.get("error") else "-")] += 1
                    for v in p.get("verdicts", []):
                        cnt[v.get("problem")] += 1
                        nlanes += 1
                errs = sum(1 for d in R.values() if d.get("error"))
                print(f"{model:20s} {size:6s} {rep:4s} crops={len(R):3d} lanes={nlanes:4d} "
                      f"err={errs} sev={dict(sev)} verdicts={dict(cnt)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sheet", "apply", "stats"])
    ap.add_argument("--model", default="claude-fable-5-2")
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--min-conf", type=float, default=0.5)
    ap.add_argument("--ver", help="产物版本目录 ret/<ver>/，默认 v0")
    a = ap.parse_args()
    if a.ver:
        paths.use(a.ver)
    if a.cmd == "sheet":
        sheet(a.model, a.size, a.rep)
    elif a.cmd == "apply":
        apply(a.model, a.size, a.rep, a.min_conf)
    else:
        stats()
