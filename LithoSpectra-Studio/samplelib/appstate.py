# -*- coding: utf-8 -*-
"""上次退出时的界面状态。

回答的只有一个问题：**打开就回到上次的样子**——上次开的样本库、窗口大小与
面板布局、画布视野、勾选的样本、检索词、画布上叠着的图层。

程序自动写，退出时一次，一份，路径固定在用户数据目录 ``state/`` 下。丢了下次按
默认布局打开，数据无损——**它记的是怎么看，不是数据本身**，样本始终以样本库里的
为准，图层引的是磁盘上原本就在那儿的文件。

早先还有一套使用者显式保存的「标注工程」（``.slproj``），记的东西与这里几乎一样，
只是可以存好几份、放在自己挑的位置。已经删掉：退出时本来就自动记着，再让人手动
存一遍是同一件事做两遍，而两份状态各记一半、谁覆盖谁说不清，是更麻烦的事。

文件坏了、版本对不上、里面引的数据没了，一律当作没有状态：界面照常打开。
一份纯粹图方便的文件，绝不能成为进不去程序的理由。
"""

import json
import os
from typing import Any, Dict, List

from . import config

FORMAT = "lithospectra-session"
VERSION = 1


def state_path() -> str:
    """状态文件路径。跟着数据根走，源码与打包各是各的。"""
    return os.path.join(config.app_data_dir(), "state", "session.json")


def load() -> Dict[str, Any]:
    """读状态。任何异常都返回空字典，由调用方走默认行为。"""
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        return {}
    try:
        if int(doc.get("version", 0)) > VERSION:
            return {}                   # 更新版本写的，不猜它的字段含义
    except (TypeError, ValueError):
        return {}
    return doc


def save(doc: Dict[str, Any]) -> str:
    """写状态，返回实际写入的路径；写不成返回空字符串。

    先写 ``.part`` 再原子替换：退出过程中断电也不会留下一个半截的 JSON，
    那会让下次启动读到坏文件。
    """
    path = state_path()
    doc = dict(doc, format=FORMAT, version=VERSION)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        return ""                       # 存不下就算了，不能拦着程序退出
    return path


def clear() -> None:
    """丢掉状态，下次以默认布局启动。"""
    try:
        os.remove(state_path())
    except OSError:
        pass


def last_library(doc: Dict[str, Any] = None) -> str:
    """上次打开的样本库；文件已不在则返回空。"""
    doc = load() if doc is None else doc
    path = doc.get("library")
    return path if isinstance(path, str) and path and os.path.isfile(path) else ""


def recent(doc: Dict[str, Any] = None) -> List[str]:
    """最近打开的样本库路径。手工改坏的条目（空值、非字符串）直接跳过。"""
    doc = load() if doc is None else doc
    items = doc.get("recent")
    return [p for p in items if isinstance(p, str) and p] if isinstance(items, list) else []
