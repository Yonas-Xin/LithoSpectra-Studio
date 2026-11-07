# -*- coding: utf-8 -*-
"""统一批处理入口。

整个工具箱只有这一处循环、只有这一处判断"产物已存在就跳过"、只有这一处兜住
单件失败继续下一件。工具自己不做这些——所以换一件工具跑批量，行为完全一致：
一样的进度输出、一样的跳过规则、一样的失败隔离、一样的汇总。

    from toolbox import run_batch
    report = run_batch("remove_water_vapor", "/data/in", "/data/out",
                       params={"sensor": "ZY1F"})
    print(report.ok)
"""

from __future__ import annotations

import time
import traceback
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from . import core
from .core import Logger, Tool, ToolError, WorkItem

STATUS_COMPLETED = "completed"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


@dataclass
class ItemResult:
    key: str
    source: Path
    output: Path
    status: str
    message: str = ""
    seconds: float = 0.0


@dataclass
class BatchReport:
    tool_id: str
    results: list[ItemResult] = field(default_factory=list)

    def _of(self, status: str) -> list[ItemResult]:
        return [item for item in self.results if item.status == status]

    @property
    def completed(self) -> list[ItemResult]:
        return self._of(STATUS_COMPLETED)

    @property
    def skipped(self) -> list[ItemResult]:
        return self._of(STATUS_SKIPPED)

    @property
    def failed(self) -> list[ItemResult]:
        return self._of(STATUS_FAILED)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        return ("完成 %d，跳过 %d，失败 %d，共 %d"
                % (len(self.completed), len(self.skipped),
                   len(self.failed), len(self.results)))


def _as_tool(tool: Tool | str) -> Tool:
    return core.get(tool) if isinstance(tool, str) else tool


def _as_roots(inputs) -> list[Path]:
    if isinstance(inputs, (str, Path)):
        inputs = [inputs]
    roots = [Path(item).expanduser() for item in inputs]
    if not roots:
        raise ToolError("没有给任何输入路径")
    return roots


def _existing(paths: Sequence[Path]) -> list[Path]:
    return [path for path in paths if path.exists()]


def run_batch(tool: Tool | str,
              inputs,
              output_dir,
              params: dict | None = None,
              overwrite: bool = False,
              stop_on_error: bool = False,
              dry_run: bool = False,
              should_stop: Callable[[], bool] | None = None,
              progress: Callable[[int, int, str], None] | None = None,
              log: Logger = print) -> BatchReport:
    """对 inputs 下发现的每一件活跑一遍 tool。

    :param tool: 工具 id 或工具实例
    :param inputs: 一个路径，或一组路径。文件还是目录由工具自己解释
    :param output_dir: 产物目录，不存在会建
    :param params: 工具参数，缺的取默认值；传了不认识的键会直接报错
    :param overwrite: 产物已存在时是覆盖还是跳过
    :param stop_on_error: 单件失败后是停下还是继续
    :param dry_run: 只列出要做什么，不真跑
    :param should_stop: 返回 True 就在下一件之前停手。只在两件之间检查——
        单件内部的取消要靠工具自己支持，这里不假装能中断 GDAL 的写盘
    :param progress: 每件开工前调一次 ``(第几件, 共几件, 名字)``，给界面画进度条
    """
    tool = _as_tool(tool)
    roots = _as_roots(inputs)
    output_dir = Path(output_dir).expanduser()
    resolved = tool.resolve_params(params)
    report = BatchReport(tool_id=tool.id)

    log("工具：%s（%s）" % (tool.name, tool.id))
    log("输入：%s" % "、".join(str(root) for root in roots))
    log("输出：%s" % output_dir)
    if dry_run:
        log("试运行：只列出计划，不写任何文件")

    # -- 发现：这里出错就整批中止，属于开跑前该拦住的问题 -------------------
    items: list[WorkItem] = []
    seen: set[str] = set()
    for root in roots:
        for item in tool.discover(root, resolved, log):
            token = str(item.source.resolve())
            if token in seen:
                continue        # 输入目录相互嵌套时同一件活会被发现两次
            seen.add(token)
            items.append(item)

    if not items:
        log("没有发现可处理的对象。")
        return report

    # 两件活撞同一个产物名：自动补编号，但要把撞上的两个来源都说出来。
    # 撞名多半意味着同一景被解压了两份，或一个产品里有多个同瓦片同时相的
    # granule——补个编号让它跑完比直接罢工有用，但人得知道这事发生过。
    planned: dict[Path, WorkItem] = {}
    for item in items:
        out_path = tool.output_path(item, output_dir, resolved)
        if out_path in planned:
            clash = planned[out_path]
            out_path = _numbered(out_path, planned)
            log("[提示] %s 与 %s 的产物同名，后者改用 %s"
                % (clash.key, item.key, out_path.name))
            log("        前者来自 %s" % clash.source)
            log("        后者来自 %s" % item.source)
        tool.validate_output(item, out_path)
        planned[out_path] = item

    log("发现 %d 件。" % len(items))
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    # -- 逐件处理 -----------------------------------------------------------
    for index, (out_path, item) in enumerate(planned.items(), start=1):
        if should_stop is not None and should_stop():
            log("\n已取消，剩余 %d 件未处理。" % (len(planned) - index + 1))
            break
        if progress is not None:
            progress(index, len(planned), item.key)
        log("\n=== [%d/%d] %s ===" % (index, len(planned), item.key))
        targets = [out_path] + tool.companions(out_path)
        transients = tool.transients(out_path)
        present = _existing(targets)
        complete = len(present) == len(targets)

        if complete and not overwrite:
            log("[跳过] 产物已存在：%s" % present[0])
            report.results.append(ItemResult(item.key, item.source, out_path,
                                             STATUS_SKIPPED, "产物已存在"))
            continue

        if dry_run:
            log("[计划] 将写出：%s" % out_path)
            report.results.append(ItemResult(item.key, item.source, out_path,
                                             STATUS_SKIPPED, "试运行"))
            continue

        if present:
            if not complete:
                log("[清理] 发现不完整产物，将重新处理：%s"
                    % "、".join(str(path) for path in present))
            _remove(present, log)
        _remove(_existing(transients), log)

        started = time.monotonic()
        try:
            tool.process_with_control(item, out_path, resolved, log,
                                      should_stop=should_stop)
        except Exception as exc:                      # noqa: BLE001 单件失败要隔离
            _remove(_existing(targets), log)          # 清掉半成品，免得下次被当成已完成
            _remove(_existing(transients), log)
            detail = str(exc) if isinstance(exc, ToolError) else traceback.format_exc()
            log("[失败] %s" % detail.rstrip())
            report.results.append(ItemResult(item.key, item.source, out_path,
                                             STATUS_FAILED, str(exc),
                                             time.monotonic() - started))
            if stop_on_error:
                log("\n遇错即停，剩余 %d 件未处理。" % (len(planned) - index))
                break
            continue

        elapsed = time.monotonic() - started
        log("[完成] %s（%.1f 秒）" % (out_path, elapsed))
        report.results.append(ItemResult(item.key, item.source, out_path,
                                         STATUS_COMPLETED, "", elapsed))

    log("\n%s" % report.summary())
    for failure in report.failed:
        log("  失败：%s —— %s" % (failure.key, failure.message))
    return report


def _numbered(out_path: Path, taken: dict) -> Path:
    """给撞名的产物补一个 ``_2`` / ``_3`` 后缀。

    编号按发现顺序排定，而发现顺序本身是稳定的（各工具都按排序遍历），所以同一
    批输入重跑两次得到的分配一致——否则"产物已存在就跳过"会时灵时不灵。
    """
    index = 2
    while True:
        candidate = out_path.with_name("%s_%d%s" % (out_path.stem, index,
                                                    out_path.suffix))
        if candidate not in taken:
            return candidate
        index += 1


def _remove(paths: Sequence[Path], log: Logger) -> None:
    for path in paths:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            log("[警告] 删不掉 %s：%s" % (path, exc))


def describe_tools(log: Logger = print) -> None:
    """把已注册的工具和它们的参数打出来。"""
    for tool in core.all_tools():
        log("\n%s  %s" % (tool.id, tool.name))
        log("  %s" % tool.summary)
        log("  输入粒度：%s" % ("单个栅格文件" if tool.input_kind == "raster"
                                else "一整个产品目录"))
        log("  主产物：%s" % ("目录" if tool.output_kind == "directory" else "文件"))
        for param in tool.params:
            line = "    %-26s %s = %r" % (param.name, param.kind, param.default)
            if param.choices:
                line += "   可选：%s" % ", ".join(param.choices)
            log(line)
            if param.help:
                log("        %s" % param.help)
