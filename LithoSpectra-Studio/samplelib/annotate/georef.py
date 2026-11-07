# -*- coding: utf-8 -*-
"""影像配准信息嗅探。

GDAL 只认少数几种世界文件后缀，也只读 PAM 里标准的 ``<SRS>`` 节点。ArcGIS
导出的配准结果常常两头都不合规——世界文件写成 ``.jgwx``、坐标系埋在
``.aux.xml`` 的 ``xml:ESRI`` 段里——这种影像直接 ``gdal.Open`` 会得到单位阵
地理变换和空坐标系，看上去"能打开"，实际位置全错。

本模块把这些散落的配准信息补齐，产出一个带正确地理变换与坐标系的内存
VRT，供上层当作普通影像使用。对本来就规范的影像（GeoTIFF、ENVI 等）不做
任何改动，原样返回。
"""

import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from osgeo import gdal, osr

from .. import config
from ..store import open_raster as _gdal_open_raster

gdal.UseExceptions()
osr.UseExceptions()

# 世界文件的候选后缀。GDAL 自己会找前四种里的一部分，但 ``x`` 结尾的 ArcGIS
# 变体一律不认，因此全部自己找一遍。
_WORLD_SUFFIXES = ("w", "wx")
_WORLD_GENERIC = (".wld", ".wldx")

# 认为"没有配准"的地理变换：GDAL 打不开配准信息时返回的单位阵
_IDENTITY_GT = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

# ENVI 数据文件的常见扩展名。使用者在文件对话框里很容易点中 .hdr，而 GDAL
# 只接受数据文件本身，因此帮他找回去。空串表示"无扩展名"的裸数据文件。
_ENVI_DATA_EXTS = ("", ".dat", ".img", ".bsq", ".bil", ".bip", ".raw", ".bin")

# 边界转经纬度时每条边的加密点数，应对投影弯曲与带旋转的仿射
EDGE_DENSIFY = getattr(config, "RASTER_EDGE_DENSIFY", 25)


@dataclass
class GeoInfo:
    """一份影像的配准来源说明，用于在界面上告诉使用者坐标是哪来的。"""

    path: str = ""
    geotransform: Tuple[float, ...] = _IDENTITY_GT
    crs_wkt: str = ""
    crs_name: str = ""
    crs_code: str = ""
    gt_source: str = ""          # dataset | worldfile:<ext> | gcp
    crs_source: str = ""         # dataset | prj | esri-aux | pam
    rotated: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def georeferenced(self) -> bool:
        return bool(self.crs_wkt) and self.geotransform != _IDENTITY_GT

    def describe(self) -> str:
        lines = []
        lines.append("地理变换：%s" % (self.gt_source or "缺失"))
        if self.rotated:
            lines.append("  含旋转项，范围按四角计算")
        lines.append("坐标系：%s" % (self.crs_name or "缺失"))
        if self.crs_code:
            lines.append("  %s（来自 %s）" % (self.crs_code, self.crs_source))
        elif self.crs_source:
            lines.append("  来自 %s" % self.crs_source)
        lines.extend("  " + n for n in self.notes)
        return "\n".join(lines)


# =============================================================================
# 世界文件
# =============================================================================

def world_file_candidates(path: str) -> List[str]:
    """列出该影像可能的世界文件路径，按优先级排序。

    ``a.jpg`` -> ``a.jgw``、``a.jgwx``、``a.jpgw``、``a.jpgwx``、``a.wld``…
    大小写各试一遍，因为 Windows 导出的文件名常是大写。
    """
    stem, ext = os.path.splitext(path)
    ext = ext.lstrip(".")
    out: List[str] = []
    if len(ext) >= 3:
        # 首尾字母 + w：jpg -> jgw，png -> pgw，tif -> tfw
        short = ext[0] + ext[-1]
        for suf in _WORLD_SUFFIXES:
            out.append("%s.%s%s" % (stem, short, suf))
    if ext:
        for suf in _WORLD_SUFFIXES:
            out.append("%s.%s%s" % (stem, ext, suf))
    out.extend(stem + g for g in _WORLD_GENERIC)

    # 只对扩展名做大小写变体：整条路径小写会把目录名也改掉，在区分大小写的
    # 文件系统上永远匹配不到。
    result: List[str] = []
    for cand in out:
        base, ext2 = os.path.splitext(cand)
        for variant in (cand, base + ext2.lower(), base + ext2.upper()):
            if variant not in result:
                result.append(variant)
    return result


def read_world_file(path: str) -> Optional[Tuple[Tuple[float, ...], str]]:
    """读取世界文件并换算成 GDAL 地理变换。

    世界文件的六个数依次是 A、D、B、E、C、F，其中 C、F 指向**左上角像元的
    中心**；而 GDAL 的地理变换以左上角像元的**角点**为原点，因此要各自减去
    半个像元。带旋转时两个方向都要扣。
    """
    for cand in world_file_candidates(path):
        if not os.path.isfile(cand):
            continue
        try:
            with open(cand, "r", encoding="utf-8", errors="ignore") as fh:
                nums = [float(line.strip()) for line in fh
                        if line.strip() and _NUMBER_RE.match(line.strip())]
        except (OSError, ValueError):
            continue
        if len(nums) < 6:
            continue
        a, d, b, e, c, f = nums[:6]
        gt = (c - (a + b) / 2.0, a, b,
              f - (d + e) / 2.0, d, e)
        return gt, os.path.splitext(cand)[1].lstrip(".")
    return None


_NUMBER_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


# =============================================================================
# 坐标系
# =============================================================================

def _srs_from_wkt(wkt: str) -> Optional[osr.SpatialReference]:
    if not wkt:
        return None
    srs = osr.SpatialReference()
    ok = False
    for parse in (srs.ImportFromWkt, srs.SetFromUserInput):
        try:
            parse(wkt)
            ok = True
            break
        except Exception:
            continue
    # 不用 Validate() 把关：ArcGIS 写出的 WKT 用的是 D_China_2000、Degree 这类
    # 非标准写法，Validate 会判它 "Corrupt data"，但 PROJ 其实解析得了，
    # AutoIdentifyEPSG 也能认出 EPSG:4495。能给出名字就算解析成功。
    if not ok or not srs.GetName():
        return None
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def read_prj_file(path: str) -> Optional[osr.SpatialReference]:
    """读取同名 .prj。"""
    stem = os.path.splitext(path)[0]
    for cand in (stem + ".prj", stem + ".PRJ"):
        if not os.path.isfile(cand):
            continue
        try:
            with open(cand, "r", encoding="utf-8", errors="ignore") as fh:
                return _srs_from_wkt(fh.read().strip())
        except OSError:
            continue
    return None


def read_aux_srs(path: str) -> Optional[osr.SpatialReference]:
    """从 ``.aux.xml`` 里取坐标系。

    先找 PAM 标准的 ``<SRS>``，再找 ArcGIS 写在 ``xml:ESRI`` 元数据段里的
    ``<WKT>``——后者是 ArcMap / ArcGIS Pro 配准图片时的默认落点，GDAL 自己
    不会读。
    """
    for cand in (path + ".aux.xml", os.path.splitext(path)[0] + ".aux.xml"):
        if not os.path.isfile(cand):
            continue
        try:
            root = ET.parse(cand).getroot()
        except (ET.ParseError, OSError):
            continue
        for tag in ("SRS", "WKT"):
            for node in root.iter():
                if node.tag.split("}")[-1] != tag or not (node.text or "").strip():
                    continue
                srs = _srs_from_wkt(node.text.strip())
                if srs is not None:
                    return srs
    return None


def srs_label(srs: Optional[osr.SpatialReference]) -> Tuple[str, str]:
    """坐标系 -> (可读名称, "EPSG:xxxx")。识别不出代码时第二项为空。"""
    if srs is None:
        return "", ""
    name = srs.GetName() or ""
    code = ""
    try:
        probe = srs.Clone()
        probe.AutoIdentifyEPSG()
        auth, num = probe.GetAuthorityName(None), probe.GetAuthorityCode(None)
        if auth and num:
            code = "%s:%s" % (auth, num)
    except Exception:
        pass
    return name, code


# =============================================================================
# 打开
# =============================================================================

def resolve_envi_header(path: str) -> str:
    """选中 ENVI 头文件时，换成它对应的数据文件。

    GDAL 不接受 ``.hdr``，会直接报错让人去选数据文件。文件对话框里两者挨在
    一起，点错是常态，这里直接帮忙找回去；找不到就原样返回，让 GDAL 去报
    它自己那句更具体的错。
    """
    stem, ext = os.path.splitext(path)
    if ext.lower() != ".hdr":
        return path
    for cand_ext in _ENVI_DATA_EXTS:
        for cand in (stem + cand_ext, stem + cand_ext.upper()):
            if cand != path and os.path.isfile(cand):
                return cand
    return path


def open_raster(path: str) -> Tuple[gdal.Dataset, GeoInfo]:
    """打开影像，必要时补齐配准信息。

    返回 ``(数据集, 配准说明)``。数据集可能是原始数据集，也可能是补过地理
    变换与坐标系的内存 VRT；无论哪种，调用方都当普通 GDAL 数据集用即可。
    """
    path = resolve_envi_header(path)
    # 本模块自己也叫 open_raster（它返回「数据集 + 配准说明」），因此把
    # store 里那个纯粹开文件的取个别名，免得在这里递归调用自己。
    ds = _gdal_open_raster(path)

    info = GeoInfo(path=path)
    gt = ds.GetGeoTransform(can_return_null=True)
    wkt = ds.GetProjection() or ""
    srs = _srs_from_wkt(wkt) if wkt else None

    patched_gt = None
    if gt is None or _is_identity(gt):
        found = read_world_file(path)
        if found is not None:
            patched_gt, ext = found
            info.gt_source = "世界文件 .%s" % ext
        elif gt is not None:
            info.gt_source = "缺失（影像自带单位阵）"
    else:
        info.gt_source = "影像自带"

    patched_srs = None
    if srs is None:
        patched_srs = read_prj_file(path)
        if patched_srs is not None:
            info.crs_source = ".prj"
        else:
            patched_srs = read_aux_srs(path)
            if patched_srs is not None:
                info.crs_source = ".aux.xml（ESRI）"
    else:
        info.crs_source = "影像自带"

    if patched_gt is not None or patched_srs is not None:
        ds = _vrt_with(ds, patched_gt or gt,
                       patched_srs.ExportToWkt() if patched_srs is not None else wkt)

    final_gt = ds.GetGeoTransform(can_return_null=True) or _IDENTITY_GT
    info.geotransform = tuple(final_gt)
    info.rotated = abs(final_gt[2]) > 1e-12 or abs(final_gt[4]) > 1e-12
    final_wkt = ds.GetProjection() or ""
    info.crs_wkt = final_wkt
    info.crs_name, info.crs_code = srs_label(_srs_from_wkt(final_wkt))

    if not info.crs_wkt:
        info.notes.append("找不到坐标系，无法定位；请补一个 .prj 或用 GIS 重新配准。")
    if _is_identity(info.geotransform):
        info.notes.append("找不到地理变换，无法定位；请补一个世界文件。")
    return ds, info


def _is_identity(gt: Sequence[float]) -> bool:
    return all(abs(a - b) < 1e-12 for a, b in zip(gt, _IDENTITY_GT))


def _vrt_with(ds: gdal.Dataset, gt, wkt: str) -> gdal.Dataset:
    """复制成内存 VRT 并写入地理变换 / 坐标系，不改动原文件。"""
    vrt = gdal.GetDriverByName("VRT").CreateCopy("", ds)
    if gt is not None:
        vrt.SetGeoTransform([float(v) for v in gt])
    if wkt:
        vrt.SetProjection(wkt)
    return vrt


# =============================================================================
# 范围
# =============================================================================

def extent_4326(ds: gdal.Dataset, densify: int = EDGE_DENSIFY
                ) -> Optional[Tuple[float, float, float, float]]:
    """影像范围转经纬度 ``(minx, miny, maxx, maxy)``。

    沿四条边加密取点再变换：带旋转的仿射和高斯-克吕格这类投影下，只取四角
    会低估范围，边缘会被裁掉。
    """
    gt = ds.GetGeoTransform(can_return_null=True)
    wkt = ds.GetProjection()
    if gt is None or not wkt:
        return None
    src = _srs_from_wkt(wkt)
    if src is None:
        return None

    dst = osr.SpatialReference()
    dst.ImportFromEPSG(config.STORAGE_EPSG)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    w, h = ds.RasterXSize, ds.RasterYSize
    n = max(2, int(densify))
    pixels = []
    for i in range(n + 1):
        t = i / float(n)
        pixels.append((t * w, 0.0))
        pixels.append((t * w, float(h)))
        pixels.append((0.0, t * h))
        pixels.append((float(w), t * h))

    coords = [(gt[0] + p * gt[1] + l * gt[2], gt[3] + p * gt[4] + l * gt[5])
              for p, l in pixels]

    if not src.IsSame(dst):
        try:
            tr = osr.CoordinateTransformation(src, dst)
            coords = [(x, y) for x, y, *_ in tr.TransformPoints(coords)]
        except Exception:
            return None

    xs = [c[0] for c in coords if math.isfinite(c[0])]
    ys = [c[1] for c in coords if math.isfinite(c[1])]
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))
