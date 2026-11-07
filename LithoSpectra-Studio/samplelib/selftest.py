# -*- coding: utf-8 -*-
"""打包产物的自检：不开界面，把该带的东西挨个点一遍，结果写成 JSON。

打包脚本在冒烟测试里用它。原来的冒烟测试只看"启动后活了 12 秒"，能抓到缺
DLL 这种一起来就崩的，抓不到**起得来但少了东西**的：proj.db 没带上（坐标变换
悄悄算错）、JPEG2000 插件没进包（.jp2 读不了）、某个模块 setup 失败被外壳跳过
（页面上少一块）。这些在开发机上都看不出来，因为开发机的环境里什么都有。

用法（给打包脚本，也给排障）::

    LITHOSPECTRA_SELFTEST=/path/report.json  LithoSpectraWeb.app/Contents/MacOS/LithoSpectraWeb

程序不弹窗口，建起后端、确认编译好的页面与 pywebview 在包里，跑完把报告写到那个
路径，退出码 0 表示全部通过。打包后的程序没有控制台，所以结果只能落文件，不能靠 stdout。
"""

import json
import os
import sys
import traceback

#: 环境变量名。设了它，主程序不开界面、只跑自检。
ENV_VAR = "LITHOSPECTRA_SELFTEST"

#: 少了这些驱动程序就废了：样本库本身（GPKG）、影像（GTiff / ENVI）、
#: 导入导出（Shapefile / GeoJSON / KML）、内存与虚拟栅格（MEM / VRT）。
REQUIRED_DRIVERS = ("GPKG", "GTiff", "ENVI", "ESRI Shapefile", "GeoJSON",
                    "KML", "MEM", "VRT", "PNG", "JPEG")


def _check(report, name, fn):
    """跑一项，成了记结果，败了记原因；一项失败不影响后面的。"""
    try:
        report["checks"][name] = fn()
    except Exception as exc:                          # noqa: BLE001 — 都要记下来
        report["checks"][name] = None
        report["problems"].append("%s：%s" % (name, exc))
        report["tracebacks"][name] = traceback.format_exc()


def run(report_path: str, shell: str = "web") -> int:
    report = {"ok": False, "frozen": bool(getattr(sys, "frozen", False)), "shell": shell,
              "executable": sys.executable, "python": sys.version.split()[0],
              "checks": {}, "problems": [], "tracebacks": {}}

    from . import config
    report["version"] = getattr(sys.modules.get("samplelib"), "__version__", "")
    report["profile"] = config.MODULE_PROFILE

    # ------------------------------------------------------------ GDAL / PROJ
    def gdal_ok():
        from osgeo import gdal, ogr, osr
        gdal.UseExceptions()
        names = {gdal.GetDriver(i).ShortName for i in range(gdal.GetDriverCount())}
        names |= {ogr.GetDriver(i).GetName() for i in range(ogr.GetDriverCount())}
        missing = [d for d in REQUIRED_DRIVERS if d not in names]
        if missing:
            raise RuntimeError("缺驱动：%s" % "、".join(missing))
        # 坐标变换要 proj.db。没带上时 PROJ 不报错，只是算不出来 / 算错——
        # 这里拿一个已知点验一下数。
        src = osr.SpatialReference(); src.ImportFromEPSG(4326)
        dst = osr.SpatialReference(); dst.ImportFromEPSG(3857)
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        x, y, _z = osr.CoordinateTransformation(src, dst).TransformPoint(100.0, 40.0)
        if abs(x - 11131949.08) > 1.0 or abs(y - 4865942.28) > 1.0:
            raise RuntimeError("EPSG:4326→3857 结果不对：(%.2f, %.2f)，"
                               "多半是 proj.db 没带上" % (x, y))
        plugins = os.environ.get("GDAL_DRIVER_PATH", "")
        return {"version": gdal.__version__, "drivers": len(names),
                "jp2": "JP2OpenJPEG" in names,
                "gdal_data": os.environ.get("GDAL_DATA", ""),
                "proj_data": os.environ.get("PROJ_DATA") or os.environ.get("PROJ_LIB", ""),
                "plugin_dir": plugins,
                "plugin_dir_exists": bool(plugins) and os.path.isdir(plugins)}
    _check(report, "gdal", gdal_ok)

    # ------------------------------------------------------------ 数值与图像
    def numeric_ok():
        import numpy
        from PIL import Image
        Image.new("RGB", (2, 2))
        return {"numpy": numpy.__version__}
    _check(report, "numpy_pillow", numeric_ok)

    def h5py_ok():
        try:
            import h5py
        except ImportError:
            return {"present": False}          # 可选依赖，缺了只是 h5 输出不可用
        return {"present": True, "version": h5py.__version__}
    _check(report, "h5py", h5py_ok)

    # ------------------------------------------------------------ 外壳
    def web_ok():
        # 后端整套路由建得起来（各模块的接口按 profile 挂上），页面与窗口库都在包里
        import uvicorn  # noqa: F401
        import webview  # noqa: F401
        from webapi import create_app
        from webapi.misc_routes import enabled_modules
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        page = os.path.join(base, "web", "dist", "index.html")
        if not os.path.isfile(page):
            raise RuntimeError("编译好的页面没带上：%s" % page)
        app = create_app("", static_dir=os.path.dirname(page), remember=False)
        routes = [r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")]
        out = {"modules": sorted(enabled_modules()), "routes": len(routes),
               "webview": getattr(webview, "__version__", "")}
        if "fieldwork" in out["modules"]:
            # 修补库是用到时才 import 的，打包漏了要到去水印那一下才炸——这里先试
            from .modules.fieldwork import inpaint, ovmark
            st = inpaint.status()
            lost = [k for k in ("onnxruntime", "opencv") if not st[k]]
            if lost:
                raise RuntimeError("野外记录的修补库没带上：%s" % "、".join(lost))
            out["fieldwork"] = {"ocr": ovmark.ocr_available(), "lama_model": st["model"]}
        return out

    _check(report, "web_shell", web_ok)

    # ------------------------------------------------------------ 随包数据
    def assets_ok():
        problems = []
        if not config.icon_path(256):
            problems.append("图标 assets/logo_256.png")
        tables = [f for d in config.WAVELENGTH_DIRS if os.path.isdir(d)
                  for f in os.listdir(d) if f.lower().endswith(".txt")]
        if not tables:
            problems.append("传感器波长表（assets/*.txt）")
        from . import legend
        if not os.path.isfile(legend.DATA_PATH):
            problems.append("岩性图例 %s" % legend.DATA_PATH)
        from .module import read_profile
        if "fieldwork" in (read_profile(config.MODULE_PROFILE) or []):
            from .modules.fieldwork import geomark
            # 缺了它照片水印的指北针会退回手画的简易箭头，不报错，只能在这里拦
            if not any(os.path.isfile(p) for p in geomark.ESRI_NORTH_FONTS):
                problems.append("指北针字体 %s" % geomark.ESRI_NORTH_FONTS[0])
        # 初始样本库可有可无：分发版不带，首次启动打开空界面；带了就得打得开
        seed = config.seed_library()
        if seed:
            from osgeo import ogr
            ds = ogr.Open(seed, 0)
            if ds is None:
                problems.append("初始样本库打不开：%s" % seed)
            else:
                ds = None
        if problems:
            raise RuntimeError("缺：%s" % "；".join(problems))
        return {"wavelength_tables": sorted(tables), "seed": os.path.basename(seed),
                "resource_dir": config.RESOURCE_DIR, "data_dir": config.app_data_dir()}
    _check(report, "assets", assets_ok)

    def toolbox_ok():
        from . import toolbox_bridge
        from .module import read_profile
        wanted = read_profile(config.MODULE_PROFILE) or []
        if "toolbox" not in wanted:
            return {"wanted": False}
        if not toolbox_bridge.available():
            raise RuntimeError("找不到 toolbox/：%s" % toolbox_bridge.REPO_ROOT)
        worker = os.path.join(toolbox_bridge.REPO_ROOT, "workers", "hyde_denoise",
                              "worker.py")
        if not os.path.isfile(worker):
            raise RuntimeError("找不到去噪 Worker：%s" % worker)
        return {"wanted": True, "root": toolbox_bridge.REPO_ROOT}
    _check(report, "toolbox", toolbox_ok)

    def imports_ok():
        """把选中模块下的每个子模块都 import 一遍。

        真正的重头（matplotlib、h5py 的调用）在用到时才 import。打包排除清单排多了，
        只有走到这一步才炸——这里替使用者先走一遍。
        """
        import importlib
        import pkgutil
        from .module import read_profile
        wanted = read_profile(config.MODULE_PROFILE) or []
        count = 0
        failed = []
        for mid in wanted:
            root = importlib.import_module("%s.modules.%s" % (__package__, mid))
            for info in pkgutil.walk_packages(root.__path__, root.__name__ + "."):
                try:
                    importlib.import_module(info.name)
                    count += 1
                except Exception as exc:              # noqa: BLE001
                    failed.append("%s：%s" % (info.name, exc))
        if failed:
            raise RuntimeError("；".join(failed))
        return {"submodules": count}
    _check(report, "module_imports", imports_ok)

    def spectra_ok():
        from .module import read_profile
        wanted = read_profile(config.MODULE_PROFILE) or []
        if "spectra" not in wanted:
            return {"wanted": False}
        import matplotlib
        from matplotlib.backends import backend_agg  # noqa: F401
        from matplotlib.backends import backend_pdf, backend_svg    # noqa: F401
        # 真画一张：mpl-data（样式、字体表）没带上时到这里才会报
        from matplotlib.figure import Figure
        fig = Figure(figsize=(2, 1.5))
        ax = fig.add_subplot(111)
        ax.plot([400, 900, 2500], [0.1, 0.4, 0.3])
        ax.set_xlabel("波长 (nm)")
        backend_agg.FigureCanvasAgg(fig).draw()
        from .modules.spectra import speclib, spectra
        need = (spectra.packaged_feature_path(), speclib.packaged_library_path())
        missing = [n for n in need if not os.path.isfile(n)]
        if missing:
            raise RuntimeError("光谱模块数据缺：%s" % "、".join(missing))
        return {"wanted": True, "matplotlib": matplotlib.__version__}
    _check(report, "spectra", spectra_ok)

    def speex_ok():
        # 只报告、不算失败：找不到 libspeex 时奥维录音照常入库，只是不能在窗口里播放
        from .module import read_profile
        if "fieldwork" not in (read_profile(config.MODULE_PROFILE) or []):
            return {"wanted": False}
        from .modules.fieldwork import speex
        found = speex._load()
        return {"wanted": True, "available": found is not None, "library": getattr(found, "_name", "")}
    _check(report, "speex", speex_ok)

    def asr_ok():
        # 录音转文字的引擎：只报告。真建识别器要模型（不随包），这里只看扩展能不能加载
        from .module import read_profile
        if "fieldwork" not in (read_profile(config.MODULE_PROFILE) or []):
            return {"wanted": False}
        try:
            import sherpa_onnx
        except ImportError as exc:
            return {"wanted": True, "available": False, "error": str(exc)}
        sherpa_onnx.OfflineRecognizerConfig                # 访问一个 C++ 绑定类，确认扩展真的载入了
        return {"wanted": True, "available": True, "version": sherpa_onnx.__version__}
    _check(report, "asr", asr_ok)

    report["ok"] = not report["problems"]
    folder = os.path.dirname(os.path.abspath(report_path))
    os.makedirs(folder, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return 0 if report["ok"] else 1
