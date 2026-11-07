# -*- coding: utf-8 -*-
"""光谱库条目的中文名。

库里的名字全是英文，而且带着各家源库的习惯：``Alunite KAl``（后面跟半截化学
式）、``Albite (Feldspar)``（括号里写族）、``AtacamiteCu``（化学式直接粘在名字
后面）、``Ammonio-Jarosite``（化学前缀）、``Alun.33+Kaol.33+Musc.33``（混合物，
还带缩写和比例）。这里先把名字收拾成干净的词，再查表、拼词。

**宁可没有，不能写错。** 查不着的词、拼不出来的名字一律留空，界面上那一栏就是
空的，英文名照常显示。矿物名一个词对一个词，按《英汉矿物种名称》（2017）与地质
上通行的叫法填；岩石名按"修饰词在前、岩石名在后"的中文习惯拼，只要有一个词不
认得就整个放弃——"黑云母 Granite"这种半中半英的名字比没有更糟。

植被、人工材料、有机物、陨石的名字没有收：样本库的使用场景里用不上，而那几
类的中文译名本身就不统一。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# =============================================================================
# 矿物：英文名（小写）-> 中文名
# =============================================================================
MINERALS: Dict[str, str] = {
    # ---- 硅酸盐：层状
    "kaolinite": "高岭石", "kaolin": "高岭土", "dickite": "地开石", "nacrite": "珍珠陶土",
    "halloysite": "埃洛石", "endellite": "水埃洛石", "montmorillonite": "蒙脱石",
    "smectite": "蒙皂石", "beidellite": "贝得石", "nontronite": "绿脱石",
    "notronite": "绿脱石", "saponite": "皂石", "hectorite": "锂蒙脱石",
    "sauconite": "锌蒙脱石", "illite": "伊利石", "rectorite": "累托石",
    "corrensite": "柯绿泥石", "vermiculite": "蛭石", "muscovite": "白云母",
    "sericite": "绢云母", "paragonite": "钠云母", "phengite": "多硅白云母",
    "biotite": "黑云母", "phlogopite": "金云母", "annite": "铁云母",
    "siderophyllite": "铁叶云母", "lepidolite": "锂云母", "margarite": "珍珠云母",
    "roscoelite": "钒云母", "clintonite": "绿脆云母", "glauconite": "海绿石",
    "mica": "云母", "chlorite": "绿泥石", "clinochlore": "斜绿泥石",
    "cookeite": "锂绿泥石",
    "cronstedtite": "克铁蛇纹石", "serpentine": "蛇纹石", "antigorite": "叶蛇纹石",
    "chrysotile": "纤蛇纹石", "lizardite": "利蛇纹石", "talc": "滑石",
    "pyrophyllite": "叶蜡石", "sepiolite": "海泡石", "palygorskite": "坡缕石",
    "chrysocolla": "硅孔雀石", "prehnite": "葡萄石", "apophyllite": "鱼眼石",
    "hydroxyapophyllite": "羟鱼眼石",
    # ---- 硅酸盐：链状
    "amphibole": "角闪石", "hornblende": "普通角闪石", "actinolite": "阳起石",
    "tremolite": "透闪石", "anthophyllite": "直闪石", "cummingtonite": "镁铁闪石",
    "glaucophane": "蓝闪石", "riebeckite": "钠闪石", "uralite": "纤闪石", "nephrite": "软玉",
    "pyroxene": "辉石", "augite": "普通辉石", "diopside": "透辉石",
    "hedenbergite": "钙铁辉石", "johannsenite": "锰钙辉石", "enstatite": "顽火辉石", "bronzite": "古铜辉石", "hypersthene": "紫苏辉石",
    "pigeonite": "易变辉石", "jadeite": "硬玉", "acmite": "锥辉石",
    "aegirine": "霓石", "spodumene": "锂辉石", "wollastonite": "硅灰石",
    "pectolite": "针钠钙石", "rhodonite": "蔷薇辉石", "bustamite": "钙蔷薇辉石",
    "pyroxmangite": "三斜锰辉石",
    # ---- 硅酸盐：架状
    "quartz": "石英", "chalcedony": "玉髓", "opal": "蛋白石", "chert": "燧石",
    "cristobalite": "方石英", "feldspar": "长石", "orthoclase": "正长石",
    "microcline": "微斜长石", "sanidine": "透长石", "adularia": "冰长石",
    "perthite": "条纹长石", "albite": "钠长石", "oligoclase": "更长石",
    "andesine": "中长石", "labradorite": "拉长石", "bytownite": "培长石",
    "anorthite": "钙长石", "plagioclase": "斜长石", "buddingtonite": "水铵长石",
    "nepheline": "霞石", "leucite": "白榴石", "sodalite": "方钠石",
    "lazurite": "青金石", "scapolite": "方柱石", "marialite": "钠柱石",
    "meionite": "钙柱石", "analcime": "方沸石", "natrolite": "钠沸石", "scolecite": "钙沸石",
    "mesolite": "中沸石", "stilbite": "辉沸石", "heulandite": "片沸石",
    "clinoptilolite": "斜发沸石", "chabazite": "菱沸石", "erionite": "毛沸石",
    "mordenite": "丝光沸石", "laumontite": "浊沸石", "thomsonite": "杆沸石",
    "offretite": "菱钾沸石", "wairakite": "斜钙沸石", "zeolite": "沸石",
    # ---- 硅酸盐：岛状、双岛、环状
    "olivine": "橄榄石", "forsterite": "镁橄榄石", "fayalite": "铁橄榄石",
    "knebelite": "锰铁橄榄石", "monticellite": "钙镁橄榄石", "garnet": "石榴子石",
    "almandine": "铁铝榴石", "pyrope": "镁铝榴石", "spessartine": "锰铝榴石",
    "grossular": "钙铝榴石", "andradite": "钙铁榴石", "uvarovite": "钙铬榴石",
    "hydrogrossular": "水钙铝榴石", "zircon": "锆石", "sphene": "榍石",
    "titanite": "榍石", "kyanite": "蓝晶石", "andalusite": "红柱石",
    "sillimanite": "夕线石", "staurolite": "十字石", "topaz": "黄玉",
    "datolite": "硅硼钙石", "dumortierite": "蓝线石", "epidote": "绿帘石",
    "clinozoisite": "斜黝帘石", "zoisite": "黝帘石", "allanite": "褐帘石",
    "vesuvianite": "符山石", "hemimorphite": "异极矿", "axinite": "斧石",
    "ferroaxinite": "铁斧石", "beryl": "绿柱石", "cordierite": "堇青石",
    "tourmaline": "电气石", "elbaite": "锂电气石", "schorl": "黑电气石",
    "zunyite": "氟氯黄晶", "chloritoid": "硬绿泥石",
    # ---- 硫酸盐
    "alunite": "明矾石", "natroalunite": "钠明矾石", "jarosite": "黄钾铁矾",
    "natrojarosite": "钠铁矾", "plumbojarosite": "铅铁矾", "gypsum": "石膏",
    "anhydrite": "硬石膏", "bassanite": "烧石膏", "barite": "重晶石",
    "celestite": "天青石", "anglesite": "铅矾", "epsomite": "泻利盐",
    "hexahydrite": "六水泻盐", "kieserite": "水镁矾", "thenardite": "无水芒硝",
    "mirabilite": "芒硝", "glauberite": "钙芒硝", "bloedite": "白钠镁矾",
    "polyhalite": "杂卤石", "syngenite": "钾石膏", "kainite": "钾盐镁矾",
    "aphthitalite": "钾芒硝", "copiapite": "叶绿矾", "coquimbite": "针绿矾",
    "schwertmannite": "施氏矿物", "antlerite": "块铜矾",
    "mascagnite": "铵矾", "tschermigite": "铵明矾", "burkeite": "碳钠矾",
    # ---- 碳酸盐
    "calcite": "方解石", "aragonite": "文石", "dolomite": "白云石",
    "magnesite": "菱镁矿", "siderite": "菱铁矿", "rhodochrosite": "菱锰矿",
    "smithsonite": "菱锌矿", "cerussite": "白铅矿", "strontianite": "菱锶矿",
    "witherite": "毒重石", "malachite": "孔雀石", "azurite": "蓝铜矿",
    "hydromagnesite": "水菱镁矿", "trona": "天然碱", "natron": "泡碱",
    "gaylussite": "单斜钠钙石", "bastnaesite": "氟碳铈矿", "parisite": "氟碳钙铈矿",
    "ancylite": "碳锶铈矿",
    # ---- 氧化物、氢氧化物
    "hematite": "赤铁矿", "nanohematite": "纳米赤铁矿", "goethite": "针铁矿",
    "lepidocrocite": "纤铁矿", "limonite": "褐铁矿", "ferrihydrite": "水铁矿",
    "magnetite": "磁铁矿", "maghemite": "磁赤铁矿", "ilmenite": "钛铁矿",
    "rutile": "金红石", "anatase": "锐钛矿", "brookite": "板钛矿",
    "cassiterite": "锡石", "corundum": "刚玉", "spinel": "尖晶石",
    "chromite": "铬铁矿", "magnesiochromite": "镁铬铁矿", "gahnite": "锌尖晶石",
    "franklinite": "锌铁尖晶石", "cuprite": "赤铜矿", "zincite": "红锌矿",
    "periclase": "方镁石", "pyrolusite": "软锰矿", "psilomelane": "硬锰矿",
    "columbite": "铌铁矿", "gibbsite": "三水铝石", "diaspore": "一水硬铝石",
    "boehmite": "一水软铝石", "brucite": "水镁石", "portlandite": "氢氧钙石",
    # ---- 硫化物、自然元素
    "pyrite": "黄铁矿", "marcasite": "白铁矿", "pyrrhotite": "磁黄铁矿",
    "chalcopyrite": "黄铜矿", "bornite": "斑铜矿", "chalcocite": "辉铜矿",
    "covellite": "铜蓝", "galena": "方铅矿", "sphalerite": "闪锌矿",
    "arsenopyrite": "毒砂", "cinnabar": "辰砂", "realgar": "雄黄",
    "orpiment": "雌黄", "stibnite": "辉锑矿", "molybdenite": "辉钼矿",
    "sulfur": "自然硫", "sulphur": "自然硫", "graphite": "石墨",
    # ---- 卤化物
    "halite": "石盐", "sylvite": "钾盐", "fluorite": "萤石", "carnallite": "光卤石",
    "cryolite": "冰晶石", "atacamite": "氯铜矿",
    # ---- 磷酸盐、砷酸盐、钨酸盐等
    "apatite": "磷灰石", "fluorapatite": "氟磷灰石", "chlorapatite": "氯磷灰石",
    "hydroxylapatite": "羟磷灰石", "monazite": "独居石", "xenotime": "磷钇矿",
    "pyromorphite": "磷氯铅矿", "mimetite": "砷铅矿", "amblygonite": "锂磷铝石",
    "montebrasite": "羟磷锂铝石", "triphylite": "磷铁锂矿", "scorodite": "臭葱石",
    "scheelite": "白钨矿", "wolframite": "黑钨矿",
    # ---- 硼酸盐、硝酸盐
    "borax": "硼砂", "tincalconite": "三方硼砂", "kernite": "斜方硼砂",
    "colemanite": "硬硼钙石", "ulexite": "钠硼解石", "inderite": "多水硼镁石", "meyerhofferite": "板硼钙石",
    "pinnoite": "柱硼镁石", "niter": "硝石",
}

#: 源库里常见的缩写（多出现在混合物样品名里）与笔误
ABBREVIATIONS: Dict[str, str] = {
    "alun": "alunite", "kalun": "alunite", "kaol": "kaolinite",
    "kaolwxl": "kaolinite", "wxlkaol": "kaolinite", "musc": "muscovite",
    "muscov": "muscovite", "msc": "muscovite", "pyro": "pyrophyllite",
    "pyrophyl": "pyrophyllite", "calc": "calcite", "cal": "calcite",
    "dolo": "dolomite", "dolomit": "dolomite", "chl": "chlorite",
    "chlor": "chlorite", "chlorit": "chlorite", "epid": "epidote",
    "epidot": "epidote", "gyp": "gypsum", "jr": "jarosite", "jar": "jarosite",
    "jaros": "jarosite", "kjar": "jarosite", "dic": "dickite",
    "mont": "montmorillonite", "mon": "montmorillonite",
    "montmor": "montmorillonite", "goeth": "goethite", "gth": "goethite",
    "qtz": "quartz", "ill": "illite", "hydromag": "hydromagnesite",
    "clinopt": "clinoptilolite", "thom": "thomsonite", "wair": "wairakite",
    "hydroxyapop": "hydroxyapophyllite", "franklin": "franklinite",
    "buddingtnt": "buddingtonite", "smect": "smectite", "tremolit": "tremolite",
    "hydroxyl": "hydroxylapatite", "zeolite": "zeolite",
}

#: 化学前缀：Na-蒙脱石、Ammonio-黄钾铁矾 之类
PREFIXES: Dict[str, str] = {
    "na": "钠", "k": "钾", "ca": "钙", "mg": "镁", "fe": "铁", "mn": "锰",
    "li": "锂", "al": "铝", "cr": "铬", "zn": "锌", "cu": "铜", "ni": "镍",
    "ba": "钡", "sr": "锶", "pb": "铅", "ammonio": "铵", "nh4": "铵",
}

#: 不经过拆词、整名直接对照的矿物写法
MINERAL_FULL: Dict[str, str] = {
    "ammonio-jarosite": "铵黄钾铁矾",
    "ammonio-smectite": "铵蒙皂石",
    "ammonio-illite-smect": "铵伊利石-蒙皂石",
    "illite-smectite": "伊利石-蒙皂石混层",
    "illite/smectite": "伊利石-蒙皂石混层",
    "goethite alpha": "针铁矿", "hematite alpha": "赤铁矿",
    "hydroxyl-apatite": "羟磷灰石",
    "carbon": "碳", "fe-hydroxide": "铁的氢氧化物",
    "biotite-chlorite": "黑云母-绿泥石",
}

# =============================================================================
# 岩石
# =============================================================================
#: 岩石名（作中心词）
ROCKS: Dict[str, str] = {
    "granite": "花岗岩", "granodiorite": "花岗闪长岩", "tonalite": "英云闪长岩",
    "diorite": "闪长岩", "monzonite": "二长岩", "syenite": "正长岩",
    "gabbro": "辉长岩", "norite": "苏长岩", "anorthosite": "斜长岩",
    "diabase": "辉绿岩", "dunite": "纯橄岩", "peridotite": "橄榄岩",
    "peridotie": "橄榄岩", "pyroxenite": "辉石岩", "picrite": "苦橄岩",
    "ijolite": "霓霞岩", "lamprophyre": "煌斑岩", "aplite": "细晶岩",
    "pegmatite": "伟晶岩", "porphyry": "斑岩", "basalt": "玄武岩",
    "basanite": "碧玄岩", "andesite": "安山岩", "dacite": "英安岩",
    "rhyolite": "流纹岩", "trachyte": "粗面岩", "obsidian": "黑曜岩",
    "scoria": "火山渣", "tuff": "凝灰岩", "felsite": "霏细岩",
    "breccia": "角砾岩", "conglomerate": "砾岩", "sandstone": "砂岩",
    "siltstone": "粉砂岩", "shale": "页岩", "limestone": "灰岩",
    "dolomite": "白云岩", "chalk": "白垩", "arkose": "长石砂岩",
    "greywacke": "杂砂岩", "graywacke": "杂砂岩", "phosphorite": "磷块岩",
    "gypsum": "石膏岩", "oolite": "鲕粒岩", "travertine": "钙华",
    "chert": "燧石岩", "gneiss": "片麻岩", "schist": "片岩", "phyllite": "千枚岩",
    "slate": "板岩", "marble": "大理岩", "quartzite": "石英岩",
    "hornfels": "角岩", "serpentine": "蛇纹岩", "serpentinite": "蛇纹岩",
}

#: 修饰词（颜色、结构、成分形容词）。矿物名作修饰词时查 MINERALS。
ROCK_MODIFIERS: Dict[str, str] = {
    "gray": "灰色", "grey": "灰色", "green": "绿色", "red": "红色",
    "pink": "粉红色", "purple": "紫色", "white": "白色", "black": "黑色",
    "grey-green": "灰绿色", "purple-banded": "紫色条带状",
    "alkali": "碱性", "alkalic": "碱性", "amygdaloidal": "杏仁状",
    "vesicular": "气孔状", "porphyritic": "似斑状", "augen": "眼球状",
    "banded": "条带状", "spotted": "斑点状", "chiastolic": "空晶石",
    "arenaceous": "砂质", "argillaceous": "泥质", "calcareous": "钙质",
    "carbonaceous": "碳质", "cherty": "燧石质", "dolomitic": "白云质",
    "ferruginous": "铁质", "fossiliferous": "含化石", "glauconitic": "海绿石",
    "micaceous": "云母质", "siliceous": "硅质", "oolitic": "鲕状",
    "phosphatic": "磷质", "felsitic": "霏细质", "granitoid": "花岗质",
    "garnetiferous": "含石榴子石", "chloritic": "绿泥石", "basaltic": "玄武质",
    "arkosic": "长石质", "kaolinitic": "高岭石质", "illite-bearing": "含伊利石",
    "rhyolitic": "流纹质", "altered": "蚀变", "unaltered": "未蚀变",
    "volcanic": "火山", "lithographic": "石印", "ecrinal": "海百合",
    "encrinal": "海百合", "desert": "荒漠漆", "varnished": "",
}

#: 矿物作岩石修饰词时的习惯简称：角闪花岗岩、紫苏辉长岩，而不是"普通角闪石花岗岩"
ROCK_MINERAL_SHORT: Dict[str, str] = {
    "hornblende": "角闪", "hypersthene": "紫苏", "olivine": "橄榄", "augite": "辉石",
}

#: 颜色词。括号里补的修饰词并进来时，颜色排最前：红色云母质砂岩
COLORS = {"gray", "grey", "green", "red", "pink", "purple", "white", "black",
          "grey-green", "purple-banded"}

#: 拼不出来、或者拼出来不合中文习惯的，整名对照。值为空串表示"故意不给"。
ROCK_FULL: Dict[str, str] = {
    "andesite porphyry": "安山玢岩", "diabase porphyry": "辉绿玢岩",
    "diorite porphyry": "闪长玢岩", "mica dacite porphyry": "云母英安斑岩",
    "quartz felsite porphyry": "石英霏细斑岩",
    "quartz monzonite porphyry": "石英二长斑岩", "rhyolite porphyry": "流纹斑岩",
    "granite porphyry": "花岗斑岩", "quartz monzonite": "石英二长岩",
    "nepheline syenite": "霞石正长岩", "nepheline-sodalite syenite": "霞石方钠石正长岩",
    "muscovite-biotite granite": "二云母花岗岩", "green schist": "绿片岩",
    "diatomaceous earth": "硅藻土", "oil shale": "油页岩",
    "verde antique": "蛇纹石大理岩", "oolitic iron ore": "鲕状铁矿石",
    "limestone siltstone": "灰质粉砂岩", "limestone-siltstone": "灰质粉砂岩",
    "limestone conglomerate-breccia": "灰岩砾岩-角砾岩",
    "aporhyolite": "变流纹岩", "chocolate \"marble\" limestone": "巧克力色灰岩",
    "hypersthene gabbro": "紫苏辉长岩", "olivine gabbro": "橄榄辉长岩",
    "alkali basalt": "碱性玄武岩", "basaltic andesite": "玄武安山岩",
    "augite-hypersthene andesite": "辉石安山岩",
    "greywacke sandstone": "杂砂岩", "olivine basalt porphyry": "",
}

#: 冰雪与水
WATERS: Dict[str, str] = {
    "coarse granular snow": "粗粒雪", "medium granular snow": "中粒雪",
    "fine snow": "细雪", "frost": "霜", "ice": "冰", "h2o-ice": "冰",
    "distilled water": "蒸馏水", "tap water": "自来水", "sea water": "海水",
    "seawater": "海水", "sea foam": "海沫",
}

# =============================================================================


def _clean(name: str) -> str:
    """去掉括号里的补充说明、尾巴上的化学式碎片与问号。"""
    text = re.sub(r"\([^)]*\)", " ", name)
    text = text.replace("?", " ").replace("*", " ")
    return re.sub(r"\s+", " ", text).strip()


def _word_cn(word: str) -> Optional[str]:
    """一个词的中文：先查矿物名，再查缩写；带化学前缀的拆开查。"""
    w = word.lower().strip(" .,")
    if not w:
        return None
    if w in MINERAL_FULL:
        return MINERAL_FULL[w]
    if w in MINERALS:
        return MINERALS[w]
    if w in ABBREVIATIONS:
        return MINERALS.get(ABBREVIATIONS[w])
    if "-" in w:
        head, _sep, tail = w.partition("-")
        if head in PREFIXES:
            rest = _word_cn(tail)
            return PREFIXES[head] + rest if rest else None
        if tail in PREFIXES and len(tail) <= 2:            # Alunite-K、Montmorillonite-Na
            rest = _word_cn(head)
            return PREFIXES[tail] + rest if rest else None
    return None


def _glued(word: str) -> Optional[str]:
    """``AtacamiteCu`` 这种把化学式直接粘在后面的：找最长的已知前缀。

    后半截要是另一个矿物名（``ClinozoisiteEpidote``），两个都写上。
    """
    w = word.lower()
    for n in range(len(w), 3, -1):
        hit = MINERALS.get(w[:n])
        if hit and (n == len(w) or w[n:n + 1].isalpha() and word[n:n + 1].isupper()):
            tail = MINERALS.get(w[n:])
            return "%s-%s" % (hit, tail) if tail else hit
    return None


def mineral_cn(name: str) -> str:
    """矿物条目的中文名。认不出来返回空串。"""
    full = name.lower().strip()
    if full in MINERAL_FULL:
        return MINERAL_FULL[full]
    text = _clean(name)
    low = text.lower()
    if low in MINERAL_FULL:
        return MINERAL_FULL[low]
    words = text.split(" ")
    if not words or not words[0]:
        return ""
    # 第一个词就是矿物名，后面是化学式碎片（Alunite KAl、Gypsum CaSO4）或族名
    # （Almandine Garnet），都以第一个词为准
    hit = _word_cn(words[0]) or _glued(words[0])
    if hit:
        return hit
    # 两个矿物用连字符连着：Covellite-Pyrite
    pair = words[0].split("-")
    if len(pair) == 2:
        a, b = _word_cn(pair[0]), _word_cn(pair[1])
        if a and b:
            return "%s-%s" % (a, b)
    return ""


def mixture_cn(name: str) -> str:
    """混合物样品：``Alunite+Dickite``、``Calc.25+Dolo.25+Ca-Mont``。"""
    parts = [p for p in re.split(r"\+", name) if p.strip()]
    if len(parts) < 2:
        return ""
    out: List[str] = []
    for part in parts:
        # 去掉比例与样品号：.33、0.5、366、wt、CU91-250A
        word = re.sub(r"[0-9.]+(wt)?", " ", part)
        word = re.sub(r"\b[A-Z]{2}[0-9A-Z-]*\b", " ", word).strip(" .-")
        word = word.split(" ")[0] if word else ""
        if word.lower() in ("others", "other"):
            out.append("其他")
            continue
        cn = _word_cn(word) or _glued(word)
        if not cn:
            return ""
        out.append(cn)
    return "+".join(out)


def rock_cn(name: str) -> str:
    """岩石条目的中文名：修饰词在前、岩石名在后。有一个词不认得就放弃。"""
    full = name.lower().strip()
    if full in ROCK_FULL:
        return ROCK_FULL[full]
    paren = re.findall(r"\(([^)]*)\)", name)
    text = _clean(name).lower()
    if text in ROCK_FULL:
        return ROCK_FULL[text]
    words = text.split(" ")
    head = words[-1]
    if head not in ROCKS:
        # 中心词是矿物名的岩石（Gypsum、Serpentine 已在 ROCKS 里；Dolomite 也是）
        return ""
    pieces: List[str] = []
    for word in words[:-1]:
        if word in ROCK_MODIFIERS:
            pieces.append(ROCK_MODIFIERS[word])
            continue
        # 连字符串起来的矿物：Quartz-Sericite、Sillimanite-Garnet
        sub = [_modifier_cn(w) for w in word.split("-")]
        if any(s is None for s in sub):
            return ""
        pieces.append("".join(sub))
    # 括号里若全是修饰词（Sandstone (Red)、Shale (Calcareous)），并进去，颜色
    # 排最前；若是同义的别名（(Flagstone)、(Norite)），丢掉
    for extra in paren:
        mods = [m for m in extra.lower().split(" ") if m]
        if mods and all(m in ROCK_MODIFIERS for m in mods):
            mods.sort(key=lambda m: m not in COLORS)
            pieces = [ROCK_MODIFIERS[m] for m in mods] + pieces
    return "".join(pieces) + ROCKS[head]


def _modifier_cn(word: str) -> Optional[str]:
    """岩石名里作修饰的一个词：矿物（取习惯简称）、或者另一种岩石（变成"质"）。"""
    if word in ROCK_MINERAL_SHORT:
        return ROCK_MINERAL_SHORT[word]
    hit = _word_cn(word)
    if hit:
        return hit
    rock = ROCKS.get(word)
    if rock and rock.endswith("岩"):
        return rock[:-1] + "质"                 # 闪长质片麻岩、灰质角砾岩
    return None


def chinese_name(name: str, category: str = "") -> str:
    """按条目的分类挑规则，给出中文名；规则认不出来的查补充表，都没有返回空串。"""
    from .names_cn_extra import extra_name
    return _rule_name(name, category) or extra_name(name, category)


def _rule_name(name: str, category: str = "") -> str:
    top = (category or "").split("/", 1)[0]
    if not name:
        return ""
    if top == "rock":
        return rock_cn(name)
    if top in ("water", "liquid"):
        hit = WATERS.get(name.lower().strip())
        return hit or (mixture_cn(name) if "+" in name else "")
    if top in ("mineral", "coating", "soil", "") or top not in (
            "vegetation", "manmade", "organic", "meteorite"):
        if "+" in name:
            return mixture_cn(name)
        return mineral_cn(name)
    return ""


#: 反查：中文名 -> 英文名。存自己的谱时，敲了中文名就替他补上英文名（图例用）。
def english_for(cn: str) -> str:
    cn = (cn or "").strip()
    if not cn:
        return ""
    for en, zh in MINERALS.items():
        if zh == cn:
            return en.capitalize()
    for en, zh in ROCKS.items():
        if zh == cn:
            return en.capitalize()
    return ""
