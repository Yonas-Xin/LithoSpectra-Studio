# -*- coding: utf-8 -*-
"""内置工具。

加一件新工具：写一个模块，实现 ``toolbox.core.Tool``，在模块末尾留一个
``TOOL = YourTool()``，然后在下面的 ``_MODULES`` 里添一行。工具自己不写循环、
不写跳过、不写异常兜底——那些是 ``toolbox.batch`` 的事。
"""

from __future__ import annotations

from importlib import import_module

from ..core import register

#: 模块名 → 模块里那个 TOOL 实例
_MODULES = (
    "remove_water_vapor",
    "sentinel2_l2a_stack",
    "hyde_denoise_product",
)


def _load() -> None:
    for name in _MODULES:
        module = import_module(".%s" % name, __name__)
        register(module.TOOL)


_load()
