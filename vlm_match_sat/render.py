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


def layout_labels(info, S, pad=8, gap=34):
    """把每个高亮 Lane 的标签排到左/右两侧空白栏，纵向避让。

    返回 [(item, (lx, ly, side))]。side='left' 时 lx 是文字块左边界，
    'right' 时 lx 是右边界。
    """
    if not info:
        return []
    mid = S / 2
    rows = []
    for it in info:
        ax, ay = it["anchor"]
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


def render(im, c, lanes, S=CROP, crop_only=False):
    cx, cy = c["x"], c["y"]
    ang = heading_at(cx, cy, lanes)
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
                                     round(float(P[len(P) // 2][1]), 1)]))

    # ---- 标签放在图幅左右两侧的空白区，用引线指向车道，避免并排车道标签粘连
    # 引线必须与车道线区分开：用黄色短虚线（十字准星同色 = 纯标注）
    placed = layout_labels(info, S)
    for item, (lx, ly, side) in placed:
        txt = f"L{item['k']}"
        ax, ay = item["anchor"]
        for d in (ds, dv):
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
                rotation_deg=round(ang, 1), n_hi=len(info), lanes=info,
                roads=c["roads"], src_deg=c["deg"])
    return sat, vec, meta


def main(only=None, crop=None):
    C = json.load(open(os.path.join(HERE, "candidates.json")))["cands"]
    if only:
        C = [c for c in C if c["cid"] in only]
    lanes = geo.load_lanes()
    im = geo.open_raster()
    os.makedirs(OUT, exist_ok=True)
    for i, c in enumerate(C):
        d = os.path.join(OUT, c["cid"])
        os.makedirs(d, exist_ok=True)
        sat, vec, meta = render(im, c, lanes, crop or CROP)
        sat.save(os.path.join(d, "sat.png"))
        vec.save(os.path.join(d, "vec.png"))
        json.dump(meta, open(os.path.join(d, "meta.json"), "w"), ensure_ascii=False, indent=1)
        print(f"{i+1}/{len(C)}", c["cid"], c["kind"], "rot", meta["rotation_deg"],
              "hi", meta["n_hi"], flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="逗号分隔的 cid")
    ap.add_argument("--crop", type=int, default=CROP)
    ap.add_argument("--ver", help="产物版本目录 ret/<ver>/，默认 v0")
    a = ap.parse_args()
    if a.ver:
        paths.use(a.ver)
    main(a.only.split(",") if a.only else None, a.crop)
