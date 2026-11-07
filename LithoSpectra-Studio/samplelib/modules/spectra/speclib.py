# -*- coding: utf-8 -*-
"""光谱库：读库、检索、取谱、存自己的谱。

库文件长什么样
--------------
一个 zip，扩展名 ``.slb``，里面四种东西：

* ``manifest.json``  版本、出处、分类树、蚀变组合定义、数据块索引
* ``index.json``     每条光谱一行：名字、分类路径、样品号、在哪个块的第几行
* ``grid/<gid>.f4``  一条波长轴（nm，float32，升序）
* ``block/<bid>.f2`` 一批光谱的值（float16，行 = 一条谱，列 = 对应网格）

为什么把值和索引分开放：检索时只需要 ``index.json``（1.6 MB，一次读完），真正
占地方的是那 13 MB 数据块，按需解压、用完缓存。开一次库不到 0.3 秒，而不是把
五千条谱全部读进内存。

为什么用 float16：反射率是 0~1 的数，float16 的相对精度 5e-4，比任何一台地面
光谱仪的重复性都高一个量级；换成 float32 文件大一倍，换成定点整数还得额外记
缩放系数、NaN 还得另找一个哨兵值。

两个库
------
* **随包那个**（``samplelib/modules/spectra/data/spectral_library.slb``）只读，由
  ``tools/build_speclib.py`` 从 ENVI 的原始库编译出来。
* **自己那个**（``app_data_dir()/my_spectra.slb``）可写。从影像上取到一条觉得
  有用的谱，存进去，下次照样能检索、能拿来当参照。格式和上面完全一样，因此
  同一套读取代码、同一个检索入口、同一棵树。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ... import config
from . import names_cn, spectra

# =============================================================================
# 配置区
# =============================================================================

#: 随包的标准库文件名
LIBRARY_FILE = "spectral_library.slb"

#: 可写的个人库文件名
USER_LIBRARY_FILE = "my_spectra.slb"

#: 个人库在分类树上的根，以及它的中文名
USER_ROOT = "mine"
USER_ROOT_CN = "我的光谱"

#: 数据块缓存的上限（浮点数个数）。一个块最大 439×4595 ≈ 200 万个数，按 float32
#: 算 8 MB；给到 2000 万，常用的十来个块都留得住，超了按最早用的先扔。
CACHE_LIMIT = 20_000_000

#: 检索结果的条数上限。再多列表也翻不动，而且没人会翻到第三千条。
SEARCH_LIMIT = 2000

#: 存储用的 dtype，与编译脚本一致
STORE_DTYPE = "<f2"

# =============================================================================


class LibraryError(ValueError):
    """库文件读不出来。调用方负责说人话。"""


@dataclass(frozen=True)
class Entry:
    """库里的一条光谱。**只有元数据，没有值**——值要用时再去块里取。"""

    id: str
    name: str
    raw: str
    cls: str
    source: str
    block: str
    row: int
    wl_lo: float
    wl_hi: float
    count: int
    sample: str = ""
    group: str = ""
    instrument: str = ""
    formula: str = ""
    state: str = ""
    note: str = ""
    #: 兼属的其他分类路径。一条谱可以同时挂在好几个地方——明矾石既是硫酸盐，
    #: 又该出现在"高级泥化"的矿物里；自己采的样品既想按矿物归类，又想按矿区归。
    #: 主分类 :attr:`cls` 决定列表里"分类"那一栏显示什么，检索时两者一视同仁。
    tags: Tuple[str, ...] = ()
    alt: Tuple[str, ...] = ()
    class_source: str = ""
    superseded: bool = False
    #: 哪个库里的。``std`` 随包那个，``user`` 自己那个。
    library: str = "std"
    #: 中文名。随包那份由 :mod:`names_cn` 按英文名查出来，认不出来的留空；
    #: 自己存的谱由使用者填。
    cn: str = ""
    #: 采样位置（经纬度）。影像上取的谱存进来时自动带上；随包的标样没有。
    lon: Optional[float] = None
    lat: Optional[float] = None
    #: 采集 / 入库日期（YYYY-MM-DD）
    date: str = ""
    #: 岩性或样品描述
    lithology: str = ""

    @property
    def title(self) -> str:
        """给人看的全名：中文名在前，英文名与样品号随后。"""
        return "%s  %s" % (self.cn, self.display) if self.cn and self.cn != self.name \
            else self.display

    @property
    def located(self) -> bool:
        return self.lon is not None and self.lat is not None

    @property
    def categories(self) -> Tuple[str, ...]:
        """这条谱挂在哪些分类下。主分类排第一。"""
        return (self.cls,) + tuple(t for t in self.tags if t != self.cls)

    @property
    def display(self) -> str:
        """列表里那一行写什么。名字后面跟样品号，同名的十来条才分得开。"""
        # 名字里已经带着样品号（导入的 USGS 谱常是「Illite IMt-1.a」）就不再重复
        if self.sample and self.sample not in self.name:
            return "%s  %s" % (self.name, self.sample)
        return self.name

    def detail(self) -> str:
        rows = [self.raw or self.name]
        if self.cn:
            rows.insert(0, self.cn)
        if self.formula:
            rows.append("化学式：%s" % self.formula)
        if self.group:
            rows.append("族／描述：%s" % self.group)
        if self.instrument:
            rows.append("仪器：%s" % self.instrument)
        if self.state:
            rows.append("粒度／状态：%s" % self.state)
        if self.lithology:
            rows.append("岩性／描述：%s" % self.lithology)
        if self.located:
            rows.append("采样位置：%.6f°E, %.6f°N" % (self.lon, self.lat))
        if self.date:
            rows.append("日期：%s" % self.date)
        rows.append("波长：%.1f ~ %.1f nm，%d 个有效点"
                    % (self.wl_lo, self.wl_hi, self.count))
        if self.note:
            rows.append(self.note)
        return "\n".join(rows)


@dataclass(frozen=True)
class Category:
    path: str
    cn: str
    en: str
    count: int

    @property
    def depth(self) -> int:
        return self.path.count("/")

    @property
    def label(self) -> str:
        return "%s（%d）" % (self.cn, self.count)


def normalise_path(path: str) -> str:
    """把使用者敲进来的分类路径收拾干净：去掉首尾斜杠与空白，压掉空段。"""
    parts = [p.strip() for p in str(path or "").replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p)


def _clean_paths(paths: Sequence[str], primary: str) -> List[str]:
    out: List[str] = []
    for raw in paths or ():
        path = normalise_path(raw)
        if path and path != primary and path not in out:
            out.append(path)
    return out


def category_name(path: str) -> str:
    """一条分类路径末段的中文名。

    先问随包那份库——自己新建的谱要是归到 ``mineral/silicate/phyllosilicate``
    下面，树上那一支就该照样写"层状硅酸盐"，而不是把英文段名原样摆出来。查不到
    的（自己起的新分类）就用段名本身。
    """
    if path == USER_ROOT:
        return USER_ROOT_CN
    global _CATEGORY_CN
    if _CATEGORY_CN is None:
        std = standard_library()
        # 检索时每条谱的每个分类都要问一遍，五千条 × 八十几个分类逐个比太慢
        _CATEGORY_CN = {c.path: c.cn for c in std.categories} if std else {}
    return _CATEGORY_CN.get(path) or path.rsplit("/", 1)[-1]


_CATEGORY_CN: Optional[Dict[str, str]] = None


def _float_or_none(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _as_tuple(value) -> Tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


class Library:
    """一个 ``.slb``。构造时只读索引，值按块懒加载。"""

    def __init__(self, path: str, key: str = "std") -> None:
        self.path = path
        self.key = key
        self.entries: Tuple[Entry, ...] = ()
        self.categories: Tuple[Category, ...] = ()
        self.manifest: Dict[str, object] = {}
        self._grids: Dict[str, np.ndarray] = {}
        self._blocks: Dict[str, np.ndarray] = {}
        self._order: List[str] = []
        self._cached = 0
        self._by_id: Dict[str, Entry] = {}
        self._load()

    # ---------------------------------------------------------------- 读
    def _load(self) -> None:
        if not os.path.isfile(self.path):
            raise LibraryError("找不到光谱库文件：%s" % self.path)
        try:
            with zipfile.ZipFile(self.path) as z:
                self.manifest = json.loads(z.read("manifest.json"))
                index = json.loads(z.read("index.json"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
            raise LibraryError("光谱库文件读不出来（%s）：%s" % (self.path, exc))
        if self.manifest.get("format") != "lithospectra-speclib":
            raise LibraryError("%s 不是本程序的光谱库文件。" % self.path)

        self.entries = tuple(self._entry(row) for row in index.get("entries", ()))
        self._by_id = {e.id: e for e in self.entries}
        self.categories = tuple(
            Category(str(c["path"]), str(c.get("cn") or c["path"]),
                     str(c.get("en") or ""), int(c.get("count", 0)))
            for c in self.manifest.get("categories", ()))

    def _entry(self, row: Dict[str, object]) -> Entry:
        wl = row.get("wl") or [0.0, 0.0]
        return Entry(
            id=str(row["id"]), name=str(row.get("name") or row["id"]),
            raw=str(row.get("raw") or ""), cls=str(row.get("cls") or "other"),
            source=str(row.get("src") or self.key), block=str(row.get("blk") or ""),
            row=int(row.get("row", 0)),
            wl_lo=float(wl[0]), wl_hi=float(wl[1]),
            count=int(row.get("n", 0)),
            sample=str(row.get("sample") or ""), group=str(row.get("group") or ""),
            instrument=str(row.get("instrument") or ""),
            formula=str(row.get("formula") or ""),
            state=str(row.get("state") or ""), note=str(row.get("note") or ""),
            tags=_as_tuple(row.get("tags")), alt=_as_tuple(row.get("alt")),
            class_source=str(row.get("csrc") or ""),
            superseded=bool(row.get("superseded")),
            library=self.key,
            cn=str(row.get("cn") or "") or names_cn.chinese_name(
                str(row.get("name") or ""), str(row.get("cls") or "")),
            lon=_float_or_none(row.get("lon")), lat=_float_or_none(row.get("lat")),
            date=str(row.get("date") or ""), lithology=str(row.get("lith") or ""))

    # ------------------------------------------------------------ 取数据
    def grid(self, gid: str) -> np.ndarray:
        if gid not in self._grids:
            with zipfile.ZipFile(self.path) as z:
                raw = z.read("grid/%s.f4" % gid)
            self._grids[gid] = np.frombuffer(raw, dtype="<f4").astype(np.float64)
        return self._grids[gid]

    def block(self, bid: str) -> Tuple[np.ndarray, np.ndarray]:
        """返回 (波长, 值矩阵)。矩阵是 float32，行 = 一条谱。"""
        if bid in self._blocks:
            self._order.remove(bid)
            self._order.append(bid)
        else:
            meta = self.manifest["blocks"][bid]
            with zipfile.ZipFile(self.path) as z:
                raw = z.read("block/%s.f2" % bid)
            wl = self.grid(str(meta["grid"]))
            mat = np.frombuffer(raw, dtype=STORE_DTYPE).astype(np.float32)
            mat = mat.reshape(int(meta["rows"]), wl.size)
            self._blocks[bid] = mat
            self._order.append(bid)
            self._cached += mat.size
            self._evict()
        meta = self.manifest["blocks"][bid]
        return (self.grid(str(meta["grid"])), self._blocks[bid])

    def _evict(self) -> None:
        while self._cached > CACHE_LIMIT and len(self._order) > 1:
            drop = self._order.pop(0)
            self._cached -= self._blocks.pop(drop).size

    def values(self, entry: Entry) -> Tuple[np.ndarray, np.ndarray]:
        wl, mat = self.block(entry.block)
        return (wl, mat[entry.row].astype(np.float64))

    def spectrum(self, entry: Entry, label: str = "") -> spectra.Spectrum:
        """做成一条可以直接画的曲线。"""
        wl, y = self.values(entry)
        return spectra.Spectrum(
            label=label or entry.display, x=wl.copy(), y=y,
            bands=np.arange(1, wl.size + 1, dtype=np.float64),
            x_is_wavelength=True, origin="library", native_unit="refl",
            layer_name=str(self.manifest.get("title") or self.key),
            note=entry.detail())

    def get(self, entry_id: str) -> Optional[Entry]:
        return self._by_id.get(entry_id)

    # -------------------------------------------------------------- 其他
    @property
    def title(self) -> str:
        return str(self.manifest.get("title") or os.path.basename(self.path))

    @property
    def sources(self) -> Dict[str, Dict[str, str]]:
        return dict(self.manifest.get("sources") or {})

    @property
    def groups(self) -> List[Dict[str, str]]:
        return list(self.manifest.get("groups") or [])


class UserLibrary(Library):
    """自己存的那些。可写，整份重写——个人库到不了需要增量写的规模。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.key = "user"
        self._grids = {}
        self._blocks = {}
        self._order = []
        self._cached = 0
        self._rows: List[Dict[str, object]] = []
        self._data: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
        self.manifest = self._blank_manifest()
        self.entries = ()
        self.categories = ()
        self._by_id = {}
        if os.path.isfile(path):
            try:
                self._load_user()
            except LibraryError:
                # 坏了就当空的重来，但把坏文件留在原地，别把人家的东西删了
                self.manifest = self._blank_manifest()
                self._rows, self._data = [], {}
        self._reindex()

    @staticmethod
    def _blank_manifest() -> Dict[str, object]:
        return {"format": "lithospectra-speclib", "version": 1,
                "title": USER_ROOT_CN, "count": 0, "blocks": {},
                "categories": [], "sources": {}, "groups": [],
                # 使用者自己建的分类。**空分类也要留在这儿**——分类是先建好、
                # 再往里放东西的，如果只从条目里反推，新建的空枝在树上根本
                # 不会出现，等于建不出来。
                "folders": []}

    def _load_user(self) -> None:
        try:
            with zipfile.ZipFile(self.path) as z:
                self.manifest = json.loads(z.read("manifest.json"))
                self._rows = json.loads(z.read("index.json")).get("entries", [])
                for row in self._rows:
                    bid = str(row["blk"])
                    gid = str(self.manifest["blocks"][bid]["grid"])
                    wl = np.frombuffer(z.read("grid/%s.f4" % gid),
                                       dtype="<f4").astype(np.float64)
                    mat = np.frombuffer(z.read("block/%s.f2" % bid),
                                        dtype=STORE_DTYPE).astype(np.float64)
                    self._data[bid] = (wl, mat.reshape(1, wl.size))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
            raise LibraryError("个人光谱库读不出来：%s" % exc)
        if self.manifest.get("format") != "lithospectra-speclib":
            raise LibraryError("%s 不是本程序的光谱库文件。" % self.path)

    @property
    def folders(self) -> List[str]:
        return [str(p) for p in (self.manifest.get("folders") or [])]

    def _reindex(self) -> None:
        self.manifest["folders"] = sorted(set(self.folders))
        self.entries = tuple(self._entry(row) for row in self._rows)
        self._by_id = {e.id: e for e in self.entries}
        counts: Dict[str, int] = {}
        for folder in self.folders:
            parts = folder.split("/")
            for i in range(1, len(parts) + 1):
                counts.setdefault("/".join(parts[:i]), 0)
        for e in self.entries:
            # 一条谱挂在几个分类下就往几棵枝上计数，但同一条不在同一枝上数两次
            seen = set()
            for path in e.categories:
                parts = path.split("/")
                for i in range(1, len(parts) + 1):
                    key = "/".join(parts[:i])
                    if key not in seen:
                        seen.add(key)
                        counts[key] = counts.get(key, 0) + 1
        self.categories = tuple(
            Category(path, category_name(path), "", n)
            for path, n in sorted(counts.items()))
        self.manifest["count"] = len(self.entries)

    def block(self, bid: str) -> Tuple[np.ndarray, np.ndarray]:
        wl, mat = self._data[bid]
        return (wl, mat)

    def grid(self, gid: str) -> np.ndarray:
        for bid, meta in self.manifest["blocks"].items():
            if str(meta["grid"]) == gid and bid in self._data:
                return self._data[bid][0]
        raise LibraryError("个人库里没有网格 %s。" % gid)

    # ------------------------------------------------------------ 增删存
    def add(self, spec: spectra.Spectrum, name: str, cls: str = "",
            tags: Sequence[str] = (), alt: Sequence[str] = (),
            note: str = "", cn: str = "", sample: str = "",
            lon: Optional[float] = None, lat: Optional[float] = None,
            date: str = "", lithology: str = "", source: str = "",
            save: bool = True, reindex: bool = True) -> Optional[Entry]:
        """把一条曲线存进个人库。

        ``cls`` 是主分类，**可以是任何路径**——库里现成的
        ``mineral/silicate/phyllosilicate``，也可以是自己新起的
        ``我的分类/海德乌拉``。留空才落到「我的光谱」。``tags`` 是兼属的其他分类，
        ``alt`` 是蚀变组合。三者一起决定这条谱在树上出现在哪几个地方。

        存的是**当前的 x / y 原值**，不含平滑、不含单位换算——那些是显示时的处理，
        存进库里就没法再退回去了。

        ``cn`` 中文名、``sample`` 样品号、``lon``/``lat`` 采样位置、``date`` 日期
        （留空取今天）、``lithology`` 岩性描述、``source`` 来源（留空取曲线所在的
        影像名）。``save=False`` 时只改内存，批量存时最后统一落一次盘；
        ``reindex=False`` 连索引也不重建（返回 None），几千条一起进来时由调用方
        最后 ``_reindex()`` 一次——每条都重建一遍是平方级的，导出整个标准库要一分钟。
        """
        if not spec.x_is_wavelength:
            raise LibraryError("这条曲线的横轴是波段序号，没有波长就进不了库。")
        good = np.isfinite(spec.y)
        if not good.any():
            raise LibraryError("这条曲线上没有有效值。")
        cls = normalise_path(cls) or USER_ROOT
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        bid = "user_%s_%d" % (stamp, len(self._rows))
        while bid in self._data:              # 同一秒里批量存：序号往后挪
            bid += "x"
        gid = "g%d_%s" % (spec.x.size, bid)
        wl = np.asarray(spec.x, dtype=np.float64)
        self._data[bid] = (wl, np.asarray(spec.y, dtype=np.float64)[None, :])
        self.manifest.setdefault("blocks", {})[bid] = {
            "grid": gid, "rows": 1, "source": "user", "file": bid}
        if lon is None and lat is None and spec.origin == "image" and (spec.lon or spec.lat):
            lon, lat = float(spec.lon), float(spec.lat)
        row = {
            "id": bid, "name": name.strip() or cn.strip() or spec.label,
            "raw": spec.label, "cn": cn.strip(),
            "cls": cls, "src": "user", "blk": bid, "row": 0,
            "wl": [round(float(wl[good][0]), 2), round(float(wl[good][-1]), 2)],
            "n": int(good.sum()),
            "note": note.strip(),
            "sample": sample.strip(),
            "date": date.strip() or datetime.date.today().isoformat(),
            "lith": lithology.strip(),
            "group": source.strip() or spec.layer_name,
            "tags": _clean_paths(tags, cls),
            "alt": [str(a) for a in alt if a],
        }
        if lon is not None and lat is not None:
            row["lon"], row["lat"] = round(float(lon), 7), round(float(lat), 7)
        self._rows.append(row)
        folders = self.manifest.setdefault("folders", [])
        for path in [cls] + list(row["tags"]):
            if path and path not in folders:
                folders.append(path)
        if not reindex:
            return None
        self.manifest["folders"] = sorted(folders)
        self._reindex()
        if save:
            self.save()
        return self._by_id[str(row["id"])]

    def copy_entry(self, entry: Entry, cls: str = "",
                   libraries: Optional[Sequence["Library"]] = None,
                   save: bool = True, reindex: bool = True) -> Optional[Entry]:
        """把别的库里的一条谱复制一份进来（拖到自己的分类上就是走这里）。

        名称、中文名、化学式、样品号、备注都带过来，出处记在来源里——自己整理的
        参照集里那条明矾石到底是哪家库的哪个样品，以后还查得到。
        """
        lib = library_of(entry, libraries)
        if lib is None:
            return None
        spec = lib.spectrum(entry)
        note = entry.note
        if entry.formula:
            note = ("化学式：%s\n%s" % (entry.formula, note)).strip()
        tags: Sequence[str] = ()
        if entry.library == "user":
            cls = cls or entry.cls
            tags = entry.tags
        return self.add(spec, entry.name, cls=cls, tags=tags, alt=entry.alt,
                        note=note, cn=entry.cn,
                        sample=entry.sample, lon=entry.lon, lat=entry.lat,
                        date=entry.date, lithology=entry.lithology,
                        source=(str((lib.sources.get(entry.source) or {}).get("cn")
                                    or lib.title)
                                if entry.library != "user" else entry.group),
                        save=save, reindex=reindex)

    def move(self, entry_ids: Sequence[str], cls: str) -> int:
        """把几条谱的主分类一起换成 ``cls``，兼属分类与蚀变组合不动。"""
        path = normalise_path(cls) or USER_ROOT
        wanted = set(entry_ids)
        moved = 0
        for row in self._rows:
            if str(row["id"]) in wanted:
                row["cls"] = path
                row["tags"] = _clean_paths(row.get("tags") or [], path)
                moved += 1
        if moved:
            known = set(self.folders) | {path}
            self.manifest["folders"] = sorted(p for p in known if p)
            self._reindex()
            self.save()
        return moved

    def merge(self, path: str, under: str = "") -> Tuple[int, int]:
        """把另一个 .slb（同事给的、自己另一台机器上的）并进来。

        分类、空分类、全部元数据原样保留；``under`` 非空时整份挂到这个分类下面（层级
        不变）。同一条谱（编号相同）已经在库里的跳过，同一个文件导两次不会出两份。
        返回 (新增, 跳过)。
        """
        other = UserLibrary(path)
        if not other.entries and not other.folders:
            raise LibraryError("「%s」里没有光谱。" % os.path.basename(path))
        under = normalise_path(under)
        if under and under != USER_ROOT:
            pre = lambda p: under if (not p or p == USER_ROOT) else under + "/" + p   # noqa: E731
            # 只挪主分类；兼属分类（多是随包库的矿物类型）原样保留
            for row in other._rows:
                row["cls"] = pre(str(row.get("cls") or ""))
            primaries = {str(r["cls"]) for r in other._rows}
            other.manifest["folders"] = [pre(f) if pre(f) in primaries or not any(
                str(t) == f for r in other._rows for t in (r.get("tags") or [])) else f
                for f in other.folders] + [under]
        added = skipped = 0
        have = {str(r["id"]) for r in self._rows}
        for row in other._rows:
            rid = str(row["id"])
            if rid in have:
                skipped += 1
                continue
            bid = str(row["blk"])
            wl, mat = other._data[bid]
            self._data[bid] = (wl, mat)
            self.manifest.setdefault("blocks", {})[bid] = dict(other.manifest["blocks"][bid])
            self._rows.append(dict(row))
            have.add(rid)
            added += 1
        self.manifest["folders"] = sorted(set(self.folders) | set(other.folders)
                                          | {str(r.get("cls") or USER_ROOT)
                                             for r in other._rows})
        self._reindex()
        self.save()
        return added, skipped

    # ------------------------------------------------------------ 偏好
    @property
    def last_folder(self) -> str:
        """上次存谱时放进的分类。野外一批样品往往都进同一个分类，不必每次再挑。"""
        return str((self.manifest.get("prefs") or {}).get("last_folder") or "")

    def remember_folder(self, path: str) -> None:
        self.manifest.setdefault("prefs", {})["last_folder"] = normalise_path(path)

    def remove(self, entry_id: str) -> bool:
        keep = [r for r in self._rows if str(r["id"]) != entry_id]
        if len(keep) == len(self._rows):
            return False
        self._rows = keep
        alive = {str(r["blk"]) for r in keep}
        self._data = {k: v for k, v in self._data.items() if k in alive}
        self.manifest["blocks"] = {k: v for k, v in self.manifest["blocks"].items()
                                   if k in alive}
        self._reindex()
        self.save()
        return True

    # ------------------------------------------------------------ 分类树
    def add_folder(self, path: str) -> str:
        """新建一个分类。返回收拾干净之后的路径，已经存在就原样返回。"""
        clean = normalise_path(path)
        if not clean:
            raise LibraryError("分类名不能为空。")
        folders = self.folders
        if clean not in folders:
            folders.append(clean)
            self.manifest["folders"] = sorted(folders)
            self._reindex()
            self.save()
        return clean

    def folder_usage(self, path: str) -> Dict[str, int]:
        """一个分类（含子分类）下有多少条谱以它为主分类、多少条只是兼属、有几个子分类。"""
        clean = normalise_path(path)
        inside = lambda p: p == clean or p.startswith(clean + "/")   # noqa: E731
        primary = sum(1 for r in self._rows if inside(str(r.get("cls") or "")))
        tagged = sum(1 for r in self._rows if not inside(str(r.get("cls") or ""))
                     and any(inside(str(t)) for t in (r.get("tags") or [])))
        subs = sum(1 for f in self.folders if f.startswith(clean + "/"))
        return {"primary": primary, "tagged": tagged, "subfolders": subs}

    def remove_folder(self, path: str, mode: str = "") -> bool:
        """删一个分类（连同子分类）。

        ``mode``：空串时只删空分类，底下还有谱就报错；``move`` 把以它为主分类的谱挪到
        上一级（顶层分类挪到「我的光谱」根下）；``delete`` 把这些谱一起删掉。兼属这个分类
        的谱只摘掉这个兼属，谱本身不动。
        """
        clean = normalise_path(path)
        if not clean or clean == USER_ROOT:
            raise LibraryError("「我的光谱」本身不能删除。")
        inside = lambda p: p == clean or p.startswith(clean + "/")   # noqa: E731
        usage = self.folder_usage(clean)
        if usage["primary"] and mode not in ("move", "delete"):
            raise LibraryError("「%s」下面还有光谱（%d 条），先把它们挪走或删掉。" % (clean, usage["primary"]))
        parent = clean.rsplit("/", 1)[0] if "/" in clean else USER_ROOT
        keep = []
        for row in self._rows:
            cls = str(row.get("cls") or USER_ROOT)
            row["tags"] = [t for t in (row.get("tags") or []) if not inside(str(t))]
            if inside(cls):
                if mode == "delete":
                    continue
                row["cls"] = parent
                row["tags"] = _clean_paths(row["tags"], parent)
            keep.append(row)
        changed = len(keep) != len(self._rows) or usage["primary"] or usage["tagged"]
        self._rows = keep
        alive = {str(r["blk"]) for r in keep}
        self._data = {k: v for k, v in self._data.items() if k in alive}
        self.manifest["blocks"] = {k: v for k, v in (self.manifest.get("blocks") or {}).items() if k in alive}
        folders = [f for f in self.folders if not inside(f)]
        if len(folders) == len(self.folders) and not changed:
            return False
        self.manifest["folders"] = folders
        self._reindex()
        self.save()
        return True

    def move_folder(self, path: str, parent: str) -> str:
        """把一个分类连同子分类、里面的谱整枝挪到 ``parent`` 下面（空串是「我的光谱」根下），
        层级原样保留。目标下已有同名分类时合并进去。返回新路径。"""
        clean = normalise_path(path)
        dest = normalise_path(parent)
        if dest == USER_ROOT:
            dest = ""
        if not clean:
            raise LibraryError("没有指定要挪的分类。")
        if dest == clean or dest.startswith(clean + "/"):
            raise LibraryError("不能把分类挪到它自己或它的子分类下面。")
        leaf = clean.rsplit("/", 1)[-1]
        target = (dest + "/" + leaf) if dest else leaf
        if target == clean:
            return clean

        def moved(old: str) -> str:
            if old == clean:
                return target
            if old.startswith(clean + "/"):
                return target + old[len(clean):]
            return old

        self.manifest["folders"] = sorted({moved(f) for f in self.folders} | {target}
                                          | ({dest} if dest else set()))
        for row in self._rows:
            row["cls"] = moved(str(row.get("cls") or USER_ROOT))
            row["tags"] = _clean_paths([moved(t) for t in (row.get("tags") or [])], row["cls"])
        self._reindex()
        self.save()
        return target

    def rename_folder(self, path: str, name: str) -> str:
        """给一个分类改名。底下的子分类与已经归进去的谱一起跟着走。"""
        clean = normalise_path(path)
        leaf = normalise_path(name)
        if "/" in leaf:
            raise LibraryError("新名字里不能再带斜杠，它只是这一层的名字。")
        if not leaf:
            raise LibraryError("分类名不能为空。")
        parent = clean.rsplit("/", 1)[0] if "/" in clean else ""
        target = (parent + "/" + leaf) if parent else leaf

        def moved(old: str) -> str:
            if old == clean:
                return target
            if old.startswith(clean + "/"):
                return target + old[len(clean):]
            return old

        self.manifest["folders"] = sorted({moved(f) for f in self.folders}
                                          | {target})
        for row in self._rows:
            row["cls"] = moved(str(row.get("cls") or USER_ROOT))
            row["tags"] = [moved(t) for t in (row.get("tags") or [])]
        self._reindex()
        self.save()
        return target

    def recategorise(self, entry_id: str, cls: str,
                     tags: Sequence[str] = (), alt: Sequence[str] = ()) -> bool:
        """改一条谱的分类。存进去之后发现归错了、或者想再挂一处，走这里。"""
        for row in self._rows:
            if str(row["id"]) == entry_id:
                path = normalise_path(cls) or USER_ROOT
                row["cls"] = path
                row["tags"] = _clean_paths(tags, path)
                row["alt"] = [str(a) for a in alt if a]
                self._reindex()
                self.save()
                return True
        return False

    def rename(self, entry_id: str, name: str) -> bool:
        for row in self._rows:
            if str(row["id"]) == entry_id:
                row["name"] = name.strip() or row["name"]
                self._reindex()
                self.save()
                return True
        return False

    def update(self, entry_id: str, name: str = "", note: Optional[str] = None,
               cls: Optional[str] = None, tags: Sequence[str] = (),
               alt: Sequence[str] = (), cn: Optional[str] = None,
               sample: Optional[str] = None, lithology: Optional[str] = None,
               date: Optional[str] = None,
               location: Optional[Tuple[Optional[float], Optional[float]]] = None
               ) -> bool:
        """一次改完名称、备注、分类，只落一次盘、只重建一次索引。

        备注是检索与详情都要用的字段，改了必须重建索引：直接改 ``_rows`` 再
        ``save()``，文件是新的、内存里的 ``entries`` 还是旧的，窗口重读拿到的是
        同一个缓存对象，详情里的备注要到重启才换。
        """
        for row in self._rows:
            if str(row["id"]) != entry_id:
                continue
            if name.strip():
                row["name"] = name.strip()
            if note is not None:
                row["note"] = note.strip()
            for key, value in (("cn", cn), ("sample", sample), ("lith", lithology),
                               ("date", date)):
                if value is not None:
                    row[key] = value.strip()
            if location is not None:
                lon, lat = location
                if lon is None or lat is None:
                    row.pop("lon", None)
                    row.pop("lat", None)
                else:
                    row["lon"], row["lat"] = round(float(lon), 7), round(float(lat), 7)
            if cls is not None:
                path = normalise_path(cls) or USER_ROOT
                row["cls"] = path
                row["tags"] = _clean_paths(tags, path)
                row["alt"] = [str(a) for a in alt if a]
            self._reindex()
            self.save()
            return True
        return False

    def save(self) -> None:
        """整份重写。先写临时文件再改名，中途断电也不会留下半个库。"""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.manifest["built"] = datetime.date.today().isoformat()
        tmp = self.path + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr("manifest.json",
                       json.dumps(self.manifest, ensure_ascii=False, indent=1))
            z.writestr("index.json",
                       json.dumps({"entries": self._rows}, ensure_ascii=False))
            for bid, (wl, mat) in self._data.items():
                gid = str(self.manifest["blocks"][bid]["grid"])
                z.writestr("grid/%s.f4" % gid, wl.astype("<f4").tobytes())
                z.writestr("block/%s.f2" % bid, mat.astype(STORE_DTYPE).tobytes())
        os.replace(tmp, self.path)


# -----------------------------------------------------------------------------
# 打开
# -----------------------------------------------------------------------------

def packaged_library_path() -> str:
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", LIBRARY_FILE)
    return config.resource_path(source, "spectra", LIBRARY_FILE)


def user_library_path() -> str:
    return os.path.join(config.app_data_dir(), USER_LIBRARY_FILE)


_STD: Optional[Library] = None
_USER: Optional[UserLibrary] = None


def standard_library() -> Optional[Library]:
    """随包那个。读不到返回 None——没有光谱库程序照样能用，只是少一块功能。"""
    global _STD
    if _STD is None:
        try:
            _STD = Library(packaged_library_path(), "std")
        except LibraryError:
            return None
    return _STD


def user_library() -> UserLibrary:
    global _USER
    if _USER is None:
        _USER = UserLibrary(user_library_path())
    return _USER


def all_libraries() -> List[Library]:
    std = standard_library()
    return ([std] if std else []) + [user_library()]


# -----------------------------------------------------------------------------
# 检索
# -----------------------------------------------------------------------------

@dataclass
class Query:
    """一次检索的条件。全部留空就是"整个库"。"""

    text: str = ""
    #: 分类路径前缀。``mineral/silicate`` 会把三级的层状、架状一起带出来。
    category: str = ""
    #: 蚀变组合键（``argillic`` 之类）
    group: str = ""
    #: 只要这几个来源。空 = 不限。
    sources: Tuple[str, ...] = ()
    #: 光谱必须覆盖到的波长区间（nm）。用来剔掉只有热红外、跟影像对不上的那些。
    covers: Optional[Tuple[float, float]] = None
    #: 只要哪个库的：``std`` / ``user``，空 = 都要
    library: str = ""
    #: 是否带上被新版取代的老库
    include_superseded: bool = False
    limit: int = SEARCH_LIMIT


def _tokens(text: str) -> List[str]:
    return [t for t in re.split(r"[\s,;]+", text.strip().lower()) if t]


def matches(entry: Entry, query: Query, words: Sequence[str]) -> bool:
    if query.category:
        want = query.category
        if not any(path == want or path.startswith(want + "/")
                   for path in entry.categories):
            return False
    if query.group and query.group not in entry.alt:
        return False
    if query.sources and entry.source not in query.sources:
        return False
    if query.library and entry.library != query.library:
        return False
    if entry.superseded and not query.include_superseded:
        return False
    if query.covers:
        lo, hi = query.covers
        # 要求真正盖住，不是沾一点边：只重叠 50 nm 的谱拿去匹配算不出东西来
        if entry.wl_lo > lo + 1.0 or entry.wl_hi < hi - 1.0:
            return False
    if words:
        hay = " ".join((entry.name, entry.cn, entry.raw, entry.sample, entry.group,
                        entry.formula, entry.note, entry.lithology)
                       + entry.categories
                       + tuple(category_name(p) for p in entry.categories)).lower()
        if not all(w in hay for w in words):
            return False
    return True


def _relevance(entry: Entry, words: Sequence[str]) -> int:
    """名字与搜索词贴得多近：0 正好是它，1 以它开头，2 只是包含。

    搜「绿泥石」，排第一的该是绿泥石本身，而不是按字母序排在前面的
    「黑云母-绿泥石」。
    """
    if not words:
        return 0
    names = (entry.name.lower(), entry.cn.lower())
    if any(w in names for w in words):
        return 0
    if any(n.startswith(w) for w in words for n in names if n):
        return 1
    return 2


def search(query: Query,
           libraries: Optional[Sequence[Library]] = None) -> List[Entry]:
    """按条件挑条目。有搜索词时名字贴得近的排前面，其余按名字、来源、样品号排。"""
    libs = libraries if libraries is not None else all_libraries()
    words = _tokens(query.text)
    out: List[Entry] = []

    def order(e: Entry):
        return (_relevance(e, words), e.name.lower(), e.source, e.sample)

    for lib in libs:
        for entry in lib.entries:
            if matches(entry, query, words):
                out.append(entry)
                if len(out) >= query.limit:
                    out.sort(key=order)
                    return out
    out.sort(key=order)
    return out


def category_tree(libraries: Optional[Sequence[Library]] = None) -> List[Category]:
    """所有库的分类节点合在一起，按路径排好，父节点一定排在子节点前面。"""
    libs = libraries if libraries is not None else all_libraries()
    merged: Dict[str, Category] = {}
    order: List[str] = []
    for lib in libs:
        for cat in lib.categories:
            if cat.path in merged:
                old = merged[cat.path]
                merged[cat.path] = Category(old.path, old.cn, old.en,
                                            old.count + cat.count)
            else:
                merged[cat.path] = cat
                order.append(cat.path)
    return [merged[p] for p in order]


def entry_by_id(entry_id: str,
                libraries: Optional[Sequence[Library]] = None) -> Optional[Entry]:
    for lib in (libraries if libraries is not None else all_libraries()):
        hit = lib.get(entry_id)
        if hit is not None:
            return hit
    return None


def library_of(entry: Entry,
               libraries: Optional[Sequence[Library]] = None) -> Optional[Library]:
    for lib in (libraries if libraries is not None else all_libraries()):
        if lib.key == entry.library:
            return lib
    return None


def spectrum_of(entry: Entry, label: str = "",
                libraries: Optional[Sequence[Library]] = None
                ) -> Optional[spectra.Spectrum]:
    lib = library_of(entry, libraries)
    return lib.spectrum(entry, label) if lib else None
