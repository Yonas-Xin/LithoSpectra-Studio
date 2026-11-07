# -*- coding: utf-8 -*-
"""标注编辑的暂存模型。

标注过程中的所有增删改都先记在 :class:`EditBuffer` 里，画布直接读它的合并
视图作数据源；只有在使用者点「保存到样本库」时才一次性写进 GeoPackage。
这样做有三个理由：

* 拖顶点这种高频操作不会反复写盘；
* 撤销栈干净——一次操作对应一个可逆条目，而不是一串零碎的写库动作；
* 与「标注完把整个工作区加入样本库」的使用方式一致，中途随时可以整体放弃。

暂存中的对象用**负数 id** 表示：草稿要素的 FID 为负，草稿类别的 class_id
为负。负数不可能与 GeoPackage 分配的 FID 冲突，因此画布、选择集、撤销栈
都可以对草稿与库内对象一视同仁。
"""

import copy
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from osgeo import ogr

from .. import config
from ..model import (GEOM_POINT, GEOM_POLYGON, SampleClass, SampleKey,
                     SampleRecord)
from ..store import SampleStore

ogr.UseExceptions()


# =============================================================================
# 几何工具
# =============================================================================

def _ring_bbox(ring) -> Tuple[float, float, float, float]:
    xs = [float(p[0]) for p in ring]
    ys = [float(p[1]) for p in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def _point_in_ring(x: float, y: float, ring) -> bool:
    """射线法判点在环内。用纯 Python 而不是 OGR：环的装配要两两互测，
    为此建几百个临时 OGR 几何比直接算慢得多。"""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def _nest_rings(rings) -> List[List[list]]:
    """按包含关系把一串环装配回若干个多边形。

    :func:`samplelib.store.geom_to_coords` 为了让界面能按下标改顶点，把
    MultiPolygon 的所有环拍平成一个列表，外环与内环混在一起、也看不出哪个环
    属于哪一部件。装回去时不能想当然地"第一个是外环、其余都是孔"——两个互不
    相干的图斑会被写成"一个图斑挖掉另一个"，面积归零、几何自交，
    ``IsValid()`` 直接变假。

    这里按几何包含关系还原层次：一个环被奇数层环套住就是孔，套住它的最内层
    环是它的外环；被偶数层套住（含 0 层）就是独立部件的外环——孔里面还能再挖
    出一个岛，这条规则同样成立。这也正是 Shapefile 一贯的环解释方式。
    """
    n = len(rings)
    boxes = [_ring_bbox(r) for r in rings]
    parent: List[Optional[int]] = [None] * n
    for i in range(n):
        px, py = float(rings[i][0][0]), float(rings[i][0][1])
        best, best_area = None, None
        for j in range(n):
            if i == j:
                continue
            bi, bj = boxes[i], boxes[j]
            if not (bj[0] <= bi[0] and bj[1] <= bi[1]
                    and bj[2] >= bi[2] and bj[3] >= bi[3]):
                continue
            if not _point_in_ring(px, py, rings[j]):
                continue
            area = (bj[2] - bj[0]) * (bj[3] - bj[1])
            if best_area is None or area < best_area:
                best, best_area = j, area
        parent[i] = best

    depth = [-1] * n

    def depth_of(i: int) -> int:
        chain = []
        cur = i
        while cur is not None and depth[cur] < 0:
            if cur in chain:                      # 环状包含不可能，保险
                break
            chain.append(cur)
            cur = parent[cur]
        base = 0 if cur is None else depth[cur] + 1
        for k in reversed(chain):
            depth[k] = base
            base += 1
        return depth[i]

    parts: List[List[list]] = []
    slot: Dict[int, int] = {}
    for i in range(n):
        if depth_of(i) % 2 == 0:
            slot[i] = len(parts)
            parts.append([rings[i]])
    for i in range(n):
        if depth[i] % 2 == 1 and parent[i] is not None and parent[i] in slot:
            parts[slot[parent[i]]].append(rings[i])
    return parts


def _linear_ring(ring) -> ogr.Geometry:
    lr = ogr.Geometry(ogr.wkbLinearRing)
    for lon, lat in ring:
        lr.AddPoint_2D(float(lon), float(lat))
    if tuple(ring[0]) != tuple(ring[-1]):
        lr.AddPoint_2D(float(ring[0][0]), float(ring[0][1]))
    return lr


def coords_to_geom(geom_kind: str, coords) -> Optional[ogr.Geometry]:
    """纯 Python 坐标 -> OGR 几何（EPSG:4326）。

    与 :func:`samplelib.store.geom_to_coords` 互为逆运算。面几何按环的包含关系
    还原成多部件（见 :func:`_nest_rings`），统一提升为 MultiPolygon 以匹配样本
    图层。
    """
    if geom_kind == GEOM_POINT:
        if not coords:
            return None
        lon, lat = coords[0]
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(float(lon), float(lat))
        return pt

    rings = [r for r in (coords or []) if r is not None and len(r) >= 3]
    if not rings:
        return None
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    for part in _nest_rings(rings):
        poly = ogr.Geometry(ogr.wkbPolygon)
        for ring in part:
            poly.AddGeometry(_linear_ring(ring))
        multi.AddGeometry(poly)
    return multi


def bbox_of(geom_kind: str, coords) -> Tuple[float, float, float, float]:
    """算外接矩形，避免为了取范围去构造 OGR 几何。"""
    if geom_kind == GEOM_POINT:
        if not coords:
            return (0.0, 0.0, 0.0, 0.0)
        lon, lat = coords[0]
        return (lon, lat, lon, lat)
    xs: List[float] = []
    ys: List[float] = []
    for ring in coords or []:
        for lon, lat in ring:
            xs.append(lon)
            ys.append(lat)
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


# =============================================================================
# 草稿类别
# =============================================================================

@dataclass
class DraftClass:
    """尚未写入样本库的新类别。"""

    class_id: int                      # 负数临时 id
    name: str = ""
    code: str = ""
    region: str = ""
    sample_type: str = ""
    geom_kind: str = ""                # 空 = 还没定，第一条样本落进去时定
    color: str = config.DEFAULT_CLASS_COLOR
    description: str = ""
    parent_id: Optional[int] = None    # 可以是负数（草稿父类）或正数（库内类别）

    def to_sample_class(self) -> SampleClass:
        """转成界面通用的类别对象，便于与库内类别混在同一棵树里。"""
        return SampleClass(
            class_id=self.class_id, code=self.code, name=self.name,
            region=self.region, sample_type=self.sample_type,
            geom_kind=self.geom_kind, color=self.color,
            description=self.description, parent_id=self.parent_id,
            source="标注", origin={"user_created": True},
        )


# =============================================================================
# 撤销条目
# =============================================================================

@dataclass
class _Op:
    label: str
    undo: Callable[[], None]
    redo: Callable[[], None]


# =============================================================================
# 暂存缓冲
# =============================================================================

class EditBuffer:
    """标注编辑的暂存区。

    构造时接管一份库内数据的快照（类别与样本记录），之后所有查询都走
    :meth:`records` / :meth:`classes`，调用方无需关心某个对象是库里的还是
    刚画出来的。
    """

    def __init__(self, store: SampleStore) -> None:
        self.store = store
        # 数据版本号：合并视图每变一次加一。画布的离屏缓存拿它当指纹——同一个
        # 版本号就是同一份数据，贴图即可；变了才重画。
        self.version = 0
        self.reload()

    # ------------------------------------------------------------------ 载入
    def reload(self) -> None:
        """从样本库重新读取快照，并清空所有暂存改动与撤销栈。"""
        self._base_classes: List[SampleClass] = self.store.list_classes()
        self._base_records: List[SampleRecord] = self.store.load_records()
        self._base_by_key: Dict[SampleKey, SampleRecord] = {
            r.key: r for r in self._base_records}

        self._draft_classes: Dict[int, DraftClass] = {}
        self._draft_records: Dict[SampleKey, SampleRecord] = {}
        self._deleted: Set[SampleKey] = set()
        self._deleted_classes: Set[int] = set()             # 待删除的库内类别
        self._geom_edits: Dict[SampleKey, object] = {}      # key -> coords
        self._class_edits: Dict[SampleKey, int] = {}        # key -> new class_id
        self._label_edits: Dict[SampleKey, str] = {}
        self._class_renames: Dict[int, str] = {}            # 库内类别改名
        self._class_kinds: Dict[int, str] = {}              # 库内类别定下几何种类

        self._cache: Optional[List[SampleRecord]] = None
        self._cache_index: Dict[SampleKey, SampleRecord] = {}
        self._next_fid = -1
        self._next_class_id = -1
        self._undo: List[_Op] = []
        self._redo: List[_Op] = []
        self._touch()

    def rebase(self) -> Dict[str, int]:
        """库变了，重读底稿，草稿尽量保留。

        浏览模式下改类别名、合并、删类别、撤销，动的都是库本身，而缓冲区手里
        那份 ``_base_*`` 是打开时拍的快照。不同步的话切回标注模式看到的还是旧
        树，保存时 ``update_class`` 撞上一个已经删掉的类别直接抛错。

        草稿要素与改动只要引用的对象还在就原样保留；引用已经没了的对象（删掉的
        样本、删掉的类别）的改动丢弃并计数返回。丢过东西之后撤销栈清空——栈里
        的条目会把这些改动再放回来，指向一个不存在的对象。
        """
        self._base_classes = self.store.list_classes()
        self._base_records = self.store.load_records()
        self._base_by_key = {r.key: r for r in self._base_records}
        keys = set(self._base_by_key)
        real_cids = {c.class_id for c in self._base_classes}
        cids = real_cids | set(self._draft_classes)
        dropped = {"要素改动": 0, "草稿要素": 0, "类别改动": 0}

        for table in (self._geom_edits, self._class_edits, self._label_edits):
            for key in [k for k in table if k not in keys]:
                table.pop(key)
                dropped["要素改动"] += 1
        for key in [k for k, cid in self._class_edits.items() if cid not in cids]:
            self._class_edits.pop(key)
            dropped["要素改动"] += 1
        gone = self._deleted - keys
        self._deleted -= gone
        dropped["要素改动"] += len(gone)

        for key in [k for k, r in self._draft_records.items()
                    if r.class_id not in cids]:
            self._draft_records.pop(key)
            dropped["草稿要素"] += 1
        for cid in [c for c, d in self._draft_classes.items()
                    if d.parent_id is not None and d.parent_id not in cids]:
            self._draft_classes[cid].parent_id = None     # 父没了就升到顶层
            dropped["类别改动"] += 1
        gone_cls = self._deleted_classes - real_cids
        self._deleted_classes -= gone_cls
        dropped["类别改动"] += len(gone_cls)
        for cid in [c for c in self._class_renames if c not in real_cids]:
            self._class_renames.pop(cid)
            dropped["类别改动"] += 1
        for cid in [c for c in self._class_kinds if c not in real_cids]:
            self._class_kinds.pop(cid)
            dropped["类别改动"] += 1

        if any(dropped.values()):
            self._undo.clear()
            self._redo.clear()
        self._touch()
        return dropped

    def _touch(self) -> None:
        """合并视图变了：作废缓存、版本号加一。"""
        self._cache_dirty = True
        self.version += 1

    # ------------------------------------------------------------------ 查询
    @property
    def has_deletions(self) -> bool:
        """本次暂存里是否包含删除。删除是唯一落库后撤不回来的改动。"""
        return bool(self._deleted or self._deleted_classes)

    @property
    def dirty(self) -> bool:
        return bool(self._draft_classes or self._draft_records or self._deleted
                    or self._deleted_classes or self._geom_edits
                    or self._class_edits or self._label_edits
                    or self._class_renames or self._class_kinds)

    def classes(self) -> List[SampleClass]:
        """库内类别 + 草稿类别，改名已套用，待删除的已剔除。"""
        out: List[SampleClass] = []
        for cls in self._base_classes:
            if cls.class_id in self._deleted_classes:
                continue
            if cls.class_id in self._class_renames or cls.class_id in self._class_kinds:
                cls = copy.copy(cls)
                cls.name = self._class_renames.get(cls.class_id, cls.name)
                cls.geom_kind = self._class_kinds.get(cls.class_id, cls.geom_kind)
            out.append(cls)
        out.extend(d.to_sample_class() for d in self._draft_classes.values())
        return out

    def class_map(self) -> Dict[int, SampleClass]:
        return {c.class_id: c for c in self.classes()}

    def subtree_class_ids(self, class_id: int) -> List[int]:
        """类别自身及其全部子孙类别 id（草稿与库内混排的那棵树）。"""
        by_parent: Dict[Optional[int], List[int]] = {}
        for c in self.classes():
            by_parent.setdefault(c.parent_id, []).append(c.class_id)
        out, stack, guard = [], [class_id], set()
        while stack:
            cid = stack.pop()
            if cid in guard:
                continue
            guard.add(cid)
            out.append(cid)
            stack.extend(by_parent.get(cid, []))
        return out

    def region_class_ids(self, region: str) -> List[int]:
        """一个工作区下的全部类别 id。"""
        return [c.class_id for c in self.classes() if c.region == region]

    def records(self) -> List[SampleRecord]:
        """合并视图：删除已剔除、几何/归类/名称改动已套用、草稿已并入。"""
        if not self._cache_dirty and self._cache is not None:
            return self._cache
        out: List[SampleRecord] = []
        dropped = self._deleted_classes
        for rec in self._base_records:
            if rec.key in self._deleted:
                continue
            if dropped and self._class_edits.get(rec.key, rec.class_id) in dropped:
                continue                    # 所属类别待删除，视同已经不在了
            coords = self._geom_edits.get(rec.key)
            cid = self._class_edits.get(rec.key)
            label = self._label_edits.get(rec.key)
            if coords is None and cid is None and label is None:
                out.append(rec)
                continue
            rec = copy.copy(rec)
            if coords is not None:
                rec.coords = coords
                rec.bbox = bbox_of(rec.geom_kind, coords)
            if cid is not None:
                rec.class_id = cid
            if label is not None:
                rec.label = label
            out.append(rec)
        out.extend(r for r in self._draft_records.values()
                   if r.class_id not in dropped)
        self._cache = out
        self._cache_index = {r.key: r for r in out}
        self._cache_dirty = False
        return out

    def record(self, key: SampleKey) -> Optional[SampleRecord]:
        """按主键取合并视图里的一条。

        走索引而不是扫 :meth:`records`：命中测试、拖顶点、检查器都按键取，
        十万级样本上每次扫一遍会让鼠标跟不上手。
        """
        self.records()
        return self._cache_index.get(key)

    def is_draft_feature(self, key: SampleKey) -> bool:
        return key[1] < 0

    def is_draft_class(self, class_id: int) -> bool:
        return class_id < 0

    def counts(self) -> Dict[str, int]:
        return {
            "新增要素": len(self._draft_records),
            "删除要素": len(self._deleted),
            "改动几何": len(self._geom_edits),
            "改动归类": len(self._class_edits),
            "改动名称": len(self._label_edits),
            "新增类别": len(self._draft_classes),
            "删除类别": len(self._deleted_classes),
            "类别改名": len(self._class_renames),
            "类别定种类": len(self._class_kinds),
        }

    def summary(self) -> str:
        items = [(k, v) for k, v in self.counts().items() if v]
        if not items:
            return "没有未保存的改动。"
        return "、".join("%s %d" % (k, v) for k, v in items)

    # ------------------------------------------------------------------ 撤销
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_label(self) -> str:
        return self._undo[-1].label if self._undo else ""

    def redo_label(self) -> str:
        return self._redo[-1].label if self._redo else ""

    def undo(self) -> str:
        if not self._undo:
            return ""
        op = self._undo.pop()
        op.undo()
        self._redo.append(op)
        self._touch()
        return op.label

    def redo(self) -> str:
        if not self._redo:
            return ""
        op = self._redo.pop()
        op.redo()
        self._undo.append(op)
        self._touch()
        return op.label

    def _apply(self, label: str, do: Callable[[], None],
               undo: Callable[[], None]) -> None:
        do()
        self._undo.append(_Op(label, undo, do))
        self._redo.clear()
        self._touch()

    # ------------------------------------------------------------------ 类别
    def add_class(self, name: str, region: str, geom_kind: str = "",
                  code: str = "", color: str = config.DEFAULT_CLASS_COLOR,
                  parent_id: Optional[int] = None,
                  sample_type: str = "", description: str = "") -> int:
        cid = self._next_class_id
        self._next_class_id -= 1
        draft = DraftClass(class_id=cid, name=name, code=code, region=region,
                           geom_kind=geom_kind, color=color, parent_id=parent_id,
                           sample_type=sample_type, description=description)

        def do():
            self._draft_classes[cid] = draft

        def undo():
            self._draft_classes.pop(cid, None)

        self._apply("新增类别「%s」" % name, do, undo)
        return cid

    def rename_class(self, class_id: int, name: str) -> None:
        if class_id in self._draft_classes:
            draft = self._draft_classes[class_id]
            old = draft.name

            def do():
                draft.name = name

            def undo():
                draft.name = old
        else:
            old = self._class_renames.get(class_id)

            def do():
                self._class_renames[class_id] = name

            def undo():
                if old is None:
                    self._class_renames.pop(class_id, None)
                else:
                    self._class_renames[class_id] = old

        self._apply("类别改名为「%s」" % name, do, undo)

    def remove_draft_class(self, class_id: int) -> None:
        """删除草稿类别及其下的草稿要素。库内类别不允许在标注窗口整类删除。"""
        if class_id not in self._draft_classes:
            raise ValueError("只能删除标注过程中新建的类别。")
        draft = self._draft_classes[class_id]
        victims = {k: r for k, r in self._draft_records.items()
                   if r.class_id == class_id}

        def do():
            self._draft_classes.pop(class_id, None)
            for k in victims:
                self._draft_records.pop(k, None)

        def undo():
            self._draft_classes[class_id] = draft
            self._draft_records.update(victims)

        self._apply("删除类别「%s」及其 %d 个要素" % (draft.name, len(victims)), do, undo)

    def delete_classes(self, class_ids: Iterable[int]) -> Tuple[int, int]:
        """删除若干类别，连同它们的子类与名下要素。

        草稿类别与库内类别都能删，两者只是落库方式不同：草稿直接从暂存里抹掉，
        库内的记在 ``_deleted_classes`` 里，保存时才走 ``store.delete_class``。
        在此之前一切照旧可撤销——这也是删除必须走暂存的原因，直接删库就没有
        后悔药了。

        返回 ``(删掉的类别数, 连带删掉的要素数)``。
        """
        victims: List[int] = []
        for cid in class_ids:
            victims.extend(self.subtree_class_ids(int(cid)))
        victims = list(dict.fromkeys(victims))
        doomed = set(victims)
        if not doomed:
            return 0, 0

        n_features = sum(1 for r in self.records() if r.class_id in doomed)
        drafts = {cid: self._draft_classes[cid]
                  for cid in victims if cid in self._draft_classes}
        draft_recs = {k: r for k, r in self._draft_records.items()
                      if r.class_id in doomed}
        reals = [cid for cid in victims
                 if cid >= 0 and cid not in self._deleted_classes]

        def do():
            for cid in drafts:
                self._draft_classes.pop(cid, None)
            for k in draft_recs:
                self._draft_records.pop(k, None)
            self._deleted_classes.update(reals)

        def undo():
            self._draft_classes.update(drafts)
            self._draft_records.update(draft_recs)
            self._deleted_classes.difference_update(reals)

        self._apply("删除 %d 个类别及其 %d 个要素" % (len(victims), n_features),
                    do, undo)
        return len(victims), n_features

    def delete_region(self, region: str) -> Tuple[int, int]:
        """删除一个工作区：它下面的类别与要素一起走。"""
        return self.delete_classes(self.region_class_ids(region))

    def merge_classes(self, source_ids: Iterable[int], target_id: int) -> int:
        """把若干类别的要素并进 ``target_id``，然后删掉被并掉的类别。

        合成一个撤销条目：使用者眼里"合并"是一步操作，撤销时要一次退回去，
        而不是先退出删除、再退出改归类。
        """
        doomed: List[int] = []
        for cid in source_ids:
            if int(cid) == int(target_id):
                continue
            doomed.extend(self.subtree_class_ids(int(cid)))
        doomed = [c for c in dict.fromkeys(doomed) if c != target_id]
        if not doomed:
            return 0
        keys = [r.key for r in self.records() if r.class_id in set(doomed)]

        mark = len(self._undo)
        if keys:
            self.move_features(keys, target_id)
        self.delete_classes(doomed)
        target = self.class_map().get(target_id)
        self._coalesce(mark, "把 %d 个类别的 %d 个要素并进「%s」"
                       % (len(doomed), len(keys),
                          target.name if target else target_id))
        return len(keys)

    def _coalesce(self, mark: int, label: str) -> None:
        """把 ``mark`` 之后压入的若干步合成一个撤销条目。"""
        ops = self._undo[mark:]
        if len(ops) <= 1:
            if ops:
                self._undo[-1] = _Op(label, ops[0].undo, ops[0].redo)
            return
        del self._undo[mark:]

        def undo():
            for op in reversed(ops):
                op.undo()

        def redo():
            for op in ops:
                op.redo()

        self._undo.append(_Op(label, undo, redo))
        self._redo.clear()

    # ------------------------------------------------------------------ 种类
    # 基础类别只装一种几何。种类由第一条样本定：新建的类别 geom_kind 为空，画进
    # 去的是点它就是点类。之后再画另一种就拒绝——一个类别里点面混着，导出要拆成
    # 两个文件、裁剪要按两套规则算，谁也说不清这个类别到底是什么。
    def class_kind(self, class_id: int) -> str:
        """类别当前的几何种类：字段里定了按字段；没定则看它手里的样本。"""
        cls = self.class_map().get(class_id)
        if cls is None:
            return ""
        if cls.is_group:
            return cls.GROUP
        # 先看手里的样本：老库的字段可能写着 point 却点面都有，以实物为准
        kinds = {r.geom_kind for r in self.records() if r.class_id == class_id}
        if len(kinds) == 1:
            return next(iter(kinds))
        if len(kinds) > 1:
            return "mixed"
        return cls.geom_kind            # 空类别：字段定了就按字段，否则 ""

    def can_add(self, class_id: int, geom_kind: str) -> str:
        """能不能往这个类别里放一条这种几何。能就返回空串，否则返回原因。"""
        cls = self.class_map().get(class_id)
        if cls is None:
            return "类别不存在。"
        if cls.is_group:
            return "「%s」是分组，本身不存样本。请选它下面的类别。" % cls.name
        current = self.class_kind(class_id)
        if current in (GEOM_POINT, GEOM_POLYGON) and current != geom_kind:
            return "「%s」是%s样本类别，不能再放%s。要画%s请新建一个类别。" % (
                cls.name, "点" if current == GEOM_POINT else "面",
                "点" if geom_kind == GEOM_POINT else "面",
                "点" if geom_kind == GEOM_POINT else "面")
        return ""

    def can_move(self, keys: Iterable[SampleKey], class_id: int) -> str:
        """这些要素能不能改归到该类别。"""
        kinds = {k[0] for k in self._live(keys)}
        if len(kinds) > 1:
            return "选中的要素点面都有，一个类别只能装一种，请分开改归。"
        return self.can_add(class_id, next(iter(kinds))) if kinds else ""

    def _settle_kind(self, class_id: int, geom_kind: str):
        """类别种类还没定时，用这条要素把它定下来。返回撤销用的回调。"""
        cls = self.class_map().get(class_id)
        if cls is None or cls.geom_kind in (GEOM_POINT, GEOM_POLYGON, cls.GROUP):
            return lambda: None
        if self.class_kind(class_id) == "mixed":
            return lambda: None         # 老库的混合类别，不替它定
        if class_id in self._draft_classes:
            draft = self._draft_classes[class_id]
            old = draft.geom_kind
            draft.geom_kind = geom_kind

            def undo():
                draft.geom_kind = old
            return undo
        had = class_id in self._class_kinds
        old_kind = self._class_kinds.get(class_id)
        self._class_kinds[class_id] = geom_kind

        def undo():
            if had:
                self._class_kinds[class_id] = old_kind
            else:
                self._class_kinds.pop(class_id, None)
        return undo

    # ------------------------------------------------------------------ 要素
    def add_feature(self, class_id: int, geom_kind: str, coords,
                    label: str = "", source: str = "标注") -> SampleKey:
        reason = self.can_add(class_id, geom_kind)
        if reason:
            raise ValueError(reason)
        key: SampleKey = (geom_kind, self._next_fid)
        self._next_fid -= 1
        rec = SampleRecord(key=key, class_id=class_id, geom_kind=geom_kind,
                           coords=coords, bbox=bbox_of(geom_kind, coords),
                           source=source, label=label)
        undo_kind = [lambda: None]

        def do():
            self._draft_records[key] = rec
            undo_kind[0] = self._settle_kind(class_id, geom_kind)

        def undo():
            self._draft_records.pop(key, None)
            undo_kind[0]()

        self._apply("新增%s要素" % ("点" if geom_kind == GEOM_POINT else "面"), do, undo)
        return key

    def _live(self, keys: Iterable[SampleKey]) -> List[SampleKey]:
        """只留合并视图里还在的主键。

        画布的选中集是一份独立的集合，不跟着撤销走：画了个点选中它、再撤销那一
        笔，选中集里还留着那个已经不存在的草稿主键。拿它去删或改归，会往
        ``_deleted`` / ``_class_edits`` 里塞一条指向空处的记录，报的数目也不对。
        """
        return [k for k in keys if self.record(k) is not None]

    def delete_features(self, keys: Iterable[SampleKey]) -> int:
        keys = self._live(keys)
        if not keys:
            return 0
        drafts = {k: self._draft_records[k] for k in keys if k in self._draft_records}
        reals = [k for k in keys if k not in drafts]

        def do():
            for k in drafts:
                self._draft_records.pop(k, None)
            self._deleted.update(reals)

        def undo():
            self._draft_records.update(drafts)
            self._deleted.difference_update(reals)

        self._apply("删除 %d 个要素" % len(keys), do, undo)
        return len(keys)

    def set_geometry(self, key: SampleKey, coords) -> None:
        if key in self._draft_records:
            rec = self._draft_records[key]
            old = rec.coords
            old_box = rec.bbox

            def do():
                rec.coords = coords
                rec.bbox = bbox_of(rec.geom_kind, coords)

            def undo():
                rec.coords = old
                rec.bbox = old_box
        else:
            had = key in self._geom_edits
            old = self._geom_edits.get(key)

            def do():
                self._geom_edits[key] = coords

            def undo():
                if had:
                    self._geom_edits[key] = old
                else:
                    self._geom_edits.pop(key, None)

        self._apply("修改要素几何", do, undo)

    def move_features(self, keys: Iterable[SampleKey], class_id: int) -> int:
        keys = self._live(keys)
        if not keys:
            return 0
        reason = self.can_move(keys, class_id)
        if reason:
            raise ValueError(reason)
        old_draft = {k: self._draft_records[k].class_id
                     for k in keys if k in self._draft_records}
        reals = [k for k in keys if k not in old_draft]
        old_real = {k: self._class_edits.get(k) for k in reals}
        undo_kind = [lambda: None]

        def do():
            for k in old_draft:
                self._draft_records[k].class_id = class_id
            for k in reals:
                self._class_edits[k] = class_id
            undo_kind[0] = self._settle_kind(class_id, keys[0][0])

        def undo():
            for k, cid in old_draft.items():
                self._draft_records[k].class_id = cid
            for k, cid in old_real.items():
                if cid is None:
                    self._class_edits.pop(k, None)
                else:
                    self._class_edits[k] = cid
            undo_kind[0]()

        self._apply("把 %d 个要素改归到另一类别" % len(keys), do, undo)
        return len(keys)

    def set_label(self, key: SampleKey, label: str) -> None:
        if key in self._draft_records:
            rec = self._draft_records[key]
            old = rec.label

            def do():
                rec.label = label

            def undo():
                rec.label = old
        else:
            had = key in self._label_edits
            old = self._label_edits.get(key)

            def do():
                self._label_edits[key] = label

            def undo():
                if had:
                    self._label_edits[key] = old
                else:
                    self._label_edits.pop(key, None)

        self._apply("要素改名", do, undo)

    # ------------------------------------------------------------------ 落库
    def commit(self, backup: bool = False) -> Dict[str, object]:
        """把暂存的改动一次性写进样本库。

        先建类别（父类在前，因为子类要引用父类真实 id），再写要素，最后处理
        改动与删除。**整个过程包在一个事务里**：任何一步抛错，前面已经写下的
        新类别、新要素统统回滚，样本库退回保存前的样子，缓冲区仍是"未保存"
        状态，使用者改完重试即可。

        没有事务的话，半途失败会留下一批已经落库的新类别，而缓冲区并不知情，
        重试就再建一遍同名类别——库里堆出重复且没有要素的空类别，这种脏数据
        事后很难分辨该删哪一个。
        """
        if not self.dirty:
            return {"backup": "", "written": 0, "deleted": 0, "classes": 0,
                    "dropped_classes": 0, "keys": []}

        # 默认不备份：界面会在写入前提醒使用者自己决定要不要先存一份。
        # 调用方（脚本等）仍可显式传 backup=True 强制备份。
        backup_path = self.store.backup("标注") if backup else ""
        id_map: Dict[int, int] = {}

        with self.store.transaction():

            # 1. 新类别，按依赖顺序：父类必须先落库拿到真实 id
            pending = dict(self._draft_classes)
            while pending:
                progressed = False
                for cid, draft in list(pending.items()):
                    parent = draft.parent_id
                    if parent is not None and parent < 0:
                        if parent not in id_map:
                            continue                      # 父类还没落库，下一轮再来
                        parent = id_map[parent]
                    real = self.store.add_class(
                        name=draft.name, code=draft.code, region=draft.region,
                        sample_type=draft.sample_type, geom_kind=draft.geom_kind,
                        color=draft.color, description=draft.description,
                        parent_id=parent, source="标注", user_created=True)
                    id_map[cid] = real
                    pending.pop(cid)
                    progressed = True
                if not progressed:
                    raise RuntimeError("草稿类别的父子关系成环，无法写入：%s"
                                       % sorted(pending))

            # 2. 新要素。落库后的主键要留给调用方：草稿在画布上一直看得见，
            #    保存完却可能因为「只显示勾选」而集体消失——界面得知道是哪几条，
            #    才好把它们一并勾上。见 LibraryWindow.commit。
            new_keys: List[SampleKey] = []
            for rec in self._draft_records.values():
                geom = coords_to_geom(rec.geom_kind, rec.coords)
                if geom is None:
                    continue
                cid = id_map.get(rec.class_id, rec.class_id)
                new_keys.append(self.store.insert_sample(
                    cid, geom, source=rec.source, label=rec.label,
                    attrs=rec.attrs or None))
            written = len(new_keys)

            # 3. 已有要素的改动
            for key, coords in self._geom_edits.items():
                if key in self._deleted:
                    continue
                geom = coords_to_geom(key[0], coords)
                if geom is not None:
                    self.store.set_sample_geometry(key, geom)
            by_target: Dict[int, List[SampleKey]] = {}
            for key, cid in self._class_edits.items():
                if key in self._deleted:
                    continue
                by_target.setdefault(id_map.get(cid, cid), []).append(key)
            for cid, keys in by_target.items():
                self.store.move_samples(keys, cid)
            for key, label in self._label_edits.items():
                if key not in self._deleted:
                    self.store.set_sample_label(key, label)
            for cid, name in self._class_renames.items():
                self.store.update_class(cid, name=name)
            for cid, kind in self._class_kinds.items():
                if cid not in self._deleted_classes:
                    self.store.update_class(cid, geom_kind=kind)

            # 4. 删除。类别一定放在改动之后：合并类别时要素先改归到目标类，
            #    再删源类别；顺序反过来会连着要素一起被 delete_class 删掉。
            deleted = self.store.delete_samples(self._deleted) if self._deleted else 0
            # 只对这批里的"根"调 delete_class，子孙由它自己递归带走。挨个 id 删会出
            # 事：父类别先删就把子类别一并带走了，轮到子类别时删的是一个已经不存在
            # 的要素，GDAL 直接抛错，整次保存半途而废。
            doomed = set(self._deleted_classes)
            parent_of = {c.class_id: c.parent_id for c in self._base_classes}
            for cid in sorted(doomed):
                if parent_of.get(cid) in doomed:
                    continue
                deleted += self.store.delete_class(cid)
            dropped_classes = len(doomed)

        report = {"backup": backup_path, "written": written,
                  "deleted": deleted, "classes": len(id_map),
                  "dropped_classes": dropped_classes, "keys": new_keys}
        self.reload()
        return report

    def discard(self) -> None:
        """放弃所有暂存改动，回到打开时的状态。"""
        self.reload()
