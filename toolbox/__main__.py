# -*- coding: utf-8 -*-
"""作业入口：``python -m toolbox --job job.json``。

界面要让使用者挑运行环境，就不能在自己进程里跑批——换环境的唯一办法是另起一个
解释器。这个模块就是那个被启动的一端：读一份作业描述，把事件按行吐到标准输出。
顺带的好处是 GDAL 崩了只崩这个子进程，界面还在。

事件是每行一条 JSON，前面加 ``@@TB@@`` 前缀：GDAL 和第三方库时不时往 stdout 里
写点东西，有前缀才分得清哪些是事件、哪些是噪声。

取消走标志文件而不是信号：Windows 上没有 SIGTERM 那套语义，而"文件在不在"到哪
都一样，也不会和 Qt 的进程管理打架。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

PREFIX = "@@TB@@"


def emit(kind: str, **fields) -> None:
    fields["t"] = kind
    try:
        sys.stdout.write(PREFIX + json.dumps(fields, ensure_ascii=False) + "\n")
        sys.stdout.flush()  # 不刷新的话界面上的日志会一卡一卡地成块蹦出来
    except (BrokenPipeError, ValueError):
        # 读事件的那一端没了（界面被杀、输出被截断）。这一件还是要跑完并写完
        # 产物，不能因为没人听就半途炸掉。
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toolbox", description="工具箱作业入口")
    parser.add_argument("--job", required=True, help="作业描述 JSON 的路径")
    args = parser.parse_args(argv)

    # 本模块可能被别的解释器以 -m 启动，仓库根不一定在 sys.path 上
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)

    try:
        job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        emit("error", m="读不了作业描述：%s" % exc)
        return 2

    try:
        from toolbox import run_batch
        from toolbox.core import ToolError
    except Exception as exc:                       # noqa: BLE001 环境不对时要说清楚
        emit("error", m="这个环境里导入工具箱失败：%s: %s" % (type(exc).__name__, exc))
        return 2

    cancel_file = job.get("cancel_file")

    def should_stop() -> bool:
        return bool(cancel_file) and os.path.exists(cancel_file)

    try:
        report = run_batch(
            job["tool"],
            inputs=job["inputs"],
            output_dir=job["output_dir"],
            params=job.get("params") or {},
            overwrite=bool(job.get("overwrite")),
            stop_on_error=bool(job.get("stop_on_error")),
            dry_run=bool(job.get("dry_run")),
            should_stop=should_stop,
            progress=lambda i, n, key: emit("progress", i=i, n=n, k=key),
            log=lambda text: emit("log", m=text),
        )
    except ToolError as exc:
        emit("error", m=str(exc))
        return 2
    except Exception as exc:                       # noqa: BLE001 子进程不能让异常裸奔
        emit("error", m="%s: %s" % (type(exc).__name__, exc))
        sys.stderr.write(traceback.format_exc())
        return 2

    emit("done", summary=report.summary(), ok=report.ok,
         completed=len(report.completed), skipped=len(report.skipped),
         failed=len(report.failed))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
