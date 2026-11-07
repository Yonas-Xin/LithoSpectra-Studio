# -*- coding: utf-8 -*-
"""以隔离 Worker 对 Sensor Adapter 发现的 VN/SW 资产执行 HyDe 去噪。"""

from __future__ import annotations

import json
import os
import platform
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..core import Logger, Param, Tool, ToolError, WorkItem
from ..denoise.adapters import SENSOR_REGISTRY
from ..denoise.product import (ProductTransaction, band_pixel_hashes,
                               product_file_hashes, raster_metadata_snapshot,
                               scan_product_bands)
from ..denoise.protocol import DenoiseRequest, RasterTask
from ..denoise.runner import WorkerRunner


# =============================================================================
# 配置区
# =============================================================================

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKER_SCRIPT = REPOSITORY_ROOT / "workers/hyde_denoise/worker.py"
HYDE_COMMIT = "251a4f461cd8678462cfda09009c183da1b5a092"
HYDE_VERSION = "0.4.3"
TOOL_VERSION = "0.4.0"


def worker_python() -> Path:
    """跑算法的解释器：就是当前这一个。

    以前这是一个参数，指向一个专门的隔离环境，于是"用哪个 Python"在界面上被问
    了两遍——左下角问一次，参数表里再问一次，而两处填不一致时的表现（批处理在
    A 环境跑、算法却在 B 环境跑）没有任何提示。现在只认左下角那一个：批处理进
    程是它启动的，Worker 又是批处理的子进程，两者天然同环境，不可能对不上。

    Worker 仍是独立进程——那一层隔离防的是 Torch/OpenMP 把批处理进程带崩，与
    "在哪个环境"是两回事，去掉参数不影响它。代价是这个环境要同时装齐 GDAL 和
    torch + hyde；装不齐会在克隆产品之前被 ``WorkerRunner.preflight`` 拦下。
    """
    return Path(sys.executable)


class HyDeDenoiseProductTool(Tool):
    id = "hyde_denoise_product"
    name = "HyDe 高光谱产品去噪"
    summary = "识别产品目录中的 VNIR/SWIR 影像，逐个去噪后替换有效像元；中途失败时原产品不变"
    input_kind = "product_dir"
    output_kind = "directory"

    params = (
        Param("algorithm", "算法", "FastHyDe", kind="choice",
              choices=("FastHyDe", "FastHyIe", "L1HyMixDe", "HyMiNoR", "HyRes",
                       "WSRRR", "OTVCA", "FORPDN_SURE", "QRNN3D"),
              # 九个算法都已在真实 ZY1E 数据上端到端跑通，不再标注实验状态。
              # QRNN3D 仍标出它的前提：要有登记并通过哈希校验的权重。
              choice_labels=(("QRNN3D", "QRNN3D（需登记权重）"),),
              help="FastHyDe 为基线；FastHyIe 会按 mask_policy 从原始数据构造同形"
                   "有效性掩膜。QRNN3D 的登记权重训练于合成噪声，用于国产传感器"
                   "原始 DN 前应在真实 ROI 上自行评估。"),
        Param("device", "计算设备", "auto", kind="choice",
              choices=("auto", "cpu", "mps"),
              choice_labels=(("auto", "自动"), ("cpu", "CPU"), ("mps", "Apple GPU（MPS）"))),
        Param("noise_type", "噪声类型", "additive", kind="choice",
              choices=("additive", "poisson"),
              choice_labels=(("additive", "加性噪声"), ("poisson", "泊松噪声"))),
        Param("iid", "加性噪声各波段独立同分布", False, kind="bool",
              help="资源系列 AHSI 建议不勾选。噪声类型为泊松时不使用此参数。"),
        Param("k_subspace", "信号子空间维数", 10, kind="int"),
        Param("l1_p", "L1HyMixDe 异常比例 p", 0.05, kind="float"),
        Param("max_iter", "L1HyMixDe 最大迭代", 10, kind="int"),
        Param("hyminor_lam", "HyMiNoR 稀疏权重", 10.0, kind="float"),
        Param("iterations", "HyMiNoR 迭代次数", 50, kind="int"),
        Param("min_iter", "HyRes 最小迭代", 2, kind="int"),
        Param("rank", "WSRRR 秩", 10, kind="int"),
        Param("features", "OTVCA 特征数", 10, kind="int"),
        Param("num_itt", "OTVCA 迭代次数", 10, kind="int"),
        Param("otvca_lam", "OTVCA TV 权重", 0.01, kind="float"),
        Param("forpdn_domain", "FORPDN 域", "wavelet", kind="choice",
              choices=("wavelet", "signal"),
              choice_labels=(("wavelet", "小波域"), ("signal", "信号域"))),
        Param("scale_const", "FORPDN 缩放常数", 255.0, kind="float"),
        Param("wavelet_level", "FORPDN 小波阶数", 6, kind="int"),
        Param("mask_policy", "FastHyIe 掩膜构造", "nodata", kind="choice",
              choices=("nodata", "nodata_zero", "nodata_zero_saturation"),
              choice_labels=(("nodata", "仅 NoData 与非有限值"), ("nodata_zero", "另把 0 视为缺失"),
                             ("nodata_zero_saturation", "另把 0 与饱和值视为缺失")),
              help="哪些像元算作缺失。默认只把 NoData 与非有限值算作缺失。"),
        Param("mask_writeback", "FastHyIe 缺失值写回", "preserve_source_nodata",
              kind="choice", choices=("preserve_source_nodata", "inpaint_masked"),
              choice_labels=(("preserve_source_nodata", "保留源产品的 NoData"),
                             ("inpaint_masked", "写回缺失位置的修复值")),
              help="默认保留源产品的 NoData；选「写回缺失位置的修复值」时，FastHyIe 修复出的缺失像元也写回产品。"),
        Param("tile_size", "神经网络空间窗口", 256, kind="int"),
        Param("halo", "窗口重叠/缓冲", 16, kind="int"),
        Param("clip_mode", "写回裁剪范围", "source_band_range", kind="choice",
              choices=("source_band_range", "dtype_range"),
              choice_labels=(("source_band_range", "源波段的取值范围"), ("dtype_range", "数据类型的取值范围"))),
        Param("model_id", "登记模型 ID", "qrnn3d_blind_gaussian", kind="choice",
              choices=("qrnn3d_blind_gaussian", "qrnn3d_mixed"),
              choice_labels=(("qrnn3d_blind_gaussian", "盲高斯噪声"), ("qrnn3d_mixed", "混合噪声"))),
        Param("timeout_seconds", "单产品超时（秒，0=不限）", 0, kind="int"),
    )

    def discover(self, root: Path, params: dict, log: Logger) -> list[WorkItem]:
        self._validate_params(params)
        products = SENSOR_REGISTRY.discover(root)
        for product in products:
            product_variant = getattr(product, "variant", None)
            variant = "，产品变体 %s" % product_variant if product_variant else ""
            total_bands = sum(part.bands for part in product.parts)
            log("识别 %s %s %s：%s（%d×%d×%d%s，Adapter=%s）"
                % (product.satellite, product.sensor, product.level, product.root.name,
                   product.rows, product.cols, total_bands, variant, product.adapter_id))
        return [WorkItem(product.root.name, product.root,
                         {"product": product.to_dict()}) for product in products]

    def output_path(self, item: WorkItem, out_dir: Path, params: dict) -> Path:
        suffix = params["algorithm"].upper()
        return Path(out_dir) / (item.source.name + "__HYDE_" + suffix)

    def companions(self, out_path: Path) -> list[Path]:
        return [out_path.parent / (out_path.name + ".processing.json")]

    def validate_output(self, item: WorkItem, out_path: Path) -> None:
        from ..denoise.product import ensure_output_safe
        ensure_output_safe(item.source, out_path)

    def transients(self, out_path: Path) -> list[Path]:
        manifest = self.companions(out_path)[0]
        return [out_path.with_name(out_path.name + ".partial"),
                manifest.with_name(manifest.name + ".partial")]

    def process(self, item: WorkItem, out_path: Path, params: dict, log: Logger) -> None:
        self._process(item, out_path, params, log, should_stop=None)

    def process_with_control(self, item: WorkItem, out_path: Path, params: dict,
                             log: Logger, should_stop=None) -> None:
        self._process(item, out_path, params, log, should_stop=should_stop)

    def _process(self, item: WorkItem, out_path: Path, params: dict, log: Logger,
                 should_stop) -> None:
        self._validate_params(params)
        # 先把 Worker 环境验一遍再动产品：克隆一景是几个 GB 的复制，
        # 跑到启动 Worker 才发现环境不对，那份复制就白做了。
        runner = WorkerRunner(worker_python(), WORKER_SCRIPT)
        preflight = getattr(runner, "preflight", None)
        if preflight is not None:
            preflight(log)
        adapter = SENSOR_REGISTRY.detect(item.source)
        product = adapter.open(item.source)
        scan = scan_product_bands(product)
        for part in product.parts:
            frozen = scan[part.name]["frozen_bands"]
            groups = scan[part.name]["valid_groups"]
            log("%s：冻结 %d 个常量/无有效值波段；有效连续组 %s"
                % (part.name, len(frozen), self._format_groups(groups)))

        before_snapshots = {
            part.name: raster_metadata_snapshot(part.raster) for part in product.parts
        }
        frozen_before = {
            part.name: band_pixel_hashes(part.raster, scan[part.name]["frozen_bands"])
            for part in product.parts
        }
        started_at = datetime.now(timezone.utc)
        transaction = ProductTransaction(product.root, out_path)
        worker_result = None
        verification = None
        with transaction as staging:
            request = self._request(product, staging, scan, params)

            def on_event(event) -> None:
                if event.event in {"progress", "warning"}:
                    prefix = "[Worker 警告] " if event.event == "warning" else "[Worker] "
                    log(prefix + event.message)

            timeout = float(params["timeout_seconds"] or 0) or None
            worker_result = runner.run(request, on_event=on_event,
                                       should_cancel=should_stop,
                                       timeout_seconds=timeout)
            # 回读验证始终执行，不做成开关：它验的是"除了该改的像元之外一个
            # 字节都没动"，整景实测约 0.5 s。花半秒的正确性检查没有理由提供
            # 一个关掉它的选项——真关掉了，产品坏没坏也就无从知道了。
            verification = adapter.verify_clone(product, staging, before_snapshots)
            cloned = adapter.open(staging)
            for part in cloned.parts:
                after = band_pixel_hashes(
                    part.raster, scan[part.name]["frozen_bands"])
                if frozen_before[part.name] != after:
                    raise ToolError("%s 冻结波段像元发生变化" % part.name)
            verification["frozen_band_pixels_equal"] = True
            transaction.commit()

        completed_at = datetime.now(timezone.utc)
        manifest = {
            "schema_version": 1,
            "tool": {"id": self.id, "version": TOOL_VERSION},
            "input_product": str(product.root),
            "output_product": str(Path(out_path).resolve()),
            "source_product_sha256": product_file_hashes(product.root),
            "product": product.to_dict(),
            "sensor_adapter": adapter.id,
            "algorithm": params["algorithm"],
            "algorithm_params": self._algorithm_params(params),
            "auxiliary_inputs": request.auxiliary_inputs,
            "device_policy": params["device"],
            "hyde": {"version": HYDE_VERSION, "commit": HYDE_COMMIT},
            "model_id": params["model_id"] if params["algorithm"] == "QRNN3D" else None,
            "tile_size": params["tile_size"],
            "halo": params["halo"],
            "clip_mode": params["clip_mode"],
            "band_detection": {
                "method": "exact_block_scan_excluding_nodata_and_nonfinite",
                "parts": scan,
            },
            "quantization": "clip_then_numpy_rint_then_source_uint16",
            "worker": worker_result.data if worker_result else None,
            "worker_warnings": list(worker_result.warnings) if worker_result else [],
            "verification": verification,
            "runtime": {
                "toolbox_python": sys.version.split()[0],
                "platform": platform.platform(),
                "worker_python": str(worker_python()),
            },
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "elapsed_seconds": (completed_at - started_at).total_seconds(),
        }
        self._write_manifest_atomic(self.companions(out_path)[0], manifest)

    @staticmethod
    def _validate_params(params: dict) -> None:
        if params["tile_size"] <= 0:
            raise ToolError("tile_size 必须为正整数")
        if params["halo"] < 0 or params["halo"] * 2 >= params["tile_size"]:
            raise ToolError("halo 必须满足 0 <= 2*halo < tile_size")
        if params["k_subspace"] <= 0:
            raise ToolError("k_subspace 必须为正整数")
        for key in ("max_iter", "iterations", "min_iter", "rank", "features",
                    "num_itt", "wavelet_level"):
            if params[key] <= 0:
                raise ToolError("%s 必须为正整数" % key)
        if not 0 < params["l1_p"] < 1:
            raise ToolError("l1_p 必须在 (0, 1) 内")
        if params["hyminor_lam"] <= 0 or params["otvca_lam"] <= 0 or \
                params["scale_const"] <= 0:
            raise ToolError("算法权重与 scale_const 必须为正数")
        if params["timeout_seconds"] < 0:
            raise ToolError("timeout_seconds 不能为负数")

    #: 每种算法吃哪些参数：worker 侧键名 → 本工具的参数名。
    #: 两侧名字对不上（p / l1_p、lam / hyminor_lam 与 otvca_lam），所以要显式配对。
    #: 这张表同时供两处使用——拼 worker 参数，以及界面上按算法决定显示哪些参数。
    #: 只留一份，是因为两份迟早会不一致，而不一致的表现是界面藏掉了真正生效的参数。
    _ALGORITHM_PARAMS: dict[str, dict[str, str]] = {
        "FastHyDe": {"noise_type": "noise_type", "iid": "iid",
                     "k_subspace": "k_subspace"},
        "FastHyIe": {"noise_type": "noise_type", "iid": "iid",
                     "k_subspace": "k_subspace"},
        "L1HyMixDe": {"k_subspace": "k_subspace", "p": "l1_p",
                      "max_iter": "max_iter"},
        "HyMiNoR": {"lam": "hyminor_lam", "iterations": "iterations"},
        "HyRes": {"min_iter": "min_iter"},
        "WSRRR": {"rank": "rank"},
        "OTVCA": {"features": "features", "num_itt": "num_itt",
                  "lam": "otvca_lam"},
        "FORPDN_SURE": {"domain": "forpdn_domain", "scale_const": "scale_const",
                        "wavelet_level": "wavelet_level"},
        "QRNN3D": {},
    }

    #: 不来自参数、按算法写死的 worker 参数
    #:
    #: FORPDN 的 scale 必须是 True——它不是"再归一化一次"，而是把数据按
    #: scale_const 放大到 [0, scale_const] 再在末尾原样缩回（可逆）。SURE 搜正则
    #: 参数用的 s_int_* 区间是绝对量纲的，数据落在 [0, 1] 时最优解直接落到区间
    #: 左端 0，等于不做任何收缩。实测（ZY1E VN 128×128×24）：scale=False 时
    #: RMSE 0.0002、原样返回输入；scale=True 时 RMSE 1.08、信噪比增益 59 dB。
    _ALGORITHM_CONSTANTS: dict[str, dict] = {"FORPDN_SURE": {"scale": True}}

    #: 与算法绑定、但不进 worker 的算法参数（走清单其它字段）
    _ALGORITHM_EXTRA_UI: dict[str, tuple[str, ...]] = {
        "FastHyIe": ("mask_policy", "mask_writeback"),
        "QRNN3D": ("model_id", "tile_size", "halo"),
    }

    @classmethod
    def _algorithm_params(cls, params: dict) -> dict:
        mapping = cls._ALGORITHM_PARAMS.get(params["algorithm"], {})
        built = {key: params[name] for key, name in mapping.items()}
        built.update(cls._ALGORITHM_CONSTANTS.get(params["algorithm"], {}))
        return built

    def visible_params(self, values: dict) -> set:
        """按当前算法决定界面上显示哪些参数。

        二十来个参数里大半只对某一种算法有意义，一次全铺出来使用者没法判断哪些
        该填。判断方向是**只藏明确属于别的算法的那些**，表里没提到的参数一律显示
        ——否则往参数表里新加一项、忘了同步这张表，它就会在所有算法下凭空消失，
        而这种消失没有任何报错。
        """
        algorithm = values.get("algorithm", "")
        owned: set[str] = set()          # 所有与某个算法绑定的参数
        for mapping in self._ALGORITHM_PARAMS.values():
            owned.update(mapping.values())
        for names in self._ALGORITHM_EXTRA_UI.values():
            owned.update(names)
        mine = set(self._ALGORITHM_PARAMS.get(algorithm, {}).values())
        mine.update(self._ALGORITHM_EXTRA_UI.get(algorithm, ()))

        visible = {param.name for param in self.params
                   if param.name not in owned or param.name in mine}
        # iid 只在加性噪声下有意义，算法对了还要看噪声类型
        if values.get("noise_type") != "additive":
            visible.discard("iid")
        return visible

    def _request(self, product, staging: Path, scan: dict, params: dict) -> DenoiseRequest:
        tasks = tuple(
            RasterTask(
                part=part.name,
                relative_raster=str(part.raster.relative_to(product.root)),
                valid_groups=tuple(tuple(group) for group in scan[part.name]["valid_groups"]),
                frozen_bands=tuple(scan[part.name]["frozen_bands"]),
            )
            for part in product.parts
        )
        return DenoiseRequest(
            job_id=str(uuid.uuid4()),
            source_product=str(product.root),
            staging_product=str(Path(staging).resolve()),
            algorithm=params["algorithm"],
            algorithm_params=self._algorithm_params(params),
            device=params["device"],
            rasters=tasks,
            auxiliary_inputs=(
                {"mask": {
                    "builder": "raw_validity",
                    "policy": params["mask_policy"],
                    "meaning": "1=viable,0=missing",
                    "writeback": params["mask_writeback"],
                }} if params["algorithm"] == "FastHyIe" else {}
            ),
            tile_size=params["tile_size"],
            halo=params["halo"],
            clip_mode=params["clip_mode"],
            model_id=params["model_id"] if params["algorithm"] == "QRNN3D" else "",
        )

    @staticmethod
    def _format_groups(groups) -> str:
        labels = []
        for group in groups:
            labels.append(str(group[0]) if len(group) == 1
                          else "%d-%d" % (group[0], group[-1]))
        return ", ".join(labels) or "无"

    @staticmethod
    def _write_manifest_atomic(path: Path, manifest: dict) -> None:
        partial = path.with_name(path.name + ".partial")
        partial.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                           + "\n", encoding="utf-8")
        os.replace(partial, path)


TOOL = HyDeDenoiseProductTool()
