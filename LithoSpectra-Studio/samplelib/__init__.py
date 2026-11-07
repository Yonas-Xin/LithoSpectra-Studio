# -*- coding: utf-8 -*-
"""岩谱 LithoSpectra —— 地质样本库。

名字取"谱"字的三重含义：光谱（高光谱岩性识别）、图谱（样本库本身）、
谱系（多级分类树）。

样本以单文件 GeoPackage(.gpkg) 存储，兼容三种既有的样本组织方式，
类别支持任意层数的分级，提供导入 / 检索 / 空间筛选 / 编辑 / 导出能力，
界面是 Web 页面（``web/``），由 :mod:`webapi` 提供后端接口、pywebview 提供桌面窗口：
浏览模式用于检索、筛选与导出，标注模式用于叠加影像与绘制、编辑样本几何。
"""

from .config import APP_NAME, APP_NAME_EN, APP_TITLE
from .model import SampleClass, SampleRecord, GEOM_POINT, GEOM_POLYGON
from .store import SampleStore

__all__ = [
    "APP_NAME",
    "APP_NAME_EN",
    "APP_TITLE",
    "SampleClass",
    "SampleRecord",
    "SampleStore",
    "GEOM_POINT",
    "GEOM_POLYGON",
]

__version__ = "1.0.0"
