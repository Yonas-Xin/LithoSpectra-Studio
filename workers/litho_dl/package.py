# -*- coding: utf-8 -*-
"""模型包：一个以 ``.lithomodel`` 结尾的目录。

::

    <名称>.lithomodel/
    ├── manifest.json     结构、输入、归一化、类别表、来源与指标摘要
    ├── weights.pt        纯 state_dict，以 torch.load(weights_only=True) 读取
    ├── config.json       训练参数全文
    └── metrics.json      逐轮指标与测试集报告

预测只认 manifest：patch、波段数、归一化、类别名与颜色都从这里来，不从文件名
猜，也不反序列化整个 Python 对象。源项目的 ``torch.save(model)`` 依赖源码
目录结构，挪动模块路径后旧模型全部打不开，加载来路不明的文件还会执行任意代码。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import torch

from . import __version__
from .jobs import JobError, write_json_atomic
from .models import Classifier, FeatureNet

SUFFIX = ".lithomodel"
FORMAT_VERSION = 1

#: 没有类别颜色时的默认配色（与样本库的类别调色板同一路数：饱和度适中、彼此好分）
DEFAULT_COLORS = (
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2",
    "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ffbb78", "#98df8a", "#ff9896",
    "#c5b0d5", "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5",
)


def default_color(label: int) -> str:
    if label < len(DEFAULT_COLORS):
        return DEFAULT_COLORS[label]
    import colorsys
    h = (label * 0.618033988749895) % 1.0
    r, g, b = colorsys.hsv_to_rgb(h, 0.65, 0.85)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def with_colors(classes: list[dict]) -> list[dict]:
    out = []
    for row in classes:
        row = dict(row)
        if not row.get("color"):
            row["color"] = default_color(int(row["label"]))
        out.append(row)
    return out


def save(dest: Path, manifest: dict, state_dict: dict, config: dict | None = None,
         metrics: dict | None = None) -> Path:
    """写模型包。先写到临时目录再改名，中途失败不留下半个包。"""
    dest = Path(dest)
    if dest.suffix != SUFFIX:
        dest = dest.with_name(dest.name + SUFFIX)
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    manifest = dict(manifest)
    manifest.update({"format": "lithomodel", "format_version": FORMAT_VERSION,
                     "worker_version": __version__, "torch_version": torch.__version__,
                     "created": manifest.get("created") or time.time()})
    if manifest.get("classes"):
        manifest["classes"] = with_colors(manifest["classes"])
    torch.save({k: v.detach().cpu() for k, v in state_dict.items()}, tmp / "weights.pt")
    write_json_atomic(tmp / "manifest.json", manifest)
    write_json_atomic(tmp / "config.json", config or {})
    write_json_atomic(tmp / "metrics.json", metrics or {})
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(tmp, dest)
    return dest


def read_manifest(path: str | Path) -> dict:
    path = Path(path).expanduser()
    target = path / "manifest.json" if path.is_dir() else path
    if not target.is_file():
        raise JobError("不是模型包（缺少 manifest.json）：%s" % path)
    with open(target, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    if manifest.get("format") != "lithomodel":
        raise JobError("不是岩谱模型包：%s" % path)
    if int(manifest.get("format_version", 0)) > FORMAT_VERSION:
        raise JobError("模型包版本 %s 高于当前 Worker 支持的版本，请更新岩谱"
                       % manifest.get("format_version"))
    return manifest


def load_classifier(path: str | Path, device: torch.device | str = "cpu"
                    ) -> tuple[Classifier, dict]:
    path = Path(path).expanduser()
    manifest = read_manifest(path)
    if manifest.get("task") != "classifier":
        raise JobError("该模型包不是分类模型（task=%s）" % manifest.get("task"))
    model = Classifier(manifest["backbone"],
                       (manifest["bands"], manifest["patch_size"], manifest["patch_size"]),
                       len(manifest["classes"]), head=manifest.get("head", "auto"))
    try:
        state = torch.load(path / "weights.pt", map_location="cpu", weights_only=True)
    except Exception as exc:
        raise JobError("无法读取模型权重：%s\n%s" % (path / "weights.pt", exc)) from exc
    model.load_state_dict(state, strict=True)
    model.to(device).eval()
    return model, manifest


def load_weights(path: str | Path) -> dict:
    path = Path(path).expanduser()
    try:
        return torch.load(path / "weights.pt", map_location="cpu", weights_only=True)
    except Exception as exc:
        raise JobError("无法读取模型权重：%s\n%s" % (path / "weights.pt", exc)) from exc


def encoder_state(state: dict) -> dict:
    """模型权重里骨干那部分，去掉 ``encoder.`` 前缀。"""
    out = {k[len("encoder."):]: v for k, v in state.items() if k.startswith("encoder.")}
    if not out:
        raise JobError("模型包中没有骨干网络权重（encoder.*）")
    return out


def load_feature_model(path: str | Path, device: torch.device | str = "cpu"):
    """任何模型包都能当特征提取器：分类模型取它的骨干，对比学习的骨干直接用。"""
    path = Path(path).expanduser()
    manifest = read_manifest(path)
    task = manifest.get("task")
    if task == "classifier":
        return load_classifier(path, device)
    if task != "backbone":
        raise JobError("不支持的模型包类型：%s" % task)
    model = FeatureNet(manifest["backbone"],
                       (manifest["bands"], manifest["patch_size"], manifest["patch_size"]))
    model.encoder.load_state_dict(encoder_state(load_weights(path)), strict=True)
    model.to(device).eval()
    return model, manifest
