# -*- coding: utf-8 -*-
"""样本特征分析：用骨干给样本集的块算特征，看样本在特征空间里怎么分布。

产出 ``eval/embedding.npz``：

* ``xy``          特征的前两个主成分，画散点用
* ``labels``      样本标签
* ``suspicion``   近邻里与自身标签不同的比例（0..1）。分高的块，要么是标错了，要么
                  是两类光谱本身难以区分，两种情况都应回样本库核查
* ``majority``    近邻里最多的那个标签
* ``index`` / ``centers`` / ``sample_keys``   回到样本集与样本库的定位信息

骨干可以是对比学习的成果，也可以是分类模型里的那个骨干。
"""

from __future__ import annotations

import numpy as np
import torch
import torch.nn.functional as F

from . import devices
from .data import H5Info, class_table_for, compact_dtype, make_source, read_rows, resolve_dataset
from .jobs import Job, JobError, write_json_atomic
from .normalize import Normalizer
from .package import load_feature_model, with_colors
from .params import Param
from .train import prepare


def params() -> tuple:
    return (
        Param("model", "模型", None, "path", path_mode="directory", extensions=("lithomodel",),
              help="对比学习骨干或分类模型的模型包"),
        Param("dataset", "样本集", None, "path", path_mode="any", extensions=("h5",)),
        Param("split", "用哪部分", "all", "choice", choices=("all", "train", "test"),
              choice_labels=(("all", "训练集 + 测试集"), ("train", "训练集"), ("test", "测试集"))),
        Param("max_samples", "最多样本块数", 20000, "int", minimum=100, maximum=500000,
              help="超出时按类别分层随机抽取"),
        Param("knn_k", "近邻数", 10, "int", minimum=2, maximum=100),
        Param("device", "计算设备", "auto", "choice", choices=devices.DEVICE_CHOICES),
    )


def _pick(labels: np.ndarray, limit: int, rng) -> np.ndarray:
    if labels.size <= limit:
        return np.arange(labels.size)
    out = []
    for c in np.unique(labels):
        idx = np.nonzero(labels == c)[0]
        take = max(1, int(round(limit * idx.size / labels.size)))
        out.append(rng.choice(idx, size=min(take, idx.size), replace=False))
    return np.sort(np.concatenate(out))


def run(job: Job, p: dict) -> dict:
    device = devices.resolve_device(p["device"])
    model, manifest = load_feature_model(p["model"], device)
    norm = Normalizer.from_json(manifest["normalization"])
    train, test = resolve_dataset(p["dataset"])
    info = H5Info.read(train)
    if (info.bands, info.patch) != (int(manifest["bands"]), int(manifest["patch_size"])) and \
            manifest.get("input_kind") != "spectrum":
        raise JobError("样本集（%d 波段，块大小 %d）与模型（%s 波段，块大小 %s）不一致"
                       % (info.bands, info.patch, manifest["bands"], manifest["patch_size"]))
    if info.bands != int(manifest["bands"]):
        raise JobError("样本集有 %d 个波段，模型需要 %s 个" % (info.bands, manifest["bands"]))
    parts = []
    if p["split"] in ("all", "train"):
        parts.append(("train", train))
    if p["split"] in ("all", "test") and test is not None:
        parts.append(("test", test))
    if not parts:
        raise JobError("该样本集没有测试集")
    rng = np.random.default_rng(0)
    feats, labels, splits, index, centers, keys = [], [], [], [], [], []
    budget = devices.memory_budget(device, 0.3)
    job.phase("embed", "计算特征")
    for name, h5 in parts:
        hinfo = H5Info.read(h5)
        meta = read_rows(h5, fields=("labels", "centers", "sample_keys"))
        quota = int(p["max_samples"]) * hinfo.count // max(1, sum(H5Info.read(x).count for _, x in parts))
        idx = _pick(meta["labels"], max(1, quota), rng)
        src, _ = make_source(h5, idx, hinfo, device, budget,
                             compact_dtype(np.dtype(hinfo.dtype), 30000))
        out = []
        with torch.inference_mode():
            for b, (xb, _yb) in enumerate(src.batches(1024, shuffle=False)):
                job.check_cancel()
                out.append(F.normalize(model.features(prepare(xb, norm, model.input_kind)).float(), dim=1).cpu())
        feats.append(torch.cat(out))
        labels.append(meta["labels"][idx])
        splits.append(np.full(idx.size, 0 if name == "train" else 1, dtype=np.int8))
        index.append(idx)
        centers.append(meta["centers"][idx] if "centers" in meta else np.zeros((idx.size, 2), np.int32))
        keys.append(meta["sample_keys"][idx] if "sample_keys" in meta else np.full(idx.size, "", dtype=str))
        job.log("%s：%d 个块" % ("训练集" if name == "train" else "测试集", idx.size))
    f = torch.cat(feats)
    y = np.concatenate(labels).astype(np.int64)

    job.phase("analyse", "计算主成分与近邻")
    centered = f - f.mean(dim=0, keepdim=True)
    _u, _s, v = torch.linalg.svd(centered[: min(20000, centered.shape[0])], full_matrices=False)
    xy = (centered @ v[:2].T).numpy().astype(np.float32)
    explained = (_s[:2] ** 2 / (_s ** 2).sum()).numpy().tolist()

    k = min(int(p["knn_k"]), f.shape[0] - 1)
    yt = torch.from_numpy(y)
    suspicion = np.zeros(f.shape[0], dtype=np.float32)
    majority = np.zeros(f.shape[0], dtype=np.int64)
    fd = f.to(device)
    for s in range(0, f.shape[0], 2048):
        job.check_cancel()
        sim = fd[s:s + 2048] @ fd.T
        rows = torch.arange(s, min(s + 2048, f.shape[0]), device=device)
        sim[torch.arange(rows.numel(), device=device), rows] = -2      # 不算自己
        nb = sim.topk(k, dim=1).indices.cpu()
        nl = yt[nb]
        suspicion[s:s + 2048] = (nl != yt[s:s + 2048, None]).float().mean(1).numpy()
        majority[s:s + 2048] = torch.mode(nl, dim=1).values.numpy()
    job.path("eval").mkdir(exist_ok=True)
    np.savez(job.path("eval", "embedding.npz"),
             xy=xy, labels=y, suspicion=suspicion, majority=majority,
             split=np.concatenate(splits), index=np.concatenate(index),
             centers=np.concatenate(centers), sample_keys=np.concatenate(keys).astype(str))
    classes = with_colors(class_table_for(info))
    per_class = []
    for c in classes:
        sel = y == c["label"]
        per_class.append({"label": c["label"], "name": c["name"], "count": int(sel.sum()),
                          "suspicion": float(suspicion[sel].mean()) if sel.any() else 0.0})
    summary = {"count": int(y.size), "explained": explained, "knn_k": k,
               "mean_suspicion": float(suspicion.mean()), "per_class": per_class,
               "classes": [{"label": c["label"], "name": c["name"], "color": c["color"]} for c in classes],
               "model": manifest.get("name"), "dataset": str(p["dataset"])}
    write_json_atomic(job.path("eval", "embedding.json"), summary)
    job.emit("artifact", "特征分析：%d 个块" % y.size, kind="embedding",
             path=str(job.path("eval", "embedding.npz")))
    return summary
