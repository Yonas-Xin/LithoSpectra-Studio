# -*- coding: utf-8 -*-
"""发行版自测：把没选中的模块目录**真删掉**，程序还起不起得来。

profile 只是配置，删文件才是打包时真正发生的事。这个自测把 ``samplelib`` 与
``webapi`` 复制到临时目录，按 profile 删掉不要的模块目录，再在一个干净的子进程里把
后端建起来——只有这样才能证明"基础版"不是设想，而是一份交得出去的产物。

跑起来几秒钟，比在 CI 上打一次包便宜得多，且每次改动都能跑。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# =============================================================================
# 要验哪几个发行版：profile 名 -> 期望装上的模块
# =============================================================================
EDITIONS = {
    "basic": [],
    "analysis": ["basemap", "spectra"],
}
# =============================================================================

CHILD = '''# -*- coding: utf-8 -*-
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import samplelib
import samplelib.config as cfg
cfg.MODULE_PROFILE = %r

from webapi import create_app
from webapi.misc_routes import enabled_modules

app = create_app("", static_dir="", remember=False)
paths = sorted({r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")})
here = os.path.dirname(samplelib.__file__)
print("RESULT " + json.dumps({
    "modules": sorted(enabled_modules()),
    "routes": paths,
    "on_disk": sorted(n for n in os.listdir(os.path.join(here, "modules"))
                      if n != "__pycache__"),
}, ensure_ascii=False))
'''


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    print("  ✓ %s" % what)


def build_edition(work, profile, keep):
    """复制一份 samplelib，删掉 keep 之外的模块目录，返回临时目录。"""
    root = os.path.join(work, profile)
    os.makedirs(root)
    for name in ("samplelib", "webapi"):
        shutil.copytree(os.path.join(ROOT, name), os.path.join(root, name),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    modules = os.path.join(root, "samplelib", "modules")
    for name in sorted(os.listdir(modules)):
        path = os.path.join(modules, name)
        if os.path.isdir(path) and name not in keep:
            shutil.rmtree(path)
    script = os.path.join(root, "boot.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(CHILD % profile)
    return root, script


def run(script):
    proc = subprocess.run([sys.executable, script], capture_output=True,
                          text=True, cwd=os.path.dirname(script))
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    raise AssertionError("子进程没起来：\n%s\n%s"
                         % (proc.stdout[-2000:], proc.stderr[-2000:]))


WORK = tempfile.mkdtemp(prefix="sledition_")
try:
    for profile, keep in EDITIONS.items():
        print("[%s] 只留 %s" % (profile, "、".join(keep) or "底座"))
        root, script = build_edition(WORK, profile, keep)
        size = sum(os.path.getsize(os.path.join(d, f))
                   for d, _s, fs in os.walk(os.path.join(root, "samplelib"))
                   for f in fs) / 1e6
        got = run(script)

        check(got["on_disk"] == sorted(keep + ["__init__.py"]),
              "磁盘上只剩这些模块：%s" % got["on_disk"])
        check(got["modules"] == sorted(keep),
              "装上的正是这些：%s" % (got["modules"] or "无"))
        routes = got["routes"]
        check(any(r.startswith("/api/classes") for r in routes) and any(r.startswith("/api/regions") for r in routes),
              "底座接口都在（共 %d 个）" % len(routes))

        def has(prefix):
            return any(r.startswith(prefix) for r in routes)

        has_spectra = "spectra" in keep
        check(has("/api/spectra") is has_spectra,
              "光谱接口%s" % ("在" if has_spectra else "不在"))
        check(has("/api/clip") is ("clip" in keep),
              "裁剪接口%s" % ("在" if "clip" in keep else "不在"))
        check(has("/api/fw") is ("fieldwork" in keep),
              "野外记录接口%s" % ("在" if "fieldwork" in keep else "不在"))
        print("  · 这份产物的 samplelib 有 %.1f MB" % size)
finally:
    shutil.rmtree(WORK, ignore_errors=True)

print("\n发行版自测通过")
