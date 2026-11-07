# -*- mode: python ; coding: utf-8 -*-
"""岩谱 LithoSpectra Web 界面版打包配置（PyInstaller，macOS 与 Windows 通用）。

用法
----
    cd LithoSpectra-Studio
    (cd web && npm install && npm run build)        # 先编译页面，生成 web/dist
    pyinstaller packaging/LithoSpectraWeb.spec --noconfirm

入口是 ``packaging/entry_web.py``（外壳代码在 ``webapi/desktop_app.py``），界面是
``web/dist`` 下编译好的静态页面，窗口由 pywebview 提供（macOS 用系统 WebKit，
Windows 用 Edge WebView2），不带任何 Qt 库。
"""

import glob
import json
import os
import shutil
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# =============================================================================
# 配置区
# =============================================================================

APP_NAME = "LithoSpectraWeb"
APP_DISPLAY = "岩谱 LithoSpectra"
ENTRY = "packaging/entry_web.py"

# 编译好的页面（相对 LithoSpectra-Studio/）。打包前先在 web/ 下 npm run build。
WEB_DIST = "web/dist"

# 随包携带的初始样本库（相对 LithoSpectra-Studio/）。首次启动复制到用户数据目录。
# 留空则打包产物首次启动打开空界面。分发版不带：样本库是各人自己的数据。
SEED_LIBRARY = ""

# 发行版 profile：samplelib/profiles/ 下的文件名（不含 .json）。留空则按
# samplelib/config.py 里的 MODULE_PROFILE 走。full / analysis / basic
PROFILE = ""

# Windows 产物中 exe 旁存放程序文件的目录名（PyInstaller 默认为 _internal）
CONTENTS_DIR = "lib"

# 随包资源（图标、波长表、光谱库、图例、工具箱、Worker）的目录名。Windows 产物里
# 打包完成后移到 exe 同级，macOS 留在 .app 包内。与 samplelib/datadir.py 一致。
RESOURCES = "resources"

# macOS 目标架构：arm64 / x86_64 / universal2
MAC_TARGET_ARCH = "arm64"

# 是否对二进制做 strip。macOS 上对 .framework 可能引发签名问题，默认关闭。
DO_STRIP = False

# =============================================================================

ROOT = os.path.abspath(os.getcwd())
PKG = os.path.join(ROOT, "packaging")

_index = os.path.join(ROOT, WEB_DIST, "index.html")
if not os.path.isfile(_index):
    raise SystemExit("找不到 %s：先在 web/ 目录下运行 npm install && npm run build。" % _index)


def _read_setting(rel_path, key, default):
    """从源码里按行读一个赋值，不 import（那会把 GDAL 拉进 PyInstaller 的进程）。"""
    try:
        with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(key):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return default


APP_VERSION = _read_setting("samplelib/__init__.py", "__version__", "0.0.0")
PROFILE_NAME = PROFILE or _read_setting("samplelib/config.py", "MODULE_PROFILE", "full")


def _modules():
    """(选中的模块 id, 全部模块 id -> 随包数据)。"""
    root = os.path.join(ROOT, "samplelib", "modules")
    every = {}
    for name in sorted(os.listdir(root)):
        manifest = os.path.join(root, name, "module.json")
        if os.path.isfile(manifest):
            with open(manifest, encoding="utf-8") as fh:
                doc = json.load(fh)
            every[doc["id"]] = list(doc.get("data", ()))
    path = os.path.join(ROOT, "samplelib", "profiles", "%s.json" % PROFILE_NAME)
    if not os.path.isfile(path):
        return sorted(every), every
    with open(path, encoding="utf-8") as fh:
        return list(json.load(fh).get("modules", ())), every


WANTED, ALL_MODULES = _modules()
print("发行版 profile：%s，装上的模块：%s" % (PROFILE_NAME, "、".join(WANTED) or "无"))


def _find_data(*candidates):
    """GDAL / PROJ 数据目录。在 venv 里打包时 sys.prefix 是 venv 本身，数据在
    底下那个 conda 环境里（sys.base_prefix），两处都找。"""
    for prefix in dict.fromkeys((sys.prefix, sys.base_prefix)):
        for rel in candidates:
            path = os.path.join(prefix, rel)
            if os.path.isdir(path):
                return path
    return ""


gdal_data = _find_data("share/gdal", "Library/share/gdal", "Lib/site-packages/osgeo/data/gdal")
proj_data = _find_data("share/proj", "Library/share/proj", "Lib/site-packages/osgeo/data/proj")
gdal_plugins = _find_data("lib/gdalplugins", "Library/lib/gdalplugins")

datas = [(os.path.join(ROOT, "assets"), os.path.join(RESOURCES, "assets")),
         (os.path.join(ROOT, WEB_DIST), WEB_DIST)]

# 传感器波长表（仓库根 assets/*.txt）
datas += [(p, os.path.join(RESOURCES, "assets"))
          for p in sorted(glob.glob(os.path.join(os.path.dirname(ROOT), "assets", "*.txt")))]


def _source_tree(name):
    """仓库根下的一棵源码树原样打进随包资源的同名目录（工具箱在使用者选的解释器里跑，
    要的是磁盘上的源码，不是冻结进本程序的字节码）。"""
    root = os.path.join(os.path.dirname(ROOT), name)
    out = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(folder, os.path.dirname(root))
        out += [(os.path.join(folder, f), os.path.join(RESOURCES, rel))
                for f in files if not f.endswith((".pyc", ".pyo"))]
    return out


# 深度学习模块的 Worker 也在 workers/ 下，环境探测借用 toolbox.envs
if "toolbox" in WANTED or "deeplearn" in WANTED:
    for _tree in ("toolbox", "workers"):
        _files = _source_tree(_tree)
        if _files:
            datas += _files
        else:
            print("提示：找不到 %s/，打包产物里的工具箱会不可用。" % _tree)

_legend = os.path.join(ROOT, "samplelib", "data", "lithology_legend.json")
if os.path.isfile(_legend):
    datas.append((_legend, os.path.join(RESOURCES, "legend")))

for _mid in WANTED:
    for _rel in ALL_MODULES.get(_mid, ()):
        _path = os.path.join(ROOT, "samplelib", _rel)
        if os.path.isfile(_path):
            datas.append((_path, os.path.join(RESOURCES, _mid)))
        else:
            print("提示：模块 %s 的数据文件不在：%s" % (_mid, _rel))
    _mf = os.path.join(ROOT, "samplelib", "modules", _mid, "module.json")
    if os.path.isfile(_mf):
        datas.append((_mf, os.path.join("samplelib", "modules", _mid)))

_profile_path = os.path.join(ROOT, "samplelib", "profiles", "%s.json" % PROFILE_NAME)
if os.path.isfile(_profile_path):
    datas.append((_profile_path, os.path.join("samplelib", "profiles")))

_seed = os.path.join(ROOT, SEED_LIBRARY) if SEED_LIBRARY else ""
if _seed and os.path.isfile(_seed):
    datas.append((_seed, os.path.join(RESOURCES, "seed")))
elif SEED_LIBRARY:
    print("提示：找不到初始样本库 %s，打包产物首次启动会打开空界面。" % _seed)
if gdal_data:
    datas.append((gdal_data, "gdal_data"))
if proj_data:
    datas.append((proj_data, "proj_data"))

binaries = collect_dynamic_libs("osgeo")
if gdal_plugins:
    for _name in sorted(os.listdir(gdal_plugins)):
        _full = os.path.join(gdal_plugins, _name)
        if not os.path.isfile(_full):
            continue
        if _name.lower().endswith((".dll", ".so", ".dylib")):
            binaries.append((_full, "gdal_plugins"))
        else:
            datas.append((_full, "gdal_plugins"))

# 模块：选中的显式带上（运行时按名字 import），没选中的排除
_module_imports = ["samplelib.modules.%s" % m for m in WANTED]
_module_excludes = ["samplelib.modules.%s" % m for m in ALL_MODULES if m not in WANTED]

hiddenimports = _module_imports + [
    "osgeo", "osgeo.gdal", "osgeo.ogr", "osgeo.osr",
    # 后端各路由文件按模块开关在函数里 import
    "webapi.io_routes", "webapi.layer_routes", "webapi.tree_routes", "webapi.misc_routes",
    "webapi.spectra_routes", "webapi.module_routes",
    # 光谱图在服务端用 Agg 画，导出 PDF / SVG / EPS 按扩展名挑后端
    "matplotlib.backends.backend_agg", "matplotlib.backends.backend_pdf",
    "matplotlib.backends.backend_svg", "matplotlib.backends.backend_ps",
]
# uvicorn 的事件循环、协议、日志配置都是按字符串加载的
hiddenimports += collect_submodules("uvicorn")
# 野外记录：路由与图像处理库都是用到时才 import 的。LaMa 模型（约 200 MB）不进包，
# 第一次去水印时在程序里下载到数据目录；文字识别走 macOS 自带的 Vision 框架。
# 录音转文字用 sherpa-onnx（自带一份 onnxruntime 与 C++ 扩展，按动态库收进来），识别
# 模型（约 240 MB）同样第一次用时下载
if "fieldwork" in WANTED:
    hiddenimports += ["webapi.fieldwork_routes", "piexif", "onnxruntime", "cv2"]
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        print("提示：环境里没有 sherpa-onnx，打包产物不能录音转文字。")
    else:
        hiddenimports += collect_submodules("sherpa_onnx")
        binaries += collect_dynamic_libs("sherpa_onnx")
    if sys.platform == "darwin":
        hiddenimports += ["ocrmac", "ocrmac.ocrmac", "objc", "Vision", "CoreML"]

excludes = [
    # 不用 Qt / Tk 等桌面界面库；环境里装着也不进包
    "PySide6", "shiboken6", "PyQt5", "PyQt6", "PySide2",
    "matplotlib.backends.backend_qtagg", "matplotlib.backends.backend_qt",
    "matplotlib.backends.backend_qt5agg", "matplotlib.backends.qt_compat",
    "matplotlib.backends.backend_tkagg", "matplotlib.backends.backend_tkcairo",
    "matplotlib.backends._backend_tk", "matplotlib.backends.backend_webagg",
    "matplotlib.backends.backend_webagg_core", "matplotlib.backends.backend_nbagg",
    "matplotlib.backends.backend_gtk3agg", "matplotlib.backends.backend_gtk4agg",
    "matplotlib.backends.backend_wxagg", "matplotlib.backends.backend_macosx",
    "matplotlib.tests", "matplotlib.testing",
    "tkinter", "_tkinter", "Tkinter",
    "setuptools", "pip", "pkg_resources", "wheel", "distutils", "PyInstaller",
    "osgeo_utils",
    "numpy.f2py", "numpy.distutils", "numpy.testing",
    "doctest", "pydoc", "pydoc_data", "test", "lib2to3", "idlelib", "turtledemo", "turtle",
    "curses", "dbm", "ensurepip", "venv", "multiprocessing.dummy",
    "PIL.ImageTk", "PIL.ImageQt",
    # 测试客户端只给 tests/ 用
    "fastapi.testclient", "starlette.testclient",
    # 构建环境里装着 PyTorch（别的项目用），本程序不用；不排掉 PyInstaller 会去收集它而崩
    "torch", "torchvision", "torchaudio", "triton",
    # 程序里没有一处 import 这些。环境里常顺带装着（scikit-image、Jupyter 之类拉进来的），
    # 某个库的可选 import 碰到它们时 PyInstaller 会整包收进来，几十上百兆
    "scipy", "sympy", "skimage", "sklearn", "pandas", "numba", "llvmlite",
    "IPython", "ipykernel", "jupyter", "jupyter_client", "jupyter_core", "notebook",
    "jedi", "parso", "bokeh", "plotly", "dask", "xarray", "tensorflow", "cupy",
    "sqlalchemy", "pytest", "_pytest", "qtpy",
    # onnxruntime 只用推理；这几个子包是模型转换 / 量化工具，会把 onnx、sympy 拉进来
    "onnxruntime.transformers", "onnxruntime.tools", "onnxruntime.quantization",
] + _module_excludes

# 窗口库 pywebview 每个平台只用一个后端，别的后端与它们的依赖不进包
_WEBVIEW_BACKENDS = ["webview.platforms.%s" % n for n in
                     ("cocoa", "winforms", "edgechromium", "mshtml", "gtk", "qt", "cef", "android")]
if sys.platform == "win32":
    # Windows：WinForms 窗口 + Edge WebView2，经 pythonnet（clr）调 .NET
    hiddenimports += ["webview.platforms.winforms", "webview.platforms.edgechromium",
                      "clr", "clr_loader", "pythonnet"]
    excludes += [m for m in _WEBVIEW_BACKENDS
                 if not m.endswith(("winforms", "edgechromium"))]
    # macOS 专用：文字识别走 Vision 框架
    excludes += ["ocrmac", "objc", "AppKit", "Foundation", "WebKit", "Quartz", "Vision", "CoreML"]
elif sys.platform == "darwin":
    excludes += [m for m in _WEBVIEW_BACKENDS if not m.endswith("cocoa")]
    excludes += ["clr", "clr_loader", "pythonnet"]

a = Analysis(
    [os.path.join(ROOT, ENTRY)],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[os.path.join(PKG, "runtime_hook_gdal.py")],
    excludes=excludes,
    noarchive=False,
    optimize=1,                       # 去 assert，保留 docstring（FastAPI 用它生成接口说明）
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=DO_STRIP,
    upx=False,
    console=False,
    target_arch=MAC_TARGET_ARCH if sys.platform == "darwin" else None,
    icon=os.path.join(ROOT, "assets", "logo.icns")
         if sys.platform == "darwin" else os.path.join(ROOT, "assets", "logo.ico"),
    contents_directory=CONTENTS_DIR if sys.platform == "win32" else "_internal",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=DO_STRIP,
    upx=False,
    name=APP_NAME,
)

if sys.platform == "win32":
    # 随包资源从 lib\resources 移到 exe 同级，使用者可以直接查看、替换波长表与光谱库
    _bundle = os.path.join(DISTPATH, APP_NAME)
    _inner = os.path.join(_bundle, CONTENTS_DIR, RESOURCES)
    _outer = os.path.join(_bundle, RESOURCES)
    if os.path.isdir(_inner):
        shutil.rmtree(_outer, ignore_errors=True)
        shutil.move(_inner, _outer)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=APP_NAME + ".app",
        icon=os.path.join(ROOT, "assets", "logo.icns"),
        bundle_identifier="cn.ac.air.aircasgxt.lithospectra.web",
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_DISPLAY,
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            # 界面页面由本进程在 127.0.0.1 上以 http 提供
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
