# -*- coding: utf-8 -*-
"""图层：影像、配准图片、参考矢量、手绘范围（感兴趣区）。

图层对象就是 :mod:`samplelib.annotate.layers` 里那几个类，一行不改：

* 栅格类按视窗出图：前端的 OpenLayers 以 WMS 的格式来要图（BBOX / WIDTH /
  HEIGHT / SRS），这里拼成 :class:`~samplelib.annotate.layers.ViewSpec` 交给
  ``render_array``，把 RGBA 数组编成 PNG 送回去。每个图层自带渲染锁，不同图层
  可以并行出图，不碰样本库那把锁。
* 矢量类整层发 GeoJSON，由前端画（VectorImage 渲染，拖动时贴图、停下再重画）。顶点太多时按视觉上看不出的容差抽稀。

图层顺序、样本图层的显隐由前端管；这里只存图层本身，并能把它们写成
:mod:`~samplelib.annotate.layerspec` 的描述，供会话状态记下、下次铺回来。
"""

import io
import os
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import HTTPException, Request
from fastapi.responses import Response
from osgeo import ogr, osr
from pydantic import BaseModel

from samplelib import config
from samplelib import legend as legend_mod
from samplelib.annotate import layers as L
from samplelib.annotate import layerspec
from samplelib.annotate.editing import _nest_rings
from samplelib.roi import roi_from_features, roi_from_raster
from samplelib.store import open_vector

from . import raster_cache
from .tasks import Task

# 大影像的显示金字塔在后台建（见 raster_cache）；register 时装上
_stand_ins: Optional[raster_cache.StandInManager] = None

# 整层发给前端的顶点数上限。超过就按容差抽稀，直到落进这个数。实测一百多万
# 个顶点的 GeoJSON 在 WebView 里解析加首帧不到两秒，再多就明显卡了。
MAX_VERTICES = 1_500_000

# 透明的 1x1 PNG：视窗与图层不相交时直接回它，省得前端报一张坏图
_EMPTY_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
              b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01"
              b"\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


class AddBody(BaseModel):
    paths: List[str]


class PatchBody(BaseModel):
    name: Optional[str] = None
    visible: Optional[bool] = None
    opacity: Optional[float] = None
    bands: Optional[List[int]] = None
    stretch: Optional[List[float]] = None
    use_color_table: Optional[bool] = None
    wavelength_file: Optional[str] = None   # "" 表示不套用
    color: Optional[str] = None
    fill: Optional[bool] = None
    fill_alpha: Optional[int] = None
    line_width: Optional[float] = None


class UniqueBody(BaseModel):
    field: str = ""                          # 空：取消唯一值赋色
    colors: Optional[Dict[str, str]] = None


class FieldBody(BaseModel):
    field: str


class RingsBody(BaseModel):
    rings: List[List[List[float]]]


class SelectBody(BaseModel):
    mode: str = config.DEFAULT_ROI_MODE
    class_ids: Optional[List[int]] = None    # 只在这些类别里筛（检索结果）
    add: bool = False


class ExtentBody(BaseModel):
    valid_only: bool = True


class SaveBody(BaseModel):
    path: str


class RestoreBody(BaseModel):
    specs: List[Dict[str, Any]]


# =============================================================================
# 工具
# =============================================================================

def layer_json(layer) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": layer.uid, "kind": layer.kind, "kind_cn": layer.kind_cn,
        "name": layer.name, "visible": bool(layer.visible),
        "opacity": float(layer.opacity), "extent": layer.extent(),
        "info": list(layer.info_lines), "error": layer.error,
        "path": getattr(layer, "path", ""),
    }
    if isinstance(layer, L.RasterLayer):
        out.update(
            band_count=layer.band_count, bands=list(layer.bands),
            band_labels=[layer.band_label(b) for b in range(1, layer.band_count + 1)],
            stretch=list(layer.stretch_percent),
            has_color_table=layer.color_table is not None,
            color_table_size=len(layer.color_table) if layer.color_table is not None else 0,
            use_color_table=bool(layer.use_color_table),
            wavelengths=bool(layer.wavelengths),
            wavelength_source=layer.wavelength_source,
            wavelength_presets=layer.wavelength_presets(),
            # 0..1：显示金字塔正在后台准备；None：用不着或已就绪
            preparing=_stand_ins.progress.get(layer.uid) if _stand_ins else None,
        )
    if L.is_styled_vector(layer):
        out.update(color=layer.color, fill=bool(layer.fill),
                   fill_alpha=int(layer.fill_alpha), line_width=float(layer.line_width),
                   feature_count=len(layer.features),
                   fields=list(getattr(layer, "fields", [])),
                   color_field=layer.color_field, color_map=dict(layer.color_map))
    return out


def _ring_count(feats) -> int:
    n = 0
    for f in feats:
        if f.geom_kind == "point":
            n += 1
        else:
            n += sum(len(r) for r in f.coords)
    return n


def _simplify(coords: List[np.ndarray], kind: str, tol: float) -> List[list]:
    """按容差抽稀一个要素的各条环 / 线。抽完不足以成形的保留原样。"""
    out = []
    for ring in coords:
        arr = np.asarray(ring, dtype=np.float64)
        if tol <= 0 or len(arr) < 8:
            out.append(arr)
            continue
        g = ogr.Geometry(ogr.wkbLineString)
        for x, y in arr:
            g.AddPoint_2D(float(x), float(y))
        s = g.Simplify(tol)
        pts = s.GetPoints() if s is not None else None
        need = 4 if kind == "polygon" else 2
        out.append(np.asarray([p[:2] for p in pts]) if pts and len(pts) >= need else arr)
    return out


def _closed(ring) -> list:
    pts = [[round(float(x), 7), round(float(y), 7)] for x, y in ring]
    if pts and pts[0] != pts[-1]:
        pts.append(list(pts[0]))
    return pts


def vector_geojson(layer) -> Dict[str, Any]:
    feats = layer.features
    total = _ring_count(feats)
    tol = 0.0
    if total > MAX_VERTICES and layer.extent():
        x0, y0, x1, y1 = layer.extent()
        # 容差从全图宽度的十万分之一起步，按超出的倍数放大：一般一两轮就落进上限
        tol = max(x1 - x0, y1 - y0) / 100000.0 * (total / MAX_VERTICES)
    out = []
    for f in feats:
        if f.geom_kind == "point":
            pts = [[round(float(x), 7), round(float(y), 7)] for x, y in f.coords]
            geom = ({"type": "Point", "coordinates": pts[0]} if len(pts) == 1
                    else {"type": "MultiPoint", "coordinates": pts})
        else:
            rings = _simplify(f.coords, f.geom_kind, tol)
            if f.geom_kind == "line":
                geom = {"type": "MultiLineString",
                        "coordinates": [[[round(float(x), 7), round(float(y), 7)] for x, y in r]
                                        for r in rings]}
            else:
                # 多部件面在 layers 里被拍平成一串环：按包含关系装回去，否则互不相干
                # 的两块会被画成"一块挖掉另一块"
                plain = [[(float(x), float(y)) for x, y in r] for r in rings if len(r) >= 3]
                parts = _nest_rings(plain) if len(plain) > 1 else [plain]
                geom = {"type": "MultiPolygon",
                        "coordinates": [[_closed(r) for r in part] for part in parts]}
        out.append({"type": "Feature", "id": f.fid, "geometry": geom,
                    "properties": {"fid": f.fid, "c": layer.feature_color(f),
                                   "kind": f.geom_kind}})
    return {"type": "FeatureCollection", "features": out,
            "simplified": tol > 0, "vertices": total}


def _rgba_png(arr: np.ndarray) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def _lonlat_bounds(bounds, epsg: int):
    """投影坐标的视窗 -> 经纬度外包。四角加四边中点一起换，斜着的格网也包得住。"""
    if epsg == 4326:
        return bounds
    src = osr.SpatialReference()
    src.ImportFromEPSG(int(epsg))
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)
    x0, y0, x1, y1 = bounds
    xs, ys = [], []
    for x in (x0, (x0 + x1) / 2, x1):
        for y in (y0, (y0 + y1) / 2, y1):
            try:
                lon, lat, _ = tr.TransformPoint(x, y)
            except Exception:
                continue
            xs.append(lon)
            ys.append(lat)
    if not xs:
        return bounds
    return (min(xs), min(ys), max(xs), max(ys))


def looks_like_library(path: str) -> bool:
    """一个 gpkg 是不是样本库（有 sl_class 表），而不是普通矢量。"""
    from osgeo import gdal
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        ds = open_vector(path)
        names = {ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())}
        return "sl_class" in names and "samples_point" in names
    except Exception:
        return False
    finally:
        gdal.PopErrorHandler()


# =============================================================================
# 路由
# =============================================================================

def register(app, st, guard) -> None:
    global _stand_ins
    st.layers = OrderedDict()                # uid -> Layer
    _stand_ins = st.stand_ins = raster_cache.StandInManager()
    # 出图请求的代号：每个栅格图层只画最新的那一张。平移、缩放一次 OpenLayers 就
    # 发一个新请求，旧的还在排队；图一大，一张要几秒，排着的旧请求照画不误，
    # 越积越多，界面就像卡死了。轮到时发现后面已经来了新的，就直接放弃
    wms_seq: Dict[int, int] = {}
    wms_gate: Dict[int, Any] = {}
    s = st.session

    def get(layer_id: int):
        layer = st.layers.get(int(layer_id))
        if layer is None:
            raise HTTPException(404, "图层不存在，可能已被移除。")
        return layer

    def styled(layer_id: int):
        layer = get(layer_id)
        if not L.is_styled_vector(layer):
            raise HTTPException(400, "「%s」不是矢量图层。" % layer.name)
        return layer

    @app.get("/api/layers")
    def list_layers():
        return [layer_json(l) for l in st.layers.values()]

    @app.post("/api/layers/add")
    def add_layers(body: AddBody):
        """按文件类型分派：样本库交回前端去打开；影像、矢量加成图层；其余报原因。"""
        libs = [p for p in body.paths if p.lower().endswith(".gpkg") and looks_like_library(p)]
        if libs:
            return {"library": libs[0]}

        def work(task: Task):
            added, failed = [], []
            for i, p in enumerate(body.paths, 1):
                task(i - 1, len(body.paths), os.path.basename(p))
                kind = L.classify_drop(p)
                try:
                    if kind in ("raster", "image"):
                        cls = L.ImageLayer if kind == "image" else L.RasterLayer
                        layer = cls(p)
                        if kind == "image":
                            layer.opacity = 0.75
                    elif kind == "vector":
                        layer = L.VectorLayer(p, color=config.class_color(len(st.layers) + 1))
                    else:
                        failed.append("%s：不是可加载的影像或矢量文件" % os.path.basename(p))
                        continue
                except Exception as exc:
                    failed.append("%s：%s" % (os.path.basename(p), str(exc).splitlines()[0]))
                    continue
                st.layers[layer.uid] = layer
                _stand_ins.schedule(layer)
                added.append(layer_json(layer))
            task(len(body.paths), len(body.paths), "")
            return {"added": added, "failed": failed}

        try:
            task = st.tasks.start("加载图层", work, text="正在读取…", cancellable=False)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"task": task.to_json()}

    @app.patch("/api/layers/{layer_id}")
    def patch_layer(layer_id: int, body: PatchBody):
        layer = get(layer_id)
        if body.name is not None and body.name.strip():
            layer.name = body.name.strip()
        if body.visible is not None:
            layer.visible = bool(body.visible)
        if body.opacity is not None:
            layer.opacity = max(0.0, min(1.0, float(body.opacity)))
        if isinstance(layer, L.RasterLayer):
            if body.use_color_table is not None:
                layer.set_use_color_table(body.use_color_table)
            if body.bands:
                layer.set_bands(body.bands)
            if body.use_color_table is not None or body.bands:
                _stand_ins.schedule(layer)          # 换了显示的波段，替身要另建一份
            if body.stretch and len(body.stretch) == 2:
                lo, hi = sorted(float(v) for v in body.stretch)
                layer.set_stretch(max(0.0, lo), min(100.0, hi))
            if body.wavelength_file is not None:
                if body.wavelength_file:
                    label = next((k for k, v in layer.wavelength_presets().items()
                                  if v == body.wavelength_file), "")
                    if not layer.load_wavelengths(body.wavelength_file, label):
                        raise HTTPException(400, "波长表的波段数与影像（%d 个波段）不一致，未应用。"
                                            % layer.band_count)
                else:
                    layer.wavelengths = []
                    layer.wavelength_source = ""
                    layer._wavelength_file = ""
                    layer._refresh_wavelength_info()
        if L.is_styled_vector(layer):
            if body.color:
                layer.color = body.color
            if body.fill is not None:
                layer.fill = bool(body.fill)
            if body.fill_alpha is not None:
                layer.fill_alpha = max(0, min(255, int(body.fill_alpha)))
            if body.line_width is not None:
                layer.line_width = max(0.2, float(body.line_width))
        return layer_json(layer)

    @app.delete("/api/layers/{layer_id}")
    def remove_layer(layer_id: int):
        layer = get(layer_id)
        st.layers.pop(layer.uid, None)
        _stand_ins.cancel(layer.uid)
        layer.close()
        return {"ok": True}

    # ------------------------------------------------------------- 出图
    @app.get("/api/layers/{layer_id}/wms")
    def render(layer_id: int, request: Request):
        """WMS 1.1.1 GetMap 的子集：BBOX、WIDTH、HEIGHT、SRS。"""
        layer = get(layer_id)
        if not isinstance(layer, L.RasterLayerBase):
            raise HTTPException(400, "不是栅格图层。")
        q = {k.upper(): v for k, v in request.query_params.items()}
        try:
            bounds = tuple(float(v) for v in q["BBOX"].split(","))
            width, height = int(q["WIDTH"]), int(q["HEIGHT"])
            epsg = int((q.get("SRS") or q.get("CRS") or "EPSG:4326").split(":")[-1])
        except (KeyError, ValueError):
            raise HTTPException(400, "缺少 BBOX / WIDTH / HEIGHT。")
        width, height = max(1, min(width, 4096)), max(1, min(height, 4096))
        view = L.ViewSpec(bounds=bounds, width=width, height=height, epsg=epsg,
                          lonlat=_lonlat_bounds(bounds, epsg))
        seq = wms_seq[layer.uid] = wms_seq.get(layer.uid, 0) + 1
        gate = wms_gate.setdefault(layer.uid, threading.Lock())
        with gate:
            if wms_seq.get(layer.uid) != seq:
                # 已经有更新的请求：这一张没人要了。204 让 OpenLayers 当作没取到，
                # 它本来就只显示最新视窗那一张
                return Response(status_code=204)
            try:
                arr = layer.render_array(view)
            except Exception as exc:
                layer.error = str(exc).splitlines()[0]
                raise HTTPException(500, layer.error)
        if arr is None:
            return Response(_EMPTY_PNG, media_type="image/png")
        return Response(_rgba_png(arr), media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/layers/{layer_id}/geojson")
    def geojson(layer_id: int):
        return vector_geojson(styled(layer_id))

    @app.get("/api/layers/{layer_id}/features/{fid}")
    def feature_attrs(layer_id: int, fid: int):
        layer = styled(layer_id)
        for f in layer.features:
            if f.fid == fid:
                return {"fid": f.fid, "kind": f.geom_kind, "attrs": {
                    k: (v if isinstance(v, (int, float, str, bool)) or v is None else str(v))
                    for k, v in (f.attrs or {}).items()}}
        raise HTTPException(404, "要素不存在。")

    # ------------------------------------------------------------- 唯一值赋色
    @app.get("/api/layers/{layer_id}/values")
    def unique_values(layer_id: int, field: str):
        layer = styled(layer_id)
        if not isinstance(layer, L.VectorLayer):
            raise HTTPException(400, "手绘范围没有属性表。")
        values = layer.unique_values(field)
        current = layer.color_map if layer.color_field == field else {}
        return {"field": field,
                "values": [{"value": v, "color": current.get(v) or config.class_color(i)}
                           for i, v in enumerate(values)],
                "palette_size": len(config.CLASS_PALETTE)}

    @app.post("/api/layers/{layer_id}/unique-colors")
    def set_unique(layer_id: int, body: UniqueBody):
        layer = styled(layer_id)
        if not isinstance(layer, L.VectorLayer):
            raise HTTPException(400, "手绘范围没有属性表。")
        if body.field:
            layer.set_unique_colors(body.field, body.colors or {})
        else:
            layer.clear_unique_colors()
        return layer_json(layer)

    @app.post("/api/layers/{layer_id}/legend-colors")
    def legend_colors(layer_id: int, body: FieldBody):
        """按岩性图例库认取值、套标准配色。认不出的保持原样，不硬套。"""
        layer = styled(layer_id)
        lg = legend_mod.LithologyLegend.shared()
        if lg is None:
            raise HTTPException(400, "找不到 samplelib/data/lithology_legend.json。"
                                "运行 tools/build_legend_library.py 生成一份后再试。")
        values = layer.unique_values(body.field)
        colors, unknown = lg.color_map(values)
        hits = {}
        for v, h in lg.match_all(values).items():
            if h is not None:
                hits[v] = {"matched": h.matched, "rule": legend_mod.rule_label(h.rule),
                           "path": " / ".join(c.display for c in lg.path_of(h.cls.id)[-3:])}
        return {"colors": colors, "unknown": unknown, "hits": hits, "total": len(values)}

    # ------------------------------------------------------------- 感兴趣区
    @app.post("/api/layers/drawn")
    def new_drawn():
        layer = L.DrawnLayer(name="感兴趣区")
        st.layers[layer.uid] = layer
        return layer_json(layer)

    @app.post("/api/layers/{layer_id}/polygons")
    def add_polygon(layer_id: int, body: RingsBody):
        layer = get(layer_id)
        if not isinstance(layer, L.DrawnLayer):
            raise HTTPException(400, "只能在手绘范围图层中绘制。")
        rings = [[(float(x), float(y)) for x, y in r] for r in body.rings if len(r) >= 3]
        if not rings:
            raise HTTPException(400, "范围至少需要三个顶点。")
        layer.add_polygon(rings)
        return layer_json(layer)

    @app.post("/api/layers/{layer_id}/clear")
    def clear_drawn(layer_id: int):
        layer = get(layer_id)
        if isinstance(layer, L.DrawnLayer):
            layer.clear()
        return layer_json(layer)

    @app.post("/api/layers/{layer_id}/select-samples")
    def select_samples(layer_id: int, body: SelectBody):
        """用这个图层的范围筛样本，结果进勾选。筛选方式有相交、完全包含、中心点在内三种。"""
        layer = get(layer_id)
        feats = getattr(layer, "features", None)
        if not feats:
            raise HTTPException(400, "「%s」中没有要素，无法按范围选择。" % layer.name)

        def run():
            s.set_roi_from_features(feats, name=layer.name)
            ids = body.class_ids if body.class_ids else None
            n = s.apply_roi(body.mode, class_ids=ids, add=body.add)
            return {"hits": n, "selected": len(s.selection)}
        return guard(run)

    @app.post("/api/roi/apply")
    def roi_again(body: SelectBody):
        """拿当前那个范围再筛一次。"""
        def run():
            if s.roi is None:
                raise ValueError("尚未按任何范围选择过样本。")
            n = s.apply_roi(body.mode, class_ids=body.class_ids or None, add=body.add)
            return {"hits": n, "selected": len(s.selection), "name": s.roi.name}
        return guard(run)

    @app.post("/api/layers/{layer_id}/extent-layer")
    def extent_layer(layer_id: int, body: ExtentBody):
        """影像 -> 范围图层（有效数据轮廓或四至）。生成的是图层，不直接筛。"""
        layer = get(layer_id)
        if not isinstance(layer, L.RasterLayer):
            raise HTTPException(400, "只有影像图层能生成范围。")

        def work(task: Task):
            task.note("正在勾画有效数据轮廓…" if body.valid_only else "正在取四至…")
            roi = roi_from_raster(layer.path, valid_only=body.valid_only)
            drawn = L.DrawnLayer(name="%s 的范围" % layer.name)
            n = drawn.add_geometry(roi.geom, name="范围")
            st.layers[drawn.uid] = drawn
            return {"layer": layer_json(drawn), "parts": n}

        try:
            task = st.tasks.start("生成范围图层", work, cancellable=False)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"task": task.to_json()}

    @app.post("/api/layers/{layer_id}/save")
    def save_drawn(layer_id: int, body: SaveBody):
        layer = get(layer_id)
        if not isinstance(layer, L.DrawnLayer):
            raise HTTPException(400, "只有手绘范围能导出。")
        if not layer.features:
            raise HTTPException(400, "该范围图层为空。")
        try:
            path = layer.save_as(body.path)
        except Exception as exc:
            raise HTTPException(400, str(exc))
        return {"path": path, "count": layer.count}

    # ------------------------------------------------------------- 会话状态
    @app.get("/api/layers/specs")
    def specs():
        """当前图层写成可序列化的描述（相对 state/ 目录存路径），给界面状态记下。"""
        from samplelib import appstate
        base = os.path.dirname(appstate.state_path())
        out = []
        for l in st.layers.values():
            spec = layerspec.layer_to_spec(l, base)
            if spec is not None:
                spec["id"] = l.uid
                out.append(spec)
            elif isinstance(l, L.DrawnLayer) and l.features:
                out.append({"id": l.uid, "type": "drawn", "name": l.name,
                            "visible": l.visible, "opacity": l.opacity, "color": l.color,
                            "polygons": [[[list(map(float, p)) for p in r] for r in f.coords]
                                         for f in l.features]})
        return out

    @app.post("/api/layers/restore")
    def restore(body: RestoreBody):
        """按描述把图层铺回来。缺文件的跳过，原因汇总给界面。返回旧 id -> 新图层。"""
        from samplelib import appstate
        base = os.path.dirname(appstate.state_path())
        mapping, failed = {}, []
        for spec in body.specs:
            try:
                if spec.get("type") == "drawn":
                    layer = L.DrawnLayer(name=spec.get("name") or "感兴趣区",
                                         color=spec.get("color") or config.DEFAULT_VECTOR_COLOR)
                    for rings in spec.get("polygons") or []:
                        layer.add_polygon([[tuple(p) for p in r] for r in rings])
                    layer.visible = bool(spec.get("visible", True))
                    layer.opacity = float(spec.get("opacity", 1.0))
                else:
                    layer = layerspec.spec_to_layer(spec, base)
            except Exception as exc:
                failed.append("%s：%s" % (spec.get("name") or spec.get("path") or "?",
                                         str(exc).splitlines()[0]))
                continue
            st.layers[layer.uid] = layer
            _stand_ins.schedule(layer)
            mapping[str(spec.get("id"))] = layer_json(layer)
        return {"mapping": mapping, "failed": failed}

