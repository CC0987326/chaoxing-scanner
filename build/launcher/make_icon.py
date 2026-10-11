# -*- coding: utf-8 -*-
"""生成启动器图标 launcher.ico（放大镜 + 对勾）。

设计：青绿渐变底 → 玻璃感放大镜圈 → 圈里一个白勾。
  · 放大镜 = 「巡检 / 查未完成事项」；对勾 = 「已完成」；
  · 青绿底在深色任务栏上更醒目（暗底图标会糊进任务栏）；
  · 配色取自 webui.py 的主题：主色 #3fb6a8 系。

纯代码矢量绘制 + 4× 超采样抗锯齿，不依赖任何美术资源。
每个尺寸都按目标分辨率单独绘制再降采样，小图标（16/32px）不糊边。

用法： python build/launcher/make_icon.py [输出路径.ico]
"""
from __future__ import annotations

import pathlib
import sys

from PIL import Image, ImageDraw

# ---- 主题色 ----
TEAL_HI = (96, 216, 203, 255)    # 渐变顶（亮青）
TEAL_DK = (26, 102, 94, 255)     # 渐变底（深青）
GLASS = (9, 26, 28, 165)         # 镜片内的暗玻璃（让白勾和白圈分得开）
WHITE = (248, 252, 252, 255)
SHINE = (255, 255, 255, 46)      # 镜片上一道高光（可有可无，增强质感）

# ICO 内嵌尺寸（Windows 各场景按需选取）
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48),
         (64, 64), (128, 128), (256, 256)]

SS = 4                            # 超采样倍数
GRID = 256.0                      # 设计栅格边长（下列坐标都基于 256）


def _vgrad(w: int, h: int, top, bot) -> Image.Image:
    """竖直渐变，做成 w×h 的 RGBA 图。"""
    col = Image.new('RGBA', (1, h))
    for y in range(h):
        t = y / (h - 1) if h > 1 else 0
        col.putpixel((0, y), tuple(
            round(top[i] + (bot[i] - top[i]) * t) for i in range(4)))
    return col.resize((w, h))


def draw(size: int) -> Image.Image:
    """按目标边长绘制（内部超采样后降采样）。"""
    s = size / GRID                 # 256 栅格 → 目标像素的比例
    n = size * SS
    im = Image.new('RGBA', (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    def px(v):                      # 栅格坐标 → 超采样像素
        return v * s * SS

    def rr(vals):                   # 矩形
        return [px(v) for v in vals]

    def rad(v):                     # 半径/线宽
        return v * s * SS

    # 1) 圆角底板（青绿竖向渐变）
    x0, y0, x1, y1 = [round(px(v)) for v in (10, 10, 246, 246)]
    grad = _vgrad(x1 - x0, y1 - y0, TEAL_HI, TEAL_DK)
    mask = Image.new('L', (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, x1 - x0 - 1, y1 - y0 - 1), radius=round(rad(54)), fill=255)
    im.paste(grad, (x0, y0), mask)

    # 2) 镜片：暗玻璃填充 + 白圈
    lens = rr((58, 52, 182, 176))   # 圈外沿
    d.ellipse(lens, fill=GLASS)
    d.ellipse(lens, outline=WHITE, width=round(rad(15)))
    # 镜片左上角一道弧线高光，增加「玻璃」质感（小尺寸下自然被抹掉）
    d.arc(rr((74, 68, 150, 144)), start=200, end=300,
          fill=SHINE, width=round(rad(9)))

    # 3) 放大镜把手
    w_handle = round(rad(18))
    d.line(rr((166, 160, 202, 196)), fill=WHITE, width=w_handle)
    for (cx, cy) in ((166, 160), (202, 196)):     # 圆头
        d.ellipse(rr((cx - 9, cy - 9, cx + 9, cy + 9)), fill=WHITE)

    # 4) 圈里的对勾
    w_check = round(rad(14))
    d.line(rr((92, 114, 112, 136)), fill=WHITE, width=w_check, joint='curve')
    d.line(rr((112, 136, 150, 86)), fill=WHITE, width=w_check, joint='curve')
    for (cx, cy) in ((92, 114), (112, 136), (150, 86)):
        r = w_check / 2
        d.ellipse([px(cx) - r, px(cy) - r, px(cx) + r, px(cy) + r], fill=WHITE)

    return im.resize((size, size), Image.LANCZOS)


def _write_ico(path: 'pathlib.Path', images) -> None:
    """手写 ICO 容器，每帧用 PNG 存。

    为什么不用 PIL 的 `master.save(..., sizes=[...])`：那只会拿**一张**
    256px 图缩成各尺寸，缩小图（16/32）在细线上会糊。这里改成每个尺寸
    各自 `draw()` 原生渲染后再封进 ICO——小图标才够锐。
    （ICO 允许每帧是 PNG，Vista 起系统原生支持，Windows 7+ 无碍。）
    """
    import io
    import struct

    frames = []
    for size, im in images:                 # [(边长, RGBA图), ...] 升序
        buf = io.BytesIO()
        im.save(buf, format='PNG')
        frames.append((size, buf.getvalue()))

    n = len(frames)
    header = struct.pack('<HHH', 0, 1, n)    # reserved, type=icon, count
    offset = len(header) + 16 * n            # 图像数据起始偏移
    dir_entries = b''
    for size, data in frames:
        b = 0 if size >= 256 else size       # 256 在 ICO 里记作 0
        dir_entries += struct.pack(
            '<BBBBHHII', b, b, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    with open(path, 'wb') as f:
        f.write(header)
        f.write(dir_entries)
        for _, data in frames:
            f.write(data)


def main() -> int:
    out = (pathlib.Path(sys.argv[1]) if len(sys.argv) > 1
           else pathlib.Path(__file__).resolve().parent / 'launcher.ico')
    out.parent.mkdir(parents=True, exist_ok=True)
    # 每个尺寸各自原生绘制（不是把大图缩下去），再封进 ICO。
    images = [(size, draw(size)) for size, _ in SIZES]
    _write_ico(out, images)
    print('已生成图标：%s（%d 字节，%d 个尺寸，均原生渲染）'
          % (out, out.stat().st_size, len(images)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
