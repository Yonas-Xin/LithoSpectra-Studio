# -*- coding: utf-8 -*-
"""深度学习模块：界面进程这一侧。

PyTorch 不进本进程。训练与预测由仓库根 ``workers/litho_dl`` 在使用者选的 Python
环境里执行，两边只通过任务目录（``job.json`` / ``events.ndjson`` / ``status.json``）
打交道，见 ``workers/litho_dl/jobs.py``。

* :mod:`.runtime`  找 Worker、选环境、体检、取任务与参数声明
* :mod:`.jobs`     任务目录的创建、启动、排队、取消、续跑、事件读取
* :mod:`.catalog`  样本集与模型的登记、过期判断
* :mod:`.live`     预测进行中的实时图层
"""
