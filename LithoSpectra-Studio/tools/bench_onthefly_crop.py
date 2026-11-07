# -*- coding: utf-8 -*-
"""不落盘裁样本：训练时从整幅影像现切，与预裁样本块对照。

17×17 的块以逐像元为中心切出来，同一个像元最多会被 289 个块重复存一遍。一幅
2000×2000×150 的影像本身 1.1 GB，从中取 10 万个块却要 8.7 GB——预裁出来的数据
比源影像还大近一个数量级，这才是"数据量可能很大"的根源。

本脚本测的是另一条路：样本库只存中心位置，训练时按索引从整幅影像现切。占盘
等于影像本身，与样本数无关。代价是每取一个块要做一次窗口读，本脚本量的就是
这个代价够不够训练用。

比较的源影像布局
    bip_raw       原始 BIP（H,W,B）memmap，一个窗口是 17 段连续字节
    bsq_raw       原始 BSQ（B,H,W）memmap，一个窗口要跨 150 个波段面
    hdf5_c16      HDF5（B,H,W），chunk=(B,16,16)，不压缩
    hdf5_c16_zstd 同上 + Blosc2/Zstd
    gtiff_tiled   GDAL 平铺 GeoTIFF（32×32 瓦片，像素交叉），零转换直接读原图
"""

# =============================================================================
# 配置
# =============================================================================

import os

IMG_H = 2000                 # 影像行数
IMG_W = 2000                 # 影像列数
BANDS = 150                  # 波段数
PATCH = 17                   # 块边长
DTYPE = "uint16"

N_PATCHES = 4000             # 随机取多少个块来计时
BATCH_HINT = 64              # 仅用于换算"每秒能供多少批"

OUT_DIR = ("/private/tmp/claude-501/-Users-xintao-Project-MyProjectCode-MYHomeGIS"
           "-AircasGXT/de07b3b8-379e-4a23-9104-2730c7515c04/scratchpad/onthefly_bench")

FORMATS = ["bip_raw", "bsq_raw", "hdf5_c16", "hdf5_c16_zstd", "gtiff_tiled"]

DO_COLD = True
KEEP_DATA = False
SEED = 42

# =============================================================================

import ctypes
import ctypes.util
import mmap as _mmap
import shutil
import time

import numpy as np

DT = np.dtype(DTYPE)
HALF = PATCH // 2
IMG_MB = IMG_H * IMG_W * BANDS * DT.itemsize / 1024 ** 2
PATCH_KB = BANDS * PATCH * PATCH * DT.itemsize / 1024

_LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_LIBC.msync.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]


def invalidate(path: str) -> None:
    """丢掉文件已缓存的页，测冷读。做法与 bench_patch_storage 相同。"""
    if not DO_COLD:
        return
    files = [path] if os.path.isfile(path) else [
        os.path.join(d, f) for d, _s, fs in os.walk(path) for f in fs]
    for fp in files:
        try:
            if os.path.getsize(fp) == 0:
                continue
            fd = os.open(fp, os.O_RDWR)
        except OSError:
            continue
        try:
            mm = _mmap.mmap(fd, 0)
            addr = ctypes.addressof(ctypes.c_char.from_buffer(mm))
            _LIBC.msync(ctypes.c_void_p(addr), ctypes.c_size_t(len(mm)), 0x2)
            mm.close()
        except Exception:
            pass
        finally:
            os.close(fd)


# =============================================================================
# 合成影像
# =============================================================================

def make_cube(path: str) -> np.memmap:
    """一幅带光谱相关性的合成影像，按 (H,W,B) 存，压缩率才有参考价值。"""
    nbytes = IMG_H * IMG_W * BANDS * DT.itemsize
    if os.path.exists(path) and os.path.getsize(path) == nbytes:
        return np.memmap(path, dtype=DT, mode="r", shape=(IMG_H, IMG_W, BANDS))
    rng = np.random.default_rng(SEED)
    wl = np.linspace(0.4, 2.5, BANDS)
    ends = []
    for _ in range(8):
        base = 0.15 + 0.25 * np.exp(-((wl - rng.uniform(0.6, 2.2)) ** 2) / 0.6)
        for _ in range(3):
            c, d, w = rng.uniform(0.5, 2.4), rng.uniform(0.02, 0.12), rng.uniform(0.02, 0.08)
            base -= d * np.exp(-((wl - c) ** 2) / (2 * w ** 2))
        ends.append(np.clip(base, 0.01, 1.0))
    ends = np.asarray(ends, dtype=np.float32)

    out = np.memmap(path, dtype=DT, mode="w+", shape=(IMG_H, IMG_W, BANDS))
    for r0 in range(0, IMG_H, 100):
        r1 = min(r0 + 100, IMG_H)
        # 每行块给一片缓变的丰度场，模拟地物成片分布
        ab = rng.dirichlet(np.full(8, 0.4), size=(r1 - r0, IMG_W)).astype(np.float32)
        tile = ab @ ends
        tile *= (1.0 + rng.normal(0, 0.01, tile.shape).astype(np.float32))
        out[r0:r1] = np.clip(tile * 10000.0, 0, 65535).astype(DT)
    out.flush()
    return np.memmap(path, dtype=DT, mode="r", shape=(IMG_H, IMG_W, BANDS))


# =============================================================================
# 各布局
# =============================================================================

class Source:
    """一种源影像布局。``read`` 取以 (row,col) 为中心的 (B,PATCH,PATCH) 块。"""

    name = ""

    def __init__(self, root):
        self.path = os.path.join(root, self.name)

    def build(self, cube) -> None:
        raise NotImplementedError

    def open(self):
        raise NotImplementedError

    def read(self, h, row, col) -> np.ndarray:
        raise NotImplementedError

    def close(self, h):
        pass

    def size_mb(self) -> float:
        def blocks(fp):
            try:
                return os.stat(fp).st_blocks * 512
            except OSError:
                return 0
        if os.path.isfile(self.path):
            return blocks(self.path) / 1024 ** 2
        return sum(blocks(os.path.join(d, f))
                   for d, _s, fs in os.walk(self.path) for f in fs) / 1024 ** 2


class BipRaw(Source):
    """(H,W,B)：一个窗口是 PATCH 段连续内存，最贴合"取一小块所有波段"。"""

    name = "bip_raw.dat"

    def build(self, cube):
        out = np.memmap(self.path, dtype=DT, mode="w+", shape=(IMG_H, IMG_W, BANDS))
        for r0 in range(0, IMG_H, 200):
            out[r0:r0 + 200] = cube[r0:r0 + 200]
        out.flush()
        del out

    def open(self):
        return np.memmap(self.path, dtype=DT, mode="r", shape=(IMG_H, IMG_W, BANDS))

    def read(self, h, row, col):
        w = h[row - HALF:row + HALF + 1, col - HALF:col + HALF + 1, :]
        return np.ascontiguousarray(w.transpose(2, 0, 1))


class BsqRaw(Source):
    """(B,H,W)：与块文件的 BHW 布局一致，但一个窗口要跨 150 个波段面。"""

    name = "bsq_raw.dat"

    def build(self, cube):
        out = np.memmap(self.path, dtype=DT, mode="w+", shape=(BANDS, IMG_H, IMG_W))
        for b in range(BANDS):
            out[b] = cube[:, :, b]
        out.flush()
        del out

    def open(self):
        return np.memmap(self.path, dtype=DT, mode="r", shape=(BANDS, IMG_H, IMG_W))

    def read(self, h, row, col):
        return np.ascontiguousarray(
            h[:, row - HALF:row + HALF + 1, col - HALF:col + HALF + 1])


class Hdf5Cube(Source):
    def __init__(self, root, name, zstd=False):
        self.name = name
        self.zstd = zstd
        super().__init__(root)

    def build(self, cube):
        import h5py
        kw = {}
        if self.zstd:
            import hdf5plugin
            kw = dict(hdf5plugin.Blosc2(cname="zstd", clevel=3,
                                        filters=hdf5plugin.Blosc2.SHUFFLE))
        with h5py.File(self.path, "w") as f:
            d = f.create_dataset("image", shape=(BANDS, IMG_H, IMG_W), dtype=DT,
                                 chunks=(BANDS, 16, 16), **kw)
            # 按整行块写：chunk 的波段维是满的，逐波段写会让每个 chunk 被反复
            # 读改写，压缩时更是每写一个波段就重压一次整块
            for r0 in range(0, IMG_H, 128):
                r1 = min(r0 + 128, IMG_H)
                d[:, r0:r1, :] = np.ascontiguousarray(
                    np.asarray(cube[r0:r1]).transpose(2, 0, 1))

    def open(self):
        import h5py
        if self.zstd:
            import hdf5plugin                       # noqa: F401
        f = h5py.File(self.path, "r", rdcc_nbytes=512 * 1024 ** 2, rdcc_nslots=1_000_003)
        return f, f["image"]

    def read(self, h, row, col):
        return h[1][:, row - HALF:row + HALF + 1, col - HALF:col + HALF + 1]

    def close(self, h):
        h[0].close()


class GTiffTiled(Source):
    """平铺 GeoTIFF：源影像本来就是这个格式，连转换都省了。"""

    name = "tiled.tif"

    def build(self, cube):
        from osgeo import gdal
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(self.path, IMG_W, IMG_H, BANDS, gdal.GDT_UInt16,
                        options=["TILED=YES", "BLOCKXSIZE=32", "BLOCKYSIZE=32",
                                 "INTERLEAVE=PIXEL", "BIGTIFF=YES"])
        for b in range(BANDS):
            ds.GetRasterBand(b + 1).WriteArray(np.asarray(cube[:, :, b]))
        ds.FlushCache()
        ds = None

    def open(self):
        from osgeo import gdal
        return gdal.Open(self.path)

    def read(self, h, row, col):
        return h.ReadAsArray(col - HALF, row - HALF, PATCH, PATCH)

    def close(self, h):
        del h


def build_all(root):
    return {
        "bip_raw": BipRaw(root),
        "bsq_raw": BsqRaw(root),
        "hdf5_c16": Hdf5Cube(root, "hdf5_c16.h5", False),
        "hdf5_c16_zstd": Hdf5Cube(root, "hdf5_c16_zstd.h5", True),
        "gtiff_tiled": GTiffTiled(root),
    }


# =============================================================================
# 主流程
# =============================================================================

def timed(src: Source, centers, cold: bool) -> float:
    if cold:
        invalidate(src.path)
    h = src.open()
    t0 = time.perf_counter()
    acc = 0
    for row, col in centers:
        a = src.read(h, row, col)
        acc += int(a[0, 0, 0])
    dt = time.perf_counter() - t0
    src.close(h)
    return len(centers) / dt


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print("合成影像 %d×%d×%d %s，%.0f MB" % (IMG_H, IMG_W, BANDS, DTYPE, IMG_MB))
    cube = make_cube(os.path.join(OUT_DIR, "_cube.dat"))

    rng = np.random.default_rng(SEED)
    rows = rng.integers(HALF, IMG_H - HALF, N_PATCHES)
    cols = rng.integers(HALF, IMG_W - HALF, N_PATCHES)
    scattered = list(zip(rows.tolist(), cols.tolist()))
    # 真实样本是成片的：一个面里的中心点彼此相邻，局部性比全图随机好得多
    br, bc = int(rng.integers(200, IMG_H - 200)), int(rng.integers(200, IMG_W - 200))
    clustered = [(br + int(i // 100), bc + int(i % 100)) for i in range(N_PATCHES)]

    srcs = build_all(OUT_DIR)
    rows_out = []
    for name in FORMATS:
        s = srcs[name]
        print("\n[%s]" % name, flush=True)
        if os.path.isdir(s.path):
            shutil.rmtree(s.path, ignore_errors=True)
        elif os.path.isfile(s.path):
            os.remove(s.path)
        t0 = time.perf_counter()
        s.build(cube)
        build_s = time.perf_counter() - t0
        r = {"format": name, "build_s": build_s, "size_MB": s.size_mb()}
        print("  转换 %.1f s，占盘 %.0f MB" % (build_s, r["size_MB"]), flush=True)
        r["scatter_warm"] = timed(s, scattered, False)
        r["cluster_warm"] = timed(s, clustered, False)
        r["scatter_cold"] = timed(s, scattered, True)
        r["cluster_cold"] = timed(s, clustered, True)
        print("  暖读 块/秒: 全图随机 %.0f  成片 %.0f" % (r["scatter_warm"], r["cluster_warm"]))
        print("  冷读 块/秒: 全图随机 %.0f  成片 %.0f" % (r["scatter_cold"], r["cluster_cold"]),
              flush=True)
        rows_out.append(r)

    print("\n" + "=" * 88)
    print("单位：块/秒（越大越好）；一个块 %.0f KB，影像本身 %.0f MB" % (PATCH_KB, IMG_MB))
    print("%-16s%10s%10s%12s%12s%12s%12s"
          % ("布局", "转换s", "占盘MB", "暖·随机", "暖·成片", "冷·随机", "冷·成片"))
    print("-" * 88)
    for r in rows_out:
        print("%-16s%10.1f%10.0f%12.0f%12.0f%12.0f%12.0f"
              % (r["format"], r["build_s"], r["size_MB"], r["scatter_warm"],
                 r["cluster_warm"], r["scatter_cold"], r["cluster_cold"]))
    print("\n换算成批：%d 块/批" % BATCH_HINT)

    import json
    with open(os.path.join(OUT_DIR, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(rows_out, fh, ensure_ascii=False, indent=2)

    if not KEEP_DATA:
        for name in FORMATS:
            p = build_all(OUT_DIR)[name].path
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else None
            if os.path.isfile(p):
                os.remove(p)


if __name__ == "__main__":
    main()
