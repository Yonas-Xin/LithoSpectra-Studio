from __future__ import annotations

from pathlib import Path

import pytest
from osgeo import gdal

from toolbox.denoise.product import (ProductTransaction, band_pixel_hashes,
                                     parse_zy1e_product, raster_metadata_snapshot,
                                     verify_product_clone)
from tests.fixtures.zy1e_product_factory import create_zy1e_product


def test_transaction_preserves_non_core_files_metadata_and_frozen_pixels(tmp_path: Path) -> None:
    source_root = create_zy1e_product(tmp_path, sw_constants={22: 0, 50: 17})
    source = parse_zy1e_product(source_root)
    snapshots = {part.name: raster_metadata_snapshot(part.raster) for part in source.parts}
    frozen_before = band_pixel_hashes(source.part("SW").raster, [22, 50])
    target = tmp_path / "output" / "product_denoised"
    transaction = ProductTransaction(source.root, target)
    with transaction as staging:
        staging_product = parse_zy1e_product(staging)
        dataset = gdal.Open(str(staging_product.part("SW").raster), gdal.GA_Update)
        band = dataset.GetRasterBand(1)
        array = band.ReadAsArray()
        band.WriteArray(array + 1)
        band.FlushCache()
        dataset = None
        result = verify_product_clone(source, staging, snapshots)
        assert result["non_core_hashes_equal"]
        assert band_pixel_hashes(staging_product.part("SW").raster, [22, 50]) == frozen_before
        transaction.commit()
    assert target.is_dir()
    assert not target.with_name(target.name + ".partial").exists()


def test_transaction_failure_removes_staging_and_never_creates_final(tmp_path: Path) -> None:
    source_root = create_zy1e_product(tmp_path)
    target = tmp_path / "out" / "denoised"
    with pytest.raises(RuntimeError, match="stop"):
        with ProductTransaction(source_root, target):
            raise RuntimeError("stop")
    assert not target.exists()
    assert not target.with_name(target.name + ".partial").exists()
