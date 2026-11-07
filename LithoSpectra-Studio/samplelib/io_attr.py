# -*- coding: utf-8 -*-
"""按属性区分类别的样本互操作。

适配「一个矢量文件装下全部样本，靠属性字段区分类别」这种组织方式，例如
海德乌拉核工业样本 ``SampleForShen.shp``：750 个面，靠 ``岩性大`` / ``代号`` /
``岩性`` 等字段表达 岩类 -> 地层(岩体)代号 的分级关系。

* ``profile_fields``      统计各字段的非空数、唯一值数与示例，供界面挑选分类字段
* ``import_by_attribute`` 按一个或多个字段分级导入，字段组合的每个唯一取值成为一类
导入时每条样本的完整原始属性都会存进样本库（``attrs`` 字段），
导出时可选择把这些原始字段一并还原，实现无损往返。

导出统一由 ``io_shp.export_classes`` 负责：无论样本以哪种方式导入，
输出都遵循「一个矢量文件代表一类样本」。
"""

import os
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from osgeo import gdal, ogr

from . import config
from .model import GEOM_POINT, GEOM_POLYGON, ImportReport
from .store import (SampleStore, add_samples_by_kind, make_transform,
                    open_vector, srs_identity)

# 分类字段取值为空时归入的类别名
UNCLASSIFIED = "未分类"

# 类别名超过该长度时截断显示，完整内容转入类别说明
MAX_CLASS_NAME = 40


# =============================================================================
# 字段剖析
# =============================================================================

class FieldProfile:
    """单个字段的统计信息。"""

    __slots__ = ("name", "type_name", "total", "non_null", "unique",
                 "samples", "max_len", "top_count")

    def __init__(self, name: str, type_name: str):
        self.name = name
        self.type_name = type_name
        self.total = 0
        self.non_null = 0
        self.unique = 0
        self.samples: List[str] = []
        self.max_len = 0
        self.top_count = 0           # 出现次数最多的取值出现了多少次

    @property
    def fill_ratio(self) -> float:
        return (self.non_null / self.total) if self.total else 0.0

    @property
    def top_ratio(self) -> float:
        """众数占比。接近 1 说明该字段几乎只有一个取值，不适合做分类依据。"""
        return (self.top_count / self.total) if self.total else 0.0

    def is_good_class_field(self) -> bool:
        """判断字段是否像"类别字段"。

        要求：填充率高、唯一值数量适中、且不存在一个取值吃掉绝大多数记录
        （例如全是 0 的"海拔"字段，唯一值虽有 9 个，却不具备区分能力）。
        """
        return (self.fill_ratio >= 0.9 and 2 <= self.unique <= 200
                and self.unique < max(2, self.total * 0.5)
                and self.top_ratio <= 0.85)

    def summary(self) -> str:
        return "%s(%s) 非空 %d/%d，唯一值 %d" % (
            self.name, self.type_name, self.non_null, self.total, self.unique)


def profile_fields(path: str, layer_index: int = 0
                   ) -> Tuple[List[FieldProfile], Dict[str, object]]:
    """统计矢量文件的字段情况。

    返回 ``(字段统计列表, 图层信息字典)``。图层信息包含要素数、几何类型、
    坐标系、空几何数量等，供导入界面显示。
    """
    ds = open_vector(path)
    lyr = ds.GetLayer(layer_index)
    if lyr is None:
        raise ValueError("文件中没有图层：%s" % path)

    defn = lyr.GetLayerDefn()
    profiles = OrderedDict()
    for i in range(defn.GetFieldCount()):
        fd = defn.GetFieldDefn(i)
        profiles[fd.GetName()] = FieldProfile(fd.GetName(), fd.GetTypeName())

    seen: Dict[str, Set[str]] = {name: set() for name in profiles}
    freq: Dict[str, Dict[str, int]] = {name: {} for name in profiles}
    kinds: Dict[str, int] = {}
    null_geom = 0
    total = 0

    lyr.ResetReading()
    for feat in lyr:
        total += 1
        geom = feat.GetGeometryRef()
        if geom is None or geom.IsEmpty():
            null_geom += 1
        else:
            t = ogr.GT_Flatten(geom.GetGeometryType())
            key = (GEOM_POINT if t in (ogr.wkbPoint, ogr.wkbMultiPoint)
                   else GEOM_POLYGON if t in (ogr.wkbPolygon, ogr.wkbMultiPolygon)
                   else "other")
            kinds[key] = kinds.get(key, 0) + 1
        for name, prof in profiles.items():
            prof.total += 1
            value = feat.GetField(name)
            if value is None or value == "":
                continue
            prof.non_null += 1
            text = str(value)
            prof.max_len = max(prof.max_len, len(text))
            if len(seen[name]) < 5000:
                seen[name].add(text)
            if len(freq[name]) < 5000 or text in freq[name]:
                freq[name][text] = freq[name].get(text, 0) + 1
            if len(prof.samples) < 6 and text not in prof.samples:
                prof.samples.append(text)

    for name, prof in profiles.items():
        prof.unique = len(seen[name])
        prof.top_count = max(freq[name].values()) if freq[name] else 0

    srs = lyr.GetSpatialRef()
    info = {
        "path": path,
        "layer": lyr.GetName(),
        "features": total,
        "null_geometry": null_geom,
        "geometry": kinds,
        "crs": srs_identity(srs) or "未定义",
        "crs_name": srs.GetName() if srs is not None else "未定义",
        "extent": lyr.GetExtent() if total else None,
    }
    return list(profiles.values()), info


def _group_key(feat, class_fields: Sequence[str],
               collapse_empty: bool) -> Tuple[Tuple[str, str], ...]:
    """构造一条要素的分组键：``((字段, 取值), ...)``。

    ``collapse_empty`` 为真时，取值为空的层级直接跳过，而不是生成「未分类」
    占位节点——地层字段常常只在部分要素上有值（例如"群"只有 16% 的要素填写），
    折叠后 有群的分支保持四级、没群的分支自然收成三级。
    """
    pairs = []
    for field in class_fields:
        value = _clean(feat.GetField(field))
        if value:
            pairs.append((field, value))
        elif not collapse_empty:
            pairs.append((field, UNCLASSIFIED))
    if not pairs:
        pairs.append((class_fields[-1], UNCLASSIFIED))
    return tuple(pairs)


# 预览结果缓存。界面上每勾一次分级字段就要预览一遍，来回勾选是常态，而每次
# 预览都要把整个矢量文件扫一遍——十几万个图斑的岩性图上，勾一下卡一下。按
# (文件, 修改时间, 字段组合) 记住结果，勾回去就是瞬时的；文件在外部被改过，
# 修改时间一变缓存自然失效。
_PREVIEW_CACHE: "OrderedDict[tuple, Tuple[int, int, List[str]]]" = OrderedDict()
_PREVIEW_CACHE_MAX = 32


def preview_classes(path: str, class_fields: Sequence[str],
                    layer_index: int = 0,
                    collapse_empty_levels: bool = True
                    ) -> Tuple[int, int, List[str]]:
    """预览按给定字段分级会产生多少类别。

    返回 ``(末级类别数, 分组节点数, 前几个类别路径示例)``。
    只读取需要的字段，结果按文件与字段组合缓存，反复勾选不会重复扫全文件。
    """
    if not class_fields:
        return 0, 0, []
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = 0.0
    ckey = (os.path.abspath(path), stamp, layer_index,
            tuple(class_fields), bool(collapse_empty_levels))
    if ckey in _PREVIEW_CACHE:
        _PREVIEW_CACHE.move_to_end(ckey)
        return _PREVIEW_CACHE[ckey]
    ds = open_vector(path)
    lyr = ds.GetLayer(layer_index)
    defn = lyr.GetLayerDefn()
    all_fields = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    ignored = [f for f in all_fields if f not in class_fields]
    try:
        lyr.SetIgnoredFields(ignored)
    except Exception:                       # 驱动不支持时退化为全字段读取
        pass

    combos: "OrderedDict[Tuple[Tuple[str, str], ...], int]" = OrderedDict()
    lyr.ResetReading()
    for feat in lyr:
        geom = feat.GetGeometryRef()
        if geom is None or geom.IsEmpty():
            continue
        key = _group_key(feat, class_fields, collapse_empty_levels)
        combos[key] = combos.get(key, 0) + 1

    groups = set()
    for key in combos:
        for depth in range(len(key) - 1):
            groups.add(key[: depth + 1])
    examples = [" / ".join(v for _, v in k) + "（%d 条）" % n
                for k, n in list(combos.items())[:5]]
    result = (len(combos), len(groups), examples)
    _PREVIEW_CACHE[ckey] = result
    while len(_PREVIEW_CACHE) > _PREVIEW_CACHE_MAX:
        _PREVIEW_CACHE.popitem(last=False)
    return result


def suggest_class_fields(profiles: Sequence[FieldProfile],
                         limit: int = 2) -> List[str]:
    """给出推荐的分级字段：填充率高、唯一值数适中，按唯一值数从少到多。"""
    good = [p for p in profiles if p.is_good_class_field()]
    # 文本字段通常才是分类字段；同等条件下唯一值少的放在上层
    good.sort(key=lambda p: (p.type_name not in ("String", "StringList"),
                             p.unique, -p.fill_ratio))
    return [p.name for p in good[:limit]]


# =============================================================================
# 按属性导入
# =============================================================================

def _clean(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _short(name: str) -> str:
    return name if len(name) <= MAX_CLASS_NAME else name[:MAX_CLASS_NAME] + "…"


def _distinct(rows: Sequence[dict], field: str) -> List[str]:
    """取某字段在一组要素中的全部非空取值（保持出现顺序）。"""
    values = []
    for row in rows:
        value = _clean(row.get(field))
        if value and value not in values:
            values.append(value)
    return values


def _join(values: Sequence[str], limit: int = 6) -> str:
    """拼接取值列表，过长时省略。"""
    shown = [v if len(v) <= 24 else v[:24] + "…" for v in values[:limit]]
    text = "、".join(shown)
    if len(values) > limit:
        text += " 等 %d 项" % len(values)
    return text


def import_by_attribute(store: SampleStore, path: str,
                        class_fields: Sequence[str],
                        region: str = "",
                        name_field: str = "",
                        code_field: str = "",
                        sample_type: str = "",
                        description_fields: Sequence[str] = (),
                        collapse_empty_levels: bool = True,
                        layer_index: int = 0,
                        report: Optional[ImportReport] = None,
                        progress: Optional[Callable[[int, int, str], None]] = None
                        ) -> ImportReport:
    """把单个矢量文件按属性字段拆分成多类样本导入。

    ``class_fields`` 按顺序构成分级：第一个字段是最上层分组，最后一个字段
    对应实际持有样本的叶子类别。例如 ``("岩性大", "代号")`` 会先建立
    「火山岩 / 沉积岩 / …」等父类别，再在其下建立各地层代号的子类别。

    ``code_field`` 为空时叶子类别按顺序自动编号；``name_field`` 为空时用
    最后一级分类字段的取值作为类别名。

    ``collapse_empty_levels`` 默认开启：某要素在中间层字段上没有取值时，
    该层被跳过而不是生成「未分类」节点，因此不同分支的层数可以不同。
    """
    path = os.path.abspath(path)
    region = region or os.path.splitext(os.path.basename(path))[0]
    report = report if report is not None else ImportReport(
        library=store.name, region=region)
    if not class_fields:
        raise ValueError("至少需要指定一个分类字段。")

    ds = open_vector(path)
    lyr = ds.GetLayer(layer_index)
    if lyr is None:
        raise ValueError("文件中没有图层：%s" % path)

    defn = lyr.GetLayerDefn()
    field_names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    missing = [f for f in class_fields if f not in field_names]
    if missing:
        raise ValueError("文件中不存在字段：%s" % "、".join(missing))

    src_srs = lyr.GetSpatialRef()
    if src_srs is None:
        report.warnings.append("缺少坐标系，按 EPSG:%d 处理" % config.STORAGE_EPSG)
    native_crs = srs_identity(src_srs)
    transform = make_transform(src_srs)
    source = os.path.basename(path)

    # ---- 第一遍：按分类字段组合归组 ----
    groups: "OrderedDict[Tuple[str, ...], dict]" = OrderedDict()
    total = lyr.GetFeatureCount()
    skipped_geom = 0
    lyr.ResetReading()
    cancelled = False
    for idx, feat in enumerate(lyr, 1):
        if progress and idx % 100 == 0 and progress(idx, total, source) is False:
            cancelled = True
            break
        geom = feat.GetGeometryRef()
        if geom is None or geom.IsEmpty():
            skipped_geom += 1
            continue
        key = _group_key(feat, class_fields, collapse_empty_levels)
        attrs = {f: feat.GetField(f) for f in field_names}
        bucket = groups.setdefault(key, {"geoms": [], "attrs": [], "rows": []})
        bucket["geoms"].append(geom.Clone())
        bucket["attrs"].append(attrs)
        bucket["rows"].append(attrs)

    if cancelled:
        # 第一遍就被取消：一条都还没写库，直接空手而归，比写进去一半干净。
        report.warnings.append("已取消，未导入任何样本。")
        return report
    if skipped_geom:
        report.warnings.append("跳过 %d 条空几何要素" % skipped_geom)
    if not groups:
        report.warnings.append("没有可导入的要素：%s" % source)
        return report

    # 同一末级取值挂在不同上级分类下，通常意味着源数据的分类字段前后不一致，
    # 导入会如实分别建类，但需要提示，避免使用者以为漏掉了样本。
    if len(class_fields) > 1:
        crossed: Dict[str, List[Tuple[str, int]]] = {}
        for key, bucket in groups.items():
            crossed.setdefault(key[-1][1], []).append(
                (" / ".join(v for _, v in key[:-1]) or "（顶层）", len(bucket["geoms"])))
        for leaf, parents in crossed.items():
            if len(parents) > 1:
                detail = "、".join("%s(%d 条)" % (p, n) for p, n in parents)
                report.warnings.append(
                    "「%s」同时出现在多个上级分类下：%s，已分别建类" % (leaf, detail))

    # ---- 第二遍：建类别树并写入样本 ----
    # 整个第二遍是一个事务：建到一半抛错（磁盘满、几何写不进去）就整批回滚，
    # 不留下一棵只有前半截的类别树——那种半成品下次再导一遍就成了两套重复类别。
    # 这一遍已经在写库，停在中间同样是半截，所以不接受取消，只报进度。
    with store.transaction():
        _write_groups(store, groups, report, region=region, source=source,
                      native_crs=native_crs, transform=transform,
                      field_names=field_names, name_field=name_field,
                      code_field=code_field, sample_type=sample_type,
                      description_fields=description_fields, progress=progress)
    report.files += 1
    return report


def _write_groups(store: SampleStore, groups, report: ImportReport, *,
                  region: str, source: str, native_crs: str, transform,
                  field_names: Sequence[str], name_field: str, code_field: str,
                  sample_type: str, description_fields: Sequence[str],
                  progress=None) -> None:
    """按分组建类别树并写入样本。由 :func:`import_by_attribute` 在事务里调用。"""
    parent_cache: Dict[Tuple[str, ...], int] = {}
    leaf_seq = 0
    total = len(groups)

    # 同一个工区里已经有"同一位置、同一组分类键值对"的**分组**时复用它，而不是
    # 再建一个同名的：类别的身份就是它的 attrs（``{"岩性大": "火山岩"}``）加上它挂在
    # 谁下面，同一份岩性图导两次、或分两个文件导同一套分类，树的骨架不该长两棵。
    # 持有样本的末级类别不复用：已有「砂岩」就另建「砂岩1」（与 io_shp 一个规矩），
    # 两批样本各归各的，谁也不并进谁。
    existing: Dict[Tuple[Optional[int], frozenset], int] = {}
    for cls in store.list_classes(with_counts=False):
        if cls.region == region and cls.attrs and cls.is_group:
            existing.setdefault(
                (cls.parent_id, frozenset(cls.attrs.items())), cls.class_id)
    renamed: List[str] = []

    def find_existing(parent_id: Optional[int], attrs: dict) -> Optional[int]:
        return existing.get((parent_id, frozenset(attrs.items())))

    for done, (key, bucket) in enumerate(groups.items(), 1):
        if progress and done % 20 == 0:
            progress(done, total, "写入类别 %d/%d" % (done, total))

        # 逐级建立父类别（父类别只做分组，不直接持有样本）
        parent_id = None
        for depth in range(len(key) - 1):
            prefix = key[: depth + 1]
            if prefix not in parent_cache:
                field, value = prefix[-1]
                found = find_existing(parent_id, dict(prefix))
                if found is not None:
                    parent_cache[prefix] = found
                else:
                    parent_cache[prefix] = store.add_class(
                        name=_short(value), code="", region=region,
                        sample_type=sample_type, geom_kind="group",
                        description="分组字段 %s = %s" % (field, value),
                        parent_id=parent_id, source=source, native_crs=native_crs,
                        attrs=dict(prefix))
                    report.classes += 1
            parent_id = parent_cache[prefix]

        rows = bucket["rows"]
        desc_parts = []

        # 类名字段在组内可能有多个取值（例如同一地层代号下含安山岩、玄武岩、
        # 流纹岩）。此时用它命名会以偏概全，退回用分类字段的取值命名，
        # 并把全部取值写进类别说明。
        leaf_name = ""
        if name_field and name_field in field_names:
            values = _distinct(rows, name_field)
            if len(values) == 1:
                leaf_name = values[0]
            elif len(values) > 1:
                desc_parts.append("%s(%d 种): %s"
                                  % (name_field, len(values), _join(values)))
        leaf_name = leaf_name or key[-1][1]

        code = ""
        if code_field and code_field in field_names:
            code = next((_clean(r.get(code_field)) for r in rows
                         if _clean(r.get(code_field))), "")
        if not code:
            code = str(leaf_seq)
        leaf_seq += 1

        for f in description_fields:
            if f in field_names and f != name_field:
                values = _distinct(rows, f)
                if values:
                    desc_parts.append("%s: %s" % (f, _join(values)))
        if len(leaf_name) > MAX_CLASS_NAME:
            desc_parts.insert(0, leaf_name)
        short = _short(leaf_name)
        # 同一父级下已有同名的末级类别（多半是同一份文件导第二遍）：另起带序号的名
        final = store.unique_class_name(region, short, parent_id=parent_id,
                                        scope="siblings")
        if final != short:
            renamed.append("「%s」→「%s」" % (short, final))

        used, written = add_samples_by_kind(
            store, bucket["geoms"], bucket["attrs"], transform,
            {"name": final, "code": code, "region": region,
             "sample_type": sample_type, "description": "；".join(desc_parts),
             "parent_id": parent_id, "attrs": dict(key)},
            source=source, native_crs=native_crs, report=report)
        report.samples += written

    if renamed:
        report.warnings.append(
            "工区「%s」里已有同名类别，这次导入的 %d 个另起了带序号的名字：%s%s"
            % (region, len(renamed), "、".join(renamed[:6]),
               "…" if len(renamed) > 6 else ""))
