from __future__ import annotations

from pathlib import Path

import pytest

from toolbox.core import ToolError
from toolbox.denoise.product import (contiguous_groups, discover_ahsi_products,
                                     ensure_output_safe, parse_ahsi_product,
                                     parse_zy1e_product, scan_product_bands)
from tests.fixtures.zy1e_product_factory import (create_ahsi_product,
                                                 create_zy1e_product)


def test_parse_and_scan_uses_pixels_not_fixed_sensor_table(tmp_path: Path) -> None:
    root = create_zy1e_product(
        tmp_path, vn_constants={3: 0, 19: 41}, sw_constants={2: 9, 47: 0, 88: 12})
    product = parse_zy1e_product(root)
    assert (product.rows, product.cols) == (6, 5)
    assert product.part("VN").bands == 76
    assert product.part("SW").bands == 90

    scan = scan_product_bands(product)
    assert scan["VN"]["frozen_bands"] == [3, 19]
    assert scan["SW"]["frozen_bands"] == [2, 47, 88]
    assert scan["VN"]["valid_groups"] == [list(range(1, 3)), list(range(4, 19)),
                                            list(range(20, 77))]


def test_contiguous_groups() -> None:
    assert contiguous_groups([1, 2, 4, 5, 8]) == ((1, 2), (4, 5), (8,))
    assert contiguous_groups([]) == ()


def test_zy1f_tif_productlevel_and_h1b_variant(tmp_path: Path) -> None:
    root = create_ahsi_product(
        tmp_path, satellite="ZY1F", extension=".tif", level_tag="ProductLevel",
        variant="H1B", vn_constants={}, sw_constants={})
    product = parse_ahsi_product(root)
    assert product.satellite == "ZY1F"
    assert product.level == "LEVEL1A"
    assert product.variant == "H1B"
    assert product.part("VN").raster.suffix == ".tif"
    assert scan_product_bands(product)["SW"]["frozen_bands"] == []


def test_discover_mixed_zy1e_zy1f_products(tmp_path: Path) -> None:
    create_zy1e_product(tmp_path, name="ZY1E_ONE")
    create_ahsi_product(tmp_path, name="ZY1F_TWO", satellite="ZY1F",
                        extension=".tif", level_tag="ProductLevel", variant="H1B")
    products = discover_ahsi_products(tmp_path)
    assert [product.satellite for product in products] == ["ZY1E", "ZY1F"]


def test_missing_rpc_is_rejected(tmp_path: Path) -> None:
    root = create_zy1e_product(tmp_path)
    product = parse_zy1e_product(root)
    # 移除外置 RPB，并新建一个没有内嵌 RPC 的替代 TIFF。
    part = product.part("VN")
    part.rpb.unlink()
    from osgeo import gdal
    replacement = root / "replacement.tiff"
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(str(replacement), product.cols, product.rows, 76, gdal.GDT_UInt16)
    ds = None
    part.raster.unlink()
    replacement.rename(part.raster)
    with pytest.raises(ToolError, match="RPC"):
        parse_zy1e_product(root)


def test_wrong_level_and_shape_are_rejected(tmp_path: Path) -> None:
    wrong_level = create_zy1e_product(tmp_path, name="WRONG_LEVEL", level="LEVEL1B")
    with pytest.raises(ToolError):
        parse_zy1e_product(wrong_level)

    wrong_shape = create_zy1e_product(tmp_path, name="WRONG_SHAPE")
    xml = wrong_shape / "WRONG_SHAPE.xml"
    xml.write_text(xml.read_text(encoding="utf-8").replace(
        "<LinesInPixels>6</LinesInPixels>", "<LinesInPixels>7</LinesInPixels>"),
        encoding="utf-8")
    with pytest.raises(ToolError, match="尺寸"):
        parse_zy1e_product(wrong_shape)


def test_output_inside_input_is_rejected(tmp_path: Path) -> None:
    source = create_zy1e_product(tmp_path)
    with pytest.raises(ToolError, match="输入产品内部"):
        ensure_output_safe(source, source / "denoised")
