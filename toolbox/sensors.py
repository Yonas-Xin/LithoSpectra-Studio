# -*- coding: utf-8 -*-
"""传感器波段参数。

影像本身没写波长时的兜底来源。参数文件放在仓库根的 ``assets/``，一份一颗卫星，
格式就是 ENVI 头里那两段：

    wavelength = { 386.860000, 395.440000, ... }
    fwhm       = { 5.000000, 5.000000, ... }

放一个新传感器进去不需要改代码，丢一个同格式的 ``.txt`` 即可。
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from .core import ToolError

#: 传感器参数目录（仓库根的 assets/）
SENSOR_DIR = Path(__file__).resolve().parent.parent / "assets"

_WAVELENGTH_RE = re.compile(r"wavelength\s*=\s*\{(.*?)\}", re.DOTALL | re.IGNORECASE)
_FWHM_RE = re.compile(r"fwhm\s*=\s*\{(.*?)\}", re.DOTALL | re.IGNORECASE)


def _parse(path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    match = _WAVELENGTH_RE.search(text)
    if match is None:
        raise ToolError("传感器参数文件缺少 wavelength 段：%s" % path)
    wavelengths = np.fromstring(match.group(1).replace("\n", " "), sep=",")
    if wavelengths.size == 0:
        raise ToolError("传感器参数文件的 wavelength 段解析为空：%s" % path)

    fwhm = None
    match = _FWHM_RE.search(text)
    if match is not None:
        values = np.fromstring(match.group(1).replace("\n", " "), sep=",")
        if values.size == wavelengths.size:
            fwhm = values
    return wavelengths, fwhm


def available_sensors() -> list[str]:
    """列出能解析出波长的传感器名（文件名去掉 .txt）。"""
    if not SENSOR_DIR.is_dir():
        return []
    names = []
    for path in sorted(SENSOR_DIR.glob("*.txt")):
        try:
            _parse(path)
        except (ToolError, OSError, ValueError):
            continue     # 目录里混着说明文件是常事，解析不了就当它不是参数
        names.append(path.stem)
    return names


def load_sensor(name: str) -> tuple[np.ndarray, np.ndarray | None]:
    """按名字取一颗卫星的 (wavelengths, fwhm)，fwhm 可能为 None。"""
    path = SENSOR_DIR / ("%s.txt" % name)
    if not path.exists():
        raise ToolError("没有传感器 %r 的参数文件。可用：%s"
                        % (name, ", ".join(available_sensors()) or "无"))
    return _parse(path)
