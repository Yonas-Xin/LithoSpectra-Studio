# -*- coding: utf-8 -*-
"""训练—模型包—整景预测全链路，在合成数据上跑（CPU 上几秒钟）。

合成影像分三块"岩性"，每块一条特征光谱加噪声，左上角一片 NoData。样本集从
同一幅影像上按样本库裁剪的规则切块，写成与裁剪模块同构的 h5。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import torch

import litho_dl  # noqa: F401
from litho_dl import devices
from litho_dl.jobs import read_json
from litho_dl.models import adapt, center_index
from litho_dl.normalize import Normalizer
from litho_dl.package import load_classifier, read_manifest
from litho_dl.predict import read_block
from litho_dl.tasks import create_job, run_job

gdal = pytest.importorskip("osgeo.gdal")
h5py = pytest.importorskip("h5py")
from osgeo import ogr, osr  # noqa: E402

gdal.UseExceptions()
BANDS, ROWS, COLS, PATCH = 12, 90, 110, 5
NODATA = 0
GT = (500000.0, 30.0, 0.0, 4000000.0, 0.0, -30.0)
NAMES = ["花岗岩", "玄武岩", "砂岩"]


def truth_map() -> np.ndarray:
    yy, xx = np.mgrid[0:ROWS, 0:COLS]
    lab = np.where(xx < 40, 0, np.where(yy < 45, 1, 2)).astype(np.int64)
    return lab


@pytest.fixture(scope="module")
def scene(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("litho_dl")
    rng = np.random.default_rng(0)
    signatures = rng.uniform(1500, 5000, size=(3, BANDS))
    lab = truth_map()
    cube = signatures[lab].transpose(2, 0, 1) + rng.normal(0, 120, (BANDS, ROWS, COLS))
    cube = cube.astype(np.int16)
    cube[:, :12, :15] = NODATA
    image = root / "scene.tif"
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32650)
    ds = gdal.GetDriverByName("GTiff").Create(str(image), COLS, ROWS, BANDS, gdal.GDT_Int16)
    ds.SetGeoTransform(GT)
    ds.SetProjection(srs.ExportToWkt())
    ds.WriteArray(cube)
    for b in range(1, BANDS + 1):
        ds.GetRasterBand(b).SetNoDataValue(NODATA)
    ds = None

    # 按裁剪规则切样本：中心像元避开 NoData 区
    ds = gdal.Open(str(image))
    valid = np.argwhere(np.ones((ROWS, COLS), bool))
    valid = valid[~((valid[:, 0] < 12) & (valid[:, 1] < 15))]
    pick = valid[rng.choice(len(valid), size=900, replace=False)]
    patches = np.stack([read_block(ds, r, c, 1, 1, PATCH) for r, c in pick])
    labels = lab[pick[:, 0], pick[:, 1]].astype(np.int32)
    table = [{"label": i, "name": n, "code": "", "path": n, "class_ids": [i + 1]}
             for i, n in enumerate(NAMES)]
    out = root / "clip"
    out.mkdir()
    for name, sl in (("train_dataset.h5", slice(0, 700)), ("test_dataset.h5", slice(700, 900))):
        with h5py.File(out / name, "w") as fh:
            fh.create_dataset("patches", data=patches[sl])
            fh.create_dataset("labels", data=labels[sl])
            fh.create_dataset("centers", data=pick[sl].astype(np.int32))
            fh.attrs["count"] = int(labels[sl].size)
            fh.attrs["complete"] = True
            fh.attrs["patch_size"] = PATCH
            fh.attrs["class_table"] = json.dumps(table, ensure_ascii=False)
            fh.attrs["source_image_path"] = str(image)
    return {"root": root, "image": image, "clip": out, "truth": lab, "cube": cube}


def _train(scene, backbone: str, name: str, **extra) -> Path:
    params = {"dataset": str(scene["clip"]), "backbone": backbone, "epochs": 4,
              "batch_size": 64, "lr": 2e-3, "warmup_epochs": 0, "device": "cpu",
              "model_name": name, "seed": 1, **extra}
    return create_job(scene["root"] / "jobs", "train_classifier", params)


@pytest.fixture(scope="module")
def models(scene) -> dict:
    out = {}
    for backbone in ("1D-CNN", "2D-CNN"):
        job = _train(scene, backbone, backbone)
        assert run_job(job, echo=False) == 0, (job / "status.json").read_text()
        out[backbone] = {"job": job, "model": job / "model" / (backbone + ".lithomodel")}
    return out


def _events(job: Path) -> list[dict]:
    return [json.loads(line) for line in (job / "events.ndjson").read_text("utf-8").splitlines()]


# =============================================================================
# 训练
# =============================================================================

@pytest.mark.parametrize("backbone", ["1D-CNN", "2D-CNN"])
def test_training_converges_and_packages(models, backbone):
    job = models[backbone]["job"]
    status = read_json(job / "status.json")
    assert status["state"] == "succeeded"
    events = _events(job)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    kinds = {e["event"] for e in events}
    assert {"started", "estimate", "epoch", "evaluation", "artifact", "completed"} <= kinds
    test_eval = [e for e in events if e["event"] == "evaluation" and e["data"]["split"] == "test"]
    assert test_eval and test_eval[-1]["data"]["oa"] > 0.9

    manifest = read_manifest(models[backbone]["model"])
    assert manifest["bands"] == BANDS and manifest["patch_size"] == PATCH
    assert [c["name"] for c in manifest["classes"]] == NAMES
    assert all(c["color"].startswith("#") for c in manifest["classes"])
    assert manifest["normalization"]["scale"] == pytest.approx(1e-4)
    assert (job / "eval" / "test.npz").is_file()
    assert (job / "eval" / "val_best.npz").is_file()
    history = read_json(job / "metrics.json")["history"]
    assert len(history) == 4


def test_validation_split_keeps_groups_apart(scene):
    from litho_dl.data import read_rows, split_validation
    rows = read_rows(scene["clip"] / "train_dataset.h5", fields=("labels", "centers"))
    tr, va, _ = split_validation(rows["labels"], rows["centers"], None, 0.2, 0, PATCH)
    assert len(set(tr) & set(va)) == 0 and len(tr) + len(va) == rows["labels"].size
    cell = max(32, PATCH * 4)
    key = lambda idx: {tuple(c) for c in (rows["centers"][idx] // cell)}  # noqa: E731
    for label in range(3):
        sel_tr = tr[rows["labels"][tr] == label]
        sel_va = va[rows["labels"][va] == label]
        assert not (key(sel_tr) & key(sel_va))


def test_training_cancel_then_resume(scene):
    job = _train(scene, "1D-CNN", "resume")

    def cancel_after_first_epoch(event):
        if event["event"] == "epoch" and event["data"]["epoch"] == 1:
            (job / "cancel").touch()

    assert run_job(job, echo=False, listeners=[cancel_after_first_epoch]) == 2
    assert read_json(job / "status.json")["state"] == "cancelled"
    manifest = read_manifest(job / "model" / "resume.lithomodel")
    assert manifest["partial"] is True
    assert run_job(job, echo=False) == 0
    history = read_json(job / "metrics.json")["history"]
    assert [h["epoch"] for h in history] == [1, 2, 3, 4]


# =============================================================================
# 预测
# =============================================================================

def naive_predict(model_dir: Path, cube: np.ndarray) -> np.ndarray:
    """逐像元朴素实现：切块、归一化、fp32 前向。流水线的结果必须与它逐像元一致。"""
    model, manifest = load_classifier(model_dir, "cpu")
    norm = Normalizer.from_json(manifest["normalization"])
    lead = center_index(PATCH)
    padded = np.zeros((BANDS, ROWS + PATCH - 1, COLS + PATCH - 1), np.float32)
    padded[:, lead:lead + ROWS, lead:lead + COLS] = cube
    out = np.full((ROWS, COLS), 255, np.uint8)
    coords = [(r, c) for r in range(ROWS) for c in range(COLS)
              if not np.all(cube[:, r, c] == NODATA)]
    with torch.no_grad():
        for k in range(0, len(coords), 2048):
            chunk = coords[k:k + 2048]
            x = torch.from_numpy(np.stack([padded[:, r:r + PATCH, c:c + PATCH]
                                           for r, c in chunk]))
            x = norm.apply(x, band_axis=1)
            pred = model(adapt(x, manifest["input_kind"])).argmax(1).numpy()
            for (r, c), v in zip(chunk, pred):
                out[r, c] = v
    return out


def _predict(scene, model: Path, **extra) -> Path:
    params = {"image": str(scene["image"]), "model": str(model), "device": "cpu",
              "precision": "fp32", "block_size": 64, **extra}
    return create_job(scene["root"] / "jobs", "predict", params)


def _read(path: Path) -> np.ndarray:
    return gdal.Open(str(path)).ReadAsArray()


@pytest.mark.parametrize("backbone", ["1D-CNN", "2D-CNN"])
def test_prediction_matches_naive_and_is_georeferenced(scene, models, backbone):
    job = _predict(scene, models[backbone]["model"])
    assert run_job(job, echo=False) == 0, (job / "status.json").read_text()
    result = read_json(job / "status.json")
    assert result["state"] == "succeeded"
    out_path = next((job / "outputs").glob("*__%s.tif" % backbone))
    pred = _read(out_path)
    expected = naive_predict(models[backbone]["model"], scene["cube"])
    np.testing.assert_array_equal(pred, expected)

    truth = scene["truth"]
    valid = pred != 255
    assert (pred[valid] == truth[valid]).mean() > 0.9
    assert not valid[:12, :15].any()

    ds = gdal.Open(str(out_path))
    assert ds.GetGeoTransform() == GT
    band = ds.GetRasterBand(1)
    assert band.GetNoDataValue() == 255
    assert list(band.GetCategoryNames())[:3] == NAMES
    assert band.GetColorTable() is not None
    conf = _read(Path(str(out_path)[:-4] + "_conf.tif"))
    assert ((conf[valid] > 0) & (conf[valid] <= 1.0001)).all()
    assert (conf[~valid] == -1).all()
    assert not list((job / "outputs").glob("*.partial.tif"))
    assert (out_path.with_suffix(".classes.json")).is_file()


def test_roi_mask_sparse_blocks_match_naive(scene, models):
    """范围只覆盖一小块：大多数块走"只取有效像元"的分支，结果仍与朴素实现一致。"""
    roi = scene["root"] / "roi.geojson"
    x0, y0 = GT[0] + 50 * GT[1], GT[3] + 30 * GT[5]
    x1, y1 = GT[0] + 70 * GT[1], GT[3] + 75 * GT[5]
    roi.write_text(json.dumps({"type": "FeatureCollection", "crs": {
        "type": "name", "properties": {"name": "EPSG:32650"}}, "features": [{
            "type": "Feature", "properties": {}, "geometry": {
                "type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1],
                                                    [x0, y1], [x0, y0]]]}}]}), "utf-8")
    model = models["2D-CNN"]["model"]
    job = _predict(scene, model, mask=str(roi), confidence="none")
    assert run_job(job, echo=False) == 0, (job / "status.json").read_text()
    pred = _read(next((job / "outputs").glob("*.tif")))
    expected = naive_predict(model, scene["cube"])
    inside = pred != 255
    assert 0 < inside.sum() < pred.size / 4
    np.testing.assert_array_equal(pred[inside], expected[inside])
    rows, cols = np.nonzero(inside)
    assert rows.min() >= 29 and rows.max() <= 75 and cols.min() >= 49 and cols.max() <= 70


def test_prediction_cancel_then_resume(scene, models):
    model = models["2D-CNN"]["model"]
    job = _predict(scene, model, block_size=32)

    def cancel_after_first_block(event):
        if event["event"] == "block_done":
            (job / "cancel").touch()

    assert run_job(job, echo=False, listeners=[cancel_after_first_block]) == 2
    assert read_json(job / "status.json")["state"] == "cancelled"
    assert list((job / "outputs").glob("*.partial.tif"))
    done = np.fromfile(job / "blocks.done", dtype=np.uint8)
    assert 0 < done.sum() < done.size

    first_done = int(done.sum())
    blocks_second = []
    assert run_job(job, echo=False, listeners=[
        lambda e: blocks_second.append(e) if e["event"] == "block_done" else None]) == 0
    assert len(blocks_second) == done.size - first_done
    pred = _read(next((job / "outputs").glob("*__2D-CNN.tif")))
    np.testing.assert_array_equal(pred, naive_predict(model, scene["cube"]))


def test_band_mismatch_is_refused(scene, models, tmp_path):
    other = tmp_path / "few_bands.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(other), 20, 20, 3, gdal.GDT_Int16)
    ds.SetGeoTransform(GT)
    ds = None
    job = _predict(scene, models["1D-CNN"]["model"], image=str(other))
    assert run_job(job, echo=False) == 1
    assert "波段" in read_json(job / "status.json")["message"]


@pytest.mark.skipif(not devices.mps_available() and not torch.cuda.is_available(),
                    reason="没有 GPU")
def test_gpu_half_precision_agrees_with_fp32(scene, models):
    model = models["2D-CNN"]["model"]
    job = _predict(scene, model, device="auto", precision="auto")
    assert run_job(job, echo=False) == 0, (job / "status.json").read_text()
    pred = _read(next((job / "outputs").glob("*__2D-CNN.tif")))
    expected = naive_predict(model, scene["cube"])
    valid = expected != 255
    assert (pred == expected)[valid].mean() > 0.995
    logs = [e["message"] for e in _events(job) if e["event"] in ("log", "warning")]
    assert any("精度" in m for m in logs)


def test_command_line_runs_job_dir(scene, models):
    """界面启动 Worker 的方式：子进程 + PYTHONPATH，事件写进文件也打到标准输出。"""
    import os
    import subprocess
    import sys
    workers = Path(litho_dl.__file__).resolve().parents[1]
    job = _predict(scene, models["2D-CNN"]["model"], confidence="none")
    env = dict(os.environ, PYTHONPATH=str(workers))
    proc = subprocess.run([sys.executable, "-m", "litho_dl", "run", str(job)], env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines[-1]["event"] == "completed"
    assert lines == _events(job)


# =============================================================================
# 对比学习、迁移、特征分析、相似区域
# =============================================================================

@pytest.fixture(scope="module")
def backbone_pkg(scene) -> Path:
    job = create_job(scene["root"] / "jobs", "train_contrastive", {
        "source": "image", "image": str(scene["image"]), "samples": 2000, "patch_size": PATCH,
        "eval_dataset": str(scene["clip"]), "backbone": "2D-CNN", "epochs": 3,
        "batch_size": 128, "knn_every": 1, "device": "cpu", "model_name": "bb", "warmup_epochs": 0})
    assert run_job(job, echo=False) == 0, (job / "status.json").read_text()
    return job


def test_contrastive_from_image(backbone_pkg):
    job = backbone_pkg
    m = read_manifest(job / "model" / "bb.lithomodel")
    assert m["task"] == "backbone" and m["bands"] == BANDS and m["patch_size"] == PATCH
    hist = read_json(job / "metrics.json")["history"]
    assert len(hist) == 3 and all("knn_acc" in h for h in hist)
    assert max(h["knn_acc"] for h in hist) > 0.8
    with h5py.File(job / "unlabeled.h5", "r") as fh:
        assert int(fh.attrs["count"]) == 2000
        c = fh["centers"][:]
        assert not ((c[:, 0] < 12) & (c[:, 1] < 15)).any(), "NoData 区不该被采到"
    state = torch.load(job / "model" / "bb.lithomodel" / "weights.pt", weights_only=True)
    assert state and all(k.startswith("encoder.") for k in state)


def test_moco_on_dataset(scene):
    job = create_job(scene["root"] / "jobs", "train_contrastive", {
        "source": "dataset", "dataset": str(scene["clip"]), "method": "moco", "queue_size": 512,
        "backbone": "1D-CNN", "epochs": 2, "batch_size": 128, "device": "cpu",
        "model_name": "moco1d"})
    assert run_job(job, echo=False) == 0, (job / "status.json").read_text()
    assert read_manifest(job / "model" / "moco1d.lithomodel")["method"] == "moco"


@pytest.mark.parametrize("transfer", ["freeze", "lowlr"])
def test_transfer_from_backbone(scene, backbone_pkg, transfer):
    pkg = backbone_pkg / "model" / "bb.lithomodel"
    job = _train(scene, "2D-CNN", "tr_" + transfer, pretrained=str(pkg), transfer=transfer)
    assert run_job(job, echo=False) == 0, (job / "status.json").read_text()
    m = read_manifest(job / "model" / ("tr_%s.lithomodel" % transfer))
    assert m["pretrained"]["transfer"] == transfer
    before = torch.load(pkg / "weights.pt", weights_only=True)
    after = torch.load(job / "model" / ("tr_%s.lithomodel" % transfer) / "weights.pt", weights_only=True)
    same = all(torch.equal(before[k], after[k]) for k in before if "num_batches" not in k)
    assert same == (transfer == "freeze"), "冻结时骨干一点不动，微调时要变"


def test_transfer_rejects_other_backbone(scene, backbone_pkg):
    job = _train(scene, "1D-CNN", "bad", pretrained=str(backbone_pkg / "model" / "bb.lithomodel"))
    assert run_job(job, echo=False) == 1
    assert "不一致" in read_json(job / "status.json")["message"]


def test_embedding_suspicion(scene, models):
    job = create_job(scene["root"] / "jobs", "embed", {
        "model": str(models["2D-CNN"]["model"]), "dataset": str(scene["clip"]),
        "device": "cpu"})
    assert run_job(job, echo=False) == 0, (job / "status.json").read_text()
    data = np.load(job / "eval" / "embedding.npz")
    assert data["xy"].shape == (900, 2)
    assert data["suspicion"].mean() < 0.1
    summary = read_json(job / "eval" / "embedding.json")
    assert [c["name"] for c in summary["classes"]] == NAMES


def test_similarity_search(scene, backbone_pkg):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32650)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(srs, wgs)
    x, y = GT[0] + 90.5 * GT[1], GT[3] + 70.5 * GT[5]          # 砂岩区（标签 2）
    lon, lat, _ = tr.TransformPoint(x, y)
    job = create_job(scene["root"] / "jobs", "similarity", {
        "image": str(scene["image"]), "model": str(backbone_pkg / "model" / "bb.lithomodel"),
        "query": "%.8f,%.8f" % (lon, lat), "device": "cpu", "block_size": 64})
    assert run_job(job, echo=False) == 0, (job / "status.json").read_text()
    sim = _read(next((job / "outputs").glob("*_value.tif")))
    truth = scene["truth"]
    valid = sim != -1
    same = sim[valid & (truth == 2)].mean()
    other = sim[valid & (truth != 2)].mean()
    assert same > other + 0.2, (same, other)
    ds = gdal.Open(str(next(p for p in (job / "outputs").glob("*_similarity.tif"))))
    assert ds.GetRasterBand(1).GetColorTable() is not None
