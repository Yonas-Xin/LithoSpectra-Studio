# 第三方组件与数据

项目根目录的 MIT License 适用于项目自有代码。第三方组件、数据、字体和模型的版权及使用条款由各自权利人保留。

## 标准光谱库

`LithoSpectra-Studio/samplelib/modules/spectra/data/spectral_library.slb` 为标准参考光谱的编译文件，来自 ASTER Spectral Library v2.0、USGS Spectral Library v7、IGCP-264 与植被参考光谱。原始出处、文献引用、分类来源与处理说明保存在 ZIP 格式库文件的 `manifest.json` 和 `index.json` 内，也可在应用的光谱库信息中查看。编译程序为 `LithoSpectra-Studio/tools/build_speclib.py`。

光谱数据本身不因打包为 `.slb` 而变更许可，不包含在本项目 MIT 授权范围内。使用或再分发时应保留来源信息，并遵循对应数据提供方的使用与引用要求。

- ASTER：Baldridge, A. M., Hook, S. J., Grove, C. I., Rivera, G., 2009. The ASTER Spectral Library Version 2.0. *Remote Sensing of Environment*, 113, 711–715. [ECOSTRESS Spectral Library](https://speclib.jpl.nasa.gov/)。
- USGS：Kokaly, R. F. et al., 2017. *USGS Spectral Library Version 7*. U.S. Geological Survey Data Series 1035. [DOI: 10.3133/ds1035](https://doi.org/10.3133/ds1035)。
- IGCP-264 与植被参考光谱：逐库出处和引用信息见 `manifest.json` 中的 `sources`。

## 岩性图例

`LithoSpectra-Studio/samplelib/data/lithology_legend.json` 由以下资料整理，保留源地址与许可元数据，并补充中文名称、匹配规则、颜色继承及分类映射：

- [IUGS-CGI SimpleLithology](https://github.com/CGI-IUGS/cgi-vocabs)：CC BY 4.0。
- [Macrostrat lithologies](https://macrostrat.org/api/defs/lithologies?all)：CC BY 4.0。
- [Macrostrat 国际年代地层表](https://macrostrat.org/api/defs/intervals?timescale=international%20ages)：CC BY 4.0。

[CC BY 4.0 许可全文](https://creativecommons.org/licenses/by/4.0/legalcode)。数据生成程序为 `LithoSpectra-Studio/tools/build_legend_library.py`。

## 深度学习与去噪

`workers/litho_dl` 中的骨干网络移植自作者的 MIT 项目 `Hyperspectral_Deep-Learning_Private`，原版权声明与许可全文见 [LICENSES/Hyperspectral_Deep-Learning_Private-MIT.txt](LICENSES/Hyperspectral_Deep-Learning_Private-MIT.txt)。

HyDe 通过独立环境调用，不包含上游源码或预训练权重。固定上游版本、BSD-3-Clause 总许可及部分算法的 GPL-2.0 声明见 [workers/hyde_denoise/UPSTREAM.md](workers/hyde_denoise/UPSTREAM.md)。安装上游依赖与分发相关产物时，应遵循对应组件许可。

LaMa、SenseVoice 与 QRNN3D 等可选模型不随源码分发，模型权重遵循其发布方的条款。LaMa 与 SenseVoice 下载地址及校验值分别见照片修补和语音识别模块；QRNN3D 的登记信息见 `workers/hyde_denoise/models.json`。

## 运行依赖与字体

React、OpenLayers、Ant Design、FastAPI、pywebview、GDAL、PyTorch 等依赖由包管理器安装，依赖版本见各 `environment.yml`、`web/package.json` 与 `web/package-lock.json`。这些依赖遵循各自随包提供的许可证。

ESRI North 字体属于第三方专有资源，不随本仓库分发。程序在缺少该字体时使用自绘箭头；其他系统字体由使用者的操作系统提供。
