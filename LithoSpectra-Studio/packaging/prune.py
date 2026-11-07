# -*- coding: utf-8 -*-
"""打包后裁剪：删掉 PyInstaller 顺手带进来、但本程序用不到的东西。

PyInstaller 的依赖分析偏保守——只要某个 .so 在环境里链到过某个库，它就会
一并收进来。下面每一条都注明了为什么可以删；不确定的一律不删。

用法：
    python packaging/prune.py <产物路径>
产物路径可以是 .app 包，也可以是 onedir 目录。
"""

import os
import shutil
import sys

# =============================================================================
# 裁剪清单
# =============================================================================

# 整个删掉的文件（按名字匹配，会在包内递归查找）
DROP_FILES = [
    # 程序里没有一处 import tkinter。这两个动态库是
    # conda 的 python 自带 _tkinter 时链进来的，模块本身已排除，库成了孤儿。
    ("libtcl8.6.dylib", "Tcl 运行时：程序不使用 tkinter"),
    ("libtk8.6.dylib", "Tk 运行时：程序不使用 tkinter"),
    ("tcl86.dll", "Tcl 运行时：程序不使用 tkinter"),
    ("tcl86t.dll", "Tcl 运行时：程序不使用 tkinter"),
    ("tk86.dll", "Tk 运行时：程序不使用 tkinter"),
    ("tk86t.dll", "Tk 运行时：程序不使用 tkinter"),
]

# 按文件名前缀删（Windows 产物里才有）
DROP_PREFIXES = [
    # OpenCV 的视频读写插件，运行时按需加载，不在 cv2 的链接依赖里。程序只用 OpenCV
    # 处理照片（去水印修补），不读写视频
    ("opencv_videoio_ffmpeg", "OpenCV 视频插件：程序不读写视频"),
    # pywebview 带着 32 位 IE 内核的互操作库；本程序只用 64 位 Edge WebView2
    ("WebBrowserInterop.x86", "32 位 IE 内核互操作库：只用 64 位 Edge WebView2"),
    ("pywebview-android", "安卓后端"),
]

# 整个删掉的目录（相对包内资源根）
DROP_DIRS = [
    # spec 已经用 datas 把 share/gdal 收成 gdal_data，PyInstaller 的 conda
    # 支持又按原路径收了一份，内容完全相同。
    ("share", "与 gdal_data 重复的一份 GDAL 数据"),
    ("tcl", "Tcl 脚本库：程序不使用 tkinter"),
    ("tk", "Tk 脚本库：程序不使用 tkinter"),
    # pywebview 的 lib\runtimes\win-* 三份 WebView2Loader 都要留：它按**系统**架构挑，
    # 不按产物架构。Mac 上 Parallels 里的 Windows 是 ARM64，x64 程序在里面跑照样找 win-arm64
]

def _human(n: int) -> str:
    return "%.1f MB" % (n / 1048576.0) if n >= 1048576 else "%.0f KB" % (n / 1024.0)


def _size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p):
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
    return total


def _remove(path: str, reason: str, report: list) -> None:
    if not os.path.exists(path) and not os.path.islink(path):
        return
    n = 0 if os.path.islink(path) else _size(path)
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
        else:
            shutil.rmtree(path)
    except OSError as exc:
        report.append(("跳过", os.path.basename(path), 0, "删不掉：%s" % exc))
        return
    report.append(("删除", os.path.basename(path), n, reason))


def prune(bundle: str) -> int:
    if not os.path.exists(bundle):
        print("产物不存在：%s" % bundle)
        return 2

    before = _size(bundle)
    report = []

    # ---- 按文件名删
    wanted = {name: reason for name, reason in DROP_FILES}
    for root, _dirs, files in os.walk(bundle):
        for f in list(files):
            if f in wanted:
                _remove(os.path.join(root, f), wanted[f], report)

    # ---- 按文件名前缀删
    for root, _dirs, files in os.walk(bundle):
        for f in list(files):
            for prefix, reason in DROP_PREFIXES:
                if f.startswith(prefix):
                    _remove(os.path.join(root, f), reason, report)
                    break

    # ---- 按目录名删
    wanted_dirs = {name: reason for name, reason in DROP_DIRS}
    for root, dirs, _files in os.walk(bundle):
        for d in list(dirs):
            if d in wanted_dirs:
                _remove(os.path.join(root, d), wanted_dirs[d], report)

    after = _size(bundle)
    print("=" * 74)
    print("裁剪明细")
    print("=" * 74)
    for action, name, n, reason in sorted(report, key=lambda r: -r[2]):
        print("  %s %-26s %10s  %s" % (action, name[:26], _human(n) if n else "-", reason))
    print("=" * 74)
    print("裁剪前 %s → 裁剪后 %s（省 %s）"
          % (_human(before), _human(after), _human(before - after)))
    print("=" * 74)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    raise SystemExit(prune(sys.argv[1]))
