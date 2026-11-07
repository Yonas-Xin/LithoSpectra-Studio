# -*- coding: utf-8 -*-
"""构造不依赖真实大场景的小型 ZY1E/ZY1F AHSI L1A 产品。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from osgeo import gdal

gdal.UseExceptions()


# =============================================================================
# 配置区
# =============================================================================

DEFAULT_ROWS = 6
DEFAULT_COLS = 5
VN_BANDS = 76
SW_BANDS = 90


RPC = {
    "LINE_OFF": "3", "SAMP_OFF": "2", "LAT_OFF": "35", "LONG_OFF": "96",
    "HEIGHT_OFF": "1000", "LINE_SCALE": "3", "SAMP_SCALE": "2",
    "LAT_SCALE": "0.5", "LONG_SCALE": "0.5", "HEIGHT_SCALE": "1000",
    "LINE_NUM_COEFF": "1 " + "0 " * 19,
    "LINE_DEN_COEFF": "1 " + "0 " * 19,
    "SAMP_NUM_COEFF": "1 " + "0 " * 19,
    "SAMP_DEN_COEFF": "1 " + "0 " * 19,
}


def _write_raster(path: Path, rows: int, cols: int, bands: int,
                  constant_bands: dict[int, int]) -> None:
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(str(path), cols, rows, bands, gdal.GDT_UInt16,
                            options=["INTERLEAVE=BAND", "COMPRESS=DEFLATE"])
    if dataset is None:
        raise RuntimeError("无法创建测试 GeoTIFF")
    dataset.SetMetadata(RPC, "RPC")
    dataset.SetMetadataItem("fixture", "zy1e", "TEST")
    yy, xx = np.indices((rows, cols))
    for index in range(1, bands + 1):
        if index in constant_bands:
            array = np.full((rows, cols), constant_bands[index], dtype=np.uint16)
        else:
            array = (index * 10 + yy * 2 + xx).astype(np.uint16)
        band = dataset.GetRasterBand(index)
        band.SetDescription("Band %d" % index)
        band.SetScale(1.0)
        band.SetOffset(0.0)
        band.WriteArray(array)
    dataset.FlushCache()
    dataset = None


def _write_auxiliary(path: Path, bands: int, zero_gain_bands: set[int] | None = None) -> None:
    zero_gain_bands = zero_gain_bands or set()
    if path.name.endswith("RadCal.raw"):
        lines = ["%.6f 0.000000" % (0.0 if index in zero_gain_bands else 0.1 + index / 1000)
                 for index in range(1, bands + 1)]
    else:
        lines = ["%.6f 8.760000" % (390 + index * 8.5)
                 for index in range(1, bands + 1)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_ahsi_product(parent: Path, name: str | None = None,
                        rows: int = DEFAULT_ROWS, cols: int = DEFAULT_COLS,
                        vn_constants: dict[int, int] | None = None,
                        sw_constants: dict[int, int] | None = None,
                        level: str = "LEVEL1A", satellite: str = "ZY1E",
                        extension: str = ".tiff", level_tag: str = "Level",
                        variant: str | None = None) -> Path:
    name = name or (satellite + "_AHSI_FIXTURE_L1A0000000001")
    root = Path(parent) / name
    root.mkdir(parents=True)
    vn_constants = dict({7: 0, 18: 31} if vn_constants is None else vn_constants)
    sw_constants = dict({22: 0, 23: 0, 50: 17} if sw_constants is None else sw_constants)
    base = root / name
    vn = base.with_name(name + "_VN" + extension)
    sw = base.with_name(name + "_SW" + extension)
    _write_raster(vn, rows, cols, VN_BANDS, vn_constants)
    _write_raster(sw, rows, cols, SW_BANDS, sw_constants)
    # GTiff 驱动可能已经写出 RPB；无论如何确保显式附属文件存在。
    for raster in (vn, sw):
        rpb = raster.with_suffix(".rpb")
        if not rpb.exists():
            rpb.write_text("fixture RPC sidecar\n", encoding="ascii")

    vn_radcal = root / (satellite + "_AHSI_VNIR_RadCal.raw")
    sw_radcal = root / (satellite + "_AHSI_SWIR_RadCal.raw")
    _write_auxiliary(vn_radcal, VN_BANDS, set(vn_constants))
    _write_auxiliary(sw_radcal, SW_BANDS, set(sw_constants))
    _write_auxiliary(root / (satellite + "_AHSI_VNIR_Spectralresponse.raw"), VN_BANDS)
    _write_auxiliary(root / (satellite + "_AHSI_SWIR_Spectralresponse.raw"), SW_BANDS)

    xml = """<?xml version="1.0" encoding="UTF-8"?>
<ProductMetaData>
  <SatelliteID>{satellite}</SatelliteID><SensorID>AHSI</SensorID><{level_tag}>{level}</{level_tag}>
  <BandsCount>166</BandsCount><LinesInPixels>{rows}</LinesInPixels>
  <SamplesInPixels>{cols}</SamplesInPixels><Denoise>NONE</Denoise>
</ProductMetaData>
""".format(satellite=satellite, level_tag=level_tag, level=level, rows=rows, cols=cols)
    (root / (name + ".xml")).write_text(xml, encoding="utf-8")
    (root / (name + "_Check.xml")).write_text("<Check/>\n", encoding="utf-8")
    if variant:
        (root / (name + "_describe.xml")).write_text(
            "<ProductDescribeData><ProductLevel>%s</ProductLevel></ProductDescribeData>\n"
            % variant, encoding="utf-8")
    (root / (name + ".jpg")).write_bytes(b"fixture-preview")
    return root


def create_zy1e_product(parent: Path, name: str = "ZY1E_AHSI_FIXTURE_L1A0000000001",
                        rows: int = DEFAULT_ROWS, cols: int = DEFAULT_COLS,
                        vn_constants: dict[int, int] | None = None,
                        sw_constants: dict[int, int] | None = None,
                        level: str = "LEVEL1A") -> Path:
    return create_ahsi_product(
        parent, name=name, rows=rows, cols=cols,
        vn_constants=vn_constants, sw_constants=sw_constants, level=level)
