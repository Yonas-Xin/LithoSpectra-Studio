# -*- coding: utf-8 -*-
"""岩谱深度学习 Worker：监督分类与对比学习的训练、整景预测。

跑在使用者选的 Python 环境里（需 numpy、torch、h5py、GDAL），界面进程不 import
本包。三种入口共用同一段代码：

* ``python -m litho_dl run <任务目录>``：界面与命令行都走这条；
* ``scripts/`` 下的脚本：顶部配置区填好即可在 VS Code 里直接运行；
* 在 Python 里 ``from litho_dl.tasks import create_job, run_job``。

任务目录的约定见 :mod:`litho_dl.jobs`。

导入顺序
--------
conda 环境里 numpy（经 MKL / OpenBLAS）与 torch 各链了一份 libomp，先初始化 torch
那份的话，后一份撞上 ``OMP: Error #15`` 当场把进程打掉。先 import numpy 就不会：
第二份不必再初始化。这里在包的最顶上钉死这个顺序，任何子模块 import torch 时
numpy 都已经在了。这不是 ``KMP_DUPLICATE_LIB_OK=TRUE`` 那种带着隐患继续跑的绕过。
"""

import numpy  # noqa: F401  必须先于 torch，见上

__version__ = "0.1.0"

#: 任务目录与事件协议的版本。主版本变了，界面拒绝读这个 Worker 写的任务。
PROTOCOL_VERSION = 1
