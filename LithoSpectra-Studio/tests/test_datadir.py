# -*- coding: utf-8 -*-
"""数据目录自测：定位规则、环境变量覆盖、Windows 版旧数据迁移与路径改写。

迁移只在 Windows 打包版首次启动时发生，这里在临时目录里模拟：替换 exe 目录、旧数据
目录与平台判断，不碰本机任何真实数据。直接运行本文件。
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from samplelib import datadir                                   # noqa: E402


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    print("  ✓ %s" % what)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def make_legacy(old):
    write(os.path.join(old, "data", "lib.gpkg"), "gpkg")
    write(os.path.join(old, "data", "tilecache", "osm", "1.png"), "tile")
    write(os.path.join(old, "data", "rastercache", "a.bin"), "raster")
    write(os.path.join(old, "my_spectra.slb"), "slb")
    write(os.path.join(old, "state", "session.json"), json.dumps({
        "format": "lithospectra-session", "version": 1,
        "library": os.path.join(old, "data", "lib.gpkg"),
        "recent": [os.path.join(old, "data", "lib.gpkg"), "/elsewhere/other.gpkg"],
        "dl_jobs_root": os.path.join(old, "dl", "jobs"),
        "sibling": old + "2/x.gpkg",
    }))
    job = os.path.join(old, "dl", "jobs", "j1")
    with open(os.path.join(old, "state", "session.json"), "r+", encoding="utf-8") as fh:
        text = fh.read()
        fh.seek(0)
        fh.write("\ufeff" + text)      # 手工编辑过的文件可能带 BOM
    write(os.path.join(old, "dl", "datasets.json"), json.dumps(
        [{"id": "d1", "path": os.path.join(old, "clips", "a")}]))
    write(os.path.join(job, "events.ndjson"),
          json.dumps({"type": "artifact", "path": os.path.join(job, "model", "m.lithomodel")}) + "\n"
          + json.dumps({"type": "progress", "value": 0.5}) + "\n")


print("[1] 路径改写只认目录边界")
check(datadir.rebase_path("/a/LithoSpectra/x.gpkg", "/a/LithoSpectra", "/b/u") == "/b/u/x.gpkg",
      "旧目录下的路径换到新目录")
check(datadir.rebase_path("/a/LithoSpectra", "/a/LithoSpectra", "/b/u") == "/b/u", "旧目录本身")
check(datadir.rebase_path("/a/LithoSpectra2/x", "/a/LithoSpectra", "/b/u") == "/a/LithoSpectra2/x",
      "同名前缀的兄弟目录不改")
check(datadir.rebase_path("/other/x", "/a/LithoSpectra", "/b/u") == "/other/x", "无关路径不改")

print("[2] 旧数据迁移")
with tempfile.TemporaryDirectory() as tmp:
    old, new = os.path.join(tmp, "Documents", "LithoSpectra"), os.path.join(tmp, "app", "userdata")
    make_legacy(old)
    datadir.migration_log.clear()
    check(datadir.migrate(old, new), "执行了迁移")
    check(os.path.isfile(os.path.join(new, "data", "lib.gpkg")), "样本库移到新目录")
    check(os.path.isfile(os.path.join(new, "my_spectra.slb")), "个人光谱库移到新目录")
    check(not os.path.exists(os.path.join(old, "data", "lib.gpkg")), "旧目录里不再保留样本库")
    check(not os.path.exists(os.path.join(new, "data", "tilecache")), "瓦片缓存不移动")
    check(os.path.isfile(os.path.join(old, "data", "tilecache", "osm", "1.png")), "瓦片缓存留在原处")
    check(os.path.isfile(os.path.join(old, "data", "rastercache", "a.bin")), "栅格缓存留在原处")
    state = read_json(os.path.join(new, "state", "session.json"))
    check(state["library"] == os.path.join(new, "data", "lib.gpkg"), "状态文件中的样本库路径已改写")
    check(state["recent"] == [os.path.join(new, "data", "lib.gpkg"), "/elsewhere/other.gpkg"],
          "最近列表只改写旧目录下的路径")
    check(state["dl_jobs_root"] == os.path.join(new, "dl", "jobs"), "任务根目录已改写")
    check(state["sibling"] == old + "2/x.gpkg", "同名前缀的路径未改写")
    check(read_json(os.path.join(new, "dl", "datasets.json"))[0]["path"]
          == os.path.join(new, "clips", "a"), "样本集登记表已改写")
    with open(os.path.join(new, "dl", "jobs", "j1", "events.ndjson"), encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh if line.strip()]
    check(events[0]["path"] == os.path.join(new, "dl", "jobs", "j1", "model", "m.lithomodel")
          and events[1] == {"type": "progress", "value": 0.5}, "任务事件逐行改写")
    check(datadir.migration_log and datadir.migration_log[0].startswith("已将数据从"), "迁移记入日志")
    check(not datadir.migrate(os.path.join(tmp, "none"), new), "旧目录不存在时不迁移")

print("[3] 打包版的定位规则（模拟 Windows 与 macOS）")
saved = (datadir.FROZEN, datadir.exe_dir, datadir.legacy_dir, datadir._writable, sys.platform)
try:
    with tempfile.TemporaryDirectory() as tmp:
        exe = os.path.join(tmp, "LithoSpectraWeb")
        old = os.path.join(tmp, "Documents", "LithoSpectra")
        make_legacy(old)
        os.environ.pop(datadir.DATA_DIR_ENV, None)
        datadir.FROZEN = True
        datadir.exe_dir = lambda: exe
        datadir.legacy_dir = lambda: old

        sys.platform = "win32"
        got = datadir._resolve()
        check(got == os.path.join(exe, "userdata"), "Windows：数据目录在 exe 旁的 userdata")
        check(os.path.isfile(os.path.join(got, "data", "lib.gpkg")), "Windows：首次启动搬来旧数据")
        write(os.path.join(old, "data", "late.gpkg"), "late")
        datadir._resolve()
        check(not os.path.exists(os.path.join(got, "data", "late.gpkg")),
              "Windows：userdata 已有内容时不再迁移")

        datadir._writable = lambda folder: False
        check(datadir._resolve() == old, "Windows：exe 目录不可写时退回文档目录")
        datadir._writable = saved[3]

        sys.platform = "darwin"
        check(datadir._resolve() == old, "macOS：数据目录在 ~/Documents/LithoSpectra")
finally:
    datadir.FROZEN, datadir.exe_dir, datadir.legacy_dir, datadir._writable, sys.platform = saved

print("[4] 环境变量优先")
with tempfile.TemporaryDirectory() as tmp:
    target = os.path.join(tmp, "custom")
    out = subprocess.run(
        [sys.executable, "-c", "from samplelib import config; print(config.app_data_dir())"],
        cwd=ROOT, env=dict(os.environ, **{datadir.DATA_DIR_ENV: target}),
        capture_output=True, text=True)
    check(out.stdout.strip() == target and os.path.isdir(target), "LITHOSPECTRA_DATA_DIR 覆盖默认规则")
out = subprocess.run(
    [sys.executable, "-c", "from samplelib import config; print(config.app_data_dir())"],
    cwd=ROOT, env={k: v for k, v in os.environ.items() if k != datadir.DATA_DIR_ENV},
    capture_output=True, text=True)
check(out.stdout.strip() == ROOT, "源码运行时数据目录为项目目录")

print("\n数据目录自测通过")
