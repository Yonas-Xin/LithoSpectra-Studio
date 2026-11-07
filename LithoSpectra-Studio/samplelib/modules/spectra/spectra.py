# -*- coding: utf-8 -*-
"""从影像上取一条光谱曲线。

**只负责取数，不负责画。** 画在 ``ui/plot.py``，两者之间只隔一个
:class:`Spectrum` 数据类——将来要把同一条曲线送去别的地方（导出 CSV、送进
分类器、跟波谱库比对），拿到的都是同一个东西。

取数这件事有三处容易出错，都在这里一次性处理掉：

* **坐标**。画布给的是 EPSG:4326 经纬度，影像自己在什么坐标系里不一定。
  先投影到影像的坐标系，再用逆仿射换成行列号。
* **无效值**。nodata、Inf、以及高光谱边缘那些恒零的坏波段，一律换成 NaN 而
  不是 0——换成 0 会在曲线上拉出一条到底的尖峰，比断开难看得多，还会把纵轴
  的范围整个带偏。
* **定标系数**。GDAL 波段自带 scale / offset 时按它换算；这是 ENVI 与部分
  GeoTIFF 记录反射率缩放的正规位置，忽略它取回来的就是 0~10000 的整数。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from osgeo import gdal, osr

from ... import config

# =============================================================================
# 配置区
# =============================================================================

#: 取谱窗口的可选边长（像元）。窗口内取均值，用来压掉单像元噪声。
WINDOW_SIZES: Tuple[int, ...] = (1, 3, 5, 7, 9)

#: 领域数据文件名。水汽区间、诊断吸收特征、"范围"下拉的档位都在里面。
#: 单独放一个 JSON 而不是写死在代码里：这些数会随传感器与研究区调整，改一个
#: 中心波长不该动源码，更不该等一次发版。**改完重启程序生效。**
FEATURE_FILE = "spectral_features.json"


@dataclass(frozen=True)
class Feature:
    """一个诊断吸收特征。

    名字存两份：``name`` 给界面（菜单、提示里的 Fe³⁺ 是真 Unicode 上标），
    ``label`` 给图（matplotlib 的 mathtext）。图上不能直接用 Unicode 上下标——
    Arial、Helvetica 都没有这些字形，画出来是一串空豆腐块；mathtext 是把正常
    数字缩小挪位拼出来的，任何字体都成立。
    """

    key: str                              # F1 ~ F5
    name: str                             # 界面上的名字
    label: str                            # 图上的名字（mathtext）
    centre: float                         # 中心波长 nm
    window: Tuple[float, float]           # 判读窗口 nm，做特征提取用的就是它
    view: Tuple[float, float]             # 「范围」跳过去时显示的那一段


#: 读不到 / 读坏了时的退路，与随包那份 JSON 保持一致。
_BUILTIN: Dict[str, Any] = {
    "water_gaps": [[1324.5, 1459.0], [1795.5, 1963.7]],
    "features": [
        {"key": "F1", "name": "Fe³⁺", "label": "Fe$^{3+}$", "centre": 511.0,
         "window": [430.0, 620.0], "view": [400.0, 800.0]},
        {"key": "F2", "name": "Fe²⁺", "label": "Fe$^{2+}$", "centre": 1014.0,
         "window": [800.0, 1250.0], "view": [700.0, 1330.0]},
        {"key": "F3", "name": "Al–OH", "label": "Al–OH", "centre": 2207.0,
         "window": [2120.0, 2245.0], "view": [2050.0, 2320.0]},
        {"key": "F4", "name": "Fe–OH", "label": "Fe–OH", "centre": 2253.0,
         "window": [2230.0, 2310.0], "view": [2130.0, 2420.0]},
        {"key": "F5", "name": "CO₃/Mg–OH", "label": "CO$_3$/Mg–OH",
         "centre": 2335.0, "window": [2290.0, 2400.0], "view": [2200.0, 2470.0]},
    ],
    "ranges": [
        {"name": "可见—近红外  400 ~ 1000 nm", "span": [400.0, 1000.0]},
        {"name": "近红外  1000 ~ 1350 nm", "span": [1000.0, 1350.0]},
        {"name": "短波红外  1950 ~ 2500 nm", "span": [1950.0, 2500.0]},
    ],
}


def packaged_feature_path() -> str:
    """随包那份。打包之后是只读的，只当模板用。"""
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", FEATURE_FILE)
    return config.resource_path(source, "spectra", FEATURE_FILE)


def user_feature_path() -> str:
    """可写的那份。存在就以它为准。"""
    return os.path.join(config.app_data_dir(), FEATURE_FILE)


def _read_feature_doc() -> Dict[str, Any]:
    for path in (user_feature_path(), packaged_feature_path()):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("features"):
            return doc
    return _BUILTIN


def _parse_features(doc: Dict[str, Any]):
    """把 JSON 拆成三组常量。任何一项不合规就整份退回内置的那份。

    宁可全退也不逐条兜底：一份改了一半的定义比默认值更危险——图上标的窗口与
    算法实际用的对不上，而且不会报错。
    """
    try:
        gaps = tuple((float(a), float(b)) for a, b in doc["water_gaps"])
        features = tuple(
            Feature(str(f["key"]), str(f["name"]), str(f["label"]),
                    float(f["centre"]),
                    (float(f["window"][0]), float(f["window"][1])),
                    (float(f["view"][0]), float(f["view"][1])) if f.get("view")
                    else (float(f["window"][0]) - 80.0,
                          float(f["window"][1]) + 80.0))
            for f in doc["features"])
        ranges = tuple((str(r["name"]),
                        (float(r["span"][0]), float(r["span"][1])))
                       for r in doc.get("ranges", ()))
    except (KeyError, TypeError, ValueError, IndexError):
        if doc is _BUILTIN:
            raise
        return _parse_features(_BUILTIN)
    if not features:
        return _parse_features(_BUILTIN)
    return gaps, features, ranges


_GAPS, _FEATURES, _RANGES = _parse_features(_read_feature_doc())

#: 大气水汽强吸收区间（nm）。这两段的地表信息基本被水汽吃光，反演结果是噪声，
#: 论文里的反射率曲线一律断开而不是照画。
WATER_VAPOR_RANGES: Tuple[Tuple[float, float], ...] = _GAPS

#: 判读蚀变矿物时最先要看的就是这几处。
DIAGNOSTIC_FEATURES: Tuple[Feature, ...] = _FEATURES

#: 横轴快速定位的预设：(名字, 区间, 顺带高亮哪个判读窗口)。区间为 None 是全谱，
#: 高亮键为空串表示不高亮。看蚀变就那么几段，每次手输两个数太笨。
RANGE_PRESETS: Tuple[Tuple[str, Optional[Tuple[float, float]], str], ...] = (
    (("全谱", None, ""),)
    + tuple((name, span, "") for name, span in _RANGES)
    + tuple(("%s  %s  %.0f nm" % (f.key, f.name, f.centre), f.view, f.key)
            for f in _FEATURES))


def feature_for(key: str) -> Optional[Feature]:
    return next((f for f in DIAGNOSTIC_FEATURES if f.key == key), None)


def ensure_user_feature_file() -> str:
    """保证可写目录里有一份可以改的定义，返回它的路径。

    不存在就从随包那份复制过去；随包那份也读不到，就把内置的写出来。
    """
    path = user_feature_path()
    if os.path.isfile(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(packaged_feature_path(), "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = json.dumps(_BUILTIN, ensure_ascii=False, indent=2)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


#: 算区域平均谱时，一次读进内存的字节数上限。
#:
#: 决定按行分成多少块。给 128 MB：块太小则读的次数多、每次都有固定开销；块太大则
#: 峰值内存跟着涨，而 300 波段 × 几千列的一行就有十几 MB。
MASK_CHUNK_BYTES = 128 * 1024 * 1024

#: 判定"整条曲线恒为同一个值"的容差。ENVI 头里 bad bands 常常写成全 0 或全 -9999。
FLAT_TOLERANCE = 1e-12

#: 自动判断纵轴单位时，认为"这是反射率小数"的上限。
AUTO_REFLECTANCE_MAX = 1.6

# =============================================================================

gdal.UseExceptions()
osr.UseExceptions()


# -----------------------------------------------------------------------------
# 纵轴单位
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class YUnit:
    """纵轴的一种读法：中文菜单名 + 英文轴标题 + 换算系数。

    ``divisor`` 是"原值除以它"，因此百分比反射率的除数是 0.01。轴标题必须是
    英文——图是给论文用的。
    """

    key: str
    menu: str
    label: str
    divisor: float = 1.0
    #: 本来就是 0~1 反射率小数的谱（光谱库里的标样）该除多少才落到这根轴上。
    #:
    #: 它和 ``divisor`` 不是一回事。``divisor`` 回答的是"影像里存的原值怎么变成
    #: 轴上的数"，而库里的谱压根没经过那次放大——拿 10000 去除它，0.4 的反射率
    #: 会变成 0.00004，在图上就是贴着底的一条直线。三档反射率读法（原值、÷1000、
    #: ÷10000）显示出来的都是 0~1 的小数，所以库谱在这三档下都不用动；只有
    #: "显示成百分数"那一档要跟着乘 100。
    native_divisor: float = 1.0


AUTO_UNIT = YUnit("auto", "自动判断", "Value", 1.0)

Y_UNITS: Tuple[YUnit, ...] = (
    AUTO_UNIT,
    YUnit("refl", "反射率（原值）", "Reflectance", 1.0),
    YUnit("refl1e4", "反射率（原值 ÷ 10000）", "Reflectance", 10000.0),
    YUnit("refl1e3", "反射率（原值 ÷ 1000）", "Reflectance", 1000.0),
    YUnit("refl_pct", "反射率（%）", "Reflectance (%)", 0.01, 0.01),
    # 菜单里用 Unicode 上标（Qt 的界面字体都有），轴标题里用 mathtext——
    # 见 Feature 的说明，Arial 没有 ⁻² 这些字形。
    YUnit("radiance", "辐亮度（W m⁻² sr⁻¹ nm⁻¹）",
          "Radiance (W m$^{-2}$ sr$^{-1}$ nm$^{-1}$)", 1.0),
    YUnit("radiance_uw", "辐亮度（µW cm⁻² sr⁻¹ nm⁻¹）",
          "Radiance (µW cm$^{-2}$ sr$^{-1}$ nm$^{-1}$)", 1.0),
    YUnit("dn", "DN 值", "DN", 1.0),
)

_UNIT_BY_KEY: Dict[str, YUnit] = {u.key: u for u in Y_UNITS}


def unit_by_key(key: str) -> YUnit:
    return _UNIT_BY_KEY.get(key, AUTO_UNIT)


# -----------------------------------------------------------------------------
# 一条曲线
# -----------------------------------------------------------------------------

@dataclass(eq=False)
class Spectrum:
    """一个位置上的一条光谱。

    ``eq=False``：曲线是有身份的东西，不是值——图上两条同名的高岭石标样是两条
    曲线，不是一条。而且自动生成的 ``__eq__`` 会去比 numpy 数组，``list.index``
    一走到它就抛 "truth value of an array is ambiguous"。按同一性比才对。


    ``x`` 是波长（nm）或波段序号，由 ``x_is_wavelength`` 区分；``y`` 里的无效
    点是 NaN。两者等长，且 ``x`` 单调递增——上游若给了乱序波长，构造时排好。
    """

    label: str
    x: np.ndarray
    y: np.ndarray
    #: 每个点对应的波段号（1 起）。横轴切到"波段序号"时用它——不能现场用
    #: ``arange``：波长乱序的产品排过序，位置与波段号早就不是一一对应了。
    bands: Optional[np.ndarray] = None
    x_is_wavelength: bool = True
    lon: float = 0.0
    lat: float = 0.0
    pixel: Tuple[int, int] = (0, 0)
    window: int = 1
    layer_name: str = ""
    layer_path: str = ""
    raw_dtype: str = ""
    scaled: bool = False              # 是否已按 GDAL 的 scale/offset 换算过
    visible: bool = True              # 界面上那个勾。跟着曲线走，删掉别的不会串位
    #: 手工指定的颜色（"#RRGGBB"）。空串表示按配色轮到哪算哪。跟着曲线走，
    #: 前面删掉一条也不会把颜色串到别人身上。
    color: str = ""
    #: 这条谱是哪来的：``image`` 影像上取的、``library`` 光谱库里的、``csv``
    #: 外部导入的。只有 ``image`` 那种在画布上标得出点来——库里的标样没有经纬度，
    #: 照着 (0, 0) 标出去会在几内亚湾上留一个记号。
    origin: str = "image"
    #: 备注。库里的谱放它的出处与样品号，影像上取的留空。
    note: str = ""
    #: 这条谱占配色轮的第几格。**跟着曲线走，不跟着它在清单里排第几走。**
    #: 按位置算颜色的话，从光谱库里加一条进来，前面那些的名次一动，颜色就全变
    #: 了一遍——尤其是"当前那条"，它排在所有钉住的后面，每加一条就换一个颜色。
    #: -1 表示还没分配，进图的时候由窗口分配一个。
    slot: int = -1
    #: 这条谱的 ``y`` 本来是什么量纲。``"refl"`` 表示已经是 0~1 的反射率小数
    #: （光谱库里的标样都是），空串表示"影像里存的原值"，要按纵轴那个下拉换算。
    #: 两种谱画在同一张图上时，各按各的换——这是唯一能让它们对得上的做法。
    native_unit: str = ""

    @property
    def valid_count(self) -> int:
        return int(np.count_nonzero(np.isfinite(self.y)))

    def finite_range(self) -> Optional[Tuple[float, float]]:
        good = self.y[np.isfinite(self.y)]
        if good.size == 0:
            return None
        return (float(good.min()), float(good.max()))

    def coord_text(self) -> str:
        """图例里那半截坐标。经纬度写到小数点后四位，约合 10 m。"""
        return "%.4f°E, %.4f°N" % (self.lon, self.lat)

    def masked(self, ranges: Sequence[Tuple[float, float]]) -> np.ndarray:
        """把落进 ``ranges`` 的点挖成 NaN，返回新数组，不动原始数据。"""
        if not ranges or not self.x_is_wavelength:
            return self.y
        return mask_ranges(self.x, self.y, ranges)


def mask_ranges(x: np.ndarray, y: np.ndarray,
                ranges: Sequence[Tuple[float, float]]) -> np.ndarray:
    """挖掉若干横轴区间，返回新数组。

    挖成 NaN 而不是 0：0 在曲线上是一根到底的尖峰，还会把纵轴范围整个带偏。
    """
    out = np.asarray(y, dtype=np.float64).copy()
    for lo, hi in ranges:
        out[(np.asarray(x) >= lo) & (np.asarray(x) <= hi)] = np.nan
    return out


# -----------------------------------------------------------------------------
# 平滑
# -----------------------------------------------------------------------------

#: Savitzky-Golay 的窗口长度必须是奇数，且要大于多项式次数。上限给到 51：再宽
#: 就把 2200 nm 那种几十纳米宽的吸收峰一起抹平了，那是判读要看的东西。
SMOOTH_WINDOW_RANGE: Tuple[int, int] = (5, 51)
SMOOTH_ORDER_RANGE: Tuple[int, int] = (1, 5)
SMOOTH_DEFAULT_WINDOW = 9
SMOOTH_DEFAULT_ORDER = 2


def _savgol_matrix(window: int, order: int) -> np.ndarray:
    """窗口内的投影矩阵 ``P``：``P[i]`` 点乘窗口里的值，就是第 i 个位置的拟合值。

    中间那行是常说的 SG 系数；两端的点没有完整窗口，就用**第一个（最后一个）
    完整窗口的多项式**在该位置上的取值，与 scipy 的 ``mode="interp"`` 一致。
    比补零、比镜像都好：补零会把端点往下拽，镜像会在端点造出一个假的对称峰。
    """
    half = window // 2
    t = np.arange(-half, half + 1, dtype=np.float64)
    design = np.vander(t, order + 1, increasing=True)
    pinv = np.linalg.pinv(design)
    return design @ pinv


def savgol(y: np.ndarray, window: int = SMOOTH_DEFAULT_WINDOW,
           order: int = SMOOTH_DEFAULT_ORDER) -> np.ndarray:
    """Savitzky-Golay 平滑，返回新数组。

    在每个窗口里做一次最小二乘多项式拟合，取拟合值当平滑结果。它比滑动平均好的
    地方正在于此：滑动平均会把峰压矮、把峰位往两边推，而多项式拟合保留极值的
    高度与位置——判读吸收特征时，峰深和峰位就是结论本身。

    **按点数等间距处理，不看波长。** 高光谱的波长间隔本来就近似均匀，SG 也是这么
    定义的；真要按不等间距做，得逐点解一次加权最小二乘，慢几百倍，换来的差别在
    这里看不出来。

    NaN 处理：把曲线切成若干段连续有效值，逐段平滑，短于窗口的段原样返回。跨过
    水汽断口去平滑会把断口两侧的值搅在一起，那是无中生有。
    """
    values = np.asarray(y, dtype=np.float64)
    window = int(window)
    order = int(order)
    if window % 2 == 0:
        window += 1
    window = max(SMOOTH_WINDOW_RANGE[0], min(SMOOTH_WINDOW_RANGE[1], window))
    order = max(SMOOTH_ORDER_RANGE[0], min(SMOOTH_ORDER_RANGE[1], order))
    if order >= window:
        order = window - 1

    out = values.copy()
    proj = _savgol_matrix(window, order)
    half = window // 2
    for lo, hi in _finite_runs(values):
        n = hi - lo
        if n < window:
            continue
        seg = values[lo:hi]
        # 中间：一次卷积搞定，卷积核就是投影矩阵的中间那行（要翻转）
        centre = proj[half][::-1]
        body = np.convolve(seg, centre, mode="valid")
        out[lo + half:hi - half] = body
        # 两端：用头尾那个完整窗口的拟合值
        out[lo:lo + half] = proj[:half] @ seg[:window]
        out[hi - half:hi] = proj[half + 1:] @ seg[-window:]
    return out


def _finite_runs(values: np.ndarray) -> List[Tuple[int, int]]:
    """有效值的连续区段 ``[起, 止)``。"""
    good = np.isfinite(values)
    if not good.any():
        return []
    edges = np.flatnonzero(np.diff(good.astype(np.int8)))
    starts = [0] if good[0] else []
    ends: List[int] = []
    for e in edges:
        (ends if good[e] else starts).append(int(e) + 1)
    if good[-1]:
        ends.append(good.size)
    return list(zip(starts, ends))


# -----------------------------------------------------------------------------
# 取谱
# -----------------------------------------------------------------------------

class SpectrumReader:
    """绑定在一个影像图层上的取谱器。

    构造时把逆仿射、坐标变换、每个波段的 nodata 与 scale/offset 全部算好并缓存：
    鼠标每点一下就要读一条谱，把这些放在 :meth:`read` 里每次重算，在 300 波段的
    影像上是看得见的卡顿。
    """

    def __init__(self, layer) -> None:
        self.layer = layer
        self.name = getattr(layer, "name", "")
        self.path = getattr(layer, "path", "")
        self.ds = getattr(layer, "ds", None)
        self.band_count = int(getattr(layer, "band_count", 0) or 0)
        self.error = ""
        self._inv = None
        self._tf = None
        self._nodata: List[Optional[float]] = []
        self._scale: List[float] = []
        self._offset: List[float] = []
        self._scaled = False
        self._prepare()

    # ------------------------------------------------------------- 准备
    def _prepare(self) -> None:
        ds = self.ds
        if ds is None or self.band_count < 1:
            self.error = "图层没有可读的影像数据。"
            return
        gt = ds.GetGeoTransform(can_return_null=True)
        if gt is None:
            self.error = "影像没有地理变换，无法按坐标定位像元。"
            return
        inv = gdal.InvGeoTransform(gt)
        if inv is None:
            self.error = "影像的地理变换不可逆，无法按坐标定位像元。"
            return
        self._inv = inv

        wkt = ds.GetProjection()
        if wkt:
            dst = osr.SpatialReference()
            if dst.ImportFromWkt(wkt) == 0:
                dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                src = osr.SpatialReference()
                src.ImportFromEPSG(4326)
                src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                if not src.IsSame(dst):
                    try:
                        self._tf = osr.CoordinateTransformation(src, dst)
                    except Exception as exc:          # pragma: no cover - PROJ 环境
                        self.error = "坐标变换建立失败：%s" % exc
                        return

        for i in range(1, self.band_count + 1):
            band = ds.GetRasterBand(i)
            self._nodata.append(band.GetNoDataValue())
            scale = band.GetScale()
            offset = band.GetOffset()
            scale = 1.0 if scale in (None, 0.0) else float(scale)
            offset = 0.0 if offset is None else float(offset)
            self._scale.append(scale)
            self._offset.append(offset)
            if scale != 1.0 or offset != 0.0:
                self._scaled = True

    @property
    def ready(self) -> bool:
        return not self.error and self._inv is not None

    # ------------------------------------------------------------- 横轴
    def axis(self) -> Tuple[np.ndarray, bool]:
        """横轴取值与"它是不是波长"。

        没有波长表时退回波段序号：曲线的形状照样有意义，只是横轴读数换了。
        """
        waves = list(getattr(self.layer, "wavelengths", None) or [])
        if len(waves) == self.band_count and self.band_count > 0:
            return (np.asarray(waves, dtype=np.float64), True)
        return (np.arange(1, self.band_count + 1, dtype=np.float64), False)

    def x_label(self) -> str:
        return "Wavelength (nm)" if self.axis()[1] else "Band number"

    # ------------------------------------------------------------- 定位
    def locate(self, lon: float, lat: float) -> Optional[Tuple[int, int]]:
        """经纬度 → 行列号。落在影像外返回 None。"""
        if not self.ready:
            return None
        x, y = float(lon), float(lat)
        if self._tf is not None:
            try:
                x, y, *_ = self._tf.TransformPoint(x, y)
            except Exception:
                return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        inv = self._inv
        col = inv[0] + x * inv[1] + y * inv[2]
        row = inv[3] + x * inv[4] + y * inv[5]
        if not (math.isfinite(col) and math.isfinite(row)):
            return None
        c, r = int(math.floor(col)), int(math.floor(row))
        if c < 0 or r < 0 or c >= self.ds.RasterXSize or r >= self.ds.RasterYSize:
            return None
        return (c, r)

    def to_pixel(self, lon: float, lat: float) -> Optional[Tuple[float, float]]:
        """经纬度 → 浮点行列号，**不裁到影像范围内**。

        与 :meth:`locate` 的区别在这里：栅格化一个图斑时，落在影像外的顶点也得
        照原样参与，裁掉的话多边形的形状就变了，剪出来的是另一块地方。
        """
        if self._inv is None:
            return None
        x, y = float(lon), float(lat)
        if self._tf is not None:
            try:
                x, y, *_ = self._tf.TransformPoint(x, y)
            except Exception:
                return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        inv = self._inv
        col = inv[0] + x * inv[1] + y * inv[2]
        row = inv[3] + x * inv[4] + y * inv[5]
        if not (math.isfinite(col) and math.isfinite(row)):
            return None
        return (col, row)

    @property
    def size(self) -> Tuple[int, int]:
        return (int(self.ds.RasterXSize), int(self.ds.RasterYSize)) \
            if self.ds is not None else (0, 0)

    # ------------------------------------------------------------- 读谱
    def read(self, lon: float, lat: float, window: int = 1,
             label: str = "") -> Optional[Spectrum]:
        """读一条谱。点在影像外、或整条都是无效值时返回 None。"""
        if not self.ready:
            return None
        at = self.locate(lon, lat)
        if at is None:
            return None
        col, row = at

        half = max(0, (int(window) - 1) // 2)
        x0 = max(0, col - half)
        y0 = max(0, row - half)
        x1 = min(self.ds.RasterXSize, col + half + 1)
        y1 = min(self.ds.RasterYSize, row + half + 1)
        try:
            block = self.ds.ReadAsArray(x0, y0, x1 - x0, y1 - y0)
        except RuntimeError:
            return None
        if block is None:
            return None
        block = np.asarray(block)
        raw_dtype = str(block.dtype)
        if block.ndim == 2:                        # 单波段影像
            block = block[np.newaxis, :, :]
        values = self._reduce(block)
        if values is None:
            return None

        x, is_wave = self.axis()
        n = min(len(x), values.size)
        x, values = x[:n], values[:n]
        bands = np.arange(1, n + 1, dtype=np.float64)
        # 波长乱序时（拼接过 VNIR/SWIR 的产品常见）按波长排一遍，否则曲线会
        # 在接缝处来回折返。
        if is_wave and n > 1 and not np.all(np.diff(x) > 0):
            order = np.argsort(x, kind="stable")
            x, values, bands = x[order], values[order], bands[order]

        if not np.any(np.isfinite(values)):
            return None
        return Spectrum(
            label=label or "P1",
            x=np.ascontiguousarray(x),
            y=np.ascontiguousarray(values),
            bands=np.ascontiguousarray(bands),
            x_is_wavelength=is_wave,
            lon=float(lon), lat=float(lat),
            pixel=(col, row), window=int(window),
            layer_name=self.name, layer_path=self.path,
            raw_dtype=raw_dtype, scaled=self._scaled)

    def read_mask(self, mask: np.ndarray, origin: Tuple[int, int] = (0, 0),
                  label: str = "", progress=None) -> Optional[Spectrum]:
        """一片像元的平均谱。``mask`` 是布尔数组，``origin`` 是它左上角的行列号。

        **按行分块，每块一次读全部波段。**这一条是这个函数的全部要害。逐波段读整
        幅看起来省内存，实际上对 BIP 交错的影像是灾难：一个波段的数据在文件里是
        每个像元一小段地散着的，读一个波段就要把整个文件扫一遍，读 166 个波段就
        是扫 166 遍。实测 400 MB 的 BIP 影像，逐波段读 13.4 秒，按行分块读全波段
        0.1 秒——**快 134 倍**，而且 BSQ、BIL 上也不比逐波段慢。使用者遇到的"卡死"
        就是这个：一幅几 GB 的 BIP 影像，逐波段读要几十分钟，界面全程没有响应。

        既然一遍就能读完，就没有必要抽样了：所有像元都参与平均，结果是精确的。
        掩膜里一行都没有的块直接跳过，一个小图斑在大影像上只读它自己那几块。

        ``progress(已完成块数, 总块数)`` 返回 False 就中断，返回 None。
        """
        if not self.ready or mask is None:
            return None
        mask = np.asarray(mask, dtype=bool)
        if mask.ndim != 2 or not mask.any():
            return None
        x0, y0 = int(origin[0]), int(origin[1])
        height, width = mask.shape
        if x0 < 0 or y0 < 0 or x0 + width > self.ds.RasterXSize \
                or y0 + height > self.ds.RasterYSize:
            return None

        bands = self.band_count
        rows = max(1, int(MASK_CHUNK_BYTES / max(1, bands * width * 8)))
        # 只读掩膜里真的有像元的那些块
        blocks = [r for r in range(0, height, rows)
                  if mask[r:r + rows].any()]
        if not blocks:
            return None

        total = np.zeros(bands, dtype=np.float64)
        square = np.zeros(bands, dtype=np.float64)
        count = np.zeros(bands, dtype=np.int64)
        raw_dtype = ""
        for done, r in enumerate(blocks, 1):
            h = min(rows, height - r)
            try:
                block = self.ds.ReadAsArray(x0, y0 + r, width, h)
            except RuntimeError:
                return None
            if block is None:
                return None
            block = np.asarray(block)
            if block.ndim == 2:                    # 单波段影像
                block = block[np.newaxis, :, :]
            if not raw_dtype:
                raw_dtype = str(block.dtype)
            picked = block[:, mask[r:r + h]].astype(np.float64)
            good = np.isfinite(picked)
            for i in range(min(bands, len(self._nodata))):
                nd = self._nodata[i]
                if nd is not None:
                    good[i] &= picked[i] != nd
            picked[~good] = 0.0
            total += picked.sum(axis=1)
            square += (picked * picked).sum(axis=1)
            count += good.sum(axis=1)
            if progress is not None and not progress(done, len(blocks)):
                return None

        used = int(count.max()) if count.size else 0
        if used == 0:
            return None
        with np.errstate(invalid="ignore", divide="ignore"):
            values = np.where(count > 0, total / np.maximum(count, 1), np.nan)
            spread = np.where(count > 0,
                              square / np.maximum(count, 1) - values ** 2, np.nan)
        values = self._calibrate(values)
        if values is None:
            return None

        x, is_wave = self.axis()
        n = min(len(x), values.size)
        x, values = x[:n], values[:n]
        bands_axis = np.arange(1, n + 1, dtype=np.float64)
        if is_wave and n > 1 and not np.all(np.diff(x) > 0):
            order = np.argsort(x, kind="stable")
            x, values, bands_axis = x[order], values[order], bands_axis[order]
        if not np.any(np.isfinite(values)):
            return None

        with np.errstate(invalid="ignore"):
            sigma = float(np.nanmean(np.sqrt(np.maximum(spread, 0.0))))
        note = "%d 个像元的平均谱" % used
        if np.isfinite(sigma):
            note += "，各波段标准差平均 %.4g" % sigma
        return Spectrum(
            label=label or "区域均值",
            x=np.ascontiguousarray(x), y=np.ascontiguousarray(values),
            bands=np.ascontiguousarray(bands_axis), x_is_wavelength=is_wave,
            origin="region", window=1,
            layer_name=self.name, layer_path=self.path,
            raw_dtype=raw_dtype, scaled=self._scaled, note=note)

    def _calibrate(self, values: np.ndarray) -> Optional[np.ndarray]:
        """定标系数与坏波段处理。与 :meth:`_reduce` 后半段是同一套规矩。"""
        values = np.asarray(values, dtype=np.float64).copy()
        for i in range(min(values.size, len(self._scale))):
            if self._scale[i] != 1.0 or self._offset[i] != 0.0:
                values[i] = values[i] * self._scale[i] + self._offset[i]
        flat = np.isclose(values, 0.0, atol=FLAT_TOLERANCE)
        if np.all(flat | ~np.isfinite(values)):
            return None
        values[flat] = np.nan
        return values

    def _reduce(self, block: np.ndarray) -> Optional[np.ndarray]:
        """窗口 → 每个波段一个数。无效值不参与平均。"""
        bands = block.shape[0]
        data = block.astype(np.float64, copy=True)
        good = np.isfinite(data)
        for i in range(min(bands, len(self._nodata))):
            nd = self._nodata[i]
            if nd is not None:
                good[i] &= data[i] != nd
        data[~good] = np.nan
        with np.errstate(invalid="ignore"):
            values = np.nanmean(data.reshape(bands, -1), axis=1)

        for i in range(min(bands, len(self._scale))):
            if self._scale[i] != 1.0 or self._offset[i] != 0.0:
                values[i] = values[i] * self._scale[i] + self._offset[i]

        # 恒零 / 恒定值的坏波段：整条谱都这样才算真无效，只有个别波段这样
        # 是正常的（定标增益为 0 的通道），把它们挖成 NaN。
        flat = np.isclose(values, 0.0, atol=FLAT_TOLERANCE)
        if np.all(flat | ~np.isfinite(values)):
            return None
        values[flat] = np.nan
        return values

    # ------------------------------------------------------------- 单位
    def guess_unit(self, spec: Optional[Spectrum]) -> YUnit:
        return guess_unit(spec)


# -----------------------------------------------------------------------------
# 导出
# -----------------------------------------------------------------------------

def guess_unit(spec: Optional[Spectrum]) -> YUnit:
    """按数值范围与原始数据类型猜纵轴单位。

    猜错不会有任何后果——界面上单位是个下拉，猜的结果只是它的默认项，而且下拉里
    写着"自动判断"，使用者一眼能看出这是个猜测。

    放在模块级而不是挂在取谱器上：光谱库里的谱不经过取谱器，但它同样要猜——图上
    只摆着几条 0~1 的标准反射率谱，纵轴却写着 DN，那是明摆着的错。
    """
    if spec is None:
        return unit_by_key("dn")
    rng = spec.finite_range()
    if rng is None:
        return unit_by_key("dn")
    lo, hi = rng
    if spec.scaled or (hi <= AUTO_REFLECTANCE_MAX and lo >= -0.5
                       and "int" not in spec.raw_dtype):
        return unit_by_key("refl")
    if "float" not in spec.raw_dtype and 1.0 < hi <= 12000.0:
        # 整数存的反射率按惯例放大一万倍。范围对得上就照这个读，对不上
        # （比如上万的辐亮度）就退回 DN。
        return unit_by_key("refl1e4")
    return unit_by_key("dn")


def to_csv(path: str, specs: Sequence[Spectrum], unit: YUnit = AUTO_UNIT,
           gaps: Sequence[Tuple[float, float]] = ()) -> None:
    """把若干条谱写成一张宽表：第一列横轴，其后每条谱一列。

    横轴不一致时（不同传感器的谱画在一起）按并集对齐，缺的地方留空——留空而
    不是补零，是为了让 Excel、Origin 这些工具把它当"没有数据"而不是"值是 0"。
    """
    if not specs:
        raise ValueError("没有可导出的曲线。")
    axis_name = ("Wavelength (nm)" if all(s.x_is_wavelength for s in specs)
                 else "Band")
    grid = np.unique(np.concatenate([s.x for s in specs]))
    columns = []
    for s in specs:
        y = s.masked(gaps)
        if unit.divisor not in (0.0, 1.0):
            y = y / unit.divisor
        lookup = dict(zip(s.x.tolist(), y.tolist()))
        columns.append([lookup.get(v) for v in grid.tolist()])

    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(axis_name + "," + ",".join(_csv_cell(s.label) for s in specs) + "\n")
        for i, xv in enumerate(grid.tolist()):
            cells = ["" if (c[i] is None or not math.isfinite(c[i]))
                     else "%.6g" % c[i] for c in columns]
            fh.write("%.6g,%s\n" % (xv, ",".join(cells)))


def _csv_cell(text: str) -> str:
    text = (text or "").replace('"', '""')
    return '"%s"' % text if any(ch in text for ch in ',"\n') else text


def from_csv(path: str) -> List[Spectrum]:
    """读回一份本程序导出的 CSV：第一列横轴，其后每条曲线一列。

    留空的格子读成 NaN 而不是 0——导出时它表示"这条谱在这个波长上没有数据"，
    读回来必须还是这个意思。
    """
    with open(path, "r", encoding="utf-8-sig") as fh:
        rows = [line.rstrip("\n").rstrip("\r") for line in fh if line.strip()]
    if len(rows) < 2:
        raise ValueError("这个 CSV 里没有数据行。")
    header = _split_csv(rows[0])
    if len(header) < 2:
        raise ValueError("这个 CSV 只有一列，看不出哪列是曲线。")
    is_wave = "wavelength" in header[0].lower()

    axis: List[float] = []
    columns: List[List[float]] = [[] for _ in header[1:]]
    for line in rows[1:]:
        cells = _split_csv(line)
        if len(cells) < 2:
            continue
        try:
            axis.append(float(cells[0]))
        except ValueError:
            continue
        for i in range(len(columns)):
            raw = cells[i + 1].strip() if i + 1 < len(cells) else ""
            try:
                columns[i].append(float(raw))
            except ValueError:
                columns[i].append(float("nan"))
    if not axis:
        raise ValueError("这个 CSV 的第一列不是数字，读不出横轴。")

    x = np.asarray(axis, dtype=np.float64)
    bands = np.arange(1, x.size + 1, dtype=np.float64)
    # 波长轴按升序排：有的仪器导出是降序的，图照样画得出来，但光标找最近曲线、
    # 横轴范围这些都按"x 单调递增"算。波段序号跟着原来的行号走，不重编。
    order = (np.argsort(x, kind="stable") if is_wave and x.size > 1
             and np.any(np.diff(x) < 0) else None)
    if order is not None:
        x, bands = x[order], bands[order]
    out = []
    for name, values in zip(header[1:], columns):
        y = np.asarray(values, dtype=np.float64)
        if order is not None:
            y = y[order]
        if not np.any(np.isfinite(y)):
            continue
        out.append(Spectrum(label=name or "curve", x=x, y=y, bands=bands,
                            x_is_wavelength=is_wave, origin="csv",
                            layer_name=os.path.basename(path)))
    if not out:
        raise ValueError("这个 CSV 里没有可用的曲线。")
    return out


def _split_csv(line: str) -> List[str]:
    """按逗号切一行，认双引号包起来的字段。"""
    out, cell, quoted = [], [], False
    i = 0
    while i < len(line):
        ch = line[i]
        if quoted:
            if ch == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    cell.append('"')
                    i += 1
                else:
                    quoted = False
            else:
                cell.append(ch)
        elif ch == '"':
            quoted = True
        elif ch == ",":
            out.append("".join(cell))
            cell = []
        else:
            cell.append(ch)
        i += 1
    out.append("".join(cell))
    return [c.strip() for c in out]


def default_export_name(specs: Sequence[Spectrum], suffix: str) -> str:
    """给导出对话框一个像样的默认文件名。"""
    stem = "spectra"
    for s in specs:
        if s.layer_path:
            stem = os.path.splitext(os.path.basename(s.layer_path))[0]
            break
        if s.layer_name:
            stem = s.layer_name
            break
    return "%s_spectra%s" % (stem, suffix)
