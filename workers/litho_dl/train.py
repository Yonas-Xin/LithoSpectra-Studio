# -*- coding: utf-8 -*-
"""监督分类训练。

任务目录里的成果::

    checkpoints/last.pt     每轮覆盖：模型、优化器、调度器、轮次、历史，可续训
    checkpoints/best.pt     选优指标最好的那一轮的模型权重
    metrics.json            逐轮指标
    eval/val_best.npz       最佳轮在验证集上的逐样本真值、预测、置信度、位置
    eval/test.npz           训练结束后最佳模型在测试集上的同样内容
    model/<名称>.lithomodel  模型包

选优默认看验证集宏 F1：类别不平衡时 OA 会被大类撑高，小类全错也看不出来。
每轮评估前重标定一次 BatchNorm 统计量，见 :func:`recalibrate_bn`。
同一个任务目录再跑一次，会从 ``last.pt`` 接着训。
"""

from __future__ import annotations

import copy
import math
import time
from datetime import datetime

import numpy as np
import torch
import torch.nn as nn

from . import devices, metrics
from .augment import Augment
from .backbones import ALIASES, BACKBONES, canonical
from .data import (H5Info, class_table_for, compact_dtype, make_source, read_rows,
                   resolve_dataset, split_validation)
from .jobs import Cancelled, Job, JobError, write_json_atomic
from .models import HEAD_LABELS, HEADS, Classifier, adapt, count_parameters
from .normalize import METHOD_LABELS, METHODS, Normalizer
from .package import (SUFFIX, encoder_state, load_weights, read_manifest,
                      save as save_package, with_colors)
from .params import Param, when

SELECT_METRICS = ("macro_f1", "oa", "kappa")
NORMALIZER_SAMPLE = 20000          # 拟合归一化最多看这么多个样本
PROGRESS_INTERVAL = 2.0            # 秒；轮内进度事件的间隔
BN_RECALIBRATION_BATCHES = 50      # 每轮评估前重标定 BN 最多用这么多个 batch


def params() -> tuple:
    names = tuple(BACKBONES)
    return (
        Param("dataset", "样本集", None, "path", path_mode="any", extensions=("h5",),
              help="样本裁剪（h5 格式）的输出目录，或其中的 train_dataset.h5"),
        Param("test_dataset", "测试集", "", "path", extensions=("h5",), optional=True,
              advanced=True, help="留空则使用样本集目录中的 test_dataset.h5（如有）"),
        Param("backbone", "骨干网络", "2D-CNN" if "2D-CNN" in names else names[0],
              "choice", choices=names, aliases=tuple(ALIASES.items())),
        Param("pretrained", "预训练骨干", "", "path", path_mode="directory",
              extensions=("lithomodel",), optional=True,
              help="对比学习骨干或其他分类模型的模型包，其骨干网络与波段数须与本次训练一致"),
        Param("transfer", "迁移方式", "lowlr", "choice", choices=("lowlr", "finetune", "freeze"),
              choice_labels=(("lowlr", "骨干小学习率微调（推荐）"), ("finetune", "整体微调"),
                             ("freeze", "冻结骨干，只训分类头")),
              help="仅在指定预训练骨干时生效"),
        Param("backbone_lr_ratio", "骨干学习率比例", 0.1, "float", minimum=0.0, maximum=1.0,
              advanced=True, visible_when=when(transfer="lowlr")),
        Param("head", "分类头", "auto", "choice", choices=HEADS,
              choice_labels=tuple(HEAD_LABELS.items()), advanced=True),
        Param("epochs", "训练轮数", 50, "int", minimum=1, maximum=10000),
        Param("batch_size", "批大小", 256, "int", minimum=2, maximum=65536),
        Param("lr", "学习率", 1e-3, "float", minimum=1e-8, maximum=10.0),
        Param("min_lr", "最低学习率", 1e-5, "float", minimum=0.0, advanced=True,
              help="热身结束后，学习率按余弦曲线降至此值"),
        Param("warmup_epochs", "热身轮数", 2, "int", minimum=0, advanced=True),
        Param("weight_decay", "权重衰减", 1e-4, "float", minimum=0.0, advanced=True),
        Param("optimizer", "优化器", "adamw", "choice", choices=("adamw", "adam", "sgd"),
              choice_labels=(("adamw", "AdamW"), ("adam", "Adam"), ("sgd", "SGD 动量")),
              advanced=True),
        Param("val_ratio", "验证集比例", 0.2, "float", minimum=0.0, maximum=0.5,
              help="从训练集中按样本整体划出，用于每轮选优。设为 0 时不划分，按训练集精度选优"),
        Param("seed", "随机种子", 42, "int", advanced=True),
        Param("normalization", "归一化", "scale", "choice", choices=METHODS,
              choice_labels=tuple(METHOD_LABELS.items())),
        Param("scale_factor", "缩放系数", 0.0, "float", minimum=0.0, advanced=True,
              help="设为 0 时根据数据自动确定，反射率 ×10000 存储的整型数据取 1e-4"),
        Param("augment_flip", "随机翻转", True, "bool"),
        Param("augment_rotate", "随机 90° 旋转", True, "bool"),
        Param("spectral_mask_p", "光谱掩膜概率", 0.0, "float", minimum=0.0, maximum=1.0,
              advanced=True),
        Param("band_dropout_p", "波段丢弃概率", 0.0, "float", minimum=0.0, maximum=1.0,
              advanced=True),
        Param("noise_std", "高斯噪声标准差", 0.0, "float", minimum=0.0, advanced=True,
              help="作用于归一化后的数据"),
        Param("brightness", "亮度扰动幅度", 0.0, "float", minimum=0.0, maximum=0.9,
              advanced=True),
        Param("class_weighting", "类别加权", "none", "choice", choices=("none", "balanced"),
              choice_labels=(("none", "不加权"), ("balanced", "按样本数反比加权")),
              help="各类样本数相差悬殊时建议按反比加权"),
        Param("label_smoothing", "标签平滑", 0.0, "float", minimum=0.0, maximum=0.5,
              advanced=True),
        Param("select_metric", "选优指标", "macro_f1", "choice", choices=SELECT_METRICS,
              choice_labels=(("macro_f1", "宏 F1"), ("oa", "总体精度 OA"),
                             ("kappa", "Kappa")), advanced=True),
        Param("early_stop", "早停轮数", 0, "int", minimum=0, advanced=True,
              help="选优指标连续若干轮无提升时提前停止，设为 0 时不提前停止"),
        Param("device", "计算设备", "auto", "choice", choices=devices.DEVICE_CHOICES,
              choice_labels=(("auto", "自动"), ("cuda", "CUDA 显卡"),
                             ("mps", "Apple 芯片 GPU"), ("cpu", "CPU"))),
        Param("memory_fraction", "数据驻留内存占比", 0.4, "float", minimum=0.05,
              maximum=0.9, advanced=True,
              help="样本集小于可用内存（CUDA 为空闲显存）的该比例时一次性载入"),
        Param("model_name", "模型名称", "", "str", optional=True,
              help="留空则按骨干网络、块大小与时间自动命名"),
    )


# =============================================================================
# 工具
# =============================================================================

def prepare(xb: torch.Tensor, normalizer: Normalizer, input_kind: str,
            augment: Augment | None = None) -> torch.Tensor:
    """原始整型块 → 浮点、归一化、（增强）、骨干要的形态。"""
    x = normalizer.apply(xb.float(), band_axis=1)
    if augment is not None:
        x = augment(x)
    return adapt(x, input_kind)


def lr_lambda(epochs: int, warmup: int, floor: float):
    def fn(epoch: int) -> float:
        if epoch < warmup:
            return (epoch + 1) / (warmup + 1)
        span = max(1, epochs - warmup)
        progress = min(1.0, (epoch - warmup) / span)
        return floor + (1 - floor) * 0.5 * (1 + math.cos(math.pi * progress))
    return fn


def build_optimizer(name: str, params, lr: float, wd: float):
    """``params`` 是参数迭代器，或带各自学习率的参数组列表。"""
    if name == "adamw":
        return torch.optim.AdamW(params, lr=lr, weight_decay=wd)
    if name == "adam":
        return torch.optim.Adam(params, lr=lr, weight_decay=wd)
    return torch.optim.SGD(params, lr=lr, momentum=0.9, weight_decay=wd, nesterov=True)


@torch.no_grad()
def predict_source(model: nn.Module, source, normalizer: Normalizer, input_kind: str,
                   batch_size: int, device: torch.device, criterion=None,
                   job: Job | None = None):
    """在一个数据源上推理，返回 (真值, 预测, 置信度, 平均损失)。"""
    model.eval()
    ys, ps, cs = [], [], []
    loss_sum, count = 0.0, 0
    for xb, yb in source.batches(batch_size, shuffle=False):
        if job is not None:
            job.check_cancel()
        logits = model(prepare(xb, normalizer, input_kind)).float()
        if criterion is not None:
            loss_sum += float(criterion(logits, yb)) * yb.shape[0]
        prob = torch.softmax(logits, dim=1)
        conf, pred = prob.max(dim=1)
        ys.append(yb.cpu())
        ps.append(pred.cpu())
        cs.append(conf.cpu())
        count += yb.shape[0]
    if not ys:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty, np.zeros(0, dtype=np.float32), 0.0
    return (torch.cat(ys).numpy(), torch.cat(ps).numpy(),
            torch.cat(cs).numpy().astype(np.float32), loss_sum / max(1, count))


@torch.no_grad()
def recalibrate_bn(model: nn.Module, source, normalizer: Normalizer, input_kind: str,
                   batch_size: int, generator: torch.Generator,
                   max_batches: int = BN_RECALIBRATION_BATCHES) -> None:
    """用当前权重在训练数据上重新累计 BatchNorm 的均值与方差。

    BN 的滑动均值以 0.1 的动量追赶权重，样本少、每轮只有几十个 batch、学习率
    又大时追不上：实测合成数据上同一份训练集，用 batch 统计量精度 1.00，用滑动
    统计量只有 0.68。评估前不求梯度地前向一遍（不做增强），按累计平均重算即可。
    """
    bns = [m for m in model.modules() if isinstance(m, nn.modules.batchnorm._BatchNorm)]
    if not bns:
        return
    saved = [m.momentum for m in bns]
    for m in bns:
        m.reset_running_stats()
        m.momentum = None                      # 累计平均
    model.train()
    for b, (xb, _yb) in enumerate(source.batches(batch_size, shuffle=True, drop_last=True,
                                                 generator=generator)):
        if b >= max_batches:
            break
        model(prepare(xb, normalizer, input_kind))
    for m, momentum in zip(bns, saved):
        m.momentum = momentum
    model.eval()


def benchmark(model: Classifier, in_shape: tuple, device: torch.device, batch: int,
              normalizer: Normalizer, train_step: bool) -> float:
    """量吞吐（样本/秒）。在模型的副本上量，不碰正式权重。"""
    probe = copy.deepcopy(model).to(device)
    bands, patch, _ = in_shape
    x = torch.rand((batch, bands, patch, patch), device=device)
    if train_step:
        probe.train()
        opt = torch.optim.SGD(probe.parameters(), lr=1e-6)
        y = torch.zeros(batch, dtype=torch.long, device=device)

        def step():
            opt.zero_grad(set_to_none=True)
            nn.functional.cross_entropy(probe(adapt(x, probe.input_kind)), y).backward()
            opt.step()
    else:
        probe.eval()
        dtype = devices.inference_dtype(device)
        probe.to(dtype)
        xi = adapt(x.to(dtype), probe.input_kind)

        def step():
            with torch.inference_mode():
                probe(xi)
    step()
    devices.synchronize(device)
    t0 = time.perf_counter()
    reps = 3
    for _ in range(reps):
        step()
    devices.synchronize(device)
    dt = max(1e-6, time.perf_counter() - t0)
    del probe
    devices.empty_cache(device)
    return batch * reps / dt


def _score(summary: dict, name: str) -> float:
    return float(summary.get(name, 0.0))


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
    job.phase("data", "读取样本集")
    train_path, test_path = resolve_dataset(p["dataset"], p.get("test_dataset"))
    info = H5Info.read(train_path)
    if not info.complete:
        job.warn("该样本集在裁剪时被中途取消，可能缺少部分类别")
    classes = with_colors(class_table_for(info))
    num_classes = len(classes)
    legend = [{"label": c["label"], "name": c["name"], "color": c["color"]} for c in classes]
    if num_classes < 2:
        raise JobError("样本集只有 %d 个类别，分类训练至少需要 2 个" % num_classes)
    in_shape = (info.bands, info.patch, info.patch)

    meta = read_rows(train_path, fields=("labels", "centers", "sample_keys"))
    labels = meta["labels"].astype(np.int64)
    tr_idx, va_idx, notes = split_validation(
        labels, meta.get("centers"), meta.get("sample_keys"), float(p["val_ratio"]),
        int(p["seed"]), info.patch)
    for note in notes:
        job.warn(note)
    va_idx = np.sort(va_idx)
    train_counts = np.bincount(labels[tr_idx], minlength=num_classes)
    missing = [classes[i]["name"] for i in range(num_classes) if train_counts[i] == 0]
    if missing:
        job.warn("以下类别在训练集中没有样本，模型无法学习：%s" % "、".join(missing))

    rng = np.random.default_rng(int(p["seed"]))
    sample_idx = np.sort(rng.choice(tr_idx, size=min(NORMALIZER_SAMPLE, tr_idx.size),
                                    replace=False))
    sample = read_rows(train_path, sample_idx, fields=("patches",))["patches"]
    dtype = compact_dtype(sample.dtype, float(np.abs(sample).max()) if sample.size else 0.0)
    normalizer = Normalizer.fit(p["normalization"], sample, float(p["scale_factor"]))
    pre = None
    if p.get("pretrained"):
        pre = read_manifest(p["pretrained"])
        if canonical(pre.get("backbone") or "") != p["backbone"]:
            raise JobError("预训练骨干为 %s，与所选骨干网络 %s 不一致" % (pre.get("backbone"), p["backbone"]))
        if int(pre.get("bands", 0)) != info.bands:
            raise JobError("预训练骨干的输入为 %s 个波段，样本集为 %d 个" % (pre.get("bands"), info.bands))
        if int(pre.get("patch_size", 0)) != info.patch and pre.get("input_kind") != "spectrum":
            job.warn("预训练骨干的块大小为 %s，样本集为 %d，骨干中与块大小相关的层可能不匹配"
                     % (pre.get("patch_size"), info.patch))
        # 沿用预训练时的归一化：骨干学到的特征空间是在那个尺度上的
        normalizer = Normalizer.from_json(pre["normalization"])
    job.log("归一化：%s，系数 %g" % (METHOD_LABELS[normalizer.method], normalizer.scale))

    budget = devices.memory_budget(device, float(p["memory_fraction"]))
    train_src, how = make_source(train_path, tr_idx, info, device, budget, dtype)
    job.log("训练集 %d 个样本，%s" % (len(train_src), how))
    val_src = None
    if va_idx.size:
        val_src, how = make_source(train_path, va_idx, info, device, budget, dtype)
        job.log("验证集 %d 个样本，%s" % (len(val_src), how))
    else:
        job.warn("未划分验证集，将按训练集精度选优，所选模型可能过拟合")
    va_meta = {k: v[va_idx] for k, v in meta.items() if k != "labels"}

    # ------------------------------------------------------------ 模型
    job.phase("model", "构建模型")
    model = Classifier(p["backbone"], in_shape, num_classes, head=p["head"]).to(device)
    job.log("%s · 分类头 %s · 参数 %.2f M" % (p["backbone"], model.head_kind,
                                         count_parameters(model) / 1e6))
    frozen = False
    if pre is not None:
        try:
            model.encoder.load_state_dict(encoder_state(load_weights(p["pretrained"])), strict=True)
        except RuntimeError as exc:
            raise JobError("预训练骨干的权重与当前模型不匹配：%s" % str(exc).splitlines()[0]) from exc
        model.to(device)
        frozen = p["transfer"] == "freeze"
        if frozen:
            for prm in model.encoder.parameters():
                prm.requires_grad = False
        job.log("载入预训练骨干：%s（%s）" % (pre.get("name"), {
            "lowlr": "骨干学习率 ×%g" % float(p["backbone_lr_ratio"]), "finetune": "整体微调",
            "freeze": "冻结骨干"}[p["transfer"]]))
    epochs = int(p["epochs"])
    lr = float(p["lr"])
    trainable = model.parameters()
    if pre is not None and p["transfer"] != "finetune":
        ratio = 0.0 if frozen else float(p["backbone_lr_ratio"])
        trainable = [{"params": list(model.decoder.parameters()), "lr": lr}]
        if ratio > 0:
            trainable.append({"params": list(model.encoder.parameters()), "lr": lr * ratio})
    optimizer = build_optimizer(p["optimizer"], trainable, lr, float(p["weight_decay"]))
    floor = float(p["min_lr"]) / float(p["lr"]) if float(p["lr"]) > 0 else 0.0
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer, lr_lambda(epochs, int(p["warmup_epochs"]), min(1.0, floor)))
    weight = None
    if p["class_weighting"] == "balanced":
        counts = train_counts.astype(np.float64)
        w = np.where(counts > 0, counts.sum() / (num_classes * np.maximum(counts, 1)), 0.0)
        weight = torch.tensor(w, dtype=torch.float32, device=device)
    criterion = nn.CrossEntropyLoss(weight=weight, label_smoothing=float(p["label_smoothing"]))
    eval_criterion = nn.CrossEntropyLoss()
    scaler = torch.amp.GradScaler("cuda") if amp else None
    augment = Augment(flip=bool(p["augment_flip"]), rotate=bool(p["augment_rotate"]),
                      spectral_mask_p=float(p["spectral_mask_p"]),
                      band_dropout_p=float(p["band_dropout_p"]),
                      noise_std=float(p["noise_std"]), brightness=float(p["brightness"]))
    augment = augment if augment.active() else None

    ckpt_dir = job.path("checkpoints")
    ckpt_dir.mkdir(exist_ok=True)
    job.path("eval").mkdir(exist_ok=True)
    start_epoch, best_score, best_epoch, history = 0, -1.0, 0, []
    last = ckpt_dir / "last.pt"
    if last.is_file():
        state = torch.load(last, map_location="cpu", weights_only=True)
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        scheduler.load_state_dict(state["scheduler"])
        if scaler is not None and state.get("scaler"):
            scaler.load_state_dict(state["scaler"])
        start_epoch = int(state["epoch"])
        best_score, best_epoch = float(state["best_score"]), int(state["best_epoch"])
        history = list(state.get("history") or [])
        job.log("从第 %d 轮之后继续训练" % start_epoch)

    # ------------------------------------------------------------ 预估
    batch_size = int(p["batch_size"])
    try:
        train_rate = benchmark(model, in_shape, device, min(batch_size, 256), normalizer, True)
        infer_rate = benchmark(model, in_shape, device, 2048, normalizer, False)
        epoch_seconds = len(train_src) / train_rate + (len(val_src) / infer_rate if val_src else 0)
        job.emit("estimate", "预计每轮 %.0f 秒，整景预测每百万像元 %.0f 秒"
                 % (epoch_seconds, 1e6 / infer_rate),
                 train_samples_per_s=round(train_rate, 1),
                 epoch_seconds=round(epoch_seconds, 1),
                 total_seconds=round(epoch_seconds * (epochs - start_epoch), 1),
                 predict_pixels_per_s=round(infer_rate, 1))
    except RuntimeError as exc:
        job.warn("测速失败，未给出耗时预估：%s" % str(exc).splitlines()[0])

    # ------------------------------------------------------------ 训练
    job.phase("train", "训练")
    select = p["select_metric"]
    patience = int(p["early_stop"])
    stale = 0
    cancelled = False
    epoch = start_epoch
    try:
        for epoch in range(start_epoch + 1, epochs + 1):
            t0 = time.perf_counter()
            model.train()
            if frozen:
                model.encoder.eval()            # 冻结的骨干连 BN 统计量也不动
            n_batches = max(1, math.ceil(len(train_src) / batch_size))
            loss_sum, correct, seen = 0.0, 0, 0
            tr_cm = np.zeros((num_classes, num_classes), dtype=np.int64)
            last_emit = time.monotonic()
            for b, (xb, yb) in enumerate(train_src.batches(batch_size, shuffle=True,
                                                           drop_last=True,
                                                           generator=generator), 1):
                job.check_cancel()
                x = prepare(xb, normalizer, model.input_kind, augment)
                optimizer.zero_grad(set_to_none=True)
                if amp:
                    with torch.autocast("cuda", dtype=torch.float16):
                        logits = model(x)
                        loss = criterion(logits.float(), yb)
                    scaler.scale(loss).backward()
                    scaler.unscale_(optimizer)
                    nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    logits = model(x)
                    loss = criterion(logits, yb)
                    loss.backward()
                    nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                    optimizer.step()
                pred = logits.detach().argmax(1)
                loss_sum += float(loss.detach()) * yb.shape[0]
                correct += int((pred == yb).sum())
                seen += yb.shape[0]
                if val_src is None:
                    tr_cm += metrics.confusion(yb.cpu().numpy(), pred.cpu().numpy(), num_classes)
                if time.monotonic() - last_emit >= PROGRESS_INTERVAL:
                    last_emit = time.monotonic()
                    job.emit("progress", "", done=b, total=n_batches, unit="batch",
                             epoch=epoch, epochs=epochs, loss=loss_sum / max(1, seen))
            lr_now = optimizer.param_groups[0]["lr"]
            scheduler.step()
            if not frozen:
                recalibrate_bn(model, train_src, normalizer, model.input_kind, batch_size,
                               generator)
            record = {"epoch": epoch, "lr": lr_now,
                      "train_loss": loss_sum / max(1, seen),
                      "train_acc": correct / max(1, seen)}
            if val_src is not None:
                y, pr, conf, vloss = predict_source(model, val_src, normalizer, model.input_kind,
                                                    batch_size * 2, device, eval_criterion, job)
                cm = metrics.confusion(y, pr, num_classes)
                summary = metrics.summarize(cm)
                record.update({"val_loss": vloss, "val_oa": summary["oa"],
                               "val_aa": summary["aa"], "val_kappa": summary["kappa"],
                               "val_macro_f1": summary["macro_f1"]})
            else:
                cm = tr_cm
                summary = metrics.summarize(cm)
                y = pr = conf = None
            score = _score(summary, select)
            improved = score > best_score
            record["seconds"] = round(time.perf_counter() - t0, 2)
            record["best"] = improved
            history.append(record)
            job.emit("epoch", "第 %d/%d 轮" % (epoch, epochs), epochs=epochs, **record)

            if improved:
                best_score, best_epoch, stale = score, epoch, 0
                torch.save(model.state_dict(), ckpt_dir / "best.pt")
                if y is not None:
                    np.savez(job.path("eval", "val_best.npz"), labels=y, preds=pr,
                             confidence=conf, index=va_idx, **va_meta)
                job.emit("evaluation", "验证集（第 %d 轮）" % epoch,
                         split="val" if val_src is not None else "train", epoch=epoch,
                         confusion=cm.tolist(), classes=legend, **summary)
                job.emit("checkpoint", "第 %d 轮成为最佳一轮" % epoch, epoch=epoch,
                         score=score, metric=select)
            else:
                stale += 1
            torch.save({"model": model.state_dict(), "optimizer": optimizer.state_dict(),
                        "scheduler": scheduler.state_dict(),
                        "scaler": scaler.state_dict() if scaler is not None else None,
                        "epoch": epoch, "best_score": best_score, "best_epoch": best_epoch,
                        "history": history}, ckpt_dir / "last.pt")
            write_json_atomic(job.path("metrics.json"),
                              {"history": history, "best_epoch": best_epoch,
                               "select_metric": select})
            if patience and stale >= patience:
                job.log("选优指标连续 %d 轮无提升，提前停止训练" % patience)
                break
    except Cancelled:
        cancelled = True
        if not (ckpt_dir / "best.pt").is_file():
            raise
        job.log("已取消，使用第 %d 轮的模型生成模型包" % best_epoch)

    # ------------------------------------------------------------ 收尾
    if not (ckpt_dir / "best.pt").is_file():
        raise JobError("训练未生成模型")
    model.load_state_dict(torch.load(ckpt_dir / "best.pt", map_location="cpu",
                                     weights_only=True))
    model.to(device)
    test_summary = None
    if test_path is not None and not cancelled:
        job.phase("test", "测试集评估")
        tinfo = H5Info.read(test_path)
        if (tinfo.bands, tinfo.patch) != (info.bands, info.patch):
            job.warn("测试集（%d 波段，块大小 %d）与训练集不一致，已跳过测试"
                     % (tinfo.bands, tinfo.patch))
        else:
            tidx = np.arange(tinfo.count)
            tsrc, _ = make_source(test_path, tidx, tinfo, device, budget, dtype)
            y, pr, conf, tloss = predict_source(model, tsrc, normalizer, model.input_kind,
                                                batch_size * 2, device, eval_criterion, job)
            tmeta = read_rows(test_path, fields=("centers", "sample_keys"))
            np.savez(job.path("eval", "test.npz"), labels=y, preds=pr, confidence=conf,
                     index=tidx, **tmeta)
            cm = metrics.confusion(y, pr, num_classes)
            test_summary = metrics.summarize(cm)
            test_summary["loss"] = tloss
            job.emit("evaluation", "测试集", split="test", epoch=best_epoch,
                     confusion=cm.tolist(), classes=legend, **test_summary)

    job.phase("package", "生成模型包")
    name = (p.get("model_name") or "").strip() or "%s_P%d_%s" % (
        p["backbone"], info.patch, datetime.now().strftime("%Y%m%d-%H%M"))
    best_record = next((r for r in history if r["epoch"] == best_epoch), {})
    manifest = {
        "task": "classifier", "name": name, "backbone": p["backbone"],
        "head": model.head_kind, "input_kind": model.input_kind,
        "bands": info.bands, "patch_size": info.patch, "dtype": info.dtype,
        "wavelengths": info.wavelengths, "normalization": normalizer.to_json(),
        "classes": classes, "partial": cancelled,
        "pretrained": {"path": str(p["pretrained"]), "name": pre.get("name"),
                       "method": pre.get("method"), "transfer": p["transfer"]} if pre else None,
        "dataset": {"train": str(train_path), "test": str(test_path or ""),
                    "count": info.count, "source_image": info.source_image,
                    "label_counts": info.label_counts},
        "metrics": {"select_metric": select, "best_epoch": best_epoch,
                    "best_score": best_score, "val": {k: v for k, v in best_record.items()
                                                      if k.startswith("val_")},
                    "test": {k: test_summary[k] for k in ("oa", "aa", "kappa", "macro_f1")}
                    if test_summary else None},
        "job": job.id,
    }
    dest = save_package(job.path("model", name + SUFFIX), manifest,
                        model.state_dict(), config=p,
                        metrics={"history": history, "test": test_summary})
    job.emit("artifact", "模型包：%s" % dest.name, kind="model", path=str(dest))
    result = {"model": str(dest), "best_epoch": best_epoch, "best_score": best_score,
              "test": test_summary}
    if cancelled:
        raise Cancelled(result)
    return result
