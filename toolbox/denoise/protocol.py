# -*- coding: utf-8 -*-
"""工具箱与隔离 HyDe Worker 之间的严格 JSONL 协议。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..core import ToolError


SCHEMA_VERSION = 1
EVENT_TYPES = {"started", "progress", "warning", "completed", "failed"}
DEVICES = {"auto", "cpu", "mps"}
CLIP_MODES = {"source_band_range", "dtype_range"}


def _strict_fields(data: Mapping[str, Any], required: set[str], optional: set[str],
                   label: str) -> None:
    unknown = set(data) - required - optional
    missing = required - set(data)
    if unknown:
        raise ToolError("%s 含未知字段：%s" % (label, ", ".join(sorted(unknown))))
    if missing:
        raise ToolError("%s 缺少字段：%s" % (label, ", ".join(sorted(missing))))


@dataclass(frozen=True)
class RasterTask:
    part: str
    relative_raster: str
    valid_groups: tuple[tuple[int, ...], ...]
    frozen_bands: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.part not in {"VN", "SW"}:
            raise ToolError("未知光谱分段：%s" % self.part)
        path = Path(self.relative_raster)
        if path == Path(".") or path.is_absolute() or ".." in path.parts:
            raise ToolError("Worker 栅格路径必须是产品内安全相对路径")
        flattened = [band for group in self.valid_groups for band in group]
        if not flattened or any(int(band) <= 0 for band in flattened):
            raise ToolError("valid_groups 必须含 1 起算的正整数波段")
        if len(flattened) != len(set(flattened)):
            raise ToolError("valid_groups 中存在重复波段")
        if flattened != sorted(flattened):
            raise ToolError("valid_groups 必须按波段索引递增排列")
        for group in self.valid_groups:
            if not group or any(right != left + 1 for left, right in zip(group, group[1:])):
                raise ToolError("每个有效波段组必须严格连续")
        if set(flattened) & set(self.frozen_bands):
            raise ToolError("有效波段与冻结波段不能重叠")
        if any(band <= 0 for band in self.frozen_bands) or \
                len(self.frozen_bands) != len(set(self.frozen_bands)):
            raise ToolError("frozen_bands 必须是互不重复的正整数")

    def to_dict(self) -> dict[str, Any]:
        return {
            "part": self.part,
            "relative_raster": self.relative_raster,
            "valid_groups": [list(group) for group in self.valid_groups],
            "frozen_bands": list(self.frozen_bands),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RasterTask":
        required = {"part", "relative_raster", "valid_groups"}
        optional = {"frozen_bands"}
        _strict_fields(data, required, optional, "RasterTask")
        try:
            return cls(
                part=str(data["part"]),
                relative_raster=str(data["relative_raster"]),
                valid_groups=tuple(tuple(int(band) for band in group)
                                   for group in data["valid_groups"]),
                frozen_bands=tuple(int(band) for band in data.get("frozen_bands", ())),
            )
        except (TypeError, ValueError) as exc:
            raise ToolError("RasterTask 字段类型不合法：%s" % exc) from exc


@dataclass(frozen=True)
class DenoiseRequest:
    job_id: str
    source_product: str
    staging_product: str
    algorithm: str
    algorithm_params: dict[str, Any]
    device: str
    rasters: tuple[RasterTask, ...]
    auxiliary_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    tile_size: int = 256
    halo: int = 16
    clip_mode: str = "source_band_range"
    model_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ToolError("不支持的 Worker 协议版本：%s" % self.schema_version)
        if not self.job_id.strip():
            raise ToolError("job_id 不能为空")
        source = Path(self.source_product)
        staging = Path(self.staging_product)
        if not source.is_absolute() or not staging.is_absolute():
            raise ToolError("source_product 与 staging_product 必须是绝对路径")
        if source == staging or source in staging.parents:
            raise ToolError("staging_product 不得等于或位于 source_product 内部")
        if self.device not in DEVICES:
            raise ToolError("device 可选：%s" % ", ".join(sorted(DEVICES)))
        if self.clip_mode not in CLIP_MODES:
            raise ToolError("clip_mode 可选：%s" % ", ".join(sorted(CLIP_MODES)))
        if self.tile_size <= 0 or self.halo < 0 or self.halo * 2 >= self.tile_size:
            raise ToolError("tile_size 必须为正，且 0 <= 2*halo < tile_size")
        if not self.rasters:
            raise ToolError("请求至少需要一个 RasterTask")
        if len({task.part for task in self.rasters}) != len(self.rasters):
            raise ToolError("每个光谱分段只能出现一次")
        if not isinstance(self.algorithm_params, dict):
            raise ToolError("algorithm_params 必须是对象")
        if not isinstance(self.auxiliary_inputs, dict) or any(
                not isinstance(name, str) or not isinstance(config, dict)
                for name, config in self.auxiliary_inputs.items()):
            raise ToolError("auxiliary_inputs 必须是名称到配置对象的映射")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "source_product": self.source_product,
            "staging_product": self.staging_product,
            "algorithm": self.algorithm,
            "algorithm_params": self.algorithm_params,
            "device": self.device,
            "rasters": [task.to_dict() for task in self.rasters],
            "auxiliary_inputs": self.auxiliary_inputs,
            "tile_size": self.tile_size,
            "halo": self.halo,
            "clip_mode": self.clip_mode,
            "model_id": self.model_id,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DenoiseRequest":
        required = {
            "schema_version", "job_id", "source_product", "staging_product",
            "algorithm", "algorithm_params", "device", "rasters", "tile_size",
            "halo", "clip_mode", "model_id",
        }
        _strict_fields(data, required, {"auxiliary_inputs"}, "DenoiseRequest")
        try:
            return cls(
                schema_version=int(data["schema_version"]),
                job_id=str(data["job_id"]),
                source_product=str(data["source_product"]),
                staging_product=str(data["staging_product"]),
                algorithm=str(data["algorithm"]),
                algorithm_params=dict(data["algorithm_params"]),
                device=str(data["device"]),
                rasters=tuple(RasterTask.from_dict(item) for item in data["rasters"]),
                auxiliary_inputs={str(name): dict(config) for name, config in
                                  dict(data.get("auxiliary_inputs", {})).items()},
                tile_size=int(data["tile_size"]),
                halo=int(data["halo"]),
                clip_mode=str(data["clip_mode"]),
                model_id=str(data["model_id"]),
            )
        except (TypeError, ValueError) as exc:
            raise ToolError("DenoiseRequest 字段类型不合法：%s" % exc) from exc

    @classmethod
    def from_json(cls, line: str) -> "DenoiseRequest":
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ToolError("Worker 请求不是合法 JSON：%s" % exc) from exc
        if not isinstance(data, dict):
            raise ToolError("Worker 请求顶层必须是 JSON 对象")
        return cls.from_dict(data)


@dataclass(frozen=True)
class WorkerEvent:
    event: str
    job_id: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ToolError("不支持的 Worker 事件协议版本：%s" % self.schema_version)
        if self.event not in EVENT_TYPES:
            raise ToolError("未知 Worker 事件：%s" % self.event)
        if not self.job_id:
            raise ToolError("Worker 事件缺少 job_id")
        if not isinstance(self.data, dict):
            raise ToolError("Worker 事件 data 必须是对象")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> "WorkerEvent":
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ToolError("Worker 输出不是合法 JSONL：%r" % line.rstrip()) from exc
        if not isinstance(data, dict):
            raise ToolError("Worker 事件顶层必须是 JSON 对象")
        required = {"schema_version", "event", "job_id", "message", "data"}
        _strict_fields(data, required, set(), "WorkerEvent")
        return cls(schema_version=int(data["schema_version"]),
                   event=str(data["event"]), job_id=str(data["job_id"]),
                   message=str(data["message"]), data=dict(data["data"]))
