# -*- coding: utf-8 -*-
"""点要素按距离缓冲：把点扩成圆，重叠的圆合成一个面。

导出与裁剪共用。一个点样本往往代表野外的一处观测，训练时要的是它周围一小片
地面而不是一个像元；把点扩成半径 R 米的圆，导出得到一个面文件，裁剪则按圆内
的像元取块。只对点做，面本来就有范围；两个圆挨得近就合成一个面——同一处露头
上的两个观测点，不该在导出结果里变成两条各自独立、又互相压着的记录。

距离是米。样本库存的是经纬度，圆要在投影坐标系里画：按这批点的中心挑一个
UTM 带，整批投过去、缓冲、合并，再投回来。30 米级的圆在带内的变形可以忽略，
一批点横跨两个带的极端情况下边界处差个厘米级，同样无关紧要。
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from osgeo import ogr, osr

from .crs import utm_epsg
from .model import SampleKey
from .store import storage_srs

#: 圆的一象限用几段折线逼近。16 段一圈 64 个顶点，30 米的圆误差在厘米以下。
QUAD_SEGMENTS = 16


def _utm_for(points: Sequence[Tuple[float, float]]) -> osr.SpatialReference:
    lon = sum(p[0] for p in points) / len(points)
    lat = sum(p[1] for p in points) / len(points)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(utm_epsg(lon, lat))
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _parts(geom: ogr.Geometry) -> List[ogr.Geometry]:
    """把 Polygon / MultiPolygon / GeometryCollection 拆成一个个 Polygon。"""
    if geom is None or geom.IsEmpty():
        return []
    t = ogr.GT_Flatten(geom.GetGeometryType())
    if t == ogr.wkbPolygon:
        return [geom.Clone()]
    out: List[ogr.Geometry] = []
    for i in range(geom.GetGeometryCount()):
        out.extend(_parts(geom.GetGeometryRef(i)))
    return out


def buffer_points(items: Sequence[Tuple[SampleKey, ogr.Geometry]],
                  radius_m: float) -> List[Tuple[ogr.Geometry, List[SampleKey]]]:
    """把一批点（EPSG:4326）扩成半径 ``radius_m`` 米的圆，重叠的合成一个面。

    返回 ``[(面几何 EPSG:4326, [并进这个面的点的主键, ...]), ...]``，面按第一个
    点的出现顺序排。MultiPoint 每个部件各算一个点，主键相同。传进来的几何不
    会被改动。
    """
    if radius_m <= 0 or not items:
        return []
    keys: List[SampleKey] = []
    lonlat: List[Tuple[float, float]] = []
    for key, geom in items:
        if geom is None or geom.IsEmpty():
            continue
        name = geom.GetGeometryName()
        if name == "POINT":
            keys.append(key)
            lonlat.append((geom.GetX(), geom.GetY()))
        elif name == "MULTIPOINT":
            for i in range(geom.GetGeometryCount()):
                p = geom.GetGeometryRef(i)
                keys.append(key)
                lonlat.append((p.GetX(), p.GetY()))
    if not lonlat:
        return []

    utm = _utm_for(lonlat)
    src = storage_srs()
    to_utm = osr.CoordinateTransformation(src, utm)
    to_geo = osr.CoordinateTransformation(utm, src)

    circles = ogr.Geometry(ogr.wkbMultiPolygon)
    projected: List[Tuple[float, float]] = []
    for lon, lat in lonlat:
        x, y, *_ = to_utm.TransformPoint(float(lon), float(lat))
        projected.append((x, y))
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(x, y)
        circles.AddGeometry(pt.Buffer(float(radius_m), QUAD_SEGMENTS))
    merged = circles.UnionCascaded()
    parts = _parts(merged)

    # 每个面里装了哪些点：先按外接矩形粗筛，再做真正的包含判断
    envs = [p.GetEnvelope() for p in parts]        # (minx, maxx, miny, maxy)
    owner: List[Optional[int]] = [None] * len(projected)
    for i, (x, y) in enumerate(projected):
        for j, (minx, maxx, miny, maxy) in enumerate(envs):
            if x < minx or x > maxx or y < miny or y > maxy:
                continue
            pt = ogr.Geometry(ogr.wkbPoint)
            pt.AddPoint_2D(x, y)
            if parts[j].Contains(pt) or parts[j].Touches(pt):
                owner[i] = j
                break
        if owner[i] is None:                         # 数值抖动兜底：就近归入
            owner[i] = min(range(len(parts)),
                           key=lambda j: (projected[i][0] - (envs[j][0] + envs[j][1]) / 2) ** 2
                           + (projected[i][1] - (envs[j][2] + envs[j][3]) / 2) ** 2)

    members: Dict[int, List[SampleKey]] = {}
    order: List[int] = []
    for i, j in enumerate(owner):
        if j not in members:
            members[j] = []
            order.append(j)
        if keys[i] not in members[j]:
            members[j].append(keys[i])

    out: List[Tuple[ogr.Geometry, List[SampleKey]]] = []
    for j in order:
        poly = parts[j].Clone()
        poly.Transform(to_geo)
        out.append((poly, members[j]))
    return out


def describe(radius_m: float, class_ids: Optional[Iterable[int]] = None,
             names: Optional[Dict[int, str]] = None) -> str:
    """给报告与描述文件写的一句话。"""
    text = "点要素按半径 %g m 缓冲成圆，重叠的合成一个面" % radius_m
    if class_ids is not None:
        picked = [names.get(c, str(c)) if names else str(c) for c in class_ids]
        text += "（只对：%s）" % "、".join(picked[:8]) + ("…" if len(picked) > 8 else "")
    return text


__all__ = ["buffer_points", "describe", "QUAD_SEGMENTS"]
