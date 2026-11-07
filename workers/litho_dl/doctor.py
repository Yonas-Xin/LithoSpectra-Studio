# -*- coding: utf-8 -*-
"""环境体检：这个 Python 能不能跑训练与预测，用哪块设备。

界面选环境时调用 ``python -m litho_dl doctor --json``。每一项给出结论与修复
建议，不自动安装任何东西。
"""

from __future__ import annotations

import platform
import sys
import tempfile
import time


def diagnose() -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, hint: str = "", required: bool = True):
        checks.append({"name": name, "ok": ok, "detail": detail, "hint": hint,
                       "required": required})

    add("Python", sys.version_info >= (3, 9), "%s · %s" % (platform.python_version(),
                                                        sys.executable),
        "需要 Python 3.9 或更高版本")
    import numpy
    add("NumPy", True, numpy.__version__)
    device_info = {}
    try:
        import torch
        add("PyTorch", True, torch.__version__)
        from . import devices
        dev = devices.resolve_device("auto")
        device_info = {"device": dev.type, "describe": devices.describe(dev),
                       "cuda": torch.cuda.is_available(), "mps": devices.mps_available()}
        t0 = time.perf_counter()
        x = torch.randn(512, 512, device=dev)
        (x @ x).sum().item()
        add("计算设备", True, "%s（自检 %.0f ms）" % (devices.describe(dev),
                                               (time.perf_counter() - t0) * 1000))
    except ImportError:
        add("PyTorch", False, "未安装",
            "macOS 执行 pip install torch；Windows 有 NVIDIA 显卡时，按 pytorch.org 的说明安装 CUDA 版")
    except Exception as exc:
        add("计算设备", False, "%s: %s" % (type(exc).__name__, exc),
            "PyTorch 已安装但无法运行，通常是 CUDA 驱动与 PyTorch 版本不匹配")
    try:
        import h5py
        add("h5py", True, h5py.__version__, "pip install h5py")
    except ImportError:
        add("h5py", False, "未安装", "conda install h5py 或 pip install h5py")
    try:
        from osgeo import gdal
        add("GDAL", True, gdal.__version__)
    except ImportError:
        add("GDAL", False, "未安装", "conda install -c conda-forge gdal（预测需要）")
    try:
        with tempfile.TemporaryFile() as fh:
            fh.write(b"ok")
        add("临时目录可写", True, tempfile.gettempdir())
    except OSError as exc:
        add("临时目录可写", False, str(exc))
    from . import PROTOCOL_VERSION, __version__
    return {"ok": all(c["ok"] for c in checks if c["required"]),
            "protocol": PROTOCOL_VERSION, "worker_version": __version__,
            "platform": "%s %s" % (platform.system(), platform.machine()),
            "python": sys.executable, "checks": checks, **device_info}


def format_report(report: dict) -> str:
    lines = ["岩谱深度学习 Worker %s · %s" % (report["worker_version"], report["platform"])]
    for c in report["checks"]:
        mark = "✓" if c["ok"] else "✗"
        lines.append("  %s %-8s %s" % (mark, c["name"], c["detail"]))
        if not c["ok"] and c["hint"]:
            lines.append("      → %s" % c["hint"])
    lines.append("结论：%s" % ("可以使用" if report["ok"] else "缺少必要组件"))
    return "\n".join(lines)
