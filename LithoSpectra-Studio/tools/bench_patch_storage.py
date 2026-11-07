# -*- coding: utf-8 -*-
"""裁剪样本块的存储方案基准测试。

现在 ``samplelib.clip`` 每个样本块写一个 ``.bin``：读得快，但几十万个小文件既
难管理、又难拷贝。本脚本在同一批合成的高光谱样本块上，横向测几种"集中存储"
方案的写入耗时、占盘大小与三种读取模式的速度，用来决定 clip 该改成哪一种。

参与比较的方案
    per_file_bin        现状：一个样本一个 .bin（基线）
    packed_raw          单个 .dat + 定长偏移，np.memmap 随机寻址
    hdf5_raw            HDF5，chunk = 1 个样本，不压缩
    hdf5_lzf            HDF5，chunk = 1 个样本，LZF 压缩
    hdf5_c64_lzf        HDF5，chunk = 64 个样本，LZF 压缩
    hdf5_zstd           HDF5，chunk = 1 个样本，Blosc2/Zstd（hdf5plugin）
    hdf5_lz4            HDF5，chunk = 1 个样本，Blosc2/LZ4（hdf5plugin）
    zarr_zstd_c1        Zarr v3，chunk = 1 个样本，Blosc/Zstd
    zarr_zstd_c64       Zarr v3，chunk = 64 个样本，Blosc/Zstd
    lmdb_raw            LMDB 键值库，值为原始字节

测的读取模式
    rand1               单样本随机读（DataLoader shuffle + num_workers=0）
    randbatch           随机索引成批读（真实训练的主要形态）
    seq                 顺序整轮读（预取、统计、转格式）
    workers             torch DataLoader 多进程读（检验句柄能否跨进程）

四种模式都只按索引取块，全程不把整个数据集读进内存。冷读（DO_COLD）在每次
计时前先读满页缓存把数据集挤出去，衡量数据量远大于内存时的表现。
"""

# =============================================================================
# 配置
# =============================================================================

import os

# --- 合成数据的规模（照着 config.CLIP_PATCH_SIZE 与常见高光谱波段数取） ---
N_SAMPLES = 12000            # 样本块个数
BANDS = 150                  # 波段数
PATCH = 17                   # 块边长
DTYPE = "uint16"             # 影像数据类型

# --- 输出目录（各方案的数据都写在这里，跑完可整个删掉） ---
OUT_DIR = os.path.expanduser(
    "/private/tmp/claude-501/-Users-xintao-Project-MyProjectCode-MYHomeGIS-AircasGXT"
    "/de07b3b8-379e-4a23-9104-2730c7515c04/scratchpad/patch_bench")

# --- 参与测试的方案，注释掉即跳过 ---
FORMATS = [
    "per_file_bin",
    "packed_raw",
    "hdf5_raw",
    "hdf5_lzf",
    "hdf5_c64_lzf",
    "hdf5_zstd",
    "hdf5_lz4",
    "zarr_raw_c1",
    "zarr_zstd_c1",
    "zarr_zstd_c64",
    "lmdb_raw",
]

# --- 读取测试的强度 ---
BATCH_SIZE = 64
N_RAND1 = 3000               # 单样本随机读的次数
N_RANDBATCH = 60             # 随机批读的批数
SEQ_LIMIT = 12000            # 顺序读最多读多少个样本
N_WORKER_SAMPLES = 6000      # 多进程读测试的样本数
NUM_WORKERS = 4

# --- 冷读 ---
DO_COLD = True               # 计时前清页缓存，测"数据集装不进内存"的情形
EVICT_GB = 26                # 仅退路使用：挤压文件大小，需 >= 物理内存
KEEP_DATA = False            # 跑完是否保留各方案的数据文件

SEED = 42

# =============================================================================

import json
import random
import shutil
import time
from typing import Dict, List

import numpy as np

DT = np.dtype(DTYPE)
SHAPE = (BANDS, PATCH, PATCH)
ITEM_BYTES = int(np.prod(SHAPE)) * DT.itemsize
RAW_MB = N_SAMPLES * ITEM_BYTES / 1024 ** 2


# =============================================================================
# 合成数据
# =============================================================================

def make_source(path: str) -> np.memmap:
    """造一批带光谱/空间相关性的样本块，压缩率才有参考价值。

    纯随机噪声压不动，会让所有压缩方案看起来一样差；真实高光谱的相邻波段高度
    相关，无损压缩通常能到 1.5~2.5 倍，这里的合成数据刻意造出这个量级。
    """
    if os.path.exists(path) and os.path.getsize(path) == N_SAMPLES * ITEM_BYTES:
        return np.memmap(path, dtype=DT, mode="r", shape=(N_SAMPLES,) + SHAPE)

    rng = np.random.default_rng(SEED)
    wl = np.linspace(0.4, 2.5, BANDS)
    # 8 条端元光谱：连续基线 + 几个吸收特征，反射率 ×10000
    ends = []
    for _ in range(8):
        base = 0.15 + 0.25 * np.exp(-((wl - rng.uniform(0.6, 2.2)) ** 2) / 0.6)
        for _ in range(3):
            c, d, w = rng.uniform(0.5, 2.4), rng.uniform(0.02, 0.12), rng.uniform(0.02, 0.08)
            base -= d * np.exp(-((wl - c) ** 2) / (2 * w ** 2))
        ends.append(np.clip(base, 0.01, 1.0))
    ends = np.asarray(ends)

    out = np.memmap(path, dtype=DT, mode="w+", shape=(N_SAMPLES,) + SHAPE)
    yy, xx = np.mgrid[0:PATCH, 0:PATCH].astype(np.float32)
    for i in range(N_SAMPLES):
        w = rng.dirichlet(np.full(8, 0.4))
        spec = (w @ ends).astype(np.float32)                    # (B,)
        # 块内做一个平缓的亮度梯度，模拟地物内部的空间连续性
        gain = 1.0 + 0.15 * (rng.normal() * (xx / PATCH) + rng.normal() * (yy / PATCH))
        cube = spec[:, None, None] * gain[None, :, :]
        cube = cube * (1.0 + rng.normal(0, 0.01, cube.shape).astype(np.float32))
        out[i] = np.clip(cube * 10000.0, 0, 65535).astype(DT)
    out.flush()
    return np.memmap(path, dtype=DT, mode="r", shape=(N_SAMPLES,) + SHAPE)


# =============================================================================
# 页缓存
# =============================================================================

_EVICT_PATH = os.path.join(OUT_DIR, "_evict.bin")
_MS_INVALIDATE = 0x2

try:
    import ctypes
    import ctypes.util
    import mmap as _mmap
    _LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    _LIBC.msync.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
except Exception:                       # pragma: no cover
    _LIBC = None


def _invalidate(path: str) -> bool:
    """把一个文件已缓存的页从统一缓冲区里丢掉。

    macOS 没有 ``drop_caches``；靠读一个比内存还大的文件去挤也不可靠——顺序流
    式读的页会被优先回收，反复读过的热页反而留着，实测挤完再读还是 14 GB/s。
    ``msync(MS_INVALIDATE)`` 直接作用于统一缓冲区，对之后走 mmap 还是走 read()
    的读者一律生效，而且是毫秒级的。

    只清数据页，清不掉目录项/inode 缓存——因此"一个样本一个文件"那种方案的
    冷读数字，比真实的冷启动还乐观一些。
    """
    if _LIBC is None:
        return False
    try:
        if os.path.getsize(path) == 0:
            return True
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        mm = _mmap.mmap(fd, 0)
        addr = ctypes.addressof(ctypes.c_char.from_buffer(mm))
        rc = _LIBC.msync(ctypes.c_void_p(addr), ctypes.c_size_t(len(mm)),
                         _MS_INVALIDATE)
        mm.close()
        return rc == 0
    except Exception:
        return False
    finally:
        os.close(fd)


def evict_path(path: str) -> None:
    """清掉一个方案全部数据文件的页缓存。"""
    if not DO_COLD:
        return
    ok = True
    if os.path.isfile(path):
        ok = _invalidate(path)
    else:
        for dirpath, _dirs, files in os.walk(path):
            for f in files:
                ok = _invalidate(os.path.join(dirpath, f)) and ok
    if not ok:
        evict_by_flooding()


def prepare_evict() -> None:
    """非 macOS 的退路才需要这个大文件。"""
    if _LIBC is not None:
        return
    need = int(EVICT_GB * 1024 ** 3)
    if os.path.exists(_EVICT_PATH) and os.path.getsize(_EVICT_PATH) >= need:
        return
    block = np.zeros(64 * 1024 ** 2 // 8, dtype=np.int64)
    with open(_EVICT_PATH, "wb") as fh:
        for _ in range(need // (64 * 1024 ** 2)):
            fh.write(block.tobytes())
        fh.flush()
        os.fsync(fh.fileno())


def evict_by_flooding() -> None:
    """退路：读一个比物理内存还大的文件，把页缓存挤掉。"""
    prepare_evict()
    if not os.path.exists(_EVICT_PATH):
        return
    buf = bytearray(64 * 1024 ** 2)
    with open(_EVICT_PATH, "rb", buffering=0) as fh:
        while fh.readinto(buf):
            pass


# =============================================================================
# 各方案的写入 / 读取器
# =============================================================================

class Backend:
    """一个存储方案。``reader()`` 返回按索引取块的可调用对象。"""

    name = ""
    supports_workers = True

    def __init__(self, root: str):
        self.path = os.path.join(root, self.name)

    def write(self, src) -> None:
        raise NotImplementedError

    def open(self):
        """在当前进程里打开句柄。多进程读时每个 worker 各调一次。"""
        raise NotImplementedError

    def read(self, handle, i: int) -> np.ndarray:
        raise NotImplementedError

    def close(self, handle) -> None:
        pass

    def size_mb(self) -> float:
        """实际占用的磁盘块，不是 apparent size。

        LMDB 会按 ``map_size`` 建一个稀疏文件，按 ``st_size`` 统计会把没写过的
        空洞也算进去，看上去比原始数据还大三倍。
        """
        def blocks(fp):
            try:
                return os.stat(fp).st_blocks * 512
            except OSError:
                return 0
        if os.path.isfile(self.path):
            return blocks(self.path) / 1024 ** 2
        total = 0
        for dirpath, _dirs, files in os.walk(self.path):
            for f in files:
                total += blocks(os.path.join(dirpath, f))
        return total / 1024 ** 2

    def n_files(self) -> int:
        if os.path.isfile(self.path):
            return 1
        return sum(len(files) for _d, _s, files in os.walk(self.path))


class PerFileBin(Backend):
    """现状：一个样本块一个 .bin，外加一份 raw_bip_meta.json。"""

    name = "per_file_bin"

    def write(self, src) -> None:
        os.makedirs(self.path, exist_ok=True)
        lines = []
        for i in range(N_SAMPLES):
            p = os.path.join(self.path, "img_label0_%d.bin" % i)
            np.asarray(src[i]).tofile(p)
            lines.append("%s 0" % p)
        with open(os.path.join(self.path, ".datasets.txt"), "w") as fh:
            fh.write("\n".join(lines))
        with open(os.path.join(self.path, "raw_bip_meta.json"), "w") as fh:
            json.dump({"layout": "BHW", "bands": BANDS, "height": PATCH,
                       "width": PATCH, "dtype": DTYPE, "count": N_SAMPLES}, fh)

    def open(self):
        return self.path

    def read(self, handle, i):
        p = os.path.join(handle, "img_label0_%d.bin" % i)
        return np.fromfile(p, dtype=DT).reshape(SHAPE)


class PackedRaw(Backend):
    """单个定长 .dat：第 i 块就在 i*ITEM_BYTES 处，memmap 直接寻址。"""

    name = "packed_raw"

    def write(self, src) -> None:
        os.makedirs(self.path, exist_ok=True)
        dat = os.path.join(self.path, "patches.dat")
        out = np.memmap(dat, dtype=DT, mode="w+", shape=(N_SAMPLES,) + SHAPE)
        step = 512
        for s in range(0, N_SAMPLES, step):
            out[s:s + step] = src[s:s + step]
        out.flush()
        del out
        with open(os.path.join(self.path, "index.json"), "w") as fh:
            json.dump({"layout": "NBHW", "count": N_SAMPLES, "bands": BANDS,
                       "height": PATCH, "width": PATCH, "dtype": DTYPE,
                       "labels_file": "labels.npy"}, fh)
        np.save(os.path.join(self.path, "labels.npy"),
                np.zeros(N_SAMPLES, dtype=np.int32))

    def open(self):
        return np.memmap(os.path.join(self.path, "patches.dat"), dtype=DT,
                         mode="r", shape=(N_SAMPLES,) + SHAPE)

    def read(self, handle, i):
        return np.array(handle[i])


class Hdf5(Backend):
    """HDF5。chunk 大小与压缩器决定它是偏随机读还是偏顺序读。"""

    def __init__(self, root, name, chunk_n=1, compression=None, plugin=None):
        self.name = name
        self.chunk_n = chunk_n
        self.compression = compression
        self.plugin = plugin            # hdf5plugin 的压缩器名，如 "zstd"/"lz4"
        super().__init__(root)
        self.path += ".h5"

    def _filter_kwargs(self) -> dict:
        """h5py 自带的压缩器只有 gzip/lzf/szip；zstd 之类要靠 hdf5plugin 注册。"""
        if self.plugin is None:
            return {"compression": self.compression}
        import hdf5plugin
        return dict(hdf5plugin.Blosc2(cname=self.plugin, clevel=3,
                                      filters=hdf5plugin.Blosc2.SHUFFLE))

    def write(self, src) -> None:
        import h5py
        if self.plugin is not None:
            import hdf5plugin                      # noqa: F401  注册过滤器
        with h5py.File(self.path, "w") as f:
            d = f.create_dataset("patches", shape=(N_SAMPLES,) + SHAPE, dtype=DT,
                                 chunks=(self.chunk_n,) + SHAPE,
                                 **self._filter_kwargs())
            step = max(self.chunk_n, 256)
            for s in range(0, N_SAMPLES, step):
                d[s:s + step] = src[s:s + step]
            f.create_dataset("labels", data=np.zeros(N_SAMPLES, dtype=np.int32))
            d.attrs["layout"] = "NBHW"

    def open(self):
        import h5py
        if self.plugin is not None:
            import hdf5plugin                      # noqa: F401
        # chunk 缓存给足，否则 chunk>1 时同一 chunk 会被反复解压
        f = h5py.File(self.path, "r", rdcc_nbytes=256 * 1024 ** 2, rdcc_nslots=1_000_003)
        return f, f["patches"]

    def read(self, handle, i):
        return handle[1][i]

    def close(self, handle):
        handle[0].close()


class Zarr(Backend):
    """Zarr v3 + Blosc/Zstd。chunk=1 便于随机读，chunk=64 压缩率更高。"""

    def __init__(self, root, name, chunk_n=1, clevel=3):
        self.name = name
        self.chunk_n = chunk_n
        self.clevel = clevel          # 0 表示不压缩
        super().__init__(root)
        self.path += ".zarr"

    def write(self, src) -> None:
        import zarr
        from zarr.codecs import BloscCodec
        arr = zarr.create_array(
            store=self.path, name="patches", shape=(N_SAMPLES,) + SHAPE, dtype=DT,
            chunks=(self.chunk_n,) + SHAPE, overwrite=True,
            compressors=(None if self.clevel == 0 else
                         BloscCodec(cname="zstd", clevel=self.clevel, shuffle="shuffle")))
        step = max(self.chunk_n, 256)
        for s in range(0, N_SAMPLES, step):
            arr[s:s + step] = src[s:s + step]

    def open(self):
        import zarr
        return zarr.open_array(self.path, path="patches", mode="r")

    def read(self, handle, i):
        return handle[i]


class Lmdb(Backend):
    """LMDB：mmap 的 B+ 树，值就是原始字节，读取零拷贝。"""

    name = "lmdb_raw"

    def write(self, src) -> None:
        import lmdb
        map_size = int(N_SAMPLES * ITEM_BYTES * 1.3) + 64 * 1024 ** 2
        env = lmdb.open(self.path, map_size=map_size, subdir=True,
                        writemap=True, map_async=True, meminit=False)
        with env.begin(write=True) as txn:
            for i in range(N_SAMPLES):
                txn.put(b"%08d" % i, np.asarray(src[i]).tobytes())
            txn.put(b"__len__", str(N_SAMPLES).encode())
            txn.put(b"__meta__", json.dumps(
                {"layout": "BHW", "bands": BANDS, "height": PATCH,
                 "width": PATCH, "dtype": DTYPE}).encode())
        env.sync()
        env.close()

    def open(self):
        import lmdb
        env = lmdb.open(self.path, readonly=True, lock=False, readahead=False,
                        meminit=False, max_readers=256)
        return env, env.begin(write=False, buffers=True)

    def read(self, handle, i):
        buf = handle[1].get(b"%08d" % i)
        # LMDB 给的是只读 buffer，复制一份才能给下游写/转 tensor
        return np.frombuffer(bytearray(buf), dtype=DT).reshape(SHAPE)

    def close(self, handle):
        handle[1].abort()
        handle[0].close()


def build(root: str) -> Dict[str, Backend]:
    return {
        "per_file_bin": PerFileBin(root),
        "packed_raw": PackedRaw(root),
        "hdf5_raw": Hdf5(root, "hdf5_raw", 1, None),
        "hdf5_lzf": Hdf5(root, "hdf5_lzf", 1, "lzf"),
        "hdf5_c64_lzf": Hdf5(root, "hdf5_c64_lzf", 64, "lzf"),
        "hdf5_zstd": Hdf5(root, "hdf5_zstd", 1, plugin="zstd"),
        "hdf5_lz4": Hdf5(root, "hdf5_lz4", 1, plugin="lz4"),
        "zarr_raw_c1": Zarr(root, "zarr_raw_c1", 1, 0),
        "zarr_zstd_c1": Zarr(root, "zarr_zstd_c1", 1, 3),
        "zarr_zstd_c64": Zarr(root, "zarr_zstd_c64", 64, 3),
        "lmdb_raw": Lmdb(root),
    }


# =============================================================================
# 多进程读
# =============================================================================

class _TorchDataset:
    """给 DataLoader 用。句柄必须在 worker 里打开——HDF5/LMDB 的句柄不能跨进程。"""

    def __init__(self, backend: Backend, order):
        self.backend = backend
        self.order = order
        self._handle = None

    def __len__(self):
        return len(self.order)

    def __getitem__(self, k):
        if self._handle is None:
            self._handle = self.backend.open()
        import torch
        return torch.from_numpy(
            np.ascontiguousarray(self.backend.read(self._handle, int(self.order[k]))))

    def __getstate__(self):
        d = self.__dict__.copy()
        d["_handle"] = None
        return d


def bench_workers(backend: Backend, order) -> float:
    import torch
    from torch.utils.data import DataLoader
    ds = _TorchDataset(backend, order)
    dl = DataLoader(ds, batch_size=BATCH_SIZE, num_workers=NUM_WORKERS,
                    shuffle=False, persistent_workers=True)
    for _ in dl:                        # 第一轮只用来拉起 worker 并打开句柄
        pass
    t0 = time.perf_counter()
    n = 0
    for b in dl:
        n += b.shape[0]
    dt = time.perf_counter() - t0
    del dl
    return n / dt


# =============================================================================
# 计时
# =============================================================================

def timed_reads(backend: Backend, indices, cold: bool) -> float:
    """按给定索引逐块读，返回 样本/秒。"""
    if cold:
        evict_path(backend.path)
    handle = backend.open()
    t0 = time.perf_counter()
    acc = 0
    for i in indices:
        a = backend.read(handle, int(i))
        acc += int(a[0, 0, 0])          # 触碰数据，防止惰性读被优化掉
    dt = time.perf_counter() - t0
    backend.close(handle)
    return len(indices) / dt


def run() -> List[dict]:
    os.makedirs(OUT_DIR, exist_ok=True)
    src_path = os.path.join(OUT_DIR, "_source.dat")
    print("合成 %d 个样本块 (%d,%d,%d) %s，共 %.0f MB ..."
          % (N_SAMPLES, BANDS, PATCH, PATCH, DTYPE, RAW_MB))
    t0 = time.perf_counter()
    src = make_source(src_path)
    print("  用时 %.1f s" % (time.perf_counter() - t0))

    if DO_COLD:
        print("清缓存方式：%s"
              % ("msync(MS_INVALIDATE)" if _LIBC is not None
                 else "读 %d GB 大文件挤压" % EVICT_GB))
        prepare_evict()

    rng = random.Random(SEED)
    rand1 = [rng.randrange(N_SAMPLES) for _ in range(N_RAND1)]
    randbatch = [rng.randrange(N_SAMPLES) for _ in range(N_RANDBATCH * BATCH_SIZE)]
    seq = list(range(min(SEQ_LIMIT, N_SAMPLES)))
    worker_order = list(range(N_SAMPLES))
    rng.shuffle(worker_order)
    worker_order = worker_order[:N_WORKER_SAMPLES]

    backends = build(OUT_DIR)
    rows = []
    for name in FORMATS:
        b = backends[name]
        print("\n[%s]" % name, flush=True)
        for p in (b.path, b.path + ".h5", b.path + ".zarr"):
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.isfile(p):
                os.remove(p)
        t0 = time.perf_counter()
        b.write(src)
        write_s = time.perf_counter() - t0
        row = {"format": name, "write_s": write_s,
               "write_MBps": RAW_MB / write_s,
               "size_MB": b.size_mb(), "files": b.n_files()}
        print("  写入 %.1f s (%.0f MB/s)，占盘 %.0f MB，%d 个文件"
              % (write_s, row["write_MBps"], row["size_MB"], row["files"]), flush=True)

        row["rand1_warm"] = timed_reads(b, rand1, cold=False)
        row["randbatch_warm"] = timed_reads(b, randbatch, cold=False)
        row["seq_warm"] = timed_reads(b, seq, cold=False)
        print("  暖读 样本/秒: 单点 %.0f  随机批 %.0f  顺序 %.0f"
              % (row["rand1_warm"], row["randbatch_warm"], row["seq_warm"]), flush=True)

        if DO_COLD:
            row["randbatch_cold"] = timed_reads(b, randbatch, cold=True)
            row["seq_cold"] = timed_reads(b, seq, cold=True)
            print("  冷读 样本/秒: 随机批 %.0f  顺序 %.0f"
                  % (row["randbatch_cold"], row["seq_cold"]), flush=True)

        try:
            row["workers"] = bench_workers(b, worker_order)
            print("  DataLoader %d 进程: %.0f 样本/秒" % (NUM_WORKERS, row["workers"]),
                  flush=True)
        except Exception as exc:
            row["workers"] = float("nan")
            row["workers_error"] = "%s: %s" % (type(exc).__name__, exc)
            print("  DataLoader %d 进程: 失败 —— %s" % (NUM_WORKERS, exc), flush=True)

        rows.append(row)

    return rows


def report(rows: List[dict]) -> None:
    cols = [("format", "方案", 14, "s"), ("write_s", "写入s", 8, ".1f"),
            ("size_MB", "占盘MB", 9, ".0f"), ("files", "文件数", 9, "d"),
            ("rand1_warm", "暖单点", 10, ".0f"), ("randbatch_warm", "暖随机批", 11, ".0f"),
            ("seq_warm", "暖顺序", 10, ".0f"), ("randbatch_cold", "冷随机批", 11, ".0f"),
            ("seq_cold", "冷顺序", 10, ".0f"), ("workers", "4进程", 10, ".0f")]
    head = "".join(("%%-%ds" % w) % t if k == "format" else ("%%%ds" % w) % t
                   for k, t, w, _f in cols)
    print("\n" + "=" * 100)
    print("单位：样本/秒（越大越好）")
    print(head)
    print("-" * 100)
    for r in rows:
        line = ""
        for k, _t, w, f in cols:
            v = r.get(k, float("nan"))
            if k == "format":
                line += ("%%-%ds" % w) % v
            elif isinstance(v, float) and np.isnan(v):
                line += ("%%%ds" % w) % "-"
            else:
                line += ("%%%d%s" % (w, f)) % v
        print(line)
    out = os.path.join(OUT_DIR, "results.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    print("\n结果已写入 %s" % out)


if __name__ == "__main__":
    rows = run()
    report(rows)
    if not KEEP_DATA:
        for name in FORMATS:
            b = build(OUT_DIR)[name]
            shutil.rmtree(b.path, ignore_errors=True)
            if os.path.isfile(b.path):
                os.remove(b.path)
