# -*- coding: utf-8 -*-
"""岩谱 Web 前端的后端接口层。

把 :mod:`samplelib` 的会话层（:class:`~samplelib.session.LibrarySession`）与标注
暂存区（:class:`~samplelib.annotate.editing.EditBuffer`）包成 HTTP 接口，供
``web/`` 下的 React 界面调用。本包不依赖 Qt。
"""

from .app import create_app

__all__ = ["create_app"]
