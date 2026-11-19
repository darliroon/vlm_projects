"""v2: 新 prompt（目标=车道数发生变化的位置，不区分分支/合流，只圈大致范围）
对比 默认推理 vs 最强推理。

gpt-6-astra      -> /chat/completions, reasoning_effort=xhigh
claude-fable-5-2 -> 原生 /messages,    thinking enabled (budget 16000)
结果: ret/v2/results/<model>/<mode>/<idx>.json
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mscall import BASE, HERE, KEY, RET, TILES, img_b64  # noqa: E402

PROMPT = """这是一张荷兰高速/干线公路的正射航拍图（卫片），地面分辨率约 18.7 cm/像素，图幅约 48 m × 48 m，上北下南。

任务：找出图中【车道数沿道路方向发生变化】的位置。这类位置统称"分支合流事件"，包括：
- 车道增加或减少：新车道从路肩、导流区或匝道侧长出来，或某条车道逐渐变窄直至消失；
- 出入口匝道的分岔与并入；
- 辅助车道、加速/减速车道的起点与终点；
- 斜纹/V 形导流区、车道线实线与虚线的渐变过渡。
不需要判断是"分支"还是"合流"，也不需要判断行驶方向。

不算事件：互相平行、宽度恒定的普通车道；与主路立体交叉（桥梁跨越）但并不相连的道路；仅因车辆、阴影、视角造成的错觉。

建议步骤：
1. 找出图中每一组车行道（以中央分隔带、护栏、草带分隔）。
2. 沿每组车行道的走向，在上游端和下游端各数一次车道数（不含路肩），并留意中间车道宽度是否渐变、车道线是否出现尖端或汇聚。
3. 车道数不一致，或中间出现车道线汇聚/分离的位置，就是一个事件。

最终只输出一个 JSON 对象（可以先写分析，但 JSON 必须放在最后）：
{
  "lane_survey": [{"carriageway": "简短描述", "lanes_end_a": 整数, "lanes_end_b": 整数, "note": "..."}],
  "has_event": true/false,
  "regions": [
    {"bbox": [x0, y0, x1, y1],   // 事件发生的大致范围，归一化坐标 0~1，(0,0) 为左上角，x 向右，y 向下；范围宁大勿小
     "evidence": "一句话依据",
     "confidence": 0~1}
  ]
}
没有事件时 regions 为空数组。"""

MODES = {
    "gpt-6-astra": {"default": {}, "max": {"reasoning_effort": "xhigh"}},
    "claude-fable-5-2": {"default": {},
                         "max": {"thinking": {"type": "enabled", "budget_tokens": 16000}}},
}


def call_gpt(path, extra, size=768):
    p = {"model": "gpt-6-astra", "max_tokens": 20000,
         "messages": [{"role": "user", "content": [
             {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img_b64(path, size)}},
             {"type": "text", "text": PROMPT}]}]}
    p.update(extra)
    r = requests.post(BASE + "/chat/completions", json=p,
                      headers={"Authorization": "Bearer " + KEY}, timeout=900)
    if r.status_code != 200:
        return None, None, f"HTTP {r.status_code}: {r.text[:300]}"
    j = r.json()
    u = j.get("usage") or {}
    rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    return j["choices"][0]["message"].get("content") or "", {"completion": u.get("completion_tokens"), "reasoning": rt}, None


def call_claude(path, extra, size=768):
    p = {"model": "claude-fable-5-2", "max_tokens": 32000,
         "messages": [{"role": "user", "content": [
             {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                          "data": img_b64(path, size)}},
             {"type": "text", "text": PROMPT}]}]}
    p.update(extra)
    r = requests.post(BASE + "/messages", json=p,
                      headers={"x-api-key": KEY, "anthropic-version": "2023-06-01"}, timeout=900)
    if r.status_code != 200:
        return None, None, f"HTTP {r.status_code}: {r.text[:300]}"
    j = r.json()
    u = j.get("usage") or {}
    text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    return text, {"completion": u.get("output_tokens"),
                  "reasoning": (u.get("output_tokens_details") or {}).get("thinking_tokens")}, None


def job(a):
    model, mode, idx = a
    fp = os.path.join(RET, "v2", "results", model, mode, f"{idx}.json")
    if os.path.exists(fp):
        return a, "cached"
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    path = os.path.join(TILES, f"{idx}.png")
    t0 = time.time()
    fn = call_gpt if model == "gpt-6-astra" else call_claude
    try:
        text, usage, err = fn(path, MODES[model][mode])
    except Exception as e:  # noqa: BLE001
        text, usage, err = None, None, f"EXC {e}"
    out = {"_latency_s": round(time.time() - t0, 1), "_usage": usage, "_error": err, "_raw": text}
    if text:
        m = re.search(r"\{[^{}]*\"lane_survey\".*\}", text, re.S) or re.search(r"\{.*\}", text, re.S)
        try:
            out["parsed"] = json.loads(m.group(0))
        except Exception as e:  # noqa: BLE001
            out["_parse_error"] = str(e)
    if err is None:  # 失败不落盘，便于重跑
        json.dump(out, open(fp, "w"), ensure_ascii=False, indent=1)
    return a, f"{out['_latency_s']}s usage={usage} err={err} parse_err={out.get('_parse_error')}"


if __name__ == "__main__":
    idxs = sys.argv[1:] or ["0102", "0254", "0256", "0334", "0404"]
    jobs = [(m, mode, i) for i in idxs for m in MODES for mode in MODES[m]]
    with ThreadPoolExecutor(10) as ex:
        for a, msg in ex.map(job, jobs):
            print(*a, msg, flush=True)
