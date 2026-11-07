# -*- coding: utf-8 -*-
"""样本库存储层：单文件 GeoPackage。

设计要点
--------
一个样本库 = 一个 .gpkg 文件，内部结构：

* ``samples_point``   点样本图层（EPSG:4326）
* ``samples_polygon`` 面样本图层（EPSG:4326，统一存为 MultiPolygon）
* ``sl_class``        类别表（非空间表），表的 FID 即 class_id
* ``sl_meta``         库级元数据表（非空间表），key/value 形式

样本主键为 ``(几何类别, FID)``，点与面各自独立编号。
所有几何统一投影到 EPSG:4326 入库，原始坐标系记录在 ``native_crs`` 字段中，
导出时可还原回原坐标系。
"""

import json
import os
from contextlib import contextmanager
from datetime import date, datetime
from typing import (Callable, Dict, Iterable, Iterator, List, Optional,
                    Sequence, Set, Tuple)

from osgeo import gdal, ogr, osr

from . import config
from .model import (GEOM_POINT, GEOM_POLYGON, SampleClass, SampleKey,
                    SampleRecord)

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()


# =============================================================================
# 打开文件
# =============================================================================
# 上面这三行是进程全局开关，一旦 import 过本模块，整个程序里的 GDAL 打不开
# 文件时都会抛 RuntimeError，而不再返回 None。于是各处写的
# ``if ds is None: raise IOError("无法打开…")`` 全成了走不到的死分支，使用者
# 看到的是 GDAL 的英文原文。下面两个函数把两条路径都接住，统一成中文异常，
# 打开文件一律走它们。

def _open(path: str, flags: int, what: str) -> gdal.Dataset:
    if not path:
        raise IOError("没有给出%s路径。" % what)
    try:
        ds = gdal.OpenEx(path, flags)
    except RuntimeError as exc:
        raise IOError("无法作为%s打开：%s\n%s" % (what, path, exc)) from exc
    if ds is None:                       # 关掉异常模式时才会走到这里
        raise IOError("无法作为%s打开：%s" % (what, path))
    return ds


def open_vector(path: str, update: bool = False) -> gdal.Dataset:
    """打开矢量文件，失败时抛出带中文说明的 :class:`IOError`。"""
    return _open(path, gdal.OF_VECTOR |
                 (gdal.OF_UPDATE if update else gdal.OF_READONLY), "矢量")


def open_raster(path: str) -> gdal.Dataset:
    """打开影像文件，失败时抛出带中文说明的 :class:`IOError`。"""
    return _open(path, gdal.OF_RASTER | gdal.OF_READONLY, "影像")


# =============================================================================
# 坐标系与几何工具
# =============================================================================

def storage_srs() -> osr.SpatialReference:
    """样本库统一存储坐标系（EPSG:4326，经度在前）。"""
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(config.STORAGE_EPSG)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def srs_from_text(text: str) -> Optional[osr.SpatialReference]:
    """从 "EPSG:32646" 或 WKT 文本构造坐标系。"""
    if not text:
        return None
    srs = osr.SpatialReference()
    try:
        if text.upper().startswith("EPSG:"):
            srs.ImportFromEPSG(int(text.split(":", 1)[1]))
        else:
            srs.ImportFromWkt(text)
    except Exception:
        return None
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def srs_identity(srs: Optional[osr.SpatialReference]) -> str:
    """把坐标系压缩成一段可存储的短文本，优先使用 EPSG 代码。"""
    if srs is None:
        return ""
    try:
        srs = srs.Clone()
        srs.AutoIdentifyEPSG()
        auth, code = srs.GetAuthorityName(None), srs.GetAuthorityCode(None)
        if auth and code:
            return "%s:%s" % (auth, code)
    except Exception:
        pass
    try:
        return srs.ExportToWkt()
    except Exception:
        return ""


def make_transform(src: Optional[osr.SpatialReference],
                   dst: Optional[osr.SpatialReference] = None
                   ) -> Optional[osr.CoordinateTransformation]:
    """构造坐标变换；源与目标一致或源未知时返回 None（表示无需变换）。"""
    if src is None:
        return None
    dst = dst if dst is not None else storage_srs()
    src = src.Clone()
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    if src.IsSame(dst):
        return None
    return osr.CoordinateTransformation(src, dst)


def geom_kind_of(geom: ogr.Geometry) -> Optional[str]:
    """判断几何属于点样本还是面样本；线等其它类型返回 None。"""
    t = ogr.GT_Flatten(geom.GetGeometryType())
    if t in (ogr.wkbPoint, ogr.wkbMultiPoint):
        return GEOM_POINT
    if t in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
        return GEOM_POLYGON
    return None


def _ring_coords(ring: ogr.Geometry) -> List[Tuple[float, float]]:
    """环 -> 顶点列表，**不带闭合点**。

    OGR 的线性环首尾重复同一个点；界面这边的环一律不重复（画草图时也不会重
    复），两种混着来会出事：顶点编辑把首尾两个重合的把手当成两个顶点，拖第一
    个的时候闭合点留在原地成了一根刺，删第一个则等于没删——闭合点还在原位。
    坐标转回几何时 :func:`samplelib.annotate.editing.coords_to_geom` 会重新闭合。
    """
    pts = [(ring.GetX(j), ring.GetY(j)) for j in range(ring.GetPointCount())]
    if len(pts) > 3 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def geom_to_coords(geom: ogr.Geometry) -> Tuple[str, list]:
    """OGR 几何 -> 纯 Python 坐标结构，供界面绘制使用。面的环不带闭合点。"""
    t = ogr.GT_Flatten(geom.GetGeometryType())
    if t == ogr.wkbPoint:
        return GEOM_POINT, [(geom.GetX(), geom.GetY())]
    if t == ogr.wkbMultiPoint:
        pts = []
        for i in range(geom.GetGeometryCount()):
            p = geom.GetGeometryRef(i)
            pts.append((p.GetX(), p.GetY()))
        return GEOM_POINT, pts
    if t == ogr.wkbPolygon:
        return GEOM_POLYGON, [_ring_coords(geom.GetGeometryRef(i))
                              for i in range(geom.GetGeometryCount())]
    if t == ogr.wkbMultiPolygon:
        rings = []
        for i in range(geom.GetGeometryCount()):
            _, sub = geom_to_coords(geom.GetGeometryRef(i))
            rings.extend(sub)
        return GEOM_POLYGON, rings
    return GEOM_POLYGON, []


# =============================================================================
# 样本库
# =============================================================================

class SampleStore:
    """单文件 GeoPackage 样本库的读写接口。"""

    LAYER_POINT = "samples_point"
    LAYER_POLYGON = "samples_polygon"
    TABLE_CLASS = "sl_class"
    TABLE_META = "sl_meta"

    SAMPLE_FIELDS = [
        ("class_id", ogr.OFTInteger),
        ("label", ogr.OFTString),          # 样本自定义名称，留空时界面显示"点/面 #FID"
        ("source", ogr.OFTString),
        ("native_crs", ogr.OFTString),
        ("created", ogr.OFTString),
        ("attrs", ogr.OFTString),
    ]
    CLASS_FIELDS = [
        ("code", ogr.OFTString),
        ("name", ogr.OFTString),
        ("region", ogr.OFTString),
        ("sample_type", ogr.OFTString),
        ("geom_kind", ogr.OFTString),
        ("color", ogr.OFTString),
        ("description", ogr.OFTString),
        ("parent_id", ogr.OFTInteger),
        ("source", ogr.OFTString),
        ("native_crs", ogr.OFTString),
        ("attrs", ogr.OFTString),          # 该类别的分类键值对(JSON)，按属性导入时写入
        ("origin", ogr.OFTString),         # 导入时的原始状态(JSON)，只写一次，用于恢复
    ]

    # 格式升级步骤：``(目标版本, 函数(store))``，按版本号升序执行，只对
    # ``format_version`` 低于目标版本的库跑一次，跑完把版本号写进 sl_meta。
    # 加字段这种向后兼容的变更不用登记在这里——:meth:`_upgrade_schema` 会照着
    # ``SAMPLE_FIELDS`` / ``CLASS_FIELDS`` 自动补齐；需要改数据（拆字段、换编码、
    # 迁移旧值）的变更才登记成一步。每一步都在事务里跑，半途失败整步回滚。
    MIGRATIONS: List[Tuple[str, "Callable[[SampleStore], None]"]] = [
        ("1.1", lambda store: store._migrate_groups_1_1()),
        ("1.2", lambda store: store._migrate_split_own_1_2()),
    ]

    def _migrate_groups_1_1(self) -> None:
        """1.1：类别树分成"分组"与"基础类别"两种节点。

        老库里靠拖动建出来的父类别 ``geom_kind`` 还是 point/polygon；1.1 起有子
        类别的节点必须是分组（``group``），基础类别不能再挂子类别。有子类别、
        自己又不直接持有样本的，改成分组即可；既有子类别又直接持有样本的节点
        这一步不动，留给 1.2。
        """
        classes = self.list_classes(with_counts=True)
        has_child = {c.parent_id for c in classes if c.parent_id is not None}
        for cls in classes:
            if cls.class_id in has_child and not cls.is_group and cls.count == 0:
                self.update_class(cls.class_id, geom_kind=SampleClass.GROUP)

    def _migrate_split_own_1_2(self) -> None:
        """1.2：既有子类别、自己又直接持有样本的节点，拆成分组 + 同名基础类别。

        老库里"沉积岩"自己有 3 个点，下面还挂着砂岩、泥岩。1.1 的规矩是分组不存
        样本、基础类别不挂子类别，这种节点两头都不是。处理成：在它下面新建一个
        **同名**的基础类别「沉积岩」，把它自己直接持有的样本全部挪进去，再把它
        改成分组——树上看就是"沉积岩 → 沉积岩 / 砂岩 / 泥岩"，样本一条不少，
        谁的还是谁的。自己那份点面都有时，拆成「沉积岩（点）」「沉积岩（面）」
        两个，与导入点面混合文件的规则一致。

        新类别的类别号沿用父节点的，因此在兄弟里排最前（兄弟的号通常比父大）；
        不重编整个工区——导入时给的类别号没有理由在一次升级里被改掉。工区、
        颜色、来源、属性都从父节点带过来，``origin`` 按导入态记，树上不带「•」。
        """
        classes = self.list_classes(with_counts=True)
        has_child = {c.parent_id for c in classes if c.parent_id is not None}
        for cls in classes:
            if cls.class_id not in has_child or cls.is_group or cls.count == 0:
                continue
            by_kind: Dict[str, List[SampleKey]] = {}
            for key, _cid in self.iter_keys([cls.class_id]):
                by_kind.setdefault(key[0], []).append(key)
            if not by_kind:
                continue
            for kind, keys in by_kind.items():
                name = cls.name + (KIND_SUFFIX[kind] if len(by_kind) > 1 else "")
                leaf = self.add_class(
                    name=name, code=cls.code, region=cls.region,
                    sample_type=cls.sample_type, geom_kind=kind, color=cls.color,
                    description=cls.description, parent_id=cls.class_id,
                    source=cls.source, native_crs=cls.native_crs,
                    attrs=cls.attrs or None)
                self.move_samples(keys, leaf)
            self.update_class(cls.class_id, geom_kind=SampleClass.GROUP)

    # ------------------------------------------------------------------ 构造
    def __init__(self, path: str, ds: gdal.Dataset, update: bool = True):
        self.path = path
        self._ds = ds
        self._update = update
        self._tx_depth = 0
        self._check_schema()
        self._check_version()
        self._upgrade_schema()
        self._ensure_indexes()
        self._migrate()

    @classmethod
    def create(cls, path: str, name: str = "", description: str = "",
               overwrite: bool = False) -> "SampleStore":
        """新建一个空样本库。"""
        path = os.path.abspath(path)
        if os.path.exists(path):
            if not overwrite:
                raise FileExistsError("样本库已存在：%s" % path)
            ogr.GetDriverByName("GPKG").DeleteDataSource(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        ds = gdal.GetDriverByName("GPKG").Create(path, 0, 0, 0, gdal.GDT_Unknown)
        srs = storage_srs()

        for lname, gtype in ((cls.LAYER_POINT, ogr.wkbPoint),
                             (cls.LAYER_POLYGON, ogr.wkbMultiPolygon)):
            lyr = ds.CreateLayer(lname, srs, gtype,
                                 options=["GEOMETRY_NAME=geom", "SPATIAL_INDEX=YES"])
            for fname, ftype in cls.SAMPLE_FIELDS:
                lyr.CreateField(ogr.FieldDefn(fname, ftype))

        lyr = ds.CreateLayer(cls.TABLE_CLASS, None, ogr.wkbNone)
        for fname, ftype in cls.CLASS_FIELDS:
            lyr.CreateField(ogr.FieldDefn(fname, ftype))

        lyr = ds.CreateLayer(cls.TABLE_META, None, ogr.wkbNone)
        lyr.CreateField(ogr.FieldDefn("key", ogr.OFTString))
        lyr.CreateField(ogr.FieldDefn("value", ogr.OFTString))

        store = cls(path, ds)
        store.set_meta("library_name", name or os.path.splitext(os.path.basename(path))[0])
        store.set_meta("description", description)
        store.set_meta("format_version", config.LIBRARY_FORMAT_VERSION)
        store.set_meta("storage_crs", "EPSG:%d" % config.STORAGE_EPSG)
        store.set_meta("created", date.today().isoformat())
        return store

    @classmethod
    def open(cls, path: str, update: bool = True) -> "SampleStore":
        """打开已有样本库。"""
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise FileNotFoundError("样本库不存在：%s" % path)
        return cls(path, _open(path, gdal.OF_VECTOR |
                               (gdal.OF_UPDATE if update else gdal.OF_READONLY),
                               "样本库"), update=update)

    def _check_schema(self) -> None:
        names = {self._ds.GetLayer(i).GetName() for i in range(self._ds.GetLayerCount())}
        missing = {self.LAYER_POINT, self.LAYER_POLYGON,
                   self.TABLE_CLASS, self.TABLE_META} - names
        if missing:
            raise ValueError(
                "不是有效的样本库文件（缺少 %s）：%s" % ("、".join(sorted(missing)), self.path))

    @staticmethod
    def _version_tuple(text: str) -> Tuple[int, ...]:
        parts = []
        for piece in (text or "").split("."):
            try:
                parts.append(int(piece))
            except ValueError:
                break
        return tuple(parts) or (0,)

    def file_version(self) -> str:
        """文件里记的格式版本；老库没写过时视为 1.0。"""
        return self.get_meta("format_version") or "1.0"

    def _check_version(self) -> None:
        """主版本号比本程序支持的还新，就不要碰它。

        次版本只加字段，老程序开新库顶多看不见新字段；主版本变了意味着表结构或
        字段含义变了，老程序按自己的理解去写，写出来的就是坏数据。
        """
        mine = self._version_tuple(config.LIBRARY_FORMAT_VERSION)
        theirs = self._version_tuple(self.file_version())
        if theirs[0] > mine[0]:
            raise ValueError(
                "样本库格式 %s 比本程序支持的 %s 更新，请升级程序后再打开：%s"
                % (self.file_version(), config.LIBRARY_FORMAT_VERSION, self.path))

    def _migrate(self) -> None:
        """按 :attr:`MIGRATIONS` 把老库升到当前格式版本。只读打开时跳过。"""
        if not self._update:
            return
        current = self._version_tuple(self.file_version())
        target = config.LIBRARY_FORMAT_VERSION
        for version, step in sorted(self.MIGRATIONS,
                                    key=lambda item: self._version_tuple(item[0])):
            if self._version_tuple(version) <= current:
                continue
            with self.transaction():
                step(self)
                self.set_meta("format_version", version)
            current = self._version_tuple(version)
        if current < self._version_tuple(target):
            self.set_meta("format_version", target)

    def _upgrade_schema(self) -> None:
        """为早期版本的样本库补齐后加的字段。

        新版本只做加字段这类向后兼容的变更，老库打开即自动升级，
        无需重建；只读打开时跳过升级，缺失字段按空值处理。
        """
        if not self._update:
            return
        targets = [(self.TABLE_CLASS, self.CLASS_FIELDS),
                   (self.LAYER_POINT, self.SAMPLE_FIELDS),
                   (self.LAYER_POLYGON, self.SAMPLE_FIELDS)]
        for lname, fields in targets:
            lyr = self._ds.GetLayerByName(lname)
            if lyr is None:
                continue
            defn = lyr.GetLayerDefn()
            existing = {defn.GetFieldDefn(i).GetName()
                        for i in range(defn.GetFieldCount())}
            for fname, ftype in fields:
                if fname not in existing:
                    lyr.CreateField(ogr.FieldDefn(fname, ftype))

    def close(self) -> None:
        if self._ds is not None:
            self._ds.FlushCache()
            self._ds = None

    def __enter__(self) -> "SampleStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ 内部
    def _layer(self, name: str) -> ogr.Layer:
        if self._ds is None:
            raise RuntimeError("样本库已关闭：%s" % self.path)
        lyr = self._ds.GetLayerByName(name)
        if lyr is None:
            raise ValueError("样本库缺少图层：%s" % name)
        return lyr

    def sample_layer(self, kind: str) -> ogr.Layer:
        return self._layer(self.LAYER_POINT if kind == GEOM_POINT else self.LAYER_POLYGON)

    def _sql(self, sql: str):
        """执行 SQL 并返回行列表（已释放结果集）。"""
        rows = []
        lyr = self._ds.ExecuteSQL(sql)
        if lyr is None:
            return rows
        try:
            defn = lyr.GetLayerDefn()
            cols = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
            for feat in lyr:
                rows.append({c: feat.GetField(c) for c in cols})
        finally:
            self._ds.ReleaseResultSet(lyr)
        return rows

    def _ensure_indexes(self) -> None:
        """给 ``class_id`` 建普通索引。

        样本表上只有空间索引，按类别取样本（导出、裁剪、统计都在做）走的却是
        ``class_id`` 上的等值/IN 查询，没有索引就是全表扫描。索引是幂等的，
        老库打开时补上即可；只读打开或建不上（老 GDAL、库被占用）都不影响使用，
        只是慢一点，所以这里吞掉异常。
        """
        if not self._update:
            return
        for lname in (self.LAYER_POINT, self.LAYER_POLYGON):
            try:
                self._sql('CREATE INDEX IF NOT EXISTS "idx_%s_class_id" '
                          'ON "%s"("class_id")' % (lname, lname))
            except Exception:
                return

    @contextmanager
    def transaction(self):
        """把一批写操作合成一个事务，出错整批回滚。

        必须可重入：``commit`` 这类高层动作要把整个过程包成一个事务，而它调用
        的 :meth:`delete_samples`、:meth:`move_samples` 各自也想开事务。GDAL 不
        支持嵌套事务，里层再 ``StartTransaction`` 会直接失败，于是只有最外层真
        正开启，里层只记一层深度。
        """
        outer = self._tx_depth == 0
        if outer:
            self._ds.StartTransaction()
        self._tx_depth += 1
        try:
            yield
        except Exception:
            self._tx_depth -= 1
            if outer:
                self._ds.RollbackTransaction()
            raise
        else:
            self._tx_depth -= 1
            if outer:
                self._ds.CommitTransaction()

    # ------------------------------------------------------------------ 元数据
    def meta(self) -> Dict[str, str]:
        lyr = self._layer(self.TABLE_META)
        lyr.ResetReading()
        return {f.GetField("key"): (f.GetField("value") or "") for f in lyr}

    def get_meta(self, key: str, default: str = "") -> str:
        return self.meta().get(key, default)

    def set_meta(self, key: str, value: str) -> None:
        lyr = self._layer(self.TABLE_META)
        lyr.SetAttributeFilter("key = '%s'" % key.replace("'", "''"))
        lyr.ResetReading()
        feat = lyr.GetNextFeature()
        if feat is None:
            feat = ogr.Feature(lyr.GetLayerDefn())
            feat.SetField("key", key)
            feat.SetField("value", value or "")
            lyr.CreateFeature(feat)
        else:
            feat.SetField("value", value or "")
            lyr.SetFeature(feat)
        lyr.SetAttributeFilter(None)

    @property
    def name(self) -> str:
        return self.get_meta("library_name") or os.path.splitext(os.path.basename(self.path))[0]

    # ------------------------------------------------------------------ 类别
    @staticmethod
    def _class_from_feature(feat: ogr.Feature) -> SampleClass:
        return SampleClass(
            class_id=feat.GetFID(),
            code=feat.GetField("code") or "",
            name=feat.GetField("name") or "",
            region=feat.GetField("region") or "",
            sample_type=feat.GetField("sample_type") or "",
            geom_kind=feat.GetField("geom_kind") or "",
            color=feat.GetField("color") or config.CLASS_PALETTE[0],
            description=feat.GetField("description") or "",
            parent_id=feat.GetField("parent_id"),
            source=feat.GetField("source") or "",
            native_crs=feat.GetField("native_crs") or "",
            attrs=_load_json(feat.GetField("attrs")),
            origin=_load_json(feat.GetField("origin")),
        )

    def list_classes(self, with_counts: bool = True) -> List[SampleClass]:
        lyr = self._layer(self.TABLE_CLASS)
        lyr.ResetReading()
        classes = [self._class_from_feature(feat) for feat in lyr]
        if with_counts:
            counts = self.count_by_class()
            for c in classes:
                c.count = counts.get(c.class_id, 0)
                c.total_count = c.count
            # 子类别的样本数向上累加到父类别，父类别本身可以不直接持有样本
            by_id = {c.class_id: c for c in classes}
            for c in classes:
                pid, guard = c.parent_id, set()
                while pid is not None and pid in by_id and pid not in guard:
                    guard.add(pid)
                    by_id[pid].total_count += c.count
                    pid = by_id[pid].parent_id
        classes.sort(key=lambda c: (c.region, _code_sort_key(c.code), c.name))
        return classes

    def get_class(self, class_id: int) -> Optional[SampleClass]:
        """按 id 取单个类别。

        走 FID 直取，不要为了一个类别去 :meth:`list_classes`——那会把整张类别表
        读一遍、再对全库做一次分类计数。导出时构造类别路径要沿着父链一路上查，
        层级深、类别多的库里这项开销是平方级的。返回的类别不带样本数
        （``count``/``total_count`` 为 0），要计数请用 :meth:`list_classes`。
        """
        feat = self._layer(self.TABLE_CLASS).GetFeature(int(class_id))
        return self._class_from_feature(feat) if feat is not None else None

    def find_class(self, region: str, code: str = "", name: str = "") -> Optional[int]:
        """同一工区里叫这个名字的已有类别；没有返回 ``None``。

        只认名字。``code`` 是树的排序键，界面上不显示，老库里它是文件名前缀那个
        数，跨工区各有自己的 1、2、3——拿它认"是不是同一类"会把 ``1_泥岩.shp``
        并进「1 沉积岩」。参数留着是为了调用方不用改，不参与判断。
        """
        del code
        if not name:
            return None
        lyr = self._layer(self.TABLE_CLASS)
        lyr.ResetReading()
        for feat in lyr:
            if ((feat.GetField("region") or "") == (region or "")
                    and (feat.GetField("name") or "") == name):
                return feat.GetFID()
        return None

    def unique_class_name(self, region: str, name: str,
                          parent_id: Optional[int] = None,
                          scope: str = "region") -> str:
        """给新类别起一个在工区里（或同一父级下）没被占用的名字。

        已有「泥岩」就叫「泥岩1」，「泥岩1」也有了就「泥岩2」，依此类推。导入与
        粘贴都靠它：同名的两批样本各占一个类别，谁也不并进谁。``scope`` 为
        ``"siblings"`` 时只看同一父级下的兄弟——按属性导入允许不同分支各有一个
        「砂岩」，那是分类字段本来的样子。
        """
        name = name or "未命名"
        lyr = self._layer(self.TABLE_CLASS)
        lyr.ResetReading()
        taken = set()
        for feat in lyr:
            if (feat.GetField("region") or "") != (region or ""):
                continue
            if scope == "siblings":
                pid = feat.GetField("parent_id")
                pid = int(pid) if pid not in (None, "") else None
                if pid != (int(parent_id) if parent_id is not None else None):
                    continue
            taken.add(feat.GetField("name") or "")
        if name not in taken:
            return name
        # 「砂岩1」再撞一次不叫「砂岩11」：名字尾巴上的数字若是我们早先加的序号
        # （去掉之后的「砂岩」就在工区里），就从「砂岩」往后接着数——「砂岩2」。
        # 「T3bb2」这种本来就带数字的代号，「T3bb」不存在，照旧按整个名字加。
        base = name.rstrip("0123456789")
        if base and base != name and base in taken:
            name = base
        n = 1
        while "%s%d" % (name, n) in taken:
            n += 1
        return "%s%d" % (name, n)

    def copy_samples(self, src_class_id: int, dst_class_id: int) -> List[SampleKey]:
        """把一个类别的全部样本复制一份到另一个类别，返回新样本的主键。

        逐要素克隆：几何、名称、来源、原始坐标系、属性都原样带过去，只换
        ``class_id`` 与创建日期。复制而不是改归属，原类别一条不少。
        """
        created = date.today().isoformat()
        out: List[SampleKey] = []
        with self.transaction():
            for kind in (GEOM_POINT, GEOM_POLYGON):
                lyr = self.sample_layer(kind)
                defn = lyr.GetLayerDefn()
                fresh = []
                with self._filtered(kind, "class_id = %d" % int(src_class_id)) as src:
                    for feat in src:
                        geom = feat.GetGeometryRef()
                        if geom is None:
                            continue
                        new = ogr.Feature(defn)
                        for i in range(defn.GetFieldCount()):
                            fname = defn.GetFieldDefn(i).GetName()
                            if fname in ("class_id", "created"):
                                continue
                            if feat.IsFieldSetAndNotNull(i):
                                new.SetField(fname, feat.GetField(i))
                        new.SetField("class_id", int(dst_class_id))
                        new.SetField("created", created)
                        new.SetGeometry(geom.Clone())
                        fresh.append(new)
                for new in fresh:                 # 过滤器撤掉之后再写，别边读边写
                    lyr.CreateFeature(new)
                    out.append((kind, new.GetFID()))
        return out

    def add_class(self, name: str, code: str = "", region: str = "",
                  sample_type: str = "", geom_kind: str = GEOM_POINT,
                  color: str = "", description: str = "",
                  parent_id: Optional[int] = None, source: str = "",
                  native_crs: str = "", attrs: Optional[dict] = None,
                  user_created: bool = False,
                  class_id: Optional[int] = None,
                  origin: Optional[dict] = None) -> int:
        """新建类别，返回它的 ``class_id``。

        ``class_id`` 与 ``origin`` 只给撤销用：合并、删除这类操作要把类别原样放
        回去，而样本表里的 ``class_id`` 外键指向的是原来那个号，重新分配一个新号
        就对不上了；``origin`` 是导入时记下的原始状态，放回去时也要原样带上，否则
        「恢复到导入时的状态」会把它当成一个后来新建的类别。日常新建一律不要传。
        """
        lyr = self._layer(self.TABLE_CLASS)
        if not color:
            color = config.class_color(lyr.GetFeatureCount())
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetField("code", code)
        feat.SetField("name", name)
        feat.SetField("region", region)
        feat.SetField("sample_type", sample_type)
        feat.SetField("geom_kind", geom_kind)
        feat.SetField("color", color)
        feat.SetField("description", description)
        if parent_id is not None:
            feat.SetField("parent_id", int(parent_id))
        feat.SetField("source", source)
        feat.SetField("native_crs", native_crs)
        if attrs:
            feat.SetField("attrs", _dump_json(attrs))
        # 记录导入时的原始状态：之后无论怎么改名、改层级，都能一键还原
        if origin is None:
            origin = {"name": name, "code": code, "region": region,
                      "parent_id": parent_id, "user_created": bool(user_created)}
        feat.SetField("origin", _dump_json(origin))
        if class_id is not None:
            feat.SetFID(int(class_id))
        lyr.CreateFeature(feat)
        return feat.GetFID()

    def restore_class_row(self, cls: SampleClass) -> int:
        """把一个类别按原来的 id 与全部字段放回类别表。

        撤销合并 / 删除类别时用：``SampleClass`` 是删除前拍下的快照，这里逐字段
        写回，包括 ``origin`` 与 ``attrs``——少一个字段，恢复出来的就不是原来那个
        类别。返回它的 ``class_id``。
        """
        return self.add_class(
            name=cls.name, code=cls.code, region=cls.region,
            sample_type=cls.sample_type, geom_kind=cls.geom_kind,
            color=cls.color, description=cls.description,
            parent_id=cls.parent_id, source=cls.source,
            native_crs=cls.native_crs, attrs=cls.attrs or None,
            class_id=cls.class_id, origin=cls.origin or None)

    def has_samples(self, class_id: int) -> bool:
        """该类别自身是否直接持有样本（不含子类别）。"""
        for kind in (GEOM_POINT, GEOM_POLYGON):
            lyr = self.sample_layer(kind)
            lyr.SetSpatialFilter(None)
            lyr.SetAttributeFilter("class_id = %d" % int(class_id))
            try:
                if lyr.GetFeatureCount() > 0:
                    return True
            finally:
                lyr.SetAttributeFilter(None)
        return False

    def update_class(self, class_id: int, **kwargs) -> None:
        lyr = self._layer(self.TABLE_CLASS)
        feat = lyr.GetFeature(class_id)
        if feat is None:
            raise KeyError("类别不存在：%s" % class_id)
        valid = {f for f, _ in self.CLASS_FIELDS}
        for k, v in kwargs.items():
            if k in valid:
                if k == "attrs" and isinstance(v, dict):
                    v = _dump_json(v)
                feat.SetField(k, v)
        lyr.SetFeature(feat)

    def set_class_codes(self, codes: Dict[int, str]) -> None:
        """批量改类别号。

        类别号同时也是列表的排序键（见 :meth:`list_classes`），重排类别顺序时
        要一口气改掉一整个工区，一条条走 :meth:`update_class` 会把同一个图层
        反复取要素、写要素，改到几百类就明显卡顿。
        """
        lyr = self._layer(self.TABLE_CLASS)
        for class_id, code in codes.items():
            feat = lyr.GetFeature(int(class_id))
            if feat is None:
                continue
            feat.SetField("code", str(code or ""))
            lyr.SetFeature(feat)

    def child_class_ids(self, class_id: Optional[int]) -> List[int]:
        """直接子类别的 id。``None`` 是顶层——某工区之外所有没有父类别的类别。"""
        lyr = self._layer(self.TABLE_CLASS)
        if class_id is None:
            lyr.SetAttributeFilter("parent_id IS NULL")
        else:
            lyr.SetAttributeFilter("parent_id = %d" % int(class_id))
        lyr.ResetReading()
        try:
            return [f.GetFID() for f in lyr]
        finally:
            lyr.SetAttributeFilter(None)

    def delete_class(self, class_id: int) -> int:
        """删除类别、其子类别及全部样本，返回删除的样本数。"""
        class_id = int(class_id)
        table = self._layer(self.TABLE_CLASS)
        if table.GetFeature(class_id) is None:
            # 已经不在了（多半是随父类别一起被删掉的），不是错误。GDAL 对不存在的
            # FID 调 DeleteFeature 会直接抛错，把整个事务掀翻。
            return 0
        removed = 0
        for child in self.child_class_ids(class_id):
            removed += self.delete_class(child)
        for kind in (GEOM_POINT, GEOM_POLYGON):
            lyr = self.sample_layer(kind)
            lyr.SetSpatialFilter(None)
            lyr.SetAttributeFilter("class_id = %d" % class_id)
            try:
                fids = [f.GetFID() for f in lyr]
            finally:
                lyr.SetAttributeFilter(None)
            for fid in fids:
                lyr.DeleteFeature(fid)
                removed += 1
        table.DeleteFeature(class_id)
        return removed

    def regions(self) -> List[str]:
        seen = []
        for c in self.list_classes(with_counts=False):
            if c.region not in seen:
                seen.append(c.region)
        return seen

    # ------------------------------------------------------------------ 样本
    def add_samples(self, class_id: int, geoms: Iterable[ogr.Geometry],
                    source: str = "", native_crs: str = "",
                    attrs: Optional[Sequence[dict]] = None,
                    transform: Optional[osr.CoordinateTransformation] = None) -> int:
        """批量写入样本，几何若带 transform 则先投影到存储坐标系。

        返回实际写入的样本数。MultiPoint 会被拆分为多个点样本。
        """
        created = date.today().isoformat()
        written = 0
        layers = {}
        with self.transaction():
            for idx, geom in enumerate(geoms):
                if geom is None or geom.IsEmpty():
                    continue
                g = geom.Clone()
                if transform is not None:
                    g.Transform(transform)
                kind = geom_kind_of(g)
                if kind is None:
                    continue
                attr = attrs[idx] if attrs is not None and idx < len(attrs) else None
                parts = _explode(g, kind)
                if kind not in layers:
                    layers[kind] = self.sample_layer(kind)
                lyr = layers[kind]
                defn = lyr.GetLayerDefn()
                for part in parts:
                    feat = ogr.Feature(defn)
                    feat.SetField("class_id", int(class_id))
                    feat.SetField("source", source)
                    feat.SetField("native_crs", native_crs)
                    feat.SetField("created", created)
                    if attr:
                        feat.SetField("attrs", _dump_json(attr))
                    feat.SetGeometry(part)
                    lyr.CreateFeature(feat)
                    written += 1
        return written

    def count_by_class(self) -> Dict[int, int]:
        counts: Dict[int, int] = {}
        for lname in (self.LAYER_POINT, self.LAYER_POLYGON):
            for row in self._sql(
                    "SELECT class_id, COUNT(*) AS n FROM %s GROUP BY class_id" % lname):
                cid = row.get("class_id")
                if cid is None:
                    continue
                counts[int(cid)] = counts.get(int(cid), 0) + int(row.get("n") or 0)
        return counts

    def total_samples(self) -> int:
        return (self._layer(self.LAYER_POINT).GetFeatureCount()
                + self._layer(self.LAYER_POLYGON).GetFeatureCount())

    @staticmethod
    def _class_filter(class_ids: Optional[Iterable[int]]) -> Optional[str]:
        """``class_id IN (...)`` 过滤串；``None`` 表示不过滤，空串表示一个都不要。"""
        if class_ids is None:
            return None
        ids = sorted({int(c) for c in class_ids})
        if not ids:
            return ""
        return "class_id IN (%s)" % ",".join(str(i) for i in ids)

    @contextmanager
    def _filtered(self, kind: str, cid_filter: Optional[str],
                  ignored: Sequence[str] = ()):
        """给样本图层临时套上属性过滤与忽略字段，退出时一律清掉。

        过滤器设在图层对象上，而图层是整个 store 共用的。这里的遍历大多是生成器，
        调用方随时可能中途 break（``any(...)``、找到第一条就停），不用 finally 的
        话 ``SetAttributeFilter(None)`` 永远走不到，之后的 ``GetFeatureCount``、
        ``GetExtent`` 全按残留条件算，错得悄无声息。生成器被丢弃时 Python 会在它
        身上抛 GeneratorExit，finally 照样执行。
        """
        lyr = self.sample_layer(kind)
        lyr.SetSpatialFilter(None)
        lyr.SetAttributeFilter(cid_filter or None)
        if ignored:
            lyr.SetIgnoredFields(list(ignored))
        lyr.ResetReading()
        try:
            yield lyr
        finally:
            lyr.SetAttributeFilter(None)
            if ignored:
                lyr.SetIgnoredFields([])

    def iter_records(self, class_ids: Optional[Iterable[int]] = None,
                     kinds: Optional[Iterable[str]] = None, *,
                     geometry: bool = False, coords: bool = True
                     ) -> Iterator[SampleRecord]:
        """遍历样本。

        ``coords`` 为真时把几何转成纯 Python 坐标（画布与树要的）；``geometry``
        为真时把 OGR 几何的副本挂在 ``rec.geom`` 上（导出与裁剪要的）。两个都不
        要的场合（只数数、只取归属）传 ``coords=False``，能省掉逐顶点的转换。

        字段按下标取而不是按名字：GDAL 的 ``GetField(名字)`` 每次都要在字段定义
        里线性找一遍，十万条样本乘四个字段就是几十万次查找，占了整个遍历一半
        以上的时间。
        """
        cid_filter = self._class_filter(class_ids)
        if cid_filter == "":
            return
        for kind in (kinds or (GEOM_POINT, GEOM_POLYGON)):
            with self._filtered(kind, cid_filter,
                                ignored=("native_crs", "created")) as lyr:
                defn = lyr.GetLayerDefn()
                i_cid = defn.GetFieldIndex("class_id")
                i_label = defn.GetFieldIndex("label")
                i_source = defn.GetFieldIndex("source")
                i_attrs = defn.GetFieldIndex("attrs")
                for feat in lyr:
                    geom = feat.GetGeometryRef()
                    if geom is None:
                        continue
                    if coords:
                        gk, pts = geom_to_coords(geom)
                    else:
                        gk, pts = geom_kind_of(geom) or kind, []
                    env = geom.GetEnvelope()   # (minx, maxx, miny, maxy)
                    yield SampleRecord(
                        key=(kind, feat.GetFID()),
                        class_id=int(feat.GetField(i_cid) or 0),
                        label=feat.GetField(i_label) or "",
                        geom_kind=gk,
                        coords=pts,
                        bbox=(env[0], env[2], env[1], env[3]),
                        source=feat.GetField(i_source) or "",
                        attrs=_load_json(feat.GetField(i_attrs)),
                        geom=geom.Clone() if geometry else None,
                    )

    def iter_keys(self, class_ids: Optional[Iterable[int]] = None,
                  kinds: Optional[Iterable[str]] = None
                  ) -> Iterator[Tuple[SampleKey, int]]:
        """只遍历 ``(主键, class_id)``，不读几何与其它字段。

        导出前的覆盖校验只是想知道"选中的这些键里有多少落在允许的类别下"，
        为此把几何整个读一遍再丢掉，在大库上要花好几秒。
        """
        cid_filter = self._class_filter(class_ids)
        if cid_filter == "":
            return
        ignored = ["OGR_GEOMETRY"] + [f for f, _ in self.SAMPLE_FIELDS if f != "class_id"]
        for kind in (kinds or (GEOM_POINT, GEOM_POLYGON)):
            with self._filtered(kind, cid_filter, ignored=ignored) as lyr:
                i_cid = lyr.GetLayerDefn().GetFieldIndex("class_id")
                for feat in lyr:
                    yield (kind, feat.GetFID()), int(feat.GetField(i_cid) or 0)

    def count_records(self, class_ids: Optional[Iterable[int]] = None) -> int:
        """这些类别下有多少条样本。走 SQLite 的计数，不读要素。"""
        cid_filter = self._class_filter(class_ids)
        if cid_filter == "":
            return 0
        total = 0
        for kind in (GEOM_POINT, GEOM_POLYGON):
            with self._filtered(kind, cid_filter) as lyr:
                total += lyr.GetFeatureCount()
        return total

    def load_records(self, class_ids: Optional[Iterable[int]] = None) -> List[SampleRecord]:
        return list(self.iter_records(class_ids))

    def get_geometry(self, key: SampleKey) -> Optional[ogr.Geometry]:
        kind, fid = key
        feat = self.sample_layer(kind).GetFeature(fid)
        if feat is None:
            return None
        geom = feat.GetGeometryRef()
        return geom.Clone() if geom is not None else None

    def extent(self, class_ids: Optional[Iterable[int]] = None
               ) -> Optional[Tuple[float, float, float, float]]:
        """返回 (minx, miny, maxx, maxy)，无样本时返回 None。

        注意：OGR 的 ``GetExtent()`` 不受属性过滤影响，因此按类别取范围时
        必须逐要素累加外接矩形，不能直接用图层范围。
        """
        cid_filter = None
        if class_ids is not None:
            ids = sorted({int(c) for c in class_ids})
            if not ids:
                return None
            cid_filter = "class_id IN (%s)" % ",".join(str(i) for i in ids)

        box = None
        for kind in (GEOM_POINT, GEOM_POLYGON):
            lyr = self.sample_layer(kind)
            lyr.SetSpatialFilter(None)
            lyr.SetAttributeFilter(cid_filter)
            if lyr.GetFeatureCount() == 0:
                lyr.SetAttributeFilter(None)
                continue
            if cid_filter is None:
                minx, maxx, miny, maxy = lyr.GetExtent(force=1)
                cur = (minx, miny, maxx, maxy)
            else:
                cur = None
                lyr.ResetReading()
                for feat in lyr:
                    geom = feat.GetGeometryRef()
                    if geom is None or geom.IsEmpty():
                        continue
                    e = geom.GetEnvelope()          # (minx, maxx, miny, maxy)
                    one = (e[0], e[2], e[1], e[3])
                    cur = one if cur is None else (min(cur[0], one[0]), min(cur[1], one[1]),
                                                   max(cur[2], one[2]), max(cur[3], one[3]))
            lyr.SetAttributeFilter(None)
            if cur is None:
                continue
            box = cur if box is None else (min(box[0], cur[0]), min(box[1], cur[1]),
                                           max(box[2], cur[2]), max(box[3], cur[3]))
        return box

    # ------------------------------------------------------------------ 编辑
    def class_depth(self, class_id: int, by_id: Optional[Dict[int, SampleClass]] = None) -> int:
        """类别所在层级，最上层为 1。"""
        by_id = by_id or {c.class_id: c for c in self.list_classes(with_counts=False)}
        depth, pid, guard = 1, by_id.get(class_id).parent_id if class_id in by_id else None, set()
        while pid is not None and pid in by_id and pid not in guard:
            guard.add(pid)
            depth += 1
            pid = by_id[pid].parent_id
        return depth

    def max_depth(self) -> int:
        """样本库中类别树的最大层数。"""
        classes = self.list_classes(with_counts=False)
        by_id = {c.class_id: c for c in classes}
        return max((self.class_depth(c.class_id, by_id) for c in classes), default=0)

    def move_class(self, class_id: int, new_parent_id: Optional[int]) -> None:
        """把类别挂到新的父类别下；``None`` 表示移到顶层。

        会拒绝把类别移到它自己的子孙下面，避免类别树成环。
        """
        if new_parent_id is not None:
            new_parent_id = int(new_parent_id)
            if new_parent_id == class_id:
                raise ValueError("不能把类别移动到它自己下面。")
            by_id = {c.class_id: c for c in self.list_classes(with_counts=False)}
            pid, guard = new_parent_id, set()
            while pid is not None and pid in by_id and pid not in guard:
                guard.add(pid)
                if pid == class_id:
                    raise ValueError("不能把类别移动到它自己的子类别下面。")
                pid = by_id[pid].parent_id

        lyr = self._layer(self.TABLE_CLASS)
        feat = lyr.GetFeature(class_id)
        if feat is None:
            raise KeyError("类别不存在：%s" % class_id)
        if new_parent_id is None:
            feat.SetFieldNull("parent_id")
        else:
            feat.SetField("parent_id", int(new_parent_id))
        lyr.SetFeature(feat)

    def set_sample_label(self, key: SampleKey, label: str) -> None:
        """给单条样本命名。"""
        kind, fid = key
        lyr = self.sample_layer(kind)
        feat = lyr.GetFeature(fid)
        if feat is None:
            raise KeyError("样本不存在：%s" % (key,))
        feat.SetField("label", label or "")
        lyr.SetFeature(feat)

    def insert_sample(self, class_id: int, geom: ogr.Geometry,
                      source: str = "", native_crs: str = "", label: str = "",
                      attrs: Optional[dict] = None,
                      transform: Optional[osr.CoordinateTransformation] = None
                      ) -> SampleKey:
        """写入单条样本并返回其主键。

        与 :meth:`add_samples` 的区别：这里只接受一个几何、只写一个要素，
        并把新要素的主键返回给调用方——标注功能需要立刻拿到主键来做选中、
        撤销与后续编辑。多部件的点几何在标注场景下不会出现，遇到时直接拒绝。
        """
        g = geom.Clone()
        if transform is not None:
            g.Transform(transform)
        g.FlattenTo2D()
        kind = geom_kind_of(g)
        if kind is None:
            raise ValueError("不支持的样本几何类型：%s"
                             % ogr.GeometryTypeToName(g.GetGeometryType()))
        parts = _explode(g, kind)
        if len(parts) != 1:
            raise ValueError("insert_sample 只接受单部件几何，收到 %d 部件" % len(parts))

        lyr = self.sample_layer(kind)
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetField("class_id", int(class_id))
        feat.SetField("label", label or "")
        feat.SetField("source", source)
        feat.SetField("native_crs", native_crs)
        feat.SetField("created", date.today().isoformat())
        if attrs:
            feat.SetField("attrs", _dump_json(attrs))
        feat.SetGeometry(parts[0])
        lyr.CreateFeature(feat)
        return (kind, feat.GetFID())

    def delete_samples(self, keys: Iterable[SampleKey]) -> int:
        """按主键删除样本，返回实际删除的条数。不存在的主键静默跳过。"""
        by_kind: Dict[str, List[int]] = {}
        for kind, fid in keys:
            by_kind.setdefault(kind, []).append(int(fid))
        removed = 0
        with self.transaction():
            for kind, fids in by_kind.items():
                lyr = self.sample_layer(kind)
                for fid in fids:
                    if lyr.GetFeature(fid) is None:
                        continue
                    lyr.DeleteFeature(fid)
                    removed += 1
        return removed

    def set_sample_geometry(self, key: SampleKey, geom: ogr.Geometry) -> None:
        """替换单条样本的几何。几何必须与原样本同属点 / 面。"""
        kind, fid = key
        g = geom.Clone()
        g.FlattenTo2D()
        if geom_kind_of(g) != kind:
            raise ValueError("几何类型与样本图层不符：样本是 %s" % kind)
        parts = _explode(g, kind)
        if len(parts) != 1:
            raise ValueError("样本几何必须是单部件，收到 %d 部件" % len(parts))
        lyr = self.sample_layer(kind)
        feat = lyr.GetFeature(fid)
        if feat is None:
            raise KeyError("样本不存在：%s" % (key,))
        feat.SetGeometry(parts[0])
        lyr.SetFeature(feat)

    def move_samples(self, keys: Iterable[SampleKey], class_id: int) -> int:
        """把样本改归到另一个类别，返回受影响的条数。"""
        by_kind: Dict[str, List[int]] = {}
        for kind, fid in keys:
            by_kind.setdefault(kind, []).append(int(fid))
        moved = 0
        with self.transaction():
            for kind, fids in by_kind.items():
                lyr = self.sample_layer(kind)
                for fid in fids:
                    feat = lyr.GetFeature(fid)
                    if feat is None:
                        continue
                    feat.SetField("class_id", int(class_id))
                    lyr.SetFeature(feat)
                    moved += 1
        return moved

    def set_sample_attrs(self, key: SampleKey, attrs: Optional[dict]) -> None:
        """覆盖单条样本的附加属性。"""
        kind, fid = key
        lyr = self.sample_layer(kind)
        feat = lyr.GetFeature(fid)
        if feat is None:
            raise KeyError("样本不存在：%s" % (key,))
        if attrs:
            feat.SetField("attrs", _dump_json(attrs))
        else:
            feat.SetFieldNull("attrs")
        lyr.SetFeature(feat)

    def read_record(self, key: SampleKey) -> Optional[SampleRecord]:
        """按主键读回一条样本记录，供界面在写入后立即刷新。"""
        kind, fid = key
        lyr = self.sample_layer(kind)
        lyr.SetSpatialFilter(None)
        lyr.SetAttributeFilter(None)
        feat = lyr.GetFeature(fid)
        if feat is None:
            return None
        geom = feat.GetGeometryRef()
        if geom is None:
            return None
        gk, coords = geom_to_coords(geom)
        env = geom.GetEnvelope()
        return SampleRecord(
            key=(kind, fid),
            class_id=int(feat.GetField("class_id") or 0),
            label=feat.GetField("label") or "",
            geom_kind=gk,
            coords=coords,
            bbox=(env[0], env[2], env[1], env[3]),
            source=feat.GetField("source") or "",
            attrs=_load_json(feat.GetField("attrs")),
        )

    def rename_region(self, old_region: str, new_region: str) -> int:
        """重命名工区，返回受影响的类别数。"""
        lyr = self._layer(self.TABLE_CLASS)
        lyr.SetAttributeFilter("region = '%s'" % (old_region or "").replace("'", "''"))
        lyr.ResetReading()
        feats = [f for f in lyr]
        lyr.SetAttributeFilter(None)
        for feat in feats:
            feat.SetField("region", new_region)
            lyr.SetFeature(feat)
        return len(feats)

    def restore_class(self, class_id: int) -> bool:
        """把单个类别的名称、类别号、工区、层级恢复到导入时的状态。"""
        lyr = self._layer(self.TABLE_CLASS)
        feat = lyr.GetFeature(class_id)
        if feat is None:
            return False
        origin = _load_json(feat.GetField("origin"))
        if not origin or origin.get("user_created"):
            return False
        feat.SetField("name", origin.get("name") or "")
        feat.SetField("code", origin.get("code") or "")
        feat.SetField("region", origin.get("region") or "")
        parent = origin.get("parent_id")
        if parent is None:
            feat.SetFieldNull("parent_id")
        else:
            feat.SetField("parent_id", int(parent))
        lyr.SetFeature(feat)
        return True

    def restore_all(self) -> Dict[str, int]:
        """整库恢复到导入时的分类状态。

        步骤：先把所有导入产生的类别还原（含层级），再删除使用者后来新建的
        类别，最后清空样本上的自定义名称。样本几何与归属类别自始至终没有被
        编辑过，因此不受影响。
        """
        stats = {"restored": 0, "removed": 0, "labels": 0}
        classes = self.list_classes(with_counts=False)
        for cls in classes:
            if self.restore_class(cls.class_id):
                stats["restored"] += 1
        # 还原层级后，使用者新建的类别理应已经空了；万一还挂着东西，
        # 宁可留下这个分组也不删——恢复操作绝不能连带删除样本。
        stats["kept"] = 0
        for cls in classes:
            if not cls.origin.get("user_created"):
                continue
            if self.child_class_ids(cls.class_id):
                stats["kept"] += 1
                continue
            if any(True for _ in self.iter_records([cls.class_id])):
                stats["kept"] += 1
                continue
            self.delete_class(cls.class_id)
            stats["removed"] += 1
        for kind in (GEOM_POINT, GEOM_POLYGON):
            lyr = self.sample_layer(kind)
            lyr.SetAttributeFilter("label IS NOT NULL AND label <> ''")
            lyr.ResetReading()
            feats = [f for f in lyr]
            lyr.SetAttributeFilter(None)
            for feat in feats:
                feat.SetField("label", "")
                lyr.SetFeature(feat)
                stats["labels"] += 1
        return stats

    def backup(self, tag: str = "") -> str:
        """把当前样本库整体复制一份快照，返回快照文件路径。

        快照落在样本库同级的 ``backups/`` 下，文件名带时间戳；同一秒内再备一次
        不会把上一份盖掉（早先会——时间戳只到秒）。复制完立刻只读打开一遍核对
        类别与样本数：拷坏了的备份比没有备份更糟，那时人以为自己有退路。
        """
        folder = os.path.join(os.path.dirname(self.path), "backups")
        stem = os.path.splitext(os.path.basename(self.path))[0]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = "%s_%s%s" % (stem, stamp, ("_" + tag) if tag else "")
        target = os.path.join(folder, base + config.LIBRARY_EXT)
        n = 2
        while os.path.exists(target):
            target = os.path.join(folder, "%s_%d%s" % (base, n, config.LIBRARY_EXT))
            n += 1
        return self.copy_to(target)

    def copy_to(self, target: str) -> str:
        """把整个样本库原样复制到 ``target``（另存为、备份到指定位置都走这里）。

        先写到旁边的临时文件、只读打开核对类别与样本数，对上了才改名成目标文件：
        拷到一半出错，目标位置上原有的文件不会被半截的副本顶掉；拷坏了的副本也
        不会留下来冒充备份——那时人以为自己有退路。
        """
        import shutil
        target = os.path.abspath(target)
        if os.path.normcase(target) == os.path.normcase(os.path.abspath(self.path)):
            raise IOError("目标就是当前这个样本库文件本身，换一个位置或文件名。")
        self._ds.FlushCache()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # 临时文件也用 .gpkg 结尾：GDAL 认扩展名，叫 .partial 会每次打一行告警
        partial = os.path.splitext(target)[0] + ".partial" + config.LIBRARY_EXT
        shutil.copy2(self.path, partial)
        want = (len(self.list_classes(with_counts=False)), self.total_samples())
        try:
            copy = SampleStore.open(partial, update=False)
            try:
                got = (len(copy.list_classes(with_counts=False)), copy.total_samples())
            finally:
                copy.close()
        except Exception:
            os.remove(partial)
            raise
        if got != want:
            os.remove(partial)
            raise IOError("复制核对不一致：库里 %d 类 %d 条，副本里 %d 类 %d 条，"
                          "副本已删除，请重试。" % (want + got))
        os.replace(partial, target)
        return target

    def list_backups(self) -> List[str]:
        """按时间倒序列出该样本库的快照。"""
        folder = os.path.join(os.path.dirname(self.path), "backups")
        if not os.path.isdir(folder):
            return []
        stem = os.path.splitext(os.path.basename(self.path))[0] + "_"
        files = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.startswith(stem) and f.endswith(config.LIBRARY_EXT)]
        return sorted(files, key=os.path.getmtime, reverse=True)

    # ------------------------------------------------------------------ 空间筛选
    def select_by_geometry(self, roi: ogr.Geometry, mode: str = "intersect",
                           class_ids: Optional[Iterable[int]] = None,
                           progress=None) -> Set[SampleKey]:
        """按感兴趣区几何（EPSG:4326）筛选样本，返回样本主键集合。

        ``progress(已处理, 总数, 说明)`` 返回 ``False`` 时中止并返回已经命中的
        部分——大库上逐条做几何关系判断相当慢，得让使用者能停下来。
        """
        cid_filter = None
        if class_ids is not None:
            ids = sorted({int(c) for c in class_ids})
            if not ids:
                return set()
            cid_filter = "class_id IN (%s)" % ",".join(str(i) for i in ids)

        hits: Set[SampleKey] = set()
        done = 0
        for kind in (GEOM_POINT, GEOM_POLYGON):
            lyr = self.sample_layer(kind)
            lyr.SetAttributeFilter(cid_filter)
            lyr.SetSpatialFilter(roi)      # 空间索引粗筛，结果可能是超集
            total = lyr.GetFeatureCount()
            lyr.ResetReading()
            for feat in lyr:
                done += 1
                if progress is not None and done % 200 == 0 and progress(
                        done, total, "已命中 %d 条" % len(hits)) is False:
                    lyr.SetSpatialFilter(None)
                    lyr.SetAttributeFilter(None)
                    return hits
                geom = feat.GetGeometryRef()
                if geom is None:
                    continue
                if mode == "within":
                    ok = geom.Within(roi)
                elif mode == "centroid":
                    centroid = geom.Centroid()
                    ok = centroid is not None and centroid.Within(roi)
                else:
                    ok = geom.Intersects(roi)
                if ok:
                    hits.add((kind, feat.GetFID()))
            lyr.SetSpatialFilter(None)
            lyr.SetAttributeFilter(None)
        return hits

    # ------------------------------------------------------------------ 统计
    def stats(self) -> Dict[str, object]:
        classes = self.list_classes()
        return {
            "name": self.name,
            "path": self.path,
            "classes": len(classes),
            "regions": len(self.regions()),
            "points": self._layer(self.LAYER_POINT).GetFeatureCount(),
            "polygons": self._layer(self.LAYER_POLYGON).GetFeatureCount(),
            "samples": self.total_samples(),
            "extent": self.extent(),
        }


# =============================================================================
# 导入：一个类别只装一种几何
# =============================================================================

KIND_SUFFIX = {GEOM_POINT: "（点）", GEOM_POLYGON: "（面）"}


def add_samples_by_kind(store: SampleStore, geoms: Sequence[ogr.Geometry],
                        attrs: Optional[Sequence[dict]], transform,
                        class_kwargs: dict, source: str, native_crs: str,
                        existing_id: Optional[int] = None,
                        report=None) -> Tuple[List[int], int]:
    """把一批几何写成类别，保证每个基础类别只装一种几何。

    点面混着的一批（一个 shp 里既有点又有面、按属性分组后同一取值下点面都有）
    不再建一个 ``mixed`` 类别，而是建一个分组、下面挂「名（点）」「名（面）」两
    个基础类别。只有一种几何时照旧建一个类别。

    ``existing_id`` 给定时（重复导入同一文件、按属性导入时同一组键值对已有类别）
    样本追加到它那儿，但**只往对得上的地方追加**：

    * 它是分组：找它下面**同名**（或「名（点）」「名（面）」）且几何种类相同的基础
      类别；没有就在这个分组下新建一个。早先是"分组下随便哪个同种几何的子类"，
      往「沉积岩」分组里导「泥岩」会并进「砂岩」。
    * 它是基础类别、几何种类相同（或它还是空的，种类由第一条样本定）：追加。
    * 它是基础类别、几何种类不同：不能往点类别里塞面。在它旁边另建「名（面）」，
      并在报告里说明。

    返回 ``(用到的基础类别 id 列表, 写入样本数)``；``report`` 给了就累计
    ``classes`` 与警告。
    """
    by_kind: Dict[str, List[int]] = {}
    for i, g in enumerate(geoms):
        kind = geom_kind_of(g) if g is not None and not g.IsEmpty() else None
        if kind is not None:
            by_kind.setdefault(kind, []).append(i)
    if not by_kind:
        return [], 0
    name = class_kwargs.get("name", "")
    base = dict(class_kwargs, source=source, native_crs=native_crs)

    def write(cid: int, idx: List[int]) -> int:
        return store.add_samples(cid, [geoms[i] for i in idx], source=source,
                                 native_crs=native_crs,
                                 attrs=[attrs[i] for i in idx] if attrs is not None else None,
                                 transform=transform)

    def created(cid: int) -> int:
        if report is not None:
            report.classes += 1
        return cid

    def split_note() -> None:
        if report is not None and len(by_kind) > 1:
            report.warnings.append(
                "「%s」点面都有（点 %d、面 %d），已拆成两个基础类别：%s、%s"
                % (name, len(by_kind.get(GEOM_POINT, ())),
                   len(by_kind.get(GEOM_POLYGON, ())),
                   name + KIND_SUFFIX[GEOM_POINT], name + KIND_SUFFIX[GEOM_POLYGON]))

    existing = store.get_class(existing_id) if existing_id is not None else None

    # ---- 没有现成的：一种几何建一个类别；点面都有建分组 + 两个基础类别
    if existing is None:
        if len(by_kind) == 1:
            kind, idx = next(iter(by_kind.items()))
            cid = created(store.add_class(geom_kind=kind, **base))
            return [cid], write(cid, idx)
        group_id = created(store.add_class(geom_kind=SampleClass.GROUP, **base))
        used: List[int] = []
        written = 0
        region = base.get("region", "")
        for kind in (GEOM_POINT, GEOM_POLYGON):
            idx = by_kind.get(kind)
            if not idx:
                continue
            # 分组名已经查过重，「名（点）」也得查：工区里可能单独躺着一个同名的
            leaf = dict(base, code="", parent_id=group_id, geom_kind=kind,
                        name=store.unique_class_name(region, name + KIND_SUFFIX[kind]))
            cid = created(store.add_class(**leaf))
            used.append(cid)
            written += write(cid, idx)
        split_note()
        return used, written

    # ---- 有现成的：每种几何各找自己该去的基础类别
    used = []
    written = 0
    for kind in (GEOM_POINT, GEOM_POLYGON):
        idx = by_kind.get(kind)
        if not idx:
            continue
        cid = _slot_for(store, existing, name, kind, base, report)
        used.append(cid)
        written += write(cid, idx)
    split_note()
    return used, written


def _slot_for(store: SampleStore, existing: SampleClass, name: str, kind: str,
              base: dict, report) -> int:
    """这种几何的样本该进 ``existing`` 名下的哪个基础类别；没有合适的就建一个。"""
    def created(cid: int) -> int:
        if report is not None:
            report.classes += 1
        return cid

    if existing.is_group:
        cid = _leaf_under(store, existing.class_id, name, kind)
        if cid is not None:
            return cid
        # 分组下已有同名的别种几何时，新的叫「名（面）」，否则就叫本名
        siblings = [store.get_class(c) for c in store.child_class_ids(existing.class_id)]
        taken = any(c is not None and c.name == name for c in siblings)
        leaf = dict(base, name=name + KIND_SUFFIX[kind] if taken else name,
                    parent_id=existing.class_id, geom_kind=kind)
        return created(store.add_class(**leaf))

    # 基础类别：种类相同、或它还是空的（种类由第一条样本定），直接追加
    if not existing.geom_kind or existing.geom_kind == kind:
        return existing.class_id

    # 种类不同：不能往点类别里塞面。旁边已有「名（面）」就用它，否则另建
    for c in store.child_class_ids(existing.parent_id):
        sib = store.get_class(c)
        if (sib is not None and sib.is_leaf and sib.geom_kind == kind
                and sib.region == existing.region
                and sib.name == name + KIND_SUFFIX[kind]):
            return sib.class_id
    leaf = dict(base, name=name + KIND_SUFFIX[kind], parent_id=existing.parent_id,
                geom_kind=kind)
    cid = created(store.add_class(**leaf))
    if report is not None:
        report.warnings.append(
            "「%s」已是%s类别，这次导入的是%s：另建了「%s」放在它旁边"
            % (name, KIND_SUFFIX[existing.geom_kind].strip("（）"),
               KIND_SUFFIX[kind].strip("（）"), name + KIND_SUFFIX[kind]))
    return cid


def _leaf_under(store: SampleStore, group_id: int, name: str, kind: str) -> Optional[int]:
    """分组下装这种几何、且**就是这个名字**的基础类别：叫「名」或「名（点）」都算。

    只按几何种类找是不够的——分组下往往有好几个同种几何的子类别，随便挑一个
    等于把样本并进别人家。
    """
    for cid in store.child_class_ids(group_id):
        cls = store.get_class(cid)
        if (cls is not None and cls.is_leaf
                and cls.name in (name, name + KIND_SUFFIX[kind])
                and (not cls.geom_kind or cls.geom_kind == kind)):
            return cid
    return None


# =============================================================================
# 辅助函数
# =============================================================================

def _json_default(value):
    """JSON 装不下的属性值怎么写：字节按十六进制，日期按 ISO，其余转字符串。

    Shapefile / GPKG 的属性表里会出现 OFTBinary、OFTDateTime 这类字段，原样交给
    ``json.dumps`` 是 TypeError，整个文件的导入就跟着回滚——为了一个没人看的
    二进制字段丢掉整批样本不值。
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _dump_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _load_json(raw) -> dict:
    """安全解析 JSON 字段，失败时返回空字典。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _explode(geom: ogr.Geometry, kind: str) -> List[ogr.Geometry]:
    """把几何规整成图层可接受的形态。

    * 点图层：MultiPoint 拆成多个 Point
    * 面图层：Polygon 统一提升为 MultiPolygon
    """
    t = ogr.GT_Flatten(geom.GetGeometryType())
    if kind == GEOM_POINT:
        if t == ogr.wkbMultiPoint:
            return [geom.GetGeometryRef(i).Clone() for i in range(geom.GetGeometryCount())]
        return [geom]
    if t == ogr.wkbPolygon:
        return [ogr.ForceToMultiPolygon(geom)]
    return [geom]


def _code_sort_key(code: str):
    """类别号排序：数字按数值排，非数字排在其后。"""
    try:
        return (0, int(code), "")
    except (TypeError, ValueError):
        return (1, 0, code or "")
