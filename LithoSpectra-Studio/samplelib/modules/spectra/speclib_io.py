# -*- coding: utf-8 -*-
"""光谱库的导入与导出：文本表格、ENVI 光谱库、本程序的 .slb。

建自己的光谱库，谱多半不是一条条从影像上点出来的，而是一批批从别处来的：
野外光谱仪（ASD、SVC、PSR）导出的文本表、ASD 光谱仪直接存下的二进制 .asd、
同事给的 ENVI 光谱库、另一台电脑上存的 .slb。这里把它们统一读成 :class:`spectra.Spectrum`，交给"存入光谱库"那个
对话框——分类、名称、样品信息还是在那里一次填好。

读进来时做两件换算，并把做了什么写进 :class:`Loaded.notes` 告诉使用者：

* **波长单位**：最大值不到 30 的当成微米，乘 1000 换成纳米；
* **反射率量纲**：95 分位超过 1.6 的不是小数反射率。90 上下的当百分数（除以
  100），几千上万的当放大一万倍的整数（除以 10000）。规则与编库时那道闸门一样
  （``build_speclib.MAX_VALUE_P95``）——库里每一条都得是 0~1 的小数，这是匹配
  能算对的前提。
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

from . import speclib, spectra

#: 超过它就不是小数反射率（与编库脚本一致）
MAX_REFLECTANCE_P95 = 1.6

#: 横轴最大值小于它就当成微米
MICRON_LIMIT = 30.0

TABLE_FILTER = "文本表格 (*.csv *.txt *.tsv *.asc *.dat)"
ENVI_FILTER = "ENVI 光谱库 (*.sli *.hdr)"
ASD_FILTER = "ASD 光谱仪原始文件 (*.asd)"
SLB_FILTER = "LithoSpectra 光谱库 (*.slb)"
IMPORT_FILTER = ";;".join((
    "所有支持的格式 (*.csv *.txt *.tsv *.asc *.dat *.asd *.sli *.hdr *.slb)",
    TABLE_FILTER, ASD_FILTER, ENVI_FILTER, SLB_FILTER))


@dataclass
class Loaded:
    """读一个文件的结果。"""

    spectra: List[spectra.Spectrum] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# =============================================================================
# 换算
# =============================================================================

def normalise(specs: Sequence[spectra.Spectrum], notes: List[str]) -> None:
    """就地把横轴换成纳米、纵轴换成小数反射率，做了什么记进 ``notes``。"""
    xs = [s.x for s in specs if s.x.size]
    if xs and max(float(np.nanmax(x)) for x in xs) < MICRON_LIMIT:
        for s in specs:
            s.x = s.x * 1000.0
        notes.append("横轴按微米读进来，已换成纳米。")
    values = np.concatenate([np.abs(s.y[np.isfinite(s.y)]) for s in specs]
                            or [np.zeros(0)])
    if values.size:
        p95 = float(np.percentile(values, 95))
        if p95 > MAX_REFLECTANCE_P95:
            factor = 100.0 if p95 <= 160.0 else 10000.0
            for s in specs:
                s.y = s.y / factor
            notes.append("数值在 %.4g 上下，不是 0~1 的小数反射率，已按%s换算（除以 %g）。"
                         % (p95, "百分数" if factor == 100 else "放大一万倍的整数",
                            factor))
    for s in specs:
        s.native_unit = "refl"


# =============================================================================
# 文本表格
# =============================================================================

_NUMBER = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")


def _split(line: str, sep: Optional[str]) -> List[str]:
    if sep == ",":
        return spectra._split_csv(line)
    if sep is None:
        return line.split()
    return [c.strip().strip('"') for c in line.split(sep)]


def _sniff(lines: Sequence[str]) -> Optional[str]:
    """猜分隔符：逗号、制表符、分号，都不是就按空白切。"""
    sample = lines[-min(len(lines), 20):]
    for sep in (",", "\t", ";"):
        counts = {line.count(sep) for line in sample}
        if len(counts) == 1 and counts.pop() > 0:
            return sep
    return None


def read_table(path: str) -> Loaded:
    """文本表格：第一列波长，其后每列一条谱。

    认得的写法很宽：逗号、制表符、分号、空格分隔都行；有没有表头都行（没有就
    用文件名加序号起名）；表头之前的说明行（ASD 导出的那几行仪器信息）自动跳过；
    波长降序的排成升序。
    """
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        lines = [ln.rstrip("\r\n") for ln in fh if ln.strip()]
    if not lines:
        raise ValueError("文件是空的。")
    sep = _sniff(lines)
    rows = [_split(ln, sep) for ln in lines]

    def numeric(row) -> bool:
        return len(row) >= 2 and all(_NUMBER.match(c or "") for c in row[:2])

    first = next((i for i, row in enumerate(rows) if numeric(row)), None)
    if first is None:
        raise ValueError("找不到数字表格：第一列应是波长，其后每列一条谱。")
    header = rows[first - 1] if first > 0 and len(rows[first - 1]) >= 2 else []
    data = [row for row in rows[first:] if numeric(row)]
    width = max(len(row) for row in data)
    if width < 2:
        raise ValueError("表格只有一列，看不出哪列是谱。")

    x = np.array([float(row[0]) for row in data], dtype=np.float64)
    cols = np.full((len(data), width - 1), np.nan)
    for i, row in enumerate(data):
        for j, cell in enumerate(row[1:width]):
            if _NUMBER.match(cell or ""):
                cols[i, j] = float(cell)
    order = np.argsort(x, kind="stable")
    x, cols = x[order], cols[order]

    stem = os.path.splitext(os.path.basename(path))[0]
    out = []
    for j in range(cols.shape[1]):
        y = cols[:, j]
        if not np.isfinite(y).any():
            continue
        name = (header[j + 1] if j + 1 < len(header) and header[j + 1] else
                (stem if cols.shape[1] == 1 else "%s_%d" % (stem, j + 1)))
        out.append(spectra.Spectrum(
            label=name, x=x.copy(), y=y.copy(),
            bands=np.arange(1, x.size + 1, dtype=np.float64),
            x_is_wavelength=True, origin="csv",
            layer_name=os.path.basename(path)))
    if not out:
        raise ValueError("表格里没有有效的谱。")
    loaded = Loaded(out)
    normalise(out, loaded.notes)
    return loaded


# =============================================================================
# ENVI 光谱库
# =============================================================================

_ENVI_DTYPES = {1: "u1", 2: "i2", 3: "i4", 4: "f4", 5: "f8", 12: "u2",
                13: "u4", 14: "i8", 15: "u8"}


def _envi_header(path: str) -> dict:
    # 国内软件写的头文件常是 GBK：先按 UTF-8 严格解，不行再按 GB18030，中文谱名才不乱码
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")
    if not text.lstrip().upper().startswith("ENVI"):
        raise ValueError("「%s」不是 ENVI 头文件。" % os.path.basename(path))
    out = {}
    for m in re.finditer(r"^\s*([^=\n]+?)\s*=\s*(\{.*?\}|[^\n]*)", text,
                         re.MULTILINE | re.DOTALL):
        key = m.group(1).strip().lower()
        value = m.group(2).strip()
        if value.startswith("{"):
            value = value[1:-1]
        out[key] = value
    return out


def _envi_list(value: str) -> List[str]:
    return [v.strip() for v in value.replace("\n", " ").split(",") if v.strip()]


def read_envi_sli(path: str) -> Loaded:
    """ENVI 光谱库：``.sli`` 二进制 + ``.hdr`` 文本头。给哪一个都行。"""
    base, ext = os.path.splitext(path)
    hdr = path if ext.lower() == ".hdr" else base + ".hdr"
    if not os.path.isfile(hdr):
        raise ValueError("找不到配套的头文件 %s。" % os.path.basename(hdr))
    meta = _envi_header(hdr)
    data = path if ext.lower() != ".hdr" else None
    if data is None:
        for cand in (base + ".sli", base + ".SLI", base):
            if os.path.isfile(cand):
                data = cand
                break
    if data is None or not os.path.isfile(data):
        raise ValueError("找不到 %s 对应的数据文件（.sli）。" % os.path.basename(hdr))

    samples = int(meta.get("samples", "0"))
    lines = int(meta.get("lines", "0"))
    dtype = _ENVI_DTYPES.get(int(meta.get("data type", "4")))
    if not samples or not lines or dtype is None:
        raise ValueError("头文件里的 samples / lines / data type 不全。")
    order = ">" if meta.get("byte order", "0").strip() == "1" else "<"
    offset = int(meta.get("header offset", "0") or 0)
    raw = np.fromfile(data, dtype=order + dtype, offset=offset,
                      count=samples * lines)
    if raw.size < samples * lines:
        raise ValueError("数据文件比头文件说的短，可能拷坏了。")
    mat = raw.reshape(lines, samples).astype(np.float64)

    waves = _envi_list(meta.get("wavelength", ""))
    if len(waves) == samples:
        x = np.array([float(w) for w in waves], dtype=np.float64)
    else:
        raise ValueError("头文件里没有与波段数一致的 wavelength 列表，进不了库。")
    unit = meta.get("wavelength units", "").lower()
    notes: List[str] = []
    if unit.startswith("micro") or unit in ("um", "µm"):
        x = x * 1000.0
        notes.append("头文件写的是微米，已换成纳米。")
    scale = float(meta.get("reflectance scale factor", "1") or 1) or 1.0
    if scale not in (0.0, 1.0):
        mat = mat / scale
        notes.append("按头文件的 reflectance scale factor = %g 换算。" % scale)
    mat[np.abs(mat) > 1e20] = np.nan                  # USGS 拿 -1.23e34 当无效值
    ignore = meta.get("data ignore value", "").strip()
    if ignore:
        # 头文件声明的无效值（常见 -9999）。不挖掉的话这几个点不只是曲线上的尖刺，
        # 还会把下面的量纲判断带偏——95 分位一过 1.6，整份数据被当成万倍整数除掉
        try:
            mat[mat == float(ignore) / scale] = np.nan
        except ValueError:
            pass
    names = _envi_list(meta.get("spectra names", ""))
    order_x = np.argsort(x, kind="stable")
    x = x[order_x]
    stem = os.path.splitext(os.path.basename(data))[0]
    out = []
    for i in range(lines):
        y = mat[i, order_x]
        if not np.isfinite(y).any():
            continue
        out.append(spectra.Spectrum(
            label=names[i] if i < len(names) else "%s_%d" % (stem, i + 1),
            x=x.copy(), y=y, bands=np.arange(1, x.size + 1, dtype=np.float64),
            x_is_wavelength=True, origin="csv", layer_name=os.path.basename(data)))
    loaded = Loaded(out, notes)
    normalise(out, loaded.notes)
    return loaded


# =============================================================================
# ASD 光谱仪的二进制文件
# =============================================================================

#: ASD 文件头里 data_type 的取值：0 原始 DN、1 反射率、2 辐亮度……只认反射率
ASD_REFLECTANCE = 1
#: 数据区的数值格式：0 float32、1 int32、2 float64
_ASD_FORMATS = {0: ("f", 4), 1: ("i", 4), 2: ("d", 8)}
#: 文件头固定 484 字节，光谱数据紧随其后
_ASD_HEADER = 484


def read_asd(path: str) -> spectra.Spectrum:
    """读一个 ASD FieldSpec 存下的二进制 ``.asd``，返回小数反射率。

    文件头 484 字节：第 186 字节是数据类型，191 / 195 是首波长与步长（float32），
    199 是数值格式，204 是通道数。v7 / v8（文件开头 ``as7`` / ``as8``）的反射率文件
    存的是**目标的原始 DN**，白板的 DN 附在后面（先是 20 字节的参考头：2 字节标志、
    两个 8 字节时间、2 字节长度的说明串），反射率 = 目标 ÷ 白板，与 ViewSpecPro
    导出的一致。更早的版本没有附白板，数据区本身就是反射率。不做探测器接缝校正
    （1000、1800 nm 两处的小台阶），与 ViewSpecPro 的默认导出相同。
    """
    with open(path, "rb") as fh:
        d = fh.read()
    if len(d) < _ASD_HEADER or d[:3].lower() not in (b"as7", b"as8", b"as6", b"asd", b"as5"):
        raise ValueError("%s 不是 ASD 光谱文件" % os.path.basename(path))
    version = d[:3].lower()
    data_type = d[186]
    first, step = struct.unpack_from("<ff", d, 191)
    fmt = _ASD_FORMATS.get(d[199])
    channels = struct.unpack_from("<H", d, 204)[0]
    if fmt is None or not channels:
        raise ValueError("%s 的数值格式认不出来" % os.path.basename(path))
    code, size = fmt
    end = _ASD_HEADER + size * channels
    if len(d) < end:
        raise ValueError("%s 不完整" % os.path.basename(path))
    y = np.array(struct.unpack_from("<%d%s" % (channels, code), d, _ASD_HEADER), dtype=np.float64)
    if data_type != ASD_REFLECTANCE:
        raise ValueError("%s 存的不是反射率（数据类型 %d），先在 ViewSpecPro 里换算成反射率再导入"
                         % (os.path.basename(path), data_type))
    if version in (b"as7", b"as8"):
        off = end + 2 + 8 + 8
        if len(d) < off + 2:
            raise ValueError("%s 缺白板参考谱" % os.path.basename(path))
        off += 2 + struct.unpack_from("<H", d, off)[0]
        if len(d) < off + size * channels:
            raise ValueError("%s 缺白板参考谱" % os.path.basename(path))
        ref = np.array(struct.unpack_from("<%d%s" % (channels, code), d, off), dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            y = np.where(ref > 0, y / ref, np.nan)
    x = first + step * np.arange(channels, dtype=np.float64)
    label = os.path.splitext(os.path.basename(path))[0]
    spec = spectra.Spectrum(label=label, x=x, y=y, origin="csv", native_unit="refl")
    return spec


def read_asd_files(paths: Sequence[str]) -> Loaded:
    out, notes = [], []
    for p in paths:
        try:
            out.append(read_asd(p))
        except (OSError, ValueError, struct.error) as exc:
            notes.append(str(exc))
    loaded = Loaded(out, notes)
    normalise(out, loaded.notes)
    return loaded


def read_any(path: str) -> Loaded:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".asd":
        return read_asd_files([path])
    if ext in (".sli", ".hdr"):
        return read_envi_sli(path)
    # 没有扩展名、旁边有同名 .hdr 的，是 ENVI 光谱库的数据文件
    if any(os.path.isfile(path + h) for h in (".hdr", ".HDR")):
        return read_envi_sli(path + (".hdr" if os.path.isfile(path + ".hdr") else ".HDR"))
    return read_table(path)


# =============================================================================
# 导出
# =============================================================================

def _labels(entries: Sequence[speclib.Entry]) -> List[str]:
    out, seen = [], {}
    for e in entries:
        label = ("%s %s" % (e.display, e.cn)) if e.cn and e.cn != e.name else e.display
        n = seen.get(label, 0)
        seen[label] = n + 1
        out.append(label if n == 0 else "%s (%d)" % (label, n + 1))
    return out


def export_slb(entries: Sequence[speclib.Entry], path: str,
               libraries: Optional[Sequence[speclib.Library]] = None) -> int:
    """导成一个 .slb 发给别人：分类、中文名、样品信息全都带着，对方「导入」即可。"""
    if os.path.exists(path):
        os.remove(path)
    out = speclib.UserLibrary(path)
    done = 0
    for entry in entries:
        if speclib.library_of(entry, libraries) is None:
            continue
        # 标准库的谱也按它原来的分类路径存：对方打开时照样挂在「硫酸盐」「层状
        # 硅酸盐」下面，而不是五千条全堆在「未归类」里
        out.copy_entry(entry, cls=entry.cls, libraries=libraries, save=False,
                       reindex=False)
        done += 1
    out._reindex()
    out.save()
    return done


def export_csv(entries: Sequence[speclib.Entry], path: str,
               libraries: Optional[Sequence[speclib.Library]] = None) -> int:
    """宽表：第一列波长（nm），其后每条谱一列。Excel、Origin、ENVI 都读得了。"""
    specs = []
    for entry, label in zip(entries, _labels(entries)):
        spec = speclib.spectrum_of(entry, label, libraries)
        if spec is not None:
            specs.append(spec)
    spectra.to_csv(path, specs)
    return len(specs)


def export_envi_sli(entries: Sequence[speclib.Entry], path: str,
                    libraries: Optional[Sequence[speclib.Library]] = None) -> int:
    """ENVI 光谱库（float32，nm）。拿去 ENVI 里做 SAM 分类、波谱分析就用它。

    ENVI 光谱库只有一条波长轴，波长不同的谱要对到同一条上：取点数最多的那条谱的
    波长做轴，其余线性插值过去，超出各自测量范围的地方是 NaN（不外推）。
    """
    specs = [speclib.spectrum_of(e, label, libraries)
             for e, label in zip(entries, _labels(entries))]
    specs = [s for s in specs if s is not None]
    if not specs:
        raise ValueError("没有可导出的谱。")
    axis = max((s.x for s in specs), key=lambda x: x.size)
    mat = np.full((len(specs), axis.size), np.nan, dtype=np.float32)
    for i, s in enumerate(specs):
        good = np.isfinite(s.y)
        if good.sum() < 2:
            continue
        inside = (axis >= s.x[good][0]) & (axis <= s.x[good][-1])
        mat[i, inside] = np.interp(axis[inside], s.x[good], s.y[good])
    base = os.path.splitext(path)[0]
    mat.astype("<f4").tofile(base + ".sli")
    names = ", ".join(s.label.replace(",", " ").replace("{", "(").replace("}", ")")
                      for s in specs)
    waves = ", ".join("%.4f" % w for w in axis)
    with open(base + ".hdr", "w", encoding="utf-8") as fh:
        fh.write("ENVI\n")
        fh.write("description = {LithoSpectra Studio spectral library export}\n")
        fh.write("samples = %d\nlines = %d\nbands = 1\nheader offset = 0\n"
                 % (axis.size, len(specs)))
        fh.write("file type = ENVI Spectral Library\ndata type = 4\n"
                 "interleave = bsq\nbyte order = 0\n")
        fh.write("wavelength units = Nanometers\nreflectance scale factor = 1.0\n")
        fh.write("spectra names = {%s}\n" % names)
        fh.write("wavelength = {%s}\n" % waves)
    return len(specs)


EXPORT_FORMATS: Tuple[Tuple[str, str, str], ...] = (
    ("slb", "LithoSpectra 光谱库 (*.slb)", ".slb"),
    ("sli", "ENVI 光谱库 (*.sli)", ".sli"),
    ("csv", "CSV 表格 (*.csv)", ".csv"),
)


def export(kind: str, entries, path: str, libraries=None) -> int:
    if kind == "slb":
        return export_slb(entries, path, libraries)
    if kind == "sli":
        return export_envi_sli(entries, path, libraries)
    return export_csv(entries, path, libraries)
