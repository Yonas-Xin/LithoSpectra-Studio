# -*- coding: utf-8 -*-
"""在 Windows 上把「岩谱 LithoSpectra」打包成可直接双击运行的程序。

不要直接跑本文件——双击同目录下的 ``build_windows.bat`` 即可，那个批处理
负责找到并激活 conda 环境（GDAL 的 DLL 全靠环境激活后的 PATH 才找得到），
再把活交给这里。手工跑的话，先激活环境：

    conda activate samplelib
    python packaging\\build_windows.py

产物在 ``packaging\\dist-win64\\LithoSpectraWeb\\``，双击其中的
``LithoSpectraWeb.exe`` 启动。整个目录（或脚本最后压出的 zip）就是分发单位，
不能只拷 exe 出来。

打的是 onedir 而不是单文件 exe：单文件每次启动都要把几百兆解到临时目录，
冷启动要十几秒，GDAL 的驱动探测也常在解压路径上出岔子。

窗口是系统自带的 Edge WebView2（Windows 11 自带，Windows 10 多数已随 Edge
装好），界面是 ``web/dist`` 下编译好的页面，包里不带任何浏览器内核与 Qt。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# =============================================================================
# 配置区（按需修改）
# =============================================================================

# PyInstaller 配置文件，相对 LithoSpectra-Studio/。macOS 与 Windows 共用一份。
SPEC = os.path.join("packaging", "LithoSpectraWeb.spec")

# 产物名，须与 spec 里的 APP_NAME 一致
APP_NAME = "LithoSpectraWeb"

# exe 旁存放程序文件的目录名，须与 spec 里的 CONTENTS_DIR 一致
CONTENTS_DIR = "lib"

# 输出目录与中间目录，相对 LithoSpectra-Studio/。带 -win64 后缀，与 macOS 的
# packaging/dist-web、packaging/build-web 分开：两边共用一个目录（比如从共享盘
# 直接打包）时，Mac 产物里的符号链接 Windows 删不掉，清理旧产物会报"拒绝访问"。
DIST_DIR = os.path.join("packaging", "dist-win64")
WORK_DIR = os.path.join("packaging", "build-win64")

# 打包前清掉上次的产物。关掉可让 PyInstaller 复用缓存、构建更快，但换过
# 依赖或改过 spec 之后容易带进旧文件。
CLEAN_BEFORE = True

# 页面怎么来：
#   "auto"  —— 装了 Node.js（npm 在 PATH 上）就现编译；没装就用已有的 web\dist
#              （可以在 Mac 上 npm run build 之后连同仓库一起拷过来）
#   "always" —— 一定现编译，没有 npm 就报错
#   "never"  —— 只用已有的 web\dist
BUILD_WEB = "auto"

# 打包后调 prune.py 删掉 PyInstaller 多收的文件（Tcl/Tk 运行时、重复的 GDAL
# 数据、OpenCV 视频插件、用不到的 WebView2Loader 架构）
DO_PRUNE = True

# 产物里不该出现的东西：出现就说明排除清单漏了，打包失败。按文件或目录名
# 匹配（不分大小写，前缀匹配）。
FORBIDDEN = (
    ("PySide6", "Qt 界面库"), ("PySide2", "Qt 界面库"), ("PyQt5", "Qt 界面库"),
    ("PyQt6", "Qt 界面库"), ("Qt5Core", "Qt 运行库"), ("Qt6Core", "Qt 运行库"),
    ("torch", "PyTorch"), ("scipy", "SciPy"), ("sympy", "SymPy"), ("skimage", "scikit-image"),
    ("sklearn", "scikit-learn"), ("pandas", "pandas"), ("IPython", "IPython"),
    ("tcl86", "Tcl 运行时"), ("tk86", "Tk 运行时"), ("_tkinter", "tkinter"),
)
# 出现了只提示、不拦：MKL 是 numpy 的另一种数学库来源（Anaconda 默认频道），
# 能用但比 conda-forge 的 OpenBLAS 大几百兆
WARN_ONLY = (("mkl_", "Intel MKL 数学库（换成 conda-forge 的 numpy 可省几百兆）"),)

# 打包完列出包内最大的几项，方便一眼看出有没有多带东西
SHOW_LARGEST = 15

# 冒烟测试，两步：
#   1. 自检——设 LITHOSPECTRA_SELFTEST 启动产物，它不开界面，把 GDAL 驱动、
#      proj.db、后端接口、页面、各功能模块、随包数据挨个点一遍，报告写成 JSON。
#   2. 真启动，等窗口里的页面把界面状态写回后端，确认页面真的连上了后端。
# 两步都在临时的用户目录下跑，不碰本机已有的数据。
DO_SMOKE_TEST = True
# 冒烟测试时在 PATH 最前面放一份冒名的 gdal.dll，模拟装过 QGIS / ArcGIS 的电脑
FAKE_FOREIGN_GDAL = True
SELFTEST_TIMEOUT = 120
SMOKE_TIMEOUT = 60

# 额外压一个 zip 便于分发。几百兆压下来要一两分钟，不需要就关掉。
MAKE_ZIP = True

# 包装着、但它依赖的子包缺了（比如 pywebview 在，它要的 proxy_tools 没装上）时，
# 自动 pip install 补上缺的那几个再查一遍。conda env update 补不了这种缺口：它看到
# pywebview 已经装着就跳过了。
AUTO_PIP_FIX = True

# 要求当前解释器的版本与 environment.yml 里的 python 钉子一致。
# 打开着别关：环境版本不对时 PyInstaller 照样能跑完，最后交出一个起不来的
# exe，比一开始就报错难查得多。
REQUIRE_PINNED_PYTHON = True

# =============================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # LithoSpectra-Studio/
WEB = os.path.join(ROOT, "web")

# 退出码 3 = 环境里缺依赖。build_windows.bat 看到它会按 environment.yml 把环境
# 更新一遍再重跑一次——环境是早先建的、后来 environment.yml 又加了包时，这样
# 不用人记得去补。
RC_DEPS_MISSING = 3

# Edge WebView2 运行时在注册表里的登记（Evergreen 版，系统级与用户级两处）
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def human(n):
    if n >= 1 << 30:
        return "%.2f GB" % (n / float(1 << 30))
    if n >= 1 << 20:
        return "%.1f MB" % (n / float(1 << 20))
    return "%.0f KB" % (n / 1024.0)


def dir_size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            full = os.path.join(root, f)
            if os.path.islink(full):
                continue
            try:
                total += os.path.getsize(full)
            except OSError:
                pass
    return total


def step(text):
    print()
    print("==> " + text)


def fail(text):
    print()
    print("×  " + text)
    return 1


# --------------------------------------------------------------------- 环境
def check_platform():
    if sys.platform != "win32":
        print("本脚本用于 Windows。macOS 上请改用 packaging/build_web_macos.sh。")
        return False
    return True


def pinned_python():
    """读 environment.yml 里的 ``- python=X.Y`` 钉子，读不到返回空串。"""
    path = os.path.join(ROOT, "environment.yml")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                text = line.strip()
                if text.startswith("- python="):
                    return text.split("=", 1)[1].split()[0].split("#")[0].strip()
    except OSError:
        pass
    return ""


def check_python_version():
    """当前解释器得是 environment.yml 描述的那个。

    名字叫 samplelib 的环境不等于 environment.yml 建出来的环境——手工
    ``conda create -n samplelib python`` 会拿到最新的 Python，再 pip 装一堆
    包，看着一切正常，PyInstaller 也能跑完，最后交出的 exe 一启动就报
    「DLL load failed」。所以宁可在开头就拦住。
    """
    pin = pinned_python()
    if not pin:
        return True
    want = ".".join(pin.split(".")[:2])
    cur = "%d.%d" % sys.version_info[:2]
    if cur == want:
        return True
    print("Python 版本与 environment.yml 对不上：")
    print("    environment.yml 要求 python=%s" % pin)
    print("    当前解释器是     Python %s（%s）" % (cur, sys.prefix))
    print()
    print("这个环境不是 environment.yml 建出来的。重建一遍：")
    print("    conda deactivate")
    print("    conda env remove -n samplelib")
    print("    conda env create -f \"%s\"" % os.path.join(ROOT, "environment.yml"))
    print()
    print("确实想用当前环境硬打，把 build_windows.py 顶部的")
    print("REQUIRE_PINNED_PYTHON 改成 False。")
    return False


def wanted_modules():
    """这次打包装哪些功能模块（与 spec 同一个读法）。"""
    profile = ""
    try:
        with open(os.path.join(ROOT, "packaging", "LithoSpectraWeb.spec"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PROFILE ="):
                    profile = line.split("=", 1)[1].strip().strip("\"'")
                    break
    except OSError:
        pass
    if not profile:
        with open(os.path.join(ROOT, "samplelib", "config.py"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MODULE_PROFILE"):
                    profile = line.split("=", 1)[1].split("#")[0].strip().strip("\"'")
                    break
    path = os.path.join(ROOT, "samplelib", "profiles", "%s.json" % (profile or "full"))
    try:
        with open(path, encoding="utf-8") as fh:
            return list(json.load(fh).get("modules", ()))
    except (OSError, ValueError):
        return []


def check_deps():
    """缺哪个说哪个，一次说全，免得装一个报一个。

    返回 0 齐全；RC_DEPS_MISSING 是"装着的能用、只是少了几个"，按
    environment.yml 补一遍就好；1 是装着却加载不起来，得重建环境。

    ``clr`` 是 pythonnet：pywebview 在 Windows 上靠它调 .NET 的 WinForms 与
    WebView2。只 ``import webview`` 不会加载它，环境里没装要到真开窗口时才
    炸，所以单独查。
    """
    need = [("PyInstaller", "pip install pyinstaller"),
            ("numpy", "conda install -c conda-forge numpy"),
            ("PIL", "conda install -c conda-forge pillow"),
            ("osgeo.gdal", "conda install -c conda-forge gdal"),
            ("matplotlib", "conda install -c conda-forge matplotlib-base"),
            ("fastapi", "pip install fastapi"),
            ("uvicorn", "pip install uvicorn"),
            ("webview", "pip install pywebview"),
            ("clr", "pip install pythonnet")]
    if "fieldwork" in wanted_modules():
        need += [("piexif", "pip install piexif"),
                 ("onnxruntime", "pip install onnxruntime"),
                 ("cv2", "pip install opencv-python-headless")]
    def probe():
        out = []
        for mod, hint in need:
            try:
                __import__(mod)
            except Exception as exc:              # noqa: BLE001 — clr 加载失败抛的不是 ImportError
                out.append((mod, hint, exc))
        return out

    missing = probe()
    # 缺的是某个包依赖的子包：exc.name 不是我们要的那个模块本身
    transitive = sorted({exc.name for mod, _h, exc in missing
                         if isinstance(exc, ModuleNotFoundError) and exc.name
                         and exc.name.split(".")[0] != mod.split(".")[0]})
    if missing and transitive and AUTO_PIP_FIX:
        print("    已装的包缺了依赖：%s，用 pip 补上" % "、".join(transitive))
        subprocess.call([sys.executable, "-m", "pip", "install"]
                        + [n.split(".")[0].replace("_", "-") for n in transitive])
        for name in list(sys.modules):
            if name.split(".")[0] in {m.split(".")[0] for m, _h, _e in missing}:
                del sys.modules[name]
        missing = probe()
    missing = [(mod, hint, str(exc)) for mod, hint, exc in missing]

    try:
        __import__("h5py")
    except ImportError:
        print("提示：没有 h5py，打出来的包里「裁剪样本块」选 h5 输出会用不了。")
        print("      要带上它：conda install -c conda-forge h5py")
        print()

    if missing:
        print("依赖不可用，当前解释器：%s" % sys.executable)
        for mod, hint, err in missing:
            print("    %-16s → %s" % (mod, hint))
            print("        %s" % err)
        print()
        if any("DLL load failed" in err for _m, _h, err in missing):
            print("报「DLL load failed」说明包装着但加载不起来，多半是环境里")
            print("混了两套来源的库（conda 装一份、pip 又装一份），互相顶掉了。")
            print("重装单个包一般解决不了，把环境整个重建最快：")
            print("    conda env remove -n samplelib")
            print("    conda env create -f \"%s\"" % os.path.join(ROOT, "environment.yml"))
            return 1
        print("环境比 environment.yml 旧了（建好之后清单又加过包）。按清单补齐：")
        print("    conda env update -n samplelib -f \"%s\""
              % os.path.join(ROOT, "environment.yml"))
        print("双击 build_windows.bat 跑的话，它看到这个退出码会自己补、再重跑一次。")
        return RC_DEPS_MISSING
    return 0


def webview2_version():
    """本机装的 Edge WebView2 运行时版本，没装返回空串。"""
    try:
        import winreg
    except ImportError:
        return ""
    keys = [(winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s" % WEBVIEW2_GUID),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\EdgeUpdate\Clients\%s" % WEBVIEW2_GUID),
            (winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\EdgeUpdate\Clients\%s" % WEBVIEW2_GUID)]
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as key:
                value, _kind = winreg.QueryValueEx(key, "pv")
                if value and value != "0.0.0.0":
                    return str(value)
        except OSError:
            continue
    return ""


def prepend_dll_path():
    """把 conda 的 Library\\bin 顶到 PATH 前面。

    conda 布局下 GDAL 的 Python 扩展在 site-packages\\osgeo，真正的
    gdal*.dll / proj*.dll 却在 %CONDA_PREFIX%\\Library\\bin。PyInstaller 靠
    PATH 顺藤摸瓜找这些依赖；正常 activate 过就已经在 PATH 里，这里只是
    补一道保险——它没进去的话，打出来的包一启动就报 DLL 找不到。
    """
    extra = [os.path.join(sys.prefix, "Library", "bin"),
             os.path.join(sys.prefix, "Library", "mingw-w64", "bin"),
             os.path.join(sys.prefix, "DLLs")]
    have = [p.lower() for p in os.environ.get("PATH", "").split(os.pathsep)]
    add = [p for p in extra if os.path.isdir(p) and p.lower() not in have]
    if add:
        os.environ["PATH"] = os.pathsep.join(add + [os.environ.get("PATH", "")])
        for p in add:
            print("    PATH += %s" % p)


# --------------------------------------------------------------------- 页面
def build_web():
    """编译 web/ 下的页面，产物在 web/dist。返回是否可以继续。"""
    index = os.path.join(WEB, "dist", "index.html")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if BUILD_WEB == "never" or (BUILD_WEB == "auto" and not npm):
        if os.path.isfile(index):
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(index)))
            print("    用已有的 web\\dist（编译于 %s）" % stamp)
            if not npm:
                print("    本机没有 Node.js；页面改过的话，先在装了 Node.js 的机器上")
                print("    npm run build，再把 web\\dist 拷过来。")
            return True
        print("    没有 web\\dist，本机也没有 Node.js（npm）。二选一：")
        print("      · 装 Node.js（https://nodejs.org，LTS 版），重跑本脚本；")
        print("      · 在 Mac 上 cd web && npm run build，把 web\\dist 整个拷过来。")
        return False
    if not npm:
        print("    BUILD_WEB = \"always\"，但找不到 npm。先装 Node.js。")
        return False
    if not os.path.isdir(os.path.join(WEB, "node_modules")):
        lock = os.path.isfile(os.path.join(WEB, "package-lock.json"))
        cmd = [npm, "ci" if lock else "install"]
        print("    " + " ".join(cmd))
        if subprocess.call(cmd, cwd=WEB) != 0:
            print("    安装前端依赖失败，检查网络 / npm 镜像。")
            return False
    print("    %s run build" % npm)
    if subprocess.call([npm, "run", "build"], cwd=WEB) != 0:
        print("    页面编译失败，见上面的报错。")
        return False
    return os.path.isfile(index)


# --------------------------------------------------------------------- 构建
def clean():
    """删掉上次的产物。删不干净就停：PyInstaller 自己再删一遍也一样会失败，
    而它报出来的是一大屏 shutil 的堆栈，不如这里直接说清楚。"""
    stuck = []
    for path in (os.path.join(ROOT, DIST_DIR), os.path.join(ROOT, WORK_DIR)):
        if not os.path.isdir(path):
            continue
        shutil.rmtree(path, onerror=lambda _fn, p, _exc: stuck.append(p))
        if os.path.isdir(path):
            print("    删不干净：%s" % path)
        else:
            print("    已删除 %s" % path)
    if not stuck:
        return True
    print()
    print("    以下文件删不掉（共 %d 个），前几个：" % len(stuck))
    for p in stuck[:5]:
        print("        %s" % p)
    print()
    print("    常见原因：")
    print("      · 上次打出来的 %s.exe 还开着——关掉再来；" % APP_NAME)
    print("      · 目录在共享盘 / 网络盘上，里面有 Windows 删不掉的符号链接——")
    print("        把仓库整个拷到本机磁盘（C:\\ 或 D:\\）再打；")
    print("      · 资源管理器或杀毒软件正占着某个文件——稍等再试。")
    print("    也可以手工删掉上面两个目录后重跑。")
    return False


def run_pyinstaller():
    cmd = [sys.executable, "-m", "PyInstaller", SPEC, "--noconfirm",
           "--distpath", DIST_DIR, "--workpath", WORK_DIR, "--log-level", "WARN"]
    print("    " + " ".join(cmd))
    print()
    # cwd 必须是 LithoSpectra-Studio：spec 用 os.getcwd() 定位入口与随包数据。
    return subprocess.call(cmd, cwd=ROOT)


def check_gdal_shipped(bundle):
    """产物里有没有 GDAL 的 DLL。没有的话装到别人机器上必崩。"""
    for root, _dirs, files in os.walk(bundle):
        for f in files:
            low = f.lower()
            if low.endswith(".dll") and low.startswith("gdal"):
                return os.path.join(root, f)
    return ""


def audit_contents(bundle):
    """查有没有带进不该带的库，列出最大的几项。有违禁项返回 False。"""
    hits, warns = [], []
    for root, dirs, files in os.walk(bundle):
        for name in dirs + files:
            low = name.lower()
            for prefix, what in FORBIDDEN:
                if low.startswith(prefix.lower()):
                    hits.append("%s（%s）" % (os.path.relpath(os.path.join(root, name), bundle), what))
            for prefix, what in WARN_ONLY:
                if low.startswith(prefix.lower()):
                    warns.append("%s（%s）" % (name, what))

    internal = os.path.join(bundle, CONTENTS_DIR)
    base = internal if os.path.isdir(internal) else bundle
    sizes = sorted(((dir_size(os.path.join(base, n)), n) for n in os.listdir(base)), reverse=True)
    print("    包内最大的 %d 项：" % SHOW_LARGEST)
    for n, name in sizes[:SHOW_LARGEST]:
        print("      %10s  %s" % (human(n), name))
    for line in sorted(set(warns))[:5]:
        print("    提示：%s" % line)
    if hits:
        print()
        print("    包里带进了程序用不到的库：")
        for line in hits[:20]:
            print("      × %s" % line)
        print("    把它们加进 packaging\\LithoSpectraWeb.spec 的 excludes，或者从构建环境里卸掉。")
        return False
    print("    没有带进 Qt、PyTorch、SciPy 等无关的库")
    return True


def prune(bundle):
    return subprocess.call([sys.executable, os.path.join("packaging", "prune.py"),
                            bundle], cwd=ROOT)


def sandbox_env(home, bundle=""):
    """让产物把数据写进临时目录，而不是产物旁的 userdata 或本机的「文档\\LithoSpectra」。"""
    env = dict(os.environ)
    env["LITHOSPECTRA_DATA_DIR"] = os.path.join(home, "userdata")
    for var in ("USERPROFILE", "HOME"):
        env[var] = home
    env["APPDATA"] = os.path.join(home, "AppData", "Roaming")
    env["LOCALAPPDATA"] = os.path.join(home, "AppData", "Local")
    for var in ("APPDATA", "LOCALAPPDATA"):
        os.makedirs(env[var], exist_ok=True)
    os.makedirs(os.path.join(home, "Documents"), exist_ok=True)
    # 构建机环境里的 conda 变量不能漏给产物，否则它会去用环境里那份 GDAL / PROJ 数据
    for var in ("GDAL_DATA", "PROJ_LIB", "PROJ_DATA", "GDAL_DRIVER_PATH", "PYTHONPATH", "PYTHONHOME"):
        env.pop(var, None)
    # PATH 按"别人的电脑"来摆：去掉构建环境（conda）的目录——它们放着同版本的 GDAL，
    # 会把缺 DLL 的问题遮住；再在最前面放一份"别的软件的 gdal.dll"，模拟装过 QGIS /
    # ArcGIS 的电脑。产物必须只用自己包里的那份
    keep = [p for p in env.get("PATH", "").split(os.pathsep)
            if p and not os.path.normcase(os.path.abspath(p)).startswith(
                os.path.normcase(os.path.abspath(sys.prefix)))]
    paths = keep
    if FAKE_FOREIGN_GDAL and bundle:
        fake = os.path.join(home, "foreign_gdal")
        os.makedirs(fake, exist_ok=True)
        donor = os.path.join(bundle, CONTENTS_DIR, "zlib.dll")
        if os.path.isfile(donor):
            shutil.copyfile(donor, os.path.join(fake, "gdal.dll"))
            paths = [fake] + keep
    env["PATH"] = os.pathsep.join(paths)
    return env


def self_test(exe, report_path, home):
    """让产物自检一遍，读回 JSON 报告。通过返回 True。"""
    if os.path.exists(report_path):
        os.remove(report_path)
    env = sandbox_env(home, os.path.dirname(exe))
    env["LITHOSPECTRA_SELFTEST"] = report_path
    try:
        proc = subprocess.run([exe], cwd=os.path.dirname(exe), env=env,
                              timeout=SELFTEST_TIMEOUT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        print("    自检 %d 秒没跑完，放弃。多半是一启动就弹出了报错框（比如 GDAL 没加载起来），"
              "双击产物看报错。" % SELFTEST_TIMEOUT)
        return False

    try:
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError) as exc:
        print("    自检没写出报告（退出码 %s）：%s" % (rc, exc))
        print("    多半是一启动就崩了；下一步的真启动会把输出打出来。")
        return False

    checks = report.get("checks") or {}
    gdal = checks.get("gdal") or {}
    shell = checks.get("web_shell") or {}
    mods = checks.get("module_imports") or {}
    if gdal:
        print("    GDAL %s，%d 个驱动，JPEG2000 %s" % (
            gdal.get("version"), gdal.get("drivers", 0),
            "可用" if gdal.get("jp2") else "不可用（环境里没装 libgdal-jp2openjpeg）"))
    if shell:
        print("    后端 %d 个接口，装上的模块：%s" % (shell.get("routes", 0),
                                                 "、".join(shell.get("modules") or []) or "无"))
    if mods:
        print("    子模块 %d 个可导入" % mods.get("submodules", 0))
    h5 = checks.get("h5py") or {}
    if h5 and not h5.get("present"):
        print("    h5py 未随包：裁剪样本块选 h5 输出会用不了")
    for line in report.get("problems") or []:
        print("    × " + line)
    if report.get("ok"):
        print("    自检通过")
        return True
    print("    自检未通过，完整报告：%s" % report_path)
    return False


def kill_tree(proc):
    """结束产物及它拉起的 WebView2 子进程。"""
    subprocess.call(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def smoke_test(exe, log_path, home):
    """真启动产物，等页面把界面状态写回后端。"""
    data = os.path.join(home, "userdata", "state")
    state = os.path.join(data, "web_session.json")
    # 与双击启动一样不给标准输入输出句柄：无控制台程序这时 sys.stdout 是 None，
    # 重定向到文件反倒会把这类问题藏起来。程序自己把输出写到 state\last_run.log
    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe), env=sandbox_env(home, os.path.dirname(exe)),
                            stdin=None, stdout=None, stderr=None, close_fds=True)
    for _ in range(SMOKE_TIMEOUT):
        time.sleep(1)
        if os.path.isfile(state) or proc.poll() is not None:
            break
    alive = proc.poll() is None
    ok = os.path.isfile(state)
    if alive:
        kill_tree(proc)
    text = ""
    for name in ("last_run.log",):
        try:
            with open(os.path.join(data, name), "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            pass
    try:
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass
    if not alive and not ok:
        print("    启动失败，退出码 %s。输出如下：" % proc.returncode)
        print(text or "    （程序没有留下输出；多半在写日志之前就崩了，双击产物看弹出的报错）")
        return False
    if "桌面窗口打不开" in text:
        # 外壳在窗口开不起来时会退回系统浏览器，页面照样连得上后端，不能算通过
        print("    桌面窗口没开起来（退回了系统浏览器）。输出如下：")
        print(text)
        return False
    if ok:
        print("    启动正常，窗口里的页面已连上后端")
        return True
    print("    进程活着，但 %d 秒内页面没有连上后端。" % SMOKE_TIMEOUT)
    if not webview2_version():
        print("    本机没有 Edge WebView2 运行时，窗口开不起来。装上再试：")
        print("        https://developer.microsoft.com/microsoft-edge/webview2/")
    print("    日志：%s" % log_path)
    return False


def make_zip(bundle):
    base = os.path.join(ROOT, DIST_DIR, "%s-win64" % APP_NAME)
    return shutil.make_archive(base, "zip",
                               root_dir=os.path.dirname(bundle),
                               base_dir=os.path.basename(bundle))


# --------------------------------------------------------------------- 主流程
def main():
    if not check_platform():
        return 1

    print("解释器 : %s" % sys.executable)
    print("环境   : %s" % sys.prefix)
    print("项目   : %s" % ROOT)

    step("检查环境")
    if REQUIRE_PINNED_PYTHON and not check_python_version():
        return 1
    print("    Python %d.%d" % sys.version_info[:2])
    wv = webview2_version()
    print("    Edge WebView2 %s" % (wv or "未安装（打包照常进行，但第 2 步冒烟测试开不了窗口）"))

    step("检查依赖")
    rc = check_deps()
    if rc:
        return rc
    print("    齐全；装上的模块：%s" % ("、".join(wanted_modules()) or "无"))

    step("补全 DLL 搜索路径")
    prepend_dll_path()

    if not os.path.isfile(os.path.join(ROOT, "assets", "logo.ico")):
        return fail("缺少图标 assets\\logo.ico，先跑 tools\\make_logo.py 生成。")

    step("准备页面（web\\dist）")
    if not build_web():
        return fail("没有可用的页面。")

    if CLEAN_BEFORE:
        step("清理上次的产物")
        if not clean():
            return fail("旧产物删不干净，见上。")

    step("打包（几分钟，PyInstaller 的告警可以忽略）")
    rc = run_pyinstaller()
    if rc != 0:
        return fail("PyInstaller 失败，退出码 %d。" % rc)

    bundle = os.path.join(ROOT, DIST_DIR, APP_NAME)
    exe = os.path.join(bundle, APP_NAME + ".exe")
    if not os.path.isfile(exe):
        return fail("没找到产物 %s。" % exe)

    step("检查 GDAL 是否随包带上")
    dll = check_gdal_shipped(bundle)
    if dll:
        print("    %s" % os.path.basename(dll))
        plugins = os.path.join(bundle, CONTENTS_DIR, "gdal_plugins")
        if os.path.isdir(plugins):
            print("    驱动插件：%s" % "、".join(
                f for f in sorted(os.listdir(plugins)) if f.lower().endswith(".dll")))
        else:
            print("    没有驱动插件目录：产物只有 libgdal 内建的驱动（读不了 .jp2）。")
            print("    environment.yml 里有 libgdal-jp2openjpeg，环境比它旧的话：")
            print("        conda env update -n samplelib -f environment.yml")
    else:
        print("    没找到 gdal*.dll。多半是打包时 %CONDA_PREFIX%\\Library\\bin")
        print("    不在 PATH 上。请从「开始菜单 → Anaconda Prompt」里执行：")
        print("        conda activate samplelib")
        print("        cd /d \"%s\"" % ROOT)
        print("        python packaging\\build_windows.py")
        return fail("产物里没有 GDAL。")

    if DO_PRUNE:
        step("裁剪")
        if prune(bundle) != 0:
            print("    裁剪脚本返回非零，产物仍可用，继续。")

    step("检查包里有没有无关的库")
    if not audit_contents(bundle):
        return fail("产物里带进了用不到的库，见上。")

    if DO_SMOKE_TEST:
        os.makedirs(os.path.join(ROOT, WORK_DIR), exist_ok=True)
        home = tempfile.mkdtemp(prefix="lithospectra_smoke_")
        try:
            step("冒烟测试 1/2：自检（GDAL 驱动、proj.db、后端接口、页面、模块、随包数据）")
            report_path = os.path.join(ROOT, WORK_DIR, "selftest.json")
            passed = self_test(exe, report_path, home)

            step("冒烟测试 2/2：真启动，看后端与页面是否起来")
            log_path = os.path.join(ROOT, WORK_DIR, "run.log")
            if not smoke_test(exe, log_path, home):
                return fail("产物启动失败，日志见 %s" % log_path)
            if not passed:
                return fail("产物起得来但自检没过，报告见 %s" % report_path)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    zip_path = ""
    if MAKE_ZIP:
        step("压成 zip（较慢，配置区可关）")
        zip_path = make_zip(bundle)

    print()
    print("=" * 74)
    print("完成")
    print("=" * 74)
    print("    可执行文件  %s" % exe)
    print("    目录体积    %s" % human(dir_size(bundle)))
    if zip_path:
        print("    分发包      %s（%s）" % (zip_path, human(os.path.getsize(zip_path))))
    print("=" * 74)
    print("分发时把整个 %s 目录（或上面的 zip）一起给对方，" % APP_NAME)
    print("单拷一个 exe 出来是跑不起来的。数据放在 exe 旁的 userdata\\，")
    print("首次启动时会把旧版本在「文档\\LithoSpectra」中的数据移过来。")
    print("对方电脑需要 Edge WebView2 运行时（Windows 11 自带；Windows 10 没有时到")
    print("https://developer.microsoft.com/microsoft-edge/webview2/ 下载）。")
    print("产物未做代码签名，SmartScreen 拦截时点「更多信息 → 仍要运行」放行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
