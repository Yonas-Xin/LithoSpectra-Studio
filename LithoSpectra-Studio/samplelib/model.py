# -*- coding: utf-8 -*-
"""样本库数据模型。

模块内不依赖 GDAL，便于界面层与存储层解耦：存储层负责把 OGR 几何转换成
这里定义的纯 Python 结构，界面层只消费这些结构。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .geosymbols import fold

# 几何类别常量
GEOM_POINT = "point"
GEOM_POLYGON = "polygon"

# 样本主键：(几何类别, 要素 FID)。点与面分属两个图层，各自 FID 独立，
# 因此必须带上几何类别才能唯一定位一个样本。
SampleKey = Tuple[str, int]


def key_to_str(key: SampleKey) -> str:
    """样本主键 -> 字符串，用于树控件 iid 等场景。"""
    return "%s:%d" % (key[0], key[1])


def str_to_key(text: str) -> SampleKey:
    """字符串 -> 样本主键。"""
    kind, fid = text.split(":")
    return kind, int(fid)


@dataclass
class SampleClass:
    """一个样本类别（对应旧格式中的一个 shp 文件）。"""

    class_id: int
    code: str = ""                       # 原始类别号，如 "0"、"10"
    name: str = ""                       # 类别名，如 "第四纪"
    region: str = ""                     # 工区 / 来源分组，如 "青海都兰"
    sample_type: str = ""                # 样本类型，如 lithology、alteration
    geom_kind: str = GEOM_POINT          # point / polygon
    color: str = "#3b82f6"               # 画布显示颜色
    description: str = ""
    parent_id: Optional[int] = None      # 预留：支持岩类 -> 岩性的多级分类
    source: str = ""                     # 导入来源文件名
    native_crs: str = ""                 # 导入时的原始坐标系，导出可还原
    attrs: Dict[str, Any] = field(default_factory=dict)   # 分类键值对，如 {"岩性大": "火山岩"}
    origin: Dict[str, Any] = field(default_factory=dict)  # 导入时的原始状态，用于恢复
    count: int = 0                       # 本类直接持有的样本数，运行时统计
    total_count: int = 0                 # 含子类别的样本总数，运行时统计

    @property
    def display_name(self) -> str:
        """给人看的名字。``code`` 不进来：它只是这棵树的排序键，见 session。"""
        return self.name or "未命名"

    # 类别树上只有两种节点：**分组**是层级容器，自身不存样本；**基础类别**是树的
    # 末端，直接持有样本，只装一种几何（点或面），不再往下分。往基础类别里拖
    # 别的类别，得到的是"升一级"——建一个分组把两者都装进去，而不是让基础类别
    # 同时持有样本和子类别。
    GROUP = "group"

    @property
    def is_group(self) -> bool:
        return self.geom_kind == self.GROUP

    @property
    def is_leaf(self) -> bool:
        """基础类别：不是分组就是它。几何种类可以还没定（``geom_kind`` 为空，
        新建后还一条样本都没画时），第一条样本落进去时定。"""
        return not self.is_group

    @property
    def kind_label(self) -> str:
        return {GEOM_POINT: "点", GEOM_POLYGON: "面", self.GROUP: "分组",
                "mixed": "点+面"}.get(self.geom_kind, "未定")

    @property
    def edited(self) -> bool:
        """是否相对导入时的状态被改动过。"""
        if not self.origin:
            return False
        if self.origin.get("user_created"):
            return True
        return (self.origin.get("name") != self.name
                or self.origin.get("code") != self.code
                or self.origin.get("region") != self.region
                or self.origin.get("parent_id") != self.parent_id)

    def matches(self, keyword: str) -> bool:
        """类别级检索。

        除类别名 / 类别号 / 工区 / 样本类型 外，按属性导入的类别还会匹配
        其分类键值对，因此可以直接用「火山岩」「奥陶纪」这类属性值检索。
        """
        if not keyword:
            return True
        # 上下标折回普通字符再比：搜 T3 要能找到 T₃，搜 Qhal 找到 Qhᵃˡ
        kw = fold(keyword.strip()).lower()
        fields = [self.name, self.region, self.sample_type,
                  self.description, self.source]
        for key, value in (self.attrs or {}).items():
            fields.append(str(key))
            fields.append("" if value is None else str(value))
        return any(kw in fold(f or "").lower() for f in fields)


@dataclass
class SampleRecord:
    """一条样本记录，几何已统一转换为 EPSG:4326 的纯 Python 坐标。"""

    key: SampleKey
    class_id: int
    geom_kind: str
    # point   : [(lon, lat)]
    # polygon : [[(lon, lat), ...], ...]  每个元素是一个环（外环或内环）
    coords: Any
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    source: str = ""
    label: str = ""                       # 使用者给这条样本起的名字，可为空
    attrs: Dict[str, Any] = field(default_factory=dict)
    # 存储层按需挂上的 OGR 几何副本（``iter_records(geometry=True)``），导出与
    # 裁剪直接用它，省掉"转成坐标再按主键回库取一次几何"的往返。界面层拿到的
    # 记录里它一律是 None，本模块仍不依赖 GDAL。
    geom: Any = None

    @property
    def display_name(self) -> str:
        kind = "点" if self.geom_kind == GEOM_POINT else "面"
        return self.label or "%s #%d" % (kind, self.key[1])

    def matches(self, keyword: str) -> bool:
        """样本级检索：自定义名称与来源文件。"""
        if not keyword:
            return True
        kw = fold(keyword.strip()).lower()
        return (kw in fold(self.label or "").lower()
                or kw in fold(self.source or "").lower())


@dataclass
class ImportReport:
    """一次导入操作的结果汇总。"""

    library: str = ""
    region: str = ""
    files: int = 0
    classes: int = 0
    samples: int = 0
    skipped: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "导入完成：工区「%s」" % (self.region or "-"),
            "  矢量文件 %d 个 -> 类别 %d 个，样本 %d 条" % (self.files, self.classes, self.samples),
        ]
        if self.skipped:
            lines.append("  跳过 %d 个文件：%s" % (len(self.skipped), "、".join(self.skipped[:5])))
        if self.warnings:
            lines.append("  警告 %d 条：" % len(self.warnings))
            lines.extend("    " + w for w in self.warnings[:8])
        return "\n".join(lines)


@dataclass
class ExportReport:
    """一次导出操作的结果汇总。"""

    out_dir: str = ""
    driver: str = ""
    files: List[str] = field(default_factory=list)
    samples: int = 0
    warnings: List[str] = field(default_factory=list)
    checked: str = ""                     # 覆盖校验结论

    def summary(self) -> str:
        lines = [
            "导出完成：%s" % self.out_dir,
            "  格式 %s，文件 %d 个，样本 %d 条" % (self.driver, len(self.files), self.samples),
        ]
        if self.checked:
            lines.append("  " + self.checked)
        if self.warnings:
            lines.extend("  " + w for w in self.warnings[:8])
        return "\n".join(lines)
