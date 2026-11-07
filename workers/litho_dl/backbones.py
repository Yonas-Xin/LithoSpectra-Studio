# -*- coding: utf-8 -*-
"""骨干网络与注册表。

一个骨干只负责"一个样本 → 一个特征向量"。分类头、对比学习的投影头、输入维度
适配都在 :mod:`litho_dl.models` 里统一做，不再每个模型各写一个包装类。

每个骨干声明它要的输入形态 ``input_kind``：

==========  ======================  ==========================================
spectrum    ``[N, 1, 波段]``         只看中心像元的光谱；训练与预测都不切邻域
image       ``[N, 波段, P, P]``      波段当通道
cube        ``[N, 1, 波段, P, P]``   三维卷积
==========  ======================  ==========================================

下面六个骨干的结构与源项目 ``classifier_model/Models/Encoder.py`` 逐层一致，
参数名也一致（``tests/workers/test_litho_dl_backbones.py`` 在同一组权重下逐元素
比对输出）。新骨干放进插件目录，用 :func:`register_backbone` 声明即可。
"""

from __future__ import annotations

import dataclasses
import importlib.util
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import torch
import torch.nn as nn
import torch.nn.functional as F

INPUT_KINDS = ("spectrum", "image", "cube")
SPEEDS = ("fast", "medium", "slow", "very_slow")
SPEED_LABELS = {"fast": "速度快", "medium": "速度中等", "slow": "较慢，建议使用 CUDA",
                "very_slow": "很慢，需要 CUDA"}


@dataclass(frozen=True)
class BackboneSpec:
    name: str
    factory: Callable[[tuple], nn.Module]     # in_shape (波段, P, P) -> 模块
    input_kind: str
    min_patch: int = 1
    speed: str = "medium"
    #: 分类头的默认样式，与源项目各模型的分类器一致
    default_head: str = "mlp"
    summary: str = ""
    source: str = "builtin"

    def to_json(self) -> dict:
        return {"name": self.name, "input_kind": self.input_kind,
                "min_patch": self.min_patch, "speed": self.speed,
                "speed_label": SPEED_LABELS.get(self.speed, self.speed),
                "default_head": self.default_head, "summary": self.summary,
                "source": self.source}


BACKBONES: dict[str, BackboneSpec] = {}
#: 改过名的内置骨干：旧名 -> 现名。旧模型包的清单里记的是旧名，照样载入
ALIASES = {"Common_1DCNN": "1D-CNN", "Common_2DCNN": "2D-CNN", "Common_3DCNN": "3D-CNN"}


def canonical(name: str) -> str:
    """骨干的现名（旧名换成现名，其余原样）。"""
    return ALIASES.get(name, name)


def register_backbone(name: str, *, input_kind: str, min_patch: int = 1,
                      speed: str = "medium", default_head: str = "mlp",
                      summary: str = "", source: str = "builtin"):
    """骨干注册装饰器。被装饰的类以 ``cls(in_shape=(波段, P, P))`` 构造。"""
    if input_kind not in INPUT_KINDS:
        raise ValueError("input_kind 只能是 %s" % " / ".join(INPUT_KINDS))
    if speed not in SPEEDS:
        raise ValueError("speed 只能是 %s" % " / ".join(SPEEDS))

    def wrapper(cls):
        if name in BACKBONES and BACKBONES[name].source == "builtin" and source != "builtin":
            raise ValueError("插件骨干与内置骨干重名：%s" % name)
        BACKBONES[name] = BackboneSpec(name, lambda in_shape: cls(in_shape=in_shape),
                                       input_kind, min_patch, speed, default_head,
                                       summary, source)
        return cls
    return wrapper


def get_spec(name: str) -> BackboneSpec:
    try:
        return BACKBONES[canonical(name)]
    except KeyError:
        raise KeyError("未知骨干：%s。可选：%s" % (name, "、".join(BACKBONES))) from None


def load_plugins(directory: str | Path | None) -> list[str]:
    """导入插件目录里的每个 ``.py``，返回各文件的出错信息（成功的不列）。

    插件文件里 ``from litho_dl.backbones import register_backbone`` 后照常声明。
    一个插件写坏了只跳过它自己。
    """
    errors: list[str] = []
    if not directory:
        return errors
    folder = Path(directory).expanduser()
    if not folder.is_dir():
        return errors
    for path in sorted(folder.glob("*.py")):
        before = dict(BACKBONES)
        try:
            spec = importlib.util.spec_from_file_location("litho_dl_plugin_" + path.stem, path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
        except Exception as exc:  # 插件的任何错误都不能拖垮 Worker
            errors.append("%s：%s: %s" % (path.name, type(exc).__name__, exc))
            BACKBONES.clear()
            BACKBONES.update(before)
            continue
        clash = [n for n, spec in before.items() if BACKBONES.get(n) is not spec]
        if clash:                             # 不许插件顶掉已有的骨干
            errors.append("%s：与已有骨干重名，未载入：%s" % (path.name, "、".join(clash)))
            BACKBONES.clear()
            BACKBONES.update(before)
            continue
        for name in set(BACKBONES) - set(before):   # 插件里注册的一律标成插件，界面据此标注
            BACKBONES[name] = dataclasses.replace(BACKBONES[name], source="plugin:" + path.name)
    return errors


# =============================================================================
# 组件（与源项目一致）
# =============================================================================

class Common_1d(nn.Module):
    """卷积 → BN → ReLU。"""

    def __init__(self, in_channels, out_channels, kernel_size=3, padding=1, stride=1):
        super().__init__()
        self.conv = nn.Conv1d(in_channels, out_channels, kernel_size=kernel_size,
                              padding=padding, stride=stride)
        self.batch_norm = nn.BatchNorm1d(out_channels)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        return self.relu(self.batch_norm(self.conv(x)))


class Common_2d(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=3, padding=1, stride=1):
        super().__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size,
                              padding=padding, stride=stride)
        self.batch_norm = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        return self.relu(self.batch_norm(self.conv(x)))


class Common_3d(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=(3, 3, 3), padding=(1, 1, 1),
                 stride=1):
        super().__init__()
        self.conv = nn.Conv3d(in_channels, out_channels, kernel_size=kernel_size,
                              padding=padding, stride=stride)
        self.batch_norm = nn.BatchNorm3d(out_channels)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        return self.relu(self.batch_norm(self.conv(x)))


class Residual_block(nn.Module):
    """标准三维残差块。"""

    def __init__(self, in_channel=1, out_channel=64, kernel_size=(3, 3, 3),
                 padding=(1, 1, 1), stride=1):
        super().__init__()
        self.bottleneck = nn.Sequential(
            nn.Conv3d(in_channel, out_channel, kernel_size=kernel_size, stride=stride,
                      padding=padding, bias=False),
            nn.BatchNorm3d(out_channel),
            nn.ReLU(inplace=True),
            nn.Conv3d(out_channel, out_channel, kernel_size=kernel_size, stride=1,
                      padding=padding, bias=False),
            nn.BatchNorm3d(out_channel),
        )
        if in_channel != out_channel:
            self.use_downsample = True
            self.downsample = nn.Sequential(
                nn.Conv3d(in_channel, out_channel, kernel_size=(1, 1, 1), stride=stride,
                          bias=False),
                nn.BatchNorm3d(out_channel))
        else:
            self.use_downsample = False
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        out = self.bottleneck(x)
        if self.use_downsample:
            x = self.downsample(x)
        return self.relu(out + x)


class ECA_SpectralAttention_3d(nn.Module):
    """沿光谱维的 ECA 注意力。"""

    def __init__(self, bands, gamma=2, b=1):
        super().__init__()
        self.gap = nn.AdaptiveAvgPool3d((bands, 1, 1))
        kernel_size = int(abs((math.log(bands, 2) + b) / gamma))
        if kernel_size % 2 == 0:
            kernel_size += 1
        self.conv = nn.Conv1d(1, 1, kernel_size=kernel_size,
                              padding=(kernel_size - 1) // 2, bias=False)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        batch, _, bands, _, _ = x.shape
        gap = self.gap(x).view(batch, 1, bands)
        attn = self.sigmoid(self.conv(gap)).view(batch, 1, bands, 1, 1)
        return x * attn


class SPCModuleIN(nn.Module):
    def __init__(self, in_channels, out_channels, bias=True):
        super().__init__()
        self.s1 = nn.Conv3d(in_channels, out_channels, kernel_size=(7, 1, 1),
                            stride=(2, 1, 1), bias=False)

    def forward(self, input):
        return self.s1(input.unsqueeze(1)).squeeze(1)


class SPAModuleIN(nn.Module):
    def __init__(self, in_channels, out_channels, k=49, bias=True):
        super().__init__()
        self.s1 = nn.Conv3d(in_channels, out_channels, kernel_size=(k, 3, 3), bias=False)

    def forward(self, input):
        return self.s1(input).squeeze(2)


class ResSPC(nn.Module):
    def __init__(self, in_channels, out_channels, bias=True):
        super().__init__()
        self.spc1 = nn.Sequential(
            nn.Conv3d(in_channels, in_channels, kernel_size=(7, 1, 1), padding=(3, 0, 0),
                      bias=False),
            nn.LeakyReLU(inplace=True),
            nn.BatchNorm3d(in_channels))
        self.spc2 = nn.Sequential(
            nn.Conv3d(in_channels, in_channels, kernel_size=(7, 1, 1), padding=(3, 0, 0),
                      bias=False),
            nn.LeakyReLU(inplace=True))
        self.bn2 = nn.BatchNorm3d(out_channels)

    def forward(self, input):
        out = self.spc1(input)
        out = self.bn2(self.spc2(out))
        return F.leaky_relu(out + input)


class ResSPA(nn.Module):
    def __init__(self, in_channels, out_channels, bias=True):
        super().__init__()
        self.spa1 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1),
            nn.LeakyReLU(inplace=True),
            nn.BatchNorm2d(in_channels))
        self.spa2 = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1),
            nn.LeakyReLU(inplace=True))
        self.bn2 = nn.BatchNorm2d(out_channels)

    def forward(self, input):
        out = self.spa1(input)
        out = self.bn2(self.spa2(out))
        return F.leaky_relu(out + input)


# =============================================================================
# 骨干
# =============================================================================

@register_backbone("1D-CNN", input_kind="spectrum", min_patch=1, speed="fast",
                   default_head="mlp",
                   summary="一维光谱卷积，仅使用中心像元光谱。速度最快，适用于波段多、空间纹理弱的岩性")
class Common_1DCNN(nn.Module):
    def __init__(self, in_shape=None):
        super().__init__()
        self.conv1 = Common_1d(1, 64, kernel_size=7, padding=3, stride=2)
        self.pool1 = nn.MaxPool1d(kernel_size=2, stride=2)
        self.conv2_1 = Common_1d(64, 128, kernel_size=3, padding=1)
        self.conv2_2 = Common_1d(128, 128, kernel_size=3, padding=1, stride=2)
        self.conv3_1 = Common_1d(128, 256, kernel_size=3, padding=1)
        self.conv3_2 = Common_1d(256, 256, kernel_size=3, padding=1, stride=2)
        self.conv4_1 = Common_1d(256, 512, kernel_size=3, padding=1)
        self.conv4_2 = Common_1d(512, 512, kernel_size=3, padding=1, stride=2)
        self.pool3 = nn.AdaptiveAvgPool1d(1)

    def forward(self, x):
        x = self.pool1(self.conv1(x))
        x = self.conv2_2(self.conv2_1(x))
        x = self.conv3_2(self.conv3_1(x))
        x = self.conv4_2(self.conv4_1(x))
        return self.pool3(x)


@register_backbone("2D-CNN", input_kind="image", min_patch=3, speed="fast",
                   default_head="mlp",
                   summary="二维卷积，以波段为通道，同时利用光谱与邻域纹理")
class Common_2DCNN(nn.Module):
    def __init__(self, in_shape=None):
        super().__init__()
        bands = in_shape[0]
        self.conv1 = Common_2d(bands, 64, kernel_size=3, padding=1, stride=2)
        self.pool1 = nn.MaxPool2d(kernel_size=2, stride=2)
        self.conv2 = Common_2d(64, 128, kernel_size=3, padding=1, stride=2)
        self.conv3 = Common_2d(128, 256, kernel_size=3, padding=1)
        self.conv4 = Common_2d(256, 512, kernel_size=3, padding=1)
        self.pool3 = nn.AdaptiveAvgPool2d((1, 1))

    def forward(self, x):
        x = self.pool1(self.conv1(x))
        x = self.conv4(self.conv3(self.conv2(x)))
        return self.pool3(x)


@register_backbone("3D-CNN", input_kind="cube", min_patch=2, speed="slow",
                   default_head="linear",
                   summary="浅层三维卷积。在 Apple 芯片上无法加速，运行较慢")
class Common_3DCNN(nn.Module):
    def __init__(self, in_shape=None):
        super().__init__()
        self.conv1 = Common_3d(1, 64, kernel_size=3, padding=1, stride=(2, 1, 1))
        self.pool1 = nn.MaxPool3d(kernel_size=2, stride=2)
        self.conv2 = Common_3d(64, 128, kernel_size=(3, 3, 3), padding=(1, 1, 1), stride=(2, 1, 1))
        self.conv3 = Common_3d(128, 256, kernel_size=(3, 3, 3), padding=(1, 1, 1), stride=(2, 1, 1))
        self.conv4 = Common_3d(256, 512, kernel_size=(3, 3, 3), padding=(1, 1, 1), stride=(2, 1, 1))
        self.pool2 = nn.AdaptiveAvgPool3d((1, 1, 1))

    def forward(self, x):
        x = self.pool1(self.conv1(x))
        x = self.conv3(self.conv2(x))
        return self.pool2(self.conv4(x))


@register_backbone("SRACN", input_kind="cube", min_patch=2, speed="very_slow",
                   default_head="mlp",
                   summary="光谱注意力与三维残差网络，参数量大")
class SRACN(nn.Module):
    def __init__(self, in_shape=None):
        super().__init__()
        bands = in_shape[0]
        self.spectral_attention = ECA_SpectralAttention_3d(bands, 2, 1)
        self.conv_block = Common_3d(1, 64, 7, stride=(2, 1, 1), padding=3)
        self.pool = nn.MaxPool3d(kernel_size=2, stride=2)
        self.res_block1 = Residual_block(64, 64, (3, 3, 3), (1, 1, 1), 1)
        self.res_block2 = Residual_block(64, 128, (3, 3, 3), (1, 1, 1), (2, 1, 1))
        self.res_block3 = Residual_block(128, 128, (3, 3, 3), (1, 1, 1), 1)
        self.res_block4 = Residual_block(128, 256, (3, 3, 3), (1, 1, 1), (2, 1, 1))
        self.res_block5 = Residual_block(256, 256, (3, 3, 3), (1, 1, 1), 1)
        self.res_block6 = Residual_block(256, 512, (3, 3, 3), (1, 1, 1), (2, 1, 1))
        self.avg_pool = nn.AdaptiveAvgPool3d((1, 1, 1))

    def forward(self, x):
        x = self.spectral_attention(x)
        x = self.pool(self.conv_block(x))
        x = self.res_block3(self.res_block2(self.res_block1(x)))
        x = self.res_block5(self.res_block4(x))
        return self.avg_pool(self.res_block6(x))


@register_backbone("SSRN", input_kind="image", min_patch=3, speed="slow",
                   default_head="linear",
                   summary="光谱-空间残差网络（Zhong 等，2018），参数少但计算量大")
class SSRN(nn.Module):
    """https://github.com/zilongzhong/SSRN"""

    def __init__(self, in_shape=None):
        super().__init__()
        bands = in_shape[0]
        k = (bands - 7) // 2 + 1
        self.layer1 = SPCModuleIN(1, 28)
        self.layer2 = ResSPC(28, 28)
        self.layer3 = ResSPC(28, 28)
        self.layer4 = SPAModuleIN(28, 28, k=k)
        self.bn4 = nn.BatchNorm2d(28)
        self.layer5 = ResSPA(28, 28)
        self.layer6 = ResSPA(28, 28)

    def forward(self, x):
        x = F.leaky_relu(self.layer1(x))
        x = self.layer3(self.layer2(x))
        x = self.bn4(F.leaky_relu(self.layer4(x)))
        x = self.layer6(self.layer5(x))
        return F.avg_pool2d(x, x.size()[-1])


@register_backbone("HybridSN", input_kind="image", min_patch=9, speed="slow",
                   default_head="hybridsn",
                   summary="三维与二维卷积混合网络（Roy 等，2020），块大小至少为 9")
class HybridSN(nn.Module):
    """https://github.com/gokriznastic/HybridSN"""

    def __init__(self, in_shape=None):
        super().__init__()
        bands, h, _w = in_shape
        self.conv1 = nn.Conv3d(1, 8, (7, 3, 3))
        self.conv2 = nn.Conv3d(8, 16, (5, 3, 3))
        self.conv3 = nn.Conv3d(16, 32, (3, 3, 3))
        self.conv3_2d = nn.Conv2d((bands - 12) * 32, 64, (3, 3))
        h = h - 8
        self.fc = nn.Linear(h * h * 64, 256)
        self.relu = nn.ReLU()

    def forward(self, x):
        x = x.unsqueeze(1)
        out = self.relu(self.conv1(x))
        out = self.relu(self.conv2(out))
        out = self.relu(self.conv3(out))
        out = out.reshape(-1, out.shape[1] * out.shape[2], out.shape[3], out.shape[4])
        out = self.relu(self.conv3_2d(out))
        return self.fc(out.reshape(out.size(0), -1))
