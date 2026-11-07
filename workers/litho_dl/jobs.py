# -*- coding: utf-8 -*-
"""任务目录：Worker 与界面、命令行之间唯一的契约。

::

    <任务根>/<job_id>/
    ├── job.json          任务类型、参数、协议版本（只写一次）
    ├── events.ndjson     一行一个事件，只追加
    ├── status.json       当前状态快照，原子替换
    ├── cancel            出现即请求取消
    ├── worker.log        Worker 的标准错误（界面启动时重定向到这里）
    └── ……                各任务自己的成果，见 train / predict

界面只读这个目录：界面重启，或者任务是在命令行里跑的，都能接着看进度和成果。
事件同时写到标准输出，命令行下直接看得到。

每个事件：``{"v": 协议版本, "job": job_id, "seq": 序号, "t": 时间戳,
"event": 类型, "message": 文字, "data": {...}}``。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import PROTOCOL_VERSION, __version__

# 状态
QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED = (
    "queued", "running", "succeeded", "failed", "cancelled")

EVENT_TYPES = (
    "started", "phase", "progress", "estimate", "epoch", "evaluation", "checkpoint",
    "predict_init", "block_done", "artifact", "warning", "log", "completed", "failed",
    "cancelled",
)


class Cancelled(Exception):
    """使用者请求了取消。任务在安全边界（batch、块）抛出它。"""


class JobError(RuntimeError):
    """可预期的失败：输入不合规、文件打不开、模型与影像不匹配。消息直接给使用者看。"""


def new_job_id(task: str) -> str:
    return "%s_%s_%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), task,
                         uuid.uuid4().hex[:6])


def write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class Job:
    """一个任务目录的读写句柄，Worker 侧使用。"""

    CANCEL_POLL = 0.5          # 秒；cancel 文件不必每个 batch 都 stat 一次

    def __init__(self, job_dir: str | Path, echo: bool = True) -> None:
        self.dir = Path(job_dir).expanduser().resolve()
        spec_path = self.dir / "job.json"
        if not spec_path.is_file():
            raise JobError("任务目录中没有 job.json：%s" % self.dir)
        self.spec = read_json(spec_path)
        version = int(self.spec.get("protocol", PROTOCOL_VERSION))
        if version != PROTOCOL_VERSION:
            raise JobError("任务协议版本 %s 与 Worker（%s）不符" % (version, PROTOCOL_VERSION))
        self.id: str = self.spec["job_id"]
        self.task: str = self.spec["task"]
        self.params: dict = dict(self.spec.get("params") or {})
        self.echo = echo
        self._seq = self._last_seq()
        self._events = open(self.dir / "events.ndjson", "a", encoding="utf-8")
        self._last_poll = 0.0
        self._cancel_seen = False
        #: 测试与脚本可挂的事件监听，签名 ``fn(event_dict)``
        self.listeners: list[Callable[[dict], None]] = []
        self.status: dict = {}
        self._lock = threading.Lock()      # 预测的写线程也发事件

    # ------------------------------------------------------------------ 路径
    def path(self, *parts: str) -> Path:
        return self.dir.joinpath(*parts)

    # ------------------------------------------------------------------ 事件
    def _last_seq(self) -> int:
        path = self.dir / "events.ndjson"
        if not path.is_file():
            return 0
        last = 0
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    last = max(last, int(json.loads(line).get("seq", 0)))
                except (ValueError, AttributeError):
                    continue
        return last

    def emit(self, event: str, message: str = "", **data: Any) -> dict:
        if event not in EVENT_TYPES:
            raise ValueError("未知事件类型：%s" % event)
        with self._lock:
            self._seq += 1
            record = {"v": PROTOCOL_VERSION, "job": self.id, "seq": self._seq,
                      "t": round(time.time(), 3), "event": event, "message": message,
                      "data": data}
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"),
                              default=_jsonable)
            self._events.write(line + "\n")
            self._events.flush()
            if self.echo:
                try:
                    sys.stdout.write(line + "\n")
                    sys.stdout.flush()
                except (OSError, ValueError):
                    pass
        for fn in list(self.listeners):
            fn(record)
        return record

    def log(self, message: str) -> None:
        self.emit("log", message)

    def warn(self, message: str, **data: Any) -> None:
        self.emit("warning", message, **data)

    def phase(self, name: str, message: str) -> None:
        self.emit("phase", message, phase=name)
        self.set_status(phase=name, phase_text=message)

    # ------------------------------------------------------------------ 状态
    def set_status(self, **fields: Any) -> None:
        with self._lock:
            self.status.update(fields)
            self.status["updated"] = time.time()
            write_json_atomic(self.dir / "status.json", self.status)

    def begin(self) -> None:
        self.status = {"job": self.id, "task": self.task, "state": RUNNING,
                       "pid": os.getpid(), "started": time.time(), "finished": None,
                       "worker_version": __version__}
        self.set_status()
        self.emit("started", "开始：%s" % self.spec.get("title", self.task),
                  task=self.task, pid=os.getpid(), python=sys.executable,
                  worker_version=__version__)

    def finish(self, state: str, message: str = "", **data: Any) -> None:
        event = {SUCCEEDED: "completed", FAILED: "failed", CANCELLED: "cancelled"}[state]
        self.emit(event, message, **data)
        self.set_status(state=state, finished=time.time(), message=message)

    # ------------------------------------------------------------------ 取消
    def cancel_requested(self) -> bool:
        if self._cancel_seen:
            return True
        now = time.monotonic()
        if now - self._last_poll >= self.CANCEL_POLL:
            self._last_poll = now
            self._cancel_seen = (self.dir / "cancel").exists()
        return self._cancel_seen

    def check_cancel(self) -> None:
        if self.cancel_requested():
            raise Cancelled()

    def clear_cancel(self) -> None:
        """续跑前清掉上一次留下的取消标志。"""
        try:
            (self.dir / "cancel").unlink()
        except FileNotFoundError:
            pass
        self._cancel_seen = False

    def close(self) -> None:
        try:
            self._events.close()
        except OSError:
            pass


def create_job_dir(root: str | Path, task: str, params: dict, title: str = "",
                   job_id: str | None = None) -> Path:
    """在任务根下建一个任务目录并写 ``job.json``，返回目录路径。"""
    job_id = job_id or new_job_id(task)
    job_dir = Path(root).expanduser().resolve() / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    spec = {"protocol": PROTOCOL_VERSION, "job_id": job_id, "task": task,
            "title": title or task, "params": params, "created": time.time()}
    write_json_atomic(job_dir / "job.json", spec)
    write_json_atomic(job_dir / "status.json",
                      {"job": job_id, "task": task, "state": QUEUED, "updated": time.time()})
    return job_dir


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    try:
        import numpy as np
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except ImportError:
        pass
    raise TypeError("无法写成 JSON：%r" % type(value))
