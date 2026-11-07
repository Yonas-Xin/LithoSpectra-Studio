# -*- coding: utf-8 -*-
"""深度学习模块：样本库 → 裁剪 → 登记样本集 → 选环境 → 训练 → 错分样本定位 →
预测实时上图 → 换成正式成果 → 疑难区；任务管理的排队、取消、崩溃与重启恢复。

训练与预测要一个装了 torch 的 Python（默认找 conda 的 geo 环境，或用环境变量
LITHO_DL_TEST_PYTHON 指定）；找不到时只跑不需要 torch 的那部分。直接运行本文件。
"""

# =============================================================================
# 配置
# =============================================================================

import os

CANDIDATE_PYTHONS = [
    os.environ.get("LITHO_DL_TEST_PYTHON", ""),
    "/opt/homebrew/Caskroom/miniconda/base/envs/geo/bin/python",
    os.path.expanduser("~/miniconda3/envs/geo/bin/python"),
    os.path.expanduser("~/miniconda3/envs/geo/python.exe"),
]
JOB_TIMEOUT = 300            # 秒

# =============================================================================

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi.testclient import TestClient
except ImportError:
    print("跳过：没有装 fastapi / httpx。")
    raise SystemExit(0)

import numpy as np
from osgeo import gdal, ogr, osr

from samplelib import appstate
from samplelib.modules.deeplearn import jobs as J
from samplelib.modules.deeplearn import runtime
from samplelib.session import LibrarySession

gdal.UseExceptions()
WORK = Path(tempfile.mkdtemp(prefix="sldl_"))
FAILED = []

# 隔离：任务根、运行环境缓存、会话状态都放进临时目录，不碰使用者的数据
runtime.dl_dir = lambda: (WORK / "dl").mkdir(exist_ok=True) or (WORK / "dl")
appstate.state_path = lambda: str(WORK / "session.json")

from webapi import create_app  # noqa: E402  隔离之后再建应用


def check(cond, what):
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        FAILED.append(what)


def wait_task(c, task):
    for _ in range(2000):
        t = c.get("/api/tasks/%s" % task["id"]).json()
        if t["status"] != "running":
            return t
        time.sleep(0.05)
    raise TimeoutError(task["title"])


def wait_job(c, job_id, states=("succeeded", "failed", "cancelled")):
    deadline = time.time() + JOB_TIMEOUT
    while time.time() < deadline:
        j = c.get("/api/dl/jobs/%s" % job_id).json()
        if j["state"] in states:
            return j
        time.sleep(0.3)
    raise TimeoutError(job_id)


# =============================================================================
# 测试数据：一幅三种"岩性"的影像 + 对应的样本库
# =============================================================================

ROWS, COLS, BANDS = 120, 150, 10
GT = (110.0, 0.0002, 0.0, 40.0, 0.0, -0.0002)


def truth():
    yy, xx = np.mgrid[0:ROWS, 0:COLS]
    return np.where(xx < 50, 0, np.where(yy < 60, 1, 2))


def make_image() -> str:
    rng = np.random.default_rng(0)
    sig = rng.uniform(1500, 5000, size=(3, BANDS))
    cube = (sig[truth()].transpose(2, 0, 1) + rng.normal(0, 100, (BANDS, ROWS, COLS)))
    cube = cube.astype(np.int16)
    cube[:, :8, :8] = 0
    path = str(WORK / "scene.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, COLS, ROWS, BANDS, gdal.GDT_Int16)
    ds.SetGeoTransform(GT)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.WriteArray(cube)
    for b in range(1, BANDS + 1):
        ds.GetRasterBand(b).SetNoDataValue(0)
    ds = None
    return path


def lonlat(row, col):
    return GT[0] + (col + 0.5) * GT[1], GT[3] + (row + 0.5) * GT[5]


def square(r0, c0, r1, c1):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for r, c in ((r0, c0), (r0, c1), (r1, c1), (r1, c0), (r0, c0)):
        x, y = GT[0] + c * GT[1], GT[3] + r * GT[5]
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


def make_library() -> tuple:
    path = str(WORK / "lib.gpkg")
    s = LibrarySession()
    s.create(path, name="dl")
    st = s.store
    ids = []
    boxes = {0: [(15, 5, 35, 20), (70, 10, 95, 30), (100, 20, 115, 40)],
             1: [(10, 70, 30, 95), (35, 110, 55, 140), (12, 120, 28, 145)],
             2: [(70, 70, 90, 95), (95, 100, 115, 130), (65, 120, 85, 145)]}
    for k, name in enumerate(["花岗岩", "玄武岩", "砂岩"]):
        cid = st.add_class(name, code=str(k + 1), region="测试区", geom_kind="polygon")
        st.add_samples(cid, [square(*b) for b in boxes[k]])
        ids.append(cid)
    s.close()
    return path, ids


def find_python() -> str:
    for p in CANDIDATE_PYTHONS:
        if p and os.path.isfile(p):
            return p
    return ""


# =============================================================================
# 用例
# =============================================================================

def test_manager_without_torch() -> None:
    """不需要 torch：排队、取消、崩溃记成失败、重启后识别中断。"""
    print("任务管理（假 Worker）")
    root = WORK / "fake_jobs"
    root.mkdir()
    py = sys.executable
    crash = [py, "-c", "import sys, time; time.sleep(0.3); sys.exit(134)"]
    m = J.JobManager(root_fn=lambda: root, python_fn=lambda: py,
                     command_fn=lambda _py, _d: crash)
    a = m.create("train_classifier", {"epochs": 1}, "崩溃的任务")
    b = m.create("train_classifier", {"epochs": 1}, "排队的任务")
    check(m.summary(b)["state"] == "queued", "第二个任务排队")
    m.cancel(b)
    check(m.summary(b)["state"] == "cancelled", "排队中的任务可以直接取消")
    for _ in range(100):
        m.pump()
        if m.summary(a)["state"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    sa = m.summary(a)
    check(sa["state"] == "failed" and "134" in sa["message"],
          "Worker 崩溃（134）记成失败并带出退出码：%s" % sa["message"].splitlines()[0])

    # 模拟"上次退出时任务还在跑"：status 写着 running，进程号已不在
    c = m.create("predict", {}, "中断的任务", start=False)
    st_path = root / c / "status.json"
    st = json.loads(st_path.read_text("utf-8"))
    st.update(state="running", pid=999999)
    st_path.write_text(json.dumps(st), "utf-8")
    fresh = J.JobManager(root_fn=lambda: root, python_fn=lambda: py)
    check(fresh.summary(c)["state"] == "interrupted", "重启后进程不在的任务显示为中断")
    check(len(fresh.list()) == 3, "重启后任务列表从目录恢复")
    fresh.delete(c)
    check(not (root / c).exists(), "删除任务连目录一起删")


def test_pipeline(python: str) -> None:
    image = make_image()
    lib, class_ids = make_library()
    app = create_app(library_path=lib, remember=False)
    c = TestClient(app)

    print("裁剪并登记样本集")
    r = c.post("/api/layers/add", json={"paths": [image]}).json()
    added = wait_task(c, r["task"])["result"]["added"]
    raster_id = added[0]["id"]
    out = str(WORK / "clip")
    r = c.post("/api/clip", json={"image_path": image, "out_dir": out, "patch_size": 5,
                                  "output_format": "h5", "train_ratio": 0.7,
                                  "only_selected": False, "level": "leaf"}).json()
    t = wait_task(c, r["task"])
    check(t["status"] == "done", "裁剪完成：%s" % (t.get("result") or {}).get("summary", t.get("error")))
    import h5py
    with h5py.File(os.path.join(out, "train_dataset.h5"), "r") as fh:
        keys = [k.decode() if isinstance(k, bytes) else k for k in fh["sample_keys"][:5]]
        table = json.loads(fh.attrs["class_table"])
    check(all(k.startswith("polygon:") for k in keys), "h5 记下了每块的样本主键：%s" % keys[:2])
    check(all(row["color"].startswith("#") for row in table), "类别表带样本库的颜色")

    r = c.post("/api/dl/datasets", json={"path": out, "name": "测试样本集"})
    check(r.status_code == 200, "登记样本集")
    ds = r.json()
    check(ds["count"] > 100 and ds["test_count"] > 0 and ds["stale"] is False,
          "样本集描述：训练 %d、测试 %d、未过期" % (ds["count"], ds["test_count"]))

    if not python:
        print("  - 找不到装了 torch 的 Python，跳过训练与预测")
        return

    print("运行环境")
    r = c.post("/api/dl/envs/select", json={"python": python})
    check(r.status_code == 200 and r.json()["report"]["ok"], "体检通过并选中 %s" % python)
    caps = c.get("/api/dl/capabilities").json()
    names = {t["name"] for t in caps["tasks"]}
    check({"train_classifier", "predict"} <= names, "Worker 声明了任务：%s" % sorted(names))
    bad = c.post("/api/dl/jobs", json={"task": "train_classifier",
                                        "params": {"dataset": out, "epochs": 0}})
    check(bad.status_code == 400 and "不能小于" in bad.json()["detail"], "参数越界当场报错")

    print("训练")
    job = c.post("/api/dl/jobs", json={"task": "train_classifier", "title": "测试训练", "params": {
        "dataset": out, "backbone": "2D-CNN", "epochs": 4, "batch_size": 64,
        "lr": 2e-3, "warmup_epochs": 0, "device": "cpu", "model_name": "t2d"}}).json()
    j = wait_job(c, job["id"])
    check(j["state"] == "succeeded", "训练完成：%s" % j["message"])
    check(len(j["epochs"]) == 4 and "val" in j["evaluations"], "监控数据：逐轮指标与验证集混淆矩阵")
    check("batch" in j and j["batch"] is None, "训练结束后不再报轮内进度")
    events = (Path(j["dir"]) / "events.ndjson").read_text("utf-8").splitlines()
    beats = [json.loads(x) for x in events if '"progress"' in x]
    check(all({"done", "total", "epoch"} <= set(b["data"]) for b in beats if b["data"].get("unit") == "batch"),
          "轮内进度事件带批数与轮次（%d 条）" % len(beats))
    test_eval = j["evaluations"].get("test", {})
    check(test_eval.get("oa", 0) > 0.85, "测试集 OA %.3f" % test_eval.get("oa", 0))
    model_path = next(a["path"] for a in j["artifacts"] if a["kind"] == "model")
    models = c.get("/api/dl/models").json()
    check(any(m["path"] == model_path for m in models), "模型库里有这个模型")
    m = next(m for m in models if m["path"] == model_path)
    check([x["name"] for x in m["classes"]] == ["花岗岩", "玄武岩", "砂岩"], "模型类别名来自样本库")

    rows = c.get("/api/dl/jobs/%s/samples" % job["id"],
                 params={"split": "test", "errors_only": False, "limit": 5}).json()["rows"]
    check(rows and all("lonlat" in x and x.get("in_library") for x in rows),
          "评估样本能定位到经纬度并对上样本库里的样本")
    ll = rows[0]["lonlat"]
    check(110 < ll[0] < 110.03 and 39.97 < ll[1] < 40, "定位坐标落在影像内：%s" % ll)

    compat = c.get("/api/dl/models/compat", params={"path": model_path, "layer_id": raster_id}).json()
    check(compat["ok"], "模型与画布影像兼容：%s" % compat["text"])

    print("预测与实时图层")
    pjob = c.post("/api/dl/jobs", json={"task": "predict", "params": {
        "image": image, "model": model_path, "device": "cpu", "block_size": 32}}).json()
    live = None
    for _ in range(400):
        r = c.post("/api/dl/jobs/%s/layer" % pjob["id"])
        if r.status_code == 200:
            live = r.json()
            break
        time.sleep(0.05)
    check(live is not None, "预测开始后可以把它放上画布")
    j = wait_job(c, pjob["id"])
    check(j["state"] == "succeeded", "预测完成：%s" % j["message"])
    lid = live["layer"]["id"]
    png = c.get("/api/layers/%d/wms" % lid, params={
        "BBOX": "110,39.976,110.03,40", "WIDTH": 300, "HEIGHT": 240, "SRS": "EPSG:4326"})
    check(png.status_code == 200 and png.content[:4] == b"\x89PNG", "图层出图正常")
    for _ in range(50):
        layers = {l["id"]: l for l in c.get("/api/layers").json()}
        if layers[lid]["path"]:
            break
        time.sleep(0.1)
    check(layers[lid]["path"].endswith(".tif") and layers[lid]["has_color_table"],
          "实时图层原地换成了正式分类图（图层号不变，带颜色表）")
    lon, lat = lonlat(80, 100)            # 砂岩区
    px = c.get("/api/dl/pixel", params={"layer_id": lid, "lon": lon, "lat": lat}).json()
    check(px.get("class") == "砂岩" and 0 < px.get("confidence", 0) <= 1,
          "点查分类图：%s，置信度 %s" % (px.get("class"), px.get("confidence")))
    r = c.post("/api/dl/jobs/%s/low-confidence" % pjob["id"], json={"threshold": 0.999})
    check(r.status_code == 200 and r.json()["layer"]["kind"] == "vector",
          "低置信度区域转成疑难区图层（%s 个面）" % r.json().get("features"))

    print("对比学习、迁移、特征分析、相似检索")
    cj = c.post("/api/dl/jobs", json={"task": "train_contrastive", "params": {
        "source": "image", "image": image, "samples": 1500, "patch_size": 5, "eval_dataset": out,
        "backbone": "2D-CNN", "epochs": 2, "batch_size": 128, "knn_every": 1,
        "device": "cpu", "model_name": "bb"}}).json()
    j = wait_job(c, cj["id"])
    check(j["state"] == "succeeded" and any("knn_acc" in e for e in j["epochs"]),
          "对比学习完成，逐轮有 kNN 精度：%s" % j["message"])
    bb = next(a["path"] for a in j["artifacts"] if a["kind"] == "model")
    kinds = {m["path"]: m["task"] for m in c.get("/api/dl/models").json()}
    check(kinds.get(bb) == "backbone", "模型库里有这个骨干")
    tj = c.post("/api/dl/jobs", json={"task": "train_classifier", "params": {
        "dataset": out, "backbone": "2D-CNN", "pretrained": bb, "transfer": "lowlr",
        "epochs": 2, "batch_size": 64, "device": "cpu", "model_name": "from_bb"}}).json()
    j = wait_job(c, tj["id"])
    check(j["state"] == "succeeded", "用骨干初始化的分类训练完成：%s" % j["message"])
    ej = c.post("/api/dl/jobs", json={"task": "embed", "params": {"model": bb, "dataset": out,
                                                                 "device": "cpu"}}).json()
    j = wait_job(c, ej["id"])
    emb = c.get("/api/dl/jobs/%s/embedding" % ej["id"]).json()
    check(j["state"] == "succeeded" and len(emb["points"]) == 4200 and emb["in_library"] == 4200
          and all("ll" in p for p in emb["points"][:5]),
          "特征散点 %d 个点，都能对上样本库与经纬度" % len(emb.get("points", [])))
    lon, lat = lonlat(80, 100)
    sj = c.post("/api/dl/jobs", json={"task": "similarity", "params": {
        "model": bb, "image": image, "query": "%f,%f" % (lon, lat), "device": "cpu"}}).json()
    j = wait_job(c, sj["id"])
    check(j["state"] == "succeeded", "相似检索完成：%s" % j["message"])
    r = c.post("/api/dl/jobs/%s/layer" % sj["id"]).json()
    check(r["layer"]["has_color_table"] and r["layer"]["path"].endswith("_similarity.tif"),
          "相似度图放上画布（带色带）")
    px = c.get("/api/dl/pixel", params={"layer_id": r["layer"]["id"], "lon": lon, "lat": lat}).json()
    check(px.get("similarity", 0) > 0.9, "参考点处相似度 %.3f" % px.get("similarity", 0))

    print("样本库变化后样本集标为过期")
    with app.state.lithospectra.lock:
        s = app.state.lithospectra.session
        s.store.add_samples(class_ids[0], [square(40, 5, 50, 15)])
        s.reload()
    ds = next(d for d in c.get("/api/dl/datasets").json() if d["path"] == os.path.abspath(out))
    check(ds["stale"] is True, "加了样本之后样本集显示来源已变化")


if __name__ == "__main__":
    test_manager_without_torch()
    test_pipeline(find_python())
    print()
    if FAILED:
        print("失败 %d 项：" % len(FAILED))
        for f in FAILED:
            print("  - " + f)
        raise SystemExit(1)
    print("全部通过")
