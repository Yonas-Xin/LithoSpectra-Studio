# -*- coding: utf-8 -*-
"""去除高光谱影像的水汽吸收波段与 VNIR-SWIR 重合波段。

原实现依赖私有项目 Hyperspectral_Deep-Learning_Private 的 ``Hyperspectral_Image``
与 ``algorithms``，这里按同样的算法重写为只依赖 GDAL 与 NumPy，工具箱因此可以
独立运行。

保留下来的波段连同裁剪后的 wavelength / fwhm 一起写出，ENVI 与 GDAL 两边都读
得到波长——下游的重采样、光谱角匹配全靠它。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .. import raster, sensors
from ..core import Logger, Param, Tool, ToolError, WorkItem, iter_files

# 写出时不能照抄的结构性字段：波段数变了，这些必须由写出方重算
_STRUCTURAL_KEYS = {
    "samples", "lines", "bands", "header offset", "data type", "interleave",
    "byte order", "file type", "band names", "wavelength", "fwhm",
    "wavelength units",
}


def detect_overlap_mask(wavelengths: np.ndarray) -> np.ndarray:
    """标出破坏波长单调递增的重合波段，也就是 VNIR 与 SWIR 的拼接处。

    做法与原实现一致：第一次冲突删掉前一段的末尾波段，之后反复删掉后一段的起始
    波段，直到整条曲线单调。区别只在记账方式——这里维护一个"仍存活的原始索引"
    列表，省掉把当前位置换算回原始位置的心算。
    """
    alive = list(range(len(wavelengths)))
    values = [float(value) for value in wavelengths]
    removed: list[int] = []
    first_conflict = True

    while True:
        conflict = next(
            (i for i in range(len(values) - 1) if values[i + 1] <= values[i]),
            None,
        )
        if conflict is None:
            break
        position = conflict if first_conflict else conflict + 1
        first_conflict = False
        removed.append(alive.pop(position))
        values.pop(position)

    mask = np.zeros(len(wavelengths), dtype=bool)
    if removed:
        mask[removed] = True
    return mask


class RemoveWaterVaporTool(Tool):

    id = "remove_water_vapor"
    name = "去水汽波段"
    summary = "按波长范围剔除水汽吸收波段，并可一并去掉 VNIR-SWIR 重合波段"
    input_kind = "raster"

    params = (
        Param(
            "water_vapor_ranges", "水汽波段范围 (nm)",
            ((1340.0, 1450.0), (1800.0, 1950.0)),
            kind="ranges",
            help="落在这些闭区间内的波段会被删除。1340-1450 与 1800-1950 是两条"
                 "主要的水汽吸收带；ZY1E/ZY1F 常再加 (300,400) 与 (2450,2600) "
                 "把两端的噪声波段一起去掉。",
        ),
        Param(
            "remove_overlap", "去除 VNIR-SWIR 重合波段", True, kind="bool",
            help="拼接式传感器的两段光谱常有重叠，导致波长不再单调递增。",
        ),
        Param(
            "sensor", "传感器（波长兜底）", "", kind="str",
            help="影像自身没有波长信息时，从 assets/ 下的同名参数文件读取。"
                 "留空则要求影像必须自带波长。",
        ),
        Param(
            "output_format", "输出格式", "ENVI",
            kind="choice", choices=raster.OUTPUT_FORMATS,
            help="ENVI 产出 .dat + .hdr；GTiff 产出 .tif 并另附一个 .hdr 带波长。",
        ),
        Param(
            "suffix", "输出文件名后缀", "_cleaned", kind="str",
        ),
    )

    # -- 发现 ---------------------------------------------------------------

    def discover(self, root: Path, params: dict, log: Logger) -> list[WorkItem]:
        paths = iter_files(root, raster.RASTER_SUFFIXES)
        return [WorkItem(key=path.stem, source=path) for path in paths]

    def output_path(self, item: WorkItem, out_dir: Path, params: dict) -> Path:
        suffix = raster.output_suffix(params["output_format"])
        return out_dir / ("%s%s%s" % (item.source.stem, params["suffix"], suffix))

    def companions(self, out_path: Path) -> list[Path]:
        return [out_path.with_suffix(".hdr"), Path(str(out_path) + ".aux.xml")]

    def suggestions(self, name: str) -> tuple[str, ...]:
        # 现读 assets/，界面上就是一个可编辑下拉框；留空仍然合法
        return tuple(sensors.available_sensors()) if name == "sensor" else ()

    # -- 处理 ---------------------------------------------------------------

    def process(self, item: WorkItem, out_path: Path, params: dict, log: Logger) -> None:
        dataset = raster.open_raster(item.source)
        try:
            n_bands = dataset.RasterCount
            wavelengths = raster.band_wavelengths(dataset, item.source)
            fwhm = raster.band_fwhm(dataset, item.source)

            if wavelengths is None:
                wavelengths, fwhm = self._sensor_fallback(item, params, n_bands, log)
            else:
                log("波长来自影像元数据：%d 个波段 %.1f - %.1f nm"
                    % (n_bands, wavelengths[0], wavelengths[-1]))

            delete_mask = np.zeros(n_bands, dtype=bool)
            for low, high in params["water_vapor_ranges"]:
                in_range = (wavelengths >= low) & (wavelengths <= high)
                count = int(np.count_nonzero(in_range))
                if count:
                    log("  水汽 %.0f-%.0f nm：%d 个波段" % (low, high, count))
                delete_mask |= in_range
            water_count = int(np.count_nonzero(delete_mask))

            overlap_count = 0
            if params["remove_overlap"]:
                overlap_mask = detect_overlap_mask(wavelengths)
                overlap_count = int(np.count_nonzero(overlap_mask))
                if overlap_count:
                    listed = ", ".join(
                        "%d (%.2f nm)" % (i + 1, wavelengths[i])
                        for i in np.flatnonzero(overlap_mask)
                    )
                    log("  重合波段：%s" % listed)
                delete_mask |= overlap_mask

            keep_mask = ~delete_mask
            remaining = int(np.count_nonzero(keep_mask))
            log("删除 %d 个（水汽 %d，重合 %d），保留 %d 个"
                % (n_bands - remaining, water_count, overlap_count, remaining))
            if remaining == 0:
                raise ToolError("所有波段都被标记为删除，检查一下波长范围设置")
            if remaining == n_bands:
                log("  没有波段命中，输出与输入波段数相同")

            metadata = self._output_metadata(dataset, item.source, keep_mask,
                                             wavelengths, fwhm)
            keep_bands = (np.flatnonzero(keep_mask) + 1).tolist()
            raster.write_band_subset(dataset, out_path, keep_bands, metadata,
                                     fmt=params["output_format"])
        finally:
            dataset = None

    # -- 内部 ---------------------------------------------------------------

    def _sensor_fallback(self, item: WorkItem, params: dict, n_bands: int,
                         log: Logger):
        name = str(params["sensor"]).strip()
        if not name:
            raise ToolError(
                "%s 没有波长信息，且未指定 sensor 参数。可用传感器：%s"
                % (item.source.name, ", ".join(sensors.available_sensors()) or "无")
            )
        wavelengths, fwhm = sensors.load_sensor(name)
        if wavelengths.size != n_bands:
            raise ToolError(
                "传感器 %s 有 %d 个波段，影像 %s 有 %d 个，对不上"
                % (name, wavelengths.size, item.source.name, n_bands)
            )
        log("影像无波长信息，回退到传感器参数：%s" % name)
        return wavelengths, fwhm

    def _output_metadata(self, dataset, source: Path, keep_mask: np.ndarray,
                         wavelengths: np.ndarray, fwhm) -> dict[str, str]:
        metadata = {
            key: value
            for key, value in raster.collect_metadata(dataset, source).items()
            if key not in _STRUCTURAL_KEYS
        }
        metadata["wavelength"] = raster.envi_list(
            "%.6f" % value for value in wavelengths[keep_mask]
        )
        if fwhm is not None and len(fwhm) == len(keep_mask):
            metadata["fwhm"] = raster.envi_list(
                "%.6f" % value for value in np.asarray(fwhm)[keep_mask]
            )
        metadata["wavelength units"] = "Nanometers"
        return metadata


TOOL = RemoveWaterVaporTool()
