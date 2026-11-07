# -*- coding: utf-8 -*-
"""资源系列卫星 AHSI L1A 产品适配与无损事务克隆。

本模块只理解产品，不理解 HyDe。算法层只会看到 ``[H, W, B]`` 数组；未来接入
统一 ``HSIScene`` 时，可以替换这里的产品发现入口而无需改算法实现。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from ..core import ToolError


EXPECTED_BANDS = {"VN": 76, "SW": 90}
EXPECTED_TOTAL_BANDS = 166
SUPPORTED_SATELLITES = {"ZY1E", "ZY1F"}
CORE_SUFFIXES = {
    "VN": ("_VN.tif", "_VN.tiff"),
    "SW": ("_SW.tif", "_SW.tiff"),
}


def _gdal():
    try:
        from osgeo import gdal
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise ToolError("高光谱产品读取需要 GDAL Python 绑定") from exc
    gdal.UseExceptions()
    return gdal


@dataclass(frozen=True)
class ProductPart:
    """产品中的一段光谱立方体及其必需附属文件。"""

    name: str
    raster: Path
    rpb: Path | None
    radcal: Path | None
    spectral_response: Path | None
    bands: int
    rows: int
    cols: int

    def relative_to(self, root: Path) -> dict[str, Any]:
        def relative(path: Path | None) -> str | None:
            return str(path.relative_to(root)) if path is not None else None

        return {
            "name": self.name,
            "raster": str(self.raster.relative_to(root)),
            "rpb": relative(self.rpb),
            "radcal": relative(self.radcal),
            "spectral_response": relative(self.spectral_response),
            "bands": self.bands,
            "rows": self.rows,
            "cols": self.cols,
        }


@dataclass(frozen=True)
class AHSIProduct:
    """通过完整合同校验的 ZY1E/ZY1F AHSI LEVEL1A 产品。"""

    root: Path
    xml: Path
    satellite: str
    sensor: str
    level: str
    rows: int
    cols: int
    parts: tuple[ProductPart, ...]
    variant: str | None = None

    @property
    def adapter_id(self) -> str:
        """返回创建该产品视图的 Adapter 标识。"""
        return "zy_ahsi_l1a"

    def part(self, name: str) -> ProductPart:
        wanted = name.upper()
        for part in self.parts:
            if part.name == wanted:
                return part
        raise KeyError(name)

    @property
    def core_rasters(self) -> tuple[Path, ...]:
        return tuple(part.raster for part in self.parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "xml": str(self.xml.relative_to(self.root)),
            "satellite": self.satellite,
            "sensor": self.sensor,
            "level": self.level,
            "rows": self.rows,
            "cols": self.cols,
            "variant": self.variant,
            "adapter_id": self.adapter_id,
            "parts": [part.relative_to(self.root) for part in self.parts],
        }


# 保留旧名称，避免已经引用首版 ZY1E 合同的代码失效。
ZY1EProduct = AHSIProduct


@dataclass(frozen=True)
class BandScan:
    """一个 1 起算波段的精确分块扫描结果。"""

    band: int
    finite_count: int
    minimum: float | None
    maximum: float | None
    frozen: bool
    reason: str | None
    radcal_gain: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_fields(path: Path) -> dict[str, str]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ToolError("产品 XML 无法解析：%s\n%s" % (path, exc)) from exc
    fields: dict[str, str] = {}
    for element in root.iter():
        if element.text and element.text.strip():
            fields.setdefault(_local_name(element.tag), element.text.strip())
    return fields


def _one(paths: Iterable[Path], label: str, root: Path) -> Path:
    found = sorted(set(Path(path) for path in paths))
    if len(found) != 1:
        raise ToolError("%s 应有且仅有 1 个，实际 %d 个：%s"
                        % (label, len(found), root))
    return found[0]


def _casefold_matches(root: Path, suffix: str) -> list[Path]:
    wanted = suffix.casefold()
    return [path for path in root.iterdir()
            if path.is_file() and path.name.casefold().endswith(wanted)]


def _core_matches(root: Path, part: str) -> list[Path]:
    found: list[Path] = []
    for suffix in CORE_SUFFIXES[part]:
        found.extend(_casefold_matches(root, suffix))
    return sorted(set(found))


def _nonempty_line_count(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        return sum(1 for line in stream if line.strip())


def _open_raster(path: Path, update: bool = False):
    gdal = _gdal()
    mode = gdal.GA_Update if update else gdal.GA_ReadOnly
    dataset = gdal.Open(str(path), mode)
    if dataset is None:
        raise ToolError("GDAL 无法打开栅格：%s" % path)
    return dataset


def parse_ahsi_product(root: Path) -> AHSIProduct:
    """识别并完整校验一个 ZY1E/ZY1F AHSI LEVEL1A 产品目录。"""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ToolError("AHSI 输入必须是产品目录：%s" % root)

    xml_candidates: list[Path] = []
    for path in root.iterdir():
        if not path.is_file() or path.suffix.casefold() != ".xml":
            continue
        if path.stem.casefold().endswith(("_check", "_describe")):
            continue
        try:
            fields = _xml_fields(path)
        except ToolError:
            continue
        if fields.get("SatelliteID", "").upper() in SUPPORTED_SATELLITES and \
                fields.get("SensorID", "").upper() == "AHSI":
            xml_candidates.append(path)
    xml_path = _one(xml_candidates, "ZY1E/ZY1F AHSI 主 XML", root)
    fields = _xml_fields(xml_path)

    satellite = fields.get("SatelliteID", "").upper()
    sensor = fields.get("SensorID", "").upper()
    level = (fields.get("Level") or fields.get("ProductLevel") or "").upper()
    if satellite not in SUPPORTED_SATELLITES or sensor != "AHSI" or level != "LEVEL1A":
        raise ToolError("仅支持 ZY1E/ZY1F AHSI LEVEL1A，实际为 %s/%s/%s"
                        % (satellite or "?", sensor or "?", level or "?"))
    try:
        rows = int(fields["LinesInPixels"])
        cols = int(fields["SamplesInPixels"])
        total_bands = int(fields["BandsCount"])
    except (KeyError, ValueError) as exc:
        raise ToolError("主 XML 缺少合法的 LinesInPixels/SamplesInPixels/BandsCount") from exc
    if total_bands != EXPECTED_TOTAL_BANDS:
        raise ToolError("%s AHSI L1A 应为 %d 波段，XML 声明 %d"
                        % (satellite, EXPECTED_TOTAL_BANDS, total_bands))

    parts: list[ProductPart] = []
    for name in ("VN", "SW"):
        raster_path = _one(_core_matches(root, name),
                           "%s 核心 TIFF" % name, root)
        rpb_path = raster_path.with_suffix(".rpb")
        if not rpb_path.is_file():
            rpb_path = _one(_casefold_matches(root, "_%s.rpb" % name),
                            "%s RPC 文件" % name, root)
        long_name = "VNIR" if name == "VN" else "SWIR"
        radcal = _one(_casefold_matches(root, "%s_RadCal.raw" % long_name),
                      "%s 辐射定标文件" % name, root)
        spectral = _one(_casefold_matches(root, "%s_Spectralresponse.raw" % long_name),
                        "%s 光谱响应文件" % name, root)

        dataset = _open_raster(raster_path)
        try:
            expected = EXPECTED_BANDS[name]
            actual_shape = (dataset.RasterYSize, dataset.RasterXSize, dataset.RasterCount)
            if actual_shape != (rows, cols, expected):
                raise ToolError(
                    "%s TIFF 尺寸/波段不匹配：实际 %s，期望 %s"
                    % (name, actual_shape, (rows, cols, expected)))
            gdal = _gdal()
            types = {dataset.GetRasterBand(i).DataType for i in range(1, expected + 1)}
            if types != {gdal.GDT_UInt16}:
                labels = ", ".join(sorted(gdal.GetDataTypeName(item) for item in types))
                raise ToolError("%s TIFF 必须全为 UInt16，实际：%s" % (name, labels))
            if not dataset.GetMetadata("RPC"):
                raise ToolError("%s TIFF 缺少内嵌 RPC 元数据域" % name)
        finally:
            dataset = None

        for label, path in (("RadCal", radcal), ("Spectralresponse", spectral)):
            count = _nonempty_line_count(path)
            if count != EXPECTED_BANDS[name]:
                raise ToolError("%s %s 应有 %d 行，实际 %d：%s"
                                % (name, label, EXPECTED_BANDS[name], count, path))
        parts.append(ProductPart(name, raster_path, rpb_path, radcal, spectral,
                                 EXPECTED_BANDS[name], rows, cols))

    variant = None
    describe_files = _casefold_matches(root, "_describe.xml")
    if len(describe_files) == 1:
        variant = (_xml_fields(describe_files[0]).get("ProductLevel") or "").upper() or None

    return AHSIProduct(root, xml_path, satellite, sensor, level, rows, cols,
                       tuple(parts), variant)


def parse_zy1e_product(root: Path) -> AHSIProduct:
    """兼容旧 API；现在同时识别 ZY1E 与 ZY1F。"""
    return parse_ahsi_product(root)


def discover_ahsi_products(root: Path) -> list[AHSIProduct]:
    """把 root 本身或其子目录中的产品识别为独立工作项。"""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ToolError("输入路径不存在或不是目录：%s" % root)
    candidates = {root}
    candidates.update(path.parent for path in root.rglob("*.xml"))
    products: list[AHSIProduct] = []
    errors: list[str] = []
    for candidate in sorted(candidates):
        if not _core_matches(candidate, "VN"):
            continue
        try:
            products.append(parse_ahsi_product(candidate))
        except ToolError as exc:
            errors.append("%s：%s" % (candidate, exc))
    if not products and errors:
        raise ToolError("发现疑似 ZY1E/ZY1F AHSI 产品但校验失败：\n" + "\n".join(errors))
    return products


def read_radcal_gains(path: Path, expected_bands: int) -> tuple[float | None, ...]:
    gains: list[float | None] = []
    with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                gains.append(float(line.split()[0]))
            except (IndexError, ValueError):
                gains.append(None)
    if len(gains) != expected_bands:
        raise ToolError("RadCal 行数与波段数不一致：%s" % path)
    return tuple(gains)


def scan_part_bands(part: ProductPart) -> tuple[BandScan, ...]:
    """逐块精确扫描每个波段；不以抽样结果判定常量波段。"""
    import numpy as np

    gains = (read_radcal_gains(part.radcal, part.bands)
             if part.radcal is not None else (None,) * part.bands)
    dataset = _open_raster(part.raster)
    try:
        first_band = dataset.GetRasterBand(1)
        block_x, block_y = first_band.GetBlockSize()
        block_x = max(1, int(block_x or min(512, part.cols)))
        block_y = max(1, int(block_y or min(512, part.rows)))
        nodata = [dataset.GetRasterBand(index).GetNoDataValue()
                  for index in range(1, part.bands + 1)]
        counts = np.zeros(part.bands, dtype=np.int64)
        minima = np.full(part.bands, math.inf, dtype=np.float64)
        maxima = np.full(part.bands, -math.inf, dtype=np.float64)

        # 一次空间块读取全部波段，避免对 BAND-interleaved TIFF 发出数十万次
        # GDAL 调用；判定仍覆盖每一个像元，不是抽样。
        for yoff in range(0, part.rows, block_y):
            ysize = min(block_y, part.rows - yoff)
            for xoff in range(0, part.cols, block_x):
                xsize = min(block_x, part.cols - xoff)
                cube = dataset.ReadAsArray(xoff, yoff, xsize, ysize)
                if cube is None:
                    raise ToolError("读取 %s 空间块失败" % part.raster)
                if cube.ndim == 2:
                    cube = cube[None, ...]
                for offset in range(part.bands):
                    array = cube[offset]
                    valid = np.isfinite(array)
                    if nodata[offset] is not None:
                        valid &= array != nodata[offset]
                    values = array[valid]
                    if values.size:
                        counts[offset] += int(values.size)
                        minima[offset] = min(minima[offset], float(values.min()))
                        maxima[offset] = max(maxima[offset], float(values.max()))

        scans: list[BandScan] = []
        for offset in range(part.bands):
            index = offset + 1
            finite_count = int(counts[offset])
            minimum = float(minima[offset])
            maximum = float(maxima[offset])
            if finite_count == 0:
                scans.append(BandScan(index, 0, None, None, True,
                                      "no_finite_pixels", gains[index - 1]))
            elif maximum == minimum:
                scans.append(BandScan(index, finite_count, minimum, maximum, True,
                                      "constant_band", gains[index - 1]))
            else:
                scans.append(BandScan(index, finite_count, minimum, maximum, False,
                                      None, gains[index - 1]))
    finally:
        dataset = None
    return tuple(scans)


def contiguous_groups(bands: Iterable[int]) -> tuple[tuple[int, ...], ...]:
    ordered = sorted(set(int(item) for item in bands))
    if not ordered:
        return ()
    groups: list[list[int]] = [[ordered[0]]]
    for band in ordered[1:]:
        if band == groups[-1][-1] + 1:
            groups[-1].append(band)
        else:
            groups.append([band])
    return tuple(tuple(group) for group in groups)


def scan_product_bands(product: AHSIProduct) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for part in product.parts:
        scans = scan_part_bands(part)
        valid = [scan.band for scan in scans if not scan.frozen]
        frozen = [scan.band for scan in scans if scan.frozen]
        result[part.name] = {
            "bands": [scan.to_dict() for scan in scans],
            "valid_groups": [list(group) for group in contiguous_groups(valid)],
            "frozen_bands": frozen,
            "zero_gain_bands": [
                scan.band for scan in scans if scan.radcal_gain == 0
            ],
            "zero_gain_without_constant_warning": [
                scan.band for scan in scans
                if scan.radcal_gain == 0 and not scan.frozen
            ],
        }
    return result


def ensure_output_safe(source: Path, output: Path) -> None:
    source = Path(source).resolve()
    output = Path(output).resolve(strict=False)
    if output == source or source in output.parents:
        raise ToolError("输出目录不得等于或位于输入产品内部：%s" % output)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def product_file_hashes(root: Path, exclude: Iterable[Path] = ()) -> dict[str, str]:
    root = Path(root).resolve()
    excluded = {Path(path).resolve() for path in exclude}
    return {
        str(path.relative_to(root)): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.resolve() not in excluded
    }


def band_pixel_hashes(path: Path, bands: Iterable[int]) -> dict[int, str]:
    """按 GDAL 分块读取并哈希指定波段的像元字节。"""
    import numpy as np

    dataset = _open_raster(path)
    result: dict[int, str] = {}
    try:
        for index in sorted(set(int(item) for item in bands)):
            if not 1 <= index <= dataset.RasterCount:
                raise ToolError("像元哈希波段越界：%d" % index)
            band = dataset.GetRasterBand(index)
            block_x, block_y = band.GetBlockSize()
            block_x = max(1, int(block_x or min(512, dataset.RasterXSize)))
            block_y = max(1, int(block_y or min(512, dataset.RasterYSize)))
            digest = hashlib.sha256()
            for yoff in range(0, dataset.RasterYSize, block_y):
                ysize = min(block_y, dataset.RasterYSize - yoff)
                for xoff in range(0, dataset.RasterXSize, block_x):
                    xsize = min(block_x, dataset.RasterXSize - xoff)
                    array = band.ReadAsArray(xoff, yoff, xsize, ysize)
                    if array is None:
                        raise ToolError("读取像元哈希失败：%s band %d" % (path, index))
                    digest.update(np.ascontiguousarray(array).tobytes())
            result[index] = digest.hexdigest()
    finally:
        dataset = None
    return result


def raster_metadata_snapshot(path: Path) -> dict[str, Any]:
    """提取写像元前后必须一致的 GDAL 逻辑元信息。"""
    gdal = _gdal()
    dataset = _open_raster(path)
    try:
        # DERIVED_SUBDATASETS 是 GDAL 按当前文件绝对路径即时合成的虚拟域；产品克隆
        # 后路径必然变化，它不属于源产品逻辑元信息。
        domains = sorted((set(dataset.GetMetadataDomainList() or [])
                          | {"", "RPC", "IMAGE_STRUCTURE"})
                         - {"DERIVED_SUBDATASETS"})
        metadata = {domain: dict(dataset.GetMetadata(domain) or {}) for domain in domains}
        bands: list[dict[str, Any]] = []
        for index in range(1, dataset.RasterCount + 1):
            band = dataset.GetRasterBand(index)
            band_domains = sorted(set(band.GetMetadataDomainList() or []) | {""})
            bands.append({
                "data_type": gdal.GetDataTypeName(band.DataType),
                "nodata": band.GetNoDataValue(),
                "scale": band.GetScale(),
                "offset": band.GetOffset(),
                "description": band.GetDescription(),
                "color_interpretation": int(band.GetColorInterpretation()),
                "metadata": {domain: dict(band.GetMetadata(domain) or {})
                             for domain in band_domains},
            })
        return {
            "driver": dataset.GetDriver().ShortName,
            "shape": [dataset.RasterYSize, dataset.RasterXSize, dataset.RasterCount],
            "projection": dataset.GetProjectionRef() or "",
            "geotransform": list(dataset.GetGeoTransform(can_return_null=True) or []),
            "metadata": metadata,
            "bands": bands,
        }
    finally:
        dataset = None


def assert_metadata_equal(before: dict[str, Any], after: dict[str, Any], label: str) -> None:
    if before != after:
        before_text = json.dumps(before, ensure_ascii=False, sort_keys=True)
        after_text = json.dumps(after, ensure_ascii=False, sort_keys=True)
        raise ToolError("%s 逻辑元信息发生变化。写前=%s；写后=%s"
                        % (label, before_text, after_text))


class ProductTransaction:
    """复制到 ``.partial``，成功校验后原子提交的产品目录事务。"""

    def __init__(self, source: Path, target: Path) -> None:
        self.source = Path(source).resolve()
        self.target = Path(target).resolve(strict=False)
        self.staging = self.target.with_name(self.target.name + ".partial")
        self._committed = False

    def __enter__(self) -> Path:
        ensure_output_safe(self.source, self.target)
        if self.target.exists():
            raise ToolError("最终输出已存在，批处理应先处理覆盖策略：%s" % self.target)
        if self.staging.exists():
            shutil.rmtree(self.staging)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.source, self.staging, copy_function=shutil.copy2)
        return self.staging

    def commit(self) -> None:
        if not self.staging.is_dir():
            raise ToolError("事务暂存目录不存在：%s" % self.staging)
        os.replace(self.staging, self.target)
        self._committed = True

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self._committed and self.staging.exists():
            shutil.rmtree(self.staging)


def verify_product_clone(source: AHSIProduct, clone_root: Path,
                         before_snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """验证内部文件集合、非核心哈希和核心 TIFF 逻辑元信息。"""
    clone_root = Path(clone_root).resolve()
    cloned = parse_ahsi_product(clone_root)
    source_files = {str(path.relative_to(source.root)) for path in source.root.rglob("*") if path.is_file()}
    clone_files = {str(path.relative_to(clone_root)) for path in clone_root.rglob("*") if path.is_file()}
    if source_files != clone_files:
        raise ToolError("克隆产品内部文件集合与输入不一致")

    source_hashes = product_file_hashes(source.root, source.core_rasters)
    cloned_hashes = product_file_hashes(clone_root, cloned.core_rasters)
    if source_hashes != cloned_hashes:
        raise ToolError("克隆产品存在被改写的非核心文件")

    for part in cloned.parts:
        assert_metadata_equal(before_snapshots[part.name],
                              raster_metadata_snapshot(part.raster), part.name)
    return {
        "file_set_equal": True,
        "non_core_hashes_equal": True,
        "logical_metadata_equal": True,
        "non_core_file_count": len(source_hashes),
    }
