# 版本管理

代码用 **git tag**，实验产物用 **`ret/<版本>/`**，两者靠版本名对应。

仓库根在 `vlm_projects/`，本项目的 tag 形如 `vlm_match_sat-v1`。

## 两个层次

| | 放在哪 | 进 git | 回退方式 |
|---|---|---|---|
| 代码 | 项目根目录的 `.py` / `viewer/*.html,js` | 是 | `git checkout vlm_match_sat-v0 -- .` 或 `git checkout vlm_match_sat-v0` |
| 产物 | `ret/<版本>/` | 否 | 直接改 `--ver`，或 `export VLM_RET_VERSION=v0` |

产物不随 git 删除：回退代码不会动 `ret/`，`ret/v0/` 和 `ret/v1/` 永远同时在。

## 目录

```
ret/
  v0/                     v0 那一版的输入与结果
    crops/                40 个候选的 sat.png + vec.png（1024 px，旋转）
    works/                claude-fable-5-2/s1024/r1,r2/
    viewer/img/           查看器底图
    sheet_*.png           联系表
    proposals_*.json      车道级修改建议
    consensus_*.json      多次运行的一致性汇总
    snapshot/             旧的手工快照（code/ params.json manifest.json ...），留作对照
  v1/
    crops/                v1 的裁剪图（1110 px，不旋转，编号只在矢量图上）
    crops_rot/            v1 但按道路旋转（对照臂）
    works/                claude-fable-5-2/s1110_v1/r1,r2,r3/
  _archive/               历史手工备份（旧代码、旧图）
  human_annots.json       人工标注，跨版本共用
```

## 用法

```bash
cd /home/liufeng/data/projects/vlm_projects/vlm_match_sat

# 新增一版：v2
python render.py --meters 150 --no-rotate --ver v2 --out crops     # 产物落 ret/v2/crops
python ask.py run --prompt v1 --reps 3 --ver v2                    # 产物落 ret/v2/works
git commit -am "..." && git tag vlm_match_sat-v2

# 回看旧版代码
git switch --detach vlm_match_sat-v0
```

`--ver` 只是换 `ret/` 下的子目录名，不决定行为；行为由 `ask.py --prompt`、
`render.py --labels/--meters/--no-rotate` 这些参数决定。`paths.py: VER` 是默认值，
也可用环境变量 `VLM_RET_VERSION` 覆盖。

## 用 git 对比两版代码

```bash
git diff vlm_match_sat-v0 vlm_match_sat-v1 -- vlm_match_sat/ask.py
git diff --stat vlm_match_sat-v0 vlm_match_sat-v1
```

只有代码能这样比。产物（裁剪图、prompt 实际发出的内容）不同，要看就给两版各跑一次
`ask.py run --dry-run --ver vN`，比较 `ret/vN/works/_dryrun/` 里的 sha256 与文本。

## 版本表

| tag | 变更 |
|---|---|
| `vlm_match_sat-v0` | 初始版：1024 px ≈ 138 m、按道路旋转、编号烧在两张图上、prompt 1279 字符；每候选 2 张图；两轮各 40 次调用 |
| `vlm_match_sat-v1` | 卫片上只留线/端点/十字（编号只在矢量图）、引线指向图内可见中点、1110 px = 150 m 原生、不旋转、prompt 1323 字符（只改图例 4 句）；新增 few-shot 判例支持与 `--dry-run` |

v1 同时改了四样，所以 v0→v1 的差异不能归因到单独某一项。对照臂 `crops_rot`（v1 输入 + 旋转）用来单独看朝向的影响。

## v0 基线数字

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

## 输入侧可动的变量

按预期收益排序（都还没验证）：

| 变量 | 当前位置 | 现状 | 可尝试 |
|---|---|---|---|
| 图幅 | `render.py: CROP` | 1024 px / 138 m | 1536 px（看得更远，但标线变糊） |
| 送模型尺寸 | `ask.py --size` | 按原生尺寸（v1 是 1110） | 768（省 token）/ 1536 |
| 线宽 | `render.py: LINE_PX` | 5 px ≈ 0.68 m | 更细（减少遮挡）/ 更粗（更醒目） |
| 图像数量 | `ask.py` | 2 张（sat + vec） | 只给 sat / 给 sat + 局部放大图 |
| 是否烧入标注 | `render.py --labels` | v1 起编号只在 vec 图 | 完全不画 / 编号走文字 |
| prompt 结构 | `ask.py: PROMPT` | 六选一 + 判据提示 | 加 few-shot 示例 / 先描述后判断两步 |
| 上下文车道 | `render.py` | 灰线 alpha 130 | 完全不给 / 加强 |
| 旋转策略 | `render.py --no-rotate` | v1 起不旋转 | 旋转（`crops_rot` 就是这个对照） |

## 两个尚未解决的问题

1. **没有真值，指标无法判断好坏。**
   现在只有"两轮一致性 45%"这类稳定性指标，没有准确率。
   优先级最高的是用查看器的人工核对功能攒出 `ret/human_annots.json`，
   哪怕只核 60 条（30 正例 + 30 负例），也能开始算 precision。

2. **单次结果不稳定（45%）掩盖了输入优化的效果。**
   如果一个改动带来的提升小于采样噪声，两轮对比会看不出来。
   验证新版本时每版跑 **3 轮**，用"3 轮都指出的结论数"作为主指标。
