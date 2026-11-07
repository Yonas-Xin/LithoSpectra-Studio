from __future__ import annotations

import json
from pathlib import Path

import pytest
from osgeo import gdal

from toolbox import run_batch
from toolbox.core import ToolError
from toolbox.denoise.product import parse_zy1e_product, scan_product_bands
from toolbox.denoise.runner import WorkerResult
from toolbox.tools.hyde_denoise_product import TOOL
from tests.fixtures.zy1e_product_factory import create_zy1e_product


def test_discover_output_companions_and_dry_run(tmp_path: Path) -> None:
    source = create_zy1e_product(tmp_path / "inputs")
    params = TOOL.resolve_params()
    items = TOOL.discover(source, params, lambda _: None)
    assert len(items) == 1
    out = TOOL.output_path(items[0], tmp_path / "outputs", params)
    assert out.name.endswith("__HYDE_FASTHYDE")
    assert TOOL.companions(out)[0].name.endswith(".processing.json")
    report = run_batch(TOOL, source, tmp_path / "outputs", dry_run=True,
                       log=lambda _: None)
    assert len(report.skipped) == 1
    assert not (tmp_path / "outputs").exists()


def test_worker_request_maps_dynamic_groups(tmp_path: Path) -> None:
    source = create_zy1e_product(tmp_path, vn_constants={5: 0}, sw_constants={40: 7})
    product = parse_zy1e_product(source)
    scan = scan_product_bands(product)
    params = TOOL.resolve_params()
    request = TOOL._request(product, tmp_path / "stage", scan, params)
    assert request.rasters[0].frozen_bands == (5,)
    assert request.rasters[0].valid_groups[0] == (1, 2, 3, 4)
    assert request.rasters[1].frozen_bands == (40,)
    assert request.algorithm_params["iid"] is False


@pytest.mark.parametrize(("algorithm", "expected"), [
    ("L1HyMixDe", {"k_subspace", "p", "max_iter"}),
    ("HyMiNoR", {"lam", "iterations"}),
    ("HyRes", {"min_iter"}),
    ("WSRRR", {"rank"}),
    ("OTVCA", {"features", "num_itt", "lam"}),
    ("FORPDN_SURE", {"domain", "scale", "scale_const", "wavelet_level"}),
    ("QRNN3D", set()),
])
def test_experimental_algorithm_parameter_mapping(algorithm: str,
                                                  expected: set[str]) -> None:
    params = TOOL.resolve_params({"algorithm": algorithm})
    assert set(TOOL._algorithm_params(params)) == expected


def test_fasthyie_is_exposed_with_raw_mask_builder(tmp_path: Path) -> None:
    source = create_zy1e_product(tmp_path)
    product = parse_zy1e_product(source)
    scan = scan_product_bands(product)
    params = TOOL.resolve_params({
        "algorithm": "FastHyIe",
        "mask_policy": "nodata_zero",
    })
    request = TOOL._request(product, tmp_path / "stage", scan, params)
    assert "FastHyIe" in TOOL.params[0].choices
    assert request.auxiliary_inputs == {"mask": {
        "builder": "raw_validity",
        "policy": "nodata_zero",
        "meaning": "1=viable,0=missing",
        "writeback": "preserve_source_nodata",
    }}
    assert set(request.algorithm_params) == {"noise_type", "iid", "k_subspace"}


def test_batch_rejects_output_inside_source_before_writing(tmp_path: Path) -> None:
    source = create_zy1e_product(tmp_path)
    with pytest.raises(ToolError, match="输入产品内部"):
        run_batch(TOOL, source, source / "nested-output", dry_run=True,
                  log=lambda _: None)
    assert not (source / "nested-output").exists()


def test_full_tool_transaction_with_fake_worker(tmp_path: Path, monkeypatch) -> None:
    source = create_zy1e_product(tmp_path / "inputs", vn_constants={7: 0},
                                sw_constants={22: 0, 50: 5})

    class FakeRunner:
        def __init__(self, python_executable, worker_script):
            pass

        def run(self, request, on_event=None, should_cancel=None, timeout_seconds=None):
            for task in request.rasters:
                path = Path(request.staging_product) / task.relative_raster
                dataset = gdal.Open(str(path), gdal.GA_Update)
                # 只改每个有效组的首个波段，模拟 Worker 的合法原位更新。
                for group in task.valid_groups:
                    band = dataset.GetRasterBand(group[0])
                    array = band.ReadAsArray()
                    band.WriteArray(array + 1)
                    band.FlushCache()
                dataset = None
            return WorkerResult({"fixture": True}, (), 0.01)

    monkeypatch.setattr("toolbox.tools.hyde_denoise_product.WorkerRunner", FakeRunner)
    output_dir = tmp_path / "outputs"
    report = run_batch(
        TOOL, source, output_dir,
        params={},
        log=lambda _: None)
    assert report.ok and len(report.completed) == 1
    output = report.completed[0].output
    manifest_path = TOOL.companions(output)[0]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["verification"]["frozen_band_pixels_equal"] is True
    assert manifest["band_detection"]["parts"]["SW"]["frozen_bands"] == [22, 50]
    assert parse_zy1e_product(output).root == output.resolve()
