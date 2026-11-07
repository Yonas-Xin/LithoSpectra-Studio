# -*- coding: utf-8 -*-
"""HTTP 接口。

两类写操作：

* **类别结构**（新建、改名、改色、移动）直接落库，走
  :class:`~samplelib.session.LibrarySession`，撤销栈在会话里；
* **样本几何与归属**（画点、画面、改顶点、删除、改归、删除类别）先进
  :class:`~samplelib.annotate.editing.EditBuffer` 暂存，点「保存」才一次性写库。

库被会话改过之后调 ``buffer.rebase()``，暂存区重读底稿、草稿尽量保留；暂存区
提交之后调 ``session.reload()``。两个撤销栈由 ``_history`` 按发生顺序串起来，
界面上只有一个「撤销」。

GDAL 数据集不是线程安全的，而 FastAPI 的同步接口跑在线程池里，所有碰样本库的
调用都包在同一把锁里。导入、导出这类长任务在后台线程里整段持锁，见 :mod:`.tasks`。

**勾选**记在 ``session.selection`` 里，逐条样本，是导出、裁剪的工作集；
画布上的「选中」是另一回事，只在前端。
"""

import os
import threading
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from samplelib import appstate, config
from samplelib.annotate import tiles
from samplelib.annotate.editing import EditBuffer
from samplelib.model import key_to_str, str_to_key
from samplelib.session import LibrarySession

from .geojson import geometry_to_coords, record_to_feature
from .tasks import TaskManager

LIB = "lib"
BUF = "buf"


class AppState:
    """进程内唯一的一份会话状态。"""

    def __init__(self, remember: bool = True) -> None:
        # remember：是否把打开过的库写进 state/session.json。测试里关掉，免得
        # 临时文件混进使用者的「最近打开」。
        self.remember = remember
        self.lock = threading.RLock()
        self.session = LibrarySession()
        self.buffer: Optional[EditBuffer] = None
        self.history: List[str] = []
        self.fetcher = tiles.TileFetcher()
        self.sources = list(tiles.build_sources().values())
        self.tasks = TaskManager(self.lock)
        # 导出层级标记：分支级的「整支合并 / 按子类分别成文件」，与勾选正交
        self.export_merge: set = set()
        self.export_split: set = set()
        # 上一次发给页面的样本要素快照：键 -> (类别, 名称, 几何类型, 坐标)。页面带着
        # 快照号来取，就只回这之后变了的那些，见 /api/features
        self.feature_token = 0
        self.feature_snap: Optional[Dict[Any, tuple]] = None

    # ------------------------------------------------------------- 生命周期
    def open(self, path: str) -> None:
        self.session.open(path)
        self.reset()
        self.remember_library()

    def create(self, path: str, name: str, overwrite: bool = False) -> None:
        self.session.create(path, name=name, overwrite=overwrite)
        self.reset()
        self.remember_library()

    def close(self) -> None:
        self.session.close()
        self.buffer = None
        self.history = []
        self.export_merge.clear()
        self.export_split.clear()

    def remember_library(self) -> None:
        if self.remember:
            remember_library(self.session.library_path)

    def remember_folder(self, key: str, folder: str) -> None:
        if self.remember:
            remember_folder(key, folder)

    def reset(self) -> None:
        """换了库（打开、新建、还原）：暂存区、撤销链、导出标记都跟着旧库走，重来。"""
        self.buffer = EditBuffer(self.session.require_store()) if self.session.is_open else None
        self.history = []
        self.export_merge.clear()
        self.export_split.clear()

    def require_buffer(self) -> EditBuffer:
        if self.buffer is None:
            raise HTTPException(409, "尚未打开样本库。")
        return self.buffer

    # ------------------------------------------------------------- 写操作
    def lib_changed(self) -> Dict[str, int]:
        """会话改了库：暂存区重读底稿。"""
        self.history.append(LIB)
        dropped = self.require_buffer().rebase()
        if any(dropped.values()):
            self.history = [h for h in self.history if h != BUF]
        return dropped

    def lib_rewritten(self) -> None:
        """导入这类不进撤销栈的写库：暂存区重读底稿，撤销链里会话那一侧照旧。"""
        if self.buffer is not None:
            self.buffer.rebase()

    def buf_changed(self) -> None:
        self.history.append(BUF)

    def undo(self) -> str:
        buf = self.require_buffer()
        while self.history:
            which = self.history.pop()
            if which == BUF and buf.can_undo:
                return buf.undo()
            if which == LIB and self.session.can_undo:
                label = self.session.undo()
                buf.rebase()
                return label
        return ""

    # ------------------------------------------------------------- 只读视图
    def summary(self) -> Dict[str, Any]:
        s = self.session
        buf = self.buffer
        n_sel, n_sel_cls = s.selection_summary() if s.is_open else (0, 0)
        return {
            "open": s.is_open,
            "name": s.library_name,
            "path": s.library_path,
            "selected": n_sel,
            "selected_classes": n_sel_cls,
            "version": buf.version if buf else 0,
            "dirty": bool(buf and buf.dirty),
            "has_deletions": bool(buf and buf.has_deletions),
            "pending": buf.summary() if buf else "",
            "can_undo": bool(buf and (buf.can_undo or s.can_undo)),
            "can_redo": bool(buf and buf.can_redo),
        }

    def classes(self) -> List[Dict[str, Any]]:
        buf = self.require_buffer()
        counts: Dict[int, int] = {}
        for rec in buf.records():
            counts[rec.class_id] = counts.get(rec.class_id, 0) + 1
        classes = buf.classes()
        by_id = {c.class_id: c for c in classes}
        totals = dict(counts)
        for c in classes:
            n = counts.get(c.class_id, 0)
            pid, guard = c.parent_id, set()
            while pid is not None and pid in by_id and pid not in guard:
                guard.add(pid)
                totals[pid] = totals.get(pid, 0) + n
                pid = by_id[pid].parent_id
        # 勾选状态（none / partial / all）：先数每类直接被勾的样本，再沿父链累加，
        # 与该支的库内样本总数比。逐类调 class_selection_state 是平方级的。
        s = self.session
        lib_total: Dict[int, int] = {}
        sel_total: Dict[int, int] = {}
        for cid, recs in s.records_by_class.items():
            lib_total[cid] = lib_total.get(cid, 0) + len(recs)
        for key in s.selection:
            rec = s.record_by_key.get(key)
            if rec is not None:
                sel_total[rec.class_id] = sel_total.get(rec.class_id, 0) + 1
        lib_sub, sel_sub = dict(lib_total), dict(sel_total)
        for c in classes:
            pid, guard = c.parent_id, set()
            while pid is not None and pid in by_id and pid not in guard:
                guard.add(pid)
                lib_sub[pid] = lib_sub.get(pid, 0) + lib_total.get(c.class_id, 0)
                sel_sub[pid] = sel_sub.get(pid, 0) + sel_total.get(c.class_id, 0)
                pid = by_id[pid].parent_id
        alive = set(by_id)
        self.export_merge &= alive
        self.export_split &= alive

        def check_state(cid: int) -> str:
            n = sel_sub.get(cid, 0)
            if not n:
                return "none"
            return "all" if n >= lib_sub.get(cid, 0) else "partial"

        out = []
        for c in classes:
            out.append({
                "checked": check_state(c.class_id),
                "export_level": ("merge" if c.class_id in self.export_merge else
                                 "split" if c.class_id in self.export_split else "default"),
                "id": c.class_id,
                "parent_id": c.parent_id if c.parent_id in by_id else None,
                "name": c.display_name,
                "region": c.region,
                "color": c.color,
                "kind": buf.class_kind(c.class_id),
                "is_group": c.is_group,
                "description": c.description,
                "source": c.source,
                "attrs": c.attrs,
                "count": counts.get(c.class_id, 0),
                "total": totals.get(c.class_id, 0),
                "draft": c.class_id < 0,
                "edited": c.edited,
            })
        return out


# =============================================================================
# 请求体
# =============================================================================

class PathBody(BaseModel):
    path: str
    name: str = ""


class SaveBody(BaseModel):
    # 「只显示勾选」开着时，把刚落库的草稿勾上——否则存完它们就从画布上消失了
    auto_check: bool = False


class SelectionBody(BaseModel):
    op: str                                  # set / add / remove / all / none / invert
    class_ids: Optional[List[int]] = None
    keys: Optional[List[str]] = None


class ExportLevelBody(BaseModel):
    mode: str                                # default / merge / split


class ClassPatch(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None


class NewClass(BaseModel):
    name: str
    region: str = ""
    parent_id: Optional[int] = None
    group: bool = False


class MoveClass(BaseModel):
    parent_id: Optional[int] = None
    before_id: Optional[int] = None
    region: Optional[str] = None


class PromoteBody(BaseModel):
    other_id: int
    name: str = ""


class IdsBody(BaseModel):
    ids: List[int]


class NewFeature(BaseModel):
    class_id: int
    geometry: Dict[str, Any]
    label: str = ""


class GeometryBody(BaseModel):
    geometry: Dict[str, Any]


class LabelBody(BaseModel):
    label: str


class KeysBody(BaseModel):
    keys: List[str]
    class_id: Optional[int] = None


# =============================================================================
# 应用
# =============================================================================

def create_app(library_path: str = "", static_dir: str = "",
               remember: bool = True) -> FastAPI:
    app = FastAPI(title="LithoSpectra Web API")
    st = AppState(remember=remember)
    app.state.lithospectra = st
    if library_path and os.path.exists(library_path):
        st.open(library_path)

    def guard(fn):
        """统一加锁，并把业务层的 ValueError / KeyError 与文件错误翻成 400。"""
        with st.lock:
            try:
                return fn()
            except HTTPException:
                raise
            except (ValueError, KeyError, RuntimeError) as exc:
                raise HTTPException(400, str(exc).strip("'\""))
            except OSError as exc:
                # 文件被挪走、没有写权限、磁盘满：给使用者看系统的原因
                raise HTTPException(400, "%s：%s" % (exc.strerror or "文件操作失败", exc.filename or exc))

    # ------------------------------------------------------------- 库
    @app.get("/api/state")
    def state():
        return guard(st.summary)

    @app.post("/api/library/open")
    def open_library(body: PathBody):
        def run():
            if not os.path.exists(body.path):
                raise ValueError("文件不存在：%s" % body.path)
            st.open(body.path)
            return st.summary()
        return guard(run)

    @app.post("/api/library/create")
    def create_library(body: PathBody):
        def run():
            path = body.path
            if not path.lower().endswith(config.LIBRARY_EXT):
                path += config.LIBRARY_EXT
            st.create(path, body.name or os.path.splitext(os.path.basename(path))[0],
                      overwrite=True)
            return st.summary()
        return guard(run)

    @app.post("/api/library/reload")
    def reload_library():
        """重新从样本库载入：丢掉暂存区里没保存的改动，按库里的样子重读。"""
        def run():
            st.session.reload()
            st.require_buffer().reload()
            st.history = [h for h in st.history if h != BUF]
            return st.summary()
        return guard(run)

    @app.post("/api/library/close")
    def close_library():
        def run():
            st.close()
            return st.summary()
        return guard(run)

    @app.get("/api/library/extent")
    def extent():
        return guard(lambda: {"extent": st.session.full_extent()})

    @app.post("/api/undo")
    def undo():
        return guard(lambda: {"label": st.undo(), **st.summary()})

    @app.post("/api/redo")
    def redo():
        def run():
            label = st.require_buffer().redo()
            if label:
                st.history.append(BUF)
            return {"label": label, **st.summary()}
        return guard(run)

    @app.post("/api/save")
    def save(body: SaveBody = SaveBody()):
        def run():
            result = st.require_buffer().commit()
            st.session.reload()
            st.require_buffer().reload()
            if body.auto_check:
                # 新画、改过的样本自动勾上：只显示勾选样本时，保存后它们不会从画布上消失
                st.session.select_keys(result.get("keys") or ())
            st.history = [h for h in st.history if h != BUF]
            return {"written": result.get("written", 0),
                    "deleted": result.get("deleted", 0),
                    "classes": result.get("classes", 0), **st.summary()}
        return guard(run)

    @app.post("/api/discard")
    def discard():
        def run():
            st.require_buffer().discard()
            st.history = [h for h in st.history if h != BUF]
            return st.summary()
        return guard(run)

    # ------------------------------------------------------------- 勾选
    @app.get("/api/selection")
    def selection():
        return guard(lambda: {"keys": [key_to_str(k) for k in st.session.selection]})

    @app.post("/api/selection")
    def set_selection(body: SelectionBody):
        def run():
            s = st.session
            s.require_store()
            keys = [str_to_key(k) for k in body.keys or ()]
            ids = [i for i in body.class_ids or () if i in s.class_map]
            if body.op == "all":
                s.select_all(ids or None)
            elif body.op == "none":
                s.clear_selection()
            elif body.op == "invert":
                s.invert_selection(ids or None)
            elif body.op in ("add", "remove"):
                on = body.op == "add"
                for cid in ids:
                    s.select_class(cid, on)
                s.select_keys(keys, on)
            elif body.op == "set":
                s.set_selection(set(keys) | s.keys_of_classes(ids))
            else:
                raise ValueError("未知的勾选操作：%s" % body.op)
            return {"keys": [key_to_str(k) for k in s.selection]}
        return guard(run)

    # ------------------------------------------------------------- 类别
    @app.post("/api/classes/{class_id}/export-level")
    def set_export_level(class_id: int, body: ExportLevelBody):
        """按分支标记导出层级。两个集合互斥，只作用于所在分支。"""
        def run():
            st.export_merge.discard(class_id)
            st.export_split.discard(class_id)
            if body.mode == "merge":
                st.export_merge.add(class_id)
            elif body.mode == "split":
                st.export_split.add(class_id)
            return {"ok": True}
        return guard(run)

    @app.post("/api/export-levels/clear")
    def clear_export_levels():
        def run():
            st.export_merge.clear()
            st.export_split.clear()
            return {"ok": True}
        return guard(run)

    @app.get("/api/classes")
    def classes():
        return guard(st.classes)

    @app.post("/api/classes")
    def add_class(body: NewClass):
        def run():
            if body.parent_id is not None and body.parent_id < 0:
                cid = st.require_buffer().add_class(
                    body.name, body.region, parent_id=body.parent_id,
                    geom_kind="group" if body.group else "")
                st.buf_changed()
            elif body.group:
                cid = st.session.add_group(body.name, body.region, body.parent_id)
                st.lib_changed()
            else:
                cid = st.session.add_class(body.name, body.region, body.parent_id)
                st.lib_changed()
            return {"id": cid}
        return guard(run)

    @app.patch("/api/classes/{class_id}")
    def patch_class(class_id: int, body: ClassPatch):
        def run():
            if class_id < 0:
                if body.name is not None:
                    st.require_buffer().rename_class(class_id, body.name)
                    st.buf_changed()
                return {"ok": True}
            s = st.session
            if body.name is not None and body.name != s.class_map[class_id].name:
                s.rename_class(class_id, body.name)
                st.lib_changed()
            if body.color is not None and body.color != s.class_map[class_id].color:
                s.set_class_color(class_id, body.color)
                st.lib_changed()
            if (body.description is not None
                    and body.description != s.class_map[class_id].description):
                s.set_class_description(class_id, body.description)
                st.lib_changed()
            return {"ok": True}
        return guard(run)

    @app.post("/api/classes/{class_id}/move")
    def move_class(class_id: int, body: MoveClass):
        def run():
            if class_id < 0 or (body.parent_id is not None and body.parent_id < 0):
                raise ValueError("新建的类别尚未保存，请保存后再调整位置。")
            st.session.reorder_class(class_id, body.parent_id,
                                     before_id=body.before_id, region=body.region)
            st.lib_changed()
            return {"ok": True}
        return guard(run)

    @app.post("/api/classes/{class_id}/promote")
    def promote_class(class_id: int, body: PromoteBody):
        """把别的类别拖到基础类别上：两者升一级，装进一个新分组（默认用基础类别的名字）。"""
        def run():
            if class_id < 0 or body.other_id < 0:
                raise ValueError("新建的类别尚未保存，请保存后再调整位置。")
            name = body.name or st.session.class_map[class_id].name
            gid = st.session.promote_to_group(class_id, body.other_id, name)
            st.lib_changed()
            return {"id": gid}
        return guard(run)

    @app.post("/api/classes/delete")
    def delete_classes(body: IdsBody):
        def run():
            n_cls, n_feat = st.require_buffer().delete_classes(body.ids)
            st.buf_changed()
            return {"classes": n_cls, "features": n_feat}
        return guard(run)

    # ------------------------------------------------------------- 样本
    @app.get("/api/features")
    def features(since: Optional[int] = None, peek: bool = False):
        """全部样本要素（GeoJSON）。

        带上 ``since``（上次响应里的 ``token``）时只回变了的：``upsert`` 是新增或改过
        的要素，``remove`` 是不在了的主键。勾选、撤销、删一个点这类操作只动几条，
        不必每次把几千条要素整批重传、画布整批重建。``since`` 对不上（页面刚打开、
        中间漏了一次）就回整批。``peek`` 只看不记：回整批，不动快照（测试核对用）。
        """
        def run():
            buf = st.require_buffer()
            recs = buf.records()
            if peek:
                return {"type": "FeatureCollection", "version": buf.version,
                        "features": [record_to_feature(r) for r in recs]}
            old = st.feature_snap if since is not None and since == st.feature_token else None
            snap = {r.key: (r.class_id, r.label, r.geom_kind, r.coords) for r in recs}
            if old is None:
                body: Dict[str, Any] = {"type": "FeatureCollection",
                                        "features": [record_to_feature(r) for r in recs]}
            else:
                # 暂存区会原地改草稿的属性，所以快照存的是当时的值而不是记录对象；
                # 坐标先比引用（没改过就是同一个对象），引用不同再比值（库操作之后
                # 底稿整批重读，对象全是新的，内容多半没变）
                def same(r):
                    v = old.get(r.key)
                    return (v is not None and v[0] == r.class_id and v[1] == r.label
                            and v[2] == r.geom_kind and (v[3] is r.coords or v[3] == r.coords))
                body = {"type": "FeatureDelta", "since": since,
                        "upsert": [record_to_feature(r) for r in recs if not same(r)],
                        "remove": [key_to_str(k) for k in old if k not in snap]}
            st.feature_token += 1
            st.feature_snap = snap
            body.update(token=st.feature_token, version=buf.version)
            return body
        return guard(run)

    @app.get("/api/features/{key}")
    def feature(key: str):
        def run():
            rec = st.require_buffer().record(str_to_key(key))
            if rec is None:
                raise KeyError("样本不存在：%s" % key)
            out = record_to_feature(rec)
            out["properties"].update(source=rec.source, attrs=rec.attrs,
                                     bbox=list(rec.bbox))
            return out
        return guard(run)

    @app.post("/api/features")
    def add_feature(body: NewFeature):
        def run():
            kind, coords = geometry_to_coords(body.geometry)
            key = st.require_buffer().add_feature(body.class_id, kind, coords,
                                                  label=body.label)
            st.buf_changed()
            return {"key": key_to_str(key)}
        return guard(run)

    @app.put("/api/features/{key}/geometry")
    def set_geometry(key: str, body: GeometryBody):
        def run():
            _, coords = geometry_to_coords(body.geometry)
            st.require_buffer().set_geometry(str_to_key(key), coords)
            st.buf_changed()
            return {"ok": True}
        return guard(run)

    @app.patch("/api/features/{key}")
    def set_label(key: str, body: LabelBody):
        def run():
            st.require_buffer().set_label(str_to_key(key), body.label)
            st.buf_changed()
            return {"ok": True}
        return guard(run)

    @app.post("/api/features/delete")
    def delete_features(body: KeysBody):
        def run():
            n = st.require_buffer().delete_features(str_to_key(k) for k in body.keys)
            if n:
                st.buf_changed()
            return {"deleted": n}
        return guard(run)

    @app.post("/api/features/move")
    def move_features(body: KeysBody):
        def run():
            if body.class_id is None:
                raise ValueError("缺少目标类别。")
            n = st.require_buffer().move_features(
                [str_to_key(k) for k in body.keys], body.class_id)
            if n:
                st.buf_changed()
            return {"moved": n}
        return guard(run)

    # ------------------------------------------------------------- 底图
    @app.get("/api/basemaps")
    def basemaps():
        out = []
        for i, src in enumerate(st.sources):
            out.append({
                "id": i, "name": src.name, "group": src.group,
                "scheme": src.scheme.kind, "overlay": src.overlay,
                "min_zoom": src.scheme.min_zoom, "max_zoom": src.scheme.max_zoom,
                "usable": st.fetcher.usable(src), "attribution": src.attribution,
            })
        return out

    @app.get("/api/tiles/{source_id}/{z}/{x}/{y}")
    def tile(source_id: int, z: int, x: int, y: int):
        """底图瓦片代理：先查磁盘缓存，缺了再取。密钥只留在本机进程里。"""
        if not 0 <= source_id < len(st.sources):
            raise HTTPException(404)
        src = st.sources[source_id]
        data = st.fetcher.get_cached(src, z, x, y)
        if data is None:
            if not st.fetcher.usable(src):
                raise HTTPException(403, "该底图缺少密钥。")
            try:
                data = st.fetcher._download(src, z, x, y)
            except Exception as exc:
                raise HTTPException(502, str(exc))
            if data:
                st.fetcher.cache.put(src, z, x, y, data)
        # 页面从 localhost 取瓦片（与出图请求分开连接，见 MapView.tsx 的 TILE_ORIGIN），
        # 允许跨源读取，画布才不会被标记为受污染
        return Response(data, media_type="image/png",
                        headers={"Cache-Control": "max-age=86400",
                                 "Access-Control-Allow-Origin": "*"})

    # ------------------------------------------------------------- 任务、导入导出、库文件
    from . import io_routes, layer_routes, misc_routes, tree_routes
    io_routes.register(app, st, guard)
    layer_routes.register(app, st, guard)
    tree_routes.register(app, st, guard)
    misc_routes.register(app, st, guard)
    # 功能模块按发行版 profile 装：没装的模块连接口都没有
    enabled = misc_routes.enabled_modules()
    # 模块的接口用到时才 import：没装的模块目录不在包里，顶上 import 会让整个后端起不来
    if "spectra" in enabled:
        from . import spectra_routes
        spectra_routes.register(app, st, guard)
    if "clip" in enabled or "toolbox" in enabled:
        from . import module_routes
        module_routes.register(app, st, guard)
    if "fieldwork" in enabled:
        from . import fieldwork_routes
        fieldwork_routes.register(app, st, guard)
    if "deeplearn" in enabled:
        from . import dl_routes
        dl_routes.register(app, st, guard)

    # ------------------------------------------------------------- 静态页面
    if static_dir and os.path.isdir(static_dir):
        index = os.path.join(static_dir, "index.html")
        app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")),
                  name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            full = os.path.join(static_dir, path)
            if path and os.path.isfile(full):
                return FileResponse(full)
            return FileResponse(index)

    return app


# 最近打开记在 state/session.json 的 recent 里。只改自己的几个键，其余原样保留。
RECENT_MAX = 10


def remember_library(path: str) -> None:
    if not path:
        return
    path = os.path.abspath(path)
    norm = os.path.normcase(path)
    doc = appstate.load()
    recent = [p for p in appstate.recent(doc) if os.path.normcase(p) != norm]
    doc["recent"] = ([path] + recent)[:RECENT_MAX]
    doc["library"] = path
    appstate.save(doc)


def remember_folder(key: str, folder: str) -> None:
    doc = appstate.load()
    doc[key] = folder
    appstate.save(doc)
