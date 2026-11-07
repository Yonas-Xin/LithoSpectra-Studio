# HyDe Worker 上游与复现说明

## 固定上游

- 项目：HyDe（Python 包 `hyde-images`）
- URL：<https://github.com/Helmholtz-AI-Energy/HyDe>
- 版本：`0.4.3`
- 固定提交：`251a4f461cd8678462cfda09009c183da1b5a092`
- 仓库总许可：BSD-3-Clause

`FastHyDe` / `FastHyIe` 源文件另带原作者 GPL-2.0 与许可声明。当前仓库不复制
HyDe 源码或预训练权重，只在独立环境中固定调用上游。产品分发前仍需完成正式
许可复核。

**注意包名**：PyPI 上叫 `hyde` 的是一个静态网站生成器，与本项目无关，装了它
`import hyde` 一样成功但没有任何算法。装错时环境探测会明确报出来。

## 环境要求

去噪跑在工具箱左下角选中的那个环境里，没有单独的 Worker 解释器参数。该环境必须
同时具备：

- GDAL Python 绑定（读写产品栅格）
- `torch`
- `hyde-images`（固定提交）及其运行依赖：`bm3d`、`PyWavelets`、`kornia`、
  `scikit-image`、`pytorch-wavelets`、`h5py`、`beautifulsoup4`、`tqdm`、`requests`

装不齐时 `WorkerRunner.preflight` 会在克隆产品之前拦下，并说明缺什么。

### 参考安装（本机 samplelib 环境的实际做法）

```bash
P=<环境>/bin/python
$P -m pip install --no-deps \
  "git+https://github.com/Helmholtz-AI-Energy/HyDe.git@251a4f461cd8678462cfda09009c183da1b5a092"
$P -m pip install bm3d PyWavelets kornia scikit-image pytorch-wavelets \
  h5py beautifulsoup4 tqdm requests "setuptools<81"
```

`--no-deps` 是有意的：上游 `install_requires` 只写了 `torch>=1.8.0`、
`numpy>=1.13.0` 这类下限，直接装不会动现有 torch，但显式分开装才能保证这一点。

`setuptools<81` 是因为 `pytorch-wavelets` 1.3.0 仍在 import `pkg_resources`，
setuptools 81 起已将其移除。torch 要求 `setuptools>=77.0.3`，80.x 两边都满足。

`environment.yml` 保留的是与上游论文一致的那套锁定版本（torch 2.5.1 /
numpy 1.26.4 / gdal 3.9.3）。做需要与上游数值逐位对照的实验时按它单建一个环境，
然后在工具箱左下角选中它即可——不需要再改任何参数。

## OpenMP：GDAL 必须先于 torch 导入

conda 环境里 GDAL 与 PyTorch 各自链了一份 `libomp`，谁先初始化谁赢，后一份会撞上
`OMP: Error #15` 并当场把进程打掉（退出码 134）。本机实测：

| 导入顺序 | 结果 |
| --- | --- |
| `import torch` | 崩 |
| `import torch` → `from osgeo import gdal` | 崩 |
| `from osgeo import gdal` → `import torch` | 正常 |

`worker.py` 因此在 `main()` 开头显式调用 `_load_gdal_before_torch()`，把顺序钉死。
**不要**改用 `KMP_DUPLICATE_LIB_OK=TRUE`：那个开关是让两份 libomp 带着"可能算错"
继续跑，而这里是让第二份根本不必初始化。

## 已知上游缺陷与本仓库的处置

### 1. 非 i.i.d. 反变换的括号错误（已在包装层修正）

- 位置：`spatial_domain/fast_hyde.py:140`、`spatial_domain/fast_hyin.py:198`
- 触发条件：`noise_type="additive"` 且 `iid=False`（本工具对资源系列 AHSI 的建议值）
- 上游写法：`y_reconst = torch.sqrt(r_w_og @ y_reconst)`——对**乘积**逐元素开方
- MATLAB 原式：`Y_reconst = sqrt(Rw_ori) * Y_reconst`——对**矩阵**开方再相乘

在真实 ZY1E VN 数据 128×128×24 的 ROI 上实测：`r_w_og` 严格对角（非对角能量为 0），
乘积中有 14 个负数，逐元素开方后即 14 个 NaN；剔除 NaN 后再比，上游写法与白化前
输入的 RMSE 为 0.267，原式为 0.004。所以这不是"偶尔冒 NaN"，而是整个反变换都不对。

`algorithms.py` 的处置是不碰上游代码：白化与反白化都在包装层完成（白化沿用上游同一
段逻辑与同一个 `utils.estimate_hyperspectral_noise`），中间以 `iid=True` 调上游——
那条分支跳过白化直接跑主流程，与上游非 i.i.d. 分支想做的事逐行等价。算法本身一行
没动。每一次这样跑出来的产物，清单里都带
`worker.algorithm.output_selection.upstream_workaround` 与完整的 `upstream_bug`
说明。上游修复后应删除 `_noniid_whiten` / `_noniid_unwhiten` 并同步更新固定提交。

### 2. `FORPDN_SURE` 的 `scale` 必须为 `True`

`scale` 不是"再归一化一次"，而是把数据按 `scale_const` 放大到 `[0, scale_const]`
再在末尾原样缩回（可逆）。SURE 搜正则参数用的 `s_int_*` 区间是绝对量纲的，数据落在
`[0, 1]` 时最优解直接落到区间左端 0，等于不做任何收缩。实测（ZY1E VN 128×128×24）：
`scale=False` 时 RMSE 0.0002、原样返回输入；`scale=True` 时 RMSE 1.08、信噪比增益
59 dB。

### 3. README 与 `__all__` 不一致

上游 README 写作 `FORPDN`，实际导出名是 `FORPDN_SURE`。上游 API 只能按源码核，
不能按 README。

## 模型登记

模型只允许从 `models.json` 的固定清单加载。把对应上游权重复制到本目录的
`models/` 子目录，文件名必须与清单一致。Worker 会在加载前校验 SHA-256，并使用：

```python
torch.load(path, map_location="cpu", weights_only=True)
```

未知权重、哈希不匹配权重和需要回退到不安全 pickle 加载的权重都会被拒绝。

登记模型的训练噪声域是合成 Gaussian 或 mixed noise，并非 ZY1E/ZY1F、GF5 等国产
传感器的原始 DN。每个新 Sensor Adapter 都必须在对应真实 ROI 上完成质量门禁后，
模型才能对该传感器提升为生产可用状态。
