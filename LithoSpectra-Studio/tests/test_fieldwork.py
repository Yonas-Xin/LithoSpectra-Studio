# -*- coding: utf-8 -*-
"""野外记录模块：奥维解析、存储与导入、照片 EXIF、奥维水印、地质水印、光谱库链接、接口。

奥维文件的用例要用真实数据（``OVOBJ_SAMPLES`` 里的文件），找不到就跳过那几项；
水印用例自己合成一张带奥维式水印的照片，OCR 需要 macOS 与 ocrmac，缺了也跳过。
直接运行本文件。
"""

# =============================================================================
# 配置
# =============================================================================

#: 真实的奥维收藏夹与同名 GeoJSON 导出（奥维里「导出为 GeoJSON」），逐点核对用
OVOBJ_SAMPLES = [
    "tests/fixtures/private/20260819.ovobj",
    "tests/fixtures/private/20260817.ovobj",
]
#: 只导出一个标签的奥维文件，带照片、视频（MOV）、录音（奥维 Speex），核对单对象布局与录音解码
OVOBJ_SINGLE = "tests/fixtures/private/single.ovobj"

# =============================================================================

import io
import json
import os
import shutil
import struct
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from samplelib.modules.fieldwork import (
    geomark, importer, inpaint, ovmark, ovobj, photoexif, spectra_link,
)
from samplelib.modules.fieldwork.fieldstore import FieldStore
from samplelib.session import LibrarySession

WORK = tempfile.mkdtemp(prefix="slfw_")
FAILED = []


def check(cond, what):
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        FAILED.append(what)


def samples():
    return [p for p in OVOBJ_SAMPLES if os.path.isfile(p)]


# =============================================================================
# 奥维解析
# =============================================================================

def test_ovobj() -> None:
    print("奥维 .ovobj 解析")
    found = samples()
    if not found:
        print("  - 跳过：找不到样例文件")
        return
    for path in found:
        f = ovobj.read(path)
        name = os.path.basename(path)
        check(not f.warnings, "%s 没有警告" % name)
        refs = [h for p in f.placemarks for h in p.attachments]
        check(all(h in f.attachments for h in refs), "%s 的照片引用全部对得上" % name)
        gj_path = os.path.splitext(path)[0] + ".geojson"
        if not os.path.isfile(gj_path):
            continue
        gj = json.load(open(gj_path, encoding="utf-8"))
        pts = [x for x in gj["features"] if x["geometry"]["type"] == "Point"]
        lines = [x for x in gj["features"] if x["geometry"]["type"] == "LineString"]
        check(len(pts) == len(f.placemarks) and all(
            a.name == b["properties"]["name"]
            and abs(a.lon - b["geometry"]["coordinates"][0]) < 1e-9
            and abs(a.lat - b["geometry"]["coordinates"][1]) < 1e-9
            for a, b in zip(f.placemarks, pts)), "%s 点位与 GeoJSON 逐个一致" % name)
        check(len(lines) == len(f.tracks) and all(
            len(t.lons) == len(l["geometry"]["coordinates"])
            and max(max(abs(x - c[0]), abs(y - c[1]))
                    for x, y, c in zip(t.lons, t.lats, l["geometry"]["coordinates"])) < 5e-9
            for t, l in zip(f.tracks, lines)), "%s 轨迹与 GeoJSON 逐点一致" % name)
        for t in f.tracks:
            check(len(t.times) == len(t.lons) == len(t.elevations), "轨迹时间、高程与坐标等长")
            check(all(b >= a for a, b in zip(t.times, t.times[1:])), "轨迹时间单调不减")
            check(0 < min(t.elevations) <= max(t.elevations) < 9000, "高程在合理范围")
    check(not ovobj.is_ovobj(__file__), "非奥维文件认得出来")


# =============================================================================
# 存储与导入
# =============================================================================

def library(name: str):
    s = LibrarySession()
    s.create(os.path.join(WORK, name), name=name)
    return s


def library_open(name: str):
    s = LibrarySession()
    s.open(os.path.join(WORK, name))
    return s


def jpeg_with_gps(size=(1200, 900), lon=95.79, lat=36.31, alt=3016.0) -> bytes:
    rng = np.random.default_rng(1)
    arr = (rng.random((size[1], size[0], 3)) * 120 + 60).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=90)
    return photoexif.write_tags(buf.getvalue(), lon=lon, lat=lat, altitude=alt)


def test_store() -> None:
    print("存储：fw_* 表与样本分开")
    s = library("store.gpkg")
    fs = FieldStore(s.store)
    a = fs.add_point(100.0, 30.0, name="P1", survey="S")
    b = fs.add_point(100.001, 30.0, name="P2", survey="S")
    check(s.store.total_samples() == 0, "野外点位不进样本表")
    check(len(fs.list_points()) == 2, "两个点位")
    fs.update_point(a, {"name": "P1'", "attrs": {"岩性": "花岗岩"}, "lon": 100.1, "lat": 30.1})
    p = fs.get_point(a)
    check(p["name"] == "P1'" and p["attrs"] == {"岩性": "花岗岩"} and abs(p["lon"] - 100.1) < 1e-12,
          "改名、自定义属性、挪位置")
    t = fs.add_track([100, 100.01, 100.02], [30, 30, 30], [1_790_000_000, 1_790_000_010, 1_790_000_030],
                     [3000, 3010, 3005], name="T", survey="S")
    tr = fs.get_track(t)
    check(tr["n_points"] == 3 and tr["elev_max"] == 3010 and abs(tr["length_m"] - 1927) < 5,
          "轨迹点数、高程、长度")
    check(tr["coords"][1][3] == 1_790_000_010, "轨迹点的 M 值是时间")
    check(fs.elevation_at(100.0101, 30.0) == 3010, "按位置从轨迹估高程")
    data = jpeg_with_gps()
    rel, sha = fs.put_media("photos", data, "jpg")
    rel2, sha2 = fs.put_media("photos", data, "jpg")
    check(rel == rel2 and os.path.isfile(fs.media_path(rel)), "同一份照片只存一份")
    ph = fs.add_photo(rel, sha, photoexif.read_info(data), point_uid=a)
    fs.add_photo(rel, sha, photoexif.read_info(data), point_uid=b)
    check(fs.get_point(a)["n_photos"] == 1, "点位带照片数")
    fs.move_photo(ph, b, 0)
    check([x["uid"] for x in fs.list_photos(point_uid=b)][0] == ph, "照片挪到另一个点位的最前面")
    check(fs.surveys()[0]["points"] == 2 and fs.surveys()[0]["tracks"] == 1, "批次统计")
    check(fs.delete_point(b, keep_photos=True) and len(fs.list_photos(unattached=True)) == 2,
          "删点位时照片留下成为独立照片")
    try:
        fs.media_path("../../etc/passwd")
        check(False, "媒体路径不能越界")
    except ValueError:
        check(True, "媒体路径不能越界")
    cid = s.store.add_class("花岗岩", region="R", geom_kind="point")
    key = importer.copy_point_to_sample(fs, a, cid)
    check(s.store.total_samples() == 1 and fs.get_point(a)["sample_key"] == key,
          "显式复制为点样本，点位记下样本键")
    s.close()
    s = LibrarySession()
    s.open(os.path.join(WORK, "store.gpkg"))
    check(FieldStore(s.store).stats()["fw_point"] == 1, "重新打开库，野外记录还在")
    s.close()

    print("范围：没有坐标的照片不算")
    s = library("extent.gpkg")
    fs = FieldStore(s.store)
    fs.add_point(100.0, 30.0, name="E")
    plain = io.BytesIO()
    Image.new("RGB", (64, 48), (90, 90, 90)).save(plain, "JPEG")
    rel, sha = fs.put_media("photos", plain.getvalue(), "jpg")
    fs.add_photo(rel, sha, photoexif.read_info(plain.getvalue()))
    check(fs.extent() == [100.0, 30.0, 100.0, 30.0], "没有 GPS 的照片不把范围拉到经纬度原点")
    s.close()

    print("删除野外批次")
    s = library("survey.gpkg")
    fs = FieldStore(s.store)
    p1 = fs.add_point(100.0, 30.0, name="Q1", survey="甲")
    fs.add_point(100.1, 30.0, name="Q2", survey="甲")
    keep = fs.add_point(100.2, 30.0, name="R1", survey="乙")
    fs.add_track([100, 100.01], [30, 30], name="T", survey="甲")
    data = jpeg_with_gps()
    rel, sha = fs.put_media("photos", data, "jpg")
    fs.add_photo(rel, sha, photoexif.read_info(data), point_uid=p1)
    r = fs.delete_survey("甲", keep_photos=True)
    check(r == {"points": 2, "tracks": 1, "photos": 1, "attachments": 0}, "删除批次：点位、轨迹一并删掉（%s）" % r)
    check([p["uid"] for p in fs.list_points()] == [keep] and not fs.list_tracks(),
          "别的批次不受影响")
    check(len(fs.list_photos(unattached=True)) == 1, "保留照片时照片成为独立照片")
    fs.add_point(100.3, 30.0, name="无批次")
    check(fs.delete_survey("")["points"] == 1 and len(fs.list_points()) == 1, "可删除「未分批」的点位")
    s.close()

    print("ENVI 光谱库：GBK 头文件、无扩展名的数据文件")
    from samplelib.modules.spectra import speclib_io
    d = os.path.join(WORK, "envi")
    os.makedirs(d, exist_ok=True)
    np.array([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]], dtype="<f4").tofile(os.path.join(d, "白云母muscovite"))
    hdr = ("ENVI\nsamples = 3\nlines = 2\nbands = 1\nheader offset = 0\nfile type = ENVI Spectral Library\n"
           "data type = 4\nbyte order = 0\nwavelength units = Micrometers\n"
           "spectra names = {白云母muscovite, 高岭石kaolini}\nwavelength = {0.5, 1.0, 2.0}\n")
    with open(os.path.join(d, "白云母muscovite.hdr"), "wb") as fh:
        fh.write(hdr.encode("gbk"))
    got = speclib_io.read_any(os.path.join(d, "白云母muscovite"))
    check([x.label for x in got.spectra] == ["白云母muscovite", "高岭石kaolini"] and got.spectra[0].x[-1] == 2000,
          "GBK 编码的谱名不乱码，没有扩展名的数据文件也能读")

    print("拖入文件：按文件名换路径")
    from webview.dom import _dnd_state
    from webapi.desktop_app import DesktopApi, enable_drop_paths
    enable_drop_paths()
    check(_dnd_state["num_listeners"] >= 1, "原生层会记下拖入文件的路径")
    _dnd_state["paths"] += [("a%20b.tif", "/x/a%20b.tif"), ("c.shp", "/y/c.shp")]
    got = DesktopApi().dropped_paths(["a b.tif", "c.shp", "missing.tif"])
    check(got == ["/x/a b.tif", "/y/c.shp"] and not _dnd_state["paths"], "取到路径并从队列里拿掉")


def test_locked_meta() -> None:
    print("原片自带的照片信息锁定")
    s = library("locked.gpkg")
    fs = FieldStore(s.store)
    data = photoexif.write_tags(jpeg_with_gps(), azimuth=120.0)
    rel, sha = fs.put_media("photo", data, ".jpg")
    uid = fs.add_photo(rel, sha, photoexif.read_info(data))
    ph = fs.get_photo(uid)
    check(set(ph["locked"]) == {"position", "altitude", "azimuth"}, "原片有的位置、海拔、朝向都锁定")
    ph = fs.update_photo(uid, {"azimuth": 10, "caption": "近景"})
    check(ph["azimuth"] == 120 and ph["caption"] == "近景", "锁定的朝向改不动，说明照常可写")
    fs.set_photo_position(uid, 1.0, 2.0, 5.0)
    ph = fs.get_photo(uid)
    check(abs(ph["lon"] - 95.79) < 1e-6 and ph["altitude"] == 3016.0, "原片自带的位置、海拔不被覆盖")
    # 早先版本的库：没有 orig_meta，朝向被改过 → 重新打开时按原片恢复
    fs._update("fw_photo", uid, {"orig_meta": None, "azimuth": 33.0, "azimuth_src": "manual"})
    s.close()
    s = LibrarySession()
    s.open(os.path.join(WORK, "locked.gpkg"))
    ph = FieldStore(s.store).get_photo(uid)
    check(ph["azimuth"] == 120 and ph["azimuth_src"] == "exif" and "azimuth" in ph["locked"],
          "旧库补记原片信息，被改过的朝向恢复成原片的值")
    s.close()


def test_import() -> None:
    print("导入")
    found = samples()
    s = library("import.gpkg")
    fs = FieldStore(s.store)
    if found:
        path = found[0]
        f = ovobj.read(path)
        r = importer.import_ovobj(fs, path)
        check(r["points_new"] == len(f.placemarks) and r["tracks_new"] == len(f.tracks), "奥维：点位、轨迹都进来了")
        check(r["photos_new"] == sum(len(p.attachments) for p in f.placemarks), "奥维：照片都进来了")
        r2 = importer.import_ovobj(fs, path)
        check(r2["points_new"] == 0 and r2["points_updated"] == 0 and r2["photos_new"] == 0
              and r2["tracks_skipped"] == len(f.tracks), "再导一次不重复、不改动")
        check(importer.preview_ovobj(path, fs)["imported_before"] is not None, "预览认得出导过")
    else:
        print("  - 跳过奥维导入：找不到样例文件")
    folder = os.path.join(WORK, "photos")
    os.makedirs(folder)
    near = jpeg_with_gps(lon=100.0, lat=30.0)
    far = jpeg_with_gps(lon=101.0, lat=31.0)
    open(os.path.join(folder, "near.jpg"), "wb").write(near)
    open(os.path.join(folder, "far.jpg"), "wb").write(far)
    pid = fs.add_point(100.0001, 30.0, name="附近的点")
    r = importer.import_photos(fs, [os.path.join(folder, n) for n in ("near.jpg", "far.jpg")], mode="nearest")
    check(r["photos_new"] == 2 and r["attached"] == 1, "按距离挂到最近点位，远的成为独立照片")
    check(len(fs.list_photos(point_uid=pid)) == 1, "近的那张挂上了")
    gpx = os.path.join(WORK, "t.gpx")
    open(gpx, "w").write('<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">'
                         '<wpt lat="30.5" lon="100.5"><ele>2980</ele><name>W1</name><desc>说明</desc></wpt>'
                         '<trk><name>TR</name><trkseg><trkpt lat="30" lon="100"><ele>3000</ele><time>2026-08-01T02:00:00Z</time></trkpt>'
                         '<trkpt lat="30.01" lon="100.01"><ele>3050</ele><time>2026-08-01T02:30:00Z</time></trkpt></trkseg></trk></gpx>')
    r = importer.import_vector(fs, gpx)
    check(r["points_new"] == 1 and r["tracks_new"] == 1, "GPX：航点成点位、航迹成轨迹")
    check(any(p["name"] == "W1" and p["note"] == "说明" for p in fs.list_points()), "GPX 航点名称与说明")
    check(any(p["name"] == "W1" and p["elevation"] == 2980 for p in fs.list_points()), "GPX 航点高程")
    tr = next(t for t in fs.list_tracks(with_coords=True) if t["name"] == "TR")
    check(tr["elev_min"] == 3000 and tr["elev_max"] == 3050 and [c[2] for c in tr["coords"]] == [3000, 3050],
          "GPX 航迹逐点高程（OGR 的 tracks 层默认不带，取自 track_points）")
    utc = lambda h, m: datetime(2026, 8, 1, h, m, tzinfo=timezone.utc)
    check(bool(tr["start_time"]) and datetime.fromisoformat(tr["start_time"]) == utc(2, 0)
          and datetime.fromisoformat(tr["end_time"]) == utc(2, 30), "GPX 航迹起止时间")
    check(importer.classify(gpx) == "vector" and importer.classify(os.path.join(folder, "near.jpg")) == "photo",
          "拖放分类")
    # 配准过的图件（带世界文件的 PNG、自带地理变换的 GeoTIFF）交给图层，不当照片
    mp = os.path.join(folder, "geomap.png")
    Image.new("RGB", (300, 200), (200, 180, 120)).save(mp)
    with open(os.path.join(folder, "geomap.pgw"), "w") as fh:
        fh.write("0.001\n0\n0\n-0.001\n100\n30\n")
    from osgeo import gdal, osr
    tif = os.path.join(folder, "geomap.tif")
    ds = gdal.GetDriverByName("GTiff").Create(tif, 50, 40, 1)
    ds.SetGeoTransform((100, 0.01, 0, 30, 0, -0.01))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds = None
    plain = os.path.join(folder, "plain.png")
    Image.new("RGB", (300, 200), (90, 90, 90)).save(plain)
    check(importer.classify(mp) == "" and importer.classify(tif) == "",
          "带地理变换的 PNG / TIF 是图件，不认领成照片")
    check(importer.classify(plain) == "photo", "没有地理变换的 PNG 仍是照片")
    s.close()


# =============================================================================
# 照片：EXIF、奥维水印、地质水印、修补
# =============================================================================

def _font(px):
    for p in geomark.FONT_CANDIDATES:
        if os.path.isfile(p):
            return ImageFont.truetype(p, px)
    return ImageFont.load_default(size=px)


def ovital_photo() -> bytes:
    """合成一张奥维相机式的照片：灰色岩石纹理，左下角三行白字带黑描边。"""
    rng = np.random.default_rng(7)
    w, h = 1512, 2016
    base = rng.normal(130, 35, (h // 6, w // 6, 3)).clip(40, 210).astype(np.uint8)
    im = Image.fromarray(base).resize((w, h), Image.BICUBIC)
    d = ImageDraw.Draw(im)
    f = _font(46)
    y = h - 200
    for line in ("经纬度： 95.79082896, 36.31076126", "海拔：   3016.5米", "朝向：   东南 138°"):
        d.text((30, y), line, font=f, fill="white", stroke_width=2, stroke_fill="black")
        y += 58
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return photoexif.write_tags(buf.getvalue(), lon=95.79082896, lat=36.31076126, altitude=3016.5)


def test_photo() -> None:
    print("照片 EXIF")
    data = jpeg_with_gps()
    info = photoexif.read_info(data)
    check(abs(info["lon"] - 95.79) < 1e-6 and abs(info["lat"] - 36.31) < 1e-6 and info["altitude"] == 3016.0,
          "读出位置与海拔")
    out = photoexif.write_tags(data, azimuth=138.5, lon=1, lat=1)
    info2 = photoexif.read_info(out)
    check(info2["azimuth"] == 138.5 and info2["azimuth_ref"] == "T", "方位角写进 GPSImgDirection")
    check(abs(info2["lon"] - 95.79) < 1e-6, "已有的 GPS 不被覆盖")
    check(np.array_equal(np.asarray(Image.open(io.BytesIO(out))), np.asarray(Image.open(io.BytesIO(data)))),
          "写 EXIF 不重新压缩像素")

    print("奥维水印")
    if not ovmark.ocr_available():
        print("  - 跳过：没有 ocrmac")
        return
    src = ovital_photo()
    mark, _ = ovmark.analyse(src)
    check(mark.found, "找到水印")
    check(mark.azimuth == 138 and mark.direction == "东南" and mark.azimuth_ok, "朝向 东南 138° 且扇区一致")
    check(mark.altitude == 3016.5, "海拔")
    check(mark.lon is not None and abs(mark.lon - 95.79082896) < 1e-6, "经纬度")
    m2 = ovmark.OvitalMark()
    ovmark._parse(["朝向：东南 238°"], m2)
    check(not m2.azimuth_ok and m2.notes, "度数与方位字对不上时标记待核对")
    for engine in ("opencv", "lama") if inpaint.lama_available() else ("opencv",):
        t0 = time.time()
        out, mark, used = ovmark.remove(src, engine=engine)
        after = ovmark.detect(photoexif.open_upright(out))
        check(not after.found, "%s 去除后再识别不出水印（%.1f 秒）" % (used, time.time() - t0))
        check(photoexif.read_info(out)["azimuth"] == 138, "%s 去除后朝向写进了 EXIF" % used)
        a = np.asarray(photoexif.open_upright(src)).astype(int)
        b = np.asarray(photoexif.open_upright(out)).astype(int)
        top = np.abs(a[:1500] - b[:1500]).mean()
        check(top < 3, "水印以外的区域基本不变（上部平均差 %.2f）" % top)

    print("地质水印")
    spec = {"version": 2, "lang": "en", "items": [
        {"id": "n", "type": "north", "x": 0.85, "y": 0.05, "size": 0.12, "rotation": -138},
        {"id": "s", "type": "scale", "x": 0.05, "y": 0.9, "px_per_mm": 3.0, "length": 10, "unit": "cm"},
        {"id": "v", "type": "view", "x": 0.05, "y": 0.05, "azimuth": 138},
        {"id": "t", "type": "text", "x": 0.05, "y": 0.12, "lines": ["Granite G-01"]}]}
    out = geomark.apply(data, spec)
    check(photoexif.read_info(out)["azimuth"] is None, "地质水印只画在图上，不改 EXIF")
    diff = np.abs(np.asarray(photoexif.open_upright(out)).astype(int) - np.asarray(photoexif.open_upright(data)).astype(int))
    check(diff.max() > 100, "照片上确实画了东西")
    bx = geomark.boxes((1200, 900), spec)
    check(set(bx) == {"n", "s", "v", "t"} and all(0 <= b[0] < b[2] <= 1.01 for b in bx.values()),
          "每个组件都有包围框")
    small = geomark.boxes((600, 450), spec, ref_width=1200)
    check(abs((small["s"][2] - small["s"][0]) - (bx["s"][2] - bx["s"][0])) < 0.02,
          "缩小的预览上比例尺按原图标定，相对长度不变")
    check(geomark.bearing(45) == "N45°E" and geomark.bearing(138) == "S42°E", "英文象限方位")
    check(geomark.view_label(138, "bearing", "en") == "Viewing direction S42°E", "英文镜向")
    check(geomark.view_label(138, "azimuth", "en") == "Viewing azimuth 138°", "英文方位角")
    check("南东" in geomark.view_label(138, "bearing", "zh"), "中文镜向")
    old = geomark.merge_spec({"compass": {"show": True, "azimuth": 138}, "text": {"show": True, "lines": ["x"]}})
    check(old["version"] == 2 and {i["type"] for i in old["items"]} >= {"north", "text"}, "旧版水印参数自动转换")
    check(any(os.path.isfile(f) for f in geomark.ESRI_NORTH_FONTS), "ESRI North 指北针字体随包")
    check(geomark.north_rotation({"auto": True}, 90) == 270 and geomark.north_rotation({"auto": False, "rotation": 15}, 90) == 15,
          "指北针按照片方位角自动旋转：相机朝东时北在画面左侧")
    t0 = time.time()
    for az in (0, 45, 200):
        geomark.render(Image.new("RGB", (1600, 1200)), {"version": 2, "items": [
            {"id": "n", "type": "north", "design": "rose", "x": 0.8, "y": 0.05, "size": 0.15}]}, azimuth=az)
    check(time.time() - t0 < 3, "罗盘指北针绘制够快（%.2f 秒 / 3 次）" % (time.time() - t0))
    a = np.asarray(geomark.render(Image.new("RGB", (800, 800), (128, 128, 128)), {"version": 2, "theme": "dark", "items": [
        {"id": "n", "type": "north", "design": "rose", "x": 0.2, "y": 0.2, "size": 0.6}]}, azimuth=0)).astype(int)
    core = a[300:500, 300:500]
    check((core > 230).all(axis=2).mean() > 0.05 and (core < 40).all(axis=2).mean() > 0.05,
          "罗盘镂空处填成衬色，黑白两色分明")
    anc = {"h": "right", "dx": 0.04, "v": "bottom", "dy": 0.05}
    for wh in ((1800, 1200), (1200, 1800), (3000, 1000)):
        b = geomark.boxes(wh, {"version": 2, "items": [{"id": "n", "type": "north", "size": 0.14, "anchor": anc}]}, azimuth=230)["n"]
        short = min(wh)
        check(abs((1 - b[2]) * wh[0] - 0.04 * short) < 2 and abs((1 - b[3]) * wh[1] - 0.05 * short) < 2,
              "按离边距离摆放：%d×%d 照片上指北针离右、下边各为短边的 4%%、5%%" % wh)
    lab = geomark.boxes((1800, 1200), {"version": 2, "items": [{"id": "n", "type": "north", "size": 0.14, "x": 0, "y": 0}]}, azimuth=230)["n"]
    nol = geomark.boxes((1800, 1200), {"version": 2, "items": [{"id": "n", "type": "north", "size": 0.14, "x": 0, "y": 0, "label": False}]}, azimuth=230)["n"]
    check(lab[3] > nol[3] * 1.1, "指北针下方加了一行镜向标注")
    check(geomark.direction_name(138) == "东南" and geomark.direction_name(359) == "北", "方位名")
    check(geomark.nice_length(56.0, 500) == 10, "比例尺取 1-2-5 整数长度")

    print("照片编辑（裁剪、旋转、拉直、调色、标注）")
    from samplelib.modules.fieldwork import photo_edit
    base = Image.new("RGB", (400, 300), (120, 120, 120))
    base.paste((255, 0, 0), (0, 0, 40, 30))                    # 左上角一块红
    out = photo_edit.apply(base, {"rot90": 90})
    check(out.size == (300, 400) and out.getpixel((295, 5))[0] > 200, "顺时针转 90°：宽高互换，左上角转到右上角")
    out = photo_edit.apply(base, {"flip_x": True})
    check(out.getpixel((395, 5))[0] > 200, "水平翻转")
    out = photo_edit.apply(Image.new("RGB", (400, 300), (200, 30, 30)), {"angle": 10})
    a_ = np.asarray(out)
    check(out.size == (400, 300) and a_[[0, 0, -1, -1], [0, -1, 0, -1], 0].min() > 150, "拉直后放大填满，四角不留黑边")
    m = photo_edit.color_matrix({"contrast": 0, "exposure": 0, "saturation": 0, "warmth": 0})
    check(np.allclose(m, np.hstack([np.eye(3), np.zeros((3, 1))])), "调色全为 0 时是单位矩阵")
    g = photo_edit.apply(Image.new("RGB", (10, 10), (100, 150, 200)), {"color": {"saturation": -100}})
    check(max(g.getpixel((5, 5))) - min(g.getpixel((5, 5))) <= 1, "饱和度 -100 变灰")
    w = photo_edit.apply(Image.new("RGB", (10, 10), (128, 128, 128)), {"color": {"warmth": 100}})
    check(w.getpixel((5, 5))[0] > w.getpixel((5, 5))[2], "色温调暖：红多于蓝")
    c = photo_edit.apply(base, {"crop": [0.25, 0.25, 0.75, 0.75],
                                "annotations": [{"type": "arrow", "pts": [[0.3, 0.3], [0.7, 0.7]], "color": "#ffd400", "width": 0.01},
                                                {"type": "text", "pts": [[0.3, 0.3]], "text": "Contact", "color": "#ffffff", "size": 0.05}]})
    check(c.size == (200, 150), "按画幅比例裁剪")
    check(np.any(np.all(np.asarray(c) > [200, 180, 0], axis=2) & (np.asarray(c)[:, :, 2] < 80)), "标注画进了图里")
    check(photo_edit.is_noop({"crop": [0, 0, 1, 1]}) and not photo_edit.is_noop({"angle": 1}), "没有改动时识别出来")

    print("手工修补")
    im = photoexif.open_upright(data)
    mask = inpaint.rect_mask(im.size, [(100, 100, 200, 160)])
    fixed, _ = inpaint.inpaint(im, mask, engine="opencv")
    d = np.abs(np.asarray(fixed).astype(int) - np.asarray(im).astype(int))
    check(d[:90].max() == 0 and d[170:].max() == 0, "选区以外一个像素都不动")


# =============================================================================
# 照片是原始资料：调整不改原片，导出才出加工后的图
# =============================================================================

def photo_raw_contract(c, ph, point) -> None:
    import base64
    import hashlib
    print("照片：原片不动、调整可撤销、导出")
    from samplelib.modules.fieldwork.fieldstore import FieldStore  # noqa: F401
    uid = ph["uid"]
    orig_path = None

    def raw_bytes():
        return c.get("/api/fw/photos/%s/image?variant=original" % uid).content

    before = hashlib.sha256(raw_bytes()).hexdigest()
    r = c.patch("/api/fw/photos/%s" % uid, json={"changes": {"azimuth": 45, "caption": "远景"}}).json()
    check(r["azimuth"] == 45 and r["caption"] == "远景" and r["file"] == r["original"] and not r["edits"],
          "改朝向、写说明只记在表里，不生成新文件")
    info = c.get("/api/fw/photos/%s/info" % uid).json()
    check(info.get("azimuth") != 45, "照片文件的 EXIF 没被改动")
    check("position" in r["locked"] and "altitude" in r["locked"] and "azimuth" not in r["locked"],
          "原片自带的位置、海拔锁定，缺的朝向可补")
    gspec = {"version": 2, "lang": "en", "items": [
        {"id": "n1", "type": "north", "x": 0.8, "y": 0.05},
        {"id": "v1", "type": "view", "x": 0.05, "y": 0.05, "azimuth": 90}]}
    g = c.post("/api/fw/photos/%s/geomark" % uid, json={"spec": gspec}).json()
    check(g["overlay"] and g["azimuth"] == 90 and not g["edits"] and g["adjusted"],
          "地质水印只记参数，不画进文件；原片没有朝向时镜向补记成朝向")
    check(c.get("/api/fw/geomark/defaults").json() == {}, "水印默认摆放起初为空")
    d = {"lang": "en", "items": {"north": {"size": 0.2, "anchor": {"h": "left", "dx": 0.03, "v": "bottom", "dy": 0.03}}}}
    c.put("/api/fw/geomark/defaults", json=d)
    check(c.get("/api/fw/geomark/defaults").json() == d, "水印默认摆放存进临时数据目录，下次照此摆")
    pv = c.post("/api/fw/photos/%s/geomark/preview" % uid, json={"spec": gspec, "width": 400}).json()
    check(pv["image"].startswith("data:image/png") and set(pv["boxes"]) == {"n1", "v1"} and pv["width"] == 400,
          "水印预览给出透明图层与组件包围框")
    adj = c.get("/api/fw/photos/%s/image?size=480" % uid).content
    base = c.get("/api/fw/photos/%s/image?size=480&variant=base" % uid).content
    check(adj != base, "调整后的图上画了地质水印，底图没有")
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "JPEG")
    r = c.post("/api/fw/photos/%s/replace" % uid,
               json={"data_url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}).json()
    check(r["edits"][-1]["op"] == "edit" and r["file"] != r["original"] and r["width"] == 40,
          "编辑器结果压进步骤栈，成为新的底图")
    r2 = c.post("/api/fw/photos/%s/replace" % uid,
                json={"data_url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}).json()
    check(len(r2["edits"]) == 2, "再做一步，步骤栈两层")
    u = c.post("/api/fw/photos/%s/undo" % uid).json()
    check(len(u["edits"]) == 1 and u["file"] == r["file"], "撤销最后一步，底图回到上一步")
    check(hashlib.sha256(raw_bytes()).hexdigest() == before, "做了这么多调整，原片一个字节没变")
    check(u["sha256"] == ph["sha256"], "记录里的哈希仍是原片的，重复导入照样认得出")

    print("导出照片")
    out = os.path.join(WORK, "export_adj")
    t = c.post("/api/fw/photos/export", json={"uids": [uid], "folder": out}).json()
    for _ in range(200):
        t = c.get("/api/tasks/%s" % t["id"]).json()
        if t["status"] != "running":
            break
        time.sleep(0.05)
    check(t["status"] == "done" and t["result"]["written"] == 1, "导出调整后的照片")
    f = t["result"]["files"][0]
    check(os.path.basename(f) == "A_01.jpg", "按模板命名：点位_序号（%s）" % os.path.basename(f))
    info = photoexif.read_info(open(f, "rb").read())
    check(info["azimuth"] == 90 and abs(info["lon"] - 100) < 1e-6, "导出文件补上朝向，原有 GPS 不变")
    import piexif
    desc = piexif.load(open(f, "rb").read())["0th"].get(piexif.ImageIFD.ImageDescription, b"").decode("utf-8")
    check(desc == "A 远景", "导出文件带点位名与说明（%s）" % desc)
    t = c.post("/api/fw/photos/export", json={"uids": [uid], "folder": out}).json()
    for _ in range(200):
        t = c.get("/api/tasks/%s" % t["id"]).json()
        if t["status"] != "running":
            break
        time.sleep(0.05)
    check(os.path.basename(t["result"]["files"][0]) == "A_01_2.jpg", "同名不覆盖，加 _2")
    out2 = os.path.join(WORK, "export_raw")

    def export(**kw):
        t = c.post("/api/fw/photos/export", json=dict({"uids": [uid], "folder": out2, "template": "{原名}"}, **kw)).json()
        for _ in range(200):
            t = c.get("/api/tasks/%s" % t["id"]).json()
            if t["status"] != "running":
                break
            time.sleep(0.05)
        return open(t["result"]["files"][0], "rb").read()

    raw = export(content="original", info=False)
    check(hashlib.sha256(raw).hexdigest() == before, "导出原片、不补信息：与原文件逐字节相同")
    sup = export(content="original")
    check(photoexif.read_info(sup)["azimuth"] == 90 and
          np.array_equal(np.asarray(Image.open(io.BytesIO(sup))), np.asarray(Image.open(io.BytesIO(raw)))),
          "导出原片并补信息：只插入缺的朝向，像素不变")
    r = c.post("/api/fw/photos/%s/revert" % uid).json()
    check(r["file"] == r["original"] and not r["edits"] and not r["overlay"] and r["azimuth"] == 90,
          "清除全部调整回到原片，照片信息保留")
    check(hashlib.sha256(export(info=False)).hexdigest() == before, "没调整过的照片导出的就是原片")


def test_legacy_steps() -> None:
    from samplelib.modules.fieldwork.fieldstore import _pixel_steps
    print("早先版本的编辑历史")
    a = {"op": "ovmark-remove", "file": "f1"}
    b = {"op": "geomark", "file": "f2"}
    rv = {"op": "revert", "file": "o"}
    check(_pixel_steps([a, b], "f2", "o") == [a, b], "没有还原过：整段就是步骤栈")
    check(_pixel_steps([a, rv, b], "f2", "o") == [b], "还原过：只取还原之后的那段")
    check(_pixel_steps([a, b, rv], "o", "o") == [], "最后一步是还原：没有调整")
    check(_pixel_steps([], "f9", "o")[0]["op"] == "legacy", "没有历史但文件不是原片：当成一步早先的编辑")


# =============================================================================
# 光谱库链接与接口
# =============================================================================

# =============================================================================
# 奥维附件：录音、文档与批量导入（自己按格式说明拼一份 .ovobj）
# =============================================================================

def _ov_attachment(hash8: bytes, name: str, ext: str, data: bytes, t: int) -> bytes:
    """附件块：[校验][0x1C][长度]，下一块在 +24+长度；数据本体在 +0x7C。"""
    blk = bytearray(0x7C)
    struct.pack_into("<III", blk, 0, 0, 0x1C, 0x7C - 24 + len(data))
    blk[0x20:0x28] = hash8
    struct.pack_into("<III", blk, 0x28, len(data), t, t)
    blk[0x34:0x34 + len(name.encode())] = name.encode()
    blk[0x68:0x68 + len(ext.encode())] = ext.encode()
    return bytes(blk) + data


def _ov_placemark(uid8: bytes, name: str, comment: str, lon: float, lat: float, t: int,
                  atts: list) -> bytes:
    """标签块：+0x70 主附件、+0xC0 起附加附件的哈希，几何段在 +0x158。"""
    nb, cb = name.encode(), comment.encode()
    size = 0x158 + 0x3B + 1 + len(nb) + 1 + 4 + len(cb)
    blk = bytearray(size)
    struct.pack_into("<II", blk, 0, size - 8, 0x1F)
    struct.pack_into("<I", blk, 0x1C, 7)
    blk[0x20:0x28] = uid8
    struct.pack_into("<I", blk, 0x28, t)
    struct.pack_into("<I", blk, 0x4C, t)
    if atts:
        blk[0x70:0x78] = atts[0]
    for i, h in enumerate(atts[1:]):
        blk[0xC0 + 8 * i:0xC8 + 8 * i] = h
    g = 0x158
    struct.pack_into("<HH", blk, g, 1, 1)
    struct.pack_into("<I", blk, g + 0x18, len(atts) - 1 if atts else 0)
    struct.pack_into("<dd", blk, g + 0x1C, lat, lon)
    blk[g + 0x3B] = len(nb)
    o = g + 0x3B + 1
    blk[o:o + len(nb)] = nb
    o += len(nb) + 1
    struct.pack_into("<I", blk, o, len(cb))
    blk[o + 4:o + 4 + len(cb)] = cb
    return bytes(blk)


def write_ovobj(path: str, placemarks: list, attachments: list, single: bool = False) -> None:
    """根块在 0xF0：一串对象时类型 0x64；``single`` 时只放一个对象，根块类型 0x1F、根块即该对象
    （对象自己的 [长度][0x1F] 那 8 字节清零，长度挪到根块头里）。"""
    head = bytearray(0x108)
    head[0:4] = b"OviO"
    struct.pack_into("<I", head, 0x0C, 105)
    if single:
        assert len(placemarks) == 1
        blk = bytearray(placemarks[0])
        length = struct.unpack_from("<I", blk, 0)[0]
        blk[0:8] = bytes(8)
        struct.pack_into("<III", head, 0xF0, 0, 0x1F, length)
        objects = bytes(blk[8:])              # 块首在 0x100，前 8 字节就是 head 末尾那 8 字节
    else:
        objects = b"".join(placemarks)
        struct.pack_into("<III", head, 0xF0, 0, 0x64, len(objects))
    body = bytes(head) + objects + b"".join(attachments)
    body = body[:0x18] + struct.pack("<I", len(body)) + body[0x1C:]
    with open(path, "wb") as fh:
        fh.write(body)


def test_attachments() -> None:
    print("奥维附件：照片以外的录音、文档")
    t0 = 1_786_000_000
    photo = jpeg_with_gps(size=(64, 48), lon=100.0, lat=30.0)
    amr = b"#!AMR\n" + bytes(200)
    pdf = b"%PDF-1.4\n" + bytes(100)
    m4a = bytes(4) + b"ftypM4A " + bytes(100)
    H = [bytes([i]) * 8 for i in range(1, 6)]
    path1 = os.path.join(WORK, "day1.ovobj")
    write_ovobj(path1, [
        _ov_placemark(b"\xa1" * 8, "露头一", "片麻岩", 100.0, 30.0, t0, [H[0], H[1], H[2]]),
    ], [
        _ov_attachment(H[0], "pic_20260806_101010", "jpg", photo, t0),
        _ov_attachment(H[1], "rec_20260806_101100", "amr", amr, t0 + 60),
        _ov_attachment(H[2], "说明", "", pdf, t0 + 90),          # 扩展名缺了，按数据认
        _ov_attachment(H[3], "rec_loose", "m4a", m4a, t0 + 120),  # 没人引用
    ])
    f = ovobj.read(path1)
    check(len(f.placemarks) == 1 and f.placemarks[0].attachments == [h.hex() for h in H[:3]], "自拼文件：标签引用三个附件")
    check(f.orphans == [H[3].hex()], "认出未被引用的附件")
    check(ovobj.sniff_ext(pdf[:16]) == "pdf" and ovobj.sniff_ext(amr[:16]) == "amr" and ovobj.sniff_ext(m4a[:16]) == "m4a",
          "按数据开头认格式")
    pv = importer.preview_ovobj(path1)
    check((pv["photos"], pv["audio"], pv["files"], pv["orphans"]) == (1, 2, 1, 1), "预览分出照片、录音、文件（%s）" % pv)

    s = library("attach.gpkg")
    fs = FieldStore(s.store)
    r = importer.import_ovobj(fs, path1)
    check(r["photos_new"] == 1 and r["audio_new"] == 2 and r["files_new"] == 1 and r["loose"] == 1,
          "照片进照片表，录音、文档进附件表（%s）" % {k: r[k] for k in ("photos_new", "audio_new", "files_new", "loose")})
    pt = fs.list_points()[0]
    det = fs.get_point(pt["uid"])
    names = sorted(a["orig_name"] for a in det["attachments"])
    check(len(det["photos"]) == 1 and names == ["rec_20260806_101100.amr", "说明.pdf"], "点位带出附件（%s）" % names)
    check(pt["n_attachments"] == 2, "点位列表带附件数")
    amr_row = next(a for a in det["attachments"] if a["ext"] == "amr")
    with open(amr_row["path"], "rb") as fh:
        check(fh.read() == amr and amr_row["kind"] == "audio" and amr_row["size"] == len(amr), "录音原样存进媒体文件夹")
    loose = fs.list_attachments(unattached=True)
    check(len(loose) == 1 and loose[0]["ext"] == "m4a", "未引用的附件成为独立附件")
    r2 = importer.import_ovobj(fs, path1)
    check(r2["attachments_new"] == 0 and r2["attachments_skipped"] == 3 and r2["photos_skipped"] == 1, "再导一次附件不重复")
    fs.delete_point(pt["uid"], keep_photos=True)
    check(len(fs.list_attachments(unattached=True)) == 3, "删点位保留附件时附件成为独立附件")

    print("早先当成照片存进来的录音挪到附件表")
    pid = fs.add_point(100.0, 30.0, name="旧")
    rel, sha = fs.put_media("photos", amr, "amr")
    fs.add_photo(rel, sha, {}, point_uid=pid, orig_name="old.amr")
    s.close()
    s = library_open("attach.gpkg")
    fs = FieldStore(s.store)
    det = fs.get_point(pid)
    check(not det["photos"] and [a["orig_name"] for a in det["attachments"]] == ["old.amr"], "开库时挪过去了")

    print("添加计算机上的附件")
    doc = os.path.join(WORK, "记录.txt")
    with open(doc, "w", encoding="utf-8") as fh:
        fh.write("产状 120∠35")
    pic = os.path.join(WORK, "补拍.jpg")
    with open(pic, "wb") as fh:
        fh.write(jpeg_with_gps(size=(32, 24)))
    r = importer.import_attachments(fs, [doc, pic], point_uid=pid)
    det = fs.get_point(pid)
    check(r["attachments_new"] == 1 and r["photos_new"] == 1 and len(det["photos"]) == 1
          and "记录.txt" in [a["orig_name"] for a in det["attachments"]], "文档进附件，照片进照片")

    print("只导出一个标签的奥维文件（根块即对象）")
    path3 = os.path.join(WORK, "single.ovobj")
    write_ovobj(path3, [_ov_placemark(b"\xa9" * 8, "单点", "说明", 101.0, 31.0, t0, [H[0], H[1]])],
                [_ov_attachment(H[0], "pic_single", "jpg", photo, t0), _ov_attachment(H[1], "rec", "amr", amr, t0)],
                single=True)
    f = ovobj.read(path3)
    check(len(f.placemarks) == 1 and f.placemarks[0].name == "单点" and abs(f.placemarks[0].lon - 101.0) < 1e-9
          and len(f.attachments) == 2 and not f.warnings, "单对象文件：标签与两个附件都读出来")
    check(ovobj._objects_end(open(path1, "rb").read()) == os.path.getsize(path1) - sum(
        24 + struct.unpack_from("<I", d, 8)[0] for d in [
            _ov_attachment(H[0], "pic_20260806_101010", "jpg", photo, t0),
            _ov_attachment(H[1], "rec_20260806_101100", "amr", amr, t0 + 60),
            _ov_attachment(H[2], "说明", "", pdf, t0 + 90),
            _ov_attachment(H[3], "rec_loose", "m4a", m4a, t0 + 120)]), "列表文件：附件区从 0x108 + 根块长度开始")
    fake = b"OSPX" + struct.pack("<III", 0, 0, 0) + b"RIFF" + struct.pack("<I", 0) + b"WAVE" + \
        b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16) + b"data" + struct.pack("<I", 640) + \
        struct.pack("<I", 38) + bytes(38) + struct.pack("<I", 38) + bytes(38)
    info, frames = ovobj.ospx_frames(fake)
    check(info["rate"] == 8000 and info["pcm_bytes"] == 640 and len(frames) == 2 and ovobj.sniff_ext(fake) == "spx",
          "拆开奥维录音：WAV 头与 Speex 帧")

    if os.path.isfile(OVOBJ_SINGLE):
        print("真实的单标签文件：照片、视频、奥维录音")
        from samplelib.modules.fieldwork import speex
        f = ovobj.read(OVOBJ_SINGLE)
        kinds = sorted(importer._attachment_kind(f, a) for a in f.attachments.values())
        check(len(f.placemarks) == 1 and kinds == [("audio", "spx"), ("photo", "jpg"), ("video", "mov")],
              "一个标签，附件为照片、视频、录音（%s）" % kinds)
        rec = next(a for a in f.attachments.values() if a.ext == "spx")
        info, frames = ovobj.ospx_frames(f.read_attachment(rec))
        check(info["rate"] == 8000 and len(frames) > 100, "奥维录音：8 kHz，%d 帧" % len(frames))
        if speex.available():
            import wave
            wav = speex.ospx_to_wav(f.read_attachment(rec))
            with wave.open(io.BytesIO(wav)) as w:
                n = w.getnframes()
                pcm = np.frombuffer(w.readframes(n), dtype="<i2").astype(float)
            check(abs(n * 2 - info["pcm_bytes"]) <= 2 and pcm.std() > 100, "解码成 WAV：%.1f 秒，有声音" % (n / 8000))
        else:
            print("  - 跳过录音解码：找不到 libspeex")
        s3 = library("single.gpkg")
        fs3 = FieldStore(s3.store)
        r = importer.import_ovobj(fs3, OVOBJ_SINGLE)
        check(r["points_new"] == 1 and r["photos_new"] == 1 and r["audio_new"] == 1 and r["video_new"] == 1,
              "导入：照片进照片，录音、视频进附件")
        if speex.available():
            a = next(x for x in fs3.list_attachments() if x["ext"] == "spx")
            check(a["transcode"] == "wav" and fs3.attachment_wav(a["uid"]).endswith(".wav"), "录音转成 WAV 缓存起来")
        s3.close()
    else:
        print("  - 跳过真实单标签文件：找不到 %s" % OVOBJ_SINGLE)

    print("录音转文字")
    from samplelib.modules.fieldwork import asr
    check(asr.note_line("采样点", "2026-10-21T10:45:13+08:00") == "［录音 10:45］采样点", "追加到备注的格式")
    rate = asr.RATE
    tone = (np.sin(np.arange(rate) / 3) * 0.3).astype(np.float32)
    long = np.concatenate([np.tile(tone, 24), np.zeros(rate, np.float32), np.tile(tone, 20)])   # 45 秒，24 秒处停顿
    cuts = asr._cuts(long)
    check(len(cuts) == 3 and 20 * rate <= cuts[1] <= 25 * rate and cuts[-1] == len(long), "长录音在停顿处切段（%s）"
          % [round(c / rate, 1) for c in cuts])
    buf = io.BytesIO()
    import wave
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes((np.tile(np.int16([1000, -1000]), 8000)).tobytes())
    x = asr._wav_samples(buf.getvalue())
    check(len(x) == rate and x.dtype == np.float32, "WAV 读成 16 kHz 单声道")
    s4 = library("asr.gpkg")
    fs4 = FieldStore(s4.store)
    pid = fs4.add_point(100, 30, name="口述点", note="已有备注")
    wav_path = os.path.join(WORK, "口述.wav")
    with open(wav_path, "wb") as fh:
        fh.write(buf.getvalue())
    importer.import_attachments(fs4, [wav_path], point_uid=pid)
    rec = fs4.list_attachments(point_uid=pid)[0]
    real = asr.transcribe_file
    asr.transcribe_file = lambda path, progress=None: "岩性为花岗闪长岩，产状120°∠35°。"
    try:
        r = asr.transcribe_attachments(fs4, [rec["uid"]], append=True)
        note = fs4.get_point(pid)["note"]
        check(r["appended"] == 1 and note.startswith("已有备注\n［录音") and note.endswith("产状120°∠35°。"),
              "转写结果记在附件上并追加到备注（%r）" % note)
        check(fs4.get_attachment(rec["uid"])["transcript"] == "岩性为花岗闪长岩，产状120°∠35°。", "附件记下转写文字")
        r = asr.transcribe_attachments(fs4, [rec["uid"]], append=True)
        check(r["appended"] == 0 and fs4.get_point(pid)["note"] == note, "同样的文字不重复追加")
    finally:
        asr.transcribe_file = real
    if asr.engine_available() and asr.model_ready() and os.path.isfile(OVOBJ_SINGLE):
        f = ovobj.read(OVOBJ_SINGLE)
        a = next(x for x in f.attachments.values() if x.ext == "spx")
        from samplelib.modules.fieldwork import speex
        text = asr.transcribe(asr._wav_samples(speex.ospx_to_wav(f.read_attachment(a))))
        check("北纬" in text and "东经" in text, "真实录音识别出文字：%s" % text)
    else:
        print("  - 跳过真实识别：没有识别引擎或模型（数据目录 %s）" % asr.model_dir())
    s4.close()

    print("批量导入奥维收藏夹")
    path2 = os.path.join(WORK, "day2.ovobj")
    write_ovobj(path2, [
        _ov_placemark(b"\xa1" * 8, "露头一（改）", "片麻岩，补充", 100.0, 30.0, t0 + 86400, []),
        _ov_placemark(b"\xa2" * 8, "露头二", "", 100.01, 30.0, t0 + 86400, [H[4]]),
    ], [_ov_attachment(H[4], "rec_day2", "mp3", b"ID3" + bytes(100), t0 + 86400)])
    bogus = os.path.join(WORK, "bad.ovobj")
    with open(bogus, "wb") as fh:
        fh.write(b"OviO" + bytes(20))
    s2 = library("batch.gpkg")
    fs2 = FieldStore(s2.store)
    seen = []
    r = importer.import_ovobj_batch(fs2, [{"path": path1, "survey": ""}, {"path": bogus, "survey": ""},
                                          {"path": path2, "survey": "第二天"}],
                                    lambda d, t, x="": seen.append((d, t)) or True)
    check(r["points_new"] == 2 and r["points_updated"] == 1, "同一个标签跨文件认作一个点位，后导的更新先导的")
    check(r["surveys"] == ["day1", "第二天"] and len(r["files"]) == 2, "各文件各自的批次名")
    check(len(r["failed"]) == 1 and r["failed"][0]["name"] == "bad.ovobj", "坏文件单独报出来，不影响其余的")
    check(r["audio_new"] == 3 and r["photos_new"] == 1, "批量导入同样分出照片与附件")
    check(all(b <= t for b, t in seen) and seen[-1][0] == seen[-1][1], "进度单调到底")
    names = sorted(p["name"] for p in fs2.list_points())
    check(names == ["露头一（改）", "露头二"], "点位（%s）" % names)
    s2.close()
    s.close()


def test_spectra_and_api() -> None:
    print("光谱库链接")
    if not spectra_link.available():
        print("  - 跳过：光谱模块不在")
    else:
        hits = spectra_link.search("kaolinite", limit=3)
        check(len(hits) >= 1, "检索到光谱库条目")
        check(hits[0]["cn"] == "高岭石" and hits[0]["class_cn"].startswith("矿物"), "关联光谱时带中文名与中文分类")
        if hits:
            c = spectra_link.read_entry(hits[0]["id"])
            check(len(c["x"]) == len(c["y"]) > 10 and c["x"] == sorted(c["x"]), "读出曲线，波长递增")
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  - 跳过接口：没有 fastapi / httpx")
        return
    from samplelib import config
    # 接口会往数据目录写东西（水印默认摆放），测试里换成临时目录，不碰真正的 state/
    app_dir = os.path.join(WORK, "appdata")
    os.makedirs(app_dir, exist_ok=True)
    config.app_data_dir = lambda: app_dir
    from webapi import create_app
    print("接口")
    s = library("api.gpkg")
    s.close()
    c = TestClient(create_app(os.path.join(WORK, "api.gpkg"), remember=False))
    check(c.get("/api/fw/status").status_code == 200, "状态接口")
    p = c.post("/api/fw/points", json={"lon": 100, "lat": 30, "name": "A"}).json()
    check(p["name"] == "A" and p["survey"] == "手工添加", "新建点位")
    folder = os.path.join(WORK, "api_photos")
    os.makedirs(folder)
    open(os.path.join(folder, "x.jpg"), "wb").write(jpeg_with_gps(lon=100, lat=30))
    paths = c.post("/api/fw/import/folder", json={"path": folder}).json()["paths"]
    t = c.post("/api/fw/import/photos", json={"paths": paths, "mode": "point", "point_uid": p["uid"]}).json()
    for _ in range(200):
        t = c.get("/api/tasks/%s" % t["id"]).json()
        if t["status"] != "running":
            break
        time.sleep(0.05)
    check(t["status"] == "done" and t["result"]["attached"] == 1, "导入照片任务")
    pt = c.get("/api/fw/points/%s" % p["uid"]).json()
    ph = pt["photos"][0]
    r = c.get("/api/fw/photos/%s/image?size=160" % ph["uid"])
    check(r.status_code == 200 and max(Image.open(io.BytesIO(r.content)).size) <= 160, "缩略图")
    check(c.get("/api/fw/photos/%s/image" % ph["uid"]).content[:2] == b"\xff\xd8", "原图")
    photo_raw_contract(c, ph, p)
    if spectra_link.available():
        hits = c.get("/api/fw/spectra/search?q=kaolinite&limit=2").json()
        if hits:
            sp = c.post("/api/fw/points/%s/spectra" % p["uid"], json={"entry_id": hits[0]["id"]}).json()
            again = c.post("/api/fw/points/%s/spectra" % p["uid"], json={"entry_id": hits[0]["id"]}).json()
            check(sp["uid"] == again["uid"], "同一条谱不重复链接")
            pt = c.get("/api/fw/points/%s" % p["uid"]).json()
            check(pt["n_spectra"] == 1 and pt["spectra"][0]["entry"]["name"] == hits[0]["name"], "点位带出链接的光谱")
            curve = c.get("/api/fw/spectra/curve", params={"entry_id": hits[0]["id"]}).json()
            check(len(curve["x"]) > 10, "取曲线")
            check(c.get("/api/fw/spectra/points", params={"entry_id": hits[0]["id"]}).json() == [p["uid"]],
                  "反查哪些点位链了这条谱")
            c.delete("/api/fw/spectra/%s" % sp["uid"])
            check(c.get("/api/fw/points/%s" % p["uid"]).json()["n_spectra"] == 0, "取消链接")
    claim = c.post("/api/fw/claim", json={"paths": paths + [__file__]}).json()
    check(claim["photo"] == paths and claim["rest"] == [__file__], "拖放认领")
    ov = c.get("/api/fw/overview").json()
    check(ov["stats"]["fw_point"] == 1 and ov["extent"] is not None, "概况")
    check(c.get("/api/features").json()["features"] == [], "样本图层里看不到野外点位")
    check(c.delete("/api/fw/points/%s" % p["uid"]).json()["deleted"], "删点位")

    print("接口：批量导入奥维与附件")
    ovdir = os.path.join(WORK, "api_ovobj")
    os.makedirs(os.path.join(ovdir, "子目录"))
    for src in ("day1.ovobj", "day2.ovobj", "bad.ovobj"):
        shutil.copy(os.path.join(WORK, src), os.path.join(ovdir, "子目录" if src == "day2.ovobj" else "", src))
    found = c.post("/api/fw/import/ovobj-folder", json={"path": ovdir}).json()["paths"]
    check(sorted(os.path.basename(x) for x in found) == ["bad.ovobj", "day1.ovobj", "day2.ovobj"], "文件夹里（含子文件夹）的奥维文件")
    pv = c.post("/api/fw/import/ovobj/preview-batch", json={"paths": found}).json()["items"]
    check([x["name"] for x in pv] == ["day1.ovobj", "day2.ovobj", "bad.ovobj"] and "error" in pv[-1],
          "批量预览按时间先后排，坏文件排最后并带原因")
    items = [{"path": x["path"], "survey": x["survey"]} for x in pv if "error" not in x]
    t = c.post("/api/fw/import/ovobj-batch", json={"items": items}).json()
    for _ in range(200):
        t = c.get("/api/tasks/%s" % t["id"]).json()
        if t["status"] != "running":
            break
        time.sleep(0.05)
    check(t["status"] == "done" and t["result"]["points_new"] == 2 and t["result"]["audio_new"] == 3, "批量导入任务")
    pts = c.get("/api/fw/points").json()
    one = next(x for x in pts if x["name"].startswith("露头一"))
    det = c.get("/api/fw/points/%s" % one["uid"]).json()
    rec = next(a for a in det["attachments"] if a["ext"] == "amr")
    r = c.get("/api/fw/attachments/%s/file" % rec["uid"])
    check(r.status_code == 200 and r.content.startswith(b"#!AMR") and r.headers["content-type"].startswith("audio/amr")
          and r.headers["content-disposition"].startswith("inline"), "取附件原文件（内联）")
    r = c.get("/api/fw/attachments/%s/file" % rec["uid"], headers={"Range": "bytes=0-5"})
    check(r.status_code == 206 and r.content == b"#!AMR\n", "附件支持分段读取（录音拖进度条）")
    loose = c.get("/api/fw/attachments?unattached=true").json()
    moved = c.post("/api/fw/attachments/%s/move" % loose[0]["uid"], json={"point_uid": one["uid"]}).json()
    check(moved["point_uid"] == one["uid"], "附件挂到点位上")
    ren = c.patch("/api/fw/attachments/%s" % moved["uid"], json={"changes": {"caption": "补充录音"}}).json()
    check(ren["caption"] == "补充录音", "附件说明")
    check(c.delete("/api/fw/attachments/%s" % moved["uid"]).json()["deleted"], "删附件")
    check(c.get("/api/fw/overview").json()["stats"]["fw_attachment"] >= 2, "概况里有附件数")


def main() -> int:
    test_ovobj()
    test_store()
    test_import()
    test_attachments()
    test_photo()
    test_locked_meta()
    test_spectra_and_api()
    test_legacy_steps()
    print("\n%s" % ("全部通过" if not FAILED else "失败 %d 项" % len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
