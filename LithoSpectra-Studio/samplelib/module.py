# -*- coding: utf-8 -*-
"""功能模块的清单与发现。

平台底座是"样本库 + 画布 + 窗口外壳"，此外的功能——光谱、光谱库、样本裁剪、
预处理工具箱、在线底图、野外记录——按客户需求装或不装。一个模块不在这份发行版里，
界面上就没有它的入口，包里也没有它的代码与数据，而不是灰掉一个点了报错的菜单项。

* :class:`Manifest` —— 模块的身份与依赖，写在各自目录下的 ``module.json``。
  打包脚本要在不 import samplelib 的前提下读它（见 packaging/LithoSpectraWeb.spec），
  所以用标准库的 json。
* :func:`discover` —— 扫出包里有哪些模块；:func:`read_profile` —— 某个发行版要哪些。

Web 后端按这两样决定开放哪些接口，页面按 ``/api/modules`` 的结果决定显示哪些入口。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

#: 宿主接口版本。模块清单里的 ``api`` 写它要求的主版本号。
HOST_API_VERSION = 1

#: 模块目录名，相对本文件所在的包
MODULES_DIR = "modules"
#: 发行版 profile 目录
PROFILES_DIR = "profiles"

MANIFEST_NAME = "module.json"


class ModuleError(RuntimeError):
    """清单不合规、依赖不满足、或模块自己装不上。"""


@dataclass(frozen=True)
class Manifest:
    """一个模块的身份。"""

    id: str
    name: str
    version: str
    summary: str = ""
    #: 依赖的模块 id。缺一个就整个不装——半装上的模块比不装更难查。
    requires: Tuple[str, ...] = ()
    #: 随模块走的数据文件，相对 samplelib/。打包时按 profile 决定带不带。
    data: Tuple[str, ...] = ()
    #: 要求的宿主接口主版本，见 :data:`HOST_API_VERSION`。不写按当前算。
    api: int = HOST_API_VERSION

    @staticmethod
    def load(path: str) -> "Manifest":
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ModuleError("%s 读不出来：%s" % (path, exc)) from exc
        if not isinstance(doc, dict):
            raise ModuleError("%s 不是一个 JSON 对象" % path)
        missing = [k for k in ("id", "name", "version") if not doc.get(k)]
        if missing:
            raise ModuleError("%s 缺字段：%s" % (path, "、".join(missing)))
        try:
            api = int(doc.get("api", HOST_API_VERSION))
        except (TypeError, ValueError):
            raise ModuleError("%s 的 api 字段不是整数" % path)
        return Manifest(
            id=str(doc["id"]),
            name=str(doc["name"]),
            version=str(doc["version"]),
            summary=str(doc.get("summary", "")),
            requires=tuple(str(r) for r in doc.get("requires", ())),
            data=tuple(str(d) for d in doc.get("data", ())),
            api=api,
        )


# =============================================================================
# 发现
# =============================================================================

def _here() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def discover(broken: Optional[List[Tuple[str, str]]] = None) -> Dict[str, Manifest]:
    """扫出包里带 ``module.json`` 的模块目录。

    发现即可用：目录在，模块就装得上。不做"装了再用开关挡住"那一套——同一份
    产物里藏着能力再用界面挡住，既挡不住也不体面。要哪些由 profile 说了算，
    没选中的模块压根不进包。

    清单写坏了的模块不会让整个程序起不来：跳过它，原因记进 ``broken``（给了
    列表的话），其余照装。一个模块的 JSON 少个逗号，不该是打不开样本库的理由。
    """
    root = os.path.join(_here(), MODULES_DIR)
    found: Dict[str, Manifest] = {}
    if not os.path.isdir(root):
        return found
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, MANIFEST_NAME)
        if not os.path.isfile(path):
            continue
        try:
            manifest = Manifest.load(path)
            if manifest.id != name:
                raise ModuleError("模块目录名与 id 对不上：%s != %s"
                                  % (name, manifest.id))
        except ModuleError as exc:
            if broken is not None:
                broken.append((name, str(exc)))
            continue
        found[manifest.id] = manifest
    return found


def read_profile(name: str) -> Optional[List[str]]:
    """读一个发行版 profile，返回它要装的模块 id。

    profile 文件不存在时返回 ``None``，调用方当作"包里有什么装什么"。
    """
    path = os.path.join(_here(), PROFILES_DIR, "%s.json" % name)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return [str(x) for x in doc.get("modules", ())]
