# -*- coding: utf-8 -*-
"""按 batch 在设备上做的数据增强，输入输出都是 ``[N, 波段, P, P]``。

几何变换只用翻转与 90° 倍数旋转：源项目默认的任意角旋转要插值，会把相邻像元
的光谱混成现实中不存在的光谱。奇数块翻转、旋转后中心像元不动，偶数块中心会
挪一格，因此偶数块不做几何变换。

光谱维的增强（光谱掩膜、波段丢弃、噪声、亮度缩放）对 ``spectrum`` 骨干同样
有效，几何变换对它没有意义。
"""

from __future__ import annotations

from dataclasses import dataclass

import torch


@dataclass
class Augment:
    flip: bool = True
    rotate: bool = True
    #: 每个样本以这个概率随机置零一部分（spectral_mask_ratio）波段×像元
    spectral_mask_p: float = 0.0
    spectral_mask_ratio: float = 0.25
    #: 每个样本以这个概率整段丢弃一部分（band_drop_ratio）波段
    band_dropout_p: float = 0.0
    band_drop_ratio: float = 0.1
    noise_std: float = 0.0
    #: 每个样本整体亮度乘 1 ± brightness 内的随机数，模拟光照差异
    brightness: float = 0.0
    #: 每个样本以这个概率擦除一个矩形（保留中心像元）
    erase_p: float = 0.0

    def active(self) -> bool:
        return any((self.flip, self.rotate, self.spectral_mask_p, self.band_dropout_p,
                    self.noise_std, self.brightness, self.erase_p))

    @torch.no_grad()
    def __call__(self, x: torch.Tensor) -> torch.Tensor:
        n, bands, h, w = x.shape
        dev = x.device
        geometric = h == w and h % 2 == 1 and h > 1
        if geometric and self.flip:
            mask = torch.rand(n, device=dev) < 0.5
            x = torch.where(mask.view(n, 1, 1, 1), x.flip(-1), x)
            mask = torch.rand(n, device=dev) < 0.5
            x = torch.where(mask.view(n, 1, 1, 1), x.flip(-2), x)
        if geometric and self.rotate:
            k = torch.randint(0, 4, (n,), device=dev)
            out = x.clone()
            for turns in (1, 2, 3):
                sel = (k == turns).nonzero(as_tuple=True)[0]
                if sel.numel():
                    out[sel] = torch.rot90(x[sel], turns, dims=(-2, -1))
            x = out
        if self.brightness > 0:
            factor = 1 + (torch.rand(n, 1, 1, 1, device=dev, dtype=x.dtype) * 2 - 1) * self.brightness
            x = x * factor
        if self.spectral_mask_p > 0:
            hit = torch.rand(n, 1, 1, 1, device=dev) < self.spectral_mask_p
            keep = torch.rand(n, bands, h, w, device=dev) >= self.spectral_mask_ratio
            x = x * (keep | ~hit).to(x.dtype)
        if self.band_dropout_p > 0:
            hit = torch.rand(n, 1, 1, 1, device=dev) < self.band_dropout_p
            keep = torch.rand(n, bands, 1, 1, device=dev) >= self.band_drop_ratio
            x = x * (keep | ~hit).to(x.dtype)
        if self.noise_std > 0:
            x = x + torch.randn_like(x) * self.noise_std
        if self.erase_p > 0 and h > 2:
            x = self._erase(x)
        return x

    def _erase(self, x: torch.Tensor) -> torch.Tensor:
        n, _, h, w = x.shape
        dev = x.device
        hit = torch.rand(n, device=dev) < self.erase_p
        eh = torch.randint(1, h // 2 + 1, (n,), device=dev)
        ew = torch.randint(1, w // 2 + 1, (n,), device=dev)
        top = (torch.rand(n, device=dev) * (h - eh + 1).float()).long()
        left = (torch.rand(n, device=dev) * (w - ew + 1).float()).long()
        rows = torch.arange(h, device=dev).view(1, h, 1)
        cols = torch.arange(w, device=dev).view(1, 1, w)
        box = ((rows >= top.view(n, 1, 1)) & (rows < (top + eh).view(n, 1, 1))
               & (cols >= left.view(n, 1, 1)) & (cols < (left + ew).view(n, 1, 1)))
        c = (h - 1) // 2
        box[:, c, c] = False
        box &= hit.view(n, 1, 1)
        return x.masked_fill(box.unsqueeze(1), 0)
