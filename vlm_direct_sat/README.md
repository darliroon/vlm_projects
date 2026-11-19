# vlm_direct_sat — 直接从卫片定位车道数变化点

输入一张 48 m 见方、18.7 cm/像素的正射航拍切片，让模型在图上找出
**车道数发生变化的那一小段过渡**（新车道出现/消失、导流斜纹区收口、渐变加宽），
输出归一化 bbox。

## 目录

```
mscall.py          v1 脚本（最早的 prompt + 调用封装，全项目的 BASE/KEY/TILES 也在这里）
ms2.py             v2：新 prompt —— 目标改成"车道数变化的位置"，不再区分分支/合流；
                        对比默认推理 vs 最强推理
ms3.py             v3：依据人工标注归纳的 prompt（A/B/C 三种样式）；
                        变体 tight(原图) / grid(叠 10x10 编号网格)
ms4.py             v4：3 次采样 + 局部放大复核 + 投票
ms5.py             v5：不投票，放大后只修位置、不许否决
ms6.py             v6：prompt 不变，只改送图尺寸 256/512/1024，对照已有的 768
score.py           按 annotations.json 打分（hit_in / hit_near / IoU / 框面积）
make_v6_compare.py 出 1024 px 的逐行对比图
annotate_tool.py   本地标注小工具（标准库 only，存 annotations.json）
probe_reasoning.py 探测端点支持哪些模型/推理选项（一次性排查脚本）
annotations.json   人工标注：5 张切片，每张一处
```

## 产物

`ret/vN/` 与脚本一一对应，`ret/` 不进 git：

```
ret/v1/results/<model>/<idx>.json             mscall 的单次结果 + 日志
ret/v2/results/<model>/{default,max}/<idx>.json
ret/v3/results/{tight,grid}/<model>/r<k>/<idx>.json   + grid/ 切片原图
ret/v4/results/<model>/r<k>/<idx>.json        + v4_compare.png  v4_final_*.json
ret/v5/results/<model>/r<k>/<idx>.json        + v5_compare.png
ret/v6/results/s{256,512,1024}/<model>/r<k>/<idx>.json + v6_compare.png
ret/gt/   人工标注参考图、得分图、misses/overlay 检查图
```

## 运行

```bash
cd /home/liufeng/projects/vlm_projects/vlm_direct_sat

python ms3.py                 # 跑 v3（tight + grid，2 模型 × 2 轮 × 5 张）
python score.py               # 打分
python ms6.py run             # 跑 v6 的三种尺寸
python ms6.py                 # 只看 v6 报告（会一并带上 v3 的 768 px 结果）
python annotate_tool.py       # 标注工具，浏览器开 http://127.0.0.1:8765
```

原始切片在 `/home/liufeng/data/lane-tiles/z19/<idx>.png`（在 `mscall.py: TILES` 里）。
凭据从仓库根目录的 `.env` 读。
