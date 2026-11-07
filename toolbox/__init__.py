# -*- coding: utf-8 -*-
"""岩谱工具箱：遥感数据预处理工具的统一外壳。

一件工具只描述怎么处理**一个**对象，批量永远走 ``run_batch``。

    from toolbox import run_batch, describe_tools

    describe_tools()                       # 列出所有工具与参数
    run_batch("remove_water_vapor",        # 跑一批
              inputs="/data/ZY1F",
              output_dir="/data/cleaned",
              params={"sensor": "ZY1F"})

只依赖 GDAL 与 NumPy，桌面端（samplelib 环境）与研究端（geo 环境）都能导入。
"""

from __future__ import annotations

from .batch import BatchReport, ItemResult, describe_tools, run_batch
from .core import (Param, Tool, ToolError, WorkItem, all_tools, get,
                   register, when)

__all__ = [
    "BatchReport",
    "ItemResult",
    "Param",
    "Tool",
    "ToolError",
    "WorkItem",
    "all_tools",
    "describe_tools",
    "get",
    "register",
    "run_batch",
    "when",
]
