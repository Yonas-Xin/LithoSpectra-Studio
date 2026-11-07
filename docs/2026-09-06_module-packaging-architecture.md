# 岩谱平台模块化封装方案

面向按客户需求裁剪打包的分发形态：一份源码、一套装配机制、若干发行版组合。

## 1. 结论速览

| 层 | 内容 | 可否裁剪 |
| --- | --- | --- |
| 平台底座 | 样本库存储与会话、样本导入、样本导出、画布与图层、窗口外壳（主题 / 任务 / 检查器 / 样本树） | 否 |
| 功能模块 | 标注编辑、光谱曲线、光谱库与匹配、类别平均谱、样本裁剪、预处理工具箱、深度去噪 Worker、在线底图、岩性图例库 | 是 |

判据只有一条：**去掉它之后，剩下的程序仍然是一个自洽、能交付的产品**。样本库打不开、图层画不出来，程序就没有存在意义，这两件事进底座；没有光谱曲线的样本库仍然是一个可用的样本库，光谱曲线就是模块。

LithoReason 智能体在独立仓库中开发与交付，不属于本方案的模块。桌面端与它之间只有一条协议边界，不共享 Python 进程。

## 2. 现状盘点

代码规模与依赖方向（截至 develop 分支）：

| 单元 | 位置 | 行数 | 说明 |
| --- | --- | --- | --- |
| 桌面端 | `LithoSpectra-Studio/samplelib/` | 25 119 | 存储层 + `annotate/` 画布 + `ui/` 界面 |
| 工具箱 | `toolbox/` | 3 623 | 只依赖 GDAL 与 NumPy，已是平台级独立包 |
| 去噪 Worker | `workers/hyde_denoise/` | 997 + 20 MB 模型 | 独立 conda 环境，进程外调用 |

已经站住的两处解耦，是本方案的地基：

- `samplelib.toolbox_bridge` 用 `available()` 探测工具箱在不在，菜单据此启停——**模块缺席时不给入口**的范式已经跑通。
- `toolbox.core` 的 `register` / `Param` 声明式注册：工具自带参数声明，界面据此自动生成面板，加一件工具不必改界面代码。

桌面端的分层大体是干净的（`config` 以下不碰界面），但有四处硬伤挡着模块拆分，见第 5 节。

## 3. 平台底座

底座是所有发行版都带、模块可以依赖、模块之间不互相依赖的那部分。

### 3.1 样本库内核

`model.py` `store.py` `session.py` `appstate.py` `crs.py` `config.py`（拆分后的核心段）

GeoPackage 单文件存储、任意层级的类别树、schema 升级、备份与还原、会话状态。整个平台的数据主体在这里，没有第二个候选。

### 3.2 样本导入与导出

`io_shp.py` `io_attr.py` `roi.py` + `ui/dialogs.py` 的导入导出对话框

三种样本组织方式的导入（文件名承载类别 A/B、属性字段承载类别 C）、一类一文件导出、导出层级标记、按范围空间筛选。

导入格式与导出目标本身应做成**可扩展点**——将来某个客户要一种新格式，是加一个模块，不是改底座。

### 3.3 画布与图层

`annotate/layers.py` `georef.py` `layerspec.py` `layer_props.py` `mapview.py` 的渲染与导航部分、`panels.py`

图层模型（影像 / 配准图片 / 矢量 / 样本）、配准嗅探、分块渲染与后台重采样、平移缩放、图层清单与唯一值赋色、图层的可序列化描述。

**绘制与编辑不在这里**，见 4.1。

### 3.4 窗口外壳

`ui/library_window.py`（瘦身后）`theme.py` `icons.py` `chrome.py` `tasks.py` `tree_panel.py` `inspector.py` `run.py`

主窗口装配、设计令牌与深浅色主题、共用控件、长任务进度与取消、样本树、检查器。外壳还多一项新职责：**装载模块并把扩展点交给它们**，见第 6 节。

## 4. 功能模块

每个模块自带界面入口、参数配置、随包数据与自测，模块缺席时对应入口不出现（不是灰掉）。

| 模块 id | 名称 | 主要文件 | 行数 | 随包数据 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| `annotate` | 标注编辑 | `annotate/editing.py` + `mapview.py` 的绘制与顶点编辑 | ~1 400 | — | — |
| `spectra` | 光谱曲线 | `spectra.py` `ui/plot.py` `ui/plot_view.py` `ui/spectrum_window.py` | 3 702 | `spectral_features.json` 2 KB | — |
| `speclib` | 光谱库与匹配 | `speclib.py` `specmatch.py` `ui/speclib_window.py` | 1 961 | `spectral_library.slb` 13 MB | `spectra` |
| `groupspec` | 类别平均谱 | `groupspec.py` `ui/groupspec_dialog.py` | 985 | — | `spectra` |
| `clip` | 样本裁剪与数据集导出 | `clip.py` + `ClipDialog` | ~900 | — | — |
| `toolbox` | 预处理工具箱 | `toolbox/` + `ui/toolbox_window.py` `ui/toolbox_params.py` | 4 581 | — | — |
| `denoise` | 深度去噪 Worker | `workers/hyde_denoise/` | 997 | 模型 20 MB | `toolbox` |
| `basemap` | 在线底图 | `annotate/tiles.py` `basemap.py` | 582 | — | — |
| `legend` | 岩性图例库 | `legend.py` | 270 | `lithology_legend.json` 159 KB | — |

各模块为什么值得单独拿出来：

### 4.1 标注编辑

浏览与编辑是两种交付形态。给数据使用方的版本只需要看图和导出，落点、画面、改顶点、删要素、暂存与撤销栈是生产方才用的能力；去掉它同时去掉了误改样本库的整条风险路径，只读发行版因此是**更安全**而不只是更小的产品。

### 4.2 光谱曲线

取谱、无效值与定标处理、SG 平滑、论文样式出图。样本库客户里有相当一部分只做样本组织与分发，从不看谱线。这个模块也是 `speclib` 与 `groupspec` 的地基，三者应作为一个功能包一起卖或一起不卖。

### 4.3 光谱库与匹配

13 MB 随包数据 + 光谱重采样 / 包络线去除 / SAM·SCM·SID 匹配。数据本身有来源许可约束，能单独摘除对合规交付有实际意义。

### 4.4 样本裁剪

几何 → 像素位置 → 影像小块，产出深度学习训练集。只有做模型训练的客户需要，`config.py` 里整整一段裁剪参数（块大小、格式、划分方式、抽样上限）也随它一起走。

### 4.5 预处理工具箱与去噪 Worker

工具箱本就是平台级独立包，桌面端只是它的一个使用者；去噪 Worker 更是独立 conda 环境、进程外调用、20 MB 模型。这两个是最容易摘、也最该单独定价的部分。工具箱在，去噪不一定在；去噪在，工具箱必须在。

### 4.6 在线底图

瓦片编号、下载、磁盘缓存。内网交付的客户机器不出外网，这个模块装了也是死入口，且它是唯一一处主动发起外网请求的代码——摘掉它，整个产品可以声明为完全离线运行。

### 4.7 岩性图例库

岩性名 → 标准岩性类 → 颜色的映射表，159 KB 数据带许可来源。不做岩性专题图的客户用不上。

## 5. 拆分前必须做的四处手术

这四处不是重构洁癖，是模块化物理上做不成的阻碍点。

### 5.1 消掉 `annotate` → `ui` 的反向依赖

```
samplelib/annotate/mapview.py:39   from ..ui import theme as _theme
samplelib/annotate/panels.py:17    from ..ui.chrome import ...
```

画布（底座）反过来依赖界面层，模块与底座就没有一条能画的线。把 `theme.py` `icons.py` `chrome.py` `tasks.py` 提成底座里的 `uikit`，画布与界面共同依赖它，依赖方向恢复单向。

### 5.2 抽出 `RasterSource` 协议

```
samplelib/ui/spectrum_window.py:34    from ..annotate import layers as L
samplelib/ui/groupspec_dialog.py:26   from ..annotate import layers as L
samplelib/ui/inspector.py:35          from ..annotate import layers as L
```

光谱模块直接依赖画布的图层具体类。真正需要的只是「一个能按坐标取像元、报得出波长与定标的栅格数据源」。把它定义成底座的协议，光谱模块依赖协议，画布提供实现——光谱模块从此可以脱离画布单测，也可以在别的宿主里复用。

### 5.3 把画布工具从常量改成对象

`annotate/mapview.py:309` 的 `Tool` 是一组字符串常量，八种工具的行为散在 `MapView` 的事件处理分支里（该文件 2 638 行，单个 `MapView` 类占 2 100 行）。改成 `MapTool` 协议（`press` / `move` / `release` / `paint` / `cursor` / `hint`）加 `MapView.register_tool()`，各工具立刻各归其位：

- 底座保留：平移、选择
- `annotate` 模块贡献：画点、画面、改顶点、删要素
- `spectra` 模块贡献：取谱
- 底座的 ROI 能力贡献：画范围

这是整个方案里工作量最大、收益也最大的一处。做完之后画布内核会瘦下来一大截，且新增一种交互不再需要碰 `MapView`。

### 5.4 主窗口瘦身

`ui/library_window.py` 2 124 行，把所有功能的入口装配在一处：仅 spectrum 相关引用就有 36 处，另有 speclib、toolbox、clip 的菜单与信号连接。这些代码应当搬进各模块的 `setup(host)`，主窗口只保留外壳与扩展点。

配套的两件小事：`config.py` 按模块拆（裁剪段归 `clip`、底图段归 `basemap`、标注段归 `annotate`），随包数据按模块归位（`spectral_library.slb` 归 `speclib`、`lithology_legend.json` 归 `legend`）。

## 6. 装配机制

三件事必须同时做到，缺一件模块化就是假的：**代码上拆得开、界面上裁得掉、打包时减得了**。

### 6.1 模块清单

每个模块一份 `module.json`：

```json
{
  "id": "spectra",
  "name": "光谱曲线与光谱库",
  "version": "1.0.0",
  "summary": "画布上取谱、论文样式出图、光谱库检索与匹配、按类别取平均谱",
  "requires": [],
  "data": ["data/spectral_features.json", "data/spectral_library.slb"]
}
```

用 JSON 而不是 YAML：桌面端的环境里只有 GDAL、Qt、
NumPy 这几样，为一份十几行的清单再拉一个解析器进来不值当；更要紧的是打包 spec
必须在**不 import samplelib** 的前提下读它（import 会把 GDAL 与 Qt 一并拉起来，
既慢又干扰依赖分析），而 json 是标准库。

清单先校验再 import：依赖缺失、版本不匹配、数据文件不在，都应当在装载前报出来，而不是等用户点开菜单才崩。

### 6.2 宿主扩展点

底座提供 `ModuleHost`，模块只认它，不认主窗口的任何具体类：

```python
class ModuleHost(Protocol):
    # 装配期
    def add_menu_action(self, menu_id, text, slot, shortcut=None, icon=None) -> QAction
    def add_toolbar_action(self, group_id, action) -> None
    def add_map_tool(self, tool: MapTool) -> None
    def add_dock(self, dock_id, factory, area) -> None
    def add_inspector_page(self, page) -> None
    def add_import_format(self, fmt) -> None
    def add_export_target(self, target) -> None

    # 运行期信号
    signals: HostSignals        # libraryOpened / layerChanged / pixelPicked / selectionChanged

    # 服务
    def store(self) -> SampleStore
    def raster_sources(self) -> list[RasterSource]
    def run_task(self, task) -> TaskHandle
    def theme(self) -> Theme
```

模块侧只写一个入口：

```python
# litho_modules/speclib/__init__.py
def setup(host: ModuleHost) -> None:
    host.add_menu_action("视图", "光谱库…", lambda: SpectralLibraryWindow(host).show())

MODULE = StudioModule(id="speclib", setup=setup)
```

底座对模块的可见性到此为止：模块能往外壳上挂东西、能订阅事件、能取到数据服务，不能反过来改外壳的结构。

### 6.3 发行版 profile

一份 profile 同时驱动三处：运行期装载哪些模块、打包时带哪些代码与数据、关于框里显示什么。

```yaml
edition: analysis
display_name: 岩谱 LithoSpectra · 分析版
modules: [annotate, basemap, legend, spectra, speclib, groupspec]
```

`packaging/LithoSpectra.spec` 读同一份 profile 生成 `hiddenimports` / `excludes` / `datas`——不装的模块，它的 13 MB 光谱库根本不进包，而不是进了包再灰掉入口。

### 6.4 发行版组合示例

| 发行版 | 模块 | 面向 |
| --- | --- | --- |
| 基础版 | 无（仅底座） | 只做样本组织与分发，只读交付 |
| 标注版 | `annotate` `basemap` `legend` | 野外与室内样本生产 |
| 分析版 | 标注版 + `spectra` `speclib` `groupspec` | 岩性光谱分析 |
| 训练版 | 分析版 + `clip` `toolbox` | 深度学习数据集生产 |
| 全功能版 | 训练版 + `denoise` | 平台完整交付 |

体积上要说实话：包体两百多兆的大头是 Qt 与 GDAL，模块能省下的是 13 MB 光谱库、20 MB 去噪模型、0.16 MB 图例库，约占一成七。**模块化真正省下的不是体积，是责任面**——验收范围、授权边界、外网访问声明、出问题时要查的代码量，这几样才是按客户裁剪的理由。

### 6.5 授权与发现

默认走**装了即启用**：模块包在磁盘上、清单校验通过，功能就在。不引入运行期开关去伪装成"未购买"——同一份二进制里藏着能力再用界面挡住，既挡不住也不体面。

试用版与限时授权是另一回事，需要时在 profile 之上叠一层签名许可，控制的是**能不能跑**而不是**在不在包里**。这一层现在不做。

## 7. 落地步骤

| 阶段 | 内容 | 判定完成 |
| --- | --- | --- |
| 一 · 已完成 | 消反向依赖（5.1）、抽 `RasterSource`（5.2） | `annotate` 与 `spectra` 各自可脱离对方 import |
| 二 · 已完成 | 画布工具对象化（5.3） | `MapView` 不再有 `if tool == ...` 分支 |
| 三 · 已完成 | 定义 `ModuleHost` 与模块清单契约，主窗口瘦身（5.4） | 主窗口不 import 任何模块 |
| 四 · 进行中 | 按第 4 节逐个模块搬迁，一次一个，每个搬完跑全量自测 | `tests/run_all.py` 全绿。光谱、裁剪、工具箱、在线底图已搬完；标注编辑与瓦片下载器还在底座里 |
| 五 | profile 驱动打包，出基础版与全功能版两个产物做对照 | 基础版无光谱入口且包内无 `.slb` |

先做一、二、三三个阶段的骨架，第四阶段可以按模块分批推进，不必一次拆完——底座与契约立住之后，每个模块是独立可交付的一小步。

顺序上建议第四阶段先搬 `toolbox` 与 `denoise`：它们本就在桌面端之外，`toolbox_bridge` 的探测范式也已经跑通，用它们验证 `ModuleHost` 的形状，代价最小。

## 8. 风险

**主窗口瘦身与画布工具对象化会大面积改动两个最大的文件**（`library_window.py` 2 124 行、`mapview.py` 2 638 行），这两处也是缺陷回归最集中的地方。搬迁期间 `tests/test_fixes.py` 里每个已复现缺陷的用例都要保持绿灯，不通过不合并。

**模块组合数会带来测试面膨胀**。对策是只承诺第 6.4 节的五个发行版组合，每个组合有一条冒烟路径，不承诺任意模块自由组合。

**`spectra` 与 `annotate` 之间存在事实上的双向期待**：取谱工具画在画布上，谱线窗口又要跟着画布图层切换。`RasterSource` 协议加 `pixelPicked` / `layerChanged` 信号足以覆盖，但这条边界要在阶段一就定死，后面每加一处直连都会让基础版做不出来。
