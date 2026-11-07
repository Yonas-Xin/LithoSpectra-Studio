# -*- coding: utf-8 -*-
"""栅格公共层：GDAL 的获取、元数据读取、波长解析与波段子集写出。

工具不直接跟 GDAL 的边角打交道，都走这里。集中一处的好处是波长这类东西只需要
处理一次兼容问题——同一份数据，波长可能躺在 GDAL 默认元数据域、ENVI 元数据域，
或者只在旁边那个 ``.hdr`` 里，三处都要认。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from .core import ToolError

# 常见的栅格后缀。ENVI 只收 .dat/.img 这类数据文件，.hdr 是头不是数据。
RASTER_SUFFIXES = (".tif", ".tiff", ".dat", ".img", ".bsq", ".bil", ".bip", ".jp2")

# GDAL 数据类型 → ENVI data type 编号
_GDAL_TO_ENVI_DTYPE = {
    1: 1,    # Byte
    2: 12,   # UInt16
    3: 2,    # Int16
    4: 13,   # UInt32
    5: 3,    # Int32
    6: 4,    # Float32
    7: 5,    # Float64
    10: 6,   # CFloat32
    11: 9,   # CFloat64
}

OUTPUT_FORMATS = ("ENVI", "GTiff")
_FORMAT_SUFFIX = {"ENVI": ".dat", "GTiff": ".tif"}


def require_gdal():
    """取回 osgeo.gdal，顺手打开异常模式。缺库时给一句能照着做的提示。"""
    try:
        from osgeo import gdal
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise ToolError(
            "本工具需要 GDAL（osgeo）。请在目标 conda 环境里安装：\n"
            "    conda install -c conda-forge gdal\n"
            "注意 GDAL 不要用 pip 装。"
        ) from exc
    gdal.UseExceptions()
    return gdal


def output_suffix(fmt: str) -> str:
    try:
        return _FORMAT_SUFFIX[fmt]
    except KeyError:
        raise ToolError("不支持的输出格式 %r，可选：%s"
                        % (fmt, ", ".join(OUTPUT_FORMATS))) from None


def open_raster(path: Path):
    """只读打开一个栅格，失败时把路径带进错误里。"""
    gdal = require_gdal()
    try:
        dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
    except RuntimeError as exc:
        raise ToolError("打不开栅格 %s：%s" % (path, exc)) from exc
    if dataset is None:
        raise ToolError("打不开栅格：%s" % path)
    return dataset


# =============================================================================
# 元数据与波长
# =============================================================================

def parse_number_list(text: str | None) -> np.ndarray | None:
    """把 ``{1.0, 2.0, 3.0}`` 或 ``1.0, 2.0, 3.0`` 解析成数组。"""
    if not text:
        return None
    raw = str(text).strip().strip("{}")
    if not raw:
        return None
    values = np.fromstring(raw.replace("\n", " "), sep=",")
    return values if values.size else None


def read_envi_sidecar(path: Path) -> dict[str, str]:
    """读取数据文件旁边的 ENVI ``.hdr``。

    GDAL 打开 GeoTIFF 时不会去看同名 .hdr，但用户手上不少数据正是"tif 配 hdr"
    这种组合，波长只写在 hdr 里。这里补上这一路。
    """
    candidates = [path.with_suffix(".hdr"), Path(str(path) + ".hdr")]
    hdr = next((item for item in candidates if item.exists()), None)
    if hdr is None:
        return {}

    metadata: dict[str, str] = {}
    key: str | None = None
    parts: list[str] = []
    try:
        with open(hdr, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                text = line.strip()
                if not text or text.upper() == "ENVI":
                    continue
                if key is not None:          # 正在收一个跨行的 {...} 值
                    parts.append(text)
                    if "}" in text:
                        metadata[key] = " ".join(parts)
                        key, parts = None, []
                    continue
                if "=" not in text:
                    continue
                name, _, value = text.partition("=")
                name, value = name.strip().lower(), value.strip()
                if value.startswith("{") and "}" not in value:
                    key, parts = name, [value]
                else:
                    metadata[name] = value
    except OSError:
        return {}
    return metadata


def collect_metadata(dataset, path: Path) -> dict[str, str]:
    """合并三处元数据：GDAL 默认域、ENVI 域、旁边的 .hdr。后者优先级最低。"""
    merged: dict[str, str] = {}
    merged.update({k.lower(): v for k, v in read_envi_sidecar(path).items()})
    for domain in ("", "ENVI"):
        try:
            items = dataset.GetMetadata(domain) or {}
        except RuntimeError:
            items = {}
        merged.update({k.lower(): v for k, v in items.items()})
    return merged


def band_wavelengths(dataset, path: Path) -> np.ndarray | None:
    """取每个波段的中心波长（纳米）。取不到返回 None。"""
    metadata = collect_metadata(dataset, path)
    values = parse_number_list(metadata.get("wavelength"))
    if values is None or values.size != dataset.RasterCount:
        return None
    units = str(metadata.get("wavelength units", "")).strip().lower()
    if units.startswith("micro") or units in ("um", "µm"):
        values = values * 1000.0
    return values


def band_fwhm(dataset, path: Path) -> np.ndarray | None:
    metadata = collect_metadata(dataset, path)
    values = parse_number_list(metadata.get("fwhm"))
    if values is None or values.size != dataset.RasterCount:
        return None
    return values


# =============================================================================
# 写出
# =============================================================================

def envi_list(values: Iterable[Any]) -> str:
    return "{" + ", ".join(str(value) for value in values) + "}"


def format_float(value: float) -> str:
    return "%.8g" % value


def write_band_subset(dataset, out_path: Path, keep_bands: Sequence[int],
                      metadata: dict[str, str], fmt: str = "ENVI") -> None:
    """把 ``keep_bands``（1 起算）写成一个新栅格，并带上更新后的元数据。

    ENVI 格式由 GDAL 直接写出 ``.dat`` + ``.hdr``；GTiff 另外补一个
    ``file type = TIFF`` 的 ``.hdr``，这样 ENVI 打开 .tif 时也读得到波长。
    """
    gdal = require_gdal()
    if fmt not in OUTPUT_FORMATS:
        raise ToolError("不支持的输出格式 %r" % fmt)
    if not keep_bands:
        raise ToolError("没有任何波段被保留，无法写出")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    creation = ["INTERLEAVE=BIP"] if fmt == "GTiff" else ["INTERLEAVE=BSQ"]
    options = gdal.TranslateOptions(format=fmt, bandList=list(keep_bands),
                                    creationOptions=creation)
    out_ds = gdal.Translate(str(out_path), dataset, options=options)
    if out_ds is None:
        raise ToolError("无法创建输出：%s" % out_path)

    try:
        out_ds.SetProjection(dataset.GetProjection())
        out_ds.SetGeoTransform(dataset.GetGeoTransform())
        for out_index, in_index in enumerate(keep_bands, start=1):
            in_band = dataset.GetRasterBand(in_index)
            nodata = in_band.GetNoDataValue()
            if nodata is not None:
                out_ds.GetRasterBand(out_index).SetNoDataValue(nodata)
            description = in_band.GetDescription()
            if description:
                out_ds.GetRasterBand(out_index).SetDescription(description)
        if metadata:
            out_ds.SetMetadata({k: str(v) for k, v in metadata.items()}, "ENVI")
        out_ds.FlushCache()
        if fmt == "GTiff" and "wavelength" in metadata:
            write_envi_sidecar(out_path, metadata, out_ds, file_type="TIFF")
    finally:
        out_ds = None


def write_envi_sidecar(out_path: Path, metadata: dict[str, str], dataset,
                       file_type: str = "TIFF") -> Path:
    """在非 ENVI 产物旁边补一个 ENVI 头，主要为了把波长带出去。"""
    hdr_path = out_path.with_suffix(".hdr")
    bands = dataset.RasterCount
    envi_dtype = _GDAL_TO_ENVI_DTYPE.get(dataset.GetRasterBand(1).DataType, 4)
    wavelengths = parse_number_list(metadata.get("wavelength"))
    fwhm = parse_number_list(metadata.get("fwhm"))

    def block(values: np.ndarray) -> str:
        items = ["%.6f" % value for value in values]
        rows = [" " + ", ".join(items[i:i + 5]) for i in range(0, len(items), 5)]
        return ",\n".join(rows)

    names = [dataset.GetRasterBand(i).GetDescription() or "Band %d" % i
             for i in range(1, bands + 1)]

    with open(hdr_path, "w", encoding="utf-8") as handle:
        handle.write("ENVI\n")
        description = metadata.get("description")
        if description:
            handle.write("description = {%s}\n" % description)
        handle.write("samples = %d\n" % dataset.RasterXSize)
        handle.write("lines   = %d\n" % dataset.RasterYSize)
        handle.write("bands   = %d\n" % bands)
        handle.write("header offset = 0\n")
        handle.write("file type = %s\n" % file_type)
        handle.write("data type = %d\n" % envi_dtype)
        handle.write("interleave = bip\n")
        handle.write("byte order = 0\n")
        handle.write("wavelength units = %s\n"
                     % metadata.get("wavelength units", "Nanometers"))
        if wavelengths is not None and wavelengths.size == bands:
            handle.write("wavelength = {\n%s}\n" % block(wavelengths))
        if fwhm is not None and fwhm.size == bands:
            handle.write("fwhm = {\n%s}\n" % block(fwhm))
        handle.write("band names = {\n %s}\n" % ",\n ".join(names))
    return hdr_path
