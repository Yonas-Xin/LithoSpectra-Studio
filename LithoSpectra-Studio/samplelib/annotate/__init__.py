# -*- coding: utf-8 -*-
"""图层与编辑。

图层模型与渲染（:mod:`layers`）、配准嗅探（:mod:`georef`）、在线瓦片（:mod:`tiles`）、
图层描述的存取（:mod:`layerspec`），以及样本几何的暂存编辑（:mod:`editing`）。
画布本身在 Web 页面里，接口在 :mod:`webapi.layer_routes`。
"""

__all__ = ["georef", "editing", "layers", "tiles", "layerspec"]
