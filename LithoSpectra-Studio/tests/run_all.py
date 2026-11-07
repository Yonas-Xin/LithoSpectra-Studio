# -*- coding: utf-8 -*-
"""跑完 tests/ 下的所有自测，逐个报结果。

直接运行本文件即可：``python tests/run_all.py``。每个自测都是独立脚本，用子进程
跑，一个挂了不影响后面的，最后汇总。
"""

# =============================================================================
# 配置
# =============================================================================

SUITES = [
    "test_fixes.py",          # 审查缺陷的回归测试
    "test_new_features.py",   # 类别排序 / 删除合并 / 唯一值赋色 / 颜色表
    "test_integrity.py",      # 合并不丢样本 / 撤销 / 过滤器泄漏 / 缓冲区同步 / 模块契约
    "test_legend.py",         # 岩性图例库
    "test_spectra.py",        # 取谱、绘图引擎、导出
    "test_speclib.py",        # 光谱库、检索、SG 平滑、光谱匹配
    "test_keys.py",           # 底图密钥：源码里没有、解析顺序、落盘权限
    "test_datadir.py",        # 数据目录：定位规则、环境变量覆盖、旧数据迁移与路径改写
    "test_edition.py",        # 删掉模块目录之后，后端还起不起得来
    "test_webapi.py",         # Web 界面后端：暂存编辑、撤销顺序、保存落库
    "test_fieldwork.py",      # 野外记录：奥维解析、导入、照片水印、光谱库链接、接口
    "test_deeplearn.py",      # 深度学习：任务管理、裁剪登记、训练、错分定位、实时预测图层
]
SHOW_OUTPUT = False           # 通过的用例也把详细输出打出来

# =============================================================================

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    failed = []
    for name in SUITES:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            print("!! 找不到 %s，跳过" % name)
            continue
        proc = subprocess.run([sys.executable, path], capture_output=True,
                              text=True)
        ok = proc.returncode == 0
        print("%s %s" % ("✓" if ok else "✗", name))
        if SHOW_OUTPUT or not ok:
            print(proc.stdout.rstrip())
            if proc.stderr.strip():
                print(proc.stderr.rstrip())
        if not ok:
            failed.append(name)
    print("\n%d/%d 通过" % (len(SUITES) - len(failed), len(SUITES)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
