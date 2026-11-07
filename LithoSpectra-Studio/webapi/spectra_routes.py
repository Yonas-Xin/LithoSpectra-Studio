# -*- coding: utf-8 -*-
"""光谱模块：取谱、曲线窗口、出图、光谱库、匹配、我的光谱、类别平均谱。

逻辑全在 :mod:`samplelib.modules.spectra`；曲线窗口的状态（当前那条、钉住的那些、
配色格、横纵轴、平滑、样式）放在服务端的 :class:`CurveBoard`，取谱、出图都按它来。

**屏幕与导出是同一份绘制。** 图由 :func:`plot.draw_into` 画，
前端拿到的是 PNG，外加一份坐标映射（轴在图上的位置、横纵范围、各曲线的采样点），
鼠标浮标的读数在前端按它算，不必每动一下都回来要图。
"""

import base64
import io
import os
from dataclasses import asdict, replace
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import HTTPException
from pydantic import BaseModel

from samplelib.annotate import layers as L
from samplelib.modules.spectra import groupspec, plot as P, specmatch, speclib, speclib_io, spectra

from .tasks import Task

MAX_CURVES = 24
WINDOW_SIZES = list(spectra.WINDOW_SIZES)


class CurveBoard:
    """一扇曲线窗口的全部状态。"""

    def __init__(self) -> None:
        self.layer_id: Optional[int] = None
        self.reader: Optional[spectra.SpectrumReader] = None
        self.live: Optional[spectra.Spectrum] = None
        self.pinned: List[spectra.Spectrum] = []
        self.next_slot = 0
        self.unit_key = "auto"
        self.auto_unit: Optional[spectra.YUnit] = None
        self.x_pref = "wave"                 # 使用者明确选过的横轴；没选过按波长
        self.window = 1
        self.smooth = False
        self.smooth_window = spectra.SMOOTH_DEFAULT_WINDOW
        self.smooth_order = spectra.SMOOTH_DEFAULT_ORDER
        self.style = P.DEFAULT_STYLE
        self.range_key = "全谱"
        self.dpi = 600

    # ------------------------------------------------------------- 曲线
    def curves(self) -> List[spectra.Spectrum]:
        return self.pinned + ([self.live] if self.live is not None else [])

    def by_id(self, cid: int) -> spectra.Spectrum:
        for s in self.curves():
            if id(s) == cid:
                return s
        raise KeyError("曲线不存在，可能已被删除。")

    def take_slot(self, spec, inherit=None) -> None:
        """颜色跟着曲线走：进图时分一格配色，此后不再变。当前那条继承上一条的格子。"""
        if spec.slot >= 0:
            return
        if inherit is not None and inherit.slot >= 0:
            spec.slot = inherit.slot
            return
        spec.slot = self.next_slot
        self.next_slot += 1

    def fresh_label(self) -> str:
        taken = {s.label for s in self.pinned}
        n = len(self.pinned) + 1
        while "P%d" % n in taken:
            n += 1
        return "P%d" % n

    def pin_live(self) -> str:
        if self.live is None:
            return "暂无可保留的曲线，请先在影像上取谱。"
        if len(self.pinned) >= MAX_CURVES:
            return "最多同时画 %d 条曲线。" % MAX_CURVES
        self.pinned.append(self.live)
        self.live = None
        return ""

    def add(self, spec: spectra.Spectrum) -> str:
        if len(self.pinned) >= MAX_CURVES:
            return "最多同时画 %d 条曲线。" % MAX_CURVES
        self.take_slot(spec)
        self.pinned.append(spec)
        return ""

    # ------------------------------------------------------------- 轴
    def axis_options(self):
        """``(画得成波长轴吗, 画得成波段序号轴吗)``——看图上的曲线，不看绑的影像。"""
        curves = [s for s in self.curves() if s.visible]
        if not curves:
            if self.reader is None:
                return (True, True)
            return (self.reader.axis()[1], True)
        wave = all(s.x_is_wavelength for s in curves)
        band = all(s.bands is not None and s.bands.size == s.x.size for s in curves)
        return (wave, band)

    @property
    def x_is_band(self) -> bool:
        wave, band = self.axis_options()
        if not wave:
            return True
        if not band:
            return False
        return self.x_pref == "band"

    def active_unit(self) -> spectra.YUnit:
        unit = spectra.unit_by_key(self.unit_key)
        if unit is not spectra.AUTO_UNIT:
            return unit
        shot = next((c for c in self.curves() if c.native_unit != "refl"), None)
        if shot is not None:
            return spectra.guess_unit(shot)
        if self.curves():
            return spectra.unit_by_key("refl")
        return self.auto_unit or spectra.unit_by_key("dn")

    def xlabel(self) -> str:
        return "Band number" if self.x_is_band else "Wavelength (nm)"

    def color_of(self, spec):
        slot = spec.slot if spec.slot >= 0 else 0
        color, dash = P.style_for(slot, self.style.color_set)
        return (spec.color or color, dash)

    def series(self, for_export: bool = False) -> List[P.Series]:
        """当前要画的曲线。先挖水汽再平滑，每条按自己的量纲换算。"""
        unit = self.active_unit()
        gaps = (P.WATER_GAPS if self.style.gap_mode in (P.GAP_BREAK, P.GAP_SHADE) else ())
        band_axis = self.x_is_band
        out: List[P.Series] = []
        rows = [(s, False) for s in self.pinned if s.visible]
        if self.live is not None and self.live.visible:
            rows.append((self.live, True))
        for spec, live in rows:
            color, dash = self.color_of(spec)
            x = spec.bands if (band_axis and spec.bands is not None) else spec.x
            y = spec.y
            if gaps and spec.x_is_wavelength:
                y = spectra.mask_ranges(spec.x, y, gaps)
            if self.smooth:
                y = spectra.savgol(y, self.smooth_window, self.smooth_order)
            divisor = unit.native_divisor if spec.native_unit == "refl" else unit.divisor
            if divisor != 1.0:
                y = y / divisor
            out.append(P.Series(spec.label, x, y, color, dash,
                                emphasis=live and not for_export,
                                reference=spec.origin == "library"))
        return out

    def styled(self) -> P.PlotStyle:
        rows = self.series()
        width = P.measure_legend_mm(rows, self.style)
        return replace(self.style, legend_width_mm=width)

    def note(self) -> str:
        """说明行：横轴为什么是现在这样，下一步能做什么。"""
        wave, _band = self.axis_options()
        if self.reader is not None and not self.reader.axis()[1] and not self.curves():
            return ("「%s」没有波长表，横轴按波段序号。可以在「图层属性」里加载传感器波长表。"
                    % self.reader.name)
        if not wave:
            return "部分曲线没有波长信息，横轴按波段序号显示；水汽波段标注、诊断吸收位置与光谱库匹配不可用。"
        return ""

    def curve_json(self, s: spectra.Spectrum) -> Dict[str, Any]:
        color, _ = self.color_of(s)
        return {"id": id(s), "label": s.label, "color": color, "custom_color": bool(s.color),
                "visible": s.visible, "live": s is self.live, "origin": s.origin,
                "wavelength": s.x_is_wavelength, "valid": s.valid_count,
                "coord": s.coord_text() if s.origin == "image" else "",
                "lon": s.lon, "lat": s.lat, "note": s.note, "layer": s.layer_name}

    def state(self) -> Dict[str, Any]:
        wave, band = self.axis_options()
        unit = self.active_unit()
        return {
            "layer_id": self.layer_id,
            "layer_name": self.reader.name if self.reader else "",
            "curves": [self.curve_json(s) for s in self.curves()],
            "x_band": self.x_is_band, "x_options": {"wave": wave, "band": band},
            "unit": self.unit_key, "unit_active": unit.key, "unit_label": unit.menu,
            "window": self.window, "smooth": self.smooth,
            "smooth_window": self.smooth_window, "smooth_order": self.smooth_order,
            "style": asdict(self.style), "range": self.range_key, "dpi": self.dpi,
            "note": self.note(),
            # 采过点的位置给画布标记用（只有影像上取的才有经纬度）
            "marks": [{"lon": s.lon, "lat": s.lat, "color": self.color_of(s)[0],
                       "live": s is self.live}
                      for s in self.curves() if s.origin == "image" and s.visible],
        }


class PickBody(BaseModel):
    lon: float
    lat: float
    append: bool = False


class BindBody(BaseModel):
    layer_id: Optional[int] = None


class OptionsBody(BaseModel):
    unit: Optional[str] = None
    x: Optional[str] = None                   # wave / band
    window: Optional[int] = None
    smooth: Optional[bool] = None
    smooth_window: Optional[int] = None
    smooth_order: Optional[int] = None
    range: Optional[str] = None
    custom_range: Optional[List[float]] = None
    dpi: Optional[int] = None
    style: Optional[Dict[str, Any]] = None
    preset: Optional[str] = None              # 图幅预设名


class CurvePatch(BaseModel):
    label: Optional[str] = None
    color: Optional[str] = None               # "" 恢复默认颜色
    visible: Optional[bool] = None


class IdsBody(BaseModel):
    ids: List[Any]


class PathBody(BaseModel):
    path: str


class ExportBody(BaseModel):
    path: str
    kind: str = "figure"                      # figure / csv


class SearchBody(BaseModel):
    text: str = ""
    category: str = ""
    group: str = ""
    library: str = ""
    covers: Optional[List[float]] = None
    limit: int = 2000


class MatchBody(BaseModel):
    curve_id: int
    #: 比哪些谱：给了 ``entry_ids`` 就比这几条，否则按下面的检索条件取全部命中的
    entry_ids: Optional[List[str]] = None
    text: str = ""
    category: str = ""
    group: str = ""
    library: str = ""
    algorithm: str = specmatch.DEFAULT_ALGORITHM
    continuum: bool = True
    mask_water: bool = True
    span: Optional[List[float]] = None
    #: 只留完整覆盖比较范围的库谱；端点容差（nm）
    full_cover: bool = True
    edge_tolerance: float = specmatch.EDGE_TOLERANCE
    top: int = 50


class SaveCurvesBody(BaseModel):
    curve_ids: List[int]
    folder: str = ""
    names: Optional[Dict[str, str]] = None


class EntryPatch(BaseModel):
    name: Optional[str] = None
    note: Optional[str] = None
    cls: Optional[str] = None


class FolderBody(BaseModel):
    path: str
    name: str = ""
    #: 删除分类时底下的谱怎么办：move 挪到上一级 / delete 一起删；空串只删空分类
    mode: str = ""
    #: 挪分类时的新上级（空串是「我的光谱」根下）
    parent: str = ""


class ImportBody(BaseModel):
    path: str
    #: 放进哪个分类；空串放在「我的光谱」根下。.slb 自带的分类挂到它下面
    folder: str = ""


class MoveEntriesBody(BaseModel):
    ids: List[str]
    folder: str


class LibExportBody(BaseModel):
    ids: List[str]
    path: str
    kind: str = "slb"                         # slb / sli / csv


class GroupBody(BaseModel):
    keys: List[str]                           # 选中的类别（SampleSource 的 group key）


def _float_list(a) -> List[Optional[float]]:
    return [None if not np.isfinite(v) else round(float(v), 6) for v in np.asarray(a, dtype=np.float64)]


def register(app, st, guard) -> None:
    board = CurveBoard()
    st.curves = board

    def layer_of(layer_id: Optional[int]):
        if layer_id is None:
            return None
        layer = getattr(st, "layers", {}).get(int(layer_id))
        if layer is None:
            raise HTTPException(404, "图层不存在，可能已被移除。")
        if not isinstance(layer, L.RasterLayer):
            raise HTTPException(400, "「%s」不是影像图层。" % layer.name)
        return layer

    def libraries():
        return speclib.all_libraries()

    def entry_of(eid: str) -> speclib.Entry:
        for lib in libraries():
            e = lib.get(eid)
            if e is not None:
                return e
        raise HTTPException(404, "光谱库中没有该条目：%s" % eid)

    def lib_for(e: speclib.Entry):
        return next(l for l in libraries() if l.key == e.library)

    # ------------------------------------------------------------- 曲线窗口
    @app.get("/api/spectra/meta")
    def meta():
        """下拉框要的那些固定选项。"""
        return {
            "units": [{"key": u.key, "menu": u.menu} for u in spectra.Y_UNITS],
            "windows": WINDOW_SIZES,
            "ranges": [{"key": name, "span": span, "feature": key} for name, span, key in spectra.RANGE_PRESETS],
            "presets": [{"name": n, "w": w, "h": h} for n, w, h in P.FIGURE_PRESETS],
            "color_sets": [{"key": k, "name": n, "colors": list(P.COLOR_SETS[k])} for k, n in P.COLOR_SET_NAMES],
            "gap_modes": [{"key": k, "name": n} for k, n in P.GAP_MODES],
            "feature_modes": [{"key": k, "name": n} for k, n in P.FEATURE_MODES],
            "marker_modes": [{"key": k, "name": n} for k, n in P.MARKER_MODES],
            "legend_places": [{"key": k, "name": n} for k, n in P.LEGEND_PLACES],
            "fonts": [6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0],
            "dpis": [300, 600, 900, 1200],
            "line_width": list(P.LINE_WIDTH_RANGE), "marker_size": list(P.MARKER_SIZE_RANGE),
            "smooth_window": list(spectra.SMOOTH_WINDOW_RANGE), "smooth_order": list(spectra.SMOOTH_ORDER_RANGE),
            "algorithms": [{"key": a.key, "name": a.cn, "tip": a.tip} for a in specmatch.ALGORITHMS],
            "edge_tolerance": [specmatch.EDGE_TOLERANCE, specmatch.EDGE_TOLERANCE_MAX],
            "library": bool(speclib.standard_library()),
        }

    @app.get("/api/spectra/state")
    def state():
        return board.state()

    @app.post("/api/spectra/bind")
    def bind(body: BindBody):
        """把窗口绑到一幅影像上（None 解绑）。没有影像也能开窗口：翻库、导 CSV、出图。"""
        layer = layer_of(body.layer_id)
        if layer is None:
            board.reader, board.layer_id = None, None
            return board.state()
        reader = spectra.SpectrumReader(layer)
        if not reader.ready:
            raise HTTPException(400, reader.error or "无法从该影像读取光谱。")
        board.reader, board.layer_id = reader, layer.uid
        return board.state()

    @app.post("/api/spectra/pick")
    def pick(body: PickBody):
        """画布上点了一下。Shift 单击把当前这条留在图上。"""
        if board.reader is None:
            raise HTTPException(409, "曲线窗口尚未绑定影像。请右键影像图层，选择「光谱曲线…」。")
        spec = board.reader.read(body.lon, body.lat, board.window, label=board.fresh_label())
        if spec is None:
            return {"message": "该位置不在「%s」范围内，或该处没有有效值。" % board.reader.name,
                    **board.state()}
        if board.unit_key == "auto" and not board.pinned and board.live is None:
            board.auto_unit = board.reader.guess_unit(spec)
        board.take_slot(spec, inherit=board.live)
        board.live = spec
        msg = "%s：%s，%d 个有效波段。" % (spec.label, spec.coord_text(), spec.valid_count)
        if body.append:
            problem = board.pin_live()
            msg = problem or msg
        return {"message": msg, **board.state()}

    @app.post("/api/spectra/pin")
    def pin():
        problem = board.pin_live()
        return {"message": problem, **board.state()}

    @app.post("/api/spectra/options")
    def options(body: OptionsBody):
        if body.unit is not None:
            board.unit_key = body.unit
        if body.x in ("wave", "band"):
            board.x_pref = body.x
        if body.window in WINDOW_SIZES:
            board.window = int(body.window)
        if body.smooth is not None:
            board.smooth = bool(body.smooth)
        if body.smooth_window:
            board.smooth_window = int(body.smooth_window) | 1      # 窗口要奇数
        if body.smooth_order:
            board.smooth_order = int(body.smooth_order)
        if body.dpi:
            board.dpi = int(body.dpi)
        style = board.style
        if body.preset:
            hit = next(((w, h) for n, w, h in P.FIGURE_PRESETS if n == body.preset), None)
            if hit:
                style = replace(style, width_mm=hit[0], height_mm=hit[1])
        if body.style:
            allowed = {k: v for k, v in body.style.items()
                       if k in P.PlotStyle.__dataclass_fields__ and k not in ("xlim", "legend_width_mm")}
            style = replace(style, **allowed)
        if body.range is not None:
            hit = next(((span, key) for name, span, key in spectra.RANGE_PRESETS if name == body.range), None)
            if body.range == "自定义" and body.custom_range and len(body.custom_range) == 2:
                lo, hi = sorted(float(v) for v in body.custom_range)
                style = replace(style, xlim=(lo, hi), highlight="")
                board.range_key = "自定义"
            elif hit is not None:
                span, key = hit
                style = replace(style, xlim=tuple(span) if span else None, highlight=key)
                board.range_key = body.range
        elif body.custom_range is not None:
            if len(body.custom_range) == 2:
                lo, hi = sorted(float(v) for v in body.custom_range)
                style = replace(style, xlim=(lo, hi), highlight="")
                board.range_key = "自定义"
            else:
                style = replace(style, xlim=None, highlight="")
                board.range_key = "全谱"
        board.style = style
        return board.state()

    @app.patch("/api/spectra/curves/{cid}")
    def patch_curve(cid: int, body: CurvePatch):
        def run():
            s = board.by_id(cid)
            if body.label is not None and body.label.strip():
                s.label = body.label.strip()
            if body.color is not None:
                s.color = body.color
            if body.visible is not None:
                s.visible = bool(body.visible)
            return board.state()
        return guard(run)

    @app.post("/api/spectra/curves/delete")
    def delete_curves(body: IdsBody):
        doomed = {int(i) for i in body.ids}
        board.pinned = [s for s in board.pinned if id(s) not in doomed]
        if board.live is not None and id(board.live) in doomed:
            board.live = None
        return board.state()

    @app.post("/api/spectra/clear")
    def clear():
        board.pinned, board.live = [], None
        return board.state()

    @app.get("/api/spectra/plot")
    def render(width: int = 900, height: int = 420, scale: float = 1.0):
        """按面板大小出一张图。图幅锁定宽高比、等比放大（字号线宽同乘一个系数）。"""
        style = board.styled()
        rows = board.series()
        w_mm, h_mm = style.total_width_mm, style.height_mm
        # 按面板放得下的最大比例放大，屏幕上就是导出那张图等比放大的样子
        # 图幅按 CSS 像素放进面板（100 dpi 时 1 英寸 = 100 CSS 像素），再按屏幕像素比
        # 提高 dpi 出图：Retina 屏上一个 CSS 像素对应两个图像像素，曲线和字才清楚
        k = max(0.2, min(width / (w_mm / 25.4 * 100.0), height / (h_mm / 25.4 * 100.0)))
        dpi = 100.0 * min(3.0, max(1.0, float(scale)))
        shown = style.scaled(k)
        figure = P.make_figure(shown)
        # 没有曲线时的提示由页面画在图上（随主题配色），图里不再重复写一遍
        ax = P.draw_into(figure, rows, board.xlabel(), board.active_unit().label, shown)
        buf = io.BytesIO()
        from matplotlib import rc_context
        with rc_context(P.rc_for(shown)):
            figure.savefig(buf, format="png", dpi=dpi)
        meta: Dict[str, Any] = {"png": base64.b64encode(buf.getvalue()).decode("ascii")}
        if ax is not None:
            figure.canvas.draw() if hasattr(figure, "canvas") else None
            box = ax.get_position()
            meta.update(axes=[box.x0, box.y0, box.x1, box.y1],
                         xlim=list(ax.get_xlim()), ylim=list(ax.get_ylim()),
                         series=[{"label": s.label, "color": s.color, "x": _float_list(s.x), "y": _float_list(s.y)}
                                 for s in rows])
        return meta

    @app.post("/api/spectra/export")
    def export(body: ExportBody):
        def run():
            if not board.curves():
                raise ValueError("图中暂无曲线。")
            path = body.path
            if body.kind == "csv":
                if not path.lower().endswith(".csv"):
                    path += ".csv"
                gaps = (P.WATER_GAPS if board.style.gap_mode in (P.GAP_BREAK, P.GAP_SHADE) else ())
                spectra.to_csv(path, [s for s in board.curves() if s.visible], board.active_unit(), gaps)
            else:
                ext = os.path.splitext(path)[1].lower()
                if ext not in (".png", ".pdf", ".svg", ".eps", ".tif", ".tiff"):
                    path += ".png"
                P.save_figure(path, board.series(for_export=True), board.xlabel(),
                              board.active_unit().label, board.styled(), dpi=board.dpi)
            return {"path": path}
        return guard(run)

    @app.post("/api/spectra/import")
    def import_csv(body: PathBody):
        """从 CSV（本窗口导出的那种）或任意光谱表 / ENVI 光谱库读曲线进来。"""
        def run():
            if body.path.lower().endswith(".csv"):
                try:
                    specs, notes = spectra.from_csv(body.path), []
                except Exception:
                    loaded = speclib_io.read_any(body.path)
                    specs, notes = loaded.spectra, loaded.notes
            else:
                loaded = speclib_io.read_any(body.path)
                specs, notes = loaded.spectra, loaded.notes
            added = 0
            for s in specs:
                if s.origin == "image":
                    s.origin = "csv"
                if board.add(s):
                    break
                added += 1
            return {"added": added, "notes": notes[:8], **board.state()}
        return guard(run)

    # ------------------------------------------------------------- 光谱库
    @app.get("/api/speclib/tree")
    def tree():
        user = speclib.user_library()
        # 随包的库按分类列；「我的光谱」单独成一支，不和随包的分类混在一起
        cats = speclib.category_tree([lib for lib in libraries() if lib.key != user.key])
        groups = []
        std = speclib.standard_library()
        if std is not None:
            groups = std.groups
        # 条数按库里现有的条目现算（与点下去列出来的一致），不用库文件里记的那份
        by_cat, by_group, total, grouped = live_counts()
        return {"categories": [{"path": c.path, "label": "%s（%d）" % (c.cn, by_cat.get(c.path, 0)),
                                "cn": c.cn, "count": by_cat.get(c.path, 0), "depth": c.depth}
                               for c in cats],
                "groups": [dict(g, count=by_group.get(str(g.get("key")), 0)) for g in groups],
                "total": total, "grouped": grouped,
                "user_folders": user.folders, "user_tree": user_tree(user),
                "user_count": len(user.entries),
                "user_root": speclib.USER_ROOT, "user_root_cn": speclib.USER_ROOT_CN}

    def live_counts():
        """每个分类（含下级）、每个蚀变组合各有几条谱，总条数，以及挂了任一蚀变组合的
        条数。口径同检索：不算被新版取代的老条目，「我的光谱」里挂在标准分类下的也算进
        那一支。"""
        by_cat: Dict[str, int] = {}
        by_group: Dict[str, int] = {}
        total = grouped = 0
        for lib in libraries():
            for e in lib.entries:
                if e.superseded:
                    continue
                total += 1
                seen = set()
                for path in e.categories:
                    parts = speclib.normalise_path(path).split("/")
                    seen.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
                for p in seen:
                    by_cat[p] = by_cat.get(p, 0) + 1
                for g in set(e.alt):
                    by_group[g] = by_group.get(g, 0) + 1
                grouped += 1 if e.alt else 0
        return by_cat, by_group, total, grouped

    def user_tree(user) -> List[Dict[str, Any]]:
        """「我的光谱」的分类树：路径逐级展开，父节点在前；名称用中文（英文路径段按随包
        库的分类译名），数量含子分类里的谱（主分类与附加分类都算）。"""
        paths = set()
        for p in list(user.folders) + [c for e in user.entries for c in e.categories]:
            p = speclib.normalise_path(p)
            if not p or p == speclib.USER_ROOT:
                continue
            parts = p.split("/")
            paths.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
        out = []
        for p in sorted(paths, key=lambda x: [seg.lower() for seg in x.split("/")]):
            # 一条谱可以同时挂在几个分类下（主分类 + 矿物类型），哪一支下都算它
            n = sum(1 for e in user.entries if any(c == p or c.startswith(p + "/") for c in e.categories))
            out.append({"path": p, "name": speclib.category_name(p), "count": n})
        return out

    @app.post("/api/speclib/search")
    def search(body: SearchBody):
        q = speclib.Query(text=body.text, category=body.category, group=body.group,
                          library=body.library, limit=max(1, min(body.limit, 1_000_000)),
                          covers=tuple(body.covers) if body.covers else None)
        rows = speclib.search(q, libraries())
        return [{"id": e.id, "name": e.name, "cn": e.cn, "display": e.display,
                 "library": e.library, "source": e.source, "sample": e.sample,
                 "wl": [e.wl_lo, e.wl_hi], "count": e.count, "cls": e.cls,
                 "formula": e.formula} for e in rows]

    @app.get("/api/speclib/entries/{eid:path}")
    def entry(eid: str):
        e = entry_of(eid)
        x, y = lib_for(e).values(e)
        return {"id": e.id, "title": e.title, "detail": e.detail(), "library": e.library,
                "x": _float_list(x), "y": _float_list(y), "cls": e.cls, "note": e.note,
                "name": e.name, "cn": e.cn}

    @app.post("/api/speclib/add-to-plot")
    def add_to_plot(body: IdsBody):
        """把库里的谱加到图上当参照。"""
        added, problem = 0, ""
        for eid in body.ids:
            e = entry_of(str(eid))
            spec = lib_for(e).spectrum(e, label=e.cn or e.name)
            problem = board.add(spec)
            if problem:
                break
            added += 1
        return {"added": added, "message": problem, **board.state()}

    @app.post("/api/speclib/match")
    def match(body: MatchBody):
        """拿图上的一条谱去比列表里的那些。比的就是当前列表——先框窄了再比，名次才读得下去。"""
        def work(task: Task):
            spec = board.by_id(body.curve_id)
            if not spec.x_is_wavelength:
                raise ValueError("该曲线没有波长信息，无法与光谱库匹配。")
            y = np.asarray(spec.y, dtype=np.float64)
            unit = board.active_unit()
            divisor = unit.native_divisor if spec.native_unit == "refl" else unit.divisor
            if divisor != 1.0:
                y = y / divisor
            if body.entry_ids is not None:
                entries = [entry_of(i) for i in body.entry_ids]
            else:
                entries = speclib.search(speclib.Query(text=body.text, category=body.category,
                                                       group=body.group, library=body.library,
                                                       limit=1_000_000), libraries())
            opts = specmatch.MatchOptions(algorithm=body.algorithm, continuum=body.continuum,
                                          mask_water=body.mask_water,
                                          span=tuple(body.span) if body.span else None,
                                          full_cover=body.full_cover,
                                          edge_tolerance=body.edge_tolerance)
            rep = specmatch.match_report(spec.x, y, entries, libraries(), opts,
                                         progress=lambda d, t: task(d, t, "正在匹配…"))
            algo = specmatch.ALGORITHM_BY_KEY.get(body.algorithm) or specmatch.ALGORITHMS[0]
            nan = lambda v: None if not np.isfinite(v) else round(float(v), 1)
            return {"curve": spec.label, "algorithm": algo.cn, "metric_name": algo.metric_name,
                    "compared": rep.compared, "skipped_cover": rep.skipped_cover,
                    "skipped_bands": rep.skipped_bands, "lo": nan(rep.lo), "hi": nan(rep.hi),
                    "tolerance": round(rep.tolerance, 1), "full_cover": body.full_cover,
                    "hits": [{"id": h.entry.id, "display": h.entry.display, "score": round(h.score * 100, 1),
                              "metric": specmatch.metric_text(algo, h.metric), "bands": h.bands,
                              "lo": nan(h.lo), "hi": nan(h.hi), "coverage": round(h.coverage * 100, 1)}
                             for h in rep.hits[:max(1, body.top)]]}
        try:
            task = st.tasks.start("光谱匹配", work, exclusive=False)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"task": task.to_json()}

    # ------------------------------------------------------------- 我的光谱
    def user_lib() -> speclib.UserLibrary:
        if not st.remember and not getattr(st, "user_lib_ok", False):
            # 测试与无状态运行不碰使用者真正的「我的光谱」文件
            raise HTTPException(409, "测试模式下不写入「我的光谱」。")
        return speclib.user_library()

    @app.post("/api/speclib/user/save")
    def save_curves(body: SaveCurvesBody):
        def run():
            lib = user_lib()
            saved = []
            for cid in body.curve_ids:
                s = board.by_id(int(cid))
                name = (body.names or {}).get(str(cid)) or s.label
                e = lib.add(s, name, cls=body.folder, lon=s.lon if s.origin == "image" else None,
                            lat=s.lat if s.origin == "image" else None, note=s.note)
                if e is not None:
                    saved.append(e.id)
            return {"saved": len(saved)}
        return guard(run)

    @app.patch("/api/speclib/user/entries/{eid:path}")
    def patch_entry(eid: str, body: EntryPatch):
        def run():
            lib = user_lib()
            if body.name is not None:
                lib.rename(eid, body.name)
            if body.note is not None:
                lib.update(eid, note=body.note)
            if body.cls is not None:
                lib.move([eid], body.cls)
            return {"ok": True}
        return guard(run)

    @app.post("/api/speclib/user/delete")
    def delete_entries(body: IdsBody):
        def run():
            lib = user_lib()
            n = sum(1 for i in body.ids if lib.remove(str(i)))
            return {"removed": n}
        return guard(run)

    @app.post("/api/speclib/user/move")
    def move_entries(body: MoveEntriesBody):
        return guard(lambda: {"moved": user_lib().move(body.ids, body.folder)})

    @app.post("/api/speclib/user/folders")
    def add_folder(body: FolderBody):
        return guard(lambda: {"path": user_lib().add_folder(body.path)})

    @app.post("/api/speclib/user/folders/rename")
    def rename_folder(body: FolderBody):
        return guard(lambda: {"path": user_lib().rename_folder(body.path, body.name)})

    @app.post("/api/speclib/user/folders/delete")
    def delete_folder(body: FolderBody):
        return guard(lambda: {"ok": user_lib().remove_folder(body.path, body.mode)})

    @app.post("/api/speclib/user/folders/move")
    def move_folder(body: FolderBody):
        """整枝挪分类：子分类与里面的谱跟着走，层级不变。"""
        return guard(lambda: {"path": user_lib().move_folder(body.path, body.parent)})

    @app.get("/api/speclib/user/folders/usage")
    def folder_usage(path: str):
        return guard(lambda: user_lib().folder_usage(path))

    @app.post("/api/speclib/user/import")
    def import_lib(body: ImportBody):
        """野外光谱仪文本表、ENVI 光谱库、.slb 读进「我的光谱」的 ``folder`` 分类。"""
        def run():
            lib = user_lib()
            folder = speclib.normalise_path(body.folder)
            if body.path.lower().endswith(".slb"):
                added, skipped = lib.merge(body.path, under=folder)
                return {"added": added, "skipped": skipped, "notes": []}
            loaded = speclib_io.read_any(body.path)
            added, skipped = 0, 0
            for s in loaded.spectra:
                try:
                    lib.add(s, s.label, cls=folder, save=False, reindex=False)
                    added += 1
                except speclib.LibraryError:
                    skipped += 1
            lib._reindex()
            lib.save()
            return {"added": added, "skipped": skipped, "notes": loaded.notes[:8]}
        return guard(run)

    @app.post("/api/speclib/export")
    def export_lib(body: LibExportBody):
        def run():
            entries = [entry_of(i) for i in body.ids]
            n = speclib_io.export(body.kind, entries, body.path, libraries())
            return {"exported": n, "path": body.path}
        return guard(run)

    # ------------------------------------------------------------- 类别平均谱
    @app.get("/api/spectra/groups")
    def groups():
        """样本库里能在绑定影像上算平均谱的类别（只列落在影像范围内的）。"""
        def run():
            if board.reader is None:
                raise ValueError("计算类别平均谱需要先绑定影像。请右键影像图层，选择「光谱曲线…」。")
            src = groupspec.SampleSource(st.session)
            return [{"key": g.key, "name": g.name, "count": g.count, "color": g.color,
                     "parent": g.parent, "detail": g.detail} for g in src.groups(board.reader)]
        return guard(run)

    @app.post("/api/spectra/group-means")
    def group_means(body: GroupBody):
        if board.reader is None:
            raise HTTPException(409, "计算类别平均谱需要先绑定影像。")

        def work(task: Task):
            src = groupspec.SampleSource(st.session)
            all_groups = {g.key: g for g in src.groups(board.reader)}
            picked = [all_groups[k] for k in body.keys if k in all_groups]
            results = groupspec.mean_spectra(src, picked, board.reader, progress=task)
            added, problems = 0, []
            for r in results:
                if r.spectrum is None:
                    problems.append("%s：%s" % (r.group.name, r.problem))
                    continue
                r.spectrum.origin = "image"
                r.spectrum.note = "类别平均谱"
                if board.add(r.spectrum):
                    problems.append("最多同时画 %d 条曲线。" % MAX_CURVES)
                    break
                added += 1
            return {"added": added, "problems": problems}

        try:
            task = st.tasks.start("按类别取平均谱", work, text="正在读取像元…")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"task": task.to_json()}

