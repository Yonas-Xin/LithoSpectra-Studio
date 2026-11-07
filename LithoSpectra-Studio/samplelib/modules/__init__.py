# -*- coding: utf-8 -*-
"""可裁剪的功能模块。

每个子目录一个模块：``module.json`` 是它的身份，``__init__.py`` 里的
``setup(host)`` 是它唯一的入口。契约见 :mod:`samplelib.module`。

模块目录整个删掉，程序照常跑，只是少那一块功能——界面上没有它的入口，包里也
没有它的代码与数据。判断一次拆分做没做成，就看这一条。
"""

__all__ = ["spectra", "clip", "toolbox", "basemap", "fieldwork"]
