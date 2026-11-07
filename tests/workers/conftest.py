# -*- coding: utf-8 -*-
"""Worker 测试的导入路径：``litho_dl`` 以顶层包名导入，与界面启动 Worker 时一致。"""

import sys
from pathlib import Path

WORKERS = Path(__file__).resolve().parents[2] / "workers"
if str(WORKERS) not in sys.path:
    sys.path.insert(0, str(WORKERS))
