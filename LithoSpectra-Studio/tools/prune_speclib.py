# -*- coding: utf-8 -*-
"""把量纲不对的光谱从已经编好的 ``spectral_library.slb`` 里剔掉，就地重写。

为什么要有这个脚本
------------------
``build_speclib.py`` 现在会在编库时拦下"整份不是小数反射率"的文件（见那边的
``MAX_VALUE_P95``）。但库文件是**已经编好、随包发布**的产物，而原始的 ENVI
光谱库目录未必还在手边——重编不了，就得能在成品上做同一件事。

这个脚本干的就是这件事，判定规则与编库时**同一条**：一个数据块里 ``|y|`` 的
95 分位超过 ``MAX_P95``，整块剔掉。用 95 分位而不是最大值，是因为实测反射率在
信噪比差的波段边缘冒到 1 以上是常事（USGS 的植被谱在 2.7 µm 能到 2.0），那是
测量噪声；真正存成百分数的块 95 分位在 90 上下，差着两个数量级。

已知会被它剔掉的是 ASTER 那套里的 ``mineral_jhu_nicolet_2287_transmit``：
130 条 KBr 压片的**透过率百分数**（0~100，个别到 121），而 ENVI 头里的
``reflectance scale factor`` 老实写着 1，于是原样进了库。它有两重问题——
量纲是百分数，物理量根本不是反射率——画到反射率轴上是 20~110，一条就把整幅图
的纵轴撑坏；拿去做光谱匹配更没有意义。同一批矿物的**反射率**谱在隔壁那个
``mineral_jhu_nicolet_2287`` 里一条不缺（91 种里有 90 种同名、349 条），
剔掉它只丢一个变种名 alunite (ammonioalunite)，而 Alunite 本身还有三条。

直接运行即可（VS Code 里点运行也行），不需要命令行参数。
"""

import datetime
import io
import json
import os
import shutil
import sys
import zipfile
from typing import Dict, List

import numpy as np

# =============================================================================
# 配置
# =============================================================================

_HERE = os.path.dirname(os.path.abspath(__file__))
_STUDIO = os.path.dirname(_HERE)

#: 要处理的库文件。默认就是随包那一份。
LIB_PATH = os.path.join(_STUDIO, "samplelib", "modules", "spectra", "data",
                        "spectral_library.slb")

#: ``|y|`` 的 95 分位上限。与 build_speclib.MAX_VALUE_P95 保持一致。
MAX_P95 = 1.6

#: 额外要剔掉的块名（一般留空，靠上面那条自动判定就够）。
DROP_BLOCKS: tuple = ()

#: 只报告、不改文件。想先看看会剔掉什么就把它设成 True。
DRY_RUN = False

#: 改之前先存一份 ``<库文件>.bak``。库文件在版本库里，其实 git 就能退回去，
#: 但脚本自己留一份不花什么钱。
KEEP_BACKUP = True

# =============================================================================

sys.path.insert(0, _STUDIO)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _p95(mat: np.ndarray) -> float:
    finite = mat[np.isfinite(mat)]
    if finite.size == 0:
        return 0.0
    return float(np.percentile(np.abs(finite), 95))


def _category_table(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """重算分类树的条数。中文名与排序沿用编库脚本里那两张表。"""
    from build_speclib import CATEGORY_CN, ROOT_ORDER
    from collections import Counter

    count: Counter = Counter()
    for e in rows:
        parts = str(e["cls"]).split("/")
        for i in range(1, len(parts) + 1):
            count["/".join(parts[:i])] += 1

    def sort_key(path: str):
        head = path.split("/")[0]
        rank = ROOT_ORDER.index(head) if head in ROOT_ORDER else len(ROOT_ORDER)
        return (rank, path.count("/"), path)

    out = []
    for path in sorted(count, key=sort_key):
        leaf = path.split("/")[-1]
        out.append({"path": path, "cn": CATEGORY_CN.get(leaf, leaf),
                    "en": leaf.replace("_", " ").title(), "count": count[path]})
    return out


def prune() -> None:
    if not os.path.isfile(LIB_PATH):
        raise SystemExit("找不到库文件：%s" % LIB_PATH)
    sys.path.insert(0, _HERE)           # 为了 import build_speclib

    with zipfile.ZipFile(LIB_PATH) as z:
        manifest = json.loads(z.read("manifest.json"))
        rows = json.loads(z.read("index.json"))["entries"]
        blocks = dict(manifest.get("blocks") or {})
        grids = dict(manifest.get("grids") or {})
        payload = {n: z.read(n) for n in z.namelist()
                   if n.startswith(("block/", "grid/"))}

    dtype = manifest.get("dtype", "<f2")
    _log("库文件：%s" % LIB_PATH)
    _log("  %d 条光谱，%d 个数据块，%d 个波长网格"
         % (len(rows), len(blocks), len(grids)))

    doomed = []
    for bid, info in blocks.items():
        blob = payload.get("block/%s.f2" % bid)
        if blob is None:
            continue
        n = int(info["rows"])
        mat = np.frombuffer(blob, dtype=dtype).reshape(n, -1).astype(np.float64)
        level = _p95(mat)
        if level > MAX_P95 or bid in DROP_BLOCKS:
            doomed.append((bid, n, level, float(np.nanmax(np.abs(mat)))))

    if not doomed:
        _log("没有量纲不对的块，什么都不用改。")
        return

    _log("\n量纲不是小数反射率的块：")
    for bid, n, level, hi in doomed:
        _log("  %-46s %4d 条  |y| 95 分位 %.4g，最大 %.4g" % (bid, n, level, hi))

    drop_ids = {b for b, _, _, _ in doomed}
    keep = [r for r in rows if r["blk"] not in drop_ids]
    _log("\n剔除 %d 条，留下 %d 条。" % (len(rows) - len(keep), len(keep)))

    if DRY_RUN:
        _log("DRY_RUN=True，文件没动。")
        return

    for bid in drop_ids:
        blocks.pop(bid, None)
        payload.pop("block/%s.f2" % bid, None)
    # 没有块再用的波长网格一并清掉，否则库里会留下一份谁也引用不到的数据
    used = {str(info["grid"]) for info in blocks.values()}
    for gid in [g for g in grids if g not in used]:
        grids.pop(gid, None)
        payload.pop("grid/%s.f4" % gid, None)
        _log("  顺带清掉没人用的波长网格 %s" % gid)

    manifest["count"] = len(keep)
    manifest["blocks"] = blocks
    manifest["grids"] = grids
    manifest["categories"] = _category_table(keep)
    manifest["pruned"] = {
        "on": datetime.date.today().isoformat(),
        "rule": "|y| 的 95 分位 > %g 视为不是小数反射率" % MAX_P95,
        "blocks": [{"block": b, "rows": n, "p95": round(v, 4)}
                   for b, n, v, _ in doomed],
    }

    if KEEP_BACKUP:
        shutil.copy2(LIB_PATH, LIB_PATH + ".bak")
        _log("  原文件备份到 %s.bak" % os.path.basename(LIB_PATH))

    tmp = LIB_PATH + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("manifest.json",
                   json.dumps(manifest, ensure_ascii=False, indent=1))
        z.writestr("index.json",
                   json.dumps({"entries": keep}, ensure_ascii=False))
        for name in sorted(payload):
            z.writestr(name, payload[name])
    os.replace(tmp, LIB_PATH)
    _log("已重写：%.1f MB" % (os.path.getsize(LIB_PATH) / 1e6))

    # 立刻按使用者的读法验一遍：写坏了要当场知道，而不是等界面上画出空曲线
    from samplelib.modules.spectra import speclib
    lib = speclib.Library(LIB_PATH)
    worst = 0.0
    for e in lib.entries:
        _, y = lib.values(e)
        finite = y[np.isfinite(y)]
        if finite.size:
            worst = max(worst, float(np.percentile(np.abs(finite), 95)))
    _log("复核：读回 %d 条，全库 |y| 的 95 分位最大 %.4g（上限 %g）"
         % (len(lib.entries), worst, MAX_P95))
    if len(lib.entries) != len(keep) or worst > MAX_P95:
        raise SystemExit("复核不通过，请用 .bak 退回去。")


if __name__ == "__main__":
    prune()
