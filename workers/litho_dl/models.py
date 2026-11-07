# -*- coding: utf-8 -*-
"""分类模型 = 骨干 + 分类头；输入形态适配统一在这里做。

数据侧永远给出 ``[N, 波段, P, P]`` 的块（``spectrum`` 骨干在预测时例外，直接给
``[N, 1, 波段]``，见 :func:`adapt`）。属性名沿用源项目的 ``encoder`` / ``decoder``，
源项目训练出的权重字典可以原样载入，测试据此与原实现逐元素比对。
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from .backbones import BackboneSpec, get_spec

HEADS = ("auto", "linear", "mlp", "hybridsn")
HEAD_LABELS = {"auto": "跟随骨干", "linear": "线性层", "mlp": "三层全连接",
               "hybridsn": "Dropout + 两层全连接"}


class MLPHead(nn.Module):
    """源项目的 ``deep_classfier``：in → 1024 → 1024 → 类别数。"""

    def __init__(self, in_features: int, out_features: int, mid: int = 1024):
        super().__init__()
        self.fc1 = nn.Linear(in_features, mid)
        self.fc2 = nn.Linear(mid, 1024)
        self.fc3 = nn.Linear(1024, out_features)

    def forward(self, x):
        x = F.relu(self.fc1(x), inplace=True)
        x = F.relu(self.fc2(x), inplace=True)
        return self.fc3(x)


def build_head(kind: str, in_features: int, num_classes: int) -> nn.Module:
    if kind == "linear":
        return nn.Linear(in_features, num_classes)
    if kind == "mlp":
        return MLPHead(in_features, num_classes)
    if kind == "hybridsn":
        return nn.Sequential(nn.Dropout(0.4), nn.Linear(in_features, 128),
                             nn.Dropout(0.4), nn.Linear(128, num_classes))
    raise ValueError("未知分类头：%s，可选 %s" % (kind, " / ".join(HEADS)))


def center_index(patch: int) -> int:
    """块中心的行列号。偶数块中心偏左上，与样本裁剪一致。"""
    return (patch - 1) // 2


def adapt(x: torch.Tensor, input_kind: str) -> torch.Tensor:
    """把 ``[N, 波段, P, P]`` 的块转成骨干要的形态。``[N, 1, 波段]`` 原样放行。"""
    if input_kind == "spectrum":
        if x.dim() == 3:
            return x
        c = center_index(x.shape[-1])
        return x[:, :, c, c].unsqueeze(1)
    if input_kind == "image":
        return x
    if input_kind == "cube":
        return x.unsqueeze(1)
    raise ValueError("未知输入形态：%s" % input_kind)


def feature_dim(encoder: nn.Module, input_kind: str, in_shape: tuple) -> int:
    """空跑一次量出骨干输出维度。块太小、波段太少时在这里就报出可读的错误。"""
    bands, patch, _ = in_shape
    probe = torch.zeros((2, bands, patch, patch))
    was_training = encoder.training
    encoder.eval()
    try:
        with torch.no_grad():
            out = encoder(adapt(probe, input_kind))
    except RuntimeError as exc:
        raise ValueError("骨干与输入不匹配（波段 %d、块大小 %d）：%s"
                         % (bands, patch, str(exc).splitlines()[0])) from exc
    finally:
        encoder.train(was_training)
    return int(out.reshape(out.shape[0], -1).shape[1])


class Classifier(nn.Module):
    def __init__(self, backbone: str, in_shape: tuple, num_classes: int,
                 head: str = "auto") -> None:
        super().__init__()
        spec: BackboneSpec = get_spec(backbone)
        bands, patch, _ = in_shape
        if patch < spec.min_patch:
            raise ValueError("%s 要求块大小至少 %d，当前数据集是 %d"
                             % (backbone, spec.min_patch, patch))
        self.spec = spec
        self.input_kind = spec.input_kind
        self.head_kind = spec.default_head if head == "auto" else head
        self.encoder = spec.factory(tuple(in_shape))
        self.feature_dim = feature_dim(self.encoder, self.input_kind, tuple(in_shape))
        self.decoder = build_head(self.head_kind, self.feature_dim, num_classes)

    def features(self, x: torch.Tensor) -> torch.Tensor:
        """``x`` 已是骨干要的形态。"""
        return self.encoder(x).flatten(1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.decoder(self.features(x))


def count_parameters(model: nn.Module) -> int:
    return int(sum(p.numel() for p in model.parameters()))


class FeatureNet(nn.Module):
    """只有骨干：对比学习的成果，或特征分析时从任意模型包里取出的骨干。"""

    def __init__(self, backbone: str, in_shape: tuple) -> None:
        super().__init__()
        spec: BackboneSpec = get_spec(backbone)
        if in_shape[1] < spec.min_patch:
            raise ValueError("%s 要求块大小至少 %d，当前是 %d" % (backbone, spec.min_patch, in_shape[1]))
        self.spec = spec
        self.input_kind = spec.input_kind
        self.encoder = spec.factory(tuple(in_shape))
        self.feature_dim = feature_dim(self.encoder, self.input_kind, tuple(in_shape))

    def features(self, x: torch.Tensor) -> torch.Tensor:
        return self.encoder(x).flatten(1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.features(x)


class ContrastiveNet(FeatureNet):
    """骨干 + 投影头。投影头只在对比学习时用，模型包里只存骨干。"""

    def __init__(self, backbone: str, in_shape: tuple, proj_dim: int = 128) -> None:
        super().__init__(backbone, in_shape)
        hidden = max(256, min(1024, self.feature_dim * 2))
        self.projector = nn.Sequential(nn.Linear(self.feature_dim, hidden), nn.ReLU(inplace=True),
                                       nn.Linear(hidden, proj_dim))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.projector(self.features(x))
