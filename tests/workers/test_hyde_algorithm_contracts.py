from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from workers.hyde_denoise.algorithms import (AlgorithmError, load_model_entry,
                                             algorithm_catalog, normalize_by_band,
                                             primary_output, run_algorithm,
                                             validate_algorithm)
from workers.hyde_denoise.inputs import (build_algorithm_inputs,
                                         build_raw_validity_mask)
from workers.hyde_denoise.quality import (clip_and_quantize, difference_summary,
                                         reference_quality)


def cube() -> np.ndarray:
    yy, xx, bb = np.indices((8, 7, 4))
    return (100 + yy * 2 + xx + bb * 20).astype(np.float32)


def test_normalization_and_backend_contract_round_trip() -> None:
    image = cube()
    output, info = run_algorithm(
        image, "FastHyDe", {"noise_type": "additive", "iid": True, "k_subspace": 3},
        "cpu", backend=lambda normalized: normalized)
    np.testing.assert_allclose(output, image, rtol=0, atol=1e-5)
    assert output.dtype == np.float32
    assert info["device"] == "test-backend"


def test_tuple_primary_output_and_shape_nan_guards() -> None:
    image = cube()
    output, _ = run_algorithm(
        image, "FastHyDe", {"noise_type": "additive", "iid": True, "k_subspace": 2},
        "cpu", backend=lambda normalized: (normalized, "diagnostic"))
    assert output.shape == image.shape
    assert primary_output((1, 2)) == 1
    with pytest.raises(AlgorithmError, match="形状"):
        run_algorithm(image, "FastHyDe",
                      {"noise_type": "additive", "iid": True, "k_subspace": 2},
                      "cpu", backend=lambda normalized: normalized[..., :2])
    with pytest.raises(AlgorithmError, match="NaN/Inf"):
        run_algorithm(image, "FastHyDe",
                      {"noise_type": "additive", "iid": True, "k_subspace": 2},
                      "cpu", backend=lambda normalized: normalized * np.nan)


def test_constant_band_and_invalid_params_are_rejected() -> None:
    image = cube()
    image[:, :, 2] = 7
    with pytest.raises(AlgorithmError, match="常量波段"):
        normalize_by_band(image)
    with pytest.raises(AlgorithmError, match="k_subspace"):
        validate_algorithm("FastHyDe", {"k_subspace": 5}, bands=4)
    with pytest.raises(AlgorithmError, match="未知算法"):
        validate_algorithm("NotAnAlgorithm", {}, bands=4)
    with pytest.raises(AlgorithmError, match="mask"):
        run_algorithm(cube(), "FastHyIe",
                      {"noise_type": "additive", "iid": True, "k_subspace": 2},
                      "cpu",
                      backend=lambda normalized: normalized)


def test_catalog_exposes_validated_contracts_and_mask_requirements() -> None:
    catalog = {item["name"]: item for item in algorithm_catalog()}
    # 九个算法都已在真实 ZY1E 数据上跑通，status 统一为 available。
    # 这里仍然逐个断言，是为了让"某个算法被悄悄改回未验证状态"这件事会响。
    for name in ("FastHyDe", "FastHyIe", "L1HyMixDe", "HyMiNoR", "HyRes",
                 "WSRRR", "OTVCA", "FORPDN_SURE", "QRNN3D"):
        assert catalog[name]["status"] == "available"
    assert catalog["FastHyIe"]["requires_mask"] is True
    assert catalog["WSRRR"]["tuple_output"] is True
    assert catalog["WSRRR"]["primary_output"] == "denoised"
    assert catalog["FORPDN_SURE"]["tuple_output"] is False


@pytest.mark.parametrize(("name", "params"), [
    ("FastHyDe", {"noise_type": "additive", "iid": False, "k_subspace": 3}),
    ("L1HyMixDe", {"k_subspace": 3, "p": 0.05, "max_iter": 2}),
    ("HyMiNoR", {"lam": 10.0, "iterations": 2}),
    ("HyRes", {"min_iter": 2}),
    ("WSRRR", {"rank": 2}),
    ("OTVCA", {"features": 2, "num_itt": 2, "lam": 0.01}),
    ("FORPDN_SURE", {"domain": "wavelet", "scale": False,
                      "scale_const": 255.0, "wavelet_level": 6}),
    ("QRNN3D", {}),
])
def test_every_exposed_algorithm_has_same_hwc_contract(name: str, params: dict) -> None:
    image = cube()
    def backend(normalized):
        if name == "WSRRR":
            return normalized, normalized[..., :3]
        if name == "OTVCA":
            return normalized, normalized[..., :2]
        return normalized

    output, info = run_algorithm(
        image, name, params, "cpu",
        backend=backend)
    np.testing.assert_allclose(output, image, rtol=0, atol=1e-5)
    assert output.shape == image.shape
    assert info["algorithm"] == name
    if name in {"WSRRR", "OTVCA"}:
        selection = info["output_selection"]
        assert selection["primary_output"] == "denoised"
        assert selection["returned_output_count"] == 2
        assert len(selection["discarded_outputs"]) == 1


def test_fasthyie_mask_builder_and_masked_normalization_contract() -> None:
    image = cube().astype(np.uint16)
    image[0, 0, 1] = 0
    mask, summary = build_raw_validity_mask(
        image, (None,) * image.shape[2], policy="nodata_zero",
        minimum_observed_bands=3)
    assert mask.shape == image.shape
    assert mask[0, 0, 1] == 0
    assert summary["meaning"] == "1=viable,0=missing"
    output, info = run_algorithm(
        image, "FastHyIe",
        {"noise_type": "additive", "iid": True, "k_subspace": 3},
        "cpu", mask=mask,
        backend=lambda normalized: normalized)
    assert output.shape == image.shape
    assert info["algorithm"] == "FastHyIe"


def test_mask_builder_rejects_under_observed_pixels() -> None:
    image = cube().astype(np.uint16)
    image[0, 0, :3] = 0
    with pytest.raises(AlgorithmError, match="少于 k_subspace"):
        build_raw_validity_mask(image, (None,) * 4, policy="nodata_zero",
                                minimum_observed_bands=2)


def test_mask_builder_rejects_unknown_writeback_policy() -> None:
    with pytest.raises(AlgorithmError, match="writeback"):
        build_algorithm_inputs(
            cube(), (None,) * 4,
            {"mask": {"builder": "raw_validity", "policy": "nodata",
                      "writeback": "guess"}}, k_subspace=2)


def test_safe_model_registry_checks_hash(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    weight = models / "weights.pth"
    weight.write_bytes(b"safe-fixture")
    import hashlib
    digest = hashlib.sha256(weight.read_bytes()).hexdigest()
    registry = tmp_path / "models.json"
    registry.write_text(json.dumps({"models": {"fixture": {
        "architecture": "qrnn3d", "filename": "weights.pth",
        "sha256": digest, "training_noise": "test"}}}), encoding="utf-8")
    assert load_model_entry("fixture", registry)["actual_sha256"] == digest
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["models"]["fixture"]["sha256"] = "0" * 64
    registry.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AlgorithmError, match="SHA-256"):
        load_model_entry("fixture", registry)


def test_quantization_and_quality_summary() -> None:
    source = cube().astype(np.uint16)
    denoised = source.astype(np.float32) + 0.6
    quantized = clip_and_quantize(denoised, source, "dtype_range")
    assert quantized.dtype == np.uint16
    summary = difference_summary(source, quantized)
    assert summary["changed_fraction"] == 1.0
    assert summary["mean_absolute_change"] == 1.0
    quality = reference_quality(source.astype(np.float32),
                                quantized.astype(np.float32), data_range=1000)
    assert quality["psnr_db"] == 60.0
    assert quality["sam_degrees"] >= 0
