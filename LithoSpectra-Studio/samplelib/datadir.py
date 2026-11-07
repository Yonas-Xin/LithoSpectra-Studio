# -*- coding: utf-8 -*-
"""数据目录与随包资源目录的定位。

数据目录存放样本库、界面状态、缓存、深度学习任务等程序写出的文件：

* 源码运行：项目目录 ``LithoSpectra-Studio/``；
* Windows 打包版：exe 同级的 ``userdata\\``，整个程序目录拷走即带走全部数据。exe 所在
  目录不可写（例如装在 Program Files 下）时退回「文档\\LithoSpectra」；
* macOS 打包版：``~/Documents/LithoSpectra``。.app 包内不能写入（签名、只读 DMG、
  系统对未签名 App 的随机路径隔离）。

环境变量 ``LITHOSPECTRA_DATA_DIR`` 优先于以上规则，打包脚本的冒烟测试用它把数据
写到临时目录。

Windows 版旧版本的数据在「文档\\LithoSpectra」。新版第一次使用 ``userdata\\`` 时把旧
数据移过去，并把状态文件、深度学习登记表与任务文件里指向旧目录的路径改成新目录。
瓦片缓存与栅格缓存不移动，留在原处，新目录下会重新生成。

本模块不导入 ``config``，``config`` 在导入时就要用到数据目录。
"""

import json
import os
import shutil
import sys
import tempfile
from typing import Any, List, Optional

DATA_DIR_ENV = "LITHOSPECTRA_DATA_DIR"
APP_FOLDER = "LithoSpectra"
PORTABLE_DATA_NAME = "userdata"
RESOURCE_DIR_NAME = "resources"

FROZEN = bool(getattr(sys, "frozen", False))
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 迁移时不移动的缓存目录（相对数据目录），删掉后会按需重新生成
_CACHE_DIRS = (os.path.join("data", "tilecache"), os.path.join("data", "rastercache"))
# 迁移后需要改写路径的文件所在的子目录与扩展名
_REBASE_DIRS = ("state", "dl")
_REBASE_EXTS = (".json", ".ndjson")

_resolved: Optional[str] = None
migration_log: List[str] = []


def exe_dir() -> str:
    return os.path.dirname(os.path.abspath(sys.executable))


def legacy_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Documents", APP_FOLDER)


def resource_dir() -> str:
    """随包只读资源（图标、波长表、光谱库、图例、工具箱、Worker）的根目录。

    Windows 产物里在 exe 同级的 ``resources\\``；macOS 在 .app 包内，经 PyInstaller
    的解包目录访问。仅在打包版中有意义。
    """
    for base in (exe_dir(), getattr(sys, "_MEIPASS", "")):
        path = os.path.join(base, RESOURCE_DIR_NAME) if base else ""
        if path and os.path.isdir(path):
            return path
    return os.path.join(getattr(sys, "_MEIPASS", exe_dir()), RESOURCE_DIR_NAME)


def data_dir() -> str:
    """数据目录，首次调用时确定并创建，之后不变。"""
    global _resolved
    if _resolved is None:
        _resolved = _resolve()
    return _resolved


def _resolve() -> str:
    custom = os.environ.get(DATA_DIR_ENV, "").strip()
    if custom:
        os.makedirs(custom, exist_ok=True)
        return os.path.abspath(custom)
    if not FROZEN:
        return PROJECT_DIR
    if sys.platform == "win32":
        portable = os.path.join(exe_dir(), PORTABLE_DATA_NAME)
        if _writable(portable):
            if not os.listdir(portable):
                migrate(legacy_dir(), portable)
            return portable
    return _documents_dir()


def _documents_dir() -> str:
    base = legacy_dir()
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:                      # 家目录不可写时退到临时目录
        base = os.path.join(tempfile.gettempdir(), APP_FOLDER)
        os.makedirs(base, exist_ok=True)
    return base


def _writable(folder: str) -> bool:
    try:
        os.makedirs(folder, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".probe", dir=folder)
        os.close(fd)
        os.remove(probe)
        return True
    except OSError:
        return False


# -----------------------------------------------------------------------------
# 旧数据迁移
# -----------------------------------------------------------------------------

def migrate(old: str, new: str) -> bool:
    """把旧数据目录的内容移到新目录。旧目录不存在或为空时什么都不做。

    逐项移动，某一项失败时记入 :data:`migration_log` 并继续，已移动的不回滚。
    """
    if not os.path.isdir(old) or not os.listdir(old):
        return False
    if os.path.normcase(os.path.abspath(old)) == os.path.normcase(os.path.abspath(new)):
        return False
    skip = {os.path.normcase(os.path.join(old, c)) for c in _CACHE_DIRS}
    _move_tree(old, new, skip)
    _rebase_paths(new, old)
    _remove_empty_dirs(old)
    migration_log.insert(0, "已将数据从 %s 移到 %s" % (old, new))
    return True


def _move_tree(src: str, dst: str, skip: set) -> None:
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.normcase(s) in skip:
            continue
        holds_skipped = any(k.startswith(os.path.normcase(s) + os.sep) for k in skip)
        try:
            if holds_skipped or (os.path.isdir(s) and os.path.isdir(d)):
                _move_tree(s, d, skip)
            elif not os.path.exists(d):
                shutil.move(s, d)
            else:
                migration_log.append("目标已存在，未移动：%s" % s)
        except OSError as exc:
            migration_log.append("未能移动 %s：%s" % (s, exc))


def _remove_empty_dirs(root: str) -> None:
    for folder, _dirs, _files in os.walk(root, topdown=False):
        try:
            os.rmdir(folder)             # 只删空目录
        except OSError:
            pass


def _rebase_paths(root: str, old: str) -> None:
    """把 ``state/`` 与 ``dl/`` 下 JSON、NDJSON 文件里以旧目录开头的路径改到新目录。"""
    for sub in _REBASE_DIRS:
        for folder, _dirs, files in os.walk(os.path.join(root, sub)):
            for name in files:
                if name.lower().endswith(_REBASE_EXTS):
                    path = os.path.join(folder, name)
                    try:
                        _rebase_file(path, old, root)
                    except (OSError, ValueError) as exc:
                        migration_log.append("未能改写 %s 中的路径：%s" % (path, exc))


def _rebase_file(path: str, old: str, new: str) -> None:
    # utf-8-sig：手工编辑过的文件可能带 BOM（如 Windows 记事本、PowerShell 写出的）
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    if path.lower().endswith(".ndjson"):
        lines = [json.dumps(_rebase(json.loads(line), old, new), ensure_ascii=False)
                 if line.strip() else line for line in text.splitlines()]
        out = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    else:
        out = json.dumps(_rebase(json.loads(text), old, new), ensure_ascii=False, indent=2)
    if out != text:
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(out)
        os.replace(tmp, path)


def rebase_path(value: str, old: str, new: str) -> str:
    """``value`` 位于 ``old`` 之下时换成 ``new`` 下的同一相对位置，否则原样返回。

    比较时不分大小写、不分正反斜杠（按当前系统的 ``normcase``）。
    """
    n = len(old)
    if len(value) < n or os.path.normcase(value[:n]) != os.path.normcase(old):
        return value
    rest = value[n:]
    if rest and rest[0] not in "\\/":
        return value
    return new + rest


def _rebase(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return rebase_path(value, old, new)
    if isinstance(value, list):
        return [_rebase(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: _rebase(v, old, new) for k, v in value.items()}
    return value
