# -*- coding: utf-8 -*-
"""把照片导出成文件：调整后的效果图，或原片。

库里的原片始终不动。调整过的照片导出时在像素修补的底图上现画地质水印；没调整过的
照片导出的就是原片。原片 EXIF 里已有的信息（位置、海拔、朝向、拍摄时间）原样保留，
库里补记的、原片缺的信息可以选择补进 EXIF——只插入缺的标签，像素不重新压缩。不补
信息时，没调整过的照片逐字节复制。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import geomark, photoexif
from .fieldstore import FieldStore

#: 文件名模板里能用的字段
TEMPLATE_FIELDS = {
    "点位": "点位名称（未关联点位的照片为「未关联点位」）",
    "序号": "照片在点位里的顺序，两位数",
    "原名": "导入时的文件名（不含扩展名）",
    "日期": "拍摄日期，如 20260810",
    "批次": "点位所属的野外批次",
}
DEFAULT_TEMPLATE = "{点位}_{序号}"

_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]+')

Progress = Callable[[int, int, str], bool]


def _clean(name: str) -> str:
    name = _BAD.sub("_", name).strip(" .")
    return name or "照片"


def _is_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


def file_name(template: str, ph: Dict[str, Any], point: Optional[Dict[str, Any]], index: int) -> str:
    """按模板拼出不含扩展名的文件名。模板里写错的字段原样保留，不报错。"""
    taken = ph.get("taken") or ""
    try:
        day = datetime.fromisoformat(taken).strftime("%Y%m%d") if taken else ""
    except ValueError:
        day = ""
    values = {
        "点位": (point or {}).get("name") or ("未关联点位" if not point else "未命名点位"),
        "序号": "%02d" % (index + 1),
        "原名": os.path.splitext(ph.get("orig_name") or "")[0] or ph["uid"][:8],
        "日期": day,
        "批次": (point or {}).get("survey") or "",
    }
    out = re.sub(r"\{([^{}]+)\}", lambda m: values.get(m.group(1), m.group(0)), template or DEFAULT_TEMPLATE)
    return _clean(out)


def plan(fs: FieldStore, uids: Sequence[str]) -> List[Dict[str, Any]]:
    """读库，列出每张要导出的照片：记录、所属点位、在点位里的序号、文件路径。

    读库要在样本库的锁里做；后面读写文件的活不碰库，可以放到锁外慢慢做。
    """
    points: Dict[str, Optional[Dict[str, Any]]] = {}
    order: Dict[str, int] = {}
    jobs: List[Dict[str, Any]] = []
    for i, uid in enumerate(uids):
        ph = fs.get_photo(uid)
        if ph is None:
            jobs.append({"uid": uid, "missing": True})
            continue
        pu = ph.get("point_uid")
        if pu and pu not in points:
            points[pu] = fs.get_point(pu)
            order.update({p["uid"]: k for k, p in enumerate(fs.list_photos(point_uid=pu))})
        jobs.append({"uid": uid, "photo": ph, "point": points.get(pu) if pu else None,
                     "index": order.get(uid, i),
                     "base_path": fs.media_path(ph["file"]),
                     "original_path": fs.media_path(ph["original"])})
    return jobs


def render_adjusted(job: Dict[str, Any], quality: int = 92) -> bytes:
    """调整后的效果：底图（像素修补的结果，没有修补就是原片）上画地质水印。底图与
    导出文件都带着原片的 EXIF。"""
    with open(job["base_path"], "rb") as fh:
        base = fh.read()
    overlay = job["photo"].get("overlay")
    if not overlay:
        return base
    im = geomark.render(photoexif.open_upright(base), geomark.merge_spec(overlay),
                        azimuth=job["photo"].get("azimuth"))
    return photoexif.encode_jpeg(im, src=base, quality=quality)


def supplements(ph: Dict[str, Any], point: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """原片缺、库里补记了的照片信息。原片自带的项不在其中，写入时也只补缺项。"""
    locked = set(ph.get("locked") or [])
    out: Dict[str, Any] = {}
    if "azimuth" not in locked and ph.get("azimuth") is not None:
        out["azimuth"] = ph["azimuth"]
        out["azimuth_ref"] = ph.get("azimuth_ref") or photoexif.DEFAULT_AZIMUTH_REF
    if "position" not in locked:
        lon, lat = ph.get("lon"), ph.get("lat")
        if (lon is None or lat is None) and point:
            lon, lat = point.get("lon"), point.get("lat")
        if lon is not None and lat is not None:
            out["lon"], out["lat"] = lon, lat
    if "altitude" not in locked:
        alt = ph.get("altitude")
        if alt is None and point:
            alt = point.get("elevation")
        if alt is not None:
            out["altitude"] = alt
    text = ph.get("caption") or ""
    if point and point.get("name"):
        text = "%s %s" % (point["name"], text) if text else point["name"]
    if text:
        out["description"] = text
    return out


def with_info(data: bytes, ph: Dict[str, Any], point: Optional[Dict[str, Any]]) -> bytes:
    """把补记的照片信息写进 JPEG 的 EXIF，只补缺项、不重新压缩；没有要补的就原样返回。
    非 JPEG（HEIC、PNG、TIFF）写不进去，原样返回。"""
    if not _is_jpeg(data):
        return data
    extra = supplements(ph, point)
    if not extra:
        return data
    return photoexif.write_tags(data, **extra)


def export_photos(jobs: Sequence[Dict[str, Any]], folder: str, content: str = "adjusted",
                  template: str = DEFAULT_TEMPLATE, progress: Optional[Progress] = None,
                  info: bool = True) -> Dict[str, Any]:
    """按 :func:`plan` 的清单导出到 ``folder``。

    ``content``：``adjusted`` 调整过的照片导出效果图，没调整过的导出原片；``original``
    一律导出原片。``info`` 为真时把库里补记的信息（原片缺的朝向、位置、说明）写进
    EXIF，原片已有的信息不动；为假时原片逐字节复制。同名文件不覆盖，在名字后面加 _2、_3…
    """
    os.makedirs(folder, exist_ok=True)
    written: List[str] = []
    failed: List[Dict[str, str]] = []
    for i, job in enumerate(jobs):
        if job.get("missing"):
            failed.append({"uid": job["uid"], "reason": "照片不存在"})
            continue
        ph, point = job["photo"], job["point"]
        if progress and not progress(i, len(jobs), ph.get("orig_name") or ""):
            break
        try:
            if content == "adjusted" and ph.get("adjusted"):
                data = render_adjusted(job)
                ext = ".jpg"
            else:
                with open(job["original_path"], "rb") as fh:
                    data = fh.read()
                ext = os.path.splitext(ph["original"])[1] or ".jpg"
            if info:
                data = with_info(data, ph, point)
            base = file_name(template, ph, point, job["index"])
            path = os.path.join(folder, base + ext)
            n = 2
            while os.path.exists(path):
                path = os.path.join(folder, "%s_%d%s" % (base, n, ext))
                n += 1
            with open(path + ".part", "wb") as fh:
                fh.write(data)
            os.replace(path + ".part", path)
            written.append(path)
        except Exception as exc:                         # 一张坏了不影响别的
            failed.append({"uid": job["uid"], "reason": str(exc) or exc.__class__.__name__})
    if progress:
        progress(len(jobs), len(jobs), "")
    return {"folder": folder, "written": len(written), "files": written[:50], "failed": failed}
