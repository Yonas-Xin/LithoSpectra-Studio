# -*- coding: utf-8 -*-
"""感兴趣区（ROI）：从矢量或影像文件解析范围，并据此筛选样本。

ROI 几何统一转换到样本库存储坐标系（EPSG:4326），因此矢量 ROI 与影像 ROI
在后续筛选中可以同等对待。
"""

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence, Set

from osgeo import gdal, ogr, osr

# 有效值掩膜矢量化前降采样到的最长边。够勾出外轮廓，又不至于
# 在一景两千万像元的影像上跑上几十秒。
MASK_MAX_SIDE = 1024

from . import config
from .model import SampleKey
from .store import (SampleStore, make_transform, open_raster, open_vector,
                    srs_identity, storage_srs)

gdal.UseExceptions()


@dataclass
class ROI:
    """一个感兴趣区。"""

    name: str
    path: str
    kind: str                     # vector | raster
    geom: ogr.Geometry            # EPSG:4326
    native_crs: str = ""
    info: dict = field(default_factory=dict)

    @property
    def extent(self):
        """(minx, miny, maxx, maxy)，经纬度。"""
        minx, maxx, miny, maxy = self.geom.GetEnvelope()
        return (minx, miny, maxx, maxy)

    def describe(self) -> str:
        kind_cn = "矢量" if self.kind == "vector" else "影像"
        minx, miny, maxx, maxy = self.extent
        return ("%s（%s）\n范围: %.5f, %.5f ~ %.5f, %.5f\n原坐标系: %s"
                % (self.name, kind_cn, minx, miny, maxx, maxy, self.native_crs or "未知"))


# =============================================================================
# 解析
# =============================================================================

def roi_from_vector(path: str, union: bool = True) -> ROI:
    """从矢量文件构造 ROI：默认合并所有要素的几何。

    几何统一压平为二维（源数据常见 3D Polygon），再合并成一个范围；
    自相交等脏面数据先修复再重新合并；点 / 线这类没有面积的要素取凸包，
    凸包退化时再取外接矩形。
    """
    ds = open_vector(path)
    lyr = ds.GetLayer(0)
    if lyr is None or lyr.GetFeatureCount() == 0:
        raise ValueError("矢量文件中没有要素：%s" % path)

    src_srs = lyr.GetSpatialRef()
    transform = make_transform(src_srs)

    geoms = []
    lyr.ResetReading()
    for feat in lyr:
        geom = feat.GetGeometryRef()
        if geom is None or geom.IsEmpty():
            continue
        g = geom.Clone()
        g.FlattenTo2D()                 # 3D 几何会让后续的合并与相交判断出问题
        if transform is not None:
            g.Transform(transform)
        geoms.append(g)

    if not geoms:
        raise ValueError("矢量文件中没有有效几何：%s" % path)

    merged = _union_geometries(geoms) if union else None
    if merged is None or merged.IsEmpty():
        merged = _collect(geoms)

    # 合并结果不可用而输入里确实有面：多半是自相交之类的脏数据。
    # UnaryUnion 不会给无效几何做 noding，会把领结面原样打包成零面积的
    # MultiPolygon，后续 GEOS 谓词判断就成了未定义行为。修好再合一次。
    if not _is_usable(merged) and any(_is_areal(g) for g in geoms):
        retry = _union_geometries([_repair(g) for g in geoms])
        if retry is not None and not retry.IsEmpty():
            merged = retry
        merged = _repair(merged)

    # 没有面积的结果（点、线、或合并失败留下的集合）无法用于范围筛选，
    # 依次退化为凸包 -> 外接矩形
    if not _is_areal(merged):
        try:
            hull = merged.ConvexHull()
            if hull is not None and _is_areal(hull) and not hull.IsEmpty():
                merged = hull
        except Exception:
            pass
    if not _is_areal(merged):
        merged = _envelope_polygon(merged)

    return ROI(name=os.path.basename(path), path=path, kind="vector",
               geom=merged, native_crs=srs_identity(src_srs),
               info={"要素数": len(geoms), "图层": lyr.GetName(),
                     "几何": ogr.GeometryTypeToName(lyr.GetGeomType())})


def roi_from_raster(path: str, valid_only: bool = False) -> ROI:
    """从影像文件构造 ROI。

    ``valid_only`` 为假时取影像四至（边界加密后投影，避免投影弯曲带来的误差）；
    为真时改取**有效数据的轮廓**——影像常常是斜的，四至矩形会把大片黑边圈进来，
    拿它筛样本会选中一堆压根没有影像覆盖的点。
    """
    ds = open_raster(path)

    gt = ds.GetGeoTransform(can_return_null=True)
    if gt is None:
        raise ValueError("影像缺少地理变换信息，无法作为感兴趣区：%s" % path)

    if valid_only:
        roi = _roi_from_valid_mask(ds, path, gt)
        if roi is not None:
            return roi
        # 全图都是有效值（没有 nodata、也没有 alpha），轮廓就等于四至，
        # 白算一趟不如直接走下面那条。

    w, h = ds.RasterXSize, ds.RasterYSize
    src_srs = None
    wkt = ds.GetProjectionRef()
    if wkt:
        src_srs = osr.SpatialReference()
        src_srs.ImportFromWkt(wkt)
        src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = make_transform(src_srs)

    n = max(2, int(config.RASTER_EDGE_DENSIFY))
    pixels = []
    for i in range(n):                       # 上边
        pixels.append((w * i / n, 0.0))
    for i in range(n):                       # 右边
        pixels.append((float(w), h * i / n))
    for i in range(n):                       # 下边
        pixels.append((w * (n - i) / n, float(h)))
    for i in range(n):                       # 左边
        pixels.append((0.0, h * (n - i) / n))

    ring = ogr.Geometry(ogr.wkbLinearRing)
    for px, py in pixels:
        x = gt[0] + px * gt[1] + py * gt[2]
        y = gt[3] + px * gt[4] + py * gt[5]
        ring.AddPoint_2D(x, y)
    ring.CloseRings()
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    if transform is not None:
        poly.Transform(transform)

    info = {
        "尺寸": "%d x %d" % (w, h),
        "波段数": ds.RasterCount,
        "像元大小": "%.6g x %.6g" % (abs(gt[1]), abs(gt[5])),
    }
    return ROI(name=os.path.basename(path), path=path, kind="raster",
               geom=poly, native_crs=srs_identity(src_srs), info=info)


def roi_from_features(features, name: str = "图层范围") -> ROI:
    """从画布图层的要素构造 ROI。

    与 :func:`roi_from_vector` 的区别只在数据从哪儿来：那边从文件读，这边直接
    拿图层已经载进内存、且已经是经纬度的几何。手绘范围没有文件可读，只能走
    这条路；参考矢量图层走这条也省一次重复解析。

    合并、修复、退化为凸包/外接矩形的处理与文件那条完全一致——同一个筛选结果
    不该因为"范围是画的还是读的"而不同。
    """
    geoms = []
    for feat in features:
        geom = _feature_to_geometry(feat)
        if geom is not None and not geom.IsEmpty():
            geom.FlattenTo2D()
            geoms.append(geom)
    if not geoms:
        raise ValueError("这个图层里没有可用的几何。")

    merged = _union_geometries(geoms)
    if merged is None or merged.IsEmpty():
        merged = _collect(geoms)
    if not _is_usable(merged) and any(_is_areal(g) for g in geoms):
        retry = _union_geometries([_repair(g) for g in geoms])
        if retry is not None and not retry.IsEmpty():
            merged = retry
        merged = _repair(merged)
    if not _is_areal(merged):
        try:
            hull = merged.ConvexHull()
            if hull is not None and _is_areal(hull) and not hull.IsEmpty():
                merged = hull
        except Exception:
            pass
    if not _is_areal(merged):
        merged = _envelope_polygon(merged)

    return ROI(name=name, path="", kind="layer", geom=merged,
               native_crs="EPSG:4326", info={"要素数": len(geoms)})


def _feature_to_geometry(feat):
    """画布图层的 VectorFeature -> OGR 几何（经纬度）。"""
    kind = getattr(feat, "geom_kind", "")
    coords = getattr(feat, "coords", None)
    if not coords:
        return None
    if kind == "polygon":
        poly = ogr.Geometry(ogr.wkbPolygon)
        for ring in coords:
            if len(ring) < 3:
                continue
            r = ogr.Geometry(ogr.wkbLinearRing)
            for lon, lat in ring:
                r.AddPoint_2D(float(lon), float(lat))
            r.CloseRings()
            poly.AddGeometry(r)
        return poly if poly.GetGeometryCount() else None
    if kind == "line":
        multi = ogr.Geometry(ogr.wkbMultiLineString)
        for part in coords:
            line = ogr.Geometry(ogr.wkbLineString)
            for lon, lat in part:
                line.AddPoint_2D(float(lon), float(lat))
            multi.AddGeometry(line)
        return multi if multi.GetGeometryCount() else None
    multi = ogr.Geometry(ogr.wkbMultiPoint)
    for lon, lat in coords:
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(float(lon), float(lat))
        multi.AddGeometry(pt)
    return multi if multi.GetGeometryCount() else None


def _roi_from_valid_mask(ds, path: str, gt) -> Optional[ROI]:
    """按有效值掩膜勾出影像的实际覆盖范围。

    掩膜按需降采样后再矢量化：一景 GF5 是 2000x2000 以上，逐像元矢量化能出几十
    万个碎多边形，慢且没用——筛样本只需要外轮廓，降到长边 :data:`MASK_MAX_SIDE`
    以内足够，边界误差最多一个降采样后的像元。

    返回 None 表示"整幅都是有效值"，让调用方退回四至那条更快的路。
    """
    band = ds.GetRasterBand(1)
    mask = band.GetMaskBand()
    if mask is None:
        return None
    flags = band.GetMaskFlags()
    if flags & gdal.GMF_ALL_VALID:
        return None

    w, h = ds.RasterXSize, ds.RasterYSize
    scale = max(1.0, max(w, h) / float(MASK_MAX_SIDE))
    ow, oh = max(1, int(w / scale)), max(1, int(h / scale))
    arr = mask.ReadAsArray(0, 0, w, h, ow, oh)
    if arr is None:
        return None
    if not arr.any():
        raise ValueError("影像里没有有效像元：%s" % path)

    mem = gdal.GetDriverByName("MEM").Create("", ow, oh, 1, gdal.GDT_Byte)
    # 降采样之后地理变换的像元尺寸要跟着放大，否则勾出来的轮廓会缩在左上角
    mem.SetGeoTransform((gt[0], gt[1] * w / float(ow), gt[2] * h / float(oh),
                         gt[3], gt[4] * w / float(ow), gt[5] * h / float(oh)))
    mem.GetRasterBand(1).WriteArray((arr > 0).astype("uint8"))

    vec = ogr.GetDriverByName("MEM").CreateDataSource("mask")
    lyr = vec.CreateLayer("mask", None, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(mem.GetRasterBand(1), mem.GetRasterBand(1), lyr, 0)

    geoms = []
    for feat in lyr:
        if feat.GetField("v") != 1:
            continue
        geom = feat.GetGeometryRef()
        if geom is not None and not geom.IsEmpty():
            geoms.append(geom.Clone())
    vec, mem = None, None
    if not geoms:
        return None

    merged = _union_geometries(geoms) or _collect(geoms)
    if not _is_usable(merged):
        merged = _repair(merged)
    src_srs = None
    wkt = ds.GetProjectionRef()
    if wkt:
        src_srs = osr.SpatialReference()
        src_srs.ImportFromWkt(wkt)
        src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = make_transform(src_srs)
    if transform is not None:
        merged.Transform(transform)

    return ROI(name=os.path.basename(path), path=path, kind="raster",
               geom=merged, native_crs=srs_identity(src_srs),
               info={"尺寸": "%d x %d" % (w, h), "范围": "有效数据轮廓",
                     "矢量化降采样": "1/%.1f" % scale})


def load_roi(path: str) -> ROI:
    """自动识别文件类型并构造 ROI（先按矢量尝试，失败再按影像尝试）。"""
    if not os.path.exists(path):
        raise FileNotFoundError("文件不存在：%s" % path)
    errors = []
    for loader in (roi_from_vector, roi_from_raster):
        try:
            return loader(path)
        except Exception as exc:
            errors.append(str(exc))
    raise ValueError("无法识别为矢量或影像：%s\n%s" % (path, "\n".join(errors)))


@contextmanager
def _quiet_gdal():
    """临时静音 GDAL 错误输出：几何校验遇到脏数据会不停刷警告。"""
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        yield
    finally:
        gdal.PopErrorHandler()


def _is_valid(geom: Optional[ogr.Geometry]) -> bool:
    """几何拓扑是否有效。"""
    if geom is None:
        return False
    with _quiet_gdal():
        try:
            return bool(geom.IsValid())
        except Exception:
            return False


def _is_usable(geom: Optional[ogr.Geometry]) -> bool:
    """几何能否直接用于范围筛选：面状、有面积、且拓扑有效。"""
    if not _is_areal(geom):
        return False
    try:
        if geom.GetArea() <= 0:
            return False
    except Exception:
        return False
    return _is_valid(geom)


def _repair(geom: Optional[ogr.Geometry]) -> Optional[ogr.Geometry]:
    """修复无效几何：MakeValid -> Buffer(0)，都修不好就原样返回。

    优先 ``MakeValid()``：领结面被它拆成两个三角形，面积完整保留；
    ``Buffer(0)`` 对同样的图形只会留下其中一半。
    """
    if geom is None or _is_valid(geom):
        return geom
    with _quiet_gdal():
        for fix in (ogr.Geometry.MakeValid, lambda g: g.Buffer(0)):
            try:
                fixed = fix(geom)
            except Exception:
                continue
            if fixed is not None and not fixed.IsEmpty() and fixed.GetArea() > 0:
                return fixed
    return geom


def _is_areal(geom: Optional[ogr.Geometry]) -> bool:
    """几何是否是面状（能用于范围筛选）。"""
    if geom is None or geom.IsEmpty():
        return False
    return ogr.GT_Flatten(geom.GetGeometryType()) in (ogr.wkbPolygon,
                                                      ogr.wkbMultiPolygon)


def _collect(geoms: Sequence[ogr.Geometry]) -> ogr.Geometry:
    """把一组几何装进一个几何集合。"""
    collection = ogr.Geometry(ogr.wkbGeometryCollection)
    for g in geoms:
        collection.AddGeometry(g)
    return collection


def _union_geometries(geoms: Sequence[ogr.Geometry]) -> Optional[ogr.Geometry]:
    """把一组几何合并成一个。

    注意 GDAL 的 ``UnionCascaded()`` 只接受 MultiPolygon，对几何集合会直接抛
    ``must be a MultiPolygon``；``UnaryUnion()`` 才是通用的合并入口。
    这里按 UnaryUnion -> 逐个 Union -> 原样返回集合 的顺序降级，
    保证任何输入都能得到一个可用的几何。
    """
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0].Clone()

    collection = _collect(geoms)
    try:
        merged = collection.UnaryUnion()
        if merged is not None and not merged.IsEmpty():
            return merged
    except Exception:
        pass

    try:                                # 逐个合并：慢一些，但对脏数据更宽容
        merged = geoms[0].Clone()
        for g in geoms[1:]:
            merged = merged.Union(g)
        if merged is not None and not merged.IsEmpty():
            return merged
    except Exception:
        pass

    return collection


def _envelope_polygon(geom: ogr.Geometry) -> ogr.Geometry:
    """取几何的外接矩形并构造面。范围退化时向外扩一点，避免零面积。"""
    minx, maxx, miny, maxy = geom.GetEnvelope()
    if maxx - minx <= 0:
        minx, maxx = minx - 1e-6, maxx + 1e-6
    if maxy - miny <= 0:
        miny, maxy = miny - 1e-6, maxy + 1e-6
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)):
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


# =============================================================================
# 筛选
# =============================================================================

def select_by_roi(store: SampleStore, roi: ROI, mode: str = config.DEFAULT_ROI_MODE,
                  class_ids: Optional[Iterable[int]] = None,
                  progress=None) -> Set[SampleKey]:
    """返回落在 ROI 内的样本主键集合。"""
    return store.select_by_geometry(roi.geom, mode=mode, class_ids=class_ids,
                                    progress=progress)
