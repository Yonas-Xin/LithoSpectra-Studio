# -*- coding: utf-8 -*-
"""真实 DataLoader 能跑多快：worker 数、IPC、冷缓存一起算进去。

``bench_patch_storage`` 里那一列"4 进程"是虚高的：主进程只看了 ``batch.shape``，
从没碰过张量里的字节——worker 把数据写进共享内存后，父进程没有触发缺页去映射
它，等于免掉了 IPC 的一半成本。真实训练里 ``batch.to(device)`` 会碰到每个字节。

这里把洞堵上：

* 主进程对每个批做一次全量求和，强制把共享内存页真正映射进来
* worker 数从 0 扫到 8，看多进程到底还能不能线性放大
* 每种配置各测暖缓存与冷缓存两轮；冷的那轮在计时前清掉该方案的页缓存
* 用 ``persistent_workers`` 先跑一轮预热，把 worker 启动开销排除在计时之外
  （macOS 是 spawn，每个 worker 都要重新 import torch，不排除的话全是噪声）

数据集直接复用 ``bench_patch_storage`` 写出来的那几份，配置也从那边读，避免两
个脚本的参数各说各话。
"""

# =============================================================================
# 配置
# =============================================================================

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bench_patch_storage as BPS       # 复用数据、后端与清缓存的实现

# 参与测试的方案，名字取自 BPS.build() 的键
FORMATS = ["per_file_bin", "packed_raw", "hdf5_raw", "hdf5_zstd", "lmdb_raw"]

WORKER_COUNTS = [0, 2, 4, 8]
BATCH_SIZE = 64
N_EPOCH_SAMPLES = 12000      # 一轮用多少样本，越多预取的水分越小
DO_COLD = True               # 每种配置额外测一轮冷缓存
PIN_MEMORY = False           # 有 CUDA 的机器上开 True 更贴近实际
KEEP_DATA = True             # 复用已写好的数据集，别重复写盘

# =============================================================================

import json
import time

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset


class PatchDataset(Dataset):
    """按索引取块。句柄必须在 worker 进程里打开。

    HDF5 与 LMDB 的句柄都不能跨进程共享：fork 出来的子进程继承同一个文件描述
    符和内部状态，轻则读到脏数据，重则把文件读坏。所以这里不在 ``__init__`` 里
    打开，而是等到第一次 ``__getitem__``（那时已经在 worker 里了），并且在
    ``__getstate__`` 里把句柄抹掉，保证 spawn 传过去的是空的。
    """

    def __init__(self, backend, order):
        self.backend = backend
        self.order = order
        self._handle = None

    def __len__(self):
        return len(self.order)

    def __getitem__(self, k):
        if self._handle is None:
            self._handle = self.backend.open()
        arr = self.backend.read(self._handle, int(self.order[k]))
        return torch.from_numpy(np.ascontiguousarray(arr))

    def __getstate__(self):
        d = self.__dict__.copy()
        d["_handle"] = None
        return d


def run_epoch(loader, touch: bool = True) -> tuple:
    """跑一轮，返回 (样本数, 秒)。``touch`` 强制碰到每个字节。"""
    t0 = time.perf_counter()
    n = 0
    for batch in loader:
        if touch:
            # 求和会把共享内存里的每一页都读进来，等价于训练里的 .to(device)
            int(batch.sum())
        n += batch.shape[0]
    return n, time.perf_counter() - t0


def bench(backend, order, num_workers: int, cold: bool) -> float:
    ds = PatchDataset(backend, order)
    kw = {}
    if num_workers > 0:
        kw = dict(persistent_workers=True, prefetch_factor=2)
    loader = DataLoader(ds, batch_size=BATCH_SIZE, num_workers=num_workers,
                        shuffle=False, pin_memory=PIN_MEMORY, **kw)
    run_epoch(loader, touch=False)      # 预热：拉起 worker、打开句柄
    if cold:
        BPS.evict_path(backend.path)
    n, dt = run_epoch(loader, touch=True)
    del loader
    return n / dt


def main():
    os.makedirs(BPS.OUT_DIR, exist_ok=True)
    src = BPS.make_source(os.path.join(BPS.OUT_DIR, "_source.dat"))
    backends = BPS.build(BPS.OUT_DIR)
    rng = np.random.default_rng(BPS.SEED)
    order = rng.permutation(BPS.N_SAMPLES)[:N_EPOCH_SAMPLES].tolist()

    print("每轮 %d 个样本，批大小 %d，单样本 %.0f KB"
          % (len(order), BATCH_SIZE, BPS.ITEM_BYTES / 1024))
    print("主进程对每个批求和，强制触碰全部字节\n")

    rows = []
    for name in FORMATS:
        b = backends[name]
        exists = os.path.exists(b.path)
        if not exists:
            print("[%s] 数据不存在，先写一份 ..." % name, flush=True)
            b.write(src)
        r = {"format": name}
        for nw in WORKER_COUNTS:
            r["warm_w%d" % nw] = bench(b, order, nw, cold=False)
            if DO_COLD:
                r["cold_w%d" % nw] = bench(b, order, nw, cold=True)
            print("  [%s] workers=%d  暖 %.0f 样本/秒%s"
                  % (name, nw, r["warm_w%d" % nw],
                     ("  冷 %.0f 样本/秒" % r["cold_w%d" % nw]) if DO_COLD else ""),
                  flush=True)
        rows.append(r)

    print("\n" + "=" * 92)
    print("单位：样本/秒（越大越好）")
    head = "%-14s" % "方案"
    for nw in WORKER_COUNTS:
        head += "%10s" % ("暖 w%d" % nw)
    for nw in WORKER_COUNTS:
        head += "%10s" % ("冷 w%d" % nw)
    print(head)
    print("-" * 92)
    for r in rows:
        line = "%-14s" % r["format"]
        for nw in WORKER_COUNTS:
            line += "%10.0f" % r["warm_w%d" % nw]
        for nw in WORKER_COUNTS:
            line += "%10.0f" % r.get("cold_w%d" % nw, float("nan"))
        print(line)

    out = os.path.join(BPS.OUT_DIR, "dataloader_results.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    print("\n结果已写入 %s" % out)


if __name__ == "__main__":
    main()
