# -*- coding: utf-8 -*-
"""野外点位与光谱库条目的链接。

野外实测的光谱先经光谱模块导入用户光谱库（那里管仪器格式、波长单位、反射率量纲
的换算与分类），点位上只记光谱库条目的 id。一份谱只存一处，在光谱库里改了名称、
分类，点位这边看到的就是改过的。

光谱模块（``spectra``）不在这份发行版里时，链接功能整个不出现：:func:`available`
返回假，接口层据此不给入口。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np


class SpectrumLinkError(ValueError):
    pass


def _speclib():
    try:
        from ..spectra import speclib
        return speclib
    except Exception:
        return None


def available() -> bool:
    return _speclib() is not None


def _entry_json(e) -> Dict[str, Any]:
    speclib = _speclib()
    try:
        class_cn = " / ".join(speclib.category_name("/".join(e.cls.split("/")[:i + 1]))
                              for i in range(len(e.cls.split("/")))) if speclib and e.cls else ""
    except Exception:
        class_cn = ""
    return {"id": e.id, "name": e.name, "cn": getattr(e, "cn", "") or "", "class": e.cls,
            "class_cn": class_cn, "library": e.library,
            "source": e.source, "sample": e.sample, "instrument": e.instrument,
            "wl_lo": e.wl_lo, "wl_hi": e.wl_hi}


def entry_info(entry_id: str) -> Optional[Dict[str, Any]]:
    """条目的名称、分类等；光谱库里已经没有这条时返回 ``None``。"""
    speclib = _speclib()
    if speclib is None:
        return None
    entry = speclib.entry_by_id(entry_id)
    return _entry_json(entry) if entry is not None else None


def read_entry(entry_id: str) -> Dict[str, Any]:
    """条目的曲线：波长（nm）与反射率，无效值为 ``None``。"""
    speclib = _speclib()
    if speclib is None:
        raise SpectrumLinkError("关联光谱需要光谱模块，当前版本未安装")
    entry = speclib.entry_by_id(entry_id)
    if entry is None:
        raise SpectrumLinkError("光谱库中找不到条目 %s，可能已被删除" % entry_id)
    spec = speclib.spectrum_of(entry)
    if spec is None:
        raise SpectrumLinkError("无法读取光谱库条目 %s 的数据" % entry_id)
    x = np.asarray(spec.x, dtype=float)
    y = np.asarray(spec.y, dtype=float)
    order = np.argsort(x)
    x, y = x[order], y[order]
    out = _entry_json(entry)
    out["x"] = [round(float(v), 3) for v in x]
    out["y"] = [round(float(v), 6) if np.isfinite(v) else None for v in y]
    return out


def search(text: str = "", library: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    """按名称、样品号、来源等检索条目；``library`` 为 ``user`` 时只查用户光谱库。"""
    speclib = _speclib()
    if speclib is None:
        return []
    query = speclib.Query(text=text, library=library, limit=limit)
    return [_entry_json(e) for e in speclib.search(query)]
