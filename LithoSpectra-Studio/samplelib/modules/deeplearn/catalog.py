# -*- coding: utf-8 -*-
"""样本集与模型的登记、描述、过期判断、与影像的兼容性检查。

样本集登记在 ``<数据目录>/dl/datasets.json``；模型不登记，扫任务根下每个训练任务
的 ``model/*.lithomodel``，再加上使用者手动添加的模型包（``models.json``）。

过期判断：从样本库裁剪样本集时记下所涉类别的样本指纹（每类的样本数与样本号之
和）。之后样本库里这些类别增删了样本、改了归属，指纹就对不上，样本集与由它
训练的模型标"来源已变化"。只改几何不改归属的编辑不计入指纹，指纹用于提醒
重新裁剪，并非校验和。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np

from . import runtime

TRAIN_FILE = "train_dataset.h5"
TEST_FILE = "test_dataset.h5"
WAVELENGTH_TOLERANCE = 5.0
#: 改过名的骨干（与 Worker 的 ``backbones.ALIASES`` 一致）：旧模型包按现名显示、比对
BACKBONE_ALIASES = {"Common_1DCNN": "1D-CNN", "Common_2DCNN": "2D-CNN", "Common_3DCNN": "3D-CNN"}

_lock = threading.Lock()
_info_cache: Dict[str, tuple] = {}


def _registry(name: str) -> Path:
    return runtime.dl_dir() / name


def _load(name: str) -> List[dict]:
    try:
        with open(_registry(name), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(name: str, items: List[dict]) -> None:
    path = _registry(name)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# =============================================================================
# 样本集
# =============================================================================

def resolve_files(path: str) -> tuple:
    """样本集路径（裁剪输出目录或 h5）-> (训练集, 测试集或 None)。"""
    p = Path(path)
    if p.is_dir():
        train = p / TRAIN_FILE
        test = p / TEST_FILE
        if not train.is_file():
            raise ValueError("目录中没有 %s：%s\n请选择样本裁剪（h5 格式）的输出目录。"
                             % (TRAIN_FILE, p))
        return train, test if test.is_file() else None
    if p.is_file() and p.suffix.lower() == ".h5":
        sib = p.with_name(TEST_FILE) if p.name == TRAIN_FILE else None
        return p, sib if sib is not None and sib.is_file() else None
    raise ValueError("样本集不存在或不是 h5 文件：%s" % p)


def h5_info(path: Path) -> Dict[str, Any]:
    """读 h5 的属性与标签统计。按（路径, 修改时间）缓存。"""
    key = str(path)
    mtime = path.stat().st_mtime
    hit = _info_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    import h5py
    with h5py.File(path, "r") as fh:
        a = fh.attrs
        count = int(a.get("count", fh["labels"].shape[0]))
        labels = fh["labels"][:count]
        table = json.loads(a["class_table"]) if "class_table" in a else []
        info = {
            "path": str(path), "count": count,
            "bands": int(fh["patches"].shape[1]), "patch": int(fh["patches"].shape[2]),
            "dtype": str(fh["patches"].dtype), "complete": bool(a.get("complete", True)),
            "class_table": table,
            "label_counts": np.bincount(labels.astype(np.int64)).tolist() if count else [],
            "has_keys": "sample_keys" in fh,
            "source_image": str(a.get("source_image_path", "")),
            "wavelengths": [float(v) for v in a["wavelengths"]] if "wavelengths" in a else [],
            "size_mb": round(path.stat().st_size / 2 ** 20, 1),
        }
    _info_cache[key] = (mtime, info)
    return info


def library_fingerprint(records: Iterable, class_ids: Iterable[int]) -> str:
    wanted = {int(c) for c in class_ids}
    acc: Dict[int, List[int]] = {c: [0, 0] for c in wanted}
    for r in records:
        if r.class_id in wanted:
            acc[r.class_id][0] += 1
            acc[r.class_id][1] += int(r.key[1])
    text = ";".join("%d:%d:%d" % (c, n, s) for c, (n, s) in sorted(acc.items()))
    return hashlib.sha1(text.encode("ascii")).hexdigest()[:16]


def _class_ids(table: List[dict]) -> List[int]:
    out: List[int] = []
    for row in table:
        out += [int(c) for c in row.get("class_ids") or []]
    return out


def register_dataset(path: str, name: str = "", library: str = "", records=None,
                     note: str = "") -> Dict[str, Any]:
    train, _test = resolve_files(path)
    info = h5_info(train)
    with _lock:
        items = _load("datasets.json")
        norm = os.path.normcase(os.path.abspath(path))
        items = [d for d in items if os.path.normcase(os.path.abspath(d["path"])) != norm]
        entry = {"id": uuid.uuid4().hex[:10], "path": os.path.abspath(path),
                 "name": name or Path(path).name, "created": time.time(),
                 "library": library, "note": note,
                 "fingerprint": library_fingerprint(records, _class_ids(info["class_table"]))
                 if records is not None and library else ""}
        items.insert(0, entry)
        _save("datasets.json", items)
    return entry


def remove_dataset(dataset_id: str) -> None:
    with _lock:
        _save("datasets.json", [d for d in _load("datasets.json") if d["id"] != dataset_id])


def rename_dataset(dataset_id: str, name: str) -> None:
    with _lock:
        items = _load("datasets.json")
        for d in items:
            if d["id"] == dataset_id:
                d["name"] = name
        _save("datasets.json", items)


def dataset_entry(dataset_id: str) -> Dict[str, Any]:
    for d in _load("datasets.json"):
        if d["id"] == dataset_id:
            return d
    raise KeyError("样本集不存在：%s" % dataset_id)


def describe_dataset(entry: dict, session=None) -> Dict[str, Any]:
    out = dict(entry)
    try:
        train, test = resolve_files(entry["path"])
        info = h5_info(train)
        out.update({k: v for k, v in info.items() if k != "path"},
                   train=str(train), test=str(test) if test else "",
                   test_count=h5_info(test)["count"] if test else 0, missing=False)
        out["warnings"] = dataset_warnings(info, out["test_count"])
    except (OSError, ValueError, KeyError) as exc:
        out.update(missing=True, error=str(exc), warnings=[])
        return out
    out["stale"] = None
    if session is not None and session.is_open and entry.get("fingerprint") and \
            os.path.normcase(session.library_path) == os.path.normcase(entry.get("library", "")):
        now = library_fingerprint(session.records, _class_ids(info["class_table"]))
        out["stale"] = now != entry["fingerprint"]
    return out


def dataset_warnings(info: dict, test_count: int) -> List[str]:
    notes = []
    counts = info["label_counts"]
    names = {int(r["label"]): r.get("name", "") for r in info["class_table"]}
    if not info["complete"]:
        notes.append("裁剪时被中途取消，可能缺少部分类别")
    small = [names.get(i, str(i)) for i, n in enumerate(counts) if 0 < n < 20]
    if small:
        notes.append("以下类别的样本少于 20 个：%s" % "、".join(small[:6]))
    nz = [n for n in counts if n > 0]
    if nz and max(nz) / max(1, min(nz)) >= 20:
        notes.append("各类样本数最多相差 %d 倍，训练时建议将「类别加权」设为按样本数反比加权"
                     % (max(nz) // max(1, min(nz))))
    if not test_count:
        notes.append("没有测试集，训练结束后无法进行独立的精度评估")
    if not info.get("has_keys"):
        notes.append("该样本集未记录样本来源（旧版裁剪生成），错分样本只能定位到像元，无法对应到样本库中的样本")
    return notes


def list_datasets(session=None) -> List[Dict[str, Any]]:
    return [describe_dataset(d, session) for d in _load("datasets.json")]


# =============================================================================
# 模型
# =============================================================================

def _read_manifest(pkg: Path) -> Optional[dict]:
    try:
        with open(pkg / "manifest.json", "r", encoding="utf-8") as fh:
            m = json.load(fh)
        return m if m.get("format") == "lithomodel" else None
    except (OSError, ValueError):
        return None


def list_models(jobs_root: Path, datasets: Optional[List[dict]] = None) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    candidates: List[Path] = []
    if jobs_root.is_dir():
        candidates += sorted(jobs_root.glob("*/model/*.lithomodel"), reverse=True)
    candidates += [Path(m["path"]) for m in _load("models.json")]
    stale_by_path = {}
    for d in datasets or []:
        if d.get("stale"):
            for key in ("train", "path"):
                if d.get(key):
                    stale_by_path[os.path.normcase(os.path.abspath(d[key]))] = True
    for pkg in candidates:
        key = os.path.normcase(str(pkg.resolve())) if pkg.exists() else str(pkg)
        if key in seen:
            continue
        seen.add(key)
        manifest = _read_manifest(pkg)
        if manifest is None:
            continue
        ds_path = (manifest.get("dataset") or {}).get("train", "")
        out.append({
            "path": str(pkg.resolve()), "name": manifest.get("name") or pkg.stem,
            "task": manifest.get("task"), "backbone": BACKBONE_ALIASES.get(manifest.get("backbone"), manifest.get("backbone")),
            "input_kind": manifest.get("input_kind"), "bands": manifest.get("bands"),
            "patch_size": manifest.get("patch_size"), "classes": manifest.get("classes", []),
            "metrics": manifest.get("metrics", {}), "created": manifest.get("created"),
            "partial": bool(manifest.get("partial")), "job": manifest.get("job", ""),
            "dataset": manifest.get("dataset", {}), "wavelengths": bool(manifest.get("wavelengths")),
            "external": pkg.parent.name != "model",
            "method": manifest.get("method"), "feature_dim": manifest.get("feature_dim"),
            "pretrained": manifest.get("pretrained"),
            "stale": bool(stale_by_path.get(os.path.normcase(os.path.abspath(ds_path))))
            if ds_path else False,
        })
    return out


def add_model(path: str) -> None:
    pkg = Path(path)
    if _read_manifest(pkg) is None:
        raise ValueError("不是岩谱模型包（应为包含 manifest.json 的 .lithomodel 目录）：%s" % path)
    with _lock:
        items = [m for m in _load("models.json") if m["path"] != str(pkg)]
        items.insert(0, {"path": str(pkg), "added": time.time()})
        _save("models.json", items)


def forget_model(path: str) -> None:
    with _lock:
        _save("models.json", [m for m in _load("models.json") if m["path"] != path])


def read_manifest(path: str) -> dict:
    m = _read_manifest(Path(path))
    if m is None:
        raise ValueError("不是岩谱模型包：%s" % path)
    return m


def compatibility(manifest: dict, layer) -> Dict[str, Any]:
    """模型能不能用在这幅影像上：波段数必须一致；两边都有波长时逐波段比对。"""
    bands = getattr(layer, "band_count", 0)
    need = int(manifest.get("bands") or 0)
    if bands != need:
        return {"ok": False, "level": "error",
                "text": "影像有 %d 个波段，模型需要 %d 个" % (bands, need)}
    mw = manifest.get("wavelengths") or []
    lw = list(getattr(layer, "wavelengths", []) or [])
    if mw and lw and len(mw) == len(lw):
        lw_nm = [v * 1000 if max(lw) < 100 else v for v in lw]
        diff = np.abs(np.asarray(lw_nm) - np.asarray(mw))
        bad = int((diff > WAVELENGTH_TOLERANCE).sum())
        if bad:
            return {"ok": True, "level": "warn",
                    "text": "%d 个波段的波长相差超过 %g nm，最大相差 %.1f nm"
                            % (bad, WAVELENGTH_TOLERANCE, float(diff.max()))}
        return {"ok": True, "level": "ok", "text": "波段数与波长一致"}
    return {"ok": True, "level": "ok", "text": "波段数一致（%s无波长信息，未比对波长）"
            % ("模型" if not mw else "影像")}
