# 岩谱深度学习 Worker（litho_dl）

负责监督分类、对比学习的训练与整景预测，运行在使用者所选的 Python 环境中（需安装 numpy、
PyTorch、h5py 与 GDAL）。岩谱主程序不导入本包。支持 macOS（MPS / CPU）与 Windows（CUDA /
CPU）。设计说明与性能测试见 `docs/2026-10-13_deep-learning-module-plan.md`。

## 使用方式

命令行（需将 `workers` 目录加入 `PYTHONPATH`）：

```bash
python -m litho_dl doctor
python -m litho_dl capabilities
python -m litho_dl new <任务根目录> train_classifier params.json
python -m litho_dl run <任务目录>
```

`doctor` 检查运行环境，`capabilities` 以 JSON 输出任务、参数与骨干网络的声明。

VS Code：打开 `scripts/train_classifier.py` 或 `scripts/predict.py`，修改文件开头的配置区后直接运行。

岩谱界面：在深度学习工作台中选择运行环境，界面以子进程执行 `python -m litho_dl run <任务目录>`。

三种方式生成的任务目录格式相同，在命令行中运行的任务也可以在界面中查看。

## 任务目录

```
<任务根目录>/<job_id>/
├── job.json          任务类型与参数，创建后不再修改
├── events.ndjson     事件流，每行一个 JSON，只追加
├── status.json       当前状态：queued / running / succeeded / failed / cancelled
├── cancel            存在即表示请求取消
├── checkpoints/      训练：last.pt（用于继续训练）与 best.pt
├── metrics.json      训练：每轮指标
├── eval/             训练：验证集与测试集的逐样本结果（.npz）
├── model/            训练：<名称>.lithomodel 模型包
├── outputs/          预测：分类栅格、置信度栅格、类别表 .classes.json
├── preview.u8        预测：实时预览（行×列 uint8，255 表示尚未计算）
└── blocks.done       预测：各分块的完成标记，继续运行时据此跳过已完成的分块
```

对同一任务目录再次执行 `run` 即继续运行：训练从 `last.pt` 继续，预测只计算未完成的分块。

## 骨干网络

| 名称 | 输入 | 速度 | 说明 |
| --- | --- | --- | --- |
| 1D-CNN | 中心像元光谱 | 快 | 不使用邻域，预测时直接读取光谱 |
| 2D-CNN | 以波段为通道的块 | 快 | 在 Mac 上实验时的默认选择 |
| 3D-CNN | 三维块 | 慢 | 在 Apple 芯片上无法加速 |
| SSRN | 块 | 慢 | 参数少，计算量大 |
| HybridSN | 块 | 慢 | 块大小至少为 9 |
| SRACN | 三维块 | 很慢 | 需要 CUDA |

网络结构与源项目逐层一致，`tests/workers/test_litho_dl_backbones.py` 在相同权重下逐元素对照输出。

新的骨干网络放在插件目录中。由岩谱启动时，插件目录为岩谱数据目录下的 `dl/plugins/`，通过
环境变量 `LITHO_DL_PLUGINS` 传给 Worker；单独运行 Worker 且未设置该变量时，默认为
`~/Documents/LithoSpectra/dl/plugins/`：

```python
from litho_dl.backbones import register_backbone

@register_backbone("MyNet", input_kind="image", min_patch=5, speed="fast")
class MyNet(torch.nn.Module):
    def __init__(self, in_shape):      # (波段, P, P)
        ...
    def forward(self, x):              # 返回 [N, 特征维] 或可展平的张量
        ...
```

加载失败或与已有骨干网络重名的插件会被跳过，原因列在 `capabilities` 的 `plugin_errors` 中。

## 实现说明

- 包的 `__init__` 先导入 numpy，子模块再导入 PyTorch。在 conda 环境中先初始化 PyTorch 自带的
  libomp 会触发 `OMP: Error #15` 并导致进程退出。
- 归一化参数在训练集上计算一次并保存到模型包，预测时使用同一组参数。
- 每轮评估前重新统计 BatchNorm 的均值与方差，避免样本较少时滑动统计量滞后。
- 预测分为读取、计算、写入三个阶段，由线程衔接。原始分块只向设备传输一次，在设备上按行
  切片展开为 patch。GPU 上使用半精度推理，第一块先与单精度比对分类结果，一致率低于 99.9%
  时改用单精度。

## 测试

```bash
python -m pytest tests/workers -q -p no:cacheprovider
```
