"""为 HTML 查看器导出干净的底图 + 几何数据（不含任何烧进图里的标记）。

输出
----
viewer/img/<cid>.jpg      无标注的卫星裁剪（旋转后），2048 px
viewer/viewer_data.js     window.DATA = {cands:[...]}，几何用裁剪图坐标
viewer/thumbs/ 内嵌       列表用的缩略图（base64 直接在 js 里）

坐标：所有点都是"裁剪图坐标系"，原点左上，单位 = 底图像素。
全局像素 -> 裁剪图: local = R(-ang)·(p - c) + S/2
（与 render.py 的 rot() 完全一致，模型给的 where 乘 S/1024 即可对上）
"""
import base64
import io
import json
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geo  # noqa: E402
from render import heading_at, rot  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
import paths  # noqa: E402

OUT = paths.p("viewer")
IMG = os.path.join(OUT, "img")
SIZE = 2048
MODEL = "claude-fable-5-2"
WORK = paths.p("works", MODEL, "s1024")
MODEL_SIZE = 1024          # 模型当时看到的图幅（坐标基准）


def clean_crop(im, cx, cy, S, ang):
    half = int(S * 0.75) + 8
    x0, y0 = int(cx - half), int(cy - half)
    tile = Image.new("RGB", (2 * half, 2 * half), (0, 0, 0))
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(geo.W, x0 + 2 * half), min(geo.H, y0 + 2 * half)
    tile.paste(im.crop((sx0, sy0, sx1, sy1)), (sx0 - x0, sy0 - y0))
    ccx, ccy = cx - x0, cy - y0
    tile = tile.rotate(ang, resample=Image.BICUBIC, center=(ccx, ccy))
    l0, t0 = ccx - S / 2, ccy - S / 2
    return tile.crop((int(round(l0)), int(round(t0)),
                      int(round(l0)) + S, int(round(t0)) + S)).convert("RGB")


def b64jpg(img, q=70):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=q, subsampling=0, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def load_model(cid):
    """读两轮结果，取 r1 作为展示，r2 用于"两轮一致"标记"""
    out = {}
    for rep in (1, 2):
        fp = os.path.join(WORK, f"r{rep}", f"{cid}.json")
        if os.path.exists(fp):
            out[rep] = json.load(open(fp, encoding="utf-8"))
    return out


def _dist_seg(p, a, b):
    vx, vy = b[0] - a[0], b[1] - a[1]
    l2 = vx * vx + vy * vy
    t = 0.0 if not l2 else ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / l2
    t = max(0.0, min(1.0, t))
    return ((p[0] - (a[0] + t * vx)) ** 2 + (p[1] - (a[1] + t * vy)) ** 2) ** 0.5


def _nearest_on(pts, p):
    """返回 (最近点, 距离)。距离单位 = 裁剪图像素。"""
    best = (None, 1e18)
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        vx, vy = b[0] - a[0], b[1] - a[1]
        l2 = vx * vx + vy * vy
        t = 0.0 if not l2 else ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / l2
        t = max(0.0, min(1.0, t))
        q = [a[0] + t * vx, a[1] + t * vy]
        dd = ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5
        if dd < best[1]:
            best = (q, dd)
    return best


def match_lane(lane_key, L):
    """模型给的 lane 标识 -> 本裁剪图里的 lane 下标。

    模型有时写错编号（L3 写成 L13）或干脆不写编号，所以先精确匹配，
    再退化为"编号最接近"的猜测，都不行就返回 None。
    """
    if not lane_key:
        return None
    s = str(lane_key).strip()
    import re as _re
    m = _re.fullmatch(r"L(\d+)", s)
    if m:
        k = int(m.group(1))
        if any(x["k"] == k for x in L):
            return k
        # 容错：与某个高亮 lane 的编号"编辑距离"很小（如 13 vs 3）
        hi = [x["k"] for x in L if x["hi"]]
        for cand in hi:
            if str(cand) == s[1:].lstrip("0") or abs(cand - k) <= 10:
                return cand
        return None
    return None


def build():
    os.makedirs(IMG, exist_ok=True)
    C = json.load(open(os.path.join(HERE, "candidates.json")))["cands"]
    lanes = geo.load_lanes()
    im = geo.open_raster()
    cands = []

    for c in C:
        cid = c["cid"]
        cx, cy = c["x"], c["y"]
        ang = heading_at(cx, cy, lanes)
        crop = clean_crop(im, cx, cy, SIZE, ang)
        crop.save(os.path.join(IMG, f"{cid}.jpg"), quality=90, subsampling=0, optimize=True)
        thumb = b64jpg(crop.resize((128, 128), Image.LANCZOS), 68)

        # ---- 几何（裁剪图坐标）
        L = []
        for k, ln in enumerate(lanes):
            P = rot(ln["xy"], ang, cx, cy, SIZE)
            if not ((P[:, 0] > -SIZE * 0.15) & (P[:, 0] < SIZE * 1.15) &
                    (P[:, 1] > -SIZE * 0.15) & (P[:, 1] < SIZE * 1.15)).any():
                continue
            pts = [[round(float(x), 1), round(float(y), 1)] for x, y in P]
            L.append(dict(k=k, id=ln["id"], road=ln["road"],
                          len_m=round(geo.polyline_len(ln["xy"]) * geo.G, 1),
                          hi=(ln["id"] in set(c["lanes"])), pts=pts))

        # ---- 模型结论
        # 模型给的 where 是"看着像"的粗位置，实测中位偏差 11.6 m、p90 35 m，
        # 所以把它锚定到所属 lane 的最近点上，另存原始点 + 偏差供查看器画参考线。
        res = load_model(cid)
        parsed = (res.get(1) or {}).get("parsed") or {}
        sc = SIZE / MODEL_SIZE
        by_k = {x["k"]: x for x in L}
        verd = []
        for v in parsed.get("verdicts", []):
            w = v.get("where")
            raw = None
            if isinstance(w, list) and len(w) == 2:
                try:
                    raw = [round(float(w[0]) * sc, 1), round(float(w[1]) * sc, 1)]
                except (TypeError, ValueError):
                    raw = None
            k = match_lane(v.get("lane"), L)
            anchor, dev_m = None, None
            if k is not None and raw:
                anchor, dev_px = _nearest_on(by_k[k]["pts"], raw)
                anchor = [round(anchor[0], 1), round(anchor[1], 1)]
                dev_m = round(dev_px * geo.G, 1)
            elif k is not None:
                # 没给位置就放在该 lane 中点
                pts = by_k[k]["pts"]
                anchor = pts[len(pts) // 2]
            verd.append(dict(lane=v.get("lane"), k=k, problem=v.get("problem"),
                             confidence=v.get("confidence"), xy=raw, anchor=anchor,
                             dev_m=dev_m, evidence=v.get("evidence"), fix=v.get("fix")))

        # 两轮都指出的 lane+类型
        def key(d):
            return {(x.get("lane"), x.get("problem"))
                    for x in ((d or {}).get("parsed") or {}).get("verdicts", [])
                    if x.get("problem") not in ("ok", "unreadable", None)}
        k1, k2 = key(res.get(1)), key(res.get(2))
        stable = sorted([f"{a}|{b}" for a, b in (k1 & k2)])

        cands.append(dict(
            cid=cid, kind=c["kind"], reasons=c["reasons"], note=c["note"],
            roads=c["roads"], deg=c["deg"], n_hi=sum(1 for x in L if x["hi"]),
            severity=parsed.get("severity"), overall=parsed.get("overall"),
            size=SIZE, rotation_deg=round(ang, 1),
            ground_m=round(SIZE * geo.G, 1),
            pxPerM=round(1 / geo.G, 3),
            center_xy=[cx, cy], cand_xy=[SIZE / 2, SIZE / 2],
            thumb=thumb, lanes=L, verdicts=verd, stable=stable,
            raw_text=(res.get(1) or {}).get("raw"),
        ))
        print(f"{cid} lanes={len(L)} verdicts={len(verd)}", flush=True)

    payload = dict(model=MODEL, size=SIZE, count=len(cands), cands=cands)
    js = "window.DATA=" + json.dumps(payload, ensure_ascii=False,
                                     separators=(",", ":")) + ";"
    open(os.path.join(OUT, "viewer_data.js"), "w", encoding="utf-8").write(js)
    mb = os.path.getsize(os.path.join(OUT, "viewer_data.js")) / 1e6
    print(f"wrote viewer/viewer_data.js ({mb:.1f} MB), {len(cands)} candidates")
    tot = sum(os.path.getsize(os.path.join(IMG, f)) for f in os.listdir(IMG))
    print(f"images: {len(os.listdir(IMG))} files, {tot/1e6:.0f} MB")


def serve(port=8770, host="0.0.0.0"):
    """带 /api/mark 写盘接口的静态服务。

    默认监听 0.0.0.0：远程 SSH 连到服务器时，只绑 127.0.0.1 的端口
    在自己电脑的浏览器里是打不开的。要只允许本机访问，传 host="127.0.0.1"。

    HTML 里的"导出核对结果"按钮会 POST 到这里，直接落到 annotations.json，
    不需要手动下载再挪文件。
    """
    import http.server
    import socketserver
    HOST = host

    ann_path = os.path.join(HERE, "annotations.json")

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=OUT, **kw)

        def do_POST(self):
            if self.path.rstrip("/") != "/api/mark":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                self.send_error(400, "bad json")
                return
            # 与已有标注合并（按 cid|lane|problem 去重，新的覆盖旧的）
            old = {}
            if os.path.exists(ann_path):
                try:
                    for r in json.load(open(ann_path, encoding="utf-8")).get("rows", []):
                        old[f"{r['cid']}|{r['lane']}|{r['problem']}"] = r
                except (ValueError, KeyError):
                    pass
            for r in payload.get("rows", []):
                old[f"{r['cid']}|{r['lane']}|{r['problem']}"] = r
            rows = list(old.values())
            json.dump(dict(saved_at=payload.get("saved_at"), n=len(rows), rows=rows),
                      open(ann_path, "w"), ensure_ascii=False, indent=1)
            body = json.dumps({"ok": True, "n": len(rows)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(f"[mark] +{len(payload.get('rows', []))} -> {len(rows)} 条 -> {ann_path}", flush=True)

        def log_message(self, *a):
            pass

    class S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with S((HOST, port), H) as httpd:
        # 远程 SSH 场景下，127.0.0.1 只对本机可见；默认监听所有网卡，
        # 并同时打印本机 IP，方便直接复制到浏览器。
        import socket
        ips = [f"http://127.0.0.1:{port}/"]
        if HOST in ("0.0.0.0", ""):
            try:
                _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
                ips += [f"http://{a}:{port}/" for a in addrs if not a.startswith("127.")]
            except OSError:
                pass
        print("查看器:")
        for u in dict.fromkeys(ips):
            print("  " + u)
        print(f"标注保存到: {ann_path}")
        print("Ctrl-C 退出")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nbye")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true", help="启动本地服务（带标注保存接口）")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="0.0.0.0",
                    help="监听地址；远程访问用默认 0.0.0.0，只允许本机用 127.0.0.1")
    ap.add_argument("--ver", help="产物版本目录 ret/<ver>/，默认 v0")
    a = ap.parse_args()
    if a.ver:
        paths.use(a.ver)
        OUT = paths.p("viewer")
        IMG = os.path.join(OUT, "img")
        WORK = paths.p("works", MODEL, "s1024")
    if a.serve:
        serve(a.port, a.host)
    else:
        build()
