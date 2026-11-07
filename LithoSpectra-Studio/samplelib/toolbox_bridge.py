# -*- coding: utf-8 -*-
"""定位仓库根的 ``toolbox`` 包。

工具箱是平台级的库（只依赖 GDAL 与 NumPy），桌面端是它的一个使用者，界面这一层
则住在桌面端里。依赖因此只有一个方向：samplelib → toolbox。

桌面端与工具箱是仓库里的同级目录，运行时不一定谁在 ``sys.path`` 上，这里补一次。
"""

from __future__ import annotations

import os
import sys


def _repo_root() -> str:
    """``toolbox`` 包所在的那一层。

    源码运行时是仓库根（LithoSpectra-Studio 的上一级）。打包之后 ``toolbox`` 与
    ``workers`` 在随包资源目录下。
    """
    if getattr(sys, "frozen", False):
        from samplelib.datadir import resource_dir
        return resource_dir()
    here = os.path.dirname(os.path.abspath(__file__))          # .../samplelib
    return os.path.dirname(os.path.dirname(here))


#: ``toolbox`` 包所在的那一层
REPO_ROOT = _repo_root()


def available() -> bool:
    """工具箱在不在。菜单据此启停，不在就别给一个点了报错的入口。"""
    return os.path.isdir(os.path.join(REPO_ROOT, "toolbox"))


def load():
    """返回 ``toolbox`` 模块。"""
    if not available():
        raise ImportError("找不到工具箱包：%s" % os.path.join(REPO_ROOT, "toolbox"))
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    import toolbox
    return toolbox
