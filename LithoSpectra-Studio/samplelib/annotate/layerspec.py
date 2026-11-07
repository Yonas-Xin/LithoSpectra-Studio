# -*- coding: utf-8 -*-
"""图层与它的**可序列化描述**之间的互转。

存的是**怎么看**，不是数据本身：图层清单、各层的路径 / 波段 / 拉伸 / 透明度 /
顺序。样本始终以样本库里的为准，这里不存任何几何。

引用的影像 / 矢量按相对路径存（同盘时），整个目录搬走仍能找回来。

它服务的是"退出时记下这一刻的界面、下次打开原样铺回来"那条链路
（:mod:`samplelib.appstate` 与 ``LibraryWindow._save_state``）。这个模块原来叫
``project``，连带一套 ``.slproj`` 标注工程文件的读写；那套东西已经删掉——图层
现场每次退出都自动记着，再让人手动"保存工程"是重复一遍同一件事。剩下的只是这层
互转，所以改名叫 ``layerspec``。
"""

import os
from typing import Any, Dict, Optional

from .. import config
from . import layers as L, tiles


# =============================================================================
# 图层 <-> 描述
# =============================================================================

def layer_to_spec(layer, base_dir: str = "") -> Optional[Dict[str, Any]]:
    """图层 -> 可序列化的描述。无法复现的图层返回 None。"""
    common = {"name": layer.name, "visible": bool(layer.visible),
              "opacity": round(float(layer.opacity), 4)}
    if isinstance(layer, L.SampleLayer):
        return dict(common, type="samples", fill=bool(layer.fill),
                    point_radius=layer.point_radius)
    if isinstance(layer, L.BasemapLayer):
        return dict(common, type="basemap", source=layer.source.name)
    if isinstance(layer, L.RasterLayer):
        return dict(common,
                    type="image" if isinstance(layer, L.ImageLayer) else "raster",
                    path=_rel(layer.path, base_dir),
                    bands=list(layer.bands),
                    stretch=list(layer.stretch_percent),
                    use_color_table=bool(layer.use_color_table),
                    wavelength_file=_rel(getattr(layer, "_wavelength_file", ""),
                                         base_dir))
    if isinstance(layer, L.VectorLayer):
        return dict(common, type="vector", path=_rel(layer.path, base_dir),
                    layer_index=getattr(layer, "layer_index", 0),
                    color=layer.color, fill=bool(layer.fill),
                    fill_alpha=int(layer.fill_alpha),
                    line_width=layer.line_width,
                    color_field=layer.color_field,
                    color_map=dict(layer.color_map))
    return None


def spec_to_layer(spec: Dict[str, Any], base_dir: str, buffer=None,
                  fetcher=None, on_tile=None):
    """描述 -> 图层。文件缺失或格式不支持时抛异常，由调用方汇总为警告。"""
    kind = spec.get("type")
    if kind == "samples":
        if buffer is None:
            raise ValueError("没有可用的样本缓冲区")
        layer = L.SampleLayer(buffer, name=spec.get("name") or "样本库")
        layer.fill = bool(spec.get("fill", True))
        layer.point_radius = spec.get("point_radius", config.POINT_RADIUS)
    elif kind == "basemap":
        if fetcher is None:
            raise ValueError("没有可用的瓦片下载器")
        sources = tiles.build_sources()
        src = sources.get(spec.get("source", ""))
        if src is None:
            raise ValueError("底图「%s」已不在配置中" % spec.get("source"))
        if not fetcher.usable(src):
            raise ValueError("底图「%s」需要 %s，当前没有配置"
                             % (src.name, src.key_name))
        layer = L.BasemapLayer(src, fetcher, on_tile=on_tile)
    elif kind in ("raster", "image"):
        path = _abs(spec.get("path", ""), base_dir)
        cls = L.ImageLayer if kind == "image" else L.RasterLayer
        layer = cls(path, name=spec.get("name") or "")
        if spec.get("bands"):
            layer.set_bands(spec["bands"])
        if spec.get("stretch") and len(spec["stretch"]) == 2:
            layer.set_stretch(*spec["stretch"])
        if "use_color_table" in spec:
            layer.set_use_color_table(spec["use_color_table"])
        wl = _abs(spec.get("wavelength_file", ""), base_dir)
        if wl and os.path.isfile(wl):
            layer.load_wavelengths(wl)
    elif kind == "vector":
        path = _abs(spec.get("path", ""), base_dir)
        layer = L.VectorLayer(path, layer_index=int(spec.get("layer_index", 0)),
                              name=spec.get("name") or "",
                              color=spec.get("color", config.DEFAULT_VECTOR_COLOR))
        layer.fill = bool(spec.get("fill", True))
        layer.fill_alpha = int(spec.get("fill_alpha", config.VECTOR_FILL_ALPHA))
        layer.line_width = float(spec.get("line_width", 1.2))
        if spec.get("color_field"):
            layer.set_unique_colors(spec["color_field"],
                                    spec.get("color_map") or {})
    else:
        raise ValueError("不认识的图层类型：%r" % kind)

    layer.name = spec.get("name") or layer.name
    layer.visible = bool(spec.get("visible", True))
    layer.opacity = float(spec.get("opacity", 1.0))
    return layer


# 相对路径最多允许往上几层。数据放在完全不相干的目录时，相对路径会退化成
# 一长串 ".."，既难读也经不起挪动，这种情况直接存绝对路径。
MAX_RELATIVE_UPS = 3


def _rel(path: str, base_dir: str) -> str:
    if not path or not base_dir:
        return path or ""
    try:
        if os.path.splitdrive(path)[0] != os.path.splitdrive(base_dir)[0]:
            return path
        rel = os.path.relpath(path, base_dir)
    except ValueError:
        return path
    ups = 0
    for part in rel.split(os.sep):
        if part != os.pardir:
            break
        ups += 1
    return path if ups > MAX_RELATIVE_UPS else rel


def _abs(path: str, base_dir: str) -> str:
    if not path or os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base_dir, path))
