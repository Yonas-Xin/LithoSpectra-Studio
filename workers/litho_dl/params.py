# -*- coding: utf-8 -*-
"""任务参数声明。

声明式写法让三处都不必逐任务写代码：Worker 靠它填默认值与校验类型，界面靠
``capabilities`` 输出的 JSON 生成参数表单，脚本的配置区照着它填。形状与仓库根
``toolbox.core.Param`` 一致，只多了取值范围与"高级"分组；Worker 要能拷到别的
机器上单独跑，所以不 import toolbox。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

KINDS = ("str", "int", "float", "bool", "choice", "path")


class ParamError(ValueError):
    """参数缺失、类型不对或超出范围。消息直接给使用者看。"""


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    default: Any
    kind: str = "str"
    choices: tuple = ()
    #: ``(取值, 显示名)`` 成对给出；没给的选项直接显示取值本身
    choice_labels: tuple = ()
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    #: path 参数选文件还是目录；文件时可给扩展名过滤
    path_mode: str = "file"
    extensions: tuple = ()
    #: 为真时界面默认折叠在"高级"里
    advanced: bool = False
    #: 可以留空（path / str 留空表示不用）
    optional: bool = False
    #: ``((控制参数名, (允许取值, ...)), ...)``，全部成立才显示
    visible_when: tuple = ()
    #: choice 参数的旧取值 ``((旧值, 现值), ...)``：改过名的选项，旧任务、旧模型包照样认得
    aliases: tuple = ()

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError("未知参数类型 %r" % self.kind)
        if self.kind == "choice" and not self.choices:
            raise ValueError("choice 参数 %r 必须给 choices" % self.name)

    def coerce(self, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            if self.optional:
                return None if self.kind in ("path", "str") else self.default
            if self.default is None:
                raise ParamError("缺少参数：%s（%s）" % (self.label, self.name))
            value = self.default
        try:
            out = self._coerce(value)
        except (TypeError, ValueError) as exc:
            raise ParamError("%s 取值不合法：%r（%s）" % (self.label, value, exc)) from exc
        if self.kind in ("int", "float"):
            if self.minimum is not None and out < self.minimum:
                raise ParamError("%s 不能小于 %s：%r" % (self.label, self.minimum, out))
            if self.maximum is not None and out > self.maximum:
                raise ParamError("%s 不能大于 %s：%r" % (self.label, self.maximum, out))
        return out

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
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("需要整数")
            return int(value)
        if self.kind == "float":
            return float(value)
        if self.kind == "path":
            return str(Path(str(value)).expanduser())
        if self.kind == "choice":
            text = str(value)
            text = dict((str(a).lower(), b) for a, b in self.aliases).get(text.lower(), text)
            for option in self.choices:
                if str(option).lower() == str(text).lower():
                    return option
            raise ValueError("可选值为 %s" % " / ".join(map(str, self.choices)))
        return str(value)

    def to_json(self) -> dict:
        labels = dict(self.choice_labels)
        return {
            "name": self.name, "label": self.label, "default": self.default,
            "kind": self.kind, "help": self.help,
            "choices": [{"value": c, "label": labels.get(c, str(c))} for c in self.choices],
            "minimum": self.minimum, "maximum": self.maximum,
            "path_mode": self.path_mode, "extensions": list(self.extensions),
            "advanced": self.advanced, "optional": self.optional,
            "visible_when": [{"param": name, "values": list(values)}
                             for name, values in self.visible_when],
            "aliases": {str(a): b for a, b in self.aliases},
        }


def when(**conditions) -> tuple:
    """``visible_when`` 的写法糖：``when(normalization="scale")``。"""
    out = []
    for name, allowed in conditions.items():
        if isinstance(allowed, (str, bytes)) or not isinstance(allowed, (list, tuple, set)):
            allowed = (allowed,)
        out.append((name, tuple(allowed)))
    return tuple(out)


def coerce_all(params: Sequence[Param], raw: Mapping[str, Any]) -> dict:
    """按声明补默认值、转类型；有未声明的键直接报错，以免拼错的参数名被静默忽略。"""
    known = {p.name for p in params}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ParamError("未知参数：%s" % "、".join(unknown))
    return {p.name: p.coerce(raw.get(p.name)) for p in params}


@dataclass
class TaskSpec:
    """一种任务：名字、显示名、参数声明、执行函数。"""

    name: str
    title: str
    params: tuple
    run: Any = field(repr=False)
    summary: str = ""

    def to_json(self) -> dict:
        return {"name": self.name, "title": self.title, "summary": self.summary,
                "params": [p.to_json() for p in self.params]}
