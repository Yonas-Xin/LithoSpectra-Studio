from __future__ import annotations

import os
from pathlib import Path

import pytest

from toolbox.denoise.product import parse_ahsi_product, scan_product_bands


_REAL_PRODUCT_VALUE = os.environ.get("ZY1F_HYDE_TEST_PRODUCT")
REAL_PRODUCT = Path(_REAL_PRODUCT_VALUE) if _REAL_PRODUCT_VALUE else None


@pytest.mark.integration
@pytest.mark.skipif(REAL_PRODUCT is None or not REAL_PRODUCT.is_dir(),
                    reason="设置 ZY1F_HYDE_TEST_PRODUCT 后运行真实产品合同检查")
def test_real_zy1f_h1b_product_contract() -> None:
    assert REAL_PRODUCT is not None
    product = parse_ahsi_product(REAL_PRODUCT)
    assert product.satellite == "ZY1F"
    assert product.level == "LEVEL1A"
    assert product.variant == "H1B"
    scan = scan_product_bands(product)
    assert scan["VN"]["frozen_bands"] == []
    assert scan["SW"]["frozen_bands"] == []
