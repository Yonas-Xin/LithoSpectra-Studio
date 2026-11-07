# -*- coding: utf-8 -*-
"""Web 界面的后端接口：读库、暂存编辑、撤销重做、保存落库、库操作与暂存区同步。

需要 fastapi 与 httpx（TestClient 依赖它）；缺了就跳过。直接运行本文件。
"""

import json
import io
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi.testclient import TestClient
except ImportError:
    print("跳过：没有装 fastapi / httpx。")
    raise SystemExit(0)

from osgeo import ogr

from samplelib.session import LibrarySession
from webapi import create_app

WORK = tempfile.mkdtemp(prefix="slweb_")
FAILED = []


def check(cond, what):
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        FAILED.append(what)


def pt(x, y):
    g = ogr.Geometry(ogr.wkbPoint)
    g.AddPoint_2D(float(x), float(y))
    return g


def library():
    """分组 G -> 点类 P(2 点)；顶层面类 Q(空)。"""
    path = os.path.join(WORK, "web.gpkg")
    s = LibrarySession()
    s.create(path, name="web")
    st = s.store
    g = st.add_class("G", region="R", geom_kind="group")
    p = st.add_class("P", code="1", region="R", parent_id=g, geom_kind="point")
    q = st.add_class("Q", code="2", region="R", geom_kind="polygon")
    st.add_samples(p, [pt(100, 30), pt(100.1, 30.1)])
    s.close()
    return path, g, p, q


def wait(c, task):
    """轮询任务直到结束，返回最终状态。"""
    for _ in range(600):
        t = c.get("/api/tasks/%s" % task["id"]).json()
        if t["status"] != "running":
            return t
        time.sleep(0.05)
    raise TimeoutError(task["title"])


def feature_delta(c, p, q) -> None:
    """要素增量同步：页面带着快照号来取，只拿变了的；草稿被原地改动也要算变了。"""
    print("要素增量同步")

    def delta(token):
        r = c.get("/api/features", params={"since": token}).json()
        if r["type"] != "FeatureDelta":
            check(False, "带对得上的快照号却拿到了整批")
        return r

    full = c.get("/api/features").json()
    check(full["type"] == "FeatureCollection" and full["token"] > 0, "不带快照号拿整批")
    r = delta(full["token"])
    check(not r["upsert"] and not r["remove"], "没有改动时增量为空")
    square = {"type": "Polygon",
              "coordinates": [[[100, 30], [100.1, 30], [100.1, 30.1], [100, 30.1], [100, 30]]]}
    key = c.post("/api/features", json={"class_id": q, "geometry": square}).json()["key"]
    r = delta(r["token"])
    check([f["id"] for f in r["upsert"]] == [key] and not r["remove"], "新增草稿只回这一条")
    bigger = {"type": "Polygon",
              "coordinates": [[[100, 30], [100.3, 30], [100.3, 30.3], [100, 30.3], [100, 30]]]}
    c.put("/api/features/%s/geometry" % key, json={"geometry": bigger})
    r = delta(r["token"])
    check([f["id"] for f in r["upsert"]] == [key]
          and r["upsert"][0]["geometry"]["coordinates"][0][0][1] == [100.3, 30],
          "原地改了草稿的几何，增量里带着新几何")
    c.patch("/api/features/%s" % key, json={"label": "改了名"})
    r = delta(r["token"])
    check([f["properties"]["label"] for f in r["upsert"]] == ["改了名"], "改名也算变了")
    pt_key = next(f["id"] for f in full["features"] if f["properties"]["class_id"] == p)
    c.post("/api/features/delete", json={"keys": [pt_key]})
    r = delta(r["token"])
    check(r["remove"] == [pt_key] and not r["upsert"], "删除只回主键")
    c.post("/api/undo")
    r = delta(r["token"])
    check([f["id"] for f in r["upsert"]] == [pt_key] and not r["remove"], "撤销删除，那一条回来")
    c.post("/api/classes", json={"name": "增量测试", "region": "R"})
    r = delta(r["token"])
    check(not r["upsert"] and not r["remove"], "库操作（新建类别）后底稿重读，内容没变就不回")
    peek = c.get("/api/features", params={"peek": True}).json()
    check(peek["type"] == "FeatureCollection" and "token" not in peek, "peek 只看不记")
    check(delta(r["token"])["upsert"] == [], "peek 之后快照号仍然对得上")
    stale = c.get("/api/features", params={"since": 1}).json()
    check(stale["type"] == "FeatureCollection", "快照号对不上就回整批")
    c.post("/api/undo")                     # 新建类别
    c.post("/api/discard")


def io_roundtrip(c, g, p, q) -> None:
    print("勾选在后端")
    r = c.post("/api/selection", json={"op": "add", "class_ids": [g]}).json()
    check(len(r["keys"]) == 2, "勾分组 = 勾下面全部样本")
    by = {x["id"]: x for x in c.get("/api/classes").json()}
    check(by[g]["checked"] == "all" and by[q]["checked"] == "none", "类别带三态勾选")
    c.post("/api/selection", json={"op": "remove", "keys": [r["keys"][0]]})
    by = {x["id"]: x for x in c.get("/api/classes").json()}
    check(by[g]["checked"] == "partial", "去掉一条变成部分勾选")
    c.post("/api/selection", json={"op": "all"})

    print("导出（长任务）")
    out = os.path.join(WORK, "out")
    opts = c.get("/api/export/options").json()
    check(opts["selected"] == 3 and opts["point_classes"], "导出选项带勾选数与点类别")
    pre = c.post("/api/export/preview", json={"level": "leaf"}).json()
    check(pre["units"] == 2 and pre["samples"] == 3, "预告：两个单元三条样本")
    c.post("/api/classes/%d/export-level" % g, json={"mode": "merge"})
    pre = c.post("/api/export/preview", json={"level": "custom"}).json()
    check(pre["units"] == 2, "按树上标记：分组整支合并仍是两个单元（G 与 Q）")
    t = wait(c, c.post("/api/export", json={"out_dir": out, "level": "leaf"}).json())
    check(t["status"] == "done" and t["result"]["files"] == 2, "导出两个文件")
    shp = sorted(f for f in os.listdir(out) if f.endswith(".shp"))
    check(len(shp) == 2, "目录里有两个 shp：%s" % "、".join(shp))

    print("新建样本库并导入")
    new = os.path.join(WORK, "new.gpkg")
    r = c.post("/api/library/create", json={"path": new, "name": "新库"}).json()
    check(r["open"] and r["path"] == new, "新建并打开")
    t = wait(c, c.post("/api/import/dir", json={"path": out, "region": "R2"}).json())
    check(t["status"] == "done" and t["result"]["samples"] == 3, "导入目录：3 条样本")
    check(t["result"]["warnings"] == 0, "导出目录里的 description.json / labels.json 不再当矢量去开")
    names = sorted(x["name"] for x in c.get("/api/classes").json())
    check(len(names) == 2, "两个类别：%s" % "、".join(names))

    print("按属性导入")
    gj = os.path.join(WORK, "attr.geojson")
    feats = [{"type": "Feature", "properties": {"大类": big, "代号": code},
              "geometry": {"type": "Point", "coordinates": [100 + i * 0.01, 30]}}
             for i, (big, code) in enumerate([("火山岩", "T3a"), ("火山岩", "T3b"),
                                              ("沉积岩", "J1"), ("沉积岩", "J1")])]
    with open(gj, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh, ensure_ascii=False)
    t = wait(c, c.post("/api/import/profile", json={"path": gj}).json())
    fields = [f["name"] for f in t["result"]["fields"]]
    check(t["status"] == "done" and fields == ["大类", "代号"], "剖析出两个字段")
    pv = c.post("/api/import/attr/preview", json={"path": gj, "class_fields": ["大类", "代号"]}).json()
    check(pv["leaves"] == 3 and pv["groups"] == 2, "预览：3 个末级、2 个分组")
    t = wait(c, c.post("/api/import/attr", json={"path": gj, "class_fields": ["大类", "代号"],
                                                 "region": "属性工区"}).json())
    check(t["status"] == "done" and t["result"]["samples"] == 4, "按属性导入 4 条")

    print("备份与还原")
    st = c.get("/api/state").json()
    t = wait(c, c.post("/api/library/copy", json={"path": os.path.join(WORK, "bak.gpkg")}).json())
    check(t["status"] == "done" and t["result"]["samples"] == 7, "备份到：7 条，已核对")
    c.post("/api/classes/delete", json={"ids": [x["id"] for x in c.get("/api/classes").json()]})
    c.post("/api/save")
    check(len(c.get("/api/features").json()["features"]) == 0, "删光并保存")
    pr = c.post("/api/library/probe", json={"path": os.path.join(WORK, "bak.gpkg")}).json()
    check(pr["samples"] == 7, "核实备份：7 条")
    t = wait(c, c.post("/api/library/restore", json={"path": os.path.join(WORK, "bak.gpkg")}).json())
    check(t["status"] == "done" and len(c.get("/api/features").json()["features"]) == 7,
          "从备份还原回 7 条")
    check(any("before-rollback" in b["name"] for b in c.get("/api/library/backups").json()),
          "还原前自动留了快照")
    check(c.get("/api/state").json()["path"] == st["path"], "还原后仍是同一个库文件")


def tree_and_layers(c) -> None:
    print("树上整理类别")
    before = c.get("/api/classes").json()
    src = next(x for x in before if not x["is_group"] and x["count"] > 0)
    p = src["id"]
    r = c.post("/api/classes/paste", json={"ids": [p], "region": src["region"]}).json()
    check("得到 1 个新类别" in r["message"], "复制类别到工区顶层")
    names = [x["name"] for x in c.get("/api/classes").json()]
    check(len(names) == len(before) + 1 and names.count(src["name"]) == 1,
          "复制出来的同名类别按导入规矩改名")
    r = c.post("/api/classes/paste", json={"ids": [p], "target_id": p}).json()
    check("条样本复制进" in r["message"], "粘到基础类别上是复制样本")
    r = c.post("/api/regions/new", json={"region": "工作区甲", "name": "首类"}).json()
    check(r["id"] > 0, "新建工作区连同第一个类别")
    check(c.post("/api/regions/new", json={"region": "工作区甲", "name": "x"}).status_code == 400, "同名工作区被拒绝")
    c.post("/api/regions/rename", json={"region": "工作区甲", "name": "R3"})
    check("R3" in {x["region"] for x in c.get("/api/classes").json()}, "工区改名")
    n = c.get("/api/regions/R3/count").json()
    check(n["classes"] == 1, "工区统计")
    c.post("/api/regions/delete", json={"region": "R3"})
    check("R3" not in {x["region"] for x in c.get("/api/classes").json()}, "删工区走暂存")
    c.post("/api/undo")
    check("R3" in {x["region"] for x in c.get("/api/classes").json()}, "删工区可撤销")
    c.post("/api/discard")

    print("图层与范围")
    gj = os.path.join(WORK, "ref.geojson")
    with open(gj, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"岩性": "花岗岩"}, "geometry": {"type": "MultiPolygon", "coordinates": [
                [[[99.9, 29.9], [100.05, 29.9], [100.05, 30.05], [99.9, 30.05], [99.9, 29.9]]],
                [[[101, 31], [101.1, 31], [101.1, 31.1], [101, 31]]]]}},
            {"type": "Feature", "properties": {"岩性": "砂岩"}, "geometry": {"type": "Point", "coordinates": [100.1, 30.1]}}]},
            fh, ensure_ascii=False)
    r = c.post("/api/layers/add", json={"paths": [gj, os.path.join(WORK, "nope.xyz")]}).json()
    t = wait(c, r["task"])
    check(t["status"] == "done" and len(t["result"]["added"]) == 1 and len(t["result"]["failed"]) == 1,
          "加载参考矢量，认不出的文件报原因")
    lid = t["result"]["added"][0]["id"]
    g = c.get("/api/layers/%d/geojson" % lid).json()
    kinds = sorted(f["geometry"]["type"] for f in g["features"])
    check(kinds == ["MultiPolygon", "Point"] and len(g["features"][0]["geometry"]["coordinates"]) == 2,
          "多部件面按部件装回 MultiPolygon，不被画成挖洞")
    v = c.get("/api/layers/%d/values" % lid, params={"field": "岩性"}).json()
    check([x["value"] for x in v["values"]] == ["花岗岩", "砂岩"], "唯一值")
    c.post("/api/layers/%d/unique-colors" % lid, json={"field": "岩性", "colors": {"花岗岩": "#ff0000"}})
    g = c.get("/api/layers/%d/geojson" % lid).json()
    check(g["features"][0]["properties"]["c"] == "#ff0000", "唯一值赋色落到要素颜色上")
    attrs = c.get("/api/layers/%d/features/%d" % (lid, g["features"][0]["id"])).json()["attrs"]
    check(attrs.get("岩性") == "花岗岩", "要素属性")
    c.post("/api/selection", json={"op": "none"})
    r = c.post("/api/layers/%d/select-samples" % lid, json={"mode": "intersect"}).json()
    check(r["hits"] >= 1, "按参考矢量的范围筛样本：%d 条" % r["hits"])
    d = c.post("/api/layers/drawn").json()
    c.post("/api/layers/%d/polygons" % d["id"], json={"rings": [[[99, 29], [102, 29], [102, 32], [99, 32]]]})
    r = c.post("/api/layers/%d/select-samples" % d["id"], json={"mode": "within"}).json()
    check(r["selected"] == r["hits"] and r["hits"] >= 3, "手绘范围筛样本（完全包含）")
    out = os.path.join(WORK, "roi.geojson")
    check(c.post("/api/layers/%d/save" % d["id"], json={"path": out}).json()["count"] == 1
          and os.path.isfile(out), "范围导出成矢量")
    specs = c.get("/api/layers/specs").json()
    check({s["type"] for s in specs} == {"vector", "drawn"}, "图层写成可序列化描述")
    for l in c.get("/api/layers").json():
        c.delete("/api/layers/%d" % l["id"])
    r = c.post("/api/layers/restore", json={"specs": specs}).json()
    check(len(r["mapping"]) == 2 and not r["failed"], "按描述把图层铺回来")

    print("显示坐标系")
    m = c.get("/api/crs/menu", params={"lon": 100, "lat": 30}).json()
    check(m[0]["epsg"] == 32647 and m[0]["recommended"], "按视野中心推荐 UTM 带")
    d = c.get("/api/crs/32647").json()
    check("+proj=utm" in d["proj4"] and d["area"] and d["projected"], "投影定义与适用范围")
    check(c.get("/api/crs/999999").status_code == 400, "不认识的 EPSG 被拒绝")


def ui_state_roundtrip(path) -> None:
    """界面状态与「最近打开」写在数据目录下；测试时把数据目录换成临时目录。"""
    from samplelib import config
    print("界面状态与最近打开（临时数据目录）")
    tmp = tempfile.mkdtemp(prefix="slweb_state_")
    real = config.app_data_dir
    config.app_data_dir = lambda: tmp
    try:
        c = TestClient(create_app(path, remember=True))
        c.post("/api/ui-state", json={"state": {"library": path, "grid": False}})
        check(c.get("/api/ui-state").json().get("grid") is False, "界面状态存得下、读得回")
        recent = json.load(open(os.path.join(tmp, "state", "session.json"), encoding="utf-8"))["recent"]
        check(recent and os.path.samefile(recent[0], path), "打开的库记进最近打开")
        c.post("/api/geosymbols/recent", json={"items": ["T₃", "ηγ"]})
        check(c.get("/api/geosymbols").json()["recent"][:2] == ["T₃", "ηγ"], "最近用过的符号")
    finally:
        config.app_data_dir = real


def make_scene(path: str, bands: int = 60) -> list:
    """40×30 的 ENVI 高光谱小景，经纬度坐标，头文件带波长。返回波长表。"""
    import numpy as np
    from osgeo import gdal, osr
    waves = [float(w) for w in np.linspace(400.0, 2500.0, bands)]
    ds = gdal.GetDriverByName("ENVI").Create(path, 40, 30, bands, gdal.GDT_Int16)
    ds.SetGeoTransform((100.0, 0.001, 0.0, 30.03, 0.0, -0.001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    rows, cols = np.mgrid[0:30, 0:40]
    for i, w in enumerate(waves):
        arr = 2200 + 1200 * np.sin((w - 400) / 380.0) + 25 * cols + 15 * rows
        ds.GetRasterBand(i + 1).WriteArray(arr.astype(np.int16))
    ds = None
    with open(os.path.splitext(path)[0] + ".hdr", "a", encoding="utf-8") as fh:
        fh.write("wavelength units = Nanometers\n")
        fh.write("wavelength = {%s}\n" % ",".join("%.3f" % w for w in waves))
    return waves


def raster_display(path) -> None:
    """大图的显示金字塔（替身）与只画最新那张出图请求。替身写在临时数据目录里。"""
    import io as _io
    import threading
    import numpy as np
    from PIL import Image
    from osgeo import gdal, osr
    from samplelib import config
    from webapi import raster_cache
    print("大图显示：金字塔替身与过时出图请求")
    tmp = tempfile.mkdtemp(prefix="slweb_raster_")
    real = config.app_data_dir
    config.app_data_dir = lambda: tmp
    try:
        c = TestClient(create_app(path, remember=False))
        st = c.app.state.lithospectra
        # 5000 x 3000 的 RGBA PNG，左边一条透明；带世界文件与坐标系
        png = os.path.join(WORK, "plate.png")
        rgba = np.zeros((3000, 5000, 4), np.uint8)
        rgba[..., 0] = (np.arange(5000) % 251)[None, :]
        rgba[..., 1] = (np.arange(3000) % 241)[:, None]
        rgba[..., 2] = 120
        rgba[..., 3] = 255
        rgba[:, :600, 3] = 0
        Image.fromarray(rgba).save(png)
        with open(os.path.join(WORK, "plate.pgw"), "w") as fh:
            fh.write("0.0002\n0\n0\n-0.0002\n100\n30.6\n")
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        with open(png + ".aux.xml", "w") as fh:
            fh.write("<PAMDataset><SRS>%s</SRS></PAMDataset>" % srs.ExportToWkt())
        t = wait(c, c.post("/api/layers/add", json={"paths": [png]}).json()["task"])
        info = t["result"]["added"][0]
        lid, ext = info["id"], info["extent"]
        layer = st.layers[lid]
        check(raster_cache.needs_stand_in(layer), "不能随机读取的大 PNG 要建替身")

        def wms(bounds, w=600, h=360):
            r = c.get("/api/layers/%d/wms" % lid, params={
                "BBOX": ",".join(map(str, bounds)), "WIDTH": w, "HEIGHT": h, "SRS": "EPSG:4326"})
            if r.status_code != 200:
                return r.status_code, None
            return 200, np.asarray(Image.open(_io.BytesIO(r.content)).convert("RGBA"))

        cx, cy = (ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2
        near = (cx - 0.02, cy - 0.012, cx + 0.02, cy + 0.012)
        _, before = wms(near)
        _, whole = wms(ext)
        check(whole[180, 20, 3] == 0 and whole[180, 400, 3] == 255,
              "PNG 的透明区画出来仍是透明的（alpha 没被挑波段丢掉）")
        for _ in range(600):
            if st.stand_ins.progress.get(lid) is None:
                break
            time.sleep(0.05)
        check(layer.display_key() in layer._stand_ins, "替身建好并登记给图层")
        files = os.listdir(raster_cache.cache_dir())
        check(len(files) == 1 and files[0].endswith(".tif"), "替身写在数据目录的 rastercache 下")
        _, after = wms(near)
        check(np.abs(before.astype(int) - after).mean() < 1.0, "放大处替身与原图出图一致")
        _, whole2 = wms(ext)
        check(whole2[180, 20, 3] == 0, "替身同样保留透明区")
        # 换显示波段：另建一份
        c.patch("/api/layers/%d" % lid, json={"bands": [3, 2, 1]})
        for _ in range(600):
            if st.stand_ins.progress.get(lid) is None:
                break
            time.sleep(0.05)
        check(len(os.listdir(raster_cache.cache_dir())) == 2, "换了显示波段另建一份替身")
        # 同一时刻连发：只有排在最后的那张真的画
        codes = {}

        def go(i):
            codes[i] = wms((ext[0] + i * 0.001, ext[1], ext[2], ext[3]), 900, 600)[0]
        threads = [threading.Thread(target=go, args=(i,)) for i in range(5)]
        for th in threads:
            th.start()
            time.sleep(0.005)
        for th in threads:
            th.join()
        check(codes[4] == 200 and list(codes.values()).count(204) >= 1,
              "连发出图请求时过时的直接放弃（%s）" % [codes[i] for i in range(5)])
        c.delete("/api/layers/%d" % lid)
        check(lid not in st.stand_ins.progress, "移除图层时撤掉它的替身任务")
    finally:
        config.app_data_dir = real


def spectra_and_toolbox(path) -> None:
    """取谱、存进「我的光谱」、改名挪分类删除，再用工具箱真跑一批去水汽。

    「我的光谱」和最近打开都落在数据目录下，这里把数据目录换成临时目录。
    """
    from samplelib import config
    from samplelib.modules.spectra import speclib
    tmp = tempfile.mkdtemp(prefix="slweb_spec_")
    real, real_user = config.app_data_dir, speclib._USER
    config.app_data_dir = lambda: tmp
    speclib._USER = None
    try:
        c = TestClient(create_app(path, remember=True))
        scene = os.path.join(WORK, "scene.dat")
        waves = make_scene(scene)

        print("取谱")
        t = wait(c, c.post("/api/layers/add", json={"paths": [scene]}).json()["task"])
        lid = t["result"]["added"][0]["id"]
        s = c.post("/api/spectra/bind", json={"layer_id": lid}).json()
        check(s["layer_id"] == lid and s["x_options"]["wave"], "曲线窗口绑到影像，有波长轴")
        s = c.post("/api/spectra/pick", json={"lon": 100.005, "lat": 30.025}).json()
        check(len(s["curves"]) == 1 and s["curves"][0]["live"]
              and s["curves"][0]["valid"] == len(waves), "单击读到一条完整的谱")
        s = c.post("/api/spectra/pick", json={"lon": 100.03, "lat": 30.01, "append": True}).json()
        check(len(s["curves"]) == 1 and not s["curves"][0]["live"], "Shift 单击把这次读到的谱留在图上")
        s = c.post("/api/spectra/pick", json={"lon": 100.02, "lat": 30.02}).json()
        check(len(s["curves"]) == 2 and len(s["marks"]) == 2, "再单击一条，留下的那条不被替换")
        r = c.post("/api/spectra/pick", json={"lon": 105, "lat": 35}).json()
        check("不在" in r["message"], "影像范围外单击给出提示")
        plot = c.get("/api/spectra/plot", params={"width": 800, "height": 400}).json()
        check(plot["png"] and len(plot["series"]) == 2 and plot["xlim"][0] < plot["xlim"][1],
              "出图并带回坐标轴供读数")
        import base64 as _b64
        from PIL import Image as _Im
        w1 = _Im.open(io.BytesIO(_b64.b64decode(plot["png"]))).width
        hi = c.get("/api/spectra/plot", params={"width": 800, "height": 400, "scale": 2}).json()
        w2 = _Im.open(io.BytesIO(_b64.b64decode(hi["png"]))).width
        check(abs(w2 - 2 * w1) <= 2, "高分屏按像素比出图，曲线不糊（%d → %d 像素）" % (w1, w2))
        csv = os.path.join(WORK, "curves.csv")
        c.post("/api/spectra/export", json={"path": csv, "kind": "csv"})
        check(os.path.isfile(csv), "曲线导出成 CSV")

        print("光谱库分类计数与全库匹配")
        tree = c.get("/api/speclib/tree").json()
        everything = c.post("/api/speclib/search", json={"limit": 1000000}).json()
        check(tree["total"] == len(everything) > 3000,
              "「全部」的条数是现算的，与列出来的一致：%d" % tree["total"])
        for cat in tree["categories"][:12]:
            got = len(c.post("/api/speclib/search", json={"category": cat["path"], "limit": 1000000}).json())
            check(cat["count"] == got, "分类「%s」显示 %d 条，点下去列出 %d 条" % (cat["cn"], cat["count"], got))
        g = tree["groups"][0]
        got = len(c.post("/api/speclib/search", json={"group": g["key"], "limit": 1000000}).json())
        check(g["count"] == got and got > 0, "蚀变组合「%s」也带条数：%d" % (g.get("cn") or g["key"], got))
        check(0 < tree["grouped"] <= tree["total"], "「蚀变组合」这一级也带条数：%d" % tree["grouped"])
        ref = next(x for x in everything if x["cls"].startswith("mineral") and x["count"] > 400
                   and x["wl"][0] < 500 and x["wl"][1] > 2400)
        board = c.post("/api/speclib/add-to-plot", json={"ids": [ref["id"]]}).json()
        cid = next(x["id"] for x in board["curves"] if x["origin"] == "library")
        t = wait(c, c.post("/api/speclib/match", json={"curve_id": cid}).json()["task"])
        res = t.get("result") or {}
        check(t["status"] == "done" and res.get("compared") == len(everything),
              "不给条目就按检索条件比全库：比了 %s 条（不再卡在 3000）" % res.get("compared"))
        check(res["hits"][0]["id"] == ref["id"], "第一名是它自己")
        check(res["full_cover"] and "skipped_cover" in res and res["tolerance"] >= 20,
              "默认只看完整覆盖的库谱，挡掉 %s 条，端点容差 %s nm" % (res.get("skipped_cover"), res.get("tolerance")))
        check(all(h["lo"] is not None and 0 < h["coverage"] <= 100 for h in res["hits"]),
              "每条命中带着实际比较的波段与覆盖率")
        t = wait(c, c.post("/api/speclib/match", json={"curve_id": cid, "full_cover": False,
                                                       "span": [2000, 2400]}).json()["task"])
        part = t.get("result") or {}
        check(t["status"] == "done" and part.get("skipped_cover") == 0
              and part["lo"] >= 2000 and part["hi"] <= 2400,
              "关掉完整覆盖、限定 2000–2400 nm：比较范围 %s–%s" % (part.get("lo"), part.get("hi")))

        print("我的光谱（临时数据目录）")
        ids = [x["id"] for x in s["curves"]]
        r = c.post("/api/speclib/user/save", json={"curve_ids": ids, "folder": "野外/甲",
                                                   "names": {str(ids[0]): "样点一"}}).json()
        check(r.get("saved") == 2 and os.path.isfile(os.path.join(tmp, speclib.USER_LIBRARY_FILE)),
              "两条谱存进「我的光谱」，文件落在临时数据目录")
        tree = c.get("/api/speclib/tree").json()
        check("野外/甲" in tree["user_folders"], "存的时候建好了分类")
        ut = {x["path"]: x for x in tree["user_tree"]}
        check(ut.get("野外", {}).get("count") == 2 and ut.get("野外/甲", {}).get("name") == "甲",
              "我的光谱分类逐级展开，父分类计入子分类的谱")
        check(not any(cat["path"].startswith("野外") for cat in tree["categories"]),
              "我的光谱不混进随包库的分类")
        rows = c.post("/api/speclib/search", json={"library": "user"}).json()
        check(len(rows) == 2 and "样点一" in {x["name"] for x in rows}, "能搜到存进去的谱")
        eid = next(x["id"] for x in rows if x["name"] == "样点一")
        c.patch("/api/speclib/user/entries/%s" % eid, json={"name": "样点 A", "note": "蚀变带"})
        e = c.get("/api/speclib/entries/%s" % eid).json()
        check(e["name"] == "样点 A" and e["note"] == "蚀变带" and len(e["y"]) == len(waves),
              "改名、写备注，数值完整")
        c.post("/api/speclib/user/folders", json={"path": "室内"})
        r = c.post("/api/speclib/user/move", json={"ids": [eid], "folder": "室内"}).json()
        check(r["moved"] == 1 and c.get("/api/speclib/entries/%s" % eid).json()["cls"] == "室内",
              "挪到另一个分类")
        r = c.post("/api/speclib/user/folders/rename", json={"path": "室内", "name": "实验室"}).json()
        check(r["path"] == "实验室" and c.get("/api/speclib/entries/%s" % eid).json()["cls"] == "实验室",
              "分类改名，条目跟着走")
        out = os.path.join(WORK, "mine.csv")
        r = c.post("/api/speclib/export", json={"ids": [x["id"] for x in rows], "kind": "csv", "path": out})
        check(r.status_code == 200 and os.path.isfile(out), "库里的谱导出")

        c.post("/api/spectra/clear")
        r = c.post("/api/speclib/add-to-plot", json={"ids": [eid]}).json()
        check(r["added"] == 1 and r["curves"][0]["origin"] != "image", "库里的谱加回图上当参照")
        r = c.post("/api/speclib/user/delete", json={"ids": [x["id"] for x in rows]}).json()
        check(r["removed"] == 2 and not c.post("/api/speclib/search", json={"library": "user"}).json(),
              "删除")
        r = c.post("/api/speclib/user/import", json={"path": csv}).json()
        check(r.get("added") == 2, "曲线 CSV 读进「我的光谱」")
        print("我的光谱：分类管理")
        cls_of = lambda i: c.get("/api/speclib/entries/%s" % i).json()["cls"]   # noqa: E731
        mine = [x["id"] for x in c.post("/api/speclib/search", json={"library": "user"}).json()]
        eid, other = mine[0], mine[1]
        c.post("/api/speclib/user/move", json={"ids": [eid], "folder": "实验室"})
        c.post("/api/speclib/user/move", json={"ids": [other], "folder": "实验室/甲组"})
        r = c.post("/api/speclib/user/folders/move", json={"path": "实验室", "parent": "深地专项"}).json()
        check(r["path"] == "深地专项/实验室" and cls_of(eid) == "深地专项/实验室"
              and cls_of(other) == "深地专项/实验室/甲组", "整枝挪分类：子分类与光谱跟着走，层级不变")
        r = c.post("/api/speclib/user/folders/move", json={"path": "深地专项", "parent": "深地专项/实验室"})
        check(r.status_code >= 400, "不能把分类挪进它自己的子分类")
        u = c.get("/api/speclib/user/folders/usage", params={"path": "深地专项/实验室"}).json()
        check(u["primary"] == 2 and u["subfolders"] == 1, "分类用量：2 条光谱、1 个子分类")
        r = c.post("/api/speclib/user/folders/delete", json={"path": "深地专项/实验室"})
        check(r.status_code >= 400, "不选处理方式时，不删有光谱的分类")
        c.post("/api/speclib/user/folders/delete", json={"path": "深地专项/实验室", "mode": "move"})
        tree = c.get("/api/speclib/tree").json()
        check(cls_of(eid) == "深地专项" and cls_of(other) == "深地专项"
              and not any(f["path"].startswith("深地专项/实验室") for f in tree["user_tree"]),
              "删除分类、光谱移到上一级")
        c.post("/api/speclib/user/folders/delete", json={"path": "深地专项", "mode": "delete"})
        check(not c.post("/api/speclib/search", json={"library": "user"}).json(), "删除分类连同光谱")

        print("工具箱真跑一批（去水汽波段）")
        tool = next(x for x in c.get("/api/toolbox/tools").json() if x["id"] == "remove_water_vapor")
        check(tool["input_kind"] == "raster", "工具描述带输入类型")
        check(any(x["path"] == scene for x in c.get("/api/toolbox/rasters").json()),
              "画布里的影像可直接当输入")
        out_dir = os.path.join(WORK, "tb_out")
        run = c.post("/api/toolbox/run", json={"tool": "remove_water_vapor", "python": sys.executable,
                                                "inputs": [scene], "output_dir": out_dir}).json()
        state = {}
        for _ in range(1200):
            state = c.get("/api/toolbox/runs/%s" % run["id"]).json()
            if state["done"]:
                break
            time.sleep(0.1)
        log = "\n".join(x["t"] for x in state.get("lines", []))
        ok = state.get("done") and state.get("code") == 0
        check(ok, "子进程跑完且返回 0" + ("" if ok else "：\n" + log[-800:]))
        outs = [f for f in os.listdir(out_dir) if f.endswith(".dat")] if os.path.isdir(out_dir) else []
        check(len(outs) == 1, "输出目录里有结果影像")
        if outs:
            from osgeo import gdal
            ds = gdal.Open(os.path.join(out_dir, outs[0]))
            n = ds.RasterCount if ds else -1
            check(0 < n < len(waves), "水汽波段被去掉：%d → %d" % (len(waves), n))
            ds = None
    finally:
        config.app_data_dir = real
        speclib._USER = real_user


def main() -> int:
    path, g, p, q = library()
    c = TestClient(create_app(path, remember=False))

    print("读库")
    classes = {x["id"]: x for x in c.get("/api/classes").json()}
    check(classes[g]["total"] == 2 and classes[p]["count"] == 2, "分组样本数向上累加")
    fc = c.get("/api/features").json()
    check(len(fc["features"]) == 2, "要素以 GeoJSON 返回")

    print("暂存编辑")
    square = {"type": "Polygon",
              "coordinates": [[[100, 30], [100.2, 30], [100.2, 30.2], [100, 30.2], [100, 30]]]}
    r = c.post("/api/features", json={"class_id": p, "geometry": square})
    check(r.status_code == 400, "点类别里画面被拒绝")
    key = c.post("/api/features", json={"class_id": q, "geometry": square}).json()["key"]
    check(key.startswith("polygon:-"), "草稿要素拿到负数主键")
    f = c.get("/api/features/%s" % key).json()
    ring = f["geometry"]["coordinates"][0][0]
    check(f["geometry"]["type"] == "MultiPolygon" and ring[0] == ring[-1], "面按 MultiPolygon 返回且环闭合")
    check(c.get("/api/state").json()["dirty"], "暂存区变脏")

    print("库操作与暂存区同步")
    new_id = c.post("/api/classes", json={"name": "N", "region": "R"}).json()["id"]
    names = [x["name"] for x in c.get("/api/classes").json()]
    check("N" in names, "新建类别立即可见")
    check(c.get("/api/features/%s" % key).status_code == 200, "库被改过之后草稿仍在")

    print("拖到基础类别上：两者升一级")
    t = c.post("/api/classes", json={"name": "T", "region": "R"}).json()["id"]
    gid = c.post("/api/classes/%d/promote" % q, json={"other_id": t, "name": "QT"}).json()["id"]
    by = {x["id"]: x for x in c.get("/api/classes").json()}
    check(by[gid]["is_group"] and by[q]["parent_id"] == gid and by[t]["parent_id"] == gid,
          "新分组装下了两个类别")
    check(c.post("/api/undo").json()["label"] != "", "升级可撤销")
    c.post("/api/undo")                     # 撤掉新建的 T

    print("撤销 / 重做按发生顺序")
    check(c.post("/api/undo").json()["label"].startswith("新建类别"), "先撤销后发生的库操作")
    check(new_id not in [x["id"] for x in c.get("/api/classes").json()], "新建的类别已撤掉")
    check(c.post("/api/undo").json()["label"].startswith("新增面"), "再撤销更早的暂存编辑")
    check(c.post("/api/redo").json()["label"].startswith("新增面"), "重做找回草稿")

    print("保存")
    c.patch("/api/features/%s" % key, json={"label": "野外面 1"})
    r = c.post("/api/save").json()
    check(r["written"] >= 1 and not r["dirty"], "保存后暂存区清空")
    labels = [x["properties"]["label"] for x in c.get("/api/features").json()["features"]]
    check("野外面 1" in labels, "保存的要素带着名称读回来")
    s = LibrarySession()
    s.open(path)
    check(any(r.label == "野外面 1" for r in s.records), "确实写进了 GeoPackage")
    s.close()

    print("删除类别走暂存，可撤销")
    c.post("/api/classes/delete", json={"ids": [g]})
    check(len(c.get("/api/features").json()["features"]) == 1, "分组连同样本一起隐去")
    c.post("/api/undo")
    check(len(c.get("/api/features").json()["features"]) == 3, "撤销后都回来了")

    feature_delta(c, p, q)
    io_roundtrip(c, g, p, q)
    tree_and_layers(c)
    ui_state_roundtrip(c.get("/api/state").json()["path"])
    spectra_and_toolbox(c.get("/api/state").json()["path"])
    raster_display(c.get("/api/state").json()["path"])

    print("\n%s" % ("全部通过" if not FAILED else "失败 %d 项" % len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
