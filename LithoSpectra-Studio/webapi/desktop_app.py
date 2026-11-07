# -*- coding: utf-8 -*-
"""岩谱 LithoSpectra —— 桌面外壳。

界面是 ``web/`` 下的 React 程序，编译成静态文件后由本进程内的 FastAPI 提供；
桌面窗口用 pywebview（macOS 上是系统 WebKit，Windows 上是 Edge WebView2），
存储与会话层在 ``samplelib``。

程序以打包好的 .app / .exe 运行（``bash packaging/build_web_macos.sh``），入口是
``packaging/entry_web.py``，它只调用这里的 :func:`main`。
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

# =============================================================================
# 配置区（按需修改）
# =============================================================================

# 没有上次打开的样本库时（首次启动）打开的样本库(.gpkg)，相对路径以数据目录为基准。
# 留空时：spec 设置了 SEED_LIBRARY 则把随包样本库复制到数据目录的 data/ 下打开，
# 否则打开空界面，由使用者新建或打开自己的库。
LIBRARY_PATH = ""

# 启动时优先打开上次退出时开着的样本库
RESTORE_LAST_LIBRARY = True

# 界面来源：
#   dist  使用 web/dist 下编译好的静态文件（正常使用、打包分发）
#   dev   连到 `npm run dev` 起的开发服务器，改前端代码即时刷新
UI_SOURCE = "dist"
DEV_SERVER_URL = "http://127.0.0.1:5173"

# 后端端口。0 表示自动挑一个空闲端口；dev 模式下固定为 8765，供 vite 代理转发。
PORT = 0

# 桌面窗口：True 用 pywebview 开原生窗口；False 只起服务并用系统浏览器打开
DESKTOP_WINDOW = True
WINDOW_SIZE = (1440, 900)

# 打开开发者工具（右键「检查元素」），调试界面时用
DEBUG = False

# =============================================================================
# 启动
# =============================================================================

#: 项目根目录（LithoSpectra-Studio/）：源码运行时找 web/dist 用
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_dir() -> str:
    """只读资源的根目录：源码运行时是项目目录，打包后是 PyInstaller 的解包目录。"""
    return getattr(sys, "_MEIPASS", HERE)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def first_run_library(path: str) -> None:
    """打包产物首次启动：把随包的那份样本库复制到数据目录，装上就有数据可看。

    源码运行时包里没有种子库，什么都不做。
    """
    import shutil
    import stat
    from samplelib import config
    seed = config.seed_library()
    if not seed:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shutil.copy2(seed, path)
    # 包内的文件可能是只读的，复制出来必须能写，否则一落库就报错
    os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
    print("已从随包样本库初始化：%s" % path)


def resolve_library() -> str:
    """启动时打开哪个样本库：上次开着的那个优先；没有（首次启动、上次的库已不在）
    才用默认库，默认库不存在时从随包种子库复制一份。"""
    from samplelib import appstate, config
    last = appstate.last_library() if RESTORE_LAST_LIBRARY else ""
    if last:
        return last
    if LIBRARY_PATH:
        default = os.path.join(config.app_data_dir(), LIBRARY_PATH)
    else:
        seed = config.seed_library()
        default = os.path.join(config.app_data_dir(), "data", os.path.basename(seed)) if seed else ""
    if default and not os.path.exists(default):
        first_run_library(default)
    path = default
    if path and not os.path.exists(path):
        print("样本库不存在，将打开空界面：%s" % path)
        return ""
    return path


def start_server(port: int):
    import uvicorn
    from webapi import create_app

    static = os.path.join(resource_dir(), "web", "dist")
    if UI_SOURCE == "dist" and not os.path.isfile(os.path.join(static, "index.html")):
        raise SystemExit("找不到 web/dist/index.html，先在 web/ 目录下运行 npm install && npm run build。")
    app = create_app(resolve_library(), static_dir=static if UI_SOURCE == "dist" else "")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True, name="api").start()

    # 等服务起来再开窗口，免得窗口先打开、页面先报一次连接失败
    for _ in range(100):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=0.5)
            return app
        except Exception:
            time.sleep(0.1)
    raise SystemExit("后端没能在 10 秒内启动。")


class DesktopApi:
    """暴露给页面的原生能力：文件对话框。页面里通过 window.pywebview.api 调用。"""

    def __init__(self) -> None:
        # 下划线开头：pywebview 会遍历 js_api 对象的公开属性暴露给页面，Windows 上顺着
        # 窗口对象钻进 WinForms 原生对象会无限递归
        self._window = None

    def _dialog(self, kind, **kwargs):
        import webview
        result = self._window.create_file_dialog(kind, **kwargs)
        if not result:
            return None
        return result if isinstance(result, str) else result[0]

    # 文件类型过滤：键是页面里用的名字，值是 pywebview 的过滤串
    FILTERS = {
        "library": ("样本库 (*.gpkg)", "所有文件 (*.*)"),
        "vector": ("矢量 (*.shp;*.geojson;*.json;*.gpkg)", "所有文件 (*.*)"),
        "image": ("影像 (*.tif;*.tiff;*.dat;*.img;*.vrt;*.bsq;*.bil;*.bip;*.jp2)",
                  "所有文件 (*.*)"),
        "any": ("所有文件 (*.*)",),
        "ovobj": ("奥维收藏夹 (*.ovobj)", "所有文件 (*.*)"),
        "photo": ("照片 (*.jpg;*.jpeg;*.png;*.heic;*.tif;*.tiff)", "所有文件 (*.*)"),
        "track": ("轨迹与点位 (*.gpx;*.kml;*.kmz;*.geojson;*.json;*.shp)", "所有文件 (*.*)"),
    }

    def pick_file(self, kind="any", directory=""):
        import webview
        return self._dialog(webview.FileDialog.OPEN, directory=directory or "",
                            file_types=self.FILTERS.get(kind, self.FILTERS["any"]))

    def pick_files(self, kind="any", directory=""):
        """多选文件，返回路径列表（成批导入照片用）。"""
        import webview
        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN, directory=directory or "", allow_multiple=True,
            file_types=self.FILTERS.get(kind, self.FILTERS["any"]))
        if not result:
            return []
        return [result] if isinstance(result, str) else list(result)

    def dropped_paths(self, names):
        """页面收到 drop 后按文件名来换真实路径（见 :func:`enable_drop_paths`）。

        原生层在 drop 之前就记下了路径；同名文件按先来先用配对。没配上的返回时略过。
        """
        import urllib.parse
        try:
            from webview.dom import _dnd_state
        except Exception:
            return []
        pool = _dnd_state.get("paths")
        if pool is None:
            return []
        out = []
        for name in names or []:
            for item in list(pool):
                if urllib.parse.unquote(item[0]) == name:
                    out.append(urllib.parse.unquote(item[1]))
                    pool.remove(item)
                    break
        return out

    def pick_folder(self, directory=""):
        import webview
        return self._dialog(webview.FileDialog.FOLDER, directory=directory or "")

    def save_file(self, filename="", directory=""):
        import webview
        return self._dialog(webview.FileDialog.SAVE, directory=directory or "",
                            save_filename=filename or "")

    def open_library_dialog(self):
        return self.pick_file("library")

    def reveal_in_folder(self, path):
        """在访达 / 资源管理器里显示并选中这个文件。"""
        if not path or not os.path.exists(path):
            return False
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        elif os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return True

    def copy_text(self, text):
        """写系统剪贴板。WebView 里的 navigator.clipboard 受权限限制，不可靠。"""
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
        elif os.name == "nt":
            subprocess.run(["clip"], input=text.encode("utf-16-le"), check=False)
        else:
            return False
        return True


def confirm_close(window, app) -> bool:
    """关窗前还有未保存的标注改动就问一句；返回 False 取消关闭。"""
    buf = app.state.lithospectra.buffer
    if buf is None or not buf.dirty:
        return True
    title = "有未保存的改动"
    message = ("%s\n\n关闭窗口将丢失这些改动。如需保存，请点击取消，然后点击「保存到样本库」。"
               % buf.summary())
    if sys.platform == "darwin":
        return _confirm_on_main_thread(title, message)
    return window.create_confirmation_dialog(title, message)


def _confirm_on_main_thread(title: str, message: str) -> bool:
    """macOS：直接在主线程弹确认框。

    pywebview 在主线程里同步执行关窗事件，而 ``window.create_confirmation_dialog``
    会把弹窗排到主线程再原地等结果，两者互相等待，程序卡死。关窗事件本身就在主线程，
    这里直接调用 NSAlert。
    """
    import AppKit
    AppKit.NSRunningApplication.currentApplication().activateWithOptions_(
        AppKit.NSApplicationActivateIgnoringOtherApps)
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_("关闭窗口")
    alert.addButtonWithTitle_("取消")
    alert.setAlertStyle_(AppKit.NSWarningAlertStyle)
    return alert.runModal() == AppKit.NSAlertFirstButtonReturn


# 菜单栏：四个顶级菜单（文件 / 编辑 / 视图 / 帮助）。每一项是
# (显示名, 动作号)，动作号交给页面里的 window.lithoAction 去办——菜单栏、命令条、
# 快捷键走的是同一个入口。None 是分隔线，(名字, [...]) 是子菜单。
MENUS = [
    ("文件", [
        ("新建样本库…", "new"), ("打开样本库…", "open"), ("关闭样本库", "close"), None,
        ("保存到样本库", "save"), ("另存为…", "save-as"), ("备份到…", "backup"),
        ("从备份还原…", "restore"), ("在访达中显示" if sys.platform == "darwin" else "在资源管理器中显示", "reveal"), None,
        ("导入", [("导入样本目录…", "import-dir"), ("导入样本库根目录…", "import-root"),
                  ("导入单个矢量…", "import-file"), None, ("按属性导入…", "import-attr")]),
        ("导出样本…", "export"), ("裁剪样本块…", "clip"), None,
        ("添加图层", [("添加影像（ENVI / GeoTIFF）…", "add-raster"),
                      ("添加配准图片（JPG / PNG）…", "add-image"), ("添加参考矢量…", "add-vector")]),
        ("工具箱…", "toolbox"),
    ]),
    ("编辑", [
        ("撤销", "undo"), ("重做", "redo"), None,
        ("新建工区…", "new-region"), None,
        ("全部勾选", "check-all"), ("全部取消勾选", "uncheck-all"), ("反向勾选", "check-invert"),
        ("按当前范围重新选择…", "roi-again"), ("清除全部导出层级标记", "clear-levels"), None,
        ("查看被改动的类别…", "show-edited"), ("恢复全部类别到导入时的状态…", "restore-all"), None,
        ("删除选中要素", "delete-sel"), ("将选中要素改归到当前类别", "move-sel"),
        ("将选中要素改归到其他类别…", "move-sel-to"), ("用选中要素新建类别…", "new-class-sel"),
        ("给选中要素命名…", "label-sel"), ("清空画布选中", "clear-sel"), ("将画布选中的样本加入勾选", "check-sel"), None,
        ("按图层范围选择样本…", "roi-layer"), ("清空手绘范围", "roi-clear"), None,
        ("放弃全部未保存改动", "discard"), ("重新从样本库载入", "reload"),
    ]),
    ("视图", [
        ("浏览模式", "browse"), ("标注模式", "annotate"), None,
        ("全图", "zoom-all"), ("缩放到选中", "zoom-sel"), None,
        ("捕捉顶点", "snap"), ("只显示勾选的样本", "only-checked"), ("显示经纬网", "grid"), None,
        ("样本与图层栏", "toggle-tree"), ("检查器与要素信息栏", "toggle-inspector"), None,
        ("光谱曲线…", "spectrum"), ("光谱库…", "speclib"), None,
        ("外观", [("跟随系统", "theme-system"), ("浅色", "theme-light"), ("深色", "theme-dark")]),
    ]),
    ("帮助", [("使用说明", "help"), ("关于", "about")]),
]

# 野外记录模块装了才有的顶级菜单，插在「视图」之后
FIELDWORK_MENU = ("野外", [
    ("野外记录…", "fw-panel"), ("在画布上显示野外记录", "fw-toggle"), None,
    ("导入奥维收藏夹（.ovobj，可多选）…", "fw-import-ovobj"), ("导入奥维收藏夹文件夹…", "fw-import-ovobj-folder"),
    ("导入照片…", "fw-import-photos"),
    ("导入照片文件夹…", "fw-import-folder"), ("导入轨迹与点位（GPX / KML）…", "fw-import-vector"), None,
    ("缩放到野外记录", "fw-zoom"),
])

# 深度学习模块装了才有的顶级菜单
DEEPLEARN_MENU = ("深度学习", [
    ("深度学习工作台…", "dl-open"), None,
    ("从样本库裁剪样本集…", "dl-clip"), ("样本集", "dl-data"),
    ("训练…", "dl-train"), ("整景预测…", "dl-predict"), ("特征分析与相似检索…", "dl-features"), None,
    ("任务", "dl-jobs"), ("模型库", "dl-models"), ("运行环境…", "dl-env"),
])


def build_menus(dispatch):
    """把 MENUS 变成 pywebview 的菜单对象。"""
    from webview.menu import Menu, MenuAction, MenuSeparator

    def items(spec):
        out = []
        for entry in spec:
            if entry is None:
                out.append(MenuSeparator())
            elif isinstance(entry[1], list):
                out.append(Menu(entry[0], items(entry[1])))
            else:
                out.append(MenuAction(entry[0], lambda action=entry[1]: dispatch(action)))
        return out

    menus = list(MENUS)
    try:
        from webapi.misc_routes import enabled_modules
        enabled = enabled_modules()
        if "fieldwork" in enabled:
            menus.insert(3, FIELDWORK_MENU)
        if "deeplearn" in enabled:
            menus.insert(menus.index(next(m for m in menus if m[0] == "帮助")), DEEPLEARN_MENU)
    except Exception:
        pass
    return [Menu(title, items(spec)) for title, spec in menus]


def enable_drop_paths() -> None:
    """让 pywebview 记下拖进窗口的文件的真实路径。

    WebView 里拿不到被拖文件的路径（安全限制），pywebview 在原生层把路径记在
    ``_dnd_state`` 里，但只在「有 drop 监听」时才记。**不用它的 DOM 事件监听**：那条路
    会把整个事件对象连同所在文档的整棵 DOM 树序列化后传给 Python，拖动时每次
    dragover 都在界面线程上做一遍，界面越复杂卡得越久。这里只把计数置上，drop 事件由
    页面自己处理，再按文件名向 :meth:`DesktopApi.dropped_paths` 换取路径。
    """
    try:
        from webview.dom import _dnd_state
        _dnd_state["num_listeners"] = max(1, int(_dnd_state.get("num_listeners") or 0))
    except Exception:
        pass


def _persistent_mpl_cache() -> None:
    """Matplotlib 的字体缓存放进数据目录。

    PyInstaller 的运行时钩子把 ``MPLCONFIGDIR`` 指到一个用完即删的临时目录，于是每次
    启动 Matplotlib 都要把系统字体重新扫一遍、建缓存（Mac 上约 8 秒，界面迟迟不出来）。
    换到数据目录后只在第一次启动时建一次。必须在任何模块 import matplotlib 之前调用。"""
    if not getattr(sys, "frozen", False):
        return
    from samplelib import config
    folder = os.path.join(config.app_data_dir(), "cache", "matplotlib")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return                       # 数据目录写不了：沿用临时目录，只是慢
    os.environ["MPLCONFIGDIR"] = folder
    if not os.listdir(folder):
        # 第一次启动还没有缓存：界面出来之后在后台建好，第一次打开光谱曲线时就不用等
        def warm():
            try:
                from matplotlib import font_manager  # noqa: F401
            except Exception:                         # noqa: BLE001
                pass
        timer = threading.Timer(5.0, warm)
        timer.daemon = True
        timer.start()


def _ensure_stdio() -> None:
    """双击启动的 Windows 程序没有控制台，``sys.stdout`` / ``sys.stderr`` 是 ``None``，
    uvicorn 配日志时一调 ``isatty()`` 就崩。这时把两者接到数据目录下的日志文件，
    出了问题也有地方查。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from samplelib import config
    try:
        folder = os.path.join(config.app_data_dir(), "state")
        os.makedirs(folder, exist_ok=True)
        stream = open(os.path.join(folder, "last_run.log"), "w", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def main() -> None:
    _ensure_stdio()
    _persistent_mpl_cache()
    from samplelib import config, datadir, selftest
    print("数据目录：%s" % config.app_data_dir())
    for line in datadir.migration_log:
        print(line)
    # 打包脚本的冒烟测试：设了这个环境变量就不开窗口，自检完把报告写到该路径
    report = os.environ.get(selftest.ENV_VAR, "").strip()
    if report:
        raise SystemExit(selftest.run(report, shell="web"))

    port = 8765 if UI_SOURCE == "dev" else (PORT or free_port())
    app = start_server(port)
    url = DEV_SERVER_URL if UI_SOURCE == "dev" else "http://127.0.0.1:%d/" % port

    if DESKTOP_WINDOW:
        try:
            import webview
        except ImportError:
            print("没有装 pywebview，改用系统浏览器打开：pip install pywebview")
        else:
            bridge = DesktopApi()

            def dispatch(action: str) -> None:
                bridge._window.evaluate_js("window.lithoAction && window.lithoAction(%s)"
                                          % json.dumps(action))

            bridge._window = webview.create_window(
                config.APP_TITLE, url, js_api=bridge,
                width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=(1024, 640),
                background_color="#F0EEE6", text_select=True, menu=build_menus(dispatch))
            bridge._window.events.closing += lambda: confirm_close(bridge._window, app)
            # 不用隐私模式：外观（浅色 / 深色）这类本机偏好记在页面存储里，隐私模式
            # 每次启动都清空。存储放在数据目录下，与样本库、瓦片缓存在一处。
            storage = os.path.join(config.app_data_dir(), "state", "webview")
            enable_drop_paths()
            try:
                webview.start(debug=DEBUG, private_mode=False, storage_path=storage)
            except Exception:                     # noqa: BLE001
                # 窗口开不起来（比如 Windows 上没有 WebView2 运行时）：别留一个看不见
                # 窗口的进程挂着，退回用系统浏览器打开界面
                traceback.print_exc()
                print("桌面窗口打不开，改用系统浏览器。", flush=True)
            else:
                shutdown(app)

    import webbrowser
    webbrowser.open(url)
    print("界面已在浏览器中打开：%s  （Ctrl+C 退出）" % url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    shutdown(app)


def shutdown(app) -> None:
    """窗口关了：收尾后直接结束进程。

    后台线程（建显示金字塔、没画完的出图）可能正停在 GDAL 里。让 Python 照常退出
    的话，解释器清理全局对象时 GDAL 的内部锁先被销毁，那些线程一碰就整个进程中止
    （macOS 上弹「意外退出」）。所以先停掉建金字塔、关好样本库，再用 ``os._exit``
    跳过清理。
    """
    st = getattr(app.state, "lithospectra", None)
    try:
        if st is not None:
            stand_ins = getattr(st, "stand_ins", None)
            if stand_ins is not None:
                stand_ins.stop()
            # 导入这类长任务正占着样本库时别干等：最多 3 秒，拿不到就不关了
            if st.lock.acquire(timeout=3):
                try:
                    st.close()
                finally:
                    st.lock.release()
    except Exception:
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
