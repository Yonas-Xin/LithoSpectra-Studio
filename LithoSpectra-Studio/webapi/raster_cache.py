# -*- coding: utf-8 -*-
"""大影像的显示用替身：分块、带金字塔的 GeoTIFF。

画布按视窗出图，每帧都是对原图做一次 ``gdal.Warp``。原图没有金字塔时，缩到全幅
要把全分辨率整幅读一遍；PNG / JPG 不能随机读取，取下半幅也得从头解码。图一大，
每帧就要好几秒。

这里在图层加进来之后，于后台把**当前显示的那几个波段**另存成一份分块、带金字塔的
GeoTIFF，登记给图层（:meth:`RasterLayer.set_stand_in`）。之后缩小读金字塔、放大按块
随机读，与原图无关。原文件一个字节不动；替身放在数据目录的 ``data/rastercache`` 下，
按「路径 + 修改时间 + 大小 + 显示波段」命名，下次打开同一幅图直接复用，总量超出
上限时删最久没用的。
"""

import hashlib
import os
import threading
from typing import Any, Dict, Optional, Tuple

from osgeo import gdal

from samplelib import config
from samplelib.annotate import georef
from samplelib.annotate import layers as L

# =============================================================================
# 配置
# =============================================================================

#: 长边超过这么多像素才值得建替身；PNG / JPG 不能随机读取，门槛更低
MIN_SIDE = 3000
MIN_SIDE_UNTILED = 2000
#: 替身目录的总量上限（字节），超出时删最久没用的
CACHE_LIMIT = 8 * 1024 ** 3
#: 金字塔各级的缩小倍数一直取到长边不足这么多像素
OVERVIEW_MIN_SIDE = 256

_UNTILED_DRIVERS = {"PNG", "JPEG", "BMP", "GIF"}


def cache_dir() -> str:
    return os.path.join(config.app_data_dir(), "data", "rastercache")


def _source_driver(layer: L.RasterLayer) -> str:
    try:
        return gdal.OpenEx(layer.path).GetDriver().ShortName
    except Exception:
        return ""


def needs_stand_in(layer: L.RasterLayer) -> bool:
    """这幅图要不要建替身：够大，且原图不是现成的分块带金字塔。"""
    w, h = layer.ds.RasterXSize, layer.ds.RasterYSize
    side = max(w, h)
    driver = _source_driver(layer)
    if driver in _UNTILED_DRIVERS:
        return side > MIN_SIDE_UNTILED
    if side <= MIN_SIDE:
        return False
    band = layer.ds.GetRasterBand(1)
    bx, by = band.GetBlockSize()
    tiled = bx < w and by < h and bx <= 1024 and by <= 1024
    return not (tiled and band.GetOverviewCount() > 0)


def _key_name(layer: L.RasterLayer, key: Tuple[Any, ...]) -> str:
    try:
        st = os.stat(layer.path)
        stamp = "%s|%d|%d" % (os.path.abspath(layer.path), int(st.st_mtime), st.st_size)
    except OSError:
        stamp = os.path.abspath(layer.path)
    digest = hashlib.sha1(("%s|%r" % (stamp, key)).encode("utf-8")).hexdigest()[:20]
    base = os.path.splitext(os.path.basename(layer.path))[0][:40]
    return "%s-%s.tif" % (base, digest)


def _evict(keep: str) -> None:
    """总量超过上限时按最近使用时间删旧的，``keep`` 不删；顺带清掉没建完的残片。"""
    folder = cache_dir()
    try:
        for f in os.listdir(folder):
            if f.endswith(".part") and os.path.join(folder, f) != keep + ".part":
                _remove(os.path.join(folder, f))
    except OSError:
        pass
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".tif")]
    except OSError:
        return
    stats = []
    for f in files:
        try:
            s = os.stat(f)
            stats.append((s.st_atime, s.st_size, f))
        except OSError:
            pass
    total = sum(s for _, s, _ in stats)
    for _, size, f in sorted(stats):
        if total <= CACHE_LIMIT:
            break
        if os.path.abspath(f) == os.path.abspath(keep):
            continue
        try:
            os.remove(f)
            total -= size
        except OSError:
            pass


def build(layer: L.RasterLayer, key: Tuple[Any, ...], out: str, progress=None) -> bool:
    """把 ``key`` 对应的显示波段写成分块带金字塔的 GeoTIFF。成功返回 True。

    在后台线程里跑，自己重新打开原图：GDAL 的数据集不能两个线程同时用，图层手上
    那份正被画布出图占着。
    """
    ds, _info = georef.open_raster(layer.path)
    palette = key[0] == "index"
    bands = [1] if palette else [int(b) for b in key[1:]]
    tmp = out + ".part"

    def cb(fraction, _msg, _data):
        if progress is None:
            return 1
        return 1 if progress(fraction) else 0

    src = gdal.Translate("", ds, format="VRT", bandList=bands,
                         **({} if palette else L.display_mask_option(ds, bands)))
    if palette:
        # 与 RasterLayer._index_dataset 同理：摘掉调色板，warp 出来才是类别号本身
        band = src.GetRasterBand(1)
        try:
            band.SetColorTable(None)
            band.SetColorInterpretation(gdal.GCI_GrayIndex)
        except RuntimeError:
            pass
    opts = ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=LZW",
            "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"]
    if src.RasterCount > 1:
        opts.append("INTERLEAVE=PIXEL")
    # 前 70% 进度给复制，后 30% 给金字塔
    copy = gdal.Translate(tmp, src, format="GTiff", creationOptions=opts,
                          callback=lambda f, m, d: cb(0.7 * f, m, d))
    if copy is None:
        _remove(tmp)
        return False
    factors, side = [], max(copy.RasterXSize, copy.RasterYSize)
    f = 2
    while side / f >= OVERVIEW_MIN_SIDE:
        factors.append(f)
        f *= 2
    if factors:
        gdal.SetConfigOption("COMPRESS_OVERVIEW", "LZW")
        # 类别号是标称值，金字塔只能取最近邻，平均会造出不存在的类别
        ok = copy.BuildOverviews("NEAREST" if palette else "AVERAGE", factors,
                                 callback=lambda f, m, d: cb(0.7 + 0.3 * f, m, d))
        if ok != 0:
            copy = None
            _remove(tmp)
            return False
    copy.FlushCache()
    copy = None
    os.replace(tmp, out)
    return True


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class StandInManager:
    """排队给图层建替身：一个后台线程，一次建一个，建好就登记给图层。

    不走任务框架：那里同一时间只许一个任务，建金字塔要是占着它，导入、导出、
    加图层都得等，界面看着又像卡住了。进度挂在 ``progress`` 里，图层信息带出去，
    图层面板上显示「准备中」。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: Dict[Tuple[int, Tuple[Any, ...]], L.RasterLayer] = {}
        self._thread: Optional[threading.Thread] = None
        #: 图层 uid -> 0..1，正在建或排着队的
        self.progress: Dict[int, float] = {}
        self._cancel: set = set()

    def schedule(self, layer) -> None:
        """图层加进来、换了显示波段或显示方式之后调一次。用不着的直接返回。"""
        if not isinstance(layer, L.RasterLayer):
            return
        try:
            if not needs_stand_in(layer):
                return
        except Exception:
            return
        key = layer.display_key()
        out = os.path.join(cache_dir(), _key_name(layer, key))
        if os.path.isfile(out):
            try:
                os.utime(out)                       # 记一次「最近用过」
            except OSError:
                pass
            layer.set_stand_in(key, out)
            return
        with self._lock:
            self._queue[(layer.uid, key)] = layer
            self.progress.setdefault(layer.uid, 0.0)
            self._cancel.discard(layer.uid)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="raster-stand-in")
                self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """程序要退出了：排队的全撤掉，正在建的停下，等它从 GDAL 里出来。"""
        with self._lock:
            uids = {k[0] for k in self._queue} | set(self.progress)
            self._queue.clear()
            self._cancel.update(uids)
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def cancel(self, uid: int) -> None:
        """图层被移除：排队的撤掉，正在建的停下。"""
        with self._lock:
            for k in [k for k in self._queue if k[0] == uid]:
                del self._queue[k]
            self._cancel.add(uid)
            self.progress.pop(uid, None)

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._thread = None
                    return
                (uid, key), layer = next(iter(self._queue.items()))
                del self._queue[(uid, key)]
            out = os.path.join(cache_dir(), _key_name(layer, key))

            def progress(f: float, uid=uid) -> bool:
                with self._lock:
                    if uid in self._cancel:
                        return False
                    self.progress[uid] = f
                return True

            try:
                os.makedirs(cache_dir(), exist_ok=True)
                ok = build(layer, key, out, progress=progress)
            except Exception:
                ok = False
            with self._lock:
                cancelled = uid in self._cancel
                if not any(k[0] == uid for k in self._queue):
                    self.progress.pop(uid, None)
            if ok and not cancelled and layer.ds is not None:
                layer.set_stand_in(key, out)
                _evict(out)
