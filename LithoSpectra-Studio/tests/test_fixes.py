# -*- coding: utf-8 -*-
"""审查缺陷的回归测试。

每个用例对应一条已复现的缺陷，用例名后面括号里是缺陷编号。直接运行本文件即可
（VS Code 里按运行键，或 ``python tests/test_fixes.py``），不需要命令行参数。
失败会抛 AssertionError 并指出是哪一条。
"""

# =============================================================================
# 配置
# =============================================================================

VERBOSE = True                 # 打印每条通过的检查
KEEP_WORKDIR = False           # 保留临时目录以便人工检查
WORKDIR_PREFIX = "slfix_"

# =============================================================================

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

import numpy as np
from osgeo import gdal, ogr, osr

from samplelib import config, io_shp
from samplelib.annotate.editing import EditBuffer, coords_to_geom
from samplelib.modules.clip.clip import _PatchCutter
from samplelib.model import GEOM_POINT, GEOM_POLYGON
from samplelib.session import LibrarySession
from samplelib.store import SampleStore, geom_to_coords

WORK = tempfile.mkdtemp(prefix=WORKDIR_PREFIX)
PASSED = []


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    PASSED.append(what)
    if VERBOSE:
        print("  ✓ %s" % what)


def new_store(name="lib.gpkg"):
    return SampleStore.create(os.path.join(WORK, name), name="测试库",
                              overwrite=True)


def square(x, y, s=1.0):
    return [(x, y), (x + s, y), (x + s, y + s), (x, y + s), (x, y)]


def wkt_poly(rings):
    parts = ["(%s)" % ", ".join("%.10f %.10f" % (a, b) for a, b in r)
             for r in rings]
    return "POLYGON (%s)" % ", ".join(parts)


# =============================================================================
# 1. MultiPolygon 编辑不能破坏拓扑
# =============================================================================
print("[1] MultiPolygon 编辑（缺陷 1）")

multi = ogr.CreateGeometryFromWkt(
    "MULTIPOLYGON (((0 0, 1 0, 1 1, 0 1, 0 0)), ((5 5, 6 5, 6 6, 5 6, 5 5)))")
kind, coords = geom_to_coords(multi)
check(kind == GEOM_POLYGON and len(coords) == 2, "两个独立面拍平成 2 个环")

back = coords_to_geom(GEOM_POLYGON, coords)
check(abs(back.GetArea() - 2.0) < 1e-9,
      "装回去面积仍是 2.0（实得 %.4f）" % back.GetArea())
check(back.IsValid(), "装回去几何有效")
check(back.GetGeometryCount() == 2, "装回去仍是 2 个部件")

# 挪一个顶点后再装配，拓扑不能变
moved = [list(r) for r in coords]
moved[1][0] = (5.1, 5.1)
back2 = coords_to_geom(GEOM_POLYGON, moved)
check(back2.IsValid() and back2.GetGeometryCount() == 2,
      "改一个顶点后仍是两个有效部件")

# 真正的孔要认出来
holed = ogr.CreateGeometryFromWkt(wkt_poly(
    [square(0, 0, 10), list(reversed(square(2, 2, 3)))]))
_, hcoords = geom_to_coords(holed)
hback = coords_to_geom(GEOM_POLYGON, hcoords)
check(abs(hback.GetArea() - (100.0 - 9.0)) < 1e-9,
      "外环里的内环仍当孔处理，面积 %.2f" % hback.GetArea())
check(hback.IsValid(), "带孔的面有效")

# 孔里的岛
island = ogr.CreateGeometryFromWkt(
    "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0), (2 2, 2 8, 8 8, 8 2, 2 2), "
    "(4 4, 5 4, 5 5, 4 5, 4 4))")
_, icoords = geom_to_coords(island)
iback = coords_to_geom(GEOM_POLYGON, icoords)
check(abs(iback.GetArea() - (100.0 - 36.0 + 1.0)) < 1e-9,
      "孔里的岛算成独立部件，面积 %.2f" % iback.GetArea())

# 走一遍真实的编辑链路：入库 -> 缓冲区改顶点 -> 提交 -> 再读出来
store = new_store("multi.gpkg")
cid = store.add_class(name="面类", region="R", geom_kind=GEOM_POLYGON)
store.insert_sample(cid, multi.Clone())
buf = EditBuffer(store)
rec = buf.records()[0]
rings = [list(r) for r in rec.coords]
rings[1][0] = (5.05, 5.05)
buf.set_geometry(rec.key, rings)
buf.commit()
saved = store.load_records()[0]
g = store.get_geometry(saved.key)
# 挪动的那个顶点让第二个方块小了一点点，1.97 左右；修复前这里是 0
check(g.IsValid() and g.GetArea() > 1.9,
      "改完顶点存回库里仍有效，面积 %.4f" % g.GetArea())
store.close()


# =============================================================================
# 2. 保存要么全成要么全不成
# =============================================================================
print("[2] 标注保存的原子性（缺陷 2）")

store = new_store("atomic.gpkg")
base_cid = store.add_class(name="已有类", region="R", geom_kind=GEOM_POLYGON)
buf = EditBuffer(store)
draft_cid = buf.add_class("新类别", region="R", geom_kind=GEOM_POLYGON)
buf.add_feature(draft_cid, GEOM_POLYGON, [square(0, 0)])
buf.add_feature(draft_cid, GEOM_POLYGON, [square(3, 3)])

calls = {"n": 0}
real_insert = store.insert_sample


def flaky(*a, **kw):
    calls["n"] += 1
    if calls["n"] == 2:
        raise RuntimeError("模拟写要素失败")
    return real_insert(*a, **kw)


store.insert_sample = flaky
try:
    buf.commit()
    raised = False
except RuntimeError:
    raised = True
store.insert_sample = real_insert

check(raised, "写要素失败时 commit 抛错")
names = [c.name for c in store.list_classes()]
check(names == ["已有类"], "失败后新类别没有留在库里：%s" % names)
check(store.total_samples() == 0, "失败后没有半截要素留在库里")
check(buf.dirty, "缓冲区仍是未保存状态，可以直接重试")

report = buf.commit()                      # 重试
check(report["written"] == 2, "重试后 2 个要素都写进去了")
names = sorted(c.name for c in store.list_classes())
check(len(names) == 2 and set(names) == {"新类别", "已有类"},
      "重试没有建出重复类别：%s" % names)
store.close()


# =============================================================================
# 3./4. 导出：混合几何不丢样本；重复导出不留旧文件
# =============================================================================
print("[3] 混合点面的类别导出（缺陷 3）")

store = new_store("export.gpkg")
mix = store.add_class(name="混合类", code="1", region="R", geom_kind=GEOM_POLYGON)
only = store.add_class(name="纯点类", code="2", region="R", geom_kind=GEOM_POINT)
pt = ogr.CreateGeometryFromWkt("POINT (100.0 40.0)")
pg = ogr.CreateGeometryFromWkt(
    "MULTIPOLYGON (((100 40, 100.01 40, 100.01 40.01, 100 40.01, 100 40)))")
store.insert_sample(mix, pt.Clone())
store.insert_sample(mix, pg.Clone())
store.insert_sample(only, ogr.CreateGeometryFromWkt("POINT (101.0 41.0)"))

out = os.path.join(WORK, "out")
rep = io_shp.export_classes(store, out, driver_key="shp")
check(rep.samples == 3, "3 条样本一条不少地导出（实得 %d）" % rep.samples)
check(len(rep.files) == 3, "混合类拆成两个文件，共 3 个文件")
kinds = set()
for f in rep.files:
    ds = ogr.Open(f)
    kinds.add(ogr.GT_Flatten(ds.GetLayer(0).GetGeomType()))
    ds = None
check(ogr.wkbPoint in kinds
      and bool(kinds & {ogr.wkbPolygon, ogr.wkbMultiPolygon}),
      "点文件与面文件都在：%s" % sorted(kinds))
labels = {c["label_name"]: c["label_id"] for c in
          __import__("json").load(open(os.path.join(out, config.DESCRIPTION_JSON),
                                       encoding="utf-8"))["classes"]}
check(sorted(set(labels.values())) == [0, 1], "标签号 0-base 连续：%s" % labels)

print("[4] 重复导出到同一目录（缺陷 4）")
rep2 = io_shp.export_classes(store, out, driver_key="shp", class_ids=[only])
left = sorted(f for f in os.listdir(out) if f.endswith(".shp"))
check(len(left) == 1, "只导一类后目录里只剩 1 个 shp：%s" % left)
check(any("已清理" in w for w in rep2.warnings),
      "报告里说明清理了哪些遗留文件")
store.close()


# =============================================================================
# 5. 长任务可以取消
# =============================================================================
print("[5] 导出可取消（缺陷 5）")

store = new_store("cancel.gpkg")
for i in range(5):
    c = store.add_class(name="类%d" % i, code=str(i), region="R",
                        geom_kind=GEOM_POINT)
    store.insert_sample(c, ogr.CreateGeometryFromWkt("POINT (%d 40)" % (100 + i)))
out2 = os.path.join(WORK, "out2")
seen = []


def stop_after_two(done, total, name):
    seen.append(done)
    return done < 3


rep = io_shp.export_classes(store, out2, driver_key="shp",
                            progress=stop_after_two)
check(len(rep.files) == 2, "按取消后停在第 2 个文件：%d" % len(rep.files))
check(any("已取消" in w for w in rep.warnings), "报告里说明已取消")
check("未做覆盖校验" in rep.checked, "取消后不谎报覆盖校验通过")
store.close()


# =============================================================================
# 6. 改类别名不重读全部样本
# =============================================================================
print("[6] 轻量操作不全量重读（缺陷 6）")

path = os.path.join(WORK, "session.gpkg")
store = SampleStore.create(path, name="s", overwrite=True)
cid = store.add_class(name="旧名", region="R", geom_kind=GEOM_POINT)
store.add_samples(cid, [ogr.CreateGeometryFromWkt("POINT (%f 40)" % (100 + i / 1000.0))
                        for i in range(300)])
store.close()

sess = LibrarySession()
sess.open(path)
before = sess.records[0]
loads = {"n": 0}
real_load = sess.store.load_records


def counting_load(*a, **kw):
    loads["n"] += 1
    return real_load(*a, **kw)


sess.store.load_records = counting_load
sess.rename_class(cid, "新名")
check(loads["n"] == 0, "改类别名没有重读样本")
check(sess.records[0] is before, "内存里的样本对象原封不动")
check(sess.class_map[cid].name == "新名", "类别名确实改了")
check(sess.class_map[cid].count == 300, "样本计数仍然正确")
sess.delete_classes([cid])
check(loads["n"] == 1, "删类别这种动到样本的操作照旧全量重读")
sess.store.load_records = real_load
sess.close()


# =============================================================================
# 7. 取单个类别不扫全库
# =============================================================================
print("[7] get_class 直取（缺陷 7）")

store = new_store("path.gpkg")
root = store.add_class(name="火山岩", code="1", region="R")
mid = store.add_class(name="玄武岩", code="2", region="R", parent_id=root)
leaf = store.add_class(name="T3bb2", code="3", region="R", parent_id=mid)
scans = {"n": 0}
real_list = store.list_classes


def counting_list(*a, **kw):
    scans["n"] += 1
    return real_list(*a, **kw)


store.list_classes = counting_list
cls = store.get_class(leaf)
check(cls is not None and cls.name == "T3bb2", "按 id 取到了类别")
check(scans["n"] == 0, "取单个类别没有列举整张类别表")
by_id = {c.class_id: c for c in real_list()}
store.list_classes = counting_list
scans["n"] = 0
name = io_shp._class_path_name(by_id[leaf], store, by_id=by_id)
check(name == "火山岩-玄武岩-T3bb2", "层级路径正确：%s" % name)
check(scans["n"] == 0, "构造层级路径没有回库查询")
store.list_classes = real_list
idx = store._sql("SELECT name FROM sqlite_master WHERE type='index' "
                 "AND name LIKE 'idx_%class_id'")
check(len(idx) == 2, "两张样本表都建了 class_id 索引：%s" % [r["name"] for r in idx])
store.close()


# =============================================================================
# 8. 旋转影像的块地理变换
# =============================================================================
print("[8] 旋转影像裁块（缺陷 8）")

tif = os.path.join(WORK, "rot.tif")
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create(tif, 64, 64, 1, gdal.GDT_Byte)
rot_gt = (500000.0, 8.66, -5.0, 4000000.0, -5.0, -8.66)   # 30° 旋转
ds.SetGeoTransform(rot_gt)
srs = osr.SpatialReference()
srs.ImportFromEPSG(32646)
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(np.arange(64 * 64, dtype=np.uint8).reshape(64, 64))
ds = None

ds = gdal.Open(tif)
cutter = _PatchCutter(ds, 8)
row, col = 40, 24
_, gt = cutter.cut(row, col)
x_start, y_start = col - cutter.left_top, row - cutter.left_top
want_x = (rot_gt[0] + x_start * rot_gt[1] + y_start * rot_gt[2])
want_y = (rot_gt[3] + x_start * rot_gt[4] + y_start * rot_gt[5])
check(abs(gt[0] - want_x) < 1e-6 and abs(gt[3] - want_y) < 1e-6,
      "块原点算进了旋转项：(%.3f, %.3f)" % (gt[0], gt[3]))
# 与 GDAL 自己的像素->地理换算对齐，这才是"位置对不对"的判据
gx = rot_gt[0] + x_start * rot_gt[1] + y_start * rot_gt[2]
gy = rot_gt[3] + x_start * rot_gt[4] + y_start * rot_gt[5]
check(abs(gt[0] - gx) < 1e-6 and abs(gt[3] - gy) < 1e-6,
      "与 GDAL 仿射公式一致")
# 北向影像不受影响
ds2 = drv.Create(os.path.join(WORK, "north.tif"), 32, 32, 1, gdal.GDT_Byte)
ds2.SetGeoTransform((100.0, 1.0, 0.0, 200.0, 0.0, -1.0))
ds2.GetRasterBand(1).WriteArray(np.zeros((32, 32), dtype=np.uint8))
ds2 = None
c2 = _PatchCutter(gdal.Open(os.path.join(WORK, "north.tif")), 4)
_, gt2 = c2.cut(10, 6)
want = (100.0 + (6 - c2.left_top) * 1.0, 200.0 - (10 - c2.left_top) * 1.0)
check(abs(gt2[0] - want[0]) < 1e-9 and abs(gt2[3] - want[1]) < 1e-9,
      "北向影像结果不变：(%.1f, %.1f)" % (gt2[0], gt2[3]))
ds = None

# =============================================================================
# 9. 导出编号：0 起、连续、跨工区不重来，文件名上的号就是它
# =============================================================================
print("[9] 导出的标签号与文件名")

store = new_store("labels.gpkg")
# 甲区：库内号故意留空档（1、5、9），其中 9 号带两个子类
a1 = store.add_class(name="晚更新世堆积物", code="1", region="甲区",
                     geom_kind=GEOM_POINT)
a2 = store.add_class(name="干柴沟组", code="5", region="甲区", geom_kind=GEOM_POINT)
a3 = store.add_class(name="油砂山组", code="9", region="甲区", geom_kind=GEOM_POINT)
a3a = store.add_class(name="粉砂岩", code="10", region="甲区",
                      geom_kind=GEOM_POINT, parent_id=a3)
a3b = store.add_class(name="泥岩", code="11", region="甲区",
                      geom_kind=GEOM_POINT, parent_id=a3)
# 乙区：库内号又从头编，且撞上甲区已经用过的号
b1 = store.add_class(name="英云闪长岩", code="11", region="乙区",
                     geom_kind=GEOM_POINT)
b2 = store.add_class(name="羊曲组", code="12", region="乙区", geom_kind=GEOM_POINT)
for i, cid in enumerate([a1, a2, a3a, a3b, b1, b2]):
    store.insert_sample(cid, ogr.CreateGeometryFromWkt(
        "POINT (%f 40.0)" % (100.0 + i * 0.01)))

out = os.path.join(WORK, "labels_out")
rep = io_shp.export_classes(store, out, driver_key="shp", level=1)
desc = json.load(open(os.path.join(out, config.DESCRIPTION_JSON), encoding="utf-8"))
rows = desc["classes"]
ids = [c["label_id"] for c in rows]
check(ids == list(range(len(rows))),
      "标签号 0 起连续、无跳号：%s" % ids)
check(ids[0] == 0, "第一个类别是 0 号，不是 1 号")

# 文件名上的号 == 文件里的 class 字段 == description 里的 label_id
for row in rows:
    stem = os.path.splitext(row["shp_file"])[0]
    n = int(stem.split("_")[1])
    check(n == row["label_id"],
          "文件名与标签号一致：%s → %d" % (row["shp_file"], row["label_id"]))
    ds = ogr.Open(os.path.join(out, row["shp_file"]))
    vals = {f.GetField("class") for f in ds.GetLayer(0)}
    ds = None
    check(vals == {row["label_id"]},
          "%s 里的 class 字段是 %d" % (row["shp_file"], row["label_id"]))

# 库内号跳空、跨工区重号，都不该影响导出编号
by_name = {c["label_name"]: c for c in rows}
check(by_name["干柴沟组"]["label_id"] + 1 == by_name["油砂山组"]["label_id"],
      "库内号 5 与 9 的两个类，导出后编号相邻：%d、%d"
      % (by_name["干柴沟组"]["label_id"], by_name["油砂山组"]["label_id"]))
# 工区之间不重新计数：每个工区的标签号是一段连续区间，下一个工区紧接着上一段
# 往下排（工区先后由类别表的排序决定，与树上看到的顺序一致）
blocks = {}
for row in rows:
    blocks.setdefault(row["region"], []).append(row["label_id"])
spans = sorted((min(v), max(v)) for v in blocks.values())
check(all(len(v) == max(v) - min(v) + 1 for v in blocks.values()),
      "每个工区的标签号各自连成一段：%s" % blocks)
check(all(spans[i][1] + 1 == spans[i + 1][0] for i in range(len(spans) - 1)),
      "下一个工区接着上一段往下排，不从头再来：%s" % spans)
check(by_name["英云闪长岩"]["label_id"] != 11,
      "库内号 11 的类别不再原样写成 11：实得 %d"
      % by_name["英云闪长岩"]["label_id"])
check(len({c["label_id"] for c in rows}) == len(rows), "没有两个类别撞号")
check(by_name["英云闪长岩"]["label_code"] == "11",
      "库内类别号仍完整记在 label_code 里")

# 想用库内号的人写 {code} 仍然照旧
out_code = os.path.join(WORK, "labels_code")
io_shp.export_classes(store, out_code, driver_key="shp", level=1,
                      name_template="Label_{code}_{name}")
names = sorted(f for f in os.listdir(out_code) if f.endswith(".shp"))
check(any(n.startswith("Label_11_") for n in names),
      "{code} 模板仍写库内号：%s" % names[:3])
store.close()


# =============================================================================
# 10. 导出的文件不藏起来
# =============================================================================
print("[10] 输出文件不带前导点")

hidden = [f for f in os.listdir(out) if f.startswith(".")]
check(not hidden, "导出目录里没有点开头的隐藏文件：%s" % os.listdir(out)[:4])
check(os.path.isfile(os.path.join(out, "description.json")),
      "库描述文件叫 description.json")

# 老目录里的 .description.json 仍然读得出来
legacy_dir = os.path.join(WORK, "legacy")
os.makedirs(legacy_dir, exist_ok=True)
shutil.copy(os.path.join(out, "description.json"),
            os.path.join(legacy_dir, config.DESCRIPTION_JSON_LEGACY))
info = io_shp.read_description_json(legacy_dir)
check(info.get("_library", {}).get("name"), "带点的老描述文件照样认")

# 同一个目录里不会新旧两份并存
shutil.copy(os.path.join(out, "description.json"),
            os.path.join(out, config.DESCRIPTION_JSON_LEGACY))
store = SampleStore.open(os.path.join(WORK, "labels.gpkg"))
io_shp.export_classes(store, out, driver_key="shp", level=1)
check(not os.path.exists(os.path.join(out, config.DESCRIPTION_JSON_LEGACY)),
      "重导时把旧的点名描述文件清掉了")
store.close()


# =============================================================================
# 11. h5 样本集
# =============================================================================
print("[11] h5 样本集")

try:
    import h5py
except ImportError:
    h5py = None
    print("  - 没装 h5py，跳过")

if h5py is not None:
    from samplelib.modules.clip import clip as clip_mod

    img = os.path.join(WORK, "clip.tif")
    d = gdal.GetDriverByName("GTiff").Create(img, 200, 200, 4, gdal.GDT_UInt16)
    d.SetGeoTransform((100.0, 0.001, 0, 40.0, 0, -0.001))
    s4326 = osr.SpatialReference()
    s4326.ImportFromEPSG(4326)
    s4326.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    d.SetProjection(s4326.ExportToWkt())
    rnd = np.random.default_rng(0)
    for b in range(4):
        d.GetRasterBand(b + 1).WriteArray(
            rnd.integers(0, 4000, (200, 200)).astype(np.uint16))
    d = None

    store = new_store("clip.gpkg")
    for i, nm in enumerate(["花岗岩", "灰岩", "玄武岩"]):
        cid = store.add_class(name=nm, code=str(i + 1), region="R",
                              geom_kind=GEOM_POLYGON)
        x0, y0 = 100.01 + i * 0.03, 39.99 - i * 0.02
        for k in range(2):
            yy = y0 - k * 0.02
            store.insert_sample(cid, ogr.CreateGeometryFromWkt(
                "MULTIPOLYGON (((%f %f, %f %f, %f %f, %f %f, %f %f)))"
                % (x0, yy, x0 + 0.012, yy, x0 + 0.012, yy - 0.012,
                   x0, yy - 0.012, x0, yy)))

    oh, ob = os.path.join(WORK, "h5"), os.path.join(WORK, "bin")
    rh = clip_mod.clip_library(store, img, oh, patch_size=9, output_format="h5",
                               train_ratio=0.6, split_by="feature", seed=1)
    rb = clip_mod.clip_library(store, img, ob, patch_size=9, output_format="bin",
                               train_ratio=0.6, split_by="feature", seed=1)
    check([u.label for u in rh.units] == [0, 1, 2], "h5 标签号 0 起连续")

    with h5py.File(os.path.join(oh, "train_dataset.h5"), "r") as f:
        hp = np.asarray(f["patches"])
        hl = np.asarray(f["labels"])
        hc = np.asarray(f["centers"])
        hnames = [n.decode("utf-8") if isinstance(n, bytes) else str(n)
                  for n in f["label_names"][:]]
        attrs = dict(f.attrs)
    check(hp.shape[1:] == (4, 9, 9),
          "块形状 (波段, 边长, 边长) = %s" % (hp.shape[1:],))
    check(int(attrs["count"]) == hp.shape[0] == len(hl) == len(hc),
          "count / labels / centers 与 patches 条数一致：%d" % hp.shape[0])
    check(hnames == [u.name for u in rh.units],
          "label_names 按标签号顺序排：%s" % hnames)
    check(attrs["dtype"] == "uint16" and int(attrs["bands"]) == 4
          and attrs["layout"] == "NBHW", "属性齐全且与影像一致")

    listing = os.path.join(ob, "train_dataset", "datasets.txt")
    check(os.path.isfile(listing), "bin 的样本清单叫 datasets.txt，不带前导点")
    lines = [l for l in open(listing, encoding="utf-8").read().split("\n") if l]
    bp = np.stack([np.fromfile(l.rsplit(" ", 1)[0], dtype=np.uint16).reshape(4, 9, 9)
                   for l in lines])
    bl = np.array([int(l.rsplit(" ", 1)[1]) for l in lines])
    check(np.array_equal(hp, bp) and np.array_equal(hl, bl),
          "h5 与 bin 切出来的 %d 个块逐字节相同" % len(bl))

    # 测试集整个为空要说清楚
    st1 = SampleStore.create(os.path.join(WORK, "one.gpkg"), name="one",
                             overwrite=True)
    one = st1.add_class(name="独苗", code="1", region="R", geom_kind=GEOM_POLYGON)
    st1.insert_sample(one, ogr.CreateGeometryFromWkt(
        "MULTIPOLYGON (((100.01 39.99, 100.022 39.99, 100.022 39.978, "
        "100.01 39.978, 100.01 39.99)))"))
    r1 = clip_mod.clip_library(st1, img, os.path.join(WORK, "onlytrain"),
                               patch_size=9, output_format="h5",
                               train_ratio=0.6, split_by="feature", seed=1)
    check(any("测试集一个样本块都没有" in w for w in r1.warnings),
          "测试集为空时明确警告")
    st1.close()

    # 中途失败不留半个 h5
    boom_dir = os.path.join(WORK, "boom")
    real_pos = clip_mod._unit_positions
    hits = {"n": 0}

    def boom(*a, **kw):
        hits["n"] += 1
        if hits["n"] == 2:
            raise RuntimeError("模拟裁剪中途失败")
        return real_pos(*a, **kw)

    clip_mod._unit_positions = boom
    try:
        clip_mod.clip_library(store, img, boom_dir, patch_size=9,
                              output_format="h5", train_ratio=0.6, seed=1)
        blew = False
    except RuntimeError:
        blew = True
    clip_mod._unit_positions = real_pos
    check(blew, "裁剪中途失败会抛错")
    check(not os.path.exists(os.path.join(boom_dir, "train_dataset.h5")),
          "写了一半的 h5 被删掉，不会留一个读不出东西的空壳")
    store.close()


# =============================================================================
# 打包产物里找不到工具箱
# =============================================================================
# 冻结之后 toolbox 与 workers 在随包资源目录 resources/ 下，与源码布局不同；REPO_ROOT
# 若照源码那样往上数三级，「文件 → 工具箱…」点开永远是"找不到工具箱包"。

def _repo_root_of(fake_file, frozen):
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "samplelib", "toolbox_bridge.py")
    with open(src, encoding="utf-8") as fh:
        code = compile(fh.read(), fake_file, "exec")
    had = getattr(sys, "frozen", None)
    if frozen:
        sys.frozen = True
    elif had is not None:
        del sys.frozen
    try:
        namespace = {"__file__": fake_file, "__name__": "_tb_probe"}
        exec(code, namespace)
        return namespace["REPO_ROOT"]
    finally:
        if had is None:
            if getattr(sys, "frozen", None) is not None:
                del sys.frozen
        else:
            sys.frozen = had


_bundle = os.path.join(WORK, "LithoSpectra.app", "Contents", "Frameworks")
os.makedirs(os.path.join(_bundle, "samplelib"))
os.makedirs(os.path.join(_bundle, "resources", "toolbox"))
sys._MEIPASS = _bundle
try:
    _frozen_root = _repo_root_of(os.path.join(_bundle, "samplelib", "toolbox_bridge.pyc"), True)
finally:
    del sys._MEIPASS
check(_frozen_root == os.path.join(_bundle, "resources"),
      "冻结后 REPO_ROOT 指向随包资源目录，工具箱包就在它下面")

_source = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
check(_repo_root_of(os.path.join(_source, "LithoSpectra-Studio", "samplelib",
                                 "toolbox_bridge.py"), False) == _source,
      "源码运行时 REPO_ROOT 仍是仓库根")


# =============================================================================
# 状态文件里的坏条目让程序起不来
# =============================================================================
# 「最近打开」里混进 null（手工编辑、别的程序写坏）时，记住样本库那一步对 None 调
# normcase，启动直接崩。状态文件只记怎么看，坏了也必须照常打开。

from samplelib import appstate                                    # noqa: E402

_bad = {"library": None, "recent": [None, "", 3, "/x/a.gpkg"]}
check(appstate.recent(_bad) == ["/x/a.gpkg"], "最近列表跳过空值与非字符串条目")
check(appstate.last_library(_bad) == "", "上次样本库为 null 时当作没有")
check(appstate.recent({"recent": "oops"}) == [], "最近列表不是列表时当作空")


if not KEEP_WORKDIR:
    shutil.rmtree(WORK, ignore_errors=True)
print("\n%d 项检查全部通过" % len(PASSED))
