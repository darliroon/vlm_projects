# 版本管理（手动）

本目录不使用 git。版本以 `versions/<名字>/` 的目录形式手动固化。

## 为什么按"快照"管，而不是只看代码

后续优化重点是**输入大模型的东西**：prompt、图幅、线宽、图层、候选判据。
这些东西散在 6 个 `.py` 里，光对比源码看不出"模型实际收到的内容"变了多少。
所以每一版都存一份**输入指纹**——每个候选的高亮车道几何 + 两张图的 sha256。

## 目录结构

```
versions/
  v0/
    code/            6 个 .py 的完整副本（可直接回滚运行）
    params.json      所有可调参数（AST 提取，不漏项）
    prompt.txt       完整 prompt 原文
    manifest.json    40 个候选的输入指纹（几何 x/y + lanes + sat/vec 的 sha256）
    results.json     该版本的模型结论全文（含 usage / latency）
    metrics.json     规模 / 成本 / 一致性指标
    candidates.json  候选定义
    notes.md         这版改了什么、为什么
```

不含大图（`crops/`、`viewer/img/`）。要恢复某版图片，用该版 `code/` + `candidates.json` 重跑 `render.py`。

## 用法

```bash
cd /home/liufeng/data/lane-distill/merge_split2

# 拍快照
python snapshot.py take v1 "改动说明"

# 对比两版（重点看 prompt diff / 图片哈希变化 / 指标变化）
python snapshot.py compare v0 v1

# 列出所有版本
python snapshot.py list
```

## 优化工作流

1. 从 v0 的 `code/` 里复制一份，在**根目录**改
2. 改完重跑必要的阶段（改 prompt 只需重跑 `ask.py`；改渲染要重跑 `render.py` + `ask.py`）
3. `python snapshot.py take v1 "为什么这么改"`
4. `python snapshot.py compare v0 v1` 看输入和指标的差异

**建议每次只改一个变量**，否则 `compare` 出来的差异没法归因。

## v1（代码已就绪，尚未调用模型）

v1 不覆盖 v0 的任何东西：图在 `crops_v1/`，结论在 `works/<model>/s1110_<tag>/`。
`render.py`、`ask.py` 不带新参数时的行为与 v0 逐像素、逐字相同。

v1 相对 v0 同时改了四样（所以 v0→v1 的差异不能归因到单独某一项）：

| 项 | v0 | v1 |
|---|---|---|
| 卫片上的编号/引线 | 烧在卫片和纯矢量图上 | 只留在纯矢量图上，卫片上只有线、端点、十字 |
| 引线指向 | 中间顶点（可能在图外，8/166 条编号画到了画布外） | 图内可见部分的中点（166/166 可见） |
| 图幅 | 1024 px = 138.4 m | 1110 px = 150.0 m（原生分辨率，不重采样） |
| 朝向 | 按道路走向旋转 | 不旋转，图的上方是正北 |
| prompt | 1279 字符 | 1323 字符，只改图例相关的 4 句，判据和输出格式逐字不变 |

prompt 里不加车道坐标。

| 实验臂 | 相对 v1 | 渲染 | 调模型 |
|---|---|---|---|
| `v1` | — | `render.py --labels vec --meters 150 --no-rotate --out crops_v1` | `ask.py run --prompt v1 --crops crops_v1 --tag v1 --reps 3` |
| `v1rot` | 按道路走向旋转（与 v0 相同的朝向，对照用，可不跑） | `render.py --labels vec --meters 150 --out crops_v1_rot` | `ask.py run --prompt v1 --crops crops_v1_rot --tag v1rot --reps 3` |
| `v1fs` | 加人工判例 | 同 v1 | `ask.py run --prompt v1 --crops crops_v1 --tag v1fs --fewshot 3 --reps 3` |

- `--reps N`：每个候选独立调用 N 次（结果分别在 `r1/`…`rN/`），调用总数 = 候选数 × N。
  已有结果的会跳过，中断后重跑同一条命令只补没跑完的。
- 不旋转的原因：v0 里模型写的"向东/向南延长"按的是图上方向（检验的 65 条里 60 条如此），
  旋转后的图上这些方位与真实方向不符；旋转角本身也不稳定。查看器底图同步改为不旋转。
- 任何一条 `ask.py run` 后面加 `--dry-run` 就不调模型，只把将要发送的内容写到 `works/_dryrun/`。
- `ask.py` 不给 `--size` 时按裁剪图原生尺寸送图（v0 是 1024，v1 是 1110）。
- 人工判例来自查看器的"✎ 标注模式"，自动保存到 `human_annots.json`；
  挑判例时会排除被测候选自己，以及图幅与它重叠的相邻候选。
- 看某一版的结论：`python webexport.py --tag v1` 重新导出查看器数据。
- v1 的联系表/建议：`review.py sheet --size 1110 --tag v1 --crops crops_v1`。
- 拍快照：`python snapshot.py take v1 "说明" --crops crops_v1 --prompt v1`。

## v0 基线

| 项 | 值 |
|---|---|
| 候选 | 40 个（branch+seam+dangle 8、branch+seam 2、branch+dangle 4、dangle 26） |
| 高亮车道总数 | 166 |
| prompt 长度 | 1279 字符 |
| 图幅 | 1024 px ≈ 138.4 m |
| 每候选输入 | 2 张图（sat.png 烧入标注 + vec.png 纯矢量） |
| 平均输入 token | 4270 |
| 平均输出 token | 5848（其中 thinking 约 89%） |
| 中位延迟 | 45 s |

两轮结果：

| | r1 | r2 |
|---|---|---|
| 结论条数 | 152 | 151 |
| ok 占比 | 42.8% | 32.5% |
| missing_lane | 68 | 78 |
| wrong_branch | 13 | 10 |
| wrong_break | 2 | 11 |
| wrong_count | 3 | 2 |
| 两轮候选级一致 | 18/40 = **45%** | — |

## 已知的、v1 可以动的输入侧变量

按预期收益排序（都还没验证）：

| 变量 | 当前位置 | 现状 | 可尝试 |
|---|---|---|---|
| 图幅 | `render.py: CROP` | 1024 px / 138 m | 1536 px（看得更远，但标线变糊） |
| 送模型尺寸 | `ask.py --size` | 1024（不缩放） | 768（省 token）/ 1536 |
| 线宽 | `render.py: LINE_PX` | 5 px ≈ 0.68 m | 更细（减少遮挡）/ 更粗（更醒目） |
| 图像数量 | `ask.py` | 2 张（sat + vec） | 只给 sat / 给 sat + 局部放大图 |
| 是否烧入标注 | `render.py` | 位置/编号都烧进图 | 只画线，编号走文字 |
| prompt 结构 | `ask.py: PROMPT` | 六选一 + 判据提示 | 加 few-shot 示例 / 先描述后判断两步 |
| 上下文车道 | `render.py` | 灰线 alpha 130 | 完全不给 / 加强 |
| 旋转策略 | `render.py: heading_at` | 沿道路转正 | 固定正北（原始朝向） |
| 候选判据 | `candidates.py` | branch/dangle/seam | 加入"车道数突变"等新判据 |

## 两个必须先解决的问题

1. **没有真值，指标无法判断好坏。**
   现在只有"两轮一致性 45%"这类稳定性指标，没有准确率。
   优先级最高的是用 `viewer/` 的人工核对功能攒出 `annotations.json`，
   哪怕只核 60 条（30 正例 + 30 负例），也能开始算 precision。

2. **单次结果不稳定（45%）掩盖了输入优化的效果。**
   如果一个改动带来的提升小于采样噪声，两轮对比会看不出来。
   验证新版本时建议每版跑 **3 轮**，用"3 轮都指出的结论数"作为主指标。
