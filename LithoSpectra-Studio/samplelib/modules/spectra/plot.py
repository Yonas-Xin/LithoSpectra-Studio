# -*- coding: utf-8 -*-
"""光谱曲线的绘制。

用 matplotlib，样式对齐论文插图的那一套 rcParams：7 pt 无衬线、0.8 pt 轴线、
去掉上右两条边框、无边框图例、``pdf.fonttype = 42`` 与 ``svg.fonttype = none``
（矢量导出里文字仍是可编辑的真文字，不是轮廓）。参数写在 :data:`PAPER_RC`，
改一处四种画布一起变。

**屏幕与导出是同一份绘制。** 界面上那块画布挂的就是一个 ``Figure``，导出时
另建一个同尺寸的 ``Figure`` 走同一个 :func:`draw_into`。图幅尺寸按毫米给定，
屏幕上靠调 ``figure.dpi`` 等比放大——所以面板拉多大，字号与线宽的相对比例
一点不变，看到的就是存下来的。

不用 ``pyplot``：它带一份全局状态（当前图、当前轴），在一个可以同时开好几个
窗口的界面里迟早互相串。这里一律直接构造 ``Figure``。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

import numpy as np

if TYPE_CHECKING:
    from matplotlib.figure import Figure


# matplotlib 第一次用到时才载入：它连带字体表要零点几秒到几秒，放在模块顶部就压在
# 程序启动的路上，而多数时候打开程序并不马上画曲线
def rc_context(rc):
    from matplotlib import rc_context as _rc_context
    return _rc_context(rc)


def _new_figure(**kw) -> "Figure":
    from matplotlib.figure import Figure as _Figure
    return _Figure(**kw)

from . import spectra

# =============================================================================
# 配置区
# =============================================================================

#: 图幅预设：名字 → (宽 mm, 高 mm)。宽度取自主流期刊的排版栏宽——单栏 90 mm、
#: 通栏 190 mm 覆盖了 Elsevier、IEEE、Springer 的绝大多数模板。
FIGURE_PRESETS: Tuple[Tuple[str, float, float], ...] = (
    ("单栏 90×65", 90.0, 65.0),
    ("单栏高 90×80", 90.0, 80.0),
    ("通栏 180×80", 180.0, 80.0),
    ("通栏 190×90", 190.0, 90.0),
    ("通栏高 190×120", 190.0, 120.0),
    ("方形 100×100", 100.0, 100.0),
    ("幻灯片 160×90", 160.0, 90.0),
)

#: 三套配色。
#: ``default`` 是 matplotlib 的 tab10，也是绝大多数论文插图里看到的那一组；
#: ``cb`` 是 Okabe-Ito 色盲友好色板（去掉白底上看不清的亮黄）；
#: ``mono`` 是灰度，投黑白印刷的期刊时用，靠线型区分。
#: 红打头：单条曲线的图占绝大多数，而单条曲线用红最惯常（也是参考插图里的
#: 那个红）。其余顺序不动。
COLOR_SETS: Dict[str, Tuple[str, ...]] = {
    "default": ("#D62728", "#1F77B4", "#2CA02C", "#FF7F0E", "#9467BD",
                "#8C564B", "#E377C2", "#7F7F7F", "#BCBD22", "#17BECF"),
    "cb": ("#D55E00", "#0072B2", "#009E73", "#CC79A7", "#E69F00",
           "#56B4E9", "#8C564B", "#1A1A1A"),
    "mono": ("#000000", "#4D4D4D", "#808080", "#A6A6A6"),
}

COLOR_SET_NAMES: Tuple[Tuple[str, str], ...] = (
    ("default", "默认（tab10）"),
    ("cb", "色盲友好（Okabe-Ito）"),
    ("mono", "灰度（黑白印刷）"),
)

#: 线型循环。颜色用完一轮就换线型，灰度打印下仍能区分。
LINESTYLES: Tuple[str, ...] = ("-", "--", ":", "-.")

#: 水汽波段与诊断吸收位置都是**领域事实**，写在 spectra.py 里，这边只是
#: 拿来画。两处各写一份迟早会不一致——曲线上断开的位置与灰带对不上，图就错了。
WATER_GAPS = spectra.WATER_VAPOR_RANGES

#: 水汽波段的三种处理
GAP_NONE, GAP_BREAK, GAP_SHADE = "none", "break", "shade"
GAP_MODES: Tuple[Tuple[str, str], ...] = (
    (GAP_SHADE, "断开并标灰"),
    (GAP_BREAK, "只断开"),
    (GAP_NONE, "不处理"),
)

#: 诊断吸收位置的两种画法。"高亮某个判读窗口"是另一件事，见 PlotStyle.highlight——
#: 五个窗口在短波红外挨着甚至叠着，一起铺出来能盖满整张图，等于什么都没标。
FEAT_NONE, FEAT_LINE = "none", "line"
FEATURE_MODES: Tuple[Tuple[str, str], ...] = (
    (FEAT_NONE, "不显示"),
    (FEAT_LINE, "竖线 + 名称"),
)

#: 数据点标记的三档
MARKER_AUTO, MARKER_ON, MARKER_OFF = "auto", "on", "off"
MARKER_MODES: Tuple[Tuple[str, str], ...] = (
    (MARKER_AUTO, "自动"),
    (MARKER_ON, "总是"),
    (MARKER_OFF, "从不"),
)

#: 线宽与标记大小的可调范围（pt）。1.15 / 3.0 是论文插图里最常见的一档。
LINE_WIDTH_RANGE = (0.4, 3.0)
MARKER_SIZE_RANGE = (1.0, 8.0)

DIAGNOSTIC_FEATURES = spectra.DIAGNOSTIC_FEATURES

#: 论文样式。与 50_code/prior_attention_v5 里那套一致。
PAPER_RC: Dict[str, object] = {
    "font.family": "sans-serif",
    # 数学排版跟着正文字体走：默认的 dejavusans 与 Arial 并排会看出两种字形，
    # 而 H₂O 的下标、Fe³⁺ 的上标都得走 mathtext（正文字体没有这些字形）。
    "mathtext.fontset": "custom",
    "mathtext.rm": "sans",           # 走 font.sans-serif，与正文同一个字体
    # custom 这套 fontset 会把 rm/it/bf/cal/sf/tt 六项全找一遍，哪怕根本用不上。
    # cal 默认指向 cursive，在 macOS 上落到 Apple Chancery 并甩出一句
    # "Failed to find font weight normal"。指回 sans 就不去找了。
    "mathtext.cal": "sans",
    "mathtext.default": "regular",
    # 后面几个中文字体是**兜底**，不是主字体：matplotlib 会逐个字体去找字形，
    # 拉丁字母永远落在 Arial 上，只有 Arial 里没有的汉字才轮到它们。图例上的曲线
    # 名一律建议用英文（这是投稿要求），但样本库的类别名、矢量属性值本来就是中文，
    # 没有兜底就是一排空豆腐块——那比字体不统一难看得多，也让人看不出是哪一类。
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans", "Liberation Sans",
                        "PingFang SC", "Heiti SC", "Microsoft YaHei",
                        "Noto Sans CJK SC", "Source Han Sans SC", "SimHei"],
    "font.serif": ["Times New Roman", "Nimbus Roman", "DejaVu Serif",
                   "Songti SC", "SimSun", "Noto Serif CJK SC"],
    "svg.fonttype": "none",          # SVG 里保留真文字，Illustrator 能改
    "pdf.fonttype": 42,              # TrueType 子集，不转轮廓
    "ps.fonttype": 42,
    "axes.linewidth": 0.8,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.labelpad": 3.0,
    "axes.titlepad": 5.0,
    "legend.frameon": False,
    "legend.handlelength": 2.2,
    "legend.columnspacing": 1.2,
    "legend.borderaxespad": 0.4,
    "xtick.direction": "out",
    "ytick.direction": "out",
    "xtick.major.width": 0.7,
    "ytick.major.width": 0.7,
    "xtick.minor.width": 0.6,
    "ytick.minor.width": 0.6,
    "xtick.major.size": 3.0,
    "ytick.major.size": 3.0,
    "xtick.minor.size": 1.8,
    "ytick.minor.size": 1.8,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.facecolor": "white",
    "savefig.transparent": False,
    "lines.solid_capstyle": "round",
    "lines.dash_capstyle": "round",
}

#: 水汽灰带与网格的颜色，与论文插图一致
GAP_COLOR = "#D8D8D8"
GAP_ALPHA = 0.55
GAP_LABEL = "H$_2$O"
GAP_TEXT_COLOR = "#8A8A8A"
GRID_COLOR = "#E7E7E7"
GRID_WIDTH = 0.5
DIAG_COLOR = "#8E8E8E"
#: 诊断窗口的底色。淡到不抢曲线，又能一眼看出范围在哪。
FEATURE_FACE = "#F2E6BF"
FEATURE_ALPHA = 0.55
FEATURE_TEXT_COLOR = "#7A6A2E"

#: 曲线的鼠标命中半径（像素）。1 pt 的线在屏幕上不到两个像素宽，不放宽点不着。
PICK_RADIUS = 6

#: 点数少于这个数就自动带上数据点标记——十几个波段的多光谱影像画成光溜溜的
#: 折线，看不出采样位置在哪。高光谱几百个波段则一律不带。
MARKER_MAX_POINTS = 30

#: 图例摆哪儿
LEGEND_IN = "inside"
LEGEND_SIDE = "side"
LEGEND_PLACES: Tuple[Tuple[str, str], ...] = (
    (LEGEND_IN, "图内"),
    (LEGEND_SIDE, "图右单独一栏"),
)

#: 单独一栏时，图与图例之间留的空当（mm）
LEGEND_GAP_MM = 3.0

#: 单独一栏时图例四周的内边距（mm）
LEGEND_PAD_MM = 1.5

#: 图例一行占的高度，按字号折算成毫米的系数。1 pt = 1/72 inch = 0.3528 mm，
#: 行距连同标记留白算 1.55 倍。只用来估"一列能放下几行"，不必精确。
LEGEND_ROW_MM_PER_PT = 0.3528 * 1.55

#: 图例每多一行，纵轴顶部就多留这么多比例的空白，好让 ``loc="best"`` 有地方放。
LEGEND_ROW_HEADROOM = 0.055
LEGEND_HEADROOM_MAX = 0.45

#: 打开诊断吸收标注时，纵轴顶部额外留出的比例——那些名称是竖着写的，占地不小。
FEATURE_LABEL_HEADROOM = 0.16

MM_PER_INCH = 25.4

# =============================================================================


def mm_to_inch(mm: float) -> float:
    return float(mm) / MM_PER_INCH


# -----------------------------------------------------------------------------
# 样式与数据
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class PlotStyle:
    """一幅图的全部排印参数。尺寸以毫米计，字号线宽以点计。"""

    width_mm: float = 190.0
    height_mm: float = 90.0

    serif: bool = False
    font_pt: float = 9.0             # 论文插图的正文字号
    label_scale: float = 1.15        # 轴标题相对正文的倍数
    legend_scale: float = 1.0
    curve_lw: float = 1.15

    box_frame: bool = False          # True 画四边闭合的框
    grid: bool = True                # 横向网格线（reference 图里那种极淡的）
    minor_ticks: bool = True
    legend: bool = True
    legend_loc: str = "best"
    legend_cols: int = 0             # 0 = 自动
    #: 图例摆在图内还是右边单独一栏。曲线一多，图内的图例压着曲线、又挤又乱，
    #: 挪到右边自己一栏去，谁也不碍谁。
    legend_place: str = LEGEND_IN
    #: 右边那一栏有多宽（mm）。**由内容量出来**，不是拍脑袋定的，也不能反过来跟
    #: 屏幕尺寸有关——一旦它跟着画布的像素宽变，就会绕出"画布变宽 → 图例变宽 →
    #: 画布再变宽"的圈，图会自己慢慢长。窗口那边用 measure_legend_mm() 算好塞进来。
    legend_width_mm: float = 0.0
    marker_mode: str = MARKER_AUTO
    marker_size: float = 3.0         # 数据点直径，pt

    color_set: str = "default"
    gap_mode: str = GAP_SHADE
    feature_mode: str = FEAT_NONE    # 诊断吸收位置怎么画
    #: 高亮哪一个判读窗口（DIAGNOSTIC_FEATURES 的 key，空串表示不高亮）。
    #: 一次只高亮一个：这是"我现在在看这段"的意思，标满了就没有重点。
    highlight: str = ""
    title: str = ""
    #: 横轴显示范围（nm）。None 表示按数据两端。纵轴跟着这段里的数据重新定，
    #: 不然放大到 SWIR 还挂着全谱的纵轴，曲线挤成一条线。
    xlim: Optional[Tuple[float, float]] = None

    @property
    def side_legend(self) -> bool:
        return (self.legend and self.legend_place == LEGEND_SIDE
                and self.legend_width_mm > 0.0)

    @property
    def total_width_mm(self) -> float:
        """整幅图的宽度：图本身，加上右边那一栏（如果开着）。"""
        if not self.side_legend:
            return self.width_mm
        return self.width_mm + LEGEND_GAP_MM + self.legend_width_mm

    @property
    def size_inch(self) -> Tuple[float, float]:
        return (mm_to_inch(self.total_width_mm), mm_to_inch(self.height_mm))

    def scaled(self, k: float) -> "PlotStyle":
        """整幅图放大 ``k`` 倍的同一份样式。

        屏幕上的画布比 90 mm 的图幅大得多。直接把 7 pt 的字画在一块 300 mm 宽的
        画布上，看到的字会比导出的小一半，预览就骗人了。把图幅、字号、线宽同乘
        一个系数，屏幕上就是导出那张图**等比放大**的样子——每一处比例都一致。
        """
        k = max(0.05, float(k))
        return replace(self, width_mm=self.width_mm * k,
                       height_mm=self.height_mm * k,
                       legend_width_mm=self.legend_width_mm * k,
                       font_pt=self.font_pt * k,
                       curve_lw=self.curve_lw * k,
                       marker_size=self.marker_size * k)

    @property
    def markers(self) -> Optional[bool]:
        """三档标记开关折成 ``True / False / None（自动）``。"""
        if self.marker_mode == MARKER_ON:
            return True
        if self.marker_mode == MARKER_OFF:
            return False
        return None

    @property
    def aspect(self) -> float:
        """整幅图的高宽比。画布锁的就是它，所以要算上右边那一栏。"""
        return self.height_mm / max(1e-6, self.total_width_mm)


DEFAULT_STYLE = PlotStyle()


@dataclass
class Series:
    """图上的一条线。``y`` 里的 NaN 表示断开，不是 0。"""

    label: str
    x: np.ndarray
    y: np.ndarray
    color: str = "#1F77B4"
    linestyle: str = "-"
    linewidth: Optional[float] = None
    marker: Optional[bool] = None
    emphasis: bool = False           # 屏幕上的游标：画粗一点，导出时不带
    #: 这条是"参照"（光谱库里的标样），不是这幅影像上量出来的。只影响横轴的
    #: 自动范围，见 :func:`_set_limits`。
    reference: bool = False


def style_for(index: int, color_set: str = "default") -> Tuple[str, str]:
    """第 ``index`` 条曲线的颜色与线型。颜色用完一轮换线型。"""
    colors = COLOR_SETS.get(color_set) or COLOR_SETS["default"]
    n = len(colors)
    return (colors[index % n], LINESTYLES[(index // n) % len(LINESTYLES)])


_FONT_CACHE: Dict[Tuple[str, ...], List[str]] = {}


def installed_families(names: Sequence[str]) -> List[str]:
    """把字体名单里本机真装了的挑出来，顺序不动。

    **不能把整张名单原样交给 matplotlib。** 名单里排了几个中文兜底字体是为了跨平台
    （macOS 的 PingFang、Windows 的微软雅黑、Linux 的思源黑体），到了具体一台机器
    上必然有几个是没有的；而 font.family 一旦写成显式列表，matplotlib 每画一次文字
    就会为每个找不到的字体打一行 ``findfont: Font family 'X' not found``——一张图刷
    下来上千行，日志淹掉不说，每次还都要重新查一遍。

    结果缓存起来：font_manager 的字体表在进程里是不变的。
    """
    key = tuple(names)
    if key not in _FONT_CACHE:
        from matplotlib import font_manager
        have = {f.name for f in font_manager.fontManager.ttflist}
        picked = [n for n in names if n in have]
        # 一个都没装是不可能的（DejaVu 随 matplotlib 发），真到了那一步就原样交回去
        _FONT_CACHE[key] = picked or list(names)
    return list(_FONT_CACHE[key])


def text_families(style: PlotStyle) -> List[str]:
    """画一段文字该用哪几个字体（含中文兜底），已按本机装了什么过滤。

    给**不经过 rc_context 的那些文字**用。图是在 ``rc_context(rc_for(style))``
    里画的，字体设置只在那个块里生效；浮标读数是后来贴上去的一层，跑在块外面，
    拿到的是 matplotlib 的全局默认值——``font.family`` 是 "sans-serif" 别名，不触发
    逐字形回退，中文就成了空豆腐块。那一层必须自己把名单带上。
    """
    key = "font.serif" if style.serif else "font.sans-serif"
    return installed_families(PAPER_RC[key])


def rc_for(style: PlotStyle) -> Dict[str, object]:
    """这套样式对应的 rcParams。

    ``scaled()`` 只改了图幅与字号，轴线、刻度、网格的粗细写在 :data:`PAPER_RC`
    里，得按同一个系数一起放大，否则放大之后线相对变细，屏幕上看着比导出的秀气。
    """
    k = style.font_pt / DEFAULT_STYLE.font_pt
    rc = dict(PAPER_RC)
    for key in ("axes.linewidth", "xtick.major.width", "ytick.major.width",
                "xtick.minor.width", "ytick.minor.width", "xtick.major.size",
                "ytick.major.size", "xtick.minor.size", "ytick.minor.size",
                "axes.labelpad", "axes.titlepad"):
        rc[key] = float(PAPER_RC[key]) * k
    # **必须把字体名一个个列出来，不能写 "sans-serif" 这个别名。** matplotlib 的
    # 逐字形回退（某个字在当前字体里没有就往后一个字体找）只在 font.family 是
    # 显式列表时才生效；写别名时它只认 font.sans-serif 里第一个装了的字体，找不到
    # 汉字就直接画成空豆腐块。实测：写别名 5 条缺字警告，写显式列表 0 条。
    key = "font.serif" if style.serif else "font.sans-serif"
    rc["font.family"] = installed_families(rc.get(key) or PAPER_RC[key])
    rc["font.size"] = style.font_pt
    rc["axes.labelsize"] = style.font_pt * style.label_scale
    rc["axes.titlesize"] = style.font_pt * style.label_scale
    rc["legend.fontsize"] = style.font_pt * style.legend_scale
    rc["xtick.labelsize"] = style.font_pt
    rc["ytick.labelsize"] = style.font_pt
    rc["axes.spines.top"] = style.box_frame
    rc["axes.spines.right"] = style.box_frame
    return rc


# -----------------------------------------------------------------------------
# 绘制
# -----------------------------------------------------------------------------

def _scale(style: PlotStyle) -> float:
    """当前样式相对基准样式放大了多少倍。见 :meth:`PlotStyle.scaled`。"""
    return style.font_pt / DEFAULT_STYLE.font_pt


def split_gaps(x: np.ndarray, y: np.ndarray,
               gaps: Sequence[Tuple[float, float]]) -> np.ndarray:
    """把落进 ``gaps`` 的点挖成 NaN，返回新数组。"""
    out = np.asarray(y, dtype=np.float64).copy()
    for lo, hi in gaps:
        out[(x >= lo) & (x <= hi)] = np.nan
    return out


def draw_into(figure: "Figure", series: Sequence[Series], xlabel: str = "",
              ylabel: str = "", style: PlotStyle = DEFAULT_STYLE,
              for_export: bool = False, empty_hint: str = "") -> Optional[object]:
    """把整幅图画进 ``figure``（会先清空）。返回画好的 Axes，空图时返回 None。

    ``for_export`` 为真时不画屏幕专用的东西——眼下只有"当前那条加粗"。它是个
    游标，标的是"再点一下会被换掉的是这条"，跟鼠标浮标同类，属于交互不属于图。
    """
    with rc_context(rc_for(style)):
        figure.clear()
        figure.set_layout_engine("constrained")
        figure.get_layout_engine().set(w_pad=0.02, h_pad=0.02,
                                       wspace=0.0, hspace=0.0)
        # 图例单独一栏时，图与图例是同一张图上并排的两块：**一样高**，图例那块
        # 宽度按内容量出来。两块在一张 figure 里而不是两个画布，导出才还是一个
        # 文件——插进版面时图和图例本来就该是一体的。
        legend_ax = None
        if style.side_legend:
            grid = figure.add_gridspec(
                1, 2, width_ratios=[style.width_mm, style.legend_width_mm],
                wspace=LEGEND_GAP_MM / max(1e-6, style.width_mm))
            ax = figure.add_subplot(grid[0, 0])
            legend_ax = figure.add_subplot(grid[0, 1])
            legend_ax.set_axis_off()
        else:
            ax = figure.add_subplot(111)

        drawable = [s for s in series
                    if np.any(np.isfinite(np.asarray(s.y, dtype=np.float64)))]
        if not drawable:
            ax.set_axis_off()
            if empty_hint:
                ax.text(0.5, 0.5, empty_hint, transform=ax.transAxes,
                        ha="center", va="center", color="#8A8A8A",
                        fontsize=style.font_pt * 1.2)
            return None

        wavelength = "Wavelength" in xlabel
        for s in drawable:
            _draw_series(ax, s, style, for_export)

        _set_limits(ax, drawable, style)
        if wavelength and style.gap_mode == GAP_SHADE:
            _draw_gaps(ax, style)
        if wavelength and style.highlight:
            _draw_highlight(ax, style)
        if wavelength and style.feature_mode != FEAT_NONE:
            _draw_features(ax, style)

        if style.grid:
            ax.grid(axis="y", color=GRID_COLOR,
                    linewidth=GRID_WIDTH * _scale(style), zorder=0)
            ax.set_axisbelow(True)
        if style.minor_ticks:
            ax.minorticks_on()
            ax.tick_params(axis="both", which="minor", top=False, right=False)
        ax.tick_params(axis="both", which="major",
                       top=style.box_frame, right=style.box_frame)

        if xlabel:
            ax.set_xlabel(xlabel)
        if ylabel:
            ax.set_ylabel(ylabel)
        if style.title:
            ax.set_title(style.title, loc="left")

        if style.legend and any(s.label for s in drawable):
            _draw_legend(ax, drawable, style, legend_ax)
        return ax


def _draw_series(ax, s: Series, style: PlotStyle, for_export: bool) -> None:
    x = np.asarray(s.x, dtype=np.float64)
    y = np.asarray(s.y, dtype=np.float64)
    n = min(x.size, y.size)
    x, y = x[:n], y[:n]

    width = s.linewidth if s.linewidth is not None else style.curve_lw
    if s.emphasis and not for_export:
        width *= 1.5
    marker = s.marker if s.marker is not None else style.markers
    if marker is None:
        marker = int(np.count_nonzero(np.isfinite(y))) <= MARKER_MAX_POINTS
    line, = ax.plot(x, y, color=s.color, linestyle=s.linestyle, linewidth=width,
                    marker="o" if marker else "None",
                    markersize=style.marker_size, markeredgewidth=0.0,
                    label=s.label or "_nolegend_",
                    solid_joinstyle="round", zorder=3)
    # 让曲线可以被点中：点一下就能给它换颜色（见 plot_view.SpectrumCanvas）。
    # 命中半径比线本身宽几个像素，不然细线根本点不着。
    line.set_picker(True)
    line.set_pickradius(PICK_RADIUS)


def _halo(style: PlotStyle):
    """给标注描一圈白边。

    诊断名称与 H₂O 都竖在图顶，曲线爬上去时字就压在线上。描白边是图表里通行的
    办法：不动数据、不挪标注，字仍然读得出来。
    """
    from matplotlib import patheffects
    return [patheffects.withStroke(linewidth=2.0 * _scale(style),
                                   foreground="white")]


def _draw_gaps(ax, style: PlotStyle) -> None:
    """水汽灰带，带一个 H₂O 字样。

    只标灰不写字，读者得自己猜那两条灰带是什么；写上一次就够，之后都认得。
    """
    lo, hi = ax.get_xlim()
    for low, high in WATER_GAPS:
        if high <= lo or low >= hi:
            continue
        ax.axvspan(low, high, facecolor=GAP_COLOR, edgecolor="none",
                   alpha=GAP_ALPHA, zorder=0)
        if (min(high, hi) - max(low, lo)) / max(1e-9, hi - lo) < 0.03:
            continue                      # 太窄，字塞不进去
        ax.text((max(low, lo) + min(high, hi)) / 2.0, 0.97, GAP_LABEL,
                transform=ax.get_xaxis_transform(), ha="center", va="top",
                fontsize=style.font_pt * 0.95, color=GAP_TEXT_COLOR, zorder=4,
                path_effects=_halo(style))


def _draw_features(ax, style: PlotStyle) -> None:
    """诊断吸收位置的竖点线，带名称。

    名称竖着写在顶端。SWIR 那三条（2207 / 2253 / 2335 nm）挨得很近，横排的字
    必然叠在一起；竖排既不重叠，也不占纵向的数据空间。
    """
    lo, hi = ax.get_xlim()
    for feature in DIAGNOSTIC_FEATURES:
        if not (lo <= feature.centre <= hi):
            continue
        ax.axvline(feature.centre, color=DIAG_COLOR,
                   linewidth=0.7 * _scale(style),
                   linestyle=(0, (1, 3)), zorder=1)
        ax.text(feature.centre, 0.985,
                "%s %.0f" % (feature.label, feature.centre),
                transform=ax.get_xaxis_transform(), rotation=90,
                ha="right", va="top", fontsize=style.font_pt * 0.9,
                color=DIAG_COLOR, zorder=4, path_effects=_halo(style))


def _draw_highlight(ax, style: PlotStyle) -> None:
    """把某一个判读窗口铺上底色，并在上方写出它是哪一段。

    这是"我现在在看这一段"的标注：论文里常见的那种浅色竖带。窗口本身在
    spectra.py 里定义，与做特征提取时用的是同一组边界——图上标的范围
    要是跟算法实际用的范围不一样，那张图就是在骗人。
    """
    feature = next((f for f in DIAGNOSTIC_FEATURES if f.key == style.highlight),
                   None)
    if feature is None:
        return
    low, high = feature.window
    lo, hi = ax.get_xlim()
    if high <= lo or low >= hi:
        return
    ax.axvspan(low, high, facecolor=FEATURE_FACE, edgecolor="none",
               alpha=FEATURE_ALPHA, zorder=0)
    ax.text((max(low, lo) + min(high, hi)) / 2.0, 0.97,
            "%s  %s" % (feature.key, feature.label),
            transform=ax.get_xaxis_transform(), ha="center", va="top",
            fontsize=style.font_pt * 0.95, color=FEATURE_TEXT_COLOR,
            zorder=4, path_effects=_halo(style))


def _set_limits(ax, series: Sequence[Series], style: PlotStyle) -> None:
    """横轴贴着数据两端，纵轴留出图例要占的顶部空白。

    横轴不留边距：光谱的两端就是传感器的量程，留白反而让人以为那里还有数据。
    """
    if style.xlim is not None:
        lo, hi = style.xlim
    else:
        # 横轴的自动范围**由实测谱定，参照谱只是铺在上面**。光谱库里那些
        # ASTER 标样一直测到 25 µm，跟着它把横轴撑开，影像那条 0.4~2.5 µm 的谱
        # 就被压进最左边一指宽的地方，等于什么都看不见了。没有实测谱时（只翻库
        # 的情形）才按全部曲线取。
        primary = [s for s in series if not s.reference] or list(series)
        lo = min(float(np.nanmin(s.x)) for s in primary)
        hi = max(float(np.nanmax(s.x)) for s in primary)
    if hi > lo:
        ax.set_xlim(lo, hi)

    # 纵轴只看**当前横轴范围内**的数据。放大到 SWIR 还挂着全谱的纵轴，那一段的
    # 起伏会被压成一条直线，等于没放大。
    visible = []
    for s in series:
        x = np.asarray(s.x, dtype=np.float64)
        y = np.asarray(s.y, dtype=np.float64)
        n = min(x.size, y.size)
        if n == 0:
            continue
        inside = (x[:n] >= lo) & (x[:n] <= hi)
        visible.append(y[:n][inside])
    values = np.concatenate(visible) if visible else np.array([])
    values = values[np.isfinite(values)]
    if values.size == 0:
        return
    y0, y1 = float(values.min()), float(values.max())
    if y1 <= y0:
        y1 = y0 + max(abs(y0) * 0.05, 1e-6)
    span = y1 - y0
    # 图例每多一行就往上多留一点：matplotlib 的 loc="best" 只会挑最空的位置，
    # 不会替你把轴撑开，四五条曲线铺满整幅图时它无论放哪都压着线。
    rows = sum(1 for s in series if s.label)
    cols = _legend_cols(rows, style)
    head = 0.05
    # 图例挪到右边单独一栏之后，图内不再有它，顶部那块空白就不用留了——留着就是
    # 白白把曲线压矮一截。
    if style.legend and rows and not style.side_legend:
        head += LEGEND_ROW_HEADROOM * math.ceil(rows / max(1, cols))
    if style.feature_mode != FEAT_NONE:
        # 诊断名称竖在图顶，字本身要占掉一截高度
        head += FEATURE_LABEL_HEADROOM
    elif style.highlight:
        head += FEATURE_LABEL_HEADROOM * 0.35
    ax.set_ylim(y0 - span * 0.05, y1 + span * min(head, LEGEND_HEADROOM_MAX))


_LEGEND_MM_CACHE: Dict[Tuple, float] = {}


def measure_legend_mm(series: Sequence[Series], style: PlotStyle) -> float:
    """右边那一栏该有多宽（mm）。量出来的，不是拍脑袋定的。

    **只跟内容和排印参数有关，跟屏幕上画布多大无关。** 这一条是这个函数存在的
    全部理由：图例栏的宽度一旦跟着画布的像素宽走，就会绕出"画布变宽 → 图例变宽
    → 整幅更宽 → 画布再变宽"的圈，图会自己一点点长大——这个窗口在这上头栽过，
    不能再来一次。

    做法是拿一张离屏的图，按同样的字体字号把图例排一遍，量它的外接矩形。结果按
    （标签、字号、列数、线宽、标记）缓存：这些不变，宽度就不会变。
    """
    labels = [s.label for s in series if s.label]
    if not labels or not style.legend or style.legend_place != LEGEND_SIDE:
        return 0.0
    key = (tuple(labels), round(style.font_pt, 3), style.legend_cols,
           style.serif, round(style.legend_scale, 3), style.marker_mode,
           round(style.marker_size, 2), round(style.curve_lw, 3),
           round(style.height_mm, 2))
    if key in _LEGEND_MM_CACHE:
        return _LEGEND_MM_CACHE[key]

    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.lines import Line2D

    with rc_context(rc_for(style)):
        figure = _new_figure(figsize=(24.0, 24.0), dpi=100)
        FigureCanvasAgg(figure)
        ax = figure.add_subplot(111)
        ax.set_axis_off()
        handles = [
            Line2D([], [], color=s.color, linestyle=s.linestyle,
                   linewidth=s.linewidth or style.curve_lw,
                   marker="o" if style.markers else "none",
                   markersize=style.marker_size)
            for s in series if s.label]
        legend = ax.legend(handles, labels, loc="center",
                           ncols=_legend_cols(len(labels), style),
                           borderaxespad=0.0, handletextpad=0.5,
                           labelspacing=0.55, frameon=False)
        figure.canvas.draw()
        box = legend.get_window_extent()
        width = box.width / figure.dpi * MM_PER_INCH + 2 * LEGEND_PAD_MM
    _LEGEND_MM_CACHE[key] = width
    return width


def _legend_cols(rows: int, style: PlotStyle) -> int:
    if style.legend_cols > 0:
        return style.legend_cols
    if style.legend_place == LEGEND_SIDE:
        # 单独一栏时纵向是富余的、横向是贵的：先把一列填满，装不下才开下一列。
        # 多开一列，右边那栏就宽一倍，图跟着窄一截。
        per_col = max(1, int(style.height_mm
                             / max(1e-6, style.font_pt * LEGEND_ROW_MM_PER_PT)))
        return max(1, math.ceil(rows / per_col))
    # 宽图分两列比一列矮一半，而通栏图的横向本来就有富余
    if rows >= 4 and style.width_mm >= 140:
        return 2
    return 1


def _draw_legend(ax, series: Sequence[Series], style: PlotStyle,
                 legend_ax=None) -> None:
    rows = sum(1 for s in series if s.label)
    cols = _legend_cols(rows, style)
    if legend_ax is None:
        ax.legend(loc=style.legend_loc, ncols=cols)
        return
    # 单独一栏：把图例挂到右边那块空 Axes 上，靠左居中排。handles 从左边那块取，
    # 两处的颜色线型自然一致。
    handles, labels = ax.get_legend_handles_labels()
    legend_ax.legend(handles, labels, loc="center left",
                     bbox_to_anchor=(0.0, 0.5), ncols=cols,
                     borderaxespad=0.0, handletextpad=0.5,
                     labelspacing=0.55, frameon=False)


# -----------------------------------------------------------------------------
# 导出
# -----------------------------------------------------------------------------

def make_figure(style: PlotStyle) -> "Figure":
    """一个按图幅尺寸建好的空 Figure。"""
    return _new_figure(figsize=style.size_inch, dpi=100)


def save_figure(path: str, series: Sequence[Series], xlabel: str = "",
                ylabel: str = "", style: PlotStyle = DEFAULT_STYLE,
                dpi: int = 600) -> None:
    """按后缀写 PNG / PDF / SVG / EPS / TIFF。

    不用 ``bbox_inches="tight"``：那会按内容裁掉边，出来的图就不是要的物理
    尺寸了。图幅由 constrained layout 保证内容全落在里面。
    """
    figure = make_figure(style)
    draw_into(figure, series, xlabel, ylabel, style, for_export=True)
    ext = os.path.splitext(path)[1].lower()
    with rc_context(rc_for(style)):
        if ext in (".pdf", ".svg", ".eps"):
            figure.savefig(path, format=ext[1:])
        else:
            figure.savefig(path, dpi=int(dpi), format=ext[1:] or "png")


def render_png_bytes(series: Sequence[Series], xlabel: str = "",
                     ylabel: str = "", style: PlotStyle = DEFAULT_STYLE,
                     dpi: int = 600) -> bytes:
    """渲染成 PNG 字节流，用来放进剪贴板。"""
    import io

    figure = make_figure(style)
    draw_into(figure, series, xlabel, ylabel, style, for_export=True)
    buffer = io.BytesIO()
    with rc_context(rc_for(style)):
        figure.savefig(buffer, format="png", dpi=int(dpi))
    return buffer.getvalue()


#: 导出对话框的过滤器。EPS 留着是因为少数期刊的投稿系统只认它。
FIGURE_FILTER = ("PNG 位图 (*.png);;PDF 矢量图 (*.pdf);;SVG 矢量图 (*.svg);;"
                 "EPS 矢量图 (*.eps);;TIFF 位图 (*.tif)")
