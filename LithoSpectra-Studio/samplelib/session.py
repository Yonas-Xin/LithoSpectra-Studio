# -*- coding: utf-8 -*-
"""会话层：把样本库、内存缓存、当前选择与感兴趣区组织成界面可直接消费的状态。

界面层只与本模块交互，不直接操作 GDAL，因此这里的逻辑可以脱离 GUI 单独测试。
"""

import os
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import config, io_attr, io_shp
from .model import (ExportReport, ImportReport, SampleClass, SampleKey,
                    SampleRecord)
from .roi import (ROI, load_roi, roi_from_features, roi_from_raster,
                  select_by_roi)
from .store import SampleStore


class LibrarySession:
    """一次样本库编辑会话。"""

    def __init__(self) -> None:
        self.store: Optional[SampleStore] = None
        self.classes: List[SampleClass] = []
        self.records: List[SampleRecord] = []
        self.class_map: Dict[int, SampleClass] = {}
        self.records_by_class: Dict[int, List[SampleRecord]] = {}
        self.record_by_key: Dict[SampleKey, SampleRecord] = {}
        self.children: Dict[Optional[int], List[int]] = {}
        self.selection: Set[SampleKey] = set()
        self.roi: Optional[ROI] = None
        self._undo: List[Tuple[str, object]] = []
        # 数据版本号：类别或样本的内存副本每换一次加一（勾选不算，那是视图的事）。
        # 画布的离屏缓存拿它当指纹，见 annotate.layers.SessionSource。
        self.version = 0

    # ------------------------------------------------------------------ 状态
    @property
    def is_open(self) -> bool:
        return self.store is not None

    @property
    def library_name(self) -> str:
        return self.store.name if self.store else ""

    @property
    def library_path(self) -> str:
        return self.store.path if self.store else ""

    def require_store(self) -> SampleStore:
        if self.store is None:
            raise RuntimeError("尚未打开样本库。")
        return self.store

    # ------------------------------------------------------------------ 生命周期
    def create(self, path: str, name: str = "", description: str = "",
               overwrite: bool = False) -> None:
        self.close()
        self.store = SampleStore.create(path, name=name, description=description,
                                        overwrite=overwrite)
        self.reload()

    def open(self, path: str) -> None:
        self.close()
        self.store = SampleStore.open(path, update=True)
        self.reload()

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
        self.store = None
        self.classes = []
        self.records = []
        self.class_map = {}
        self.records_by_class = {}
        self.record_by_key = {}
        self.children = {}
        self.selection = set()
        self.roi = None
        self._undo = []
        self.version += 1

    def reload_classes(self) -> None:
        """只重读类别表，内存里的样本原封不动。

        改名、改类别号、改说明、调整顺序这些操作一条样本都没动，却会触发
        :meth:`reload` 把全库几何重读一遍——两万条点样本上光是改个类别名就要
        零点几秒，十万级会明显卡顿，而且每次都重建一遍样本对象，内存反复起落。
        动到样本归属或数量的操作（导入、删除、合并、撤销）仍然要走
        :meth:`reload`。
        """
        store = self.require_store()
        self.version += 1
        self.classes = store.list_classes()
        self.class_map = {c.class_id: c for c in self.classes}
        self.children = {}
        for c in self.classes:
            parent = c.parent_id if c.parent_id in self.class_map else None
            self.children.setdefault(parent, []).append(c.class_id)

    def reload(self) -> None:
        """重新从样本库读取类别与样本，并清理失效的选择。"""
        store = self.require_store()
        self.reload_classes()
        self.records = store.load_records()
        self.records_by_class = {}
        self.record_by_key = {}
        for rec in self.records:
            self.records_by_class.setdefault(rec.class_id, []).append(rec)
            self.record_by_key[rec.key] = rec
        self.selection &= set(self.record_by_key)

    # ------------------------------------------------------------------ 检索
    def filtered_classes(self, keyword: str = "") -> List[SampleClass]:
        if not keyword:
            return list(self.classes)
        return [c for c in self.classes if c.matches(keyword)]

    def regions(self, classes: Optional[Sequence[SampleClass]] = None) -> List[str]:
        seen: List[str] = []
        for c in (classes if classes is not None else self.classes):
            if c.region not in seen:
                seen.append(c.region)
        return seen

    def child_class_ids(self, parent_id: Optional[int]) -> List[int]:
        """某个类别（或顶层）的直接子类别 id，按类别列表原有顺序。"""
        return list(self.children.get(parent_id, []))

    def root_class_ids(self, region: Optional[str] = None) -> List[int]:
        """顶层类别 id；给定工区时只返回该工区的。"""
        ids = self.child_class_ids(None)
        if region is None:
            return ids
        return [i for i in ids if self.class_map[i].region == region]

    def subtree_class_ids(self, class_id: int) -> List[int]:
        """类别自身及其全部子孙类别 id。"""
        out, stack, guard = [], [class_id], set()
        while stack:
            cid = stack.pop()
            if cid in guard:
                continue
            guard.add(cid)
            out.append(cid)
            stack.extend(self.children.get(cid, []))
        return out

    def ancestor_ids(self, class_id: int) -> List[int]:
        """自下而上的祖先类别 id（不含自身）。"""
        out, guard = [], set()
        cls = self.class_map.get(class_id)
        pid = cls.parent_id if cls else None
        while pid is not None and pid in self.class_map and pid not in guard:
            guard.add(pid)
            out.append(pid)
            pid = self.class_map[pid].parent_id
        return out

    def keys_of_class(self, class_id: int) -> List[SampleKey]:
        return [r.key for r in self.records_by_class.get(class_id, [])]

    def keys_of_subtree(self, class_id: int) -> List[SampleKey]:
        """类别及其子孙类别持有的全部样本主键。"""
        keys: List[SampleKey] = []
        for cid in self.subtree_class_ids(class_id):
            keys.extend(self.keys_of_class(cid))
        return keys

    def keys_of_classes(self, class_ids: Iterable[int]) -> Set[SampleKey]:
        keys: Set[SampleKey] = set()
        for cid in class_ids:
            keys.update(self.keys_of_class(cid))
        return keys

    # ------------------------------------------------------------------ 选择
    def set_selection(self, keys: Iterable[SampleKey]) -> None:
        self.selection = {k for k in keys if k in self.record_by_key}

    def select_keys(self, keys: Iterable[SampleKey], on: bool = True) -> None:
        keys = {k for k in keys if k in self.record_by_key}
        if on:
            self.selection |= keys
        else:
            self.selection -= keys

    def toggle_key(self, key: SampleKey) -> bool:
        if key in self.selection:
            self.selection.discard(key)
            return False
        if key in self.record_by_key:
            self.selection.add(key)
            return True
        return False

    def select_class(self, class_id: int, on: bool = True) -> None:
        """勾选一个类别：连同其子类别一起处理，父类别本身可以不持有样本。"""
        self.select_keys(self.keys_of_subtree(class_id), on)

    def select_all(self, class_ids: Optional[Iterable[int]] = None) -> None:
        if class_ids is None:
            self.selection = set(self.record_by_key)
        else:
            self.selection = self.keys_of_classes(class_ids)

    def clear_selection(self) -> None:
        self.selection = set()

    def invert_selection(self, class_ids: Optional[Iterable[int]] = None) -> None:
        universe = (set(self.record_by_key) if class_ids is None
                    else self.keys_of_classes(class_ids))
        self.selection = (universe - self.selection) | (self.selection - universe)

    def class_selection_state(self, class_id: int) -> str:
        """类别（含子类别）的勾选状态：none / partial / all。"""
        keys = self.keys_of_subtree(class_id)
        if not keys:
            return "none"
        hit = sum(1 for k in keys if k in self.selection)
        if hit == 0:
            return "none"
        return "all" if hit == len(keys) else "partial"

    def selection_summary(self) -> Tuple[int, int]:
        """返回 (已选样本数, 涉及类别数)。"""
        cids = {self.record_by_key[k].class_id for k in self.selection
                if k in self.record_by_key}
        return len(self.selection), len(cids)

    def point_class_ids(self, class_ids: Optional[Iterable[int]] = None) -> List[int]:
        """持有点样本的基础类别；给了 ``class_ids`` 只在其中挑。缓冲对话框用。"""
        pool = list(class_ids) if class_ids is not None else [c.class_id for c in self.classes]
        return [cid for cid in pool
                if any(r.geom_kind == "point" for r in self.records_by_class.get(cid, ()))]

    def selected_class_ids(self) -> List[int]:
        cids = {self.record_by_key[k].class_id for k in self.selection
                if k in self.record_by_key}
        return [c.class_id for c in self.classes if c.class_id in cids]

    def export_preview(self, level, unit_class_ids: Optional[Iterable[int]] = None,
                       use_selection: bool = True) -> dict:
        """按某个层级导出 / 裁剪会得到什么：单元数、文件数、样本数与兜底说明。

        给对话框顶上的那行提示用，换一个层级就重算一次。口径与真正导出时
        :func:`io_shp.export_targets` 完全一样——预告和结果对不上，比没有预告更糟。
        文件数按"一个单元里点面都有就拆两个文件"算，与导出一致。
        """
        by_id = self.class_map
        allowed = (set(self.selected_class_ids()) if use_selection else set(by_id))
        lvl = level if level in ("custom", "leaf") else int(level)
        targets, notes = io_shp.export_targets(self.classes, by_id, allowed, lvl,
                                               unit_class_ids)
        files = 0
        samples = 0
        for _unit, source_ids in targets:
            kinds = set()
            for cid in source_ids:
                for rec in self.records_by_class.get(cid, ()):
                    if use_selection and rec.key not in self.selection:
                        continue
                    kinds.add(rec.geom_kind)
                    samples += 1
            files += max(1, len(kinds))
        return {"units": len(targets), "files": files, "samples": samples,
                "classes": len(allowed), "notes": list(notes)}

    def export_units_from_selection(self,
                                    merge_ids: Optional[Iterable[int]] = None,
                                    split_ids: Optional[Iterable[int]] = None
                                    ) -> List[int]:
        """把树上的勾选状态换算成导出单元。

        默认规则：整支被勾选的类别直接作为一个单元（不再下钻）；只勾了一部分
        时继续下钻到子类别，自身若还直接持有被选样本、且下面没有子单元，则
        自身也算一个单元。也就是"勾了大类"导成一个文件，"只勾了小类"各自成文件。

        勾选状态只能表达"要哪些样本"，表达不了"这一支按哪一层拆文件"——同一次
        导出里往往一支要合并、另一支要拆开。``merge_ids`` / ``split_ids`` 就是
        针对单个分支的显式覆盖：

        * ``merge_ids`` 中的类别整支作为一个单元，即使只勾选了其中一部分；
        * ``split_ids`` 中的类别自身不成单元，改为下钻到子类别，即使整支都被勾选。

        两者都只影响所在分支，其余分支仍按默认规则。每条被选样本恰好归到链上
        最深的那个单元，不会落进两个文件；父单元与子单元同在时，父单元只装它
        自己直接持有的样本。
        """
        merge = {int(c) for c in (merge_ids or ())}
        split = {int(c) for c in (split_ids or ())}
        units: List[int] = []

        def walk(cid: int) -> bool:
            """返回该分支下是否产生了单元。"""
            state = self.class_selection_state(cid)
            if state == "none":
                return False
            if cid in merge:                       # 显式要求整支合并
                units.append(cid)
                return True
            if cid not in split and state == "all":
                units.append(cid)
                return True
            produced = False
            for child in self.child_class_ids(cid):
                produced = walk(child) or produced
            # 自身直接持有被选样本就自成一个单元——**不管子类别有没有产生单元**。
            # 老库里"沉积岩"自己有 3 个点、下面又挂着砂岩 / 泥岩：只勾砂岩再
            # 框选那 3 个点，或者给沉积岩打了「分」标记，子类别各成一个单元，
            # 沉积岩自己的 3 条却没有单元管，导出时报"不在任何单元之下"就丢了。
            # 单元列表里于是可能父子同在；_export_targets 按最深的那个归属，
            # 父单元只装它自己直接持有的样本。
            if any(k in self.selection for k in self.keys_of_class(cid)):
                units.append(cid)
                produced = True
            return produced

        for region in self.regions():
            for cid in self.root_class_ids(region):
                walk(cid)
        return units

    # ------------------------------------------------------------------ 范围
    def extent_of_keys(self, keys: Iterable[SampleKey]
                       ) -> Optional[Tuple[float, float, float, float]]:
        box = None
        for k in keys:
            rec = self.record_by_key.get(k)
            if rec is None:
                continue
            b = rec.bbox
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                         max(box[2], b[2]), max(box[3], b[3]))
        return box

    def full_extent(self) -> Optional[Tuple[float, float, float, float]]:
        return self.extent_of_keys(self.record_by_key)

    # ------------------------------------------------------------------ 感兴趣区
    def load_roi(self, path: str) -> ROI:
        self.roi = load_roi(path)
        return self.roi

    def set_roi_from_features(self, features, name: str = "图层范围") -> ROI:
        """拿画布上某个图层的要素当感兴趣区。"""
        self.roi = roi_from_features(features, name=name)
        return self.roi

    def set_roi_from_raster(self, path: str, valid_only: bool = True) -> ROI:
        """拿一景影像的覆盖范围当感兴趣区。

        默认取有效数据轮廓而不是四至：影像常常是斜的，四至会把大片黑边圈进来，
        用它筛样本会选中一堆压根没有影像覆盖的点——而"这批样本在这景影像上裁得
        出块吗"正是要问的问题。
        """
        self.roi = roi_from_raster(path, valid_only=valid_only)
        return self.roi

    def clear_roi(self) -> None:
        self.roi = None

    def apply_roi(self, mode: str = config.DEFAULT_ROI_MODE,
                  class_ids: Optional[Iterable[int]] = None,
                  add: bool = False, progress=None) -> int:
        """按感兴趣区选择样本，返回命中数量。"""
        if self.roi is None:
            raise RuntimeError("尚未导入感兴趣区。")
        hits = select_by_roi(self.require_store(), self.roi, mode=mode,
                             class_ids=class_ids, progress=progress)
        if add:
            self.selection |= hits
        else:
            self.selection = hits
        return len(hits)

    # ------------------------------------------------------------------ 导入导出
    def import_dir(self, directory: str, region: str = "", sample_type: str = "",
                   progress=None) -> ImportReport:
        report = io_shp.import_vector_dir(self.require_store(), directory,
                                          region=region, sample_type=sample_type,
                                          progress=progress)
        self.reload()
        return report

    def import_root(self, root: str, progress=None) -> List[ImportReport]:
        reports = io_shp.import_library_root(self.require_store(), root, progress=progress)
        self.reload()
        return reports

    def import_file(self, path: str, region: str = "", sample_type: str = "",
                    progress=None) -> ImportReport:
        report = ImportReport(library=self.library_name,
                              region=region or os.path.basename(os.path.dirname(path)))
        if progress is not None:
            progress(0, 1, os.path.basename(path))
        io_shp.import_vector_file(self.require_store(), path, region=report.region,
                                  sample_type=sample_type, report=report)
        self.reload()
        return report

    def profile_vector(self, path: str):
        """剖析矢量文件字段，供"按属性导入"对话框使用。"""
        return io_attr.profile_fields(path)

    def import_by_attribute(self, path: str, class_fields, region: str = "",
                            name_field: str = "", code_field: str = "",
                            sample_type: str = "", description_fields=(),
                            collapse_empty_levels: bool = True,
                            progress=None) -> ImportReport:
        report = io_attr.import_by_attribute(
            self.require_store(), path, class_fields=class_fields, region=region,
            name_field=name_field, code_field=code_field, sample_type=sample_type,
            description_fields=description_fields,
            collapse_empty_levels=collapse_empty_levels, progress=progress)
        self.reload()
        return report

    def export(self, out_dir: str, use_selection: bool = True,
               class_ids: Optional[Iterable[int]] = None,
               driver_key: str = config.DEFAULT_EXPORT_DRIVER,
               target_crs: str = "",
               name_template: str = config.DEFAULT_EXPORT_TEMPLATE,
               level="leaf", unit_class_ids: Optional[Iterable[int]] = None,
               restore_attrs: bool = False, buffer_radius: float = 0.0,
               buffer_class_ids: Optional[Iterable[int]] = None,
               progress=None) -> ExportReport:
        """导出样本，始终遵循「一个矢量文件代表一类样本」。

        ``level`` 决定"一类"的口径：``"leaf"`` 每个末级类别一个文件、
        整数 N 按第 N 层类别、``"custom"`` 由 ``unit_class_ids`` 指定任意层级
        的类别作为导出单元。
        """
        selection = self.selection if use_selection else None
        if use_selection and class_ids is None:
            class_ids = self.selected_class_ids()
        return io_shp.export_classes(self.require_store(), out_dir,
                                     class_ids=class_ids, selection=selection,
                                     driver_key=driver_key, target_crs=target_crs,
                                     name_template=name_template,
                                     level=level, unit_class_ids=unit_class_ids,
                                     restore_attrs=restore_attrs,
                                     buffer_radius=buffer_radius,
                                     buffer_class_ids=buffer_class_ids,
                                     progress=progress)

    def delete_classes(self, class_ids: Iterable[int]) -> int:
        """删除若干类别（连同子类别与样本），返回删掉的样本数。

        传进来的列表里父子同在时只删"根"：子类别会随父类别一起走，再单独删一次
        就是删一个已经不存在的类别。整批放在一个事务里，删到一半出错就全部回滚。
        """
        store = self.require_store()
        roots = self._subtree_roots(class_ids)
        removed = 0
        with store.transaction():
            for cid in roots:
                removed += store.delete_class(cid)
        self.reload()
        return removed

    def region_class_ids(self, region: str) -> List[int]:
        """工区下的全部类别 id（每一级都算）。"""
        return [c.class_id for c in self.classes if c.region == region]

    def delete_region(self, region: str) -> Tuple[int, int]:
        """删掉整个工区：它下面每一级类别连同样本。返回 ``(类别数, 样本数)``。

        工区不是独立的表，只是类别上的一个字段，删工区就是删这些类别。与
        :meth:`delete_classes` 一样只删子树的根，整批一个事务。
        """
        ids = self.region_class_ids(region)
        if not ids:
            return 0, 0
        removed = self.delete_classes(ids)
        return len(ids), removed

    def _subtree_roots(self, class_ids: Iterable[int]) -> List[int]:
        """去掉列表里被其它成员覆盖（是其子孙）的类别，只留各棵子树的根。"""
        wanted = [int(c) for c in dict.fromkeys(class_ids)]
        covered: Set[int] = set()
        for cid in wanted:
            covered.update(self.subtree_class_ids(cid)[1:])
        return [cid for cid in wanted if cid not in covered]

    # ------------------------------------------------------------------ 编辑
    def max_depth(self) -> int:
        """类别树的最大层数。"""
        if not self.is_open:
            return 0
        depths = [self.class_depth(c.class_id) for c in self.classes]
        return max(depths) if depths else 0

    def class_depth(self, class_id: int) -> int:
        """类别所在层级，最上层为 1。"""
        depth, guard = 1, set()
        cls = self.class_map.get(class_id)
        pid = cls.parent_id if cls else None
        while pid is not None and pid in self.class_map and pid not in guard:
            guard.add(pid)
            depth += 1
            pid = self.class_map[pid].parent_id
        return depth

    def class_path(self, class_id: int, sep: str = " / ") -> str:
        """类别的层级路径。"""
        cls = self.class_map.get(class_id)
        if cls is None:
            return ""
        names, guard = [cls.name], set()
        pid = cls.parent_id
        while pid is not None and pid in self.class_map and pid not in guard:
            guard.add(pid)
            names.append(self.class_map[pid].name)
            pid = self.class_map[pid].parent_id
        return sep.join(reversed(names))

    def _record_undo(self, label: str, inverse) -> None:
        self._undo.append((label, inverse))
        del self._undo[:-100]                      # 只保留最近 100 步

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def undo_label(self) -> str:
        return self._undo[-1][0] if self._undo else ""

    def undo(self) -> str:
        """撤销上一步编辑，返回被撤销操作的描述。

        逆操作抛错时把条目放回栈顶再往外抛：撤销失败不等于这一步已经撤掉了，
        条目丢了使用者就再也没机会重试或看到它。库内数据由 store 的事务保证不会
        写到一半。
        """
        if not self._undo:
            return ""
        label, inverse = self._undo[-1]
        try:
            inverse()
        except Exception:
            self.reload()
            raise
        self._undo.pop()
        self.reload()
        return label

    def rename_class(self, class_id: int, name: str) -> None:
        store = self.require_store()
        old = self.class_map[class_id].name
        store.update_class(class_id, name=name)
        self._record_undo("重命名类别「%s」" % old,
                          lambda: store.update_class(class_id, name=old))
        self.reload_classes()


    def set_class_description(self, class_id: int, text: str) -> None:
        store = self.require_store()
        old = self.class_map[class_id].description
        store.update_class(class_id, description=text)
        self._record_undo("修改类别说明",
                          lambda: store.update_class(class_id, description=old))
        self.reload_classes()

    def add_class(self, name: str, region: str = "",
                  parent_id: Optional[int] = None,
                  color: str = "", geom_kind: str = "") -> int:
        """新建一个空类别，直接落库。

        与 :meth:`add_group` 的区别：分组只是层级容器，自身不持有样本
        （``geom_kind="group"``）；这里建的是真的能往里画样本的类别。

        标注时新建类别走这里而不是走 EditBuffer：类别是库的结构，不是几何
        草稿。落进缓冲区的话它在样本树上看不见，而样本树正是选"当前类别"的
        地方——新建完一个类却选不中它，等于没建。
        """
        store = self.require_store()
        parent = self.class_map.get(parent_id) if parent_id is not None else None
        if parent is not None and parent.is_leaf:
            raise ValueError("「%s」是基础类别，下面不能再建类别。" % parent.name)
        # geom_kind 留空表示"还没定"：第一条样本画进去是点就是点类，是面就是面类
        kwargs = {"name": name, "region": region, "parent_id": parent_id,
                  "user_created": True, "geom_kind": geom_kind or ""}
        if color:
            kwargs["color"] = color
        class_id = store.add_class(**kwargs)
        self._record_undo("新建类别「%s」" % name,
                          lambda: self._undo_new_class(store, class_id, name))
        self.reload_classes()
        return class_id

    @staticmethod
    def _undo_new_class(store: SampleStore, class_id: int, name: str) -> None:
        """撤销"新建类别"：类别里已经有了样本或子类别就拒绝。

        新建之后使用者可能已经在标注模式里往这个类别画了样本并保存——那些样本
        不在本撤销栈里，``delete_class`` 会把它们一起带走，而且找不回来。
        """
        if store.has_samples(class_id) or store.child_class_ids(class_id):
            raise ValueError("类别「%s」已经有了样本或子类别，不能撤销新建。"
                             "要删它请用「删除该类别及其样本」。" % name)
        store.delete_class(class_id)

    def set_class_color(self, class_id: int, color: str) -> None:
        store = self.require_store()
        old = self.class_map[class_id].color
        store.update_class(class_id, color=color)
        self._record_undo("修改类别颜色",
                          lambda: store.update_class(class_id, color=old))
        self.reload_classes()

    def merge_classes(self, source_ids: Iterable[int], target_id: int) -> int:
        """把若干类别（整支，含子类别）的样本并进目标类别，然后删掉源类别。

        源类别按整棵子树处理：勾选一个父类别再合并，它下面每一级的样本都并进
        目标，然后整支删掉。早先只搬父类别自己直接持有的样本，随后
        ``delete_class`` 递归删子树时把子类别里的样本一并删了——合并变成了删除，
        且撤销也救不回来。

        目标类别不能落在任何一个源的子树里（把父类并进它自己的子类，删父类时
        子类也没了）；已勾到的子类别若其父也在源里，只按父那一支算一次。

        撤销要把样本按原属分别送回去、把整支类别原样放回（含 origin 与 attrs），
        所以合并前先记下每个 key 原来属于谁与每个类别的完整快照。
        """
        store = self.require_store()
        target_id = int(target_id)
        roots = [c for c in self._subtree_roots(source_ids) if c != target_id]
        if not roots:
            return 0
        doomed: List[int] = []
        for cid in roots:
            doomed.extend(self.subtree_class_ids(cid))
        doomed = [c for c in dict.fromkeys(doomed) if c != target_id]
        if target_id in {c for r in roots for c in self.subtree_class_ids(r)}:
            raise ValueError("目标类别在要合并的分支里面，不能把一支合并进它自己的子类别。")
        if target_id not in self.class_map:
            raise KeyError("目标类别不存在：%s" % target_id)

        origin: Dict[int, List[SampleKey]] = {}
        snapshot = [self.class_map[c] for c in doomed if c in self.class_map]
        moved = 0
        with store.transaction():
            for cid in doomed:
                keys = self.keys_of_class(cid)
                if keys:
                    origin[cid] = list(keys)
                    moved += store.move_samples(keys, target_id)
            for cid in roots:
                store.delete_class(cid)

        def inverse():
            with store.transaction():
                # 父在前：子类别的 parent_id 指向父，父不先回来树上就悬空一截
                order = {c.class_id: i for i, c in enumerate(snapshot)}
                pending = list(snapshot)
                placed: Set[int] = set()
                while pending:
                    rest = []
                    for cls in pending:
                        pid = cls.parent_id
                        if pid in order and pid not in placed:
                            rest.append(cls)
                            continue
                        store.restore_class_row(cls)
                        placed.add(cls.class_id)
                    if len(rest) == len(pending):
                        for cls in rest:              # 不该发生，兜底别死循环
                            store.restore_class_row(cls)
                        break
                    pending = rest
                for cid, keys in origin.items():
                    store.move_samples(keys, cid)

        self._record_undo("合并 %d 个类别" % len(doomed), inverse)
        self.reload()
        return moved

    def move_class(self, class_id: int, new_parent_id: Optional[int]) -> None:
        """把类别挂到新的父类别下（``None`` 为顶层）。"""
        store = self.require_store()
        cls = self.class_map[class_id]
        target = self.class_map.get(new_parent_id) if new_parent_id is not None else None
        if target is not None and target.is_leaf:
            raise ValueError("「%s」是基础类别，下面不能再挂类别。" % target.name)
        old_parent = cls.parent_id
        old_region = cls.region
        store.move_class(class_id, new_parent_id)
        # 跨工区移动时，整支跟随新父类别的工区，避免树里出现割裂的分支
        new_region = (self.class_map[new_parent_id].region
                      if new_parent_id in self.class_map else old_region)
        moved_regions = []
        if new_region != old_region:
            for cid in self.subtree_class_ids(class_id):
                moved_regions.append((cid, self.class_map[cid].region))
                store.update_class(cid, region=new_region)

        def inverse():
            store.move_class(class_id, old_parent)
            for cid, region in moved_regions:
                store.update_class(cid, region=region)

        self._record_undo("移动类别「%s」" % cls.name, inverse)
        self.reload_classes()

    # -------------------------------------------------------------- 类别顺序
    # ``code`` 是 :meth:`SampleStore.list_classes` 的排序键，树上从上到下的顺序
    # 由它决定；**界面上不显示、也不让人改**——给人看的编号只有导出时按导出单元
    # 从上到下 0 起连续排的那个 label。拖动调整顺序之后按新的深度优先顺序把整个
    # 工区的 code 重编一遍，顺序才记得住。

    def _region_tree_order(self, region: str, children=None,
                           region_of=None) -> List[int]:
        """工区内类别的深度优先顺序，与树上自上而下的阅读顺序一致。"""
        kids = self.children if children is None else children
        regions = region_of or {c.class_id: c.region for c in self.classes}
        out: List[int] = []

        def walk(cid: int) -> None:
            out.append(cid)
            for child in kids.get(cid, []):
                walk(child)

        for cid in kids.get(None, []):
            if regions.get(cid) == region:
                walk(cid)
        return out

    def _codes_for(self, region: str, children=None, region_of=None) -> Dict[int, str]:
        base = config.CLASS_CODE_BASE
        return {cid: str(i + base) for i, cid in
                enumerate(self._region_tree_order(region, children, region_of))}


    def reorder_class(self, class_id: int, new_parent_id: Optional[int],
                      before_id: Optional[int] = None,
                      region: Optional[str] = None) -> None:
        """把类别插到指定位置：挂到 ``new_parent_id`` 下、排在 ``before_id`` 之前。

        ``before_id`` 为 ``None`` 表示排在该父级的末尾；``region`` 只在移到顶层
        时有意义，用于把整支迁到另一个工区。移动完成后受影响工区的类别号整体
        重编，因此编号始终等于树上的显示顺序。
        """
        store = self.require_store()
        cls = self.class_map[class_id]
        if new_parent_id is not None:
            new_parent_id = int(new_parent_id)
            if new_parent_id in set(self.subtree_class_ids(class_id)):
                raise ValueError("不能把类别移动到它自己或它的子类别下面。")
            if new_parent_id not in self.class_map:
                new_parent_id = None
            elif self.class_map[new_parent_id].is_leaf:
                raise ValueError("「%s」是基础类别，下面不能再挂类别；"
                                 "要合并请用 promote_to_group。"
                                 % self.class_map[new_parent_id].name)

        old_parent = cls.parent_id if cls.parent_id in self.class_map else None
        old_region = cls.region
        if new_parent_id is not None:
            new_region = self.class_map[new_parent_id].region
        elif region is not None:
            new_region = region
        else:
            new_region = old_region

        # 先在内存里排好新的兄弟顺序，再据此一次算出全工区的类别号
        children = {k: list(v) for k, v in self.children.items()}
        for kids in children.values():
            if class_id in kids:
                kids.remove(class_id)
        siblings = children.setdefault(new_parent_id, [])
        if before_id is not None and before_id in siblings:
            siblings.insert(siblings.index(before_id), class_id)
        else:
            siblings.append(class_id)

        region_of = {c.class_id: c.region for c in self.classes}
        moved_regions: List[Tuple[int, str]] = []
        if new_region != old_region:
            for cid in self.subtree_class_ids(class_id):
                moved_regions.append((cid, region_of[cid]))
                region_of[cid] = new_region

        codes: Dict[int, str] = {}
        for region in dict.fromkeys([old_region, new_region]):
            codes.update(self._codes_for(region, children, region_of))
        old_codes = {c.class_id: c.code for c in self.classes}

        if old_parent != new_parent_id:
            store.move_class(class_id, new_parent_id)
        for cid, _ in moved_regions:
            store.update_class(cid, region=new_region)
        store.set_class_codes(codes)

        def inverse():
            store.move_class(class_id, old_parent)
            for cid, region in moved_regions:
                store.update_class(cid, region=region)
            store.set_class_codes({cid: old_codes.get(cid, "") for cid in codes})

        self._record_undo("调整类别顺序「%s」" % cls.name, inverse)
        self.reload_classes()

    # -------------------------------------------------------------- 复制粘贴
    def _top_of(self, class_ids: Iterable[int]) -> List[int]:
        """去掉祖先也在集合里的那些：复制一个分组连它的子类别一起选中时，
        子类别跟着分组走一遍就够了，不能再单独复制一份。"""
        ids = [int(c) for c in class_ids if int(c) in self.class_map]
        pool = set(ids)
        out = []
        for cid in ids:
            pid, guard = self.class_map[cid].parent_id, set()
            nested = False
            while pid is not None and pid in self.class_map and pid not in guard:
                guard.add(pid)
                if pid in pool:
                    nested = True
                    break
                pid = self.class_map[pid].parent_id
            if not nested:
                out.append(cid)
        return out

    def copy_classes(self, class_ids: Iterable[int],
                     parent_id: Optional[int] = None,
                     region: Optional[str] = None) -> List[int]:
        """把若干类别（分组连同整支）复制到 ``parent_id`` 下 / ``region`` 顶层。

        复制出来的是**新类别 + 新样本**，与原来的互不相干；同名的按导入的规矩
        起名（「泥岩」已有就叫「泥岩1」）。一步可撤销。返回新建的顶层类别 id。
        """
        store = self.require_store()
        roots = self._top_of(class_ids)
        if not roots:
            return []
        if parent_id is not None:
            parent_id = int(parent_id)
            target = self.class_map.get(parent_id)
            if target is None:
                raise ValueError("目标分组不存在。")
            if target.is_leaf:
                raise ValueError("「%s」是基础类别，下面不能再挂类别。" % target.name)
            region = target.region
        elif region is None:
            region = self.class_map[roots[0]].region
        for cid in roots:
            if parent_id is not None and parent_id in set(self.subtree_class_ids(cid)):
                raise ValueError("不能把「%s」复制到它自己下面。" % self.class_map[cid].name)

        created: List[int] = []

        def dup(cid: int, new_parent: Optional[int]) -> int:
            cls = self.class_map[cid]
            new_id = store.add_class(
                name=store.unique_class_name(region, cls.name),
                code=cls.code, region=region, sample_type=cls.sample_type,
                geom_kind=cls.geom_kind, color=cls.color,
                description=cls.description, parent_id=new_parent,
                source=cls.source, native_crs=cls.native_crs,
                attrs=dict(cls.attrs) if cls.attrs else None, user_created=True)
            if cls.is_leaf:
                store.copy_samples(cid, new_id)
            for child in self.child_class_ids(cid):
                dup(child, new_id)
            return new_id

        with store.transaction():
            for cid in roots:
                created.append(dup(cid, parent_id))

        # 粘出来的排在目标位置的末尾（工区顶层或分组之内），整个工区的排序键按
        # 新顺序重编——与拖动调顺序走的是同一条路。
        old_codes = {c.class_id: c.code for c in self.classes}
        self.reload_classes()
        children = {k: list(v) for k, v in self.children.items()}
        siblings = children.setdefault(parent_id, [])
        for cid in created:
            if cid in siblings:
                siblings.remove(cid)
        siblings.extend(created)
        region_of = {c.class_id: c.region for c in self.classes}
        codes = self._codes_for(region, children, region_of)
        store.set_class_codes(codes)

        def inverse():
            for cid in created:
                store.delete_class(cid)
            store.set_class_codes({cid: old_codes.get(cid, "") for cid in codes
                                   if cid in old_codes})

        names = "、".join(self.class_map[c].name for c in roots[:3])
        self._record_undo("复制 %d 个类别（%s%s）"
                          % (len(roots), names, "…" if len(roots) > 3 else ""),
                          inverse)
        self.reload()
        return created

    def copy_samples_into(self, class_ids: Iterable[int], target_id: int) -> int:
        """把若干类别（含子孙）的全部样本复制一份进目标基础类别，返回条数。

        目标是基础类别、只装一种几何：来源里几何种类对不上的那些不能进，直接
        拒绝而不是悄悄丢一半。一步可撤销。
        """
        store = self.require_store()
        target = self.class_map.get(int(target_id))
        if target is None or target.is_group:
            raise ValueError("目标必须是基础类别。")
        sources: List[int] = []
        for cid in self._top_of(class_ids):
            sources.extend(c for c in self.subtree_class_ids(cid)
                           if self.class_map[c].is_leaf and c != target.class_id)
        if not sources:
            return 0
        kinds = {self.class_map[c].geom_kind for c in sources
                 if self.class_map[c].geom_kind}
        if target.geom_kind and kinds - {target.geom_kind}:
            raise ValueError("「%s」是%s类别，来源里有%s样本，不能混进去。"
                             % (target.name, target.kind_label,
                                "、".join(self.class_map[c].kind_label
                                          for c in sources
                                          if self.class_map[c].geom_kind != target.geom_kind)))
        keys: List[SampleKey] = []
        with store.transaction():
            for cid in sources:
                keys.extend(store.copy_samples(cid, target.class_id))
        self._record_undo("把 %d 条样本复制进「%s」" % (len(keys), target.name),
                          lambda: store.delete_samples(keys))
        self.reload()
        return len(keys)

    def promote_to_group(self, leaf_id: int, other_id: int, name: str) -> int:
        """把一个类别拖到基础类别上：两者升一级，合进一个新分组。

        基础类别是树的末端，不能再有子类别；拖进来的诉求是"这两个应当归为一
        类"，于是在基础类别原来的位置建一个分组，把它和拖进来的那支都挂到分组
        下面。分组沿用基础类别的工区、上级与颜色；名字由调用方给（默认就是基
        础类别的名字）。整个过程一个事务、一个撤销条目。返回新分组的 id。
        """
        store = self.require_store()
        leaf_id, other_id = int(leaf_id), int(other_id)
        leaf = self.class_map[leaf_id]
        other = self.class_map[other_id]
        if not leaf.is_leaf:
            raise ValueError("「%s」是分组，直接拖进去就行，不用升级。" % leaf.name)
        if leaf_id == other_id or leaf_id in self.subtree_class_ids(other_id):
            raise ValueError("不能把一支拖到它自己的子类别上。")
        parent_id = leaf.parent_id if leaf.parent_id in self.class_map else None
        other_parent = other.parent_id if other.parent_id in self.class_map else None
        other_region = other.region
        moved_regions: List[Tuple[int, str]] = []
        old_codes = {c.class_id: c.code for c in self.classes}

        with store.transaction():
            group_id = store.add_class(name=name, region=leaf.region,
                                       geom_kind=SampleClass.GROUP,
                                       parent_id=parent_id, color=leaf.color,
                                       description="由「%s」升级而来的分组" % leaf.name,
                                       user_created=True)
            store.move_class(leaf_id, group_id)
            store.move_class(other_id, group_id)
            if other_region != leaf.region:
                for cid in self.subtree_class_ids(other_id):
                    moved_regions.append((cid, self.class_map[cid].region))
                    store.update_class(cid, region=leaf.region)
            # 分组顶替基础类别原来的位置，两个孩子按"原来的在前、拖进来的在后"
            children = {k: list(v) for k, v in self.children.items()}
            for kids in children.values():
                for cid in (leaf_id, other_id):
                    if cid in kids:
                        kids.remove(cid)
            siblings = children.setdefault(parent_id, [])
            slot = old_position = None
            for i, cid in enumerate(self.children.get(parent_id, [])):
                if cid == leaf_id:
                    old_position = i
            siblings.insert(old_position if old_position is not None else len(siblings),
                            group_id)
            children[group_id] = [leaf_id, other_id]
            region_of = {c.class_id: c.region for c in self.classes}
            region_of[group_id] = leaf.region
            for cid, _r in moved_regions:
                region_of[cid] = leaf.region
            codes: Dict[int, str] = {}
            for region in dict.fromkeys([leaf.region, other_region]):
                codes.update(self._codes_for(region, children, region_of))
            store.set_class_codes(codes)

        def inverse():
            with store.transaction():
                store.move_class(leaf_id, parent_id)
                store.move_class(other_id, other_parent)
                for cid, region in moved_regions:
                    store.update_class(cid, region=region)
                store.delete_class(group_id)
                store.set_class_codes({cid: old_codes.get(cid, "")
                                       for cid in codes if cid != group_id})

        self._record_undo("把「%s」与「%s」合进分组「%s」" % (leaf.name, other.name, name),
                          inverse)
        self.reload_classes()
        return group_id

    def add_group(self, name: str, region: str = "",
                  parent_id: Optional[int] = None) -> int:
        """新建一个分组类别（自身不持有样本，用于自定义分级）。"""
        store = self.require_store()
        parent = self.class_map.get(parent_id) if parent_id is not None else None
        if parent is not None and parent.is_leaf:
            raise ValueError("「%s」是基础类别，下面不能再建分组。" % parent.name)
        class_id = store.add_class(name=name, region=region, geom_kind="group",
                                   parent_id=parent_id,
                                   description="使用者新建的分组",
                                   user_created=True)

        def inverse():
            # 撤销顺序（后进先出）保证此时分组多半是空的，但仍然先把可能残留的
            # 子类别托管给上级再删除——绝不让一次撤销连带删掉真实样本。分组自己
            # 若被画进了样本（不该发生，但库文件可能被别的程序改过），同样拒绝。
            if store.has_samples(class_id):
                raise ValueError("分组「%s」下已经有样本，不能撤销新建。" % name)
            with store.transaction():
                for child in store.child_class_ids(class_id):
                    store.move_class(child, parent_id)
                store.delete_class(class_id)

        self._record_undo("新建分组「%s」" % name, inverse)
        self.reload()
        return class_id

    def rename_sample(self, key: SampleKey, label: str) -> None:
        store = self.require_store()
        rec = self.record_by_key.get(key)
        old = rec.label if rec else ""
        store.set_sample_label(key, label)
        self._record_undo("重命名样本 #%d" % key[1],
                          lambda: store.set_sample_label(key, old))
        # 只动了一条样本的名字，不必把全库几何重读一遍
        self.refresh_record(key)

    def refresh_record(self, key: SampleKey) -> Optional[SampleRecord]:
        """把一条样本从库里重读一遍，替换内存里的那份；库里已没有则移除。"""
        fresh = self.require_store().read_record(key)
        self.version += 1
        old = self.record_by_key.get(key)
        if old is not None:
            self.records = [r for r in self.records if r.key != key]
            bucket = self.records_by_class.get(old.class_id, [])
            bucket[:] = [r for r in bucket if r.key != key]
            self.record_by_key.pop(key, None)
        if fresh is None:
            self.selection.discard(key)
            return None
        self.records.append(fresh)
        self.records_by_class.setdefault(fresh.class_id, []).append(fresh)
        self.record_by_key[key] = fresh
        return fresh

    def rename_region(self, old_region: str, new_region: str) -> int:
        store = self.require_store()
        count = store.rename_region(old_region, new_region)
        self._record_undo("重命名工区「%s」" % old_region,
                          lambda: store.rename_region(new_region, old_region))
        self.reload_classes()
        return count

    def restore_class(self, class_id: int) -> bool:
        """把类别恢复到导入时的名称与层级。"""
        store = self.require_store()
        cls = self.class_map[class_id]
        before = (cls.name, cls.code, cls.region, cls.parent_id)
        ok = store.restore_class(class_id)
        if ok:
            def inverse():
                store.update_class(class_id, name=before[0], code=before[1],
                                   region=before[2])
                store.move_class(class_id, before[3])
            self._record_undo("恢复类别「%s」" % cls.name, inverse)
            self.reload()
        return ok

    def restore_all(self) -> Dict[str, int]:
        """整库恢复到导入时的分类状态。

        这一步会清空撤销栈——整库恢复之后逐步撤销已无意义——因此确实不可逆。
        但备份与否交给使用者决定：界面在执行前会提醒并给出备份办法，这里不再
        自作主张地存快照。
        """
        store = self.require_store()
        stats = store.restore_all()
        self._undo = []
        self.reload()
        return stats

    def edited_classes(self) -> List[SampleClass]:
        """列出相对导入状态被改动过的类别。"""
        return [c for c in self.classes if c.edited]

    # ------------------------------------------------------------------ 备份
    def backup(self, tag: str = "") -> str:
        return self.require_store().backup(tag=tag)

    def list_backups(self) -> List[str]:
        return self.require_store().list_backups() if self.is_open else []

    def copy_to(self, target: str) -> str:
        """整库复制到指定位置（另存为 / 备份到…），复制完核对。"""
        return self.require_store().copy_to(target)


    def restore_backup(self, backup_path: str) -> None:
        """用某个快照覆盖当前样本库，并重新打开。"""
        import shutil
        store = self.require_store()
        path = store.path
        safety = store.backup(tag="before-rollback")
        store.close()
        self.store = None
        try:
            shutil.copy2(backup_path, path)
        except Exception:
            shutil.copy2(safety, path)
            self.open(path)
            raise
        self.open(path)

    # ------------------------------------------------------------------ 统计
    def stats_text(self) -> str:
        if not self.is_open:
            return "未打开样本库"
        st = self.require_store().stats()
        sel, sel_cls = self.selection_summary()
        return ("库: %s   工区 %d   类别 %d   样本 %d (点 %d / 面 %d)   已选 %d 条 / %d 类"
                % (st["name"], st["regions"], st["classes"], st["samples"],
                   st["points"], st["polygons"], sel, sel_cls))
