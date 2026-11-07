# -*- coding: utf-8 -*-
"""独立 HyDe 计算进程；stdin 接收一个请求，stdout 只输出 JSONL 事件。"""

from __future__ import annotations

import contextlib
import importlib.metadata
import platform
import sys
import traceback
from pathlib import Path

import numpy as np

# =============================================================================
# 配置区
# =============================================================================

MODEL_REGISTRY = Path(__file__).resolve().with_name("models.json")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from toolbox.denoise.protocol import DenoiseRequest, WorkerEvent  # noqa: E402

from algorithms import ALGORITHMS, AlgorithmError, run_algorithm  # noqa: E402
from inputs import build_algorithm_inputs  # noqa: E402
from quality import clip_and_quantize, difference_summary  # noqa: E402


def emit(event: WorkerEvent) -> None:
    sys.stdout.write(event.to_json() + "\n")
    sys.stdout.flush()


def _gdal():
    """取 GDAL。**必须先于 torch 被导入**，见 `_load_gdal_before_torch`。"""
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise RuntimeError("Worker 环境缺少 GDAL Python 绑定") from exc
    gdal.UseExceptions()
    return gdal


def _load_gdal_before_torch() -> None:
    """开工前先把 GDAL 拉起来。

    conda 环境里 GDAL 和 PyTorch 各自链了一份 libomp，谁先初始化谁赢，后一份会
    撞上 ``OMP: Error #15`` 并当场把进程打掉（退出码 134）。实测顺序决定成败：

        import torch            → 崩
        import torch; gdal      → 崩
        gdal; import torch      → 正常

    以前这件事是碰巧对的——读栅格自然排在跑算法之前，于是 GDAL 先入场。碰巧对
    的东西迟早会被改坏（比如哪天在算法里加一句提前 import torch 的探测），所以
    在这里显式钉死，而不是依赖调用顺序。

    注意这不是 ``KMP_DUPLICATE_LIB_OK=TRUE`` 那种绕过：那个开关是让两份 libomp
    带着"可能算错"继续跑，这里是让第二份根本不必初始化。
    """
    _gdal()


def _safe_staging_raster(staging: Path, relative: str) -> Path:
    staging = staging.resolve()
    raster = (staging / relative).resolve()
    if staging not in raster.parents:
        raise RuntimeError("RasterTask 试图访问暂存产品以外的路径")
    if not raster.is_file():
        raise RuntimeError("暂存产品中找不到核心栅格：%s" % raster)
    return raster


def _read_group(dataset, bands: tuple[int, ...]) -> np.ndarray:
    arrays = []
    for index in bands:
        array = dataset.GetRasterBand(index).ReadAsArray()
        if array is None:
            raise RuntimeError("读取第 %d 波段失败" % index)
        arrays.append(array)
    return np.stack(arrays, axis=-1)


def _group_nodata(dataset, bands: tuple[int, ...]) -> tuple[float | int | None, ...]:
    return tuple(dataset.GetRasterBand(index).GetNoDataValue() for index in bands)


def _restore_invalid(dataset, bands: tuple[int, ...], source: np.ndarray,
                     output: np.ndarray) -> None:
    for offset, index in enumerate(bands):
        band = dataset.GetRasterBand(index)
        nodata = band.GetNoDataValue()
        invalid = ~np.isfinite(source[:, :, offset])
        if nodata is not None:
            invalid |= source[:, :, offset] == nodata
        if np.any(invalid):
            output[:, :, offset][invalid] = source[:, :, offset][invalid]


def process_request(request: DenoiseRequest) -> dict:
    gdal = _gdal()
    staging = Path(request.staging_product)
    if not staging.is_dir():
        raise RuntimeError("staging_product 不存在：%s" % staging)
    spec = ALGORITHMS.get(request.algorithm)
    if spec is None:
        raise AlgorithmError("未知算法：%s" % request.algorithm)
    if spec.requires_mask and "mask" not in request.auxiliary_inputs:
        raise AlgorithmError("%s 需要由协议声明 mask 构造器" % request.algorithm)
    if not spec.requires_mask and request.auxiliary_inputs:
        raise AlgorithmError("%s 不接受附加输入：%s"
                             % (request.algorithm,
                                ", ".join(sorted(request.auxiliary_inputs))))

    total_groups = sum(len(task.valid_groups) for task in request.rasters)
    completed_groups = 0
    summaries = []
    resolved_algorithm = None
    for task in request.rasters:
        raster_path = _safe_staging_raster(staging, task.relative_raster)
        dataset = gdal.Open(str(raster_path), gdal.GA_Update)
        if dataset is None:
            raise RuntimeError("GDAL 无法以更新模式打开：%s" % raster_path)
        try:
            for group_index, bands in enumerate(task.valid_groups, start=1):
                if max(bands) > dataset.RasterCount:
                    raise RuntimeError("%s 请求波段超出栅格范围" % task.part)

                source = _read_group(dataset, bands)
                too_few = len(bands) < spec.min_bands
                k_subspace = request.algorithm_params.get("k_subspace")
                if k_subspace is not None and int(k_subspace) > len(bands):
                    too_few = True
                if too_few:
                    message = ("%s 波段组 %d-%d 不满足 %s 的最小波段/子空间合同，保持原值"
                               % (task.part, bands[0], bands[-1], request.algorithm))
                    emit(WorkerEvent("warning", request.job_id, message,
                                     {"part": task.part, "bands": list(bands)}))
                    summary = difference_summary(source, source)
                    summary.update({"part": task.part, "group_index": group_index,
                                    "bands": list(bands), "passthrough": True})
                    summaries.append(summary)
                    completed_groups += 1
                    emit(WorkerEvent(
                        "progress", request.job_id,
                        "%s 第 %d 组保持原值" % (task.part, group_index),
                        {"part": task.part, "group_index": group_index,
                         "bands": list(bands), "completed": completed_groups,
                         "total": total_groups},
                    ))
                    continue

                algorithm_inputs, input_summaries = build_algorithm_inputs(
                    source, _group_nodata(dataset, bands), request.auxiliary_inputs,
                    k_subspace=int(request.algorithm_params.get("k_subspace", 1)))
                with contextlib.redirect_stdout(sys.stderr):
                    denoised, resolved_algorithm = run_algorithm(
                        source.astype(np.float32), request.algorithm,
                        request.algorithm_params, request.device,
                        model_id=request.model_id,
                        model_registry=MODEL_REGISTRY,
                        tile_size=request.tile_size,
                        halo=request.halo,
                        **algorithm_inputs,
                    )
                quantized = clip_and_quantize(denoised, source, request.clip_mode)
                mask_config = request.auxiliary_inputs.get("mask", {})
                preserve_source_nodata = (
                    request.algorithm != "FastHyIe"
                    or mask_config.get("writeback", "preserve_source_nodata")
                    == "preserve_source_nodata"
                )
                if preserve_source_nodata:
                    _restore_invalid(dataset, bands, source, quantized)
                for offset, band_index in enumerate(bands):
                    band = dataset.GetRasterBand(band_index)
                    band.WriteArray(quantized[:, :, offset])
                    band.FlushCache()
                summary = difference_summary(source, quantized)
                summary.update({
                    "part": task.part,
                    "group_index": group_index,
                    "bands": list(bands),
                    "auxiliary_inputs": input_summaries,
                })
                summaries.append(summary)
                if summary["changed_pixels"] == 0:
                    emit(WorkerEvent(
                        "warning", request.job_id,
                        "%s 第 %d 组量化后与输入逐值相同" % (task.part, group_index),
                        {"part": task.part, "group_index": group_index,
                         "bands": list(bands)},
                    ))
                completed_groups += 1
                emit(WorkerEvent(
                    "progress", request.job_id,
                    "%s 第 %d 组完成" % (task.part, group_index),
                    {"part": task.part, "group_index": group_index,
                     "bands": list(bands), "completed": completed_groups,
                     "total": total_groups},
                ))
        finally:
            dataset.FlushCache()
            dataset = None

    return {
        "algorithm": resolved_algorithm,
        "group_summaries": summaries,
        "groups_completed": completed_groups,
        "groups_total": total_groups,
        "changed_pixels_total": int(sum(item["changed_pixels"] for item in summaries)),
        "environment": _environment_versions(),
    }


def _environment_versions() -> dict:
    versions = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "gdal": _gdal().VersionInfo("RELEASE_NAME"),
    }
    for distribution in ("torch", "hyde-images", "bm3d", "PyWavelets",
                         "kornia", "scikit-image", "pytorch-wavelets"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = None
    return versions


def main() -> int:
    _load_gdal_before_torch()
    line = sys.stdin.readline()
    job_id = "unknown"
    try:
        if not line:
            raise RuntimeError("stdin 没有 Worker 请求")
        request = DenoiseRequest.from_json(line)
        job_id = request.job_id
        emit(WorkerEvent("started", job_id, "HyDe Worker 已启动", {}))
        result = process_request(request)
        emit(WorkerEvent("completed", job_id, "HyDe Worker 已完成", result))
        return 0
    except Exception as exc:  # noqa: BLE001 Worker 必须把所有失败结构化返回
        traceback.print_exc(file=sys.stderr)
        emit(WorkerEvent("failed", job_id, str(exc),
                         {"error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
