# -*- coding: utf-8 -*-
"""长耗时操作：后台线程跑，界面轮询进度，可以取消。

各 io 模块的进度回调是
``progress(done, total, name) -> bool``，返回 ``False`` 表示使用者按了取消，
调用方尽快收尾返回。这里的 :class:`Task` 就是那个回调。

为什么可以开线程
----------------
GDAL 数据集不是线程安全的。这里所有碰样本库
的接口都串在 ``AppState.lock`` 这一把锁上，任务线程整段持锁——跑任务期间别的
接口只能排队，等于把样本库独占给了任务。只有查进度、按取消这两个接口不拿锁，
它们只碰本模块的内存状态。

同一时刻只跑一个任务：前端在任务期间显示模态进度框，不会有人在导入进行到一半
时又去点导出；后端再拦一道，防止重复提交。
"""

import itertools
import threading
import time
import traceback
from typing import Any, Callable, Dict, Optional

RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"

# 任务结束后结果保留多久（秒）。前端拿到结果就不再问，留一会儿只为网络抖动时重取。
KEEP_FINISHED = 600


class Cancelled(Exception):
    """任务函数可以抛它表示"按取消停下了"，与正常返回一样不算失败。"""


class Task:
    """一次长任务的状态，同时充当 io 模块的进度回调。"""

    def __init__(self, task_id: str, title: str, text: str = "",
                 cancellable: bool = True) -> None:
        self.id = task_id
        self.title = title
        self.text = text
        self.cancellable = cancellable
        self.status = RUNNING
        self.done = 0
        self.total = 0
        self.result: Any = None
        self.error = ""
        self.started = time.time()
        self.finished = 0.0
        self._cancel = threading.Event()

    # -------------------------------------------------------------- 回调
    def __call__(self, done: int, total: int, name: str = "") -> bool:
        self.done = int(done)
        self.total = int(total)
        if name:
            self.text = str(name)
        return not self._cancel.is_set()

    def note(self, text: str) -> None:
        """只更新说明文字，不改进度。"""
        self.text = text

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        if self.cancellable:
            self._cancel.set()

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "text": self.text,
            "status": self.status, "done": self.done, "total": self.total,
            "cancellable": self.cancellable, "cancel_requested": self.cancelled,
            "result": self.result if self.status in (DONE, CANCELLED) else None,
            "error": self.error,
            "elapsed": round((self.finished or time.time()) - self.started, 2),
        }


class TaskManager:
    """登记、启动、查询任务。"""

    def __init__(self, lock: threading.RLock) -> None:
        self._lock = lock                     # 样本库那把锁，任务整段持有
        self._tasks: Dict[str, Task] = {}
        self._ids = itertools.count(1)
        self._guard = threading.Lock()        # 只保护 _tasks 本身

    def busy(self) -> Optional[Task]:
        with self._guard:
            for t in self._tasks.values():
                if t.status == RUNNING:
                    return t
        return None

    def start(self, title: str, fn: Callable[[Task], Any], *, text: str = "",
              cancellable: bool = True, exclusive: bool = True) -> Task:
        """起一个任务。``fn(task)`` 在后台线程里跑，返回值即任务结果（要能转 JSON）。

        ``exclusive`` 为假时不拿样本库那把锁：裁剪这类另开只读连接的任务用它，
        跑的时候界面还能照常翻库。
        """
        running = self.busy()
        if running is not None:
            raise RuntimeError("「%s」尚未完成，请等待其结束后再试。" % running.title)
        task = Task("t%d" % next(self._ids), title, text, cancellable)
        with self._guard:
            self._prune()
            self._tasks[task.id] = task

        def body() -> None:
            try:
                task.result = fn(task)
                task.status = CANCELLED if task.cancelled else DONE
            except Cancelled:
                task.status = CANCELLED
            except Exception as exc:          # 任务失败要报给界面，不能吞掉
                task.error = str(exc) or exc.__class__.__name__
                task.status = ERROR
                traceback.print_exc()
            finally:
                task.finished = time.time()

        def work() -> None:
            if exclusive:
                with self._lock:
                    body()
            else:
                body()

        threading.Thread(target=work, daemon=True, name="task-" + task.id).start()
        return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._guard:
            return self._tasks.get(task_id)

    def _prune(self) -> None:
        now = time.time()
        for tid in [tid for tid, t in self._tasks.items()
                    if t.status != RUNNING and now - t.finished > KEEP_FINISHED]:
            del self._tasks[tid]
