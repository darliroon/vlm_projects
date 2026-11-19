# vlm_projects

用视觉大模型（VLM）处理车道矢量数据的两个实验项目。代码用 git 管理，实验产物不进 git。

| 项目 | 做什么 | 入口 |
|---|---|---|
| [vlm_direct_sat/](vlm_direct_sat/) | 直接看卫片，在图上找出"车道数变化的过渡点"并给出 bbox | `ms3.py` / `ms6.py` / `score.py` |
| [vlm_match_sat/](vlm_match_sat/) | 把已提取的矢量车道和卫片对照，让模型判断矢量哪里不对 | `ask.py` / `render.py` / `webexport.py` |

## 约定

**代码和产物分开管。**

- 代码：在项目根目录，进 git。回退某个版本 = `git checkout vlm_match_sat-v0`。
- 产物（裁剪图、模型返回、查看器大图、对比图）：在 `ret/<版本>/`，被 `.gitignore` 排除。
  需要回溯产物，就切到对应 tag 再用该版代码重跑；`ret/` 里已有的旧产物原样保留，不随 git 删除。

```
vlm_projects/
  .env                      凭据，不进 git
  vlm_direct_sat/
    mscall.py ms2.py ...    各版本脚本（v1→mscall, v2→ms2, ...）
    score.py                按 annotations.json 打分
    ret/v1/ v2/ ... v6/     各版本的模型输出、日志、对比图
    ret/gt/                 人工标注的参考图与得分图
  vlm_match_sat/
    ask.py render.py ...    当前版本代码
    paths.py                统一给出 ret/<ver>/ 路径
    ret/v0/ v1/             各版本的裁剪图、模型结论、查看器、报表
    ret/_archive/           历史手工备份
```

## 版本

| tag | 项目 | 说明 |
|---|---|---|
| `vlm_match_sat-v0` | vlm_match_sat | 初始版：1024 px、双图、六选一 prompt，两轮各 40 次调用 |
| `vlm_match_sat-v1` | vlm_match_sat | 卫片不烧编号、150 m 图幅、不旋转、prompt 小改 |

`vlm_direct_sat` 的六轮探索（v1～v6）是同一份代码的不同实验臂，脚本并存、用文件名区分，
没有做成 tag；每个脚本头部注释写了它对应 `ret/` 下的哪个目录。

## 环境

`.env` 放在本目录（`OPENAI_BASE_URL` / `OPENAI_API_KEY`），两个项目的脚本都从 `../.env` 读。
原始航拍数据不在这里，路径见各项目的 README。
