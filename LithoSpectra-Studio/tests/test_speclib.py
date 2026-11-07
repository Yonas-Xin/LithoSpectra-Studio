# -*- coding: utf-8 -*-
"""光谱库：库文件、检索、SG 平滑、光谱匹配、个人库、按类别取平均谱、中文名。

匹配这一节全部用**库里真实的谱**当查询样本，而不是造几条正弦波：正弦波上
任何算法都能拿满分，测不出"包络线去除有没有真的去掉基线"、"水汽段有没有真的
剔掉"这类事。做法是取一条标样，重采样到影像那种波段密度上、加一点噪声，再送
回去比——第一名必须还是它自己。
"""

import json
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zipfile

import warnings

import numpy as np
from collections import Counter

WORK = tempfile.mkdtemp(prefix="slslb_")


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    print("  ✓ %s" % what)


from samplelib.modules.spectra import speclib, specmatch, spectra          # noqa: E402
from samplelib.modules.spectra import plot as P                         # noqa: E402

# 个人光谱库指到临时目录：存入、拖放、导入都会写它，绝不能写到使用者真正的
# my_spectra.slb 里去
speclib.user_library_path = lambda: os.path.join(WORK, "my_spectra.slb")
speclib._USER = None

print("[1] 库文件本身")
LIB = speclib.standard_library()
check(LIB is not None, "随包的光谱库读得出来")
check(len(LIB.entries) > 4000, "条目 %d 条" % len(LIB.entries))
check(LIB.manifest["format"] == "lithospectra-speclib", "格式标记对得上")
check(LIB.manifest["wavelength_unit"] == "nm", "波长单位统一成 nm")

with zipfile.ZipFile(LIB.path) as z:
    names = set(z.namelist())
blocks = LIB.manifest["blocks"]
missing = [b for b in blocks if "block/%s.f2" % b not in names]
check(not missing, "每个数据块都在包里：缺 %s" % missing)
grids = {str(m["grid"]) for m in blocks.values()}
check(all("grid/%s.f4" % g in names for g in grids),
      "每个波长网格都在包里（%d 个）" % len(grids))
check(all(e.block in blocks for e in LIB.entries), "每条谱都指向一个存在的块")

paths = {c.path for c in LIB.categories}
check(all(e.cls in paths for e in LIB.entries), "每条谱的分类都在分类树上")
check(all(c.cn for c in LIB.categories), "每个分类节点都有中文名")
for cat in LIB.categories:
    if "/" in cat.path:
        check(cat.path.rsplit("/", 1)[0] in paths,
              "分类 %s 的父节点也在树上" % cat.path) if False else None
orphan = [c.path for c in LIB.categories
          if "/" in c.path and c.path.rsplit("/", 1)[0] not in paths]
check(not orphan, "没有父节点缺失的分类：%s" % orphan)

# 分类来源必须能交代：aster 来自源数据、curated 是脚本里手工补的、file 按材料大类
sources = {}
for e in LIB.entries:
    sources[e.class_source] = sources.get(e.class_source, 0) + 1
check(set(sources) <= {"aster", "curated", "file", "name", ""},
      "分类来源取值合法：%s" % sources)
unknown = sum(1 for e in LIB.entries if e.cls == "mineral/unclassified")
minerals = sum(1 for e in LIB.entries if e.cls.startswith("mineral"))
check(unknown < minerals * 0.02,
      "未分类的矿物是极少数：%d / %d" % (unknown, minerals))

print("[2] 取一条谱")
alunite = [e for e in LIB.entries
           if e.name == "Alunite" and e.wl_lo < 400 and e.wl_hi > 2400]
check(alunite, "库里有覆盖可见—短波红外的明矾石")
wl, y = LIB.values(alunite[0])
check(wl.size == y.size and wl.size > 100, "波长与值等长（%d 点）" % wl.size)
check(np.all(np.diff(wl) > 0), "波长严格递增")
good = y[np.isfinite(y)]
check(0.0 <= good.min() and good.max() < 2.0,
      "值已归一成 0~1 的反射率：%.3f ~ %.3f" % (good.min(), good.max()))
spec = LIB.spectrum(alunite[0])
check(spec.origin == "library", "做出来的曲线标着出处 library")
check(spec.x_is_wavelength and spec.note, "带波长、带出处说明")

print("[3] 检索")
q = speclib.Query(text="kaolinite", category="mineral")
hits = speclib.search(q, [LIB])
check(hits and all("kaolinite" in e.name.lower() or "kaolinite" in e.raw.lower()
                   for e in hits), "按名字搜到 %d 条高岭石" % len(hits))
phyllo = speclib.search(speclib.Query(category="mineral/silicate"), [LIB])
sub = speclib.search(speclib.Query(category="mineral/silicate/phyllosilicate"), [LIB])
check(len(phyllo) > len(sub) > 0, "上级分类把下级一起带出来：%d > %d"
      % (len(phyllo), len(sub)))
check(all(e.cls.startswith("mineral/silicate") for e in phyllo),
      "分类过滤没有漏进别的类")
two = speclib.search(speclib.Query(text="alunite na"), [LIB])
one = speclib.search(speclib.Query(text="alunite"), [LIB])
check(len(two) < len(one), "多个词是「都要满足」：%d < %d" % (len(two), len(one)))
grp = speclib.search(speclib.Query(group="argillic"), [LIB])
check(grp and all("argillic" in e.alt for e in grp),
      "按蚀变组合筛到 %d 条泥化矿物" % len(grp))
cov = speclib.search(speclib.Query(covers=(500.0, 2400.0), category="mineral"), [LIB])
check(cov and all(e.wl_lo <= 501 and e.wl_hi >= 2399 for e in cov),
      "波长覆盖过滤留下 %d 条真能盖住的" % len(cov))
check(len(cov) < len(speclib.search(speclib.Query(category="mineral"), [LIB])),
      "覆盖过滤确实滤掉了只测热红外的那些")

print("[4] Savitzky-Golay 平滑")
rng = np.random.RandomState(7)
clean = np.sin(np.linspace(0, 8, 300))
noisy = clean + rng.normal(0, 0.05, 300)
smooth = spectra.savgol(noisy, 9, 2)
check(np.sqrt(np.mean((smooth - clean) ** 2))
      < 0.6 * np.sqrt(np.mean((noisy - clean) ** 2)), "噪声压掉四成以上")
line = np.arange(60.0) * 3.0 - 7.0
check(np.allclose(spectra.savgol(line, 11, 2), line),
      "直线原样返回（端点也不塌）")
peak = np.exp(-((np.arange(201.0) - 100) ** 2) / 50.0)
sm = spectra.savgol(peak, 11, 2)
check(int(np.argmax(sm)) == 100 and sm.max() > 0.98,
      "峰位不动、峰高保住 %.3f" % sm.max())
gap = noisy.copy()
gap[120:140] = np.nan
out = spectra.savgol(gap, 9, 2)
check(np.array_equal(np.isnan(out), np.isnan(gap)), "NaN 断口原样留着")
short = np.array([1.0, 5.0, 2.0])
check(np.array_equal(spectra.savgol(short, 9, 2), short),
      "比窗口还短的段不动它")
check(np.allclose(spectra.savgol(noisy, 8, 2), spectra.savgol(noisy, 9, 2)),
      "偶数窗口自动补成奇数")

print("[5] 光谱匹配")
# 造一条"影像上取到的"明矾石：重采样到 166 个波段、加噪、放大一万倍当整数存
ref = alunite[0]
wl, y = LIB.values(ref)
gx = np.linspace(400.0, 2500.0, 166)
gy = specmatch.resample(wl, y, gx)[0] + np.random.RandomState(3).normal(0, 0.004, 166)
cand = speclib.search(speclib.Query(category="mineral", covers=(500.0, 2400.0)), [LIB])
check(len(cand) > 800, "候选 %d 条" % len(cand))

for algo in specmatch.ALGORITHMS:
    opts = specmatch.MatchOptions(algorithm=algo.key, continuum=True)
    ranked = specmatch.match(gx, gy, cand, [LIB], opts)
    check(ranked[0].entry.name == "Alunite",
          "%s 的第一名是明矾石（得分 %s）" % (algo.cn, ranked[0].score_text()))
    check(all(0.0 <= h.score <= 1.0 for h in ranked), "%s 的得分都在 0~1" % algo.cn)
    check(ranked[0].score >= ranked[-1].score, "%s 的排序是从高到低" % algo.cn)

base = specmatch.match(gx, gy, cand, [LIB], specmatch.MatchOptions())
scaled = specmatch.match(gx, gy * 10000.0, cand, [LIB], specmatch.MatchOptions())
check([h.entry.id for h in base[:20]] == [h.entry.id for h in scaled[:20]],
      "放大一万倍名次不变（SAM 与整体亮度无关）")

# 包络线去除：结果必须落在 0~1，且吸收谷处明显小于 1
cr = specmatch.continuum_removed(gx, gy[None, :])[0]
finite = cr[np.isfinite(cr)]
check(finite.max() <= 1.0 + 1e-6, "去包络后没有大于 1 的点（凸包在曲线之上）")
check(finite.min() < 0.9, "吸收谷确实被压下去了：最低 %.3f" % finite.min())

# 水汽段：剔与不剔必须真的算的是不同的东西
prepared = specmatch.prepare_query(gx, gy, specmatch.MatchOptions(mask_water=True))
kept = specmatch.prepare_query(gx, gy, specmatch.MatchOptions(mask_water=False))
check(prepared[0].size < kept[0].size, "剔水汽段后参与比较的点少了 %d 个"
      % (kept[0].size - prepared[0].size))
for lo, hi in spectra.WATER_VAPOR_RANGES:
    check(not np.any((prepared[0] >= lo) & (prepared[0] <= hi)),
          "%.0f~%.0f nm 一个点都不剩" % (lo, hi))

# 重叠太少的谱要被挡掉，不能靠几个点冲上榜首。库里那批热红外谱从 2079 nm 起，
# 跟 400~2500 nm 的影像只在末尾搭上两成，正好用来验这道闸门。
tir = [e for e in LIB.entries if e.wl_lo > 2000.0][:300]
check(tir, "库里有从热红外才开始的谱：%d 条" % len(tir))
check(all(e.wl_lo > gx[-1] * 0.8 for e in tir), "它们与影像的重叠不足两成")
check(not specmatch.match(gx, gy, tir, [LIB], specmatch.MatchOptions()),
      "重叠不够的谱一条都不进榜")

narrow = specmatch.match(gx, gy, cand, [LIB],
                         specmatch.MatchOptions(span=(2100.0, 2300.0)))
check(narrow and narrow[0].bands < base[0].bands,
      "限定波段范围后参与比较的点变少：%d < %d"
      % (narrow[0].bands, base[0].bands))

# 范围与分辨率对不上的几种情况，用同一条函数造出来：理论上全都该是 0°
FINE = np.arange(350.0, 2501.0)
COARSE = np.arange(400.0, 2501.0, 10.0)


def shape(x):
    return (0.4 + 0.1 * np.sin(x / 150.0) - 0.1 * np.exp(-0.5 * ((x - 900.0) / 40.0) ** 2)
            - 0.15 * np.exp(-0.5 * ((x - 2203.0) / 4.25) ** 2))


def sensor(x_out, fwhm):
    """按高斯响应把 1 nm 的谱"测"成 fwhm 宽的波段。"""
    s = fwhm / 2.355
    w = np.exp(-0.5 * ((x_out[:, None] - FINE[None, :]) / s) ** 2)
    return (w * shape(FINE)).sum(1) / w.sum(1)


cases = speclib.UserLibrary(os.path.join(WORK, "cases.slb"))
for label, x, y in (
        ("全段 1nm", FINE, shape(FINE)),
        ("400-1800", FINE[(FINE >= 400) & (FINE <= 1800)], shape(FINE[(FINE >= 400) & (FINE <= 1800)])),
        ("从 410 起", FINE[FINE >= 410], shape(FINE[FINE >= 410])),
        ("从 450 起", FINE[FINE >= 450], shape(FINE[FINE >= 450])),
        ("中间缺 200nm", FINE, np.where((FINE > 1000) & (FINE < 1200), np.nan, shape(FINE))),
        ("10nm 传感器", COARSE, sensor(COARSE, 10.0))):
    cases.add(spectra.Spectrum(label, x, y, origin="csv"), label, save=False)
image = sensor(COARSE, 10.0)


def degrees(report):
    return {h.entry.name: math.degrees(h.metric) for h in report.hits}


full = specmatch.match_report(COARSE, image, cases.entries, [cases], specmatch.MatchOptions())
got = degrees(full)
check(set(got) == {"全段 1nm", "从 410 起", "10nm 传感器"},
      "默认只留完整覆盖的：%s" % "、".join(sorted(got)))
check(full.skipped_cover == 3, "没完整覆盖的 3 条报出来：%d" % full.skipped_cover)
check(full.tolerance == specmatch.EDGE_TOLERANCE, "端点容差 %.0f nm" % full.tolerance)
loose = specmatch.match_report(COARSE, image, cases.entries, [cases],
                               specmatch.MatchOptions(full_cover=False))
got = degrees(loose)
check(len(got) == 6 and loose.skipped_cover == 0, "关掉后部分重叠的也比")
check(max(got.values()) < 0.02,
      "库谱窄于查询谱时包络线在重叠段上求，同一条谱都接近 0°（最大 %.3f°）" % max(got.values()))
narrow_hit = [h for h in loose.hits if h.entry.name == "400-1800"][0]
check(narrow_hit.hi < 1800.0 and narrow_hit.coverage < 0.75,
      "命中带着实际比较的范围与覆盖率：%.0f–%.0f nm，%.0f%%"
      % (narrow_hit.lo, narrow_hit.hi, 100 * narrow_hit.coverage))
wide = degrees(specmatch.match_report(COARSE, image, cases.entries, [cases],
                                      specmatch.MatchOptions(edge_tolerance=50.0)))
check("从 450 起" in wide, "容差放到 50 nm，从 450 起的那条也算完整覆盖")
capped = specmatch.match_report(COARSE, image, cases.entries, [cases],
                                specmatch.MatchOptions(edge_tolerance=500.0))
check(capped.tolerance == specmatch.EDGE_TOLERANCE_MAX,
      "容差最多放到 %.0f nm" % specmatch.EDGE_TOLERANCE_MAX)

# 分辨率：影像 10 nm、库 1 nm。只在波段中心取点的话窄峰比实测深，要先卷积
fine_hit = got["全段 1nm"]
gx, gy = specmatch.prepare_query(COARSE, image, specmatch.MatchOptions())
point = specmatch.continuum_removed(gx, specmatch.resample(FINE, shape(FINE), gx))
m, L, Q = specmatch._masked(point, specmatch.continuum_removed(gx, gy)[0])
naive = math.degrees(specmatch._sam(m, L, Q, None)[0])
check(fine_hit < 0.02 < naive,
      "1nm 库谱先按影像分辨率卷积：%.3f°（只取中心点是 %.3f°）" % (fine_hit, naive))
# 反过来：查询谱 1 nm、库谱 10 nm，查询谱平滑到库那一档
dense = degrees(specmatch.match_report(FINE, shape(FINE), cases.entries, [cases],
                                       specmatch.MatchOptions(span=(400.0, 2500.0))))
check(dense["10nm 传感器"] < 0.3, "1nm 查询谱对 10nm 库谱：%.3f°" % dense["10nm 传感器"])

print("[6] 个人光谱库")
mine = speclib.UserLibrary(os.path.join(WORK, "my.slb"))
check(len(mine.entries) == 0, "新建的个人库是空的")
entry = mine.add(spec, "野外 P1 明矾石", note="海德乌拉，样品 HD-07")
check(len(mine.entries) == 1 and entry.library == "user", "存进去一条")
check(entry.cls.startswith(speclib.USER_ROOT), "落在「我的光谱」下面：%s" % entry.cls)
again = speclib.UserLibrary(mine.path)
check(len(again.entries) == 1, "重新打开还在")
back = again.spectrum(again.entries[0])
check(np.allclose(back.x, spec.x) and
      np.allclose(back.y, spec.y, equal_nan=True, atol=1e-3),
      "存进去再读回来是同一条谱")
found = speclib.search(speclib.Query(text="海德乌拉"), [again])
check(len(found) == 1, "备注也能搜到")
check(again.rename(entry.id, "改个名"), "改得了名")
check(again.remove(entry.id) and not again.entries, "删得掉")

with open(os.path.join(WORK, "broken.slb"), "wb") as fh:
    fh.write(b"not a zip at all")
broken = speclib.UserLibrary(os.path.join(WORK, "broken.slb"))
check(len(broken.entries) == 0, "坏文件不炸，当空库处理")

print("[7] 自定义分类与多重分类")
custom = speclib.UserLibrary(os.path.join(WORK, "custom.slb"))
kept = custom.add(LIB.spectrum(alunite[0]), "野外明矾石",
                  cls="mineral/sulfate",
                  tags=["我的分类/海德乌拉", "mineral/silicate/phyllosilicate"],
                  alt=["advanced_argillic", "argillic"], note="HD-07")
check(kept.cls == "mineral/sulfate", "主分类可以是库里现成的路径，不再强塞进「我的光谱」")
check(len(kept.categories) == 3, "一条谱同时挂在 %d 个分类下" % len(kept.categories))
for path, label in (("mineral/sulfate", "硫酸盐"),
                    ("mineral/silicate", "硅酸盐（上级）"),
                    ("我的分类", "自己新起的分类")):
    found = speclib.search(speclib.Query(category=path), [custom])
    check(len(found) == 1, "在「%s」下面找得到它" % label)
check(len(speclib.search(speclib.Query(group="argillic"), [custom])) == 1,
      "蚀变组合也能自己指定")
names = {c.path: c.cn for c in custom.categories}
check(names.get("mineral/silicate/phyllosilicate") == "层状硅酸盐",
      "自己的谱挂到标准分类下时，树上照样写中文名：%s" % names)
check(names.get("我的分类/海德乌拉") == "海德乌拉", "自起的分类用段名本身当显示名")
check(custom.recategorise(kept.id, "我的分类/矿区B", tags=[], alt=["iron_oxide"]),
      "分类改得动")
again2 = speclib.UserLibrary(custom.path)
check(again2.entries[0].cls == "我的分类/矿区B" and not again2.entries[0].tags
      and again2.entries[0].alt == ("iron_oxide",), "改完存得住、读得回")

print("[8] 在分类树上新建、改名、删除分类")
tree_lib = speclib.UserLibrary(os.path.join(WORK, "folders.slb"))
made = tree_lib.add_folder("海德乌拉工区")
check(made == "海德乌拉工区", "建了一个顶层分类")
check(any(c.path == made for c in tree_lib.categories),
      "空分类也出现在树上——分类是先建好再往里放东西的")
tree_lib.add_folder("海德乌拉工区/蚀变带 A")
tree_lib.add_folder("mineral/silicate/我的补充")
paths = {c.path for c in tree_lib.categories}
check("海德乌拉工区/蚀变带 A" in paths, "建得了子分类")
check("mineral/silicate/我的补充" in paths, "也能挂在随包库的分类底下")
moved = tree_lib.rename_folder("海德乌拉工区", "海德乌拉")
check(moved == "海德乌拉", "改名改的是这一层，不是整条路径")
paths = {c.path for c in tree_lib.categories}
check("海德乌拉/蚀变带 A" in paths and "海德乌拉工区" not in paths,
      "子分类跟着一起走：%s" % sorted(p for p in paths if "海德" in p))
tree_lib.add(LIB.spectrum(alunite[0]), "样品一", cls="海德乌拉/蚀变带 A")
try:
    tree_lib.remove_folder("海德乌拉")
    check(False, "底下还有谱时不该删得掉")
except speclib.LibraryError as exc:
    check("还有光谱" in str(exc), "底下还有谱时拦住并说明原因：%s" % exc)
check(tree_lib.remove_folder("mineral/silicate/我的补充"), "空分类删得掉")
again3 = speclib.UserLibrary(tree_lib.path)
check("海德乌拉/蚀变带 A" in {c.path for c in again3.categories},
      "分类树跟着库文件一起存下来")
check(again3.entries[0].cls == "海德乌拉/蚀变带 A", "改名之后条目的分类也跟着改了")

print("[9] 按类别取平均谱：样本库 / 分类栅格 / 矢量属性")
from osgeo import gdal, ogr, osr                          # noqa: E402
from samplelib.annotate import layers as L                # noqa: E402
from samplelib.modules.spectra import groupspec                           # noqa: E402

# 造一幅"三种矿物拼起来"的影像，真值已知，算出来的平均谱必须对得上
GB, GX, GY = 100, 180, 140
GWAVE = np.linspace(400.0, 2500.0, GB)
GT = (100.0, 0.001, 0.0, 40.0, 0.0, -0.001)
truth = {}
for value, name in ((1, "Kaolinite"), (2, "Alunite"), (3, "Muscovite")):
    hit = [e for e in LIB.entries
           if e.name == name and e.wl_lo < 400 and e.wl_hi > 2400][0]
    wl, y = LIB.values(hit)
    truth[value] = specmatch.resample(wl, y, GWAVE)[0]
label_grid = np.zeros((GY, GX), np.int16)
label_grid[:, :GX // 3] = 1
label_grid[:, GX // 3:2 * GX // 3] = 2
label_grid[:, 2 * GX // 3:] = 3

scene2 = os.path.join(WORK, "groups.dat")
gds = gdal.GetDriverByName("ENVI").Create(scene2, GX, GY, GB, gdal.GDT_Int16)
gds.SetGeoTransform(GT)
gsrs = osr.SpatialReference()
gsrs.ImportFromEPSG(4326)
gds.SetProjection(gsrs.ExportToWkt())
noise = np.random.RandomState(5).normal(0, 0.01, (GY, GX))
for i in range(GB):
    plane = np.zeros((GY, GX))
    for value, curve in truth.items():
        plane[label_grid == value] = curve[i]
    gds.GetRasterBand(i + 1).WriteArray(((plane + noise) * 10000).astype(np.int16))
gds.FlushCache()
gds = None
with open(os.path.splitext(scene2)[0] + ".hdr", "a", encoding="utf-8") as fh:
    fh.write("wavelength units = Nanometers\n")
    fh.write("wavelength = {%s}\n" % ",".join("%.3f" % v for v in GWAVE))
greader = spectra.SpectrumReader(L.RasterLayer(scene2))
check(greader.ready, "造好的影像读得出来")


def worst(spec, value):
    return float(np.nanmax(np.abs(spec.y / 10000.0 - truth[value])))


# 分类栅格
class_path = os.path.join(WORK, "labels.tif")
cds = gdal.GetDriverByName("GTiff").Create(class_path, GX, GY, 1, gdal.GDT_Int16)
cds.SetGeoTransform(GT)
cds.SetProjection(gsrs.ExportToWkt())
cds.GetRasterBand(1).SetNoDataValue(0)
cds.GetRasterBand(1).WriteArray(label_grid)
cds.FlushCache()
cds = None
rsource = groupspec.ClassRasterSource(L.RasterLayer(class_path))
rgroups = [g for g in rsource.groups(greader) if not g.background]
check([g.key for g in rgroups] == ["1", "2", "3"],
      "分类栅格读出三类（背景那一类另算）：%s" % [g.name for g in rgroups])
check(all(g.name.startswith("Class") for g in rgroups),
      "自动生成的类别名是英文的——它要原样写到图例上：%s"
      % [g.name for g in rgroups])
for r in groupspec.mean_spectra(rsource, rgroups, greader):
    check(r.spectrum is not None, "%s 算出来了" % r.group.name)
    check(worst(r.spectrum, int(r.group.key)) < 0.005,
          "%s 的平均谱与真值最大差 %.4f" % (r.group.name,
                                          worst(r.spectrum, int(r.group.key))))
    check("个像元的平均谱" in r.spectrum.note and r.spectrum.origin == "region",
          "备注写清了是多少个像元平均出来的：%s" % r.spectrum.note)

# 换个坐标系的分类栅格，走重投影那条路
utm_path = os.path.join(WORK, "labels_utm.tif")
gdal.Warp(utm_path, class_path, dstSRS="EPSG:32650", resampleAlg="near",
          dstNodata=0)
usource = groupspec.ClassRasterSource(L.RasterLayer(utm_path))
# 背景那一类（重投影时补出来的 0）不参与比对，它不对应任何一种矿物
ugroups = [g for g in usource.groups(greader) if not g.background]
ures = groupspec.mean_spectra(usource, ugroups, greader)
check(ures and all(r.spectrum is not None for r in ures),
      "换了坐标系的分类栅格照样能算")
check(max(worst(r.spectrum, int(r.group.key)) for r in ures) < 0.01,
      "重投影之后与真值仍然对得上：最大差 %.4f"
      % max(worst(r.spectrum, int(r.group.key)) for r in ures))

# 矢量属性
vec_path = os.path.join(WORK, "endmembers.shp")
vdrv = ogr.GetDriverByName("ESRI Shapefile")
vds = vdrv.CreateDataSource(vec_path)
vlyr = vds.CreateLayer("em", gsrs, ogr.wkbPolygon)
vlyr.CreateField(ogr.FieldDefn("endmember", ogr.OFTString))
for tag, lo, hi in (("EM_A", 0.02, 0.30), ("EM_B", 0.36, 0.63),
                    ("EM_C", 0.70, 0.98)):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for fx, fy in ((lo, 0.1), (hi, 0.1), (hi, 0.9), (lo, 0.9), (lo, 0.1)):
        ring.AddPoint_2D(GT[0] + fx * GX * GT[1], GT[3] + fy * GY * GT[5])
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    feat = ogr.Feature(vlyr.GetLayerDefn())
    feat.SetGeometry(poly)
    feat.SetField("endmember", tag)
    vlyr.CreateFeature(feat)
vds = None
vsource = groupspec.VectorFieldSource(L.VectorLayer(vec_path), "endmember")
vgroups = vsource.groups()
check([g.key for g in vgroups] == ["EM_A", "EM_B", "EM_C"],
      "按属性分出三组：%s" % [g.key for g in vgroups])
pairs = {"EM_A": 1, "EM_B": 2, "EM_C": 3}
for r in groupspec.mean_spectra(vsource, vgroups, greader):
    check(r.spectrum is not None and worst(r.spectrum, pairs[r.group.key]) < 0.005,
          "%s 的平均谱对得上真值" % r.group.key)

# 完全不重叠的图层要说出原因，而不是静静地少一条曲线
away = os.path.join(WORK, "away.tif")
ads = gdal.GetDriverByName("GTiff").Create(away, 40, 40, 1, gdal.GDT_Int16)
ads.SetGeoTransform((0.0, 0.001, 0.0, 0.0, 0.0, -0.001))
ads.SetProjection(gsrs.ExportToWkt())
ads.GetRasterBand(1).SetNoDataValue(0)
ads.GetRasterBand(1).WriteArray(np.ones((40, 40), np.int16))
ads.FlushCache()
ads = None
asource = groupspec.ClassRasterSource(L.RasterLayer(away))
ares = groupspec.mean_spectra(asource, asource.groups(), greader)
check(ares and all(r.spectrum is None and "没有像元" in r.problem for r in ares),
      "跟影像不重叠时说明原因：%s" % [r.problem for r in ares])

# 掩膜为空、掩膜越界都要稳稳地返回 None
check(greader.read_mask(np.zeros((4, 4), bool), (0, 0)) is None,
      "空掩膜返回 None")
check(greader.read_mask(np.ones((4, 4), bool), (GX - 2, 0)) is None,
      "越出影像的掩膜返回 None")
# 大片掩膜：所有像元都参与，不抽样；块的进度能报出来、也能中断
tiny = np.zeros((GY, GX), bool)
tiny[:, :GX // 3] = True
ticks = []
whole = greader.read_mask(tiny, (0, 0),
                          progress=lambda done, total: ticks.append((done, total)) is None)
check(whole is not None and worst(whole, 1) < 0.005,
      "整片区域的平均谱是精确的（没有抽样）：%.4f" % worst(whole, 1))
check(str(GY * GX // 3) in whole.note,
      "备注里写的是全部参与的像元数：%s" % whole.note)
check(ticks and ticks[-1][0] == ticks[-1][1],
      "分块读的进度一路报到底：%s" % (str(ticks[-1]) if ticks else "无"))
check(greader.read_mask(tiny, (0, 0), progress=lambda d, t: d < 1) is None,
      "进度回调说停就停，返回 None")

# 样本库来源：拿真的 SampleClass / SampleRecord 走一遍点与面两条几何路径
from samplelib.model import (GEOM_POINT, GEOM_POLYGON, SampleClass,  # noqa: E402
                             SampleRecord)


class FakeSession:
    """SampleSource 只用到这两个属性，真的 Session 也就暴露这两个。"""

    def __init__(self, classes, by_class):
        self.classes = classes
        self.records_by_class = by_class


def lonlat(fx, fy):
    return (GT[0] + fx * GX * GT[1], GT[3] + fy * GY * GT[5])


sample_classes = [SampleClass(class_id=11, code="1", name="高岭石带",
                              color="#D62728", geom_kind=GEOM_POINT),
                  SampleClass(class_id=22, code="2", name="明矾石带",
                              color="#1F77B4", geom_kind=GEOM_POLYGON)]
points = [SampleRecord(key=("a", i), class_id=11, geom_kind=GEOM_POINT,
                       coords=[lonlat(0.15, 0.12 + 0.025 * i)])
          for i in range(30)]
ring = [lonlat(fx, fy) for fx, fy in
        ((0.38, 0.15), (0.61, 0.15), (0.61, 0.85), (0.38, 0.85), (0.38, 0.15))]
patch = [SampleRecord(key=("b", 0), class_id=22, geom_kind=GEOM_POLYGON,
                      coords=[ring])]
ssource = groupspec.SampleSource(
    FakeSession(sample_classes, {11: points, 22: patch}))
sgroups = ssource.groups()
# 树的顶层是工区，与样本树面板一致：工区 -> 类别（可多级）-> 样本
roots = [g for g in sgroups if g.key.startswith(groupspec.REGION_PREFIX)]
check(len(roots) == 1 and roots[0].count == 0,
      "顶层是工区，它自己不算一条曲线：%s" % [(g.name, g.count) for g in roots])
check(all(g.parent == roots[0].key for g in sgroups
          if not g.key.startswith(groupspec.REGION_PREFIX)),
      "类别都挂在工区下面")
sgroups = [g for g in sgroups if not g.key.startswith(groupspec.REGION_PREFIX)]
check([g.count for g in sgroups] == [30, 1],
      "样本库来源按类别数出样本数：%s" % [(g.name, g.count) for g in sgroups])
sres = groupspec.mean_spectra(ssource, sgroups, greader)
check(all(r.spectrum is not None for r in sres), "点样本与面样本都算得出平均谱")
check(worst(sres[0].spectrum, 1) < 0.02,
      "点样本（30 个像元）的平均谱对得上真值：%.4f" % worst(sres[0].spectrum, 1))
check(worst(sres[1].spectrum, 2) < 0.005,
      "面样本的平均谱对得上真值：%.4f" % worst(sres[1].spectrum, 2))
check(sres[0].spectrum.color == "#D62728",
      "沿用类别自带的颜色，图上的曲线与画布上的样本对得上")

# 只列落在影像里的：造一个影像外的类别，它不该出现
outside = SampleClass(class_id=33, code="9", name="影像外的类",
                      geom_kind=GEOM_POINT)
far_away = [SampleRecord(key=("z", 0), class_id=33, geom_kind=GEOM_POINT,
                         coords=[(0.0, 0.0)], bbox=(0.0, 0.0, 0.0, 0.0))]
tree_session = FakeSession(
    sample_classes + [SampleClass(class_id=99, name="蚀变岩", geom_kind=GEOM_POINT),
                      outside],
    {11: points, 22: patch, 33: far_away})
# 挂成两级：两个带都归到「蚀变岩」下面
for cls in tree_session.classes:
    if cls.class_id in (11, 22):
        cls.parent_id = 99
tsource = groupspec.SampleSource(tree_session)
inside = tsource.groups(greader)
check(all(g.name != "[9] 影像外的类" for g in inside),
      "影像范围外的类别不列出来：%s" % [g.name for g in inside])
check(any(g.name == "影像外的类" or g.key == "33"
          for g in tsource.groups()), "不传 reader 时它还在（说明是范围过滤掉的）")
parents = {g.key: g.parent for g in inside}
check(parents.get("11") == "99" and parents.get("22") == "99",
      "类别的层级带出来了：%s" % parents)
check(any(g.key == "99" and g.count == 0 for g in inside),
      "本身没有样本、但下面挂着有样本子类的那一层也留着，树才不会断在中间")
check(parents.get("99", "").startswith(groupspec.REGION_PREFIX),
      "顶层类别挂在工区节点下面：%s" % parents.get("99"))

# 工区分得开：同名类别分属两个工区时，树上是两支
two = [SampleClass(class_id=71, name="灰岩", region="青海都兰",
                   geom_kind=GEOM_POINT),
       SampleClass(class_id=72, name="灰岩", region="新疆哈密",
                   geom_kind=GEOM_POINT)]
two_rows = {71: [SampleRecord(key=("p", i), class_id=71, geom_kind=GEOM_POINT,
                              coords=[lonlat(0.2, 0.3)],
                              bbox=lonlat(0.2, 0.3) + lonlat(0.2, 0.3))
                 for i in range(3)],
            72: [SampleRecord(key=("q", i), class_id=72, geom_kind=GEOM_POINT,
                              coords=[lonlat(0.8, 0.3)],
                              bbox=lonlat(0.8, 0.3) + lonlat(0.8, 0.3))
                 for i in range(2)]}
regional = groupspec.SampleSource(FakeSession(two, two_rows)).groups(greader)
names = [g.name for g in regional if g.key.startswith(groupspec.REGION_PREFIX)]
check(sorted(names) == ["新疆哈密", "青海都兰"],
      "两个工区各成一支：%s" % names)

# 分类栅格没声明 nodata 时，所有取值都列出来，谁是背景由使用者指
plain = os.path.join(WORK, "binary.tif")
bds = gdal.GetDriverByName("GTiff").Create(plain, GX, GY, 1, gdal.GDT_Byte)
bds.SetGeoTransform(GT)
bds.SetProjection(gsrs.ExportToWkt())
flags = np.zeros((GY, GX), np.uint8)
flags[20:60, 20:60] = 1
bds.GetRasterBand(1).WriteArray(flags)
bds.FlushCache()
bds = None
bsource = groupspec.ClassRasterSource(L.RasterLayer(plain))
bgroups = bsource.groups(greader)
check([g.key for g in bgroups] == ["0", "1"],
      "没声明 nodata 时 0 值照样列出来：%s" % [g.key for g in bgroups])
check(not any(g.background for g in bgroups),
      "没声明 nodata 就不替他猜哪个是背景")
bsource.background.add("0")
bgroups = bsource.groups(greader)
check([g.background for g in bgroups] == [True, False],
      "标了之后 0 值成了背景")
back = groupspec.mean_spectra(bsource, [bgroups[0]], greader)
check(back[0].spectrum is not None,
      "标成背景只是默认不勾，真要算它的平均谱照样算得出来")
# utm_path 是重投影出来的，四角补的 0 就是它声明的 nodata
declared = groupspec.ClassRasterSource(L.RasterLayer(utm_path))
check(any(g.background and g.key == "0" for g in declared.groups(greader)),
      "文件里声明了 nodata 的，自动标上背景：%s"
      % [(g.key, g.background) for g in declared.groups(greader)])

# 掩膜按行分块读，而不是逐波段读整幅——BIP 交错的影像上这是 100 倍的差别
spot = np.zeros((GY, GX), bool)
spot[10:14, 10:14] = True
seen = []
greader.read_mask(spot, (0, 0), progress=lambda d, t: seen.append(t) is None)
check(seen and seen[0] == 1,
      "一小块图斑只读一块，不是读 %d 个波段：报了 %s 块"
      % (greader.band_count, seen[0] if seen else "?"))
seen = []
greader.read_mask(np.ones((GY, GX), bool), (0, 0),
                  progress=lambda d, t: seen.append(t) is None)
check(seen and seen[0] < greader.band_count,
      "整幅也是按行分块，块数 %d 远小于波段数 %d" % (seen[0], greader.band_count))

print("[10] 打包与出处")
manifest_path = os.path.join(os.path.dirname(speclib.__file__), "module.json")
data = json.load(open(manifest_path, encoding="utf-8"))["data"]
check(any(d.endswith(speclib.LIBRARY_FILE) for d in data),
      "模块清单的随包数据里带上了光谱库文件（打包按它收）")
for key, meta in LIB.sources.items():
    check(meta.get("citation"), "%s 有引用格式" % key)

print("[11] 库里每一条都得是小数反射率")
# manifest 自己写着 value=reflectance，那就是这份库对使用者的承诺。破过一次：
# ASTER 的 mineral_jhu_nicolet_2287_transmit 装的是 KBr 压片的透过率百分数
# （0~100，个别到 121），而 ENVI 头里的 reflectance scale factor 老实写着 1，
# 于是原样进了库。画到反射率轴上是 20~110，一条就把整幅图的纵轴撑坏；拿去做
# 光谱匹配更没有意义——透过率与反射率是两种物理量。
check(LIB.manifest.get("value") == "reflectance",
      "manifest 里写明整份库的物理量是反射率")

worst_p95 = 0.0
worst_id = ""
for e in LIB.entries:
    _, y = LIB.values(e)
    finite = y[np.isfinite(y)]
    if finite.size == 0:
        continue
    level = float(np.percentile(np.abs(finite), 95))
    if level > worst_p95:
        worst_p95, worst_id = level, e.id
# 用 95 分位而不是最大值：实测反射率在信噪比差的波段边缘冒到 1 以上是常事
# （USGS 的植被谱在 2.7 µm 能到 2.0），那是测量噪声，不是量纲错；真正存成
# 百分数的块 95 分位在 90 上下，差着两个数量级，一眼分得开。
check(worst_p95 <= 1.6,
      "全库 |y| 的 95 分位最大 %.4g（%s），没有按百分数存的" % (worst_p95, worst_id))
check(not any(e.id.startswith("aster/mineral_jhu_nicolet_2287_transmit")
              for e in LIB.entries),
      "那一批透过率谱已经不在库里")
# 剔掉它没有丢矿物：同一批的反射率谱在隔壁文件里
names = {e.name.lower() for e in LIB.entries}
for mineral in ("calcite", "alunite", "acmite", "albite", "almandine"):
    check(mineral in names, "「%s」的反射率谱还在" % mineral)

# 剔过什么要留得下痕迹，不能悄悄少一批
pruned = LIB.manifest.get("pruned")
check(pruned and pruned.get("blocks"),
      "manifest 里记着剔掉过哪些块、按的什么规则：%s"
      % (pruned or {}).get("rule"))
check(LIB.manifest["count"] == len(LIB.entries),
      "manifest 里的条数与实际对得上：%d" % LIB.manifest["count"])
cat_counts = {c["path"]: c["count"] for c in LIB.manifest["categories"]}
real = Counter()
for e in LIB.entries:
    parts = e.cls.split("/")
    for i in range(1, len(parts) + 1):
        real["/".join(parts[:i])] += 1
check(all(cat_counts[k] == real.get(k, 0) for k in cat_counts),
      "分类树上的条数也跟着重算过了")

print("[12] 中文名")
from samplelib.modules.spectra import names_cn as NC                       # noqa: E402
for en, cls, cn in (("Alunite KAl", "mineral/sulfate", "明矾石"),
                    ("Chlorite", "mineral/silicate", "绿泥石"),
                    ("Hematite alpha", "mineral/oxide", "赤铁矿"),
                    ("Albite (Feldspar)", "mineral/silicate", "钠长石"),
                    ("AtacamiteCu", "mineral/halide", "氯铜矿"),
                    ("Ammonio-Jarosite", "mineral/sulfate", "铵黄钾铁矾"),
                    ("Montmorillonite-Na", "soil", "钠蒙脱石"),
                    ("Alun.33+Kaol.33+Musc.33", "soil", "明矾石+高岭石+白云母"),
                    ("Biotite Granite", "rock/igneous", "黑云母花岗岩"),
                    ("Argillaceous Limestone", "rock/sedimentary", "泥质灰岩"),
                    ("Hornblende Gneiss", "rock/metamorphic", "角闪片麻岩"),
                    ("Diorite Porphyry", "rock/igneous", "闪长玢岩"),
                    ("Sandstone (Micaceous Red)", "rock/sedimentary", "红色云母质砂岩")):
    got = NC.chinese_name(en, cls)
    check(got == cn, "%s → %s" % (en, got))
check(NC.chinese_name("Big Sagebrush", "vegetation") == "三齿蒿" and
      NC.chinese_name("Pinyon Pine (Grey Bark)", "vegetation") == "矮松（灰色树皮）" and
      NC.chinese_name("10084.1939", "soil/lunar") == "月壤样品 10084",
      "植被、月壤等按补充表翻译，括号里的部位一并译出")
check(NC.chinese_name("Unobtainium Schist", "rock") == "",
      "岩石名里有一个词不认得就整个不给，宁缺毋错")
share_all = sum(1 for e in LIB.entries if e.cn) / float(len(LIB.entries))
check(share_all > 0.99, "全部条目 %.1f%% 有中文名" % (share_all * 100))
minerals = [e for e in LIB.entries if e.cls.startswith("mineral")]
share = sum(1 for e in minerals if e.cn) / float(len(minerals))
check(share > 0.95, "矿物条目 %.1f%% 有中文名" % (share * 100))
hits = speclib.search(speclib.Query(text="赤铁矿"), [LIB])
check(hits and hits[0].cn == "赤铁矿" and "Hematite" in hits[0].name,
      "搜中文名：「赤铁矿」第一条就是 Hematite")
hits = speclib.search(speclib.Query(text="绿泥石"), [LIB])
check(hits[0].cn == "绿泥石", "名字正好对上的排最前（不是按字母序排前的「黑云母-绿泥石」）")
check(len(speclib.search(speclib.Query(text="层状硅酸盐"), [LIB])) > 100,
      "分类的中文名也能搜")
check(NC.english_for("明矾石") == "Alunite", "中文名反查英文名")

print("[13] ASD 光谱仪的二进制 .asd")
import struct                                                              # noqa: E402
from samplelib.modules.spectra import speclib_io                           # noqa: E402


def _asd(path, target, ref=None, version=b"as7", data_type=1):
    head = bytearray(484)
    head[:3] = version
    head[186] = data_type
    struct.pack_into("<ff", head, 191, 350.0, 1.0)
    head[199] = 2                                     # float64
    struct.pack_into("<H", head, 204, len(target))
    body = struct.pack("<%dd" % len(target), *target)
    if ref is not None:
        body += b"\xff\xff" + b"\0" * 16 + struct.pack("<H", 0) + struct.pack("<%dd" % len(ref), *ref)
    with open(path, "wb") as fh:
        fh.write(bytes(head) + body + b"\0" * 40)


n_ch = 2151
target = np.linspace(100.0, 800.0, n_ch)
white = np.full(n_ch, 2000.0)
p7 = os.path.join(WORK, "a-00001.asd")
_asd(p7, target, white)
sp = speclib_io.read_asd(p7)
check(sp.x[0] == 350.0 and sp.x[-1] == 2500.0 and sp.x.size == n_ch, "波长从文件头算出：350～2500 nm，2151 个通道")
check(np.allclose(sp.y, target / white), "v7 反射率 = 目标 DN ÷ 附带的白板 DN")
check(sp.label == "a-00001", "曲线名取文件名")
p6 = os.path.join(WORK, "old.asd")
_asd(p6, list(np.linspace(0.1, 0.5, n_ch)), version=b"as6")
check(np.allclose(speclib_io.read_asd(p6).y, np.linspace(0.1, 0.5, n_ch)), "老版本没附白板，数据本身就是反射率")
raw = os.path.join(WORK, "raw.asd")
_asd(raw, target, white, data_type=0)
loaded = speclib_io.read_any(raw)
check(not loaded.spectra and "不是反射率" in loaded.notes[0], "原始 DN 文件不收，并说明原因")
loaded = speclib_io.read_any(p7)
check(len(loaded.spectra) == 1 and loaded.spectra[0].native_unit == "refl", "read_any 认得 .asd")

print("全部通过")
