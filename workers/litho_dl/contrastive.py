# -*- coding: utf-8 -*-
"""对比学习预训练：不要标签，从整景影像（或现成样本集）里学一个骨干。

两种目标
    simclr   批内负样本：同一块的两个增强视图互为正样本，批内其他块都是负样本
             （源项目的 ETE 模式；那里第二个编码器的参数每步直接指向第一个，等于
             同一个网络不求梯度再算一遍，这里就用一个编码器）
    moco     动量编码器 + 负样本队列：批小也有大量负样本，队列维度跟随投影头

选优：给了带标签的样本集时，每隔几轮用骨干特征做 kNN 分类，按精度选最佳骨干；
源项目按"训练集上的前置任务 Top-1"选优，那个指标与下游分类效果无关。没给就存
最后一轮。

成果是 ``task=backbone`` 的模型包，只含骨干权重。分类训练时选它作"预训练骨干"，
可冻结、整体微调或骨干小学习率微调。

任务目录里的成果::

    unlabeled.h5            从影像采的无标签块（续跑时直接用）
    checkpoints/last.pt     续训用
    checkpoints/best.pt     kNN 精度最好的一轮（没有监控集时就是最后一轮）
    metrics.json            逐轮损失、对比准确率、kNN 精度
    model/<名称>.lithomodel  骨干模型包
"""

from __future__ import annotations

import copy
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from . import devices
from .augment import Augment
from .backbones import ALIASES, BACKBONES
from .data import (H5Info, compact_dtype, make_source, read_rows, resolve_dataset,
                   split_validation)
from .jobs import Cancelled, Job, JobError, write_json_atomic
from .models import ContrastiveNet, adapt, center_index, count_parameters
from .normalize import METHOD_LABELS, METHODS, Normalizer
from .package import SUFFIX, save as save_package
from .params import Param, when
from .train import lr_lambda, prepare, recalibrate_bn

NORMALIZER_SAMPLE = 20000
PROGRESS_INTERVAL = 2.0
SAMPLE_BLOCK = 512


def params() -> tuple:
    names = tuple(BACKBONES)
    return (
        Param("source", "无标签数据", "image", "choice", choices=("image", "dataset"),
              choice_labels=(("image", "从整景影像随机采样"), ("dataset", "已有样本集（不使用标签）"))),
        Param("image", "影像", "", "path", path_mode="file", optional=True,
              visible_when=when(source="image"), help="从影像的有效像元中随机采样"),
        Param("mask", "采样范围", "", "path", path_mode="file", optional=True,
              extensions=("shp", "gpkg", "geojson", "kml"), visible_when=when(source="image"),
              help="仅在面内采样，留空则使用全图"),
        Param("samples", "采样块数", 50000, "int", minimum=500, maximum=2000000,
              visible_when=when(source="image")),
        Param("patch_size", "块大小", 9, "int", minimum=1, maximum=64,
              visible_when=when(source="image"),
              help="用该骨干训练分类模型时，样本集的块大小须与此一致"),
        Param("dataset", "样本集", "", "path", path_mode="any", extensions=("h5",), optional=True,
              visible_when=when(source="dataset")),
        Param("eval_dataset", "监控用样本集", "", "path", path_mode="any", extensions=("h5",),
              optional=True, help="带标签的样本集，定期用骨干特征做 kNN 分类并据此选出最佳一轮；"
                                  "波段数与块大小须与无标签数据一致。留空则保存最后一轮"),
        Param("backbone", "骨干网络", "2D-CNN" if "2D-CNN" in names else names[0],
              "choice", choices=names, aliases=tuple(ALIASES.items())),
        Param("method", "对比方式", "simclr", "choice", choices=("simclr", "moco"),
              choice_labels=(("simclr", "批内负样本（SimCLR）"), ("moco", "动量队列（MoCo）"))),
        Param("epochs", "训练轮数", 100, "int", minimum=1, maximum=10000),
        Param("batch_size", "批大小", 256, "int", minimum=16, maximum=65536),
        Param("lr", "学习率", 1e-3, "float", minimum=1e-8, maximum=10.0),
        Param("min_lr", "最低学习率", 1e-5, "float", minimum=0.0, advanced=True),
        Param("warmup_epochs", "热身轮数", 5, "int", minimum=0, advanced=True),
        Param("weight_decay", "权重衰减", 1e-4, "float", minimum=0.0, advanced=True),
        Param("temperature", "温度", 0.1, "float", minimum=0.01, maximum=2.0, advanced=True),
        Param("proj_dim", "投影维度", 128, "int", minimum=16, maximum=2048, advanced=True),
        Param("queue_size", "队列长度", 8192, "int", minimum=256, maximum=262144, advanced=True,
              visible_when=when(method="moco")),
        Param("momentum", "动量", 0.99, "float", minimum=0.5, maximum=0.99999, advanced=True,
              visible_when=when(method="moco")),
        Param("normalization", "归一化", "scale", "choice", choices=METHODS,
              choice_labels=tuple(METHOD_LABELS.items())),
        Param("scale_factor", "缩放系数", 0.0, "float", minimum=0.0, advanced=True),
        Param("augment_flip", "随机翻转", True, "bool", advanced=True),
        Param("augment_rotate", "随机 90° 旋转", True, "bool", advanced=True),
        Param("spectral_mask_p", "光谱掩膜概率", 0.5, "float", minimum=0.0, maximum=1.0,
              advanced=True),
        Param("band_dropout_p", "波段丢弃概率", 0.3, "float", minimum=0.0, maximum=1.0,
              advanced=True),
        Param("noise_std", "高斯噪声标准差", 0.02, "float", minimum=0.0, advanced=True),
        Param("brightness", "亮度扰动幅度", 0.2, "float", minimum=0.0, maximum=0.9, advanced=True),
        Param("erase_p", "随机擦除概率", 0.2, "float", minimum=0.0, maximum=1.0, advanced=True),
        Param("knn_every", "kNN 评估间隔（轮）", 5, "int", minimum=1, advanced=True),
        Param("knn_k", "kNN 近邻数", 20, "int", minimum=1, maximum=200, advanced=True),
        Param("seed", "随机种子", 42, "int", advanced=True),
        Param("device", "计算设备", "auto", "choice", choices=devices.DEVICE_CHOICES,
              choice_labels=(("auto", "自动"), ("cuda", "CUDA 显卡"),
                             ("mps", "Apple 芯片 GPU"), ("cpu", "CPU"))),
        Param("memory_fraction", "数据驻留内存占比", 0.4, "float", minimum=0.05, maximum=0.9,
              advanced=True),
        Param("model_name", "模型名称", "", "str", optional=True),
    )


# =============================================================================
# 无标签采样
# =============================================================================

def sample_image(job: Job, image: str, mask: str | None, count: int, patch: int,
                 nodata_value, seed: int) -> Path:
    """从影像的有效像元里随机采 ``count`` 个块，写成 ``unlabeled.h5``。

    按 512 块分批读：每块里先算有效掩膜，再按各块有效像元数的比例分配名额，在块内
    随机取位置、从带 halo 的块数组里切 patch。整景只读一遍，与样本数无关。
    """
    from .predict import RoiMask, _gdal, block_grid, image_wavelengths, read_block, valid_mask
    import h5py

    out = job.path("unlabeled.h5")
    if out.is_file():
        with h5py.File(out, "r") as fh:
            if fh.attrs.get("complete") and int(fh.attrs.get("count", 0)) > 0:
                job.log("使用已采样的 %d 个无标签块" % int(fh.attrs["count"]))
                return out
    gdal, _ogr, _osr = _gdal()
    ds = gdal.Open(image)
    if ds is None:
        raise JobError("无法打开影像：%s" % image)
    rows, cols, bands = ds.RasterYSize, ds.RasterXSize, ds.RasterCount
    gt = ds.GetGeoTransform(can_return_null=True)
    proj = ds.GetProjection()
    nodata = nodata_value if nodata_value is not None else ds.GetRasterBand(1).GetNoDataValue()
    roi = RoiMask(mask, gt, proj) if mask else None
    grid = block_grid(rows, cols, SAMPLE_BLOCK)
    lead = center_index(patch)

    job.phase("sample", "统计有效像元")
    valid_counts = []
    for k, (i, j, h, w) in enumerate(grid):
        job.check_cancel()
        core = ds.ReadAsArray(j, i, w, h)
        core = core[np.newaxis] if core.ndim == 2 else core
        v = valid_mask(core, nodata)
        if roi is not None:
            v &= roi.window(j, i, w, h)
        valid_counts.append(int(v.sum()))
    total = sum(valid_counts)
    if total == 0:
        raise JobError("影像（或指定范围）内没有有效像元，无法采样")
    rng = np.random.default_rng(seed)
    quota = rng.multinomial(min(count, total), np.asarray(valid_counts) / total)

    job.phase("sample", "从影像中采样 %d 个块" % int(quota.sum()))
    dtype = None
    written = 0
    with h5py.File(out, "w") as fh:
        for k, (i, j, h, w) in enumerate(grid):
            job.check_cancel()
            if not quota[k]:
                continue
            raw = read_block(ds, i, j, h, w, patch)
            v = valid_mask(raw[:, lead:lead + h, lead:lead + w], nodata)
            if roi is not None:
                v &= roi.window(j, i, w, h)
            rr, cc = np.nonzero(v)
            pick = rng.choice(rr.size, size=min(int(quota[k]), rr.size), replace=False)
            batch = np.stack([raw[:, r:r + patch, c:c + patch] for r, c in zip(rr[pick], cc[pick])])
            centers = np.stack([rr[pick] + i, cc[pick] + j], axis=1).astype(np.int32)
            if dtype is None:
                dtype = batch.dtype
                n = int(quota.sum())
                pd = fh.create_dataset("patches", shape=(n, bands, patch, patch), dtype=dtype,
                                       chunks=(min(256, n), bands, patch, patch))
                ld = fh.create_dataset("labels", shape=(n,), dtype=np.int32)
                cd = fh.create_dataset("centers", shape=(n, 2), dtype=np.int32)
            pd[written:written + len(batch)] = batch
            ld[written:written + len(batch)] = 0
            cd[written:written + len(batch)] = centers
            written += len(batch)
            job.emit("progress", "", done=k + 1, total=len(grid), unit="block")
        a = fh.attrs
        a["count"] = written
        a["complete"] = True
        a["patch_size"] = patch
        a["source_image_path"] = image
        if gt is not None:
            a["source_geotransform"] = np.asarray(gt, dtype=np.float64)
        a["source_projection"] = proj
        wl = image_wavelengths(ds)
        if wl:
            a["wavelengths"] = np.asarray(wl, dtype=np.float64)
    job.log("已采样 %d 个无标签块（%d 波段，块大小 %d）" % (written, bands, patch))
    return out


# =============================================================================
# kNN 监控
# =============================================================================

@torch.no_grad()
def embed_source(model, source, normalizer, kind, batch, device) -> tuple:
    model.eval()
    feats, labels = [], []
    for xb, yb in source.batches(batch, shuffle=False):
        f = model.features(prepare(xb, normalizer, kind)).float()
        feats.append(F.normalize(f, dim=1))
        labels.append(yb)
    return torch.cat(feats), torch.cat(labels)


@torch.no_grad()
def knn_accuracy(bank: torch.Tensor, bank_y: torch.Tensor, query: torch.Tensor,
                 query_y: torch.Tensor, k: int, num_classes: int, t: float = 0.07) -> float:
    correct = 0
    k = min(k, bank.shape[0])
    for s in range(0, query.shape[0], 1024):
        sim = query[s:s + 1024] @ bank.T
        top, idx = sim.topk(k, dim=1)
        votes = torch.zeros(top.shape[0], num_classes, device=bank.device)
        votes.scatter_add_(1, bank_y[idx], (top / t).exp())
        correct += int((votes.argmax(1) == query_y[s:s + 1024]).sum())
    return correct / max(1, query.shape[0])


# =============================================================================
# 损失
# =============================================================================

def nt_xent(z1: torch.Tensor, z2: torch.Tensor, t: float) -> tuple:
    """SimCLR 的 NT-Xent：2N 个视图，每个以另一视图为正、其余 2N-2 个为负。"""
    z = F.normalize(torch.cat([z1, z2]), dim=1).float()
    n = z1.shape[0]
    sim = z @ z.T / t
    sim.fill_diagonal_(float("-inf"))
    target = torch.cat([torch.arange(n, 2 * n), torch.arange(0, n)]).to(z.device)
    loss = F.cross_entropy(sim, target)
    acc = (sim.argmax(1) == target).float().mean()
    return loss, acc


class MoCoState:
    def __init__(self, model: ContrastiveNet, dim: int, size: int, momentum: float,
                 device: torch.device) -> None:
        self.key = copy.deepcopy(model)
        for p in self.key.parameters():
            p.requires_grad = False
        self.m = momentum
        self.queue = F.normalize(torch.randn(size, dim, device=device), dim=1)
        self.ptr = 0

    @torch.no_grad()
    def update(self, model: nn.Module) -> None:
        for pk, pq in zip(self.key.parameters(), model.parameters()):
            pk.mul_(self.m).add_(pq.detach(), alpha=1 - self.m)
        for bk, bq in zip(self.key.buffers(), model.buffers()):
            bk.copy_(bq)

    @torch.no_grad()
    def enqueue(self, k: torch.Tensor) -> None:
        n, size = k.shape[0], self.queue.shape[0]
        idx = (torch.arange(n, device=k.device) + self.ptr) % size
        self.queue[idx] = k.float()
        self.ptr = (self.ptr + n) % size

    def loss(self, q: torch.Tensor, k: torch.Tensor, t: float) -> tuple:
        q = F.normalize(q, dim=1).float()
        k = F.normalize(k, dim=1).float()
        l_pos = (q * k).sum(1, keepdim=True)
        l_neg = q @ self.queue.clone().T          # 入队在反向传播之前，不能直接用原张量
        logits = torch.cat([l_pos, l_neg], dim=1) / t
        target = torch.zeros(q.shape[0], dtype=torch.long, device=q.device)
        loss = F.cross_entropy(logits, target)
        acc = (logits.argmax(1) == 0).float().mean()
        self.enqueue(k)
        return loss, acc


# =============================================================================
# 任务
# =============================================================================

def run(job: Job, p: dict) -> dict:
    torch.manual_seed(int(p["seed"]))
    generator = torch.Generator().manual_seed(int(p["seed"]))
    device = devices.resolve_device(p["device"])
    amp = devices.train_amp(device)
    job.log("计算设备：%s" % devices.describe(device))

    # ------------------------------------------------------------ 数据
    job.phase("data", "准备无标签数据")
    if p["source"] == "image":
        if not p.get("image"):
            raise JobError("请选择要采样的影像")
        h5 = sample_image(job, p["image"], p.get("mask"), int(p["samples"]),
                          int(p["patch_size"]), None, int(p["seed"]))
    else:
        if not p.get("dataset"):
            raise JobError("请选择样本集")
        h5, _test = resolve_dataset(p["dataset"])
    info = H5Info.read(h5)
    in_shape = (info.bands, info.patch, info.patch)
    idx = np.arange(info.count)
    rng = np.random.default_rng(int(p["seed"]))
    sample_idx = np.sort(rng.choice(idx, size=min(NORMALIZER_SAMPLE, idx.size), replace=False))
    sample = read_rows(h5, sample_idx, fields=("patches",))["patches"]
    dtype = compact_dtype(sample.dtype, float(np.abs(sample).max()) if sample.size else 0.0)
    normalizer = Normalizer.fit(p["normalization"], sample, float(p["scale_factor"]))
    budget = devices.memory_budget(device, float(p["memory_fraction"]))
    source, how = make_source(h5, idx, info, device, budget, dtype)
    job.log("无标签块 %d 个，%s。归一化：%s，系数 %g" % (len(source), how,
                                                METHOD_LABELS[normalizer.method], normalizer.scale))

    # 监控集：带标签样本集的训练部分作特征库、测试部分（没有就切 20%）作查询
    knn = None
    if p.get("eval_dataset"):
        etrain, etest = resolve_dataset(p["eval_dataset"])
        einfo = H5Info.read(etrain)
        if (einfo.bands, einfo.patch) != (info.bands, info.patch):
            job.warn("监控样本集（%d 波段，块大小 %d）与无标签数据（%d 波段，块大小 %d）不一致，不进行 kNN 评估"
                     % (einfo.bands, einfo.patch, info.bands, info.patch))
        else:
            if etest is not None:
                bank_src, _ = make_source(etrain, np.arange(einfo.count), einfo, device, budget, dtype)
                tinfo = H5Info.read(etest)
                query_src, _ = make_source(etest, np.arange(tinfo.count), tinfo, device, budget, dtype)
            else:
                meta = read_rows(etrain, fields=("labels", "centers", "sample_keys"))
                tr, va, _ = split_validation(meta["labels"], meta.get("centers"),
                                             meta.get("sample_keys"), 0.2, int(p["seed"]), einfo.patch)
                bank_src, _ = make_source(etrain, tr, einfo, device, budget, dtype)
                query_src, _ = make_source(etrain, np.sort(va), einfo, device, budget, dtype)
            knn = (bank_src, query_src, einfo.num_classes)
            job.log("kNN 评估：特征库 %d 块，查询 %d 块" % (len(bank_src), len(query_src)))

    # ------------------------------------------------------------ 模型
    job.phase("model", "构建模型")
    model = ContrastiveNet(p["backbone"], in_shape, int(p["proj_dim"])).to(device)
    job.log("%s · 特征 %d 维 · 投影 %d 维 · 参数 %.2f M · %s"
            % (p["backbone"], model.feature_dim, int(p["proj_dim"]), count_parameters(model) / 1e6,
               "MoCo" if p["method"] == "moco" else "SimCLR"))
    epochs = int(p["epochs"])
    batch = int(p["batch_size"])
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(p["lr"]),
                                  weight_decay=float(p["weight_decay"]))
    floor = float(p["min_lr"]) / float(p["lr"])
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer, lr_lambda(epochs, int(p["warmup_epochs"]), min(1.0, floor)))
    scaler = torch.amp.GradScaler("cuda") if amp else None
    moco = MoCoState(model, int(p["proj_dim"]), int(p["queue_size"]), float(p["momentum"]),
                     device) if p["method"] == "moco" else None
    augment = Augment(flip=bool(p["augment_flip"]), rotate=bool(p["augment_rotate"]),
                      spectral_mask_p=float(p["spectral_mask_p"]),
                      band_dropout_p=float(p["band_dropout_p"]), noise_std=float(p["noise_std"]),
                      brightness=float(p["brightness"]), erase_p=float(p["erase_p"]))
    t = float(p["temperature"])

    ckpt = job.path("checkpoints")
    ckpt.mkdir(exist_ok=True)
    start, best, best_epoch, history = 0, -1.0, 0, []
    if (ckpt / "last.pt").is_file():
        state = torch.load(ckpt / "last.pt", map_location="cpu", weights_only=True)
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        scheduler.load_state_dict(state["scheduler"])
        if moco is not None and state.get("key") is not None:
            moco.key.load_state_dict(state["key"])
            moco.queue = state["queue"].to(device)
            moco.ptr = int(state["ptr"])
        start, best, best_epoch = int(state["epoch"]), float(state["best"]), int(state["best_epoch"])
        history = list(state.get("history") or [])
        job.log("从第 %d 轮之后继续训练" % start)

    # ------------------------------------------------------------ 训练
    job.phase("train", "对比学习")
    cancelled = False
    try:
        for epoch in range(start + 1, epochs + 1):
            t0 = time.perf_counter()
            model.train()
            n_batches = max(1, len(source) // batch)
            loss_sum = acc_sum = 0.0
            seen = 0
            last_emit = time.monotonic()
            for b, (xb, _yb) in enumerate(source.batches(batch, shuffle=True, drop_last=True,
                                                         generator=generator), 1):
                job.check_cancel()
                x = normalizer.apply(xb.float(), band_axis=1)
                v1 = adapt(augment(x), model.input_kind)
                v2 = adapt(augment(x), model.input_kind)
                optimizer.zero_grad(set_to_none=True)
                with torch.autocast(device.type, dtype=torch.float16, enabled=amp):
                    if moco is None:
                        loss, acc = nt_xent(model(v1), model(v2), t)
                    else:
                        q = model(v1)
                        with torch.no_grad():
                            moco.update(model)
                            k = moco.key(v2)
                        loss, acc = moco.loss(q, k, t)
                if scaler is not None:
                    scaler.scale(loss).backward()
                    scaler.unscale_(optimizer)
                    nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    loss.backward()
                    nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                    optimizer.step()
                n = xb.shape[0]
                loss_sum += float(loss.detach()) * n
                acc_sum += float(acc) * n
                seen += n
                if time.monotonic() - last_emit >= PROGRESS_INTERVAL:
                    last_emit = time.monotonic()
                    job.emit("progress", "", done=b, total=n_batches, unit="batch", epoch=epoch,
                             epochs=epochs, loss=loss_sum / max(1, seen))
            if seen == 0:
                raise JobError("无标签块数量不足一个批次（批大小 %d），请增加采样数或减小批大小" % batch)
            lr_now = optimizer.param_groups[0]["lr"]
            scheduler.step()
            record = {"epoch": epoch, "lr": lr_now, "train_loss": loss_sum / seen,
                      "train_acc": acc_sum / seen}
            score = None
            if knn is not None and (epoch % int(p["knn_every"]) == 0 or epoch == epochs or epoch == 1):
                recalibrate_bn(model, source, normalizer, model.input_kind, batch, generator)
                bank, bank_y = embed_source(model, knn[0], normalizer, model.input_kind, batch * 2, device)
                qf, qy = embed_source(model, knn[1], normalizer, model.input_kind, batch * 2, device)
                score = knn_accuracy(bank, bank_y, qf, qy, int(p["knn_k"]), knn[2])
                record["knn_acc"] = score
            improved = (score is not None and score > best) or (knn is None)
            if improved:
                best = score if score is not None else best
                best_epoch = epoch
                torch.save(model.state_dict(), ckpt / "best.pt")
            record["best"] = improved and knn is not None
            record["seconds"] = round(time.perf_counter() - t0, 2)
            history.append(record)
            job.emit("epoch", "第 %d/%d 轮" % (epoch, epochs), epochs=epochs, **record)
            if improved and knn is not None:
                job.emit("checkpoint", "第 %d 轮成为最佳一轮（kNN 精度 %.1f%%）" % (epoch, score * 100),
                         epoch=epoch, score=score, metric="knn_acc")
            torch.save({"model": model.state_dict(), "optimizer": optimizer.state_dict(),
                        "scheduler": scheduler.state_dict(), "epoch": epoch, "best": best,
                        "best_epoch": best_epoch, "history": history,
                        "key": moco.key.state_dict() if moco else None,
                        "queue": moco.queue.cpu() if moco else None,
                        "ptr": moco.ptr if moco else 0}, ckpt / "last.pt")
            write_json_atomic(job.path("metrics.json"), {"history": history, "best_epoch": best_epoch})
    except Cancelled:
        cancelled = True
        if not (ckpt / "best.pt").is_file():
            raise
        job.log("已取消，使用第 %d 轮的骨干生成模型包" % best_epoch)

    # ------------------------------------------------------------ 收尾
    job.phase("package", "生成骨干模型包")
    model.load_state_dict(torch.load(ckpt / "best.pt", map_location="cpu", weights_only=True))
    name = (p.get("model_name") or "").strip() or "%s_%s_P%d_%s" % (
        p["backbone"], p["method"], info.patch, datetime.now().strftime("%Y%m%d-%H%M"))
    manifest = {
        "task": "backbone", "name": name, "backbone": p["backbone"], "input_kind": model.input_kind,
        "bands": info.bands, "patch_size": info.patch, "dtype": info.dtype,
        "wavelengths": info.wavelengths, "normalization": normalizer.to_json(),
        "feature_dim": model.feature_dim, "method": p["method"], "partial": cancelled,
        "dataset": {"train": str(h5), "count": info.count, "source_image": info.source_image},
        "metrics": {"best_epoch": best_epoch, "knn_acc": best if knn is not None else None,
                    "select_metric": "knn_acc" if knn is not None else "last"},
        "job": job.id,
    }
    enc = {k: v for k, v in model.state_dict().items() if k.startswith("encoder.")}
    dest = save_package(job.path("model", name + SUFFIX), manifest, enc, config=p,
                        metrics={"history": history})
    job.emit("artifact", "骨干模型包：%s" % dest.name, kind="model", path=str(dest))
    result = {"model": str(dest), "best_epoch": best_epoch,
              "knn_acc": best if knn is not None else None}
    if cancelled:
        raise Cancelled(result)
    return result
