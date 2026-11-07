# -*- coding: utf-8 -*-
"""长任务、样本库文件、导入、导出的接口。

逻辑全在 :mod:`samplelib` 里（``session`` / ``io_shp`` / ``io_attr`` / ``store``），
这里只做三件事：把参数从请求里取出来、包成后台任务、把结果整理成界面要的样子。
"""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from samplelib import appstate, config, io_attr
from samplelib.model import ImportReport
from samplelib.store import SampleStore

from .tasks import Task


# =============================================================================
# 请求体
# =============================================================================

class PathBody(BaseModel):
    path: str


class CopyBody(BaseModel):
    path: str
    open: bool = False             # True：另存为（复制后打开新的那份）；False：备份到


class ImportBody(BaseModel):
    path: str
    region: str = ""
    sample_type: str = ""


class AttrPreviewBody(BaseModel):
    path: str
    class_fields: List[str]


class AttrImportBody(BaseModel):
    path: str
    class_fields: List[str]
    region: str
    name_field: str = ""
    code_field: str = ""
    sample_type: str = ""


class ExportPreviewBody(BaseModel):
    level: str = "leaf"            # custom / leaf / 1 / 2 …


class ExportBody(BaseModel):
    out_dir: str
    driver: str = config.DEFAULT_EXPORT_DRIVER
    template: str = config.DEFAULT_EXPORT_TEMPLATE
    target_crs: str = ""
    level: str = "leaf"
    restore_attrs: bool = True
    buffer_radius: float = 0.0
    buffer_class_ids: Optional[List[int]] = None


# =============================================================================
# 结果整理
# =============================================================================

def import_result(reports: List[ImportReport]) -> Dict[str, Any]:
    """导入结果。一切顺利只要一句话；有跳过、有警告才需要把全文摆出来。"""
    reports = [r for r in reports if r is not None]
    noisy = [r for r in reports if r.skipped or r.warnings]
    return {
        "reports": len(reports),
        "files": sum(r.files for r in reports),
        "classes": sum(r.classes for r in reports),
        "samples": sum(r.samples for r in reports),
        "warnings": sum(len(r.warnings) + len(r.skipped) for r in reports),
        "noisy": bool(noisy),
        "summary": "\n\n".join(r.summary() for r in reports),
    }


def _short_folder(folder: str) -> str:
    """菜单里的文件夹写短一点：家目录写成 ~，太长的只留最后两级。"""
    home = os.path.expanduser("~")
    if folder.startswith(home):
        folder = "~" + folder[len(home):]
    parts = folder.replace("\\", "/").split("/")
    return folder if len(parts) <= 4 else "…/" + "/".join(parts[-2:])


def _level(value: str):
    return value if value in ("custom", "leaf") else int(value)


# =============================================================================
# 路由
# =============================================================================

def register(app, st, guard) -> None:
    s = st.session

    def start(title: str, fn, *, text: str = "", cancellable: bool = True):
        """起任务并立刻返回任务号。前端拿着它去轮询 /api/tasks/{id}。"""
        try:
            task = st.tasks.start(title, fn, text=text, cancellable=cancellable)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return task.to_json()

    def need_library() -> None:
        if not s.is_open:
            raise HTTPException(409, "请先打开或新建样本库。")

    # ------------------------------------------------------------- 任务
    # 这两个接口不拿样本库的锁：任务线程整段持锁，查进度要是也排队，进度条就不走了
    @app.get("/api/tasks/current")
    def current_task():
        t = st.tasks.busy()
        return t.to_json() if t else None

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str):
        t = st.tasks.get(task_id)
        if t is None:
            raise HTTPException(404, "任务不存在。")
        return t.to_json()

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str):
        t = st.tasks.get(task_id)
        if t is None:
            raise HTTPException(404, "任务不存在。")
        t.cancel()
        return t.to_json()

    # ------------------------------------------------------------- 库文件
    @app.get("/api/library/recent")
    def recent():
        """最近打开的样本库，当前这个不重复列。记在 state/session.json。"""
        current = os.path.normcase(os.path.abspath(s.library_path)) if s.is_open else ""
        out = []
        for path in appstate.recent():
            if os.path.normcase(path) == current:
                continue
            out.append({"path": path, "name": os.path.basename(path),
                        "folder": _short_folder(os.path.dirname(path)),
                        "exists": os.path.isfile(path)})
        return out

    @app.post("/api/library/recent/clear")
    def clear_recent():
        if st.remember:
            doc = appstate.load()
            doc["recent"] = []
            appstate.save(doc)
        return []

    @app.get("/api/library/defaults")
    def defaults():
        """另存为 / 备份对话框的默认位置与文件名：上次用过的文件夹，名字带日期。"""
        def run():
            need_library()
            doc = appstate.load()
            here = os.path.dirname(s.library_path)
            stem = os.path.splitext(os.path.basename(s.library_path))[0]

            def folder(key: str) -> str:
                f = doc.get(key) or here
                return f if os.path.isdir(f) else os.path.expanduser("~")

            stamp = datetime.now().strftime("%Y%m%d_%H%M")
            return {
                "backup_dir": folder("backup_dir"),
                "backup_name": "%s_备份_%s%s" % (stem, stamp, config.LIBRARY_EXT),
                "save_as_dir": folder("save_as_dir"),
                "save_as_name": "%s_副本%s" % (stem, config.LIBRARY_EXT),
                "library_dir": here,
            }
        return guard(run)

    @app.post("/api/library/copy")
    def copy_library(body: CopyBody):
        """另存为（open=True）与备份到（open=False）。

        复制的是库文件，暂存区里没保存的改动不在文件里——前端在调这个之前已经
        问过「先保存 / 只要已保存的内容」。另存为之后打开新的那一份，属于旧库的
        草稿随之丢弃。
        """
        need_library()
        target = body.path
        if not target.lower().endswith(config.LIBRARY_EXT):
            target += config.LIBRARY_EXT

        def work(task: Task):
            task.note("正在复制并核对…")
            path = s.copy_to(target)
            st.remember_folder("save_as_dir" if body.open else "backup_dir",
                               os.path.dirname(path))
            n_cls, n_rec = len(s.classes), len(s.records)
            old = s.library_path
            if body.open:
                st.open(path)
            return {"path": path, "old": old, "opened": body.open,
                    "size_mb": round(os.path.getsize(path) / 1048576.0, 1),
                    "classes": n_cls, "samples": n_rec}

        return start("另存为" if body.open else "备份样本库", work, cancellable=False)

    @app.get("/api/library/backups")
    def backups():
        """样本库旁边 backups/ 里的自动快照，新的在前。"""
        def run():
            out = []
            for p in s.list_backups():
                out.append({"path": p, "name": os.path.basename(p),
                            "time": datetime.fromtimestamp(os.path.getmtime(p))
                            .strftime("%Y-%m-%d %H:%M:%S"),
                            "size_mb": round(os.path.getsize(p) / 1048576.0, 1)})
            return out
        return guard(run)

    @app.post("/api/library/probe")
    def probe(body: PathBody):
        """核实一个外部文件确实是样本库，报出它有多少类多少条，给还原前的确认用。"""
        def run():
            need_library()
            if os.path.normcase(os.path.abspath(body.path)) == os.path.normcase(
                    os.path.abspath(s.library_path)):
                raise ValueError("所选文件就是当前样本库，无需还原。")
            try:
                store = SampleStore.open(body.path, update=False)
                n_cls = len(store.list_classes(with_counts=False))
                n_rec = store.total_samples()
                store.close()
            except Exception as exc:
                raise ValueError("该文件不是有效的样本库：%s" % exc)
            return {"path": body.path, "classes": n_cls, "samples": n_rec,
                    "current": {"path": s.library_path, "classes": len(s.classes),
                                "samples": len(s.records)}}
        return guard(run)

    @app.post("/api/library/restore")
    def restore(body: PathBody):
        """用一份备份整体替换当前样本库。替换前先把当前状态自动快照一份。"""
        need_library()

        def work(task: Task):
            task.note("正在快照当前状态并还原…")
            failure = None
            try:
                s.restore_backup(body.path)
            except Exception as exc:
                failure = exc
            # 还原会关掉再重开 store（失败时也会把原库重新打开），暂存区攥着的是
            # 已关闭的旧 store，必须重建
            st.reset()
            if failure is not None:
                raise failure
            return {"source": os.path.basename(body.path)}

        return start("从备份还原", work, cancellable=False)

    # ------------------------------------------------------------- 导入
    @app.post("/api/import/dir")
    def import_dir(body: ImportBody):
        need_library()

        def work(task: Task):
            report = s.import_dir(body.path, region=body.region or os.path.basename(body.path),
                                  sample_type=body.sample_type, progress=task)
            st.lib_rewritten()
            return import_result([report])

        return start("导入样本目录", work, text="正在读取…")

    @app.post("/api/import/root")
    def import_root(body: ImportBody):
        need_library()

        def work(task: Task):
            reports = s.import_root(body.path, progress=task)
            st.lib_rewritten()
            return import_result(reports)

        return start("导入样本库根目录", work, text="正在读取…")

    @app.post("/api/import/file")
    def import_file(body: ImportBody):
        need_library()

        def work(task: Task):
            report = s.import_file(body.path, region=body.region,
                                   sample_type=body.sample_type, progress=task)
            st.lib_rewritten()
            return import_result([report])

        return start("导入矢量文件", work, text="正在读取…", cancellable=False)

    @app.post("/api/import/profile")
    def import_profile(body: PathBody):
        """按属性导入的第一步：统计字段，并给出推荐的分级字段。"""
        need_library()

        def work(task: Task):
            task.note("正在统计属性字段…")
            profiles, info = s.profile_vector(body.path)
            try:
                suggested = list(io_attr.suggest_class_fields(profiles))
            except Exception:
                suggested = []
            return {
                "path": body.path,
                "name": os.path.basename(body.path),
                "default_region": os.path.splitext(os.path.basename(body.path))[0],
                "info": {k: (v if isinstance(v, (int, float, str, bool)) or v is None else str(v))
                         for k, v in (info or {}).items()},
                "fields": [{"name": f.name, "type": f.type_name, "total": f.total,
                            "non_null": f.non_null, "unique": f.unique,
                            "samples": [str(v) for v in f.samples[:6]]} for f in profiles],
                "suggested": suggested,
            }

        return start("读取字段", work, cancellable=False)

    @app.post("/api/import/attr/preview")
    def attr_preview(body: AttrPreviewBody):
        def run():
            if not body.class_fields:
                return {"leaves": 0, "groups": 0, "examples": []}
            leaves, groups, examples = io_attr.preview_classes(body.path, body.class_fields)
            return {"leaves": leaves, "groups": groups, "examples": list(examples)[:3]}
        return guard(run)

    @app.post("/api/import/attr")
    def import_attr(body: AttrImportBody):
        need_library()
        if not body.class_fields or not body.region.strip():
            raise HTTPException(400, "请选择分级字段并填写工区名称。")

        def work(task: Task):
            report = s.import_by_attribute(
                body.path, class_fields=body.class_fields, region=body.region.strip(),
                name_field=body.name_field, code_field=body.code_field,
                sample_type=body.sample_type, progress=task)
            st.lib_rewritten()
            return import_result([report])

        return start("按属性导入", work, text="正在分组…")

    # ------------------------------------------------------------- 导出
    def export_units() -> List[int]:
        """导出单元：按勾选状态推断，再套用分支级的导出层级标记。"""
        return sorted(s.export_units_from_selection(
            merge_ids=st.export_merge, split_ids=st.export_split))

    @app.get("/api/export/options")
    def export_options():
        def run():
            need_library()
            units = export_units()
            n_sel, n_cls = s.selection_summary()
            point_cls = s.point_class_ids(s.selected_class_ids())
            return {
                "drivers": list(config.EXPORT_DRIVERS),
                "driver": config.DEFAULT_EXPORT_DRIVER,
                "template": config.DEFAULT_EXPORT_TEMPLATE,
                "buffer_radius": config.BUFFER_RADIUS_M,
                "max_depth": s.max_depth(),
                "units": [{"id": cid, "name": s.class_map[cid].name}
                          for cid in units if cid in s.class_map],
                "selected": n_sel,
                "selected_classes": n_cls,
                "point_classes": [{"id": cid, "path": s.class_path(cid)} for cid in point_cls],
                "last_dir": appstate.load().get("export_dir", ""),
            }
        return guard(run)

    @app.post("/api/export/preview")
    def export_preview(body: ExportPreviewBody):
        """按某个层级导出会得到什么：单元数、文件数、样本数与兜底说明。"""
        def run():
            need_library()
            lvl = _level(body.level)
            units = export_units() if lvl == "custom" else None
            if lvl == "custom" and not units:
                return {"units": 0, "files": 0, "samples": 0, "notes": []}
            return s.export_preview(lvl, units)
        return guard(run)

    @app.post("/api/export")
    def export(body: ExportBody):
        need_library()
        if not s.selection:
            raise HTTPException(400, "未勾选任何样本。请先在样本树中勾选要导出的类别。")
        if not body.out_dir.strip():
            raise HTTPException(400, "请选择导出目录。")
        lvl = _level(body.level)
        units = export_units() if lvl == "custom" else None
        if lvl == "custom" and not units:
            raise HTTPException(400, "样本树中没有标记导出分支，请选择其他导出层级。")

        def work(task: Task):
            report = s.export(
                body.out_dir.strip(), use_selection=True, progress=task,
                driver_key=body.driver, target_crs=body.target_crs.strip(),
                name_template=body.template.strip() or config.DEFAULT_EXPORT_TEMPLATE,
                level=lvl, unit_class_ids=units, restore_attrs=body.restore_attrs,
                buffer_radius=body.buffer_radius, buffer_class_ids=body.buffer_class_ids)
            st.remember_folder("export_dir", body.out_dir.strip())
            return {"out_dir": report.out_dir, "files": len(report.files),
                    "samples": report.samples, "checked": report.checked,
                    "warnings": list(report.warnings), "summary": report.summary()}

        return start("导出样本", work, text="正在写入…")
