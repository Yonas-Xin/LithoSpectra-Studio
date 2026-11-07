# -*- coding: utf-8 -*-
"""样本坐标结构与 GeoJSON 之间的互转。

存储层给界面的坐标是拍平的：面样本是一串环（外环、内环混在一起，环不带闭合
点），见 :func:`samplelib.store.geom_to_coords`。浏览器端的地图库要的是标准
GeoJSON，这里负责两头翻译；环的层次按包含关系还原，与写库时用的是同一套规则。
"""

from typing import Any, Dict, List

from samplelib.annotate.editing import _nest_rings
from samplelib.model import GEOM_POINT, GEOM_POLYGON, SampleRecord, key_to_str


def _closed(ring) -> List[List[float]]:
    pts = [[float(x), float(y)] for x, y in ring]
    if pts and pts[0] != pts[-1]:
        pts.append(list(pts[0]))
    return pts


def _open(ring) -> List[List[float]]:
    pts = [[float(p[0]), float(p[1])] for p in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def coords_to_geometry(geom_kind: str, coords) -> Dict[str, Any]:
    """拍平坐标 -> GeoJSON 几何。点为 Point，面一律为 MultiPolygon。"""
    if geom_kind == GEOM_POINT:
        x, y = coords[0]
        return {"type": "Point", "coordinates": [float(x), float(y)]}
    rings = [r for r in (coords or []) if r and len(r) >= 3]
    parts = _nest_rings(rings) if rings else []
    return {"type": "MultiPolygon",
            "coordinates": [[_closed(r) for r in part] for part in parts]}


def geometry_to_coords(geometry: Dict[str, Any]):
    """GeoJSON 几何 -> ``(geom_kind, 拍平坐标)``。"""
    gtype = geometry.get("type")
    c = geometry.get("coordinates")
    if gtype == "Point":
        return GEOM_POINT, [(float(c[0]), float(c[1]))]
    if gtype == "MultiPoint" and len(c) == 1:
        return GEOM_POINT, [(float(c[0][0]), float(c[0][1]))]
    if gtype == "Polygon":
        return GEOM_POLYGON, [_open(r) for r in c]
    if gtype == "MultiPolygon":
        return GEOM_POLYGON, [_open(r) for poly in c for r in poly]
    raise ValueError("不支持的几何类型：%s" % gtype)


def record_to_feature(rec: SampleRecord) -> Dict[str, Any]:
    return {
        "type": "Feature",
        "id": key_to_str(rec.key),
        "geometry": coords_to_geometry(rec.geom_kind, rec.coords),
        "properties": {
            "key": key_to_str(rec.key),
            "class_id": rec.class_id,
            "label": rec.label,
            "kind": rec.geom_kind,
            "draft": rec.key[1] < 0,
        },
    }
