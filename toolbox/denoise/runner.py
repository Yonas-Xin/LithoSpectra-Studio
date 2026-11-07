# -*- coding: utf-8 -*-
"""隔离 HyDe Worker 的生命周期、进度、取消和错误映射。"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import envs
from ..core import ToolError
from .protocol import DenoiseRequest, WorkerEvent


@dataclass(frozen=True)
class WorkerResult:
    data: dict
    warnings: tuple[str, ...]
    seconds: float


class WorkerRunner:
    """每个请求启动一个 Worker 进程，避免 Torch 状态污染工具箱进程。"""

    def __init__(self, python_executable: Path, worker_script: Path) -> None:
        self.python_executable = Path(python_executable).expanduser()
        self.worker_script = Path(worker_script).expanduser()

    #: 探过的解释器，按路径缓存。探一次要启一个解释器，每件活重探太慢。
    _PROBES: dict[str, dict] = {}

    CREATE_HINT = (
        "去噪要求这个环境同时装齐 GDAL 与 torch + hyde-images。\n"
        "在左下角换一个装齐了的环境，或者按 workers/hyde_denoise/environment.yml\n"
        "把缺的装上（hyde-images 必须从固定提交安装，PyPI 上叫 hyde 的是同名的\n"
        "静态网站生成器，装了也没有算法）。"
    )

    def preflight(self, log: Callable[[str], None] | None = None) -> dict:
        """确认这个解释器真的能起 Worker：文件在、torch 与 hyde 都装了。

        必须在克隆产品之前调用。克隆一景是几个 GB 的复制，跑到启动 Worker 才发现
        环境不对，那份复制就白做了。

        检查落在 Runner 上而不是工具里，是因为"能不能启动 Worker"本来就是 Runner
        的职责；测试替换掉 Runner 时，这道检查也跟着一起被替换掉，不会拦住用假
        Worker 的用例。
        """
        from .. import envs

        if not self.python_executable.is_file():
            raise ToolError("跑算法的 Python 不存在：%s" % self.python_executable)
        if not self.worker_script.is_file():
            raise ToolError("HyDe Worker 入口不存在：%s" % self.worker_script)

        key = str(self.python_executable)
        info = self._PROBES.get(key)
        if info is None:
            info = envs.probe(self.python_executable)
            self._PROBES[key] = info
        if not envs.can_denoise(info):
            raise ToolError("当前运行环境跑不了去噪：%s\n%s\n%s"
                            % (self.python_executable,
                               envs.denoise_problem(info), self.CREATE_HINT))
        if log is not None:
            log("算法环境：%s（%s）" % (self.python_executable, envs.describe(info)))
        return info

    def run(self, request: DenoiseRequest,
            on_event: Callable[[WorkerEvent], None] | None = None,
            should_cancel: Callable[[], bool] | None = None,
            timeout_seconds: float | None = None) -> WorkerResult:
        if not self.python_executable.is_file():
            raise ToolError("跑算法的 Python 不存在：%s" % self.python_executable)
        if not self.worker_script.is_file():
            raise ToolError("HyDe Worker 入口不存在：%s" % self.worker_script)

        started = time.monotonic()
        process = subprocess.Popen(
            [str(self.python_executable), "-u", str(self.worker_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(self.worker_script.parent),
            # Worker 跑在另一个环境的解释器里：调用方的 GDAL_DATA / PROJ 路径
            # 对它是错的，一律摘掉；Windows 上也别给它弹一个黑窗口。
            env=envs.clean_environment(),
            **envs.no_window_flags(),
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_queue: queue.Queue[str | None] = queue.Queue()
        stderr_lines: list[str] = []

        def read_stdout() -> None:
            try:
                for line in process.stdout:
                    stdout_queue.put(line)
            finally:
                stdout_queue.put(None)

        def read_stderr() -> None:
            stderr_lines.extend(process.stderr.readlines())

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        process.stdin.write(request.to_json() + "\n")
        process.stdin.flush()
        process.stdin.close()

        warnings: list[str] = []
        completed: WorkerEvent | None = None
        failed: WorkerEvent | None = None
        stdout_done = False
        try:
            while True:
                if should_cancel is not None and should_cancel():
                    self._terminate(process)
                    raise ToolError("HyDe Worker 已取消")
                if timeout_seconds is not None and \
                        time.monotonic() - started > timeout_seconds:
                    self._terminate(process)
                    raise ToolError("HyDe Worker 超时（%.1f 秒）" % timeout_seconds)

                try:
                    line = stdout_queue.get(timeout=0.1)
                except queue.Empty:
                    line = ""
                if line is None:
                    stdout_done = True
                elif line.strip():
                    event = WorkerEvent.from_json(line)
                    if event.job_id != request.job_id:
                        raise ToolError("Worker 事件 job_id 不匹配：%s" % event.job_id)
                    if on_event is not None:
                        on_event(event)
                    if event.event == "warning":
                        warnings.append(event.message)
                    elif event.event == "failed":
                        failed = event
                    elif event.event == "completed":
                        completed = event

                code = process.poll()
                if code is not None and stdout_done:
                    break

            stdout_thread.join(timeout=1.0)
            stderr_thread.join(timeout=1.0)
            stderr = "".join(stderr_lines).strip()
            if failed is not None:
                detail = failed.message
                if failed.data.get("error_type"):
                    detail = "%s：%s" % (failed.data["error_type"], detail)
                raise ToolError("HyDe Worker 报错：%s" % detail)
            if process.returncode != 0:
                raise ToolError("HyDe Worker 异常退出（code=%d）：%s"
                                % (process.returncode, stderr or "无 stderr"))
            if completed is None:
                raise ToolError("HyDe Worker 正常退出但没有 completed 事件：%s"
                                % (stderr or "无 stderr"))
            return WorkerResult(completed.data, tuple(warnings),
                                time.monotonic() - started)
        except Exception:
            self._terminate(process)
            raise
        finally:
            if process.poll() is None:
                self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3.0)
