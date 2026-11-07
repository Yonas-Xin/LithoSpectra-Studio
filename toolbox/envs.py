# -*- coding: utf-8 -*-
"""找出本机可用的 Python 环境，并探它们的成色。

工具箱要在哪个环境里跑不是小事：``samplelib`` 没装 JPEG2000 驱动，读不了
Sentinel-2 的 ``.jp2``；``geo`` 有。以前只能关掉程序换个环境重开，现在让使用者
在界面上直接挑。

发现走的是文件系统而不是 ``conda`` 命令——``conda env list`` 要启一次 conda，
慢且不一定在 PATH 上。conda 自己把所有环境路径写在 ``~/.conda/environments.txt``
里，读它最快也最准，再补上几个常见的安装位置兜底。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# 探测子进程的超时（秒）。环境坏掉时 import 会卡住，不能无限等。
PROBE_TIMEOUT = 25

_WINDOWS = os.name == "nt"


def no_window_flags() -> dict:
    """给 ``subprocess`` 的额外参数：Windows 上不弹控制台窗口。

    界面打包成 exe 之后自己没有控制台，这时再去启动 ``python.exe`` 这种控制台
    程序，Windows 会为它新开一个黑窗口——探测环境时闪一下，跑去噪 Worker 时
    则整个过程都挂着一个黑框。加 ``CREATE_NO_WINDOW`` 就不会。管道照常可用。
    """
    if _WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


@dataclass(frozen=True)
class PythonEnv:
    """一个可用来跑工具箱的 Python 环境。"""

    name: str
    python: Path
    kind: str = "conda"        # conda / venv / system
    current: bool = False

    @property
    def label(self) -> str:
        mark = "（当前）" if self.current else ""
        return "%s%s" % (self.name, mark)


def _python_in(prefix: Path) -> Path | None:
    exe = prefix / ("python.exe" if _WINDOWS else "bin/python")
    return exe if exe.exists() else None


def _conda_prefixes() -> list[Path]:
    """conda 认得的所有环境目录。"""
    found: list[Path] = []

    listing = Path.home() / ".conda" / "environments.txt"
    if listing.exists():
        try:
            for line in listing.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line:
                    found.append(Path(line))
        except OSError:
            pass

    # 兜底：常见安装位置下的 envs/，以及当前激活环境的根
    roots = [Path(os.environ[key]) for key in ("CONDA_PREFIX", "CONDA_ROOT")
             if os.environ.get(key)]
    names = ("miniconda3", "anaconda3", "miniforge3", "mambaforge", "Miniconda3",
             "Anaconda3")
    roots += [Path.home() / name for name in names]
    if _WINDOWS:
        # Miniconda 装成 "Just Me" 在用户目录，"All Users" 在 ProgramData；
        # 有的安装器默认落到 LOCALAPPDATA。都看一眼。
        for key in ("LOCALAPPDATA", "ProgramData", "ProgramFiles"):
            base = os.environ.get(key)
            if base:
                roots += [Path(base) / name for name in names]
        roots += [Path(r"C:\miniconda3"), Path(r"C:\anaconda3")]
    else:
        roots += [Path("/opt/homebrew/Caskroom/miniconda/base"),
                  Path("/opt/miniconda3"), Path("/opt/anaconda3")]
    for root in roots:
        found.append(root)
        envs = root / "envs"
        if envs.is_dir():
            try:
                found.extend(child for child in envs.iterdir() if child.is_dir())
            except OSError:
                pass
    return found


def discover() -> list[PythonEnv]:
    """列出本机能找到的 Python 环境。当前解释器永远排在第一位。

    按 python 可执行文件去重：同一个环境常被好几条路径指到（``CONDA_PREFIX``
    与 ``environments.txt`` 里那份），列两遍只会让人犹豫该选哪个。
    """
    # 打包产物里 sys.executable 是应用自己的可执行文件，不是 Python 解释器。
    # 把它列进来，选中后启动的是第二个界面窗口而不是批处理。
    frozen = bool(getattr(sys, "frozen", False))
    current = Path(sys.executable).resolve()
    found: dict[Path, PythonEnv] = {}

    def add(name: str, python: Path, kind: str) -> None:
        try:
            key = python.resolve()
        except OSError:
            return
        if key in found:
            return
        found[key] = PythonEnv(name=name, python=key, kind=kind,
                               current=(key == current))

    if not frozen:
        add(_env_name(current.parent.parent), current, _env_kind(current))

    for prefix in _conda_prefixes():
        python = _python_in(prefix)
        if python is not None:
            add(prefix.name or str(prefix), python, "conda")

    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        python = _python_in(Path(venv))
        if python is not None:
            add(Path(venv).name, python, "venv")

    items = list(found.values())
    items.sort(key=lambda env: (not env.current, env.name.lower()))
    return items


def _env_name(prefix: Path) -> str:
    name = prefix.name
    return name if name and name not in (".", "/") else str(prefix)


def _env_kind(python: Path) -> str:
    parts = {part.lower() for part in python.parts}
    if "envs" in parts or "conda" in parts or "miniforge3" in parts:
        return "conda"
    if (python.parent.parent / "pyvenv.cfg").exists():
        return "venv"
    return "system"


# =============================================================================
# 探测
# =============================================================================

_PROBE = r"""
import json, sys
out = {"python": sys.version.split()[0], "ok": True, "missing": [], "notes": []}
try:
    import numpy
    out["numpy"] = numpy.__version__
except Exception:
    out["missing"].append("numpy")
try:
    from osgeo import gdal
    out["gdal"] = gdal.__version__
    drivers = [n for n in ("JP2OpenJPEG", "JP2KAK", "JP2ECW", "JP2MrSID", "JP2Lura")
               if gdal.GetDriverByName(n)]
    out["jp2"] = bool(drivers)
    out["envi"] = bool(gdal.GetDriverByName("ENVI"))
except Exception:
    out["missing"].append("gdal")
# 以下是选配：只有跑 HyDe 去噪的环境才需要，缺了不影响这个环境跑别的工具
for name in ("torch", "bm3d", "pywt"):
    try:
        module = __import__(name)
        out[name] = getattr(module, "__version__", "?")
    except Exception:
        pass
# hyde 这个名字在 PyPI 上被一个静态网站生成器占着，`import hyde` 成功说明不了
# 什么。认准算法本身：装错包时要能说出装错了，而不是报"缺少 hyde"让人以为没装。
def _hyde_version():
    # hyde/__init__.py 拿的是发行版 "hyde" 的版本，而包实际叫 hyde-images，
    # 于是自报 "unknown"。版本要从发行版元数据取。
    import importlib.metadata as meta
    for dist in ("hyde-images", "hyde"):
        try:
            return meta.version(dist)
        except Exception:
            pass
    return "?"
try:
    import hyde
    if hasattr(hyde, "FastHyDe"):
        out["hyde"] = _hyde_version()
    else:
        out["hyde_wrong_package"] = getattr(hyde, "__version__", "?")
except Exception:
    pass
out["ok"] = not out["missing"]
print("PROBE" + json.dumps(out))
"""


#: conda 激活脚本注入的、写死了某一个环境路径的变量。探测别的环境时必须摘掉，
#: 否则子解释器会去调用方那个环境的目录里找东西，答出来的是调用方的成色。
ENV_LEAKS = (
    "GDAL_DRIVER_PATH", "GDAL_DATA", "PROJ_DATA", "PROJ_LIB", "PROJ_NETWORK",
    "PYTHONPATH", "PYTHONHOME", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
)


def clean_environment(base: dict | None = None) -> dict:
    """去掉指向调用方环境的变量，让别的解释器用它自己的默认路径。

    conda 激活 ``geo`` 时会设 ``GDAL_DRIVER_PATH=<geo>/lib/gdalplugins``。带着这个
    变量去启动 ``samplelib`` 的解释器，它就会跑到 geo 的插件目录里找驱动——于是
    界面跑在哪个环境里，所有环境就都被报成哪个环境的成色。JPEG2000 这种以插件
    形式安装的驱动，正好会因此报反。

    打包后的界面更甩不掉这些：启动钩子把 ``GDAL_DATA`` / ``GDAL_DRIVER_PATH``
    指到包内随带的那一份，那是它自己那个 GDAL 版本的；子解释器的 GDAL 多半是
    另一个版本，照这个路径去装插件只会报"不兼容"，自带的插件目录反而被顶掉。
    探测环境、启动批处理、启动 Worker 三处都得先洗一遍。
    """
    source = os.environ if base is None else base
    return {key: value for key, value in source.items() if key not in ENV_LEAKS}


def probe(env) -> dict:
    """在目标环境里跑一段小脚本，问清楚它装了什么。

    这件事必须在**那个**解释器里做，本进程 import 得到什么都不算数。
    ``env`` 可以是 :class:`PythonEnv`，也可以直接是解释器路径。
    """
    python = getattr(env, "python", env)
    try:
        done = subprocess.run(
            [str(python), "-c", _PROBE],
            capture_output=True, text=True, errors="replace",
            timeout=PROBE_TIMEOUT, env=clean_environment(),
            **no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc), "missing": ["python"]}

    for line in (done.stdout or "").splitlines():
        if line.startswith("PROBE"):
            try:
                return json.loads(line[5:])
            except ValueError:
                break
    message = (done.stderr or done.stdout or "").strip().splitlines()
    return {"ok": False, "missing": ["python"],
            "error": message[-1] if message else "解释器没有正常应答"}


#: 跑 HyDe 去噪需要的东西。缺任何一样，去噪工具都启动不了 Worker。
DENOISE_REQUIREMENTS = ("torch", "hyde")


def can_denoise(info: dict) -> bool:
    """这个环境能不能直接当 HyDe 去噪 Worker 用。"""
    return bool(info.get("ok")) and all(info.get(name) for name in DENOISE_REQUIREMENTS)


def denoise_problem(info: dict) -> str:
    """去噪跑不起来的原因，一句话。能跑时返回空串。"""
    if can_denoise(info):
        return ""
    if not info.get("ok"):
        return info.get("error") or ("缺少 " + "、".join(info.get("missing") or ["依赖"]))
    if info.get("hyde_wrong_package"):
        return ("装的 hyde 是 PyPI 上同名的静态网站生成器（%s），不是算法库 "
                "hyde-images" % info["hyde_wrong_package"])
    missing = [name for name in DENOISE_REQUIREMENTS if not info.get(name)]
    return "缺少 " + "、".join(missing or ["依赖"])


def describe(info: dict) -> str:
    """把 probe 的结果说成一句人话。"""
    if not info.get("ok"):
        if info.get("error"):
            return "不可用：%s" % info["error"]
        return "缺少 %s" % "、".join(info.get("missing") or ["依赖"])
    bits = ["Python %s" % info.get("python", "?")]
    if info.get("gdal"):
        bits.append("GDAL %s" % info["gdal"])
    bits.append("JPEG2000 " + ("可读" if info.get("jp2") else "缺驱动"))
    if info.get("hyde"):
        bits.append("HyDe %s（可跑去噪）" % info["hyde"])
    elif info.get("hyde_wrong_package"):
        bits.append("hyde 装成了静态网站生成器，不能去噪")
    elif info.get("torch"):
        bits.append("torch %s，无 hyde" % info["torch"])
    return " · ".join(bits)
