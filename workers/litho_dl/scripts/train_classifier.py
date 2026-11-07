# -*- coding: utf-8 -*-
"""训练分类模型，无需打开岩谱。

修改下方配置区后在 VS Code 中运行。脚本先创建任务目录再执行任务，执行过程与岩谱界面相同；
任务根目录与岩谱一致时，可在岩谱的任务列表中查看本次训练的曲线、混淆矩阵与模型包。

全部参数及说明见 ``python -m litho_dl capabilities`` 的输出，或 ``litho_dl/train.py`` 中的
``params()``。
"""

import sys
from pathlib import Path

# =============================================================================
# 配置区
# =============================================================================

# 样本裁剪（h5 格式）的输出目录，或其中的 train_dataset.h5
DATASET = r"/path/to/clip_output"

# 任务根目录：每次运行在其下新建一个任务目录。岩谱的默认任务根目录为 文档/LithoSpectra/dl/jobs
JOB_ROOT = str(Path.home() / "Documents" / "LithoSpectra" / "dl" / "jobs")

PARAMS = {
    "backbone": "2D-CNN",           # 1D-CNN / 2D-CNN / 3D-CNN / SSRN / HybridSN / SRACN
    "epochs": 50,
    "batch_size": 256,
    "lr": 1e-3,
    "val_ratio": 0.2,               # 验证集比例，从训练集中按样本整体划出
    "normalization": "scale",       # scale / band_standard / pixel_l2
    "augment_flip": True,
    "augment_rotate": True,
    "class_weighting": "none",      # none / balanced
    "device": "auto",               # auto / cuda / mps / cpu
    "model_name": "",               # 留空则自动命名
}

TITLE = ""                          # 任务名称，留空则使用默认名称

# =============================================================================

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from litho_dl.tasks import create_job, run_job  # noqa: E402

if __name__ == "__main__":
    job_dir = create_job(JOB_ROOT, "train_classifier", {"dataset": DATASET, **PARAMS}, TITLE)
    print("任务目录：%s" % job_dir)
    sys.exit(run_job(job_dir))
