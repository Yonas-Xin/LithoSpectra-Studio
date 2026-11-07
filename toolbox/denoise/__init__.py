# -*- coding: utf-8 -*-
"""高光谱去噪的产品适配、Worker 协议与进程调度层。"""

from .product import (AHSIProduct, BandScan, ProductPart, ZY1EProduct,
                      parse_ahsi_product, parse_zy1e_product, scan_product_bands)
from .adapters import (DenoiseProductView, DenoiseSensorAdapter,
                       DenoiseSensorRegistry, SENSOR_REGISTRY, ZYAHSIL1AAdapter)
from .protocol import DenoiseRequest, RasterTask, WorkerEvent

__all__ = [
    "AHSIProduct",
    "BandScan",
    "DenoiseRequest",
    "DenoiseProductView",
    "DenoiseSensorAdapter",
    "DenoiseSensorRegistry",
    "ProductPart",
    "RasterTask",
    "WorkerEvent",
    "ZY1EProduct",
    "ZYAHSIL1AAdapter",
    "SENSOR_REGISTRY",
    "parse_zy1e_product",
    "parse_ahsi_product",
    "scan_product_bands",
]
