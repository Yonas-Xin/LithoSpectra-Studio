# -*- coding: utf-8 -*-
"""去噪输出的无参考数值质量摘要与安全量化。"""

from __future__ import annotations

from typing import Any

import numpy as np


class QualityError(RuntimeError):
    pass


def clip_and_quantize(denoised: np.ndarray, source: np.ndarray,
                      clip_mode: str) -> np.ndarray:
    denoised = np.asarray(denoised, dtype=np.float32)
    source = np.asarray(source)
    if denoised.shape != source.shape:
        raise QualityError("量化前输出形状与源数据不一致")
    if not np.isfinite(denoised).all():
        raise QualityError("量化前输出含 NaN/Inf")
    if not np.issubdtype(source.dtype, np.integer):
        raise QualityError("首版产品写回只支持整数源数据")
    dtype_limits = np.iinfo(source.dtype)
    if clip_mode == "source_band_range":
        lower = source.min(axis=(0, 1), keepdims=True).astype(np.float32)
        upper = source.max(axis=(0, 1), keepdims=True).astype(np.float32)
    elif clip_mode == "dtype_range":
        lower = float(dtype_limits.min)
        upper = float(dtype_limits.max)
    else:
        raise QualityError("未知 clip_mode：%s" % clip_mode)
    clipped = np.clip(denoised, lower, upper)
    return np.rint(clipped).astype(source.dtype)


def difference_summary(source: np.ndarray, output: np.ndarray) -> dict[str, Any]:
    """去噪前后的差异摘要。

    残差在 float32 上算。以前这里把 source 和 output 都提升成 float64，整景一个
    76 波段组就是两份 2.5 GB 的临时数组；实测 2.69 s，改成 float32 后 1.01 s，
    四个波段组一次运行省下约 7 s 和 5 GB 峰值。

    两端都是整数 DN，残差也是整数值，float32 有 24 位尾数，精确表示的整数上限
    是 2^24 = 16777216，远超 uint16 的量程——所以差值本身一位不差。只有平方和
    在累加上千万项时会掉精度，那一处仍走 float64 累加器。
    """
    source_f = np.asarray(source, dtype=np.float32)
    output_f = np.asarray(output, dtype=np.float32)
    if source_f.shape != output_f.shape:
        raise QualityError("质量统计的输入输出形状不一致")
    residual = output_f - source_f
    absolute = np.abs(residual)
    changed = int(np.count_nonzero(residual))
    return {
        "pixels": int(source_f.size),
        "changed_pixels": changed,
        "changed_fraction": float(changed / max(1, residual.size)),
        "mean_absolute_change": float(np.mean(absolute, dtype=np.float64)),
        "rmse_change": float(np.sqrt(np.mean(np.square(residual, dtype=np.float64),
                                             dtype=np.float64))),
        "maximum_absolute_change": float(np.max(absolute)),
        "source_min": float(np.min(source_f)),
        "source_max": float(np.max(source_f)),
        "output_min": float(np.min(output_f)),
        "output_max": float(np.max(output_f)),
    }


def reference_quality(reference: np.ndarray, estimate: np.ndarray,
                      data_range: float | None = None) -> dict[str, float]:
    """合成噪声 ROI 门禁使用的 PSNR、SAM、SSIM 与残差条带指标。"""
    reference = np.asarray(reference, dtype=np.float64)
    estimate = np.asarray(estimate, dtype=np.float64)
    if reference.shape != estimate.shape or reference.ndim != 3:
        raise QualityError("reference/estimate 必须是同形状 HWC 立方体")
    if not np.isfinite(reference).all() or not np.isfinite(estimate).all():
        raise QualityError("参考质量评价输入含 NaN/Inf")
    residual = estimate - reference
    mse = float(np.mean(residual ** 2))
    if data_range is None:
        data_range = float(reference.max() - reference.min())
    if data_range <= 0:
        raise QualityError("PSNR data_range 必须为正")
    psnr = float("inf") if mse == 0 else float(10 * np.log10(data_range ** 2 / mse))

    dot = np.sum(reference * estimate, axis=-1)
    denominator = (np.linalg.norm(reference, axis=-1)
                   * np.linalg.norm(estimate, axis=-1))
    valid = denominator > 0
    cosine = np.ones_like(dot)
    cosine[valid] = np.clip(dot[valid] / denominator[valid], -1.0, 1.0)
    sam = float(np.degrees(np.mean(np.arccos(cosine))))

    try:
        from skimage.metrics import structural_similarity
        band_ssim = [
            structural_similarity(reference[:, :, index], estimate[:, :, index],
                                  data_range=data_range)
            for index in range(reference.shape[2])
        ]
        ssim = float(np.mean(band_ssim))
    except ImportError:  # pragma: no cover - 隔离环境清单已固定 scikit-image
        ssim = float("nan")

    residual_rms = float(np.sqrt(mse))
    column_bias = np.mean(residual, axis=0)
    stripe_ratio = (float(np.sqrt(np.mean(column_bias ** 2))) / residual_rms
                    if residual_rms > 0 else 0.0)
    return {
        "psnr_db": psnr,
        "sam_degrees": sam,
        "ssim_mean": ssim,
        "rmse": residual_rms,
        "column_bias_to_residual_rms": stripe_ratio,
    }
