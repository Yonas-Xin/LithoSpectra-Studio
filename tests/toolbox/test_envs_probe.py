"""探测一个环境时，不能把调用方环境的成色算到它头上。"""

from __future__ import annotations

import os
import sys

from toolbox import envs


def test_probe_strips_variables_that_pin_another_environment(monkeypatch):
    # conda 激活 geo 会设 GDAL_DRIVER_PATH=<geo>/lib/gdalplugins。带着它去启动
    # samplelib 的解释器，samplelib 就会到 geo 的插件目录里找驱动，于是 JPEG2000
    # 这种以插件安装的驱动会报成调用方的状态——界面跑在哪个环境，所有环境就都被
    # 报成哪个环境的成色。
    monkeypatch.setenv("GDAL_DRIVER_PATH", "/somewhere/else/lib/gdalplugins")
    monkeypatch.setenv("GDAL_DATA", "/somewhere/else/share/gdal")
    monkeypatch.setenv("PROJ_DATA", "/somewhere/else/share/proj")
    monkeypatch.setenv("CONDA_PREFIX", "/somewhere/else")
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else/lib")
    monkeypatch.setenv("KEEP_ME", "yes")

    cleaned = envs.clean_environment()

    for leaked in ("GDAL_DRIVER_PATH", "GDAL_DATA", "PROJ_DATA", "CONDA_PREFIX", "PYTHONPATH"):
        assert leaked not in cleaned, leaked
    assert cleaned["KEEP_ME"] == "yes"


def test_probe_runs_the_target_interpreter_with_the_cleaned_environment(monkeypatch):
    captured = {}

    class _Done:
        stdout = 'PROBE{"ok": true, "missing": [], "jp2": true}'
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _Done()

    monkeypatch.setenv("GDAL_DRIVER_PATH", "/caller/lib/gdalplugins")
    monkeypatch.setattr(envs.subprocess, "run", fake_run)

    info = envs.probe(sys.executable)

    assert info["jp2"] is True
    assert captured["argv"][0] == sys.executable
    assert captured["env"] is not None
    assert "GDAL_DRIVER_PATH" not in captured["env"]
    # 调用方自己的环境不能被改动
    assert os.environ["GDAL_DRIVER_PATH"] == "/caller/lib/gdalplugins"
