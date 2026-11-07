# -*- coding: utf-8 -*-
"""坐标系：状态栏读数用的显示坐标系，以及常用坐标系清单。

**这里改的只是"读数怎么显示"，不动存储。** 样本库里的几何一律存 EPSG:4326
（见 README 第 2 节），换显示坐标系不会重写任何数据，也不影响导出——导出的
坐标系在导出对话框里单独选。把两件事混在一起是这类软件最容易出的事故：
以为只是换个读数，结果把整库几何投影了一遍。

清单为什么是这几个
------------------
野外与遥感处理里真正会用到的就三类：经纬度（WGS 84 / CGCS2000）、Web 墨卡托
（跟底图对坐标时用）、以及 UTM 分带（量距离、算面积、做投影后的裁剪）。UTM 有
120 个带，全列出来找起来还不如不列，因此按当前视野中心算出该用哪个带，把它放在
最前面并标出来。
"""

from typing import List, Optional, Tuple

from osgeo import osr

# (EPSG, 显示名)。UTM 带由 utm_epsg / utm_label 动态生成，不在这张表里。
BASE_CRS: List[Tuple[int, str]] = [
    (4326, "WGS 84（经纬度）"),
    (4490, "CGCS2000（经纬度）"),
    (3857, "WGS 84 / Pseudo-Mercator（Web 墨卡托）"),
]

DEFAULT_EPSG = 4326


def utm_epsg(lon: float, lat: float) -> int:
    """按经纬度算出该用哪个 WGS 84 / UTM 带。"""
    zone = int((lon + 180.0) / 6.0) + 1
    zone = min(60, max(1, zone))
    return (32600 if lat >= 0 else 32700) + zone


def utm_label(epsg: int) -> str:
    zone = epsg % 100
    hemi = "N" if 32600 < epsg < 32700 else "S"
    return "WGS 84 / UTM zone %d%s" % (zone, hemi)


def label_of(epsg: int) -> str:
    for code, name in BASE_CRS:
        if code == epsg:
            return name
    if 32601 <= epsg <= 32660 or 32701 <= epsg <= 32760:
        return utm_label(epsg)
    return describe(epsg) or "EPSG:%d" % epsg


def describe(epsg: int) -> str:
    """向 PROJ 要这个 EPSG 的正式名称。查不到返回空串。"""
    srs = osr.SpatialReference()
    try:
        if srs.ImportFromEPSG(int(epsg)) != 0:
            return ""
    except Exception:
        return ""
    return srs.GetName() or ""


def is_valid(epsg: int) -> bool:
    return bool(describe(epsg))


def is_projected(epsg: int) -> bool:
    srs = osr.SpatialReference()
    try:
        if srs.ImportFromEPSG(int(epsg)) != 0:
            return False
    except Exception:
        return False
    return bool(srs.IsProjected())


class Formatter:
    """把 EPSG:4326 的经纬度换算成显示坐标系的读数。

    构造一次、复用同一个 ``CoordinateTransformation``：鼠标每动一像素就要换算
    一次，每次新建变换对象的开销在移动时是看得见的。
    """

    def __init__(self, epsg: int = DEFAULT_EPSG) -> None:
        self.epsg = int(epsg)
        self.label = label_of(self.epsg)
        self.projected = is_projected(self.epsg)
        self._tf = None
        if self.epsg != 4326:
            src = osr.SpatialReference()
            src.ImportFromEPSG(4326)
            src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            dst = osr.SpatialReference()
            if dst.ImportFromEPSG(self.epsg) == 0:
                dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                self._tf = osr.CoordinateTransformation(src, dst)

    @staticmethod
    def _degrees(lon: float, lat: float) -> str:
        """经纬度按度显示，半球用 E/W、N/S 标，不写成 "-105°E"。

        六位小数约合 0.1 m，够用且不至于晃得眼花。
        """
        return "%.6f°%s  %.6f°%s" % (abs(lon), "W" if lon < 0 else "E",
                                     abs(lat), "S" if lat < 0 else "N")

    def format(self, lon: float, lat: float) -> str:
        if self._tf is None:
            return self._degrees(lon, lat)
        try:
            x, y, *_ = self._tf.TransformPoint(float(lon), float(lat))
        except Exception:
            return self._degrees(lon, lat)
        if self.projected:
            # 投影坐标是米，小数点后两位已经到厘米，再多没有意义
            return "%.2f E  %.2f N" % (x, y)
        return self._degrees(x, y)


def menu_entries(lon: Optional[float] = None,
                 lat: Optional[float] = None) -> List[Tuple[int, str, bool]]:
    """给坐标系菜单用的条目：(EPSG, 显示名, 是否是"按当前位置推荐的")。

    推荐项放最前面：UTM 有 120 个带，让人自己数当前在第几带是没必要的负担。
    """
    out: List[Tuple[int, str, bool]] = []
    if lon is not None and lat is not None:
        code = utm_epsg(lon, lat)
        out.append((code, utm_label(code), True))
    for code, name in BASE_CRS:
        out.append((code, name, False))
    return out
