# -*- coding: utf-8 -*-
"""把外部的野外记录整理进库：奥维 .ovobj、成批的照片、GPX / KML / GeoJSON 等矢量。

进度回调与样本导入一致：``progress(done, total, text) -> bool``，返回 ``False``
表示使用者按了取消。每种导入返回一份报告（dict），界面直接显示。

重复导入
--------
同一份奥维文件改过之后再导一次是常事（野外当天导一次、回来补完备注再导一次）。
点位与轨迹按奥维对象 id 认人：已有的更新名称、备注与位置，没有的新建；照片与其他
附件按内容哈希认，已挂在同一个点位上的不再重复挂。

奥维的附件不只有照片：录音、视频、文档存进附件表（``fw_attachment``），照片照旧进
照片表。挂在轨迹上的附件跟着轨迹；文件里有、但没有哪个对象引用的附件作为独立的
照片、附件导入，一样不丢。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from . import ovobj, photoexif
from .fieldstore import FieldStore, haversine, iso, sha256_file

Progress = Callable[[int, int, str], bool]

PHOTO_EXTS = {".jpg", ".jpeg", ".heic", ".png", ".tif", ".tiff"}
#: 用 Pillow 能直接读 EXIF 的照片格式；HEIC 需要 pillow-heif，没有就只存不解析
EXIF_EXTS = {".jpg", ".jpeg", ".tif", ".tiff"}
VECTOR_EXTS = {".gpx", ".kml", ".kmz", ".geojson", ".json", ".shp", ".gpkg"}

_PIC_TIME = re.compile(r"(\d{8})_(\d{6})")


def _noop(done: int, total: int, text: str = "") -> bool:
    return True


def _is_map_image(path: str) -> bool:
    """这张图带地理变换（自带，或旁边有世界文件），是配准过的图件而不是野外照片。

    拖进来的 tif / png / jpg 两种都有：相机拍的照片没有地理变换，扫描配准过的
    地质图、影像有。后者要交给图层去画，当成照片导进来就加载不上了。坐标系缺不缺
    不在这里管——缺的话图层那边会把原因报出来，总比被当成照片悄悄吞掉强。
    """
    try:
        from ...annotate import georef
        _ds, info = georef.open_raster(path)
    except Exception:
        return False
    return not georef._is_identity(info.geotransform)


def classify(path: str) -> str:
    """拖进来的文件该交给谁：``ovobj`` / ``photo`` / ``vector`` / ``""``（不归本模块）。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".ovobj" or ovobj.is_ovobj(path):
        return "ovobj"
    if ext in PHOTO_EXTS:
        return "" if _is_map_image(path) else "photo"
    if ext in (".gpx",):
        return "vector"
    return ""


def _taken_from_name(name: str) -> str:
    """奥维附件文件名里的拍摄、录制时间（pic_20261021_105535、spx_…、MOV_…）。附件块里记的
    时间是挂到标签上的时间，晚于实际拍摄，所以文件名里有时间时优先用它。"""
    m = _PIC_TIME.search(name or "")
    if not m:
        return ""
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").astimezone().isoformat(timespec="seconds")
    except ValueError:
        return ""


# =============================================================================
# 奥维
# =============================================================================

def _attachment_kind(f: ovobj.OvobjFile, att: ovobj.Attachment) -> tuple:
    """附件的类型与扩展名。扩展名缺了、或与数据对不上（扩展名说是照片、数据却不是）时
    按数据开头认。"""
    ext = (att.ext or "").lower()
    if not ext or ovobj.attachment_kind(ext) == "photo":
        with open(f.path, "rb") as fh:
            fh.seek(att.offset)
            head = fh.read(16)
        sniffed = ovobj.sniff_ext(head)
        if sniffed and (not ext or ovobj.attachment_kind(sniffed) != "photo"):
            ext = sniffed
    ext = ext or "jpg"
    return ovobj.attachment_kind(ext), ext


def _attachment_counts(f: ovobj.OvobjFile) -> Dict[str, int]:
    counts = {"photo": 0, "audio": 0, "video": 0, "file": 0}
    for att in f.attachments.values():
        counts[_attachment_kind(f, att)[0]] += 1
    return counts


def preview_ovobj(path: str, fs: Optional[FieldStore] = None) -> Dict[str, Any]:
    """导入前看一眼：多少点位、轨迹、照片与其他附件，是否导过。"""
    f = ovobj.read(path)
    kinds = _attachment_counts(f)
    out = {
        "path": path, "name": os.path.basename(path),
        "survey": os.path.splitext(os.path.basename(path))[0],
        "placemarks": len(f.placemarks), "tracks": len(f.tracks),
        "photos": kinds["photo"], "audio": kinds["audio"], "video": kinds["video"], "files": kinds["file"],
        "orphans": len(f.orphans), "folders": [x for x in f.folders if x],
        "track_points": sum(len(t.lons) for t in f.tracks),
        "warnings": f.warnings, "imported_before": None, "existing": 0,
    }
    times = [t for tr in f.tracks for t in (tr.times[:1] + tr.times[-1:])]
    times += [int(p.created.timestamp()) for p in f.placemarks if p.created]
    if times:
        out["start"] = iso(datetime.fromtimestamp(min(times)).astimezone())
        out["end"] = iso(datetime.fromtimestamp(max(times)).astimezone())
    if fs is not None:
        prev = fs.imported_before(sha256_file(path))
        out["imported_before"] = prev
        out["existing"] = sum(1 for p in f.placemarks if fs.find_point_by_source(p.uid))
    return out


def import_ovobj(fs: FieldStore, path: str, survey: str = "", progress: Progress = _noop,
                 update_existing: bool = True) -> Dict[str, Any]:
    f = ovobj.read(path)
    survey = survey or os.path.splitext(os.path.basename(path))[0]
    source = os.path.basename(path)
    rep = {"source": source, "survey": survey, "points_new": 0, "points_updated": 0,
           "tracks_new": 0, "tracks_skipped": 0, "photos_new": 0, "photos_skipped": 0,
           "attachments_new": 0, "attachments_skipped": 0, "audio_new": 0, "video_new": 0, "files_new": 0,
           "loose": 0, "warnings": list(f.warnings), "cancelled": False}
    orphans = f.orphans
    total = len(f.placemarks) + len(f.tracks) + (1 if orphans else 0)
    done = 0

    def put(att: ovobj.Attachment, point_uid: Optional[str], track_uid: Optional[str],
            lonlat: Optional[tuple], note: str = "") -> Optional[float]:
        """存一个附件。照片进照片表（返回照片里的海拔），其余进附件表。"""
        kind, ext = _attachment_kind(f, att)
        data = f.read_attachment(att)
        if kind != "photo":
            rel, sha = fs.put_media("files", data, ext)
            if fs.find_attachment(sha, point_uid=point_uid, track_uid=track_uid):
                rep["attachments_skipped"] += 1
                return None
            name = att.filename if att.ext else "%s.%s" % (att.name or att.hash, ext)
            fs.add_attachment(rel, sha, ext, point_uid=point_uid, track_uid=track_uid, orig_name=name,
                              taken=_taken_from_name(att.name) or iso(att.time), source=source)
            rep["attachments_new"] += 1
            rep[{"audio": "audio_new", "video": "video_new"}.get(kind, "files_new")] += 1
            if not (point_uid or track_uid):
                rep["loose"] += 1
            return None
        # 照片挂在点位上；挂在轨迹上的、没人引用的照片成为独立照片
        rel, sha = fs.put_media("photos", data, ext)
        if fs.find_photo(point_uid, sha):
            rep["photos_skipped"] += 1
            return None
        try:
            info = photoexif.read_info(data)
        except Exception as exc:
            info = {}
            rep["warnings"].append("无法读取 %s 的 EXIF：%s" % (att.filename, exc))
        if not info.get("taken"):
            info["taken"] = _taken_from_name(att.name) or iso(att.time)
        if info.get("lon") is None and lonlat:
            info["lon"], info["lat"] = lonlat
        fs.add_photo(rel, sha, info, point_uid=point_uid, orig_name=att.filename, source=source,
                     caption=note or None)
        rep["photos_new"] += 1
        if not point_uid:
            rep["loose"] += 1
        return info.get("altitude")

    with fs.store.transaction():
        for tr in f.tracks:
            if not progress(done, total, "轨迹 %s" % tr.name):
                rep["cancelled"] = True
                break
            done += 1
            track_uid = fs.find_track_by_source(tr.uid)
            if track_uid:
                rep["tracks_skipped"] += 1
            elif len(tr.lons) < 2:
                rep["warnings"].append("轨迹「%s」不足两个点，已跳过" % tr.name)
            else:
                track_uid = fs.add_track(tr.lons, tr.lats, tr.times, tr.elevations, name=tr.name,
                                         note=tr.comment, survey=survey, source=source, source_uid=tr.uid,
                                         created=iso(tr.created or tr.start) or None,
                                         modified=iso(tr.modified) or None)
                rep["tracks_new"] += 1
            for h in tr.attachments:
                att = f.attachments[h]
                if _attachment_kind(f, att)[0] == "photo":
                    # 照片只挂得上点位：轨迹上的照片成为独立照片，说明里记下来自哪条轨迹
                    put(att, None, None, None, note="轨迹「%s」上的照片" % tr.name)
                else:
                    put(att, None, track_uid, None)
        for pm in f.placemarks:
            if rep["cancelled"] or not progress(done, total, "点位 %s" % pm.name):
                rep["cancelled"] = True
                break
            done += 1
            existing = fs.find_point_by_source(pm.uid)
            if existing:
                uid = existing["uid"]
                changed = (existing["name"] != pm.name or (existing["note"] or "") != pm.comment
                           or abs(existing["lon"] - pm.lon) > 1e-9 or abs(existing["lat"] - pm.lat) > 1e-9)
                if update_existing and changed:
                    fs.update_point(uid, {"name": pm.name, "note": pm.comment, "lon": pm.lon, "lat": pm.lat})
                    rep["points_updated"] += 1
            else:
                uid = fs.add_point(pm.lon, pm.lat, name=pm.name, note=pm.comment, survey=survey,
                                   icon=pm.icon, source=source, source_uid=pm.uid,
                                   created=iso(pm.created) or None, modified=iso(pm.modified) or None)
                rep["points_new"] += 1
            elevation = None
            for h in pm.attachments:
                alt = put(f.attachments[h], uid, None, (pm.lon, pm.lat))
                if elevation is None and alt is not None:
                    elevation = alt
            if not existing:
                if elevation is None:
                    when = pm.created.timestamp() if pm.created else None
                    elevation = fs.elevation_at(pm.lon, pm.lat, when)
                if elevation is not None:
                    fs.update_point(uid, {"elevation": float(elevation)})
        if orphans and not rep["cancelled"]:
            if progress(done, total, "未关联的附件"):
                for h in orphans:
                    put(f.attachments[h], None, None, None)
            else:
                rep["cancelled"] = True
        progress(total, total, "")
    if rep["loose"]:
        rep["warnings"].append("%d 个附件没有挂在标签上（轨迹上的照片，或文件里未被引用的附件），"
                               "已放在「未关联点位的照片与附件」中" % rep["loose"])
    if not rep["cancelled"]:
        fs.log_import(source, sha256_file(path), "ovobj", _ovobj_summary(rep))
    return rep


def _ovobj_summary(rep: Dict[str, Any]) -> str:
    parts = ["%d 点位（新 %d）" % (rep["points_new"] + rep["points_updated"], rep["points_new"]),
             "%d 轨迹" % rep["tracks_new"], "%d 照片" % rep["photos_new"]]
    if rep["attachments_new"]:
        parts.append("%d 个其他附件" % rep["attachments_new"])
    return "、".join(parts)


def import_ovobj_batch(fs: FieldStore, items: Sequence[Dict[str, str]], progress: Progress = _noop) -> Dict[str, Any]:
    """一次导入多份奥维收藏夹。``items`` 是 [{path, survey}]，按给的顺序导（界面按时间先后排好）。

    每份文件各自一个事务：哪一份读不了、导失败，记下原因接着导下一份。同一个标签出现在
    多份文件里（重导、按日导出又按月导出）时，按对象 id 认作同一个点位，后导的更新先导的。
    """
    keys = ("points_new", "points_updated", "tracks_new", "tracks_skipped", "photos_new", "photos_skipped",
            "attachments_new", "attachments_skipped", "audio_new", "video_new", "files_new", "loose")
    total_rep: Dict[str, Any] = {k: 0 for k in keys}
    total_rep.update({"files": [], "failed": [], "surveys": [], "cancelled": False})
    sizes = []
    for it in items:
        try:
            f = ovobj.read(it["path"])
            sizes.append(len(f.placemarks) + len(f.tracks) + (1 if f.orphans else 0))
        except Exception:
            sizes.append(1)
    grand = sum(sizes) or 1
    base = 0
    for it, size in zip(items, sizes):
        name = os.path.basename(it["path"])
        if not progress(base, grand, name):
            total_rep["cancelled"] = True
            break

        def sub(done: int, total: int, text: str = "", _base=base, _size=size, _name=name) -> bool:
            frac = done / total if total else 1
            return progress(_base + int(frac * _size), grand, "%s · %s" % (_name, text) if text else _name)

        try:
            rep = import_ovobj(fs, it["path"], it.get("survey") or "", sub)
        except Exception as exc:
            total_rep["failed"].append({"path": it["path"], "name": name, "reason": str(exc)})
            base += size
            continue
        base += size
        for k in keys:
            total_rep[k] += rep[k]
        total_rep["files"].append(rep)
        if rep["survey"] not in total_rep["surveys"]:
            total_rep["surveys"].append(rep["survey"])
        if rep["cancelled"]:
            total_rep["cancelled"] = True
            break
    progress(grand, grand, "")
    return total_rep


def import_attachments(fs: FieldStore, paths: Sequence[str], point_uid: Optional[str] = None,
                       progress: Progress = _noop) -> Dict[str, Any]:
    """把计算机上的文件作为附件挂到点位上（``point_uid`` 为空时成为独立附件）。照片照旧进照片表。"""
    rep = {"photos_new": 0, "attachments_new": 0, "skipped": 0, "warnings": [], "cancelled": False}
    files = [p for p in paths if os.path.isfile(p)]
    with fs.store.transaction():
        for i, path in enumerate(files):
            name = os.path.basename(path)
            if not progress(i, len(files), name):
                rep["cancelled"] = True
                break
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if ovobj.attachment_kind(ext) == "photo":
                r = import_photos(fs, [path], mode="point" if point_uid else "none", point_uid=point_uid)
                rep["photos_new"] += r["photos_new"]
                rep["skipped"] += r["photos_skipped"]
                rep["warnings"] += r["warnings"]
                continue
            rel, sha = fs.put_media_file("files", path)
            if fs.find_attachment(sha, point_uid=point_uid):
                rep["skipped"] += 1
                continue
            taken = datetime.fromtimestamp(os.path.getmtime(path)).astimezone().isoformat(timespec="seconds")
            fs.add_attachment(rel, sha, ext or "bin", point_uid=point_uid, orig_name=name, taken=taken, source=name)
            rep["attachments_new"] += 1
        progress(len(files), len(files), "")
    return rep


# =============================================================================
# 照片
# =============================================================================

def import_photos(fs: FieldStore, paths: Sequence[str], mode: str = "nearest",
                  point_uid: Optional[str] = None, max_dist: float = 30.0,
                  survey: str = "", progress: Progress = _noop) -> Dict[str, Any]:
    """成批导入照片。

    ``mode``：

    * ``point``   全部挂到 ``point_uid`` 指定的点位；
    * ``nearest`` 有 GPS 的照片挂到 ``max_dist`` 米内最近的点位，找不到的成为独立照片；
    * ``new``     每张有 GPS 的照片新建一个点位（名称取文件名）；
    * ``none``    全部作为独立照片，之后在界面上再拖到点位上。
    """
    rep = {"photos_new": 0, "photos_skipped": 0, "attached": 0, "points_new": 0,
           "no_gps": 0, "warnings": [], "cancelled": False}
    points = fs.list_points() if mode == "nearest" else []
    files = [p for p in paths if os.path.isfile(p)]
    with fs.store.transaction():
        for i, path in enumerate(files):
            name = os.path.basename(path)
            if not progress(i, len(files), name):
                rep["cancelled"] = True
                break
            ext = os.path.splitext(path)[1].lower()
            rel, sha = fs.put_media_file("photos", path)
            info: Dict[str, Any] = {}
            if ext in EXIF_EXTS:
                try:
                    with open(path, "rb") as fh:
                        info = photoexif.read_info(fh.read())
                except Exception as exc:
                    rep["warnings"].append("无法读取 %s 的 EXIF：%s" % (name, exc))
            if not info.get("taken"):
                info["taken"] = _taken_from_name(name)
            has_gps = info.get("lon") is not None
            if not has_gps:
                rep["no_gps"] += 1
            target = None
            if mode == "point":
                target = point_uid
            elif mode == "nearest" and has_gps and points:
                best = min(points, key=lambda p: haversine(info["lon"], info["lat"], p["lon"], p["lat"]))
                if haversine(info["lon"], info["lat"], best["lon"], best["lat"]) <= max_dist:
                    target = best["uid"]
            elif mode == "new" and has_gps:
                target = fs.add_point(info["lon"], info["lat"], name=os.path.splitext(name)[0],
                                      survey=survey or "照片导入", elevation=info.get("altitude"),
                                      source=name)
                rep["points_new"] += 1
            if fs.find_photo(target, sha):
                rep["photos_skipped"] += 1
                continue
            fs.add_photo(rel, sha, info, point_uid=target, orig_name=name, source=name)
            rep["photos_new"] += 1
            if target:
                rep["attached"] += 1
        progress(len(files), len(files), "")
    return rep


# =============================================================================
# 矢量（GPX、KML、GeoJSON、Shapefile……）
# =============================================================================

_NAME_FIELDS = ("name", "名称", "Name", "NAME", "title", "label")
_NOTE_FIELDS = ("desc", "description", "cmt", "备注", "comment", "note", "Description")


def _ogr_epoch(feat, field: str) -> Optional[int]:
    """OGR 日期时间字段 → Unix 秒。没有时区标记的按 UTC 算（GPX 规定用 UTC）。"""
    if not feat.IsFieldSetAndNotNull(field):
        return None
    y, mo, d, h, mi, sec, tzf = feat.GetFieldAsDateTime(field)
    tz = timezone(timedelta(minutes=(tzf - 100) * 15)) if tzf >= 100 else timezone.utc
    try:
        return int(datetime(y, mo, d, h, mi, int(sec), tzinfo=tz).timestamp())
    except ValueError:
        return None


def _gpx_track_points(ds) -> Dict[tuple, Dict[str, list]]:
    """GPX 航迹每个点的高程与时间，按（航迹、航段）归组、按点序排好。

    OGR 的 tracks 层几何默认不带高程（要开 ELE_AS_25D），也没有时间；这两样都在
    track_points 层的 ele、time 字段里。"""
    lyr = ds.GetLayerByName("track_points")
    out: Dict[tuple, Dict[str, list]] = {}
    if lyr is None:
        return out
    rows: Dict[tuple, list] = {}
    for f in lyr:
        key = (f.GetField("track_fid"), f.GetField("track_seg_id"))
        ele = f.GetField("ele") if f.IsFieldSetAndNotNull("ele") else None
        rows.setdefault(key, []).append((f.GetField("track_seg_point_id") or 0, ele, _ogr_epoch(f, "time")))
    for key, pts in rows.items():
        pts.sort(key=lambda r: r[0])
        out[key] = {"ele": [r[1] for r in pts], "time": [r[2] for r in pts]}
    return out


def import_vector(fs: FieldStore, path: str, survey: str = "", progress: Progress = _noop) -> Dict[str, Any]:
    """点要素成为野外点位，线要素成为轨迹。坐标统一转到 EPSG:4326。"""
    from osgeo import ogr, osr
    from ...store import make_transform, open_vector, storage_srs

    survey = survey or os.path.splitext(os.path.basename(path))[0]
    source = os.path.basename(path)
    rep = {"source": source, "survey": survey, "points_new": 0, "tracks_new": 0,
           "skipped": 0, "warnings": [], "cancelled": False}
    ds = open_vector(path)
    dst = storage_srs()
    gpx = ds.GetDriver().GetName() == "GPX"
    gpx_points = _gpx_track_points(ds) if gpx else {}
    with fs.store.transaction():
        for li in range(ds.GetLayerCount()):
            lyr = ds.GetLayer(li)
            # GPX 的 route_points / track_points 是逐点展开的重复数据
            if lyr.GetName() in ("route_points", "track_points"):
                continue
            srs = lyr.GetSpatialRef()
            tf = make_transform(srs, dst) if srs is not None else None
            defn = lyr.GetLayerDefn()
            names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
            nf = next((n for n in _NAME_FIELDS if n in names), None)
            cf = next((n for n in _NOTE_FIELDS if n in names), None)
            total = lyr.GetFeatureCount()
            for k, feat in enumerate(lyr):
                if not progress(k, total, lyr.GetName()):
                    rep["cancelled"] = True
                    break
                g = feat.GetGeometryRef()
                if g is None:
                    rep["skipped"] += 1
                    continue
                g = g.Clone()
                if tf is not None:
                    g.Transform(tf)
                name = str(feat.GetField(nf) or "") if nf else ""
                note = str(feat.GetField(cf) or "") if cf else ""
                attrs = {n: feat.GetField(n) for n in names if n not in (nf, cf) and feat.IsFieldSetAndNotNull(n)}
                attrs = {k2: v for k2, v in attrs.items() if isinstance(v, (str, int, float))}
                flat = ogr.GT_Flatten(g.GetGeometryType())
                if flat == ogr.wkbPoint:
                    elevation = g.GetZ() if g.Is3D() else None
                    if elevation is None and gpx and "ele" in names and feat.IsFieldSetAndNotNull("ele"):
                        elevation = float(feat.GetField("ele"))
                    fs.add_point(g.GetX(), g.GetY(), name=name, note=note, survey=survey, source=source,
                                 elevation=elevation, attrs=attrs or None)
                    rep["points_new"] += 1
                elif flat in (ogr.wkbLineString, ogr.wkbMultiLineString):
                    parts = [g] if flat == ogr.wkbLineString else [g.GetGeometryRef(j) for j in range(g.GetGeometryCount())]
                    for seg, part in enumerate(parts):
                        n = part.GetPointCount()
                        if n < 2:
                            continue
                        lons = [part.GetX(j) for j in range(n)]
                        lats = [part.GetY(j) for j in range(n)]
                        elev = [part.GetZ(j) for j in range(n)] if part.Is3D() else None
                        times = [int(part.GetM(j)) for j in range(n)] if part.IsMeasured() else None
                        # GPX 航迹：高程、时间从 track_points 补上；点数对不上或有缺的就不用
                        extra = gpx_points.get((feat.GetFID(), seg)) if lyr.GetName() == "tracks" else None
                        if extra and len(extra["ele"]) == n:
                            if elev is None and all(v is not None for v in extra["ele"]):
                                elev = [float(v) for v in extra["ele"]]
                            if times is None and all(v is not None for v in extra["time"]):
                                times = extra["time"]
                        fs.add_track(lons, lats, times, elev, name=name, note=note, survey=survey, source=source)
                        rep["tracks_new"] += 1
                else:
                    rep["skipped"] += 1
            if rep["cancelled"]:
                break
    ds = None
    return rep


def copy_point_to_sample(fs: FieldStore, point_uid: str, class_id: int) -> str:
    """把野外点位复制成一个点样本（显式操作；点位本身不动）。返回样本键。"""
    from osgeo import ogr
    from ...model import key_to_str

    pt = fs.get_point(point_uid)
    if pt is None:
        raise KeyError("点位不存在：%s" % point_uid)
    cls = fs.store.get_class(int(class_id))
    if cls is None:
        raise KeyError("类别不存在：%s" % class_id)
    g = ogr.Geometry(ogr.wkbPoint)
    g.AddPoint_2D(pt["lon"], pt["lat"])
    with fs.store.transaction():
        key = key_to_str(fs.store.insert_sample(
            int(class_id), g, label=pt["name"] or "", source="野外点位 %s" % (pt["survey"] or ""),
            attrs={"field_point": point_uid, "note": pt["note"] or ""}))
        feat = fs._find("fw_point", point_uid)
        feat.SetField("sample_key", key)
        fs.layer("fw_point").SetFeature(feat)
    return key
