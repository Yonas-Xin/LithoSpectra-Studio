# -*- coding: utf-8 -*-
"""HyDe 算法注册表、统一数值契约与安全神经网络加载。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np


class AlgorithmError(RuntimeError):
    """算法参数、依赖或数值合同不满足。"""


@dataclass(frozen=True)
class AlgorithmSpec:
    name: str
    status: str
    family: str
    allowed_params: frozenset[str]
    min_bands: int = 2
    tuple_output: bool = False
    requires_mask: bool = False
    output_names: tuple[str, ...] = ("denoised",)
    primary_output_index: int = 0


ALGORITHMS: dict[str, AlgorithmSpec] = {
    "FastHyDe": AlgorithmSpec(
        "FastHyDe", "available", "traditional",
        frozenset({"noise_type", "iid", "k_subspace"}), min_bands=2),
    "L1HyMixDe": AlgorithmSpec(
        "L1HyMixDe", "available", "traditional",
        frozenset({"k_subspace", "p", "max_iter"}), min_bands=2),
    "HyMiNoR": AlgorithmSpec(
        "HyMiNoR", "available", "traditional",
        frozenset({"lam", "iterations"}), min_bands=2),
    "HyRes": AlgorithmSpec(
        "HyRes", "available", "traditional", frozenset({"min_iter"}), min_bands=2),
    "WSRRR": AlgorithmSpec(
        "WSRRR", "available", "traditional", frozenset({"rank"}),
        min_bands=2, tuple_output=True,
        output_names=("denoised", "principal_components")),
    "OTVCA": AlgorithmSpec(
        "OTVCA", "available", "traditional",
        frozenset({"features", "num_itt", "lam"}), min_bands=2, tuple_output=True,
        output_names=("denoised", "features")),
    "FastHyIe": AlgorithmSpec(
        "FastHyIe", "available", "traditional",
        frozenset({"noise_type", "iid", "k_subspace"}), min_bands=2,
        requires_mask=True),
    "FORPDN_SURE": AlgorithmSpec(
        "FORPDN_SURE", "available", "traditional",
        frozenset({"s_int_st", "s_int_step", "s_int_end", "domain", "scale",
                   "scale_const", "wavelet_level"}), min_bands=2),
    "QRNN3D": AlgorithmSpec(
        "QRNN3D", "available", "neural", frozenset(), min_bands=2),
}


def algorithm_catalog() -> list[dict[str, Any]]:
    return [
        {"name": spec.name, "status": spec.status, "family": spec.family,
         "allowed_params": sorted(spec.allowed_params), "min_bands": spec.min_bands,
         "tuple_output": spec.tuple_output, "requires_mask": spec.requires_mask,
         "output_names": list(spec.output_names),
         "primary_output": spec.output_names[spec.primary_output_index]}
        for spec in ALGORITHMS.values()
    ]


def validate_algorithm(name: str, params: dict[str, Any],
                       bands: int) -> AlgorithmSpec:
    """校验算法名、参数集与波段数。

    这里以前还有一道"实验算法必须显式允许"的开关。它守的是"用户不小心选到未验证
    的算法"，可从下拉框里选中 HyRes 这个动作本身就是选择，再让人勾一次没有多出
    任何信息。九个算法此后都在真实 ZY1E 整景上端到端跑通，``status`` 统一为
    ``available``，开关与标注一并去掉。
    """
    try:
        spec = ALGORITHMS[name]
    except KeyError:
        raise AlgorithmError("未知算法 %r，可用：%s"
                             % (name, ", ".join(ALGORITHMS))) from None
    unknown = set(params) - spec.allowed_params
    if unknown:
        raise AlgorithmError("%s 含未知参数：%s" % (name, ", ".join(sorted(unknown))))
    if bands < spec.min_bands:
        raise AlgorithmError("%s 至少需要 %d 个连续有效波段" % (name, spec.min_bands))

    k_subspace = params.get("k_subspace")
    if k_subspace is not None and not 1 <= int(k_subspace) <= bands:
        raise AlgorithmError("k_subspace 必须在 1..%d 内" % bands)
    if params.get("noise_type", "additive") not in {"additive", "poisson"}:
        raise AlgorithmError("noise_type 只能是 additive 或 poisson")
    if "p" in params and not 0 < float(params["p"]) < 1:
        raise AlgorithmError("p 必须在 (0, 1) 内")
    for key in ("iterations", "max_iter", "min_iter", "rank", "features", "num_itt"):
        if key in params and int(params[key]) <= 0:
            raise AlgorithmError("%s 必须为正整数" % key)
    if "rank" in params and int(params["rank"]) >= bands:
        raise AlgorithmError("rank 必须小于输入波段数")
    if "features" in params and int(params["features"]) > bands:
        raise AlgorithmError("features 不能大于输入波段数")
    if name == "FORPDN_SURE":
        if params.get("domain", "wavelet") not in {"wavelet", "signal"}:
            raise AlgorithmError("FORPDN_SURE domain 只能是 wavelet 或 signal")
        if "scale_const" in params and float(params["scale_const"]) <= 0:
            raise AlgorithmError("scale_const 必须为正数")
        if "wavelet_level" in params and int(params["wavelet_level"]) <= 0:
            raise AlgorithmError("wavelet_level 必须为正整数")
    return spec


def validate_cube(image: np.ndarray, allow_nonfinite: bool = False) -> np.ndarray:
    array = np.asarray(image)
    if array.ndim != 3:
        raise AlgorithmError("算法输入必须是 [H, W, B] 三维数组")
    if array.shape[2] < 1 or array.shape[0] < 1 or array.shape[1] < 1:
        raise AlgorithmError("算法输入不能有空维度")
    array = np.asarray(array, dtype=np.float32)
    if not allow_nonfinite and not np.isfinite(array).all():
        raise AlgorithmError("算法输入含 NaN/Inf")
    return array


def normalize_by_band(image: np.ndarray,
                      mask: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    image = validate_cube(image, allow_nonfinite=mask is not None)
    if mask is None:
        minimum = image.min(axis=(0, 1), keepdims=True)
        maximum = image.max(axis=(0, 1), keepdims=True)
        working = image
    else:
        valid = np.asarray(mask) > 0
        if valid.shape != image.shape:
            raise AlgorithmError("mask 形状与影像不一致")
        if np.any(valid & ~np.isfinite(image)):
            raise AlgorithmError("mask 把 NaN/Inf 标记成了可用值")
        minima, maxima = [], []
        for offset in range(image.shape[2]):
            values = image[:, :, offset][valid[:, :, offset]]
            if not values.size:
                raise AlgorithmError("mask 导致第 %d 个组内波段没有可用值" % (offset + 1))
            minima.append(float(values.min()))
            maxima.append(float(values.max()))
        minimum = np.asarray(minima, dtype=np.float32).reshape(1, 1, -1)
        maximum = np.asarray(maxima, dtype=np.float32).reshape(1, 1, -1)
        working = np.where(valid, image, minimum)
    scale = maximum - minimum
    constant = np.flatnonzero(scale.reshape(-1) == 0)
    if constant.size:
        raise AlgorithmError("算法输入仍含常量波段（组内索引）：%s"
                             % ", ".join(str(int(i + 1)) for i in constant))
    normalized = (working - minimum) / scale
    return normalized.astype(np.float32, copy=False), minimum, scale


def undo_normalize(image: np.ndarray, minimum: np.ndarray, scale: np.ndarray) -> np.ndarray:
    restored = np.asarray(image, dtype=np.float32) * scale + minimum
    if not np.isfinite(restored).all():
        raise AlgorithmError("算法输出反归一化后含 NaN/Inf")
    return restored.astype(np.float32, copy=False)


def primary_output(output: Any) -> Any:
    return output[0] if isinstance(output, (tuple, list)) else output


def select_algorithm_output(output: Any, spec: AlgorithmSpec) -> tuple[Any, dict[str, Any]]:
    """按算法合同选取主去噪结果，并记录被舍弃输出的类型和形状。"""
    values = tuple(output) if isinstance(output, (tuple, list)) else (output,)
    if not values:
        raise AlgorithmError("%s 返回了空输出" % spec.name)
    if spec.tuple_output and len(values) != len(spec.output_names):
        raise AlgorithmError("%s 应返回 %d 项，实际 %d 项"
                             % (spec.name, len(spec.output_names), len(values)))
    if spec.primary_output_index >= len(values):
        raise AlgorithmError("%s 主输出索引越界" % spec.name)
    names = (spec.output_names if len(spec.output_names) == len(values)
             else tuple("output_%d" % index for index in range(len(values))))
    discarded = []
    for index, value in enumerate(values):
        if index == spec.primary_output_index:
            continue
        shape = list(value.shape) if hasattr(value, "shape") else None
        dtype = str(value.dtype) if hasattr(value, "dtype") else None
        discarded.append({"name": names[index], "type": type(value).__name__,
                          "shape": shape, "dtype": dtype})
    return values[spec.primary_output_index], {
        "primary_output": names[spec.primary_output_index],
        "returned_output_count": len(values),
        "discarded_outputs": discarded,
    }


def _torch_and_device(requested: str, neural: bool):
    try:
        import torch
    except ImportError as exc:
        raise AlgorithmError("隔离 Worker 环境缺少 PyTorch") from exc
    if requested == "auto":
        device = "mps" if neural and torch.backends.mps.is_available() else "cpu"
    else:
        device = requested
    if device == "mps" and not torch.backends.mps.is_available():
        raise AlgorithmError("请求了 MPS，但当前 PyTorch/Mac 不可用")
    if device == "mps" and not neural:
        raise AlgorithmError("传统 HyDe/BM3D 首版仅允许 CPU，避免算子与数值不一致")
    return torch, device


#: 上游非 iid 反变换的括号打错了，这里在外面按 MATLAB 原式重做一遍。
NONIID_WORKAROUND = "noniid_retransform_fixed_in_wrapper"

#: 出问题的算法与版本，记进清单，方便日后上游修好了把这段删掉。
NONIID_BUG = {
    "affects": ["FastHyDe", "FastHyIe"],
    "condition": "noise_type=additive 且 iid=False",
    "upstream": "hyde-images 0.4.3 @ 251a4f461cd8678462cfda09009c183da1b5a092",
    "where": "spatial_domain/fast_hyde.py:140、spatial_domain/fast_hyin.py:198",
    "upstream_code": "y_reconst = torch.sqrt(r_w_og @ y_reconst)",
    "matlab_original": "Y_reconst = sqrt(Rw_ori) * Y_reconst",
    "symptom": "对乘积逐元素开方，负数项变 NaN；即便不出 NaN，反变换本身也是错的",
}


def _noniid_whiten(name: str, tensor, mask_tensor, torch):
    """把上游非 iid 分支的白化搬到包装层来做。

    上游那一段是「白化 → 走 iid 流程 → 反白化」，白化写对了，反白化的括号打错：

        上游   y_reconst = torch.sqrt(r_w_og @ y_reconst)     # 对乘积开方
        原式   Y_reconst = sqrt(Rw_ori) * Y_reconst           # 对矩阵开方再乘

    在真实 ZY1E VN 数据 128×128×24 的 ROI 上实测：``r_w_og`` 严格对角，乘积里
    有 14 个负数，逐元素开方后就是 14 个 NaN；把这些 NaN 剔掉再比，上游写法与
    白化前输入的 RMSE 是 0.267，原式是 0.004——所以这不只是"偶尔冒 NaN"，是整个
    反变换都不对。

    修的办法是不碰上游代码：白化和反白化都由这里做（白化沿用上游同一段逻辑与
    同一个 ``utils.estimate_hyperspectral_noise``），中间以 ``iid=True`` 调上游
    ——那条分支跳过白化、直接跑主流程，与上游非 iid 分支想做的事逐行等价。算法
    本身一行没动。
    """
    from hyde.lowlevel import utils

    rows, cols, bands = tensor.shape
    pixels = rows * cols
    flat = torch.reshape(tensor, (pixels, bands)).T
    if name == "FastHyIe" and mask_tensor is not None:
        # 上游只在有效像元上估噪声（fast_hyin.py:117），这里照办
        viable = torch.ones((rows, cols), dtype=torch.bool, device=tensor.device)
        viable[torch.nonzero(mask_tensor.sum(dim=-1) < bands, as_tuple=True)] = 0
        sample = flat[:, torch.nonzero(viable.flatten(), as_tuple=True)[0]]
    else:
        sample = flat
    _, r_w = utils.estimate_hyperspectral_noise(
        sample, "additive", calculation_dtype=tensor.dtype)
    whitened = torch.sqrt(torch.inverse(r_w)) @ flat
    return torch.reshape(torch.conj(whitened.T), (rows, cols, bands)), r_w


def _noniid_unwhiten(output, r_w, torch):
    """反白化，按 MATLAB 原式：先对矩阵开方，再乘。"""
    rows, cols, bands = output.shape
    flat = torch.reshape(output, (rows * cols, bands)).T
    return torch.reshape((torch.sqrt(r_w) @ flat).T, (rows, cols, bands))


def _traditional_forward(name: str, normalized: np.ndarray, params: dict[str, Any],
                         device: str, mask: np.ndarray | None = None):
    torch, resolved_device = _torch_and_device(device, neural=False)
    try:
        import hyde
    except ImportError as exc:
        raise AlgorithmError("隔离 Worker 环境缺少固定提交的 hyde-images") from exc
    try:
        algorithm_type = getattr(hyde, name)
    except AttributeError as exc:
        raise AlgorithmError("当前 HyDe 版本没有算法 %s" % name) from exc
    tensor = torch.from_numpy(np.ascontiguousarray(normalized)).to(resolved_device)
    algorithm = algorithm_type()
    kwargs = dict(params)
    if name in {"FastHyDe", "FastHyIe", "L1HyMixDe"}:
        kwargs["normalize"] = False
    if name == "FastHyIe":
        if mask is None:
            raise AlgorithmError("FastHyIe 必须提供与影像同形状的有效像元 mask")
        mask_array = np.asarray(mask)
        if mask_array.shape != normalized.shape:
            raise AlgorithmError("FastHyIe mask 形状与影像不一致")
        kwargs["mask"] = torch.from_numpy(
            np.ascontiguousarray(mask_array.astype(np.float32))).to(resolved_device)
    if name == "FORPDN_SURE":
        kwargs.setdefault("scale", True)   # 上游默认值；False 会让它退化成恒等
        for key in ("s_int_st", "s_int_step", "s_int_end"):
            if key in kwargs:
                kwargs[key] = torch.tensor(kwargs[key], dtype=tensor.dtype,
                                           device=resolved_device)

    # 非 iid 的白化/反白化由包装层接管，见 _noniid_whiten
    noise_covariance = None
    if (name in {"FastHyDe", "FastHyIe"}
            and kwargs.get("noise_type", "additive") == "additive"
            and not kwargs.get("iid", True)):
        tensor, noise_covariance = _noniid_whiten(
            name, tensor, kwargs.get("mask"), torch)
        kwargs["iid"] = True

    with torch.no_grad():
        raw_output = algorithm(tensor, **kwargs)
    output, output_info = select_algorithm_output(raw_output, ALGORITHMS[name])
    if not hasattr(output, "detach"):
        raise AlgorithmError("%s 返回了不支持的输出类型" % name)
    if noise_covariance is not None:
        output = _noniid_unwhiten(output, noise_covariance, torch)
        # 记进清单：这一景的结果没有走上游那段代码，日后要能查出来是哪一次、
        # 用的是哪种反变换
        output_info["upstream_workaround"] = NONIID_WORKAROUND
        output_info["upstream_bug"] = NONIID_BUG
    return output.detach().to("cpu").numpy(), resolved_device, None, output_info


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def load_model_entry(model_id: str, registry_path: Path) -> dict[str, Any]:
    try:
        registry = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AlgorithmError("无法读取固定模型清单：%s" % registry_path) from exc
    models = registry.get("models", {})
    if model_id not in models:
        raise AlgorithmError("模型 %r 未登记，可用：%s"
                             % (model_id, ", ".join(sorted(models)) or "无"))
    entry = dict(models[model_id])
    required = {"architecture", "filename", "sha256", "training_noise"}
    if set(entry) != required:
        raise AlgorithmError("模型清单条目 %s 字段不完整或含未知字段" % model_id)
    path = Path(registry_path).parent / "models" / entry["filename"]
    if not path.is_file():
        raise AlgorithmError("已登记模型文件不存在：%s" % path)
    actual_hash = sha256_file(path)
    if actual_hash.lower() != str(entry["sha256"]).lower():
        raise AlgorithmError("模型 %s SHA-256 不匹配，拒绝加载" % model_id)
    entry.update({"model_id": model_id, "path": path, "actual_sha256": actual_hash})
    return entry


def _load_safe_network(entry: dict[str, Any], device: str):
    torch, resolved_device = _torch_and_device(device, neural=True)
    try:
        from hyde.nn import models
    except ImportError as exc:
        raise AlgorithmError("隔离 Worker 环境缺少 HyDe 神经网络定义") from exc
    architecture = entry["architecture"]
    factory = getattr(models, architecture, None)
    if factory is None:
        raise AlgorithmError("HyDe 没有已登记架构 %s" % architecture)
    network = factory()
    try:
        checkpoint = torch.load(entry["path"], map_location="cpu", weights_only=True)
    except TypeError as exc:
        raise AlgorithmError("当前 PyTorch 不支持安全 weights_only 加载，拒绝降级") from exc
    state = checkpoint.get("net") if isinstance(checkpoint, dict) else None
    if not isinstance(state, dict):
        raise AlgorithmError("模型检查点缺少 net state_dict")
    if state and all(str(key).startswith("module.") for key in state):
        state = {str(key)[7:]: value for key, value in state.items()}
    network.load_state_dict(state, strict=True)
    if "qrnn" in architecture:
        network.clamp = False
    network.eval().to(resolved_device)
    return torch, network, resolved_device


def _blend_vector(length: int, overlap: int, at_start: bool, at_end: bool) -> np.ndarray:
    weights = np.ones(length, dtype=np.float32)
    fade = min(overlap, length // 2)
    if fade > 0:
        ramp = np.linspace(0.05, 1.0, fade, dtype=np.float32)
        if not at_start:
            weights[:fade] = ramp
        if not at_end:
            weights[-fade:] = ramp[::-1]
    return weights


def _window_starts(length: int, window: int, overlap: int) -> list[int]:
    if window >= length:
        return [0]
    stride = max(1, window - overlap)
    starts = list(range(0, max(1, length - window + 1), stride))
    last = length - window
    if starts[-1] != last:
        starts.append(last)
    return starts


def _qrnn_weighted_inference(normalized: np.ndarray, entry: dict[str, Any],
                             device: str, tile_size: int, halo: int,
                             band_window: int = 10) -> tuple[np.ndarray, str]:
    torch, network, resolved_device = _load_safe_network(entry, device)
    source_height, source_width, bands = normalized.shape
    pad = max(0, int(halo))
    pad_mode = "reflect" if source_height > 1 and source_width > 1 else "edge"
    working = (np.pad(normalized, ((pad, pad), (pad, pad), (0, 0)), mode=pad_mode)
               if pad else normalized)
    height, width, _ = working.shape
    spatial_window = max(16, int(tile_size) + 2 * pad)
    spatial_overlap = min(max(0, pad * 2), spatial_window - 1)
    band_window = min(max(2, int(band_window)), bands)
    band_overlap = min(2, band_window - 1)

    output = np.zeros_like(working, dtype=np.float32)
    weight_sum = np.zeros_like(working, dtype=np.float32)
    y_starts = _window_starts(height, spatial_window, spatial_overlap)
    x_starts = _window_starts(width, spatial_window, spatial_overlap)
    b_starts = _window_starts(bands, band_window, band_overlap)

    with torch.no_grad():
        for y0 in y_starts:
            y1 = min(height, y0 + spatial_window)
            wy = _blend_vector(y1 - y0, spatial_overlap,
                               y0 == 0, y1 == height)
            for x0 in x_starts:
                x1 = min(width, x0 + spatial_window)
                wx = _blend_vector(x1 - x0, spatial_overlap,
                                   x0 == 0, x1 == width)
                spatial_weight = wy[:, None] * wx[None, :]
                for b0 in b_starts:
                    b1 = min(bands, b0 + band_window)
                    wb = _blend_vector(b1 - b0, band_overlap,
                                       b0 == 0, b1 == bands)
                    patch = working[y0:y1, x0:x1, b0:b1]
                    tensor = torch.from_numpy(np.ascontiguousarray(
                        patch.transpose(2, 0, 1)[None, None]
                    )).to(resolved_device)
                    prediction = network(tensor).detach().to("cpu").numpy()
                    prediction = prediction[0, 0].transpose(1, 2, 0).astype(np.float32)
                    weights = spatial_weight[:, :, None] * wb[None, None, :]
                    output[y0:y1, x0:x1, b0:b1] += prediction * weights
                    weight_sum[y0:y1, x0:x1, b0:b1] += weights
    if np.any(weight_sum <= 0):
        raise AlgorithmError("QRNN3D 加权融合存在未覆盖像元")
    blended = output / weight_sum
    if pad:
        blended = blended[pad:pad + source_height, pad:pad + source_width, :]
    return blended, resolved_device


def run_algorithm(image: np.ndarray, name: str, params: dict[str, Any], device: str,
                  model_id: str = "",
                  model_registry: Path | None = None, tile_size: int = 256,
                  halo: int = 16,
                  backend: Callable[[np.ndarray], Any] | None = None,
                  mask: np.ndarray | None = None) -> tuple[np.ndarray, dict[str, Any]]:
    """执行统一 ``float32 HWC -> float32 HWC`` 合同。"""
    image = validate_cube(image, allow_nonfinite=mask is not None)
    spec = validate_algorithm(name, params, image.shape[2])
    if spec.requires_mask and mask is None:
        raise AlgorithmError("%s 必须提供有效像元 mask" % name)
    if mask is not None:
        mask_array = np.asarray(mask)
        if mask_array.shape != image.shape or not np.isin(mask_array, (0, 1)).all():
            raise AlgorithmError("mask 必须与影像同形且仅包含 0/1")
        mask = mask_array.astype(np.float32, copy=False)
    normalized, minimum, scale = normalize_by_band(image, mask=mask)

    model_info = None
    output_info = {"primary_output": "denoised", "returned_output_count": 1,
                   "discarded_outputs": []}
    if backend is not None:
        selected, output_info = select_algorithm_output(backend(normalized), spec)
        normalized_output = np.asarray(selected)
        resolved_device = "test-backend"
    elif spec.family == "neural":
        if not model_id:
            raise AlgorithmError("QRNN3D 必须指定已登记 model_id")
        if model_registry is None:
            raise AlgorithmError("QRNN3D 缺少模型清单路径")
        entry = load_model_entry(model_id, model_registry)
        normalized_output, resolved_device = _qrnn_weighted_inference(
            normalized, entry, device, tile_size, halo)
        model_info = {
            "model_id": model_id,
            "architecture": entry["architecture"],
            "sha256": entry["actual_sha256"],
            "training_noise": entry["training_noise"],
        }
    else:
        normalized_output, resolved_device, model_info, output_info = _traditional_forward(
            name, normalized, params, device, mask=mask)

    normalized_output = np.asarray(normalized_output, dtype=np.float32)
    if normalized_output.shape != image.shape:
        raise AlgorithmError("%s 输出形状 %s 与输入 %s 不一致"
                             % (name, normalized_output.shape, image.shape))
    if not np.isfinite(normalized_output).all():
        raise AlgorithmError("%s 输出含 NaN/Inf" % name)
    output = undo_normalize(normalized_output, minimum, scale)
    return output, {
        "algorithm": name,
        "status": spec.status,
        "family": spec.family,
        "device": resolved_device,
        "model": model_info,
        "output_selection": output_info,
    }
