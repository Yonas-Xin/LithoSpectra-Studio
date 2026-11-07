# 岩谱工具箱

遥感数据预处理工具的统一外壳。**一件工具只描述怎么处理一个对象，批量永远走
`run_batch`。**

工具箱没有自己的启动脚本。界面从桌面端进（`LithoSpectra-Studio/main.py` 的
「文件 → 工具箱…」），批量从代码里调 `run_batch`。

## 1. 用法

当库用：

```python
from toolbox import run_batch, describe_tools

describe_tools()
report = run_batch(
    "remove_water_vapor",
    inputs="/data/ZY1F",              # 一个文件、一个目录，或一组路径
    output_dir="/data/cleaned",
    params={"sensor": "ZY1F"},
    overwrite=False,                  # 产物已存在时跳过
    stop_on_error=False,              # 单件失败记下继续
    dry_run=False,                    # 只列计划不落盘
)
print(report.summary(), report.ok)
```

## 2. 界面

窗口分左右两栏：左栏选工具、填参数，右栏定输入输出、看运行日志，底栏是进度与
汇总。主题、控件样式、图标全部取自桌面端的设计系统，不另立一套令牌，跟着一起
明暗切换。

两个入口，都是同一个窗口类：

| 入口 | 说明 |
| --- | --- |
| 桌面端「文件 → 工具箱…」（Ctrl+Shift+T） | 按需打开，不随主窗口启动 |
| 图层面板右键影像 →「用工具箱处理…」 | 顺带把那个图层填成输入 |

从桌面端进的是一个**独立顶级窗口**，不是停靠面板：批处理动辄跑几十分钟，压在主
窗口里会占掉画布，而它跟画布本来就没有交互。窗口只开一个，再点是把它抬到前面；
主窗口关闭时它跟着关。输入行的「图层…」列出画布里所有带本地文件的栅格图层，
同名的加序号区分。

界面代码住在 `LithoSpectra-Studio/samplelib/ui/toolbox_window.py`——它要用桌面端
的设计系统，放在这边依赖就只有一个方向：samplelib → toolbox。

**参数面板是从 `tool.params` 生成的**，界面里没有一行"这件工具有哪些参数"的硬
编码。加一件新工具，界面自动就有了它的参数——这正是当初把参数写成声明式的目的。
`kind` 决定控件：`bool` 给复选框、`choice` 给下拉、`ranges` 给区间输入框，
`suggestions()` 有候选的 `str` 给可编辑下拉框（传感器就是这么列出 `assets/` 里
那几颗卫星的）。要支持一种新的 `kind`，只需在
`samplelib/ui/toolbox_params.py` 的 `build_field` 里添一支。

批处理跑在 `QThread` 里，界面不僵。这跟桌面端 `samplelib.ui.tasks` 明确不开线程
的做法相反，理由是那一层动的是 `SampleStore` 持有的同一个 `gdal.Dataset`，主线程
还在读它画画布；工具箱每件活自己开自己关数据集，跟样本库没有共享对象。

取消只在两件之间生效——GDAL 写盘中途没法安全打断，与其假装能停，不如说清楚。

工具箱本体（core / batch / raster / tools）不知道界面的存在，也不需要 Qt，
命令行照跑。

## 3. 现有工具

| id | 输入粒度 | 做什么 |
| --- | --- | --- |
| `remove_water_vapor` | 单个栅格文件 | 按波长范围剔除水汽吸收波段，可一并去掉 VNIR-SWIR 重合波段 |
| `sentinel2_l2a_stack` | 一整个产品目录 | L2A 的 10 m 与 20 m 波段统一到 10 m 网格，写成一个 ENVI 栈 |
| `hyde_denoise_product` | 一整个产品目录 | 事务式克隆 ZY1E/ZY1F AHSI L1A 产品，并在独立 Worker 进程中去噪有效 VN/SW 波段 |

参数不在这里重复，`describe_tools()` 就是唯一的那份清单。

`remove_water_vapor` 的波长按三处依次找：GDAL 默认元数据域、ENVI 元数据域、数据
文件旁边的 `.hdr`。都没有时才用 `sensor` 参数从仓库根 `assets/` 下读同名传感器
参数文件（现有 `GF5`、`ZY1E`、`ZY1F`）。放一个新传感器进去不用改代码，丢一个同
格式的 `.txt` 即可。

## 4. 加一件新工具

写一个模块放进 `tools/`，实现 `core.Tool` 的三个方法，末尾留 `TOOL = YourTool()`，
再在 `tools/__init__.py` 的 `_MODULES` 里添一行。

```python
class MyTool(Tool):
    id = "my_tool"
    name = "我的工具"
    summary = "一句话"
    input_kind = "raster"            # 或 "product_dir"
    params = (Param("threshold", "阈值", 0.5, kind="float"),)

    def discover(self, root, params, log):   # 有哪些活
        return [WorkItem(key=p.stem, source=p)
                for p in iter_files(root, raster.RASTER_SUFFIXES)]

    def output_path(self, item, out_dir, params):   # 产物叫什么
        return out_dir / f"{item.source.stem}_out.tif"

    def process(self, item, out_path, params, log): # 这一件怎么干
        ...

TOOL = MyTool()
```

**工具里不写循环、不写"已存在就跳过"、不写异常兜底。** 这三件事归 `batch.py`，
只此一处。工具里出现遍历输入目录的 for 循环，就是写错了地方。

发现阶段（`discover`）抛异常会中止整批——路径不存在、参数不合法、缺驱动都属于
开跑前该拦住的问题。处理阶段（`process`）抛异常只废掉这一件。

`companions()` 用来声明主产物之外的伴生文件（ENVI 的 `.hdr`、GDAL 的
`.aux.xml`）。批处理判断"产物已存在"和失败后清理半成品时都要看它，漏声明会导致
半成品被下一次运行当成已完成。

## 5. 为什么这样拆

两件工具的"一件活"根本不是一回事：去水汽的输入是单个栅格文件，Sentinel-2 的输入
是一整个 granule 目录。批处理入口如果自己去 glob 文件，第二种就没法支持。把发现
工作项的职责留在工具里，入口就不必知道任何一件工具的输入长什么样——这是加新工具
不改框架的前提。

`Param` 写成声明式也是为此：批处理靠它填默认值与校验类型，界面靠它生成参数面板。
两边都不必为每件工具写一遍代码。

## 6. 依赖与环境

工具箱本体只要 GDAL 与 NumPy，界面另需 PySide6。

本仓库真正需要的 conda 环境只有一个：

| 环境 | 定义 | 装了什么 | 谁用 |
|---|---|---|---|
| `samplelib` | `LithoSpectra-Studio/environment.yml` | Python 3.11、GDAL（含 JPEG2000 驱动）、NumPy、Pillow、h5py、PySide6 | 桌面端、工具箱界面、批处理 |

去噪 Worker 另外要 torch 与 `hyde-images`。它没有单独的解释器参数——跑在左下角
选中的那个环境里，所以那个环境得同时有 GDAL 和 torch。`samplelib` 现在两样都有，
一个环境就够。要单独建一个可复现的 Worker 环境，配方在
`workers/hyde_denoise/environment.yml`（版本全部钉死）。

`geo` 之类的研究环境同样可选：只要 `probe` 报出 GDAL 与 JPEG2000 就能跑，工具箱
不假定环境叫什么名字。

### 运行环境选择器

界面左下角那一栏决定这批活跑在哪个 Python 环境里。这不是装饰：不同工具对环境的
要求不一样（Sentinel-2 要 JPEG2000 驱动、HyDe 去噪要 torch + hyde-images），以前
换环境只能关掉程序重开。

环境从 `~/.conda/environments.txt` 读，而不是调 `conda env list`——后者要启一次
conda，慢且不一定在 PATH 上。选中后会真的把那个解释器启动起来探一遍，报出
Python / GDAL 版本、有没有 JPEG2000 驱动、能不能跑去噪。

批处理因此永远跑在子进程里：换环境没有第二种办法。顺带两个好处——GDAL 段错误
只崩子进程，界面还在；界面也不会被 GDAL 卡住。代价是取消要靠标志文件传话，且只
在两件之间生效。

JPEG2000 驱动在 conda-forge 上是单独的插件包，不随 GDAL 一起装。Sentinel-2 的
原始 `.jp2` 波段读不了时：

```bash
conda install -c conda-forge libgdal-jp2openjpeg
```

工具箱在发现阶段就检出这个情况并给出上面这条命令，不会跑到一半才失败。探测时会
先摘掉 `GDAL_DRIVER_PATH`、`PROJ_DATA`、`CONDA_PREFIX` 这些指向调用方环境的变量，
否则界面跑在哪个环境里，所有环境就都被报成哪个环境的成色。

## 7. HyDe 产品去噪

`hyde_denoise_product` 接受完整 ZY1E/ZY1F AHSI `LEVEL1A` 产品。输入目录必须有
唯一主 XML、76 波段 `*_VN.tif` 或 `*_VN.tiff`、90 波段 `*_SW.tif` 或 `*_SW.tiff`、两份 `.rpb`，以及行数与
波段数严格一致的 `RadCal.raw` / `Spectralresponse.raw`。

ZY1E 与 ZY1F 共用同一个产品合同：Adapter 同时识别 `.tif` / `.tiff`，以及主 XML
中的 `Level` / `ProductLevel` 差异。ZY1F 的 `_describe.xml`（例如 `H1B`）作为产品
变体写入 provenance，但主处理级别仍以原厂主 XML 的 `LEVEL1A` 为准。

每次运行都会对 VN/SW 的每个波段做精确分块扫描。无有限值或 `max == min` 的波段
自动冻结，不进入算法；剩余波段按连续索引动态分组，避免算法跨越水汽吸收空洞建立
虚假光谱关系。冻结波段和 NoData 像元从源产品逐值保持。

输出先写到 `<product>__HYDE_<METHOD>.partial`。Worker 只以 GDAL `GA_Update` 更新暂存
副本的像元，不重建 TIFF、不改 XML 或元数据。文件集合、非核心文件 SHA-256、RPC、
`IMAGE_STRUCTURE`、默认元数据域和逐波段属性通过回读验证后，暂存目录才原子改名为
最终产品。处理事实保存在产品目录外的同名 `.processing.json` 中。

工具箱主进程不导入 Torch/HyDe——算法跑在一个独立的 Worker 子进程里，那一层隔离防的
是 Torch/OpenMP 把批处理进程带崩。Worker 用的解释器就是左下角选中的那个，没有单独
的参数：以前两处各问一遍，填不一致时（批处理在 A 环境跑、算法在 B 环境跑）没有任何
提示。因此这个环境要同时装齐 GDAL 与 torch + hyde-images，装不齐会在克隆产品之前被
`WorkerRunner.preflight` 拦下——克隆一景是几个 GB 的复制，跑到启动 Worker 才发现环境
不对，那份复制就白做了。依赖清单、已知上游缺陷与模型准备见
`workers/hyde_denoise/UPSTREAM.md`；依赖尚未准备时，dry-run 和产品合同测试仍可运行。

算法参数面板提供以下互斥选项；一次任务只运行一个算法，并生成带算法后缀的独立
克隆产品，避免多种去噪连续叠加：

| 算法 | 状态 | 说明 |
| --- | --- | --- |
| FastHyDe | 可用基线 | 默认传统算法；仍需真实 ROI 质量评价后决定生产默认值 |
| FastHyIe | 可用 | 从原始 VN/SW 按可配置策略构造同形 0/1 有效性掩膜，用于去噪与缺失值修复 |
| L1HyMixDe | 可用 | 面向脉冲、条带和混合噪声，计算量大 |
| HyMiNoR | 可用 | 混合高斯与稀疏噪声，内部依赖 HyRes |
| HyRes | 可用 | 全局 PCA/DWT 方法 |
| WSRRR | 可用 | 返回去噪影像和 PC；只选择去噪影像写回，PC 类型与形状记入 provenance |
| OTVCA | 可用 | 返回去噪影像和特征；只选择去噪影像写回，特征类型与形状记入 provenance |
| FORPDN_SURE | 可用 | SURE/DWT；scale 必须为 True，否则退化成恒等 |
| QRNN3D | 可用 | 需要登记权重并通过哈希校验；权重训练于合成噪声 |

九个算法都已在真实 ZY1E 数据上跑通，不再标注实验状态，也不设「允许实验算法」开关
——在下拉框里选中它这个动作本身就是选择。八个传统算法经过完整管线端到端验证；
QRNN3D 需要登记权重并通过 SHA-256 校验。注意 QRNN3D 的登记权重训练于合成 Gaussian /
mixed 噪声，并非国产传感器原始 DN，用于生产前应在真实 ROI 上自行评估——`status`
为 `available` 只表示它能跑通并受哈希保护，不代表它在本传感器上已通过质量门禁。

`FastHyIe` 的 mask 与当前连续波段组严格同形，遵循上游语义 `1=可用、0=缺失`。`mask_policy=nodata` 是保守默认值，只把
非有限值和各波段显式 NoData 标为缺失；`nodata_zero` 额外把原始 DN 0 标为缺失；
`nodata_zero_saturation` 再加入整数数据类型上限。后两种策略可能把真实暗像元或真实
饱和值误判为缺失，因此必须由用户显式选择。若某像元剩余可用波段少于
`k_subspace`，Worker 会在运行算法前拒绝，避免矩阵求逆奇异。
默认 `mask_writeback=preserve_source_nodata`，因此源产品显式 NoData 仍保持原值；只有
显式选择 `inpaint_masked`，FastHyIe 生成的缺失位置修复值才会写回产品。掩膜构造
策略、缺失元素数量、最少可用波段数和写回策略均进入 provenance。

```python
report = run_batch(
    "hyde_denoise_product",
    inputs="/data/ZY1E_AHSI_L1A_PRODUCT",
    output_dir="/data/denoised",
    params={
        "algorithm": "FastHyDe",
        "device": "cpu",
        "iid": False,
        "k_subspace": 10,
    },
)
```

### 为什么不分块

传统算法整景一次算，不做空间分块。子空间估计、噪声协方差和逐波段归一化都是全局统计
量，分块等于每块投影到不同的低秩基上——相邻块的同一地物会出现系统性光谱偏移。缝可以
靠重叠羽化掩盖，基不一致掩盖不了，而岩性识别比的正是光谱形状。`tile_size` / `halo`
只对 QRNN3D 生效，那是 CNN 固定感受野决定的，用 halo 加线性斜坡加权融合缓解。

光谱方向则一直是分组的：VN/SW 各自成栅格，栅格内再按冻结波段切成连续组，**逐组顺序
执行**，所以峰值内存由最大的那一组决定，不是整个产品。分更细的组能进一步压低峰值，但
要付代价：这类方法的效力来自「B 维光谱只占 k 维子空间」的冗余度，默认 `k_subspace=10`
时 76 波段的冗余度是 7.6 倍，切成 4 组 19 波段就只剩 1.9 倍，去噪能力随之下降；且每组
独立估噪声、独立归一化，组间会出现光谱台阶。

本机（24 GiB）整景 VN 2051×1999×76 实测峰值：OTVCA 5.8 GiB / 12.0 s，FastHyDe
6.6 GiB / 469.6 s，FastHyIe 7.2 GiB / 521.8 s，HyRes 7.3 GiB / 35.6 s。余量充足，因此
不再设置任何像元数闸门。QRNN3D 仍需使用已登记、哈希匹配的模型。

## 8. 去噪的分层与扩展点

去噪代码按四层拆分，层与层之间只靠一个规范化的产品视图相连：

- `toolbox/denoise/adapters.py`：Adapter 注册表与最小 `DenoiseProductView`；只要求产品提供规范化 `VN`/`SW` 资产；
- `toolbox/denoise/product.py`：当前 ZY1E/ZY1F Adapter 的文件映射、波段质量预检和事务克隆；
- `workers/hyde_denoise/inputs.py`：从原始立方体和 NoData 构造算法所需 mask 等附加输入；
- `workers/hyde_denoise/algorithms.py`：传感器无关的 `[H, W, B] float32` 算法合同与多输出选择；
- `toolbox/tools/hyde_denoise_product.py`：批处理编排、provenance 和产品级验证。

当前注册 `zy_ahsi_l1a` Adapter。未来接入 GF5 时，只需新增并注册 GF5 Adapter，由它
把实际文件组织映射为规范名称 `VN`/`SW`、栅格相对路径、波段数、尺寸、NoData 和产品
验证逻辑；去噪编排、Worker、mask 构造和算法均无需修改。测试中已有一个最小 GF5
Fixture Adapter，验证了这一插件路径。

算法层不需要理解 ZY1E/ZY1F/GF-5 的文件名、XML XPath、定标参数或 RPC——它只收
`[H, W, B] float32` 与一个 mask。去噪在**原始 DN 域**执行，不做 DN→Radiance、大气
校正或正射校正；那些属于另一条流水线，接进来时从 Adapter 这一层接，算法层不动。
