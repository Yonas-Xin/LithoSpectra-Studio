# -*- coding: utf-8 -*-
"""野外照片的地质水印：按需组合的组件（指北针、比例尺、镜向、文字），中英双语。

这些水印是给论文出图用的：只记参数（``spec``），不画进库里的任何文件，看图与导出
时由这里现画。编辑器的预览也走这里（后端出预览图），所见即导出所得。

坐标约定
--------
每个组件的 ``x`` / ``y`` 是它包围框左上角相对照片宽高的比例（0~1）；``size`` 是相对
照片短边的比例。比例尺的 ``px_per_mm`` 是原图上每毫米多少像素（在照片里的参照物上
标定得到），画到缩小的预览图上时按 ``ref_width`` 换算。

``spec`` 结构::

    {
      "version": 2,
      "lang": "en",              # en / zh：组件里的文字用哪种语言
      "theme": "light",          # light 白色组件加深色描边（深色照片）/ dark 深色组件加白描边
      "panel": false,            # 组件背后衬一块半透明圆角底板
      "items": [
        {"id": "n1", "type": "north", "x": 0.88, "y": 0.06, "size": 0.12,
         "design": "arrow", "rotation": 0},
        {"id": "s1", "type": "scale", "x": 0.05, "y": 0.90, "size": 0.035,
         "design": "bar", "px_per_mm": 12.5, "length": 10, "unit": "cm", "segments": 2},
        {"id": "v1", "type": "view", "x": 0.05, "y": 0.05, "size": 0.03,
         "azimuth": 45, "format": "bearing"},
        {"id": "t1", "type": "text", "x": 0.05, "y": 0.10, "size": 0.03, "lines": ["Outcrop A"]}
      ]
    }
"""

from __future__ import annotations

import copy
import glob
import math
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ... import config
from . import photoexif

# =============================================================================
# 配置
# =============================================================================

#: 西文字体（英文版的重中之重）：常规体、粗体。依次尝试
LATIN_FONTS: Sequence[Tuple[str, int, int]] = (
    # (路径, 常规体 index, 粗体 index)
    ("/System/Library/Fonts/Helvetica.ttc", 0, 1),
    ("/System/Library/Fonts/HelveticaNeue.ttc", 0, 1),
    ("C:/Windows/Fonts/arial.ttf", 0, 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0, 0),
)
LATIN_BOLD_FILES = {
    "C:/Windows/Fonts/arial.ttf": "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
}

#: 中文字体，依次尝试；苹方在新版 macOS 上放在系统资源目录里，按名字找
FONT_CANDIDATES = [
    *sorted(glob.glob("/System/Library/AssetsV2/com_apple_MobileAsset_Font*/*/AssetData/PingFang.ttc")),
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

#: ESRI North 指北针字体（ArcGIS 的指北针符号库），依次尝试。随包的那份在资源目录的
#: assets/fonts 下（源码运行时是仓库的 assets），不能按本文件的位置去找：打包后代码与
#: 资源不在同一棵目录树里，找不到时指北针会退回成手画的简易箭头
ESRI_NORTH_FONTS = [
    os.path.join(config.ASSETS_DIR, "fonts", "esri_north.ttf"),
    "C:/Windows/Fonts/esri_north.ttf",
]

#: 指北针样式 → (ESRI North 字符码, 是否在四周标 N E S W)
NORTH_GLYPHS: Dict[str, Tuple[int, bool]] = {
    "rose": (193, True),        # 粗环八角罗盘
    "star": (53, False),        # 八角星，自带 N E S W
    "arrow": (177, False),      # 半黑半白箭头，上方 N
    "circle": (111, False),     # 圆圈内箭头，上方 N
}

#: 罗盘四周方位字母用的衬线字体
SERIF_FONTS = [
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/System/Library/Fonts/Times.ttc",
    "C:/Windows/Fonts/times.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
]

#: 先画大再缩小，边缘才平滑
SUPERSAMPLE = 4

DEFAULT_ITEMS: Dict[str, Dict[str, Any]] = {
    # auto：按照片的方位角自动旋转（北在画面里的方向 = -方位角）；否则用 rotation
    # label：指北针下方标一行镜向（Viewing azimuth 230°），label_format 同镜向组件的 format
    "north": {"x": 0.84, "y": 0.04, "size": 0.14, "design": "rose", "rotation": 0.0, "auto": True,
              "label": True, "label_format": "azimuth"},
    "scale": {"x": 0.05, "y": 0.90, "size": 0.032, "design": "bar", "px_per_mm": 0.0,
              "length": 10.0, "unit": "cm", "segments": 2},
    "view": {"x": 0.05, "y": 0.05, "size": 0.03, "azimuth": None, "format": "bearing"},
    "text": {"x": 0.05, "y": 0.10, "size": 0.03, "lines": []},
}

UNIT_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0}

DIRECTION_NAMES = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
DIRECTION_NAMES_ZH_GEO = ["北", "北东", "东", "南东", "南", "南西", "西", "北西"]

# =============================================================================
# 字体
# =============================================================================

_cache: Dict[Tuple[str, int, bool], ImageFont.FreeTypeFont] = {}
_CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


def _latin(px: int, bold: bool) -> ImageFont.FreeTypeFont:
    for path, regular, bold_idx in LATIN_FONTS:
        if not os.path.isfile(path):
            continue
        try:
            if bold and path in LATIN_BOLD_FILES and os.path.isfile(LATIN_BOLD_FILES[path]):
                return ImageFont.truetype(LATIN_BOLD_FILES[path], px)
            return ImageFont.truetype(path, px, index=bold_idx if bold else regular)
        except OSError:
            continue
    return ImageFont.load_default(size=px)


def _cjk(px: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                continue
    return ImageFont.load_default(size=px)


def font_for(text: str, px: float, bold: bool = False) -> ImageFont.FreeTypeFont:
    """含中文的用中文字体，纯西文用 Helvetica / Arial。"""
    px = max(10, int(round(px)))     # Helvetica 常规体在 8 像素以下会出错
    key = ("cjk" if _CJK.search(text or "") else "latin", px, bold)
    if key not in _cache:
        _cache[key] = _cjk(px) if key[0] == "cjk" else _latin(px, bold)
    return _cache[key]


# =============================================================================
# 文字格式
# =============================================================================

def direction_name(azimuth: float) -> str:
    """罗盘八方位（中文）。"""
    return DIRECTION_NAMES[int(((azimuth % 360) + 22.5) // 45) % 8]


def bearing(azimuth: float) -> str:
    """象限方位角：45 → N45°E，135 → S45°E，正南北东西写成 N / S / E / W。"""
    a = round(azimuth % 360.0, 1) % 360.0
    exact = {0.0: "N", 90.0: "E", 180.0: "S", 270.0: "W"}
    if a in exact:
        return exact[a]
    if a < 90:
        return "N%s°E" % _deg(a)
    if a < 180:
        return "S%s°E" % _deg(180 - a)
    if a < 270:
        return "S%s°W" % _deg(a - 180)
    return "N%s°W" % _deg(360 - a)


def _deg(v: float) -> str:
    return ("%.1f" % v).rstrip("0").rstrip(".")


def view_label(azimuth: float, fmt: str, lang: str) -> str:
    """镜向标注。英文：Viewing azimuth 230° / Viewing direction S50°W；中文：镜向 230° / 镜向 南西50°。"""
    a = azimuth % 360.0
    if fmt == "azimuth":
        value = "%d°" % (int(round(a)) % 360)
        return ("Viewing azimuth " if lang == "en" else "镜向 ") + value
    if lang == "en":
        return "Viewing direction " + bearing(a)
    b = bearing(a)
    zh = b.replace("N", "北").replace("S", "南").replace("E", "东").replace("W", "西")
    # 北45°东 → 北东45°（地质记录的习惯写法）
    m = re.match(r"^([北南])([\d.]+)°([东西])$", zh)
    return "镜向 " + ("%s%s%s°" % (m.group(1), m.group(3), m.group(2)) if m else zh)


def nice_length(px_per_unit: float, target_px: float) -> float:
    """在 1-2-5 序列里挑一个画出来接近 ``target_px`` 的整数长度。"""
    if px_per_unit <= 0:
        return 1.0
    raw = target_px / px_per_unit
    exp = math.floor(math.log10(raw)) if raw > 0 else 0
    best = 1.0
    for e in (exp - 1, exp, exp + 1):
        for m in (1, 2, 5):
            v = m * 10 ** e
            if abs(v * px_per_unit - target_px) < abs(best * px_per_unit - target_px):
                best = v
    return best


# =============================================================================
# 参数
# =============================================================================

def merge_spec(spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """补齐缺省项；早先的格式（compass / scale / text 三件套）换算成组件列表。"""
    spec = copy.deepcopy(spec or {})
    if spec.get("version") != 2:
        spec = _from_v1(spec)
    out: Dict[str, Any] = {"version": 2, "lang": spec.get("lang") or "en",
                           "theme": spec.get("theme") or "light", "panel": bool(spec.get("panel")),
                           "items": []}
    for i, it in enumerate(spec.get("items") or []):
        kind = it.get("type")
        if kind not in DEFAULT_ITEMS:
            continue
        item = dict(DEFAULT_ITEMS[kind], **it)
        item.setdefault("id", "%s%d" % (kind[0], i + 1))
        out["items"].append(item)
    return out


def _from_v1(old: Dict[str, Any]) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    c = old.get("compass") or {}
    if c.get("show") and c.get("azimuth") is not None:
        items.append({"type": "north", "x": max(0.0, c.get("x", 0.88) - c.get("size", 0.16) / 2),
                      "y": max(0.0, c.get("y", 0.12) - c.get("size", 0.16) / 2),
                      "size": c.get("size", 0.16) * 0.8, "design": "arrow", "auto": False,
                      "rotation": -float(c["azimuth"])})
        if c.get("label", True):
            items.append({"type": "view", "azimuth": float(c["azimuth"]), "x": 0.05, "y": 0.05})
    s = old.get("scale") or {}
    if s.get("show") and s.get("px") and s.get("value"):
        unit = s.get("unit") or "cm"
        items.append({"type": "scale", "x": s.get("x", 0.06), "y": max(0.0, s.get("y", 0.92) - 0.06),
                      "px_per_mm": float(s["px"]) / (float(s["value"]) * UNIT_MM.get(unit, 10.0)),
                      "length": float(s["value"]), "unit": unit, "segments": s.get("segments", 2)})
    t = old.get("text") or {}
    if t.get("show") and t.get("lines"):
        items.append({"type": "text", "x": t.get("x", 0.03), "y": t.get("y", 0.04),
                      "size": t.get("size", 0.028), "lines": list(t["lines"])})
    theme = "dark" if (old.get("color") or "#ffffff").lower() in ("#000000", "#000") else "light"
    return {"version": 2, "lang": "zh", "theme": theme, "items": items}


# =============================================================================
# 画
# =============================================================================

class _Pen:
    """一个组件的画布：先画大 SUPERSAMPLE 倍，最后缩小贴回照片。"""

    def __init__(self, w: float, h: float, theme: str, panel: bool, pad: float) -> None:
        k = SUPERSAMPLE
        self.k = k
        self.pad = pad
        self.w, self.h = int(math.ceil((w + 2 * pad) * k)), int(math.ceil((h + 2 * pad) * k))
        self.im = Image.new("RGBA", (max(1, self.w), max(1, self.h)), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.im)
        dark = theme == "dark"
        self.ink = (17, 17, 17, 255) if dark else (255, 255, 255, 255)
        self.paper = (255, 255, 255, 255) if dark else (17, 17, 17, 255)
        # 衬底开着时不加描边：干净；没衬底时一圈细描边保证落在任何颜色上都看得清
        self.halo = None if panel else ((255, 255, 255, 235) if dark else (0, 0, 0, 200))
        if panel:
            fill = (255, 255, 255, 225) if dark else (20, 20, 20, 150)
            r = pad * k * 0.9
            self.d.rounded_rectangle([0, 0, self.w - 1, self.h - 1], radius=r, fill=fill)

    def p(self, x: float, y: float) -> Tuple[float, float]:
        """组件局部坐标（原图像素，不含内边距）→ 画布坐标。"""
        return ((x + self.pad) * self.k, (y + self.pad) * self.k)

    def poly(self, pts, fill, line: Optional[Tuple[int, int, int, int]] = None, width: float = 0,
             halo: bool = True) -> None:
        """多边形。``fill`` 为 None 只描边（ImageDraw 不做混合，透明填充会把底下擦掉）。"""
        q = [self.p(x, y) for x, y in pts]
        if halo and self.halo is not None and width:
            self.d.line(q + [q[0]], fill=self.halo, width=max(1, int(width * self.k * 3.2)), joint="curve")
        if fill is not None:
            self.d.polygon(q, fill=fill)
        if line is not None and width:
            self.d.line(q + [q[0]], fill=line, width=max(1, int(width * self.k)), joint="curve")

    def rect(self, x0, y0, x1, y1, fill, line=None, width: float = 0, halo: bool = True) -> None:
        self.poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], fill, line, width, halo)

    def line(self, pts, width: float) -> None:
        q = [self.p(x, y) for x, y in pts]
        if self.halo is not None:
            self.d.line(q, fill=self.halo, width=max(1, int(width * self.k * 2.6)), joint="curve")
        self.d.line(q, fill=self.ink, width=max(1, int(width * self.k)), joint="curve")

    def text(self, xy, s: str, px: float, anchor: str = "la", bold: bool = False) -> None:
        f = font_for(s, px * self.k, bold)
        x, y = self.p(*xy)
        stroke = max(1, int(px * self.k * 0.075)) if self.halo is not None else 0
        self.d.text((x, y), s, font=f, fill=self.ink, anchor=anchor,
                    stroke_width=stroke, stroke_fill=self.halo)

    def done(self) -> Image.Image:
        w = max(1, int(round(self.w / self.k)))
        h = max(1, int(round(self.h / self.k)))
        return self.im.resize((w, h), Image.LANCZOS)


def text_size(s: str, px: float, bold: bool = False) -> Tuple[float, float]:
    f = font_for(s, px * SUPERSAMPLE, bold)
    x0, y0, x1, y1 = f.getbbox(s)
    return (x1 - x0) / SUPERSAMPLE, (y1 - y0) / SUPERSAMPLE


def _rot(x: float, y: float, deg: float) -> Tuple[float, float]:
    a = math.radians(deg)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def _first_font(paths: Sequence[str], px: int) -> Optional[ImageFont.FreeTypeFont]:
    for path in paths:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                continue
    return None


def north_rotation(it: Dict[str, Any], azimuth: Optional[float]) -> float:
    """指北针在画面里顺时针转多少度。自动模式下取照片方位角的相反数：相机朝东（90°）
    拍时，北在画面左侧。"""
    if it.get("auto", True) and azimuth is not None:
        return -float(azimuth) % 360.0
    return float(it.get("rotation") or 0.0)


def _glyph_layer(path: str, px: int, ch: str, side: int, ink, paper, halo, grow: int) -> Image.Image:
    """ESRI 字形画成黑白两色：字形本身只有一种颜色，镂空处会透出照片，把被字形围住的
    镂空填成衬色（与 ArcGIS 出图一致），外轮廓再加一圈描边。结果按参数缓存。"""
    key = (path, px, ch, side, ink, paper, halo, grow)
    hit = _glyph_cache.get(key)
    if hit is not None:
        return hit.copy()
    import cv2
    import numpy as np

    font = ImageFont.truetype(path, px)
    gx0, gy0, gx1, gy1 = font.getbbox(ch)
    ink_mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(ink_mask).text((side / 2 - (gx0 + gx1) / 2, side / 2 - (gy0 + gy1) / 2), ch, font=font, fill=255)
    m = np.asarray(ink_mask)
    region = (m > 0).astype(np.uint8)
    flood = region.copy()
    cv2.floodFill(flood, np.zeros((side + 2, side + 2), np.uint8), (0, 0), 2)
    holes = (flood == 0).astype(np.uint8) * 255
    holes = cv2.dilate(holes, np.ones((3, 3), np.uint8))
    shape = (flood != 2).astype(np.uint8) * 255
    layer = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    if halo is not None:
        grown = cv2.dilate(shape, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow, grow)))
        layer.paste(Image.new("RGBA", (side, side), halo), (0, 0), Image.fromarray(grown))
    layer.paste(Image.new("RGBA", (side, side), paper), (0, 0), Image.fromarray(holes))
    layer.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", (side, side), ink[:3]).split(), ink_mask)))
    if len(_glyph_cache) > 32:
        _glyph_cache.clear()
    _glyph_cache[key] = layer
    return layer.copy()


_glyph_cache: Dict[tuple, Image.Image] = {}


def _north(it: Dict[str, Any], short: float, theme: str, panel: bool, rot: float = 0.0,
           azimuth: Optional[float] = None, lang: str = "en") -> Image.Image:
    """指北针，用 ESRI North 字体里的符号；没有这个字体时退回自绘的箭头与罗盘。
    ``label`` 开着且知道照片方位角时，符号下方居中标一行镜向。"""
    glyph = NORTH_GLYPHS.get(it.get("design") or "rose")
    k = SUPERSAMPLE
    H = it["size"] * short
    probe = _first_font(ESRI_NORTH_FONTS, 200) if glyph else None
    if probe is None:
        return _north_drawn(dict(it, rotation=rot), short, theme, panel)
    code, letters = glyph
    ch = chr(code)
    pad = H * 0.08
    label = (view_label(float(azimuth), it.get("label_format") or "azimuth", lang)
             if it.get("label", True) and azimuth is not None else "")
    fs = H * 0.115                               # 标注字号：随指北针大小走
    gap = H * 0.07
    tw = text_size(label, fs)[0] if label else 0.0
    W = max(H, tw)
    pen = _Pen(W, H + (gap + fs * 1.22 if label else 0.0), theme, panel, pad)
    ox = (W - H) / 2                              # 符号在画布里水平居中
    D = H * (0.66 if letters else 1.0)           # 符号外接正方形边长
    x0, y0, x1, y1 = probe.getbbox(ch)
    px = max(8, int(round(200 * D * k / max(x1 - x0, y1 - y0))))
    side = int(math.ceil(D * k * 1.5))
    layer = _glyph_layer(probe.path, px, ch, side, pen.ink, pen.paper, pen.halo,
                         max(3, int(round(D * k * 0.014)) * 2 + 1))
    if rot:
        layer = layer.rotate(-rot, resample=Image.BICUBIC)
    cx, cy = pen.p(ox + H / 2, H / 2)
    pen.im.alpha_composite(layer, (int(round(cx - side / 2)), int(round(cy - side / 2))))
    if letters:
        L = H * 0.16
        lf = _first_font(SERIF_FONTS, max(8, int(round(L * k)))) or font_for("N", L * k)
        r = D / 2 + L * 0.66
        for letter, ang in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            dx, dy = _rot(0, -r, ang + rot)
            px, py = pen.p(ox + H / 2 + dx, H / 2 + dy)
            pen.d.text((px, py), letter, font=lf, fill=pen.ink, anchor="mm",
                       stroke_width=max(1, int(L * k * 0.07)) if pen.halo is not None else 0,
                       stroke_fill=pen.halo)
    if label:
        pen.text((W / 2, H + gap + fs * 0.95), label, fs, anchor="ms")
    return pen.done()


def _north_drawn(it: Dict[str, Any], short: float, theme: str, panel: bool) -> Image.Image:
    """自绘指北针。``arrow``：两半一深一浅的箭头，箭头上方粗体 N（科研制图最常见的样式）；
    ``rose``：四角星罗盘，北角两半填色，外加一圈细环。N 始终正立，跟着箭头方向走。

    画布取正方形，箭头怎么转都不出界。"""
    H = it["size"] * short
    rot = float(it.get("rotation") or 0.0)
    lw = max(1.0, H * 0.016)
    letter = H * 0.24
    R = (H - letter * 1.3) / 2                     # 图形半径（中心到箭尖）
    pad = H * 0.10
    pen = _Pen(H, H, theme, panel, pad)
    cx, cy = H / 2, H / 2 + letter * 0.62
    rotp = lambda x, y: _rot(x, y, rot)
    T = lambda x, y: (cx + rotp(x, y)[0], cy + rotp(x, y)[1])
    ink, paper = pen.ink, pen.paper
    if it.get("design") == "rose":
        r_in = R * 0.20
        ring = [T(R * 0.78 * math.sin(math.radians(a)), -R * 0.78 * math.cos(math.radians(a))) for a in range(0, 361, 4)]
        pen.line(ring, lw)
        for a in (90, 180, 270):
            tip = T(R * 0.92 * math.sin(math.radians(a)), -R * 0.92 * math.cos(math.radians(a)))
            l1 = T(r_in * math.sin(math.radians(a - 90)), -r_in * math.cos(math.radians(a - 90)))
            l2 = T(r_in * math.sin(math.radians(a + 90)), -r_in * math.cos(math.radians(a + 90)))
            pen.poly([tip, l1, T(0, 0)], paper, ink, lw)
            pen.poly([tip, T(0, 0), l2], ink, ink, lw)
        tip = T(0, -R)
        pen.poly([tip, T(-r_in * 1.2, 0), T(0, 0)], ink, ink, lw)
        pen.poly([tip, T(0, 0), T(r_in * 1.2, 0)], paper, ink, lw)
    else:
        half = R * 0.52
        tip, base_l, base_r, notch = (0, -R), (-half, R * 0.92), (half, R * 0.92), (0, R * 0.42)
        pen.poly([T(*tip), T(*base_l), T(*notch)], ink, ink, lw)
        pen.poly([T(*tip), T(*notch), T(*base_r)], paper, ink, lw)
    # N 正立，放在箭尖外侧
    nx, ny = rotp(0, -R - letter * 0.62)
    pen.text((cx + nx, cy + ny), "N", letter, anchor="mm", bold=True)
    return pen.done()


def _scale(it: Dict[str, Any], short: float, theme: str, panel: bool, factor: float, lang: str) -> Optional[Image.Image]:
    """比例尺。``bar``：黑白相间分段条，段界下方标数字，末端带单位（0  5  10 cm）；
    ``line``：一条细线两端短竖，上方居中标总长。"""
    ppm = float(it.get("px_per_mm") or 0.0) * factor
    unit = it.get("unit") or "cm"
    length = float(it.get("length") or 0.0)
    if ppm <= 0 or length <= 0:
        return None
    L = length * UNIT_MM.get(unit, 10.0) * ppm
    bh = it["size"] * short * 0.34                 # 条高
    fs = it["size"] * short * 0.66                 # 字号
    lw = max(1.0, bh * 0.10)
    segs = max(1, min(10, int(it.get("segments") or 1)))
    pad = fs * 0.55
    if it.get("design") == "line":
        label = "%s %s" % (_num(length), unit)
        tw, th = text_size(label, fs)
        W, H = max(L, tw), fs * 1.25 + bh
        pen = _Pen(W, H, theme, panel, pad)
        x0 = (W - L) / 2
        y = H - lw
        pen.line([(x0, y - bh), (x0, y), (x0 + L, y), (x0 + L, y - bh)], lw * 1.4)
        pen.text((W / 2, fs * 1.05), label, fs, anchor="ms")
        return pen.done()
    # 数字居中对准段界；单位跟在最后一个数字后面，不参与对齐
    x0 = text_size("0", fs)[0] / 2 + lw
    last_num = _num(length)
    unit_w = text_size(last_num + " " + unit, fs)[0] - text_size(last_num, fs)[0]
    W = x0 + L + text_size(last_num, fs)[0] / 2 + unit_w + lw
    H = bh + fs * 1.5
    pen = _Pen(W, H, theme, panel, pad)
    seg = L / segs
    pen.rect(x0, 0, x0 + L, bh, None, None, lw)                       # 整条外侧的描边（衬托用）
    for i in range(segs):
        pen.rect(x0 + i * seg, 0, x0 + (i + 1) * seg, bh,
                 pen.ink if i % 2 == 0 else pen.paper, None, 0, halo=False)
    pen.rect(x0, 0, x0 + L, bh, None, pen.ink, lw, halo=False)        # 外框
    for i in range(segs + 1):
        value = length * i / segs
        x = x0 + i * seg
        y = bh + fs * 1.28
        if i == segs:
            pen.text((x - text_size(_num(value), fs)[0] / 2, y), "%s %s" % (_num(value), unit), fs, anchor="ls")
        else:
            pen.text((x, y), _num(value), fs, anchor="ms")
    return pen.done()


def _num(v: float) -> str:
    return ("%.2f" % v).rstrip("0").rstrip(".")


def _label(it: Dict[str, Any], short: float, theme: str, panel: bool, lines: List[str]) -> Optional[Image.Image]:
    lines = [str(x) for x in lines if str(x).strip()]
    if not lines:
        return None
    fs = it["size"] * short
    sizes = [text_size(s, fs) for s in lines]
    W = max(w for w, _ in sizes)
    H = fs * 1.3 * len(lines)
    pen = _Pen(W, H, theme, panel, fs * 0.45)
    for i, s in enumerate(lines):
        pen.text((0, fs * 1.3 * i + fs * 1.02), s, fs, anchor="ls")
    return pen.done()


def render_items(size: Tuple[int, int], spec: Optional[Dict[str, Any]],
                 ref_width: Optional[int] = None, azimuth: Optional[float] = None
                 ) -> List[Tuple[Dict[str, Any], Image.Image, Tuple[int, int]]]:
    """画出每个组件：(组件参数, RGBA 小图, 左上角像素位置)。``azimuth`` 是照片的方位角：
    自动旋转的指北针、没单独填镜向的镜向组件都用它。"""
    spec = merge_spec(spec)
    w, h = size
    short = min(w, h)
    factor = w / float(ref_width) if ref_width else 1.0
    theme, panel, lang = spec["theme"], spec["panel"], spec["lang"]
    out = []
    for it in spec["items"]:
        kind = it["type"]
        if kind == "north":
            patch = _north(it, short, theme, panel, north_rotation(it, azimuth), azimuth, lang)
        elif kind == "scale":
            patch = _scale(it, short, theme, panel, factor, lang)
        elif kind == "view":
            az = it.get("azimuth") if it.get("azimuth") is not None else azimuth
            patch = None if az is None else _label(
                it, short, theme, panel, [view_label(float(az), it.get("format") or "bearing", lang)])
        else:
            patch = _label(it, short, theme, panel, it.get("lines") or [])
        if patch is None:
            continue
        out.append((it, patch, place(it, patch.size, (w, h))))
    return out


def place(it: Dict[str, Any], patch: Tuple[int, int], size: Tuple[int, int]) -> Tuple[int, int]:
    """组件左上角的像素位置。

    有 ``anchor`` 时按「离最近两条边多远」摆：``{"h": "left"|"right", "dx": 0.04,
    "v": "top"|"bottom", "dy": 0.04}``，距离以照片短边为单位。这样同一套默认摆放
    用到宽窄、横竖不同的照片上，组件都贴在同一个角、离边一样远、大小比例一致。
    没有 ``anchor`` 时用 ``x`` / ``y``（包围框左上角相对宽高的比例）。"""
    w, h = size
    pw, ph = patch
    a = it.get("anchor")
    if isinstance(a, dict) and a.get("h") and a.get("v"):
        short = min(w, h)
        dx, dy = float(a.get("dx") or 0.0) * short, float(a.get("dy") or 0.0) * short
        x = dx if a["h"] == "left" else w - dx - pw
        y = dy if a["v"] == "top" else h - dy - ph
        return int(round(max(0, min(x, w - pw)))), int(round(max(0, min(y, h - ph))))
    return int(round(it["x"] * w)), int(round(it["y"] * h))


def render(im: Image.Image, spec: Optional[Dict[str, Any]], ref_width: Optional[int] = None,
           azimuth: Optional[float] = None) -> Image.Image:
    """在照片上画地质水印，返回新图（RGB）。``ref_width`` 是原图宽度：画在缩小的预览图上
    时传进来，比例尺的像素标定按它换算。"""
    base = im.convert("RGBA")
    for _it, patch, (x, y) in render_items(base.size, spec, ref_width, azimuth):
        base.alpha_composite(patch, (max(0, min(x, base.width - 1)), max(0, min(y, base.height - 1))))
    return base.convert("RGB")


def boxes(size: Tuple[int, int], spec: Optional[Dict[str, Any]],
          ref_width: Optional[int] = None, azimuth: Optional[float] = None) -> Dict[str, List[float]]:
    """每个组件的包围框（相对宽高的比例 x0, y0, x1, y1），编辑器拿它做拖动与选中。"""
    w, h = size
    return {it["id"]: [x / w, y / h, (x + p.width) / w, (y + p.height) / h]
            for it, p, (x, y) in render_items(size, spec, ref_width, azimuth)}


def apply(data: bytes, spec: Optional[Dict[str, Any]], azimuth: Optional[float] = None) -> bytes:
    """在照片上画水印，返回新的 JPEG（EXIF 原样带上）。"""
    im = photoexif.open_upright(data)
    return photoexif.encode_jpeg(render(im, spec, azimuth=azimuth), src=data)
