# -*- coding: utf-8 -*-
"""移植的骨干与源项目在同一组权重下输出逐元素一致。

源项目只读：导入前关掉字节码缓存，不往它的目录里写 __pycache__。源项目不在
本机时跳过。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy  # noqa: F401  先于 torch
import pytest
import torch

import litho_dl  # noqa: F401
from litho_dl.backbones import ALIASES, BACKBONES
from litho_dl.models import Classifier, adapt

SOURCE = Path.home() / ("Projects/"
                        "Hyperspectral_Deep-Learning_Private")
IN_SHAPE = (30, 9, 9)


@pytest.fixture(scope="module")
def source_registry():
    if not (SOURCE / "classifier_model" / "Models" / "Models.py").is_file():
        pytest.skip("源项目不在本机")
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SOURCE))
    try:
        from classifier_model.Models.Models import MODEL_REGISTRY
    except Exception as exc:          # 源项目缺依赖（如 kornia）时跳过
        pytest.skip("源项目导入失败：%s" % exc)
    finally:
        sys.dont_write_bytecode = previous
    return MODEL_REGISTRY


@pytest.mark.parametrize("name", sorted(BACKBONES))
def test_output_matches_source(source_registry, name):
    torch.manual_seed(0)
    source_name = {new: old for old, new in ALIASES.items()}.get(name, name)   # 源项目里还是旧名
    original = source_registry[source_name](out_classes=5, in_shape=IN_SHAPE).eval()
    ported = Classifier(name, IN_SHAPE, 5).eval()
    ported.load_state_dict(original.state_dict(), strict=True)
    x = torch.randn(6, *IN_SHAPE)
    with torch.no_grad():
        expected = original(x)
        actual = ported(adapt(x, ported.input_kind))
    torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-5)


def test_patch_too_small_is_reported():
    with pytest.raises(ValueError, match="至少"):
        Classifier("HybridSN", (30, 5, 5), 3)


def test_plugin_backbone_is_discovered(tmp_path):
    from litho_dl.backbones import BACKBONES, load_plugins
    (tmp_path / "tiny.py").write_text(
        "import torch.nn as nn\n"
        "from litho_dl.backbones import register_backbone\n"
        "@register_backbone('TinyPlugin', input_kind='spectrum', speed='fast', default_head='linear')\n"
        "class Tiny(nn.Module):\n"
        "    def __init__(self, in_shape):\n"
        "        super().__init__()\n"
        "        self.fc = nn.Linear(in_shape[0], 8)\n"
        "    def forward(self, x):\n"
        "        return self.fc(x.flatten(1))\n", "utf-8")
    (tmp_path / "broken.py").write_text("raise RuntimeError('坏插件')\n", "utf-8")
    (tmp_path / "clash.py").write_text(
        "import torch.nn as nn\n"
        "from litho_dl.backbones import register_backbone\n"
        "@register_backbone('1D-CNN', input_kind='spectrum')\n"
        "class Fake(nn.Module):\n"
        "    def __init__(self, in_shape):\n"
        "        super().__init__()\n", "utf-8")
    builtin = BACKBONES["1D-CNN"]
    errors = load_plugins(tmp_path)
    try:
        assert "TinyPlugin" in BACKBONES and BACKBONES["TinyPlugin"].source == "plugin:tiny.py"
        assert len(errors) == 2 and "broken.py" in errors[0] and "重名" in errors[1]
        assert BACKBONES["1D-CNN"] is builtin
        model = Classifier("TinyPlugin", (12, 1, 1), 3)
        assert model(torch.randn(4, 1, 12)).shape == (4, 3)
    finally:
        BACKBONES.pop("TinyPlugin", None)


def test_renamed_backbones_still_load():
    """改名前训练的模型包清单里记的是旧名，照样建得出同一个骨干，参数里写旧名也认。"""
    from litho_dl.backbones import get_spec
    from litho_dl.train import params
    assert get_spec("Common_2DCNN").name == "2D-CNN"
    model = Classifier("Common_1DCNN", (12, 1, 1), 3)
    assert model(torch.randn(2, 1, 12)).shape == (2, 3)
    backbone = next(p for p in params() if p.name == "backbone")
    assert backbone.coerce("Common_3DCNN") == "3D-CNN"
