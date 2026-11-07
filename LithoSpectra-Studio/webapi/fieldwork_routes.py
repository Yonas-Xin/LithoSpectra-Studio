# -*- coding: utf-8 -*-
"""野外记录模块：点位、轨迹、照片、光谱库链接、奥维导入、水印。

逻辑全在 :mod:`samplelib.modules.fieldwork`；这里只做三件事：把当前打开的样本库
包成 :class:`FieldStore`、把长活交给任务框架、把照片文件按需缩放后送给前端。

**原片不动。** 照片的原片只读；去水印、修补、编辑器的结果是另存的步骤，地质水印
只记参数，朝向与说明只记在表里——见 :mod:`samplelib.modules.fieldwork.fieldstore`
开头的说明。要拿到加工好的图，走「导出照片」。

**锁。** 读写 ``fw_*`` 表与别的接口一样串在 ``st.lock`` 上。去水印、加水印这类
几秒钟的图像计算不持锁：先在锁里取出照片记录与文件路径，锁外算，算完再进锁写
回——跑的时候界面照常能翻点位、看照片。
"""

import base64
import hashlib
import io
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from samplelib import config
from samplelib.modules.fieldwork import (
    asr, geomark, importer, inpaint, ovmark, photo_edit, photo_export, photoexif, spectra_link, speex,
)
from samplelib.modules.fieldwork.fieldstore import FieldStore

from .tasks import Task

#: 缩略图边长档位；请求的尺寸就近取一档，缓存才命中得上
THUMB_SIZES = (160, 480, 1600)


class PathBody(BaseModel):
    path: str
    survey: str = ""


class PathsBody(BaseModel):
    paths: List[str]


class PhotoImportBody(BaseModel):
    paths: List[str]
    mode: str = "nearest"            # point / nearest / new / none
    point_uid: Optional[str] = None
    max_dist: float = 30.0
    survey: str = ""


class PointBody(BaseModel):
    lon: float
    lat: float
    name: str = ""
    note: str = ""
    category: str = ""
    survey: str = ""


class PatchBody(BaseModel):
    changes: Dict[str, Any]


class MoveBody(BaseModel):
    point_uid: Optional[str] = None
    index: Optional[int] = None


class UidsBody(BaseModel):
    uids: List[str]
    engine: str = "auto"


class OvmarkBody(BaseModel):
    engine: str = "auto"
    write_exif: bool = True
    azimuth: Optional[float] = None   # 使用者核对后改过的朝向，优先于识别结果


class GeomarkBody(BaseModel):
    spec: Dict[str, Any]


class GeomarkPreviewBody(BaseModel):
    spec: Dict[str, Any]
    width: int = 1200
    azimuth: Optional[float] = None       # 编辑器里正在改、还没保存的照片方位角


class RectsBody(BaseModel):
    rects: List[List[float]]          # 原图（已转正）像素坐标 x0, y0, x1, y1
    engine: str = "auto"


class ReplaceBody(BaseModel):
    data_url: str                     # 编辑器导出的图（data:image/jpeg;base64,…）
    op: str = "edit"


class SampleBody(BaseModel):
    class_id: int


class ExportBody(BaseModel):
    uids: List[str] = []
    survey: Optional[str] = None          # 给了就导出这个批次全部点位的照片（忽略 uids）
    folder: str
    content: str = "adjusted"             # adjusted 调整后 / original 原片
    template: str = photo_export.DEFAULT_TEMPLATE
    info: bool = True                     # 把补记的信息补进 EXIF（只补原片缺的项）


class BatchItem(BaseModel):
    path: str
    survey: str = ""


class BatchBody(BaseModel):
    items: List[BatchItem]


class AttachImportBody(BaseModel):
    paths: List[str]
    point_uid: Optional[str] = None


class AttachMoveBody(BaseModel):
    point_uid: Optional[str] = None


class TranscribeBody(BaseModel):
    uids: List[str] = []
    point_uid: Optional[str] = None       # 给了就转写这个点位上的全部录音
    surveys: List[str] = []               # 给了就转写这些批次所有点位上的录音
    append: bool = False                  # 转写结果追加到所属点位的备注
    redo: bool = False                    # 已经转写过的也重新转


#: 浏览器认不出的几种录音格式，给出正确的类型（能不能放由页面按 canPlayType 判断）
_MIME = {"amr": "audio/amr", "m4a": "audio/mp4", "aac": "audio/aac", "caf": "audio/x-caf",
         "opus": "audio/ogg", "awb": "audio/amr-wb", "3gp": "video/3gpp"}


def _open_with_system(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif os.name == "nt":
        os.startfile(path)                         # noqa: S606（本机文件，用系统默认程序打开）
    else:
        subprocess.Popen(["xdg-open", path])


class SpectrumBody(BaseModel):
    entry_id: str
    note: str = ""


def register(app, st, guard) -> None:
    s = st.session
    cache: Dict[str, Any] = {"store": None, "fs": None}

    def fs() -> FieldStore:
        """当前样本库上的 FieldStore；库换了就重建。调用方须已持 st.lock。"""
        if not s.is_open:
            raise HTTPException(409, "请先打开或新建样本库。")
        if cache["store"] is not s.store:
            cache["store"] = s.store
            cache["fs"] = FieldStore(s.store)
        return cache["fs"]

    def start(title: str, fn, *, text: str = "", cancellable: bool = True, exclusive: bool = True):
        try:
            task = st.tasks.start(title, fn, text=text, cancellable=cancellable, exclusive=exclusive)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return task.to_json()

    def photo_or_404(uid: str) -> Dict[str, Any]:
        ph = fs().get_photo(uid)
        if ph is None:
            raise HTTPException(404, "照片不存在，可能已被删除。")
        return ph

    def read_photo(uid: str, original: bool = False):
        """在锁里取照片记录与文件内容。"""
        with st.lock:
            ph = photo_or_404(uid)
            path = fs().media_path(ph["original"] if original else ph["file"])
        if not os.path.isfile(path):
            raise HTTPException(404, "媒体文件夹中找不到照片文件：%s" % path)
        with open(path, "rb") as fh:
            return ph, fh.read()

    def save_step(uid: str, data: bytes, op: str, extra: Optional[Dict[str, Any]] = None):
        """像素修补的结果另存一个文件，作为新的一步压进步骤栈。原片不动。"""
        # 裁剪、改尺寸之后宽高变了，跟着新文件走
        info = photoexif.read_info(data)
        extra = dict(extra or {}, width=info["width"], height=info["height"])
        with st.lock:
            f = fs()
            rel, _sha = f.put_media("photos", data, "jpg")
            return f.push_step(uid, rel, op, extra)

    # ------------------------------------------------------------- 概况
    @app.get("/api/fw/status")
    def fw_status():
        return {
            "ocr": ovmark.ocr_available(),
            "inpaint": inpaint.status(),
            "spectra": spectra_link.available(),
            "speex": speex.available(),
            "asr": asr.status(),
        }

    @app.get("/api/fw/overview")
    def fw_overview():
        def run():
            f = fs()
            # 野外记录存在当前打开的样本库里（fw_* 表），照片放在库旁的同名 .media 文件夹
            return {"stats": f.stats(), "surveys": f.surveys(), "extent": f.extent(),
                    "media_dir": f.media_dir, "library": os.path.abspath(f.store.path)}
        return guard(run)

    @app.post("/api/fw/surveys/rename")
    def fw_rename_survey(body: Dict[str, str]):
        return guard(lambda: {"changed": fs().rename_survey(body.get("old", ""), body.get("new", ""))})

    @app.post("/api/fw/surveys/delete")
    def fw_delete_survey(body: Dict[str, Any]):
        """删一个批次的点位、轨迹；照片按 keep_photos 删除或留作独立照片。"""
        return guard(lambda: fs().delete_survey(str(body.get("survey") or ""), bool(body.get("keep_photos"))))

    # ------------------------------------------------------------- 点位
    @app.get("/api/fw/points")
    def fw_points(survey: str = ""):
        return guard(lambda: fs().list_points(survey or None))

    @app.get("/api/fw/points/{uid}")
    def fw_point(uid: str):
        def run():
            p = fs().get_point(uid)
            if p is None:
                raise HTTPException(404, "点位不存在，可能已被删除。")
            for sp in p["spectra"]:
                sp["entry"] = spectra_link.entry_info(sp["entry_id"])
            return p
        return guard(run)

    @app.post("/api/fw/points")
    def fw_add_point(body: PointBody):
        def run():
            f = fs()
            uid = f.add_point(body.lon, body.lat, name=body.name, note=body.note,
                              category=body.category, survey=body.survey or "手工添加",
                              elevation=f.elevation_at(body.lon, body.lat))
            return f.get_point(uid)
        return guard(run)

    @app.patch("/api/fw/points/{uid}")
    def fw_patch_point(uid: str, body: PatchBody):
        return guard(lambda: fs().update_point(uid, body.changes))

    @app.delete("/api/fw/points/{uid}")
    def fw_delete_point(uid: str, keep_photos: bool = False):
        return guard(lambda: {"deleted": fs().delete_point(uid, keep_photos=keep_photos)})

    @app.post("/api/fw/points/{uid}/to-sample")
    def fw_to_sample(uid: str, body: SampleBody):
        def run():
            key = importer.copy_point_to_sample(fs(), uid, body.class_id)
            # 样本是绕过暂存区直接落库的，暂存区要重读底稿
            if st.buffer is not None:
                st.buffer.rebase()
            return {"key": key}
        return guard(run)

    # ------------------------------------------------------------- 轨迹
    @app.get("/api/fw/tracks")
    def fw_tracks(survey: str = "", coords: bool = True):
        return guard(lambda: fs().list_tracks(survey or None, with_coords=coords))

    @app.patch("/api/fw/tracks/{uid}")
    def fw_patch_track(uid: str, body: PatchBody):
        return guard(lambda: fs().update_track(uid, body.changes))

    @app.delete("/api/fw/tracks/{uid}")
    def fw_delete_track(uid: str):
        return guard(lambda: {"deleted": fs().delete_track(uid)})

    # ------------------------------------------------------------- 附件（录音、视频、文档）
    def attachment_or_404(uid: str) -> Dict[str, Any]:
        a = fs().get_attachment(uid)
        if a is None:
            raise HTTPException(404, "附件不存在，可能已被删除。")
        return a

    @app.get("/api/fw/attachments")
    def fw_attachments(point_uid: str = "", track_uid: str = "", unattached: bool = False):
        return guard(lambda: fs().list_attachments(point_uid or None, track_uid or None, unattached))

    def attachment_wav(f: FieldStore, a: Dict[str, Any]) -> str:
        """奥维录音转好的 WAV。库与记录已在锁里取好，转换只读原文件、写缓存，不碰库，不持锁。"""
        try:
            return f.attachment_wav(a)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, "无法转换录音：%s" % exc)

    @app.get("/api/fw/attachments/{uid}/file")
    def fw_attachment_file(uid: str, as_: str = Query("", alias="as")):
        """附件原文件，内联返回（录音能在页面里直接放，支持拖进度条的分段请求）。
        ``as=wav`` 时返回奥维录音转成的 WAV。"""
        with st.lock:
            f = fs()
            a = attachment_or_404(uid)
        if a["missing"]:
            raise HTTPException(404, "媒体文件夹中找不到附件文件：%s" % a["path"])
        if as_ == "wav":
            stem = os.path.splitext(a["orig_name"] or "录音")[0]
            return FileResponse(attachment_wav(f, a), media_type="audio/wav", filename=stem + ".wav",
                                content_disposition_type="inline")
        mime = _MIME.get(a["ext"] or "") or mimetypes.guess_type("x." + (a["ext"] or ""))[0] or "application/octet-stream"
        return FileResponse(a["path"], media_type=mime, filename=a["orig_name"] or os.path.basename(a["path"]),
                            content_disposition_type="inline")

    @app.post("/api/fw/attachments/{uid}/open")
    def fw_attachment_open(uid: str):
        """用系统默认程序打开。先按原文件名复制到临时文件夹：媒体文件夹里的文件以哈希命名，
        直接打开时程序里看到的是一串哈希；改了副本也不会动到库里的原件。"""
        with st.lock:
            f = fs()
            a = attachment_or_404(uid)
        if a["missing"]:
            raise HTTPException(404, "媒体文件夹中找不到附件文件：%s" % a["path"])
        folder = os.path.join(tempfile.gettempdir(), "LithoSpectra-attachments", a["uid"])
        os.makedirs(folder, exist_ok=True)
        name = os.path.basename(a["orig_name"] or "") or os.path.basename(a["path"])
        source = a["path"]
        if a["transcode"] == "wav":
            # 奥维录音系统程序也认不得：打开转好的 WAV
            source = attachment_wav(f, a)
            name = os.path.splitext(name)[0] + ".wav"
        target = os.path.join(folder, name)
        if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(source):
            shutil.copyfile(source, target)
        try:
            _open_with_system(target)
        except OSError as exc:
            raise HTTPException(400, "无法打开：%s" % exc)
        return {"path": target}

    @app.patch("/api/fw/attachments/{uid}")
    def fw_patch_attachment(uid: str, body: PatchBody):
        return guard(lambda: fs().update_attachment(uid, body.changes))

    @app.post("/api/fw/attachments/{uid}/move")
    def fw_move_attachment(uid: str, body: AttachMoveBody):
        return guard(lambda: fs().move_attachment(uid, body.point_uid))

    @app.delete("/api/fw/attachments/{uid}")
    def fw_delete_attachment(uid: str):
        return guard(lambda: {"deleted": fs().delete_attachment(uid)})

    @app.post("/api/fw/asr/download")
    def fw_asr_download():
        return start("下载语音识别模型", lambda t: {"path": asr.download_model(t)},
                     text="正在连接下载源…", exclusive=False)

    @app.post("/api/fw/attachments/transcribe")
    def fw_transcribe(body: TranscribeBody):
        """录音转文字（逐段识别，几秒到几十秒）。可按附件、点位或批次给出要转的录音。"""
        with st.lock:
            f = fs()
            uids = list(body.uids)
            points = []
            if body.point_uid:
                points = [body.point_uid]
            elif body.surveys:
                want = set(body.surveys)
                points = [p["uid"] for p in f.list_points() if (p["survey"] or "") in want]
            for pid in points:
                uids += [a["uid"] for a in f.list_attachments(point_uid=pid)
                         if a["kind"] == "audio" and (body.redo or not a["transcript"])]
        if not uids:
            raise HTTPException(400, "没有需要转写的录音。")
        if not asr.engine_available():
            raise HTTPException(400, "没有安装语音识别引擎（sherpa-onnx）。")
        if not asr.model_ready():
            raise HTTPException(409, "还没有下载语音识别模型。")
        return start("录音转文字", lambda t: asr.transcribe_attachments(f, uids, body.append, t),
                     text="共 %d 段录音…" % len(uids))

    @app.post("/api/fw/import/attachments")
    def fw_import_attachments(body: AttachImportBody):
        with st.lock:
            f = fs()
        return start("添加附件", lambda t: importer.import_attachments(f, body.paths, body.point_uid, t),
                     text="正在复制附件…")

    # ------------------------------------------------------------- 照片
    @app.get("/api/fw/photos")
    def fw_photos(point_uid: str = "", unattached: bool = False):
        return guard(lambda: fs().list_photos(point_uid or None, unattached))

    @app.get("/api/fw/photos/{uid}")
    def fw_photo(uid: str):
        return guard(lambda: photo_or_404(uid))

    @app.get("/api/fw/photos/{uid}/image")
    def fw_photo_image(uid: str, size: int = 0, variant: str = "adjusted", original: bool = False, v: str = ""):
        """取一张图。``variant``：``adjusted`` 调整后（底图 + 地质水印）、``base`` 底图
        （像素修补的结果，编辑器在它上面接着改）、``original`` 原片。``size`` 为 0 给
        全尺寸，否则给按 EXIF 转正、长边不超过那一档的 JPEG。生成的图都缓存在媒体
        文件夹的 thumbs 下，删了会重建。"""
        if original:
            variant = "original"
        with st.lock:
            ph = photo_or_404(uid)
            f = fs()
            rel = ph["original"] if variant == "original" else ph["file"]
            path = f.media_path(rel)
            media = f.media_dir
        if not os.path.isfile(path):
            raise HTTPException(404, "媒体文件夹中找不到照片文件。")
        headers = {"Cache-Control": "max-age=31536000, immutable"}
        overlay = ph.get("overlay") if variant == "adjusted" else None
        digest = os.path.splitext(os.path.basename(rel))[0]
        if overlay:
            # 地质水印是现画的：缓存按「底图 + 水印参数」区分
            digest += "_" + hashlib.sha1(json.dumps([overlay, ph.get("azimuth")], sort_keys=True).encode()).hexdigest()[:12]
        if size <= 0 and not overlay:
            return FileResponse(path, media_type="image/jpeg", headers=headers)
        tier = 0 if size <= 0 else next((t for t in THUMB_SIZES if t >= size), THUMB_SIZES[-1])
        out = os.path.join(media, "thumbs", digest[:2], "%s_%d.jpg" % (digest, tier))
        if not os.path.isfile(out):
            with open(path, "rb") as fh:
                data = fh.read()
            if overlay:
                im = geomark.render(photoexif.open_upright(data), geomark.merge_spec(overlay),
                                    azimuth=ph.get("azimuth"))
                data = photoexif.encode_jpeg(im, quality=92)
            if tier:
                data = photoexif.thumbnail(data, tier)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out + ".part", "wb") as fh:
                fh.write(data)
            os.replace(out + ".part", out)
        return FileResponse(out, media_type="image/jpeg", headers=headers)

    @app.patch("/api/fw/photos/{uid}")
    def fw_patch_photo(uid: str, body: PatchBody):
        # 朝向、说明只记在表里，导出时才写进导出文件的 EXIF
        return guard(lambda: fs().update_photo(uid, body.changes))

    @app.post("/api/fw/photos/{uid}/move")
    def fw_move_photo(uid: str, body: MoveBody):
        def run():
            fs().move_photo(uid, body.point_uid, body.index)
            return photo_or_404(uid)
        return guard(run)

    @app.delete("/api/fw/photos/{uid}")
    def fw_delete_photo(uid: str):
        return guard(lambda: {"deleted": fs().delete_photo(uid)})

    @app.post("/api/fw/photos/{uid}/revert")
    def fw_revert_photo(uid: str):
        """清除全部调整，回到原片。"""
        return guard(lambda: fs().clear_adjustments(uid))

    @app.post("/api/fw/photos/{uid}/undo")
    def fw_undo_step(uid: str):
        """撤销最后一步像素修补。"""
        return guard(lambda: fs().undo_step(uid))

    @app.delete("/api/fw/photos/{uid}/geomark")
    def fw_remove_geomark(uid: str):
        """去掉地质水印。"""
        return guard(lambda: fs().set_overlay(uid, None))

    @app.get("/api/fw/photos/{uid}/info")
    def fw_photo_info(uid: str):
        """照片文件本身的 EXIF（位置、时间、朝向、相机）。"""
        _, data = read_photo(uid)
        return photoexif.read_info(data)

    # ------------------------------------------------------------- 奥维水印
    @app.post("/api/fw/photos/{uid}/ovmark/analyse")
    def fw_ovmark_analyse(uid: str):
        if not ovmark.ocr_available():
            raise HTTPException(400, "未安装 ocrmac，无法识别水印文字。")
        _, data = read_photo(uid)
        mark, info = ovmark.analyse(data)
        return {"mark": mark.to_json(), "exif": info}

    def _remove_one(uid: str, engine: str, write_exif: bool, azimuth: Optional[float], task: Task):
        ph0, data = read_photo(uid)
        # 只抹掉水印像素；识别出的朝向、位置记在表里，不写进底图
        out, mark, used = ovmark.remove(data, engine=engine, write_exif=False,
                                        progress=lambda d, t, x="": task(d, t, x))
        extra: Dict[str, Any] = {"ovital_mark": 0}
        az = azimuth if azimuth is not None else (mark.azimuth if mark.azimuth_ok else None)
        if az is not None and write_exif:
            extra.update(azimuth=float(az), azimuth_ref=photoexif.DEFAULT_AZIMUTH_REF,
                         azimuth_src="manual" if azimuth is not None else "ocr")
        ph = save_step(uid, out, "ovmark-remove", dict(extra, engine=used))
        if write_exif and ph0.get("lon") is None and mark.lon is not None and mark.lat is not None:
            with st.lock:
                fs().set_photo_position(uid, mark.lon, mark.lat, mark.altitude)
                ph = photo_or_404(uid)
        return {"photo": ph, "mark": mark.to_json(), "engine": used}

    @app.post("/api/fw/photos/{uid}/ovmark/remove")
    def fw_ovmark_remove(uid: str, body: OvmarkBody):
        with st.lock:
            photo_or_404(uid)
        return start("去除奥维水印", lambda t: _remove_one(uid, body.engine, body.write_exif, body.azimuth, t),
                     text="正在识别水印文字…", cancellable=False, exclusive=False)

    @app.post("/api/fw/photos/ovmark/batch")
    def fw_ovmark_batch(body: UidsBody):
        def work(task: Task):
            done, skipped, failed = [], [], []
            for i, uid in enumerate(body.uids):
                if not task(i, len(body.uids), "第 %d / %d 张" % (i + 1, len(body.uids))):
                    break
                try:
                    r = _remove_one(uid, body.engine, True, None, Task("sub", ""))
                    done.append({"uid": uid, "azimuth": r["photo"].get("azimuth"),
                                 "notes": r["mark"]["notes"]})
                except ValueError as exc:            # 没找到水印
                    skipped.append({"uid": uid, "reason": str(exc)})
                except Exception as exc:
                    failed.append({"uid": uid, "reason": str(exc)})
            task(len(body.uids), len(body.uids), "")
            return {"done": done, "skipped": skipped, "failed": failed}
        return start("批量去除奥维水印", work, text="正在准备…", exclusive=False)

    @app.post("/api/fw/photos/{uid}/inpaint")
    def fw_inpaint(uid: str, body: RectsBody):
        """手工框选要抹掉的区域（别的水印、杂物）。"""
        def work(task: Task):
            _, data = read_photo(uid)
            im = photoexif.open_upright(data)
            mask = inpaint.rect_mask(im.size, [tuple(r) for r in body.rects])
            fixed, used = inpaint.inpaint(im, mask, engine=body.engine, progress=task)
            out = photoexif.encode_jpeg(fixed, src=data)
            return {"photo": save_step(uid, out, "inpaint", {"engine": used}), "engine": used}
        return start("修补选区", work, text="正在修补…", cancellable=False, exclusive=False)

    @app.post("/api/fw/model/download")
    def fw_model_download():
        return start("下载 LaMa 修补模型", lambda t: {"path": inpaint.download_model(t)},
                     text="正在连接下载源…", exclusive=False)

    # ------------------------------------------------------------- 地质水印与编辑结果
    @app.post("/api/fw/photos/{uid}/geomark")
    def fw_geomark(uid: str, body: GeomarkBody):
        """地质水印只记参数，看图与导出时现画。原片没有朝向时，镜向同时补记成照片的朝向。"""
        def run():
            f = fs()
            spec = geomark.merge_spec(body.spec)
            ph = f.set_overlay(uid, spec if spec["items"] else None)
            az = next((it.get("azimuth") for it in spec["items"]
                       if it["type"] == "view" and it.get("azimuth") is not None), None)
            if az is not None and "azimuth" not in ph["locked"] and az != ph.get("azimuth"):
                ph = f.update_photo(uid, {"azimuth": float(az), "azimuth_src": "manual"})
            return ph
        return guard(run)

    # ------------------------------------------------------------- 地质水印默认摆放
    def defaults_path() -> str:
        return os.path.join(config.app_data_dir(), "state", "geomark_defaults.json")

    @app.get("/api/fw/geomark/defaults")
    def fw_geomark_defaults():
        """各类组件上次的样式、大小与摆放。新加的组件照此摆，同一批照片出图一致。"""
        try:
            with open(defaults_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @app.put("/api/fw/geomark/defaults")
    def fw_set_geomark_defaults(body: Dict[str, Any]):
        path = defaults_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=False, indent=1)
        os.replace(path + ".part", path)
        return body

    @app.get("/api/fw/fonts/esri_north.ttf")
    def fw_esri_font():
        """指北针样式选择器里显示符号用。"""
        path = next((p for p in geomark.ESRI_NORTH_FONTS if os.path.isfile(p)), None)
        if path is None:
            raise HTTPException(404, "未找到 ESRI North 字体。")
        return FileResponse(path, media_type="font/ttf", headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/fw/photos/{uid}/geomark/preview")
    def fw_geomark_preview(uid: str, body: GeomarkPreviewBody):
        """编辑地质水印时的预览：只画水印层（透明 PNG，宽 ``width``），外加各组件的包围框。
        比例尺按底图原尺寸标定，画在缩小的图上照样准。"""
        with st.lock:
            ph = photo_or_404(uid)
        fw_w, fw_h = int(ph.get("width") or 0), int(ph.get("height") or 0)
        if fw_w <= 0 or fw_h <= 0:
            raise HTTPException(400, "照片尺寸未知。")
        w = max(200, min(int(body.width), fw_w))
        h = max(1, round(w * fw_h / fw_w))
        spec = geomark.merge_spec(body.spec)
        from PIL import Image
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        boxes: Dict[str, List[float]] = {}
        az = body.azimuth if body.azimuth is not None else ph.get("azimuth")
        for it, patch, (x, y) in geomark.render_items((w, h), spec, fw_w, az):
            layer.alpha_composite(patch, (max(0, min(x, w - 1)), max(0, min(y, h - 1))))
            boxes[it["id"]] = [x / w, y / h, (x + patch.width) / w, (y + patch.height) / h]
        buf = io.BytesIO()
        layer.save(buf, "PNG", optimize=False, compress_level=3)
        return {"image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
                "boxes": boxes, "width": w, "height": h}

    class EditBody(BaseModel):
        spec: Dict[str, Any]

    @app.post("/api/fw/photos/{uid}/edit")
    def fw_edit(uid: str, body: EditBody):
        """照片编辑器的结果：裁剪、旋转、拉直、调色、标注，按原图分辨率在底图上重做一遍，
        存成一步调整。原片不动。"""
        if photo_edit.is_noop(body.spec):
            raise HTTPException(400, "没有任何改动。")
        with st.lock:
            photo_or_404(uid)

        def work(task: Task):
            task(0, 3, "正在读取底图…")
            _, data = read_photo(uid)
            im = photoexif.open_upright(data)
            task(1, 3, "正在按原图分辨率处理…")
            out = photo_edit.apply(im, body.spec)
            task(2, 3, "正在保存…")
            jpeg = photoexif.encode_jpeg(out, src=data)
            ph = save_step(uid, jpeg, "edit", {"edit": photo_edit.normalise(body.spec)})
            task(3, 3, "")
            return {"photo": ph}
        return start("编辑照片", work, text="正在处理…", cancellable=False, exclusive=False)

    @app.post("/api/fw/photos/{uid}/replace")
    def fw_replace(uid: str, body: ReplaceBody):
        """图片编辑器导出的结果：像素已转正，EXIF 从当前版本搬过来。"""
        head, _, b64 = body.data_url.partition(",")
        if "base64" not in head:
            raise HTTPException(400, "图片数据格式不正确。")
        edited = base64.b64decode(b64)
        _, data = read_photo(uid)
        from PIL import Image
        im = Image.open(io.BytesIO(edited)).convert("RGB")
        out = photoexif.encode_jpeg(im, src=data)
        return save_step(uid, out, body.op or "edit")

    # ------------------------------------------------------------- 导出照片
    @app.post("/api/fw/photos/export")
    def fw_export_photos(body: ExportBody):
        """把照片导出到文件夹：调整过的导出效果图，没调整过的导出原片。库里的原片不动。"""
        if not body.folder.strip():
            raise HTTPException(400, "请选择导出文件夹。")
        with st.lock:
            f = fs()
            uids = list(body.uids)
            if body.survey is not None:
                pts = sorted(f.list_points(body.survey), key=lambda p: p.get("created") or "")
                uids = [ph["uid"] for p in pts for ph in f.list_photos(point_uid=p["uid"])]
            if not uids:
                raise HTTPException(400, "没有要导出的照片。")
            jobs = photo_export.plan(f, uids)

        def work(task: Task):
            # 读库已在锁里做完；这里只读写文件，导出几百张照片时界面照常能用
            return photo_export.export_photos(jobs, body.folder.strip(), body.content,
                                              body.template, progress=task, info=body.info)
        return start("导出照片", work, text="正在准备…", exclusive=False)

    @app.get("/api/fw/photos/export/template")
    def fw_export_template():
        return {"default": photo_export.DEFAULT_TEMPLATE, "fields": photo_export.TEMPLATE_FIELDS}

    # ------------------------------------------------------------- 光谱库链接
    @app.get("/api/fw/spectra/search")
    def fw_spectra_search(q: str = "", library: str = "", limit: int = 50):
        return spectra_link.search(q, library, limit)

    @app.post("/api/fw/points/{uid}/spectra")
    def fw_link_spectrum(uid: str, body: SpectrumBody):
        def run():
            info = spectra_link.entry_info(body.entry_id)
            if info is None:
                raise ValueError("光谱库中找不到该光谱。")
            sid = fs().add_spectrum(uid, body.entry_id, label=info["name"], note=body.note)
            return fs().get_spectrum(sid)
        return guard(run)

    @app.patch("/api/fw/spectra/{uid}")
    def fw_patch_spectrum(uid: str, body: PatchBody):
        return guard(lambda: fs().update_spectrum(uid, body.changes))

    @app.delete("/api/fw/spectra/{uid}")
    def fw_unlink_spectrum(uid: str):
        return guard(lambda: {"deleted": fs().delete_spectrum(uid)})

    @app.get("/api/fw/spectra/curve")
    def fw_spectrum_curve(entry_id: str):
        try:
            return spectra_link.read_entry(entry_id)
        except spectra_link.SpectrumLinkError as exc:
            raise HTTPException(404, str(exc))

    @app.get("/api/fw/spectra/points")
    def fw_points_with_entry(entry_id: str):
        return guard(lambda: fs().points_with_entry(entry_id))

    # ------------------------------------------------------------- 导入
    @app.post("/api/fw/claim")
    def fw_claim(body: PathsBody):
        """拖进窗口的文件里哪些归野外记录：奥维收藏夹、照片、GPX 轨迹。"""
        out: Dict[str, List[str]] = {"ovobj": [], "photo": [], "vector": [], "rest": []}
        for p in body.paths:
            kind = importer.classify(p) if os.path.isfile(p) else ""
            out[kind or "rest"].append(p)
        return out

    @app.post("/api/fw/import/ovobj/preview")
    def fw_ovobj_preview(body: PathBody):
        def run():
            if not os.path.isfile(body.path):
                raise ValueError("文件不存在：%s" % body.path)
            return importer.preview_ovobj(body.path, fs())
        return guard(run)

    @app.post("/api/fw/import/ovobj")
    def fw_import_ovobj(body: PathBody):
        with st.lock:
            f = fs()
        return start("导入奥维收藏夹", lambda t: importer.import_ovobj(f, body.path, body.survey, t),
                     text="读取 %s…" % os.path.basename(body.path))

    @app.post("/api/fw/import/ovobj/preview-batch")
    def fw_ovobj_preview_batch(body: PathsBody):
        """批量导入前的一览：每份文件的内容、是否导过；读不了的单独标出。按时间先后排。"""
        def run():
            out = []
            for p in body.paths:
                try:
                    if not os.path.isfile(p):
                        raise ValueError("文件不存在")
                    out.append(importer.preview_ovobj(p, fs()))
                except Exception as exc:              # noqa: BLE001（一份坏文件不影响其余的）
                    # 一行表格里放不下完整路径：原因里的路径换成文件名
                    out.append({"path": p, "name": os.path.basename(p),
                                "error": str(exc).replace(p, os.path.basename(p))})
            out.sort(key=lambda r: (r.get("error") is not None, r.get("start") or "9999", r["name"]))
            return {"items": out}
        return guard(run)

    @app.post("/api/fw/import/ovobj-batch")
    def fw_import_ovobj_batch(body: BatchBody):
        with st.lock:
            f = fs()
        items = [{"path": it.path, "survey": it.survey} for it in body.items]
        return start("批量导入奥维收藏夹", lambda t: importer.import_ovobj_batch(f, items, t),
                     text="共 %d 份文件…" % len(items))

    @app.post("/api/fw/import/ovobj-folder")
    def fw_list_ovobj_folder(body: PathBody):
        """列出文件夹（含子文件夹）里的奥维收藏夹。"""
        if not os.path.isdir(body.path):
            raise HTTPException(400, "不是文件夹：%s" % body.path)
        found = []
        for root, dirs, names in os.walk(body.path):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            found += [os.path.join(root, n) for n in sorted(names)
                      if n.lower().endswith(".ovobj") and not n.startswith(".")]
        return {"paths": found}

    @app.post("/api/fw/import/photos")
    def fw_import_photos(body: PhotoImportBody):
        with st.lock:
            f = fs()
        return start("导入照片", lambda t: importer.import_photos(
            f, body.paths, body.mode, body.point_uid, body.max_dist, body.survey, t), text="正在读取照片…")

    @app.post("/api/fw/import/vector")
    def fw_import_vector(body: PathBody):
        with st.lock:
            f = fs()
        return start("导入轨迹与点位", lambda t: importer.import_vector(f, body.path, body.survey, t),
                     text="读取 %s…" % os.path.basename(body.path))

    @app.post("/api/fw/import/folder")
    def fw_list_folder(body: PathBody):
        """列出文件夹里的照片（导入照片时选的是文件夹）。"""
        if not os.path.isdir(body.path):
            raise HTTPException(400, "不是文件夹：%s" % body.path)
        files = sorted(os.path.join(body.path, n) for n in os.listdir(body.path)
                       if os.path.splitext(n)[1].lower() in importer.PHOTO_EXTS and not n.startswith("."))
        return {"paths": files}
