# -*- coding: utf-8 -*-
"""奥维相机水印：识别、抽取信息、去除。

奥维相机拍的照片左下角压着几行白字（带细黑描边）::

    经纬度： 95.79082896, 36.31076126
    海拔：   3016.5米
    朝向：   东南 138°

照片 EXIF 里通常已有经纬度与海拔，**唯独没有朝向**——这几行字里最值钱的就是它。
流程是：

1. 用 macOS 自带的 Vision 文字识别（``ocrmac``）读左下角，先粗读定位水印块，再按
   原分辨率裁出水印块细读一遍，数字准得多；
2. 解析出经纬度、海拔、朝向。朝向的度数与方位字（东南）必须落在同一个 45° 扇区，
   对不上就标记为待确认，不往 EXIF 里写；经纬度与 EXIF 里的 GPS 比对，不一致以 EXIF
   为准（水印文字是 OCR 来的，会认错个别数字）；
3. 在识别出的文字框里取白色笔画、向外膨胀盖住描边，得到掩膜，交给
   :mod:`.inpaint` 修补。
"""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageFilter

from . import inpaint as _inpaint
from . import photoexif

# =============================================================================
# 配置
# =============================================================================

#: 粗读的范围：照片下方这一比例、左侧这一比例
SEARCH_BOTTOM = 0.32
SEARCH_LEFT = 0.75
#: 粗读前把搜索区缩到这个宽度以内（Vision 对大图很慢，缩小后文字仍足够大）
COARSE_WIDTH = 1600
#: 水印行的关键字。识别出其中任一个就认为有奥维水印
KEYS = ("经纬度", "海拔", "朝向", "方位", "时间", "地址", "备注", "精度", "速度", "坐标")
#: 白色笔画的判定：三通道最小值不低于它、通道差不超过 WHITE_SPREAD。水印是纯白字，
#: 岩石里最亮的长石颗粒也很少超过 225（实测花岗岩面上不到万分之五）
WHITE_MIN = 228
WHITE_SPREAD = 40
#: 掩膜相对字高的膨胀比例（盖住黑色描边与 JPEG 振铃）
DILATE = 0.12
#: 经纬度与 EXIF 相差超过它（度）就认为水印读错了
GPS_TOLERANCE = 2e-4

DIRECTIONS = {"北": 0, "东北": 45, "东": 90, "东南": 135, "南": 180, "西南": 225, "西": 270, "西北": 315}


@dataclass
class TextItem:
    text: str
    box: Tuple[int, int, int, int]       # 原图像素坐标 x0, y0, x1, y1
    conf: float


@dataclass
class OvitalMark:
    found: bool = False
    items: List[TextItem] = field(default_factory=list)
    box: Optional[Tuple[int, int, int, int]] = None
    text: str = ""
    lon: Optional[float] = None
    lat: Optional[float] = None
    altitude: Optional[float] = None
    azimuth: Optional[float] = None
    direction: str = ""
    azimuth_ok: bool = False             # 度数与方位字一致
    notes: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["items"] = [{"text": i.text, "box": list(i.box)} for i in self.items]
        return d


# =============================================================================
# 识别
# =============================================================================

def ocr_available() -> bool:
    try:
        import ocrmac  # noqa: F401
        return True
    except ImportError:
        return False


def _ocr(img: Image.Image) -> List[Tuple[str, float, Tuple[float, float, float, float]]]:
    """返回 (文字, 置信度, 归一化框 x, y, w, h，原点在左下)。"""
    from ocrmac import ocrmac
    return ocrmac.OCR(img, language_preference=["zh-Hans", "en-US"],
                      recognition_level="accurate").recognize()


def _to_px(box, ox: int, oy: int, w: int, h: int, scale: float) -> Tuple[int, int, int, int]:
    x, y, bw, bh = box
    x0 = ox + x * w / scale
    x1 = ox + (x + bw) * w / scale
    y1 = oy + (1 - y) * h / scale
    y0 = oy + (1 - y - bh) * h / scale
    return int(x0), int(y0), int(round(x1)), int(round(y1))


def _read(im: Image.Image, region: Tuple[int, int, int, int], max_width: int) -> List[TextItem]:
    x0, y0, x1, y1 = region
    crop = im.crop(region)
    scale = min(1.0, max_width / max(1, crop.width))
    if scale < 1.0:
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.LANCZOS)
    out = []
    for text, conf, box in _ocr(crop):
        out.append(TextItem(text=text.strip(), conf=float(conf),
                            box=_to_px(box, x0, y0, crop.width, crop.height, scale)))
    return out


def _is_key(text: str) -> bool:
    return any(k in text for k in KEYS)


def _block(items: List[TextItem]) -> Tuple[List[TextItem], Optional[Tuple[int, int, int, int]]]:
    """关键字行所在的那一块：关键字行的纵向范围内、关键字右侧的值都算进来。"""
    keys = [i for i in items if _is_key(i.text)]
    if not keys:
        return [], None
    line_h = float(np.median([i.box[3] - i.box[1] for i in keys]))
    top = min(i.box[1] for i in keys) - 0.3 * line_h
    bottom = max(i.box[3] for i in keys) + 0.3 * line_h
    left = min(i.box[0] for i in keys) - 0.3 * line_h
    members = [i for i in items if i.box[1] >= top - 0.5 * line_h and i.box[3] <= bottom + 0.5 * line_h
               and i.box[0] >= left - line_h]
    box = (min(i.box[0] for i in members), min(i.box[1] for i in members),
           max(i.box[2] for i in members), max(i.box[3] for i in members))
    return members, box


def _lines(items: List[TextItem]) -> List[str]:
    """按行拼回文字（同一行的几段从左到右连起来）。"""
    rows: List[List[TextItem]] = []
    for it in sorted(items, key=lambda i: (i.box[1] + i.box[3]) / 2):
        cy = (it.box[1] + it.box[3]) / 2
        h = it.box[3] - it.box[1]
        if rows:
            last = rows[-1][0]
            if abs((last.box[1] + last.box[3]) / 2 - cy) < 0.5 * h:
                rows[-1].append(it)
                continue
        rows.append([it])
    return [" ".join(i.text for i in sorted(r, key=lambda i: i.box[0])) for r in rows]


_NUM = r"(-?\d{1,3}(?:[.．]\d+)?)"


def _parse(lines: List[str], mark: OvitalMark) -> None:
    text = "\n".join(lines)
    mark.text = text
    norm = text.replace("，", ",").replace("．", ".").replace("：", ":")
    m = re.search(r"(?:经纬度|坐标)[^\d-]*" + _NUM + r"\s*[,， ]\s*" + _NUM, norm)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        # 奥维是经度在前；万一倒过来，按取值范围纠正
        lon, lat = (a, b) if abs(b) <= 90 else (b, a)
        if abs(lat) <= 90 and abs(lon) <= 180:
            mark.lon, mark.lat = lon, lat
    m = re.search(r"海拔[^\d-]*(-?\d+(?:\.\d+)?)", norm)
    if m:
        mark.altitude = float(m.group(1))
    m = re.search(r"(?:朝向|方位)[^\n]*?(东北|东南|西北|西南|正北|正南|正东|正西|北|南|东|西)?\s*(\d{1,3}(?:\.\d+)?)\s*[°度º]",
                  norm)
    if m:
        deg = float(m.group(2))
        word = (m.group(1) or "").replace("正", "")
        if deg < 360:
            mark.azimuth = deg
            mark.direction = word
            if word in DIRECTIONS:
                diff = abs((deg - DIRECTIONS[word] + 180) % 360 - 180)
                mark.azimuth_ok = diff <= 22.5 + 1e-6
                if not mark.azimuth_ok:
                    mark.notes.append("朝向度数 %.0f° 与方位「%s」不一致，可能识别有误，请核对" % (deg, word))
            else:
                mark.notes.append("未识别出方位文字，朝向 %.0f° 请核对" % deg)


def detect(im: Image.Image) -> OvitalMark:
    """在转正后的照片上找奥维水印并解析。没有水印时 ``found`` 为假。"""
    mark = OvitalMark()
    if not ocr_available():
        mark.notes.append("未安装 ocrmac，无法识别水印文字")
        return mark
    w, h = im.size
    region = (0, int(h * (1 - SEARCH_BOTTOM)), int(w * SEARCH_LEFT), h)
    coarse = _read(im, region, COARSE_WIDTH)
    members, box = _block(coarse)
    if not members:
        return mark
    # 细读：按原分辨率裁出水印块（四周留半行），数字认得准得多
    line_h = int(np.median([i.box[3] - i.box[1] for i in members]))
    fine_region = (max(0, box[0] - line_h), max(0, box[1] - line_h // 2),
                   min(w, box[2] + 2 * line_h), min(h, box[3] + line_h // 2))
    fine = _read(im, fine_region, 2400)
    fine_members, fine_box = _block(fine)
    if fine_members:
        members, box = fine_members, fine_box
    mark.found = True
    mark.items = members
    mark.box = box
    _parse(_lines(members), mark)
    return mark


def check_against_exif(mark: OvitalMark, info: Dict[str, Any]) -> None:
    """拿 EXIF 的 GPS 核对水印里的经纬度。"""
    if mark.lon is None or info.get("lon") is None:
        return
    if abs(mark.lon - info["lon"]) > GPS_TOLERANCE or abs(mark.lat - info["lat"]) > GPS_TOLERANCE:
        mark.notes.append("水印经纬度 %.6f, %.6f 与照片 GPS %.6f, %.6f 不一致，以照片 GPS 为准"
                          % (mark.lon, mark.lat, info["lon"], info["lat"]))


# =============================================================================
# 掩膜与去除
# =============================================================================

def build_mask(im: Image.Image, mark: OvitalMark, mode: str = "strokes") -> np.ndarray:
    """``strokes``：只取文字笔画（推荐，修补面积最小）；``boxes``：整个文字框。"""
    w, h = im.size
    mask = np.zeros((h, w), dtype=bool)
    if not mark.items:
        return mask
    line_h = int(np.median([i.box[3] - i.box[1] for i in mark.items]))
    pad = max(2, int(line_h * 0.25))
    rgb = np.asarray(im.convert("RGB"))
    for it in mark.items:
        x0, y0 = max(0, it.box[0] - pad), max(0, it.box[1] - pad)
        x1, y1 = min(w, it.box[2] + pad), min(h, it.box[3] + pad)
        if mode == "boxes":
            mask[y0:y1, x0:x1] = True
            continue
        sub = rgb[y0:y1, x0:x1].astype(np.int16)
        lo, hi = sub.min(axis=2), sub.max(axis=2)
        mask[y0:y1, x0:x1] |= (lo >= WHITE_MIN) & ((hi - lo) <= WHITE_SPREAD)
    if mode == "strokes":
        k = max(3, int(line_h * DILATE) | 1)
        grown = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(k))
        mask = np.asarray(grown) > 0
        # 膨胀不能越出文字框（框外是岩石，本来就该留着）
        limit = np.zeros_like(mask)
        for it in mark.items:
            limit[max(0, it.box[1] - pad):min(h, it.box[3] + pad), max(0, it.box[0] - pad):min(w, it.box[2] + pad)] = True
        mask &= limit
    return mask


def analyse(data: bytes) -> Tuple[OvitalMark, Dict[str, Any]]:
    """只识别、不修改：返回水印解析结果与照片 EXIF 信息。"""
    info = photoexif.read_info(data)
    im = photoexif.open_upright(data)
    mark = detect(im)
    check_against_exif(mark, info)
    return mark, info


def remove(data: bytes, engine: str = "auto", write_exif: bool = True, mask_mode: str = "strokes",
           azimuth_ref: str = photoexif.DEFAULT_AZIMUTH_REF,
           progress=lambda *a: True) -> Tuple[bytes, OvitalMark, str]:
    """去掉奥维水印，把朝向（及照片缺的位置、海拔）写进 EXIF。返回 (新 JPEG, 解析结果, 引擎)。"""
    info = photoexif.read_info(data)
    im = photoexif.open_upright(data)
    mark = detect(im)
    check_against_exif(mark, info)
    if not mark.found:
        raise ValueError("照片左下角未识别到奥维水印")
    mask = build_mask(im, mark, mask_mode)
    fixed, used = _inpaint.inpaint(im, mask, engine=engine, progress=progress)
    out = photoexif.encode_jpeg(fixed, src=data)
    if write_exif:
        az = mark.azimuth if mark.azimuth_ok else None
        out = photoexif.write_tags(out, azimuth=az, azimuth_ref=azimuth_ref,
                                   lon=mark.lon, lat=mark.lat, altitude=mark.altitude)
    return out, mark, used
