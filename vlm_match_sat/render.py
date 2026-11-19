"""阶段 B：把矢量渲染到卫片上，按候选点生成"待核实小图"。

设计要点
--------
1. 不盲切瓦片：只切候选点附近（40 个 vs 全图 400+ 瓦片），省调用、每张都是"疑似有问题处"。
2. 原生分辨率裁剪（默认 1024 px ≈ 138 m），不重采样，车道标线清晰。
3. 裁剪沿候选处车道走向旋转，道路在图中接近竖直，模型读拓扑更容易。
4. 相关 Lane 高亮并编号 L0..Ln，其余 Lane 淡化为灰色细线（提供上下文但不干扰）。
5. 同时输出纯矢量图 vec.png（黑底），让模型分离"我看到的路面"与"你提的矢量"。

输出 crops/<cid>/sat.png vec.png meta.json
"""
import argparse
import json
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geo  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
import paths  # noqa: E402

OUT = paths.p("crops")
CROP = 1024
LINE_PX = 5          # 高亮线宽（原生像素）≈ 0.68 m
FADE_PX = 3          # 上下文线宽
COL_HI = (255, 45, 45)
COL_CTX = (150, 150, 160)
COL_CAND = (255, 235, 0)


def _font(sz):
    # 需要 CJK 字形（编号里只有 ASCII，但基图/联系表用得上）
    for p in ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz)
            except OSError:
                continue
    return ImageFont.load_default()


FONT = _font(26)


def rot(pts, ang, cx, cy, S):
    a = math.radians(-ang)
    ca, sa = math.cos(a), math.sin(a)
    d = np.asarray(pts, float) - np.array([cx, cy])
    return np.stack([d[:, 0] * ca - d[:, 1] * sa + S / 2,
                     d[:, 0] * sa + d[:, 1] * ca + S / 2], 1)


def unrot(pts, ang, cx, cy, S):
    """rot() 的逆变换：裁剪图坐标 -> 整幅卫片像素坐标。"""
    a = math.radians(-ang)
    ca, sa = math.cos(a), math.sin(a)
    u = np.asarray(pts, float) - S / 2
    return np.stack([u[:, 0] * ca + u[:, 1] * sa + cx,
                     -u[:, 0] * sa + u[:, 1] * ca + cy], 1)


def heading_at(cx, cy, lanes, near_px=40):
    vs = []
    for L in lanes:
        P = L["xy"]
        d = np.linalg.norm(P - np.array([cx, cy]), axis=1)
        if d.min() > near_px:
            continue
        w = 0 if d[0] < d[-1] else -1
        vs.append(geo.heading(P, at_end=(w != 0), span_px=30.0))
    if not vs:
        return 0.0
    v = np.sum(vs, axis=0)
    n = np.linalg.norm(v)
    if n < 1e-6:
        return 0.0
    v /= n
    return math.degrees(math.atan2(v[0], -v[1]))


def draw(d, P, col, width, alpha=255, boxes=True):
    d.line([tuple(p) for p in P], fill=col + (alpha,), width=width, joint="curve")
    if boxes:
        for p in (P[0], P[-1]):
            d.rectangle([p[0] - 5, p[1] - 5, p[0] + 5, p[1] + 5],
                        outline=(255, 255, 255, min(255, alpha + 40)), width=2)


def dash_line(d, p, q, fill, width, dash=9, gap=7):
    """短虚线：画在画布上的标注引线，不会被误认成车道线"""
    p, q = np.asarray(p, float), np.asarray(q, float)
    v = q - p
    L = np.linalg.norm(v)
    if L < 1e-6:
        return
    u = v / L
    t = 0.0
    while t < L:
        t2 = min(t + dash, L)
        d.line([tuple(p + u * t), tuple(p + u * t2)], fill=fill, width=width)
        t = t2 + gap


def layout_labels(info, S, pad=8, gap=34, key="anchor"):
    """把每个高亮 Lane 的标签排到左/右两侧空白栏，纵向避让。

    返回 [(item, (lx, ly, side))]。side='left' 时 lx 是文字块左边界，
    'right' 时 lx 是右边界。
    """
    if not info:
        return []
    mid = S / 2
    rows = []
    for it in info:
        ax, ay = it[key]
        rows.append((it, ax, ay))
    res = []
    for side in ("left", "right"):
        sub = [r for r in rows if (r[1] < mid) == (side == "left")]
        sub.sort(key=lambda r: r[2])
        last = -1e9
        for it, ax, ay in sub:
            ly = max(ay - 14, last + gap)
            ly = min(ly, S - 30)
            # 同一侧若被顶到底部，压缩间距
            last = ly
            res.append((it, (pad if side == "left" else S - pad, ly, side)))
    return res


LABEL_MODES = ("both", "vec", "none")


def render(im, c, lanes, S=CROP, labels="both", rotate=True):
    """生成一张候选图。

    labels 控制编号/引线画在哪：
      "both"  卫片和纯矢量图都画（v0 的做法，标注会遮挡卫片）
      "vec"   只画在纯矢量图上，卫片上只有线、端点、十字（v1 默认）
      "none"  两张图都不画（模型就没有编号可用了，只用于实验）
    rotate=False 时保持原始正北朝向，不做旋转（用于 A/B 检验"转正"是否有收益）。
    """
    assert labels in LABEL_MODES, labels
    cx, cy = c["x"], c["y"]
    ang = heading_at(cx, cy, lanes) if rotate else 0.0
    half = int(S * 0.75) + 8
    x0, y0 = int(cx - half), int(cy - half)
    tile = Image.new("RGB", (2 * half, 2 * half), (0, 0, 0))
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(geo.W, x0 + 2 * half), min(geo.H, y0 + 2 * half)
    tile.paste(im.crop((sx0, sy0, sx1, sy1)), (sx0 - x0, sy0 - y0))
    ccx, ccy = cx - x0, cy - y0
    tile = tile.rotate(ang, resample=Image.BICUBIC, center=(ccx, ccy))
    l0, t0 = ccx - S / 2, ccy - S / 2
    sat = tile.crop((int(round(l0)), int(round(t0)),
                     int(round(l0)) + S, int(round(t0)) + S)).convert("RGB")
    vec = Image.new("RGB", (S, S), (10, 12, 16))
    ds, dv = ImageDraw.Draw(sat, "RGBA"), ImageDraw.Draw(vec, "RGBA")

    hi = set(c["lanes"])
    # 先画上下文，再画高亮（保证高亮在上层）
    order = [k for k, L in enumerate(lanes) if L["id"] not in hi] + \
            [k for k, L in enumerate(lanes) if L["id"] in hi]
    info = []
    for k in order:
        L = lanes[k]
        P = rot(L["xy"], ang, cx, cy, S)
        if not ((P[:, 0] > -60) & (P[:, 0] < S + 60) & (P[:, 1] > -60) & (P[:, 1] < S + 60)).any():
            continue
        is_hi = L["id"] in hi
        col, w, a = (COL_HI, LINE_PX, 255) if is_hi else (COL_CTX, FADE_PX, 130)
        draw(ds, P, col, w + 4 if is_hi else w, int(a * 0.45))
        draw(dv, P, col, w, a)
        if is_hi:
            info.append(dict(k=k, id=L["id"], road=L["road"],
                             len_m=round(geo.polyline_len(L["xy"]) * geo.G, 1),
                             anchor=[round(float(P[len(P) // 2][0]), 1),
                                     round(float(P[len(P) // 2][1]), 1)],
                             # 完整折线（裁剪图坐标）：算标签锚点、给人工判例找最近红线时要用
                             pts=[[round(float(x), 1), round(float(y), 1)] for x, y in P]))

    # v0 的引线指向"中间顶点"，两点直线时就是末端点，还可能落在图外（标签画到画布外）。
    # v1 起改为指向"图内可见部分"的中点；整条线都在图外的不标。
    # labels="both" 保持 v0 行为不变，保证 v0 的图可以逐像素复现。
    if labels != "both":
        for it in info:
            vr = visible_run(it["pts"], S)
            it["visible"] = vr is not None
            if vr is not None:
                mid = _resample(vr[0], 3)[1]
                it["label_anchor"] = [round(float(mid[0]), 1), round(float(mid[1]), 1)]

    if labels != "none":
        # ---- 标签放在图幅左右两侧的空白区，用引线指向车道，避免并排车道标签粘连
        # 引线必须与车道线区分开：用黄色短虚线（十字准星同色 = 纯标注）
        targets = (ds, dv) if labels == "both" else (dv,)
        todo = info if labels == "both" else [it for it in info if it["visible"]]
        placed = layout_labels(todo, S, key="anchor" if labels == "both" else "label_anchor")
        for item, (lx, ly, side) in placed:
            txt = f"L{item['k']}"
            ax, ay = item["anchor" if labels == "both" else "label_anchor"]
            for d in targets:
                bb = d.textbbox((0, 0), txt, font=FONT)
                w = bb[2] - bb[0] + 12
                hgt = bb[3] - bb[1] + 8
                bx = lx if side == "left" else lx - w
                ex = bx + (w if side == "left" else 0)
                ey = ly + hgt / 2
                dash_line(d, (ax, ay), (ex, ey), COL_CAND + (200,), 2)
                d.rectangle([bx, ly, bx + w, ly + hgt], fill=(0, 0, 0, 205),
                            outline=COL_CAND + (200,))
                d.text((bx + 6, ly + 2), txt, font=FONT, fill=COL_HI + (255,))
            item["label_pos"] = [round(lx, 1), round(ly, 1), side]

    for d in (ds, dv):
        R = 24
        d.line([(S / 2 - R, S / 2), (S / 2 + R, S / 2)], fill=COL_CAND + (255,), width=3)
        d.line([(S / 2, S / 2 - R), (S / 2, S / 2 + R)], fill=COL_CAND + (255,), width=3)
        d.ellipse([S / 2 - 12, S / 2 - 12, S / 2 + 12, S / 2 + 12],
                  outline=COL_CAND + (255,), width=3)

    meta = dict(cid=c["cid"], kind=c["kind"], reasons=c["reasons"], note=c["note"],
                center_xy=[cx, cy], crop_px=S, ground_m=round(S * geo.G, 1),
                rotation_deg=round(ang, 1), rotation_exact=ang, n_hi=len(info), lanes=info,
                roads=c["roads"], src_deg=c["deg"],
                labels=labels, rotated=rotate)
    return sat, vec, meta


def _clip_seg(p, q, S):
    """线段 p->q 与 [0,S]x[0,S] 求交（Liang-Barsky），返回参数区间 (t0, t1) 或 None。"""
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for a, b in ((-dx, p[0]), (dx, S - p[0]), (-dy, p[1]), (dy, S - p[1])):
        if a == 0:
            if b < 0:
                return None
            continue
        t = b / a
        if a < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return t0, t1


def visible_run(pts, S):
    """折线在图幅内最长的一段。

    返回 (点列, 首端是否真端点, 末端是否真端点)；整条线都在图外时返回 None。
    "真端点" = 这一端就是矢量的端点（图上有白方块）；否则是被图幅边界截断的。
    """
    P = np.asarray(pts, float)
    runs, cur = [], None
    for i in range(len(P) - 1):
        c = _clip_seg(P[i], P[i + 1], S)
        if c is None or c[1] - c[0] <= 1e-9:
            cur = None
            continue
        a = P[i] + (P[i + 1] - P[i]) * c[0]
        b = P[i] + (P[i + 1] - P[i]) * c[1]
        if cur is not None and c[0] == 0.0:
            cur["pts"].append(b)
        else:
            cur = dict(pts=[a, b], head=(i == 0 and c[0] == 0.0), tail=False)
            runs.append(cur)
        cur["tail"] = (i == len(P) - 2 and c[1] == 1.0)
        if c[1] < 1.0:
            cur = None
    if not runs:
        return None
    best = max(runs, key=lambda r: geo.polyline_len(np.array(r["pts"])))
    return np.array(best["pts"]), best["head"], best["tail"]


def _resample(P, n):
    """沿弧长等分取 n 个点（含首尾）。"""
    seg = np.linalg.norm(np.diff(P, axis=0), axis=1)
    acc = np.concatenate([[0.0], np.cumsum(seg)])
    out = []
    for t in np.linspace(0.0, acc[-1], n):
        i = min(int(np.searchsorted(acc, t, side="right")) - 1, len(seg) - 1)
        f = 0.0 if seg[i] == 0 else (t - acc[i]) / seg[i]
        out.append(P[i] + (P[i + 1] - P[i]) * f)
    return np.array(out)


def main(only=None, crop=None, labels="both", rotate=True, out_dir=None):
    C = json.load(open(os.path.join(HERE, "candidates.json")))["cands"]
    if only:
        C = [c for c in C if c["cid"] in only]
    lanes = geo.load_lanes()
    im = geo.open_raster()
    out = out_dir or OUT
    os.makedirs(out, exist_ok=True)
    for i, c in enumerate(C):
        d = os.path.join(out, c["cid"])
        os.makedirs(d, exist_ok=True)
        sat, vec, meta = render(im, c, lanes, crop or CROP, labels, rotate)
        sat.save(os.path.join(d, "sat.png"))
        vec.save(os.path.join(d, "vec.png"))
        json.dump(meta, open(os.path.join(d, "meta.json"), "w"), ensure_ascii=False, indent=1)
        print(f"{i+1}/{len(C)}", c["cid"], c["kind"], "rot", meta["rotation_deg"],
              "hi", meta["n_hi"], flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="逗号分隔的 cid")
    ap.add_argument("--crop", type=int, default=CROP)
    ap.add_argument("--meters", type=float,
                    help="按地面米数指定图幅（原生分辨率，不重采样），如 150；给了就覆盖 --crop")
    ap.add_argument("--labels", choices=LABEL_MODES, default="both",
                    help="编号/引线画在哪：both=两张图都画(v0)；vec=只画在纯矢量图(v1)；none=都不画")
    ap.add_argument("--no-rotate", action="store_true",
                    help="保持原始正北朝向，不按道路走向旋转（A/B 用）")
    ap.add_argument("--out", help="输出目录，默认 ret/<ver>/crops/")
    ap.add_argument("--ver", help="产物版本目录 ret/<ver>/，默认 v1（也可设环境变量 VLM_RET_VERSION）")
    a = ap.parse_args()
    if a.ver:
        paths.use(a.ver)
    if a.meters:
        a.crop = int(round(a.meters / geo.G))
    main(a.only.split(",") if a.only else None, a.crop,
         labels=a.labels, rotate=not a.no_rotate, out_dir=a.out)
