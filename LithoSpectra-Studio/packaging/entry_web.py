# -*- coding: utf-8 -*-
"""打包用的入口（PyInstaller 的 ENTRY）：只把控制权交给 webapi.desktop_app。"""

from webapi.desktop_app import main

if __name__ == "__main__":
    main()
