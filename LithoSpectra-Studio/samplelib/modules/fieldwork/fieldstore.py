# -*- coding: utf-8 -*-
"""野外记录的存储层：样本库 gpkg 里的 ``fw_*`` 表 + 库旁的媒体文件夹。

表结构
------
* ``fw_point``    野外点位（Point，EPSG:4326）
* ``fw_track``    轨迹（LineString ZM：Z 是高程米，M 是 Unix 秒）
* ``fw_photo``    照片（Point，可为空；挂在点位上或独立存在）
* ``fw_attachment`` 照片以外的附件（非空间表）：录音、视频、文档等，挂在点位或轨迹上，
  也可独立存在。原文件按内容哈希存进媒体文件夹的 ``files/``，只存不改
* ``fw_spectrum`` 点位链接的光谱（非空间表）：光谱库条目 id。实测光谱先导入光谱库再链接
* ``fw_import``   导入记录（非空间表），重复导入同一份文件时提示

全部用 ``uid``（文本）互相引用，不用 FID：将来 iOS 端离线新建的记录拿不到库里的
FID，同步时靠 uid 合并。

媒体文件夹
----------
照片按 SHA-256 存放，同一张照片导入两次只占一份::

    <库名>.media/photos/ab/ab12….jpg
    <库名>.media/thumbs/…            缩略图缓存，删了会重建
    <库名>.media/files/ab/ab12….amr  录音、视频、文档等附件，同样按 SHA-256 存放
    <库名>.media/derived/…           奥维录音转成的 WAV（页面里播放用），删了会重建

照片：原片与调整
----------------
导入的照片是原始资料，``original`` 指向导入时逐字节复制进来的那份，之后任何操作都
不碰它（像素与 EXIF 都不改）。对照片做的处理是「调整」，为的是导出时出一张合用的图：

* 像素修补（去奥维水印、框选修补、裁剪旋转调色标注）：每一步的结果另存一个文件，
  按顺序记在 ``edits`` 里（步骤栈），``file`` 指向栈顶那一份；栈空时 ``file`` 就是
  原片。可以撤销最后一步，也可以清除全部。
* 地质水印（镜向、比例尺、文字）：只记参数（``overlay``），看图与导出时现画，
  随时回来改。
* 照片信息（朝向、说明、位置）：只记在表里，导出时才写进导出文件的 EXIF。

媒体文件只增不改，备份出去的库与原库共用同一个媒体文件夹也不会互相踩。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from osgeo import ogr

from ...store import SampleStore, storage_srs

T_POINT = "fw_point"
T_TRACK = "fw_track"
T_PHOTO = "fw_photo"
T_SPECTRUM = "fw_spectrum"
T_IMPORT = "fw_import"
T_ATTACH = "fw_attachment"

META_MEDIA = "fw_media_dir"

S, I, R = ogr.OFTString, ogr.OFTInteger, ogr.OFTReal

POINT_FIELDS = [
    ("uid", S), ("name", S), ("note", S),
    ("category", S),          # 点位类型：观察点、采样点、光谱测点……使用者自定
    ("survey", S),            # 所属野外批次，默认是导入文件名（奥维里一天一个文件）
    ("elevation", R),
    ("color", S), ("icon", I),
    ("attrs", S),             # 自定义属性（JSON 对象），如岩性、产状
    ("source", S), ("source_uid", S),
    ("created", S), ("modified", S),
    ("sample_key", S),        # 复制成点样本后记下样本键，避免重复复制
]
TRACK_FIELDS = [
    ("uid", S), ("name", S), ("note", S), ("survey", S), ("color", S),
    ("start_time", S), ("end_time", S), ("n_points", I), ("length_m", R),
    ("elev_min", R), ("elev_max", R),
    ("source", S), ("source_uid", S), ("created", S), ("modified", S),
]
PHOTO_FIELDS = [
    ("uid", S), ("point_uid", S), ("sort", I),
    ("file", S), ("original", S), ("sha256", S), ("orig_name", S),
    ("width", I), ("height", I), ("taken", S),
    ("altitude", R),
    ("azimuth", R), ("azimuth_ref", S), ("azimuth_src", S),   # src: exif / ocr / manual
    ("caption", S),
    ("ovital_mark", I),       # 1 = 带奥维水印，0 = 没有 / 已去除，空 = 没检查过
    ("edits", S),             # 像素修补的步骤栈（JSON 数组），每步 {op, time, file}
    ("overlay", S),           # 地质水印参数（JSON 对象），空 = 不加
    ("orig_meta", S),         # 原片 EXIF 里本来就有的照片信息（JSON 对象），这些项锁定不可改
    ("source", S), ("created", S), ("modified", S),
]
SPECTRUM_FIELDS = [
    ("uid", S), ("point_uid", S), ("sort", I),
    ("entry_id", S),          # 光谱库条目 id
    ("label", S),             # 链接时条目的名称，条目被删后仍能看出原来链的是什么
    ("note", S), ("created", S),
]
ATTACH_FIELDS = [
    ("uid", S), ("point_uid", S), ("track_uid", S), ("sort", I),
    ("kind", S),              # audio / video / file
    ("file", S), ("sha256", S), ("orig_name", S), ("ext", S), ("size", I),
    ("taken", S),             # 录制、拍摄时间（奥维记的附件时间）
    ("caption", S),
    ("transcript", S),        # 录音转写出的文字（可改）
    ("source", S), ("created", S), ("modified", S),
]
IMPORT_FIELDS = [
    ("source", S), ("sha256", S), ("kind", S), ("summary", S), ("time", S),
]

SCHEMA = {
    T_POINT: (ogr.wkbPoint, POINT_FIELDS),
    T_TRACK: (ogr.wkbLineStringZM, TRACK_FIELDS),
    T_PHOTO: (ogr.wkbPoint, PHOTO_FIELDS),
    T_SPECTRUM: (ogr.wkbNone, SPECTRUM_FIELDS),
    T_IMPORT: (ogr.wkbNone, IMPORT_FIELDS),
    T_ATTACH: (ogr.wkbNone, ATTACH_FIELDS),
}

#: 允许界面直接改的字段
POINT_EDITABLE = {"name", "note", "category", "survey", "elevation", "color", "attrs"}
TRACK_EDITABLE = {"name", "note", "survey", "color"}
PHOTO_EDITABLE = {"caption", "azimuth", "azimuth_ref", "azimuth_src", "sort", "point_uid", "taken"}
ATTACH_EDITABLE = {"caption", "orig_name", "transcript"}

#: 照片表里认作照片的扩展名；其余（早先导入时混进来的录音、文档）开库时挪到附件表
_IMAGE_EXTS = {"jpg", "jpeg", "png", "heic", "heif", "webp", "gif", "bmp", "tif", "tiff"}

#: 原片自带就锁定的照片信息：项名 → 表里对应的字段
LOCKABLE = {
    "position": ("lon", "lat"),
    "altitude": ("altitude",),
    "azimuth": ("azimuth", "azimuth_ref", "azimuth_src"),
    "taken": ("taken",),
}


def original_meta(info: Dict[str, Any]) -> Dict[str, Any]:
    """从原片 EXIF（:func:`photoexif.read_info` 的结果）里挑出本来就有的照片信息。"""
    meta: Dict[str, Any] = {}
    if info.get("lon") is not None and info.get("lat") is not None:
        meta["lon"], meta["lat"] = info["lon"], info["lat"]
    if info.get("altitude") is not None:
        meta["altitude"] = info["altitude"]
    if info.get("azimuth") is not None:
        meta["azimuth"] = info["azimuth"]
        meta["azimuth_ref"] = info.get("azimuth_ref") or "T"
    if info.get("taken"):
        meta["taken"] = info["taken"]
    return meta


def locked_items(meta: Optional[Dict[str, Any]]) -> List[str]:
    meta = meta or {}
    out = []
    if "lon" in meta:
        out.append("position")
    for k in ("altitude", "azimuth", "taken"):
        if k in meta:
            out.append(k)
    return out


def drop_locked(values: Dict[str, Any], meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """去掉改动里落在锁定项上的字段。"""
    banned = {f for item in locked_items(meta) for f in LOCKABLE[item]}
    return {k: v for k, v in values.items() if k not in banned}
SPECTRUM_EDITABLE = {"note", "sort"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def iso(dt: Optional[datetime]) -> str:
    return dt.astimezone().isoformat(timespec="seconds") if dt else ""


def new_uid() -> str:
    return uuid.uuid4().hex


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _q(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class FieldStore:
    """挂在一个已打开的 :class:`SampleStore` 上，共用它的 GDAL 连接与事务。"""

    def __init__(self, store: SampleStore) -> None:
        self.store = store
        self._ds = store._ds
        if store._update:
            self._ensure_schema()

    # ------------------------------------------------------------------ 结构
    def _ensure_schema(self) -> None:
        names = {self._ds.GetLayer(i).GetName() for i in range(self._ds.GetLayerCount())}
        for name, (gtype, fields) in SCHEMA.items():
            if name not in names:
                srs = storage_srs() if gtype != ogr.wkbNone else None
                opts = ["FID=fid"]
                if gtype != ogr.wkbNone:
                    opts += ["GEOMETRY_NAME=geom", "SPATIAL_INDEX=YES"]
                lyr = self._ds.CreateLayer(name, srs, gtype, options=opts)
            else:
                lyr = self._ds.GetLayerByName(name)
            defn = lyr.GetLayerDefn()
            have = {defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())}
            for fname, ftype in fields:
                if fname not in have:
                    lyr.CreateField(ogr.FieldDefn(fname, ftype))
        for name in (T_POINT, T_TRACK, T_PHOTO, T_SPECTRUM, T_ATTACH):
            try:
                self.store._sql('CREATE UNIQUE INDEX IF NOT EXISTS "idx_%s_uid" ON "%s"("uid")'
                                % (name, name))
            except Exception:
                pass
        for name in (T_PHOTO, T_SPECTRUM, T_ATTACH):
            try:
                self.store._sql('CREATE INDEX IF NOT EXISTS "idx_%s_point" ON "%s"("point_uid")'
                                % (name, name))
            except Exception:
                pass
        if not self.store.get_meta(META_MEDIA):
            self.store.set_meta(META_MEDIA, self.default_media_dir())
        self._move_non_images()
        self._lock_original_meta()

    def _move_non_images(self) -> None:
        """早先的奥维导入把所有附件都当照片存了：照片表里的录音、文档挪到附件表。"""
        try:
            feats = self._features(T_PHOTO)
        except Exception:
            return
        wrong = []
        for f in feats:
            rel = f.GetField("original") or f.GetField("file") or ""
            ext = os.path.splitext(rel)[1].lower().lstrip(".")
            if ext and ext not in _IMAGE_EXTS:
                wrong.append((self._row(f, PHOTO_FIELDS), rel, ext))
        if not wrong:
            return
        from .ovobj import attachment_kind
        with self.store.transaction():
            for row, rel, ext in wrong:
                try:
                    size = os.path.getsize(self.media_path(rel))
                except (OSError, ValueError):
                    size = None
                self._insert(T_ATTACH, {
                    "point_uid": row["point_uid"], "sort": row["sort"], "kind": attachment_kind(ext),
                    "file": rel, "sha256": row["sha256"], "orig_name": row["orig_name"], "ext": ext,
                    "size": size, "taken": row["taken"], "caption": row["caption"], "source": row["source"],
                    "created": row["created"] or now_iso(), "modified": now_iso()}, None)
                self._delete(T_PHOTO, row["uid"])

    def _lock_original_meta(self) -> None:
        """给还没记原片信息的照片补记，并把被改过的原有信息恢复成原片里的值。"""
        from .photoexif import read_info
        try:
            pending = self._features(T_PHOTO, "orig_meta IS NULL")
        except Exception:
            return
        if not pending:
            return
        with self.store.transaction():
            for feat in pending:
                uid, rel = feat.GetField("uid"), feat.GetField("original") or feat.GetField("file")
                try:
                    with open(self.media_path(rel), "rb") as fh:
                        meta = original_meta(read_info(fh.read()))
                except (OSError, ValueError):
                    continue                      # 文件不在，下次打开再试
                values: Dict[str, Any] = {"orig_meta": meta}
                for k in ("altitude", "azimuth", "azimuth_ref", "taken"):
                    if k in meta:
                        values[k] = meta[k]
                if "azimuth" in meta:
                    values["azimuth_src"] = "exif"
                geom = self._pt(meta["lon"], meta["lat"]) if "lon" in meta else None
                self._update(T_PHOTO, uid, values, geom)

    def layer(self, name: str) -> ogr.Layer:
        lyr = self._ds.GetLayerByName(name)
        if lyr is None:
            raise ValueError("样本库中没有野外记录表 %s（样本库可能以只读方式打开）" % name)
        return lyr

    # ------------------------------------------------------------------ 媒体文件夹
    def default_media_dir(self) -> str:
        return os.path.splitext(os.path.abspath(self.store.path))[0] + ".media"

    @property
    def media_dir(self) -> str:
        """库旁的同名 .media 优先；找不到（备份副本、另存为）时用库里记下的那个位置。"""
        local = self.default_media_dir()
        if os.path.isdir(local):
            return local
        recorded = self.store.get_meta(META_MEDIA)
        if recorded and os.path.isdir(recorded):
            return recorded
        return local

    def media_path(self, rel: str) -> str:
        if not rel:
            raise ValueError("空的媒体路径")
        full = os.path.normpath(os.path.join(self.media_dir, rel))
        if not full.startswith(os.path.normpath(self.media_dir) + os.sep):
            raise ValueError("媒体路径越界：%s" % rel)
        return full

    def put_media(self, kind: str, data: bytes, ext: str) -> Tuple[str, str]:
        """存一份媒体数据，返回 (相对路径, sha256)。内容相同的已存在就不再写。"""
        digest = sha256_bytes(data)
        rel = self._rel(kind, digest, ext)
        full = os.path.join(self.media_dir, rel)
        if not os.path.exists(full):
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = full + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, full)
        return rel, digest

    def put_media_file(self, kind: str, path: str) -> Tuple[str, str]:
        digest = sha256_file(path)
        ext = os.path.splitext(path)[1].lstrip(".").lower() or "bin"
        rel = self._rel(kind, digest, ext)
        full = os.path.join(self.media_dir, rel)
        if not os.path.exists(full):
            os.makedirs(os.path.dirname(full), exist_ok=True)
            shutil.copy2(path, full + ".part")
            os.replace(full + ".part", full)
        return rel, digest

    @staticmethod
    def _rel(kind: str, digest: str, ext: str) -> str:
        ext = (ext or "bin").lower().lstrip(".")
        return "%s/%s/%s.%s" % (kind, digest[:2], digest, ext)

    # ------------------------------------------------------------------ 通用读写
    @staticmethod
    def _row(feat: ogr.Feature, fields: Sequence[Tuple[str, int]]) -> Dict[str, Any]:
        row: Dict[str, Any] = {}
        for name, _ in fields:
            row[name] = feat.GetField(name) if feat.IsFieldSet(name) and not feat.IsFieldNull(name) else None
        return row

    def _find(self, table: str, uid: str) -> Optional[ogr.Feature]:
        lyr = self.layer(table)
        lyr.SetAttributeFilter("uid = %s" % _q(uid))
        try:
            lyr.ResetReading()
            return lyr.GetNextFeature()
        finally:
            lyr.SetAttributeFilter(None)

    def _features(self, table: str, where: Optional[str] = None) -> List[ogr.Feature]:
        lyr = self.layer(table)
        lyr.SetAttributeFilter(where)
        try:
            lyr.ResetReading()
            return [f for f in lyr]
        finally:
            lyr.SetAttributeFilter(None)

    def _set_fields(self, feat: ogr.Feature, values: Dict[str, Any]) -> None:
        for k, v in values.items():
            if v is None or v == "":
                feat.SetFieldNull(k)
            elif isinstance(v, (dict, list)):
                feat.SetField(k, json.dumps(v, ensure_ascii=False))
            else:
                feat.SetField(k, v)

    def _insert(self, table: str, values: Dict[str, Any], geom: Optional[ogr.Geometry]) -> str:
        lyr = self.layer(table)
        feat = ogr.Feature(lyr.GetLayerDefn())
        values = dict(values)
        values.setdefault("uid", new_uid())
        self._set_fields(feat, values)
        if geom is not None:
            feat.SetGeometry(geom)
        lyr.CreateFeature(feat)
        return values["uid"]

    def _update(self, table: str, uid: str, values: Dict[str, Any],
                geom: Optional[ogr.Geometry] = None) -> None:
        feat = self._find(table, uid)
        if feat is None:
            raise KeyError("记录不存在：%s" % uid)
        self._set_fields(feat, values)
        if geom is not None:
            feat.SetGeometry(geom)
        self.layer(table).SetFeature(feat)

    def _delete(self, table: str, uid: str) -> bool:
        feat = self._find(table, uid)
        if feat is None:
            return False
        self.layer(table).DeleteFeature(feat.GetFID())
        return True

    @staticmethod
    def _pt(lon: float, lat: float) -> ogr.Geometry:
        g = ogr.Geometry(ogr.wkbPoint)
        g.AddPoint_2D(float(lon), float(lat))
        return g

    # ------------------------------------------------------------------ 点位
    def _point_json(self, feat: ogr.Feature, counts: Optional[Dict[str, Tuple[int, int]]] = None) -> Dict[str, Any]:
        row = self._row(feat, POINT_FIELDS)
        g = feat.GetGeometryRef()
        row["lon"], row["lat"] = (g.GetX(), g.GetY()) if g is not None else (None, None)
        row["attrs"] = _json_obj(row.get("attrs"))
        if counts is not None:
            row["n_photos"], row["n_spectra"], row["n_attachments"] = counts.get(row["uid"], (0, 0, 0))
        return row

    def _counts(self) -> Dict[str, Tuple[int, int, int]]:
        def per_point(table: str) -> Dict[str, int]:
            return {r["point_uid"]: int(r["n"] or 0) for r in self.store._sql(
                'SELECT point_uid, COUNT(*) AS n FROM "%s" WHERE point_uid IS NOT NULL GROUP BY point_uid' % table)}
        photos, specs, atts = per_point(T_PHOTO), per_point(T_SPECTRUM), per_point(T_ATTACH)
        keys = set(photos) | set(specs) | set(atts)
        return {k: (photos.get(k, 0), specs.get(k, 0), atts.get(k, 0)) for k in keys}

    def list_points(self, survey: Optional[str] = None) -> List[Dict[str, Any]]:
        where = "survey = %s" % _q(survey) if survey else None
        counts = self._counts()
        return [self._point_json(f, counts) for f in self._features(T_POINT, where)]

    def get_point(self, uid: str) -> Optional[Dict[str, Any]]:
        feat = self._find(T_POINT, uid)
        if feat is None:
            return None
        out = self._point_json(feat, self._counts())
        out["photos"] = self.list_photos(point_uid=uid)
        out["spectra"] = self.list_spectra(uid)
        out["attachments"] = self.list_attachments(point_uid=uid)
        return out

    def find_point_by_source(self, source_uid: str) -> Optional[Dict[str, Any]]:
        feats = self._features(T_POINT, "source_uid = %s" % _q(source_uid))
        return self._point_json(feats[0]) if feats else None

    def add_point(self, lon: float, lat: float, **values) -> str:
        values = {k: v for k, v in values.items() if k in {f for f, _ in POINT_FIELDS}}
        values.setdefault("created", now_iso())
        values.setdefault("modified", values["created"])
        with self.store.transaction():
            return self._insert(T_POINT, values, self._pt(lon, lat))

    def update_point(self, uid: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        values = {k: v for k, v in changes.items() if k in POINT_EDITABLE}
        geom = None
        if "lon" in changes and "lat" in changes:
            geom = self._pt(changes["lon"], changes["lat"])
        values["modified"] = now_iso()
        with self.store.transaction():
            self._update(T_POINT, uid, values, geom)
        return self.get_point(uid)

    def delete_point(self, uid: str, keep_photos: bool = False) -> bool:
        """删点位。照片与附件默认一起删（只删记录，媒体文件留着）；``keep_photos`` 时变成
        独立的照片、附件。"""
        with self.store.transaction():
            for ph in self.list_photos(point_uid=uid):
                if keep_photos:
                    self._update(T_PHOTO, ph["uid"], {"point_uid": None})
                else:
                    self._delete(T_PHOTO, ph["uid"])
            self._release_attachments("point_uid", uid, keep_photos)
            for sp in self.list_spectra(uid):
                self._delete(T_SPECTRUM, sp["uid"])
            return self._delete(T_POINT, uid)

    def surveys(self) -> List[Dict[str, Any]]:
        """野外批次列表：每个批次的点数、轨迹数、时间范围。"""
        out: Dict[str, Dict[str, Any]] = {}
        for r in self.store._sql('SELECT survey, COUNT(*) AS n, MIN(created) AS t0, MAX(created) AS t1 '
                                 'FROM "%s" GROUP BY survey' % T_POINT):
            key = r["survey"] or ""
            out[key] = {"survey": key, "points": int(r["n"]), "tracks": 0,
                        "start": r["t0"] or "", "end": r["t1"] or ""}
        for r in self.store._sql('SELECT survey, COUNT(*) AS n, MIN(start_time) AS t0, MAX(end_time) AS t1 '
                                 'FROM "%s" GROUP BY survey' % T_TRACK):
            key = r["survey"] or ""
            item = out.setdefault(key, {"survey": key, "points": 0, "tracks": 0, "start": "", "end": ""})
            item["tracks"] = int(r["n"])
            item["start"] = min(filter(None, [item["start"], r["t0"] or ""]), default="")
            item["end"] = max(filter(None, [item["end"], r["t1"] or ""]), default="")
        return sorted(out.values(), key=lambda s: (s["start"] or "", s["survey"]))

    def rename_survey(self, old: str, new: str) -> int:
        n = 0
        with self.store.transaction():
            for table in (T_POINT, T_TRACK):
                for feat in self._features(table, "survey = %s" % _q(old)):
                    feat.SetField("survey", new)
                    self.layer(table).SetFeature(feat)
                    n += 1
        return n

    def delete_survey(self, name: str, keep_photos: bool = False) -> Dict[str, int]:
        """删掉一个野外批次：其中的点位（连同点位的光谱链接）与轨迹。照片默认一起删
        （只删记录，媒体文件留着）；``keep_photos`` 时变成独立照片。``name`` 为空串时
        删「未分批」的那些。"""
        where = "survey = %s" % _q(name) if name else "(survey IS NULL OR survey = '')"
        points = [f.GetField("uid") for f in self._features(T_POINT, where)]
        tracks = [f.GetField("uid") for f in self._features(T_TRACK, where)]
        photos = attachments = 0
        with self.store.transaction():
            for uid in points:
                for ph in self.list_photos(point_uid=uid):
                    photos += 1
                    if keep_photos:
                        self._update(T_PHOTO, ph["uid"], {"point_uid": None})
                    else:
                        self._delete(T_PHOTO, ph["uid"])
                attachments += self._release_attachments("point_uid", uid, keep_photos)
                for sp in self.list_spectra(uid):
                    self._delete(T_SPECTRUM, sp["uid"])
                self._delete(T_POINT, uid)
            for uid in tracks:
                attachments += self._release_attachments("track_uid", uid, keep_photos)
                self._delete(T_TRACK, uid)
        return {"points": len(points), "tracks": len(tracks), "photos": photos, "attachments": attachments}

    # ------------------------------------------------------------------ 轨迹
    def _track_json(self, feat: ogr.Feature, with_coords: bool) -> Dict[str, Any]:
        row = self._row(feat, TRACK_FIELDS)
        if with_coords:
            g = feat.GetGeometryRef()
            row["coords"] = ([[round(g.GetX(i), 8), round(g.GetY(i), 8), g.GetZ(i), g.GetM(i)]
                              for i in range(g.GetPointCount())] if g is not None else [])
        return row

    def list_tracks(self, survey: Optional[str] = None, with_coords: bool = True) -> List[Dict[str, Any]]:
        where = "survey = %s" % _q(survey) if survey else None
        return [self._track_json(f, with_coords) for f in self._features(T_TRACK, where)]

    def get_track(self, uid: str) -> Optional[Dict[str, Any]]:
        feat = self._find(T_TRACK, uid)
        return self._track_json(feat, True) if feat is not None else None

    def add_track(self, lons: Sequence[float], lats: Sequence[float],
                  times: Optional[Sequence[int]] = None, elevations: Optional[Sequence[float]] = None,
                  **values) -> str:
        n = len(lons)
        if n < 2:
            raise ValueError("轨迹至少需要两个点")
        g = ogr.Geometry(ogr.wkbLineStringZM)
        length = 0.0
        for i in range(n):
            z = float(elevations[i]) if elevations else 0.0
            m = float(times[i]) if times else 0.0
            g.AddPointZM(float(lons[i]), float(lats[i]), z, m)
            if i:
                length += haversine(lons[i - 1], lats[i - 1], lons[i], lats[i])
        values = {k: v for k, v in values.items() if k in {f for f, _ in TRACK_FIELDS}}
        values.update(n_points=n, length_m=round(length, 1))
        if elevations:
            values.update(elev_min=float(min(elevations)), elev_max=float(max(elevations)))
        if times:
            values.update(start_time=iso(datetime.fromtimestamp(times[0], tz=timezone.utc)),
                          end_time=iso(datetime.fromtimestamp(times[-1], tz=timezone.utc)))
        values.setdefault("created", now_iso())
        values.setdefault("modified", values["created"])
        with self.store.transaction():
            return self._insert(T_TRACK, values, g)

    def update_track(self, uid: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        values = {k: v for k, v in changes.items() if k in TRACK_EDITABLE}
        values["modified"] = now_iso()
        with self.store.transaction():
            self._update(T_TRACK, uid, values)
        feat = self._find(T_TRACK, uid)
        return self._track_json(feat, False)

    def delete_track(self, uid: str) -> bool:
        """删轨迹。轨迹上的附件留下，成为独立附件。"""
        with self.store.transaction():
            self._release_attachments("track_uid", uid, True)
            return self._delete(T_TRACK, uid)

    def find_track_by_source(self, source_uid: str) -> Optional[str]:
        feats = self._features(T_TRACK, "source_uid = %s" % _q(source_uid))
        return feats[0].GetField("uid") if feats else None

    def elevation_at(self, lon: float, lat: float, when: Optional[float] = None,
                     max_dist: float = 50.0) -> Optional[float]:
        """从轨迹上估一个高程：有时间就取时间最近的轨迹点，否则取最近的点。"""
        best: Optional[Tuple[float, float]] = None
        for feat in self._features(T_TRACK):
            g = feat.GetGeometryRef()
            if g is None:
                continue
            for i in range(g.GetPointCount()):
                d = haversine(lon, lat, g.GetX(i), g.GetY(i))
                if d > max_dist:
                    continue
                key = abs(g.GetM(i) - when) if when else d
                if best is None or key < best[0]:
                    best = (key, g.GetZ(i))
        return best[1] if best else None

    # ------------------------------------------------------------------ 照片
    def _photo_json(self, feat: ogr.Feature) -> Dict[str, Any]:
        row = self._row(feat, PHOTO_FIELDS)
        g = feat.GetGeometryRef()
        row["lon"], row["lat"] = (g.GetX(), g.GetY()) if g is not None and not g.IsEmpty() else (None, None)
        row["edits"] = _pixel_steps(_json_list(row.get("edits")), row.get("file"), row.get("original"))
        row["overlay"] = _json_obj_or_none(row.get("overlay"))
        row["adjusted"] = bool(row["edits"] or row["overlay"])
        row["orig_meta"] = _json_obj_or_none(row.get("orig_meta")) or {}
        row["locked"] = locked_items(row["orig_meta"])
        return row

    def list_photos(self, point_uid: Optional[str] = None, unattached: bool = False) -> List[Dict[str, Any]]:
        if point_uid:
            where = "point_uid = %s" % _q(point_uid)
        elif unattached:
            where = "point_uid IS NULL"
        else:
            where = None
        rows = [self._photo_json(f) for f in self._features(T_PHOTO, where)]
        rows.sort(key=lambda r: (r["point_uid"] or "", r["sort"] if r["sort"] is not None else 1 << 30,
                                 r["taken"] or ""))
        return rows

    def get_photo(self, uid: str) -> Optional[Dict[str, Any]]:
        feat = self._find(T_PHOTO, uid)
        return self._photo_json(feat) if feat is not None else None

    def find_photo(self, point_uid: Optional[str], sha256: str) -> Optional[str]:
        where = "sha256 = %s" % _q(sha256)
        where += " AND point_uid = %s" % _q(point_uid) if point_uid else " AND point_uid IS NULL"
        feats = self._features(T_PHOTO, where)
        return feats[0].GetField("uid") if feats else None

    def add_photo(self, rel: str, sha: str, info: Dict[str, Any], point_uid: Optional[str] = None,
                  **values) -> str:
        """``info`` 是 :func:`photoexif.read_info` 的结果。"""
        lon, lat = info.get("lon"), info.get("lat")
        geom = self._pt(lon, lat) if lon is not None and lat is not None else None
        if point_uid and values.get("sort") is None:
            values["sort"] = len(self.list_photos(point_uid=point_uid))
        row = {
            "point_uid": point_uid, "file": rel, "original": rel, "sha256": sha,
            "width": info.get("width"), "height": info.get("height"),
            "taken": info.get("taken") or "", "altitude": info.get("altitude"),
            "azimuth": info.get("azimuth"), "azimuth_ref": info.get("azimuth_ref") or "",
            "azimuth_src": "exif" if info.get("azimuth") is not None else "",
            "orig_meta": original_meta(info),
            "created": now_iso(),
        }
        row.update({k: v for k, v in values.items() if k in {f for f, _ in PHOTO_FIELDS}})
        row["modified"] = row["created"]
        with self.store.transaction():
            return self._insert(T_PHOTO, row, geom)

    def update_photo(self, uid: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        """改照片信息。原片 EXIF 里本来就有的项（位置、海拔、朝向、拍摄时间）不能改，
        落在这些项上的改动直接忽略；原片缺的项可以补。"""
        ph = self.get_photo(uid)
        if ph is None:
            raise KeyError("照片不存在：%s" % uid)
        values = drop_locked({k: v for k, v in changes.items() if k in PHOTO_EDITABLE}, ph["orig_meta"])
        values["modified"] = now_iso()
        with self.store.transaction():
            self._update(T_PHOTO, uid, values)
        return self.get_photo(uid)

    def push_step(self, uid: str, rel: str, op: str,
                  extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """像素修补追加一步：``rel`` 是这一步的结果文件，成为新的底图。原片不动。"""
        ph = self.get_photo(uid)
        if ph is None:
            raise KeyError("照片不存在：%s" % uid)
        note = {k: v for k, v in (extra or {}).items() if k not in ("width", "height")}
        step = dict({"op": op, "time": now_iso(), "file": rel}, **note)
        values: Dict[str, Any] = {"file": rel, "edits": ph["edits"] + [step], "modified": now_iso()}
        values.update(drop_locked(extra or {}, ph["orig_meta"]))
        values = {k: v for k, v in values.items() if k in {f for f, _ in PHOTO_FIELDS}}
        with self.store.transaction():
            self._update(T_PHOTO, uid, values)
        return self.get_photo(uid)

    def undo_step(self, uid: str) -> Dict[str, Any]:
        """撤销最后一步像素修补，底图回到上一步的结果（没有上一步就是原片）。"""
        ph = self.get_photo(uid)
        if ph is None:
            raise KeyError("照片不存在：%s" % uid)
        steps = ph["edits"][:-1]
        values: Dict[str, Any] = {"edits": steps, "file": steps[-1]["file"] if steps else ph["original"],
                                  "modified": now_iso()}
        if not any(s["op"] == "ovmark-remove" for s in steps):
            values["ovital_mark"] = None
        values.update(self._size_of(values["file"]))
        with self.store.transaction():
            self._update(T_PHOTO, uid, values)
        return self.get_photo(uid)

    def clear_adjustments(self, uid: str) -> Dict[str, Any]:
        """清除全部调整：像素修补与地质水印都去掉，回到原片。照片信息（朝向、说明）保留。"""
        ph = self.get_photo(uid)
        if ph is None:
            raise KeyError("照片不存在：%s" % uid)
        values: Dict[str, Any] = {"file": ph["original"], "edits": [], "overlay": None,
                                  "ovital_mark": None, "modified": now_iso()}
        values.update(self._size_of(ph["original"]))
        with self.store.transaction():
            self._update(T_PHOTO, uid, values)
        return self.get_photo(uid)

    def set_overlay(self, uid: str, spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """设置（``None`` 为去掉）地质水印参数。只记参数，不画进任何文件。"""
        with self.store.transaction():
            self._update(T_PHOTO, uid, {"overlay": spec or None, "modified": now_iso()})
        return self.get_photo(uid)

    def set_photo_position(self, uid: str, lon: float, lat: float,
                           altitude: Optional[float] = None) -> None:
        """补上照片的位置（照片自身没有 GPS、从水印里识别出来时）。只改表，不改文件；
        原片自带的位置、海拔不动。"""
        ph = self.get_photo(uid)
        if ph is None:
            raise KeyError("照片不存在：%s" % uid)
        locked = ph["locked"]
        values: Dict[str, Any] = {"modified": now_iso()}
        if altitude is not None and "altitude" not in locked:
            values["altitude"] = float(altitude)
        geom = self._pt(lon, lat) if "position" not in locked else None
        with self.store.transaction():
            self._update(T_PHOTO, uid, values, geom)

    def _size_of(self, rel: str) -> Dict[str, Any]:
        try:
            from .photoexif import read_info
            with open(self.media_path(rel), "rb") as fh:
                info = read_info(fh.read())
            return {"width": info["width"], "height": info["height"]}
        except (OSError, ValueError):
            return {}

    def move_photo(self, uid: str, point_uid: Optional[str], index: Optional[int] = None) -> None:
        """把照片挂到另一个点位（或摘下来成为独立照片），并放到 ``index`` 位置。"""
        with self.store.transaction():
            siblings = [p["uid"] for p in self.list_photos(point_uid=point_uid)] if point_uid else []
            if uid in siblings:
                siblings.remove(uid)
            pos = len(siblings) if index is None else max(0, min(index, len(siblings)))
            siblings.insert(pos, uid)
            self._update(T_PHOTO, uid, {"point_uid": point_uid, "modified": now_iso()})
            for i, s in enumerate(siblings):
                self._update(T_PHOTO, s, {"sort": i})

    def delete_photo(self, uid: str) -> bool:
        with self.store.transaction():
            return self._delete(T_PHOTO, uid)

    # ------------------------------------------------------------------ 附件
    def _attachment_json(self, feat: ogr.Feature) -> Dict[str, Any]:
        row = self._row(feat, ATTACH_FIELDS)
        try:
            row["path"] = self.media_path(row["file"])
            row["missing"] = not os.path.isfile(row["path"])
        except ValueError:
            row["path"], row["missing"] = "", True
        # 奥维录音（spx）浏览器放不了：能解码时页面改要转好的 WAV
        from . import speex
        row["transcode"] = "wav" if row.get("ext") == "spx" and speex.available() else None
        return row

    def attachment_wav(self, attachment: Union[str, Dict[str, Any]]) -> str:
        """奥维录音转成的 WAV 的路径，第一次要时转换并缓存在 ``derived/``。原文件不动。

        ``attachment`` 给附件记录（:meth:`get_attachment` 的结果）时不再读库，接口层可以
        在锁里取记录、锁外做转换；给 uid 时先查记录。"""
        from . import speex
        a = self.get_attachment(attachment) if isinstance(attachment, str) else attachment
        if a is None:
            raise KeyError("附件不存在：%s" % attachment)
        target = os.path.join(self.media_dir, "derived", a["sha256"][:2], a["sha256"] + ".wav")
        if os.path.isfile(target):
            return target
        with open(a["path"], "rb") as fh:
            data = fh.read()
        if not speex.is_ospx(data):
            raise ValueError("「%s」不是奥维录音，无需转换" % (a["orig_name"] or ""))
        wav = speex.ospx_to_wav(data)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # 播放与转写可能同时要同一段：各写各的临时文件，换上去的总是完整的一份
        part = "%s.%d.part" % (target, threading.get_ident())
        with open(part, "wb") as fh:
            fh.write(wav)
        os.replace(part, target)
        return target

    def list_attachments(self, point_uid: Optional[str] = None, track_uid: Optional[str] = None,
                         unattached: bool = False) -> List[Dict[str, Any]]:
        if point_uid:
            where = "point_uid = %s" % _q(point_uid)
        elif track_uid:
            where = "track_uid = %s" % _q(track_uid)
        elif unattached:
            where = "point_uid IS NULL AND track_uid IS NULL"
        else:
            where = None
        rows = [self._attachment_json(f) for f in self._features(T_ATTACH, where)]
        rows.sort(key=lambda r: (r["sort"] if r["sort"] is not None else 1 << 30, r["taken"] or "", r["orig_name"] or ""))
        return rows

    def get_attachment(self, uid: str) -> Optional[Dict[str, Any]]:
        feat = self._find(T_ATTACH, uid)
        return self._attachment_json(feat) if feat is not None else None

    def find_attachment(self, sha256: str, point_uid: Optional[str] = None,
                        track_uid: Optional[str] = None) -> Optional[str]:
        where = "sha256 = %s" % _q(sha256)
        where += " AND point_uid = %s" % _q(point_uid) if point_uid else " AND point_uid IS NULL"
        where += " AND track_uid = %s" % _q(track_uid) if track_uid else " AND track_uid IS NULL"
        feats = self._features(T_ATTACH, where)
        return feats[0].GetField("uid") if feats else None

    def add_attachment(self, rel: str, sha: str, ext: str, point_uid: Optional[str] = None,
                       track_uid: Optional[str] = None, **values) -> str:
        from .ovobj import attachment_kind
        ext = (ext or "").lower().lstrip(".")
        try:
            size = os.path.getsize(self.media_path(rel))
        except (OSError, ValueError):
            size = None
        if values.get("sort") is None:
            values["sort"] = len(self.list_attachments(point_uid=point_uid, track_uid=track_uid,
                                                       unattached=not (point_uid or track_uid)))
        row = {"point_uid": point_uid, "track_uid": track_uid, "kind": attachment_kind(ext),
               "file": rel, "sha256": sha, "ext": ext, "size": size, "created": now_iso()}
        row.update({k: v for k, v in values.items() if k in {f for f, _ in ATTACH_FIELDS}})
        row["modified"] = row["created"]
        with self.store.transaction():
            return self._insert(T_ATTACH, row, None)

    def update_attachment(self, uid: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        values = {k: v for k, v in changes.items() if k in ATTACH_EDITABLE}
        values["modified"] = now_iso()
        with self.store.transaction():
            self._update(T_ATTACH, uid, values)
        return self.get_attachment(uid)

    def move_attachment(self, uid: str, point_uid: Optional[str]) -> Dict[str, Any]:
        """把附件挂到另一个点位（``None`` 为摘下来成为独立附件），排在最后。"""
        n = len(self.list_attachments(point_uid=point_uid, unattached=not point_uid))
        with self.store.transaction():
            self._update(T_ATTACH, uid, {"point_uid": point_uid, "track_uid": None, "sort": n,
                                         "modified": now_iso()})
        return self.get_attachment(uid)

    def delete_attachment(self, uid: str) -> bool:
        with self.store.transaction():
            return self._delete(T_ATTACH, uid)

    def _release_attachments(self, column: str, uid: str, keep: bool) -> int:
        """点位、轨迹删掉时处理它的附件：留下（成为独立附件）或一起删记录。返回个数。"""
        rows = self.list_attachments(**{column: uid})
        for a in rows:
            if keep:
                self._update(T_ATTACH, a["uid"], {column: None})
            else:
                self._delete(T_ATTACH, a["uid"])
        return len(rows)

    # ------------------------------------------------------------------ 光谱
    def points_with_entry(self, entry_id: str) -> List[str]:
        return [f.GetField("point_uid") for f in self._features(T_SPECTRUM, "entry_id = %s" % _q(entry_id))]

    def list_spectra(self, point_uid: str) -> List[Dict[str, Any]]:
        rows = [self._row(f, SPECTRUM_FIELDS)
                for f in self._features(T_SPECTRUM, "point_uid = %s" % _q(point_uid))]
        rows.sort(key=lambda r: (r["sort"] if r["sort"] is not None else 1 << 30, r["created"] or ""))
        return rows

    def get_spectrum(self, uid: str) -> Optional[Dict[str, Any]]:
        feat = self._find(T_SPECTRUM, uid)
        return self._row(feat, SPECTRUM_FIELDS) if feat is not None else None

    def add_spectrum(self, point_uid: str, entry_id: str, **values) -> str:
        if self._find(T_POINT, point_uid) is None:
            raise KeyError("点位不存在：%s" % point_uid)
        existing = self.list_spectra(point_uid)
        for row in existing:
            if row["entry_id"] == entry_id:
                return row["uid"]
        row = {"point_uid": point_uid, "entry_id": entry_id, "created": now_iso(),
               "sort": len(existing)}
        row.update({k: v for k, v in values.items() if k in {f for f, _ in SPECTRUM_FIELDS}})
        with self.store.transaction():
            return self._insert(T_SPECTRUM, row, None)

    def update_spectrum(self, uid: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        values = {k: v for k, v in changes.items() if k in SPECTRUM_EDITABLE}
        with self.store.transaction():
            self._update(T_SPECTRUM, uid, values)
        return self.get_spectrum(uid)

    def delete_spectrum(self, uid: str) -> bool:
        with self.store.transaction():
            return self._delete(T_SPECTRUM, uid)

    # ------------------------------------------------------------------ 导入记录
    def log_import(self, source: str, sha: str, kind: str, summary: str) -> None:
        with self.store.transaction():
            lyr = self.layer(T_IMPORT)
            feat = ogr.Feature(lyr.GetLayerDefn())
            self._set_fields(feat, {"source": source, "sha256": sha, "kind": kind,
                                    "summary": summary, "time": now_iso()})
            lyr.CreateFeature(feat)

    def imported_before(self, sha: str) -> Optional[Dict[str, Any]]:
        feats = self._features(T_IMPORT, "sha256 = %s" % _q(sha))
        return self._row(feats[-1], IMPORT_FIELDS) if feats else None

    # ------------------------------------------------------------------ 范围与统计
    def extent(self) -> Optional[List[float]]:
        """点位、轨迹、照片合起来的范围；没有坐标的照片不算。

        不用图层的 GetExtent：一张照片都没有 GPS 时它给出 (0, 0, 0, 0)，并进来就把范围
        拉到了经纬度原点。"""
        box: Optional[List[float]] = None
        for table in (T_POINT, T_TRACK, T_PHOTO):
            lyr = self.layer(table)
            if lyr.GetFeatureCount() == 0:
                continue
            ext = self._geom_extent(table)
            if ext is None:
                continue
            x0, y0, x1, y1 = ext
            box = [x0, y0, x1, y1] if box is None else [
                min(box[0], x0), min(box[1], y0), max(box[2], x1), max(box[3], y1)]
        return box

    def _geom_extent(self, table: str) -> Optional[List[float]]:
        try:
            rows = self.store._sql(
                'SELECT MIN(ST_MinX(geom)) AS x0, MIN(ST_MinY(geom)) AS y0, MAX(ST_MaxX(geom)) AS x1, '
                'MAX(ST_MaxY(geom)) AS y1 FROM "%s" WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)' % table)
        except RuntimeError:
            rows = []
        if rows:
            r = rows[0]
            vals = [r.get(k) for k in ("x0", "y0", "x1", "y1")]
            return [float(v) for v in vals] if all(v is not None for v in vals) else None
        # 读不了 SQL（老版本 GDAL）：退回图层范围，全零的当作没有
        try:
            x0, x1, y0, y1 = self.layer(table).GetExtent()
        except RuntimeError:
            return None
        return None if (x0, x1, y0, y1) == (0, 0, 0, 0) else [x0, y0, x1, y1]

    def stats(self) -> Dict[str, int]:
        return {t: self.layer(t).GetFeatureCount() for t in (T_POINT, T_TRACK, T_PHOTO, T_SPECTRUM, T_ATTACH)}

    def referenced_media(self) -> set:
        refs = set()
        for r in self.store._sql('SELECT file, original FROM "%s"' % T_PHOTO):
            refs.update(v for v in (r["file"], r["original"]) if v)
        for r in self.store._sql('SELECT edits FROM "%s" WHERE edits IS NOT NULL' % T_PHOTO):
            refs.update(e.get("file") for e in _json_list(r["edits"]) if e.get("file"))
        for r in self.store._sql('SELECT file FROM "%s"' % T_ATTACH):
            if r["file"]:
                refs.add(r["file"])
        return refs


def _json_obj(raw) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _json_obj_or_none(raw) -> Optional[Dict[str, Any]]:
    """JSON 对象；空的、坏的一律当没有（``None``）。"""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) and v else None
    except (TypeError, ValueError):
        return None


def _pixel_steps(edits: List[Dict[str, Any]], file: Optional[str],
                 original: Optional[str]) -> List[Dict[str, Any]]:
    """把记录里的编辑列表整理成像素修补的步骤栈。

    新记录本来就是步骤栈。早先的版本把每次编辑（含「还原」）都追加进同一个历史
    列表：取最后一次「还原」之后的那段当步骤栈，与 ``file`` 对得上；对不上（数据
    不完整）时把当前文件当成一步「早先的编辑」。
    """
    if not edits:
        return [] if file == original else [{"op": "legacy", "time": "", "file": file}]
    last_revert = max((i for i, e in enumerate(edits) if e.get("op") == "revert"), default=-1)
    steps = [e for e in edits[last_revert + 1:] if e.get("file")]
    if file == original:
        return []
    if not steps or steps[-1].get("file") != file:
        return steps + [{"op": "legacy", "time": "", "file": file}]
    return steps


def _json_list(raw) -> List[Dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []
