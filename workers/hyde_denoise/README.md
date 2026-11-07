# HyDe 去噪 Worker

Worker 通过 stdin 接收一个严格的 JSON 请求，通过 stdout 返回 `started`、
`progress`、`warning`、`completed` 或 `failed` JSONL 事件。它不发现产品、不选择
算法、不创建输出目录，只原位更新请求指定的暂存产品 TIFF。

当前工具箱参数面板公开：

- `FastHyDe`：传统基线，生产状态为 `available`，但大于 512×512 的场景默认拒绝
  直接整景计算，不做空间分块——子空间与噪声协方差都是全局统计量。
- `FastHyIe`：Worker 从当前原始 VN/SW 连续波段组构造同形 mask，严格采用
  `1=可用、0=缺失`；默认只依据非有限值和逐波段 NoData，也可显式加入零值/饱和值。
  源 NoData 默认保留；`inpaint_masked` 必须显式选择后才写回缺失位置修复值。
- `QRNN3D`：空间与光谱窗口均做重叠加权融合，权重必须登记并校验哈希。
- `L1HyMixDe`、`HyMiNoR`、`HyRes`、`WSRRR`、`OTVCA`、`FORPDN_SURE`：均已在真实
  ZY1E 数据上经完整管线端到端跑通，`status` 为 `available`，不需要额外的允许开关。

除需要登记权重的 `QRNN3D` 外，以上算法都在真实 ZY1E AHSI L1A 产品（256×256×166，
VN 76 + SW 90 波段）上跑通过完整管线，八项校验（文件集合、逻辑元数据、非核心文件
哈希、冻结波段像元）全部通过。

输出选择不是通用的“盲取第一项”，而是算法注册表中的显式合同：`WSRRR` 选择
`denoised` 并舍弃 `principal_components`，`OTVCA` 选择 `denoised` 并舍弃
`features`；舍弃项的类型、形状和 dtype 写入运行信息。固定版本的 `FORPDN_SURE`
公开 `forward` 只返回去噪影像，不按 tuple 处理。

产品发现不在 Worker 内完成。工具箱侧 `DenoiseSensorRegistry` 把 ZY1E/ZY1F 或未来
GF5 的产品组织统一映射成 `VN`/`SW` 栅格任务；Worker 始终只处理传感器无关的
`[H, W, B]` 数组和按协议声明的附加输入。

Worker 跑在工具箱左下角选中的那个 Python 环境里，没有单独的解释器参数。该环境必须
同时装齐 GDAL 与 torch + `hyde-images`；装不齐会在克隆产品之前被
`WorkerRunner.preflight` 拦下。

Worker 的上游、环境、已知上游缺陷和模型准备见 [UPSTREAM.md](UPSTREAM.md)。其中两条
会直接影响结果，务必先读：非 i.i.d. 反变换的括号错误（已在包装层修正并记入产物清单），
以及 `FORPDN_SURE` 的 `scale` 必须为 `True`。
