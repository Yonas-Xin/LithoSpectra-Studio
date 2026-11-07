# -*- coding: utf-8 -*-
"""打包后的运行时钩子：把 GDAL / PROJ 指向随包携带的数据目录。

GDAL 靠 ``GDAL_DATA`` 找坐标系与格式的辅助数据，PROJ 靠 ``PROJ_LIB``（或
``PROJ_DATA``）找 ``proj.db``。开发环境里这两个由 conda 设好，打包之后环境
变量不存在，必须在 import osgeo 之前指到包内，否则所有坐标变换都会失败——
而且失败得很隐蔽：不报错，只是投影结果不对。
"""

import os
import sys

_base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))

for _var, _folder in (("GDAL_DATA", "gdal_data"),
                      ("PROJ_LIB", "proj_data"),
                      ("PROJ_DATA", "proj_data")):
    _path = os.path.join(_base, _folder)
    if os.path.isdir(_path):
        os.environ[_var] = _path

# 驱动插件（JPEG2000 等）只认包内那份。这里是覆盖而不是 setdefault：使用者的
# shell 里若残留着别的 conda 环境设的 GDAL_DRIVER_PATH，顺着它去装另一个版本
# GDAL 的插件只会报"不兼容"。包里没带插件目录时干脆关掉插件搜索，也别去碰
# 系统上另一份 GDAL 的默认目录。
_plugins = os.path.join(_base, "gdal_plugins")
os.environ["GDAL_DRIVER_PATH"] = _plugins if os.path.isdir(_plugins) else "disable"

# Windows：GDAL 的 osgeo/__init__.py 会顺着 PATH 找第一个放着 gdal*.dll 的目录，把它加进
# DLL 搜索路径。装过 QGIS、ArcGIS、OSGeo4W 的电脑，PATH 上就有一份别的版本的 GDAL，
# _gdal.pyd 加载时可能拿到那份 gdal.dll，版本对不上就失败，报出来却是
# "No module named '_gdal'"。把包内目录放到 PATH 最前面，它找到的第一份就是自己的。
if sys.platform == "win32":
    os.environ["PATH"] = _base + os.pathsep + os.environ.get("PATH", "")
