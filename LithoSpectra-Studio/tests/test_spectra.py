# -*- coding: utf-8 -*-
"""光谱曲线：取谱、绘图、导出。

造一幅带波长表的高光谱 ENVI 影像跑全链路——取谱要经过 GDAL 的逆仿射与坐标
变换，用真影像跑比拿假数组断言可靠得多。曲线窗口的接口在 test_webapi.py。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from dataclasses import replace
from osgeo import gdal, osr
from PIL import Image

gdal.UseExceptions()

WORK = tempfile.mkdtemp(prefix="slspec_")
BANDS, WIDTH, HEIGHT = 120, 80, 60
WAVES = np.linspace(400.0, 2500.0, BANDS)
# 坏波段：定标增益为 0 的通道在产品里就是恒零的一段，取谱要把它读成 NaN
BAD_LO, BAD_HI = 1350.0, 1450.0


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    print("  ✓ %s" % what)


def make_scene(path: str) -> None:
    ds = gdal.GetDriverByName("ENVI").Create(path, WIDTH, HEIGHT, BANDS,
                                             gdal.GDT_Int16)
    ds.SetGeoTransform((100.0, 0.001, 0.0, 40.0, 0.0, -0.001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    rows, cols = np.mgrid[0:HEIGHT, 0:WIDTH]
    for i in range(BANDS):
        base = 2200 + 1200 * np.sin((WAVES[i] - 400) / 380.0)
        arr = base + 25 * cols + 15 * rows
        if BAD_LO <= WAVES[i] <= BAD_HI:
            arr = np.zeros_like(arr)
        ds.GetRasterBand(i + 1).WriteArray(arr.astype(np.int16))
    ds.FlushCache()
    ds = None
    hdr = os.path.splitext(path)[0] + ".hdr"
    with open(hdr, "a", encoding="utf-8") as fh:
        fh.write("wavelength units = Nanometers\n")
        fh.write("wavelength = {%s}\n" % ",".join("%.3f" % w for w in WAVES))


SCENE = os.path.join(WORK, "scene.dat")
make_scene(SCENE)

from samplelib.modules.spectra import spectra                # noqa: E402
from samplelib.annotate import layers as L                   # noqa: E402
from samplelib.modules.spectra import plot as P              # noqa: E402

print("[1] 取谱")
layer = L.RasterLayer(SCENE)
check(len(layer.wavelengths) == BANDS, "波长表从 ENVI 头读到了")
reader = spectra.SpectrumReader(layer)
check(reader.ready, "取谱器就绪：%s" % (reader.error or "无错误"))
check(reader.x_label() == "Wavelength (nm)", "横轴标题是英文波长")
check(reader.locate(100.0305, 39.9805) == (30, 19), "经纬度定位到像元 (30, 19)")
check(reader.locate(90.0, 39.0) is None, "影像外定位返回 None")

spec = reader.read(100.0305, 39.9805, 1, "P1")
check(spec is not None and spec.x.size == BANDS, "读到一条 %d 点的谱" % BANDS)
bad = (spec.x >= BAD_LO) & (spec.x <= BAD_HI)
check(np.all(np.isnan(spec.y[bad])), "恒零的坏波段读成 NaN，不是 0")
check(spec.valid_count == BANDS - int(bad.sum()), "有效波段数扣掉了坏波段")
check(reader.read(90.0, 39.0) is None, "影像外读谱返回 None")
wide = reader.read(100.0305, 39.9805, 5, "P1")
check(abs(float(np.nanmean(wide.y)) - float(np.nanmean(spec.y))) < 300,
      "5×5 窗口的均值与单像元接近")
check(reader.guess_unit(spec).key == "refl1e4", "整数千位量级猜成反射率 ÷10000")

print("[2] 绘图引擎")
check(P.WATER_GAPS is spectra.WATER_VAPOR_RANGES,
      "水汽波段只有一份定义，绘图这边是拿来用的")
base = P.PlotStyle(width_mm=90, height_mm=65, font_pt=7.0)
big = base.scaled(2.0)
check(abs(big.width_mm - 180) < 1e-9 and abs(big.font_pt - 14) < 1e-9,
      "放大预览时图幅与字号同乘一个系数，比例不变")
check(abs(P.rc_for(big)["axes.linewidth"]
          - P.rc_for(base)["axes.linewidth"] * 2) < 1e-9,
      "轴线粗细跟着一起放大，不然放大后线相对变细")
first = P.style_for(0)
check(P.style_for(10)[1] != first[1] and P.style_for(10)[0] == first[0],
      "颜色用完一轮换线型，灰度打印仍能区分")
check(P.style_for(0, "cb")[0] != P.style_for(0, "default")[0], "换得了配色")
check(P.style_for(0)[0].upper() == "#D62728", "第一条曲线默认是红的")
gapped = P.split_gaps(spec.x, spec.y, P.WATER_GAPS)
check(np.all(np.isnan(gapped[(spec.x > 1800) & (spec.x < 1950)])),
      "水汽波段挖成 NaN，曲线在那里断开")

fig = P.make_figure(base)
ax = P.draw_into(fig, [P.Series("A", spec.x, gapped)], "Wavelength (nm)",
                 "Reflectance", base)
check(ax is not None, "画得出坐标轴")
check(tuple(round(v, 4) for v in fig.get_size_inches())
      == (round(90 / 25.4, 4), round(65 / 25.4, 4)),
      "图幅就是 90 × 65 mm")
check(not ax.spines["top"].get_visible()
      and not ax.spines["right"].get_visible(),
      "默认去掉上、右两条边框（论文插图的样子）")
boxed = P.draw_into(P.make_figure(base), [P.Series("A", spec.x, gapped)],
                    "Wavelength (nm)", "Reflectance",
                    replace(base, box_frame=True))
check(boxed.spines["top"].get_visible(), "「坐标全框」勾上就补回四边")
check(P.draw_into(P.make_figure(base), [], "", "", base) is None,
      "一条曲线都没有时不画坐标轴")

print("[3] 导出")
unit = reader.guess_unit(spec)
second = reader.read(100.0505, 39.9705, 1, "P2")
specs = [spec, second]
series = [P.Series(sp.label, sp.x, P.split_gaps(sp.x, sp.y / unit.divisor, P.WATER_GAPS))
          for sp in specs]
for ext in (".png", ".pdf", ".svg", ".eps"):
    out = os.path.join(WORK, "fig" + ext)
    P.save_figure(out, series, "Wavelength (nm)", "Reflectance", base, dpi=200)
    check(os.path.getsize(out) > 2000,
          "导出 %s（%d 字节）" % (ext, os.path.getsize(out)))
svg = open(os.path.join(WORK, "fig.svg"), encoding="utf-8").read()
check("Wavelength (nm)" in svg,
      "SVG 里的文字是真文字不是轮廓（svg.fonttype=none），Illustrator 能改")

png = os.path.join(WORK, "size.png")
P.save_figure(png, series, "Wavelength (nm)", "Reflectance",
              P.PlotStyle(width_mm=90, height_mm=65), dpi=300)
with Image.open(png) as image:
    w, h = image.size
check(abs(w - 90 / 25.4 * 300) < 3 and abs(h - 65 / 25.4 * 300) < 3,
      "90 × 65 mm @300 dpi 得到 %d × %d 像素" % (w, h))

csv = os.path.join(WORK, "data.csv")
spectra.to_csv(csv, specs, unit, spectra.WATER_VAPOR_RANGES)
lines = open(csv, encoding="utf-8-sig").read().strip().split("\n")
check(lines[0].startswith("Wavelength (nm),"), "CSV 表头是英文波长 + 曲线名")
check(len(lines) == BANDS + 1, "CSV 行数 = 波段数 + 表头")
back = spectra.from_csv(csv)
check(len(back) == len(specs), "导出的 CSV 能原样读回来")
check(np.all(np.isnan(back[0].y[(back[0].x > 1800) & (back[0].x < 1950)])),
      "CSV 里的空格读回来是 NaN，不是 0")

import shutil                                                # noqa: E402
shutil.rmtree(WORK, ignore_errors=True)
print("\n光谱曲线测试通过")
