from __future__ import annotations

import os
from pathlib import Path

import pytest

from toolbox.denoise.product import parse_zy1e_product, scan_product_bands


# 真实产品检查会精确扫描 166 个完整波段，默认不进入快速单元测试。
_REAL_PRODUCT_VALUE = os.environ.get("ZY1E_HYDE_TEST_PRODUCT")
REAL_PRODUCT = Path(_REAL_PRODUCT_VALUE) if _REAL_PRODUCT_VALUE else None


@pytest.mark.integration
@pytest.mark.skipif(REAL_PRODUCT is None or not REAL_PRODUCT.is_dir(),
                    reason="设置 ZY1E_HYDE_TEST_PRODUCT 后运行真实产品合同检查")
def test_real_zy1e_product_contract_and_expected_constant_bands() -> None:
    assert REAL_PRODUCT is not None
    product = parse_zy1e_product(REAL_PRODUCT)
    scan = scan_product_bands(product)
    assert scan["VN"]["frozen_bands"] == []
    assert scan["SW"]["frozen_bands"] == list(range(22, 26)) + list(range(50, 56))
