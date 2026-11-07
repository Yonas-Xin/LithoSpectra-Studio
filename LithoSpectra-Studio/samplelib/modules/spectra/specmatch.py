# -*- coding: utf-8 -*-
"""光谱匹配：拿影像上取的一条谱，去库里找最像的。

要点
----
**先对齐，再比。** 影像的波段中心和地面光谱仪的采样点从来对不上（ZY1-02D 是
一百六十多个波段，USGS 那套是 2151 个采样点），所以第一步一律把库谱对齐到
影像的波长上。反过来把影像插到库上不行：那是无中生有，而且比一次要算五千遍。

**对齐时分辨率要降到同一档。** 影像的一个波段测到的是一段波长的加权平均，
窄吸收峰在上面被展宽、变浅；1 nm 的库谱只在波段中心取一个点，峰就比实测的又深
又尖，窄峰矿物平白被扣分。所以谁细就把谁降下来：库谱比查询谱细，先按高斯响应
卷积再取样；查询谱比库谱细，把查询谱平滑到库谱那一档。没有 FWHM 元数据可用，
分辨率按局部采样间隔估计，卷积宽度取两者平方差的平方根（自己本来那份宽度不再
重复叠加）。

**只比两边都覆盖的那一段，而且包络线也在这一段上求。** 库谱只覆盖查询谱的一部分
时，凸包要在重叠段上重新求——查询谱在全段求、库谱在自己那段求，两条基线不同，
完全相同的两条谱也会差出二十度。默认更进一步：库谱必须完整覆盖比较范围（端点
留一点容差，内部有效波段不少于 :data:`FULL_COVER_VALID`）才进榜，否则排名比的
不是同一段东西。被挡掉的条数报告出来，不静悄悄地消失。

**比之前先想清楚比的是什么。** 直接比原始反射率，比出来的第一名往往只是"亮度
差不多"；矿物判读真正要比的是**吸收特征的形状**。所以默认打开包络线去除
（continuum removal）：先把曲线的上凸包当基线除掉，剩下的就是纯吸收深度与位置，
两条亮度差三倍的同种矿物谱这时才对得上。

**水汽段要剔掉。** 1400 与 1900 nm 附近影像上是噪声，留着它们会主导相似度。
默认按 :data:`spectra.WATER_VAPOR_RANGES` 挖掉。

算法
----
四个都是遥感里通行的相似性度量，各有各的脾气：

* **SAM**（光谱角）——把两条谱当高维向量算夹角。**与整体亮度无关**，因此地形
  阴影、照度差异都不影响它，是最稳的默认选择。
* **SCM**（相关系数）——先各自减去均值再算余弦，等价于 Pearson 相关。它比 SAM
  多扣掉一个常数偏移，因而对加性偏差（残余程辐射之类）更钝；代价是**连吸收深浅
  也一并扣掉了**——把包络线去除后的谱吸收深度减半，SCM 仍然给 1.0000，SAM 会
  给 0.037 rad。想区分"同一种矿物含量不同"就别用它。
* **SID**（光谱信息散度）——把归一化后的谱当概率分布，算对称的 KL 散度。对
  吸收特征的**相对深度**比 SAM 敏感，代价是对噪声也更敏感。
* **SID·tan(SAM)**——两者相乘，把"角度像"和"分布像"同时要求上，区分度最高，
  Du 等人 (2004) 提出后在矿物填图里用得很多。
**这里没有欧氏距离，是故意的。** 归一化之后的欧氏距离恰好是 ``2 sin(SAM/2)``，
与 SAM 单调等价，名次一个不差；改成除以均值再算均方根差也一样——拿全库
1825 条矿物谱实测，两者名次的 Spearman 相关是 0.99999，在"同形状、吸收深度减半"
的构造样本上也给出同一个数（0.0370 与 0.0371）。多列一个只会让人以为多了一种
判断依据。不归一化的原始欧氏距离倒是真的不同，但它比的是绝对亮度——地形、大气、
颗粒度随便一样都能把它带偏，在遥感里没人拿它下结论。

得分怎么来
----------
四个度量量纲不一样（角度、相关系数、散度……），排名可以直接比，但"0.87"到底
算好还是不好没法横着看。所以每个都折算成 0~1 的得分，大的好。

折算不是随手挑的：两条谱差别很小时，SID 大致按夹角的**平方**增长，
SID·tan(SAM) 按**立方**增长（tan 一次、SID 两次）。所以先给它们开二次、三次
方根还原成"等效夹角"，再套 SAM 那个折算式。这样四个算法的得分才落在同一个量级
上，不至于 SID 那一栏前二十名全是 100.0。开方是单调的，排名不受影响。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import spectra
from .speclib import Entry, Library

# =============================================================================
# 配置区
# =============================================================================

#: 参与比较的有效波段数下限。少于这些点，相似度就是几个点的巧合。
#:
#: 12 是从"只比一个诊断吸收窗口"这件事倒推的：2100~2300 nm 这段 Al-OH 窗口，
#: 在一景 166 波段的高光谱上只有十几个波段。定到 20 就等于把"盯着一个吸收峰比"
#: 这个用法整个封死了，而那恰恰是判读蚀变矿物最常做的一件事。
MIN_BANDS = 12

#: 有效波段数还得占查询谱的这个比例，否则不算——只在边角上重叠一小段的谱，
#: 算出来的角度很小，排名却能冲到前面，是最常见的假第一名。
MIN_COVERAGE = 0.3

#: SID 归一化时给零值垫的下限，免得 log(0)。
SID_EPS = 1e-8

#: 包络线去除后，基线接近 0 的地方比值会炸。基线小于谱线最大值的这个比例时，
#: 该点直接作废。
HULL_FLOOR = 1e-3

#: 「完整覆盖」的端点容差（nm），默认值与可调上限。只吸收仪器量程的自然差异
#: （影像到 2510、库到 2500 这种）；放到 100 nm 以上就会把 2300~2500 nm 的碳酸盐、
#: Mg-OH 吸收整段漏掉，那正是这个开关要排除的情况。想不要两端就设比较范围。
EDGE_TOLERANCE = 20.0
EDGE_TOLERANCE_MAX = 50.0
#: 端点容差至少放到查询谱这么多个波段间隔——波段很疏时差一个波段是常态。
EDGE_TOLERANCE_BANDS = 2.0

#: 「完整覆盖」还要求库谱在自身范围内的有效波段不少于这个比例。只数内部缺口，
#: 端点另算；合在一起算的话 5% 的余量在 400~2500 nm 上就是一百来纳米，全落在
#: 一端也照样过关。
FULL_COVER_VALID = 0.95

#: 卷积时高斯核截到几倍 σ。
SRF_SIGMA_SPAN = 3.0
#: 卷积出的点，核内有效权重少于这个比例就作废——核里大半是 NaN 时算出来的是
#: 边上几个点的值，不是这个波段的。
SRF_VALID = 0.8

# =============================================================================


@dataclass(frozen=True)
class Algorithm:
    key: str
    cn: str
    #: 原始度量的显示名与格式
    metric_name: str
    metric_fmt: str
    #: 度量 -> 0~1 得分
    score: Callable[[np.ndarray], np.ndarray]
    tip: str


def _score_sam(v: np.ndarray) -> np.ndarray:
    """夹角折算成得分：0 rad 满分，π/2（正交）零分。"""
    return np.clip(1.0 - v / (math.pi / 2.0), 0.0, 1.0)


def _score_root(power: float):
    """先开方还原成等效夹角，再按 SAM 折算。见模块开头「得分怎么来」。"""
    def inner(v: np.ndarray) -> np.ndarray:
        return _score_sam(np.power(np.maximum(v, 0.0), power))
    return inner


ALGORITHMS: Tuple[Algorithm, ...] = (
    Algorithm("sam", "光谱角 SAM", "角度", "%.2f°", _score_sam,
              "两条谱的夹角，与整体亮度无关。默认用它。"),
    Algorithm("scm", "相关系数 SCM", "相关系数", "%.4f",
              lambda v: (v + 1.0) / 2.0,
              "去均值后的余弦，等价于 Pearson 相关。对常数偏移不敏感。"),
    Algorithm("sid", "光谱信息散度 SID", "散度", "%.5f", _score_root(0.5),
              "把谱当概率分布算对称 KL 散度。对吸收深度比 SAM 敏感。"),
    Algorithm("sidsam", "SID×tan(SAM)", "度量", "%.6f", _score_root(1.0 / 3.0),
              "SID 与 SAM 正切之积，区分度最高，也最挑数据质量。"),
)

ALGORITHM_BY_KEY: Dict[str, Algorithm] = {a.key: a for a in ALGORITHMS}
DEFAULT_ALGORITHM = "sam"


@dataclass
class MatchOptions:
    algorithm: str = DEFAULT_ALGORITHM
    #: 先做包络线去除再比。矿物判读默认开。
    continuum: bool = True
    #: 只在这段波长里比。None = 用查询谱的全部有效范围。
    span: Optional[Tuple[float, float]] = None
    #: 挖掉水汽强吸收段
    mask_water: bool = True
    min_bands: int = MIN_BANDS
    min_coverage: float = MIN_COVERAGE
    #: 只留完整覆盖比较范围的库谱。关掉后部分重叠的也比（按 ``min_coverage`` 把关）。
    full_cover: bool = True
    #: 完整覆盖的端点容差（nm），实际取它与 ``EDGE_TOLERANCE_BANDS`` 个波段间隔中大的那个
    edge_tolerance: float = EDGE_TOLERANCE


@dataclass
class Hit:
    entry: Entry
    score: float
    metric: float
    bands: int
    #: 实际比较的波长段与它占查询谱有效波段的比例
    lo: float = float("nan")
    hi: float = float("nan")
    coverage: float = 1.0

    def score_text(self) -> str:
        return "%.1f" % (100.0 * self.score)

    def __post_init__(self) -> None:
        if not np.isfinite(self.score):
            object.__setattr__(self, "score", 0.0)


# -----------------------------------------------------------------------------
# 预处理
# -----------------------------------------------------------------------------

def prepare_query(x: np.ndarray, y: np.ndarray,
                  options: MatchOptions) -> Tuple[np.ndarray, np.ndarray]:
    """把查询谱裁成"要比的那一段"，返回 (波长, 值)，都已去掉无效点。"""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    keep = np.isfinite(x) & np.isfinite(y)
    if options.span:
        lo, hi = sorted(options.span)
        keep &= (x >= lo) & (x <= hi)
    if options.mask_water:
        for lo, hi in spectra.WATER_VAPOR_RANGES:
            keep &= ~((x >= lo) & (x <= hi))
    x, y = x[keep], y[keep]
    order = np.argsort(x)
    return (x[order], y[order])


def resample(src_x: np.ndarray, src_y: np.ndarray,
             dst_x: np.ndarray) -> np.ndarray:
    """把一批谱（``src_y`` 每行一条）线性插值到 ``dst_x`` 上。

    落在源波长范围外的点给 NaN——外推出来的值没有依据，拿去算相似度就是拿噪声
    充数。相邻两点里只要有一个是 NaN，插出来也是 NaN，这正是想要的：坏波段周围
    一个点的宽度内都不该信。
    """
    src_y = np.atleast_2d(np.asarray(src_y, dtype=np.float64))
    n = src_x.size
    idx = np.clip(np.searchsorted(src_x, dst_x, side="left"), 1, n - 1)
    x0, x1 = src_x[idx - 1], src_x[idx]
    span = np.where(x1 > x0, x1 - x0, 1.0)
    w = (dst_x - x0) / span
    out = src_y[:, idx - 1] * (1.0 - w) + src_y[:, idx] * w
    outside = (dst_x < src_x[0]) | (dst_x > src_x[-1])
    if outside.any():
        out[:, outside] = np.nan
    return out


def spacing(x: np.ndarray) -> np.ndarray:
    """每个采样点的局部间隔：取左右两个间距里小的那个。

    取小的是为了水汽段、探测器接缝那种缺口：缺口两边的点按缺口宽度算分辨率，
    会把它们当成几百纳米宽的波段。
    """
    x = np.asarray(x, dtype=np.float64)
    if x.size < 2:
        return np.ones(x.size)
    d = np.diff(x)
    return np.minimum(np.concatenate([d[:1], d]), np.concatenate([d, d[-1:]]))


def convolve(src_x: np.ndarray, src_y: np.ndarray, dst_x: np.ndarray,
             fwhm: np.ndarray) -> np.ndarray:
    """按高斯光谱响应（每个目标点各自的 FWHM）把一批谱卷积到 ``dst_x`` 上。

    NaN 不参与加权；核内有效权重不足 :data:`SRF_VALID` 的点、落在源波长范围外的
    点给 NaN。
    """
    src_y = np.atleast_2d(np.asarray(src_y, dtype=np.float64))
    sigma = np.maximum(np.asarray(fwhm, dtype=np.float64), 1e-9) / (2.0 * math.sqrt(2.0 * math.log(2.0)))
    # 核只有 ±SRF_SIGMA_SPAN·σ 宽，按窗口取源点，不拼整张 目标×源 的权重矩阵——
    # 1 nm 的查询谱自己平滑一遍，整张就是两千乘两千
    reach = SRF_SIGMA_SPAN * sigma
    lo = np.searchsorted(src_x, dst_x - reach, side="left")
    hi = np.searchsorted(src_x, dst_x + reach, side="right")
    width = max(int((hi - lo).max()) if hi.size else 0, 1)
    pos = lo[:, None] + np.arange(width)[None, :]
    inside = pos < hi[:, None]
    pos = np.minimum(pos, src_x.size - 1)
    dx = (dst_x[:, None] - src_x[pos]) / sigma[:, None]
    w = np.where(inside, np.exp(-0.5 * dx * dx), 0.0)
    vals = src_y[:, pos]
    good = np.isfinite(vals)
    num = (np.where(good, vals, 0.0) * w[None]).sum(2)
    have = (good * w[None]).sum(2)
    # 核伸出源波长范围的那一截也算"缺"：端点附近只剩半个核，加权出来是偏向一侧的
    # 平均，不是这个波段该有的值
    erf = np.frompyfunc(math.erf, 1, 1)
    root2 = math.sqrt(2.0)
    beyond = (0.5 * (1.0 + erf((src_x[0] - dst_x) / (sigma * root2)).astype(np.float64))
              + 0.5 * (1.0 - erf((src_x[-1] - dst_x) / (sigma * root2)).astype(np.float64)))
    total = (w.sum(1) / np.maximum(1.0 - beyond, 1e-9))[None, :]
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(have >= SRF_VALID * total, num / have, np.nan)
    outside = (dst_x < src_x[0]) | (dst_x > src_x[-1])
    out[:, outside] = np.nan
    return out


def align(src_x: np.ndarray, src_y: np.ndarray, dst_x: np.ndarray,
          src_res: np.ndarray, dst_res: np.ndarray) -> np.ndarray:
    """把一批谱对齐到 ``dst_x``，分辨率降到目标那一档。

    ``src_res``、``dst_res`` 是两边在 ``dst_x`` 各点上的分辨率（nm）。目标比源粗
    多少就补卷积多少（平方差开方）；补的宽度还不到源的一个采样间隔时卷积没有
    意义，那些点照旧线性插值。
    """
    out = resample(src_x, src_y, dst_x)
    extra = np.sqrt(np.maximum(dst_res * dst_res - src_res * src_res, 0.0))
    use = extra > src_res
    if use.any():
        out[:, use] = convolve(src_x, src_y, dst_x[use], extra[use])
    return out


def continuum_removed(x: np.ndarray, values: np.ndarray) -> np.ndarray:
    """包络线去除：除以上凸包。每行一条谱，返回同形状的比值谱。

    上凸包用单调链求，逐行做——凸包这东西没法向量化，好在一条谱几百个点，几千
    条也就一两秒。NaN 点不参与建包络，输出仍是 NaN。
    """
    values = np.atleast_2d(np.asarray(values, dtype=np.float64))
    out = np.full(values.shape, np.nan)
    for r in range(values.shape[0]):
        row = values[r]
        good = np.flatnonzero(np.isfinite(row))
        if good.size < 3:
            continue
        gx, gy = x[good], row[good]
        hull = _upper_hull(gx, gy)
        base = np.interp(gx, gx[hull], gy[hull])
        floor = max(abs(gy).max() * HULL_FLOOR, 1e-12)
        safe = base > floor
        ratio = np.full(gx.size, np.nan)
        ratio[safe] = gy[safe] / base[safe]
        out[r, good] = ratio
    return out


def _upper_hull(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """上凸包顶点的下标（含首尾）。单调链，O(n)。

    循环里只碰 Python 的 float：逐个取 numpy 标量要慢五六倍，而匹配时每条谱都要
    走一遍这里，全库就是几千遍。
    """
    xs, ys = x.tolist(), y.tolist()
    stack: List[int] = []
    sx: List[float] = []
    sy: List[float] = []
    for i, (xi, yi) in enumerate(zip(xs, ys)):
        while len(stack) >= 2:
            ax, ay, bx, by = sx[-2], sy[-2], sx[-1], sy[-1]
            # b 在 a->i 连线之下就丢掉：叉积 >= 0 表示不右转
            if (bx - ax) * (yi - ay) - (by - ay) * (xi - ax) >= 0:
                stack.pop()
                sx.pop()
                sy.pop()
            else:
                break
        stack.append(i)
        sx.append(xi)
        sy.append(yi)
    return np.asarray(stack, dtype=np.intp)


# -----------------------------------------------------------------------------
# 度量
# -----------------------------------------------------------------------------

def _masked(lib: np.ndarray, q: np.ndarray):
    """``q`` 可以是一条（所有行共用）也可以每行一条。"""
    q = np.broadcast_to(q, lib.shape)
    mask = np.isfinite(lib) & np.isfinite(q)
    L = np.where(mask, lib, 0.0)
    Q = np.where(mask, q, 0.0)
    return mask, L, Q


def _sam(mask, L, Q, n_ok) -> np.ndarray:
    dot = (L * Q).sum(1)
    norm = np.sqrt((L * L).sum(1)) * np.sqrt((Q * Q).sum(1))
    with np.errstate(invalid="ignore", divide="ignore"):
        cos = np.where(norm > 0, dot / norm, np.nan)
    return np.arccos(np.clip(cos, -1.0, 1.0))


def _scm(mask, L, Q, n_ok) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        mL = L.sum(1) / n_ok
        mQ = Q.sum(1) / n_ok
        Lc = np.where(mask, L - mL[:, None], 0.0)
        Qc = np.where(mask, Q - mQ[:, None], 0.0)
        denom = np.sqrt((Lc * Lc).sum(1) * (Qc * Qc).sum(1))
        return np.where(denom > 0, (Lc * Qc).sum(1) / denom, np.nan)


def _distributions(mask, L, Q):
    """归一化成概率分布。负值垫到 eps——反射率理论上非负，负的是噪声。"""
    Lp = np.where(mask, np.maximum(L, SID_EPS), 0.0)
    Qp = np.where(mask, np.maximum(Q, SID_EPS), 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        Lp = Lp / Lp.sum(1, keepdims=True)
        Qp = Qp / Qp.sum(1, keepdims=True)
    return (Lp, Qp)


def _sid(mask, L, Q, n_ok) -> np.ndarray:
    Lp, Qp = _distributions(mask, L, Q)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = np.where(mask & (Lp > 0) & (Qp > 0), np.log(Lp / Qp), 0.0)
        return np.abs(((Lp - Qp) * ratio).sum(1))


def _sidsam(mask, L, Q, n_ok) -> np.ndarray:
    return _sid(mask, L, Q, n_ok) * np.tan(_sam(mask, L, Q, n_ok))


_METRICS = {"sam": _sam, "scm": _scm, "sid": _sid, "sidsam": _sidsam}


# -----------------------------------------------------------------------------
# 主入口
# -----------------------------------------------------------------------------

@dataclass
class MatchReport:
    """一次匹配的结果与"没进榜的都去哪了"。"""

    hits: List[Hit]
    #: 交进来比的条数
    compared: int = 0
    #: 没完整覆盖比较范围被挡掉的（只在 ``full_cover`` 时有）
    skipped_cover: int = 0
    #: 有效重叠波段太少被挡掉的
    skipped_bands: int = 0
    #: 比较范围（查询谱裁剪后的首末波长）与实际用的端点容差
    lo: float = float("nan")
    hi: float = float("nan")
    tolerance: float = 0.0


def match(query_x: np.ndarray, query_y: np.ndarray,
          entries: Sequence[Entry], libraries: Sequence[Library],
          options: Optional[MatchOptions] = None,
          progress: Optional[Callable[[int, int], bool]] = None) -> List[Hit]:
    """把 ``entries`` 逐条与查询谱比一遍，按得分从高到低返回。见 :func:`match_report`。"""
    return match_report(query_x, query_y, entries, libraries, options, progress).hits


def match_report(query_x: np.ndarray, query_y: np.ndarray,
                 entries: Sequence[Entry], libraries: Sequence[Library],
                 options: Optional[MatchOptions] = None,
                 progress: Optional[Callable[[int, int], bool]] = None) -> MatchReport:
    """把 ``entries`` 逐条与查询谱比一遍，连同被挡掉的条数一起返回。

    ``progress(已完成块数, 总块数)`` 返回 False 就中断，已经算完的照常返回。
    按**数据块**分批算：同一块里的谱共用一条波长轴，对齐权重只算一次。包络线
    要按每条的重叠段求，逐行做；重叠段一样的行共用同一条去包络的查询谱。
    """
    options = options or MatchOptions()
    algo = ALGORITHM_BY_KEY.get(options.algorithm) or ALGORITHM_BY_KEY[DEFAULT_ALGORITHM]
    report = MatchReport(hits=[], compared=len(entries))
    # 分辨率按裁剪前的采样间隔估计：水汽段挖掉后缺口两边的点不该算成宽波段
    qx = np.asarray(query_x, dtype=np.float64)
    qx = np.sort(qx[np.isfinite(qx) & np.isfinite(np.asarray(query_y, dtype=np.float64))])
    gx, gy = prepare_query(query_x, query_y, options)
    if gx.size < options.min_bands:
        return report
    q_res = np.interp(gx, qx, spacing(qx))
    lo, hi = float(gx[0]), float(gx[-1])
    tol = max(min(max(options.edge_tolerance, 0.0), EDGE_TOLERANCE_MAX),
              EDGE_TOLERANCE_BANDS * float(np.median(spacing(qx))))
    report.lo, report.hi, report.tolerance = lo, hi, tol
    need = max(options.min_bands, int(gx.size * options.min_coverage))

    by_lib = {lib.key: lib for lib in libraries}
    buckets: Dict[Tuple[str, str], List[Entry]] = {}
    for e in entries:
        buckets.setdefault((e.library, e.block), []).append(e)

    metric_fn = _METRICS[algo.key]
    total = len(buckets)
    q_by_grid: Dict[bytes, np.ndarray] = {}
    for done, ((lib_key, block), rows) in enumerate(sorted(buckets.items()), 1):
        lib = by_lib.get(lib_key)
        if lib is None:
            continue
        src_x, mat = lib.block(block)
        take = np.asarray([e.row for e in rows], dtype=np.intp)
        raw = mat[take].astype(np.float64)
        l_res = np.interp(gx, src_x, spacing(src_x))
        values = align(src_x, raw, gx, l_res, q_res)
        # 查询谱降到这一档分辨率的结果只跟库的网格有关，几百个块其实只有几种网格
        grid_key = l_res.tobytes()
        if grid_key not in q_by_grid:
            q_by_grid[grid_key] = align(gx, gy, gx, q_res, l_res)[0]
        query = q_by_grid[grid_key]
        q_ok = np.isfinite(query)

        # 每条库谱自己的有效波长范围（在它自己的网格上看）
        good = np.isfinite(raw)
        has = good.any(1)
        first = np.where(has, good.argmax(1), 0)
        last = np.where(has, src_x.size - 1 - good[:, ::-1].argmax(1), 0)
        own_lo, own_hi = src_x[first], src_x[last]

        keep = np.zeros(len(rows), dtype=bool)
        lib_rows = np.full(values.shape, np.nan)
        q_rows = np.full(values.shape, np.nan)
        q_cache: Dict[Tuple[int, int], np.ndarray] = {}
        for i in range(len(rows)):
            ok = np.isfinite(values[i]) & np.isfinite(query)
            n = int(ok.sum())
            if not has[i] or n < options.min_bands:
                report.skipped_bands += 1
                continue
            if options.full_cover:
                inside = int(((gx >= own_lo[i]) & (gx <= own_hi[i])).sum())
                if (own_lo[i] > lo + tol or own_hi[i] < hi - tol
                        or n < FULL_COVER_VALID * inside):
                    report.skipped_cover += 1
                    continue
            elif n < need:
                report.skipped_bands += 1
                continue
            keep[i] = True
            if not options.continuum:
                lib_rows[i], q_rows[i] = values[i], query
                continue
            # 查询谱的包络线在两边都有效的点上求。同一块里的谱绝大多数重叠段一样、
            # 中间也没缺口，按首末下标缓存；有内部缺口的照实单算
            idx = np.flatnonzero(ok)
            i0, i1 = int(idx[0]), int(idx[-1])
            if idx.size == q_ok[i0:i1 + 1].sum():
                if (i0, i1) not in q_cache:
                    seg = np.full(gx.size, np.nan)
                    part = np.flatnonzero(q_ok[i0:i1 + 1]) + i0
                    seg[part] = continuum_removed(gx[part], query[part])[0]
                    q_cache[(i0, i1)] = seg
                q_rows[i, idx] = q_cache[(i0, i1)][idx]
            else:
                q_rows[i, idx] = continuum_removed(gx[idx], query[idx])[0]
            lib_rows[i, idx] = continuum_removed(gx[idx], values[i, idx])[0]

        mask, L, Q = _masked(lib_rows, q_rows)
        n_ok = mask.sum(1)
        with np.errstate(invalid="ignore", divide="ignore"):
            metric = metric_fn(mask, L, Q, np.maximum(n_ok, 1))
        score = algo.score(metric)
        for i, entry in enumerate(rows):
            if not keep[i]:
                continue
            if n_ok[i] < options.min_bands or not np.isfinite(metric[i]):
                report.skipped_bands += 1
                continue
            used = np.flatnonzero(mask[i])
            report.hits.append(Hit(entry, float(score[i]), float(metric[i]), int(n_ok[i]),
                                   lo=float(gx[used[0]]), hi=float(gx[used[-1]]),
                                   coverage=float(n_ok[i]) / gx.size))
        if progress is not None and not progress(done, total):
            break
    report.hits.sort(key=lambda h: -h.score)
    return report


def metric_text(algo: Algorithm, value: float) -> str:
    """原始度量的显示文本。SAM 存的是弧度，显示成度。"""
    if algo.key == "sam":
        return algo.metric_fmt % math.degrees(value)
    return algo.metric_fmt % value
