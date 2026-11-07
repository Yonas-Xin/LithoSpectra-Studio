# -*- coding: utf-8 -*-
"""从原始光谱数据构造算法附加输入。"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

try:  # 包导入用于测试；脚本导入用于隔离 Worker 直接执行。
    from .algorithms import AlgorithmError
except ImportError:  # pragma: no cover - 由 worker.py 脚本入口覆盖
    from algorithms import AlgorithmError


MASK_POLICIES = {"nodata", "nodata_zero", "nodata_zero_saturation"}


def build_raw_validity_mask(
        image: np.ndarray,
        nodata_values: Iterable[float | int | None],
        policy: str = "nodata",
        minimum_observed_bands: int = 1,
) -> tuple[np.ndarray, dict[str, Any]]:
    """构造 FastHyIe 掩膜，严格遵守上游 ``1=可用、0=缺失`` 语义。

    ``nodata`` 是保守默认值；零值和数据类型上限只有在用户显式选择相应策略时才
    视为缺失。返回掩膜与可写入 provenance 的诊断摘要。
    """
    cube = np.asarray(image)
    if cube.ndim != 3:
        raise AlgorithmError("掩膜构造输入必须是 [H, W, B]")
    if policy not in MASK_POLICIES:
        raise AlgorithmError("未知 mask policy：%s" % policy)
    nodata = tuple(nodata_values)
    if len(nodata) != cube.shape[2]:
        raise AlgorithmError("NoData 数量与输入波段数不一致")

    invalid = ~np.isfinite(cube)
    reasons = {"nonfinite": int(invalid.sum())}
    nodata_hits = np.zeros(cube.shape, dtype=bool)
    for offset, value in enumerate(nodata):
        if value is not None:
            nodata_hits[:, :, offset] = cube[:, :, offset] == value
    invalid |= nodata_hits
    reasons["nodata"] = int(nodata_hits.sum())

    if policy in {"nodata_zero", "nodata_zero_saturation"}:
        zero_hits = cube == 0
        invalid |= zero_hits
        reasons["zero"] = int(zero_hits.sum())
    if policy == "nodata_zero_saturation":
        if not np.issubdtype(cube.dtype, np.integer):
            raise AlgorithmError("饱和值掩膜策略只适用于整数原始数据")
        saturation_hits = cube == np.iinfo(cube.dtype).max
        invalid |= saturation_hits
        reasons["dtype_max"] = int(saturation_hits.sum())

    mask = (~invalid).astype(np.float32)
    observed = mask.sum(axis=2)
    insufficient = observed < int(minimum_observed_bands)
    if np.any(insufficient):
        raise AlgorithmError(
            "FastHyIe 有 %d 个像元的可用波段少于 k_subspace=%d；"
            "请调整掩膜策略或子空间维数"
            % (int(insufficient.sum()), int(minimum_observed_bands)))
    return mask, {
        "builder": "raw_validity",
        "policy": policy,
        "meaning": "1=viable,0=missing",
        "shape": list(mask.shape),
        "valid_elements": int(mask.sum()),
        "missing_elements": int(mask.size - mask.sum()),
        "pixels_with_missing_bands": int(np.any(invalid, axis=2).sum()),
        "minimum_observed_bands": int(observed.min()),
        "invalid_reason_hits": reasons,
    }


def build_algorithm_inputs(image: np.ndarray, nodata_values: Iterable[Any],
                           config: dict[str, dict[str, Any]],
                           k_subspace: int) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """按协议配置构造算法关键字输入；未知 builder 直接拒绝。"""
    built: dict[str, np.ndarray] = {}
    summaries: dict[str, Any] = {}
    for name, options in config.items():
        builder = options.get("builder")
        if name == "mask" and builder == "raw_validity":
            writeback = options.get("writeback", "preserve_source_nodata")
            if writeback not in {"preserve_source_nodata", "inpaint_masked"}:
                raise AlgorithmError("未知 mask writeback 策略：%s" % writeback)
            mask, summary = build_raw_validity_mask(
                image, nodata_values, policy=str(options.get("policy", "nodata")),
                minimum_observed_bands=int(k_subspace))
            built[name] = mask
            summary["writeback"] = writeback
            summaries[name] = summary
        else:
            raise AlgorithmError("不支持的算法附加输入：%s/%s" % (name, builder))
    return built, summaries
