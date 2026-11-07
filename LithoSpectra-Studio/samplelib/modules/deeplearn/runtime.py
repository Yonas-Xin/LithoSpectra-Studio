# -*- coding: utf-8 -*-
"""Worker 在哪、用哪个 Python 跑、那个 Python 行不行。

Worker 源码（仓库根 ``workers/litho_dl``）随安装包作为普通文件携带，由使用者选的
解释器以 ``python -m litho_dl ...`` 执行。本进程不 import torch。

体检（``doctor``）与任务声明（``capabilities``）都要在目标解释器里 import torch，
一次两三秒，结果按"解释器 + Worker 源码修改时间"缓存到磁盘，换了环境或改了
Worker 才重取。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from samplelib import appstate, config
from samplelib.toolbox_bridge import REPO_ROOT

WORKERS_DIR = Path(REPO_ROOT) / "workers"
PACKAGE_DIR = WORKERS_DIR / "litho_dl"
DOCTOR_TIMEOUT = 120
CAPS_TIMEOUT = 180

#: appstate 里的键
KEY_PYTHON = "dl_python"
KEY_JOBS_ROOT = "dl_jobs_root"

_lock = threading.Lock()


def _key(python: str) -> str:
    """同一个解释器常有好几条路径（python、python3.13、软链接），按真实路径认。"""
    try:
        return os.path.normcase(os.path.realpath(python))
    except OSError:
        return python


def available() -> bool:
    return (PACKAGE_DIR / "__main__.py").is_file()


def dl_dir() -> Path:
    path = Path(config.app_data_dir()) / "dl"
    path.mkdir(parents=True, exist_ok=True)
    return path


def plugin_dir() -> Path:
    return dl_dir() / "plugins"


def jobs_root() -> Path:
    custom = appstate.load().get(KEY_JOBS_ROOT) or ""
    path = Path(custom) if custom else dl_dir() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def set_jobs_root(path: str) -> Path:
    doc = appstate.load()
    doc[KEY_JOBS_ROOT] = str(path or "")
    appstate.save(doc)
    return jobs_root()


def _toolbox_envs():
    from samplelib import toolbox_bridge
    toolbox_bridge.load()
    from toolbox import envs
    return envs


def clean_env() -> Dict[str, str]:
    """给 Worker 的环境变量：洗掉指向本进程 GDAL / PROJ 的变量，指好包与插件目录。"""
    env = _toolbox_envs().clean_environment()
    env["PYTHONPATH"] = str(WORKERS_DIR)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["LITHO_DL_PLUGINS"] = str(plugin_dir())
    return env


def popen_kwargs() -> Dict[str, Any]:
    return dict(env=clean_env(), cwd=str(dl_dir()), **_toolbox_envs().no_window_flags())


def worker_signature() -> str:
    """Worker 源码与插件的最新修改时间。变了就让缓存失效。"""
    latest = 0.0
    for folder in (PACKAGE_DIR, plugin_dir()):
        if folder.is_dir():
            for p in folder.rglob("*.py"):
                try:
                    latest = max(latest, p.stat().st_mtime)
                except OSError:
                    pass
    return "%.0f" % latest


# =============================================================================
# 环境
# =============================================================================

def environments() -> List[Dict[str, Any]]:
    """本机能找到的 Python 环境，附上次体检的结论。打包产物自身不算。"""
    envs = _toolbox_envs()
    cache = _load_cache()
    chosen = _key(selected_python()) if selected_python() else ""
    out = []
    for e in envs.discover():
        if getattr(sys, "frozen", False) and e.current:
            continue
        hit = cache.get("doctor", {}).get(_key(str(e.python)))
        out.append({"name": e.label, "python": str(e.python), "kind": e.kind,
                    "selected": _key(str(e.python)) == chosen,
                    "checked": bool(hit), "ok": bool(hit and hit["report"].get("ok")),
                    "device": (hit or {}).get("report", {}).get("describe", "")})
    return out


def selected_python() -> str:
    path = appstate.load().get(KEY_PYTHON) or ""
    return path if path and os.path.isfile(path) else ""


def select_python(path: str) -> None:
    doc = appstate.load()
    doc[KEY_PYTHON] = path
    appstate.save(doc)


def selected_device() -> str:
    """选中环境上次体检得到的设备类型（cuda / mps / cpu），没体检过返回空。"""
    python = selected_python()
    hit = _load_cache().get("doctor", {}).get(_key(python)) if python else None
    return (hit or {}).get("report", {}).get("device", "") if hit else ""


def require_python() -> str:
    path = selected_python()
    if not path:
        raise ValueError("尚未选择运行环境。请在深度学习工作台的「运行环境」中选择已安装 PyTorch 的环境，"
                         "体检通过后选用。")
    return path


# =============================================================================
# 体检与声明
# =============================================================================

def _cache_path() -> Path:
    return dl_dir() / "runtime.json"


def _load_cache() -> Dict[str, Any]:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_cache(doc: Dict[str, Any]) -> None:
    tmp = _cache_path().with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)
    os.replace(tmp, _cache_path())


def run_worker(python: str, args: List[str], timeout: float) -> subprocess.CompletedProcess:
    if not available():
        raise RuntimeError("找不到深度学习 Worker：%s" % PACKAGE_DIR)
    if not os.path.isfile(python):
        raise ValueError("Python 解释器不存在：%s" % python)
    return subprocess.run([python, "-m", "litho_dl"] + args, capture_output=True,
                          timeout=timeout, **popen_kwargs())


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace") if data else ""


def doctor(python: str, refresh: bool = False) -> Dict[str, Any]:
    """在目标解释器里跑体检。崩溃（如 libomp 冲突的 134）也作为一份不合格的报告返回。"""
    sig = worker_signature()
    with _lock:
        cache = _load_cache()
        hit = cache.get("doctor", {}).get(_key(python))
        if hit and not refresh and hit.get("sig") == sig:
            return hit["report"]
    try:
        done = run_worker(python, ["doctor", "--json"], DOCTOR_TIMEOUT)
        text = _decode(done.stdout)
        try:
            report = json.loads(text[text.index("{"):])
        except ValueError:
            report = {"ok": False, "checks": [{
                "name": "启动", "ok": False, "required": True,
                "detail": "Worker 无法运行（退出码 %s）" % done.returncode,
                "hint": _decode(done.stderr).strip()[-1200:] or "无错误输出"}]}
    except subprocess.TimeoutExpired:
        report = {"ok": False, "checks": [{"name": "启动", "ok": False, "required": True,
                                           "detail": "体检超时（%d 秒）" % DOCTOR_TIMEOUT,
                                           "hint": "该环境导入 PyTorch 时无响应"}]}
    with _lock:
        cache = _load_cache()
        cache.setdefault("doctor", {})[_key(python)] = {"sig": sig, "report": report, "t": time.time()}
        _save_cache(cache)
    return report


def capabilities(python: Optional[str] = None, refresh: bool = False) -> Dict[str, Any]:
    """任务、参数、骨干的声明。界面据此生成表单，不硬编码任何模型名。"""
    python = python or require_python()
    sig = worker_signature()
    with _lock:
        cache = _load_cache()
        hit = cache.get("caps", {}).get(_key(python))
        if hit and not refresh and hit.get("sig") == sig:
            return hit["caps"]
    try:
        done = run_worker(python, ["capabilities"], CAPS_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("读取 Worker 声明超时")
    if done.returncode != 0:
        raise RuntimeError("读取 Worker 声明失败：%s" % (_decode(done.stderr).strip()[-800:]
                                                     or "退出码 %d" % done.returncode))
    caps = json.loads(_decode(done.stdout))
    with _lock:
        cache = _load_cache()
        cache.setdefault("caps", {})[_key(python)] = {"sig": sig, "caps": caps, "t": time.time()}
        _save_cache(cache)
    return caps


def task_params(task: str, python: Optional[str] = None) -> List[Dict[str, Any]]:
    for spec in capabilities(python).get("tasks", []):
        if spec["name"] == task:
            return spec["params"]
    raise ValueError("Worker 不支持任务：%s" % task)


def coerce_params(task: str, raw: Dict[str, Any], python: Optional[str] = None) -> Dict[str, Any]:
    """按 Worker 的参数声明在本进程先校验一遍：参数不对当场报，不必等子进程起来。

    Worker 执行前还会按同一份声明再校验一次，这里只是让错误来得早。
    """
    params = task_params(task, python)
    known = {p["name"] for p in params}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError("未知参数：%s" % "、".join(unknown))
    out: Dict[str, Any] = {}
    for p in params:
        value = raw.get(p["name"])
        empty = value is None or (isinstance(value, str) and not value.strip())
        if empty:
            if p.get("optional"):
                out[p["name"]] = None if p["kind"] in ("path", "str") else p["default"]
                continue
            if p.get("default") is None:
                raise ValueError("缺少参数：%s" % p["label"])
            value = p["default"]
        kind = p["kind"]
        try:
            if kind == "int":
                value = int(value)
            elif kind == "float":
                value = float(value)
            elif kind == "bool":
                value = value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes", "on")
            elif kind == "choice":
                allowed = [c["value"] for c in p.get("choices", [])]
                value = (p.get("aliases") or {}).get(value, value)     # 改过名的旧取值
                if value not in allowed:
                    raise ValueError("可选值为 %s" % " / ".join(map(str, allowed)))
            else:
                value = str(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("%s 取值不合法：%r（%s）" % (p["label"], value, exc))
        if kind in ("int", "float"):
            if p.get("minimum") is not None and value < p["minimum"]:
                raise ValueError("%s 不能小于 %s" % (p["label"], p["minimum"]))
            if p.get("maximum") is not None and value > p["maximum"]:
                raise ValueError("%s 不能大于 %s" % (p["label"], p["maximum"]))
        if kind == "path" and p.get("path_mode") == "file" and not p.get("optional") \
                and not os.path.exists(value):
            raise ValueError("%s 不存在：%s" % (p["label"], value))
        out[p["name"]] = value
    return out
