# -*- coding: utf-8 -*-
"""底图密钥自测：源码里不许有密钥，解析顺序要对，落盘要收权限。

这些是靠肉眼盯不住的东西——顺手把一把密钥写回 config.py 里，程序照常跑，等到
仓库公开或者产物发出去才发现。全部固化成断言。
"""

import json
import os
import re
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "samplelib")
sys.path.insert(0, ROOT)

from samplelib import config                                     # noqa: E402
from samplelib.annotate import tiles                             # noqa: E402


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    print("  ✓ %s" % what)


print("[1] 源码里没有密钥字面量")
# Mapbox 的 public token 与天地图的 32 位十六进制串各有明显特征
PATTERNS = (
    ("Mapbox token", re.compile(r"pk\.[A-Za-z0-9_\-]{20,}")),
    ("32 位十六进制密钥", re.compile(r"""["'][0-9a-f]{32}["']""")),
)
hits = []
for folder, dirs, files in os.walk(PKG):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for name in files:
        if not name.endswith(".py"):
            continue
        path = os.path.join(folder, name)
        text = open(path, encoding="utf-8").read()
        for label, pat in PATTERNS:
            for m in pat.finditer(text):
                hits.append("%s: %s（%s）"
                            % (os.path.relpath(path, ROOT), m.group()[:12] + "…",
                               label))
check(not hits, "samplelib 下没有密钥字面量%s"
      % ("" if not hits else "，实得 " + "；".join(hits)))
check(config.KEY_FILE_NAME not in open(
    os.path.join(ROOT, "packaging", "LithoSpectraWeb.spec"), encoding="utf-8").read(),
    "打包 spec 不碰密钥文件")
gitignore = open(os.path.join(os.path.dirname(ROOT), ".gitignore"),
                 encoding="utf-8").read()
check("keys.json" in gitignore, "keys.json 在 .gitignore 里")


print("[2] 解析顺序：环境变量 > 密钥文件 > 空")
WORK = tempfile.mkdtemp(prefix="slkeys_")
_real_dir = config.app_data_dir
config.app_data_dir = lambda: WORK
_env = {n: os.environ.pop(n, None) for n in config.KEY_INFO}
try:
    for name in config.KEY_INFO:
        check(config.read_key(name) == "", "%s 什么都没配时是空" % name)
        check(config.key_source(name) == "", "%s 的来源也是空" % name)

    config.save_key("MAPBOX_TOKEN", "pk.从文件来的")
    check(config.read_key("MAPBOX_TOKEN") == "pk.从文件来的", "读得到密钥文件里的值")
    check(config.key_source("MAPBOX_TOKEN") == "密钥文件", "来源报「密钥文件」")
    check(config.MAPBOX_TOKEN == "pk.从文件来的", "模块级的名字当场刷新了")
    check(tiles.source_key(tiles.build_sources()["Mapbox 街道"]) == "pk.从文件来的",
          "取片时拿到的就是这一个")

    os.environ["MAPBOX_TOKEN"] = "pk.环境变量优先"
    check(config.read_key("MAPBOX_TOKEN") == "pk.环境变量优先", "环境变量压过文件")
    check(config.key_source("MAPBOX_TOKEN") == "环境变量", "来源报「环境变量」")
    del os.environ["MAPBOX_TOKEN"]

    print("[3] 密钥文件只给本人读写，且只收认识的键")
    path = config.key_file()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    check(mode == 0o600 or os.name == "nt", "权限是 0600（实得 %o）" % mode)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"MAPBOX_TOKEN": "pk.好的", "乱写的": "x"}, fh)
    check(set(config.load_keys()) == {"MAPBOX_TOKEN"}, "不认识的键被丢掉")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ 这不是 json")
    check(config.load_keys() == {}, "文件坏了当作没配过，不抛异常")

    print("[4] 没有密钥时，要密钥的底图不可用")
    os.remove(path)
    config.MAPBOX_TOKEN = config.read_key("MAPBOX_TOKEN")
    config.TIANDITU_KEY = config.read_key("TIANDITU_KEY")
    fetcher = tiles.TileFetcher()
    try:
        sources = tiles.build_sources()
        for name, src in sources.items():
            if src.needs_key:
                check(not fetcher.usable(src), "「%s」没密钥就不可用" % name)
        check(fetcher.usable(sources["OpenStreetMap"]),
              "OpenStreetMap 不需要密钥，照常可用")
    finally:
        fetcher.shutdown()
finally:
    config.app_data_dir = _real_dir
    for n, v in _env.items():
        if v is not None:
            os.environ[n] = v
    config.TIANDITU_KEY = config.read_key("TIANDITU_KEY")
    config.MAPBOX_TOKEN = config.read_key("MAPBOX_TOKEN")
    import shutil
    shutil.rmtree(WORK, ignore_errors=True)

print("\n密钥自测通过")
