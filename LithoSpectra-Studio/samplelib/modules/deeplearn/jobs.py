# -*- coding: utf-8 -*-
"""深度学习任务：建目录、排队、启动 Worker、取消、续跑、读事件。

任务目录是唯一的事实来源（格式见 ``workers/litho_dl/jobs.py``）。这里不保存任何
只在内存里的任务状态：界面重启后扫一遍任务根就能恢复列表，命令行跑的任务放进
任务根同样会出现。

与样本库的长任务（:mod:`webapi.tasks`）分开管理：那边是"同一时刻一个、模态进度
框、整段持样本库锁"，适合几十秒的导入导出；训练动辄数小时，必须非模态、不占
样本库锁，训练期间照常标注、取谱。

同一时刻只跑一个 Worker（训练与预测都吃满 GPU），其余排队。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import runtime

PROTOCOL = 1
QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED, INTERRUPTED = (
    "queued", "running", "succeeded", "failed", "cancelled", "interrupted")
FINAL = (SUCCEEDED, FAILED, CANCELLED)
CANCEL_GRACE = 120.0        # 秒；请求取消后 Worker 还不退就强行结束
MONITOR_INTERVAL = 1.0
LOG_KEEP = 300              # 摘要里保留的日志行数

TASK_TITLES = {"train_classifier": "训练分类模型", "predict": "整景预测",
               "train_contrastive": "对比学习预训练", "embed": "样本特征分析",
               "similarity": "相似区域检索"}


def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259          # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# =============================================================================
# 事件摘要
# =============================================================================

class Digest:
    """一个任务的事件流读到哪了、归纳出什么。按字节偏移增量读，只读完整的行。"""

    def __init__(self, job_dir: Path) -> None:
        self.dir = job_dir
        self.offset = 0
        self.seq = 0
        self.epochs: List[dict] = []
        self.evaluations: Dict[str, dict] = {}
        self.estimate: Optional[dict] = None
        self.progress: Optional[dict] = None
        self.phase = ""
        self.predict_init: Optional[dict] = None
        self.blocks: Optional[dict] = None
        self.artifacts: List[dict] = []
        self.logs: List[dict] = []
        self.final: Optional[dict] = None
        self.checkpoint: Optional[dict] = None

    def update(self) -> None:
        path = self.dir / "events.ndjson"
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self.offset:             # 文件被换掉了（重建任务），从头读
            self.__init__(self.dir)
        if size == self.offset:
            return
        with open(path, "rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read(size - self.offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        for raw in chunk[:end].split(b"\n"):
            try:
                ev = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            self._apply(ev)

    def _apply(self, ev: dict) -> None:
        self.seq = max(self.seq, int(ev.get("seq", 0)))
        kind, data, msg = ev.get("event"), ev.get("data") or {}, ev.get("message", "")
        if kind == "started":
            # 续跑：同一目录再执行一次，这一轮的进度从头算
            self.final = None
            self.progress = None
            self.logs.append({"t": ev["t"], "level": "info", "text": msg})
        elif kind == "epoch":
            self.epochs = [e for e in self.epochs if e["epoch"] < data["epoch"]]
            self.epochs.append(data)
            self.progress = None
        elif kind == "evaluation":
            self.evaluations[data.get("split", "val")] = data
        elif kind == "estimate":
            self.estimate = data
        elif kind == "progress":
            self.progress = data
        elif kind == "phase":
            self.phase = msg
        elif kind == "predict_init":
            self.predict_init = data
        elif kind == "block_done":
            self.blocks = data
        elif kind == "checkpoint":
            self.checkpoint = data
        elif kind == "artifact":
            self.artifacts = [a for a in self.artifacts if a.get("path") != data.get("path")]
            self.artifacts.append(data)
        elif kind in ("log", "warning"):
            self.logs.append({"t": ev["t"], "level": "warn" if kind == "warning" else "info",
                              "text": msg})
        elif kind in ("completed", "failed", "cancelled"):
            self.final = {"event": kind, "message": msg, **data}
            level = {"completed": "ok", "failed": "error", "cancelled": "warn"}[kind]
            self.logs.append({"t": ev["t"], "level": level, "text": msg})
        if len(self.logs) > LOG_KEEP:
            self.logs = self.logs[-LOG_KEEP:]


# =============================================================================
# 管理器
# =============================================================================

class JobManager:
    def __init__(self, root_fn: Callable[[], Path] = runtime.jobs_root,
                 python_fn: Callable[[], str] = runtime.require_python,
                 command_fn: Optional[Callable[[str, Path], List[str]]] = None) -> None:
        self._root_fn = root_fn
        self._python_fn = python_fn
        # 测试可以换成假 Worker
        self._command_fn = command_fn or (lambda py, d: [py, "-m", "litho_dl", "run", str(d),
                                                         "--quiet"])
        self._lock = threading.RLock()
        self._queue: List[str] = []
        self._running: Optional[dict] = None    # {"id", "proc", "cancel_at", "log"}
        self._digests: Dict[str, Digest] = {}
        self._monitor: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.listeners: List[Callable[[str, str], None]] = []   # (job_id, 新状态)

    # ------------------------------------------------------------------ 目录
    @property
    def root(self) -> Path:
        return Path(self._root_fn())

    def job_dir(self, job_id: str) -> Path:
        path = (self.root / job_id).resolve()
        if path.parent != self.root.resolve() or not (path / "job.json").is_file():
            raise KeyError("任务不存在：%s" % job_id)
        return path

    def create(self, task: str, params: Dict[str, Any], title: str = "",
               start: bool = True) -> str:
        job_id = "%s_%s_%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), task,
                               uuid.uuid4().hex[:6])
        path = self.root / job_id
        path.mkdir(parents=True)
        _write_json(path / "job.json", {"protocol": PROTOCOL, "job_id": job_id, "task": task,
                                        "title": title or TASK_TITLES.get(task, task),
                                        "params": params, "created": time.time()})
        _write_json(path / "status.json", {"job": job_id, "task": task, "state": QUEUED,
                                           "updated": time.time()})
        if start:
            self.enqueue(job_id)
        return job_id

    # ------------------------------------------------------------------ 调度
    def enqueue(self, job_id: str) -> None:
        """排队执行（新任务或续跑）。已在队列或正在跑的不重复加。"""
        path = self.job_dir(job_id)
        with self._lock:
            if job_id in self._queue or (self._running and self._running["id"] == job_id):
                return
            status = self._status(path)
            if status.get("state") == RUNNING and pid_alive(status.get("pid")):
                raise ValueError("该任务正在其他进程中运行（进程 %s）" % status.get("pid"))
            try:
                (path / "cancel").unlink()
            except FileNotFoundError:
                pass
            status.update(state=QUEUED, updated=time.time(), message="排队中")
            _write_json(path / "status.json", status)
            self._queue.append(job_id)
        self._ensure_monitor()
        self.pump()

    def pump(self) -> None:
        """检查正在跑的进程；空出来了就启动队首。"""
        with self._lock:
            r = self._running
            if r is not None:
                code = r["proc"].poll()
                if code is None:
                    if r["cancel_at"] and time.time() - r["cancel_at"] > CANCEL_GRACE:
                        r["proc"].terminate()
                    return
                self._finish(r, code)
                self._running = None
            while self._queue and self._running is None:
                job_id = self._queue.pop(0)
                try:
                    self._start(job_id)
                except Exception as exc:          # 起不来：记成失败，接着排下一个
                    self._mark(job_id, FAILED, "无法启动 Worker：%s" % exc)

    def _start(self, job_id: str) -> None:
        path = self.job_dir(job_id)
        python = self._python_fn()
        log = open(path / "worker.log", "ab")
        log.write(("\n=== %s 启动：%s ===\n" % (datetime.now().isoformat(" ", "seconds"),
                                              python)).encode("utf-8"))
        log.flush()
        kwargs = runtime.popen_kwargs()
        proc = subprocess.Popen(self._command_fn(python, path), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=log, **kwargs)
        self._running = {"id": job_id, "proc": proc, "cancel_at": 0.0, "log": log,
                         "python": python}
        self._notify(job_id, RUNNING)

    def _finish(self, r: dict, code: int) -> None:
        try:
            r["log"].close()
        except OSError:
            pass
        path = self.root / r["id"]
        status = self._status(path)
        if status.get("state") in (QUEUED, RUNNING):
            # Worker 没来得及写终态就退出了：崩溃（如 libomp 冲突的 134）或被强行结束
            tail = ""
            try:
                tail = (path / "worker.log").read_text("utf-8", "replace").strip()[-1500:]
            except OSError:
                pass
            if r["cancel_at"]:
                self._mark(r["id"], CANCELLED, "已强制结束")
            else:
                self._mark(r["id"], FAILED, "Worker 异常退出（退出码 %s）\n%s" % (code, tail))
        else:
            self._notify(r["id"], status.get("state", ""))

    def _mark(self, job_id: str, state: str, message: str) -> None:
        path = self.root / job_id
        status = self._status(path)
        status.update(state=state, message=message, finished=time.time(), updated=time.time())
        _write_json(path / "status.json", status)
        self._notify(job_id, state)

    def _notify(self, job_id: str, state: str) -> None:
        for fn in list(self.listeners):
            try:
                fn(job_id, state)
            except Exception:
                pass

    def _ensure_monitor(self) -> None:
        with self._lock:
            if self._monitor is not None and self._monitor.is_alive():
                return
            self._stop.clear()

            def loop():
                while not self._stop.wait(MONITOR_INTERVAL):
                    self.pump()
                    with self._lock:
                        if self._running is None and not self._queue:
                            self._monitor = None
                            return

            self._monitor = threading.Thread(target=loop, daemon=True, name="dl-jobs")
            self._monitor.start()

    # ------------------------------------------------------------------ 操作
    def cancel(self, job_id: str) -> None:
        path = self.job_dir(job_id)
        with self._lock:
            if job_id in self._queue:
                self._queue.remove(job_id)
                self._mark(job_id, CANCELLED, "已从队列中移除")
                return
            (path / "cancel").touch()
            if self._running and self._running["id"] == job_id:
                self._running["cancel_at"] = time.time()

    def delete(self, job_id: str) -> None:
        path = self.job_dir(job_id)
        with self._lock:
            if job_id in self._queue:
                self._queue.remove(job_id)
            if self._running and self._running["id"] == job_id:
                raise ValueError("任务正在运行，请先取消再删除")
            status = self._status(path)
            if status.get("state") == RUNNING and pid_alive(status.get("pid")):
                raise ValueError("任务正在其他进程中运行，请先取消再删除")
            self._digests.pop(job_id, None)
        shutil.rmtree(path)

    def shutdown(self, kill: bool = False) -> None:
        """程序退出：默认让正在跑的 Worker 继续跑完（它不依赖界面），下次打开接着看。"""
        self._stop.set()
        with self._lock:
            self._queue.clear()
            if kill and self._running:
                self._running["proc"].terminate()

    # ------------------------------------------------------------------ 查询
    @staticmethod
    def _status(path: Path) -> dict:
        try:
            return _read_json(path / "status.json")
        except (OSError, ValueError):
            return {}

    def _state(self, job_id: str, status: dict) -> str:
        state = status.get("state", "")
        with self._lock:
            ours = self._running is not None and self._running["id"] == job_id
            queued = job_id in self._queue
        if ours:
            return RUNNING
        if queued:
            return QUEUED
        if state == RUNNING and not pid_alive(status.get("pid")):
            return INTERRUPTED
        if state == QUEUED:
            return INTERRUPTED             # 上次退出时还没轮到
        return state

    def digest(self, job_id: str) -> Digest:
        path = self.job_dir(job_id)
        with self._lock:
            d = self._digests.get(job_id)
            if d is None or d.dir != path:
                d = self._digests[job_id] = Digest(path)
        d.update()
        return d

    def summary(self, job_id: str, detail: bool = False) -> Dict[str, Any]:
        path = self.job_dir(job_id)
        spec = _read_json(path / "job.json")
        status = self._status(path)
        d = self.digest(job_id)
        state = self._state(job_id, status)
        out: Dict[str, Any] = {
            "id": job_id, "task": spec.get("task"), "title": spec.get("title"),
            "created": spec.get("created"), "params": spec.get("params", {}),
            "state": state, "started": status.get("started"),
            "finished": status.get("finished"), "message": status.get("message", ""),
            "phase": d.phase, "progress": self._progress(spec, d, state),
            "artifacts": d.artifacts, "estimate": d.estimate, "dir": str(path),
            "cancel_requested": (path / "cancel").exists() and state == RUNNING,
        }
        if d.epochs:
            best = [e for e in d.epochs if e.get("best")]
            out["last_epoch"] = d.epochs[-1]
            out["best_epoch"] = best[-1] if best else None
        if "test" in d.evaluations:
            t = d.evaluations["test"]
            out["test"] = {k: t.get(k) for k in ("oa", "aa", "kappa", "macro_f1")}
        if detail:
            # 正在训练的这一轮：批进度与到目前为止的平均损失（Worker 每两秒报一次）
            batch = d.progress if state == RUNNING and d.progress and d.progress.get("unit") == "batch" else None
            out.update(epochs=d.epochs, evaluations=d.evaluations, logs=d.logs,
                       predict_init=d.predict_init, blocks=d.blocks, final=d.final,
                       checkpoint=d.checkpoint, seq=d.seq, batch=batch)
        return out

    @staticmethod
    def _progress(spec: dict, d: Digest, state: str) -> Dict[str, Any]:
        """一个 0..1 的进度与一句说明。"""
        if state == SUCCEEDED:
            return {"fraction": 1.0, "text": "完成"}
        if d.blocks:
            b = d.blocks
            return {"fraction": b["done"] / max(1, b["total"]),
                    "text": "%d / %d 块" % (b["done"], b["total"]),
                    "eta": b.get("eta")}
        epochs = int((spec.get("params") or {}).get("epochs") or 0)
        if epochs and (d.epochs or d.progress):
            done = d.epochs[-1]["epoch"] if d.epochs else 0
            frac = done / epochs
            text = "第 %d / %d 轮" % (min(done + 1, epochs), epochs)
            if d.progress and d.progress.get("epoch") == done + 1:
                frac += d.progress["done"] / max(1, d.progress["total"]) / epochs
            eta = None
            if d.epochs:
                per = sum(e.get("seconds", 0) for e in d.epochs[-3:]) / len(d.epochs[-3:])
                eta = per * max(0, epochs - done)
            return {"fraction": min(1.0, frac), "text": text, "eta": eta}
        return {"fraction": 0.0, "text": d.phase or ""}

    def list(self) -> List[Dict[str, Any]]:
        self.pump()
        out = []
        root = self.root
        for path in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
            if (path / "job.json").is_file():
                try:
                    out.append(self.summary(path.name))
                except (OSError, ValueError, KeyError):
                    continue
        return out

    def running_id(self) -> Optional[str]:
        with self._lock:
            return self._running["id"] if self._running else None
