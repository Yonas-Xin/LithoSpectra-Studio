# -*- coding: utf-8 -*-
"""把 Sentinel-2 L2A 产品重采样到 10 m 并组合成一个 ENVI 栈。

以 R10m 的 B02/B03/B04/B08 作参考网格，把 R20m 的 B05/B06/B07/B8A/B11/B12 对齐
过去，写出一个多波段 ``.dat`` + ``.hdr``。可选按产品 XML 把量化 DN 换算成地表
反射率：``(DN + BOA_ADD_OFFSET) / BOA_QUANTIFICATION_VALUE``。

B09 是 60 m 的水汽波段，B10 在 L2A 里不作为地表反射率发布，两者默认都不在输出
波段里。

原脚本自带遍历整个根目录的循环，这里拆开了：``discover`` 负责找出每一个
granule，``process`` 只管一个。批量交给 toolbox.batch。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

from .. import raster
from ..core import Logger, Param, Tool, ToolError, WorkItem

# =============================================================================
# 常量
# =============================================================================

BAND_INFO = {
    "B01": {"name": "B01 Coastal aerosol", "wavelength_nm": 443.0, "native_m": 60},
    "B02": {"name": "B02 Blue", "wavelength_nm": 490.0, "native_m": 10},
    "B03": {"name": "B03 Green", "wavelength_nm": 560.0, "native_m": 10},
    "B04": {"name": "B04 Red", "wavelength_nm": 665.0, "native_m": 10},
    "B05": {"name": "B05 Red edge 1", "wavelength_nm": 705.0, "native_m": 20},
    "B06": {"name": "B06 Red edge 2", "wavelength_nm": 740.0, "native_m": 20},
    "B07": {"name": "B07 Red edge 3", "wavelength_nm": 783.0, "native_m": 20},
    "B08": {"name": "B08 Broad NIR", "wavelength_nm": 842.0, "native_m": 10},
    "B8A": {"name": "B8A Narrow NIR", "wavelength_nm": 865.0, "native_m": 20},
    "B09": {"name": "B09 Water vapour", "wavelength_nm": 940.0, "native_m": 60},
    "B10": {"name": "B10 Cirrus", "wavelength_nm": 1375.0, "native_m": 60},
    "B11": {"name": "B11 SWIR 1", "wavelength_nm": 1610.0, "native_m": 20},
    "B12": {"name": "B12 SWIR 2", "wavelength_nm": 2190.0, "native_m": 20},
}

# Sentinel-2 产品 XML 里 band_id 的取值顺序
BAND_BY_XML_ID = {
    0: "B01", 1: "B02", 2: "B03", 3: "B04", 4: "B05", 5: "B06", 6: "B07",
    7: "B08", 8: "B8A", 9: "B09", 10: "B10", 11: "B11", 12: "B12",
}

SOURCE_RESOLUTION = {
    "B02": "R10m", "B03": "R10m", "B04": "R10m", "B08": "R10m",
    "B05": "R20m", "B06": "R20m", "B07": "R20m",
    "B8A": "R20m", "B11": "R20m", "B12": "R20m",
}

RESAMPLING_METHODS = ("nearest", "bilinear", "cubic", "cubicspline", "lanczos")
INTERLEAVES = ("BSQ", "BIL", "BIP")
RASTER_SUFFIXES = {".jp2", ".tif", ".tiff"}
JP2_DRIVER_NAMES = ("JP2OpenJPEG", "JP2KAK", "JP2ECW", "JP2MrSID", "JP2Lura")

DEFAULT_BANDS = ("B02", "B03", "B04", "B05", "B06",
                 "B07", "B08", "B8A", "B11", "B12")


def _resampling_algorithm(name: str) -> int:
    """按名字取 GDAL 重采样常量。延后到用时再取，导入本模块不需要 GDAL。"""
    gdal = raster.require_gdal()
    table = {
        "nearest": gdal.GRA_NearestNeighbour,
        "bilinear": gdal.GRA_Bilinear,
        "cubic": gdal.GRA_Cubic,
        "cubicspline": gdal.GRA_CubicSpline,
        "lanczos": gdal.GRA_Lanczos,
    }
    try:
        return table[name.lower()]
    except KeyError:
        raise ToolError("不支持的重采样方法 %r，可选：%s"
                        % (name, ", ".join(RESAMPLING_METHODS))) from None


@dataclass
class ProductMetadata:
    spacecraft: str = "Sentinel-2"
    acquisition_time: str = "Unknown"
    processing_level: str = "Level-2A"
    processing_baseline: str = "Unknown"
    product_uri: str = "Unknown"
    cloud_cover_percent: str = "Unknown"
    sun_zenith_deg: str = "Unknown"
    sun_azimuth_deg: str = "Unknown"
    quantification_value: float = 10000.0
    offsets: dict = field(default_factory=dict)
    wavelengths_nm: dict = field(default_factory=dict)


# =============================================================================
# XML 元数据
# =============================================================================

def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def first_text(root: ET.Element, names: Iterable[str]) -> str | None:
    wanted = set(names)
    for element in root.iter():
        if local_name(element.tag) in wanted and element.text:
            value = element.text.strip()
            if value:
                return value
    return None


def parse_xml(path: Path | None) -> ET.Element | None:
    if path is None or not path.exists():
        return None
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ToolError("XML 元数据无法解析：%s\n%s" % (path, exc)) from exc


def normalized_band_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().upper().replace("BAND", "B")
    # 多字符的写法排在前面，免得 B8A/B10/B11/B12 被截成 B08/B01
    match = re.search(r"B(?:1[0-2]|8A|0?[1-9])", cleaned)
    if not match:
        return None
    band = match.group(0)
    if band != "B8A" and len(band) == 2:
        band = "B0%s" % band[-1]
    return band


def compact_time_to_iso(value: str) -> str:
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})", value)
    if not match:
        return value
    year, month, day, hour, minute, second = match.groups()
    return "%s-%s-%sT%s:%s:%sZ" % (year, month, day, hour, minute, second)


def _band_of(element: ET.Element) -> str | None:
    band = normalized_band_name(
        element.attrib.get("physicalBand") or element.attrib.get("physical_band")
    )
    if band is not None:
        return band
    band_id = element.attrib.get("band_id") or element.attrib.get("bandId")
    if band_id is not None and band_id.isdigit():
        return BAND_BY_XML_ID.get(int(band_id))
    return None


def parse_product_metadata(product_metadata: Path | None, tile_metadata: Path | None,
                           product_root: Path, filename_time: str) -> ProductMetadata:
    """读产品与瓦片两级 XML，缺什么都有兜底值。"""
    metadata = ProductMetadata()
    product = parse_xml(product_metadata)
    tile = parse_xml(tile_metadata)

    if product is not None:
        metadata.spacecraft = first_text(product, ["SPACECRAFT_NAME"]) or metadata.spacecraft
        metadata.acquisition_time = (
            first_text(product, ["PRODUCT_START_TIME", "DATATAKE_SENSING_START"])
            or metadata.acquisition_time)
        metadata.processing_level = (
            first_text(product, ["PROCESSING_LEVEL", "PRODUCT_TYPE"])
            or metadata.processing_level)
        metadata.processing_baseline = (
            first_text(product, ["PROCESSING_BASELINE"]) or metadata.processing_baseline)
        metadata.product_uri = (
            first_text(product, ["PRODUCT_URI", "PRODUCT_URI_1C"]) or product_root.name)
        metadata.cloud_cover_percent = (
            first_text(product, ["Cloud_Coverage_Assessment", "CLOUDY_PIXEL_PERCENTAGE"])
            or metadata.cloud_cover_percent)

        quantification = first_text(
            product, ["BOA_QUANTIFICATION_VALUE", "QUANTIFICATION_VALUE"])
        if quantification is not None:
            metadata.quantification_value = float(quantification)

        for element in product.iter():
            if local_name(element.tag) == "BOA_ADD_OFFSET" and element.text:
                band = _band_of(element)
                if band:
                    metadata.offsets[band] = float(element.text.strip())
            elif local_name(element.tag) == "Spectral_Information":
                band = _band_of(element)
                central = first_text(element, ["CENTRAL"])
                if band and central is not None:
                    metadata.wavelengths_nm[band] = float(central)

    if tile is not None:
        metadata.acquisition_time = (
            first_text(tile, ["SENSING_TIME"]) or metadata.acquisition_time)
        mean_sun = next(
            (item for item in tile.iter() if local_name(item.tag) == "Mean_Sun_Angle"),
            None)
        if mean_sun is not None:
            metadata.sun_zenith_deg = (
                first_text(mean_sun, ["ZENITH_ANGLE"]) or metadata.sun_zenith_deg)
            metadata.sun_azimuth_deg = (
                first_text(mean_sun, ["AZIMUTH_ANGLE"]) or metadata.sun_azimuth_deg)

    if metadata.acquisition_time == "Unknown" and filename_time:
        metadata.acquisition_time = compact_time_to_iso(filename_time)
    return metadata


# =============================================================================
# 发现 granule
# =============================================================================

def find_nearest_file(start: Path, stop: Path, filename: str) -> Path | None:
    current = start
    stop = stop.resolve()
    while True:
        candidate = current / filename
        if candidate.exists():
            return candidate
        if current.resolve() == stop or current.parent == current:
            return None
        current = current.parent


def find_band_file(folder: Path, band: str, resolution: str) -> Path | None:
    pattern = re.compile(
        r"_%s_%s\.(?:jp2|tif|tiff)$" % (re.escape(band), re.escape(resolution)),
        re.IGNORECASE)
    candidates = sorted(
        path for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in RASTER_SUFFIXES
        and pattern.search(path.name))
    return candidates[0] if candidates else None


def parse_tile_and_time(reference_file: Path) -> tuple[str, str]:
    match = re.search(
        r"(?P<tile>T\d{2}[A-Z]{3})_(?P<time>\d{8}T\d{6})_B02_10m",
        reference_file.name, re.IGNORECASE)
    if match:
        return match.group("tile").upper(), match.group("time")
    tile_match = re.search(r"T\d{2}[A-Z]{3}", reference_file.name, re.IGNORECASE)
    time_match = re.search(r"\d{8}T\d{6}", reference_file.name)
    return (tile_match.group(0).upper() if tile_match else "UNKNOWN_TILE",
            time_match.group(0) if time_match else "UNKNOWN_TIME")


def spacecraft_short_name(spacecraft: str, product_name: str) -> str:
    combined = ("%s %s" % (spacecraft, product_name)).upper()
    match = re.search(r"SENTINEL[- ]?2([ABC])", combined)
    if match:
        return "S2%s" % match.group(1)
    match = re.search(r"\bS2([ABC])\b", combined)
    return "S2%s" % match.group(1) if match else "S2"


# =============================================================================
# 工具
# =============================================================================

class Sentinel2L2AStackTool(Tool):

    id = "sentinel2_l2a_stack"
    name = "Sentinel-2 L2A 10 m 组合"
    summary = "把 L2A 产品的 10 m 与 20 m 波段统一到 10 m 网格，写成一个 ENVI 栈"
    input_kind = "product_dir"

    params = (
        Param("output_bands", "输出波段", DEFAULT_BANDS, kind="strlist",
              help="按此顺序写入。只支持 10 m 与 20 m 波段；B09、B10 不可选。"),
        Param("target_resolution", "目标分辨率 (m)", 10.0, kind="float"),
        Param("resampling", "重采样方法", "bilinear",
              kind="choice", choices=RESAMPLING_METHODS,
              help="反射率是连续量，推荐 bilinear。"),
        Param("interleave", "ENVI 交错方式", "BSQ",
              kind="choice", choices=INTERLEAVES),
        Param("window_size", "分块大小 (像元)", 1024, kind="int"),
        Param("apply_reflectance_scaling", "换算为地表反射率", True, kind="bool",
              help="按产品 XML 做 (DN + BOA_ADD_OFFSET) / BOA_QUANTIFICATION_VALUE。"
                   "关掉则保留量化 DN，但仍写成 Float32。"),
        Param("nodata", "输出 NoData", -9999.0, kind="float"),
        Param("num_threads", "GDAL 线程数", "ALL_CPUS", kind="str"),
    )

    # -- 发现 ---------------------------------------------------------------

    def discover(self, root: Path, params: dict, log: Logger) -> list[WorkItem]:
        bands = params["output_bands"]
        unsupported = [band for band in bands if band not in SOURCE_RESOLUTION]
        if unsupported:
            raise ToolError("这些波段不在 L2A 的可用范围内：%s" % ", ".join(unsupported))
        if len(set(bands)) != len(bands):
            raise ToolError("output_bands 里有重复波段")
        if params["window_size"] <= 0:
            raise ToolError("window_size 必须是正整数")

        root = Path(root).expanduser()
        if not root.is_dir():
            raise ToolError("Sentinel-2 输入必须是产品所在目录：%s" % root)

        items: list[WorkItem] = []
        for r10 in sorted(path for path in root.rglob("R10m") if path.is_dir()):
            r20 = r10.parent / "R20m"
            if not r20.is_dir():
                log("[跳过] 找不到 R20m：%s" % r10)
                continue

            band_files: dict[str, Path] = {}
            missing: list[str] = []
            for band in bands:
                folder = r10 if SOURCE_RESOLUTION[band] == "R10m" else r20
                resolution = "10m" if SOURCE_RESOLUTION[band] == "R10m" else "20m"
                path = find_band_file(folder, band, resolution)
                if path is None:
                    missing.append(band)
                else:
                    band_files[band] = path
            if missing:
                log("[跳过] 缺波段 %s：%s" % (", ".join(missing), r10))
                continue

            granule_root = r10.parent.parent
            product_metadata = find_nearest_file(granule_root, root, "MTD_MSIL2A.xml")
            tile_metadata = granule_root / "MTD_TL.xml"
            if not tile_metadata.exists():
                tile_metadata = None
            product_root = product_metadata.parent if product_metadata else granule_root

            tile_id, filename_time = parse_tile_and_time(band_files["B02"])
            metadata = parse_product_metadata(product_metadata, tile_metadata,
                                              product_root, filename_time)
            items.append(WorkItem(
                key="%s %s" % (tile_id, filename_time),
                source=granule_root,
                payload={
                    "band_files": band_files,
                    "metadata": metadata,
                    "tile_id": tile_id,
                    "filename_time": filename_time,
                },
            ))

        if items:
            self._check_jp2_driver(items)
        return items

    @staticmethod
    def _check_jp2_driver(items: list[WorkItem]) -> None:
        gdal = raster.require_gdal()
        uses_jp2 = any(
            path.suffix.lower() == ".jp2"
            for item in items for path in item.payload["band_files"].values())
        if not uses_jp2:
            return
        if any(gdal.GetDriverByName(name) is not None for name in JP2_DRIVER_NAMES):
            return
        raise ToolError(
            "当前 GDAL 没有 JPEG 2000 驱动，读不了 Sentinel-2 的 .jp2 波段。\n"
            "在目标 conda 环境里装上再跑：\n"
            "    conda install -c conda-forge libgdal-jp2openjpeg"
        )

    # -- 产物 ---------------------------------------------------------------

    def output_path(self, item: WorkItem, out_dir: Path, params: dict) -> Path:
        metadata: ProductMetadata = item.payload["metadata"]
        platform = spacecraft_short_name(metadata.spacecraft, item.source.name)
        timestamp = item.payload["filename_time"]
        if timestamp == "UNKNOWN_TIME":
            compact = re.sub(r"[^0-9T]", "", metadata.acquisition_time)
            timestamp = compact[:15] if len(compact) >= 15 else "UNKNOWN_TIME"
        resolution = "%gm" % params["target_resolution"]
        return out_dir / ("%s_%s_%s_L2A_%s.dat"
                          % (platform, timestamp, item.payload["tile_id"], resolution))

    def companions(self, out_path: Path) -> list[Path]:
        return [out_path.with_suffix(".hdr"), Path(str(out_path) + ".aux.xml")]

    # -- 处理 ---------------------------------------------------------------

    def process(self, item: WorkItem, out_path: Path, params: dict, log: Logger) -> None:
        gdal = raster.require_gdal()
        gdal.SetConfigOption("GDAL_NUM_THREADS", str(params["num_threads"]))
        gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")

        bands = params["output_bands"]
        nodata = params["nodata"]
        target = params["target_resolution"]
        metadata: ProductMetadata = item.payload["metadata"]
        band_files: dict[str, Path] = item.payload["band_files"]

        reference = raster.open_raster(band_files["B02"])
        output = None
        try:
            transform = reference.GetGeoTransform()
            pixel_x, pixel_y = abs(transform[1]), abs(transform[5])
            if abs(pixel_x - target) > 1e-6 or abs(pixel_y - target) > 1e-6:
                raise ToolError(
                    "参考波段 B02 的像元是 %g x %g m，不是 %g m：%s"
                    % (pixel_x, pixel_y, target, band_files["B02"]))

            out_path.parent.mkdir(parents=True, exist_ok=True)
            driver = gdal.GetDriverByName("ENVI")
            output = driver.Create(
                str(out_path), reference.RasterXSize, reference.RasterYSize,
                len(bands), gdal.GDT_Float32,
                options=["INTERLEAVE=%s" % params["interleave"].upper()])
            if output is None:
                raise ToolError("无法创建输出：%s" % out_path)

            output.SetGeoTransform(reference.GetGeoTransform())
            output.SetProjection(reference.GetProjection())
            output.SetMetadata(self._envi_metadata(item, params), "ENVI")

            algorithm = _resampling_algorithm(params["resampling"])
            for index, band in enumerate(bands, start=1):
                source_path = band_files[band]
                log("  [%02d/%02d] %s：%s" % (index, len(bands), band, source_path.name))
                self._write_band(
                    output.GetRasterBand(index), band, source_path, reference,
                    algorithm, metadata, params)
            output.FlushCache()
        except Exception:
            output = None
            reference = None
            self._remove_partial(out_path)
            raise
        finally:
            output = None
            reference = None

    def _write_band(self, out_band, band: str, source_path: Path, reference,
                    algorithm: int, metadata: ProductMetadata, params: dict) -> None:
        gdal = raster.require_gdal()
        nodata = params["nodata"]
        source = raster.open_raster(source_path)
        vrt = None
        try:
            vrt = self._aligned_vrt(source, reference, algorithm, nodata,
                                    params["num_threads"])
            out_band.SetNoDataValue(nodata)
            out_band.SetDescription(BAND_INFO[band]["name"])

            offset = metadata.offsets.get(band, 0.0)
            quantification = metadata.quantification_value
            scaling = params["apply_reflectance_scaling"]
            for xoff, yoff, xsize, ysize in _block_windows(
                    reference.RasterXSize, reference.RasterYSize,
                    params["window_size"]):
                array = vrt.GetRasterBand(1).ReadAsArray(
                    xoff, yoff, xsize, ysize, buf_type=gdal.GDT_Float32)
                if array is None:
                    raise ToolError("读取失败：%s 窗口 (%d, %d, %d, %d)"
                                    % (band, xoff, yoff, xsize, ysize))
                array = np.asarray(array, dtype=np.float32)
                invalid = (~np.isfinite(array)) | np.isclose(array, nodata)
                if scaling:
                    valid = ~invalid
                    array[valid] = (array[valid] + offset) / quantification
                array[invalid] = nodata
                out_band.WriteArray(array, xoff, yoff)
            out_band.FlushCache()
        finally:
            vrt = None
            source = None

    @staticmethod
    def _aligned_vrt(source, reference, algorithm: int, nodata: float,
                     num_threads: str):
        """做一个与 B02 的 10 m 网格严格对齐的内存 VRT。"""
        gdal = raster.require_gdal()
        transform = reference.GetGeoTransform()
        if abs(transform[2]) > 1e-12 or abs(transform[4]) > 1e-12:
            raise ToolError("不支持带旋转的参考网格")
        xmin, ymax = transform[0], transform[3]
        xmax = xmin + reference.RasterXSize * transform[1]
        ymin = ymax + reference.RasterYSize * transform[5]

        source_nodata = source.GetRasterBand(1).GetNoDataValue()
        if source_nodata is None:
            source_nodata = 0.0     # Sentinel-2 的 DN=0 就是无数据

        options = gdal.WarpOptions(
            format="VRT", outputBounds=(xmin, ymin, xmax, ymax),
            width=reference.RasterXSize, height=reference.RasterYSize,
            dstSRS=reference.GetProjection(), resampleAlg=algorithm,
            srcNodata=source_nodata, dstNodata=nodata,
            outputType=gdal.GDT_Float32, multithread=True,
            warpOptions=["NUM_THREADS=%s" % num_threads, "INIT_DEST=NO_DATA"])
        vrt = gdal.Warp("", source, options=options)
        if vrt is None:
            raise ToolError("GDAL 无法建立对齐的虚拟栅格")
        return vrt

    def _envi_metadata(self, item: WorkItem, params: dict) -> dict[str, str]:
        metadata: ProductMetadata = item.payload["metadata"]
        bands = params["output_bands"]
        wavelengths = [metadata.wavelengths_nm.get(
            band, float(BAND_INFO[band]["wavelength_nm"])) for band in bands]
        offsets = [metadata.offsets.get(band, 0.0) for band in bands]

        if params["apply_reflectance_scaling"]:
            units = "Surface reflectance (unitless)"
            scaling = "true"
            # 值里不能出现等号：ENVI 头拿它做键值分隔符，有些读取器会丢掉整条
            formula = "(DN plus BOA_ADD_OFFSET) divided by BOA_QUANTIFICATION_VALUE"
        else:
            units = "Quantized L2A digital number"
            scaling = "false"
            formula = "not applied; see quantification value and BOA add offsets"

        return {
            "band names": raster.envi_list(BAND_INFO[band]["name"] for band in bands),
            "wavelength": raster.envi_list(
                raster.format_float(value) for value in wavelengths),
            "wavelength units": "Nanometers",
            "sensor type": "Sentinel-2 MSI",
            "satellite": metadata.spacecraft,
            "acquisition time": metadata.acquisition_time,
            "processing level": metadata.processing_level,
            "processing baseline": metadata.processing_baseline,
            "tile id": item.payload["tile_id"],
            "source product": metadata.product_uri,
            "output spatial resolution metres":
                raster.format_float(params["target_resolution"]),
            "resampling method": params["resampling"],
            "data units": units,
            "reflectance scaling applied": scaling,
            "reflectance scale formula": formula,
            "BOA quantification value":
                raster.format_float(metadata.quantification_value),
            "BOA add offsets": raster.envi_list(
                raster.format_float(value) for value in offsets),
            "source native resolutions metres": raster.envi_list(
                BAND_INFO[band]["native_m"] for band in bands),
            "sun zenith degrees": metadata.sun_zenith_deg,
            "sun azimuth degrees": metadata.sun_azimuth_deg,
            "cloud cover percent": metadata.cloud_cover_percent,
            "data ignore value": raster.format_float(params["nodata"]),
        }

    def _remove_partial(self, out_path: Path) -> None:
        for path in [out_path] + self.companions(out_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _block_windows(width: int, height: int, block_size: int):
    for yoff in range(0, height, block_size):
        ysize = min(block_size, height - yoff)
        for xoff in range(0, width, block_size):
            xsize = min(block_size, width - xoff)
            yield xoff, yoff, xsize, ysize


TOOL = Sentinel2L2AStackTool()
