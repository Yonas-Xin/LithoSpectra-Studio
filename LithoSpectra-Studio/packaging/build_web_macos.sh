#!/usr/bin/env bash
# 在 macOS 上构建 岩谱 LithoSpectra Web 界面版的 .app 与 .dmg。
#
#     conda activate samplelib
#     cd LithoSpectra-Studio
#     bash packaging/build_web_macos.sh
#
# 产物在 packaging/dist-web/ 下。构建约需 2 分钟（不含首次 npm install）。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
DIST="$HERE/dist-web"
WORK="$HERE/build-web"
APP="$DIST/LithoSpectraWeb.app"

cd "$ROOT"

echo "==> 检查依赖"
python -c "import PyInstaller, numpy, PIL, fastapi, uvicorn, webview; from osgeo import gdal" || {
  echo "缺少依赖。请先： conda activate samplelib && pip install fastapi uvicorn pywebview pyinstaller"
  exit 1
}
command -v npm >/dev/null || { echo "缺少 Node.js（npm），编译不了页面。"; exit 1; }

echo "==> 编译页面"
(cd web && { [ -d node_modules ] || npm install; } && npm run build)

echo "==> 清理上次的产物"
rm -rf "$DIST" "$WORK"

echo "==> 打包"
pyinstaller packaging/LithoSpectraWeb.spec --noconfirm \
  --distpath "$DIST" --workpath "$WORK" --log-level WARN

echo "==> 裁剪"
python packaging/prune.py "$APP"

# 冒烟测试在临时的家目录下跑：打包产物的数据目录是 ~/Documents/LithoSpectra，
# 真启动会往里复制样本库、写界面状态——不能动这台机器上已有的那份。
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

echo "==> 冒烟测试 1/2：自检（GDAL 驱动、proj.db、后端路由、页面、模块、随包数据）"
REPORT="$WORK/selftest.json"
rm -f "$REPORT"
SELFTEST_OK=1
HOME="$SANDBOX" LITHOSPECTRA_SELFTEST="$REPORT" "$APP/Contents/MacOS/LithoSpectraWeb" \
  >"$WORK/selftest.log" 2>&1 || SELFTEST_OK=0
if [ -f "$REPORT" ]; then
  python - "$REPORT" <<'PY'
import json, sys
r = json.load(open(sys.argv[1], encoding="utf-8"))
c = r.get("checks") or {}
g, w, m = c.get("gdal") or {}, c.get("web_shell") or {}, c.get("module_imports") or {}
if g:
    print("    GDAL %s，%d 个驱动，JPEG2000 %s" % (
        g.get("version"), g.get("drivers", 0), "可用" if g.get("jp2") else "不可用"))
if w:
    print("    后端 %d 个接口，装上的模块：%s" % (w.get("routes", 0), "、".join(w.get("modules") or [])))
if m:
    print("    子模块 %d 个可导入" % m.get("submodules", 0))
sp = c.get("speex") or {}
if sp.get("wanted"):
    print("    奥维录音解码（libspeex）%s" % ("可用" if sp.get("available") else "不可用：录音能导入，不能在窗口里播放"))
asr_c = c.get("asr") or {}
if asr_c.get("wanted"):
    print("    录音转文字引擎（sherpa-onnx）%s" % ("可用 " + asr_c.get("version", "") if asr_c.get("available")
                                             else "不可用：%s" % asr_c.get("error", "")))
for line in r.get("problems") or []:
    print("    × " + line)
print("    自检通过" if r.get("ok") else "    自检未通过，完整报告：" + sys.argv[1])
PY
else
  echo "    自检没写出报告，输出如下："
  cat "$WORK/selftest.log"
  SELFTEST_OK=0
fi

echo "==> 冒烟测试 2/2：真启动，看后端与页面是否起来"
HOME="$SANDBOX" "$APP/Contents/MacOS/LithoSpectraWeb" >"$WORK/run.log" 2>&1 &
PID=$!
STATE="$SANDBOX/Documents/LithoSpectra/state/web_session.json"
for _ in $(seq 1 30); do
  [ -f "$STATE" ] && break
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
if ! kill -0 "$PID" 2>/dev/null; then
  echo "    启动失败，输出如下："
  cat "$WORK/run.log"
  exit 1
fi
# 页面加载后会把界面状态写回后端：这个文件出现，说明窗口里的页面真的跑起来了
if [ -f "$STATE" ]; then
  echo "    启动正常，页面已连上后端"
else
  echo "    进程活着，但 30 秒内页面没有连上后端，输出如下："
  cat "$WORK/run.log"
  kill "$PID" 2>/dev/null || true
  exit 1
fi
kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true
if [ "$SELFTEST_OK" != "1" ]; then
  echo "    产物起得来但自检没过，见 $REPORT"
  exit 1
fi

echo "==> 打成 dmg"
hdiutil create -quiet -srcfolder "$APP" -volname "LithoSpectra" \
  -format UDZO -ov "$DIST/LithoSpectraWeb-$(uname -m).dmg"

echo
echo "完成："
du -sh "$APP" | sed 's/^/    /'
du -sh "$DIST"/*.dmg | sed 's/^/    /'
echo
echo "注意：产物未做代码签名与公证。分发给别人时，对方首次打开需要"
echo "      右键 → 打开，或在「系统设置 → 隐私与安全性」里放行。"
