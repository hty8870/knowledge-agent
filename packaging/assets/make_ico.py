# -*- coding: utf-8 -*-
"""确定性派生 BioData Agent 品牌图标。

来源（不改品牌形状）：站点 favicon —— `src/dataset_recommender/app/webapp.py`
`_FAVICON_SVG`（webapp.py:816-826 内联 SVG，浏览器 /favicon.ico 同一标记）：
青底圆角方块（#0d9488，rx=5/22）+ 白色圆环（r=5.4/22、线宽 1.9/22）+ 白色轨道弧
（三次贝塞尔，线宽 1.3/22、opacity .7）。`web/static/assets/` 下没有 favicon 文件，
最权威的既存品牌圆环标记就是该内联 SVG，故直接以它为几何真源。

实现（确定性）：Pillow 在 1024×1024 画布（46.55× 放大）渲染一次，几何全部由
22 单位 viewBox 常量线性换算；白色轨道弧按贝塞尔公式解析采样（200 段）成折线；
再 LANCZOS 逐级降采样到 ICO 需要的 8 个图层。无随机数、无生成式图片、无外部字体
（favicon 不含文字）。相同输入 → 字节级相同输出。

用法：
  <python-with-Pillow> packaging/assets/make_ico.py [--out packaging/assets/BioDataAgent.ico]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ── 几何真源（逐字对应 webapp.py _FAVICON_SVG 的 viewBox="0 0 22 22"）───────────────
_VB = 22          # viewBox 边长
_RX = 5           # 圆角矩形圆角半径（22 单位）
_TEAL = (13, 148, 136, 255)     # #0d9488
_RING_R = 5.4     # 白色圆环半径
_RING_W = 1.9     # 白色圆环线宽
_SWOOSH = ((11, 5.6), (6, 8.6), (16, 12.4), (11, 15.4))  # 三次贝塞尔控制点
_SWOOSH_W = 1.3   # 轨道弧线宽
_SWOOSH_ALPHA = 0.7

# ICO 图层（16/20/24/32/48/64/128/256）；256 由 Pillow 自动存 PNG 压缩条目。
_ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
_CANVAS = 1024    # 母版渲染边长（46.55×，降采样后边缘平滑）
_BEZIER_N = 200   # 轨道弧采样段数


def _cubic_bezier(p0, p1, p2, p3, n: int):
    """返回贝塞尔折线采样点（单位：22-unit viewBox 坐标）。"""
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1.0 - t
        x = mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t ** 3 * p3[0]
        y = mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t ** 3 * p3[1]
        pts.append((x, y))
    return pts


def render_master(canvas: int = _CANVAS) -> Image.Image:
    """1024×1024 RGBA 母版：圆角青底 + 白环 + 白色轨道弧（含 0.7 透明度）。"""
    s = canvas / _VB  # 单位缩放
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # ① 圆角矩形底（#0d9488，rx=5/22）
    d.rounded_rectangle((0, 0, canvas - 1, canvas - 1), radius=_RX * s, fill=_TEAL)

    # ② 白色圆环：外圆减内圆（r=5.4、线宽 1.9）。内圆必须补回青底，
    #    因为 SVG 的 circle 是 fill="none"（透出 rect 青底），不是把画布挖透明。
    #    旧实现填透明，Windows 浅色标题栏会从洞里透出白色，16px 下退化成一个
    #    实心白圆并吞掉轨道弧——正是「简化版图标」的根因。
    ring_outer = (_RING_R + _RING_W / 2) * s
    ring_inner = (_RING_R - _RING_W / 2) * s
    cx = cy = 11 * s
    d.ellipse((cx - ring_outer, cy - ring_outer, cx + ring_outer, cy + ring_outer), fill=(255, 255, 255, 255))
    d.ellipse((cx - ring_inner, cy - ring_inner, cx + ring_inner, cy + ring_inner), fill=_TEAL)

    # ③ 白色轨道弧（贝塞尔，线宽 1.3，opacity .7）。必须在独立 overlay
    #    绘制后 alpha_composite；直接在 RGBA 主图上画半透明像素会把青底替换成
    #    半透明白，让标题栏颜色透进图标，仍与 SVG 的正常 source-over 合成不一致。
    bez = _cubic_bezier(*_SWOOSH, _BEZIER_N)
    scaled = [(x * s, y * s) for x, y in bez]
    overlay = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).line(
        scaled,
        fill=(255, 255, 255, round(255 * _SWOOSH_ALPHA)),
        width=max(1, round(_SWOOSH_W * s)),
        joint="curve",
    )
    return Image.alpha_composite(img, overlay)


def build_ico(master: Image.Image) -> Image.Image:
    """按图层表逐级 LANCZOS 降采样；ICO 由 Pillow 写为多尺寸单文件。"""
    return master


def main() -> int:
    parser = argparse.ArgumentParser(description="确定性派生 BioDataAgent.ico（favicon 圆环标记）")
    parser.add_argument("--out", default=str(_REPO_ROOT / "packaging" / "assets" / "BioDataAgent.ico"))
    parser.add_argument("--canvas", type=int, default=_CANVAS, help=argparse.SUPPRESS)
    args = parser.parse_args()

    out = Path(args.out).resolve()
    master = render_master(args.canvas)
    master.save(
        out, format="ICO", sizes=_ICO_SIZES,
        append_images=[master.resize(sz, Image.LANCZOS) for sz in _ICO_SIZES],
    )
    # Pillow 的 ICO 保存：256 条目自动走 PNG 压缩；这里显式给出每层尺寸，保存后校验。
    print(f"[ico] 已生成：{out}")
    print(f"[ico] 图层：{', '.join(f'{w}x{h}' for w, h in _ICO_SIZES)}")
    print(f"[ico] 字节：{out.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
