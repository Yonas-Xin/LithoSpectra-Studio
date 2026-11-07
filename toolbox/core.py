# -*- coding: utf-8 -*-
"""工具箱内核：参数声明、工作项、工具协议与注册表。

一件工具只回答三个问题：**给定一个根路径，有哪些活要干**（``discover``）、
**某件活的产物叫什么**（``output_path``）、**这一件活怎么干**（``process``）。
它不循环、不跳过已存在的产物、不吞异常、不打印进度——这些统统归
``toolbox.batch`` 管。工具里出现 for 循环遍历输入目录，就是写错了地方。

这样拆分是为了兼容性：去水汽波段的输入是单个栅格文件，Sentinel-2 组合的输入
是一整个 granule 目录，两者的"一件活"根本不是一回事。把发现工作项的职责留在
工具里，批处理入口就不必知道任何一件工具的输入长什么样。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# 日志回调：批处理传进来，工具只管往里写
Logger = Callable[[str], None]


class ToolError(RuntimeError):
    """可预期的失败：输入不合规、缺少 GDAL 驱动、波长信息缺失等。

    批处理会记下这一件、继续下一件。工具内部的意外异常不必包成 ToolError，
    批处理同样会兜住，只是报告里会带上完整调用栈。
    """


# =============================================================================
# 参数声明
# =============================================================================

PARAM_KINDS = ("str", "int", "float", "bool", "choice", "path", "strlist", "ranges")


@dataclass(frozen=True)
class Param:
    """一个可配置参数。

    声明式写法让两边都不必逐工具写代码：批处理靠它填默认值与校验类型，
    ``samplelib.ui`` 靠它生成参数面板。
    """

    name: str
    label: str
    default: Any
    kind: str = "str"
    choices: tuple[str, ...] = ()
    #: 选项的显示名，``(取值, 显示名)`` 成对给出；没给的选项直接显示取值本身。
    #: 存的仍然是取值，显示的是名字——这样"HyRes 是实验算法"这件事写在选它的
    #: 地方，而不是另开一个复选框让人再确认一遍。
    choice_labels: tuple[tuple[str, str], ...] = ()
    help: str = ""
    path_mode: str = "directory"
    #: 何时才显示这个参数。一组 ``(控制参数名, 允许取值)`` 条件，全部成立才显示；
    #: 空表示一直显示。用 :func:`when` 来写更省事。
    visible_when: tuple = ()

    def __post_init__(self) -> None:
        if self.kind not in PARAM_KINDS:
            raise ValueError("未知的参数类型 %r，可选：%s"
                             % (self.kind, ", ".join(PARAM_KINDS)))
        if self.kind == "choice" and not self.choices:
            raise ValueError("choice 类型的参数 %r 必须给出 choices" % self.name)
        unknown = {value for value, _ in self.choice_labels} - set(self.choices)
        if unknown:
            raise ValueError("参数 %r 的 choice_labels 指向了不存在的选项：%s"
                             % (self.name, "、".join(sorted(unknown))))
        if self.kind == "path" and self.path_mode not in {"file", "directory"}:
            raise ValueError("path 参数 %r 的 path_mode 只能是 file 或 directory" % self.name)

    def coerce(self, value: Any) -> Any:
        """把外部传进来的值转成本参数的规范形式，不合法就抛 ToolError。"""
        try:
            return self._coerce(value)
        except ToolError:
            raise
        except (TypeError, ValueError) as exc:
            raise ToolError("参数 %s（%s）取值不合法：%r —— %s"
                            % (self.name, self.label, value, exc)) from exc

    def _coerce(self, value: Any) -> Any:
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("true", "yes", "1", "on"):
                return True
            if text in ("false", "no", "0", "off"):
                return False
            raise ValueError("需要真假值")
        if self.kind == "int":
            return int(value)
        if self.kind == "float":
            return float(value)
        if self.kind == "path":
            return Path(value).expanduser()
        if self.kind == "choice":
            text = str(value)
            for option in self.choices:
                if option.lower() == text.lower():
                    return option
            raise ValueError("可选值为 %s" % ", ".join(self.choices))
        if self.kind == "strlist":
            if isinstance(value, str):
                items = [part.strip() for part in value.split(",")]
            else:
                items = [str(part).strip() for part in value]
            items = [item for item in items if item]
            if not items:
                raise ValueError("列表不能为空")
            return tuple(items)
        if self.kind == "ranges":
            ranges: list[tuple[float, float]] = []
            for pair in value:
                low, high = (float(item) for item in pair)
                if low > high:
                    low, high = high, low
                ranges.append((low, high))
            return tuple(ranges)
        return str(value)


def when(**conditions) -> tuple:
    """``visible_when`` 的写法糖。

        Param("rank", "WSRRR 秩", 10, kind="int", visible_when=when(algorithm="WSRRR"))
        Param("iid", "IID", False, kind="bool",
              visible_when=when(algorithm=("FastHyDe", "FastHyIe"), noise_type="additive"))

    值可以是单个，也可以是一组；多个条件之间是「并且」。
    """
    normalized = []
    for name, allowed in conditions.items():
        if isinstance(allowed, (str, bytes)) or not isinstance(allowed, (list, tuple, set)):
            allowed = (allowed,)
        normalized.append((name, tuple(allowed)))
    return tuple(normalized)


def _matches(param: Param, values: dict) -> bool:
    """当前取值下，这个参数该不该露面。"""
    for name, allowed in param.visible_when:
        current = values.get(name)
        if current not in allowed and str(current) not in {str(a) for a in allowed}:
            return False
    return True


# =============================================================================
# 工作项与工具
# =============================================================================

@dataclass(frozen=True)
class WorkItem:
    """一件活。

    ``key`` 是给人看的稳定标识，进日志、拼输出名；``source`` 是主输入（文件或
    目录）；``payload`` 装工具在发现阶段就顺手拿到的东西——比如 Sentinel-2 的
    各波段文件路径与已解析的产品元数据，免得 ``process`` 再翻一次目录。
    """

    key: str
    source: Path
    payload: dict = field(default_factory=dict)


class Tool(ABC):
    """一件工具。子类填好类属性并实现三个方法即可。"""

    #: 稳定的机器标识，批处理与配置文件按它取工具
    id: str = ""
    #: 人读的名字
    name: str = ""
    #: 一句话说明
    summary: str = ""
    #: 输入粒度：``raster`` 单个栅格文件，``product_dir`` 一整个产品目录。
    #: 批处理据此给出合适的提示，将来的界面据此决定用文件还是目录选择器。
    input_kind: str = "raster"
    #: 主产物类型。批处理据此做安全清理；现有工具默认仍是文件。
    output_kind: str = "file"
    #: 参数表
    params: tuple[Param, ...] = ()

    # -- 参数 ---------------------------------------------------------------

    def defaults(self) -> dict:
        return {param.name: param.default for param in self.params}

    def resolve_params(self, given: dict | None = None) -> dict:
        """填默认值、逐项校验；传了表里没有的键就直接报错。

        宁可在开跑前报错，也不要把拼错的参数名默默忽略掉——批量任务动辄跑几十
        个场景，参数没生效要到最后看产物才发现。
        """
        given = dict(given or {})
        known = {param.name: param for param in self.params}
        unknown = sorted(set(given) - set(known))
        if unknown:
            raise ToolError(
                "工具 %s 不认识这些参数：%s。可用参数：%s"
                % (self.id, ", ".join(unknown), ", ".join(sorted(known)) or "无")
            )
        resolved = {}
        for name, param in known.items():
            resolved[name] = param.coerce(given[name]) if name in given else param.default
        return resolved

    # -- 三个必须实现的方法 --------------------------------------------------

    @abstractmethod
    def discover(self, root: Path, params: dict, log: Logger) -> list[WorkItem]:
        """从一个根路径（文件或目录）找出所有待处理的工作项。

        这里抛异常会中止整批——发现阶段的问题（路径不存在、参数不合法、缺
        驱动）属于开跑前就该拦住的事，不该等跑到一半才报。
        """

    @abstractmethod
    def output_path(self, item: WorkItem, out_dir: Path, params: dict) -> Path:
        """这件活的主产物路径。"""

    @abstractmethod
    def process(self, item: WorkItem, out_path: Path, params: dict, log: Logger) -> None:
        """处理一件活。只管写出产物，不要跳过、不要吞异常。"""

    def process_with_control(self, item: WorkItem, out_path: Path, params: dict,
                             log: Logger,
                             should_stop: Callable[[], bool] | None = None) -> None:
        """带取消检查的处理入口；现有工具默认沿用 ``process``。

        长时间运行的独立 Worker 可覆写此方法，把同一个取消检查传给子进程调度器。
        """
        self.process(item, out_path, params, log)

    # -- 可选 ---------------------------------------------------------------

    def companions(self, out_path: Path) -> list[Path]:
        """主产物之外的伴生文件，如 ENVI 的 ``.hdr``、GDAL 的 ``.aux.xml``。

        批处理判断"产物已存在"和失败后清理半成品时都要看这些。默认没有。
        """
        return []

    def validate_output(self, item: WorkItem, out_path: Path) -> None:
        """在批处理创建/删除任何路径前验证输出位置。"""
        if Path(out_path).resolve(strict=False) == item.source.resolve():
            raise ToolError("输出路径不得覆盖主输入：%s" % out_path)

    def transients(self, out_path: Path) -> list[Path]:
        """处理中可能产生、但不能被视为完成产物的临时路径。

        目录型工具通常先写 ``<target>.partial``，校验后再原子改名。批处理会在
        开工前和失败后清理这些路径，但不会因为它们存在就跳过任务。
        """
        return []

    def visible_params(self, values: dict) -> set:
        """当前取值下应当显示哪些参数名。

        默认按各参数自己的 ``visible_when`` 求值。参数与算法的对应关系如果工具
        自己已经有一份权威表（比如按算法拼 worker 参数的那条分支），就重写这个
        方法直接用那份表——**不要**在界面层再抄一遍，两份迟早会不一致。

        只影响界面显示。批处理仍旧把所有参数都填上默认值交给工具，藏起来的参数
        取值始终合法，不会因为没露面就变成空洞。
        """
        return {param.name for param in self.params if _matches(param, values)}

    def suggestions(self, name: str) -> tuple[str, ...]:
        """某个参数的候选值，供界面做成可编辑下拉框。

        与 ``choice`` 的区别是不限定取值——候选只是省一次手打。取值要枚举得死，
        该用 ``kind="choice"``。默认没有候选。

        做成方法而不是 ``Param`` 的字段，是因为候选常要现读磁盘（有哪些传感器
        参数文件），不该在模块导入时就去翻目录。
        """
        return ()


# =============================================================================
# 注册表
# =============================================================================

_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    if not tool.id:
        raise ValueError("工具必须有 id：%r" % type(tool).__name__)
    existing = _REGISTRY.get(tool.id)
    if existing is not None and type(existing) is not type(tool):
        raise ValueError("工具 id 重复：%s" % tool.id)
    _REGISTRY[tool.id] = tool
    return tool


def get(tool_id: str) -> Tool:
    _load_builtin_tools()
    try:
        return _REGISTRY[tool_id]
    except KeyError:
        raise ToolError("没有名为 %r 的工具。已注册：%s"
                        % (tool_id, ", ".join(sorted(_REGISTRY)) or "无")) from None


def all_tools() -> list[Tool]:
    _load_builtin_tools()
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


def _load_builtin_tools() -> None:
    """首次访问注册表时导入内置工具包，避免模块间循环导入。"""
    if _REGISTRY:
        return
    from . import tools  # noqa: F401  导入即注册


# =============================================================================
# 发现工作项时的公共帮手
# =============================================================================

def iter_files(root: Path, suffixes: Sequence[str], recursive: bool = True) -> list[Path]:
    """列出 root 下所有指定后缀的文件；root 本身是文件时就只有它自己。

    ENVI 数据是 ``.dat`` 配 ``.hdr``，只收数据文件，头文件会被当成数据重复处理。
    """
    root = Path(root).expanduser()
    wanted = {suffix.lower() for suffix in suffixes}
    if root.is_file():
        if root.suffix.lower() not in wanted:
            raise ToolError("文件 %s 的后缀不在本工具支持的范围内：%s"
                            % (root, ", ".join(sorted(wanted))))
        return [root]
    if not root.is_dir():
        raise ToolError("输入路径不存在：%s" % root)
    walker: Iterable[Path] = root.rglob("*") if recursive else root.glob("*")
    return sorted(
        path for path in walker
        if path.is_file() and path.suffix.lower() in wanted
    )
