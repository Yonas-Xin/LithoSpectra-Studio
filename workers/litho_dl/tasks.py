# -*- coding: utf-8 -*-
"""任务注册表与执行入口。

新任务类型（对比学习、领域自适应……）在这里登记一行：参数声明 + 执行函数。
界面通过 ``capabilities`` 拿到全部任务的参数声明并据此生成表单。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

from . import PROTOCOL_VERSION, __version__, contrastive, features, predict, train
from .backbones import BACKBONES, load_plugins
from .jobs import CANCELLED, FAILED, SUCCEEDED, Cancelled, Job, JobError, create_job_dir
from .params import ParamError, TaskSpec, coerce_all

#: 插件目录：环境变量优先，其次是岩谱的数据目录
PLUGIN_DIR = os.environ.get("LITHO_DL_PLUGINS") or str(
    Path.home() / "Documents" / "LithoSpectra" / "dl" / "plugins")

_plugin_errors: list[str] | None = None


def plugin_errors() -> list[str]:
    global _plugin_errors
    if _plugin_errors is None:
        _plugin_errors = load_plugins(PLUGIN_DIR)
    return _plugin_errors


def tasks() -> dict[str, TaskSpec]:
    plugin_errors()            # 先装插件，骨干下拉框里才有它们
    return {
        "train_classifier": TaskSpec("train_classifier", "训练分类模型", train.params(),
                                     train.run, "用样本库裁剪出的样本集训练监督分类模型"),
        "predict": TaskSpec("predict", "整景预测", predict.params(), predict.run,
                            "用分类模型对整幅影像逐像元分类"),
        "train_contrastive": TaskSpec("train_contrastive", "对比学习预训练", contrastive.params(),
                                      contrastive.run, "不用标签，从影像里学一个骨干，供分类训练初始化"),
        "embed": TaskSpec("embed", "样本特征分析", features.params(), features.run,
                          "样本在特征空间的分布与疑似错标"),
        "similarity": TaskSpec("similarity", "相似区域检索", predict.similarity_params(),
                               predict.run_similarity, "整景计算与参考点的特征相似度"),
    }


def capabilities() -> dict:
    return {
        "protocol": PROTOCOL_VERSION,
        "worker_version": __version__,
        "tasks": [spec.to_json() for spec in tasks().values()],
        "backbones": [spec.to_json() for spec in BACKBONES.values()],
        "plugin_dir": PLUGIN_DIR,
        "plugin_errors": plugin_errors(),
    }


def validate(task: str, params: dict) -> dict:
    registry = tasks()
    if task not in registry:
        raise ParamError("未知任务：%s。可选：%s" % (task, "、".join(registry)))
    return coerce_all(registry[task].params, params)


def create_job(root: str | Path, task: str, params: dict, title: str = "") -> Path:
    """校验参数后建任务目录。参数不对在这里就报，不会留下一个跑不起来的目录。"""
    clean = validate(task, params)
    return create_job_dir(root, task, clean, title or tasks()[task].title)


def run_job(job_dir: str | Path, echo: bool = True, listeners=()) -> int:
    """执行一个任务目录。返回进程退出码：0 成功，1 失败，2 取消。"""
    job = Job(job_dir, echo=echo)
    job.listeners.extend(listeners)
    job.clear_cancel()
    job.begin()
    try:
        spec = tasks().get(job.task)
        if spec is None:
            raise JobError("未知任务类型：%s" % job.task)
        params = coerce_all(spec.params, job.params)
        result = spec.run(job, params)
        job.finish(SUCCEEDED, "完成", result=result)
        return 0
    except Cancelled as exc:
        result = exc.args[0] if exc.args else None
        job.finish(CANCELLED, "已取消", result=result)
        return 2
    except (JobError, ParamError) as exc:
        job.finish(FAILED, str(exc))
        return 1
    except Exception as exc:
        trace = traceback.format_exc()
        sys.stderr.write(trace)
        job.finish(FAILED, "%s: %s" % (type(exc).__name__, exc), traceback=trace)
        return 1
    finally:
        job.close()
