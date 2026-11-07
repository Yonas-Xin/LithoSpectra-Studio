# -*- coding: utf-8 -*-
"""用分类模型进行整景预测，无需打开岩谱。

修改下方配置区后在 VS Code 中运行。中途停止后，将 ``RESUME_JOB`` 设为上次的任务目录
再次运行，只计算未完成的分块。
"""

import sys
from pathlib import Path

# =============================================================================
# 配置区
# =============================================================================

IMAGE = r"/path/to/scene.tif"                         # 待预测的影像
MODEL = r"/path/to/xxx.lithomodel"                    # 训练生成的模型包
JOB_ROOT = str(Path.home() / "Documents" / "LithoSpectra" / "dl" / "jobs")

PARAMS = {
    "output": "",              # 输出分类栅格路径，留空则保存到任务目录的 outputs 文件夹
    "mask": "",                # 预测范围矢量（shp / gpkg / geojson），留空则预测全图
    "confidence": "max_prob",  # max_prob / entropy / none
    "probabilities": False,    # 是否输出各类概率（多波段，文件较大）
    "block_size": 512,
    "device": "auto",          # auto / cuda / mps / cpu
    "precision": "auto",       # auto（GPU 使用半精度）/ fp32 / fp16
}

RESUME_JOB = ""                # 继续运行：填写上次的任务目录；留空则新建任务

# =============================================================================

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from litho_dl.tasks import create_job, run_job  # noqa: E402

if __name__ == "__main__":
    if RESUME_JOB:
        job_dir = Path(RESUME_JOB)
    else:
        job_dir = create_job(JOB_ROOT, "predict", {"image": IMAGE, "model": MODEL, **PARAMS})
    print("任务目录：%s" % job_dir)
    sys.exit(run_job(job_dir))
