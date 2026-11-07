# -*- coding: utf-8 -*-
"""样本裁剪与预处理工具箱。

* 裁剪：逻辑在 :func:`samplelib.modules.clip.clip.clip_library`。另开
  一份只读连接读样本库，裁的是**已经落库**的样本；任务不拿样本库的锁，跑的时候
  界面照常能翻库。
* 工具箱：工具与参数声明在仓库根的 ``toolbox`` 包里。批处理跑在使用者选的那个
  Python 环境里（``python -m toolbox --job``），用作业文件、
  ``@@TB@@`` 事件行协议与"取消标志文件"通信。界面上的参数面板按 ``Param`` 声明
  生成，这里没有一行"某件工具有哪些参数"的硬编码。
"""

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from samplelib import appstate, config
from samplelib.store import SampleStore

from .tasks import Task

EVENT_PREFIX = "@@TB@@"


class ClipBody(BaseModel):
    image_path: str
    out_dir: str
    patch_size: int = config.CLIP_PATCH_SIZE
    output_format: str = config.CLIP_FORMAT
    train_ratio: float = config.CLIP_TRAIN_RATIO
    split_by: str = config.CLIP_SPLIT_BY
    only_selected: bool = True
    level: str = "leaf"
    buffer_radius: float = 0.0
    buffer_class_ids: Optional[List[int]] = None


class ClipPreviewBody(BaseModel):
    level: str = "leaf"
    only_selected: bool = True


class ToolRunBody(BaseModel):
    tool: str
    python: str
    inputs: List[str]
    output_dir: str
    params: Dict[str, Any] = {}
    overwrite: bool = False
    stop_on_error: bool = False
    dry_run: bool = False


class VisibleBody(BaseModel):
    tool: str
    values: Dict[str, Any]


def _jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _level(value: str):
    return value if value in ("custom", "leaf") else int(value)


class ToolRun:
    """一次批处理：子进程 + 逐行日志。"""

    def __init__(self, run_id: str, job: dict, python: str, repo_root: str) -> None:
        from toolbox import envs as tb_envs

        self.id = run_id
        self.lines: List[Dict[str, str]] = []
        self.progress = (0, 0)
        self.status = "运行中…"
        self.done = False
        self.code: Optional[int] = None
        handle, self.cancel_file = tempfile.mkstemp(prefix="tb-cancel-")
        os.close(handle)
        os.unlink(self.cancel_file)          # 「存在即取消」，所以先让它不存在
        job["cancel_file"] = self.cancel_file
        handle, self.job_file = tempfile.mkstemp(prefix="tb-job-", suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(job, out, ensure_ascii=False)
        # 子进程多半是另一个环境的解释器：本进程指着自己那套 GDAL / PROJ 的变量
        # 对它全是错的路径，先洗掉
        env = tb_envs.clean_environment()
        env["PYTHONPATH"] = repo_root
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [python, "-m", "toolbox", "--job", self.job_file], cwd=repo_root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **tb_envs.no_window_flags())
        threading.Thread(target=self._pump, args=(self.proc.stdout, False), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, True), daemon=True).start()
        threading.Thread(target=self._wait, daemon=True).start()

    @staticmethod
    def _tone(line: str) -> str:
        if line.startswith(("[失败]", "[错误]")) or line.lstrip().startswith("失败："):
            return "danger"
        if line.startswith("[完成]"):
            return "success"
        if line.startswith(("[跳过]", "[计划]", "[警告]")):
            return "muted"
        if line.startswith("==="):
            return "strong"
        return "text"

    def append(self, text: str, tone: str = "") -> None:
        self.lines.append({"t": text, "tone": tone or self._tone(text)})

    def _pump(self, stream, is_err: bool) -> None:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if is_err:
                if line.strip():
                    self.append(line, "muted")
                continue
            if not line.startswith(EVENT_PREFIX):
                if line.strip():          # 子进程里第三方库自己打的东西
                    self.append(line, "muted")
                continue
            try:
                event = json.loads(line[len(EVENT_PREFIX):])
            except ValueError:
                self.append(line, "muted")
                continue
            kind = event.get("t")
            if kind == "log":
                for part in str(event.get("m", "")).split("\n"):
                    self.append(part)
            elif kind == "progress":
                total = int(event.get("n", 0)) or 1
                index = int(event.get("i", 0))
                self.progress = (max(0, index - 1), total)
                self.status = "第 %d / %d 件" % (index, total)
            elif kind == "done":
                self.status = str(event.get("summary", ""))
            elif kind == "error":
                self.append("[错误] %s" % event.get("m", ""), "danger")
                self.status = "失败"

    def _wait(self) -> None:
        self.code = self.proc.wait()
        time.sleep(0.2)                   # 让读日志的线程把最后几行收完
        for path in (self.job_file, self.cancel_file):
            try:
                os.unlink(path)
            except OSError:
                pass
        if self.code not in (0, 1) and self.status.startswith(("运行中", "第 ", "取消中")):
            self.status = "子进程异常退出（代码 %d）" % self.code
        self.done = True

    def cancel(self) -> None:
        try:
            Path(self.cancel_file).touch()
            self.status = "取消中，等这一件跑完…"
        except OSError as exc:
            self.append("[错误] 写不了取消标志：%s" % exc, "danger")


def register(app, st, guard) -> None:
    s = st.session

    # ------------------------------------------------------------- 裁剪
    def clip_units() -> List[int]:
        return sorted(s.export_units_from_selection(merge_ids=st.export_merge,
                                                    split_ids=st.export_split))

    @app.get("/api/clip/options")
    def clip_options():
        def run():
            if not s.is_open:
                raise ValueError("请先打开或新建样本库。")
            doc = appstate.load()
            units = clip_units()
            n_sel, n_cls = s.selection_summary()
            return {
                "patch_size": config.CLIP_PATCH_SIZE, "format": config.CLIP_FORMAT,
                "formats": ["h5", "bin", "tif"], "train_ratio": config.CLIP_TRAIN_RATIO,
                "split_by": config.CLIP_SPLIT_BY, "buffer_radius": config.BUFFER_RADIUS_M,
                "max_depth": s.max_depth(), "units": [{"id": u, "name": s.class_map[u].name} for u in units if u in s.class_map],
                "selected": n_sel, "selected_classes": n_cls, "total": len(s.records),
                "point_classes_selected": [{"id": c, "path": s.class_path(c)} for c in s.point_class_ids(s.selected_class_ids())],
                "point_classes_all": [{"id": c, "path": s.class_path(c)} for c in s.point_class_ids(None)],
                "last_image": doc.get("clip_image", ""), "last_dir": doc.get("clip_dir", ""),
                "rasters": [{"id": l.uid, "name": l.name, "path": l.path}
                            for l in getattr(st, "layers", {}).values()
                            if getattr(l, "path", "") and hasattr(l, "band_count")],
            }
        return guard(run)

    @app.post("/api/clip/preview")
    def clip_preview(body: ClipPreviewBody):
        def run():
            lvl = _level(body.level)
            units = clip_units() if lvl == "custom" else None
            if lvl == "custom" and not units:
                return {"units": 0, "files": 0, "samples": 0, "notes": [], "classes": 0}
            return s.export_preview(lvl, units, use_selection=body.only_selected)
        return guard(run)

    @app.post("/api/clip")
    def clip(body: ClipBody):
        if not s.is_open:
            raise HTTPException(409, "请先打开或新建样本库。")
        if body.only_selected and not s.selection:
            raise HTTPException(400, "未勾选任何样本。请先在样本树中勾选，或取消「仅裁剪已勾选的样本」。")
        if not os.path.isfile(body.image_path):
            raise HTTPException(400, "影像文件不存在：%s" % body.image_path)
        if not body.out_dir.strip():
            raise HTTPException(400, "请选择输出目录。")
        lvl = _level(body.level)
        with st.lock:
            units = clip_units() if lvl == "custom" else None
            selection = set(s.selection) if body.only_selected else None
            class_ids = s.selected_class_ids() if body.only_selected else None
            library = s.library_path
        if lvl == "custom" and not units:
            raise HTTPException(400, "样本树中没有标记裁剪分支，请选择其他裁剪层级。")

        def work(task: Task):
            from samplelib.modules.clip import clip as C
            store = SampleStore.open(library, update=False)
            try:
                report = C.clip_library(
                    store, body.image_path, body.out_dir.strip(), class_ids=class_ids,
                    selection=selection, level=lvl, unit_class_ids=units,
                    patch_size=int(body.patch_size), output_format=body.output_format,
                    train_ratio=float(body.train_ratio), split_by=body.split_by,
                    buffer_radius=body.buffer_radius, buffer_class_ids=body.buffer_class_ids,
                    progress=task)
            finally:
                store.close()
            if st.remember:
                doc = appstate.load()
                doc["clip_image"], doc["clip_dir"] = body.image_path, body.out_dir.strip()
                appstate.save(doc)
            return {"summary": report.summary(), "out_dir": report.out_dir,
                    "cancelled": report.cancelled, "warnings": list(report.warnings),
                    "units": [{"label": u.label, "name": u.name, "train": u.train, "test": u.test,
                               "outside": u.outside} for u in report.units]}

        try:
            task = st.tasks.start("裁剪样本块", work, text="正在裁剪…", exclusive=False)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"task": task.to_json()}

    # ------------------------------------------------------------- 工具箱
    from samplelib import toolbox_bridge
    runs: Dict[str, ToolRun] = {}

    def tb():
        if not toolbox_bridge.available():
            raise HTTPException(404, "找不到工具箱。")
        return toolbox_bridge.load()

    @app.get("/api/toolbox/tools")
    def tools():
        mod = tb()
        out = []
        for tool in mod.all_tools():
            params = []
            for p in tool.params:
                params.append({
                    "name": p.name, "label": p.label, "default": _jsonable(p.default), "kind": p.kind,
                    "choices": list(p.choices), "choice_labels": [list(x) for x in p.choice_labels],
                    "help": p.help, "path_mode": p.path_mode,
                    "suggestions": list(tool.suggestions(p.name)),
                })
            out.append({"id": tool.id, "name": tool.name, "summary": tool.summary,
                        "input_kind": tool.input_kind, "output_kind": tool.output_kind,
                        "params": params})
        return out

    @app.post("/api/toolbox/visible")
    def visible(body: VisibleBody):
        mod = tb()
        tool = mod.get(body.tool)
        return sorted(tool.visible_params(body.values))

    @app.get("/api/toolbox/envs")
    def envs():
        tb()
        from toolbox import envs as tb_envs
        return [{"name": e.label, "python": str(e.python), "kind": e.kind, "current": e.current}
                for e in tb_envs.discover()]

    @app.get("/api/toolbox/probe")
    def probe(python: str):
        tb()
        from toolbox import envs as tb_envs
        info = tb_envs.probe(python)
        return {"text": tb_envs.describe(info), "ok": bool(info.get("ok"))}

    @app.post("/api/toolbox/run")
    def run_tool(body: ToolRunBody):
        mod = tb()
        tool = mod.get(body.tool)
        if not body.inputs:
            raise HTTPException(400, "请选择输入路径。")
        if not body.output_dir.strip():
            raise HTTPException(400, "请选择输出目录。")
        if not body.python:
            raise HTTPException(400, "请选择运行环境。")
        try:
            params = {k: _jsonable(p.coerce(body.params.get(k, p.default)))
                      for k, p in ((p.name, p) for p in tool.params)}
        except Exception as exc:
            raise HTTPException(400, str(exc))
        job = {"tool": tool.id, "inputs": body.inputs, "output_dir": body.output_dir.strip(),
               "params": params, "overwrite": body.overwrite,
               "stop_on_error": body.stop_on_error, "dry_run": body.dry_run}
        if any(not r.done for r in runs.values()):
            raise HTTPException(409, "上一批处理尚未完成。")
        run_id = "r%d" % (len(runs) + 1)
        try:
            runs[run_id] = ToolRun(run_id, job, body.python, toolbox_bridge.REPO_ROOT)
        except OSError as exc:
            raise HTTPException(400, "无法启动子进程：%s" % exc)
        runs[run_id].append("运行环境：%s" % body.python, "muted")
        return {"id": run_id}

    @app.get("/api/toolbox/runs/{run_id}")
    def run_state(run_id: str, since: int = 0):
        r = runs.get(run_id)
        if r is None:
            raise HTTPException(404, "运行记录不存在。")
        return {"lines": r.lines[since:], "next": len(r.lines), "progress": list(r.progress),
                "status": r.status, "done": r.done, "code": r.code}

    @app.post("/api/toolbox/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        r = runs.get(run_id)
        if r is None:
            raise HTTPException(404, "运行记录不存在。")
        r.cancel()
        return {"ok": True}

    @app.get("/api/toolbox/rasters")
    def toolbox_rasters():
        """画布里有本地文件的栅格图层，供工具箱当输入。"""
        return [{"name": l.name, "path": l.path} for l in getattr(st, "layers", {}).values()
                if getattr(l, "path", "") and hasattr(l, "band_count") and os.path.exists(l.path)]
