# -*- coding: utf-8 -*-
"""命令行入口。

::

    python -m litho_dl doctor [--json]        环境体检
    python -m litho_dl capabilities           任务与骨干声明（JSON）
    python -m litho_dl run <任务目录>          执行任务目录里的 job.json
    python -m litho_dl new <任务根> <任务> <参数.json>   建任务目录并打印其路径

需要先把 ``workers`` 目录放进 ``PYTHONPATH``（界面启动 Worker 时会设好）。
"""

from __future__ import annotations

import argparse
import json
import sys


def _print_json(data) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _utf8_stdio() -> None:
    """Windows 控制台默认 GBK，"✓"、"→" 之类写不出去会直接抛错；统一按 UTF-8 写，写不出的替换。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(prog="litho_dl", description="岩谱深度学习 Worker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    doc = sub.add_parser("doctor", help="环境体检")
    doc.add_argument("--json", action="store_true")
    sub.add_parser("capabilities", help="任务与骨干声明")
    run = sub.add_parser("run", help="执行任务目录")
    run.add_argument("job_dir")
    run.add_argument("--quiet", action="store_true", help="事件只写文件，不打到标准输出")
    new = sub.add_parser("new", help="建任务目录")
    new.add_argument("root")
    new.add_argument("task")
    new.add_argument("params_json")
    args = parser.parse_args(argv)

    if args.cmd == "doctor":
        from .doctor import diagnose, format_report
        report = diagnose()
        if args.json:
            _print_json(report)
        else:
            sys.stdout.write(format_report(report) + "\n")
        return 0 if report["ok"] else 1
    if args.cmd == "capabilities":
        from .tasks import capabilities
        _print_json(capabilities())
        return 0
    if args.cmd == "run":
        from .tasks import run_job
        return run_job(args.job_dir, echo=not args.quiet)
    if args.cmd == "new":
        from .tasks import create_job
        with open(args.params_json, "r", encoding="utf-8") as fh:
            params = json.load(fh)
        sys.stdout.write(str(create_job(args.root, args.task, params)) + "\n")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
