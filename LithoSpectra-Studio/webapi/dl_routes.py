# -*- coding: utf-8 -*-
"""深度学习：运行环境、样本集、模型库、任务、实时预测图层、结果联动。

逻辑在 :mod:`samplelib.modules.deeplearn`；这里只把请求翻译过去。任务不走
:mod:`.tasks` 的模态长任务，也不占用样本库锁，训练期间界面照常可用。
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from samplelib.annotate import layers as L
from samplelib.modules.deeplearn import bridge, catalog, runtime
from samplelib.modules.deeplearn.jobs import SUCCEEDED, JobManager
from samplelib.modules.deeplearn.live import LivePredictionLayer


class PythonBody(BaseModel):
    python: str
    refresh: bool = False


class PathBody(BaseModel):
    path: str
    name: str = ""


class NameBody(BaseModel):
    name: str


class JobBody(BaseModel):
    task: str
    params: Dict[str, Any]
    title: str = ""


class LowConfBody(BaseModel):
    threshold: float = 0.6
    min_pixels: int = 4


def register(app, st, guard) -> None:
    s = st.session
    jobs = st.dl_jobs = JobManager()

    def bad(fn):
        try:
            return fn()
        except HTTPException:
            raise
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\""))
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc).strip("'\""))
        except OSError as exc:
            raise HTTPException(400, "%s：%s" % (exc.strerror or "文件操作失败", exc.filename or exc))

    # ------------------------------------------------------------- 环境
    @app.get("/api/dl/status")
    def dl_status():
        return {"available": runtime.available(), "python": runtime.selected_python(),
                "device": runtime.selected_device(),
                "jobs_root": str(runtime.jobs_root()), "running": jobs.running_id(),
                "plugin_dir": str(runtime.plugin_dir())}

    @app.get("/api/dl/envs")
    def dl_envs():
        return bad(runtime.environments)

    @app.post("/api/dl/envs/doctor")
    def dl_doctor(body: PythonBody):
        return bad(lambda: runtime.doctor(body.python, refresh=body.refresh))

    @app.post("/api/dl/envs/select")
    def dl_select(body: PythonBody):
        def run():
            report = runtime.doctor(body.python, refresh=body.refresh)
            if not report.get("ok"):
                raise ValueError("该环境无法运行深度学习任务，请查看体检结果")
            runtime.select_python(body.python)
            return {"python": body.python, "report": report}
        return bad(run)

    @app.get("/api/dl/capabilities")
    def dl_caps(refresh: bool = False):
        return bad(lambda: runtime.capabilities(refresh=refresh))

    @app.post("/api/dl/jobs-root")
    def dl_jobs_root(body: PathBody):
        return bad(lambda: {"jobs_root": str(runtime.set_jobs_root(body.path))})

    # ------------------------------------------------------------- 样本集
    def session_view():
        return s if s.is_open else None

    @app.get("/api/dl/datasets")
    def dl_datasets():
        with st.lock:
            return bad(lambda: catalog.list_datasets(session_view()))

    @app.post("/api/dl/datasets")
    def dl_add_dataset(body: PathBody):
        """登记一份样本集。样本库开着时顺带记下指纹，之后样本有变就能提示过期。"""
        def run():
            with st.lock:
                lib = s.library_path if s.is_open else ""
                records = s.records if s.is_open else None
                entry = catalog.register_dataset(body.path, body.name, lib, records)
                return catalog.describe_dataset(entry, session_view())
        return bad(run)

    @app.patch("/api/dl/datasets/{dataset_id}")
    def dl_rename_dataset(dataset_id: str, body: NameBody):
        return bad(lambda: catalog.rename_dataset(dataset_id, body.name.strip()) or {"ok": True})

    @app.delete("/api/dl/datasets/{dataset_id}")
    def dl_remove_dataset(dataset_id: str):
        return bad(lambda: catalog.remove_dataset(dataset_id) or {"ok": True})

    # ------------------------------------------------------------- 模型
    @app.get("/api/dl/models")
    def dl_models():
        def run():
            with st.lock:
                datasets = catalog.list_datasets(session_view())
            return catalog.list_models(runtime.jobs_root(), datasets)
        return bad(run)

    @app.post("/api/dl/models")
    def dl_add_model(body: PathBody):
        return bad(lambda: catalog.add_model(body.path) or {"ok": True})

    @app.delete("/api/dl/models")
    def dl_forget_model(path: str):
        return bad(lambda: catalog.forget_model(path) or {"ok": True})

    @app.get("/api/dl/models/compat")
    def dl_compat(path: str, layer_id: int):
        def run():
            layer = st.layers.get(int(layer_id))
            if layer is None or not isinstance(layer, L.RasterLayer):
                raise ValueError("不是影像图层")
            return catalog.compatibility(catalog.read_manifest(path), layer)
        return bad(run)

    @app.get("/api/dl/rasters")
    def dl_rasters():
        """画布上可以拿来预测的影像（有本地文件的多波段栅格）。"""
        return [{"id": l.uid, "name": l.name, "path": l.path, "bands": l.band_count}
                for l in st.layers.values()
                if isinstance(l, L.RasterLayer) and getattr(l, "path", "")
                and os.path.exists(l.path)]

    # ------------------------------------------------------------- 任务
    @app.get("/api/dl/jobs")
    def dl_jobs():
        return bad(jobs.list)

    @app.post("/api/dl/jobs")
    def dl_new_job(body: JobBody):
        def run():
            params = runtime.coerce_params(body.task, body.params)
            job_id = jobs.create(body.task, params, body.title.strip())
            return jobs.summary(job_id)
        return bad(run)

    @app.get("/api/dl/jobs/{job_id}")
    def dl_job(job_id: str):
        return bad(lambda: jobs.summary(job_id, detail=True))

    @app.post("/api/dl/jobs/{job_id}/cancel")
    def dl_cancel(job_id: str):
        return bad(lambda: jobs.cancel(job_id) or jobs.summary(job_id))

    @app.post("/api/dl/jobs/{job_id}/resume")
    def dl_resume(job_id: str):
        return bad(lambda: jobs.enqueue(job_id) or jobs.summary(job_id))

    @app.delete("/api/dl/jobs/{job_id}")
    def dl_delete(job_id: str):
        def run():
            for uid, layer in list(st.layers.items()):
                if isinstance(layer, LivePredictionLayer) and layer.job_id == job_id:
                    st.layers.pop(uid, None)
            jobs.delete(job_id)
            return {"ok": True}
        return bad(run)

    @app.get("/api/dl/jobs/{job_id}/samples")
    def dl_samples(job_id: str, split: str = "val", true_label: Optional[int] = None,
                   pred_label: Optional[int] = None, errors_only: bool = True, limit: int = 300):
        def run():
            out = bridge.eval_samples(jobs.job_dir(job_id), split, true_label, pred_label,
                                      errors_only, limit)
            with st.lock:
                live = {"%s:%d" % r.key for r in s.records} if s.is_open else set()
            for row in out["rows"]:
                row["in_library"] = row.get("key") in live
            return out
        return bad(run)

    @app.get("/api/dl/jobs/{job_id}/embedding")
    def dl_embedding(job_id: str):
        def run():
            out = bridge.embedding_points(jobs.job_dir(job_id))
            with st.lock:
                live = {"%s:%d" % r.key for r in s.records} if s.is_open else set()
            out["in_library"] = sum(1 for p in out["points"] if p.get("k") in live)
            return out
        return bad(run)

    # ------------------------------------------------------------- 预测结果上画布
    def _outputs(job_id: str) -> Dict[str, str]:
        d = jobs.digest(job_id)
        return {a["kind"]: a["path"] for a in d.artifacts if a.get("kind")}

    def _replace(old, new) -> None:
        new.uid = old.uid
        new.opacity, new.visible = old.opacity, old.visible
        st.layers[old.uid] = new
        old.close()

    def _final_layer(path: str, name: str):
        layer = L.RasterLayer(path, name=name)
        return layer

    @app.post("/api/dl/jobs/{job_id}/layer")
    def dl_show(job_id: str):
        """预测任务上画布：完成了加正式分类图，还在跑加实时图层。已经在画布上的不重复加。"""
        from .layer_routes import layer_json

        def run():
            summary = jobs.summary(job_id, detail=True)
            if summary["task"] not in ("predict", "similarity"):
                raise ValueError("该任务没有栅格结果")
            outs = _outputs(job_id)
            name = summary["title"] or "预测结果"
            for uid, layer in list(st.layers.items()):
                if isinstance(layer, LivePredictionLayer) and layer.job_id == job_id:
                    if summary["state"] == SUCCEEDED:
                        # 任务是在命令行里跑完的、或完成通知错过了：这时补换
                        finalize_live(job_id, SUCCEEDED)
                        layer = st.layers[uid]
                    return {"layer": layer_json(layer),
                            "live": isinstance(layer, LivePredictionLayer)}
                if outs.get("class") and getattr(layer, "path", "") == outs["class"]:
                    return {"layer": layer_json(layer), "live": False}
            if summary["state"] == SUCCEEDED and outs.get("class"):
                layer = _final_layer(outs["class"], name)
            else:
                init = summary.get("predict_init")
                if not init or not init.get("geotransform"):
                    raise ValueError("预测尚未开始，请稍后再试")
                layer = LivePredictionLayer(job_id, init["preview"], init, name=name)
            st.layers[layer.uid] = layer
            return {"layer": layer_json(layer), "live": isinstance(layer, LivePredictionLayer)}
        return bad(run)

    def finalize_live(job_id: str, state: str) -> None:
        """预测完成：实时图层原地换成正式成果（同一个图层号，顺序、透明度不变）。"""
        if state != SUCCEEDED:
            return
        for layer in list(st.layers.values()):
            if isinstance(layer, LivePredictionLayer) and layer.job_id == job_id:
                path = _outputs(job_id).get("class")
                if path and os.path.isfile(path):
                    try:
                        _replace(layer, _final_layer(path, layer.name))
                    except Exception as exc:
                        layer.error = str(exc).splitlines()[0]

    jobs.listeners.append(finalize_live)

    @app.post("/api/dl/jobs/{job_id}/low-confidence")
    def dl_low_conf(job_id: str, body: LowConfBody):
        """置信度低于阈值的区域转成"疑难区"矢量并加到画布上。"""
        from .layer_routes import layer_json

        def run():
            conf = _outputs(job_id).get("confidence")
            if not conf or not os.path.isfile(conf):
                raise ValueError("该预测任务未输出置信度栅格")
            out = str(Path(conf).with_name(Path(conf).stem + "_low%02d.gpkg"
                                           % int(round(body.threshold * 100))))
            res = bridge.low_confidence_layer(conf, float(body.threshold), out,
                                              int(body.min_pixels))
            layer = L.VectorLayer(out, name="疑难区（置信度 < %.2f）" % body.threshold,
                                  color="#e8590c")
            st.layers[layer.uid] = layer
            return {"layer": layer_json(layer), "features": res["features"], "path": out}
        return bad(run)

    @app.get("/api/dl/pixel")
    def dl_pixel(layer_id: int, lon: float, lat: float):
        """分类图上一点：类别、置信度（有置信度栅格时）。检查器用。"""
        def run():
            layer = st.layers.get(int(layer_id))
            path = getattr(layer, "path", "") if layer is not None else ""
            if not path:
                raise ValueError("不是分类图层")
            return _pixel_info(path, lon, lat)
        return bad(run)


def _pixel_info(path: str, lon: float, lat: float) -> Dict[str, Any]:
    import json as _json
    from osgeo import gdal, osr
    sidecar = Path(path).with_suffix(".classes.json")
    similarity = path.endswith("_similarity.tif")
    if not sidecar.is_file() and not similarity:
        raise ValueError("不是深度学习的结果")
    meta = _json.loads(sidecar.read_text("utf-8")) if sidecar.is_file() else {}
    ds = gdal.Open(path)
    srs = osr.SpatialReference()
    srs.ImportFromWkt(ds.GetProjection())
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    x, y, _ = osr.CoordinateTransformation(wgs, srs).TransformPoint(lon, lat)
    inv = gdal.InvGeoTransform(ds.GetGeoTransform())
    col, row = (int(v) for v in gdal.ApplyGeoTransform(inv, x, y))
    if not (0 <= col < ds.RasterXSize and 0 <= row < ds.RasterYSize):
        return {"inside": False}
    value = int(ds.GetRasterBand(1).ReadAsArray(col, row, 1, 1)[0, 0])
    if similarity:
        vpath = Path(str(Path(path).with_suffix("")) + "_value.tif")
        out = {"inside": True, "row": row, "col": col, "background": value == 255}
        if vpath.is_file() and value != 255:
            vds = gdal.Open(str(vpath))
            out["similarity"] = round(float(vds.GetRasterBand(1).ReadAsArray(col, row, 1, 1)[0, 0]), 4)
        return out
    names = {int(c["label"]): c for c in meta.get("classes", [])}
    out: Dict[str, Any] = {"inside": True, "row": row, "col": col, "value": value,
                           "background": value == meta.get("nodata")}
    if value in names:
        out["class"] = names[value].get("name")
        out["color"] = names[value].get("color")
    conf = Path(str(Path(path).with_suffix("")) + "_conf.tif")
    if conf.is_file():
        cds = gdal.Open(str(conf))
        out["confidence"] = round(float(cds.GetRasterBand(1).ReadAsArray(col, row, 1, 1)[0, 0]), 4)
    out["model"] = meta.get("model")
    return out
