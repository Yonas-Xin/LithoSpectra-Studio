# -*- coding: utf-8 -*-
"""新增功能的端到端自测：类别排序 / 删除合并 / 标签重编 / 唯一值赋色 / 颜色表。"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from osgeo import gdal, ogr, osr

from samplelib import config
from samplelib.store import SampleStore
from samplelib.session import LibrarySession
from samplelib.annotate.editing import EditBuffer
from samplelib import io_shp
from samplelib.modules.clip import clip

gdal.UseExceptions()
WORK = tempfile.mkdtemp(prefix="slfeat_")


def pt(x, y):
    g = ogr.Geometry(ogr.wkbPoint)
    g.AddPoint_2D(x, y)
    return g


def make_image(path, w=60, h=60, bands=4):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, w, h, bands, gdal.GDT_UInt16)
    ds.SetGeoTransform((100.0, 0.001, 0, 40.0, 0, -0.001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    rng = np.random.default_rng(0)
    for b in range(1, bands + 1):
        ds.GetRasterBand(b).WriteArray(rng.integers(0, 4000, (h, w)).astype(np.uint16))
    ds = None
    return path


def make_paletted(path, w=40, h=40):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, w, h, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((100.0, 0.001, 0, 40.0, 0, -0.001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    arr = np.zeros((h, w), dtype=np.uint8)
    arr[:, :13] = 1
    arr[:, 13:26] = 2
    arr[:, 26:] = 3
    ct = gdal.ColorTable()
    ct.SetColorEntry(0, (0, 0, 0, 0))
    ct.SetColorEntry(1, (255, 0, 0, 255))
    ct.SetColorEntry(2, (0, 255, 0, 255))
    ct.SetColorEntry(3, (0, 0, 255, 255))
    # GTiff 必须先定调色板再写数据，写过之后 PhotometricInterpretation 就锁死了
    ds.GetRasterBand(1).SetRasterColorTable(ct)
    ds.GetRasterBand(1).WriteArray(arr)
    ds = None
    return path


def make_vector(path):
    drv = ogr.GetDriverByName("GeoJSON")
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    lyr = ds.CreateLayer("v", srs, ogr.wkbPoint)
    lyr.CreateField(ogr.FieldDefn("rock", ogr.OFTString))
    for i, rock in enumerate(["灰岩", "砂岩", "灰岩", "花岗岩", None]):
        f = ogr.Feature(lyr.GetLayerDefn())
        if rock is not None:
            f.SetField("rock", rock)
        f.SetGeometry(pt(100.01 + i * 0.001, 39.99))
        lyr.CreateFeature(f)
    ds = None
    return path


def build_library(path):
    st = SampleStore.create(path, name="测试库", overwrite=True)
    ids = {}
    ids["A"] = st.add_class("甲组", code="7", region="工区一", geom_kind="group")
    ids["A1"] = st.add_class("甲一", code="9", region="工区一", parent_id=ids["A"])
    ids["A2"] = st.add_class("甲二", code="2", region="工区一", parent_id=ids["A"])
    ids["B"] = st.add_class("乙组", code="4", region="工区一")
    ids["C"] = st.add_class("丙组", code="1", region="工区二")
    # 乙组 / 丙组 名字里带"组"，但它们直接持有样本，是基础类别
    grid = [(100.01 + (i % 5) * 0.002, 39.99 - (i // 5) * 0.002) for i in range(8)]
    for key in ("A1", "A2", "B", "C"):
        st.add_samples(ids[key], [pt(x, y) for x, y in grid[:4]], source="test")
    st.close()
    return ids


def codes_of(sess, region):
    return [(sess.class_map[c].name, sess.class_map[c].code)
            for c in sess._region_tree_order(region)]


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    print("  ✓ %s" % what)


# =============================================================================
print("[1] 类别顺序与类别号")
lib = os.path.join(WORK, "lib.gpkg")
ids = build_library(lib)
sess = LibrarySession()
sess.open(lib)

order = [sess.class_map[c].name for c in sess._region_tree_order("工区一")]
check(order == ["乙组", "甲组", "甲二", "甲一"],
      "初始顺序按类别号：%s" % order)

# 把「甲组」拖到「乙组」之前
sess.reorder_class(ids["A"], None, before_id=ids["B"])
order = codes_of(sess, "工区一")
check([n for n, _ in order] == ["甲组", "甲二", "甲一", "乙组"],
      "拖到顶层第一位后顺序正确：%s" % [n for n, _ in order])
check([c for _, c in order] == ["1", "2", "3", "4"],
      "类别号连续重编：%s" % [c for _, c in order])
check([n for n, _ in codes_of(sess, "工区二")] == ["丙组"], "另一个工区不受影响")

# 兄弟之间调序：甲一排到甲二之前
sess.reorder_class(ids["A1"], ids["A"], before_id=ids["A2"])
order = codes_of(sess, "工区一")
check([n for n, _ in order] == ["甲组", "甲一", "甲二", "乙组"],
      "同级调序生效：%s" % [n for n, _ in order])
check([c for _, c in order] == ["1", "2", "3", "4"], "类别号仍然连续")

# 变成子类
sess.reorder_class(ids["B"], ids["A"], before_id=None)
check(sess.class_map[ids["B"]].parent_id == ids["A"], "落在类别中间 = 变成它的子类")
check([c for _, c in codes_of(sess, "工区一")] == ["1", "2", "3", "4"], "重排后仍连续")

# 跨工区
sess.reorder_class(ids["C"], None, None, region="工区一")
check(sess.class_map[ids["C"]].region == "工区一", "拖到工区标题上会改工区")
check(len(sess._region_tree_order("工区二")) == 0, "原工区已空")

# 撤销一路退回
for _ in range(4):
    sess.undo()
order = codes_of(sess, "工区一")
check([n for n, _ in order] == ["乙组", "甲组", "甲二", "甲一"],
      "撤销回到初始顺序：%s" % [n for n, _ in order])
check([c for _, c in order] == ["4", "7", "2", "9"],
      "撤销把原类别号也还了回去：%s" % [c for _, c in order])
check(sess.class_map[ids["C"]].region == "工区二", "工区也还原了")

# 成环保护
try:
    sess.reorder_class(ids["A"], ids["A1"])
    raise AssertionError("应当拒绝把父类挂到子类下")
except ValueError:
    print("  ✓ 拒绝把类别挂到自己的子类下面")

check(not hasattr(sess, "renumber_region") and not hasattr(sess, "set_class_code"),
      "类别号不再给人看、也不再给人改：没有手动重排 / 修改类别号的入口")
check(all(c.display_name == c.name for c in sess.classes), "树上显示的就是类别名，不带编号")
sess.close()

# =============================================================================
print("[2] 标注窗口：删除与合并")
sess = LibrarySession()
sess.open(lib)
buf = EditBuffer(sess.require_store())

n_before = len(buf.records())
cid_draft = buf.add_class("草稿类", "工区一")
buf.add_feature(cid_draft, "point", [(100.02, 39.98)])
check(len(buf.classes()) == 6 and len(buf.records()) == n_before + 1, "草稿类别已建")

count, feats = buf.delete_classes([ids["A"]])
check(count == 3, "删除一个父类连带两个子类：%d" % count)
check(feats == 8, "连带删掉 8 个要素：%d" % feats)
check(all(c.class_id not in (ids["A"], ids["A1"], ids["A2"]) for c in buf.classes()),
      "类别已从合并视图里消失")
check(all(r.class_id not in (ids["A1"], ids["A2"]) for r in buf.records()),
      "要素也跟着消失")
buf.undo()
check(len(buf.classes()) == 6 and len(buf.records()) == n_before + 1, "删除可撤销")

count, feats = buf.delete_classes([cid_draft])
check(count == 1 and feats == 1, "草稿类别也能删")
buf.undo()

moved = buf.merge_classes([ids["A1"], ids["A2"]], ids["A1"])
check(moved == 4, "合并搬走 4 个要素：%d" % moved)
check(ids["A2"] not in buf.class_map(), "被并掉的类别没了")
check(sum(1 for r in buf.records() if r.class_id == ids["A1"]) == 8,
      "要素都归到了目标类")
buf.undo()
check(ids["A2"] in buf.class_map()
      and sum(1 for r in buf.records() if r.class_id == ids["A1"]) == 4,
      "合并一步撤销")

count, feats = buf.delete_region("工区二")
check(count == 1 and feats == 4, "删除整个工作区")
buf.undo()

# 落库：合并 + 删除
buf.merge_classes([ids["A1"], ids["A2"]], ids["A1"])
report = buf.commit()
check(report["dropped_classes"] == 1, "保存时真的删掉了类别")
sess.reload()
check(ids["A2"] not in sess.class_map, "库内已无被并掉的类别")
check(len(sess.records_by_class.get(ids["A1"], [])) == 8, "要素都在目标类下，一条没丢")

# 落库：整支删除（父类 + 子类一起进删除集，不能挨个 id 删）
buf2 = EditBuffer(sess.require_store())
count, feats = buf2.delete_classes([ids["A"]])
check(count == 2 and feats == 8, "整支进删除集：%d 类 %d 要素" % (count, feats))
rep2 = buf2.commit()
check(rep2["dropped_classes"] == 2 and rep2["deleted"] == 8,
      "整支删除落库：删类别 %d 个、要素 %d 条" % (rep2["dropped_classes"], rep2["deleted"]))
sess.reload()
check(ids["A"] not in sess.class_map and ids["A1"] not in sess.class_map,
      "父类与子类都从库里消失了")
check(sorted(c.name for c in sess.classes) == sorted(["乙组", "丙组", "草稿类"]),
      "剩下的类别没受牵连：%s" % [c.name for c in sess.classes])
sess.close()

# =============================================================================
print("[3] 导出 / 裁剪的标签号")
lib2 = os.path.join(WORK, "lib2.gpkg")
ids2 = build_library(lib2)
st = SampleStore.open(lib2)
out = os.path.join(WORK, "shp")
os.makedirs(out, exist_ok=True)
rep = io_shp.export_classes(st, out, level="leaf")
import json
desc = json.load(open(os.path.join(out, config.DESCRIPTION_JSON), encoding="utf-8"))
labels = [c["label_id"] for c in desc["classes"]]
check(labels == list(range(len(labels))), "导出标签 0 起连续：%s" % labels)
codes = [c["label_code"] for c in desc["classes"]]
check(set(codes) & {"9", "2", "4", "1"}, "库内类别号仍记在 label_code 里：%s" % codes)

# 只导其中两类，标签依然是 0、1
out2 = os.path.join(WORK, "shp2")
os.makedirs(out2, exist_ok=True)
io_shp.export_classes(st, out2, class_ids=[ids2["A1"], ids2["C"]], level="leaf")
desc2 = json.load(open(os.path.join(out2, config.DESCRIPTION_JSON), encoding="utf-8"))
check([c["label_id"] for c in desc2["classes"]] == [0, 1],
      "只导两类时标签是 0、1，不沿用库内的 9 和 1")

# 裁剪：有一个类别的样本全在影像外，它不该占号
img = make_image(os.path.join(WORK, "img.tif"))
far = st.add_class("界外类", code="99", region="工区一")
st.add_samples(far, [pt(120.0, 20.0)], source="test")
st.close()
st = SampleStore.open(lib2)
rep = clip.clip_library(st, img, os.path.join(WORK, "clip"), level="leaf",
                        patch_size=5, train_ratio=1.0, output_format="bin")
labels = [u.label for u in rep.units]
check(labels == list(range(len(labels))), "裁剪标签 0 起连续：%s" % labels)
check(all(u.train for u in rep.units), "登记在册的每个标签都真有样本块")
check(any("界外类" in w for w in rep.warnings),
      "没裁出块的类别有提示：%s" % [w for w in rep.warnings if "界外" in w])
doc = json.load(open(os.path.join(WORK, "clip", "labels.json"), encoding="utf-8"))
check([l["label"] for l in doc["labels"]] == list(range(len(doc["labels"]))),
      "labels.json 里也是连续的")
st.close()

# =============================================================================
print("[4] 矢量唯一值赋色")
from samplelib.annotate import layers as L

vec = make_vector(os.path.join(WORK, "v.geojson"))
vl = L.VectorLayer(vec)
check(vl.color_groups(vl.features) == {vl.color: vl.features}, "默认整层一个颜色")
values = vl.set_unique_colors("rock")
check(values == ["灰岩", "砂岩", "花岗岩", L.NULL_VALUE_LABEL],
      "唯一值按出现顺序去重，空值单列：%s" % values)
groups = vl.color_groups(vl.features)
check(len(groups) == 4, "四个取值分四组")
check(len(groups[vl.color_map["灰岩"]]) == 2, "同一取值的要素归到一组")
old = dict(vl.color_map)
vl.set_unique_colors("rock", vl.color_map)
check(vl.color_map == old, "重新赋色时保留已定的颜色")
vl.clear_unique_colors()
check(vl.color_field == "" and len(vl.color_groups(vl.features)) == 1, "可以取消赋色")

# =============================================================================
print("[5] 颜色表影像")
pal = make_paletted(os.path.join(WORK, "pal.tif"))
rl = L.RasterLayer(pal)
check(rl.color_table is not None and len(rl.color_table) >= 4,
      "读到颜色表：%d 项" % len(rl.color_table))
check(rl.use_color_table and rl.bands == (1,), "默认按颜色表显示单波段")
view = L.ViewSpec(bounds=rl.extent(), width=80, height=80)
arr = rl.render_array(view)
check(arr is not None and arr.shape == (80, 80, 4), "渲染出 RGBA：%s" % (arr.shape,))
seen = {tuple(c) for c in arr[40, :, :3]}
check((255, 0, 0) in seen and (0, 255, 0) in seen and (0, 0, 255) in seen,
      "画出来的就是颜色表里的红绿蓝：%s" % sorted(seen))
rl.set_use_color_table(False)
arr2 = rl.render_array(view)
check(arr2 is not None and {tuple(c) for c in arr2[40, :, :3]} != seen,
      "关掉之后退回按数值拉伸")
rl.close()

# 普通影像不受影响
rl2 = L.RasterLayer(img)
check(rl2.color_table is None and not rl2.use_color_table, "无颜色表的影像照旧")
check(rl2.render_array(L.ViewSpec(bounds=rl2.extent(), width=60, height=60)) is not None,
      "普通影像仍能渲染")
rl2.close()

# =============================================================================
print("[6] 图层描述往返")
# 退出时把画布上的图层记成描述、下次打开原样铺回来，走的就是这一对函数。
# （原来它还兼着 .slproj 标注工程的读写，那套已经删掉。）
from samplelib.annotate import layerspec

spec = layerspec.layer_to_spec(vl, WORK)
vl.set_unique_colors("rock")
spec = layerspec.layer_to_spec(vl, WORK)
back = layerspec.spec_to_layer(spec, WORK)
check(back.color_field == "rock" and back.color_map == vl.color_map,
      "唯一值配色能存成描述再读回来")
spec = layerspec.layer_to_spec(L.RasterLayer(pal), WORK)
check(spec["use_color_table"] is True, "颜色表开关也存下来了")
import samplelib.annotate as _annotate
check(not os.path.exists(os.path.join(os.path.dirname(_annotate.__file__),
                                      "project.py")),
      "标注工程那个模块已经不在了")

shutil.rmtree(WORK, ignore_errors=True)
print("\n全部通过")
