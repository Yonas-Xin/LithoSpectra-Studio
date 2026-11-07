<div align="center">
  <img src="LithoSpectra-Studio/assets/logo_256.png" width="128" alt="岩谱 LithoSpectra">

  <h1>岩谱 · LithoSpectra Studio</h1>

  <p>面向地质与高光谱遥感的桌面工作台</p>

  <p>样本管理 · 地图标注 · 光谱分析 · 野外记录 · 深度学习</p>
</div>

LithoSpectra 将地质样本、遥感影像、标准光谱与野外调查记录放在同一个工作空间，支持 macOS 与 Windows。界面使用 React、TypeScript 和 OpenLayers，由 pywebview 提供桌面窗口，FastAPI 与 GDAL 提供本地服务和地理数据处理。

## 下载桌面版

无需安装 Python 或 Node.js，可从 [GitHub Releases](https://github.com/Yonas-Xin/LithoSpectra-Studio/releases/latest) 下载现成安装包。

| 平台 | 安装包 | 要求 |
| --- | --- | --- |
| macOS | [1.0.0 · Apple Silicon DMG](https://github.com/Yonas-Xin/LithoSpectra-Studio/releases/download/v1.0.0/LithoSpectra-Studio-1.0.0-macos-arm64.dmg) | macOS 12+，Apple Silicon |
| Windows | [1.0.0 · 64 位 ZIP](https://github.com/Yonas-Xin/LithoSpectra-Studio/releases/download/v1.0.0/LithoSpectra-Studio-1.0.0-windows-x64.zip) | Windows 10 / 11，Edge WebView2 Runtime |

macOS 将 `.app` 拖到“应用程序”后运行；Windows 完整解压后双击 `LithoSpectraWeb.exe`，保留同级的 `lib` 与 `resources` 文件夹。安装包的构建日期、功能限制与 SHA-256 校验文件见 [发行说明](https://github.com/Yonas-Xin/LithoSpectra-Studio/releases/tag/v1.0.0)。

## 功能

- **地质样本库**：使用 GeoPackage 保存点、面样本与多级分类，支持 Shapefile、GeoJSON 等格式导入导出，以及撤销、备份和按类别裁剪样本集。
- **地图与标注**：多图层浏览、影像波段组合、坐标转换、点面编辑、配准图片与在线底图。
- **光谱分析**：影像取谱、标准光谱检索、光谱匹配、曲线绘制与导出；标准光谱库随源码提供，个人光谱库由使用者在本地维护。
- **野外记录**：奥维 `.ovobj`、照片、GPX、KML 与 GeoJSON 导入，管理点位、轨迹、照片与录音，支持照片编辑、地质水印和离线语音转文字。
- **遥感工具箱**：产品组织、波段处理与 HyDe 去噪 Worker 接入。
- **深度学习**：监督分类、对比学习、任务监控、整景预测、特征分析与相似区域检索，支持独立 PyTorch 环境。

## 目录

```text
.
├── LithoSpectra-Studio/   桌面端、React 前端、FastAPI 接口与打包脚本
├── toolbox/              遥感预处理工具箱与环境管理
├── workers/
│   ├── litho_dl/         分类、对比学习与预测
│   └── hyde_denoise/     HyDe 算法适配
├── assets/               传感器波长表
├── tests/                工具箱与 Worker 测试
└── docs/                 模块设计文档
```

桌面端会从同级目录加载 `toolbox/` 和 `workers/`，克隆后请保持上述目录关系。

## 安装与构建

需要 Git、Conda 与 Node.js 22.12+。Python 环境由 `environment.yml` 定义，前端依赖由 `package-lock.json` 锁定。

```bash
git clone https://github.com/Yonas-Xin/LithoSpectra-Studio.git
cd LithoSpectra-Studio/LithoSpectra-Studio
conda env create -f environment.yml
conda activate samplelib
cd web
npm ci
npm run build
cd ..
```

macOS 构建桌面程序：

```bash
bash packaging/build_web_macos.sh
```

产物位于 `packaging/dist-web/`。Windows 的环境准备、WebView2 与打包步骤见 [Windows 构建指南](LithoSpectra-Studio/packaging/WINDOWS.md)。

前端开发可在 `web/` 中运行 `npm run dev`，并将 `webapi/desktop_app.py` 顶部的 `UI_SOURCE` 设为 `dev`。桌面端配置、数据格式和完整操作说明见 [使用手册](LithoSpectra-Studio/README.md)。

## 独立计算环境

桌面主程序不加载 PyTorch。训练和预测在使用者选择的独立环境中运行，参考 [深度学习 Worker](workers/litho_dl/README.md) 与 [环境定义](workers/litho_dl/environment.yml)。

HyDe 去噪需要独立安装上游依赖及相应模型，参考 [HyDe Worker](workers/hyde_denoise/README.md) 与 [上游版本及许可说明](workers/hyde_denoise/UPSTREAM.md)。LaMa 与 SenseVoice 模型通过程序按需下载。

## 数据与服务

标准光谱库位于 `LithoSpectra-Studio/samplelib/modules/spectra/data/spectral_library.slb`，包含 ASTER、USGS、IGCP-264 与植被参考谱，其来源和引用信息保存在库文件内。图例数据与光谱数据的使用条款见 [第三方说明](THIRD_PARTY_NOTICES.md)。

在线底图所需密钥由使用者在界面中设置，保存在本地 `keys.json`。样本库、照片、轨迹、个人光谱、模型权重与缓存由使用者自行提供和管理。

ESRI North 字体不随源码分发；缺少该字体时，地质水印使用程序绘制的简易箭头。

## 验证

在已安装桌面端依赖的环境中运行：

```bash
cd LithoSpectra-Studio
python tests/run_all.py
cd web
npm ci
npm run build
```

在仓库根目录运行工具箱与 Worker 测试，需要 pytest 及对应模块的运行依赖：

```bash
python -m pytest tests/toolbox tests/workers -q -p no:cacheprovider
```

需要真实遥感产品、预训练权重或参考源项目的测试可能跳过。问题反馈与功能建议请提交 [Issue](https://github.com/Yonas-Xin/LithoSpectra-Studio/issues)。

## 许可证

项目自有代码采用 [MIT License](LICENSE)。第三方依赖、数据与可选模型遵循各自的许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md)。
