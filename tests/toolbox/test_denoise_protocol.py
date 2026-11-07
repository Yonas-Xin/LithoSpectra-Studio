from __future__ import annotations

import sys
from pathlib import Path

import pytest

from toolbox.core import ToolError
from toolbox.denoise.protocol import DenoiseRequest, RasterTask, WorkerEvent
from toolbox.denoise.runner import WorkerRunner


def request(tmp_path: Path) -> DenoiseRequest:
    return DenoiseRequest(
        job_id="job-1",
        source_product=str((tmp_path / "source").resolve()),
        staging_product=str((tmp_path / "staging").resolve()),
        algorithm="FastHyDe",
        algorithm_params={"noise_type": "additive", "iid": False, "k_subspace": 3},
        device="cpu",
        rasters=(RasterTask("VN", "cube_VN.tiff", ((1, 2, 3),), (4,)),),
    )


def test_request_and_event_round_trip(tmp_path: Path) -> None:
    original = request(tmp_path)
    assert DenoiseRequest.from_json(original.to_json()) == original
    event = WorkerEvent("progress", "job-1", "ok", {"completed": 1, "total": 2})
    assert WorkerEvent.from_json(event.to_json()) == event


def test_request_round_trip_preserves_auxiliary_input_builder(tmp_path: Path) -> None:
    data = request(tmp_path).to_dict()
    data["algorithm"] = "FastHyIe"
    data["auxiliary_inputs"] = {
        "mask": {"builder": "raw_validity", "policy": "nodata",
                 "meaning": "1=viable,0=missing",
                 "writeback": "inpaint_masked"}
    }
    rebuilt = DenoiseRequest.from_dict(data)
    assert rebuilt.auxiliary_inputs["mask"]["builder"] == "raw_validity"


def test_protocol_rejects_unknown_missing_and_non_contiguous(tmp_path: Path) -> None:
    data = request(tmp_path).to_dict()
    data["surprise"] = 1
    with pytest.raises(ToolError, match="未知字段"):
        DenoiseRequest.from_dict(data)
    data = request(tmp_path).to_dict()
    del data["job_id"]
    with pytest.raises(ToolError, match="缺少字段"):
        DenoiseRequest.from_dict(data)
    with pytest.raises(ToolError, match="严格连续"):
        RasterTask("VN", "x.tif", ((1, 3),))


def _write_worker(path: Path, body: str) -> None:
    path.write_text("import json, sys, time\n" + body, encoding="utf-8")


def test_runner_progress_and_completion(tmp_path: Path) -> None:
    script = tmp_path / "worker.py"
    _write_worker(script, """
r = json.loads(sys.stdin.readline())
base = {'schema_version': 1, 'job_id': r['job_id'], 'message': '', 'data': {}}
print(json.dumps(dict(base, event='started')), flush=True)
print(json.dumps(dict(base, event='progress', data={'completed': 1})), flush=True)
print(json.dumps(dict(base, event='completed', data={'ok': True})), flush=True)
""")
    events = []
    result = WorkerRunner(Path(sys.executable), script).run(
        request(tmp_path), on_event=events.append, timeout_seconds=2)
    assert result.data == {"ok": True}
    assert [event.event for event in events] == ["started", "progress", "completed"]


def test_runner_rejects_malformed_json_and_nonzero_exit(tmp_path: Path) -> None:
    bad_json = tmp_path / "bad_json.py"
    _write_worker(bad_json, "sys.stdin.readline(); print('not-json', flush=True)\n")
    with pytest.raises(ToolError, match="合法 JSONL"):
        WorkerRunner(Path(sys.executable), bad_json).run(request(tmp_path))

    failed = tmp_path / "failed.py"
    _write_worker(failed, "sys.stdin.readline(); print('boom', file=sys.stderr); sys.exit(7)\n")
    with pytest.raises(ToolError, match="code=7"):
        WorkerRunner(Path(sys.executable), failed).run(request(tmp_path))


def test_runner_timeout_terminates_worker(tmp_path: Path) -> None:
    script = tmp_path / "slow.py"
    _write_worker(script, "sys.stdin.readline(); time.sleep(2)\n")
    with pytest.raises(ToolError, match="超时"):
        WorkerRunner(Path(sys.executable), script).run(
            request(tmp_path), timeout_seconds=0.05)


def test_runner_cancellation_terminates_worker(tmp_path: Path) -> None:
    script = tmp_path / "cancel.py"
    _write_worker(script, "sys.stdin.readline(); time.sleep(2)\n")
    with pytest.raises(ToolError, match="已取消"):
        WorkerRunner(Path(sys.executable), script).run(
            request(tmp_path), should_cancel=lambda: True)
