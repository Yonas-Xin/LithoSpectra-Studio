# -*- coding: utf-8 -*-
"""预测进行中的实时图层。

Worker 每完成一块，就把类别写进任务目录里的 ``preview.u8``（行×列的 uint8
memmap，255 表示还没算到或背景），然后才发 ``block_done`` 事件。本图层每次出图
都从这份文件里现取视窗覆盖的那一块：

* 不读 Worker 正在写的 GeoTIFF：两个进程同时开一个 GDAL 数据集，读到的是
  半写的块缓存；
* 不走 GDAL 的块缓存：每次重新映射文件，Worker 刚写进去的块立刻看得见；
* 缩得很小时按步长抽稀再交给 ``gdal.Warp``，一帧的开销与影像大小无关。

预测完成后，接口层把它原地换成正式成果的普通栅格图层（同一个图层号，图层顺序、
透明度、显隐都不变）。
"""

from __future__ import annotations

import math
import os
from typing import Dict, List, Optional, Sequence

import numpy as np
from osgeo import gdal, osr

from samplelib.annotate import georef
from samplelib.annotate import layers as L

PENDING = 255
DENSIFY = 8


def _hex(color: str) -> tuple:
    c = (color or "#888888").lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


class LivePredictionLayer(L.RasterLayerBase):
    kind = "raster"

    def __init__(self, job_id: str, preview: str, init: Dict, name: str = "") -> None:
        super().__init__(name or "实时预测", opacity=0.85)
        self.job_id = job_id
        self.preview = preview
        self.path = ""                       # 不让显示金字塔去给它做替身
        self.rows = int(init["rows"])
        self.cols = int(init["cols"])
        self.gt = tuple(init["geotransform"])
        self.proj = init.get("projection") or ""
        self.classes: List[dict] = list(init.get("classes") or [])
        self.lut = np.zeros((256, 4), dtype=np.uint8)
        if init.get("lut"):                  # 相似度检索：连续色带
            self.lut[:] = np.asarray(init["lut"], dtype=np.uint8)[:256]
        for row in self.classes:
            label = int(row["label"])
            if 0 <= label < 255:
                self.lut[label] = _hex(row.get("color", "")) + (255,)
        self.srs = osr.SpatialReference()
        if self.proj:
            self.srs.ImportFromWkt(self.proj)
        self.srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        probe = gdal.GetDriverByName("VRT").Create("", self.cols, self.rows, 0)
        probe.SetGeoTransform(self.gt)
        if self.proj:
            probe.SetProjection(self.proj)
        self._extent = georef.extent_4326(probe)
        probe = None
        self.info_lines = ["预测进行中：%d × %d 像元" % (self.cols, self.rows),
                           "已完成的分块即时显示，全部完成后替换为分类图"]

    @property
    def kind_cn(self) -> str:
        return "实时预测"

    def extent(self):
        return self._extent

    def _window(self, view: L.ViewSpec):
        """视窗在影像上覆盖的像元窗口 (r0, r1, c0, c1)；不相交返回 None。"""
        view_srs = osr.SpatialReference()
        view_srs.ImportFromEPSG(int(view.epsg))
        view_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = osr.CoordinateTransformation(view_srs, self.srs)
        x0, y0, x1, y1 = view.bounds
        xs, ys = [], []
        for i in range(DENSIFY + 1):
            t = i / DENSIFY
            for x, y in ((x0 + t * (x1 - x0), y0), (x0 + t * (x1 - x0), y1),
                         (x0, y0 + t * (y1 - y0)), (x1, y0 + t * (y1 - y0))):
                try:
                    px, py, _ = tr.TransformPoint(x, y)
                except Exception:
                    continue
                if math.isfinite(px) and math.isfinite(py):
                    xs.append(px)
                    ys.append(py)
        if not xs:
            return None
        inv = gdal.InvGeoTransform(self.gt)
        cols, rows = [], []
        for x in (min(xs), max(xs)):
            for y in (min(ys), max(ys)):
                c, r = gdal.ApplyGeoTransform(inv, x, y)
                cols.append(c)
                rows.append(r)
        c0, c1 = max(0, int(math.floor(min(cols))) - 1), min(self.cols, int(math.ceil(max(cols))) + 1)
        r0, r1 = max(0, int(math.floor(min(rows))) - 1), min(self.rows, int(math.ceil(max(rows))) + 1)
        if c0 >= c1 or r0 >= r1:
            return None
        return r0, r1, c0, c1

    def render_array(self, view: L.ViewSpec) -> Optional[np.ndarray]:
        if not view.valid or self._extent is None or not os.path.isfile(self.preview):
            return None
        if not L.intersects(self._extent, view.lonlat_bounds):
            return None
        with self._render_lock:
            win = self._window(view)
            if win is None:
                return None
            r0, r1, c0, c1 = win
            step = max(1, int(min((c1 - c0) / max(1, view.width),
                                  (r1 - r0) / max(1, view.height)) // 1.5))
            data = np.memmap(self.preview, dtype=np.uint8, mode="r",
                             shape=(self.rows, self.cols))
            sub = np.array(data[r0:r1:step, c0:c1:step])
            del data
            if not (sub != PENDING).any():
                return None
            h, w = sub.shape
            mem = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
            gt = self.gt
            mem.SetGeoTransform((gt[0] + c0 * gt[1] + r0 * gt[2], gt[1] * step, gt[2] * step,
                                 gt[3] + c0 * gt[4] + r0 * gt[5], gt[4] * step, gt[5] * step))
            if self.proj:
                mem.SetProjection(self.proj)
            band = mem.GetRasterBand(1)
            band.WriteArray(sub)
            band.SetNoDataValue(PENDING)
            minx, miny, maxx, maxy = view.bounds
            warped = gdal.Warp("", mem, format="MEM", dstSRS="EPSG:%d" % view.epsg,
                               outputBounds=(minx, miny, maxx, maxy),
                               width=view.width, height=view.height,
                               resampleAlg=gdal.GRA_NearestNeighbour, dstNodata=PENDING,
                               errorThreshold=0.125)
            mem = None
            if warped is None:
                return None
            index = warped.GetRasterBand(1).ReadAsArray()
            warped = None
            return np.ascontiguousarray(self.lut[index])

    def legend(self) -> List[dict]:
        return [{"label": int(r["label"]), "name": r.get("name", ""), "color": r.get("color", "")}
                for r in self.classes]
