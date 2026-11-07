# -*- coding: utf-8 -*-
"""按类别算平均光谱：把"一堆位置"变成影像上的一片像元，再取平均。

三种"一堆位置"
--------------
* **样本库里的一个类别**——已经标好的样本点与图斑，天然带类别。
* **分类栅格图层**——值是 0、1、2… 的类别号，背景是 nodata。端元提取、监督分类
  的结果通常长这样。
* **矢量图层的某个属性**——按属性值分组，一个取值一条平均谱。端元结果导出成矢量
  时，端元类型一般就放在某个字段里。

三种来源在这里被抹平成同一个接口：给一个 :class:`Group`，还一片布尔掩膜（以及它
在影像上的左上角行列号）。取平均那一步谁都不用管，交给
:meth:`spectra.SpectrumReader.read_mask`。

**为什么统一到"影像的像元网格"上**：平均谱是这幅影像上的读数，别处的网格再准也
没用。矢量按像元坐标栅格化、分类栅格重采样到本影像的网格，都是为了这一件事。
栅格重采样一律用最近邻——类别号插值出来的 1.5 不是任何一类。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from osgeo import gdal, ogr, osr

from . import spectra

# =============================================================================
# 配置区
# =============================================================================

#: 栅格化一个图斑时，外接矩形最多允许多大（像元数）。超过就先按比例抽稀……
#: 实际上超过这个数说明选中的范围几乎覆盖整幅影像，那时直接用整幅的窗口即可，
#: 这个上限只是防止在超大影像上一次分配几百 MB 的掩膜。
MASK_MAX_CELLS = 200_000_000

#: 分类栅格找类别号时，最多读多少个像元来数有哪些取值。整幅读一遍在大影像上要
#: 几秒，而"有哪些类"这件事抽样看一眼就够了。
SCAN_MAX_CELLS = 4_000_000

#: 类别号超过这么多种就不当类别栅格看了——那多半是一幅连续值影像。
MAX_CLASSES = 256

#: 点样本落到影像上时，按这么大的窗口取（像元边长）。1 就是只取落进去的那一个。
POINT_WINDOW = 1

# =============================================================================


@dataclass
class Group:
    """一个可以单独算一条平均谱的类别。"""

    key: str
    name: str
    #: 这一类有多少个来源要素（样本数 / 要素数），栅格来源上是像元数
    count: int = 0
    color: str = ""
    detail: str = ""
    #: 上级类别的 key。样本库的类别是有层级的，树就靠它搭起来。
    parent: str = ""
    #: 这一类是背景（分类栅格里的 nodata / 无效值）。仍然可以算，只是默认不勾。
    background: bool = False


class GroupSource:
    """"一堆位置"的统一接口。"""

    #: 界面上显示的来源名
    title = ""

    def groups(self, reader=None) -> List[Group]:
        """有哪些类别。

        给了 ``reader`` 就**只返回落在这幅影像里的**，条数也按影像范围内的算。
        一个工区几百个类别、影像只盖住其中几个，把几百个全铺出来等于让人自己去
        找哪几个有用。
        """
        raise NotImplementedError

    def mask_for(self, group: Group, reader: "spectra.SpectrumReader"):
        """返回 ``(掩膜, (x0, y0))``，取不到就 None。"""
        raise NotImplementedError


# -----------------------------------------------------------------------------
# 几何 -> 影像像元
# -----------------------------------------------------------------------------

def _pixel_rings(rings, reader) -> List[List[Tuple[float, float]]]:
    """把若干环的经纬度坐标换成影像的浮点行列号。"""
    out = []
    for ring in rings:
        pts = []
        for lon, lat in ring:
            at = reader.to_pixel(lon, lat)
            if at is not None:
                pts.append(at)
        if len(pts) >= 3:
            out.append(pts)
    return out


def _bounds_of(rings, points) -> Optional[Tuple[int, int, int, int]]:
    xs: List[float] = []
    ys: List[float] = []
    for ring in rings:
        xs.extend(p[0] for p in ring)
        ys.extend(p[1] for p in ring)
    for x, y in points:
        xs.append(x)
        ys.append(y)
    if not xs:
        return None
    return (int(np.floor(min(xs))), int(np.floor(min(ys))),
            int(np.ceil(max(xs))) + 1, int(np.ceil(max(ys))) + 1)


def rasterize(rings, points, reader) -> Optional[Tuple[np.ndarray, Tuple[int, int]]]:
    """把像元坐标下的环与点烧成一张布尔掩膜。

    **在像元坐标里栅格化，不碰坐标系。** 几何早就用 ``reader.to_pixel`` 换算过
    了，这里给 MEM 栅格一个单位仿射（左上角就是窗口原点、一个像元一个单位），
    GDAL 那边就完全不用知道投影是什么，也就不会有第二次坐标变换带来的偏移。
    """
    width_img, height_img = reader.size
    box = _bounds_of(rings, points)
    if box is None:
        return None
    x0 = max(0, min(box[0], width_img))
    y0 = max(0, min(box[1], height_img))
    x1 = max(x0, min(box[2], width_img))
    y1 = max(y0, min(box[3], height_img))
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        return None
    if width * height > MASK_MAX_CELLS:
        return None

    mask = np.zeros((height, width), dtype=bool)
    for x, y in points:
        col, row = int(np.floor(x)) - x0, int(np.floor(y)) - y0
        if 0 <= col < width and 0 <= row < height:
            half = max(0, (POINT_WINDOW - 1) // 2)
            mask[max(0, row - half):row + half + 1,
                 max(0, col - half):col + half + 1] = True

    if rings:
        drv = gdal.GetDriverByName("MEM")
        target = drv.Create("", width, height, 1, gdal.GDT_Byte)
        target.SetGeoTransform((float(x0), 1.0, 0.0, float(y0), 0.0, 1.0))
        # GDAL 3.11 起 "Memory" 改名 "MEM"，旧名字只是还能用、每次都告警一次
        driver = ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")
        mem = driver.CreateDataSource("rings")
        layer = mem.CreateLayer("rings", None, ogr.wkbPolygon)
        for ring in rings:
            poly = ogr.Geometry(ogr.wkbPolygon)
            outline = ogr.Geometry(ogr.wkbLinearRing)
            for x, y in ring:
                outline.AddPoint_2D(float(x), float(y))
            if ring[0] != ring[-1]:
                outline.AddPoint_2D(float(ring[0][0]), float(ring[0][1]))
            poly.AddGeometry(outline)
            feat = ogr.Feature(layer.GetLayerDefn())
            feat.SetGeometry(poly)
            layer.CreateFeature(feat)
        gdal.RasterizeLayer(target, [1], layer, burn_values=[1])
        burned = target.GetRasterBand(1).ReadAsArray()
        if burned is not None:
            mask |= burned.astype(bool)
        target = None
        mem = None

    if not mask.any():
        return None
    return (mask, (x0, y0))


def _split_geometry(items, reader):
    """把一批 ``(geom_kind, coords)`` 拆成像元坐标下的环与点。"""
    rings: List[List[Tuple[float, float]]] = []
    points: List[Tuple[float, float]] = []
    for kind, coords in items:
        if not coords:
            continue
        if kind == "point":
            for lon, lat in coords:
                at = reader.to_pixel(lon, lat)
                if at is not None:
                    points.append(at)
        else:
            rings.extend(_pixel_rings(coords, reader))
    return (rings, points)


def _under(cls, roots: Sequence[int], classes) -> bool:
    """这个类别是不是挂在 ``roots`` 里某一支下面（含自身）。"""
    parents = {c.class_id: c.parent_id for c in classes}
    up = cls.class_id
    guard = 0
    while up is not None and guard < 64:
        if up in roots:
            return True
        up = parents.get(up)
        guard += 1
    return False


def _record_inside(record, reader) -> bool:
    """这条样本有没有一部分落在影像里。按外接矩形判，够快也够准。"""
    box = getattr(record, "bbox", None)
    width, height = reader.size
    corners = ((box[0], box[1]), (box[2], box[3])) if box and any(box) else None
    if corners is None:
        coords = getattr(record, "coords", None) or []
        flat = coords if record.geom_kind == "point" else [
            p for ring in coords for p in ring]
        if not flat:
            return False
        xs = [p[0] for p in flat]
        ys = [p[1] for p in flat]
        corners = ((min(xs), min(ys)), (max(xs), max(ys)))
    cols, rows = [], []
    for lon, lat in corners:
        at = reader.to_pixel(lon, lat)
        if at is None:
            return False
        cols.append(at[0])
        rows.append(at[1])
    return (max(cols) >= 0 and min(cols) <= width
            and max(rows) >= 0 and min(rows) <= height)


def _feature_inside(feature, reader) -> bool:
    box = getattr(feature, "bbox", None)
    if not box:
        return True
    width, height = reader.size
    cols, rows = [], []
    for lon, lat in ((box[0], box[1]), (box[2], box[3])):
        at = reader.to_pixel(lon, lat)
        if at is None:
            return False
        cols.append(at[0])
        rows.append(at[1])
    return (max(cols) >= 0 and min(cols) <= width
            and max(rows) >= 0 and min(rows) <= height)


# -----------------------------------------------------------------------------
# 来源一：样本库里的类别
# -----------------------------------------------------------------------------

#: 工区节点的 key 前缀。工区不是类别，它只是类别的分组，key 要跟类别 id 分开，
#: 否则"工区 3"和"class_id 3"会撞在一起。
REGION_PREFIX = "region:"

#: 没填工区的那些归到这一支下面，与样本树面板的写法保持一致
UNGROUPED = "（未分组）"


class SampleSource(GroupSource):
    """样本库里的类别，**顶层是工区**。

    树的形状必须跟样本树面板一致：工区 → 类别（可多级）→ 样本。把工区那一层去掉，
    同名类别分属两个工区时就并排摆着、认不出谁是谁。
    """

    title = "样本库"

    def __init__(self, session) -> None:
        self.session = session

    def groups(self, reader=None) -> List[Group]:
        by_class = getattr(self.session, "records_by_class", None) or {}
        classes = list(getattr(self.session, "classes", ()))
        counted: Dict[int, int] = {}
        for cls in classes:
            rows = by_class.get(cls.class_id) or []
            if reader is not None:
                rows = [r for r in rows if _record_inside(r, reader)]
            counted[cls.class_id] = len(rows)

        # 类别是有层级的：本身没有样本、但下面挂着有样本的子类，那一层也得留着，
        # 否则树断在中间，子类无处可挂。
        keep = {c.class_id for c in classes if counted.get(c.class_id)}
        parents = {c.class_id: c.parent_id for c in classes}
        for class_id in list(keep):
            up = parents.get(class_id)
            while up is not None and up not in keep:
                keep.add(up)
                up = parents.get(up)

        out = []
        regions: Dict[str, List[int]] = {}
        for cls in classes:
            if cls.class_id not in keep:
                continue
            if cls.parent_id is None:
                regions.setdefault(cls.region, []).append(cls.class_id)
        for region, members in regions.items():
            total = sum(counted.get(c.class_id, 0) for c in classes
                        if c.class_id in keep and _under(c, members, classes))
            out.append(Group(
                key=REGION_PREFIX + region, name=region or UNGROUPED,
                count=0,        # 工区只是分组，自己不算一条曲线
                detail="工区，共 %d 个类别、%d 个样本" % (
                    sum(1 for c in classes
                        if c.class_id in keep and _under(c, members, classes)
                        and counted.get(c.class_id, 0)), total)))
        for cls in classes:
            if cls.class_id not in keep:
                continue
            out.append(Group(
                key=str(cls.class_id), name=cls.display_name,
                count=counted.get(cls.class_id, 0),
                color=cls.color or "", detail=cls.region or cls.source,
                parent=(REGION_PREFIX + cls.region) if cls.parent_id is None
                else str(cls.parent_id)))
        return out

    def mask_for(self, group: Group, reader):
        if group.key.startswith(REGION_PREFIX):
            return None                   # 工区是分组，不是一条曲线
        by_class = getattr(self.session, "records_by_class", None) or {}
        rows = by_class.get(int(group.key)) or []
        items = [(r.geom_kind, r.coords) for r in rows]
        if not items:
            return None
        return rasterize(*_split_geometry(items, reader), reader=reader)


# -----------------------------------------------------------------------------
# 来源二：分类栅格图层
# -----------------------------------------------------------------------------

class ClassRasterSource(GroupSource):
    """一幅类别号栅格：0、1、2… 是类别，nodata 是背景。"""

    title = "分类栅格"

    def __init__(self, layer, background: Optional[Sequence[str]] = None) -> None:
        self.layer = layer
        self.path = getattr(layer, "path", "")
        self.name = getattr(layer, "name", "") or os.path.basename(self.path)
        self._cache: Dict[str, Tuple[np.ndarray, Tuple[int, int]]] = {}
        #: 哪些取值算背景。文件里声明了 nodata 就先按它来；没声明就空着，由使用者
        #: 自己指。二值图上背景常常就是 0，而 0 是个正经数字，程序猜不出来。
        self.background = set(background or self._declared_nodata())

    def _declared_nodata(self) -> List[str]:
        ds = gdal.Open(self.path, gdal.GA_ReadOnly)
        if ds is None:
            return []
        value = ds.GetRasterBand(1).GetNoDataValue()
        return [] if value is None else ["%g" % float(value)]

    def groups(self, reader=None) -> List[Group]:
        """把栅格里出现过的取值**全部**列出来，nodata 也在内，只是打个标签。

        以前是把 nodata 直接滤掉的，结果二值图上就只剩一个类：那种图往往压根没
        声明 nodata，0 就是个正经数字，程序猜不出它是背景。现在一律列出来，谁是
        背景由使用者指——也许他就是想看背景那一片的平均谱。
        """
        if reader is not None:
            warped = self._warp(reader)
            data, step = (warped[0], 1) if warped else (None, 1)
        else:
            data, step = self._scan()
        if data is None:
            return []
        data = np.asarray(data)
        good = np.isfinite(data) if data.dtype.kind == "f" else np.ones(
            data.shape, dtype=bool)
        values, counts = np.unique(data[good], return_counts=True)
        if values.size > MAX_CLASSES:
            return []
        names = self._names()
        out = []
        for value, count in zip(values, counts):
            key = "%g" % float(value)
            label = names.get(int(value), "") if float(value).is_integer() else ""
            # 自动生成的名字用英文：它会原样写到图例上，而图例按投稿要求是英文。
            # 栅格属性表里带了名字，那就照他写的来。
            out.append(Group(
                key=key, name=label or "Class %s" % key,
                count=int(count) * step * step,
                background=key in self.background,
                detail=("像元数按 1/%d 抽样估计" % (step * step)) if step > 1
                else ("影像范围内 %d 个像元" % (int(count) * step * step))))
        return out

    def _scan(self):
        """没有参照影像时，抽样看一眼这幅栅格里有哪些取值。"""
        ds = gdal.Open(self.path, gdal.GA_ReadOnly)
        if ds is None:
            return (None, 1)
        step = 1
        while (ds.RasterXSize // step) * (ds.RasterYSize // step) > SCAN_MAX_CELLS:
            step *= 2
        data = ds.GetRasterBand(1).ReadAsArray(
            buf_xsize=max(1, ds.RasterXSize // step),
            buf_ysize=max(1, ds.RasterYSize // step))
        return (data, step)

    def _names(self) -> Dict[int, str]:
        ds = gdal.Open(self.path, gdal.GA_ReadOnly)
        return self._category_names(ds.GetRasterBand(1)) if ds else {}

    @staticmethod
    def _category_names(band) -> Dict[int, str]:
        """栅格属性表 / 类别名里带的名字，有就用上。"""
        try:
            names = band.GetCategoryNames() or []
        except Exception:
            names = []
        return {i: n for i, n in enumerate(names) if n}

    def mask_for(self, group: Group, reader):
        warped = self._warp(reader)
        if warped is None:
            return None
        data, origin = warped
        want = float(group.key)
        return (data == want, origin)

    def _warp(self, reader):
        """把类别栅格重采样到**这幅影像的网格**上，只做两者重叠的那一块。"""
        key = reader.path or reader.name
        if key in self._cache:
            return self._cache[key]
        ds = reader.ds
        gt = ds.GetGeoTransform(can_return_null=True)
        if gt is None or gt[2] or gt[4]:
            return None                    # 带旋转的仿射不处理
        src = gdal.Open(self.path, gdal.GA_ReadOnly)
        if src is None:
            return None
        box = self._overlap(ds, gt, src)
        if box is None:
            return None
        x0, y0, width, height = box
        bounds = (gt[0] + x0 * gt[1], gt[3] + (y0 + height) * gt[5],
                  gt[0] + (x0 + width) * gt[1], gt[3] + y0 * gt[5])
        try:
            out = gdal.Warp("", src, format="MEM", outputBounds=bounds,
                            width=width, height=height,
                            dstSRS=ds.GetProjection() or None,
                            resampleAlg="near")
        except RuntimeError:
            return None
        if out is None:
            return None
        data = out.GetRasterBand(1).ReadAsArray()
        if data is None:
            return None
        result = (np.asarray(data), (x0, y0))
        self._cache[key] = result
        return result

    @staticmethod
    def _overlap(ds, gt, src) -> Optional[Tuple[int, int, int, int]]:
        """类别栅格与影像重叠的那一块，用影像的行列号表示。"""
        src_gt = src.GetGeoTransform(can_return_null=True)
        if src_gt is None:
            return None
        corners = [(src_gt[0], src_gt[3]),
                   (src_gt[0] + src.RasterXSize * src_gt[1],
                    src_gt[3] + src.RasterYSize * src_gt[5])]
        tf = None
        src_wkt, dst_wkt = src.GetProjection(), ds.GetProjection()
        if src_wkt and dst_wkt and src_wkt != dst_wkt:
            a, b = osr.SpatialReference(), osr.SpatialReference()
            if a.ImportFromWkt(src_wkt) == 0 and b.ImportFromWkt(dst_wkt) == 0:
                a.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                b.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                if not a.IsSame(b):
                    tf = osr.CoordinateTransformation(a, b)
        pts = []
        for x, y in corners:
            if tf is not None:
                try:
                    x, y, *_ = tf.TransformPoint(x, y)
                except Exception:
                    return None
            pts.append((x, y))
        inv = gdal.InvGeoTransform(gt)
        cols, rows = [], []
        for x, y in pts:
            cols.append(inv[0] + x * inv[1] + y * inv[2])
            rows.append(inv[3] + x * inv[4] + y * inv[5])
        x0 = max(0, int(np.floor(min(cols))))
        y0 = max(0, int(np.floor(min(rows))))
        x1 = min(ds.RasterXSize, int(np.ceil(max(cols))))
        y1 = min(ds.RasterYSize, int(np.ceil(max(rows))))
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0, y0, x1 - x0, y1 - y0)


# -----------------------------------------------------------------------------
# 来源三：矢量图层的某个属性
# -----------------------------------------------------------------------------

class VectorFieldSource(GroupSource):
    """按矢量图层某个字段的取值分组。"""

    title = "矢量属性"

    def __init__(self, layer, field: str) -> None:
        self.layer = layer
        self.field = field
        self.name = getattr(layer, "name", "")

    def groups(self, reader=None) -> List[Group]:
        counts: Dict[str, int] = {}
        for feat in getattr(self.layer, "features", ()):
            if reader is not None and not _feature_inside(feat, reader):
                continue
            text = self.layer.value_text((feat.attrs or {}).get(self.field))
            counts[text] = counts.get(text, 0) + 1
        colors = getattr(self.layer, "color_map", {}) or {}
        return [Group(key=text, name=text, count=n, color=colors.get(text, ""),
                      detail="%s = %s" % (self.field, text))
                for text, n in counts.items()]

    def mask_for(self, group: Group, reader):
        items = []
        for feat in getattr(self.layer, "features", ()):
            text = self.layer.value_text((feat.attrs or {}).get(self.field))
            if text == group.key:
                items.append((feat.geom_kind, feat.coords))
        if not items:
            return None
        return rasterize(*_split_geometry(items, reader), reader=reader)


# -----------------------------------------------------------------------------
# 算
# -----------------------------------------------------------------------------

@dataclass
class Result:
    group: Group
    spectrum: Optional[spectra.Spectrum] = None
    problem: str = ""


#: 取消时写进 ``Result.problem`` 的原因
CANCELLED = "已取消"


def mean_spectra(source: GroupSource, groups: Sequence[Group],
                 reader, progress=None, block_progress=None) -> List[Result]:
    """给每个类别算一条平均谱。

    算不出来的那些不静静跳过，而是带着原因一起返回——"这一类一个像元都没落在
    影像上"和"这一类算出来是这样"是两件完全不同的事，界面得说得出区别。
    """
    out: List[Result] = []
    cancelled = [False]

    def block(done: int, total: int) -> bool:
        ok = block_progress(done, total) if block_progress is not None else True
        if not ok:
            cancelled[0] = True
        return ok

    for i, group in enumerate(groups, 1):
        if progress is not None and not progress(i, len(groups), group.name):
            out.append(Result(group, None, CANCELLED))
            break
        try:
            found = source.mask_for(group, reader)
        except Exception as exc:                    # pragma: no cover - GDAL
            out.append(Result(group, None, "取范围时出错：%s" % exc))
            continue
        if found is None:
            out.append(Result(group, None, "没有像元落在这幅影像的范围内"))
            continue
        mask, origin = found
        spec = reader.read_mask(mask, origin, label=group.name, progress=block)
        if cancelled[0]:
            # 读到一半被取消：read_mask 返回 None，不能报成"全是无效值"
            out.append(Result(group, None, CANCELLED))
            break
        if spec is None:
            out.append(Result(group, None, "范围内的像元全是无效值"))
            continue
        if group.color:
            spec.color = group.color
        out.append(Result(group, spec))
    return out
