# -*- coding: utf-8 -*-
"""分类精度：混淆矩阵、OA、AA、Kappa、宏 F1、逐类指标。只用 numpy。"""

from __future__ import annotations

import numpy as np


def confusion(y_true: np.ndarray, y_pred: np.ndarray, num_classes: int) -> np.ndarray:
    """行是真值、列是预测。"""
    y_true = np.asarray(y_true, dtype=np.int64)
    y_pred = np.asarray(y_pred, dtype=np.int64)
    idx = y_true * num_classes + y_pred
    return np.bincount(idx, minlength=num_classes * num_classes).reshape(
        num_classes, num_classes)


def summarize(cm: np.ndarray) -> dict:
    """由混淆矩阵算各项指标。没有样本的类别不参与 AA 与宏 F1。"""
    cm = np.asarray(cm, dtype=np.float64)
    total = cm.sum()
    if total == 0:
        return {"oa": 0.0, "aa": 0.0, "kappa": 0.0, "macro_f1": 0.0, "per_class": []}
    tp = np.diag(cm)
    support = cm.sum(axis=1)
    predicted = cm.sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        recall = np.where(support > 0, tp / support, 0.0)
        precision = np.where(predicted > 0, tp / predicted, 0.0)
        f1 = np.where(precision + recall > 0, 2 * precision * recall / (precision + recall), 0.0)
    present = support > 0
    oa = tp.sum() / total
    pe = (support * predicted).sum() / (total * total)
    kappa = (oa - pe) / (1 - pe) if pe < 1 else 1.0
    return {
        "oa": float(oa),
        "aa": float(recall[present].mean()) if present.any() else 0.0,
        "kappa": float(kappa),
        "macro_f1": float(f1[present].mean()) if present.any() else 0.0,
        "per_class": [{"label": i, "precision": float(precision[i]),
                       "recall": float(recall[i]), "f1": float(f1[i]),
                       "support": int(support[i])} for i in range(len(tp))],
    }
