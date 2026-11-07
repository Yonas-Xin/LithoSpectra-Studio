# -*- coding: utf-8 -*-
"""样本库全局配置。

界面与逻辑层的可调参数集中在此，方便统一修改与后续打包。
"""

import colorsys
import json
import os
import stat
import sys
from collections import OrderedDict
from typing import Dict, List

from . import datadir

# =============================================================================
# 名称与图标
# =============================================================================

APP_NAME = "岩谱"
APP_NAME_EN = "LithoSpectra"
APP_SUBTITLE = "地质样本库"
APP_TITLE = "%s %s · %s" % (APP_NAME, APP_NAME_EN, APP_SUBTITLE)

# 是否运行在打包产物里（PyInstaller 等冻结环境）
FROZEN = datadir.FROZEN

# 随包只读资源的根目录。源码运行时各资源分散在仓库各处，打包后集中在这里：
# Windows 产物为 exe 同级的 resources\，macOS 在 .app 包内。
RESOURCE_DIR = datadir.resource_dir() if FROZEN else ""

# 图标等界面资源
ASSETS_DIR = (os.path.join(RESOURCE_DIR, "assets") if FROZEN else
              os.path.join(datadir.PROJECT_DIR, "assets"))


def app_data_dir() -> str:
    """可写的数据根目录：样本库、界面状态、缓存、深度学习任务都放这里。

    源码运行时是项目目录；Windows 打包版是 exe 同级的 userdata；macOS 打包版是
    ~/Documents/LithoSpectra。规则与旧数据迁移见 :mod:`samplelib.datadir`。
    """
    return datadir.data_dir()


def resource_path(source: str, *packed: str) -> str:
    """随包资源的位置：源码运行时为 ``source``，打包后为 ``RESOURCE_DIR`` 下的 ``packed``。"""
    return os.path.join(RESOURCE_DIR, *packed) if FROZEN else source


def seed_library() -> str:
    """随包携带的初始样本库；没有就返回空字符串。

    打包分发时把一份现成的样本库放进随包资源的 ``seed/``，首次启动复制到用户数据
    目录——否则新装的人打开只看到一个空库，界面上什么都没有。源码运行时包内
    没有这个目录，返回空，仍走「新建空库」那条路。
    """
    folder = os.path.join(RESOURCE_DIR, "seed") if FROZEN else ""
    if not folder or not os.path.isdir(folder):
        return ""
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(".gpkg"):
            return os.path.join(folder, name)
    return ""


def icon_path(size: int = 256) -> str:
    """取指定尺寸的图标路径，不存在时返回空字符串。"""
    path = os.path.join(ASSETS_DIR, "logo_%d.png" % size)
    return path if os.path.isfile(path) else ""


# =============================================================================
# 发行版
# =============================================================================

# 这份产物装哪些功能模块。取 samplelib/profiles/ 下的文件名（不含 .json）：
#   full      全功能版：在线底图 + 光谱 + 裁剪 + 工具箱
#   analysis  分析版：在线底图 + 光谱
#   basic     基础版：只有底座——样本库、导入导出、画布
# 填一个不存在的名字则装上包里能找到的全部模块。打包时 spec 读同一份 profile
# 决定带哪些代码与数据，没选中的模块连它的随包数据也不进包。
MODULE_PROFILE = "full"

# =============================================================================
# 存储配置
# =============================================================================

# 样本库统一存储坐标系。所有样本入库时投影到该坐标系，导出时再投影回目标坐标系。
STORAGE_EPSG = 4326

# 样本库文件扩展名与格式
LIBRARY_EXT = ".gpkg"
# 1.1：类别树分成分组与基础类别两种节点，有子类别的节点必须是分组
# （见 SampleStore.MIGRATIONS）
LIBRARY_FORMAT_VERSION = "1.2"

# 导出时可选的驱动：显示名 -> (OGR 驱动名, 扩展名)
EXPORT_DRIVERS = {
    "Shapefile (*.shp)": ("ESRI Shapefile", ".shp"),
    "GeoJSON (*.geojson)": ("GeoJSON", ".geojson"),
    "GeoPackage (*.gpkg)": ("GPKG", ".gpkg"),
}
DEFAULT_EXPORT_DRIVER = "Shapefile (*.shp)"

# 导出文件命名模板，可用字段：
#   {label}  本次导出的标签号，0 起、连续，与文件里 class 字段、
#            description.json 的 label_id 完全一致（默认用它）
#   {code}   库内的类别号，1 起、可能不连续，只在需要与库对照时用
#   {name} {region} {type} {date}
#   {parent} 上级类别名，{path} 完整层级路径（如 火山岩-T3bb2）
#
# 默认必须是 {label} 而不是 {code}：文件名上的号是使用者唯一看得见的编号，
# 它要是跟文件里的 class 字段对不上，人工核对时全乱套——库内号 19、23、26 的
# 三个类导出成三个文件，文件名会写成 19、23、26，而 class 字段写的是 0、1、2。
DEFAULT_EXPORT_TEMPLATE = "Label_{label}_{name}"

# 拖动调整类别顺序后，整个工区的排序键（sl_class.code）从这个数开始连续重编。
# 它只在后台记顺序，界面上不显示；导出与裁剪写进文件的标签号另算，永远从 0 起、
# 按本次实际选中的类别连续排（见 io_shp.export_classes / clip.clip_library）。
CLASS_CODE_BASE = 1

# =============================================================================
# 样本裁剪（生成深度学习训练用的影像块）
# =============================================================================

# 块大小（像素）。以样本位置为中心切方块，偶数时中心偏左上。13-30 较常用。
CLIP_PATCH_SIZE = 17

# 输出格式：
#   h5   全部样本进一个文件，形状/标签/类别名都存在文件里，便于拷贝与管理
#   bin  一个样本一个文件，体积小、读得快，但几十万个小文件不好管
#   tif  一个样本一个文件，带地理信息，方便逐块核对
CLIP_FORMAT = "bin"

# h5 输出的压缩方式：none / lzf / gzip / zstd。
# zstd 压缩率最好（高光谱块实测约 1.76 倍），但写和读**两边**都要装 hdf5plugin；
# 缺包时会自动退回不压缩并给出提示，不会写出别人读不开的文件。
# lzf 对高光谱几乎不压（实测省不到 1%），基本没有意义。
CLIP_H5_COMPRESSION = "none"

# 训练集比例。1.0 表示不划分，全部作为训练集。
CLIP_TRAIN_RATIO = 0.6

# 训练/测试怎么划分：
#   feature 按要素分——一个面整体归训练或测试，相邻像元不会横跨两侧
#   pixel   按像元分——与项目里 split_and_clip_dataset 的行为一致，但同一露头的
#           相邻像元会同时出现在训练与测试集，测试精度会偏乐观
CLIP_SPLIT_BY = "feature"

# 单个类别最多取多少个像素位置。一片大面能覆盖几十万像元，全裁出来既慢，
# 又让类别之间严重失衡。
CLIP_MAX_POSITIONS = 50000

# 随机划分与抽样的种子，固定它才能复现同一份数据集
CLIP_SEED = 42

# 块文件名前缀，最终形如 img_label0_1.bin
CLIP_PREFIX = "img"

# 导出与裁剪时点要素缓冲的默认半径（米）。0 表示默认不缓冲，对话框里勾上才做。
BUFFER_RADIUS_M = 30.0


# =============================================================================
# 显示配置
# =============================================================================

# 类别配色表。前 20 个是 tab20（沿用至今的配色，换掉会让老样本库的类别集体
# 变色），其后由 :func:`_spread_colors` 补到 CLASS_PALETTE_SIZE 个。
#
# 为什么不是把色轮均分 256 份：类别号相邻的两类在图上往往也挨着，均分之后相邻
# 序号的色相只差 1.4°，看着就是同一个颜色。这里按黄金角（≈137.5°）跳着取色相，
# 再让明度/饱和度四档轮换——无论截取哪一段，连着的几个颜色都拉得开。
CLASS_PALETTE_SIZE = 256

_TAB20 = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5",
    "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5",
]

_GOLDEN_ANGLE = 0.6180339887498949      # 1/φ，转成圈数就是黄金角


def _spread_colors(count: int, phase: float = 0.11) -> List[str]:
    """生成 count 个彼此拉得开的颜色。"""
    out: List[str] = []
    sats = (0.62, 0.88, 0.45, 0.74)
    vals = (0.86, 0.60, 0.95, 0.72)
    for i in range(max(0, count)):
        h = (phase + i * _GOLDEN_ANGLE) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, sats[i % 4], vals[i % 4])
        out.append("#%02x%02x%02x" % (int(r * 255 + 0.5), int(g * 255 + 0.5),
                                      int(b * 255 + 0.5)))
    return out


CLASS_PALETTE = _TAB20 + _spread_colors(CLASS_PALETTE_SIZE - len(_TAB20))

# 样本点半径（像素），矢量图层的点也按它画
POINT_RADIUS = 3

# =============================================================================
# 影像底图配置
# =============================================================================

# 影像拉伸分位数
RASTER_STRETCH_PERCENT = (2.0, 98.0)

# 波段数 >= 3 时默认使用的显示波段（1 基）；高光谱可在界面中另行指定
DEFAULT_RGB_BANDS = (1, 2, 3)

# =============================================================================
# 空间筛选配置
# =============================================================================

# 感兴趣区筛选模式：intersect（相交）/ within（完全包含）/ centroid（中心点在内）
DEFAULT_ROI_MODE = "intersect"

# 栅格 ROI 边界转经纬度时每条边的加密点数（应对投影弯曲）
RASTER_EDGE_DENSIFY = 25

# =============================================================================
# 导入配置
# =============================================================================

# 旧样本目录中可识别的矢量扩展名
IMPORT_VECTOR_EXTS = (".shp", ".geojson", ".json", ".gpkg")

# 导出目录的库描述文件名。
#
# 不带前导点：点开头的文件在资源管理器、Finder 和 ls 里默认都是隐藏的，
# 而这个文件恰恰是使用者最需要打开核对的东西——哪个标签号对应哪个类别全在里面。
# 早期版本写的是 ".description.json"，导入时两个名字都认，老目录不用动。
DESCRIPTION_JSON = "description.json"
DESCRIPTION_JSON_LEGACY = ".description.json"

# 裁剪样本块时写出的样本清单文件名（每行「路径 标签」）。同样不带前导点，
# 理由见上。Train.py 读的时候两个名字都认，老数据集不用重裁。
DATASETS_LIST = "datasets.txt"
DATASETS_LIST_LEGACY = ".datasets.txt"

# 标签号 → 类别的对照表。导出 shp 与裁剪样本块都写这一份，格式相同
# （labels: [{label, name, code, path, ...}]）；训练把它带进模型、预测把它写进
# 结果影像、栅格转矢量再把它变成属性表——整条链路只认这一种格式。
LABELS_JSON = "labels.json"

# Shapefile 属性表编码（读取旧数据时使用；留空表示交给 GDAL 自动判断）
SHAPE_ENCODING_READ = ""
# 导出 Shapefile 时写入的编码，UTF-8 可正确保存中文类别名（同时生成 .cpg）
SHAPE_ENCODING_WRITE = "UTF-8"


# 取不到类别色时的兜底，以及新建参考矢量图层的默认色。散在各处写死的话，
# 换配色时总会漏掉一两处，界面上就冒出一个来历不明的颜色。
DEFAULT_CLASS_COLOR = "#3b82f6"
DEFAULT_VECTOR_COLOR = "#f97316"


def class_color(index: int) -> str:
    """按序号取一个类别配色。"""
    return CLASS_PALETTE[index % len(CLASS_PALETTE)]


# =============================================================================
# 在线底图
# =============================================================================

# ---------------------------------------------------------------------------
# 底图服务密钥
# ---------------------------------------------------------------------------
# **密钥不写在代码里。** 它们是账号凭据：写进源码就意味着随仓库外泄、随每一份
# 打包产物发给每一个客户、想换一次得重新发版。按下面的顺序找，一个都找不到就
# 让相关底图在菜单里停用，并在界面上给出配置入口（底图 → 配置底图密钥…）。
#
#   1. 环境变量   TIANDITU_KEY / MAPBOX_TOKEN
#   2. 密钥文件   <app_data_dir>/keys.json，形如 {"MAPBOX_TOKEN": "pk...."}
#   3. 都没有 -> 空，该底图不可用
#
# 密钥文件放在可写的数据目录而不是包内：源码运行时是项目目录（已在 .gitignore
# 里），打包之后是使用者的数据目录（Windows 为 exe 旁的 userdata，macOS 为
# Documents/LithoSpectra）。每台机器各填各的，包本身
# 不带任何人的凭据。

KEY_FILE_NAME = "keys.json"

#: 每个密钥叫什么、上哪儿申请、长什么样。配置对话框照这份表生成界面。
KEY_INFO = OrderedDict((
    ("TIANDITU_KEY", {
        "label": "天地图",
        "apply": "https://console.tianditu.gov.cn",
        "hint": "免费申请，32 位十六进制字符串",
        "prefix": "",
    }),
    ("MAPBOX_TOKEN", {
        "label": "Mapbox",
        "apply": "https://account.mapbox.com",
        "hint": "public token，pk. 开头。免费额度按月计，瓦片有磁盘缓存，"
                "同一片只会下载一次",
        "prefix": "pk.",
    }),
))


def key_file() -> str:
    """密钥文件的位置。"""
    return os.path.join(app_data_dir(), KEY_FILE_NAME)


def load_keys() -> Dict[str, str]:
    """读密钥文件。文件不在、读不动、格式不对，一律当作没配过。

    这里不该抛异常：密钥没配好是最常见的初次使用状态，不是错误，程序要照常
    起来，只是那几个底图点不动。
    """
    try:
        with open(key_file(), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    return {k: str(v or "").strip() for k, v in doc.items() if k in KEY_INFO}


def read_key(name: str) -> str:
    """取一个密钥：环境变量优先，其次密钥文件。"""
    return (os.environ.get(name) or load_keys().get(name, "")).strip()


def save_key(name: str, value: str) -> None:
    """写一个密钥并让它立刻生效。传空表示清掉。

    先写临时文件再 rename：写到一半断电也不会留下一个解析不了的密钥文件，
    那会让所有底图一起失效。写完把权限收成只有本人可读写。
    """
    if name not in KEY_INFO:
        raise KeyError("没有这个密钥：%s" % name)
    keys = load_keys()
    value = (value or "").strip()
    if value:
        keys[name] = value
    else:
        keys.pop(name, None)
    path = key_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(keys, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:                       # Windows 上没有 POSIX 权限位
        pass
    # 模块级的那两个名字是 TileSource.key_name 查的目标，一并刷新
    globals()[name] = read_key(name)


def key_source(name: str) -> str:
    """这个密钥是从哪儿来的，给界面显示用。"""
    if os.environ.get(name):
        return "环境变量"
    if load_keys().get(name):
        return "密钥文件"
    return ""


# 天地图：https://console.tianditu.gov.cn
TIANDITU_KEY = read_key("TIANDITU_KEY")

# Mapbox：https://account.mapbox.com 的 public token
MAPBOX_TOKEN = read_key("MAPBOX_TOKEN")

# 瓦片磁盘缓存目录与上限（MB）。缓存按 底图/z/x/y 分层存放，删掉即可清空。
TILE_CACHE_DIR = os.path.join(app_data_dir(), "data", "tilecache")
TILE_CACHE_LIMIT_MB = 512

# 同时下载的瓦片数。过大反而会被服务端限流。
TILE_WORKERS = 6
TILE_TIMEOUT = 8.0

# 单次视窗最多取多少片。超出时自动降一个层级——底图糊一点，总好过一次向
# 服务端发上百个请求被限流，也免得高分屏拉大窗口时流量失控。
TILE_MAX_PER_VIEW = 48

# 取片失败后隔多久允许重试（秒）。限流、断网这类失败都是暂时的，永久拉黑会
# 让那一片整个会话都空着。
TILE_RETRY_AFTER = 30.0

# ---------------------------------------------------------------------------
# 内置底图
# ---------------------------------------------------------------------------
# kind 决定瓦片编号方案：
#   tianditu  经纬度切片（TILEMATRIXSET=c，EPSG:4490，与样本库同为经纬度）
#   xyz       Web 墨卡托切片（EPSG:3857），使用前需重投影
# key_name 指向本文件里存放密钥的变量名，留空表示该底图不需要密钥。
# overlay 表示这是压在别的底图之上的注记层，本身大面积透明。
_MAPBOX_STYLE = ("https://api.mapbox.com/styles/v1/mapbox/%s/tiles/256/"
                 "{z}/{x}/{y}?access_token={key}")
_MAPBOX_ATTR = "© Mapbox © OpenStreetMap"

BASEMAPS = OrderedDict((
    ("天地图 影像", {"kind": "tianditu", "layer": "img", "group": "天地图",
                     "key_name": "TIANDITU_KEY"}),
    ("天地图 影像注记", {"kind": "tianditu", "layer": "cia", "group": "天地图",
                         "key_name": "TIANDITU_KEY", "overlay": True}),
    ("天地图 矢量", {"kind": "tianditu", "layer": "vec", "group": "天地图",
                     "key_name": "TIANDITU_KEY"}),
    ("天地图 矢量注记", {"kind": "tianditu", "layer": "cva", "group": "天地图",
                         "key_name": "TIANDITU_KEY", "overlay": True}),
    ("天地图 地形", {"kind": "tianditu", "layer": "ter", "group": "天地图",
                     "key_name": "TIANDITU_KEY"}),

    # 卫星影像走 v4 纯栅格接口，比 Styles API 少一层样式合成，出图也更清晰
    ("Mapbox 卫星影像", {
        "kind": "xyz", "group": "Mapbox", "key_name": "MAPBOX_TOKEN",
        "url": "https://api.mapbox.com/v4/mapbox.satellite/"
               "{z}/{x}/{y}.jpg90?access_token={key}",
        "max_zoom": 20, "attribution": _MAPBOX_ATTR}),
    ("Mapbox 卫星+路网", {
        "kind": "xyz", "group": "Mapbox", "key_name": "MAPBOX_TOKEN",
        "url": _MAPBOX_STYLE % "satellite-streets-v12",
        "max_zoom": 20, "attribution": _MAPBOX_ATTR}),
    ("Mapbox 户外地形", {
        "kind": "xyz", "group": "Mapbox", "key_name": "MAPBOX_TOKEN",
        "url": _MAPBOX_STYLE % "outdoors-v12",
        "max_zoom": 20, "attribution": _MAPBOX_ATTR}),
    ("Mapbox 街道", {
        "kind": "xyz", "group": "Mapbox", "key_name": "MAPBOX_TOKEN",
        "url": _MAPBOX_STYLE % "streets-v12",
        "max_zoom": 20, "attribution": _MAPBOX_ATTR}),
    ("Mapbox 浅色", {
        "kind": "xyz", "group": "Mapbox", "key_name": "MAPBOX_TOKEN",
        "url": _MAPBOX_STYLE % "light-v11",
        "max_zoom": 20, "attribution": _MAPBOX_ATTR}),
    ("Mapbox 深色", {
        "kind": "xyz", "group": "Mapbox", "key_name": "MAPBOX_TOKEN",
        "url": _MAPBOX_STYLE % "dark-v11",
        "max_zoom": 20, "attribution": _MAPBOX_ATTR}),

    ("OpenStreetMap", {"kind": "xyz", "group": "其它", "key_name": "",
                       "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                       "max_zoom": 19,
                       "attribution": "© OpenStreetMap contributors"}),
))

# 请求瓦片时使用的 User-Agent。OSM 明确要求标识调用方，缺省会被拒。
TILE_USER_AGENT = "%s/%s (sample library annotator)" % (APP_NAME_EN,
                                                        LIBRARY_FORMAT_VERSION)

# =============================================================================
# 图层渲染
# =============================================================================

# 参考矢量面的默认填充不透明度（0-255）。默认半透明是为了看清底下的影像；
# 画岩性图这类"矢量本身就是主角"的场合，在图层面板把「填充」拉到 100 即可。
# 它与图层不透明度是相乘的关系：两个都拉满才是真正的不透明。
VECTOR_FILL_ALPHA = 70

# 画矢量前先按均匀网格索引筛一遍要素。一张岩性图动辄几万个图斑，每帧拿全部
# 要素挨个比外接矩形，光筛选就要几十毫秒。
VECTOR_INDEX_SIDE = 96           # 网格边长（格数），0 表示不建索引
VECTOR_INDEX_MIN_FEATURES = 2000  # 要素少于这个数就不值当建索引

# 波长表搜索目录。影像自身没有波长元数据时（ENVI 头里没有 wavelength），
# 从这些目录里找波段数吻合的传感器波长表供选择。
# 第二个目录留给使用者自己添表：源码运行时是仓库根的 assets；打包之后包内
# 是只读的临时目录，放不进去，因此改到用户数据目录下的 wavelengths。
if FROZEN:
    _USER_WAVELENGTH_DIR = os.path.join(app_data_dir(), "wavelengths")
else:
    _USER_WAVELENGTH_DIR = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "assets")

WAVELENGTH_DIRS = [
    ASSETS_DIR,
    _USER_WAVELENGTH_DIR,
]

# 拖拽加入图层时按扩展名分派。三类都认不出来的，会用 GDAL 依次试着当影像、
# 当矢量打开——ENVI 的裸数据文件常常没有扩展名，靠试探才认得出来。
DROP_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".gif")
DROP_RASTER_EXTS = (".dat", ".img", ".tif", ".tiff", ".bsq", ".bil", ".bip",
                    ".hdr", ".vrt", ".jp2", ".ers", ".pix", ".rst", ".grd")
DROP_VECTOR_EXTS = (".shp", ".geojson", ".json", ".gpkg", ".kml", ".kmz",
                    ".gml", ".dxf", ".tab", ".mif")
