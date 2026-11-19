"""车道 分支/合流 识别：调用 gpt-6-astra / claude-fable-5-2（OpenAI 兼容中转端点）。

用法:
    python mscall.py MODEL TILE_INDEX [TILE_INDEX ...]
结果写入 ret/v1/results/<model>/<index>.json
"""
import base64
import io
import json
import os
import re
import sys
import time

import requests
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
RET = os.path.join(HERE, "ret")          # 所有实验结果按 vN 分目录放在 ret/ 下
TILES = "/home/liufeng/data/lane-tiles/z19"


def load_env(path=os.path.join(HERE, "..", ".env")):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()
BASE = ENV["OPENAI_BASE_URL"].rstrip("/")
KEY = ENV["OPENAI_API_KEY"]

PROMPT = """这是一张荷兰高速/干线公路的正射航拍图（卫片），地面分辨率约 18.7 cm/像素，图幅约 48 m × 48 m，上北下南。

任务：判断图中主路是否存在【车道分支 (diverge/split)】或【车道合流 (merge)】。
- 分支：一条车行道（或一组车道）在下游分成两条独立的行驶路径，例如出口匝道岔出、车道数增加后分叉。路面上通常出现 V 形/斜纹导流区(gore)、实线渐变为虚线、车道线尖端 (nose)。
- 合流：两条独立路径在下游汇成一条，例如入口匝道并入、车道数减少。
- 不算：平行的普通直行车道、与主路立体交叉(桥梁跨越)但不相连的道路、仅有路肩/硬路肩宽度变化、车辆遮挡造成的错觉。

请只输出一个 JSON 对象，不要有其他文字：
{
  "has_event": true/false,
  "events": [
    {
      "type": "diverge" | "merge",
      "nose_xy": [x, y],          // 导流区尖端/分合流点在图中的位置，归一化坐标 0~1，(0,0)为左上角，x向右，y向下
      "from_lanes": 整数,          // 分/合流前的车道数（单侧）
      "to_lanes": 整数,            // 分/合流后的车道总数（各分支之和）
      "evidence": "一句话说明依据"
    }
  ],
  "confidence": 0~1 的小数,
  "notes": "一句话总体判断"
}
没有事件时 events 为空数组。"""


def img_b64(path, size=768):
    im = Image.open(path).convert("RGB")
    if size and size != im.width:
        im = im.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def call(model, path, prompt=PROMPT, size=768, max_tokens=4000, timeout=300):
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + img_b64(path, size)}},
                {"type": "text", "text": prompt},
            ],
        }],
        "max_tokens": max_tokens,
    }
    t0 = time.time()
    r = requests.post(BASE + "/chat/completions", json=payload,
                      headers={"Authorization": "Bearer " + KEY}, timeout=timeout)
    dt = time.time() - t0
    if r.status_code != 200:
        return {"_error": f"HTTP {r.status_code}: {r.text[:500]}", "_latency_s": round(dt, 1)}
    j = r.json()
    content = j["choices"][0]["message"].get("content") or ""
    out = {"_latency_s": round(dt, 1), "_usage": j.get("usage"), "_raw": content}
    m = re.search(r"\{.*\}", content, re.S)
    try:
        out["parsed"] = json.loads(m.group(0))
    except Exception as e:
        out["_parse_error"] = str(e)
    return out


def run(model, idxs, force=False):
    d = os.path.join(RET, "v1", "results", model)
    os.makedirs(d, exist_ok=True)
    for i in idxs:
        fp = os.path.join(d, f"{int(i):04d}.json")
        if os.path.exists(fp) and not force:
            continue
        res = call(model, os.path.join(TILES, f"{int(i):04d}.png"))
        json.dump(res, open(fp, "w"), ensure_ascii=False, indent=1)
        p = res.get("parsed")
        print(model, f"{int(i):04d}", res["_latency_s"], "s",
              (p and p.get("has_event")) if p else res.get("_error") or res.get("_parse_error"),
              flush=True)


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2:])
