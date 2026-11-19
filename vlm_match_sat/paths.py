"""所有实验产物（裁剪图 / 模型结论 / 查看器 / 报表）的落盘位置。

代码在本目录用 git 管理，版本 = tag；产物不进 git，统一放 ret/<版本>/。
用环境变量 VLM_RET_VERSION 指定用哪一版产物，默认 v1（当前代码对应的版本）。

    ret/
      v0/  crops/ works/ viewer/ viewer_img/ *.png *.json  snapshot/
      v1/  crops/ crops_rot/ works/ viewer/ *.png *.json
      _archive/  旧代码/旧图的手工备份
      human_annots.json   人工标注（跨版本共用）
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RET = os.path.join(HERE, "ret")
VER = os.environ.get("VLM_RET_VERSION", "v1")


def p(*parts):
    """ret/<VER>/<parts...>"""
    return os.path.join(RET, VER, *parts)


def use(ver):
    """命令行 --ver 用：切换产物版本目录并重算依赖它的模块级常量。"""
    global VER
    VER = ver


def rel(path):
    """相对本目录的路径（命令行里给人看用）"""
    return os.path.relpath(path, HERE)
