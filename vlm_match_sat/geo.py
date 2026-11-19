"""公共几何工具：EPSG:3857 <-> 经纬度 <-> 卫片像素；读取各图层。

坐标约定
--------
所有内部计算都用"图像像素坐标" (px, py)：
  px = (x - X0)/PS ,  py = (Y0 - y)/PS
注意 PS 是 3857 投影单位/像素；真实地面米/像素 G = PS*cos(lat) ≈ 0.1351 m/px（纬度 25.15°）。
凡是提到"米"的地方都用 G 换算（地面米），不要直接用 PS。
"""
import json
import math

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

ROOT = "/home/liufeng/data/data/lht_1112_1550_key_ret/5826125288959271"
TIF = f"{ROOT}/merged.tif"
LANE = f"{ROOT}/standardize/Lane.geojson"
BOUNDARY = f"{ROOT}/standardize/LaneBoundary.geojson"
CURB = f"{ROOT}/standardize/Curb.geojson"

X0, Y0 = 5740726.571530763, 2895167.101440467
PS = 0.14929107084857865           # 3857 单位/像素
R = 6378137.0
W, H = 10752, 11776

LAT0 = 25.150689691847585
G = PS * math.cos(math.radians(LAT0))   # 地面米/像素 ≈ 0.13514
PXM = G                                 # 别名：pixel -> ground metre


def ll2m(lon, lat):
    lon = np.asarray(lon, float)
    lat = np.asarray(lat, float)
    return R * np.radians(lon), R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def m2px(x, y):
    return (np.asarray(x) - X0) / PS, (Y0 - np.asarray(y)) / PS


def ll2px(lon, lat):
    return m2px(*ll2m(lon, lat))


def px2ll(px, py):
    x = np.asarray(px) * PS + X0
    y = Y0 - np.asarray(py) * PS
    lon = np.degrees(x / R)
    lat = np.degrees(2 * np.arctan(np.exp(y / R)) - np.pi / 2)
    return lon, lat


def ground_m_per_px(lat):
    return PS * math.cos(math.radians(lat))


def load_geojson(path):
    """读一个线图层，返回 [{id, road, props, xy(N,2 图像像素)}]（MultiLineString 会被拆开）"""
    d = json.load(open(path, encoding="utf-8"))
    out = []
    for f in d["features"]:
        g = f["geometry"]
        parts = [g["coordinates"]] if g["type"] == "LineString" else (
            g["coordinates"] if g["type"] == "MultiLineString" else [])
        p = dict(f.get("properties") or {})
        for k, part in enumerate(parts):
            c = np.asarray(part, float)
            if len(c) < 2:
                continue
            px, py = ll2px(c[:, 0], c[:, 1])
            out.append(dict(id=p.get("Id"), part=k, road=p.get("RoadId"),
                            props=p, xy=np.stack([px, py], 1)))
    return out


def load_lanes():
    return load_geojson(LANE)


def open_raster():
    return Image.open(TIF).convert("RGB")


def crop(im, cx, cy, size, scale=1.0):
    """以 (cx,cy) 为中心裁 size×size（像素），可选放大 scale 倍。返回 RGB Image。"""
    s = int(size)
    x0 = int(round(cx - s / 2))
    y0 = int(round(cy - s / 2))
    box = (x0, y0, x0 + s, y0 + s)
    im2 = im.crop(box)
    if scale != 1.0:
        im2 = im2.resize((int(s * scale), int(s * scale)), Image.LANCZOS)
    return im2, box


def is_nodata(im, box, thr=12):
    """黑块（无影像覆盖）判定。"""
    a = np.asarray(im.crop(box).resize((64, 64), Image.BILINEAR), dtype=np.float32)
    return float(a.mean()) < thr


def polyline_len(a):
    return float(np.linalg.norm(np.diff(a, axis=0), axis=1).sum())


def densify(xy, step_px=4.0):
    """按像素步长加密折线。"""
    xy = np.asarray(xy, float)
    out = []
    for i in range(len(xy) - 1):
        p, q = xy[i], xy[i + 1]
        n = max(1, int(np.ceil(np.linalg.norm(q - p) / step_px)))
        for k in range(n):
            out.append(p + (q - p) * (k / n))
    out.append(xy[-1])
    return np.array(out)


def heading(xy, at_end, span_px=25.0):
    """端点处的走向单位向量，指向"离开端点、进入线内"的反方向。"""
    a = np.asarray(xy, float)
    if at_end:
        a = a[::-1]
    acc = 0.0
    for i in range(len(a) - 1):
        seg = a[i + 1] - a[i]
        L = np.linalg.norm(seg)
        acc += L
        if acc >= span_px or i == len(a) - 2:
            v = a[min(i + 1, len(a) - 1)] - a[0]
            n = np.linalg.norm(v)
            return v / n if n else np.array([1.0, 0.0])
    return np.array([1.0, 0.0])
