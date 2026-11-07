# -*- coding: utf-8 -*-
"""标注画布的图层模型。

分成两类：

* **栅格类**（影像、配准图片、底图瓦片）实现 :meth:`RasterLayerBase.render_array`，
  返回一块与请求范围**严格对齐**的 RGBA 数组。因为对齐，画布只要把它铺进对应
  的矩形即可，不必再做任何几何换算。这个方法是纯 GDAL / numpy 运算，不碰 Qt，
  因此可以整个丢进工作线程。
* **矢量类**（参考矢量、样本图层）实现 :meth:`VectorLayerBase.visible_features`，
  按视窗范围裁出要画的要素，绘制由画布用 QPainter 完成。

拉伸统计只在图层载入时算一次并缓存。若每个视窗各算各的，平移时画面亮度会
随视窗内容跳变，看起来像在闪。
"""

import itertools
import math
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from osgeo import gdal, ogr, osr

from .. import config
from ..model import GEOM_POINT, GEOM_POLYGON, SampleKey, SampleRecord
from ..store import open_vector
from . import georef, tiles

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()

_counter = itertools.count(1)

# 载入矢量图层时，超过这个要素数就只保留前若干个并给出警告：一次性把几十万
# 个要素读进内存既慢又没意义，标注时也看不清。
VECTOR_FEATURE_LIMIT = 200000

# 按属性唯一值赋色时，空值单列一类的显示名
NULL_VALUE_LABEL = "（空值）"

# 计算拉伸统计时的采样边长
STATS_SAMPLE_SIZE = 512

# 形如 "Band 80"、"波段 12"、"B3" 的自动生成描述，不携带任何信息
_BAND_ORDINAL_RE = re.compile(r"^(band|波段|b)\s*[-_]?\s*\d+$", re.IGNORECASE)


# =============================================================================
# 视图
# =============================================================================

@dataclass(frozen=True)
class ViewSpec:
    """一次渲染请求：目标范围 + 目标像素尺寸 + 目标坐标系。

    ``bounds`` 的单位跟着 ``epsg`` 走——画布用哪个投影显示，影像就重采样到哪个
    投影的格网上。默认 EPSG:4326 时它就是经纬度范围，与从前一致。
    """

    bounds: Tuple[float, float, float, float]     # minx, miny, maxx, maxy
    width: int
    height: int
    epsg: int = 4326
    # 同一个视窗的经纬度外包。瓦片底图要靠它挑瓦片——瓦片编号是按经纬度或
    # 墨卡托定义的，跟画布现在用哪个投影显示没有关系。留空表示与 bounds 相同。
    lonlat: Optional[Tuple[float, float, float, float]] = None
    # 视窗外多画的一圈占视窗的比例（每侧）。画布拿它当平移余量；底图按它
    # 放大取片上限，免得多画的这一圈把层级压低一级。
    margin: float = 0.0
    # 比例尺 / 投影 / 设备像素比的指纹。视窗只是平移、这些没变时，旧图挪个
    # 位置还能用——判断"能不能只贴不重画"靠它，不靠含着范围的 signature。
    scale_key: Tuple = ()

    def signature(self) -> Tuple:
        return (round(self.bounds[0], 6), round(self.bounds[1], 6),
                round(self.bounds[2], 6), round(self.bounds[3], 6),
                self.width, self.height, self.epsg)

    @property
    def valid(self) -> bool:
        return (self.width > 0 and self.height > 0
                and self.bounds[2] > self.bounds[0]
                and self.bounds[3] > self.bounds[1])

    @property
    def lonlat_bounds(self) -> Tuple[float, float, float, float]:
        return self.lonlat if self.lonlat is not None else self.bounds


def intersects(a, b) -> bool:
    """两个 ``(minx, miny, maxx, maxy)`` 是否相交。"""
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


# =============================================================================
# 基类
# =============================================================================

class Layer:
    """图层基类。"""

    kind = "layer"

    def __init__(self, name: str, opacity: float = 1.0) -> None:
        self.uid = next(_counter)
        self.name = name
        self.visible = True
        self.opacity = float(opacity)
        self.error = ""
        self.info_lines: List[str] = []

    @property
    def is_raster(self) -> bool:
        return isinstance(self, RasterLayerBase)

    @property
    def is_shown(self) -> bool:
        """画布上看不看得见这一层：勾着、且没有被调到完全透明。

        画、点选、框选、识别要素都按这一条判，免得出现"看不见却点得中"。
        """
        return self.visible and self.opacity > 0.004

    def extent(self) -> Optional[Tuple[float, float, float, float]]:
        return None

    def describe(self) -> str:
        lines = ["%s（%s）" % (self.name, self.kind_cn)]
        box = self.extent()
        if box:
            lines.append("范围：%.5f, %.5f ~ %.5f, %.5f" % box)
        lines.extend(self.info_lines)
        if self.error:
            lines.append("错误：" + self.error)
        return "\n".join(lines)

    @property
    def kind_cn(self) -> str:
        return {"raster": "影像", "image": "配准图片", "vector": "矢量",
                "samples": "样本", "basemap": "底图"}.get(self.kind, self.kind)

    def close(self) -> None:
        pass


class RasterLayerBase(Layer):
    """栅格类图层：能按任意视窗产出对齐的 RGBA 数组。

    ``render_array`` 会在工作线程里被调用。**GDAL 的 Dataset 不是线程安全的**：
    快速缩放时防抖定时器比一次 warp 还快，旧任务尚未结束新任务就已排入，两个
    线程同时对同一个 Dataset 调 ``Warp`` 会直接段错误。因此每个图层自带一把
    渲染锁，把自己的重采样串行化——图层之间仍然并行。
    """

    def __init__(self, name: str, opacity: float = 1.0) -> None:
        super().__init__(name, opacity)
        self._render_lock = threading.Lock()

    def render_array(self, view: ViewSpec) -> Optional[np.ndarray]:
        raise NotImplementedError


class VectorLayerBase(Layer):
    """矢量类图层：能按视窗裁出要画的要素。"""

    def visible_features(self, bounds) -> Iterable[Any]:
        raise NotImplementedError


# =============================================================================
# 影像图层
# =============================================================================

def parse_wavelength_file(path: str, count: int = 0) -> List[float]:
    """从 ENVI 头或传感器波长表里解析 ``wavelength = {...}``。

    ``count`` 非零时要求波段数吻合，不吻合返回空表——拿一份 166 波段的表去
    套 GF5 的 330 波段影像只会得到错的波长，宁可什么都不显示。单位是微米时
    自动换算成纳米。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError:
        return []
    m = re.search(r"wavelength\s*=\s*\{(.*?)\}", text, re.DOTALL | re.IGNORECASE)
    if not m:
        return []
    try:
        values = [float(v) for v in m.group(1).replace("\n", " ").split(",")
                  if v.strip()]
    except ValueError:
        return []
    if count and len(values) != count:
        return []
    unit = re.search(r"wavelength\s+units?\s*=\s*(\S+)", text, re.IGNORECASE)
    micro = unit and unit.group(1).strip().lower().startswith(("micro", "um", "µm"))
    # 没写单位时按数值判断：可见-短波红外的波长用纳米在几百到几千，用微米则
    # 在 0.x 到几。
    if not unit and values and max(values) < 100:
        micro = True
    return [v * 1000.0 for v in values] if micro else values


def _read_envi_wavelengths(path: str, count: int) -> List[float]:
    """从影像自带的 ENVI 头文件读波长表。读不到时返回空表。"""
    stem = os.path.splitext(path)[0]
    for cand in (stem + ".hdr", path + ".hdr", stem + ".HDR", path + ".HDR"):
        if os.path.isfile(cand):
            values = parse_wavelength_file(cand, count)
            if values:
                return values
    return []


def wavelength_presets(count: int) -> Dict[str, str]:
    """在配置的目录里找波段数吻合的传感器波长表。

    使用者的高光谱影像常常是处理链上游生成的，头文件里没有 wavelength——
    这时用传感器预置表补上，波段选择器才有意义。
    """
    out: Dict[str, str] = {}
    for folder in getattr(config, "WAVELENGTH_DIRS", ()):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith((".txt", ".hdr")):
                continue
            path = os.path.join(folder, name)
            if parse_wavelength_file(path, count):
                out.setdefault(os.path.splitext(name)[0], path)
    return out


def _band_stats(ds: gdal.Dataset, band: int,
                percent: Tuple[float, float]) -> Tuple[float, float]:
    """在整幅的降采样上算拉伸区间，保证平移时亮度稳定。"""
    b = ds.GetRasterBand(band)
    w, h = ds.RasterXSize, ds.RasterYSize
    scale = min(1.0, float(STATS_SAMPLE_SIZE) / max(w, h))
    bw, bh = max(1, int(w * scale)), max(1, int(h * scale))
    arr = b.ReadAsArray(0, 0, w, h, bw, bh)
    if arr is None:
        return (0.0, 1.0)
    arr = arr.astype(np.float32, copy=False).ravel()
    nodata = b.GetNoDataValue()
    good = np.isfinite(arr)
    if nodata is not None:
        good &= arr != nodata
    arr = arr[good]
    if arr.size == 0:
        return (0.0, 1.0)
    lo, hi = np.percentile(arr, percent)
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        lo, hi = float(arr.min()), float(arr.max())
    if hi <= lo:
        hi = lo + 1.0
    return (float(lo), float(hi))


def read_color_table(ds: gdal.Dataset, band: int = 1) -> Optional[np.ndarray]:
    """读出一个波段的调色板，返回 ``(N, 4)`` 的 uint8 查找表；没有就返回 None。

    分类结果、地质图这类 tif 把类别号存成单波段索引，真正的颜色在 GDAL 的
    ColorTable 里。不查这张表就只能按数值拉伸显示，出来是一片深浅相近的灰，
    十几个岩性根本分不开。
    """
    if band < 1 or band > ds.RasterCount:
        return None
    ct = ds.GetRasterBand(band).GetColorTable()
    if ct is None:
        return None
    n = ct.GetCount()
    if n <= 0:
        return None
    lut = np.zeros((n, 4), dtype=np.uint8)
    for i in range(n):
        entry = ct.GetColorEntry(i)          # (r, g, b, a)
        lut[i] = [entry[0] & 255, entry[1] & 255, entry[2] & 255,
                  entry[3] & 255 if len(entry) > 3 else 255]
    return lut


def display_mask_option(ds: gdal.Dataset, bands: Sequence[int]) -> Dict[str, Any]:
    """挑波段做 VRT 时该怎么带上透明信息，返回给 ``gdal.Translate`` 的参数。

    RGBA 的 PNG 只挑 1、2、3 波段出来，alpha 就丢了，透明背景画成一片不透明，
    压在下面的影像全被盖住。alpha 波段没被挑中时把它当掩膜带上；整幅共用的掩膜
    （GeoTIFF 内部掩膜）也照样带上。
    """
    for i in range(1, ds.RasterCount + 1):
        if (ds.GetRasterBand(i).GetColorInterpretation() == gdal.GCI_AlphaBand
                and i not in bands):
            return {"maskBand": i}
    if ds.RasterCount and ds.GetRasterBand(1).GetMaskFlags() == gdal.GMF_PER_DATASET:
        return {"maskBand": "mask"}
    return {}


class RasterLayer(RasterLayerBase):
    """GDAL 可读影像图层（ENVI 高光谱、GeoTIFF、配准过的 JPG/PNG…）。

    每次渲染都用 ``gdal.Warp`` 把影像按当前视窗重投影 + 重采样到画布尺寸。
    比"全幅降采样一次"的做法贵，但放大到米级时不会糊成一片——标注要对着地物
    描边，这一点不能省。
    """

    kind = "raster"

    def __init__(self, path: str, name: str = "", opacity: float = 1.0) -> None:
        # 使用者在文件对话框里点中 ENVI 的 .hdr 是常事，这里换回数据文件；
        # 之后找波长表、世界文件、显示图层名都以它为准。
        path = georef.resolve_envi_header(path)
        super().__init__(name or os.path.basename(path), opacity)
        self.path = path
        self.ds, self.geoinfo = georef.open_raster(path)
        if not self.geoinfo.georeferenced:
            raise ValueError("影像没有可用的配准信息：\n" + self.geoinfo.describe())

        self.band_count = self.ds.RasterCount
        self.wavelengths = _read_envi_wavelengths(path, self.band_count)
        self.driver = self.ds.GetDriver().ShortName
        if self.driver == "VRT":
            self.driver = "%s（已补配准）" % (gdal.Open(path).GetDriver().ShortName)
        self.stretch_percent = tuple(config.RASTER_STRETCH_PERCENT)
        # 带调色板的影像（分类图、地质图）默认就按调色板画
        self.color_table = read_color_table(self.ds, 1)
        self.use_color_table = self.color_table is not None
        self.bands = self._default_bands()
        self._extent = georef.extent_4326(self.ds)
        self._stats: Dict[int, Tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._disp_ds = None
        self._disp_bands: Tuple[int, ...] = ()
        self._index_ds = None            # 调色板影像的索引波段 VRT
        # 显示用的替身文件：键是 ("bands", b1, b2, b3) 或 ("index",)，值是一份
        # 分块、带金字塔的 GeoTIFF，内容与原图这几个波段相同。大图缩到全幅时读
        # 金字塔、放大时按块随机读，不必每帧把全分辨率整幅扫一遍（PNG 还得从头
        # 解码）。谁来生成由外壳决定，没有替身就照旧读原图
        self._stand_ins: Dict[Tuple[Any, ...], str] = {}
        self._stand_in_ds: Dict[Tuple[Any, ...], gdal.Dataset] = {}

        self.info_lines = [
            "尺寸：%d x %d，%d 个波段" % (self.ds.RasterXSize, self.ds.RasterYSize,
                                          self.band_count),
            "格式：%s" % self.driver,
            self.geoinfo.describe(),
        ]
        if self.color_table is not None:
            self.info_lines.insert(1, "颜色表：%d 项（已按颜色表显示）"
                                   % len(self.color_table))
        self.wavelength_source = "影像自带" if self.wavelengths else ""
        self._wavelength_file = ""            # 外部波长表路径，随会话状态一起记
        self._refresh_wavelength_info()

    # -------------------------------------------------------------- 波长
    def _refresh_wavelength_info(self) -> None:
        self.info_lines = [l for l in self.info_lines if not l.startswith("波长：")]
        if self.wavelengths:
            self.info_lines.insert(1, "波长：%.1f ~ %.1f nm（%s）"
                                   % (self.wavelengths[0], self.wavelengths[-1],
                                      self.wavelength_source))
        elif self.band_count > 12:
            self.info_lines.insert(1, "波长：影像未记录，可加载传感器波长表")

    def wavelength_presets(self) -> Dict[str, str]:
        """波段数吻合的传感器波长表，供界面下拉选择。"""
        return wavelength_presets(self.band_count)

    def load_wavelengths(self, path: str, label: str = "") -> bool:
        """加载一份外部波长表。波段数不吻合时拒绝并返回 False。"""
        values = parse_wavelength_file(path, self.band_count)
        if not values:
            return False
        self.wavelengths = values
        self.wavelength_source = label or os.path.basename(path)
        self._wavelength_file = path
        self._refresh_wavelength_info()
        return True

    # -------------------------------------------------------------- 波段
    def _default_bands(self) -> Tuple[int, ...]:
        n = self.band_count
        if n <= 0:
            return ()
        if self.color_table is not None:
            return (1,)                  # 索引波段，颜色由调色板给
        if n < 3:
            return (1,)
        if n <= 12:
            wanted = tuple(b for b in config.DEFAULT_RGB_BANDS if 1 <= b <= n)
            if len(wanted) == 3:
                return wanted
        # 高光谱固定取前三个波段几乎全是噪声，在全谱段均匀取三个更像真彩色
        return (max(1, n * 3 // 4), max(1, n // 2), max(1, n // 4))

    def band_label(self, band: int) -> str:
        if self.wavelengths and 1 <= band <= len(self.wavelengths):
            return "B%d  %.1f nm" % (band, self.wavelengths[band - 1])
        desc = (self.ds.GetRasterBand(band).GetDescription() or "").strip()
        # GDAL 的 ENVI 驱动在头文件没有 band names 时会自动编出 "Band 80"
        # 这类描述，显示出来只是噪声，等同于没有描述。
        if _BAND_ORDINAL_RE.match(desc):
            desc = ""
        return "B%d  %s" % (band, desc) if desc else "B%d" % band

    def display_key(self) -> Tuple[Any, ...]:
        """当前显示方式对应的替身键。"""
        if self.use_color_table and self.color_table is not None:
            return ("index",)
        return ("bands",) + tuple(self.bands[:3] or (1,))

    def set_stand_in(self, key: Tuple[Any, ...], path: str) -> None:
        """登记一份显示用替身文件（见 ``_stand_ins``）。"""
        with self._lock:
            self._stand_ins[tuple(key)] = path
            self._stand_in_ds.pop(tuple(key), None)

    def _stand_in(self, key: Tuple[Any, ...]) -> Optional[gdal.Dataset]:
        """调用方已持有 ``_lock``。"""
        path = self._stand_ins.get(key)
        if not path:
            return None
        ds = self._stand_in_ds.get(key)
        if ds is None:
            try:
                ds = gdal.Open(path)
            except RuntimeError:
                ds = None
            if ds is None:
                self._stand_ins.pop(key, None)
                return None
            self._stand_in_ds[key] = ds
        return ds

    def set_bands(self, bands: Sequence[int]) -> None:
        """设置显示波段。非法项（None、越界、非数字）一律忽略。

        调用方常常是界面控件，下拉框在索引无效时 ``currentData()`` 就是
        ``None``，不该让它把整个渲染链路带崩。
        """
        clean = tuple(int(b) for b in bands
                      if isinstance(b, (int, float)) and 1 <= int(b) <= self.band_count)
        if clean:
            self.bands = clean
            with self._lock:
                self._disp_ds = None

    def _display_ds(self) -> gdal.Dataset:
        """只含当前显示波段的 VRT。

        直接对高光谱原图调用 ``gdal.Warp`` 会把全部波段都重采样一遍：166 个
        波段里只用 3 个，等于白干 55 倍的活（实测一帧要 15 秒）。先用 VRT 把
        波段挑出来再 warp，代价就只剩下真正要显示的那三个波段。
        """
        with self._lock:
            stand_in = self._stand_in(("bands",) + tuple(self.bands[:3] or (1,)))
            if stand_in is not None:
                return stand_in
            if self._disp_ds is None or self._disp_bands != self.bands:
                bands = list(self.bands[:3]) or [1]
                self._disp_ds = gdal.Translate("", self.ds, format="VRT",
                                               bandList=bands,
                                               **display_mask_option(self.ds, bands))
                self._disp_bands = self.bands
            return self._disp_ds

    def set_stretch(self, low: float, high: float) -> None:
        self.stretch_percent = (float(low), float(high))
        with self._lock:
            self._stats.clear()

    def stats_for(self, band: int) -> Tuple[float, float]:
        with self._lock:
            if band not in self._stats:
                self._stats[band] = _band_stats(self.ds, band, self.stretch_percent)
            return self._stats[band]

    # -------------------------------------------------------------- 渲染
    def extent(self):
        return self._extent

    def render_array(self, view: ViewSpec) -> Optional[np.ndarray]:
        if not view.valid or self._extent is None:
            return None
        # 相交判断必须拿经纬度对经纬度：self._extent 是经纬度，而 view.bounds
        # 在投影显示时是米，两者直接比永远不相交，影像就一张都不画了。
        if not intersects(self._extent, view.lonlat_bounds):
            return None
        with self._render_lock:
            return self._render_locked(view)

    def set_use_color_table(self, on: bool) -> None:
        """在"按颜色表显示"与"按数值拉伸显示"之间切换。"""
        self.use_color_table = bool(on) and self.color_table is not None
        with self._lock:
            self._disp_ds = None

    def _index_dataset(self) -> gdal.Dataset:
        """只含索引波段、且已摘掉调色板的 VRT。

        直接把带调色板的波段丢给 ``gdal.Warp``，GDAL 有可能替我们把调色板展开
        成 RGB(A)，那样读回来的第一个波段就是红色分量而不是类别号，查表全错。
        先把颜色解释改成灰度、去掉调色板，warp 出来的就一定还是索引本身。
        """
        with self._lock:
            stand_in = self._stand_in(("index",))
            if stand_in is not None:
                return stand_in
            if self._index_ds is None:
                vrt = gdal.Translate("", self.ds, format="VRT", bandList=[1])
                band = vrt.GetRasterBand(1)
                try:
                    band.SetColorTable(None)
                    band.SetColorInterpretation(gdal.GCI_GrayIndex)
                except RuntimeError:
                    pass
                self._index_ds = vrt
            return self._index_ds

    def _render_locked(self, view: ViewSpec) -> Optional[np.ndarray]:
        if self.use_color_table and self.color_table is not None:
            return self._render_palette(view)
        minx, miny, maxx, maxy = view.bounds
        src = self._display_ds()
        picked = list(self.bands[:3]) or [1]
        # 缩得很小时用最近邻：平均值重采样在大比例缩减下开销高得多，而屏幕上
        # 一个像素本来就代表成百上千个源像元，画质差别看不出来。
        shrink = self._shrink_factor(view)
        alg = gdal.GRA_NearestNeighbour if shrink > 8 else gdal.GRA_Average
        warped = gdal.Warp(
            "", src, format="MEM",
            dstSRS="EPSG:%d" % view.epsg,
            outputBounds=(minx, miny, maxx, maxy),
            width=view.width, height=view.height,
            resampleAlg=alg, dstAlpha=True,
            errorThreshold=0.125, multithread=True,
            warpOptions=["NUM_THREADS=ALL_CPUS"])
        if warped is None:
            return None

        total = warped.RasterCount
        alpha = None
        data_bands = total
        if total >= 2:
            last = warped.GetRasterBand(total)
            if last.GetColorInterpretation() == gdal.GCI_AlphaBand:
                alpha = last.ReadAsArray()
                data_bands = total - 1

        planes = []
        # warp 过的是抽波段后的 VRT，因此这里按位置取 1..3，拉伸区间仍按
        # 原图的波段号查，两者不能混。
        for pos, band in enumerate(picked[:data_bands], start=1):
            arr = warped.GetRasterBand(pos).ReadAsArray()
            if arr is None:
                continue
            arr = arr.astype(np.float32, copy=False)
            lo, hi = self.stats_for(band)
            out = (np.clip(arr, lo, hi) - lo) * (255.0 / (hi - lo))
            planes.append(out.astype(np.uint8))
        if not planes:
            return None
        if len(planes) == 1:
            rgb = np.dstack([planes[0]] * 3)
        else:
            while len(planes) < 3:
                planes.append(planes[-1])
            rgb = np.dstack(planes[:3])

        if alpha is None:
            alpha = np.full(rgb.shape[:2], 255, dtype=np.uint8)
        out = np.dstack([rgb, alpha.astype(np.uint8)])
        warped = None
        return np.ascontiguousarray(out)

    def _render_palette(self, view: ViewSpec) -> Optional[np.ndarray]:
        """按调色板出图：重采样必须用最近邻。

        类别号是标称值，平均两个类别号得到的是第三个类别，重采样一插值整幅图
        就多出一堆本不存在的类别。所以这里不看缩放档位，一律最近邻。
        """
        minx, miny, maxx, maxy = view.bounds
        warped = gdal.Warp(
            "", self._index_dataset(), format="MEM",
            dstSRS="EPSG:%d" % view.epsg,
            outputBounds=(minx, miny, maxx, maxy),
            width=view.width, height=view.height,
            resampleAlg=gdal.GRA_NearestNeighbour, dstAlpha=True,
            errorThreshold=0.125, multithread=True,
            warpOptions=["NUM_THREADS=ALL_CPUS"])
        if warped is None:
            return None
        index = warped.GetRasterBand(1).ReadAsArray()
        if index is None:
            return None
        alpha = None
        if warped.RasterCount >= 2:
            last = warped.GetRasterBand(warped.RasterCount)
            if last.GetColorInterpretation() == gdal.GCI_AlphaBand:
                alpha = last.ReadAsArray()

        lut = self.color_table
        idx = np.clip(index.astype(np.int32), 0, len(lut) - 1)
        rgba = lut[idx]
        out_alpha = rgba[:, :, 3]
        if alpha is not None:
            out_alpha = np.minimum(out_alpha, alpha.astype(np.uint8))
        warped = None
        return np.ascontiguousarray(
            np.dstack([rgba[:, :, :3], out_alpha.astype(np.uint8)]))

    def _shrink_factor(self, view: ViewSpec) -> float:
        """源像元数 / 目标像元数的边长比，用来判断缩放档位。"""
        if self._extent is None or view.width <= 0:
            return 1.0
        # 两边都按经纬度算：view.bounds 在投影显示时是米，与源影像的度混在
        # 一起比会得出天文数字的缩放比，一律退化成最近邻。
        lonlat = view.lonlat_bounds
        src_px_per_deg = self.ds.RasterXSize / max(1e-12,
                                                   self._extent[2] - self._extent[0])
        dst_px_per_deg = view.width / max(1e-12, lonlat[2] - lonlat[0])
        return max(1.0, src_px_per_deg / max(1e-12, dst_px_per_deg))

    def close(self) -> None:
        self._disp_ds = None
        self._index_ds = None
        self._stand_in_ds.clear()
        self.ds = None


class ImageLayer(RasterLayer):
    """带世界文件 / ESRI 配准信息的 JPG、PNG。

    与 :class:`RasterLayer` 共用全部实现，单独立一个类只为在图层面板里区分
    图标与默认不透明度——配准图片通常是压在影像上看的，默认给一点透明。
    """

    kind = "image"

    def _default_bands(self) -> Tuple[int, ...]:
        n = self.band_count
        return (1, 2, 3) if n >= 3 else (1,)


# =============================================================================
# 底图图层
# =============================================================================

class BasemapLayer(RasterLayerBase):
    """在线瓦片底图。"""

    kind = "basemap"

    def __init__(self, source: tiles.TileSource, fetcher: tiles.TileFetcher,
                 opacity: float = 1.0, on_tile=None) -> None:
        super().__init__(source.name, opacity)
        self.source = source
        self.fetcher = fetcher
        self.on_tile = on_tile          # 新片到货时通知画布重绘
        self.info_lines = [source.attribution] if source.attribution else []
        self._pending = 0

    def extent(self):
        return None                      # 底图覆盖全球，不参与"缩放到图层"

    @property
    def pending(self) -> int:
        return self._pending

    def _tile_ready(self, source, z, x, y, data) -> None:
        if self.on_tile is not None:
            self.on_tile(self)

    def render_array(self, view: ViewSpec) -> Optional[np.ndarray]:
        if not view.valid:
            return None
        with self._render_lock:
            return self._render_locked(view)

    def _render_locked(self, view: ViewSpec) -> Optional[np.ndarray]:
        scheme = self.source.scheme
        native = self._to_native(view.lonlat_bounds)
        if native is None:
            return None
        # 多画的那一圈按比例放大取片上限：层级仍按可见部分选，只是多取周边几片
        budget = int(round(getattr(config, "TILE_MAX_PER_VIEW", 48)
                           * (1.0 + 2.0 * view.margin) ** 2))
        z, rng = tiles.plan(self.source, native, view.width, max_tiles=budget)
        x0, y0, x1, y1 = rng

        size = scheme.tile_size
        cols, rows = (x1 - x0 + 1), (y1 - y0 + 1)
        canvas = np.zeros((rows * size, cols * size, 4), dtype=np.uint8)
        pending = 0
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                data = self.fetcher.request(self.source, z, x, y, self._tile_ready)
                if data is None:
                    pending += 1
                    continue
                tile = _decode_tile(data)
                if tile is None:
                    continue
                r0 = (y - y0) * size
                c0 = (x - x0) * size
                canvas[r0:r0 + tile.shape[0], c0:c0 + tile.shape[1], :] = tile
        self._pending = pending

        mosaic = tiles.mosaic_bounds(scheme, z, rng)
        return _resample_to_view(canvas, mosaic, scheme.epsg, view)

    def _to_native(self, bounds):
        """视窗范围（经纬度）转成该瓦片方案的原生坐标。"""
        scheme = self.source.scheme
        if scheme.kind == "geodetic":
            # EPSG:4490 与 EPSG:4326 的椭球差异在瓦片尺度上远小于一个像素，
            # 直接当同一套经纬度用。
            minx, miny, maxx, maxy = bounds
            return (max(-180.0, minx), max(-85.0, miny),
                    min(180.0, maxx), min(85.0, maxy))
        return _lonlat_to_mercator(bounds)

    def close(self) -> None:
        self.on_tile = None


def _decode_tile(data: bytes) -> Optional[np.ndarray]:
    """瓦片字节流 -> RGBA 数组。"""
    try:
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(data))
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        return np.asarray(img, dtype=np.uint8)
    except Exception:
        return None


def _lonlat_to_mercator(bounds):
    minx, miny, maxx, maxy = bounds
    miny = max(-85.05112878, miny)
    maxy = min(85.05112878, maxy)
    if maxy <= miny:
        return None

    def y(lat):
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * tiles.MERC_R / math.pi

    def x(lon):
        return lon * tiles.MERC_R / 180.0

    return (x(minx), y(miny), x(maxx), y(maxy))


def _resample_to_view(array: np.ndarray, src_bounds, src_epsg: int,
                      view: ViewSpec) -> Optional[np.ndarray]:
    """把一块已知范围的 RGBA 数组重采样到视窗，必要时顺带重投影。"""
    h, w = array.shape[:2]
    if h == 0 or w == 0:
        return None
    mem = gdal.GetDriverByName("MEM").Create("", w, h, 4, gdal.GDT_Byte)
    gt = (src_bounds[0], (src_bounds[2] - src_bounds[0]) / w, 0.0,
          src_bounds[3], 0.0, -(src_bounds[3] - src_bounds[1]) / h)
    mem.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(src_epsg)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    mem.SetProjection(srs.ExportToWkt())
    for i in range(4):
        mem.GetRasterBand(i + 1).WriteArray(array[:, :, i])
    mem.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)

    minx, miny, maxx, maxy = view.bounds
    warped = gdal.Warp("", mem, format="MEM",
                       dstSRS="EPSG:%d" % view.epsg,
                       outputBounds=(minx, miny, maxx, maxy),
                       width=view.width, height=view.height,
                       resampleAlg=gdal.GRA_Bilinear)
    mem = None
    if warped is None:
        return None
    out = np.dstack([warped.GetRasterBand(i + 1).ReadAsArray() for i in range(4)])
    warped = None
    return np.ascontiguousarray(out.astype(np.uint8))


# =============================================================================
# 矢量图层
# =============================================================================

@dataclass
class VectorFeature:
    """参考矢量中的一个要素，几何已转成经纬度坐标。"""

    fid: int
    geom_kind: str                       # point | line | polygon
    coords: Any
    bbox: Tuple[float, float, float, float]
    attrs: Dict[str, Any] = field(default_factory=dict)


class _GridIndex:
    """均匀网格索引。

    一张岩性图动辄几万个图斑，每帧拿全部要素挨个比外接矩形，光筛选就要几十
    毫秒——还没开始画就已经掉帧了。这里在载入时把要素按外接矩形登记到覆盖的
    格子上，之后只看落进视窗的那几格。

    跨很多格子的大要素（区域边界、水系那种）不进格子，单独放一份"每次都参与
    判断"的清单：把它们抄进上千个格子，建索引本身就成了瓶颈。
    """

    MAX_CELLS_PER_FEATURE = 64      # 超过这么多格子的要素归入 big

    def __init__(self, features: Sequence["VectorFeature"], extent,
                 side: int = 0) -> None:
        self.side = int(side or config.VECTOR_INDEX_SIDE)
        self.cells: Dict[Tuple[int, int], List["VectorFeature"]] = {}
        self.big: List["VectorFeature"] = []
        if not features or extent is None or self.side < 1:
            self.big = list(features)
            self.x0 = self.y0 = 0.0
            self.w = self.h = 1.0
            self.side = 1
            return
        x0, y0, x1, y1 = extent
        self.x0, self.y0 = x0, y0
        self.w = max((x1 - x0) / self.side, 1e-12)
        self.h = max((y1 - y0) / self.side, 1e-12)
        for f in features:
            i0, j0, i1, j1 = self._span(f.bbox)
            if (i1 - i0 + 1) * (j1 - j0 + 1) > self.MAX_CELLS_PER_FEATURE:
                self.big.append(f)
                continue
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    self.cells.setdefault((i, j), []).append(f)

    def _span(self, box) -> Tuple[int, int, int, int]:
        last = self.side - 1
        def clamp(v):
            return 0 if v < 0 else (last if v > last else v)
        return (clamp(int((box[0] - self.x0) / self.w)),
                clamp(int((box[1] - self.y0) / self.h)),
                clamp(int((box[2] - self.x0) / self.w)),
                clamp(int((box[3] - self.y0) / self.h)))

    def query(self, bounds) -> Optional[List["VectorFeature"]]:
        """落进视窗的候选要素；``None`` 表示"不如全扫"，由调用方兜底。"""
        if not self.cells:
            return None
        i0, j0, i1, j1 = self._span(bounds)
        if (i1 - i0 + 1) * (j1 - j0 + 1) >= self.side * self.side * 0.5:
            return None                  # 视窗覆盖了大半张图，走索引反而更慢
        seen = set()
        out = list(self.big)
        seen.update(id(f) for f in self.big)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                for f in self.cells.get((i, j), ()):
                    if id(f) not in seen:
                        seen.add(id(f))
                        out.append(f)
        return out


class VectorLayer(VectorLayerBase):
    """参考矢量图层：只用于对照参考，不参与样本编辑。"""

    kind = "vector"

    def __init__(self, path: str, layer_index: int = 0, name: str = "",
                 opacity: float = 1.0,
                 color: str = config.DEFAULT_VECTOR_COLOR) -> None:
        super().__init__(name or os.path.basename(path), opacity)
        self.path = path
        self.layer_index = int(layer_index)
        self.color = color
        self.fill = True
        # 面填充的不透明度（0-255）。与图层不透明度是相乘的关系：默认半透明是
        # 为了看清底下的影像，画岩性图时把两个都拉满才是真正的不透明。
        self.fill_alpha = int(config.VECTOR_FILL_ALPHA)
        self.line_width = 1.2
        self.label_field = ""
        # 按属性唯一值赋色：为空表示整层一个颜色，走 self.color
        self.color_field = ""
        self.color_map: Dict[str, str] = {}
        self.features: List[VectorFeature] = []
        self._extent = None
        self._load(layer_index)

    # ---------------------------------------------------------- 唯一值赋色
    @staticmethod
    def value_text(value) -> str:
        """属性值 -> 用作赋色键的文本。空值单独成一类，否则会与空字符串混在一起。"""
        if value is None or value == "":
            return NULL_VALUE_LABEL
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    def unique_values(self, field: str) -> List[str]:
        """某个字段出现过的全部取值，按出现顺序去重。"""
        seen: List[str] = []
        mark = set()
        for f in self.features:
            text = self.value_text((f.attrs or {}).get(field))
            if text not in mark:
                mark.add(text)
                seen.append(text)
        return seen

    def set_unique_colors(self, field: str,
                          colors: Optional[Dict[str, str]] = None) -> List[str]:
        """按 ``field`` 的唯一值赋色，返回值的顺序列表。

        已有的取值保留原来的颜色，新出现的从配色表里顺次取——重新赋色时不希望
        整张图的配色全变，那样刚记住的"红色是灰岩"就作废了。
        """
        values = self.unique_values(field)
        table = dict(colors or {})
        used = len([v for v in values if v in table])
        for value in values:
            if value not in table:
                table[value] = config.class_color(used)
                used += 1
        self.color_field = field
        self.color_map = table
        self._refresh_color_info()
        return values

    def clear_unique_colors(self) -> None:
        self.color_field = ""
        self.color_map = {}
        self._refresh_color_info()

    def _refresh_color_info(self) -> None:
        self.info_lines = [l for l in getattr(self, "info_lines", [])
                           if not l.startswith("赋色字段：")]
        if self.color_field:
            self.info_lines.append("赋色字段：%s（%d 个唯一值）"
                                   % (self.color_field, len(self.color_map)))

    def feature_color(self, feature: "VectorFeature") -> str:
        if not self.color_field:
            return self.color
        key = self.value_text((feature.attrs or {}).get(self.color_field))
        return self.color_map.get(key, self.color)

    def color_groups(self, feats: Sequence["VectorFeature"]
                     ) -> "OrderedDict[str, List[VectorFeature]]":
        """把要素按颜色分组，画的时候一个颜色只换一次画笔。"""
        groups: "OrderedDict[str, List[VectorFeature]]" = OrderedDict()
        if not self.color_field:
            groups[self.color] = list(feats)
            return groups
        for f in feats:
            groups.setdefault(self.feature_color(f), []).append(f)
        return groups

    def _load(self, layer_index: int) -> None:
        ds = open_vector(self.path)
        lyr = ds.GetLayer(layer_index)
        if lyr is None:
            raise IOError("矢量文件里没有第 %d 个图层" % layer_index)

        src = lyr.GetSpatialRef()
        dst = osr.SpatialReference()
        dst.ImportFromEPSG(config.STORAGE_EPSG)
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = None
        if src is not None:
            s = src.Clone()
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if not s.IsSame(dst):
                tr = osr.CoordinateTransformation(s, dst)
        self.native_crs = src.GetName() if src is not None else ""

        defn = lyr.GetLayerDefn()
        self.fields = [defn.GetFieldDefn(i).GetName()
                       for i in range(defn.GetFieldCount())]

        box = None
        truncated = False
        lyr.ResetReading()
        for feat in lyr:
            if len(self.features) >= VECTOR_FEATURE_LIMIT:
                truncated = True
                break
            geom = feat.GetGeometryRef()
            if geom is None or geom.IsEmpty():
                continue
            g = geom.Clone()
            g.FlattenTo2D()
            if tr is not None:
                try:
                    g.Transform(tr)
                except Exception:
                    continue
            kind, coords = _vector_coords(g)
            if not coords:
                continue
            if kind != "point":
                # 环存成 (N,2) 的 float64 数组。画的时候要把上万个顶点一次性
                # 换算到屏幕坐标，用 Python 列表逐点算是几十倍的差距。
                coords = [np.asarray(r, dtype=np.float64) for r in coords
                          if len(r) >= 2]
                if not coords:
                    continue
            e = g.GetEnvelope()
            bbox = (e[0], e[2], e[1], e[3])
            box = bbox if box is None else (min(box[0], bbox[0]), min(box[1], bbox[1]),
                                            max(box[2], bbox[2]), max(box[3], bbox[3]))
            self.features.append(VectorFeature(
                fid=feat.GetFID(), geom_kind=kind, coords=coords, bbox=bbox,
                attrs={k: feat.GetField(k) for k in self.fields}))
        self._extent = box
        ds = None
        self._index = (_GridIndex(self.features, self._extent)
                       if len(self.features) >= config.VECTOR_INDEX_MIN_FEATURES
                       else None)

        self.info_lines = [
            "要素：%d 个" % len(self.features),
            "原坐标系：%s" % (self.native_crs or "未知"),
            "字段：%s" % ("、".join(self.fields[:8]) or "无"),
        ]
        self._refresh_color_info()
        if truncated:
            self.info_lines.append(
                "要素超过 %d 个，只载入了前 %d 个用于显示。"
                % (VECTOR_FEATURE_LIMIT, len(self.features)))

    def extent(self):
        return self._extent

    def visible_features(self, bounds) -> List[VectorFeature]:
        pool = self._index.query(bounds) if self._index is not None else None
        if pool is None:
            pool = self.features
        return [f for f in pool if intersects(f.bbox, bounds)]

    @staticmethod
    def sublayers(path: str) -> List[str]:
        """列出矢量文件里的图层名，供多图层的 gpkg / gdb 选择。"""
        try:
            ds = open_vector(path)
        except IOError:
            return []
        names = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]
        ds = None
        return names


def _vector_coords(geom: ogr.Geometry) -> Tuple[str, Any]:
    """OGR 几何 -> (类别, 绘制坐标)。线与面都表示成环的列表。"""
    t = ogr.GT_Flatten(geom.GetGeometryType())
    if t == ogr.wkbPoint:
        return "point", [(geom.GetX(), geom.GetY())]
    if t == ogr.wkbMultiPoint:
        return "point", [(geom.GetGeometryRef(i).GetX(),
                          geom.GetGeometryRef(i).GetY())
                         for i in range(geom.GetGeometryCount())]
    if t in (ogr.wkbLineString, ogr.wkbLinearRing):
        return "line", [[(geom.GetX(i), geom.GetY(i))
                         for i in range(geom.GetPointCount())]]
    if t == ogr.wkbMultiLineString:
        rings = []
        for i in range(geom.GetGeometryCount()):
            _, sub = _vector_coords(geom.GetGeometryRef(i))
            rings.extend(sub)
        return "line", rings
    if t == ogr.wkbPolygon:
        rings = []
        for i in range(geom.GetGeometryCount()):
            r = geom.GetGeometryRef(i)
            rings.append([(r.GetX(j), r.GetY(j)) for j in range(r.GetPointCount())])
        return "polygon", rings
    if t in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        rings = []
        for i in range(geom.GetGeometryCount()):
            _, sub = _vector_coords(geom.GetGeometryRef(i))
            if sub and isinstance(sub[0], list):
                rings.extend(sub)
        return "polygon", rings
    return "", []


# =============================================================================
# 样本图层
# =============================================================================

class SessionSource:
    """让 :class:`SampleLayer` 直接吃 :class:`~samplelib.session.LibrarySession`。

    主窗口的画布是只读的，没必要为它造一个编辑缓冲区；这个适配器只提供
    SampleLayer 真正用到的三个方法，接口与 EditBuffer 一致。
    """

    def __init__(self, session) -> None:
        self.session = session

    @property
    def version(self) -> int:
        return self.session.version

    def records(self):
        return self.session.records

    def classes(self):
        return self.session.classes

    def class_map(self):
        return self.session.class_map

    def record(self, key):
        return self.session.record_by_key.get(key)


class SampleLayer(VectorLayerBase):
    """样本库图层：数据来自 :class:`~samplelib.annotate.editing.EditBuffer`。

    这是唯一可编辑的图层。它不缓存要素——缓冲区随时可能因为一次撤销而变化，
    每次取当前合并视图即可；样本量在万级以内，逐条过滤的开销可以忽略。
    """

    kind = "samples"

    def __init__(self, buffer, name: str = "样本库", opacity: float = 1.0) -> None:
        super().__init__(name, opacity)
        self.buffer = buffer
        self.fill = True
        self.line_width = 1.4
        self.point_radius = config.POINT_RADIUS
        self.visible_class_ids: Optional[set] = None   # None = 全部显示

    def extent(self):
        box = None
        for rec in self.buffer.records():
            b = rec.bbox
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                         max(box[2], b[2]), max(box[3], b[3]))
        return box

    def class_extent(self, class_ids) -> Optional[Tuple[float, float, float, float]]:
        ids = set(class_ids)
        box = None
        for rec in self.buffer.records():
            if rec.class_id not in ids:
                continue
            b = rec.bbox
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                         max(box[2], b[2]), max(box[3], b[3]))
        return box

    def visible_features(self, bounds) -> List[SampleRecord]:
        allow = self.visible_class_ids
        out = []
        for rec in self.buffer.records():
            if allow is not None and rec.class_id not in allow:
                continue
            if intersects(rec.bbox, bounds):
                out.append(rec)
        return out

    def colors(self) -> Dict[int, str]:
        return {c.class_id: c.color for c in self.buffer.classes()}

    def data_version(self) -> Optional[Tuple[int, int]]:
        """数据源的身份与版本，画布拿它当离屏缓存的指纹。

        源换了对象（浏览 / 标注模式切换）或版本号变了，都算数据变了。数据源不
        报版本号（第三方源）时返回 None，画布对这层就不用缓存。
        """
        version = getattr(self.buffer, "version", None)
        if version is None:
            return None
        return (id(self.buffer), int(version))

    def feature_count(self) -> int:
        return len(self.buffer.records())


# =============================================================================
# 图层栈
# =============================================================================

class LayerStack:
    """图层集合。索引 0 画在最上层，与图层面板从上往下的顺序一致。"""

    def __init__(self) -> None:
        self.layers: List[Layer] = []

    def __len__(self) -> int:
        return len(self.layers)

    def __iter__(self):
        return iter(self.layers)

    def __getitem__(self, i) -> Layer:
        return self.layers[i]

    def add(self, layer: Layer, index: int = 0) -> Layer:
        self.layers.insert(max(0, min(len(self.layers), index)), layer)
        return layer

    def append(self, layer: Layer) -> Layer:
        self.layers.append(layer)
        return layer

    def remove(self, layer: Layer) -> None:
        if layer in self.layers:
            self.layers.remove(layer)
            layer.close()

    def move(self, layer: Layer, delta: int) -> int:
        i = self.layers.index(layer)
        j = max(0, min(len(self.layers) - 1, i + delta))
        if i != j:
            self.layers.pop(i)
            self.layers.insert(j, layer)
        return j

    def index_of(self, layer: Layer) -> int:
        return self.layers.index(layer)

    def draw_order(self) -> List[Layer]:
        """自底向上的绘制顺序：列表末尾的图层先画。"""
        return [l for l in reversed(self.layers) if l.is_shown]

    def sample_layer(self) -> Optional[SampleLayer]:
        for l in self.layers:
            if isinstance(l, SampleLayer):
                return l
        return None

    def extent(self) -> Optional[Tuple[float, float, float, float]]:
        box = None
        for l in self.layers:
            b = l.extent()
            if b is None:
                continue
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                         max(box[2], b[2]), max(box[3], b[3]))
        return box

    def close(self) -> None:
        for l in self.layers:
            l.close()
        self.layers = []


def is_styled_vector(layer) -> bool:
    """是不是"有颜色和填充可调的矢量图层"。

    参考矢量与手绘范围都算，样本图层不算——它虽然也是矢量，颜色却是按类别
    逐个要素来的，没有"整层一个颜色"这回事。直接写 isinstance(x, VectorLayer)
    会漏掉手绘范围，写 VectorLayerBase 又会把样本图层卷进来（它没有 .color，
    一刷新就 AttributeError）。判定条件收在这里一处。
    """
    return isinstance(layer, VectorLayerBase) and hasattr(layer, "color")


# =============================================================================
# 手绘范围图层
# =============================================================================

class DrawnLayer(VectorLayerBase):
    """在画布上画出来的面图层，几何只在内存里。

    感兴趣区从"藏在会话里的一个隐形范围"改成了普通图层，就是靠它：画出来之后
    它在图层清单里看得见、能改颜色、能调透明度、能导出成矢量文件，也能像任何
    别的矢量图层一样拿去筛样本。原先那个隐形 ROI 使用者根本不知道它还在不在。

    它继承 :class:`VectorLayerBase` 而不是 :class:`VectorLayer`：后者构造时就要
    读文件，而这里压根没有文件。渲染走的是同一套 ``visible_features``，因此画布
    那边一行都不用改。
    """

    kind = "drawn"
    kind_cn = "手绘范围"

    def __init__(self, name: str = "感兴趣区", opacity: float = 1.0,
                 color: str = config.DEFAULT_VECTOR_COLOR) -> None:
        super().__init__(name, opacity)
        self.path = ""
        self.color = color
        self.fill = True
        self.fill_alpha = int(config.VECTOR_FILL_ALPHA)
        self.line_width = 1.6
        self.label_field = ""
        self.color_field = ""
        self.color_map: Dict[str, str] = {}
        self.features: List[VectorFeature] = []
        self._next_fid = 1

    # ------------------------------------------------------------------ 内容
    def add_polygon(self, rings) -> VectorFeature:
        """加一个面。``rings`` 是 [[(lon, lat), ...], ...]，第一个是外环。"""
        pts = [pt for ring in rings for pt in ring]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        feat = VectorFeature(fid=self._next_fid, geom_kind="polygon",
                             coords=[list(r) for r in rings],
                             bbox=(min(xs), min(ys), max(xs), max(ys)),
                             attrs={"name": "范围 %d" % self._next_fid})
        self._next_fid += 1
        self.features.append(feat)
        return feat

    def add_geometry(self, geom, name: str = "") -> int:
        """把一个 OGR 面几何（EPSG:4326）拆成若干个面加进来，返回加了几个。

        影像的有效范围常常是好几块互不相连的区域（云掩膜、拼接缝），必须一块一个
        要素——合成一个 MultiPolygon 塞进单个要素的话，图层清单上看到的是"1 个
        要素"，删掉其中一块也无从谈起。
        """
        from osgeo import ogr

        added = 0
        stack = [geom]
        while stack:
            g = stack.pop()
            if g is None or g.IsEmpty():
                continue
            t = ogr.GT_Flatten(g.GetGeometryType())
            if t in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
                stack.extend(g.GetGeometryRef(i)
                             for i in range(g.GetGeometryCount()))
                continue
            if t != ogr.wkbPolygon:
                continue
            rings = []
            for i in range(g.GetGeometryCount()):
                r = g.GetGeometryRef(i)
                ring = [(r.GetX(j), r.GetY(j)) for j in range(r.GetPointCount())]
                if len(ring) >= 3:
                    rings.append(ring)
            if not rings:
                continue
            feat = self.add_polygon(rings)
            if name:
                feat.attrs["name"] = "%s %d" % (name, feat.fid)
            added += 1
        return added

    def remove_last(self) -> bool:
        if not self.features:
            return False
        self.features.pop()
        return True

    def clear(self) -> None:
        self.features = []

    @property
    def count(self) -> int:
        return len(self.features)

    # ------------------------------------------------------------------ 渲染
    def visible_features(self, bounds) -> List[VectorFeature]:
        return [f for f in self.features if intersects(f.bbox, bounds)]

    def feature_color(self, feature: VectorFeature) -> str:
        return self.color

    def color_groups(self, feats):
        """整层一个颜色，一组到底。画布按颜色分组换笔，这个接口得有。"""
        return OrderedDict([(self.color, list(feats))])

    def extent(self):
        if not self.features:
            return None
        xs0 = min(f.bbox[0] for f in self.features)
        ys0 = min(f.bbox[1] for f in self.features)
        xs1 = max(f.bbox[2] for f in self.features)
        ys1 = max(f.bbox[3] for f in self.features)
        return (xs0, ys0, xs1, ys1)

    def describe(self) -> str:
        return "手绘范围 · %d 个面" % len(self.features)

    # ------------------------------------------------------------------ 导出
    def save_as(self, path: str) -> str:
        """写成矢量文件。按扩展名挑驱动，默认 GeoJSON。"""
        from osgeo import ogr, osr

        drivers = {".shp": "ESRI Shapefile", ".geojson": "GeoJSON",
                   ".json": "GeoJSON", ".gpkg": "GPKG"}
        ext = os.path.splitext(path)[1].lower()
        driver = ogr.GetDriverByName(drivers.get(ext, "GeoJSON"))
        if driver is None:
            raise RuntimeError("这个格式的 GDAL 驱动不可用：%s" % ext)
        if os.path.exists(path):
            driver.DeleteDataSource(path)
        ds = driver.CreateDataSource(path)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        lyr = ds.CreateLayer("roi", srs, ogr.wkbPolygon)
        lyr.CreateField(ogr.FieldDefn("name", ogr.OFTString))
        for f in self.features:
            poly = ogr.Geometry(ogr.wkbPolygon)
            for ring in f.coords:
                r = ogr.Geometry(ogr.wkbLinearRing)
                for lon, lat in ring:
                    r.AddPoint_2D(float(lon), float(lat))
                r.CloseRings()
                poly.AddGeometry(r)
            feat = ogr.Feature(lyr.GetLayerDefn())
            feat.SetField("name", (f.attrs or {}).get("name", ""))
            feat.SetGeometry(poly)
            lyr.CreateFeature(feat)
        ds = None
        return path


# =============================================================================
# 拖进来的文件是什么
# =============================================================================

def classify_drop(path: str) -> str:
    """判断拖进来的文件该当成什么：raster / image / vector / unknown。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in config.DROP_IMAGE_EXTS:
        return "image"
    if ext in config.DROP_RASTER_EXTS:
        return "raster"
    if ext in config.DROP_VECTOR_EXTS:
        return "vector"
    return _probe(path)


def _probe(path: str) -> str:
    """扩展名认不出时，让 GDAL 试着打开。

    ENVI 的裸数据文件常常没有扩展名，只能靠试探。栅格在前——同一个文件被两种
    驱动都认下的情况极少，而这里的主要目标就是影像。
    """
    from osgeo import gdal
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        try:
            ds = gdal.OpenEx(path, gdal.OF_RASTER | gdal.OF_READONLY)
            if ds is not None and ds.RasterCount > 0:
                return "raster"
        except Exception:
            pass
        try:
            ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY)
            if ds is not None and ds.GetLayerCount() > 0:
                return "vector"
        except Exception:
            pass
    finally:
        gdal.PopErrorHandler()
    return "unknown"
