# -*- coding: utf-8 -*-
"""底图瓦片：编号方案、下载、磁盘缓存与拼接。

本模块不依赖 Qt，可以单独测试。下载在后台线程池里进行，完成后通过回调通知
调用方；界面层负责把回调转回 GUI 线程。

支持两套编号方案
----------------
``geodetic``
    天地图的 ``TILEMATRIXSET=c``（EPSG:4490，经纬度）。层级 z 有 2^z 列、
    2^(z-1) 行，每片跨 360/2^z 度。因为本身就是经纬度，拼出来的图不需要
    重投影就能贴到画布上——这是选它而不是 Web 墨卡托版本的原因。
``mercator``
    通行的 XYZ / Web 墨卡托（EPSG:3857），层级 z 有 2^z 行列。拼好之后必须
    重投影到 EPSG:4326 才能与样本对齐。
"""

import hashlib
import math
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

OrderedDictType = Dict[str, List[str]]

from .. import config

# Web 墨卡托的半个地球周长（米）
MERC_R = 20037508.342789244

TileId = Tuple[int, int, int]        # (z, x, y)


# =============================================================================
# 编号方案
# =============================================================================

@dataclass(frozen=True)
class TileScheme:
    """瓦片编号方案。"""

    kind: str                        # geodetic | mercator
    epsg: int
    tile_size: int = 256
    min_zoom: int = 1
    max_zoom: int = 18

    def grid(self, z: int) -> Tuple[int, int]:
        """该层级的列数与行数。"""
        if self.kind == "geodetic":
            return (2 ** z, max(1, 2 ** (z - 1)))
        return (2 ** z, 2 ** z)

    def span(self, z: int) -> float:
        """单片在原生坐标下的边长。两个方向相等。"""
        if self.kind == "geodetic":
            return 360.0 / (2 ** z)
        return 2.0 * MERC_R / (2 ** z)

    def origin(self) -> Tuple[float, float]:
        """左上角原点（原生坐标）。"""
        return (-180.0, 90.0) if self.kind == "geodetic" else (-MERC_R, MERC_R)

    def resolution(self, z: int) -> float:
        """原生坐标单位 / 像素。"""
        return self.span(z) / float(self.tile_size)

    def bounds(self) -> Tuple[float, float, float, float]:
        if self.kind == "geodetic":
            return (-180.0, -90.0, 180.0, 90.0)
        return (-MERC_R, -MERC_R, MERC_R, MERC_R)

    def tile_bounds(self, z: int, x: int, y: int
                    ) -> Tuple[float, float, float, float]:
        """单片的原生坐标范围 ``(minx, miny, maxx, maxy)``。"""
        ox, oy = self.origin()
        s = self.span(z)
        return (ox + x * s, oy - (y + 1) * s, ox + (x + 1) * s, oy - y * s)

    def zoom_for(self, resolution: float) -> int:
        """给定目标分辨率，选一个不比它粗的层级。"""
        if resolution <= 0:
            return self.max_zoom
        z = self.min_zoom
        while z < self.max_zoom and self.resolution(z) > resolution:
            z += 1
        return max(self.min_zoom, min(self.max_zoom, z))

    def tiles_in(self, z: int, bounds) -> Tuple[int, int, int, int]:
        """覆盖给定范围的瓦片编号区间 ``(x0, y0, x1, y1)``，闭区间。"""
        ox, oy = self.origin()
        s = self.span(z)
        cols, rows = self.grid(z)
        minx, miny, maxx, maxy = bounds
        x0 = int(math.floor((minx - ox) / s))
        x1 = int(math.floor((maxx - ox) / s))
        y0 = int(math.floor((oy - maxy) / s))
        y1 = int(math.floor((oy - miny) / s))
        x0 = max(0, min(cols - 1, x0))
        x1 = max(0, min(cols - 1, x1))
        y0 = max(0, min(rows - 1, y0))
        y1 = max(0, min(rows - 1, y1))
        return (x0, y0, x1, y1)


GEODETIC = TileScheme(kind="geodetic", epsg=4490, min_zoom=1, max_zoom=18)
MERCATOR = TileScheme(kind="mercator", epsg=3857, min_zoom=0, max_zoom=19)


# =============================================================================
# 底图定义
# =============================================================================

@dataclass
class TileSource:
    """一个可取片的底图。

    ``key_name`` 是这个底图要用的密钥的名字（如 ``"TIANDITU_KEY"``、
    ``"MAPBOX_TOKEN"``），留空表示该底图不需要密钥。这样接入新的服务商只要在
    配置里加一条，不必改代码。

    密钥本身不在代码里：由 :func:`samplelib.config.read_key` 从环境变量或使用者
    自己的 ``keys.json`` 取，见 config 的「底图服务密钥」一节。
    """

    name: str
    scheme: TileScheme
    url_template: str
    subdomains: Tuple[str, ...] = ()
    key_name: str = ""
    group: str = ""                  # 菜单里的分组名
    overlay: bool = False            # 注记层，叠在影像上，本身是透明的
    attribution: str = ""

    @property
    def needs_key(self) -> bool:
        return bool(self.key_name)

    def url(self, z: int, x: int, y: int, key: str = "") -> str:
        sub = self.subdomains[(x + y) % len(self.subdomains)] if self.subdomains else ""
        return self.url_template.format(s=sub, z=z, x=x, y=y, key=key)

    @property
    def cache_key(self) -> str:
        return hashlib.md5(self.name.encode("utf-8")).hexdigest()[:12]


def source_key(source: TileSource, overrides: Optional[Dict[str, str]] = None) -> str:
    """取某个底图的密钥。

    ``overrides`` 优先，其次是 config 里那两个模块级名字——它们在 import 时由
    :func:`samplelib.config.read_key` 填好，:func:`samplelib.config.save_key`
    会就地刷新。不在这里每次都读文件：一片瓦片一次磁盘读，平移时就是几百次。
    """
    if not source.key_name:
        return ""
    if overrides and source.key_name in overrides:
        return overrides[source.key_name] or ""
    return getattr(config, source.key_name, "") or ""


_TIANDITU_URL = ("https://t{s}.tianditu.gov.cn/{layer}_c/wmts"
                 "?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
                 "&LAYER={layer}&STYLE=default&TILEMATRIXSET=c&FORMAT=tiles"
                 "&TILEMATRIX={{z}}&TILEROW={{y}}&TILECOL={{x}}&tk={{key}}")


def build_sources() -> Dict[str, TileSource]:
    """按配置生成底图清单。"""
    out: Dict[str, TileSource] = {}
    for name, spec in config.BASEMAPS.items():
        common = dict(name=name, key_name=spec.get("key_name", ""),
                      group=spec.get("group", ""),
                      overlay=bool(spec.get("overlay")))
        if spec.get("kind") == "tianditu":
            src = TileSource(
                scheme=GEODETIC,
                url_template=_TIANDITU_URL.format(s="{s}", layer=spec["layer"]),
                subdomains=tuple("01234567"),
                attribution=spec.get("attribution", "© 天地图"), **common)
        else:
            scheme = MERCATOR
            if spec.get("max_zoom"):
                scheme = TileScheme(kind="mercator", epsg=3857, min_zoom=0,
                                    max_zoom=int(spec["max_zoom"]))
            src = TileSource(
                scheme=scheme, url_template=spec["url"],
                subdomains=tuple(spec.get("subdomains", ())),
                attribution=spec.get("attribution", ""), **common)
        out[name] = src
    return out


def groups() -> "OrderedDictType":
    """按服务商分组的底图清单，供菜单分节显示。"""
    from collections import OrderedDict
    out = OrderedDict()
    for name, src in build_sources().items():
        out.setdefault(src.group or "其它", []).append(name)
    return out


# =============================================================================
# 磁盘缓存
# =============================================================================

class TileCache:
    """瓦片磁盘缓存：``<缓存目录>/<底图>/<z>/<x>/<y>.tile``。

    超出 ``config.TILE_CACHE_LIMIT_MB`` 时按最后访问时间淘汰最旧的片。不在每次
    写入时都去量一遍目录——那要遍历上万个小文件，平移地图时每秒要发生几十次；
    改成累计写入量到一定程度才查一次，代价摊薄到可以忽略。
    """

    # 累计写入多少字节之后查一次总量
    CHECK_EVERY_BYTES = 8 * 1024 * 1024
    # 触发淘汰时一次清到上限的这个比例，免得贴着上限反复触发
    EVICT_TO = 0.8

    def __init__(self, root: str = "") -> None:
        self.root = root or config.TILE_CACHE_DIR
        self._written_since_check = 0
        self._lock = threading.Lock()

    def path(self, source: TileSource, z: int, x: int, y: int) -> str:
        return os.path.join(self.root, source.cache_key, str(z), str(x),
                            "%d.tile" % y)

    def get(self, source: TileSource, z: int, x: int, y: int) -> Optional[bytes]:
        p = self.path(source, z, x, y)
        try:
            with open(p, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        # 命中即续命：淘汰按 mtime 排序，常看的区域不该因为下载得早就被清掉
        try:
            os.utime(p, None)
        except OSError:
            pass
        return data

    def put(self, source: TileSource, z: int, x: int, y: int, data: bytes) -> None:
        p = self.path(source, z, x, y)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, p)
        except OSError:
            return
        with self._lock:
            self._written_since_check += len(data)
            due = self._written_since_check >= self.CHECK_EVERY_BYTES
            if due:
                self._written_since_check = 0
        if due:
            self.enforce_limit()

    def _entries(self) -> List[Tuple[float, int, str]]:
        """缓存里的全部文件：``(最后访问时间, 字节数, 路径)``。"""
        out = []
        for dirpath, _dirs, files in os.walk(self.root):
            for f in files:
                full = os.path.join(dirpath, f)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                out.append((st.st_mtime, st.st_size, full))
        return out

    def enforce_limit(self, limit_mb: float = 0.0) -> int:
        """把缓存压到上限以内，返回删掉的文件数。上限 <= 0 表示不限制。"""
        limit_mb = limit_mb or getattr(config, "TILE_CACHE_LIMIT_MB", 0)
        if limit_mb <= 0:
            return 0
        limit = int(limit_mb * 1024 * 1024)
        entries = self._entries()
        total = sum(size for _mtime, size, _p in entries)
        if total <= limit:
            return 0
        target = int(limit * self.EVICT_TO)
        removed = 0
        for _mtime, size, full in sorted(entries):      # 最旧的先删
            if total <= target:
                break
            try:
                os.remove(full)
            except OSError:
                continue
            total -= size
            removed += 1
        return removed

    def size_mb(self) -> float:
        return sum(size for _mtime, size, _p in self._entries()) / (1024.0 * 1024.0)

    def clear(self) -> None:
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)
        with self._lock:
            self._written_since_check = 0


# =============================================================================
# 下载
# =============================================================================

class TileFetcher:
    """后台取片。

    ``request`` 立刻返回：命中缓存时同步回调，否则排进线程池，下载完成后在
    工作线程里回调。同一片重复请求会被合并，避免平移时反复下载。
    """

    def __init__(self, key_overrides: Optional[Dict[str, str]] = None,
                 cache: Optional[TileCache] = None, workers: int = 0) -> None:
        # 每个底图各有各的密钥，按 TileSource.key_name 到 config 里取（那边的
        # 值来自环境变量或使用者的 keys.json）；这里的 overrides 只是给入口
        # 脚本和测试留的覆盖口。
        self.key_overrides: Dict[str, str] = dict(key_overrides or {})
        self.cache = cache or TileCache()
        self._pool = ThreadPoolExecutor(
            max_workers=workers or config.TILE_WORKERS,
            thread_name_prefix="tile")
        self._inflight: Dict[Tuple[str, int, int, int], bool] = {}
        self._lock = threading.Lock()
        # 失败记录带时间戳：过了冷却期就允许再试一次，避免一次限流把那片
        # 永久拉黑。
        self._failed: Dict[Tuple[str, int, int, int], Tuple[float, str]] = {}
        self.closed = False

    def shutdown(self) -> None:
        self.closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)

    def get_cached(self, source: TileSource, z: int, x: int, y: int
                   ) -> Optional[bytes]:
        return self.cache.get(source, z, x, y)

    def request(self, source: TileSource, z: int, x: int, y: int,
                done: Callable[[TileSource, int, int, int, Optional[bytes]], None]
                ) -> Optional[bytes]:
        """请求一片。命中缓存时直接返回数据，否则返回 None 并在稍后回调。"""
        data = self.cache.get(source, z, x, y)
        if data is not None:
            return data
        if self.closed:
            return None
        ident = (source.name, z, x, y)
        with self._lock:
            if ident in self._inflight:
                return None
            failed = self._failed.get(ident)
            if failed is not None:
                if time.time() - failed[0] < config.TILE_RETRY_AFTER:
                    return None
                self._failed.pop(ident, None)
            self._inflight[ident] = True
        self._pool.submit(self._work, source, z, x, y, done)
        return None

    def _work(self, source, z, x, y, done) -> None:
        ident = (source.name, z, x, y)
        data = None
        try:
            data = self._download(source, z, x, y)
            if data:
                self.cache.put(source, z, x, y, data)
        except Exception as exc:               # 网络问题不该弄崩界面
            with self._lock:
                self._failed[ident] = (time.time(), str(exc))
        finally:
            with self._lock:
                self._inflight.pop(ident, None)
        if not self.closed:
            try:
                done(source, z, x, y, data)
            except Exception:
                pass

    def key_for(self, source: TileSource) -> str:
        return source_key(source, self.key_overrides)

    def usable(self, source: TileSource) -> bool:
        """该底图当前是否有可用密钥。"""
        return not source.needs_key or bool(self.key_for(source))

    def _download(self, source: TileSource, z: int, x: int, y: int) -> bytes:
        url = source.url(z, x, y, self.key_for(source))
        req = urllib.request.Request(url, headers={
            "User-Agent": config.TILE_USER_AGENT,
            "Referer": "https://www.tianditu.gov.cn/",
        })
        with urllib.request.urlopen(req, timeout=config.TILE_TIMEOUT) as resp:
            if resp.status != 200:
                raise IOError("HTTP %s" % resp.status)
            return resp.read()

    def failures(self) -> Dict[Tuple[str, int, int, int], str]:
        with self._lock:
            return {k: v[1] for k, v in self._failed.items()}

    def retry_failed(self) -> int:
        """清掉失败记录，让下一帧重新尝试。返回清掉的条数。"""
        with self._lock:
            n = len(self._failed)
            self._failed.clear()
        return n

    def clear_failures(self) -> None:
        with self._lock:
            self._failed.clear()


# =============================================================================
# 拼接
# =============================================================================

def mosaic_bounds(scheme: TileScheme, z: int, rng: Tuple[int, int, int, int]
                  ) -> Tuple[float, float, float, float]:
    """拼图整体的原生坐标范围。"""
    x0, y0, x1, y1 = rng
    left, _, _, top = scheme.tile_bounds(z, x0, y0)
    _, bottom, right, _ = scheme.tile_bounds(z, x1, y1)
    return (left, bottom, right, top)


def plan(source: TileSource, bounds_native, px_width: int,
         max_tiles: int = 0) -> Tuple[int, Tuple[int, int, int, int]]:
    """给定视窗与目标像素宽，选层级并算出要取哪些片。

    先按分辨率选层级，再检查片数：超过上限就逐级降下来。高分屏上把窗口拉到
    3000 像素宽时，理想层级要取 80 多片，一次朝服务端发这么多请求会被限流，
    宁可底图糊一级。
    """
    max_tiles = max_tiles or getattr(config, "TILE_MAX_PER_VIEW", 48)
    width = max(1e-12, bounds_native[2] - bounds_native[0])
    z = source.scheme.zoom_for(width / max(1, px_width))
    rng = source.scheme.tiles_in(z, bounds_native)
    while z > source.scheme.min_zoom and tile_count(rng) > max_tiles:
        z -= 1
        rng = source.scheme.tiles_in(z, bounds_native)
    return z, rng


def tile_count(rng: Tuple[int, int, int, int]) -> int:
    x0, y0, x1, y1 = rng
    return (x1 - x0 + 1) * (y1 - y0 + 1)
