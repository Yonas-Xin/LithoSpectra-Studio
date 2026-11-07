# -*- coding: utf-8 -*-
"""零碎但必须有的接口：起名符号表、底图密钥与缓存、模块装配、界面状态、关于。"""

import json
import os
from typing import Any, Dict, List

from fastapi import HTTPException
from pydantic import BaseModel

from samplelib import __version__, appstate, config
from samplelib import crs as C
from samplelib import geosymbols as G
from samplelib import module as M
from samplelib.annotate import tiles

#: 「最近用过」的符号最多几个，存在 state/symbols_recent.json
RECENT_SYMBOLS_MAX = 14

#: 界面的会话状态（视野、勾选、图层、面板开关）。「最近打开」另存在 session.json。
WEB_STATE_NAME = "web_session.json"


class RecentBody(BaseModel):
    items: List[str]


class KeyBody(BaseModel):
    name: str
    value: str


class StateBody(BaseModel):
    state: Dict[str, Any]


def symbols_recent_path() -> str:
    return os.path.join(config.app_data_dir(), "state", "symbols_recent.json")


def web_state_path() -> str:
    return os.path.join(os.path.dirname(appstate.state_path()), WEB_STATE_NAME)


def _write_json(path: str, doc) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def enabled_modules() -> List[str]:
    """这份产物装了哪些功能模块：包里有的 ∩ profile 要的。

    模块目录在、profile 选中了才算装上；没装的模块界面上
    不给入口，而不是给了入口再报"没装"。
    """
    found = M.discover()
    wanted = M.read_profile(config.MODULE_PROFILE)
    ids = list(found) if wanted is None else [m for m in wanted if m in found]
    return ids


def register(app, st, guard) -> None:
    # ------------------------------------------------------------- 起名符号
    @app.get("/api/geosymbols")
    def geosymbols():
        """起名对话框要的全部符号表与上下标映射。转换在前端做，规则照这里给的表。"""
        try:
            with open(symbols_recent_path(), "r", encoding="utf-8") as fh:
                recent = [str(x) for x in json.load(fh) if isinstance(x, str) and x]
        except Exception:
            recent = []
        return {
            "sub": G.SUBSCRIPT,
            "sup": G.SUPERSCRIPT,
            "wide": dict(G._WIDE),
            "ages": [{"name": name, "symbols": syms} for name, syms in G.age_symbols()],
            "age_pieces": list(G.AGE_PIECES),
            "tabs": [{"title": title, "symbols": list(syms)} for title, syms in G.TABS],
            "recent": recent[:RECENT_SYMBOLS_MAX],
        }

    @app.post("/api/geosymbols/recent")
    def save_recent(body: RecentBody):
        items = list(dict.fromkeys(x for x in body.items if x))[:RECENT_SYMBOLS_MAX]
        if st.remember:
            try:
                _write_json(symbols_recent_path(), items)
            except OSError:
                pass                     # 记不下就算了，不影响起名本身
        return items

    # ------------------------------------------------------------- 底图密钥与缓存
    @app.get("/api/basemap/keys")
    def basemap_keys():
        out = []
        for name, info in config.KEY_INFO.items():
            value = config.read_key(name)
            out.append({"name": name, "label": info.get("label", name),
                        "apply": info.get("apply", ""), "hint": info.get("hint", ""),
                        "prefix": info.get("prefix", ""),
                        "set": bool(value), "source": config.key_source(name),
                        "masked": (value[:4] + "…" + value[-4:]) if len(value) > 10 else ""})
        return {"keys": out, "file": config.key_file()}

    @app.post("/api/basemap/keys")
    def set_basemap_key(body: KeyBody):
        if body.name not in config.KEY_INFO:
            raise HTTPException(400, "未知的密钥：%s" % body.name)
        value = body.value.strip()
        prefix = config.KEY_INFO[body.name].get("prefix", "")
        if value and prefix and not value.startswith(prefix):
            raise HTTPException(400, "%s 的密钥应以 %s 开头。" % (
                config.KEY_INFO[body.name]["label"], prefix))
        if not st.remember:
            raise HTTPException(409, "测试模式下不写入密钥文件。")
        try:
            config.save_key(body.name, value)
        except Exception as exc:
            raise HTTPException(400, str(exc))
        st.fetcher.clear_failures()
        return basemap_keys()

    @app.get("/api/basemap/cache")
    def cache_info():
        return {"size_mb": round(st.fetcher.cache.size_mb(), 1),
                "limit_mb": config.TILE_CACHE_LIMIT_MB, "dir": config.TILE_CACHE_DIR}

    @app.post("/api/basemap/cache/clear")
    def cache_clear():
        st.fetcher.cache.clear()
        st.fetcher.clear_failures()
        return cache_info()

    @app.post("/api/basemap/retry")
    def retry_tiles():
        st.fetcher.clear_failures()
        return {"ok": True}

    # ------------------------------------------------------------- 模块与关于
    @app.get("/api/app")
    def app_info():
        from osgeo import gdal
        found = M.discover()
        enabled = enabled_modules()
        return {
            "title": config.APP_TITLE,
            "name": config.APP_NAME,
            "name_en": config.APP_NAME_EN,
            "version": __version__,
            "profile": config.MODULE_PROFILE,
            "modules": enabled,
            "module_info": [{"id": m, "name": found[m].name, "summary": found[m].summary}
                            for m in enabled],
            "format_version": config.LIBRARY_FORMAT_VERSION,
            "storage_epsg": config.STORAGE_EPSG,
            "gdal": gdal.__version__,
            "data_dir": config.app_data_dir(),
            "tile_sources": len(tiles.build_sources()),
        }

    # ------------------------------------------------------------- 显示坐标系
    # 只改"画布怎么显示、读数怎么写"，不动存储：样本库里一律是 EPSG:4326
    @app.get("/api/crs/menu")
    def crs_menu(lon: float = None, lat: float = None):
        return [{"epsg": code, "label": label, "recommended": rec}
                for code, label, rec in C.menu_entries(lon, lat)]

    @app.get("/api/crs/{epsg}")
    def crs_def(epsg: int):
        """前端拿 proj4 定义去注册投影，OpenLayers 才能在这个坐标系下显示。"""
        from osgeo import osr
        srs = osr.SpatialReference()
        try:
            if srs.ImportFromEPSG(int(epsg)) != 0:
                raise ValueError
        except Exception:
            raise HTTPException(400, "无法识别的 EPSG 代码：%s" % epsg)
        # 适用范围（经纬度）：OpenLayers 的经纬网、范围裁切要知道投影的边界
        area = None
        try:
            a = srs.GetAreaOfUse()
            if a is not None:
                area = [a.west_lon_degree, a.south_lat_degree, a.east_lon_degree, a.north_lat_degree]
        except Exception:
            area = None
        return {"epsg": int(epsg), "label": C.label_of(int(epsg)),
                "projected": bool(srs.IsProjected()), "proj4": srs.ExportToProj4(), "area": area}

    # ------------------------------------------------------------- 界面状态
    @app.get("/api/ui-state")
    def ui_state():
        try:
            with open(web_state_path(), "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            return doc if isinstance(doc, dict) else {}
        except Exception:
            return {}

    @app.post("/api/ui-state")
    def save_ui_state(body: StateBody):
        if not st.remember:
            return {"saved": False}
        try:
            _write_json(web_state_path(), body.state)
        except OSError:
            return {"saved": False}
        return {"saved": True}
