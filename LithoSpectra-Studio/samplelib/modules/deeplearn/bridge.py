# -*- coding: utf-8 -*-
"""深度学习结果与样本库、画布之间的联动。

* 评估结果里的每个样本块 → 它在影像上的位置（经纬度）与样本库里的样本主键，
  混淆矩阵点一格就能列出、定位那些块；
* 置信度栅格 → "疑难区"矢量：低于阈值的像元连成面，标注时在里面补样本。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from osgeo import gdal, ogr, osr

from samplelib import config

from . import catalog


def _to_lonlat(gt, proj: str):
    src = osr.SpatialReference()
    src.ImportFromWkt(proj)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(config.STORAGE_EPSG)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)

    def convert(row: float, col: float):
        x, y = gdal.ApplyGeoTransform(gt, col + 0.5, row + 0.5)
        lon, lat, _ = tr.TransformPoint(x, y)
        return lon, lat
    return convert


def _source_georef(h5_path: str):
    import h5py
    with h5py.File(h5_path, "r") as fh:
        a = fh.attrs
        gt = [float(v) for v in a.get("source_geotransform", [])]
        proj = str(a.get("source_projection", ""))
    return gt, proj


def eval_samples(job_dir: Path, split: str = "val", true_label: Optional[int] = None,
                 pred_label: Optional[int] = None, errors_only: bool = True,
                 limit: int = 500) -> Dict[str, Any]:
    """读训练任务的逐样本评估结果，按真值 / 预测筛选，附上位置与样本主键。"""
    name = {"val": "val_best.npz", "test": "test.npz"}.get(split)
    if name is None:
        raise ValueError("split 只能是 val 或 test")
    path = job_dir / "eval" / name
    if not path.is_file():
        raise ValueError("该任务尚无%s集的评估结果" % ("验证" if split == "val" else "测试"))
    data = np.load(path, allow_pickle=False)
    y, p, conf = data["labels"], data["preds"], data["confidence"]
    mask = np.ones(y.shape, dtype=bool)
    if errors_only:
        mask &= y != p
    if true_label is not None:
        mask &= y == int(true_label)
    if pred_label is not None:
        mask &= p == int(pred_label)
    idx = np.nonzero(mask)[0]
    # 最没把握的错分排在最前（置信度高的错分更值得怀疑是错标）
    order = idx[np.argsort(-conf[idx])] if errors_only else idx[np.argsort(conf[idx])]
    total = int(order.size)
    order = order[:limit]
    spec = json.loads((job_dir / "job.json").read_text("utf-8"))
    ds_path = spec.get("params", {}).get("dataset", "")
    h5 = ""
    try:
        train, test = catalog.resolve_files(ds_path)
        h5 = str(test if split == "test" and test else train)
    except ValueError:
        pass
    convert = None
    if h5 and os.path.isfile(h5):
        gt, proj = _source_georef(h5)
        if gt and proj:
            convert = _to_lonlat(gt, proj)
    centers = data["centers"] if "centers" in data.files else None
    keys = data["sample_keys"] if "sample_keys" in data.files else None
    rows = []
    for i in order:
        row: Dict[str, Any] = {"index": int(data["index"][i]) if "index" in data.files else int(i),
                               "label": int(y[i]), "pred": int(p[i]),
                               "confidence": round(float(conf[i]), 4)}
        if centers is not None:
            r, c = (int(v) for v in centers[i])
            row["pixel"] = [r, c]
            if convert is not None:
                lon, lat = convert(r, c)
                row["lonlat"] = [round(lon, 8), round(lat, 8)]
        if keys is not None and str(keys[i]):
            row["key"] = str(keys[i])
        rows.append(row)
    return {"total": total, "rows": rows, "split": split}


def low_confidence_layer(conf_path: str, threshold: float, out_path: str,
                         min_pixels: int = 4) -> Dict[str, Any]:
    """置信度低于阈值的像元连成面，写成矢量（GeoJSON / gpkg 按扩展名）。"""
    ds = gdal.Open(conf_path)
    if ds is None:
        raise ValueError("无法打开置信度栅格：%s" % conf_path)
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    mem = gdal.GetDriverByName("MEM").Create("", ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(ds.GetGeoTransform())
    mem.SetProjection(ds.GetProjection())
    out_band = mem.GetRasterBand(1)
    block = 1024
    for r in range(0, ds.RasterYSize, block):
        h = min(block, ds.RasterYSize - r)
        arr = band.ReadAsArray(0, r, ds.RasterXSize, h)
        low = (arr < threshold)
        if nodata is not None:
            low &= arr != nodata
        out_band.WriteArray(low.astype(np.uint8), 0, r)
    if min_pixels > 1:
        gdal.SieveFilter(out_band, None, out_band, min_pixels, 8)
    srs = osr.SpatialReference()
    srs.ImportFromWkt(ds.GetProjection())
    driver = "GPKG" if out_path.lower().endswith(".gpkg") else "GeoJSON"
    if os.path.exists(out_path):
        ogr.GetDriverByName(driver).DeleteDataSource(out_path)
    vds = ogr.GetDriverByName(driver).CreateDataSource(out_path)
    layer = vds.CreateLayer("low_confidence", srs=srs, geom_type=ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("low", ogr.OFTInteger))
    gdal.Polygonize(out_band, out_band, layer, 0, [], callback=None)
    count = layer.GetFeatureCount()
    vds = None
    return {"path": out_path, "features": count}


def embedding_points(job_dir: Path) -> Dict[str, Any]:
    """特征分析的散点数据：二维坐标、标签、疑似度、样本主键与经纬度。"""
    path = job_dir / "eval" / "embedding.npz"
    if not path.is_file():
        raise ValueError("该任务尚无特征分析结果")
    data = np.load(path, allow_pickle=False)
    summary = json.loads((job_dir / "eval" / "embedding.json").read_text("utf-8"))
    spec = json.loads((job_dir / "job.json").read_text("utf-8"))
    convert = None
    try:
        train, _test = catalog.resolve_files(spec.get("params", {}).get("dataset", ""))
        gt, proj = _source_georef(str(train))
        if gt and proj:
            convert = _to_lonlat(gt, proj)
    except (ValueError, OSError):
        pass
    xy = data["xy"]
    lo, hi = xy.min(axis=0), xy.max(axis=0)
    span = np.where(hi - lo > 0, hi - lo, 1.0)
    norm = (xy - lo) / span
    points = []
    for i in range(xy.shape[0]):
        pt: Dict[str, Any] = {"x": round(float(norm[i, 0]), 4), "y": round(float(norm[i, 1]), 4),
                              "l": int(data["labels"][i]), "s": round(float(data["suspicion"][i]), 3),
                              "m": int(data["majority"][i]), "t": int(data["split"][i]),
                              "i": int(data["index"][i])}
        key = str(data["sample_keys"][i])
        if key:
            pt["k"] = key
        if convert is not None:
            r, c = (int(v) for v in data["centers"][i])
            lon, lat = convert(r, c)
            pt["ll"] = [round(lon, 7), round(lat, 7)]
        points.append(pt)
    return {"summary": summary, "points": points}
