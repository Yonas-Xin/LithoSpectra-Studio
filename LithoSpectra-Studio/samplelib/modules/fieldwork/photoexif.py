# -*- coding: utf-8 -*-
"""照片 EXIF：读位置、时间、方位角，补写照片缺的朝向、位置与说明。

读用 Pillow（不改文件），写用 piexif（只替换 APP1 段，像素数据原样不动，不重新
压缩）。方位角写进 GPS IFD 的 ``GPSImgDirection`` / ``GPSImgDirectionRef``——
这是 EXIF 里拍摄朝向的标准位置，iPhone 自带相机、Lightroom、QGIS 的照片插件都认。
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Any, Dict, Optional, Tuple

from PIL import ExifTags, Image, ImageOps

GPS_IFD = 0x8825
EXIF_IFD = 0x8769

#: 奥维与 iPhone 罗盘默认给的是真北方位；需要磁北时在写入时传 ``ref="M"``。
DEFAULT_AZIMUTH_REF = "T"


def _rational(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        try:
            num, den = v
            return num / den if den else 0.0
        except Exception:
            return 0.0


def _dms(values, ref: str) -> Optional[float]:
    try:
        d, m, s = (_rational(x) for x in values)
    except Exception:
        return None
    deg = d + m / 60.0 + s / 3600.0
    return -deg if ref in ("S", "W") else deg


def _exif_time(raw: str, offset: str = "") -> str:
    """EXIF 的 ``2026:10:08 13:45:12`` + 时区偏移 → ISO 8601。没有偏移时按本机时区。"""
    try:
        dt = datetime.strptime(raw.strip(), "%Y:%m:%d %H:%M:%S")
    except (AttributeError, ValueError):
        return ""
    tz = None
    if offset:
        try:
            sign = -1 if offset.strip().startswith("-") else 1
            hh, mm = offset.strip().lstrip("+-").split(":")
            tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
        except ValueError:
            tz = None
    if tz is None:
        dt = dt.astimezone()
    else:
        dt = dt.replace(tzinfo=tz)
    return dt.isoformat(timespec="seconds")


def read_info(data: bytes) -> Dict[str, Any]:
    """照片的尺寸（已按 EXIF 方向转正）、拍摄时间、经纬度、海拔、方位角。"""
    out: Dict[str, Any] = {"width": None, "height": None, "taken": "", "lon": None, "lat": None,
                           "altitude": None, "azimuth": None, "azimuth_ref": "", "make": "", "model": ""}
    with Image.open(io.BytesIO(data)) as im:
        exif = im.getexif()
        w, h = im.size
        if exif.get(0x0112) in (5, 6, 7, 8):      # 旋转 90° 的几种方向
            w, h = h, w
        out["width"], out["height"] = w, h
        out["make"] = str(exif.get(0x010F) or "").strip()
        out["model"] = str(exif.get(0x0110) or "").strip()
        sub = exif.get_ifd(EXIF_IFD)
        taken = sub.get(0x9003) or exif.get(0x0132)
        out["taken"] = _exif_time(str(taken), str(sub.get(0x9011) or "")) if taken else ""
        gps = exif.get_ifd(GPS_IFD)
        if gps:
            lat = _dms(gps.get(2), str(gps.get(1) or "N")) if gps.get(2) else None
            lon = _dms(gps.get(4), str(gps.get(3) or "E")) if gps.get(4) else None
            if lat is not None and lon is not None and not (lat == 0 and lon == 0):
                out["lat"], out["lon"] = lat, lon
            if gps.get(6) is not None:
                alt = _rational(gps.get(6))
                ref = gps.get(5)
                if ref in (1, b"\x01"):
                    alt = -alt
                out["altitude"] = round(alt, 2)
            if gps.get(17) is not None:
                out["azimuth"] = round(_rational(gps.get(17)) % 360.0, 2)
                out["azimuth_ref"] = str(gps.get(16) or DEFAULT_AZIMUTH_REF).strip() or DEFAULT_AZIMUTH_REF
    return out


def _to_rational(value: float, precision: int = 100) -> Tuple[int, int]:
    f = Fraction(value).limit_denominator(precision * 100)
    return f.numerator, f.denominator


def _to_dms(value: float) -> Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, int]]:
    value = abs(value)
    d = int(value)
    m_full = (value - d) * 60
    m = int(m_full)
    s = (m_full - m) * 60
    return (d, 1), (m, 1), (int(round(s * 10000)), 10000)


def write_tags(data: bytes, azimuth: Optional[float] = None, azimuth_ref: str = DEFAULT_AZIMUTH_REF,
               lon: Optional[float] = None, lat: Optional[float] = None,
               altitude: Optional[float] = None, overwrite_gps: bool = False,
               orientation_reset: bool = False, description: Optional[str] = None) -> bytes:
    """往 JPEG 的 EXIF 里补照片信息（朝向、位置、海拔、说明），返回新的文件内容。

    只补照片里没有的项：已有的经纬度、海拔、朝向、说明一律不动（``overwrite_gps``
    为真时位置、海拔、朝向可以覆盖）。什么都不用补、也不用改方向标记时原样返回
    ``data``，一个字节都不变。``orientation_reset`` 用于像素已经转正过的图，把方向
    标记改回 1，免得看图软件再转一次。
    """
    import piexif

    try:
        exif = piexif.load(data)
    except Exception:
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    gps = exif.setdefault("GPS", {})
    zeroth = exif.setdefault("0th", {})
    changed = False
    if azimuth is not None and (overwrite_gps or piexif.GPSIFD.GPSImgDirection not in gps):
        gps[piexif.GPSIFD.GPSImgDirectionRef] = (azimuth_ref or DEFAULT_AZIMUTH_REF).encode()
        gps[piexif.GPSIFD.GPSImgDirection] = _to_rational(float(azimuth) % 360.0)
        changed = True
    has_pos = piexif.GPSIFD.GPSLatitude in gps and piexif.GPSIFD.GPSLongitude in gps
    if lon is not None and lat is not None and (overwrite_gps or not has_pos):
        gps[piexif.GPSIFD.GPSLatitudeRef] = b"S" if lat < 0 else b"N"
        gps[piexif.GPSIFD.GPSLatitude] = _to_dms(lat)
        gps[piexif.GPSIFD.GPSLongitudeRef] = b"W" if lon < 0 else b"E"
        gps[piexif.GPSIFD.GPSLongitude] = _to_dms(lon)
        changed = True
    if altitude is not None and (overwrite_gps or piexif.GPSIFD.GPSAltitude not in gps):
        gps[piexif.GPSIFD.GPSAltitudeRef] = 1 if altitude < 0 else 0
        gps[piexif.GPSIFD.GPSAltitude] = _to_rational(abs(float(altitude)))
        changed = True
    if gps:
        gps.setdefault(piexif.GPSIFD.GPSVersionID, (2, 3, 0, 0))
    have_desc = zeroth.get(piexif.ImageIFD.ImageDescription) or b""
    if isinstance(have_desc, str):
        have_desc = have_desc.encode("utf-8", "replace")
    if description and not have_desc.strip(b"\0 "):
        # ImageDescription 按规范是 ASCII，多数软件也认 UTF-8；Windows 资源管理器读
        # XPComment（UTF-16），没有时一并补上
        zeroth[piexif.ImageIFD.ImageDescription] = description.encode("utf-8")
        if piexif.ImageIFD.XPComment not in zeroth:
            zeroth[piexif.ImageIFD.XPComment] = tuple((description + "\0").encode("utf-16-le"))
        changed = True
    if orientation_reset:
        zeroth[piexif.ImageIFD.Orientation] = 1
        # 缩略图与主图不一致时不少看图软件会显示旧图，编辑过的照片直接去掉缩略图
        exif["thumbnail"] = None
        exif["1st"] = {}
        changed = True
    if not changed:
        return data
    # piexif 对个别厂商私有标签会拒绝序列化，去掉 MakerNote 再试一次
    try:
        blob = piexif.dump(exif)
    except Exception:
        exif.get("Exif", {}).pop(piexif.ExifIFD.MakerNote, None)
        blob = piexif.dump(exif)
    out = io.BytesIO()
    piexif.insert(blob, data, out)
    return out.getvalue()


def copy_exif(src: bytes, dst: bytes, orientation_reset: bool = True) -> bytes:
    """把 ``src`` 的 EXIF 搬到 ``dst``（编辑器导出的图没有 EXIF）。"""
    import piexif

    try:
        exif = piexif.load(src)
    except Exception:
        return dst
    if orientation_reset:
        exif.setdefault("0th", {})[piexif.ImageIFD.Orientation] = 1
        exif["thumbnail"] = None
        exif["1st"] = {}
    try:
        blob = piexif.dump(exif)
    except Exception:
        exif.get("Exif", {}).pop(piexif.ExifIFD.MakerNote, None)
        blob = piexif.dump(exif)
    out = io.BytesIO()
    piexif.insert(blob, dst, out)
    return out.getvalue()


def open_upright(data: bytes) -> Image.Image:
    """按 EXIF 方向转正后的 RGB 图像。"""
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    return im


def encode_jpeg(im: Image.Image, src: Optional[bytes] = None, quality: int = 95) -> bytes:
    """存成 JPEG；给了原图就把它的 EXIF 与 ICC 一起带上（方向改为 1）。"""
    buf = io.BytesIO()
    icc = None
    if src is not None:
        try:
            with Image.open(io.BytesIO(src)) as orig:
                icc = orig.info.get("icc_profile")
        except Exception:
            icc = None
    im.save(buf, "JPEG", quality=quality, subsampling=0 if quality >= 95 else 2,
            optimize=True, **({"icc_profile": icc} if icc else {}))
    data = buf.getvalue()
    return copy_exif(src, data, orientation_reset=True) if src is not None else data


def thumbnail(data: bytes, size: int) -> bytes:
    im = open_upright(data)
    im.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


__all__ = ["read_info", "write_tags", "copy_exif", "open_upright", "encode_jpeg", "thumbnail",
           "DEFAULT_AZIMUTH_REF", "ExifTags"]
