# -*- coding: utf-8 -*-
"""整景分块预测：读、算、写三段流水线。

::

    读线程                     计算（主线程）                          写线程
    GDAL 读块（含 halo）  →   原始块整块上设备（一次）           →   分类 / 置信度 / 概率栅格按块写入
    算有效掩膜（NoData、       转浮点、归一化、转推理精度              写预览 memmap、记 blocks.done
    外部范围）                 spectrum：直接取中心像元                发 block_done 事件
                              image / cube：按行切片展开 patch
                              前向；argmax 与置信度在设备上算

为什么这样设计（实测见 docs/2026-10-13_deep-learning-module-plan.md 2.1 节）：

* 一个像元的 9×9×150 patch 是原始数据的 81 倍。源项目在 CPU 上切块、经进程间
  共享内存交给主进程再上 GPU，膨胀后的数据要过两道搬运；这里只把原始块送上
  设备一次，膨胀发生在显存里，按若干行一批展开、用完即弃。
* 在设备上按坐标逐个取块很慢（MPS 上 23 万块/秒）；按整行切片是连续拷贝（59
  万块/秒）。有效像元占多数的块按行切，无效像元也算、结果丢弃；有效像元不到
  一半的块才改为只取有效像元。
* 读线程、计算、写线程之间是深度 2 的有界队列：内存里最多三块原始数据；读盘
  慢（压缩 GeoTIFF、机械硬盘、网络盘）时被藏到计算后面。GDAL 读写、numpy、
  torch 都释放 GIL，线程足够，也不像多进程 DataLoader 那样在 Windows 上要
  spawn 子进程重新 import torch。

中断后再次运行同一任务，只算 ``blocks.done`` 里没完成的块。正式成果先写成
``*.partial.tif``，全部完成后改名。
"""

from __future__ import annotations

import copy
import json
import math
import os
import queue
import threading
import time
from pathlib import Path

import numpy as np
import torch

from . import devices
from .jobs import Cancelled, Job, JobError, write_json_atomic
from .models import adapt, center_index
from .normalize import Normalizer
from .package import load_classifier, load_feature_model
from .params import Param, when

CONFIDENCE = ("max_prob", "entropy", "none")
QUEUE_DEPTH = 2
PATCH_BUDGET = 256 * 2 ** 20      # 每批展开的 patch 最多占这么多字节
SPECTRUM_BATCH = 16384
DENSE_THRESHOLD = 0.5             # 有效像元占比不低于它时按整行展开
FP16_AGREEMENT = 0.999            # 半精度与 fp32 的判定一致率低于它就退回 fp32
CONF_NODATA = -1.0
WAVELENGTH_TOLERANCE = 5.0        # nm


def params() -> tuple:
    return (
        Param("image", "影像", None, "path", path_mode="file",
              help="待预测的多波段影像，支持 GeoTIFF、ENVI 等 GDAL 可读格式"),
        Param("model", "模型", None, "path", path_mode="directory", extensions=("lithomodel",),
              help="训练任务生成的 .lithomodel 模型包"),
        Param("output", "输出分类栅格", "", "path", path_mode="file", extensions=("tif",),
              optional=True, help="留空则保存到任务目录的 outputs 文件夹"),
        Param("mask", "预测范围", "", "path", path_mode="file", optional=True,
              extensions=("shp", "gpkg", "geojson", "kml"),
              help="仅预测面内的像元，留空则预测全图"),
        Param("confidence", "置信度栅格", "max_prob", "choice", choices=CONFIDENCE,
              choice_labels=(("max_prob", "最大后验概率"), ("entropy", "熵（越大越不确定）"),
                             ("none", "不输出"))),
        Param("probabilities", "输出各类概率", False, "bool", advanced=True,
              help="多波段 float32 栅格，波段数等于类别数，文件较大"),
        Param("block_size", "分块大小", 512, "int", minimum=16, maximum=4096, advanced=True),
        Param("nodata", "无效值", "", "str", optional=True, advanced=True,
              help="留空则使用影像的 NoData 值；影像未设置 NoData 时，所有波段均为 0 的像元视为背景"),
        Param("device", "计算设备", "auto", "choice", choices=devices.DEVICE_CHOICES,
              choice_labels=(("auto", "自动"), ("cuda", "CUDA 显卡"),
                             ("mps", "Apple 芯片 GPU"), ("cpu", "CPU"))),
        Param("precision", "推理精度", "auto", "choice", choices=devices.PRECISION_CHOICES,
              choice_labels=(("auto", "自动（GPU 使用半精度）"), ("fp32", "单精度"),
                             ("fp16", "半精度")), advanced=True),
    )


def similarity_params() -> tuple:
    return (
        Param("image", "影像", None, "path", path_mode="file",
              help="待检索的影像"),
        Param("model", "模型", None, "path", path_mode="directory", extensions=("lithomodel",),
              help="对比学习骨干或分类模型的模型包，使用其骨干网络提取特征"),
        Param("query", "参考点", None, "str",
              help="格式为\"经度,纬度\"，多个点以分号分隔，取各点特征的平均值"),
        Param("output", "输出栅格", "", "path", path_mode="file", extensions=("tif",),
              optional=True, help="留空则保存到任务目录的 outputs 文件夹"),
        Param("mask", "检索范围", "", "path", path_mode="file", optional=True,
              extensions=("shp", "gpkg", "geojson", "kml")),
        Param("block_size", "分块大小", 512, "int", minimum=16, maximum=4096, advanced=True),
        Param("nodata", "无效值", "", "str", optional=True, advanced=True),
        Param("device", "计算设备", "auto", "choice", choices=devices.DEVICE_CHOICES),
        Param("precision", "推理精度", "auto", "choice", choices=devices.PRECISION_CHOICES,
              advanced=True),
    )


#: 相似度显示用的色带：低 → 高（深蓝 → 青 → 黄），0..250 对应余弦相似度 -1..1
_RAMP = ((0, (30, 34, 92)), (90, (33, 102, 172)), (170, (53, 183, 121)), (250, (253, 231, 37)))
SIM_LEVELS = 250


def similarity_lut() -> list:
    out = []
    for v in range(256):
        if v > SIM_LEVELS:
            out.append([0, 0, 0, 0])
            continue
        for (a, ca), (b, cb) in zip(_RAMP, _RAMP[1:]):
            if a <= v <= b:
                t = (v - a) / (b - a)
                out.append([round(ca[k] + (cb[k] - ca[k]) * t) for k in range(3)] + [255])
                break
    return out


# =============================================================================
# GDAL 辅助
# =============================================================================

def _gdal():
    try:
        from osgeo import gdal, ogr, osr
    except ImportError as exc:
        raise JobError("当前环境未安装 GDAL 的 Python 绑定，无法读写影像") from exc
    gdal.UseExceptions()
    ogr.UseExceptions()
    return gdal, ogr, osr


def _parse_list(text: str) -> list[float]:
    text = text.strip().strip("{}")
    out = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.append(float(part))
    return out


def image_wavelengths(ds) -> list[float]:
    """影像各波段中心波长（nm）。读 ENVI 元数据或各波段的 wavelength 项；没有返回空。"""
    values: list[float] = []
    try:
        envi = ds.GetMetadata("ENVI") or {}
        if envi.get("wavelength"):
            values = _parse_list(envi["wavelength"])
        if not values:
            for b in range(1, ds.RasterCount + 1):
                band = ds.GetRasterBand(b)
                item = band.GetMetadataItem("wavelength") or band.GetMetadataItem("WAVELENGTH")
                if item is None:
                    return []
                values.append(float(item))
    except (ValueError, RuntimeError):
        return []
    if len(values) != ds.RasterCount:
        return []
    if values and max(values) < 100:          # 微米
        values = [v * 1000 for v in values]
    return values


def _hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _create(gdal, path: Path, cols: int, rows: int, bands: int, gdal_type, gt, proj,
            nodata: float):
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(str(path), cols, rows, bands, gdal_type,
                       options=["TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256",
                                "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER",
                                "INTERLEAVE=BAND" if bands > 1 else "INTERLEAVE=PIXEL"])
    if ds is None:
        raise JobError("无法创建输出文件：%s" % path)
    if gt is not None:
        ds.SetGeoTransform(gt)
    if proj:
        ds.SetProjection(proj)
    for b in range(1, bands + 1):
        ds.GetRasterBand(b).SetNoDataValue(nodata)
    return ds


class RoiMask:
    """把范围矢量重投影到影像坐标系后放进内存图层，按块栅格化。只在读线程里用。"""

    def __init__(self, path: str, gt, proj: str) -> None:
        gdal, ogr, osr = _gdal()
        self.gdal, self.gt = gdal, gt
        src = ogr.Open(path)
        if src is None:
            raise JobError("无法打开范围矢量：%s" % path)
        self._mem = ogr.GetDriverByName("Memory").CreateDataSource("roi")
        img_srs = osr.SpatialReference(wkt=proj) if proj else None
        if img_srs is not None:
            img_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self.layer = self._mem.CreateLayer("roi", srs=img_srs, geom_type=ogr.wkbMultiPolygon)
        count = 0
        for li in range(src.GetLayerCount()):
            lyr = src.GetLayer(li)
            lsrs = lyr.GetSpatialRef()
            tr = None
            if lsrs is not None and img_srs is not None:
                lsrs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                if not lsrs.IsSame(img_srs):
                    tr = osr.CoordinateTransformation(lsrs, img_srs)
            for feat in lyr:
                geom = feat.GetGeometryRef()
                if geom is None:
                    continue
                geom = geom.Clone()
                if tr is not None:
                    geom.Transform(tr)
                out = ogr.Feature(self.layer.GetLayerDefn())
                out.SetGeometry(geom)
                self.layer.CreateFeature(out)
                count += 1
        if count == 0:
            raise JobError("范围矢量中没有要素：%s" % path)

    def window(self, xoff: int, yoff: int, w: int, h: int) -> np.ndarray:
        gt = self.gt
        mem = self.gdal.GetDriverByName("MEM").Create("", w, h, 1, self.gdal.GDT_Byte)
        mem.SetGeoTransform((gt[0] + xoff * gt[1] + yoff * gt[2], gt[1], gt[2],
                             gt[3] + xoff * gt[4] + yoff * gt[5], gt[4], gt[5]))
        self.gdal.RasterizeLayer(mem, [1], self.layer, burn_values=[1])
        return mem.GetRasterBand(1).ReadAsArray().astype(bool)


# =============================================================================
# 块
# =============================================================================

def block_grid(rows: int, cols: int, size: int) -> list[tuple[int, int, int, int]]:
    """(行偏移, 列偏移, 高, 宽)，按行优先。"""
    out = []
    for i in range(0, rows, size):
        for j in range(0, cols, size):
            out.append((i, j, min(size, rows - i), min(size, cols - j)))
    return out


def read_block(ds, i: int, j: int, h: int, w: int, patch: int) -> np.ndarray:
    """读 ``[波段, h+P-1, w+P-1]``，越界部分补 0（与样本裁剪一致）。"""
    lead = center_index(patch)
    trail = patch - 1 - lead
    r0, c0 = i - lead, j - lead
    r1, c1 = i + h + trail, j + w + trail
    rr0, cc0 = max(r0, 0), max(c0, 0)
    rr1, cc1 = min(r1, ds.RasterYSize), min(c1, ds.RasterXSize)
    arr = ds.ReadAsArray(cc0, rr0, cc1 - cc0, rr1 - rr0)
    if arr.ndim == 2:
        arr = arr[np.newaxis]
    if (rr0, cc0, rr1, cc1) == (r0, c0, r1, c1):
        return arr
    out = np.zeros((arr.shape[0], r1 - r0, c1 - c0), dtype=arr.dtype)
    out[:, rr0 - r0:rr0 - r0 + arr.shape[1], cc0 - c0:cc0 - c0 + arr.shape[2]] = arr
    return out


def valid_mask(core: np.ndarray, nodata: float | None) -> np.ndarray:
    """所有波段都等于无效值（或任一波段非有限数）的像元为背景。"""
    if nodata is None:
        bg = np.all(core == 0, axis=0)
    elif np.isnan(nodata):
        bg = np.all(np.isnan(core), axis=0)
    else:
        bg = np.all(core == nodata, axis=0)
    if np.issubdtype(core.dtype, np.floating):
        bg |= ~np.all(np.isfinite(core), axis=0)
    return ~bg


def to_torch_dtype(arr: np.ndarray) -> np.ndarray:
    """torch 对 uint16 / uint32 支持不全，读进来就转成能放下的有符号类型。"""
    if arr.dtype in (np.uint16, np.uint32):
        return arr.astype(np.int32 if arr.dtype == np.uint16 else np.int64)
    if arr.dtype == np.float64:
        return arr.astype(np.float32)
    return arr


class BlockPredictor:
    """一块原始数据 → 类别、置信度、（概率）。全部在设备上做。"""

    def __init__(self, model, manifest: dict, device: torch.device, dtype: torch.dtype,
                 confidence: str, probabilities: bool, block_width: int,
                 query: torch.Tensor | None = None) -> None:
        self.model = model
        #: 相似度检索：参考特征（已归一化）。有它时输出余弦相似度而不是类别
        self.query = query
        self.device = device
        self.dtype = dtype
        self.kind = manifest["input_kind"]
        self.patch = int(manifest["patch_size"])
        self.bands = int(manifest["bands"])
        self.num_classes = len(manifest.get("classes") or [])
        self.normalizer = Normalizer.from_json(manifest["normalization"])
        self.confidence = confidence
        self.probabilities = probabilities
        per_patch = self.bands * self.patch * self.patch * torch.finfo(dtype).bits // 8
        self.rows_per_batch = max(1, PATCH_BUDGET // max(1, per_patch * block_width))
        self.sparse_batch = max(256, PATCH_BUDGET // max(1, per_patch))

    def _forward(self, x: torch.Tensor):
        if self.query is not None:
            f = torch.nn.functional.normalize(self.model.features(adapt(x, self.kind)).float(), dim=1)
            sim = f @ self.query
            level = ((sim + 1) * (SIM_LEVELS / 2)).round().clamp(0, SIM_LEVELS).to(torch.uint8)
            return level, sim, None
        logits = self.model(adapt(x, self.kind)).float()
        prob = torch.softmax(logits, dim=1)
        conf, pred = prob.max(dim=1)
        if self.confidence == "entropy":
            conf = -(prob * torch.log(prob.clamp_min(1e-12))).sum(dim=1)
        return pred, conf, prob if self.probabilities else None

    @torch.inference_mode()
    def __call__(self, raw: np.ndarray, valid: np.ndarray):
        h, w = valid.shape
        lead = center_index(self.patch)
        t = torch.from_numpy(raw).to(self.device, non_blocking=True).float()
        self.normalizer.apply(t, band_axis=0)
        t = t.to(self.dtype)
        dense = valid.mean() >= DENSE_THRESHOLD
        preds, confs, probs = [], [], []
        if self.kind == "spectrum":
            core = t[:, lead:lead + h, lead:lead + w]
            if dense:
                spectra = core.reshape(self.bands, -1).T
            else:
                rr, cc = np.nonzero(valid)
                spectra = core[:, torch.from_numpy(rr).to(self.device),
                               torch.from_numpy(cc).to(self.device)].T
            for k in range(0, spectra.shape[0], SPECTRUM_BATCH):
                p, c, pr = self._forward(spectra[k:k + SPECTRUM_BATCH].unsqueeze(1))
                preds.append(p), confs.append(c), probs.append(pr)
        else:
            u = t.unfold(1, self.patch, 1).unfold(2, self.patch, 1)   # B, h, w, P, P（视图）
            if dense:
                for r in range(0, h, self.rows_per_batch):
                    x = u[:, r:r + self.rows_per_batch].permute(1, 2, 0, 3, 4)
                    x = x.reshape(-1, self.bands, self.patch, self.patch)
                    p, c, pr = self._forward(x)
                    preds.append(p), confs.append(c), probs.append(pr)
            else:
                rr, cc = np.nonzero(valid)
                rr_t = torch.from_numpy(rr).to(self.device)
                cc_t = torch.from_numpy(cc).to(self.device)
                for k in range(0, rr.size, self.sparse_batch):
                    x = u[:, rr_t[k:k + self.sparse_batch], cc_t[k:k + self.sparse_batch]]
                    p, c, pr = self._forward(x.permute(1, 0, 2, 3))
                    preds.append(p), confs.append(c), probs.append(pr)
        pred = torch.cat(preds).cpu().numpy() if preds else np.zeros(0, np.int64)
        conf = torch.cat(confs).cpu().numpy() if confs else np.zeros(0, np.float32)
        prob = torch.cat(probs).cpu().numpy() if self.probabilities and probs else None
        if dense:
            pred, conf = pred.reshape(h, w), conf.reshape(h, w)
            if prob is not None:
                prob = prob.T.reshape(self.num_classes, h, w)
            return pred, conf, prob, None
        return pred, conf, prob, np.nonzero(valid)


# =============================================================================
# 任务
# =============================================================================

def run_similarity(job: Job, p: dict) -> dict:
    return run(job, dict(p, confidence="max_prob", probabilities=False), mode="similarity")


def _query_vector(job: Job, model, manifest: dict, ds, text: str, device) -> torch.Tensor:
    """参考点（经纬度）→ 影像上的块 → 骨干特征的平均，归一化。"""
    _gdal_mod, _ogr, osr = _gdal()
    gdal = _gdal_mod
    srs = osr.SpatialReference()
    srs.ImportFromWkt(ds.GetProjection())
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(wgs, srs)
    inv = gdal.InvGeoTransform(ds.GetGeoTransform())
    patch = int(manifest["patch_size"])
    norm = Normalizer.from_json(manifest["normalization"])
    blocks = []
    for part in text.replace("；", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            lon, lat = (float(v) for v in part.replace("，", ",").split(","))
        except ValueError as exc:
            raise JobError("参考点格式应为\"经度,纬度\"：%r" % part) from exc
        x, y, _ = tr.TransformPoint(lon, lat)
        col, row = (int(v) for v in gdal.ApplyGeoTransform(inv, x, y))
        if not (0 <= row < ds.RasterYSize and 0 <= col < ds.RasterXSize):
            raise JobError("参考点 %s 不在影像范围内" % part)
        blocks.append(read_block(ds, row, col, 1, 1, patch).astype(np.float32))
    if not blocks:
        raise JobError("未指定参考点")
    x = torch.from_numpy(np.stack(blocks)).to(device)
    norm.apply(x, band_axis=1)
    with torch.inference_mode():
        f = torch.nn.functional.normalize(model.features(adapt(x, manifest["input_kind"])).float(), dim=1)
    q = torch.nn.functional.normalize(f.mean(dim=0), dim=0)
    job.log("参考点 %d 个" % len(blocks))
    return q


def run(job: Job, p: dict, mode: str = "classify") -> dict:
    similar = mode == "similarity"
    gdal, _ogr, _osr = _gdal()
    image = Path(p["image"])
    if not image.is_file():
        raise JobError("影像不存在：%s" % image)
    try:
        ds = gdal.Open(str(image))
    except RuntimeError as exc:
        raise JobError("无法打开影像：%s\n%s" % (image, exc)) from exc
    rows, cols, bands = ds.RasterYSize, ds.RasterXSize, ds.RasterCount
    gt = ds.GetGeoTransform(can_return_null=True)
    proj = ds.GetProjection()
    if gt is None or not proj:
        job.warn("影像没有地理参考，输出栅格将不含坐标信息")

    device = devices.resolve_device(p["device"])
    job.log("计算设备：%s" % devices.describe(device))
    if similar:
        model, manifest = load_feature_model(p["model"], device)
    else:
        model, manifest = load_classifier(p["model"], device)
    if manifest.get("partial"):
        job.warn("该模型来自中途取消的训练")
    if bands != int(manifest["bands"]):
        raise JobError("影像有 %d 个波段，模型需要 %d 个。请使用同一传感器且经过相同波段处理的影像"
                       % (bands, manifest["bands"]))
    model_wl = manifest.get("wavelengths") or []
    img_wl = image_wavelengths(ds)
    if model_wl and img_wl:
        diff = np.abs(np.asarray(img_wl) - np.asarray(model_wl))
        bad = np.nonzero(diff > WAVELENGTH_TOLERANCE)[0]
        if bad.size:
            show = "、".join("第 %d 波段（%.1f / %.1f nm）" % (b + 1, img_wl[b], model_wl[b])
                            for b in bad[:5])
            job.warn("影像与模型有 %d 个波段的波长相差超过 %g nm：%s"
                     % (bad.size, WAVELENGTH_TOLERANCE, show))
    nodata = None
    if p.get("nodata"):
        try:
            nodata = float(p["nodata"])
        except ValueError as exc:
            raise JobError("无效值必须是数字：%r" % p["nodata"]) from exc
    else:
        nodata = ds.GetRasterBand(1).GetNoDataValue()
        if nodata is None:
            job.log("影像未设置 NoData，所有波段均为 0 的像元将视为背景")

    classes = [] if similar else manifest["classes"]
    num_classes = len(classes)
    patch = int(manifest["patch_size"])
    query = _query_vector(job, model, manifest, ds, p["query"], device) if similar else None
    size = int(p["block_size"])
    grid = block_grid(rows, cols, size)

    # ------------------------------------------------------------ 输出
    if p.get("output"):
        output = Path(p["output"])
    else:
        output = job.path("outputs", "%s__%s%s.tif" % (image.stem, manifest.get("name", "model"),
                                                     "_similarity" if similar else ""))
    output.parent.mkdir(parents=True, exist_ok=True)
    stem = output.with_suffix("")
    conf_path = Path(str(stem) + ("_value.tif" if similar else "_conf.tif"))
    prob_path = Path(str(stem) + "_prob.tif")
    targets = {"class": output}
    if p["confidence"] != "none":
        targets["confidence"] = conf_path
    if p["probabilities"]:
        targets["probabilities"] = prob_path
    partial = {k: v.with_name(v.stem + ".partial.tif") for k, v in targets.items()}
    done_path = job.path("blocks.done")
    preview_path = job.path("preview.u8")

    if all(v.is_file() for v in targets.values()) and done_path.is_file() and \
            not any(v.exists() for v in partial.values()):
        job.log("结果已存在，跳过预测")
        return {k: str(v) for k, v in targets.items()}

    class_type = gdal.GDT_Byte if num_classes <= 254 else gdal.GDT_UInt16
    class_nodata = 255 if class_type == gdal.GDT_Byte else 65535
    np_class = np.uint8 if class_type == gdal.GDT_Byte else np.uint16
    resume = done_path.is_file() and all(v.is_file() for v in partial.values()) and \
        os.path.getsize(done_path) == len(grid)
    if resume:
        done = np.fromfile(done_path, dtype=np.uint8)
        outs = {k: gdal.Open(str(v), gdal.GA_Update) for k, v in partial.items()}
        job.log("继续运行：已完成 %d / %d 块" % (int(done.sum()), len(grid)))
    else:
        done = np.zeros(len(grid), dtype=np.uint8)
        done.tofile(done_path)
        outs = {"class": _create(gdal, partial["class"], cols, rows, 1, class_type, gt, proj,
                                 class_nodata)}
        band = outs["class"].GetRasterBand(1)
        if similar:
            table = gdal.ColorTable()
            for v, rgba in enumerate(similarity_lut()):
                table.SetColorEntry(v, tuple(rgba))
            band.SetRasterColorTable(table)
            band.SetRasterColorInterpretation(gdal.GCI_PaletteIndex)
            band.SetDescription("与参考点的余弦相似度，0..250 对应 -1..1")
        else:
            if class_type == gdal.GDT_Byte:
                table = gdal.ColorTable()
                for v in range(256):
                    table.SetColorEntry(v, (0, 0, 0, 0))
                for row in classes:
                    table.SetColorEntry(int(row["label"]), _hex_rgb(row["color"]) + (255,))
                band.SetRasterColorTable(table)
                band.SetRasterColorInterpretation(gdal.GCI_PaletteIndex)
            names = [""] * (max(int(r["label"]) for r in classes) + 1)
            for row in classes:
                names[int(row["label"])] = str(row["name"])
            band.SetCategoryNames(names)
        if "confidence" in partial:
            outs["confidence"] = _create(gdal, partial["confidence"], cols, rows, 1,
                                         gdal.GDT_Float32, gt, proj, CONF_NODATA)
        if "probabilities" in partial:
            outs["probabilities"] = _create(gdal, partial["probabilities"], cols, rows,
                                            num_classes, gdal.GDT_Float32, gt, proj,
                                            CONF_NODATA)
            for row in classes:
                outs["probabilities"].GetRasterBand(int(row["label"]) + 1).SetDescription(
                    str(row["name"]))
    preview_mode = "r+" if resume and preview_path.is_file() else "w+"
    preview = np.memmap(preview_path, dtype=np.uint8, mode=preview_mode, shape=(rows, cols))
    if preview_mode == "w+":
        preview[:] = 255
    todo = [k for k in range(len(grid)) if not done[k]]

    job.emit("predict_init", "预测 %d×%d，%d 块" % (cols, rows, len(grid)),
             rows=rows, cols=cols, bands=bands, geotransform=list(gt) if gt else None,
             projection=proj, block_size=size, blocks=len(grid), remaining=len(todo),
             classes=classes, preview=str(preview_path), outputs={k: str(v) for k, v in
                                                               targets.items()},
             lut=similarity_lut() if similar else None, mode=mode)

    # ------------------------------------------------------------ 精度
    dtype = devices.inference_dtype(device, p["precision"])
    model32 = model
    if dtype != torch.float32:
        model = copy.deepcopy(model32).to(dtype)
    predictor = BlockPredictor(model, manifest, device, dtype, p["confidence"],
                               bool(p["probabilities"]), min(size, cols),
                               query=query.to(device) if query is not None else None)
    checked = dtype == torch.float32 or p["precision"] == "fp16"

    # ------------------------------------------------------------ 流水线
    stop = threading.Event()
    q_in: queue.Queue = queue.Queue(maxsize=QUEUE_DEPTH)
    q_out: queue.Queue = queue.Queue(maxsize=QUEUE_DEPTH)
    roi_path = p.get("mask")

    def reader():
        try:
            src = gdal.Open(str(image))
            roi = RoiMask(roi_path, gt, proj) if roi_path else None
            for k in todo:
                if stop.is_set():
                    break
                i, j, h, w = grid[k]
                raw = read_block(src, i, j, h, w, patch)
                lead = center_index(patch)
                valid = valid_mask(raw[:, lead:lead + h, lead:lead + w], nodata)
                if roi is not None:
                    valid &= roi.window(j, i, w, h)
                q_in.put((k, to_torch_dtype(raw), valid))
            q_in.put(None)
        except BaseException as exc:          # 交给主线程抛
            q_in.put(exc)

    t_start = time.perf_counter()
    finished = {"n": 0, "pixels": 0}

    def writer():
        try:
            while True:
                item = q_out.get()
                if item is None:
                    break
                k, cls_map, conf_map, prob_map = item
                i, j, h, w = grid[k]
                outs["class"].GetRasterBand(1).WriteArray(cls_map, j, i)
                if conf_map is not None and "confidence" in outs:
                    outs["confidence"].GetRasterBand(1).WriteArray(conf_map, j, i)
                if prob_map is not None and "probabilities" in outs:
                    outs["probabilities"].WriteArray(prob_map, j, i)
                preview[i:i + h, j:j + w] = np.minimum(cls_map, 255).astype(np.uint8)
                done[k] = 1
                with open(done_path, "r+b") as fh:
                    fh.seek(k)
                    fh.write(b"\x01")
                finished["n"] += 1
                valid_px = int((cls_map != class_nodata).sum())
                finished["pixels"] += valid_px
                elapsed = time.perf_counter() - t_start
                left = len(todo) - finished["n"]
                job.emit("block_done", "", block=k, row=i, col=j, height=h, width=w,
                         valid=valid_px, done=int(done.sum()), total=len(grid),
                         elapsed=round(elapsed, 2),
                         eta=round(elapsed / finished["n"] * left, 1))
        except BaseException as exc:
            q_writer_error.append(exc)
            stop.set()

    q_writer_error: list[BaseException] = []
    t_read = threading.Thread(target=reader, daemon=True, name="litho-dl-read")
    t_write = threading.Thread(target=writer, daemon=True, name="litho-dl-write")
    t_read.start()
    t_write.start()
    cancelled = False
    try:
        while True:
            if q_writer_error:
                raise q_writer_error[0]
            if job.cancel_requested():
                cancelled = True
                break
            item = q_in.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            k, raw, valid = item
            h, w = valid.shape
            cls_map = np.full((h, w), class_nodata, dtype=np_class)
            conf_map = np.full((h, w), CONF_NODATA, dtype=np.float32) \
                if p["confidence"] != "none" else None
            prob_map = np.full((num_classes, h, w), CONF_NODATA, dtype=np.float32) \
                if p["probabilities"] else None
            if valid.any():
                if not checked:
                    predictor = _check_half(job, predictor, model32, raw, valid, manifest,
                                            device, p, min(size, cols))
                    checked = True
                pred, conf, prob, where = _predict_retry(job, predictor, raw, valid, grid[k])
                if where is None:
                    cls_map[valid] = pred[valid]
                    if conf_map is not None:
                        conf_map[valid] = conf[valid]
                    if prob_map is not None:
                        prob_map[:, valid] = prob[:, valid]
                else:
                    cls_map[where] = pred
                    if conf_map is not None:
                        conf_map[where] = conf
                    if prob_map is not None:
                        prob_map[:, where[0], where[1]] = prob.T
            q_out.put((k, cls_map, conf_map, prob_map))
    finally:
        stop.set()
        # 排空读队列，让读线程不卡在 put 上
        while t_read.is_alive():
            try:
                q_in.get(timeout=0.1)
            except queue.Empty:
                pass
        q_out.put(None)
        t_write.join()
        for dsx in outs.values():
            dsx.FlushCache()
        outs.clear()
        preview.flush()
        del preview
    if q_writer_error:
        raise q_writer_error[0]
    if cancelled:
        raise Cancelled({"done": int(done.sum()), "total": len(grid)})

    # ------------------------------------------------------------ 收尾
    job.phase("finalize", "生成金字塔并保存结果")
    if max(rows, cols) > 2048:
        dsx = gdal.Open(str(partial["class"]), gdal.GA_Update)
        levels, f = [], 2
        while max(rows, cols) / f >= 256:
            levels.append(f)
            f *= 2
        dsx.BuildOverviews("NEAREST", levels)
        dsx = None
    for key, src in partial.items():
        os.replace(src, targets[key])
        aux = Path(str(src) + ".aux.xml")
        if aux.is_file():
            os.replace(aux, Path(str(targets[key]) + ".aux.xml"))
    if not similar:
        legend = output.with_suffix(".classes.json")
        write_json_atomic(legend, {"model": manifest.get("name"), "job": job.id,
                                   "image": str(image), "nodata": class_nodata,
                                   "classes": classes})
    labels = ({"class": "相似度图（显示用）", "confidence": "相似度数值"} if similar else
              {"class": "分类栅格", "confidence": "置信度栅格", "probabilities": "概率栅格"})
    for key, path in targets.items():
        job.emit("artifact", "%s：%s" % (labels[key], path.name), kind=key, path=str(path))
    seconds = time.perf_counter() - t_start
    return {**{k: str(v) for k, v in targets.items()}, "seconds": round(seconds, 2),
            "pixels": finished["pixels"]}


def _predict_retry(job: Job, predictor: BlockPredictor, raw, valid, window):
    """单块失败（多半是显存不够）时清缓存、批减半再试一次。"""
    try:
        return predictor(raw, valid)
    except RuntimeError as exc:
        job.warn("第 %d 行 %d 列的分块出错，已减小批量重试：%s"
                 % (window[0], window[1], str(exc).splitlines()[0]))
        devices.empty_cache(predictor.device)
        predictor.rows_per_batch = max(1, predictor.rows_per_batch // 2)
        predictor.sparse_batch = max(64, predictor.sparse_batch // 2)
        try:
            return predictor(raw, valid)
        except RuntimeError as exc2:
            raise JobError("第 %d 行 %d 列（%d×%d）的分块预测失败：%s"
                           % (window[0], window[1], window[2], window[3],
                              str(exc2).splitlines()[0])) from exc2


def _check_half(job: Job, predictor: BlockPredictor, model32, raw, valid, manifest,
                device, p, width) -> BlockPredictor:
    """半精度与 fp32 在第一块的一部分像元上比判定；一致率不够就整景退回 fp32。"""
    sub = valid.copy()
    idx = np.flatnonzero(sub)
    if idx.size > 4096:
        keep = np.zeros(sub.size, dtype=bool)
        keep[idx[np.linspace(0, idx.size - 1, 4096).astype(int)]] = True
        sub &= keep.reshape(sub.shape)
    ref = BlockPredictor(model32, manifest, device, torch.float32, "none", False, width,
                         query=predictor.query)
    a = predictor(raw, sub)
    b = ref(raw, sub)
    pa = a[0][sub] if a[3] is None else a[0]
    pb = b[0][sub] if b[3] is None else b[0]
    if predictor.query is not None:      # 相似度量化成 250 级，差一级不算不一致
        agree = float((np.abs(pa.astype(np.int16) - pb.astype(np.int16)) <= 1).mean()) if pa.size else 1.0
    else:
        agree = float((pa == pb).mean()) if pa.size else 1.0
    if agree < FP16_AGREEMENT:
        job.warn("半精度与单精度的分类一致率为 %.2f%%，低于要求，改用单精度" % (agree * 100))
        return BlockPredictor(model32, manifest, device, torch.float32, p["confidence"],
                              bool(p["probabilities"]), width, query=predictor.query)
    job.log("使用半精度推理（与单精度的分类一致率为 %.2f%%）" % (agree * 100))
    return predictor
