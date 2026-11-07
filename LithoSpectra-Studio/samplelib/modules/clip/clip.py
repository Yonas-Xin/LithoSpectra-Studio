# -*- coding: utf-8 -*-
"""从样本库直接裁剪深度学习样本块。

样本库存的是「哪个位置属于哪一类」，训练要的是「以该位置为中心的一小块影像
加一个标签」。本模块把前者变成后者，直接读 GeoPackage，不经中间的 shapefile。

输出与项目里 ``split_and_clip_dataset`` 那条链路（``aircasgxt_core.gdal_utils``）
完全一致，``Train.py`` / ``Models/Data.py`` 可以直接拿去用：

* 块文件 ``{前缀}_label{标签}_{序号}.tif|bin``，边缘不足处补 0
* ``datasets.txt``：每行「路径 标签」，空格分隔
* bin 格式附 ``raw_bip_meta.json``，记 layout / bands / height / width / dtype

另有第三种输出 ``h5``：整次裁剪只出 ``train_dataset.h5`` / ``test_dataset.h5``
两个文件，形状、dtype、标签、类别名、源影像与 geotransform 都存在文件内部。
几十万个小文件既难拷贝也难备份，元数据还挂在外面，一分家就读不出形状；单文件
则是一个自描述的整体。读取见 ``Models.Data.HDF5_Dataset``。

切块的窗口计算、边缘补零、地理变换改写都照搬原实现，没有改动其数学。

与那条链路相比只在三处不同，都是样本库能做得更好的地方：

**坐标系**
    样本库统一存 EPSG:4326，原链路只做反地理变换、不重投影——影像若是 UTM，
    经纬度算出的像素位置会全部落到影像外，而越界位置是被静默跳过的，于是
    「跑完不报错、输出空目录」。这里在取像素位置前先做坐标变换。

**标签**
    原链路的标签是目录里 shapefile 的枚举序号，换台机器或改个文件名就会变。
    这里的标签由类别层级决定，并随输出写一份 ``labels.json`` 对照表。

**训练/测试划分**
    原链路对面样本是**按像元**随机划分的，同一个露头的相邻像元会同时落进
    训练集与测试集，测试精度因此偏乐观。这里默认按**要素**划分（一个面整体
    归训练或测试），需要与原行为一致时把 ``split_by`` 设为 ``"pixel"``。
"""

import json
import os
import random
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
from osgeo import gdal, ogr, osr

from ... import config
from ...buffering import buffer_points
from ...io_shp import _class_path_name, _export_targets
from ...model import SampleKey, SampleRecord, key_to_str
from ...store import SampleStore, storage_srs

# GDAL 数据类型 -> (名字, numpy 类型)。与 aircasgxt_core.gdal_utils 保持一致，
# bin 文件的 dtype 字段直接写这个名字，下游按它 np.fromfile。
GDAL2NP_TYPE = {
    gdal.GDT_Byte: ("uint8", np.uint8),
    gdal.GDT_UInt16: ("uint16", np.uint16),
    gdal.GDT_Int16: ("int16", np.int16),
    gdal.GDT_UInt32: ("uint32", np.uint32),
    gdal.GDT_Int32: ("int32", np.int32),
    gdal.GDT_Float32: ("float32", np.float32),
    gdal.GDT_Float64: ("float64", np.float64),
}
NP2GDAL_TYPE = {np.dtype(name): gtype for gtype, (name, _np) in GDAL2NP_TYPE.items()}

# 没有地理参考的影像的标志性地理变换
_NO_GEOTRANSFORM = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


# =============================================================================
# 报告
# =============================================================================

@dataclass
class ClipUnit:
    """一个标签对应的类别单元。"""

    label: int
    name: str                          # 层级名「大类-子类」，同名类别在不同分组下也不重
    code: str
    path: str = ""                     # 层级路径「大类 / 子类」，与导出 shp 的 class_path 一致
    class_ids: List[int] = field(default_factory=list)
    color: str = ""                    # 样本库里该类别的颜色，模型与分类图沿用
    features: int = 0                  # 参与裁剪的样本要素数
    positions: int = 0                 # 展开成的像素位置数
    train: int = 0                     # 实际写出的训练块数
    test: int = 0                      # 实际写出的测试块数
    outside: int = 0                   # 落在影像范围外，未写出


@dataclass
class ClipReport:
    """一次裁剪的结果。"""

    out_dir: str = ""
    image: str = ""
    patch_size: int = 0
    output_format: str = "bin"
    split_by: str = "feature"
    train_ratio: float = 1.0
    buffer_radius: float = 0.0         # 点样本按这个半径（米）扩成圆再裁；0 不缓冲
    units: List[ClipUnit] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def train_total(self) -> int:
        return sum(u.train for u in self.units)

    @property
    def test_total(self) -> int:
        return sum(u.test for u in self.units)

    @property
    def outside_total(self) -> int:
        return sum(u.outside for u in self.units)

    def summary(self) -> str:
        head = "共 %d 类，训练 %d 块" % (len(self.units), self.train_total)
        if self.test_total:
            head += "，测试 %d 块" % self.test_total
        head += "，块大小 %d×%d，格式 %s。" % (self.patch_size, self.patch_size,
                                             self.output_format)
        if self.outside_total:
            head += " %d 个位置落在影像外，已跳过。" % self.outside_total
        if self.cancelled:
            head = "已取消。" + head
        return head


# =============================================================================
# 几何 -> 像素位置
# =============================================================================

def _pixel_of(x: float, y: float, inv_gt) -> Tuple[int, int]:
    """地理坐标 -> (行, 列)。截断取整，与原链路一致。"""
    col, row = gdal.ApplyGeoTransform(inv_gt, x, y)
    return int(row), int(col)


def _memory_vector():
    """内存矢量数据源。``Memory`` 驱动在 GDAL 3.11 起改叫 ``MEM``，两边都试。"""
    for name in ("MEM", "Memory"):
        drv = ogr.GetDriverByName(name)
        if drv is not None:
            ds = drv.CreateDataSource("")
            if ds is not None:
                return ds
    raise RuntimeError("GDAL 没有可用的内存矢量驱动")


def _pixel_window(geom: ogr.Geometry, inv_gt, width: int, height: int
                  ) -> Optional[Tuple[int, int, int, int]]:
    """要素外接矩形对应的像素窗口 ``(r0, r1, c0, c1)``，外扩一圈并裁到影像内；
    与影像不相交时返回 None。窗口与影像像素网格对齐，因此在窗口上栅格化选出的
    像元与整幅栅格化的结果完全相同。"""
    minx, maxx, miny, maxy = geom.GetEnvelope()
    corners = [_pixel_of(px, py, inv_gt)
               for px, py in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy))]
    rows = [c[0] for c in corners]
    cols = [c[1] for c in corners]
    r0 = max(0, min(rows) - 1)
    r1 = min(height, max(rows) + 2)
    c0 = max(0, min(cols) - 1)
    c1 = min(width, max(cols) + 2)
    if r1 <= r0 or c1 <= c0:
        return None
    return r0, r1, c0, c1


# 一次栅格化的窗口最多多少像元（Int32，8M 像元 = 32 MB）。超过就把这批面对半
# 分开各自栅格化，直到装得下或只剩一个面。
_RASTER_WINDOW_PIXELS = 8_000_000


def _rasterize_polygons(polys: Sequence[Tuple[int, ogr.Geometry, Tuple[int, int, int, int]]],
                        gt, srs, proj: str,
                        out: List[Tuple[int, int, int]]) -> None:
    """把一批面一次栅格化，往 ``out`` 里追加 ``(行, 列, 要素序号)``。

    一个面一次 ``RasterizeLayer`` 的做法在几万个小图斑上慢得离谱：每次调用要建
    内存栅格、建内存矢量、跑一遍栅格化的固定开销，而图斑本身只占两三个像元。
    改成一个单元的面合在一起烧一次，烧的值是要素序号，读回来就同时得到位置与
    归属。像元集合与逐个烧完全一样（窗口对齐同一套像素网格）；重叠处以**先来
    的面**为准，与原先 ``seen`` 集合的语义一致——所以要素按倒序加进图层，最后
    烧上去的就是序号最小的那个。
    """
    if not polys:
        return
    r0 = min(w[0] for _o, _g, w in polys)
    r1 = max(w[1] for _o, _g, w in polys)
    c0 = min(w[2] for _o, _g, w in polys)
    c1 = max(w[3] for _o, _g, w in polys)
    if (r1 - r0) * (c1 - c0) > _RASTER_WINDOW_PIXELS and len(polys) > 1:
        # 装不下：按窗口中心行排序后对半分，两半各自的窗口都比整体小
        ordered = sorted(polys, key=lambda item: (item[2][0] + item[2][1], item[2][2]))
        half = len(ordered) // 2
        _rasterize_polygons(ordered[:half], gt, srs, proj, out)
        _rasterize_polygons(ordered[half:], gt, srs, proj, out)
        return

    mem = gdal.GetDriverByName("MEM").Create("", c1 - c0, r1 - r0, 1, gdal.GDT_Int32)
    mem.SetGeoTransform((gt[0] + c0 * gt[1] + r0 * gt[2], gt[1], gt[2],
                         gt[3] + c0 * gt[4] + r0 * gt[5], gt[4], gt[5]))
    if proj:
        mem.SetProjection(proj)
    band = mem.GetRasterBand(1)
    band.Fill(0)

    vec = _memory_vector()
    # 带上坐标系，否则 GDAL 只能"假定两边一致"并给出警告
    layer = vec.CreateLayer("unit", srs=srs, geom_type=ogr.wkbUnknown)
    layer.CreateField(ogr.FieldDefn("oid", ogr.OFTInteger))
    defn = layer.GetLayerDefn()
    # 按要素序号倒序烧：对半分之后列表是按窗口位置排的，不能直接 reversed
    for owner, geom, _w in sorted(polys, key=lambda item: item[0], reverse=True):
        feat = ogr.Feature(defn)
        feat.SetField(0, owner + 1)                 # 0 留给"没烧到"
        feat.SetGeometry(geom)
        layer.CreateFeature(feat)
    gdal.RasterizeLayer(mem, [1], layer, options=["ATTRIBUTE=oid"])

    arr = band.ReadAsArray()
    vec = None
    mem = None
    if arr is None:
        return
    rr, cc = np.nonzero(arr)
    owners = arr[rr, cc] - 1
    out.extend(zip((rr + r0).tolist(), (cc + c0).tolist(), owners.tolist()))


def _unit_positions(store: SampleStore, class_ids: Sequence[int],
                    selection: Optional[Set[SampleKey]],
                    transform, gt, inv_gt, srs, proj: str,
                    width: int, height: int,
                    buffer_radius: float = 0.0,
                    buffer_set: Optional[Set[int]] = None
                    ) -> Tuple[List[Tuple[int, int]], List[int], int, int, int, List[str]]:
    """收集一个单元的全部像素位置。

    返回 ``(位置列表, 与位置一一对应的要素序号, 要素数, 整个落在影像外的面数,
    在影像内却没盖住任何像元中心的面数, 要素序号 -> 样本主键)``。
    要素序号用于按要素划分训练/测试——面样本的相邻像元必须整体归到同一侧，否则
    测试集里全是训练集的邻居，精度看着好看而已。
    """
    cells: List[Tuple[int, int, int]] = []     # (行, 列, 要素序号)
    polys: List[Tuple[int, ogr.Geometry, Tuple[int, int, int, int]]] = []
    features = 0
    outside = 0                     # 整个落在影像外的面要素数
    owner_keys: List[str] = []      # 要素序号 -> 样本主键（"point:12"），h5 记进 sample_keys

    # 带几何、不转坐标：这里只要 OGR 几何，转成坐标再按主键回库取一次是白绕一圈
    records = [r for r in store.iter_records(class_ids=class_ids, geometry=True, coords=False)
               if (selection is None or r.key in selection) and r.geom is not None]
    if buffer_radius > 0:
        # 点扩成圆（经纬度下做，圆是在 UTM 里画的），随后当作面要素处理：
        # 一个合并后的圆就是一个要素，按要素划分训练/测试时整体归一侧
        todo = [r for r in records if r.geom_kind == "point"
                and (buffer_set is None or r.class_id in buffer_set)]
        if todo:
            circles = buffer_points([(r.key, r.geom) for r in todo], buffer_radius)
            done = {r.key for r in todo}
            records = [r for r in records if r.key not in done]
            for poly, keys in circles:
                records.append(SampleRecord(key=keys[0], class_id=todo[0].class_id,
                                            geom_kind="polygon", coords=[], geom=poly))
    for record in records:
        geom = record.geom
        if transform is not None:
            if geom.Transform(transform) != 0:
                continue                # 该要素落在目标坐标系的定义域外
        features += 1
        owner = features - 1
        owner_keys.append(key_to_str(record.key))
        if geom.GetGeometryName() in ("POINT", "MULTIPOINT"):
            pts = [geom] if geom.GetGeometryName() == "POINT" else [
                geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())]
            for p in pts:
                r, c = _pixel_of(p.GetX(), p.GetY(), inv_gt)
                cells.append((r, c, owner))
        else:
            window = _pixel_window(geom, inv_gt, width, height)
            if window is None:
                # 点要素落在影像外由写块那一步计数（位置还在列表里）；面要素的
                # 窗口与影像不相交时这里就知道了，不记一笔就没人知道这个面被
                # 跳过了，报告里"要素 5 个、块 0 个"对不上。
                outside += 1
                continue
            polys.append((owner, geom, window))

    _rasterize_polygons(polys, gt, srs, proj, cells)
    # 在影像范围内却一个像元中心都没盖住的面：比一个像元还小，或被同类里排在
    # 前面的面整个盖住了。它们不产出任何位置，报告里得有一笔。
    burned = {owner for _r, _c, owner in cells}
    uncovered = sum(1 for owner, _g, _w in polys if owner not in burned)

    # 面样本相互重叠时像元只算一次，先来的要素为准；最后按位置排序，与整幅
    # 栅格化的 np.where 结果同序，便于与原链路逐块比对
    seen: Set[Tuple[int, int]] = set()
    positions: List[Tuple[int, int]] = []
    owners: List[int] = []
    cells.sort(key=lambda item: item[2])       # 点与面混在一起时也按要素先后
    for r, c, owner in cells:
        cell = (r, c)
        if cell in seen:
            continue
        seen.add(cell)
        positions.append(cell)
        owners.append(owner)
    order = sorted(range(len(positions)), key=lambda i: positions[i])
    return ([positions[i] for i in order], [owners[i] for i in order],
            features, outside, uncovered, owner_keys)


# =============================================================================
# 写块
# =============================================================================

def _write_tif(path: str, data: np.ndarray, gt, proj: str) -> None:
    """写一个样本块。参数与原链路的 ``write_data_to_tif`` 对齐。"""
    bands, rows, cols = data.shape
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(path, cols, rows, bands, NP2GDAL_TYPE[data.dtype],
                       options=["INTERLEAVE=PIXEL"])
    if ds is None:
        raise IOError("无法创建文件：%s" % path)
    ds.SetGeoTransform(gt)
    if proj:
        ds.SetProjection(proj)
    for i in range(bands):
        ds.GetRasterBand(i + 1).WriteArray(data[i, :, :])
    ds.FlushCache()
    ds = None


def _write_meta(out_dir: str, bands: int, patch_size: int, dtype_name: str,
                count: int, image: str, list_path: str,
                complete: bool = True) -> None:
    """bin 格式的伴生元数据。没有它，下游只看到一堆没有形状的字节。"""
    meta = {
        "layout": "BHW",
        "bands": int(bands),
        "height": int(patch_size),
        "width": int(patch_size),
        "dtype": str(dtype_name),
        "count": int(count),
        "complete": bool(complete),     # 半途取消的裁剪写 False
        "source_image_path": image,
        "list_path": list_path,
    }
    with open(os.path.join(out_dir, "raw_bip_meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)


def _clean_stale_patches(out_dir: str) -> int:
    """删掉上一次裁剪到同一目录、登记在样本清单里的块文件，返回删了几个。

    块文件按「前缀_label标签_序号」命名，往同一个目录重裁一次类别少、块少的
    结果时，旧文件只被覆盖一部分，剩下的留在目录里：清单里没有它们，但目录看
    上去像有更多样本，拿目录直接喂给按文件名扫描的读取代码就会把过时的块也
    读进去。只删旧清单登记过的文件（新旧两个清单名都认），使用者自己放进去的
    东西不碰。
    """
    removed = 0
    for name in (config.DATASETS_LIST, config.DATASETS_LIST_LEGACY):
        path = os.path.join(out_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for line in lines:
            victim = line.rsplit(" ", 1)[0] if " " in line else line
            if not victim:
                continue
            if not os.path.isabs(victim):
                victim = os.path.join(out_dir, victim)
            # 只碰输出目录里的文件：清单若被人改过、指到别处去，也不能顺着删
            if os.path.dirname(os.path.abspath(victim)) != os.path.abspath(out_dir):
                continue
            try:
                os.remove(victim)
                removed += 1
            except OSError:
                pass
        try:
            os.remove(path)
        except OSError:
            pass
    return removed


class _PatchCutter:
    """位置 -> 样本块。窗口计算、边缘补零、块的地理变换都在这里，各格式共用。

    切法与 ``aircasgxt_core.gdal_utils`` 那条链路完全一致：以位置为中心开窗，
    偶数块中心偏左上，窗口超出影像的部分补 0。
    """

    def __init__(self, ds: gdal.Dataset, patch_size: int) -> None:
        self.ds = ds
        self.patch = patch_size
        self.width = ds.RasterXSize
        self.height = ds.RasterYSize
        self.bands = ds.RasterCount
        self.gt = ds.GetGeoTransform()
        self.proj = ds.GetProjection()
        gtype = ds.GetRasterBand(1).DataType
        name, np_type = GDAL2NP_TYPE.get(gtype, ("unknown", None))
        if np_type is None:
            raise ValueError("不支持的影像数据类型：%s" % gdal.GetDataTypeName(gtype))
        self.dtype_name = name
        self.np_type = np_type
        # 偶数块取中心偏左上，与原链路一致
        if patch_size % 2 == 0:
            self.left_top = patch_size // 2 - 1
            self.right_bottom = patch_size // 2
        else:
            self.left_top = self.right_bottom = patch_size // 2

    def cut(self, row: int, col: int):
        """切一个块。返回 ``(块数组, 块的地理变换)``；窗口完全在影像外时返回
        ``(None, None)``。"""
        x_start = col - self.left_top
        y_start = row - self.left_top
        read_x = max(0, x_start)
        read_y = max(0, y_start)
        read_w = min(col + self.right_bottom + 1, self.width) - read_x
        read_h = min(row + self.right_bottom + 1, self.height) - read_y
        if read_w <= 0 or read_h <= 0:
            return None, None
        data = self.ds.ReadAsArray(read_x, read_y, read_w, read_h)
        if data is None:
            return None, None
        if data.ndim == 2:
            data = data[np.newaxis, :, :]
        full = np.zeros((self.bands, self.patch, self.patch), dtype=self.np_type)
        full[:, read_y - y_start:read_y - y_start + read_h,
             read_x - x_start:read_x - x_start + read_w] = data
        block_gt = list(self.gt)
        # 旋转项 gt[2]/gt[4] 不能漏：北向影像它们是 0，看不出差别；影像一旦带
        # 旋转或倾斜，块原点就必须把另一个方向的位移也算进去，否则每个块都按
        # 自己离原点的距离偏出去一段。
        block_gt[0] = self.gt[0] + x_start * self.gt[1] + y_start * self.gt[2]
        block_gt[3] = self.gt[3] + x_start * self.gt[4] + y_start * self.gt[5]
        return full, block_gt


class _PatchWriter(_PatchCutter):
    """把位置写成一个个块文件，并记下 ``.datasets.txt`` 需要的行。"""

    def __init__(self, ds: gdal.Dataset, out_dir: str, patch_size: int,
                 output_format: str, prefix: str) -> None:
        super().__init__(ds, patch_size)
        self.out_dir = out_dir
        self.fmt = output_format
        self.prefix = prefix
        self.lines: List[str] = []
        os.makedirs(out_dir, exist_ok=True)
        self.removed = _clean_stale_patches(out_dir)


    def write(self, positions: Sequence[Tuple[int, int]], label: int,
              keys: Optional[Sequence[str]] = None) -> Tuple[int, int]:
        """写一批位置，返回 (写出块数, 越界跳过数)。``keys`` 只有 h5 记。"""
        name = "%s_label%d" % (self.prefix, label)
        written = outside = 0
        for row, col in positions:
            full, block_gt = self.cut(row, col)
            if full is None:
                outside += 1
                continue
            written += 1
            if self.fmt == "tif":
                path = os.path.join(self.out_dir, "%s_%d.tif" % (name, written))
                _write_tif(path, full, block_gt, self.proj)
            else:
                path = os.path.join(self.out_dir, "%s_%d.bin" % (name, written))
                full.tofile(path)
            self.lines.append("%s %d" % (path, label))
        return written, outside

    def finish(self, image: str, units: Optional[Sequence["ClipUnit"]] = None,
               complete: bool = True) -> str:
        """写出 ``datasets.txt``（与 bin 的元数据），返回清单路径。"""
        list_path = os.path.join(self.out_dir, config.DATASETS_LIST)
        with open(list_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(self.lines))
        if self.fmt == "bin":
            _write_meta(self.out_dir, self.bands, self.patch, self.dtype_name,
                        len(self.lines), image, list_path, complete=complete)
        return list_path

    def abort(self) -> None:
        """裁剪半途失败时收尾。

        已经落盘的块文件留着不动（它们各自都是完整的），但**不写样本清单**——
        没有清单，下游就不会把这批残缺数据当成一份可用的样本集拿去训练。
        """
        self.lines = []

    @property
    def count(self) -> int:
        return len(self.lines)


def h5_filter_kwargs(compression: Optional[str],
                     warnings: Optional[List[str]] = None) -> dict:
    """把配置里的压缩器名换成 h5py 的关键字参数。

    h5py 自带的只有 gzip / lzf；zstd 要靠 ``hdf5plugin`` 注册过滤器，而且**读的
    一侧也要装** ``hdf5plugin``，否则打开文件时报"未知过滤器"。所以缺包时不是
    报错而是退回不压缩——宁可多占盘，也别写出一份别人读不开的样本集。
    """
    name = str(compression or "none").lower()
    if name in ("", "none", "off"):
        return {}
    if name == "lzf":
        return {"compression": "lzf"}
    if name == "gzip":
        return {"compression": "gzip", "compression_opts": 1}
    if name == "zstd":
        try:
            import hdf5plugin
        except ImportError:
            if warnings is not None:
                warnings.append(
                    "未安装 hdf5plugin，zstd 压缩不可用，本次按不压缩写出。"
                    "需要压缩请先 pip install hdf5plugin（读取时也要装）。")
            return {}
        return dict(hdf5plugin.Blosc2(cname="zstd", clevel=3,
                                      filters=hdf5plugin.Blosc2.SHUFFLE))
    raise ValueError("不认识的 HDF5 压缩方式：%r" % compression)


class _H5Writer(_PatchCutter):
    """把一次裁剪的全部块写进一个 ``.h5``。

    ``chunks`` 必须按**一个样本**切。HDF5 的最小读写单位就是 chunk：chunk 若按
    几十个样本切，训练时随机取一个样本也要把整块读出来（压缩时还要整块解压），
    实测随机读会从两万多样本/秒掉到三百，慢两个数量级。

    块的地理变换在这里没法逐块存，改成存每个块的中心行列号（``centers``）加上
    源影像的 geotransform / 投影，需要时照样能把任意一个块定位回原图。
    """

    FLUSH = 256                         # 攒够这么多再落一次盘，逐个 resize 太慢

    def __init__(self, ds: gdal.Dataset, path: str, patch_size: int,
                 split: str, filter_kwargs: Optional[dict] = None) -> None:
        super().__init__(ds, patch_size)
        try:
            import h5py
        except ImportError as exc:
            raise RuntimeError(
                "输出 h5 需要 h5py，当前环境没有装。\n"
                "请先 pip install h5py，或把输出格式改回 bin / tif。") from exc
        self.path = path
        self.split = split
        self._count = 0
        self._buf_data: List[np.ndarray] = []
        self._buf_label: List[int] = []
        self._buf_center: List[Tuple[int, int]] = []
        self._buf_key: List[str] = []
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        shape = (self.bands, patch_size, patch_size)
        self._f = h5py.File(path, "w")
        self._data = self._f.create_dataset(
            "patches", shape=(0,) + shape, maxshape=(None,) + shape,
            chunks=(1,) + shape, dtype=self.np_type, **(filter_kwargs or {}))
        self._labels = self._f.create_dataset(
            "labels", shape=(0,), maxshape=(None,), dtype=np.int32)
        self._centers = self._f.create_dataset(
            "centers", shape=(0, 2), maxshape=(None, 2), dtype=np.int32)
        # 每个块来自样本库哪条样本：训练时按样本整体切验证集，评估时把错分的块
        # 指回样本库里那一条
        self._keys = self._f.create_dataset(
            "sample_keys", shape=(0,), maxshape=(None,), dtype=h5py.string_dtype("utf-8"))

    def write(self, positions: Sequence[Tuple[int, int]], label: int,
              keys: Optional[Sequence[str]] = None) -> Tuple[int, int]:
        written = outside = 0
        for i, (row, col) in enumerate(positions):
            full, _block_gt = self.cut(row, col)
            if full is None:
                outside += 1
                continue
            self._buf_data.append(full)
            self._buf_label.append(label)
            self._buf_center.append((row, col))
            self._buf_key.append(keys[i] if keys is not None else "")
            written += 1
            if len(self._buf_data) >= self.FLUSH:
                self._flush()
        return written, outside

    def _flush(self) -> None:
        n = len(self._buf_data)
        if not n:
            return
        end = self._count + n
        self._data.resize(end, axis=0)
        self._data[self._count:end] = np.stack(self._buf_data)
        self._labels.resize(end, axis=0)
        self._labels[self._count:end] = np.asarray(self._buf_label, dtype=np.int32)
        self._centers.resize(end, axis=0)
        self._centers[self._count:end] = np.asarray(self._buf_center, dtype=np.int32)
        self._keys.resize(end, axis=0)
        self._keys[self._count:end] = np.asarray(self._buf_key, dtype=object)
        self._count = end
        self._buf_data.clear()
        self._buf_label.clear()
        self._buf_center.clear()
        self._buf_key.clear()

    def finish(self, image: str, units: Optional[Sequence["ClipUnit"]] = None,
               complete: bool = True) -> str:
        """补齐元数据后关闭文件，返回 .h5 路径。

        形状、dtype、类别名都写进同一个文件——样本集换台机器、改个文件名都还
        读得出来，不像 bin 那样一旦跟 raw_bip_meta.json 分家就成了一堆没有形状
        的字节。
        """
        import h5py
        self._flush()
        a = self._f.attrs
        a["layout"] = "NBHW"
        a["bands"] = int(self.bands)
        a["height"] = int(self.patch)
        a["width"] = int(self.patch)
        a["patch_size"] = int(self.patch)
        a["dtype"] = str(self.dtype_name)
        a["count"] = int(self._count)
        a["complete"] = bool(complete)      # 半途取消的裁剪写 False
        a["split"] = str(self.split)
        a["source_image_path"] = str(image)
        a["source_geotransform"] = np.asarray(self.gt, dtype=np.float64)
        a["source_projection"] = str(self.proj)
        from ...annotate.layers import _read_envi_wavelengths
        wavelengths = _read_envi_wavelengths(image, self.bands)
        if wavelengths:
            a["wavelengths"] = np.asarray(wavelengths, dtype=np.float64)
        if units:
            vlen = h5py.string_dtype(encoding="utf-8")
            self._f.create_dataset(
                "label_names", data=np.array([u.name for u in units], dtype=object),
                dtype=vlen)
            self._f.create_dataset(
                "label_codes", data=np.array([u.code or "" for u in units], dtype=object),
                dtype=vlen)
            # 整张类别表（与 labels.json 的 labels 数组同构）写成一个 JSON 属性：
            # 下游一次读出即得完整的表，labels.json 丢了也不影响。
            a["class_table"] = json.dumps(class_table(units), ensure_ascii=False)
        self._f.close()
        self._f = None
        return self.path

    def abort(self) -> None:
        """裁剪半途失败时收尾：关掉句柄，删掉写了一半的文件。

        半个 h5 比没有文件更坏——它有 ``patches`` 却没有 ``count``、``dtype``
        这些属性，读的一侧只会报一句"样本集是空的"，谁也想不到是上一次裁剪崩在
        中途。句柄不关在 Windows 上还会把文件锁住，重裁都覆盖不了。
        """
        if self._f is not None:
            try:
                self._f.close()
            except Exception:
                pass
            self._f = None
        if os.path.isfile(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass

    @property
    def count(self) -> int:
        return self._count


# =============================================================================
# 主流程
# =============================================================================

def _cut_point(total: int, train_ratio: float) -> int:
    """按比例取切分点，并保证两侧都不为空。

    直接 ``int(total * ratio)`` 在样本单位很少时会把一侧切空：一个类别只有
    一个要素时 ``int(1 * 0.6) == 0``，该类别就完全没有训练样本了——而标签仍在
    ``labels.json`` 里，模型永远学不到它，跑完还什么都不报。要素数少时误差也
    很大（3 个要素按 0.6 切实际是 33%），改用四舍五入更贴近设定值。
    """
    if total <= 1:
        return total                    # 只有一个单位，只能整个归训练侧
    return max(1, min(total - 1, int(round(total * train_ratio))))


def _split(positions: Sequence[Tuple[int, int]], owners: Sequence[int],
           train_ratio: float, split_by: str, rng: random.Random):
    """按比例把位置分成训练/测试两份。"""
    if train_ratio >= 1.0 or not positions:
        return list(positions), []
    if split_by == "pixel":
        idx = list(range(len(positions)))
        rng.shuffle(idx)
        train_idx = set(idx[:_cut_point(len(idx), train_ratio)])
        train = [positions[i] for i in sorted(train_idx)]
        test = [positions[i] for i in range(len(positions)) if i not in train_idx]
        return train, test
    # 按要素划分：整个面归一侧，避免相邻像元跨越训练与测试
    ids = sorted(set(owners))
    rng.shuffle(ids)
    train_ids = set(ids[:_cut_point(len(ids), train_ratio)])
    train = [p for p, o in zip(positions, owners) if o in train_ids]
    test = [p for p, o in zip(positions, owners) if o not in train_ids]
    return train, test


def clip_library(store: SampleStore, image_path: str, out_dir: str, *,
                 class_ids: Optional[Iterable[int]] = None,
                 selection: Optional[Set[SampleKey]] = None,
                 level="leaf",
                 unit_class_ids: Optional[Iterable[int]] = None,
                 patch_size: int = config.CLIP_PATCH_SIZE,
                 output_format: str = config.CLIP_FORMAT,
                 h5_compression: Optional[str] = config.CLIP_H5_COMPRESSION,
                 train_ratio: float = config.CLIP_TRAIN_RATIO,
                 split_by: str = config.CLIP_SPLIT_BY,
                 max_positions: int = config.CLIP_MAX_POSITIONS,
                 seed: int = config.CLIP_SEED,
                 prefix: str = config.CLIP_PREFIX,
                 buffer_radius: float = 0.0,
                 buffer_class_ids: Optional[Iterable[int]] = None,
                 progress: Optional[Callable[[int, int, str], bool]] = None
                 ) -> ClipReport:
    """把样本库里的样本裁成训练用的影像块。

    ``output_format`` 为 ``"h5"`` 时输出单文件样本集，``h5_compression`` 才有
    意义（``"none"`` / ``"lzf"`` / ``"gzip"`` / ``"zstd"``，zstd 需要 hdf5plugin）。

    ``level`` / ``unit_class_ids`` 与导出用的是同一套口径（见
    :func:`samplelib.io_shp.export_classes`）：一个导出单元就是一个标签。
    ``selection`` 给定时只裁其中的样本。``progress`` 每完成一个类别调用一次，
    返回 ``False`` 表示取消。

    ``buffer_radius`` 大于 0 时点样本扩成该半径（米）的圆再裁：圆内每个像元
    一个块，重叠的圆合成一个要素；``buffer_class_ids`` 限定只对这些基础类别
    的点做。不缓冲的点仍是"一个点一个块"。
    """
    output_format = str(output_format).lower()
    if output_format not in ("tif", "bin", "h5"):
        raise ValueError("输出格式只能是 tif、bin 或 h5：%r" % output_format)
    if patch_size < 1:
        raise ValueError("块大小必须是正整数：%r" % patch_size)
    if not 0 < train_ratio <= 1:
        raise ValueError("训练集比例必须在 0 与 1 之间：%r" % train_ratio)

    try:
        ds = gdal.Open(image_path)
    except RuntimeError as exc:         # 新版 GDAL 默认抛异常，消息是英文的
        raise RuntimeError("无法打开影像：%s\n%s" % (image_path, exc)) from exc
    if ds is None:
        raise RuntimeError("无法打开影像：%s" % image_path)
    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()
    if gt == _NO_GEOTRANSFORM or not proj:
        raise ValueError(
            "影像没有地理参考信息，无法把样本的地理位置对到像素上。\n"
            "未经几何校正的 dat 影像请先做几何校正，或转成带坐标系的 tif。")
    inv_gt = gdal.InvGeoTransform(gt)
    if inv_gt is None:
        raise ValueError("影像的地理变换不可逆，无法定位样本：%s" % image_path)

    img_srs = osr.SpatialReference(wkt=proj)
    img_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    src_srs = storage_srs()
    transform = None
    if not src_srs.IsSame(img_srs):
        transform = osr.CoordinateTransformation(src_srs, img_srs)

    report = ClipReport(out_dir=os.path.abspath(out_dir), image=image_path,
                        patch_size=patch_size, output_format=output_format,
                        split_by=split_by, train_ratio=train_ratio,
                        buffer_radius=float(buffer_radius or 0.0))
    buffer_set = ({int(c) for c in buffer_class_ids}
                  if buffer_class_ids is not None else None)
    if " " in report.out_dir and output_format != "h5":
        report.warnings.append(
            "输出目录含空格。样本清单 %s 以空格分隔路径与标签，"
            % config.DATASETS_LIST +
            "下游按空格切分会解析错，建议换一个不含空格的目录。")

    all_classes = store.list_classes()
    by_id = {c.class_id: c for c in all_classes}
    allowed = ({int(c) for c in class_ids} if class_ids is not None else set(by_id))
    targets, notes = _export_targets(all_classes, by_id, allowed,
                                     1 if level == "root" else level, unit_class_ids)
    report.warnings.extend(notes)
    if not targets:
        report.warnings.append("没有符合条件的类别，未生成任何样本块。")
        ds = None
        return report

    if output_format == "h5":
        # 一个 split 一个文件，放在输出目录下，与 labels.json 同级
        filters = h5_filter_kwargs(h5_compression, report.warnings)
        train_writer = _H5Writer(ds, os.path.join(report.out_dir, "train_dataset.h5"),
                                 patch_size, "train", filters)
        test_writer = (_H5Writer(ds, os.path.join(report.out_dir, "test_dataset.h5"),
                                 patch_size, "test", filters)
                       if train_ratio < 1.0 else None)
    else:
        train_dir = os.path.join(report.out_dir, "train_dataset")
        test_dir = os.path.join(report.out_dir, "test_dataset")
        train_writer = _PatchWriter(ds, train_dir, patch_size, output_format, prefix)
        test_writer = (_PatchWriter(ds, test_dir, patch_size, output_format, prefix)
                       if train_ratio < 1.0 else None)
        stale = train_writer.removed + (test_writer.removed if test_writer else 0)
        if stale:
            report.warnings.append("已清理上一次裁剪遗留的 %d 个块文件。" % stale)
    rng = random.Random(seed)

    # 裁剪半途出错时必须收尾：h5 的句柄不关，写了一半的文件既没有属性也读不出
    # 东西，Windows 上还会被锁住；bin 这边则不写样本清单，免得下游把残缺的一批
    # 块当成可用样本集。
    try:

        # 标签号按**真正写出了块**的类别从 0 连续排。用 enumerate(targets) 直接编号
        # 看着一样，但只要有一个类别一块都没裁出来（样本全落在影像外、或本次没勾中
        # 它的样本），那个号就永远空着，下游按 0..N-1 建分类器会多出一个没有样本的
        # 类。所以号在写完之后才算数，没写出东西的类别把号让给下一个。
        next_label = 0
        for done, (cls, source_ids) in enumerate(targets, start=1):
            unit = ClipUnit(label=next_label,
                            name=_class_path_name(cls, store, by_id=by_id),
                            code=cls.code or "",
                            path=_class_path_name(cls, store, sep=" / ", by_id=by_id),
                            class_ids=list(source_ids), color=cls.color or "")
            positions, owners, features, off_image, uncovered, owner_keys = _unit_positions(
                store, source_ids, selection, transform, gt, inv_gt, img_srs, proj,
                ds.RasterXSize, ds.RasterYSize,
                buffer_radius=report.buffer_radius, buffer_set=buffer_set)
            unit.features = features
            if uncovered:
                report.warnings.append(
                    "「%s」有 %d 个面没有盖住任何像元中心（比一个像元还小，或被同类"
                    "的面整个盖住），未产出样本块。" % (unit.name, uncovered))
            if max_positions and len(positions) > max_positions:
                # 一片大面能覆盖几十万像元，全裁出来既慢又让类别之间严重失衡
                keep = sorted(rng.sample(range(len(positions)), max_positions))
                positions = [positions[i] for i in keep]
                owners = [owners[i] for i in keep]
                report.warnings.append(
                    "「%s」覆盖像元过多，已随机抽取 %d 个位置。" % (unit.name, max_positions))
            unit.positions = len(positions)
            key_of = {p: owner_keys[o] for p, o in zip(positions, owners)}

            train_pos, test_pos = _split(positions, owners, train_ratio, split_by, rng)
            if test_writer is not None and not test_pos and positions:
                # 划分单位只有一个（按要素划分时就是只有一个面），切不出测试集。
                # 不说一声的话，这个类别在测试集里就凭空消失了。
                report.warnings.append(
                    "「%s」只有 %d 个%s，不足以划分，已全部作为训练样本。"
                    % (unit.name, features if split_by == "feature" else len(positions),
                       "要素" if split_by == "feature" else "像元"))
            written, outside = train_writer.write(train_pos, unit.label,
                                                  [key_of[p] for p in train_pos])
            unit.train, unit.outside = written, outside + off_image
            if test_writer is not None:
                written, outside = test_writer.write(test_pos, unit.label,
                                                     [key_of[p] for p in test_pos])
                unit.test = written
                unit.outside += outside

            if unit.train or unit.test:
                report.units.append(unit)
                next_label += 1
            else:
                report.warnings.append(
                    "「%s」没有裁出任何样本块，未参与标签编号。" % unit.name
                    + ("（%d 个位置全落在影像范围外）" % unit.outside if unit.outside else ""))

            if progress is not None and not progress(done, len(targets), unit.name):
                report.cancelled = True
                break

    except BaseException:
        train_writer.abort()
        if test_writer is not None:
            test_writer.abort()
        raise

    if report.cancelled:
        # 已写出的块留着（各自完整），但清单、h5 属性与 labels.json 都标成未完成：
        # 一份少了几类的样本集看起来与完整的一模一样，是最容易被拿去训练的那种
        # 错误。
        report.warnings.insert(0, "已取消：只裁了 %d/%d 类，输出目录标记为未完成。"
                               % (len(report.units), len(targets)))
    train_writer.finish(image_path, report.units, complete=not report.cancelled)
    if test_writer is not None:
        test_writer.finish(image_path, report.units, complete=not report.cancelled)
        if test_writer.count == 0 and train_writer.count:
            # 整个测试集一块都没有。逐个类别的"不足以划分"提示可能都出现过，但
            # 使用者未必意识到合起来的结果是"测试集是空的"——直接拿去训练，读到
            # 的会是一句莫名其妙的"样本集是空的"。
            report.warnings.append(
                "测试集一个样本块都没有，%s 是空文件。"
                % ("test_dataset.h5" if output_format == "h5" else "test_dataset/")
                + "要么把训练集比例调低，要么改成按像元划分，要么把比例设成 1.00 "
                  "只出训练集。")
    _write_labels(report)
    ds = None
    return report


def class_table(units: Sequence[ClipUnit]) -> List[dict]:
    """标签号 → 类别的对照表，labels.json 与 h5 属性 ``class_table`` 用的同一份。

    与导出 shp 时写的 labels.json 同构（label / name / code / path），训练、预测、
    栅格转矢量都只认这一种格式。
    """
    return [{"label": u.label, "name": u.name, "code": u.code, "path": u.path,
             "class_ids": u.class_ids, "color": u.color, "features": u.features,
             "train": u.train, "test": u.test}
            for u in units]


def _write_labels(report: ClipReport) -> str:
    """标签对照表。没有它，训练出来的模型没人说得清 3 号类到底是什么岩性。"""
    doc = {
        "image": report.image,
        "complete": not report.cancelled,
        "patch_size": report.patch_size,
        "format": report.output_format,
        "split_by": report.split_by,
        "train_ratio": report.train_ratio,
        "buffer_radius_m": report.buffer_radius,
        "labels": class_table(report.units),
    }
    path = os.path.join(report.out_dir, "labels.json")
    os.makedirs(report.out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    return path
