# -*- coding: utf-8 -*-
"""生成岩谱（LithoSpectra）的程序图标。

设计说明
--------
图标画的是一次分类：左边是影像，右边是岩性图。

暖象牙的底板上并排两块。左块是六枚方形像元，色调杂乱不纯——遥感影像本来的
样子。右块是一根平顶圆底的柱体，钻孔柱状图的轮廓，内部三层连续、干净，是判读
之后的岩性层序。离散对连续，杂乱对归类，中间那道空白就是模型做的事。

两侧严格按行对齐。左块第二行有一枚陶土色像元，右块第二层整层是同一个陶土色：
从混杂像元里检出一个信号，整层被判为该类。全图只有这一点色相，AI 不画成脑图
或芯片，画成"识别"这个动作本身。

平顶配圆底，沿用 design-system 里唯一被允许的非零圆角签名（主 CTA 的下沿）。
配色全部取自 web/src/generated/tokens.ts 的浅色主题令牌。图形没有细笔画，缩到 16 像素
时左右两点陶土仍然对得上。

直接运行本文件即可重新生成 assets/ 下的全部图标。
"""

import os
import shutil

from PIL import Image, ImageDraw

# =============================================================================
# 配置区
# =============================================================================

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")

# 输出的 PNG 尺寸
PNG_SIZES = (16, 32, 48, 64, 128, 256, 512, 1024)

# Windows 图标中打包的尺寸
ICO_SIZES = (16, 32, 48, 64, 128, 256)

# 渲染分辨率与超采样倍数（先大后缩，得到平滑边缘）
BASE = 1024
SUPERSAMPLE = 4

# 配色（全部对应 tokens.ts 的浅色主题令牌）
PAPER = "#F0EEE6"   # bg              底板：暖象牙，不用纯白
RIM   = "#8A8477"   # border_strong   底板发丝描边
INK   = "#141413"   # text            最重的一层
INK2  = "#5C574C"   # text_muted
INK3  = "#6B6558"   # text_placeholder
INK4  = "#8A8477"   # border_strong
SOFT  = "#A9A296"   # text_disabled
CLAY  = "#9A4A25"   # accent          被识别出的那一类，全图唯一色相

# 底板圆角半径与描边宽度（相对边长）
RADIUS = 0.225
RIM_WIDTH = 0.005

# 内容区上下边界（归一化坐标）
CONTENT_TOP, CONTENT_BOTTOM = 0.225, 0.775

# 行间与列间的缝隙宽度（相对边长）
GAP = 0.024

# 左块：影像像元的横向范围与列数
PIXELS = (0.130, 0.480)
PIXEL_COLUMNS = 2

# 左块像元色调，三行 × 两列，自上而下；索引 [1][1] 是被检出的那一枚
PIXEL_COLORS = ((INK3, INK2),
                (SOFT, CLAY),
                (INK4, INK3))

# 右块：岩性柱的横向范围与下沿圆角
COLUMN = (0.550, 0.870)
COLUMN_RADIUS = 0.105

# 右块三层的颜色，与左块三行一一对应
BAND_COLORS = (INK, CLAY, INK4)


# =============================================================================
# 绘制
# =============================================================================

def _rgba(color: str):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4)) + (255,)


def render(size: int) -> Image.Image:
    """渲染一枚指定边长的图标。"""
    s = size * SUPERSAMPLE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 底板
    inset = s * 0.02
    draw.rounded_rectangle([inset, inset, s - inset, s - inset],
                           radius=s * RADIUS, fill=_rgba(PAPER),
                           outline=_rgba(RIM), width=max(1, int(s * RIM_WIDTH)))

    top, bottom = CONTENT_TOP * s, CONTENT_BOTTOM * s
    height = bottom - top
    gap = max(1.0, s * GAP)
    row_height = (height - gap * 2) / 3

    # 左块：离散像元，行列都留缝
    px0, px1 = (v * s for v in PIXELS)
    cell = ((px1 - px0) - gap * (PIXEL_COLUMNS - 1)) / PIXEL_COLUMNS
    for r, row in enumerate(PIXEL_COLORS):
        y = top + r * (row_height + gap)
        for c, color in enumerate(row[:PIXEL_COLUMNS]):
            x = px0 + c * (cell + gap)
            draw.rectangle([x, y, x + cell, y + row_height], fill=_rgba(color))

    # 右块：连续柱体，层与层之间不留缝——离散对连续，正是这枚图标要说的事
    cx0, cx1 = (v * s for v in COLUMN)
    column = Image.new("L", (s, s), 0)
    ImageDraw.Draw(column).rounded_rectangle(
        [cx0, top, cx1, bottom], radius=s * COLUMN_RADIUS,
        corners=(False, False, True, True), fill=255)

    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    last = len(BAND_COLORS) - 1
    for r, color in enumerate(BAND_COLORS):
        # 分层界线落在左块行缝的中线上，两侧因此严格对齐；
        # 首末两层向柱外延伸，多出的部分由蒙版裁掉
        a = -s if r == 0 else top + r * (row_height + gap) - gap / 2
        b = 2 * s if r == last else top + r * (row_height + gap) + row_height + gap / 2
        ld.rectangle([0, a, s, b], fill=_rgba(color))
    img.paste(layer, (0, 0), column)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    out = os.path.normpath(OUT_DIR)
    os.makedirs(out, exist_ok=True)

    master = render(BASE)
    for size in PNG_SIZES:
        path = os.path.join(out, "logo_%d.png" % size)
        (master if size == BASE else render(size)).save(path)
        print("生成", os.path.basename(path))

    master.save(os.path.join(out, "logo.png"))
    master.save(os.path.join(out, "logo.ico"),
                sizes=[(n, n) for n in ICO_SIZES])
    print("生成 logo.png / logo.ico")

    # macOS 图标（有 iconutil 时才生成）
    iconset = os.path.join(out, "logo.iconset")
    os.makedirs(iconset, exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        render(size).save(os.path.join(iconset, "icon_%dx%d.png" % (size, size)))
        render(size * 2).save(os.path.join(iconset, "icon_%dx%d@2x.png" % (size, size)))
    if os.system("iconutil -c icns '%s' -o '%s' 2>/dev/null"
                 % (iconset, os.path.join(out, "logo.icns"))) == 0:
        print("生成 logo.icns")
    else:
        print("未找到 iconutil，跳过 .icns")
    # iconset 只是 iconutil 的中间产物，别留在 assets 里当垃圾
    shutil.rmtree(iconset, ignore_errors=True)


if __name__ == "__main__":
    main()
