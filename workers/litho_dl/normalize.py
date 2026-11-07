# -*- coding: utf-8 -*-
"""输入归一化：训练时在训练集上拟合一次，存进模型包，预测时原样套用。

源项目按"最大值大于 100 就乘 1e-4"临时判断：训练看第一个样本，预测看每个块
各自的最大值。某个块的值恰好都不超过 100 时，这一块的输入被放大一万倍，结果
整块错而不报错。这里把系数定死在模型里，训练与预测用的是同一个数。

三种方式
    scale           乘一个固定系数（默认）。系数留 0 时按数据给建议：整型且
                    99.9 分位超过 100 的按反射率 ×10000 存储处理，取 1e-4
    band_standard   逐波段减均值、除标准差
    pixel_l2        每个像元的光谱向量做 L2 归一化（只看光谱形状，不看亮度）
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import torch

METHODS = ("scale", "band_standard", "pixel_l2")
METHOD_LABELS = {"scale": "固定系数缩放", "band_standard": "逐波段标准化",
                 "pixel_l2": "像元光谱 L2 归一化"}
_EPS = 1e-12


def suggest_scale(sample: np.ndarray) -> float:
    if np.issubdtype(sample.dtype, np.integer):
        high = float(np.percentile(np.abs(sample), 99.9)) if sample.size else 0.0
        return 1e-4 if high > 100 else 1.0
    return 1.0


@dataclass
class Normalizer:
    method: str = "scale"
    scale: float = 1.0
    mean: list = field(default_factory=list)
    std: list = field(default_factory=list)

    @classmethod
    def fit(cls, method: str, sample: np.ndarray, scale: float = 0.0) -> "Normalizer":
        """``sample`` 形如 ``[N, 波段, P, P]``，取每块的中心像元统计。"""
        if method not in METHODS:
            raise ValueError("未知归一化方式：%s" % method)
        factor = float(scale) if scale and scale > 0 else suggest_scale(sample)
        norm = cls(method=method, scale=factor)
        if method == "band_standard":
            c = (sample.shape[-1] - 1) // 2
            spectra = sample[:, :, c, c].astype(np.float64) * factor
            norm.mean = spectra.mean(axis=0).tolist()
            std = spectra.std(axis=0)
            norm.std = np.where(std < 1e-6, 1.0, std).tolist()
        return norm

    def apply(self, x: torch.Tensor, band_axis: int = 1) -> torch.Tensor:
        """``x`` 已是浮点。块 ``[N, 波段, P, P]`` 用 band_axis=1，整块影像
        ``[波段, H, W]`` 用 band_axis=0。原地修改并返回。"""
        x.mul_(self.scale)
        if self.method == "band_standard":
            shape = [1] * x.dim()
            shape[band_axis] = -1
            mean = torch.tensor(self.mean, dtype=x.dtype, device=x.device).view(shape)
            std = torch.tensor(self.std, dtype=x.dtype, device=x.device).view(shape)
            x.sub_(mean).div_(std)
        elif self.method == "pixel_l2":
            norm = torch.linalg.vector_norm(x.float(), dim=band_axis, keepdim=True)
            x.div_(norm.clamp_min(_EPS).to(x.dtype))
        return x

    def to_json(self) -> dict:
        return {"method": self.method, "scale": self.scale, "mean": self.mean,
                "std": self.std}

    @classmethod
    def from_json(cls, data: dict) -> "Normalizer":
        return cls(method=data["method"], scale=float(data["scale"]),
                   mean=list(data.get("mean") or []), std=list(data.get("std") or []))
