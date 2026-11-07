# -*- coding: utf-8 -*-
"""生成岩性图例库 ``samplelib/data/lithology_legend.json``。

它是什么
--------
一张「岩性名称 -> 标准岩性类 -> 颜色 + 花纹号」的对照表。导入岩性矢量、按属性
分类时，可以拿属性值（"灰色中细粒黑云母二长花岗岩"这种）直接查出该用什么颜色，
而不是随便分一个调色板颜色。

数据从哪来
----------
* **IUGS-CGI SimpleLithology**（CC-BY）——岩性分类的骨架。265 个概念，带
  ``skos:broader`` 层级、英文定义，以及 100 个概念的中文标签。这是国际地科联
  下属机构维护的受控词表，GeoSciML / INSPIRE 都用它，拿它当主键最稳。
* **Macrostrat 岩性表**（CC-BY 4.0）——颜色与 FGDC 花纹号。它的岩性名基本沿用
  英文习惯叫法，和 CGI 直接同名的有 74 个；其余顺着 CGI 的层级往上找最近的有色
  祖先——"斑状二长花岗岩"本来就该长得像花岗岩。
* **Macrostrat / ICS 国际年代地层表**（CC-BY 4.0）——地层时代的官方配色
  （CGMW 色标），给"按时代赋色"用。

**GB/T 958-2015《区域地质图图例》与 DZ/T 0179-1997《地质图用色标准及色标》没有
公开的机器可读版本**，也不能在这里复制它的色值。本脚本因此不包含 GB/T 958 的
颜色与花纹；若你手上有标准的电子版，把它整理成 ``GBT958_OVERRIDE`` 那样的
``{岩性id: {"color": ..., "pattern": ...}}`` 表放进来，生成时会覆盖上面的默认
配色，其余逻辑（名称匹配、层级继承）不用改。

怎么用
------
直接运行即可（VS Code 里点运行就行）。缺数据文件时自动下载，已有就复用。
"""

# =============================================================================
# 配置
# =============================================================================

import os

# 源数据缓存目录。删掉即可强制重新下载。
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_legend_cache")

# 输出文件
OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "samplelib", "data", "lithology_legend.json")

# 源数据地址
SOURCES = {
    "simplelithology.ttl":
        "https://raw.githubusercontent.com/CGI-IUGS/cgi-vocabs/master/"
        "vocabularies/geosciml/simplelithology.ttl",
    "macrostrat_lithologies.json":
        "https://macrostrat.org/api/defs/lithologies?all",
    "macrostrat_intervals.json":
        "https://macrostrat.org/api/defs/intervals?timescale=international%20ages",
}

DOWNLOAD_TIMEOUT = 60

# 三大类的兜底配色：顺着层级也找不到颜色时用它，总比没有强
CLASS_FALLBACK_COLOR = {
    "igneous": "#F0544A",
    "sedimentary": "#F2E394",
    "metamorphic": "#9E7BB5",
    "unconsolidated": "#E8D9B0",
    "other": "#BFBFBF",
}

# GB/T 958-2015 色值/花纹覆盖表。标准不公开，这里留空；有电子版的话按
# {"granite": {"color": "#RRGGBB", "pattern": "花纹号"}} 补进来即可。
GBT958_OVERRIDE = {}

# =============================================================================

import json
import re
import sys
import urllib.request
from datetime import date

# 中文名补充表：CGI 只给了 100 个概念的中文标签，而且缺的正好是中国区调里最
# 常用的那些。下面这些是《岩石学》教材与区调报告里的通用叫法，key 是 CGI 概念
# id，value 是中文名列表，第一个作为主名。
#
# CGI 词表里没有对应概念的（辉绿岩、糜棱岩、金伯利岩…），挂到语义上最接近的
# 上位概念，宁可粗一级也不要错一类——图例的用途是"一眼看出这是哪类岩石"。
ZH_SUPPLEMENT = {
    # ---- 沉积岩
    "clastic_conglomerate": ["砾岩", "复成分砾岩", "砂砾岩"],
    "generic_conglomerate": ["砾岩"],
    "breccia": ["角砾岩"],
    "clastic_sandstone": ["砂岩", "长石砂岩", "岩屑砂岩"],
    "generic_sandstone": ["砂岩"],
    "arenite": ["净砂岩", "石英砂岩"],
    "wacke": ["杂砂岩"],
    "siltstone": ["粉砂岩"],
    "clastic_mudstone": ["泥岩"],
    "generic_mudstone": ["泥岩"],
    "claystone": ["粘土岩", "黏土岩"],
    "shale": ["页岩", "泥页岩"],
    "limestone": ["灰岩", "石灰岩", "生物碎屑灰岩", "泥晶灰岩"],
    "impure_limestone": ["泥质灰岩"],
    "dolostone": ["白云岩"],
    "impure_dolostone": ["泥质白云岩"],
    "carbonate_mudstone": ["泥晶灰岩"],
    "chalk": ["白垩"],
    "travertine": ["钙华"],
    "impure_carbonate_sedimentary_rock": ["泥灰岩"],
    "non_clastic_siliceous_sedimentary_rock": ["硅质岩", "燧石岩", "硅质页岩"],
    "phosphorite": ["磷块岩"],
    "iron_rich_sedimentary_rock": ["铁质岩", "含铁建造"],
    "bauxite": ["铝土矿", "铝土岩"],
    "coal": ["煤", "煤层"],
    "bituminous_coal": ["烟煤"],
    "anthracite_coal": ["无烟煤"],
    "lignite": ["褐煤"],
    "peat": ["泥炭"],
    "rock_salt": ["岩盐"],
    "rock_gypsum_or_anhydrite": ["石膏", "硬石膏"],
    "evaporite": ["蒸发岩", "盐岩"],
    "diamictite": ["混积岩", "冰碛岩"],
    "sedimentary_rock": ["沉积岩"],
    # ---- 侵入岩
    "granite": ["花岗岩"],
    "monzogranite": ["二长花岗岩"],
    "syenogranite": ["正长花岗岩"],
    "alkali_feldspar_granite": ["碱长花岗岩", "钾长花岗岩"],
    "granodiorite": ["花岗闪长岩"],
    "tonalite": ["英云闪长岩", "奥长花岗岩"],
    "granitoid": ["花岗质岩", "花岗岩类"],
    "diorite": ["闪长岩"],
    "quartz_diorite": ["石英闪长岩"],
    "monzonite": ["二长岩"],
    "quartz_monzonite": ["石英二长岩"],
    "monzodiorite": ["二长闪长岩"],
    "syenite": ["正长岩"],
    "quartz_syenite": ["石英正长岩"],
    "foid_syenite": ["霞石正长岩", "似长石正长岩"],
    "gabbro": ["辉长岩"],
    "quartz_gabbro": ["石英辉长岩"],
    "monzogabbro": ["二长辉长岩"],
    "doleritic_rock": ["辉绿岩", "煌斑岩"],
    "anorthosite": ["斜长岩"],
    "peridotite": ["橄榄岩", "纯橄岩", "方辉橄榄岩", "二辉橄榄岩"],
    "pyroxenite": ["辉石岩"],
    "hornblendite": ["角闪石岩"],
    "ultramafic_igneous_rock": ["超镁铁岩", "超基性岩"],
    "ultrabasic_igneous_rock": ["超基性岩"],
    "pegmatite": ["伟晶岩", "花岗伟晶岩"],
    "aplite": ["细晶岩"],
    "porphyry": ["斑岩", "花岗斑岩", "石英斑岩"],
    "carbonatite": ["碳酸岩"],
    "exotic_composition_igneous_rock": ["金伯利岩", "钾镁煌斑岩"],
    "phaneritic_igneous_rock": ["深成岩", "侵入岩"],
    "igneous_rock": ["岩浆岩", "火成岩"],
    # ---- 火山岩
    "basalt": ["玄武岩"],
    "tholeiitic_basalt": ["拉斑玄武岩"],
    "alkali-olivine_basalt": ["碱性橄榄玄武岩"],
    "spilite": ["细碧岩"],
    "andesite": ["安山岩"],
    "dacite": ["英安岩"],
    "rhyolite": ["流纹岩"],
    "rhyolitoid": ["流纹质岩"],
    "trachyte": ["粗面岩"],
    "latite": ["粗安岩", "二长斑岩"],
    "phonolilte": ["响岩"],
    "tephrite": ["碱玄岩"],
    "basanite": ["碱玄玄武岩"],
    "foidite": ["似长岩", "白榴岩"],
    "boninite": ["玻安岩"],
    "komatiitic_rock": ["科马提岩"],
    "fine_grained_igneous_rock": ["喷出岩", "火山熔岩"],
    "glassy_igneous_rock": ["玻璃质岩", "黑曜岩", "珍珠岩", "浮岩", "松脂岩"],
    "pyroclastic_rock": ["火山碎屑岩"],
    "ash_tuff_lapillistone_and_lapilli_tuff": ["凝灰岩", "晶屑凝灰岩", "岩屑凝灰岩"],
    "tuff_breccia_agglomerate_or_pyroclastic_breccia": ["火山角砾岩", "集块岩"],
    "tuffite": ["沉凝灰岩"],
    "tephra": ["火山碎屑物"],
    # ---- 变质岩
    "gneiss": ["片麻岩", "黑云斜长片麻岩"],
    "orthogneiss": ["正片麻岩"],
    "paragneiss": ["副片麻岩"],
    "schist": ["片岩"],
    "mica_schist": ["云母片岩", "黑云母片岩", "二云母片岩"],
    "phyllite": ["千枚岩"],
    "slate": ["板岩", "炭质板岩"],
    "marble": ["大理岩", "白云质大理岩"],
    "quartzite": ["石英岩"],
    "amphibolite": ["角闪岩", "斜长角闪岩"],
    "granulite": ["麻粒岩"],
    "eclogite": ["榴辉岩"],
    "serpentinite": ["蛇纹岩"],
    "skarn": ["矽卡岩", "夕卡岩"],
    "migmatite": ["混合岩", "混合片麻岩"],
    "hornfels": ["角岩", "角页岩"],
    "granofels": ["变粒岩", "浅粒岩"],
    "mylonitic_rock": ["糜棱岩", "初糜棱岩", "超糜棱岩"],
    "cataclasite_series": ["碎裂岩", "构造角砾岩"],
    "phyllonite": ["千糜岩"],
    "breccia_gouge_series": ["断层角砾岩", "断层泥"],
    "fault_related_material": ["构造岩"],
    "metasomatic_rock": ["交代岩", "蚀变岩"],
    "chlorite_actinolite_epidote_metamorphic_rock": ["绿片岩", "绿泥石片岩"],
    "glaucophane_lawsonite_epidote_metamorphic_rock": ["蓝片岩", "蓝闪石片岩"],
    "foliated_metamorphic_rock": ["片理化岩石"],
    "metamorphic_rock": ["变质岩"],
    # ---- 松散堆积
    "gravel": ["砾石", "砾"],
    "sand": ["砂"],
    "silt": ["粉砂"],
    "mud": ["淤泥"],
    "clay": ["粘土", "黏土"],
    "unconsolidated_material": ["松散堆积物", "第四系松散层"],
    "natural_unconsolidated_material": ["冲积物", "洪积物", "坡积物", "残积物",
                                        "冲洪积物", "风积物", "黄土"],
    "residual_material": ["残积物", "红土", "风化壳"],
    "anthropogenic_material": ["人工堆积", "填土"],
}

# 修饰词。匹配不上时按这些词逐层剥，剥完再试一次。
# 顺序无所谓，剥的时候按长度从长到短，免得"中细粒"被"细粒"先咬掉一半。
MODIFIERS = {
    "颜色": ["灰白色", "灰黑色", "灰绿色", "浅灰色", "深灰色", "紫红色", "褐黄色",
             "灰白", "灰黑", "灰绿", "浅灰", "深灰", "紫红", "褐红", "褐黄",
             "黄绿", "青灰", "肉红色", "肉红", "杂色", "浅色", "暗色",
             "红色", "灰色", "白色", "黑色", "绿色", "黄色", "紫色", "褐色"],
    "粒度结构": ["细粒", "中细粒", "中粗粒", "粗粒", "中粒", "微粒", "巨粒",
                 "不等粒", "似斑状", "斑状", "隐晶质", "显晶质", "玻璃质",
                 "块状", "片状", "条带状", "眼球状", "角砾状", "杏仁状",
                 "气孔状", "枕状", "碎裂", "糜棱"],
    "成分前缀": ["黑云母", "白云母", "二云母", "角闪石", "辉石", "橄榄石",
                 "石榴子石", "石榴石", "绿泥石", "绢云母", "阳起石", "透闪石",
                 "红柱石", "十字石", "蓝晶石", "矽线石", "夕线石", "堇青石",
                 "黑云", "二云", "白云", "角闪", "斜长", "钾长", "石英",
                 "含砾", "含泥", "含钙", "含碳", "含铁", "含硅", "含煤",
                 "泥质", "钙质", "硅质", "铁质", "碳质", "炭质", "凝灰质",
                 "白云质", "石膏质", "磷质", "长石质", "岩屑"],
    "蚀变变质": ["强蚀变", "弱蚀变", "蚀变", "硅化", "绿泥石化", "绿帘石化",
                 "绢云母化", "碳酸盐化", "高岭土化", "钾化", "黄铁矿化",
                 "混合岩化", "浅变质", "变质", "变余", "变"],
}


# CGI 概念 -> Macrostrat 岩性名。只列自动规则对不上、但视觉归属很明确的那些；
# 值必须是 Macrostrat 岩性表里真实存在的名字。
NAME_ALIAS = {
    "phonolilte": "phonolite",                 # CGI 词表里的拼写错误
    "clastic_conglomerate": "conglomerate",
    "generic_conglomerate": "conglomerate",
    "ash_tuff_lapillistone_and_lapilli_tuff": "tuff",
    "tuff_breccia_agglomerate_or_pyroclastic_breccia": "agglomerate",
    "ash_and_lapilli": "tephra",
    "ash_breccia_bomb_or_block_tephra": "tephra",
    "rock_gypsum_or_anhydrite": "gypsum",
    "rock_salt": "halite",
    "non_clastic_siliceous_sedimentary_rock": "chert",
    "non_clastic_siliceous_sediment": "chert",
    "biogenic_silica_sedimentary_rock": "diatomite",
    "iron_rich_sedimentary_rock": "iron formation",
    "iron_rich_sediment": "iron formation",
    "phosphate_rich_sedimentary_material": "phosphorite",
    "phosphate_rich_sediment": "phosphorite",
    "mica_schist": "schist",
    "chlorite_actinolite_epidote_metamorphic_rock": "greenschist",
    "glaucophane_lawsonite_epidote_metamorphic_rock": "blueschist",
    "breccia_gouge_series": "fault breccia",
    "fault_related_material": "cataclasite",
    "exotic_composition_igneous_rock": "kimberlite",
    "glassy_igneous_rock": "volcanic glass",
    "glass_rich_igneous_rock": "volcanic glass",
    "fine_grained_igneous_rock": "volcanic",
    "phaneritic_igneous_rock": "plutonic",
    "fragmental_igneous_rock": "volcaniclastic",
    "fragmental_igneous_material": "volcaniclastic",
    "pyroclastic_rock": "volcaniclastic",
    "pyroclastic_material": "volcaniclastic",
    "unconsolidated_material": "regolith",
    "natural_unconsolidated_material": "regolith",
    "material_formed_in_surficial_environment": "regolith",
    "residual_material": "laterite",
    "duricrust": "laterite",
    "carbonate_mudstone": "lime mudstone",
    "pure_carbonate_mudstone": "lime mudstone",
    "crystalline_carbonate": "carbonate",
    "carbonate_sedimentary_rock": "carbonate",
    "carbonate_sedimentary_material": "carbonate",
    "calcareous_carbonate_sedimentary_rock": "limestone",
    "impure_carbonate_sedimentary_rock": "marl",
    "dolomitic_or_magnesian_sedimentary_rock": "dolostone",
    "dolomitic_or_magnesian_sedimentary_material": "dolostone",
    "dolomitic_sediment": "dolostone",
    "impure_dolostone": "dolostone",
    "granofels": "granofel",
    "sedimentary_rock": "sedimentary",
    "sedimentary_material": "sedimentary",
    "clastic_sedimentary_rock": "siliciclastic",
    "clastic_sedimentary_material": "siliciclastic",
    "clastic_sediment": "siliciclastic",
    "hybrid_sedimentary_rock": "mixed carbonate-siliciclastic",
    "hybrid_sediment": "mixed carbonate-siliciclastic",
    "igneous_rock": "igneous",
    "igneous_material": "igneous",
    "metamorphic_rock": "metamorphic",
    "foliated_metamorphic_rock": "schist",
    "metasomatic_rock": "skarn",
    "ultramafic_igneous_rock": "peridotite",
    "ultrabasic_igneous_rock": "peridotite",
    "acidic_igneous_rock": "felsite",
    "acidic_igneous_material": "felsite",
    "basic_igneous_rock": "mafic",
    "basic_igneous_material": "mafic",
    "high_magnesium_fine_grained_igneous_rocks": "komatiite",
    "komatiitic_rock": "komatiite",
    "doleritic_rock": "dolerite",
    "anthracite_coal": "anthracite",
    "bituminous_coal": "coal",
    "organic_rich_sedimentary_rock": "coal",
    "silicate_mudstone": "mudstone",
    "silicate_mud": "mud",
    "organic_bearing_mudstone": "shale",
    "sapropel": "gyttja",
    "carbonate_ooze": "calcareous ooze",
    "siliceous_ooze": "siliceous ooze",
    "anthropogenic_unconsolidated_material": "drift",
    "gravel_size_sediment": "gravel",
    "sand_size_sediment": "sand",
    "mud_size_sediment": "mud",
    "boulder_gravel_size_sediment": "gravel",
    "cobble_gravel_size_sediment": "gravel",
    "pebble_gravel_size_sediment": "gravel",
}

# id -> Macrostrat 名的自动规则：这些前后缀在 CGI 里是分类学限定词，
# Macrostrat 那边没有对应词条，去掉之后往往就对上了。
_DROP_PREFIX = ("clastic ", "generic ", "pure ", "impure ", "natural ",
                "calcareous ", "composite genesis ")
_DROP_SUFFIX = (" rock", " material", " series", " sediment", " rocks")
_IC_SUFFIX = {"itic": "ite", "otic": "ote", "ytic": "yte", "oic": "o",
              "atic": "ate", "onitic": "onite"}


def _candidates(cid: str, name_en: str):
    """一个 CGI 概念可能对应的 Macrostrat 岩性名，按可信度从高到低。"""
    seen = []

    def add(v):
        v = " ".join(v.split()).lower()
        if v and v not in seen:
            seen.append(v)

    add(cid.replace("_", " "))
    add(name_en)
    for base in (cid.replace("_", " ").lower(), name_en.lower()):
        cur = base
        for suf in _DROP_SUFFIX:
            if cur.endswith(suf):
                cur = cur[:-len(suf)]
        for pre in _DROP_PREFIX:
            if cur.startswith(pre):
                cur = cur[len(pre):]
        add(cur)
        # "doleritic rock" -> "dolerite"、"trachytic rock" -> "trachyte"
        head = cur.split()[-1] if cur.split() else ""
        for bad, good in _IC_SUFFIX.items():
            if head.endswith(bad):
                add(" ".join(cur.split()[:-1] + [head[:-len(bad)] + good]))
        if head.endswith("oid"):
            add(" ".join(cur.split()[:-1] + [head[:-3] + "e"]))
        add(head)
    return seen


def fetch(name: str, url: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, name)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    print("下载 %s ..." % name)
    req = urllib.request.Request(url, headers={"User-Agent": "samplelib-legend/1.0"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        data = resp.read()
    with open(path, "wb") as fh:
        fh.write(data)
    return path


_CONCEPT_RE = re.compile(
    r"<http://resource\.geosciml\.org/classifier/cgi/lithology/([^>\s]+)>\s+a\s+skos:Concept")


def parse_cgi(path: str) -> dict:
    """解析 SKOS 词表，返回 {id: {...}}。

    只用正则不上 rdflib：这份 ttl 的写法很规整，而为了一个离线生成脚本引入
    一个重量级依赖不划算——真出问题也是这一个文件的事。
    """
    text = open(path, encoding="utf-8").read()
    blocks = re.split(r"\n(?=<http://resource\.geosciml\.org/classifier/cgi/lithology/)",
                      text)
    out = {}
    for block in blocks:
        m = _CONCEPT_RE.match(block)
        if not m:
            continue
        cid = m.group(1)
        en = re.findall(r'"((?:[^"\\]|\\.)*)"@en', block)
        # prefLabel/definition/example 都是 @en，靠所在行区分
        pref = ""
        for line in block.splitlines():
            if "skos:prefLabel" in line or (pref == "" and '"@en' in line
                                            and "definition" not in line
                                            and "provenance" not in line
                                            and "example" not in line
                                            and "changeNote" not in line
                                            and "editorialNote" not in line):
                mm = re.search(r'"((?:[^"\\]|\\.)*)"@en', line)
                if mm and "skos:prefLabel" in line:
                    pref = mm.group(1)
                    break
        if not pref:
            pref = cid.replace("_", " ")
        defn = ""
        mm = re.search(r'skos:definition\s+"((?:[^"\\]|\\.)*)"@en', block)
        if mm:
            defn = mm.group(1).replace('\\"', '"')
        broader = re.findall(
            r"skos:broader\s+<http://resource\.geosciml\.org/classifier/cgi/lithology/([^>]+)>",
            block)
        zh = re.findall(r'"([^"]+)"@zh', block)
        out[cid] = {
            "id": cid,
            "name_en": pref,
            "broader": broader[0] if broader else None,
            "definition": defn,
            "zh": list(dict.fromkeys(zh)),
        }
    return out


ROOT_CLASS = {
    "igneous_material": "igneous",
    "igneous_rock": "igneous",
    "sedimentary_material": "sedimentary",
    "sedimentary_rock": "sedimentary",
    "sediment": "sedimentary",
    "metamorphic_rock": "metamorphic",
    "unconsolidated_material": "unconsolidated",
    "natural_unconsolidated_material": "unconsolidated",
    "anthropogenic_material": "unconsolidated",
}


def rock_class(cid: str, concepts: dict) -> str:
    """顺着 broader 往上走，落到哪个根就算哪一类。"""
    seen = set()
    cur = cid
    while cur and cur not in seen:
        seen.add(cur)
        if cur in ROOT_CLASS:
            return ROOT_CLASS[cur]
        node = concepts.get(cur)
        cur = node["broader"] if node else None
    return "other"


def inherit(cid: str, concepts: dict, table: dict):
    """自身没有就顺着 broader 往上找最近的有值祖先。"""
    seen = set()
    cur = cid
    while cur and cur not in seen:
        seen.add(cur)
        if cur in table:
            return table[cur], cur
        node = concepts.get(cur)
        cur = node["broader"] if node else None
    return None, None


def main() -> None:
    paths = {name: fetch(name, url) for name, url in SOURCES.items()}
    concepts = parse_cgi(paths["simplelithology.ttl"])
    print("CGI SimpleLithology：%d 个概念" % len(concepts))

    ms = json.load(open(paths["macrostrat_lithologies.json"], encoding="utf-8"))
    ms = ms["success"]["data"]
    by_name = {}
    for row in ms:
        by_name[row["name"].strip().lower()] = row
    # CGI 的 id 与 Macrostrat 的 name 对齐（id 里的下划线换空格）
    bad_alias = sorted(v for v in NAME_ALIAS.values() if v.lower() not in by_name)
    if bad_alias:
        raise SystemExit("NAME_ALIAS 里这些名字不在 Macrostrat 岩性表中：%s"
                         % "、".join(bad_alias))
    style = {}
    for cid, node in concepts.items():
        row = None
        if cid in NAME_ALIAS:
            row = by_name.get(NAME_ALIAS[cid].lower())
        if row is None:
            for cand in _candidates(cid, node["name_en"]):
                row = by_name.get(cand)
                if row is not None:
                    break
        if row:
            style[cid] = {"color": row["color"], "pattern": row.get("fill"),
                          "type": row.get("type") or ""}
    print("与 Macrostrat 对上号：%d 个" % len(style))

    unknown = sorted(set(ZH_SUPPLEMENT) - set(concepts))
    if unknown:
        print("警告：补充表里有 %d 个 id 不在词表中，已跳过：%s"
              % (len(unknown), "、".join(unknown)))

    # 中文名：补充表优先于词表自带的 @zh 标签。CGI 的中文标签是机器对齐来的，
    # 有一部分明显不对（phyllite 标成"粒硬绿泥石"、sand 标成"砂岩"），照单全收
    # 会把"砂岩"认到"砂"上去。凡是补充表已经占用的名字，词表那边一律让路。
    curated = {}
    for cid, names in ZH_SUPPLEMENT.items():
        for name in names:
            curated.setdefault(name, cid)

    classes = []
    inherited = 0
    dropped_labels = []
    for cid in sorted(concepts):
        node = concepts[cid]
        keep_auto = []
        for name in node["zh"]:
            owner = curated.get(name)
            if owner is not None and owner != cid:
                dropped_labels.append("%s（词表挂在 %s，补充表挂在 %s）"
                                      % (name, cid, owner))
                continue
            keep_auto.append(name)
        zh = list(dict.fromkeys(ZH_SUPPLEMENT.get(cid, []) + keep_auto))
        cls = rock_class(cid, concepts)
        st, from_id = inherit(cid, concepts, style)
        if st is None:
            color, pattern, mtype = CLASS_FALLBACK_COLOR[cls], None, ""
            from_id = ""
        else:
            color, pattern, mtype = st["color"], st["pattern"], st["type"]
            if from_id != cid:
                inherited += 1
        over = GBT958_OVERRIDE.get(cid) or {}
        classes.append({
            "id": cid,
            "name_en": node["name_en"],
            "name_zh": zh[0] if zh else "",
            "aliases_zh": zh[1:],
            "broader": node["broader"],
            "rock_class": cls,
            "rock_type": mtype,
            "color": over.get("color", color),
            "fgdc_pattern": over.get("pattern", pattern),
            "color_from": from_id,
            "definition": node["definition"],
        })
    if dropped_labels:
        print("让位给补充表的词表中文标签 %d 条：%s"
              % (len(dropped_labels), "、".join(dropped_labels[:6])
                 + ("…" if len(dropped_labels) > 6 else "")))
    named = sum(1 for c in classes if c["name_zh"])
    zh_ok = sum(1 for c in classes if c["name_zh"] and c["color_from"])
    print("颜色：直接命中 %d、继承自上位概念 %d、按大类兜底 %d"
          % (len(style), inherited, len(classes) - len(style) - inherited))
    print("有中文名的概念：%d / %d，其中 %d 个拿到了真实配色（而非大类兜底）"
          % (named, len(classes), zh_ok))

    intervals = json.load(open(paths["macrostrat_intervals.json"], encoding="utf-8"))
    intervals = [{"name": r["name"], "abbrev": r.get("abbrev"),
                  "t_age": r["t_age"], "b_age": r["b_age"],
                  "rank": r.get("int_type"), "color": r["color"]}
                 for r in intervals["success"]["data"]]

    doc = {
        "version": "1.0",
        "generated": date.today().isoformat(),
        "sources": [
            {"name": "IUGS-CGI SimpleLithology", "license": "CC-BY 4.0",
             "url": SOURCES["simplelithology.ttl"],
             "role": "岩性分类骨架、层级、中英文名"},
            {"name": "Macrostrat lithologies", "license": "CC-BY 4.0",
             "url": SOURCES["macrostrat_lithologies.json"],
             "role": "颜色与 FGDC 花纹号"},
            {"name": "Macrostrat / ICS international ages", "license": "CC-BY 4.0",
             "url": SOURCES["macrostrat_intervals.json"],
             "role": "国际年代地层表官方配色（CGMW 色标）"},
        ],
        "notes": [
            "GB/T 958-2015《区域地质图图例》与 DZ/T 0179-1997《地质图用色标准及"
            "色标》没有公开的机器可读版本，本文件不含其色值；需要严格按国标出图"
            "时，用 build_legend_library.py 里的 GBT958_OVERRIDE 覆盖。",
            "fgdc_pattern 是 FGDC-STD-013-2006 的花纹编号，目前只记编号，没有"
            "内置花纹位图。",
        ],
        "modifiers": MODIFIERS,
        "classes": classes,
        "chronostrat": intervals,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    print("已写出 %s（%.1f KB）" % (OUT_PATH, os.path.getsize(OUT_PATH) / 1024))


if __name__ == "__main__":
    sys.exit(main())
