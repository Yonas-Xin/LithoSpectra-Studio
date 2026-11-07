from __future__ import annotations

from pathlib import Path

from toolbox import Tool, WorkItem, run_batch


class DirectoryTool(Tool):
    id = "test_directory"
    name = "测试目录产物"
    summary = ""
    input_kind = "product_dir"
    output_kind = "directory"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def discover(self, root, params, log):
        return [WorkItem(Path(root).name, Path(root))]

    def output_path(self, item, out_dir, params):
        return Path(out_dir) / (item.key + "_out")

    def transients(self, out_path):
        return [out_path.with_name(out_path.name + ".partial")]

    def process(self, item, out_path, params, log):
        partial = self.transients(out_path)[0]
        partial.mkdir(parents=True)
        (partial / "value.txt").write_text("new", encoding="utf-8")
        if self.fail:
            raise RuntimeError("intentional")
        partial.rename(out_path)


def test_existing_directory_skips_without_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    output_dir = tmp_path / "output"
    existing = output_dir / "source_out"
    existing.mkdir(parents=True)
    (existing / "value.txt").write_text("old", encoding="utf-8")
    report = run_batch(DirectoryTool(), source, output_dir, log=lambda _: None)
    assert len(report.skipped) == 1
    assert (existing / "value.txt").read_text(encoding="utf-8") == "old"


def test_overwrite_replaces_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    output_dir = tmp_path / "output"
    existing = output_dir / "source_out"
    existing.mkdir(parents=True)
    (existing / "old.txt").write_text("old", encoding="utf-8")
    report = run_batch(DirectoryTool(), source, output_dir, overwrite=True,
                       log=lambda _: None)
    assert len(report.completed) == 1
    assert not (existing / "old.txt").exists()
    assert (existing / "value.txt").read_text(encoding="utf-8") == "new"


def test_failure_cleans_transient_and_never_touches_input(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    marker = source / "original.txt"
    marker.write_text("untouched", encoding="utf-8")
    output_dir = tmp_path / "output"
    report = run_batch(DirectoryTool(fail=True), source, output_dir,
                       log=lambda _: None)
    assert len(report.failed) == 1
    assert marker.read_text(encoding="utf-8") == "untouched"
    assert not (output_dir / "source_out").exists()
    assert not (output_dir / "source_out.partial").exists()
