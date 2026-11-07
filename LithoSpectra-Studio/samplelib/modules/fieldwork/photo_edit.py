# -*- coding: utf-8 -*-
"""照片编辑：裁剪、旋转、翻转、拉直、调色、标注，按原图分辨率出图。

编辑器在页面上只改参数、看预览（缩小的底图 + 同一套变换），保存时把参数交到这里
按原图重新做一遍，结果作为一步调整压进步骤栈，原片不动。

处理顺序（与页面预览一致）
--------------------------
1. 翻转（``flip_x`` / ``flip_y``），再顺时针转 ``rot90``（0 / 90 / 180 / 270）。
   这一步之后的宽高叫「画幅」。
2. 拉直：绕画幅中心顺时针转 ``angle`` 度（±45），同时放大到正好填满画幅，四角不留空。
3. 调色：曝光、对比度、饱和度、色温合成一个 3×4 颜色矩阵，作用在 0~1 的 sRGB 值上。
   页面用 SVG 的 feColorMatrix（sRGB 空间）套同一个矩阵，所见即所得。
4. 标注：坐标是画幅的比例（0~1），线宽、字号是画幅短边的比例。
5. 裁剪：``crop`` 是画幅的比例 [x0, y0, x1, y1]。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw

from . import geomark

#: 调色各项的范围是 -100 ~ 100
COLOR_KEYS = ("exposure", "contrast", "saturation", "warmth")

#: 亮度系数（Rec. 709），饱和度围绕它缩放
_LUMA = np.array([0.2126, 0.7152, 0.0722])


# =============================================================================
# 参数
# =============================================================================

def normalise(spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    spec = dict(spec or {})
    rot = int(spec.get("rot90") or 0) % 360
    crop = spec.get("crop")
    if crop:
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in crop)
        crop = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
        if crop[2] - crop[0] < 1e-3 or crop[3] - crop[1] < 1e-3:
            crop = None
    color = {k: max(-100.0, min(100.0, float((spec.get("color") or {}).get(k) or 0.0))) for k in COLOR_KEYS}
    return {
        "rot90": rot - rot % 90,
        "flip_x": bool(spec.get("flip_x")),
        "flip_y": bool(spec.get("flip_y")),
        "angle": max(-45.0, min(45.0, float(spec.get("angle") or 0.0))),
        "crop": crop,
        "color": color,
        "annotations": list(spec.get("annotations") or []),
    }


def is_noop(spec: Dict[str, Any]) -> bool:
    s = normalise(spec)
    return (not s["rot90"] and not s["flip_x"] and not s["flip_y"] and abs(s["angle"]) < 1e-6
            and (s["crop"] is None or s["crop"] == [0.0, 0.0, 1.0, 1.0])
            and not any(s["color"].values()) and not s["annotations"])


def color_matrix(color: Dict[str, float]) -> np.ndarray:
    """调色参数 → 3×4 矩阵 M，新值 = M[:, :3] · rgb + M[:, 3]。页面上的 colorMatrix() 同一算法。"""
    e = 2.0 ** (float(color.get("exposure") or 0) / 100.0)            # ±1 档曝光
    c = 1.0 + float(color.get("contrast") or 0) / 100.0               # 0 ~ 2
    s = 1.0 + float(color.get("saturation") or 0) / 100.0             # 0 ~ 2
    w = float(color.get("warmth") or 0) / 100.0                       # 暖：红增蓝减

    def affine(lin: np.ndarray, off: np.ndarray) -> np.ndarray:
        m = np.eye(4)
        m[:3, :3] = lin
        m[:3, 3] = off
        return m

    exposure = affine(np.eye(3) * e, np.zeros(3))
    contrast = affine(np.eye(3) * c, np.full(3, 0.5 * (1.0 - c)))
    sat = (1.0 - s) * np.tile(_LUMA, (3, 1)) + s * np.eye(3)
    saturation = affine(sat, np.zeros(3))
    warmth = affine(np.diag([1.0 + 0.12 * w, 1.0, 1.0 - 0.12 * w]), np.zeros(3))
    m = warmth @ saturation @ contrast @ exposure
    return m[:3, :]


def straighten_zoom(w: float, h: float, angle: float) -> float:
    """转 ``angle`` 度后要放大多少倍才能填满原画幅。"""
    a = math.radians(abs(angle))
    ca, sa = math.cos(a), math.sin(a)
    return max((w * ca + h * sa) / w, (w * sa + h * ca) / h)


# =============================================================================
# 处理
# =============================================================================

def _orient(im: Image.Image, spec: Dict[str, Any]) -> Image.Image:
    if spec["flip_x"]:
        im = im.transpose(Image.FLIP_LEFT_RIGHT)
    if spec["flip_y"]:
        im = im.transpose(Image.FLIP_TOP_BOTTOM)
    # PIL 的 ROTATE_90 是逆时针；这里的 rot90 是顺时针
    turn = {90: Image.ROTATE_270, 180: Image.ROTATE_180, 270: Image.ROTATE_90}.get(spec["rot90"])
    return im.transpose(turn) if turn is not None else im


def _straighten(im: Image.Image, angle: float) -> Image.Image:
    if abs(angle) < 1e-6:
        return im
    w, h = im.size
    s = straighten_zoom(w, h, angle)
    a = math.radians(angle)
    ca, sa = math.cos(a), math.sin(a)
    cx, cy = w / 2.0, h / 2.0
    # 输出像素 p → 输入像素：先移到中心、缩小 s 倍、逆时针转回 angle（图像坐标 y 向下）
    m00, m01 = ca / s, sa / s
    m10, m11 = -sa / s, ca / s
    c0 = cx - m00 * cx - m01 * cy
    c1 = cy - m10 * cx - m11 * cy
    return im.transform((w, h), Image.AFFINE, (m00, m01, c0, m10, m11, c1), resample=Image.BICUBIC)


def _color(im: Image.Image, color: Dict[str, float]) -> Image.Image:
    if not any(color.values()):
        return im
    m = color_matrix(color).astype(np.float32)
    arr = np.asarray(im.convert("RGB"))
    out = np.empty_like(arr)
    step = 512                                        # 分行块算，千万像素的照片也不吃内存
    for y in range(0, arr.shape[0], step):
        block = arr[y:y + step].astype(np.float32) / 255.0
        res = block @ m[:, :3].T + m[:, 3]
        out[y:y + step] = np.clip(res * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return Image.fromarray(out, "RGB")


def _hex(color: str) -> Tuple[int, int, int]:
    c = (color or "#e53935").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return 229, 57, 53


def _arrow_head(p0, p1, size):
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    left = (p1[0] - size * math.cos(ang - 0.42), p1[1] - size * math.sin(ang - 0.42))
    right = (p1[0] - size * math.cos(ang + 0.42), p1[1] - size * math.sin(ang + 0.42))
    base = (p1[0] - size * 0.72 * math.cos(ang), p1[1] - size * 0.72 * math.sin(ang))
    return [p1, left, base, right]


def _annotate(im: Image.Image, items: Sequence[Dict[str, Any]]) -> Image.Image:
    if not items:
        return im
    w, h = im.size
    short = min(w, h)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for it in items:
        kind = it.get("type")
        pts = [(float(x) * w, float(y) * h) for x, y in (it.get("pts") or [])]
        rgb = _hex(str(it.get("color") or ""))
        ink = rgb + (255,)
        lw = max(1, int(round(float(it.get("width") or 0.006) * short)))
        if kind == "text":
            text = str(it.get("text") or "").strip()
            if not text or not pts:
                continue
            px = float(it.get("size") or 0.04) * short
            font = geomark.font_for(text, px, bold=True)
            dark = sum(rgb) < 3 * 110
            halo = (255, 255, 255, 230) if dark else (0, 0, 0, 200)
            d.text(pts[0], text, font=font, fill=ink, anchor="la",
                   stroke_width=max(1, int(px * 0.08)), stroke_fill=halo)
            continue
        if len(pts) < 2:
            continue
        (x0, y0), (x1, y1) = pts[0], pts[1]
        if kind in ("line", "arrow"):
            d.line([pts[0], pts[1]], fill=ink, width=lw)
            if kind == "arrow":
                d.polygon(_arrow_head(pts[0], pts[1], lw * 4.5), fill=ink)
        elif kind == "rect":
            d.rectangle([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], outline=ink, width=lw)
        elif kind == "ellipse":
            d.ellipse([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], outline=ink, width=lw)
    base = im.convert("RGBA")
    base.alpha_composite(layer)
    return base.convert("RGB")


def _crop(im: Image.Image, crop: Optional[List[float]]) -> Image.Image:
    if not crop:
        return im
    w, h = im.size
    box = (int(round(crop[0] * w)), int(round(crop[1] * h)),
           int(round(crop[2] * w)), int(round(crop[3] * h)))
    if box == (0, 0, w, h):
        return im
    return im.crop(box)


def apply(im: Image.Image, spec: Optional[Dict[str, Any]]) -> Image.Image:
    """按编辑参数处理一张（已按 EXIF 转正的）照片。"""
    s = normalise(spec)
    im = _orient(im.convert("RGB"), s)
    im = _straighten(im, s["angle"])
    im = _color(im, s["color"])
    im = _annotate(im, s["annotations"])
    return _crop(im, s["crop"])


__all__ = ["apply", "normalise", "is_noop", "color_matrix", "straighten_zoom", "COLOR_KEYS"]
