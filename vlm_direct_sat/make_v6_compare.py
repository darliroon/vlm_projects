"""1024px 结果对比图: 每行一张切片; 列 = 你的标注 / gpt 3 次 / claude 3 次。
粗绿框 = 你的标注(第一列), 细绿框 = 标注(其余列, 作参照); 橙/黄/粉 = 第 1/2/3 次运行。
"""
import os
from PIL import Image, ImageDraw
from score import GT, load_boxes, score_tile
from mscall import HERE, RET, TILES

TILE_IDS = sorted(GT)
MODELS = [("gpt-6-astra", "gpt"), ("claude-fable-5-2", "claude")]
PAL = [(255, 120, 0), (255, 220, 0), (255, 0, 200)]
S = 400
cols = [("GT", None)] + [(f"{short} 1024px", m) for m, short in MODELS]
sheet = Image.new("RGB", ((S + 6) * len(cols), (S + 6) * len(TILE_IDS)), "white")
for r, t in enumerate(TILE_IDS):
    base = Image.open(os.path.join(TILES, f"{t}.png")).convert("RGB").resize((S, S), Image.LANCZOS)
    for c, (label, m) in enumerate(cols):
        im = base.copy()
        d = ImageDraw.Draw(im)
        d.rectangle([v * S for v in GT[t]], outline=(0, 255, 0), width=4 if m is None else 1)
        text = f"{t} {label}"
        if m:
            hits = 0
            for k in range(3):
                fp = os.path.join(RET, "v6", "results", "s1024", m, f"r{k + 1}", f"{t}.json")
                boxes = load_boxes(fp) or []
                hits += score_tile(boxes, GT[t])["hit_near"]
                for b in boxes:
                    d.rectangle([v * S for v in b], outline=PAL[k], width=3)
            text += f"  hit {hits}/3"
        d.rectangle([0, 0, S, 14], fill=(0, 0, 0))
        d.text((3, 2), text, fill="white")
        sheet.paste(im, (c * (S + 6), r * (S + 6)))
sheet.save(os.path.join(RET, "v6", "v6_compare.png"))
print("saved", sheet.size)
