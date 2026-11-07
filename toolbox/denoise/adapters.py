# -*- coding: utf-8 -*-
"""传感器可插拔的去噪产品发现层。

去噪核心只依赖 Adapter 暴露的产品根目录和 ``VN``/``SW`` 光谱资产，不依赖卫星
文件名或 XML 字段。新增 GF5 等传感器时注册一个 Adapter 即可，不需要修改 Worker
或算法注册表。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core import ToolError
from .product import (AHSIProduct, ProductPart, discover_ahsi_products, parse_ahsi_product,
                      verify_product_clone)


@runtime_checkable
class DenoiseProductView(Protocol):
    """去噪 Pipeline 从 HSIScene/产品 Adapter 所需的最小只读视图。"""

    root: Path
    satellite: str
    sensor: str
    level: str
    rows: int
    cols: int
    parts: tuple[ProductPart, ...]
    adapter_id: str

    @property
    def core_rasters(self) -> tuple[Path, ...]: ...

    def to_dict(self) -> dict[str, Any]: ...


@runtime_checkable
class DenoiseSensorAdapter(Protocol):
    """传感器产品到统一去噪资产的最小合同。"""

    id: str

    def detect(self, root: Path) -> bool:
        """快速判断目录是否可能由该 Adapter 负责。"""

    def open(self, root: Path) -> DenoiseProductView:
        """严格解析产品，并返回含 ``root/parts`` 的统一产品视图。"""

    def discover(self, root: Path) -> list[DenoiseProductView]:
        """发现目录本身或其子目录中的完整产品。"""

    def verify_clone(self, source: DenoiseProductView, clone_root: Path,
                     before_snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """验证去噪只改变了允许改变的核心像元。"""


@dataclass(frozen=True)
class ZYAHSIL1AAdapter:
    """ZY1E/ZY1F AHSI L1A Adapter。"""

    id: str = "zy_ahsi_l1a"

    def detect(self, root: Path) -> bool:
        root = Path(root)
        if not root.is_dir():
            return False
        names = [path.name.casefold() for path in root.iterdir() if path.is_file()]
        return (any(name.endswith(("_vn.tif", "_vn.tiff")) for name in names)
                and any(name.endswith(("_sw.tif", "_sw.tiff")) for name in names))

    def open(self, root: Path) -> AHSIProduct:
        return parse_ahsi_product(root)

    def discover(self, root: Path) -> list[AHSIProduct]:
        return discover_ahsi_products(root)

    def verify_clone(self, source: AHSIProduct, clone_root: Path,
                     before_snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return verify_product_clone(source, clone_root, before_snapshots)


class DenoiseSensorRegistry:
    """有序 Adapter 注册表；检测冲突会显式失败。"""

    def __init__(self) -> None:
        self._adapters: dict[str, DenoiseSensorAdapter] = {}

    def register(self, adapter: DenoiseSensorAdapter) -> None:
        if not isinstance(adapter, DenoiseSensorAdapter):
            raise TypeError("Adapter 未实现完整 DenoiseSensorAdapter 合同")
        if not adapter.id or adapter.id in self._adapters:
            raise ValueError("Adapter id 为空或重复：%r" % adapter.id)
        self._adapters[adapter.id] = adapter

    def adapter(self, adapter_id: str) -> DenoiseSensorAdapter:
        try:
            return self._adapters[adapter_id]
        except KeyError:
            raise ToolError("去噪 SensorRegistry 未注册 Adapter：%s" % adapter_id) from None

    def detect(self, root: Path) -> DenoiseSensorAdapter:
        matches = [adapter for adapter in self._adapters.values()
                   if adapter.detect(Path(root))]
        if not matches:
            raise ToolError("没有 Adapter 能识别高光谱产品：%s" % Path(root))
        if len(matches) > 1:
            raise ToolError("多个 Adapter 同时匹配产品：%s（%s）"
                            % (Path(root), ", ".join(item.id for item in matches)))
        return matches[0]

    def open(self, root: Path) -> DenoiseProductView:
        return self.detect(root).open(Path(root))

    def discover(self, root: Path) -> list[DenoiseProductView]:
        products: dict[Path, DenoiseProductView] = {}
        for adapter in self._adapters.values():
            for product in adapter.discover(Path(root)):
                resolved = Path(product.root).resolve()
                if resolved in products:
                    raise ToolError("多个 Adapter 发现了同一个产品：%s" % resolved)
                products[resolved] = product
        return [products[path] for path in sorted(products)]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._adapters)


def default_sensor_registry() -> DenoiseSensorRegistry:
    registry = DenoiseSensorRegistry()
    registry.register(ZYAHSIL1AAdapter())
    return registry


SENSOR_REGISTRY = default_sensor_registry()
