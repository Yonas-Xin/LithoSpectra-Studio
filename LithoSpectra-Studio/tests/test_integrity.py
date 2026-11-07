# -*- coding: utf-8 -*-
"""数据完整性回归：合并 / 撤销 / 过滤器泄漏 / 缓冲区同步 / 导出校验 / 模块契约。

每一条都对应一个曾经真实存在的缺陷，缺陷描述写在各节开头。直接运行本文件。
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osgeo import gdal, ogr

from samplelib import config, io_attr, io_shp
from samplelib import module as M
from samplelib.annotate.editing import EditBuffer
from samplelib.session import LibrarySession
from samplelib.store import SampleStore

gdal.UseExceptions()
WORK = tempfile.mkdtemp(prefix="slint_")
FAILED = []


def check(cond, what):
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        FAILED.append(what)


def pt(x, y):
    g = ogr.Geometry(ogr.wkbPoint)
    g.AddPoint_2D(float(x), float(y))
    return g


def square(x, y, s=0.01):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for dx, dy in ((0, 0), (s, 0), (s, s), (0, s), (0, 0)):
        ring.AddPoint_2D(x + dx, y + dy)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


def tree_library(name):
    """沉积岩(分组) -> A(2 点) / B(1 点)，另有顶层 T(1 点)。"""
    s = LibrarySession()
    s.create(os.path.join(WORK, name), name="t")
    st = s.store
    parent = st.add_class("沉积岩", region="R", geom_kind="group")
    a = st.add_class("A", code="1", region="R", parent_id=parent, attrs={"岩性": "砂岩"})
    b = st.add_class("B", code="2", region="R", parent_id=parent)
    t = st.add_class("T", code="3", region="R")
    st.add_samples(a, [pt(1, 1), pt(2, 2)])
    st.add_samples(b, [pt(3, 3)])
    st.add_samples(t, [pt(4, 4)])
    s.reload()
    return s, parent, a, b, t


# =============================================================================
print("[1] 合并整支：子类别的样本并进目标，不再被 delete_class 连带删掉")
# 缺陷：merge_classes 只搬父类别自己直接持有的样本，随后 delete_class 递归删
# 子树时把子类别里的样本一起删了；撤销只放回类别，样本永久丢失。
s, parent, a, b, t = tree_library("merge.gpkg")
before = s.store.total_samples()
moved = s.merge_classes([parent], t)
check(moved == 3, "并进目标 3 条（A 的 2 条 + B 的 1 条），实得 %d" % moved)
check(s.store.total_samples() == before, "合并后样本总数不变：%d" % s.store.total_samples())
check([c.name for c in s.classes] == ["T"], "源分支整支删掉：%s" % [c.name for c in s.classes])
check(len(s.keys_of_class(t)) == 4, "目标类别现在持有 4 条")

s.undo()
names = sorted(c.name for c in s.classes)
check(names == ["A", "B", "T", "沉积岩"], "撤销后类别全部回来：%s" % names)
check(len(s.keys_of_class(a)) == 2 and len(s.keys_of_class(b)) == 1
      and len(s.keys_of_class(t)) == 1, "撤销后样本各归各位")
ra = s.class_map[a]
check(ra.parent_id == parent and ra.attrs == {"岩性": "砂岩"}
      and ra.origin.get("name") == "A" and not ra.edited,
      "撤销放回的类别带着层级、attrs 与 origin，不算被改动过")

print("[2] 目标在源的子树里：拒绝，库不动")
s, parent, a, b, t = tree_library("merge2.gpkg")
try:
    s.merge_classes([parent, b], a)
    check(False, "应当拒绝")
except ValueError as exc:
    check("子类别" in str(exc), "拒绝并说明：%s" % exc)
check(s.store.total_samples() == 4 and len(s.classes) == 4, "拒绝之后什么都没变")
# 但把 B 并进 A（同级）是允许的，父类别留下当容器
moved = s.merge_classes([b], a)
check(moved == 1 and len(s.keys_of_class(a)) == 3 and b not in s.class_map,
      "同级合并照常")

# =============================================================================
print("[3] delete_classes 父子同列只删根，且整批一个事务")
s, parent, a, b, t = tree_library("del.gpkg")
n = s.delete_classes([a, parent, b])           # 旧实现：删完 parent 再删 a 会抛错
check(n == 3 and [c.name for c in s.classes] == ["T"], "删掉 3 条样本，只剩 T")

# =============================================================================
print("[4] iter_records 中途 break 不会把过滤器留在图层上")
# 缺陷：生成器里 SetAttributeFilter(None) 在 break 之后走不到，之后的
# GetFeatureCount / GetExtent 都按残留条件算。
s, parent, a, b, t = tree_library("iter.gpkg")
st = s.store
any(True for _ in st.iter_records([t]))         # 找到第一条就停
check(st.total_samples() == 4, "total_samples 仍是 4（旧实现会变成 1）")
check(st.sample_layer("point").GetFeatureCount() == 4, "图层计数不受影响")

# =============================================================================
print("[5] 撤销「新建类别」时类别已经有了样本：拒绝，条目留在栈上")
s, parent, a, b, t = tree_library("undo.gpkg")
cid = s.add_class("新类", region="R")
s.store.add_samples(cid, [pt(9, 9)])            # 相当于标注模式里画了并保存
s.reload()
try:
    s.undo()
    check(False, "应当拒绝")
except ValueError as exc:
    check("不能撤销" in str(exc), "拒绝：%s" % exc)
check(cid in s.class_map and len(s.keys_of_class(cid)) == 1, "类别与样本都还在")
check(s.can_undo and "新类" in s.undo_label, "撤销条目没有被吞掉")

# =============================================================================
print("[6] 标注缓冲区 rebase：浏览模式改了库，草稿保留、悬空引用丢弃")
s, parent, a, b, t = tree_library("rebase.gpkg")
buf = EditBuffer(s.store)
k_draft_a = buf.add_feature(a, "point", [(1.5, 1.5)])      # 草稿要素，归 A
k_draft_b = buf.add_feature(b, "point", [(3.5, 3.5)])      # 草稿要素，归 B
real_t = s.keys_of_class(t)[0]
buf.set_label(real_t, "标了名")                              # 改库内要素
buf.rename_class(b, "B改名")                                 # 改库内类别名
# 浏览模式下：删掉 B（连样本）
s.delete_classes([b])
dropped = buf.rebase()
check(dropped["草稿要素"] == 1 and dropped["类别改动"] == 1,
      "丢弃了指向 B 的草稿要素与改名：%s" % dropped)
recs = {r.key: r for r in buf.records()}
check(k_draft_a in recs and k_draft_b not in recs, "归 A 的草稿还在，归 B 的没了")
check(recs[real_t].label == "标了名", "库内要素的改动保留")
check(not buf.can_undo, "丢过东西之后撤销栈清空")
report = buf.commit()
check(report["written"] == 1, "保存不再撞上已删除的类别：写入 1 条")
s.reload()
check(len(s.keys_of_class(a)) == 3 and s.record_by_key[real_t].label == "标了名",
      "落库结果正确")

print("[6b] 没丢东西时 rebase 不动撤销栈")
buf = EditBuffer(s.store)
buf.add_feature(a, "point", [(1.6, 1.6)])
s.rename_class(a, "A2")
check(buf.rebase() == {"要素改动": 0, "草稿要素": 0, "类别改动": 0} and buf.can_undo,
      "只是改名：什么都不丢，撤销栈还在")
check(buf.class_map()[a].name == "A2", "缓冲区看到的是改名之后的树")

print("[6c] EditBuffer.record 走索引")
check(buf.record(real_t) is not None and buf.record(("point", 99999)) is None,
      "按键取记录正确")

# =============================================================================
print("[7] 导出：非法坐标系与模板在开工前报错，取消不改写 description.json")
s, parent, a, b, t = tree_library("export.gpkg")
s.select_all()
out = os.path.join(WORK, "out")
for bad_crs in ("EPSG:999999", "不是坐标系"):
    try:
        s.export(out, target_crs=bad_crs)
        check(False, "应当拒绝 %s" % bad_crs)
    except ValueError as exc:
        check("坐标系" in str(exc), "拒绝坐标系 %s：%s" % (bad_crs, exc))
check(not os.path.isdir(out) or not os.listdir(out), "拒绝时没有写任何文件")
for bad_tpl, why in (("Label_{foo}", "不认识的占位符"), ("固定名", "没有占位符"),
                     ("{label", "写法不对")):
    try:
        s.export(out, name_template=bad_tpl)
        check(False, "应当拒绝模板 %s" % bad_tpl)
    except ValueError as exc:
        check(True, "拒绝模板 %r（%s）：%s" % (bad_tpl, why, str(exc)[:40]))

rep = s.export(out, level="leaf")
check(len(rep.files) == 3 and "通过" in rep.checked, "正常导出 3 个文件")
desc_before = open(os.path.join(out, config.DESCRIPTION_JSON), encoding="utf-8").read()
stop = [0]


def cancel_after_first(done, total, name):
    stop[0] += 1
    return stop[0] < 2


rep = s.export(out, level="leaf", progress=cancel_after_first)
desc_after = open(os.path.join(out, config.DESCRIPTION_JSON), encoding="utf-8").read()
check(desc_after == desc_before and any("取消" in w for w in rep.warnings),
      "取消之后描述文件原样保留，并在报告里说明")

# =============================================================================
print("[8] 按属性导入：第二遍在一个事务里，失败整批回滚")
src = os.path.join(WORK, "attr.geojson")
drv = ogr.GetDriverByName("GeoJSON")
ds = drv.CreateDataSource(src)
lyr = ds.CreateLayer("s", geom_type=ogr.wkbPolygon)
lyr.CreateField(ogr.FieldDefn("岩性大", ogr.OFTString))
lyr.CreateField(ogr.FieldDefn("代号", ogr.OFTString))
for i, (big, code) in enumerate((("火山岩", "T1"), ("火山岩", "T2"), ("沉积岩", "P1"))):
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetField("岩性大", big)
    f.SetField("代号", code)
    f.SetGeometry(square(100 + i * 0.1, 40))
    lyr.CreateFeature(f)
ds = None
s = LibrarySession()
s.create(os.path.join(WORK, "attr.gpkg"), name="t")
rep = s.import_by_attribute(src, class_fields=("岩性大", "代号"), region="R")
check(rep.classes == 5 and rep.samples == 3, "正常导入 5 类 3 条")

orig = SampleStore.add_samples
calls = [0]


def boom(self, *a, **kw):
    calls[0] += 1
    if calls[0] == 2:
        raise RuntimeError("模拟写盘失败")
    return orig(self, *a, **kw)


SampleStore.add_samples = boom
before_cls, before_n = len(s.classes), s.store.total_samples()
try:
    s.import_by_attribute(src, class_fields=("岩性大", "代号"), region="R2")
    check(False, "应当抛错")
except RuntimeError:
    pass
finally:
    SampleStore.add_samples = orig
s.reload()
check(len(s.classes) == before_cls and s.store.total_samples() == before_n,
      "失败后类别数与样本数都没变（旧实现会留下半棵树）")

# =============================================================================
print("[9] 格式版本：比程序新的主版本拒绝打开，老库自动补版本号")
path = os.path.join(WORK, "ver.gpkg")
st = SampleStore.create(path, name="v")
st.set_meta("format_version", "9.0")
st.close()
try:
    SampleStore.open(path)
    check(False, "应当拒绝")
except ValueError as exc:
    check("更新" in str(exc), "拒绝：%s" % str(exc)[:50])
try:
    SampleStore.open(path, update=False)
    check(False, "只读打开也应当拒绝")
except ValueError:
    check(True, "只读打开同样拒绝——按老格式去读新库，读出来的也不可信")
ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_UPDATE)
ds.ExecuteSQL("DELETE FROM sl_meta WHERE key = 'format_version'")
ds = None
st = SampleStore.open(path)
check(st.file_version() == config.LIBRARY_FORMAT_VERSION,
      "没写版本的老库打开后补成 %s" % st.file_version())
st.close()

# =============================================================================
print("[10] 模块清单：坏清单不拖垮发现")
root = os.path.join(os.path.dirname(M.__file__), M.MODULES_DIR)
bad = os.path.join(root, "zz_broken")
os.makedirs(bad, exist_ok=True)
with open(os.path.join(bad, M.MANIFEST_NAME), "w", encoding="utf-8") as fh:
    fh.write("{ not json")
try:
    broken = []
    found = M.discover(broken=broken)
    check("zz_broken" not in found and [i for i, _ in broken] == ["zz_broken"],
          "坏清单被跳过并记录：%s" % broken)
finally:
    shutil.rmtree(bad)

# =============================================================================
print("[11] 按属性重复导入：分组复用不长第二棵树，末级类别另起带序号的名")
s = LibrarySession()
s.create(os.path.join(WORK, "attr2.gpkg"), name="t")
rep1 = s.import_by_attribute(src, class_fields=("岩性大", "代号"), region="R")
n_groups = len([c for c in s.classes if c.is_group])
n_leaves = len([c for c in s.classes if c.is_leaf])
rep2 = s.import_by_attribute(src, class_fields=("岩性大", "代号"), region="R")
check(rep2.classes == n_leaves and rep2.samples == 3 and any("带序号" in w for w in rep2.warnings),
      "第二次只新建末级类别（%d 个），分组复用：%s" % (n_leaves, rep2.warnings[:1]))
check(len([c for c in s.classes if c.is_group]) == n_groups
      and len([c for c in s.classes if c.is_leaf]) == 2 * n_leaves
      and s.store.total_samples() == 6, "分组 %d 个不变、末级翻倍、6 条样本" % n_groups)
leaf_names = sorted(c.name for c in s.classes if c.is_leaf)
check(any(n.endswith("1") for n in leaf_names), "新末级类别带序号 1：%s" % leaf_names)
rep3 = s.import_by_attribute(src, class_fields=("岩性大", "代号"), region="R3")
check(rep3.classes == 5, "换个工区照建：不同工区的同名类别是两回事")

print("[12] 导入旧样本：SHAPE_ENCODING 用完复位，二进制属性不会让整个文件失败")
before_enc = gdal.GetConfigOption("SHAPE_ENCODING")
config.SHAPE_ENCODING_READ = "GBK"
try:
    shp = os.path.join(WORK, "Label_3_二进制.gpkg")     # Shapefile 存不了二进制字段
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(shp)
    lyr = ds.CreateLayer("l", geom_type=ogr.wkbPoint)
    lyr.CreateField(ogr.FieldDefn("blob", ogr.OFTBinary))
    lyr.CreateField(ogr.FieldDefn("day", ogr.OFTDate))
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetFieldBinaryFromHexString("blob", "DEADBEEF")
    f.SetField("day", "2026-09-15")
    f.SetGeometry(pt(100, 40))
    lyr.CreateFeature(f)
    ds = None
    s = LibrarySession()
    s.create(os.path.join(WORK, "bin.gpkg"), name="t")
    rep = s.import_file(shp, region="R")
    check(rep.samples == 1 and not rep.skipped, "带二进制字段的文件导入成功：%s" % rep.warnings)
    rec = s.records[0]
    # 这一版 GDAL 的 GetField 对二进制字段直接给十六进制文本，别的版本给 bytes；
    # 两种都得能进 JSON
    check(str(rec.attrs.get("blob")).lower() == "deadbeef", "二进制按十六进制存：%s" % rec.attrs)
    from samplelib.store import _dump_json
    from datetime import date as _date
    check(json.loads(_dump_json({"b": b"\xde\xad", "d": _date(2026, 9, 15), "o": object()}))["b"]
          == "dead", "bytes / 日期 / 任意对象都序列化得出来")
finally:
    config.SHAPE_ENCODING_READ = ""
check(gdal.GetConfigOption("SHAPE_ENCODING") == before_enc,
      "SHAPE_ENCODING 复位为 %r" % before_enc)

print("[13] 坐标读数：西经南纬不写成负的 E/N")
from samplelib.crs import Formatter
check(Formatter(4326).format(-105.5, -33.25) == "105.500000°W  33.250000°S",
      "读数：%s" % Formatter(4326).format(-105.5, -33.25))

print("[14] 裁剪：整个落在影像外的面要素计入 outside")
import numpy as np
from samplelib.modules.clip import clip as C
img = os.path.join(WORK, "img.tif")
d = gdal.GetDriverByName("GTiff").Create(img, 20, 20, 1, gdal.GDT_Byte)
d.SetGeoTransform((100.0, 0.001, 0, 40.0, 0, -0.001))
from osgeo import osr
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
d.SetProjection(srs.ExportToWkt()); d.GetRasterBand(1).WriteArray(np.ones((20, 20), np.uint8)); d = None
s = LibrarySession()
s.create(os.path.join(WORK, "clip.gpkg"), name="t")
c = s.store.add_class("C", region="R")
s.store.add_samples(c, [square(100.002, 39.995, 0.003), square(120, 10, 0.01)])
s.reload()
rep = C.clip_library(s.store, img, os.path.join(WORK, "clipout"), patch_size=3,
                     output_format="tif", train_ratio=1.0)
check(len(rep.units) == 1 and rep.units[0].features == 2 and rep.units[0].outside == 1
      and rep.units[0].train > 0,
      "2 个面、1 个在影像外：features=%d outside=%d train=%d"
      % (rep.units[0].features, rep.units[0].outside, rep.units[0].train))

# =============================================================================
print("[15] 存储层的几条快路径与全量遍历结果一致")
s, parent, a, b, t = tree_library("fast.gpkg")
st = s.store
full = list(st.iter_records())
check(sorted(k for k, _c in st.iter_keys()) == sorted(r.key for r in full)
      and dict(st.iter_keys())[full[0].key] == full[0].class_id,
      "iter_keys 给出同样的主键与归属")
check(st.count_records() == 4 and st.count_records([a, b]) == 3 and st.count_records([]) == 0,
      "count_records 与实际条数一致")
g = list(st.iter_records([a], geometry=True, coords=False))
check(len(g) == 2 and all(r.geom is not None and r.coords == [] for r in g)
      and g[0].geom.GetGeometryName() == "POINT", "geometry=True 带几何、coords=False 不转坐标")
check(all(r.geom is None for r in full), "默认不挂几何，界面层拿到的记录不依赖 GDAL")
check(st.total_samples() == 4 and st.sample_layer("point").GetLayerDefn() is not None,
      "遍历之后忽略字段与过滤器都清掉了")
rec = st.read_record(full[0].key)
check(rec.attrs == full[0].attrs and rec.bbox == full[0].bbox, "read_record 与遍历结果一致")

print("[16] 给样本命名只刷新那一条，不整库重读")
key = s.keys_of_class(t)[0]
s.select_keys([key])
s.rename_sample(key, "新名字")
check(s.record_by_key[key].label == "新名字" and key in s.selection
      and len(s.records) == 4 and len(s.records_by_class[t]) == 1,
      "名字改了、选择集没动、条数不变")
s.undo()
check(s.record_by_key[key].label == "", "撤销后名字清空")

# =============================================================================
# =============================================================================
print("[17] 库内面的环不带闭合点：拖 / 删第一个顶点不再留下幽灵顶点")
from samplelib.annotate.editing import coords_to_geom
s = LibrarySession()
s.create(os.path.join(WORK, "ring.gpkg"), name="t")
c = s.store.add_class("C", region="R")
s.store.add_samples(c, [square(0, 0, 1.0)])
s.reload()
rec = s.records[0]
check(len(rec.coords[0]) == 4 and rec.coords[0][0] != rec.coords[0][-1],
      "读出来的环 4 个顶点、首尾不重复：%s" % rec.coords[0])
rings = [list(r) for r in rec.coords]
rings[0][0] = (0.5, -0.5)                      # 拖动第 0 个顶点
g = coords_to_geom("polygon", rings)
pts = g.GetGeometryRef(0).GetGeometryRef(0).GetPointCount()
check(pts == 5 and g.IsValid(), "拖第 0 个顶点后仍是 4 边形（闭合 5 点）且合法：%d" % pts)
rings = [list(r) for r in rec.coords]
rings[0].pop(0)                                 # 删第 0 个顶点
g = coords_to_geom("polygon", rings)
check(g.GetGeometryRef(0).GetGeometryRef(0).GetPointCount() == 4
      and (0.0, 0.0) not in [tuple(p[:2]) for p in g.GetGeometryRef(0).GetGeometryRef(0).GetPoints()],
      "删第 0 个顶点后那个角真的没了")

print("[18] 裁剪：重裁清理旧块，取消标记为未完成")
img = os.path.join(WORK, "img2.tif")
d = gdal.GetDriverByName("GTiff").Create(img, 30, 30, 1, gdal.GDT_Byte)
d.SetGeoTransform((100.0, 0.001, 0, 40.0, 0, -0.001))
d.SetProjection(srs.ExportToWkt())
d.GetRasterBand(1).WriteArray(np.ones((30, 30), np.uint8))
d = None
s = LibrarySession()
s.create(os.path.join(WORK, "clip2.gpkg"), name="t")
for k in range(3):
    ck = s.store.add_class("K%d" % k, region="R")
    s.store.add_samples(ck, [pt(100.005 + 0.008 * k, 39.995), pt(100.006 + 0.008 * k, 39.990)])
s.reload()
out = os.path.join(WORK, "clipout2")
rep = C.clip_library(s.store, img, out, patch_size=3, output_format="bin", train_ratio=1.0)
first = sorted(os.listdir(os.path.join(out, "train_dataset")))
check(len([f for f in first if f.endswith(".bin")]) == 6, "第一次 6 个块")
rep = C.clip_library(s.store, img, out, patch_size=3, output_format="bin", train_ratio=1.0,
                     class_ids=[s.classes[0].class_id])
second = [f for f in os.listdir(os.path.join(out, "train_dataset")) if f.endswith(".bin")]
check(len(second) == 2 and any("清理" in w for w in rep.warnings),
      "只裁一类后目录里只剩 2 个块，旧的清掉并有提示：%s" % second)
meta = json.load(open(os.path.join(out, "train_dataset", "raw_bip_meta.json"), encoding="utf-8"))
labels = json.load(open(os.path.join(out, "labels.json"), encoding="utf-8"))
check(meta["complete"] is True and labels["complete"] is True, "完整裁剪标记 complete")
rep = C.clip_library(s.store, img, out, patch_size=3, output_format="h5", train_ratio=1.0,
                     progress=lambda done, total, name: done < 1)
labels = json.load(open(os.path.join(out, "labels.json"), encoding="utf-8"))
import h5py
with h5py.File(os.path.join(out, "train_dataset.h5")) as fh:
    h5_complete = bool(fh.attrs["complete"])
check(rep.cancelled and labels["complete"] is False and h5_complete is False
      and any("已取消" in w for w in rep.warnings),
      "取消：labels.json 与 h5 都标 complete=False，并有提示")

# =============================================================================
print("[19] 删除整个工区")
s, parent, a, b, t = tree_library("region.gpkg")
other = s.store.add_class("别处", region="R2")
s.store.add_samples(other, [pt(7, 7)])
s.reload()
s.select_all()
n_cls, n = s.delete_region("R")
check((n_cls, n) == (4, 4), "删掉 R 的 4 个类别（含分组）与 4 条样本：%s" % ((n_cls, n),))
check([c.region for c in s.classes] == ["R2"] and s.store.total_samples() == 1,
      "别的工区不受影响")
check(all(s.record_by_key[k].class_id == other for k in s.selection),
      "选择集里只剩还存在的样本")
check(s.delete_region("没有这个") == (0, 0), "不存在的工区返回 (0, 0)")

# =============================================================================
print("[20] 基础类别与分组：拖到基础类别上升一级，基础类别不能再挂子类别")
s, parent, a, b, t = tree_library("promote.gpkg")
check(s.class_map[parent].is_group and s.class_map[a].is_leaf, "分组 / 基础类别判定")
for bad in (lambda: s.reorder_class(t, a), lambda: s.move_class(t, a),
            lambda: s.add_class("x", region="R", parent_id=a),
            lambda: s.add_group("x", region="R", parent_id=a)):
    try:
        bad()
        check(False, "应当拒绝往基础类别下挂东西")
    except ValueError as exc:
        check("基础类别" in str(exc), "拒绝：%s" % str(exc)[:30])
g = s.promote_to_group(a, t, "A 与 T")
ga = s.class_map[g]
check(ga.is_group and ga.parent_id == parent and ga.region == "R", "新分组挂在 A 原来的上级下")
check(s.class_map[a].parent_id == g and s.class_map[t].parent_id == g, "A 与 T 都在新分组下")
check(s.child_class_ids(parent) == [g, b] or s.child_class_ids(parent) == [b, g],
      "分组顶替了 A 的位置：%s" % s.child_class_ids(parent))
check(s.child_class_ids(g) == [a, t], "分组里原来的在前、拖进来的在后")
check(len(s.keys_of_class(a)) == 2 and len(s.keys_of_class(t)) == 1, "样本一条没动")
s.undo()
check(g not in s.class_map and s.class_map[a].parent_id == parent
      and s.class_map[t].parent_id is None, "撤销后分组消失、两者回到原位")

print("[20b] 老库升级 1.1：有子类别却不是分组、且自己没样本的，改成分组")
path = os.path.join(WORK, "old.gpkg")
st = SampleStore.create(path)
p_ = st.add_class("父", region="R")                        # 老库：父类别是 point
c_ = st.add_class("子", region="R", parent_id=p_)
st.add_samples(c_, [pt(1, 1)])
st.set_meta("format_version", "1.0")
st.close()
st = SampleStore.open(path)
check(st.get_class(p_).is_group and st.get_class(c_).is_leaf
      and st.file_version() == config.LIBRARY_FORMAT_VERSION,
      "升级后父是分组、版本写成 %s" % config.LIBRARY_FORMAT_VERSION)
st.close()

print("[21] 基础类别只装一种几何：第一条定种类，之后另一种被拒绝")
s, parent, a, b, t = tree_library("kind.gpkg")
buf = EditBuffer(s.store)
new_cls = s.add_class("空类", region="R")
buf.rebase()
check(buf.class_kind(new_cls) == "" and buf.can_add(new_cls, "polygon") == "", "空类别什么都能放")
k1 = buf.add_feature(new_cls, "polygon", [[(0, 0), (1, 0), (1, 1)]])
check(buf.class_kind(new_cls) == "polygon", "第一条是面，类别就是面类")
check("面样本类别" in buf.can_add(new_cls, "point"), "再放点被拒绝")
try:
    buf.add_feature(new_cls, "point", [(5, 5)])
    check(False, "应当拒绝")
except ValueError:
    check(True, "add_feature 也拒绝")
check("点样本类别" in buf.can_add(a, "polygon"), "老类别 A 里是点，不能画面")
check("分组" in buf.can_add(parent, "point"), "分组不能画")
ka = s.keys_of_class(a)[0]
check("面样本类别" in buf.can_move([ka], new_cls), "点要素不能改归到面类")
check(buf.can_move([ka], b) == "", "点改归到点类可以")
buf.undo()
check(buf.class_kind(new_cls) == "", "撤销第一条之后种类回到未定")
buf.redo()
buf.commit()
s.reload_classes()
check(s.class_map[new_cls].geom_kind == "polygon", "保存后类别的种类写进库")

print("[22] 点面混合的文件导入成分组 + 两个基础类别")
mixed = os.path.join(WORK, "Label_5_混合.geojson")
ds = ogr.GetDriverByName("GeoJSON").CreateDataSource(mixed)
lyr = ds.CreateLayer("m", geom_type=ogr.wkbUnknown)
for g_ in (pt(100.1, 40.1), pt(100.2, 40.1), square(100.3, 40.1)):
    f = ogr.Feature(lyr.GetLayerDefn()); f.SetGeometry(g_); lyr.CreateFeature(f)
ds = None
s = LibrarySession()
s.create(os.path.join(WORK, "mixed.gpkg"), name="t")
rep = s.import_file(mixed, region="R")
names = sorted(c.name for c in s.classes)
check(names == ["混合", "混合（点）", "混合（面）"], "三个类别：%s" % names)
grp = next(c for c in s.classes if c.name == "混合")
leaves = [c for c in s.classes if c.parent_id == grp.class_id]
check(grp.is_group and grp.code == "5" and {c.geom_kind for c in leaves} == {"point", "polygon"},
      "分组带类别号，两个基础类别各一种几何")
check(rep.samples == 3 and any("拆成" in w for w in rep.warnings), "3 条样本，报告里说明拆分")
rep2 = s.import_file(mixed, region="R")
names = sorted(c.name for c in s.classes)
check(rep2.classes == 3 and names == ["混合", "混合1", "混合1（点）", "混合1（面）", "混合（点）", "混合（面）"]
      and s.store.total_samples() == 6,
      "再导一次：另起「混合1」分组 + 两个基础类别，原来的一条不动：%s" % names)

print("[23] 点要素缓冲：导出成面并合并重叠，裁剪按圆取像元，只对指定类别")
from samplelib.buffering import buffer_points
s = LibrarySession()
s.create(os.path.join(WORK, "buf.gpkg"), name="t")
near = s.store.add_class("近点", region="R")
far = s.store.add_class("独点", region="R")
s.store.add_samples(near, [pt(100.0, 38.0), pt(100.00045, 38.0)])     # 相距约 40 m
s.store.add_samples(far, [pt(100.02, 38.0)])
s.reload()
s.select_all()
out = os.path.join(WORK, "bufout")
rep = s.export(out, level="leaf", buffer_radius=30)
check(len(rep.files) == 2 and rep.samples == 2 and "通过" in rep.checked,
      "两类各一个文件、两个面（近点合成一个），覆盖校验按合并后的数目：%s" % rep.checked)
dsx = ogr.Open([f for f in rep.files if "近点" in f][0])
lx = dsx.GetLayer(0)
fx = lx.GetNextFeature()
check(ogr.GT_Flatten(lx.GetGeomType()) in (ogr.wkbPolygon, ogr.wkbMultiPolygon)
      and lx.GetFeatureCount() == 1, "近点文件是 1 个面")
env = fx.GetGeometryRef().GetEnvelope()
width_m = (env[1] - env[0]) * 111320 * np.cos(np.radians(38))
check(95 < width_m < 105, "合并后的面宽约 100 m（40 + 2×30）：%.1f" % width_m)
desc = json.load(open(os.path.join(out, config.DESCRIPTION_JSON), encoding="utf-8"))
check(desc["buffer_radius_m"] == 30, "描述文件记下缓冲半径")
dsx = None
rep = s.export(out, level="leaf", buffer_radius=30, buffer_class_ids=[far])
dsx = ogr.Open([f for f in rep.files if "近点" in f][0])
check(ogr.GT_Flatten(dsx.GetLayer(0).GetGeomType()) == ogr.wkbPoint
      and dsx.GetLayer(0).GetFeatureCount() == 2, "只对「独点」缓冲：近点仍按点导出 2 条")
dsx = None

img = os.path.join(WORK, "bufimg.tif")
d = gdal.GetDriverByName("GTiff").Create(img, 400, 400, 1, gdal.GDT_Byte)
d.SetGeoTransform((99.99, 0.0001, 0, 38.01, 0, -0.0001))      # 约 8.8 m × 11 m 像元
d.SetProjection(srs.ExportToWkt())
d.GetRasterBand(1).WriteArray(np.ones((400, 400), np.uint8))
d = None
rep = C.clip_library(s.store, img, os.path.join(WORK, "bufclip"), patch_size=3,
                     output_format="tif", train_ratio=1.0)
plain = {u.name: u.train for u in rep.units}
rep = C.clip_library(s.store, img, os.path.join(WORK, "bufclip2"), patch_size=3,
                     output_format="tif", train_ratio=1.0, buffer_radius=30,
                     buffer_class_ids=[far])
buffered = {u.name: (u.train, u.features) for u in rep.units}
check(plain["近点"] == 2 and plain["独点"] == 1, "不缓冲：一个点一个块")
check(buffered["近点"] == (2, 2) and buffered["独点"][0] > 10 and buffered["独点"][1] == 1,
      "只缓冲「独点」：它按圆裁出十几个块、算一个要素；「近点」照旧：%s" % buffered)
labels = json.load(open(os.path.join(WORK, "bufclip2", "labels.json"), encoding="utf-8"))
check(labels["buffer_radius_m"] == 30, "labels.json 记下缓冲半径")

# =============================================================================
print("[24] 老库升级 1.2：自己持有样本、下面又挂着子类别的节点，拆成分组 + 同名基础类别")
# 1.1 之前的库允许"沉积岩"自己有 3 个点、下面还挂着砂岩 / 泥岩。升级时在它下面
# 建一个同名基础类别「沉积岩」装那 3 个点，它自己改成分组；自己那份点面都有的
# 拆成「名（点）」「名（面）」。样本一条不少，谁的还是谁的。
s = LibrarySession()
s.create(os.path.join(WORK, "legacy.gpkg"), name="old")
st = s.store
sed = st.add_class("沉积岩", code="1", region="R", geom_kind="point", color="#aa0000",
                   attrs={"岩类": "沉积岩"}, source="old.shp")
ss = st.add_class("砂岩", code="2", region="R", parent_id=sed, geom_kind="point")
md = st.add_class("泥岩", code="3", region="R", parent_id=sed, geom_kind="polygon")
ign = st.add_class("岩浆岩", code="4", region="R", geom_kind="point")
gr = st.add_class("花岗岩", code="5", region="R", parent_id=ign, geom_kind="point")
met = st.add_class("变质岩", code="6", region="R", geom_kind="polygon")
gn = st.add_class("片麻岩", code="7", region="R", parent_id=met, geom_kind="point")
st.add_samples(sed, [pt(100.0015, 39.9985), pt(100.0035, 39.9965), pt(100.0055, 39.9945)])
st.add_samples(ss, [pt(100.0075, 39.9925), pt(100.0095, 39.9905)])
st.add_samples(md, [square(100.010, 39.985, 0.003)])
st.add_samples(gr, [pt(100.0135, 39.9865)])
st.add_samples(met, [square(100.001, 39.981, 0.002), pt(100.0155, 39.9845)])   # 自己那份点面都有
st.add_samples(gn, [pt(100.0175, 39.9825)])
st.set_meta("format_version", "1.0")
s.close()
s = LibrarySession()
s.open(os.path.join(WORK, "legacy.gpkg"))
cm = s.class_map
by_name = {}
for c in s.classes:
    by_name.setdefault(c.name, []).append(c)
check(s.store.get_meta("format_version") == config.LIBRARY_FORMAT_VERSION
      and len(s.records) == 10, "版本写成 %s，10 条一条不少" % config.LIBRARY_FORMAT_VERSION)
check(cm[sed].is_group and cm[sed].count == 0 and cm[sed].total_count == 6,
      "沉积岩改成分组：自身 0 条，含子类 6 条")
leaf = [c for c in by_name["沉积岩"] if c.class_id != sed]
check(len(leaf) == 1 and leaf[0].parent_id == sed and leaf[0].is_leaf
      and leaf[0].geom_kind == "point" and leaf[0].count == 3
      and leaf[0].code == "1" and leaf[0].color == "#aa0000"
      and leaf[0].attrs == {"岩类": "沉积岩"} and leaf[0].source == "old.shp"
      and not leaf[0].edited,
      "同名基础类别「沉积岩」挂在分组下：3 个点，类别号 / 颜色 / 属性 / 来源都带过来，不算改动过")
leaf = leaf[0]
check(s.child_class_ids(sed) == [leaf.class_id, ss, md], "同名类别排在兄弟最前")
check(cm[ign].is_group and cm[ign].count == 0 and len(by_name["岩浆岩"]) == 1,
      "自己没样本的父类只改成分组，不多建类别")
met_leaves = sorted(c.name for c in s.classes if c.parent_id == met and c.class_id != gn)
check(cm[met].is_group and met_leaves == ["变质岩（点）", "变质岩（面）"]
      and cm[met].total_count == 3,
      "自己那份点面都有：拆成「变质岩（点）」「变质岩（面）」")
# 第二次打开不再动
s.close()
s = LibrarySession(); s.open(os.path.join(WORK, "legacy.gpkg"))
check(len(s.classes) == 10 and len(s.records) == 10, "再次打开：类别数与样本数不变")
# 树上不再有"两头都不是"的节点
bad = [c.name for c in s.classes
       if (c.is_group and c.count) or (c.is_leaf and s.child_class_ids(c.class_id))]
check(not bad, "分组不存样本、基础类别不挂子类别：%s" % bad)
# 勾选 / 导出 / 裁剪按新树走
s.select_class(sed, True)
check(len(s.selection) == 6 and s.export_units_from_selection() == [sed], "勾整支：6 条一个单元")
s.selection.clear()
s.select_class(leaf.class_id, True)
s.select_class(md, True)
units = s.export_units_from_selection()
check(sorted(units) == sorted([leaf.class_id, md]), "只勾同名类别与泥岩：各成单元")
rep = s.export(os.path.join(WORK, "legacy_exp1"), use_selection=True,
               level="custom", unit_class_ids=units)
check(rep.samples == 4 and len(rep.files) == 2 and not rep.warnings,
      "custom 导出 4 条 2 个文件：%s" % rep.warnings)
s.select_class(sed, True); s.select_class(met, True); s.select_class(ign, True)
rep = s.export(os.path.join(WORK, "legacy_exp2"), use_selection=True, level=1)
check(rep.samples == 10 and len(rep.files) == 5 and len(rep.warnings) == 2,
      "按第 1 层导出：10 条 5 个文件（沉积岩、变质岩点面各拆两个，有提示）")
rep = s.export(os.path.join(WORK, "legacy_exp3"), use_selection=True, level="leaf")
check(rep.samples == 10 and len(rep.files) == 7, "按末级导出：10 条 7 个文件")
# 裁剪：影像盖住全部样本，三种层级下位置数都对得上
img2 = os.path.join(WORK, "legacy.tif")
d = gdal.GetDriverByName("GTiff").Create(img2, 40, 40, 1, gdal.GDT_Byte)
d.SetGeoTransform((99.999, 0.0005, 0, 40.0, 0, -0.0005))
d.SetProjection(srs.ExportToWkt()); d.GetRasterBand(1).WriteArray(np.ones((40, 40), np.uint8)); d = None
def _clip(**kw):
    return C.clip_library(s.store, img2, os.path.join(WORK, "legacy_clip"), patch_size=3,
                          output_format="tif", train_ratio=1.0, max_positions=10 ** 6,
                          selection=set(s.selection), **kw)
rep = _clip(level="leaf")
feats = {u.name: u.features for u in rep.units}
check(len(rep.units) == 7 and sum(feats.values()) == 10 and feats.get("沉积岩-沉积岩") == 3
      and feats.get("变质岩-变质岩（面）") == 1 and feats.get("变质岩-变质岩（点）") == 1
      and all(u.outside == 0 for u in rep.units),
      "裁剪 leaf：7 个单元、10 个要素、同名类别 3 个：%s" % feats)
rep = _clip(level=1)
feats = {u.name: u.features for u in rep.units}
check(len(rep.units) == 3 and feats == {"沉积岩": 6, "岩浆岩": 1, "变质岩": 3},
      "裁剪第 1 层：三个分组各自汇总：%s" % feats)
rep = _clip(level="custom", unit_class_ids=[leaf.class_id, ss, met])
feats = {u.name: u.features for u in rep.units}
check(feats == {"沉积岩-沉积岩": 3, "沉积岩-砂岩": 2, "变质岩": 3},
      "裁剪 custom：同名基础类别（按路径命名）与同名分组各自成单元、不串：%s" % feats)
s.close()

# 兜底：库里若仍有"自己持有被选样本、子类别又各成单元"的父节点（存储层不拦），
# 父节点自身那份也要有单元管，不再被"不在任何单元之下"跳过
s = LibrarySession()
s.create(os.path.join(WORK, "odd.gpkg"), name="odd")
st = s.store
p_ = st.add_class("父", code="1", region="R", geom_kind="point")
c_ = st.add_class("子", code="2", region="R", parent_id=p_, geom_kind="point")
st.add_samples(p_, [pt(1, 1), pt(2, 2)])
st.add_samples(c_, [pt(3, 3)])
s.reload()
s.select_class(c_, True)
s.select_keys(s.keys_of_class(p_)[:1], True)       # 父只选了自己两条里的一条：部分勾选
units = s.export_units_from_selection()
rep = s.export(os.path.join(WORK, "odd_exp"), use_selection=True, level="custom",
               unit_class_ids=units)
check(sorted(units) == sorted([p_, c_]) and rep.samples == 2 and len(rep.files) == 2
      and not rep.warnings, "父自身那份自成单元、按最深归属：2 条 2 个文件")
s.close()

print("[25] 导入时同名类别另起带序号的名，谁也不并进谁；复制粘贴与跨工区移动")


def _shp32(name, poly=False, n=3):
    path = os.path.join(WORK, "imp32_%s.geojson" % name)
    ds_ = ogr.GetDriverByName("GeoJSON").CreateDataSource(path)
    lyr_ = ds_.CreateLayer(name, geom_type=ogr.wkbPolygon if poly else ogr.wkbPoint)
    for i in range(n):
        f_ = ogr.Feature(lyr_.GetLayerDefn())
        f_.SetGeometry(square(100.5 + i * 0.01, 40.5) if poly else pt(100.5 + i * 0.01, 40.5))
        lyr_.CreateFeature(f_)
    ds_ = None
    final = os.path.join(WORK, "%s.geojson" % name)
    if os.path.exists(final):
        os.remove(final)
    os.rename(path, final)
    return final


s = LibrarySession()
s.create(os.path.join(WORK, "imp32.gpkg"), name="t")
G32 = s.store.add_class("沉积岩", code="1", region="工区一", geom_kind="group")
A32 = s.store.add_class("砂岩", code="11", region="工区一", parent_id=G32)
B32 = s.store.add_class("花岗岩", code="2", region="工区一")
s.store.add_samples(A32, [pt(100.01, 39.99)]); s.store.add_samples(B32, [pt(100.02, 39.99)])
s.reload()
# 1) 号撞上分组「1 沉积岩」：号不算数，泥岩是新类别
rep = s.import_file(_shp32("1_泥岩"), region="工区一")
by_name = {c.name: c for c in s.classes}
check("泥岩" in by_name and by_name["泥岩"].parent_id is None and by_name["泥岩"].count == 3
      and by_name["砂岩"].count == 1 and rep.classes == 1 and not rep.warnings,
      "「泥岩」作为新类别出现在顶层，砂岩没被塞样本")
# 2) 同名再导：不追加，另起「泥岩1」；再来一次是「泥岩2」
rep = s.import_file(_shp32("1_泥岩"), region="工区一")
by_name = {c.name: c for c in s.classes}
check("泥岩1" in by_name and by_name["泥岩1"].count == 3 and by_name["泥岩"].count == 3
      and any("命名为「泥岩1」" in w for w in rep.warnings),
      "同名再导 → 「泥岩1」，原来的不动，报告里说了")
s.import_file(_shp32("1_泥岩"), region="工区一")
by_name = {c.name: c for c in s.classes}
check("泥岩2" in by_name, "第三次 → 「泥岩2」")
# 3) 与分组同名：也是另起「沉积岩1」，不进分组
s.import_file(_shp32("1_沉积岩"), region="工区一")
by_name = {c.name: c for c in s.classes}
check("沉积岩1" in by_name and by_name["沉积岩1"].is_leaf and by_name["沉积岩1"].parent_id is None,
      "与分组同名 → 顶层新建「沉积岩1」")
# 3b) 序号不叠加：「泥岩2」再撞不叫「泥岩21」，从「泥岩」接着数 → 「泥岩3」
s.copy_classes([by_name["泥岩2"].class_id], region="工区一")
by_name = {c.name: c for c in s.classes}
check("泥岩3" in by_name and "泥岩21" not in by_name, "复制「泥岩2」→「泥岩3」，不是「泥岩21」")
check(s.store.unique_class_name("工区一", "T3bb2") == "T3bb2"
      and s.store.unique_class_name("工区一", "泥岩") == "泥岩4",
      "本来就带数字的代号不被当成序号；「泥岩」下一个空号是 4")
# 3c) 点面混合文件的两片叶子也查重：工区里已单独躺着「混合（点）」
s.store.add_class("混合（点）", region="工区一"); s.reload()
mixed32 = os.path.join(WORK, "混合.geojson")
ds_ = ogr.GetDriverByName("GeoJSON").CreateDataSource(mixed32)
lyr_ = ds_.CreateLayer("m", geom_type=ogr.wkbUnknown)
for g_ in (pt(100.6, 40.6), square(100.61, 40.6)):
    f_ = ogr.Feature(lyr_.GetLayerDefn()); f_.SetGeometry(g_); lyr_.CreateFeature(f_)
ds_ = None
s.import_file(mixed32, region="工区一")
names32 = [c.name for c in s.classes if c.region == "工区一"]
check(names32.count("混合（点）") == 1 and "混合（点）1" in names32 and "混合（面）" in names32,
      "分组下的「混合（点）」撞上顶层已有的 → 「混合（点）1」")
by_name = {c.name: c for c in s.classes}
# 4) 名字里已带数字：「砂岩」在分组下，顶层导「砂岩」→「砂岩1」（工区范围内查重）
s.import_file(_shp32("11_砂岩"), region="工区一")
by_name = {c.name: c for c in s.classes}
check("砂岩1" in by_name and by_name["砂岩"].count == 1, "分组下有「砂岩」→ 顶层叫「砂岩1」")
# 5) 树上能看见
reachable = set()
for cid in s.root_class_ids("工区一"):
    reachable.update(s.subtree_class_ids(cid))
check(reachable == {c.class_id for c in s.classes}, "每个类别都能从工区顶层走到")

# ---- 复制粘贴
n_cls, n_rec = len(s.classes), len(s.records)
new_ids = s.copy_classes([G32], region="工区二")
by_name = {(c.name, c.region): c for c in s.classes}
check(len(new_ids) == 1 and ("沉积岩", "工区二") in by_name and by_name[("沉积岩", "工区二")].is_group,
      "把分组「沉积岩」复制到工区二：新分组")
kids = [c for c in s.classes if c.parent_id == new_ids[0]]
check([c.name for c in kids] == ["砂岩"] and kids[0].count == 1 and by_name[("砂岩", "工区一")].count == 1,
      "子类别与样本一并复制，原来的原样")
check(len(s.records) == n_rec + 1 and len(s.classes) == n_cls + 2, "样本 +1、类别 +2")
s.undo()
check(len(s.classes) == n_cls and len(s.records) == n_rec, "撤销：复制的整支连样本一起消失")
# 粘贴到同一工区：同名另起序号
s.copy_classes([by_name[("泥岩", "工区一")].class_id], region="工区一")
names = {c.name for c in s.classes if c.region == "工区一"}
check("泥岩4" in names, "同工区粘贴 → 「泥岩4」（1、2、3 已被占）")
# 分组复制到另一个分组下
s.copy_classes([by_name[("泥岩", "工区一")].class_id], parent_id=G32)
inside = [c.name for c in s.classes if c.parent_id == G32]
check("泥岩5" in inside, "粘贴进分组「沉积岩」下：%s" % inside)
# 复制到自己下面：拒绝
try:
    s.copy_classes([G32], parent_id=G32)
    check(False, "应当拒绝")
except ValueError:
    check(True, "拒绝把分组复制到它自己下面")
# 样本粘贴进基础类别
target = by_name[("花岗岩", "工区一")].class_id
n = s.copy_samples_into([by_name[("泥岩", "工区一")].class_id], target)
check(n == 3 and s.class_map[target].count == 4, "把泥岩的 3 条样本复制进花岗岩 → 4 条")
s.undo()
check(s.class_map[target].count == 1, "撤销后花岗岩回到 1 条")
poly_cls = s.store.add_class("面类", region="工区一", geom_kind="polygon")
s.store.add_samples(poly_cls, [square(100.7, 40.7)])
s.reload()
try:
    s.copy_samples_into([poly_cls], target)
    check(False, "应当拒绝")
except ValueError:
    check(True, "面类别的样本不能复制进点类别")
# 复制整个工区（所有顶层类别）到另一个工区
tops = s.root_class_ids("工区一")
made = s.copy_classes(tops, region="工区三")
check(len(made) == len(tops) and len(s.root_class_ids("工区三")) == len(tops),
      "工区一的 %d 个顶层类别整体复制到工区三" % len(tops))

# ---- 跨工区拖动：拖到另一个工区的两条之间也要真的换工区
mover = by_name[("泥岩", "工区一")].class_id
anchor = s.root_class_ids("工区三")[0]
s.reorder_class(mover, None, before_id=anchor, region="工区三")
check(s.class_map[mover].region == "工区三" and s.root_class_ids("工区三")[0] == mover,
      "拖到工区三第一条之前：进了工区三、排在最前")
s.undo()
check(s.class_map[mover].region == "工区一", "撤销回工区一")
s.close()

print("[26] 导出预览：按此层级会得到几个类别")
s = LibrarySession()
s.open(os.path.join(WORK, "imp32.gpkg"))
by_name = {c.name: c for c in s.classes if c.region == "工区一"}
s.select_all()
n_leaf = len([c for c in s.classes if c.is_leaf and c.count])
n_top = len([cid for cid in s.root_class_ids()
             if any(s.class_map[c].count for c in s.subtree_class_ids(cid))])
pv = s.export_preview("leaf")
check(pv["units"] == n_leaf and pv["samples"] == len(s.records),
      "预览：末级 %d 个单元、%d 条样本" % (pv["units"], pv["samples"]))
pv1 = s.export_preview(1)
check(pv1["units"] == n_top and pv1["samples"] == len(s.records),
      "预览：第 1 层 %d 个单元（分组下几片叶子并成分组一个）" % pv1["units"])
from samplelib import io_shp as _io_shp
_targets1, _notes1 = _io_shp.export_targets(s.classes, s.class_map, set(s.class_map), 1)
mixed_units = [u for u, ids in _targets1
               if len({r.geom_kind for c in ids for r in s.records_by_class[c]}) > 1]
check(pv1["files"] == pv1["units"] + len(mixed_units), "点面都有的单元按两个文件数")
s.close()

# =============================================================================
print()
if FAILED:
    print("失败 %d 项：" % len(FAILED))
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
