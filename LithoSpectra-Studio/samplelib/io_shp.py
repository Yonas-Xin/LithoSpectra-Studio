# -*- coding: utf-8 -*-
"""旧格式样本互操作：目录 + "一个矢量文件代表一类样本"。

* 导入：``import_vector_dir`` / ``import_vector_file``
  兼容两种历史命名（``Label_0_unknown_20260404`` 与 ``label_10_鄂拉山花岗闪长岩reproj``），
  可读取目录下的 ``.description.json`` 补全类别名与描述。
* 导出：``export_classes``
  严格遵循"一个矢量文件代表一类样本"，同时写出 ``.description.json``，
  使 导出 -> 导入 可无损往返。
"""

import json
import os
import re
import string
from collections import OrderedDict
from contextlib import contextmanager
from datetime import date
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from osgeo import gdal, ogr, osr

from . import config
from .buffering import buffer_points
from .buffering import describe as describe_buffer
from .model import (GEOM_POINT, GEOM_POLYGON, ExportReport, ImportReport,
                    SampleKey, SampleRecord)
from .store import (SampleStore, add_samples_by_kind, make_transform,
                    open_vector, srs_from_text, srs_identity, storage_srs)

# 文件名中会被忽略的后缀（历史处理流程留下的痕迹）
_STRIP_SUFFIXES = ("reprojected", "reproj", "reprj", "_wgs84", "_utm")

# Label_10_花岗岩 / label10-花岗岩 / 10_花岗岩 都能识别
_LABEL_RE = re.compile(r"^(?:label|class|cls)?[\s_\-]*(\d+)[\s_\-]+(.*)$", re.IGNORECASE)
# 结尾的日期串，如 _20260404
_DATE_RE = re.compile(r"[\s_\-](\d{6,8})$")
# Windows / macOS 下的非法文件名字符
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


# =============================================================================
# 文件名解析
# =============================================================================

def parse_label_filename(stem: str) -> Tuple[str, str, str]:
    """解析类别文件名，返回 (类别号, 类别名, 日期)。

    >>> parse_label_filename("Label_0_unknown_20260404")
    ('0', 'unknown', '20260404')
    >>> parse_label_filename("label_10_鄂拉山花岗闪长岩reproj")
    ('10', '鄂拉山花岗闪长岩', '')
    >>> parse_label_filename("Label_1__第四纪reproj")
    ('1', '第四纪', '')
    """
    name = stem.strip()
    lowered = name.lower()
    for suffix in _STRIP_SUFFIXES:
        if lowered.endswith(suffix):
            name = name[: len(name) - len(suffix)]
            lowered = name.lower()
    name = name.strip(" _-")

    code, label, day = "", name, ""
    m = _LABEL_RE.match(name)
    if m:
        code, label = m.group(1), m.group(2).strip(" _-")
    m = _DATE_RE.search(label)
    if m:
        day = m.group(1)
        label = label[: m.start()].strip(" _-")
    if not label:
        label = ("类别%s" % code) if code else stem
    return code, label, day


def description_path(directory: str) -> str:
    """找出目录里的库描述文件，找不到返回空串。

    新导出写的是 ``description.json``，早期版本写的是带前导点的
    ``.description.json``（点开头在各家文件管理器里都是隐藏的，改掉了）。
    读的时候两个都认，老目录不用手工改名。
    """
    for name in (config.DESCRIPTION_JSON, config.DESCRIPTION_JSON_LEGACY):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            return path
    return ""


def read_description_json(directory: str) -> Dict[str, dict]:
    """读取样本目录的库描述文件（新旧文件名都认）。

    返回 ``{"_library": {...}, "<shp 文件名>": {类别信息}}``；文件不存在时返回空字典。
    """
    path = description_path(directory)
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return {}

    info: Dict[str, dict] = {"_library": {
        "name": data.get("sample_library_name", ""),
        "type": data.get("sample_library_type", ""),
        "description": data.get("description", ""),
        "created": data.get("create_date", ""),
    }}
    for item in data.get("classes", []) or []:
        key = os.path.basename(item.get("shp_file", "") or "")
        if key:
            info[key] = item
    return info


def scan_vector_dir(directory: str) -> List[str]:
    """列出目录下所有可导入的矢量文件（按文件名排序）。

    导出目录里与矢量放在一起的 ``description.json``、``labels.json`` 是说明文件，
    扩展名同为 .json 却不是 GeoJSON。不剔掉的话，把导出目录原样导回来，每次都会
    多出"无法打开"的警告和跳过记录。
    """
    if not os.path.isdir(directory):
        return []
    sidecars = {config.DESCRIPTION_JSON.lower(), config.DESCRIPTION_JSON_LEGACY.lower(),
                config.LABELS_JSON.lower()}
    files = [os.path.join(directory, f) for f in sorted(os.listdir(directory))
             if os.path.splitext(f)[1].lower() in config.IMPORT_VECTOR_EXTS
             and not f.startswith(".") and f.lower() not in sidecars]
    return files


# =============================================================================
# 导入
# =============================================================================

def import_vector_file(store: SampleStore, path: str, region: str = "",
                       sample_type: str = "", class_info: Optional[dict] = None,
                       report: Optional[ImportReport] = None) -> int:
    """把单个矢量文件作为一类样本导入样本库，返回写入的样本数。"""
    report = report if report is not None else ImportReport()
    stem = os.path.splitext(os.path.basename(path))[0]
    code, name, _day = parse_label_filename(stem)

    class_info = class_info or {}
    if class_info.get("label_name"):
        name = str(class_info["label_name"])
    if class_info.get("label_id") is not None and not code:
        code = str(class_info["label_id"])
    description = str(class_info.get("description", "") or "")
    stype = sample_type or str(class_info.get("sample_type", "") or "")

    # SHAPE_ENCODING 是进程全局的配置项，只在打开这个文件的那一下生效，读完
    # 立刻复位：留着它，之后画布加载矢量图层、光谱库读 shp 都会被按这个编码解，
    # 而那些文件未必是这个编码。
    with _shape_encoding(config.SHAPE_ENCODING_READ):
        try:
            ds = open_vector(path)
        except IOError:
            report.skipped.append(os.path.basename(path))
            report.warnings.append("无法打开：%s" % os.path.basename(path))
            return 0
        lyr = ds.GetLayer(0)
        # 属性表的解码发生在读要素时，所以要素也要在这个范围内读完
        return _import_layer(store, lyr, path, code, name, description, stype,
                             region, report)


@contextmanager
def _shape_encoding(encoding: str):
    """临时设置 GDAL 的 SHAPE_ENCODING，退出时还原。留空则什么都不做。"""
    if not encoding:
        yield
        return
    previous = gdal.GetConfigOption("SHAPE_ENCODING")
    gdal.SetConfigOption("SHAPE_ENCODING", encoding)
    try:
        yield
    finally:
        gdal.SetConfigOption("SHAPE_ENCODING", previous)


def _import_layer(store: SampleStore, lyr, path: str, code: str, name: str,
                  description: str, stype: str, region: str,
                  report: ImportReport) -> int:
    if lyr is None or lyr.GetFeatureCount() == 0:
        report.skipped.append(os.path.basename(path))
        report.warnings.append("没有要素，已跳过：%s" % os.path.basename(path))
        return 0

    src_srs = lyr.GetSpatialRef()
    if src_srs is None:
        report.warnings.append("缺少坐标系(.prj)，按 EPSG:%d 处理：%s"
                               % (config.STORAGE_EPSG, os.path.basename(path)))
    native_crs = srs_identity(src_srs)
    transform = make_transform(src_srs)

    defn = lyr.GetLayerDefn()
    field_names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]

    geoms, attrs, kinds = [], [], set()
    lyr.ResetReading()
    for feat in lyr:
        geom = feat.GetGeometryRef()
        if geom is None or geom.IsEmpty():
            continue
        gtype = ogr.GT_Flatten(geom.GetGeometryType())
        if gtype in (ogr.wkbPoint, ogr.wkbMultiPoint):
            kinds.add(GEOM_POINT)
        elif gtype in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
            kinds.add(GEOM_POLYGON)
        else:
            continue
        geoms.append(geom.Clone())
        attrs.append({f: feat.GetField(f) for f in field_names})

    if not geoms:
        report.skipped.append(os.path.basename(path))
        report.warnings.append("没有可用的点/面几何，已跳过：%s" % os.path.basename(path))
        return 0

    # 工区里已有同名类别时不往里并，另起一个带序号的名字：「泥岩」有了叫
    # 「泥岩1」，「泥岩1」也有了叫「泥岩2」。两批样本各归各的，谁也不吞谁。
    final = store.unique_class_name(region, name)
    if final != name:
        report.warnings.append("工区「%s」里已有「%s」，这次导入的命名为「%s」（%s）"
                               % (region, name, final, os.path.basename(path)))
    _ids, written = add_samples_by_kind(
        store, geoms, attrs, transform,
        {"name": final, "code": code, "region": region, "sample_type": stype,
         "description": description},
        source=os.path.basename(path), native_crs=native_crs, report=report)
    report.files += 1
    report.samples += written
    return written


def import_vector_dir(store: SampleStore, directory: str, region: str = "",
                      sample_type: str = "",
                      progress: Optional[Callable[[int, int, str], None]] = None
                      ) -> ImportReport:
    """把一个旧样本目录整体导入样本库（目录名默认作为工区名）。"""
    directory = os.path.abspath(directory)
    report = ImportReport(library=store.name,
                          region=region or os.path.basename(directory.rstrip(os.sep)))

    desc = read_description_json(directory)
    lib_info = desc.get("_library", {})
    if lib_info.get("type") and not sample_type:
        sample_type = lib_info["type"]

    files = scan_vector_dir(directory)
    if not files:
        report.warnings.append("目录中没有可导入的矢量文件：%s" % directory)
        return report

    for idx, path in enumerate(files, 1):
        # 回调返回 False = 使用者按了取消。停在这里，已经导进去的文件保留，
        # 报告里注明还剩几个没导——半途而废也要说清楚停在哪。
        if progress and progress(idx, len(files), os.path.basename(path)) is False:
            report.warnings.append("已取消，%d 个文件未导入。" % (len(files) - idx + 1))
            break
        info = desc.get(os.path.basename(path), {})
        try:
            import_vector_file(store, path, region=report.region,
                               sample_type=sample_type, class_info=info, report=report)
        except Exception as exc:                    # 单个文件失败不影响整批导入
            report.skipped.append(os.path.basename(path))
            report.warnings.append("导入失败 %s：%s" % (os.path.basename(path), exc))
    return report


def import_library_root(store: SampleStore, root: str,
                        progress: Optional[Callable[[int, int, str], None]] = None
                        ) -> List[ImportReport]:
    """把包含多个工区子目录的样本库根目录整体导入，每个子目录作为一个工区。"""
    root = os.path.abspath(root)
    reports = []
    subdirs = [d for d in sorted(os.listdir(root))
               if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")]
    targets = subdirs or [""]
    for name in targets:
        path = os.path.join(root, name) if name else root
        if not scan_vector_dir(path):
            continue
        reports.append(import_vector_dir(store, path, region=name or os.path.basename(root),
                                         progress=progress))
        if any("已取消" in w for w in reports[-1].warnings):
            break
    return reports


# =============================================================================
# 导出
# =============================================================================

def _class_path_name(cls, store, sep: str = "-", by_id=None) -> str:
    """类别的层级路径，如 ``火山岩-T3bb2``；可用于文件命名或写入属性。

    调用方手里已经有 ``{class_id: 类别}`` 时请传 ``by_id``：沿父链每上一层就回
    库里查一次，在层级深、类别多的库上是平方级的重复查询。
    """
    names, guard = [cls.name or "未命名"], set()
    pid = cls.parent_id
    while pid is not None and pid not in guard:
        guard.add(pid)
        parent = by_id.get(pid) if by_id is not None else store.get_class(pid)
        if parent is None:
            break
        names.append(parent.name or "")
        pid = parent.parent_id
    return sep.join(n for n in reversed(names) if n)


# 导出文件名的最大字符数。按属性导入的类别名可能是一整段岩性描述，
# 不截断会撞上 Windows 的路径长度限制。
MAX_FILENAME_CHARS = 60


def _safe_filename(text: str) -> str:
    cleaned = _ILLEGAL_RE.sub("_", (text or "").strip())
    cleaned = cleaned.strip(" .")
    if len(cleaned) > MAX_FILENAME_CHARS:
        cleaned = cleaned[:MAX_FILENAME_CHARS].rstrip(" .、，,-_")
    return cleaned or "class"


#: 命名模板里认得的占位符。多一个不认识的名字，导出前就要说清楚，而不是写到
#: 一半抛一个 KeyError('foo') 出来。
TEMPLATE_FIELDS = ("label", "code", "name", "region", "type", "date",
                   "parent", "path")


def check_name_template(template: str) -> None:
    """命名模板不合法就抛 :class:`ValueError`，消息给使用者看。"""
    try:
        fields = [f for _lit, f, _spec, _conv in string.Formatter().parse(template)
                  if f is not None]
    except ValueError as exc:
        raise ValueError("命名模板写法不对：%s（%s）" % (template, exc)) from exc
    unknown = [f for f in fields if f.split(".")[0].split("[")[0]
               not in TEMPLATE_FIELDS]
    if unknown:
        raise ValueError("命名模板里有不认识的占位符：%s。可用：%s"
                         % ("、".join("{%s}" % f for f in unknown),
                            " ".join("{%s}" % f for f in TEMPLATE_FIELDS)))
    if not fields:
        raise ValueError("命名模板里至少要有一个占位符（如 {label} 或 {name}），"
                         "否则所有类别都会写成同一个文件名。")


def _target_srs(target_crs: str, native_crs: str) -> osr.SpatialReference:
    """解析导出目标坐标系；``native`` 表示还原为该类别导入前的坐标系。

    使用者明确指定的坐标系解析不了就报错，而不是悄悄退回存储坐标系——写着
    EPSG:32646 却导出一份经纬度，下游叠到影像上全在别处，比一句报错难查得多。
    ``native`` 找不到原坐标系记录时退回存储坐标系是合理的：那本来就是"尽量还原"。
    """
    if target_crs == "native":
        srs = srs_from_text(native_crs)
        return srs if srs is not None else storage_srs()
    if not target_crs:
        return storage_srs()
    srs = srs_from_text(target_crs)
    if srs is None:
        raise ValueError("无法识别目标坐标系：%s（请写 EPSG:代号 或 WKT）" % target_crs)
    return srs


def export_classes(store: SampleStore, out_dir: str,
                   class_ids: Optional[Iterable[int]] = None,
                   selection: Optional[Set[SampleKey]] = None,
                   driver_key: str = config.DEFAULT_EXPORT_DRIVER,
                   target_crs: str = "",
                   name_template: str = config.DEFAULT_EXPORT_TEMPLATE,
                   level: str = "leaf",
                   unit_class_ids: Optional[Iterable[int]] = None,
                   restore_attrs: bool = False,
                   write_description: bool = True,
                   buffer_radius: float = 0.0,
                   buffer_class_ids: Optional[Iterable[int]] = None,
                   progress: Optional[Callable[[int, int, str], bool]] = None
                   ) -> ExportReport:
    """按「一个矢量文件代表一类样本」导出，这是本系统唯一的导出结构。

    ``level`` 决定"一类"按哪一层算：

    * ``"leaf"``：每个末级类别一个文件（默认）
    * ``"root"``：等同于 ``1``，每个大类一个文件
    * 整数 N：按第 N 层类别导出；分支层数不足 N 时退回该分支自身的末级类别，
      保证样本不丢
    * ``"custom"``：由 ``unit_class_ids`` 指定导出单元，可混合不同层级

    对没有层级的样本库（一个文件一类的旧格式），前三种取值结果相同。
    无论用哪种口径，每条样本都只会出现在一个输出文件中，导出结束会做覆盖校验。

    ``selection`` 给定时只导出其中的样本；``target_crs`` 支持 ``"EPSG:32646"``、
    ``"native"``（还原导入前坐标系）或留空（使用样本库存储坐标系）。
    ``restore_attrs`` 为真时，把导入时保存的原始属性字段一并写出。

    ``buffer_radius`` 大于 0 时，点要素扩成该半径（米）的圆、重叠的合成一个面，
    以面写出；``buffer_class_ids`` 限定只对这些基础类别的点做（``None`` 为全部）。
    面要素不受影响。见 :mod:`samplelib.buffering`。
    """
    driver_name, ext = config.EXPORT_DRIVERS.get(
        driver_key, config.EXPORT_DRIVERS[config.DEFAULT_EXPORT_DRIVER])
    check_name_template(name_template)
    if target_crs and target_crs != "native":
        _target_srs(target_crs, "")            # 先把坐标系解析一遍，不合法就别开工
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    report = ExportReport(out_dir=out_dir, driver=driver_name)

    all_classes = store.list_classes()
    by_id = {c.class_id: c for c in all_classes}
    allowed = ({int(c) for c in class_ids} if class_ids is not None
               else set(by_id))
    if level == "root":
        level = 1
    targets, notes = export_targets(all_classes, by_id, allowed, level,
                                    unit_class_ids)
    report.warnings.extend(notes)
    if not targets:
        report.warnings.append("没有符合条件的类别，未生成任何文件。")
        return report

    # 覆盖校验的基准：本次应当导出多少条样本。只数键，不读几何。
    if selection is None:
        expected = store.count_records(sorted(allowed))
    else:
        expected = sum(1 for key, _cid in store.iter_keys(sorted(allowed))
                       if key in selection)

    driver = ogr.GetDriverByName(driver_name)
    src_srs = storage_srs()
    used_names: Set[str] = set()
    next_label = 0                     # 输出文件里的标签号，从 0 连续排
    desc_classes = []
    label_units: "OrderedDict[int, dict]" = OrderedDict()   # labels.json：一个标签号一条
    today = date.today().isoformat()
    buffer_radius = float(buffer_radius or 0.0)
    buffer_set = ({int(c) for c in buffer_class_ids}
                  if buffer_class_ids is not None else None)
    merged_away = 0                    # 缓冲合并掉的点数，覆盖校验要扣掉

    def _write_unit(cls, records, gtype, stem, code_int, dst_srs, transform,
                    source_ids) -> None:
        """把一组同类同几何的样本写成一个文件，并登记到描述表里。"""
        unique, n = stem, 1
        while unique.lower() in used_names:
            n += 1
            unique = "%s_%d" % (stem, n)
        used_names.add(unique.lower())

        path = os.path.join(out_dir, unique + ext)
        if os.path.exists(path):
            driver.DeleteDataSource(path)

        ds = driver.CreateDataSource(path)
        if ds is None:
            report.warnings.append("无法创建文件：%s" % os.path.basename(path))
            return

        options = (["ENCODING=%s" % config.SHAPE_ENCODING_WRITE]
                   if driver_name == "ESRI Shapefile" else [])
        lyr = ds.CreateLayer(unique, dst_srs, gtype, options=options)

        # 与旧格式保持一致：整型 class 字段承载类别号，另附类别名等信息。
        #
        # 编号按**本次实际写出的类别**从 0 连续排，不沿用库内的类别号。库内的
        # 号是对整库编的，只导其中几类时必然留出空号（比如只挑了 2、5、9 三
        # 类），下游拿它当标签建 N 类分类器就会多出几个永远没样本的类。类别与
        # 编号的对应写在 description.json 和各文件的 class_code 字段里，重编
        # 不会丢失这层对应关系。一个类别拆成点、面两个文件时两者共用同一个号
        # ——它们本来就是同一类样本。
        lyr.CreateField(ogr.FieldDefn("class", ogr.OFTInteger))
        for fname, width in (("class_code", 32), ("class_name", 128),
                             ("class_path", 254), ("region", 96),
                             ("type", 64), ("source", 96)):
            fd = ogr.FieldDefn(fname, ogr.OFTString)
            fd.SetWidth(width)
            lyr.CreateField(fd)
        lyr.CreateField(ogr.FieldDefn("sample_id", ogr.OFTInteger))

        # 可选：还原导入时保存的原始属性字段
        attr_alias: "OrderedDict[str, str]" = OrderedDict()
        if restore_attrs:
            used_fields = {"class", "class_code", "class_name", "class_path",
                           "region", "type", "source", "sample_id"}
            attr_types: "OrderedDict[str, int]" = OrderedDict()
            for rec in records:
                for key, value in (rec.attrs or {}).items():
                    guess = _ogr_type(value)
                    if key not in attr_types:
                        attr_types[key] = guess
                    elif attr_types[key] != guess:
                        attr_types[key] = ogr.OFTString
            for key, ftype in attr_types.items():
                safe = _safe_field(key, used_fields, driver_name)
                attr_alias[key] = safe
                fd = ogr.FieldDefn(safe, ftype)
                if ftype == ogr.OFTString:
                    fd.SetWidth(254)
                lyr.CreateField(fd)

        class_path = _class_path_name(cls, store, sep=" / ", by_id=by_id)
        defn = lyr.GetLayerDefn()
        # 字段下标只算一次：SetField(名字) 每次都在字段定义里线性找，十万条乘
        # 八个字段就是近百万次查找，占了写文件一半的时间。
        idx = {name: defn.GetFieldIndex(name) for name in
               ("class", "class_code", "class_name", "class_path", "region",
                "type", "source", "sample_id")}
        attr_idx = [(key, defn.GetFieldIndex(safe)) for key, safe in attr_alias.items()]
        fixed = ((idx["class"], code_int), (idx["class_code"], cls.code or ""),
                 (idx["class_name"], cls.name), (idx["class_path"], class_path),
                 (idx["region"], cls.region), (idx["type"], cls.sample_type))
        written = 0
        # GPKG / GeoJSON 这类驱动逐条写是逐条提交，包成一个事务快得多；Shapefile
        # 没有事务，跳过。
        tx = bool(lyr.TestCapability(ogr.OLCTransactions))
        if tx:
            lyr.StartTransaction()
        try:
            for rec in records:
                geom = rec.geom
                if geom is None:
                    continue
                if transform is not None:
                    geom.Transform(transform)
                if gtype == ogr.wkbMultiPolygon and ogr.GT_Flatten(
                        geom.GetGeometryType()) == ogr.wkbPolygon:
                    geom = ogr.ForceToMultiPolygon(geom)
                feat = ogr.Feature(defn)
                for i, value in fixed:
                    feat.SetField(i, value)
                feat.SetField(idx["source"], rec.source)
                feat.SetField(idx["sample_id"], rec.key[1])
                for key, i in attr_idx:
                    value = (rec.attrs or {}).get(key)
                    if value is not None:
                        feat.SetField(i, value)
                feat.SetGeometry(geom)
                lyr.CreateFeature(feat)
                written += 1
            if tx:
                lyr.CommitTransaction()
        except Exception:
            if tx:
                lyr.RollbackTransaction()
            raise
        ds.FlushCache()
        ds = None

        report.files.append(path)
        report.samples += written
        unit = label_units.setdefault(code_int, {
            "label": code_int,
            "name": _class_path_name(cls, store, by_id=by_id),   # 与裁剪器一致：层级名
            "code": cls.code or "",
            "path": class_path, "class_ids": list(source_ids),
            "files": [], "sample_count": 0})
        unit["files"].append(os.path.basename(path))
        unit["sample_count"] += written
        desc_classes.append({
            "label_id": code_int,
            "label_name": cls.name,
            "label_code": cls.code,
            "label_path": class_path,
            "sample_type": cls.sample_type,
            "region": cls.region,
            "shp_file": os.path.basename(path),
            "sample_count": written,
            "geometry_type": "point" if gtype == ogr.wkbPoint else "polygon",
            "crs": srs_identity(dst_srs),
            "merged_classes": [_class_path_name(by_id[i], store, sep=" / ",
                                                by_id=by_id)
                               for i in source_ids
                               if i in by_id and i != cls.class_id],
            "attrs": cls.attrs,
            "description": cls.description,
        })

    cancelled = False
    for done, (cls, source_ids) in enumerate(targets, 1):
        if progress and progress(done, len(targets), cls.name or "") is False:
            cancelled = True
            report.warnings.append("已取消，%d 个类别未导出。" % (len(targets) - done + 1))
            break
        # 带几何、不转坐标：这里要的是 OGR 几何本身，转成坐标再按主键回库取一次
        # 是白绕一圈——那一圈在五万条样本上要多花六秒。
        records = [r for r in store.iter_records(source_ids, geometry=True, coords=False)
                   if selection is None or r.key in selection]
        if not records:
            continue

        dst_srs = _target_srs(target_crs, cls.native_crs or _first_native(
            source_ids, by_id))
        transform = make_transform(src_srs, dst_srs)

        # 标签号先定下来再拼文件名——文件名上的号就是它。用库内的类别号会出两种
        # 错：库内号 19、23、26 的三个类导出后文件名跟着跳号，而文件里的 class
        # 字段是 0、1、2；跨工区导出时两个工区各自从 1 起编，文件名还会重号。
        code_int = next_label
        next_label += 1

        stem = _safe_filename(name_template.format(
            label=code_int, code=cls.code or cls.class_id,
            name=cls.name or "未命名",
            region=cls.region or "", type=cls.sample_type or "", date=today,
            parent=(by_id[cls.parent_id].name
                    if cls.parent_id in by_id else ""),
            path=_class_path_name(cls, store, by_id=by_id)))

        # 一个矢量文件只能装一种几何。类别里点、面都有时**分成两个文件**，
        # 不能只留一种——把点扔掉就是无声的样本丢失，覆盖校验虽然会报数目
        # 对不上，但样本已经不在导出结果里了。两个文件共用同一个标签号。
        points = [r for r in records if r.geom_kind == GEOM_POINT]
        polys = [r for r in records if r.geom_kind == GEOM_POLYGON]
        if buffer_radius > 0 and points:
            todo = [r for r in points
                    if buffer_set is None or r.class_id in buffer_set]
            if todo:
                circles = buffer_points([(r.key, r.geom) for r in todo], buffer_radius)
                by_key = {r.key: r for r in todo}
                for poly, keys in circles:
                    head = by_key[keys[0]]
                    polys.append(SampleRecord(
                        key=head.key, class_id=head.class_id, geom_kind=GEOM_POLYGON,
                        coords=[], bbox=head.bbox, source=head.source,
                        label=head.label,
                        attrs=dict(head.attrs or {}, buffered_from=len(keys)),
                        geom=poly))
                done_keys = {r.key for r in todo}
                points = [r for r in points if r.key not in done_keys]
                merged_away += len(todo) - len(circles)
                report.warnings.append(
                    "类别「%s」：%d 个点按 %g m 缓冲成 %d 个面%s。"
                    % (cls.name, len(todo), buffer_radius, len(circles),
                       "（重叠的已合并）" if len(circles) < len(todo) else ""))
        if points and polys:
            groups = [(points, ogr.wkbPoint, stem + "_point"),
                      (polys, ogr.wkbMultiPolygon, stem + "_polygon")]
            report.warnings.append(
                "类别「%s」同时含点(%d)与面(%d)，已拆成两个文件导出，共用标签号 %d。"
                % (cls.name, len(points), len(polys), code_int))
        elif points:
            groups = [(points, ogr.wkbPoint, stem)]
        else:
            groups = [(polys, ogr.wkbMultiPolygon, stem)]

        for grp_records, gtype, grp_stem in groups:
            _write_unit(cls, grp_records, gtype, grp_stem, code_int,
                        dst_srs, transform, source_ids)
    if cancelled:
        # 半途停下的目录不是一份完整的导出：不清理旧文件，也不改写 description.json
        # ——写一份只列了一半类别的描述，再导入回来就只剩这一半，还以为是全部。
        report.warnings.append("已取消：目录里的 description.json 未更新，"
                               "这次写出的文件与它对不上，重新导出一次即可覆盖。")
    else:
        if report.files:
            report.warnings.extend(_clean_stale_exports(out_dir, report.files))
        if write_description and desc_classes:
            _write_description_json(store, out_dir, desc_classes, driver_name,
                                    ext, level=level,
                                    buffer_radius=buffer_radius,
                                    buffer_note=(describe_buffer(
                                        buffer_radius, buffer_set,
                                        {c.class_id: c.name for c in all_classes})
                                        if buffer_radius > 0 else ""))
            _write_labels_json(store, out_dir, list(label_units.values()))
    if not report.files:
        report.warnings.append("没有符合条件的样本，未生成任何文件。")
    expected -= merged_away
    if cancelled:
        report.checked = "已取消，未做覆盖校验。"
    elif level != "custom" and report.samples != expected:
        report.warnings.append(
            "覆盖校验不一致：应导出 %d 条，实际导出 %d 条，请核对上面的提示。"
            % (expected, report.samples))
    else:
        report.checked = "覆盖校验通过：应导出 %d 条，实际导出 %d 条。" % (
            expected, report.samples)
    return report


def export_targets(all_classes, by_id, allowed, level, unit_class_ids=None):
    """确定导出单元：把每个"持有样本的类别"归到唯一一个输出文件上。

    做法是先找出所有直接持有样本的类别，再给每个类别指派一个"导出单元"，
    而不是先挑单元再收集子树——后者在层数不齐或单元互为祖先时会重复或漏掉样本。

    * ``level="leaf"``：每个持有样本的类别自成一个单元
    * ``level=N``：单元取该类别在第 N 层的祖先；**分支深度不足 N 时退回该类别
      自身**，保证浅分支的样本不会因为"没有第 N 层"而丢失
    * ``level="custom"``：单元取 ``unit_class_ids`` 中最深的那个祖先（或自身），
      路径上一个都没选中的样本不导出，由调用方在报告中提示。取最深而不是最浅：
      单元里父子同在只有一种来历——父类别自己直接持有样本、子类别又各自成
      单元，这时父单元只该装它自己那份；取最浅会把子单元整个吞进父文件

    返回 ``([(单元类别, [取样类别 id, ...]), ...], 兜底说明列表)``。
    """
    def depth_of(cls) -> int:
        d, pid, guard = 1, cls.parent_id, set()
        while pid is not None and pid in by_id and pid not in guard:
            guard.add(pid)
            d += 1
            pid = by_id[pid].parent_id
        return d

    def ancestors(cls):
        """自身 -> 顶层 的链条。"""
        chain, pid, guard = [cls], cls.parent_id, set()
        while pid is not None and pid in by_id and pid not in guard:
            guard.add(pid)
            chain.append(by_id[pid])
            pid = by_id[pid].parent_id
        return chain

    holders = [c for c in all_classes if c.count and c.class_id in allowed]
    units: "OrderedDict[int, List[int]]" = OrderedDict()
    shallow: List[str] = []
    uncovered: List[str] = []

    chosen = {int(i) for i in unit_class_ids} if unit_class_ids else set()

    for holder in holders:
        chain = ancestors(holder)                    # [自身, 父, 祖父, ...]
        if level == "leaf":
            unit = holder
        elif level == "custom":
            picked = [c for c in chain if c.class_id in chosen]
            if not picked:
                uncovered.append("%s（%d 条）" % (holder.name, holder.count))
                continue
            unit = picked[0]                         # 链上最深的那个
        else:
            wanted = int(level)
            own_depth = depth_of(holder)
            if own_depth <= wanted:
                unit = holder
                if own_depth < wanted:
                    shallow.append("%s（第 %d 层）" % (holder.name, own_depth))
            else:
                unit = chain[own_depth - wanted]     # 第 wanted 层的祖先
        units.setdefault(unit.class_id, []).append(holder.class_id)

    targets = [(by_id[cid], ids) for cid, ids in units.items()]
    notes = []
    if shallow:
        notes.append("以下分支层数不足，已按其自身层级单独导出：%s"
                     % "、".join(dict.fromkeys(shallow)))
    if uncovered:
        notes.append("以下类别不在任何已选导出单元之下，未导出：%s"
                     % "、".join(uncovered[:8]))
    return targets, notes


_export_targets = export_targets            # 旧名，clip 模块还在引


def _first_native(source_ids, by_id) -> str:
    """合并导出时，取第一个有原始坐标系记录的子类别。"""
    for cid in source_ids:
        cls = by_id.get(cid)
        if cls is not None and cls.native_crs:
            return cls.native_crs
    return ""


def _ogr_type(value) -> int:
    if isinstance(value, bool):
        return ogr.OFTInteger
    if isinstance(value, int):
        return ogr.OFTInteger64
    if isinstance(value, float):
        return ogr.OFTReal
    return ogr.OFTString


def _safe_field(name: str, used: Set[str], driver_name: str) -> str:
    """生成合法且不重名的字段名（Shapefile 字段名不超过 10 字节）。"""
    safe = str(name)
    if driver_name == "ESRI Shapefile":
        while len(safe.encode("utf-8")) > 10:
            safe = safe[:-1]
        safe = safe or "field"
    base, n = safe, 1
    while safe.lower() in used:
        n += 1
        suffix = str(n)
        if driver_name == "ESRI Shapefile":
            trimmed = base
            while len((trimmed + suffix).encode("utf-8")) > 10:
                trimmed = trimmed[:-1]
            safe = trimmed + suffix
        else:
            safe = "%s_%d" % (base, n)
    used.add(safe.lower())
    return safe


def _clean_stale_exports(out_dir: str, kept: Sequence[str]) -> List[str]:
    """删掉上一次导出到同一目录、这次没有再生成的类别文件。

    导出目录是一个整体：``.description.json`` 列出的文件就是这一批样本的全部。
    往同一个目录重导一次少几类的结果，旧文件不清掉就会留在那里，而描述文件里
    已经没有它们——目录自相矛盾，再导入回来会把过时的类别一并读进去，成了凭空
    多出来的样本。

    只删上一份描述文件登记过的文件（连同 Shapefile 的附属文件），使用者自己放
    在目录里的东西一概不碰。返回给使用者看的说明。
    """
    desc = description_path(out_dir)
    if not desc:
        return []
    try:
        with open(desc, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return []
    listed = [c.get("shp_file") for c in payload.get("classes") or []]
    keep = {os.path.basename(p).lower() for p in kept}
    removed = []
    for name in listed:
        if not name or os.path.basename(name).lower() in keep:
            continue
        stem, sub = os.path.splitext(os.path.basename(name))
        for ext_i in (sub, ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn",
                      ".sbx", ".shp.xml"):
            victim = os.path.join(out_dir, stem + ext_i)
            if os.path.isfile(victim):
                try:
                    os.remove(victim)
                except OSError:
                    pass
        removed.append(os.path.basename(name))
    if not removed:
        return []
    return ["已清理上一次导出遗留的 %d 个文件：%s"
            % (len(removed), "、".join(removed[:8])
               + ("…" if len(removed) > 8 else ""))]


def _write_labels_json(store: SampleStore, out_dir: str,
                       labels: Sequence[dict]) -> str:
    """标签对照表，与裁剪样本块写的 labels.json 同一格式。

    description.json 是给再次导入用的库描述；这一份是给训练链路用的：
    ``split_and_clip_dataset`` 按它给样本块编号，训练把它带进模型，预测写进结果
    影像，栅格转矢量再变成属性表。两份文件里的编号完全一致。
    """
    doc = {
        "source": "export",
        "exported_from": os.path.basename(store.path),
        "create_date": date.today().isoformat(),
        "complete": True,
        "labels": list(labels),
    }
    path = os.path.join(out_dir, config.LABELS_JSON)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    return path


def _write_description_json(store: SampleStore, out_dir: str,
                            classes: Sequence[dict], driver_name: str, ext: str,
                            level: str = "leaf", buffer_radius: float = 0.0,
                            buffer_note: str = "") -> None:
    """写出与旧格式兼容的库描述文件，保证导出结果可被再次导入。"""
    meta = store.meta()
    payload = {
        "buffer_radius_m": float(buffer_radius or 0.0),
        "buffer_note": buffer_note,
        "sample_library_name": meta.get("library_name", store.name),
        "sample_library_type": next((c.get("sample_type") for c in classes
                                     if c.get("sample_type")), ""),
        "description": meta.get("description", ""),
        "create_date": date.today().isoformat(),
        "exported_from": os.path.basename(store.path),
        "coordinate_file": "prj" if driver_name == "ESRI Shapefile" else "embedded",
        "sample_storage_rule": {
            "format": driver_name,
            "one_file_per_class": True,
            "class_level": ("大类（合并子类）" if level == "root"
                            else "末级类别" if level == "leaf" else "第 %s 层" % level),
            "naming_rule": "Label_{label_id}_{label_name}" + ext,
            "label_id_base": 0,
            "required_files": [ext] + ([".shx", ".dbf", ".prj", ".cpg"]
                                       if driver_name == "ESRI Shapefile" else []),
        },
        "classes": list(classes),
    }
    path = os.path.join(out_dir, config.DESCRIPTION_JSON)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    # 同一个目录里不能同时留着新旧两份描述文件——再导入时读到哪一份全看运气
    legacy = os.path.join(out_dir, config.DESCRIPTION_JSON_LEGACY)
    if os.path.isfile(legacy):
        try:
            os.remove(legacy)
        except OSError:
            pass
