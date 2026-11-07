# -*- coding: utf-8 -*-
"""计算设备与精度。

macOS 上是 MPS 或 CPU，Windows 上是 CUDA 或 CPU。``auto`` 依次取 CUDA、MPS、CPU。

精度只在推理时自动降到半精度：实测 MPS 上 batch 8192 时 2D CNN 从 9.3 万像元/秒
升到 25 万，1D CNN 从 2.9 万升到 12 万；CPU 上半精度反而慢，保持 fp32。训练在
CUDA 上用自动混合精度，MPS 与 CPU 上用 fp32，因为 MPS 的混合精度训练还不稳定。
"""

from __future__ import annotations

import os
import sys

import numpy as np
import torch

DEVICE_CHOICES = ("auto", "cuda", "mps", "cpu")
PRECISION_CHOICES = ("auto", "fp32", "fp16")


class DeviceError(RuntimeError):
    """指定的设备在这台机器上不可用。"""


def mps_available() -> bool:
    backend = getattr(torch.backends, "mps", None)
    return bool(backend is not None and backend.is_available())


def resolve_device(name: str = "auto") -> torch.device:
    name = (name or "auto").lower()
    if name == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda:0")
        if mps_available():
            return torch.device("mps")
        return torch.device("cpu")
    if name.startswith("cuda"):
        if not torch.cuda.is_available():
            raise DeviceError("当前环境的 PyTorch 无法使用 CUDA（没有 NVIDIA 显卡，"
                              "或安装的是 CPU 版 PyTorch）。请将计算设备改为「自动」或 CPU。")
        return torch.device("cuda:0" if name == "cuda" else name)
    if name == "mps":
        if not mps_available():
            raise DeviceError("本机无法使用 MPS（不是 Apple 芯片，或 macOS、PyTorch 版本过低）。"
                              "请将计算设备改为「自动」或 CPU。")
        return torch.device("mps")
    if name == "cpu":
        return torch.device("cpu")
    raise DeviceError("未知设备：%s，可选 %s" % (name, " / ".join(DEVICE_CHOICES)))


def inference_dtype(device: torch.device, precision: str = "auto") -> torch.dtype:
    precision = (precision or "auto").lower()
    if precision == "fp32" or device.type == "cpu":
        return torch.float32
    if precision in ("fp16", "auto"):
        return torch.float16
    raise ValueError("未知精度：%s" % precision)


def train_amp(device: torch.device) -> bool:
    """训练是否开自动混合精度。"""
    return device.type == "cuda"


def synchronize(device: torch.device) -> None:
    if device.type == "cuda":
        torch.cuda.synchronize()
    elif device.type == "mps":
        torch.mps.synchronize()


def empty_cache(device: torch.device) -> None:
    if device.type == "cuda":
        torch.cuda.empty_cache()
    elif device.type == "mps":
        torch.mps.empty_cache()


def describe(device: torch.device) -> str:
    if device.type == "cuda":
        props = torch.cuda.get_device_properties(device)
        return "CUDA · %s · %.1f GB" % (props.name, props.total_memory / 2 ** 30)
    if device.type == "mps":
        return "MPS · Apple 芯片"
    return "CPU · %d 核" % (os.cpu_count() or 1)


def system_memory() -> tuple[int, int]:
    """(总内存, 可用内存)，字节。取不到时返回 (0, 0)。"""
    try:
        if sys.platform == "win32":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return int(status.ullTotalPhys), int(status.ullAvailPhys)
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        if sys.platform == "darwin":
            # macOS 没有 SC_AVPHYS_PAGES；页缓存随用随还，按总量的一半估可用量
            return int(total), int(total * 0.5)
        return int(total), int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES"))
    except (OSError, ValueError, AttributeError):
        return 0, 0


def memory_budget(device: torch.device, fraction: float = 0.4) -> int:
    """数据驻留可用的字节数。CUDA 看显存，MPS 与 CPU 看内存（统一内存）。"""
    if device.type == "cuda":
        free, _total = torch.cuda.mem_get_info(device)
        return int(free * fraction)
    _total, avail = system_memory()
    if avail <= 0:
        return 2 * 2 ** 30
    return int(avail * fraction)


def seed_everything(seed: int) -> np.random.Generator:
    torch.manual_seed(seed)
    return np.random.default_rng(seed)
