"""阶段 A：纯矢量 + 栅格元数据的确定性分析，生成待核实的候选点。

不做任何视觉判断，只回答"哪些地方值得看"、"矢量层面我怀疑什么"。

设计
----
以"节点"为单位，一个节点可以同时带多条理由（避免同一处被 J/R/E 重复报）：

  branch  节点度 >= 3：多条 Lane 汇聚/分开
  dangle  端点只连着一条 Lane（该续未续 / 该断不该断）
  seam    不同 RoadId 交接且接缝不自然（车道数不等 / 折角 >18° / 错位 >1.5m）

同一处问题常常"多条车道各报一次"（例如 3 条车道一起断头），用空间聚类合并成
一个候选，理想情况下一个候选就是"一个路口 / 一个分合流点"。

输出 candidates.json / candidates.csv
"""
import argparse
import collections
import json
import os
import sys

import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geo  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

NODE_TOL_M = 1.0        # 顶点聚类成节点
MERGE_M = 45.0          # 同一条"事件"的空间合并半径
BORDER_PX = 40
NODATA_THR = 12
SEAM_ANG_DEG = 18.0
SEAM_OFF_M = 1.5


def cluster_nodes(lanes, tol_m=NODE_TOL_M):
    """把顶点聚成节点，返回 (nxy, deg, lanes_at, ends)。

    deg 的语义是"该节点上有几条线端"（线端数）：
      一条 Lane 的端点落在这里  -> 贡献 1
      一条 Lane 只是穿过这里    -> 贡献 2（进来又出去）
    这样 T 型接合（A 的端点撞在 B 的内部顶点上）会得到 deg=3，
    而不是像"只连端点"的邻接图那样漏掉 B。
    """
    pts = np.vstack([l["xy"] for l in lanes])
    tp = tol_m / geo.G
    tree = cKDTree(pts)
    pairs = tree.query_pairs(tp, output_type="ndarray")
    parent = np.arange(len(pts))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    root = np.array([find(i) for i in range(len(pts))])
    uniq, nid = np.unique(root, return_inverse=True)
    nxy = np.array([pts[nid == k].mean(0) for k in range(len(uniq))])

    # 顶点 -> 所属 Lane（用于 T 型接合）
    owner = np.concatenate([[i] * len(l["xy"]) for i, l in enumerate(lanes)])

    ends = {}
    ptr = 0
    for i, l in enumerate(lanes):
        a, b = int(nid[ptr]), int(nid[ptr + len(l["xy"]) - 1])
        ptr += len(l["xy"])
        ends[(i, 0)], ends[(i, -1)] = a, b

    # 所有顶点都参与建节点集合（不止端点），才能看见"穿过"的线
    lanes_at = collections.defaultdict(set)
    for v, n in enumerate(nid):
        lanes_at[int(n)].add(int(owner[v]))

    # 线端数：端点记 1，纯穿过记 2
    deg = collections.defaultdict(int)
    for n, ids in lanes_at.items():
        s = 0
        for i in ids:
            c = int(ends[(i, 0)] == n) + int(ends[(i, -1)] == n)
            s += c if c > 0 else 2
        deg[n] = s
    return nxy, dict(deg), lanes_at, ends


def endpoint_heading(lane, which):
    p = lane["xy"][0] if which == 0 else lane["xy"][-1]
    inner = geo.heading(lane["xy"], at_end=(which != 0), span_px=30.0)
    return p, inner


def seam_metrics(lanes, ids, ends, n):
    """接缝处：车道数差异、最差折角、最大错位"""
    cnt = collections.Counter(lanes[i]["road"] for i in ids)
    ang_bad = off_bad = 0.0
    for i in ids:
        L = lanes[i]
        for w in (0, -1):
            if ends[(i, w)] != n:
                continue
            p, inner = endpoint_heading(L, w)
            best = None
            for j in ids:
                if lanes[j]["road"] == L["road"]:
                    continue
                for wj in (0, -1):
                    if ends[(j, wj)] != n:
                        continue
                    pj, innerj = endpoint_heading(lanes[j], wj)
                    cang = float(np.dot(inner, innerj))
                    off = float(np.linalg.norm(p - pj)) * geo.G
                    if best is None or cang < best[0]:
                        best = (cang, off)
            if best:
                ang_bad = max(ang_bad, float(np.degrees(np.arccos(np.clip(-best[0], -1, 1)))))
                off_bad = max(off_bad, best[1])
    return dict(lane_counts=dict(cnt), ang_deg=ang_bad, off_m=off_bad,
                n_diff=len(set(cnt.values())) > 1)


def build(verbose=True):
    lanes = geo.load_lanes()
    bnds = geo.load_geojson(geo.BOUNDARY)
    curbs = geo.load_geojson(geo.CURB)
    im = geo.open_raster()
    aux_tree = cKDTree(np.vstack([b["xy"] for b in bnds + curbs]))

    nxy, deg, lanes_at, ends = cluster_nodes(lanes)

    # ---- 每个节点收集理由
    nodes = {}
    notes = collections.defaultdict(list)

    for n, ids in lanes_at.items():
        d = deg.get(n, 0)
        rs = []
        if d >= 3:
            rs.append("branch")
        if d <= 1:
            rs.append("dangle")
            for i in ids:
                gap = float(aux_tree.query(nxy[n], k=1)[0]) * geo.G
                if gap < 2.0:
                    notes[n].append(f"LaneBoundary/Curb 在此继续（距 {gap:.1f} m），偏向漏建")
                    break
        roads = {lanes[i]["road"] for i in ids}
        if len(roads) >= 2 and len(ids) >= 2:
            sm = seam_metrics(lanes, sorted(ids), ends, n)
            bad = []
            if sm["n_diff"]:
                bad.append("接缝两侧车道数不等")
            if sm["ang_deg"] > SEAM_ANG_DEG:
                bad.append(f"折角 {sm['ang_deg']:.0f}°")
            if sm["off_m"] > SEAM_OFF_M:
                bad.append(f"错位 {sm['off_m']:.1f} m")
            if bad:
                rs.append("seam")
                notes[n].append("、".join(bad))
        if rs:
            nodes[n] = dict(n=n, x=float(nxy[n][0]), y=float(nxy[n][1]), deg=int(d),
                            lanes=sorted({lanes[i]["id"] for i in ids}),
                            roads=sorted(roads), reasons=rs,
                            note="；".join(notes[n]),
                            lane_counts=sorted(collections.Counter(
                                lanes[i]["road"] for i in ids).values()))

    # ---- 空间合并：一个路口 = 一个候选
    items = sorted(nodes.values(), key=lambda r: (-len(r["reasons"]), r["y"], r["x"]))
    used, groups = set(), []
    for a in items:
        if a["n"] in used:
            continue
        g = [a]
        used.add(a["n"])
        for b in items:
            if b["n"] in used:
                continue
            if np.hypot(b["x"] - a["x"], b["y"] - a["y"]) * geo.G <= MERGE_M:
                g.append(b)
                used.add(b["n"])
        # 合并：中心取加权（branch 优先）平均
        w = np.array([3.0 if "branch" in x["reasons"] else 1.0 for x in g])
        cx = float(np.average([x["x"] for x in g], weights=w))
        cy = float(np.average([x["y"] for x in g], weights=w))
        reasons = sorted({r for x in g for r in x["reasons"]},
                         key=lambda r: ["branch", "seam", "dangle"].index(r))
        groups.append(dict(
            x=cx, y=cy,
            reasons=reasons,
            deg=max(x["deg"] for x in g),
            n_nodes=len(g),
            lanes=sorted({i for x in g for i in x["lanes"]}),
            roads=sorted({r for x in g for r in x["roads"]}),
            lane_counts=sorted({v for x in g for v in x["lane_counts"]}),
            note="；".join(sorted({x["note"] for x in g if x["note"]})),
        ))

    # ---- 可行性过滤
    out = []
    for c in groups:
        x, y = c["x"], c["y"]
        if not (BORDER_PX < x < geo.W - BORDER_PX and BORDER_PX < y < geo.H - BORDER_PX):
            continue
        box = (int(x) - 32, int(y) - 32, int(x) + 32, int(y) + 32)
        if geo.is_nodata(im, box, NODATA_THR):
            c["skip"] = "nodata"
            out.append(c)
            continue
        out.append(c)

    # ---- 优先级：branch+seam > branch > seam > dangle；同级别按 Lane 数
    def prio(c):
        r = set(c["reasons"])
        return (-(("branch" in r) * 2 + ("seam" in r) * 2 + ("dangle" in r) * 1),
                -len(c["lanes"]))
    live = [c for c in out if "skip" not in c]
    live.sort(key=lambda c: (prio(c), c["y"], c["x"]))
    for k, c in enumerate(live):
        c["kind"] = "+".join(c["reasons"])

    # ---- cid 必须稳定：crops/ 与 works/ 都按 cid 命名，一旦编号漂移，
    #      图和结果就会错位。所以 cid 由几何位置决定，与理由/排序无关：
    #      取 50 m 网格坐标。旧数据里出现过 cid 漂移导致 12/40 对不上的事故。
    for c in live:
        gx, gy = int(round(c["x"] * geo.G / 50)), int(round(c["y"] * geo.G / 50))
        c["cid_geom"] = f"C{gx:04d}_{gy:04d}"
    # 若几何 key 撞车（同一格内两个候选），按坐标排序加后缀
    seen = collections.Counter(c["cid_geom"] for c in live)
    if any(v > 1 for v in seen.values()):
        live.sort(key=lambda c: (c["y"], c["x"]))
        dup = collections.defaultdict(int)
        for c in live:
            k = c["cid_geom"]
            dup[k] += 1
            if seen[k] > 1:
                c["cid_geom"] = f"{k}_{dup[k]}"
    for c in live:
        c["cid"] = c["cid_geom"]

    if verbose:
        print(f"lanes={len(lanes)} nodes={len(nxy)}")
        print("候选", len(live), "被跳过(nodata/边界)", len(out) - len(live))
        print("理由组合:", collections.Counter(c["kind"] for c in live))
        print("度数分布(候选内):", collections.Counter(c["deg"] for c in live))
    return live, lanes


if __name__ == "__main__":
    lanes, lanes_raw = None, None
    live, lanes = build()
    json.dump({"cands": live}, open(os.path.join(HERE, "candidates.json"), "w"),
              ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "candidates.csv"), "w") as f:
        f.write("cid,kind,x,y,deg,n_lanes,roads,note\n")
        for c in live:
            f.write(f"{c['cid']},{c['kind']},{c['x']:.1f},{c['y']:.1f},{c['deg']},"
                    f"{len(c['lanes'])},{';'.join(map(str, c['roads']))},"
                    f"{c['note'].replace(',', '，')}\n")
    print("wrote candidates.json / candidates.csv")
