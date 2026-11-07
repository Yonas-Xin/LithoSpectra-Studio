# Windows 打包流程

从一台什么都没装的 Windows 机器，到一个可以拷给别人的 `LithoSpectraWeb-win64.zip`。

全程只有前两步需要动手装东西，之后双击一个文件就行。

---

## 0. 先确认这些

| 项 | 要求 |
| --- | --- |
| 系统 | Windows 10 / 11，64 位 |
| 磁盘 | 空余 10 GB 以上（conda 环境约 3 GB，打包中间产物约 2 GB，产物与 zip 约 1 GB）。在 Mac 的 Parallels 虚拟机里打包时，直接在共享出来的仓库目录（如 `Y:\Litho-Intelligence-Platform`）里打，产物落在 Mac 仓库的 `packaging/dist-win64/` 下 |
| 网络 | 需要能访问 conda-forge、PyPI 与 npm。校园网走代理的话先把代理配好，见文末 |
| 权限 | 不需要管理员。装在自己的用户目录下即可 |
| Edge WebView2 | 程序窗口用它显示界面。Windows 11 自带；Windows 10 装过新版 Edge 的一般也有。没有的话到 <https://developer.microsoft.com/microsoft-edge/webview2/> 装 Evergreen 版 |

打包必须**在 Windows 上做**。PyInstaller 不能跨平台打包——在 macOS 上打出来的
是 `.app`，没有办法在 Mac 上产出 Windows 版。

---

## 1. 装 Miniconda（只做一次）

下载 <https://docs.conda.io/en/latest/miniconda.html> 的
**Miniconda3 Windows 64-bit**，双击安装。

- **Install for**：Just Me（不需要管理员权限）
- **安装路径**：默认的 `C:\Users\你的用户名\miniconda3` 就好，**路径里不要有中文
  和空格**——GDAL 的 DLL 加载对路径比较敏感
- **Advanced Options**：勾不勾 "Add to PATH" 都行，打包脚本会自己找到 conda
  （依次找 `CONDA_EXE`、`PATH`、Miniconda/Anaconda/miniforge 的常见安装位置）

装完不需要重启，也不需要手工建环境。

## 2. 装 Node.js（可选）

界面是 `web\` 下的 React 页面，打包前要编译成 `web\dist`。

- 装了 Node.js（<https://nodejs.org>，选 LTS 版），打包脚本每次都现编译，页面一定
  是最新的；
- 不想装的话，在 Mac 上 `cd web && npm run build`，把编译好的 `web\dist` 连同仓库
  一起拷过来，脚本会直接用它（并打印它是什么时候编译的）。

## 3. 拿到源码

把**整个仓库**拷到本机，路径同样**避免中文与空格**。不能只拷
`LithoSpectra-Studio\`：spec 还要从仓库根收 `toolbox\`（工具箱）、`workers\`
（去噪 Worker 与权重）和 `assets\*.txt`（传感器波长表），少了它们打包照样能过，
只是产物里工具箱用不了、ENVI 头缺波长时补不上。

```
D:\code\AircasGXT\
├── assets\                       GF5 / ZY1E / ZY1F 波长表
├── toolbox\                      预处理工具箱（随包以源码分发）
├── workers\                      去噪 Worker
└── LithoSpectra-Studio\
    ├── packaging\
    │   └── build_windows.bat      ← 待会儿双击的就是它
    ├── environment.yml
    ├── web\                       界面源码（编译出 web\dist）
    ├── webapi\
    └── samplelib\
```

## 4. 双击 `packaging\build_windows.bat`

在资源管理器里双击，不用开命令行、不用先激活环境。窗口里会打出
`[step 1/3]` 这样的进度：

1. **找 conda 并准备环境**。`samplelib` 环境不存在就直接
   `conda env create -f environment.yml` 建一个——这一步要下载 GDAL，
   **5 到 15 分钟**，进度条不动是正常的，别关窗口。环境已经有了就跳过；
   它若是早先建的、后来 `environment.yml` 又加了包，第 3 步查依赖会发现少东西并以
   退出码 3 返回，批处理看到就 `conda env update` 补齐一遍、再重跑一次。
2. **确认 pyinstaller**。没装就现装。
3. **打包**，交给 `build_windows.py`：
   1. 查 Python 版本与依赖（含 pywebview 在 Windows 上要的 pythonnet），看本机有没有 WebView2；
   2. 编译页面（或用已有的 `web\dist`）；
   3. 清理上次产物，跑 PyInstaller（几分钟，中间的告警可以忽略）；
   4. 检查 GDAL 的 DLL 与驱动插件有没有随包带上；
   5. `prune.py` 裁掉多收的文件；
   6. **查包里有没有无关的库**，并列出最大的十几项；
   7. **两步冒烟测试**，都在临时用户目录下跑，不碰本机已有的数据：先让产物不开窗口自检
      一遍（GDAL 驱动、proj.db、后端接口、页面、各功能模块、随包数据），再真启动，
      等窗口里的页面连上后端；
   8. 压 zip。

跑完窗口停在 `BUILD OK`，按任意键关闭。第一次从零跑通大约 **20 分钟**，
之后环境已在，重打一次约 **5 分钟**。

## 5. 产物

```
LithoSpectra-Studio\packaging\dist-win64\
├── LithoSpectraWeb\              ← 分发单位，整个目录
│   ├── LithoSpectraWeb.exe       ← 双击启动
│   ├── lib\                      ← Python、GDAL、页面等程序文件，不能删
│   ├── resources\                ← 随包资源：图标与波长表、光谱库、岩性图例、工具箱、Worker
│   └── userdata\                 ← 数据目录，首次启动时生成，产物里没有
└── LithoSpectraWeb-win64.zip     ← 拷给别人就发这个
```

`lib` 的名字由 spec 的 `CONTENTS_DIR` 决定（PyInstaller 默认为 `_internal`）。
`resources` 由 PyInstaller 先收进 `lib\resources\`，spec 在打包完成后把它移到 exe 同级。

`packaging\build-win64\` 下也有一个同名 exe，那是 PyInstaller 的中间产物，旁边没有
`lib`，双击只会报 `Failed to load python3xx.dll`。要启动的始终是
`dist-win64\` 下的那个。

打的是 onedir 不是单文件 exe：单文件每次启动都要把几百兆解到临时目录，冷启动
十几秒，GDAL 在解压路径上探测驱动还常出岔子。所以**只把 exe 单独拷出来是跑不起来
的**，必须整个目录一起。

拿到 zip 的人解压后双击 exe 即可，对方机器上**不需要装 Python、conda、GDAL 或
Node.js**，只需要 Edge WebView2。

---

## 包里带什么、不带什么

只带程序真正用到的：Python 运行时、GDAL / PROJ 及其数据、numpy、Pillow、
matplotlib（光谱出图）、FastAPI / uvicorn（进程内接口）、pywebview + pythonnet
（窗口）、h5py（裁剪样本块的 h5 输出），以及装了野外记录模块时的 onnxruntime、
OpenCV、piexif。

以下即便构建环境里装着也不进包，由 `packaging\LithoSpectraWeb.spec` 的 `excludes`
排掉：

- Qt / Tk 等桌面界面库、pywebview 的其它平台后端（Cocoa、GTK、Qt、CEF、IE 内核）；
- PyTorch、SciPy、SymPy、scikit-image、scikit-learn、pandas、IPython / Jupyter 等；
- onnxruntime 的模型转换与量化工具、macOS 专用的文字识别库。

`prune.py` 再删掉 PyInstaller 顺手收进来的 Tcl/Tk 运行时、重复的一份 GDAL 数据、
OpenCV 的视频插件（`opencv_videoio_ffmpeg*.dll`）、IE 内核的 32 位互操作库。

打包脚本最后会按 `build_windows.py` 里的 `FORBIDDEN` 清单把产物扫一遍：发现 Qt、
PyTorch、SciPy 之类就判失败，不会把一个虚胖的包交出去。

---

## 常见问题

**窗口一闪而过看不到报错。**
不会——脚本最后有 `pause`。如果真的一闪而过，说明 `cmd.exe` 没能读完批处理，
多半是文件被某个编辑器改成了 LF 换行或混进了中文。仓库里它是纯 ASCII + CRLF
（`.gitattributes` 已约定），用 Git 正常检出就不会有这个问题。

**`PermissionError: [WinError 5] 拒绝访问`。**
多半是旧产物还被占用：上次启动的 `LithoSpectraWeb.exe` 或它的 WebView2 子进程没有退出。
在任务管理器里结束它们再打。Windows 的产物放在 `packaging\dist-win64\` 与
`packaging\build-win64\`，不和 Mac 的 `dist-web\`、`build-web\` 混在一起（Mac 产物里的
符号链接 Windows 删不掉）；旧产物删不干净时脚本会在开头就停下并列出是哪些文件。

**卡在 `conda env create` / `npm ci` 很久，或下载失败。**
换国内镜像后重来：

```cmd
conda config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge
conda config --set show_channel_urls yes
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
npm config set registry https://registry.npmmirror.com
```

**提示 `no python on PATH`。**
Miniconda 没装好，或者装成了 "All Users" 而当前用户读不到。重装一遍，选 Just Me。

**「没有 web\dist，本机也没有 Node.js」。**
按第 2 步二选一：装 Node.js，或者把在 Mac 上编译好的 `web\dist` 拷过来。

**「包里带进了程序用不到的库」。**
产物里出现了 `FORBIDDEN` 清单上的东西，脚本列出了具体路径。说明某个库的可选 import
把它拉了进来。把它的包名加进 `LithoSpectraWeb.spec` 的 `excludes` 重打；确实要用的话，
从 `build_windows.py` 的 `FORBIDDEN` 里拿掉那一项。

**提示 MKL。**
环境里的 numpy 来自 Anaconda 默认频道，带着几百兆的 Intel MKL。能用，只是包大。
用 `environment.yml` 建的环境走 conda-forge（OpenBLAS），不会有这一条。

**别人电脑上报 `No module named '_gdal'`。**
对方电脑上装过 QGIS、ArcGIS、OSGeo4W 之类，PATH 里有一份别的版本的 GDAL。GDAL 的
`osgeo/__init__.py` 会顺着 PATH 找第一个放着 `gdal*.dll` 的目录加进 DLL 搜索路径，
`_gdal.pyd` 于是可能拿到那份 `gdal.dll`，版本对不上，加载失败（真实错误是
`DLL load failed while importing _gdal: 找不到指定的程序`），GDAL 再把它改报成
`No module named '_gdal'`。`runtime_hook_gdal.py` 在启动时把包内目录放到 PATH 最前面，
它找到的第一份就是自己的。冒烟测试会去掉构建环境的目录、再在 PATH 最前面放一份冒名
的 `gdal.dll` 来启动产物，这类问题在打包机上就会暴露。

**双击产物弹出 `Unhandled exception in script`，或者窗口没出来。**
程序的输出写在 exe 旁的 `userdata\state\last_run.log`（双击启动
时没有控制台，输出都落在这里），报错的完整堆栈在里面。冒烟测试第 2 步也是用同样
的方式启动的，打包机上出问题时脚本会把这个文件的内容打出来。

**在 Mac 的 Parallels 虚拟机里打包。**
可以。Apple 芯片上的 Windows 是 ARM64 版，conda 环境与产物都是 x64 的，靠系统自带
的转译运行，打出来的 exe 在普通 x64 电脑上照样能用。pywebview 按**系统**架构加载
WebView2 的加载库，所以 `prune.py` 不删 `win-arm64` / `win-x86` 那几份，删了在 ARM
电脑上窗口就开不起来。直接在 Parallels 共享出来的 Mac 仓库目录（Y: 之类）里打包，
不要把仓库另拷一份到虚拟机的本机磁盘。

**冒烟测试第 2 步：「进程活着，但页面没有连上后端」。**
最常见的是打包机上没有 Edge WebView2 运行时，窗口开不起来——脚本会顺带说明。装上
WebView2 再打即可；产物本身没问题，只是在这台机器上验证不了。

**自检没过，窗口里有一行 `× xxx：……`。**
产物起得来，但少了东西：

- `gdal：缺驱动：…` / `EPSG:4326→3857 结果不对` —— GDAL 数据或 `proj.db` 没进包。
  看 `%CONDA_PREFIX%\Library\share\gdal` 与 `Library\share\proj` 在不在，
  不在说明环境本身装残了，按下面"重建环境"处理。
- `module_imports：samplelib.modules.xxx：No module named 'yyy'` —— spec 的排除清单
  排掉了某个运行时才 import 的模块。把 `yyy` 从 `LithoSpectraWeb.spec` 的
  `excludes` 里拿掉重打。
- `web_shell：编译好的页面没带上` —— `web\dist` 不完整，重新编译页面。
- `web_shell：野外记录的修补库没带上：…` —— onnxruntime 或 OpenCV 没进包，
  确认环境里 `import onnxruntime, cv2` 能过。

完整报告在 `packaging\build-win64\selftest.json`，`tracebacks` 一节有每项失败的
完整堆栈。也可以对装到别人机器上的产物手工跑一遍自检：

```cmd
set LITHOSPECTRA_SELFTEST=%TEMP%\lithospectra-report.json
LithoSpectraWeb.exe
notepad %TEMP%\lithospectra-report.json
```

exe 不弹窗口，跑完就退出，报告落在那个路径。

**自检报 JPEG2000 不可用。**
conda-forge 把 JPEG2000 驱动拆成了独立包 `libgdal-jp2openjpeg`，环境里没装它，
产物就没有。按清单补一遍再重打：

```cmd
conda env update -n samplelib -f environment.yml
```

**冒烟测试失败，日志里是 `ImportError: DLL load failed`。**
先在命令行里确认环境本身是好的——打包只是把环境里的东西搬进一个目录，
环境里跑不起来的，打出来一样跑不起来：

```cmd
conda activate samplelib
python -c "from osgeo import gdal; print(gdal.__version__)"
python -c "import clr, webview; print('webview ok')"
```

有报错的，是环境的问题。把环境删掉重建：

```cmd
conda deactivate
conda env remove -n samplelib
conda env create -f environment.yml
```

**必须用 `environment.yml` 建**，不要图省事 `conda create -n samplelib python`：
后者拿到的是当时最新的 Python，再 pip 装一堆包，常见的结果是 DLL 版本对不上、
`DLL load failed`。`environment.yml` 把 Python 钉在 3.11、频道钉在 conda-forge，
就是为了避开这类错配。`build_windows.py` 开头会比对当前解释器与
`environment.yml` 的 `python=` 钉子，对不上直接停下来。

**想用别的环境名。**
改 `build_windows.bat` 配置区的 `ENV_NAME`，或者在命令行里
`build_windows.bat 我的环境名`。

**想跳过裁剪 / 冒烟测试 / 打 zip。**
改 `build_windows.py` 顶部配置区的 `DO_PRUNE`、`DO_SMOKE_TEST`、`MAKE_ZIP`。
反复调试时把 `MAKE_ZIP` 关掉能省一两分钟。`DO_SMOKE_TEST` 别关：它是唯一一道
能在打包机上抓到"少了东西"的检查。

---

## 打包后数据存在哪

数据目录是 exe 旁的 `userdata\`，整个程序目录拷到别处，数据随之带走：

```
LithoSpectraWeb\userdata\
├── data\
│   ├── backups\                还原前自动留的快照
│   ├── tilecache\              底图瓦片缓存，删掉即清空
│   ├── rastercache\            大影像的显示金字塔，删掉即清空、下次重建
│   └── models\                 野外记录去水印用的修补模型，用到时下载
├── dl\                         深度学习的任务、样本集登记与骨干插件
├── state\                      最近打开的样本库、界面状态、运行日志 last_run.log
├── my_spectra.slb              我的光谱
├── keys.json                   底图密钥
└── wavelengths\                自备的传感器波长表（.txt）
```

exe 所在目录不可写时（例如放在 `C:\Program Files` 下），数据目录改为
`%USERPROFILE%\Documents\LithoSpectra\`。

旧版本的数据在 `%USERPROFILE%\Documents\LithoSpectra\`。新版首次启动、`userdata\` 还是空的
时候，会把旧目录里的内容移到 `userdata\`，并把状态文件、深度学习登记表与任务文件中
指向旧目录的路径改为新位置。瓦片缓存与栅格缓存不移动，留在旧目录，可以手动删除。
迁移结果记在 `userdata\state\last_run.log` 的开头。

升级到新版本时，把新产物解压到原目录覆盖即可，`userdata\` 不受影响；如果换到新目录，
把旧目录下的 `userdata\` 一起拷过去。分发版不带样本库，首次启动打开空界面，由使用者
新建或打开自己的库。

野外记录里自动识别并去除奥维水印要靠 macOS 自带的 Vision 文字识别，Windows 上
没有这一项；照片浏览器里框选区域修补（同样可以抹掉水印）与地质水印照常可用，
onnxruntime 与 OpenCV 就是给它们用的。
