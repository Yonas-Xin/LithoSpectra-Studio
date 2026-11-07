from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from toolbox.core import ToolError
from toolbox.denoise.adapters import (DenoiseSensorRegistry, SENSOR_REGISTRY,
                                      ZYAHSIL1AAdapter)
from tests.fixtures.zy1e_product_factory import create_zy1e_product


def test_default_registry_detects_resource_ahsi(tmp_path: Path) -> None:
    root = create_zy1e_product(tmp_path)
    adapter = SENSOR_REGISTRY.detect(root)
    assert isinstance(adapter, ZYAHSIL1AAdapter)
    assert SENSOR_REGISTRY.open(root).adapter_id == adapter.id


@dataclass
class FixtureGF5Adapter:
    """证明未来 GF5 只需注册 Adapter，不需要修改去噪编排。"""

    id: str = "gf5_fixture"

    def detect(self, root: Path) -> bool:
        return (Path(root) / "GF5.marker").is_file()

    def open(self, root: Path):
        return SimpleNamespace(root=Path(root).resolve(), adapter_id=self.id, parts=())

    def discover(self, root: Path):
        candidates = [Path(root)] + [path for path in Path(root).iterdir()
                                          if path.is_dir()]
        return [self.open(path) for path in candidates if self.detect(path)]

    def verify_clone(self, source, clone_root: Path, before_snapshots: dict):
        return {"fixture": True}


def test_new_sensor_adapter_plugs_into_registry_without_core_changes(tmp_path: Path) -> None:
    product = tmp_path / "GF5_AHSI_PRODUCT"
    product.mkdir()
    (product / "GF5.marker").write_text("fixture", encoding="utf-8")
    registry = DenoiseSensorRegistry()
    registry.register(FixtureGF5Adapter())
    assert registry.ids == ("gf5_fixture",)
    assert registry.discover(tmp_path)[0].adapter_id == "gf5_fixture"


def test_registry_rejects_detection_conflicts(tmp_path: Path) -> None:
    product = tmp_path / "GF5_AHSI_PRODUCT"
    product.mkdir()
    (product / "GF5.marker").write_text("fixture", encoding="utf-8")
    registry = DenoiseSensorRegistry()
    registry.register(FixtureGF5Adapter("first"))
    registry.register(FixtureGF5Adapter("second"))
    with pytest.raises(ToolError, match="多个 Adapter"):
        registry.detect(product)
