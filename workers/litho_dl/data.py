# -*- coding: utf-8 -*-
"""训练数据：读样本库裁剪出的 h5，划分验证集，按 batch 送上设备。

h5 的结构由样本库的裁剪模块写出（``samplelib/modules/clip/clip.py``）::

    patches      (N, 波段, P, P)   源影像原始数据类型
    labels       (N,)              int32，从 0 连续
    centers      (N, 2)            每块中心在源影像上的行列号
    sample_keys  (N,)              可选：每块来自样本库哪条样本（"point:12" 形式）
    attrs        bands / patch_size / dtype / count / complete / class_table /
                 source_image_path / source_geotransform / source_projection

送数据不起 DataLoader 子进程。数据集估算体积在预算以内时整体读入并驻留在设备
上（保持原始整型，batch 取出后才转浮点），否则由一个读线程按连续区段成段读入、
段间段内各打乱一次，经有界队列交给训练循环。两条路在 macOS 与 Windows 上行为
一致，不涉及 spawn 子进程重新 import torch、pickle 数据集对象这些事。
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np
import torch

from .jobs import JobError

TRAIN_FILE = "train_dataset.h5"
TEST_FILE = "test_dataset.h5"
READ_CHUNK = 4096           # 成段读取时每段的样本数


def _h5py():
    try:
        import h5py
    except ImportError as exc:
        raise JobError("当前环境未安装 h5py，无法读取样本集。请在该环境中执行 pip install h5py") from exc
    return h5py


# =============================================================================
# 数据集描述
# =============================================================================

@dataclass
class H5Info:
    path: str
    count: int
    bands: int
    patch: int
    dtype: str
    complete: bool
    class_table: list
    label_counts: list
    has_keys: bool
    source_image: str = ""
    geotransform: list = field(default_factory=list)
    projection: str = ""
    wavelengths: list = field(default_factory=list)

    @property
    def num_classes(self) -> int:
        labels = [int(c["label"]) for c in self.class_table] if self.class_table else []
        top = max(labels) + 1 if labels else 0
        return max(top, len(self.label_counts))

    def to_json(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def read(cls, path: str | Path) -> "H5Info":
        h5py = _h5py()
        path = Path(path)
        if not path.is_file():
            raise JobError("样本集文件不存在：%s" % path)
        try:
            fh = h5py.File(path, "r")
        except OSError as exc:
            raise JobError("无法打开样本集（文件损坏或不是 h5 格式）：%s\n%s" % (path, exc)) from exc
        with fh:
            if "patches" not in fh or "labels" not in fh:
                raise JobError("不是样本裁剪生成的样本集（缺少 patches 或 labels）：%s" % path)
            a = fh.attrs
            shape = fh["patches"].shape
            count = int(a.get("count", shape[0]))
            if count == 0:
                raise JobError("样本集为空：%s" % path)
            labels = fh["labels"][:count]
            table = a.get("class_table")
            table = json.loads(table) if table is not None else []
            wl = a.get("wavelengths")
            return cls(
                path=str(path), count=count, bands=int(shape[1]), patch=int(shape[2]),
                dtype=str(fh["patches"].dtype), complete=bool(a.get("complete", True)),
                class_table=table,
                label_counts=np.bincount(labels.astype(np.int64)).tolist(),
                has_keys="sample_keys" in fh,
                source_image=str(a.get("source_image_path", "")),
                geotransform=[float(v) for v in a.get("source_geotransform", [])],
                projection=str(a.get("source_projection", "")),
                wavelengths=[float(v) for v in wl] if wl is not None else [],
            )


def resolve_dataset(path: str | Path, test_path: str | Path | None = None
                    ) -> tuple[Path, Path | None]:
    """接受裁剪输出目录或单个 h5，返回 (训练集, 测试集或 None)。"""
    p = Path(path).expanduser()
    if p.is_dir():
        train = p / TRAIN_FILE
        if not train.is_file():
            raise JobError("目录中没有 %s：%s\n请选择样本裁剪（h5 格式）的输出目录"
                           % (TRAIN_FILE, p))
        auto_test = p / TEST_FILE
    elif p.is_file():
        train = p
        auto_test = p.with_name(TEST_FILE) if p.name == TRAIN_FILE else None
    else:
        raise JobError("样本集不存在：%s" % p)
    if test_path:
        test = Path(test_path).expanduser()
        if not test.is_file():
            raise JobError("测试集文件不存在：%s" % test)
    else:
        test = auto_test if auto_test is not None and auto_test.is_file() else None
    if test is not None:
        try:
            if H5Info.read(test).count == 0:
                test = None
        except JobError:
            test = None
    return train, test


def class_table_for(info: H5Info) -> list[dict]:
    """补齐类别表：h5 没带表、或表里缺某个标签时，给出"类别 N"。"""
    table = {int(c["label"]): dict(c) for c in info.class_table}
    out = []
    for label in range(info.num_classes):
        row = table.get(label, {"label": label, "name": "类别 %d" % label})
        row.setdefault("name", "类别 %d" % label)
        out.append(row)
    return out


# =============================================================================
# 读取
# =============================================================================

def compact_dtype(dtype: np.dtype, high: float) -> np.dtype:
    """驻留时用的数据类型：整型尽量放进 int16，浮点统一 float32。

    torch 对 uint16 的支持不全；反射率 ×10000 存成 uint16 的数据实际不超过
    32767，放进 int16 一字节不多占。
    """
    dtype = np.dtype(dtype)
    if dtype in (np.int8, np.uint8, np.int16):
        return dtype
    if np.issubdtype(dtype, np.integer):
        return np.dtype(np.int16) if high <= 32767 else np.dtype(np.int32)
    if dtype == np.float16:
        return dtype
    return np.dtype(np.float32)


def _decode(arr: np.ndarray) -> np.ndarray:
    """h5py 读出的变长字符串是 bytes，转成 str。"""
    if arr.dtype == object or arr.dtype.kind == "S":
        return np.array([v.decode("utf-8") if isinstance(v, bytes) else str(v) for v in arr],
                        dtype=str)
    return arr


def read_rows(path: str | Path, indices: np.ndarray | None = None,
              fields: tuple = ("patches", "labels")) -> dict[str, np.ndarray]:
    """按索引读若干行。索引先排序，再按连续区段整段读、在内存里挑选，因为 h5py 的
    逐点花式索引在几万个点上慢两个数量级。返回的行按 ``indices`` 原顺序。"""
    h5py = _h5py()
    with h5py.File(path, "r") as fh:
        count = int(fh.attrs.get("count", fh["labels"].shape[0]))
        out: dict[str, np.ndarray] = {}
        if indices is None:
            for name in fields:
                if name in fh:
                    out[name] = _decode(fh[name][:count])
            return out
        indices = np.asarray(indices, dtype=np.int64)
        order = np.argsort(indices, kind="stable")
        sorted_idx = indices[order]
        for name in fields:
            if name not in fh:
                continue
            ds = fh[name]
            parts = []
            start = 0
            while start < sorted_idx.size:
                lo = int(sorted_idx[start])
                hi = lo + READ_CHUNK
                stop = int(np.searchsorted(sorted_idx, hi, side="left"))
                slab = ds[lo:min(hi, count)]
                parts.append(slab[sorted_idx[start:stop] - lo])
                start = stop
            data = np.concatenate(parts) if parts else ds[:0]
            restored = np.empty_like(data)
            restored[order] = data
            out[name] = _decode(restored)
        return out


# =============================================================================
# 验证集划分
# =============================================================================

def split_validation(labels: np.ndarray, centers: np.ndarray | None,
                     keys: np.ndarray | None, ratio: float, seed: int,
                     patch: int) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """从训练集里按"组"切出验证集，返回 (训练索引, 验证索引, 提示)。

    组优先用样本库样本号：同一个面裁出的块整体归一侧，免得相邻像元一边训练
    一边验证、验证精度虚高。没有样本号时按中心位置落在哪个空间格子分组，格子
    边长取块大小的 4 倍（至少 32 像元）。每个类别各自按比例切，并保证训练侧
    至少留一组。
    """
    notes: list[str] = []
    n = labels.shape[0]
    if ratio <= 0:
        return np.arange(n), np.zeros(0, dtype=np.int64), notes
    if keys is not None and np.all(np.asarray(keys) != ""):
        groups = np.unique(np.asarray(keys), return_inverse=True)[1].astype(np.int64)
    elif centers is not None:
        cell = max(32, patch * 4)
        cells = np.asarray(centers, dtype=np.int64) // cell
        groups = cells[:, 0] * 1_000_003 + cells[:, 1]
    else:
        groups = np.arange(n)
    rng = np.random.default_rng(seed)
    val_mask = np.zeros(n, dtype=bool)
    single = []
    for label in np.unique(labels):
        members = np.nonzero(labels == label)[0]
        uniq, counts = np.unique(groups[members], return_counts=True)
        if uniq.size < 2:
            single.append(int(label))
            continue
        perm = rng.permutation(uniq.size)
        target = ratio * members.size
        taken, chosen = 0, []
        for k in perm[:-1]:                       # 至少留一组给训练
            if taken >= target:
                break
            chosen.append(uniq[k])
            taken += counts[k]
        val_mask[members[np.isin(groups[members], chosen)]] = True
    if single:
        notes.append("标签 %s 只有一个划分单元，全部留在训练集，验证集里没有这些类别"
                     % "、".join(map(str, single)))
    share = float(val_mask.mean()) if n else 0.0
    if share > ratio * 1.5 + 0.05:
        # 一组整体归一侧：样本面少、大小悬殊时，凑够比例的那一组可能很大
        notes.append("验证集实际占 %.0f%%（设定 %.0f%%）：同一划分单元的块整体归到一侧，划分单元少或"
                     "大小悬殊时比例会偏高；多画几个较小的样本面可以让划分更均匀"
                     % (share * 100, ratio * 100))
    return np.nonzero(~val_mask)[0], np.nonzero(val_mask)[0], notes


# =============================================================================
# 送数据
# =============================================================================

class ResidentSource:
    """整集驻留：数据放在设备上（或 CUDA 显存不够时放在锁页内存里）。"""

    def __init__(self, x: np.ndarray, y: np.ndarray, device: torch.device,
                 on_device: bool = True) -> None:
        self.device = device
        self.n = int(y.shape[0])
        tx = torch.from_numpy(np.ascontiguousarray(x))
        ty = torch.from_numpy(np.ascontiguousarray(y.astype(np.int64)))
        if on_device:
            self.x, self.y = tx.to(device), ty.to(device)
        else:
            if device.type == "cuda":
                tx, ty = tx.pin_memory(), ty.pin_memory()
            self.x, self.y = tx, ty
        self.on_device = on_device

    def __len__(self) -> int:
        return self.n

    def batches(self, batch_size: int, shuffle: bool, drop_last: bool = False,
                generator: torch.Generator | None = None
                ) -> Iterator[tuple[torch.Tensor, torch.Tensor]]:
        if shuffle:
            order = torch.randperm(self.n, generator=generator)
        else:
            order = torch.arange(self.n)
        if self.on_device:
            order = order.to(self.device)
        for start in range(0, self.n, batch_size):
            idx = order[start:start + batch_size]
            if drop_last and idx.numel() < batch_size and start > 0:
                break
            if idx.numel() < 2 and drop_last:
                break
            xb, yb = self.x[idx], self.y[idx]
            if not self.on_device:
                xb = xb.to(self.device, non_blocking=True)
                yb = yb.to(self.device, non_blocking=True)
            yield xb, yb


class StreamSource:
    """数据集放不进内存时：读线程成段读 h5，有界队列交给训练循环。"""

    def __init__(self, path: str | Path, indices: np.ndarray, device: torch.device,
                 dtype: np.dtype, chunk: int = READ_CHUNK * 4) -> None:
        self.path = str(path)
        self.indices = np.sort(np.asarray(indices, dtype=np.int64))
        self.device = device
        self.dtype = dtype
        self.chunk = chunk
        self.n = int(self.indices.size)

    def __len__(self) -> int:
        return self.n

    def _chunks(self, shuffle: bool, rng: np.random.Generator):
        pieces = [self.indices[i:i + self.chunk] for i in range(0, self.n, self.chunk)]
        if shuffle:
            rng.shuffle(pieces)
        return pieces

    def batches(self, batch_size: int, shuffle: bool, drop_last: bool = False,
                generator: torch.Generator | None = None):
        seed = int(torch.randint(0, 2 ** 31 - 1, (1,), generator=generator).item())
        rng = np.random.default_rng(seed)
        pieces = self._chunks(shuffle, rng)
        q: queue.Queue = queue.Queue(maxsize=2)
        stop = threading.Event()

        def reader():
            try:
                for piece in pieces:
                    if stop.is_set():
                        return
                    rows = read_rows(self.path, piece)
                    x, y = rows["patches"].astype(self.dtype, copy=False), rows["labels"]
                    if shuffle:
                        perm = rng.permutation(y.shape[0])
                        x, y = x[perm], y[perm]
                    q.put((x, y))
            except Exception as exc:              # 读线程的错误交给主线程抛
                q.put(exc)
                return
            q.put(None)

        thread = threading.Thread(target=reader, daemon=True, name="litho-dl-reader")
        thread.start()
        carry_x = carry_y = None
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                x, y = item
                if carry_x is not None:
                    x, y = np.concatenate([carry_x, x]), np.concatenate([carry_y, y])
                    carry_x = carry_y = None
                full = (x.shape[0] // batch_size) * batch_size
                for start in range(0, full, batch_size):
                    yield self._to_device(x[start:start + batch_size], y[start:start + batch_size])
                if full < x.shape[0]:
                    carry_x, carry_y = x[full:], y[full:]
            if carry_x is not None and not (drop_last and carry_x.shape[0] < batch_size):
                if carry_x.shape[0] >= 2 or not drop_last:
                    yield self._to_device(carry_x, carry_y)
        finally:
            stop.set()

    def _to_device(self, x: np.ndarray, y: np.ndarray):
        tx = torch.from_numpy(np.ascontiguousarray(x))
        ty = torch.from_numpy(np.ascontiguousarray(y.astype(np.int64)))
        if self.device.type == "cuda":
            tx = tx.pin_memory()
        return tx.to(self.device, non_blocking=True), ty.to(self.device, non_blocking=True)


def make_source(path: Path, indices: np.ndarray, info: H5Info, device: torch.device,
                budget_bytes: int, dtype: np.dtype):
    """按体积选驻留或流式。返回 (数据源, 描述文字)。"""
    dtype = np.dtype(dtype)
    item = info.bands * info.patch * info.patch * dtype.itemsize
    need = item * int(np.asarray(indices).size)
    if need <= budget_bytes:
        rows = read_rows(path, indices)
        x = rows["patches"]
        if np.issubdtype(dtype, np.integer) and x.size and \
                (x.max() > np.iinfo(dtype).max or x.min() < np.iinfo(dtype).min):
            dtype = np.dtype(np.int32)        # 抽样没见到的大值，放宽一级
        x = x.astype(dtype, copy=False)
        on_device = device.type != "cuda" or need <= budget_bytes // 2
        return (ResidentSource(x, rows["labels"], device, on_device=on_device),
                "一次性载入 %.2f GB" % (need / 2 ** 30))
    if np.issubdtype(dtype, np.integer) and np.dtype(info.dtype).itemsize > dtype.itemsize:
        dtype = np.dtype(np.int32)            # 流式读看不到全集的取值范围，不冒溢出的险
    return (StreamSource(path, indices, device, dtype),
            "分段读取（%.2f GB，超出可用预算 %.2f GB）" % (need / 2 ** 30, budget_bytes / 2 ** 30))
