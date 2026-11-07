# -*- coding: utf-8 -*-
"""把 ENVI 光谱库编译成本系统自己的一个库文件 ``spectral_library.slb``。

它是什么
--------
ENVI 随机带的那几套光谱库是 110 对 ``.sli`` / ``.hdr``，分散在五个目录里，一共
7132 条光谱。直接拿来用有三个问题：

* **没有分类**。要"只跟矿物比"就得自己知道哪个文件里装的是矿物；要"只跟层状
  硅酸盐比"则完全无从下手。
* **量纲不齐**。ASTER 那套把反射率存成百分数（头里写 ``reflectance scale
  factor = 100``），USGS 存成小数；USGS 还用 ``-1.23e34`` 当无效值标记，照读
  会把曲线拉到天上去。
* **散**。110 个文件 63 MB，进不了安装包，也没法当成一份东西版本化。

这个脚本把它们归一化成**一个文件**：反射率统一成小数、无效值统一成 NaN、每条
光谱带上一条分类路径（``mineral/silicate/phyllosilicate`` 这样），存成 float16
加 deflate 压缩，体量降到原来的三分之一以内。

分类是从哪来的（这一点要能交代清楚）
------------------------------------
* **ASTER Spectral Library v2 的名字里自带分类**。它每条光谱都写成
  ``Actinolite Ca 2(...) [silicate-inosilicate-coarse-in04a]``，方括号里第一、
  第二段就是化学分类。这是**源数据自带的**，不是本脚本编的。岩石、土壤同理
  （``[igneous-felsic-...]``、``[mollisol-cryoboroll-...]``）。
* **USGS 与 IGCP-264 的名字里没有分类**，只有矿物名。做法是先从上面那份 ASTER
  的名字里收出一张「矿物名 -> 化学分类」表，再拿矿物名去查。能查到的记
  ``class_source = "aster"``。
* 查不到的那一百来个（allanite、halloysite、diaspore 之类），用下面
  ``MINERAL_CLASS`` 里手工补的一张表，记 ``class_source = "curated"``。这张表
  按矿物学通行的阴离子分类填写，逐条可核对；填不准的宁可留空，会落到
  ``mineral/unclassified``，记 ``class_source = ""``。

``ALTERATION_GROUPS`` 是另一回事：它不是源数据，也不是矿物学分类，而是**为了检索
方便**按蚀变组合归的堆（泥化、青磐岩化、铁染……）。同一个矿物可以属于多组。库
文件里单独存成 ``groups`` 字段，与 ``cls`` 分开，免得两者混为一谈。

怎么用
------
装好 ENVI 光谱库目录，改一下下面的 ``SRC_DIR``，直接运行（VS Code 里点运行就
行）。跑一次几十秒，输出覆盖 ``samplelib/modules/spectra/data/spectral_library.slb``。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import zipfile
from collections import Counter, OrderedDict
from typing import Dict, List, Tuple

import numpy as np

# =============================================================================
# 配置区
# =============================================================================

_HERE = os.path.dirname(os.path.abspath(__file__))
_STUDIO = os.path.dirname(_HERE)

#: ENVI 光谱库根目录（里面是 aster / igcp264 / usgs_v6 / usgs_v7 / veg_lib）。
SRC_DIR = os.path.join(os.path.dirname(_STUDIO), "光谱库")

#: 输出。随包发布，运行时只读。
OUT_PATH = os.path.join(_STUDIO, "samplelib", "modules", "spectra", "data",
                        "spectral_library.slb")

#: 收哪几套。
#:
#: **默认不收 usgs_v6**：v7 是 v6 的后继版本，矿物部分 88% 的（矿物名, 样品号）
#: 在 v7 里都能找到，两套一起进库只会让匹配结果里挤满同一条谱的两个版本。v6 里
#: 那 59 个 v7 没有的样品要用的话，把 "usgs_v6" 加进来重新编译即可——加进来的
#: 条目会带上 ``superseded`` 标记，界面上默认不参与检索。
INCLUDE_SOURCES: Tuple[str, ...] = ("aster", "usgs_v7", "igcp264", "veg_lib")

#: usgs_v6 里同一批光谱存了两份：一份在仪器原生波长上（如 Beckman 430 通道），
#: 一份重采样到更密的网格上（3375 通道）。谱名完全一样。收 v6 时只留原生那份，
#: 重采样那份丢掉——它是插出来的，不是测出来的，而且大八倍。
V6_DROP_RESAMPLED = True

#: 存储精度。float16 的相对精度约 5e-4，对 0~1 的反射率足够，比 float32 省一半，
#: 压缩后比"定点 uint16 + 缩放"还小 10%（试过）。NaN 也能原样存。
STORE_DTYPE = "<f2"

#: 绝对值超过它就当无效值。USGS 用 -1.23e34 标记删掉的波段；ASTER 的透射率是
#: 百分数，最大 100 上下，所以门限放在 1e4 两边都不会误伤。
VALID_ABS_MAX = 1e4
# 一份文件的 ``|y|`` 95 分位超过这个数，就说明它整份都不是小数反射率（多半是
# 百分数，也可能压根不是反射率），整份跳过并在日志里点名。见 _unit_level。
MAX_VALUE_P95 = 1.6

#: 五套库的出处。写进库文件的 manifest，界面上「关于光谱库」直接显示。
SOURCES: "OrderedDict[str, Dict[str, str]]" = OrderedDict([
    ("aster", {
        "dir": "aster",
        "cn": "ASTER 光谱库 v2.0",
        "en": "ASTER Spectral Library Version 2.0",
        "citation": "Baldridge, A.M., Hook, S.J., Grove, C.I., Rivera, G., 2009, "
                    "The ASTER Spectral Library Version 2.0: Remote Sensing of "
                    "Environment, v. 113, p. 711-715.",
        "note": "含 JHU、JPL、USGS 三家的矿物、岩石、土壤、植被、水体与人造材料，"
                "波长覆盖到热红外。",
    }),
    ("usgs_v7", {
        "dir": "usgs_v7",
        "cn": "USGS 数字光谱库 v7",
        "en": "USGS Digital Spectral Library splib07a",
        "citation": "Kokaly, R.F., Clark, R.N., Swayze, G.A., Livo, K.E., Hoefen, "
                    "T.M., Pearson, N.C., Wise, R.A., Benzel, W.M., Lowers, H.A., "
                    "Driscoll, R.L., and Klein, A.J., 2017, USGS Spectral Library "
                    "Version 7: U.S. Geological Survey Data Series 1035, 61 p., "
                    "https://doi.org/10.3133/ds1035",
        "note": "短波红外矿物谱的事实标准，蚀变矿物判读基本都引它。",
    }),
    ("usgs_v6", {
        "dir": "usgs_v6",
        "cn": "USGS 数字光谱库 v6",
        "en": "USGS Digital Spectral Library splib06a",
        "citation": "Clark, R.N., Swayze, G.A., Wise, R., Livo, E., Hoefen, T., "
                    "Kokaly, R., and Sutley, S.J., 2007, USGS Digital Spectral "
                    "Library splib06a: U.S. Geological Survey, Digital Data "
                    "Series 231.",
        "note": "已被 v7 取代，默认不收。",
        "superseded": "1",
    }),
    ("igcp264", {
        "dir": "igcp264",
        "cn": "IGCP-264 光谱库",
        "en": "IGCP-264 Spectral Library",
        "citation": "Kruse, F.A., Thiry, M., and Hauff, P.L., 1991, Spectral "
                    "identification of clay minerals: IGCP Project 264 "
                    "standard samples.",
        "note": "同一批 26 个标样在五台不同光谱仪上各测一遍，比较仪器差异时有用。",
    }),
    ("veg_lib", {
        "dir": "veg_lib",
        "cn": "植被光谱库",
        "en": "Published Vegetation Spectral Libraries",
        "citation": "Elvidge, C.D., 1990, Visible and near infrared reflectance "
                    "characteristics of dry plant materials: International "
                    "Journal of Remote Sensing, v. 11, p. 1775-1795. "
                    "(veg_1dry); Jasper Ridge spectral library (veg_2grn).",
        "note": "干枯植被与绿色植被，做植被干扰剔除时当参照。",
    }),
])

#: 文件名前缀 -> 一级分类。ENVI 那几套库都用文件名标材料大类。
MATERIAL_ROOT: Dict[str, str] = {
    "mineral": "mineral", "minerals": "mineral",
    "rock": "rock", "rocks": "rock",
    "soil": "soil", "soils": "soil",
    "vegetation": "vegetation", "veg": "vegetation",
    "water": "water",
    "manmade": "manmade", "artificial": "manmade",
    "coatings": "coating",
    "meteorite": "meteorite",
    "mixtures": "mixture",
    "organics": "organic",
    "liquids": "liquid",
    "volatiles": "volatile",
}

#: 分类节点的中文名。键是分类路径里的一段（不是整条路径）。
#: 一级用中文大类名，二三级用矿物学 / 岩石学 / 土壤分类的通行译名。
CATEGORY_CN: Dict[str, str] = {
    # 一级
    "mineral": "矿物", "rock": "岩石", "soil": "土壤", "vegetation": "植被",
    "water": "水体与冰雪", "manmade": "人造材料", "meteorite": "陨石与地外样品",
    "mixture": "混合物", "organic": "有机物", "liquid": "液体",
    "volatile": "挥发分", "coating": "表面镀层", "other": "其他",
    "unclassified": "未分类",
    # 矿物：按阴离子分类
    "silicate": "硅酸盐", "nesosilicate": "岛状硅酸盐",
    "sorosilicate": "双四面体硅酸盐", "cyclosilicate": "环状硅酸盐",
    "inosilicate": "链状硅酸盐", "phyllosilicate": "层状硅酸盐",
    "tectosilicate": "架状硅酸盐",
    "carbonate": "碳酸盐", "sulfate": "硫酸盐", "sulfide": "硫化物",
    "oxide": "氧化物", "hydroxide": "氢氧化物", "halide": "卤化物",
    "chloride": "卤化物", "phosphate": "磷酸盐", "borate": "硼酸盐",
    "arsenate": "砷酸盐", "tungstate": "钨酸盐", "vanadate": "钒酸盐",
    "molybdate": "钼酸盐", "chromate": "铬酸盐", "nitrate": "硝酸盐",
    "element": "自然元素", "organic_mineral": "有机矿物",
    "amorphous": "非晶质", "synthetic": "合成材料",
    # 岩石
    "igneous": "岩浆岩", "felsic": "长英质", "intermediate": "中性",
    "mafic": "基性", "basic": "基性", "ultramafic": "超基性",
    "alkalic": "碱性",
    "sedimentary": "沉积岩", "limestone": "灰岩", "sandstone": "砂岩",
    "shale": "页岩", "siltstone": "粉砂岩", "conglomerate": "砾岩",
    "breccia": "角砾岩", "dolomite": "白云岩", "chemical": "化学沉积岩",
    "arenaceous": "砂质岩", "argillaceous": "泥质岩", "siliceous": "硅质岩",
    "travertine": "钙华",
    "metamorphic": "变质岩", "gneiss": "片麻岩", "schist": "片岩",
    "marble": "大理岩", "quartzite": "石英岩", "slate": "板岩",
    "phyllite": "千枚岩", "hornfels": "角岩", "serpentinite": "蛇纹岩",
    # 土壤（美国土壤系统分类的土纲）
    "alfisol": "淋溶土", "aridisol": "干旱土", "entisol": "新成土",
    "inceptisol": "始成土", "mollisol": "软土", "spodosol": "灰土",
    "utisol": "老成土", "ultisol": "老成土", "vertisol": "变性土",
    # 水体
    "snow": "雪", "frost": "霜", "ice": "冰", "sea": "海水",
    "distilled": "蒸馏水", "tap": "自来水",
    # 植被
    "grass": "草本", "trees": "木本",
    # 人造材料
    "construction": "建筑材料", "roofing": "屋面材料", "asphalt": "沥青",
    "concrete": "混凝土", "target": "定标靶",
    # 陨石
    "chondrite": "球粒陨石", "achondrite": "无球粒陨石",
    "lunar": "月球样品", "anomalous": "特殊类型",
}

#: 文件名里读不出材料大类时，按来源给一个默认值。IGCP-264 的文件叫 igcp_1 ~
#: igcp_5（五台光谱仪），装的是同一批 26 个粘土与蚀变矿物标样。
SOURCE_MATERIAL: Dict[str, str] = {"igcp264": "mineral", "veg_lib": "vegetation"}

#: 上面那批标样里混着两条植被谱，名字就叫 DRYGRASS / GREENVEG。
NON_MINERAL_NAMES: Dict[str, str] = {"drygrass": "vegetation",
                                     "greenveg": "vegetation"}

#: 一级分类的显示顺序。地质用得最多的排前面。
ROOT_ORDER: Tuple[str, ...] = (
    "mineral", "rock", "soil", "vegetation", "water", "mixture",
    "meteorite", "manmade", "coating", "organic", "liquid", "volatile",
    "other",
)

#: 手工补的「矿物名 -> 化学分类」表，补 ASTER 名字里查不到的那些。
#:
#: 只在 ASTER 那份查不到时才用得上，用上了会在条目里记 ``class_source =
#: "curated"``，方便日后核对。拿不准的一律不写——落到 ``mineral/unclassified``
#: 比归错类好。非矿物（drygrass、carbon_black 之类）也不写，它们按所在文件的
#: 材料大类走。
MINERAL_CLASS: Dict[str, str] = {
    # 硅酸盐——岛状
    "allanite": "silicate/sorosilicate",
    "knebelite": "silicate/nesosilicate",
    "hydrogrossular": "silicate/nesosilicate",
    "uvarovite": "silicate/nesosilicate",
    "sphene": "silicate/nesosilicate",
    "dumortierite": "silicate/nesosilicate",
    "zunyite": "silicate/nesosilicate",
    # 硅酸盐——双四面体
    "axinite": "silicate/sorosilicate",
    "clinozoisite": "silicate/sorosilicate",
    # 硅酸盐——环状
    "elbaite": "silicate/cyclosilicate",
    # 硅酸盐——链状
    "amphibole": "silicate/inosilicate",
    "pyroxene": "silicate/inosilicate",
    "bronzite": "silicate/inosilicate",
    "pigeonite": "silicate/inosilicate",
    "fassaite": "silicate/inosilicate",
    "holmquistite": "silicate/inosilicate",
    "hornblende": "silicate/inosilicate",
    "hornblende_fe": "silicate/inosilicate",
    "hornblende_mg": "silicate/inosilicate",
    "smaragdite": "silicate/inosilicate",
    "uralite": "silicate/inosilicate",
    "nephrite": "silicate/inosilicate",
    "pyroxmangite": "silicate/inosilicate",
    "reibeckite": "silicate/inosilicate",
    "riebeckite": "silicate/inosilicate",
    # 硅酸盐——层状
    "halloysite": "silicate/phyllosilicate",
    "endellite": "silicate/phyllosilicate",
    "nacrite": "silicate/phyllosilicate",
    "beidellite": "silicate/phyllosilicate",
    "hectorite": "silicate/phyllosilicate",
    "sauconite": "silicate/phyllosilicate",
    "rectorite": "silicate/phyllosilicate",
    "prochlorite": "silicate/phyllosilicate",
    "thuringite": "silicate/phyllosilicate",
    "clinochlore": "silicate/phyllosilicate",
    "clinochlore_fe": "silicate/phyllosilicate",
    "cronstedtite": "silicate/phyllosilicate",
    "margarite": "silicate/phyllosilicate",
    "clintonite": "silicate/phyllosilicate",
    "paragonite": "silicate/phyllosilicate",
    "annite": "silicate/phyllosilicate",
    "siderophyllite": "silicate/phyllosilicate",
    "roscoelite": "silicate/phyllosilicate",
    "notronite": "silicate/phyllosilicate",
    "nontronite": "silicate/phyllosilicate",
    "illite-smectite": "silicate/phyllosilicate",
    "ammonio-smectite": "silicate/phyllosilicate",
    "ammonio-illite-smect": "silicate/phyllosilicate",
    "ammonio-illite/smect": "silicate/phyllosilicate",
    "ammonio-illite/smectit": "silicate/phyllosilicate",
    "chrysocolla": "silicate/phyllosilicate",
    # 硅酸盐——架状
    "adularia": "silicate/tectosilicate",
    "perthite": "silicate/tectosilicate",
    "celsian": "silicate/tectosilicate",
    "marialite": "silicate/tectosilicate",
    "meionite": "silicate/tectosilicate",
    "mizzonite": "silicate/tectosilicate",
    "dipyre": "silicate/tectosilicate",
    "lazurite": "silicate/tectosilicate",
    "laumontite": "silicate/tectosilicate",
    "scolecite": "silicate/tectosilicate",
    "mordenite+clinopt.": "silicate/tectosilicate",
    "chalcedony": "silicate/tectosilicate",
    "chert": "silicate/tectosilicate",
    "opal": "silicate/amorphous",
    "karpatite": "organic_mineral",
    # 氧化物 / 氢氧化物
    "brookite": "oxide",
    "cuprite": "oxide",
    "maghemite": "oxide",
    "nanohematite": "oxide",
    "psilomelane": "oxide",
    "europium_oxide": "synthetic",
    "neodymium_oxide": "synthetic",
    "praseodymium_oxide": "synthetic",
    "samarium_oxide": "synthetic",
    "diaspore": "hydroxide",
    "lepidocrocite": "hydroxide",
    "lepidocrosite": "hydroxide",
    "limonite": "hydroxide",
    "ferrihydrite": "hydroxide",
    "portlandite": "hydroxide",
    # 硫化物 / 硫酸盐
    "cinnabar": "sulfide",
    "covellite": "sulfide",
    "nickeline": "sulfide",
    "ammonio-jarosite": "sulfate",
    "butlerite": "sulfate",
    "copiapite": "sulfate",
    "coquimbite": "sulfate",
    "kieserite": "sulfate",
    "mascagnite": "sulfate",
    "schwertmannite": "sulfate",
    # 磷酸盐
    "monazite": "phosphate",
    "xenotime": "phosphate",
    "fluorapatite": "phosphate",
    "chlorapatite": "phosphate",
    "hydroxyl-apatite": "phosphate",
    "pyromorphite": "phosphate",
    # 碳酸盐
    "bastnaesite": "carbonate",
    "ancylite": "carbonate",
    "parisite": "carbonate",
    "sodium_bicarbonate": "carbonate",
    # 硝酸盐 / 自然元素
    "niter": "nitrate",
    "sulfur": "element",
}

#: 检索用的蚀变组合。**不是矿物学分类，是为了检索方便归的堆**，一个矿物可以进
#: 好几组。按经济地质里通行的蚀变分带矿物组合填写（Hunt 1977、Clark 1999 一类
#: 教科书与手册里的标准组合），只收在短波红外有明确诊断吸收、遥感识别得出来的
#: 那些。
ALTERATION_GROUPS: "OrderedDict[str, Dict[str, object]]" = OrderedDict([
    ("argillic", {
        "cn": "泥化", "en": "Argillic",
        "note": "中低温热液把长石蚀变成高岭石族与蒙脱石族，2200 nm 附近 Al-OH 吸收。",
        "minerals": ["kaolinite", "dickite", "halloysite", "nacrite", "endellite",
                     "montmorillonite", "smectite", "beidellite", "nontronite",
                     "notronite", "illite", "illite-smectite", "hectorite",
                     "saponite", "sauconite", "rectorite"],
    }),
    ("advanced_argillic", {
        "cn": "高级泥化", "en": "Advanced argillic",
        "note": "强酸性流体的产物，明矾石 2170 nm、叶蜡石 2165 nm 是标志。",
        "minerals": ["alunite", "natroalunite", "pyrophyllite", "dickite",
                     "kaolinite", "diaspore", "zunyite", "topaz", "andalusite"],
    }),
    ("phyllic", {
        "cn": "绢英岩化", "en": "Phyllic (sericitic)",
        "note": "白云母 / 绢云母 + 石英 + 黄铁矿，2200 nm 深而窄。",
        "minerals": ["muscovite", "sericite", "illite", "paragonite",
                     "phengite", "quartz", "pyrite"],
    }),
    ("propylitic", {
        "cn": "青磐岩化", "en": "Propylitic",
        "note": "绿泥石 + 绿帘石 + 方解石，2250 nm Fe-OH 与 2340 nm Mg-OH。",
        "minerals": ["chlorite", "clinochlore", "clinochlore_fe", "prochlorite",
                     "thuringite", "epidote", "clinozoisite", "zoisite",
                     "actinolite", "tremolite", "prehnite", "calcite", "albite"],
    }),
    ("carbonate", {
        "cn": "碳酸盐化", "en": "Carbonate",
        "note": "2300~2350 nm 的 CO3 吸收，方解石与白云石位置差约 20 nm。",
        "minerals": ["calcite", "dolomite", "ankerite", "siderite", "magnesite",
                     "rhodochrosite", "aragonite", "smithsonite"],
    }),
    ("silicification", {
        "cn": "硅化", "en": "Silicification",
        "note": "短波红外近乎无特征、反射率整体抬高；蛋白石在 1400 / 1900 nm 有水吸收。",
        "minerals": ["quartz", "chalcedony", "opal", "cristobalite", "tridymite",
                     "chert"],
    }),
    ("skarn", {
        "cn": "矽卡岩化", "en": "Skarn",
        "note": "钙铁石榴子石 + 透辉石 + 硅灰石组合，接触交代成因。",
        "minerals": ["grossular", "andradite", "garnet", "almandine", "spessartine",
                     "diopside", "hedenbergite", "wollastonite", "vesuvianite",
                     "epidote", "actinolite", "tremolite", "uvarovite"],
    }),
    ("iron_oxide", {
        "cn": "铁染", "en": "Iron oxide / hydroxide",
        "note": "Fe3+ 在 500 与 900 nm 的电子跃迁吸收，褐铁矿化的直接标志。",
        "minerals": ["hematite", "nanohematite", "goethite", "jarosite",
                     "ammonio-jarosite", "limonite", "ferrihydrite", "maghemite",
                     "lepidocrocite", "lepidocrosite", "magnetite",
                     "schwertmannite", "copiapite", "coquimbite"],
    }),
    ("serpentinization", {
        "cn": "蛇纹石化", "en": "Serpentinization",
        "note": "超基性岩蚀变，2320 nm 附近 Mg-OH 强吸收。",
        "minerals": ["serpentine", "antigorite", "lizardite", "chrysotile",
                     "talc", "brucite", "magnesite"],
    }),
])

# =============================================================================


def _log(msg: str) -> None:
    print(msg, flush=True)


# -----------------------------------------------------------------------------
# 读 ENVI
# -----------------------------------------------------------------------------

def _read_header(path: str) -> Dict[str, object]:
    """把 ENVI 头解析成字典。花括号里的多行值拼成一行。"""
    text = open(path, "r", encoding="latin-1").read()
    out: Dict[str, object] = {}
    for m in re.finditer(r"^([A-Za-z][A-Za-z0-9 _]*?)\s*=\s*(\{.*?\}|.*?)$",
                         text, re.S | re.M):
        key = m.group(1).strip().lower()
        val = m.group(2).strip()
        if val.startswith("{"):
            # 花括号可能跨很多行，上面的 .*? 在 re.S 下会停在第一个 }
            m2 = re.search(r"^%s\s*=\s*\{(.*?)\}" % re.escape(m.group(1)),
                           text, re.S | re.M)
            val = m2.group(1) if m2 else val.strip("{}")
        out[key] = val
    return out


def _numbers(text: str) -> np.ndarray:
    return np.array([float(t) for t in text.replace("\n", " ").split(",")
                     if t.strip()], dtype=np.float64)


def _spectra_names(text: str) -> List[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


def read_sli(hdr_path: str) -> Tuple[np.ndarray, np.ndarray, List[str], float]:
    """读一对 ``.hdr`` / ``.sli``，返回 (波长 nm, 数据 lines×samples, 名字, 缩放)。"""
    hdr = _read_header(hdr_path)
    lines = int(hdr["lines"])
    samples = int(hdr["samples"])
    dtype = int(hdr["data type"])
    if dtype != 4:
        raise ValueError("%s: 只处理 float32（data type 4），这份是 %d"
                         % (hdr_path, dtype))
    order = "<" if str(hdr.get("byte order", "0")).strip() == "0" else ">"
    wl = _numbers(str(hdr["wavelength"]))
    if wl.size != samples:
        raise ValueError("%s: 波长个数 %d 与 samples %d 对不上"
                         % (hdr_path, wl.size, samples))
    unit = str(hdr.get("wavelength units", "")).strip().lower()
    if unit.startswith("micro"):
        wl = wl * 1000.0                      # 统一成 nm
    elif unit.startswith("nano") or unit.startswith("nm"):
        pass
    else:
        raise ValueError("%s: 看不懂的波长单位 %r" % (hdr_path, unit))

    names = _spectra_names(str(hdr.get("spectra names", "")))
    if len(names) != lines:
        names = (names + ["spectrum %d" % (i + 1)
                          for i in range(len(names), lines)])[:lines]

    scale = float(str(hdr.get("reflectance scale factor", "1")).strip() or 1.0)
    data = np.fromfile(hdr_path[:-4] + ".sli", dtype=order + "f4")
    if data.size != lines * samples:
        raise ValueError("%s: 数据 %d 个数，头里说应该是 %d"
                         % (hdr_path, data.size, lines * samples))
    return wl, data.reshape(lines, samples), names, scale


def _unit_level(values: np.ndarray) -> float:
    """一份文件的量纲水平：``|y|`` 的 95 分位。

    用 95 分位而不是最大值：实测反射率在信噪比差的波段边缘冒到 1.0 以上是常
    事（USGS 的植被谱在 2.7 µm 处能到 2.0），那是测量噪声，不是量纲错。真正
    存成百分数的文件，95 分位在 90 上下，与小数反射率差着两个数量级，一眼分得开。
    """
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 0.0
    return float(np.percentile(np.abs(finite), 95))


def normalise(block: np.ndarray, scale: float) -> np.ndarray:
    """量纲统一成小数反射率，无效值统一成 NaN。"""
    out = block.astype(np.float64)
    bad = ~np.isfinite(out) | (np.abs(out) > VALID_ABS_MAX)
    out[bad] = np.nan
    if scale and scale != 1.0:
        out = out / scale
    return out


# -----------------------------------------------------------------------------
# 名字解析
# -----------------------------------------------------------------------------

#: 元素符号表。ASTER 的矿物名后面直接跟化学式，而化学式常以一串裸元素符号开头
#: （``Jarosite K Fe 3(SO 4) 2(OH) 6``）。只靠"有没有数字"切不开——切出来的名字
#: 会变成"Jarosite K"，拿它去查分类表当然查不着。
_ELEMENTS = frozenset(
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni "
    "Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I "
    "Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt "
    "Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U".split())

#: 括号里只有一个单词的，是矿物族名而不是化学式的一部分：``Albite (Feldspar)``。
_GROUP_PAREN = re.compile(r"^\(([A-Za-z][A-Za-z\-]*)\)$")


def _is_formula_token(token: str) -> bool:
    tok = token.strip(",")
    if not tok:
        return False
    if any(ch.isdigit() for ch in tok):
        return True
    if any(ch in "()[]^+\u00b7" for ch in tok):
        return True
    return tok in _ELEMENTS


def _split_formula(head: str) -> Tuple[str, str, str]:
    """``Albite (Feldspar) (K Na)AlSi 3O 8`` -> ("Albite", "Feldspar", 化学式)。

    从右往左吃：只要还像化学式就继续往左，遇到第一个不像的就停。这样裸元素符号
    也会被吃进化学式里，名字才干净。整段没有数字就当它压根没有化学式——"Alkalic
    Granite" 不能因为末尾是个大写词就被切掉一半。
    """
    words = head.split()
    if not words:
        return (head.strip(), "", "")
    cut = len(words)
    while cut > 1 and _is_formula_token(words[cut - 1]):
        cut -= 1
    tail = words[cut:]
    if not any(ch.isdigit() for w in tail for ch in w):
        cut = len(words)
        tail = []
    group = ""
    if cut < len(words):
        m = _GROUP_PAREN.match(words[cut])
        if m:
            group = m.group(1)
            cut += 1
            tail = words[cut:]
    name = " ".join(words[:cut]).strip(" ,")
    return (name or head.strip(), group, " ".join(tail).strip())


def parse_aster(raw: str) -> Dict[str, str]:
    m = re.match(r"^(.*?)\s*\[([^\]]*)\]\s*$", raw)
    head, bracket = (m.group(1), m.group(2)) if m else (raw, "")
    parts = [p.strip() for p in bracket.split("-")] if bracket else []
    name, group, formula = _split_formula(head)
    return {
        "name": name, "group": group, "formula": formula,
        "sample": parts[3] if len(parts) > 3 else "",
        "state": parts[2] if len(parts) > 2 and parts[2] != "none" else "",
        "b1": parts[0] if parts else "",
        "b2": parts[1] if len(parts) > 1 and parts[1] != "none" else "",
    }


#: USGS v6 名字尾巴上的测量代码，如 ``[W1R1Bb AREF]``。
_V6_CODE = re.compile(r"\s*\[([^\]]*)\]\s*$")
#: 样品号：NMNH…、GDS…、HS…、CU…、WS… 这些馆藏/野外编号。
_SAMPLE_ID = re.compile(r"^(NMNH|GDS|HS|CU|WS|BM|SCR|SC-|CO-|AZ-|MV|LV|CA-)", re.I)


def parse_usgs6(raw: str) -> Dict[str, str]:
    code = ""
    m = _V6_CODE.search(raw)
    if m:
        code = m.group(1).strip()
        raw = raw[:m.start()]
    words = raw.split()
    name = words[0] if words else raw
    sample, extra = "", []
    for w in words[1:]:
        if not sample and _SAMPLE_ID.match(w):
            sample = w
        else:
            extra.append(w)
    return {"name": name, "sample": sample, "group": " ".join(extra),
            "instrument": code, "formula": ""}


#: USGS v7 名字最后一段是仪器代码：ASDFRb、NIC4bbb、BECKb、AVIRISb…
_V7_INSTRUMENT = re.compile(r"^(ASD|NIC|BECK|AVIRIS|CRSM|M3|CRISM)", re.I)


def parse_usgs7(raw: str) -> Dict[str, str]:
    parts = [p for p in raw.split("_") if p]
    instrument = ""
    if len(parts) > 1 and _V7_INSTRUMENT.match(parts[-1]):
        instrument = parts.pop()
    name = parts[0] if parts else raw
    sample, extra = "", []
    for p in parts[1:]:
        if not sample and (_SAMPLE_ID.match(p) or re.match(r"^[A-Z]{1,3}[0-9]", p)):
            sample = p
        else:
            extra.append(p)
    return {"name": name, "sample": sample, "group": " ".join(extra),
            "instrument": instrument, "formula": ""}


def parse_igcp(raw: str) -> Dict[str, str]:
    parts = raw.split("_")
    name = parts[0].strip()
    name = name[:1].upper() + name[1:].lower() if name.isupper() else name
    return {"name": name, "sample": parts[1] if len(parts) > 1 else "",
            "group": "", "instrument": "", "formula": ""}


def parse_veg(raw: str) -> Dict[str, str]:
    sample, _, rest = raw.partition(":")
    if not rest:
        sample, rest = "", raw
    return {"name": rest.strip() or sample.strip(), "sample": sample.strip(),
            "group": "", "instrument": "", "formula": ""}


PARSERS = {"aster": parse_aster, "usgs_v6": parse_usgs6, "usgs_v7": parse_usgs7,
           "igcp264": parse_igcp, "veg_lib": parse_veg}


# -----------------------------------------------------------------------------
# 分类
# -----------------------------------------------------------------------------

#: ASTER 的分类段里有几个同义写法，落库前统一，免得树上并排挂着两个"卤化物"。
CLASS_ALIAS: Dict[str, str] = {
    "chloride": "halide",
    "ultisol": "utisol",
    "basic": "mafic",
}


def canonical_class(path: str) -> str:
    parts = path.split("/")
    return "/".join(CLASS_ALIAS.get(p, p) for p in parts)


def mineral_keys(name: str) -> List[str]:
    """一个矿物名可以用来查表的几个键，从最具体到最宽。

    ``Clinochlore_Fe`` 先按整名查，再按 ``clinochlore`` 查；ASTER 那边收来的
    ``Almandine Garnet`` 也能被 USGS 的 ``Almandine`` 命中。
    """
    key = re.sub(r"\s*\(.*?\)\s*", " ", name.strip().lower()).strip(" ,.")
    key = re.sub(r"\s+", " ", key)
    out = [key]
    for probe in (re.split(r"[_/+]", key)[0], key.split(" ")[0]):
        probe = probe.strip(" ,.")
        if probe and probe not in out:
            out.append(probe)
    return [k for k in out if k]


def harvest_aster_classes(src_dir: str) -> Dict[str, str]:
    """从 ASTER 的矿物名字里收出「矿物名 -> 化学分类」。

    同名冲突时取出现次数最多的那个分类——ASTER 自己偶尔把同一个矿物在两个文件里
    归到不同的段（比如 chloride 与 halide），少数服从多数即可。
    """
    votes: Dict[str, Counter] = {}
    root = os.path.join(src_dir, SOURCES["aster"]["dir"])
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".hdr") or not fn.startswith("mineral"):
            continue
        hdr = _read_header(os.path.join(root, fn))
        for raw in _spectra_names(str(hdr.get("spectra names", ""))):
            info = parse_aster(raw)
            if not info["b1"]:
                continue
            path = info["b1"] + ("/" + info["b2"] if info["b2"] else "")
            for key in mineral_keys(info["name"]):
                votes.setdefault(key, Counter())[path] += 1
    return {k: c.most_common(1)[0][0] for k, c in votes.items()}


def classify(source: str, material: str, info: Dict[str, str],
             aster_classes: Dict[str, str]) -> Tuple[str, str]:
    """返回 (分类路径, 分类来源)。"""
    root = MATERIAL_ROOT.get(material, "other")
    for key in mineral_keys(info.get("name", "")):
        if key in NON_MINERAL_NAMES:
            return (NON_MINERAL_NAMES[key], "name")

    if source == "aster" and info.get("b1"):
        b1 = info["b1"]
        if root == "mineral":
            path = "mineral/" + b1 + ("/" + info["b2"] if info["b2"] else "")
        elif root == "rock":
            path = "rock/" + b1 + ("/" + info["b2"] if info["b2"] else "")
        else:
            # 土壤只切到土纲，水体 / 植被 / 人造材料只切到第二级：再往下
            # （haplustalf 这种亚纲）分得太碎，一个节点挂一两条，翻起来比搜还慢。
            path = root + "/" + b1
        return (canonical_class(path), "aster")

    if root == "mineral":
        for probe in mineral_keys(info.get("name", "")):
            if probe in MINERAL_CLASS:
                return ("mineral/" + canonical_class(MINERAL_CLASS[probe]), "curated")
            if probe in aster_classes:
                return ("mineral/" + canonical_class(aster_classes[probe]), "aster")
        return ("mineral/unclassified", "")

    return (root, "file")


def alteration_of(name: str) -> List[str]:
    keys = set(mineral_keys(name))
    return [gkey for gkey, g in ALTERATION_GROUPS.items()
            if keys & set(g["minerals"])]


# -----------------------------------------------------------------------------
# 写库
# -----------------------------------------------------------------------------

def _grid_key(wl: np.ndarray) -> str:
    import hashlib
    h = hashlib.sha1(np.round(wl, 4).astype("<f8").tobytes()).hexdigest()[:10]
    return "g%d_%s" % (wl.size, h)


def _drop_v6_twins(files: List[str]) -> List[str]:
    """v6 里同名两份只留波段少的那份（仪器原生网格）。"""
    by_names: Dict[Tuple[str, ...], List[Tuple[int, str]]] = {}
    for path in files:
        hdr = _read_header(path)
        key = tuple(_spectra_names(str(hdr.get("spectra names", ""))))
        by_names.setdefault(key, []).append((int(hdr["samples"]), path))
    keep = []
    for group in by_names.values():
        group.sort()
        keep.append(group[0][1])
        for _n, path in group[1:]:
            _log("    跳过重采样副本 %s" % os.path.basename(path))
    return sorted(keep)


def build() -> None:
    if not os.path.isdir(SRC_DIR):
        raise SystemExit("找不到光谱库目录：%s\n改一下脚本顶上的 SRC_DIR。" % SRC_DIR)

    _log("==> 收 ASTER 自带的矿物分类")
    aster_classes = harvest_aster_classes(SRC_DIR)
    _log("    %d 个矿物名" % len(aster_classes))

    entries: List[Dict[str, object]] = []
    grids: "OrderedDict[str, np.ndarray]" = OrderedDict()
    blocks: "OrderedDict[str, Dict[str, object]]" = OrderedDict()
    payload: Dict[str, bytes] = {}
    class_source_count: Counter = Counter()
    skipped_units: List[Tuple[str, float]] = []

    for source in INCLUDE_SOURCES:
        meta = SOURCES[source]
        root = os.path.join(SRC_DIR, meta["dir"])
        if not os.path.isdir(root):
            _log("!!! 没有 %s，跳过" % root)
            continue
        files = sorted(os.path.join(root, f) for f in os.listdir(root)
                       if f.endswith(".hdr"))
        if source == "usgs_v6" and V6_DROP_RESAMPLED:
            files = _drop_v6_twins(files)
        _log("==> %s：%d 个文件" % (source, len(files)))

        for path in files:
            stem = os.path.splitext(os.path.basename(path))[0]
            material = stem.split("_")[0].lower()
            if material not in MATERIAL_ROOT:
                material = SOURCE_MATERIAL.get(source, material)
            wl, data, names, scale = read_sli(path)

            order = np.argsort(wl)
            if not np.all(order == np.arange(wl.size)):
                wl, data = wl[order], data[:, order]

            gid = _grid_key(wl)
            if gid not in grids:
                grids[gid] = wl
                payload["grid/%s.f4" % gid] = wl.astype("<f4").tobytes()

            values = normalise(data, scale)
            level = _unit_level(values)
            if level > MAX_VALUE_P95:
                # 整份文件都不是小数反射率。最典型的是 ASTER 那套里的
                # ``*_transmit``：它装的是 KBr 压片的**透过率百分数**（0~100），
                # 头里的 reflectance scale factor 又老实写着 1，于是原样进库，
                # 画到反射率轴上是 20~110 那种数量级，一条就把整幅图的纵轴撑坏；
                # 拿去做光谱匹配更糟——透过率与反射率是两种物理量，比出来的名次
                # 没有意义。宁可不收：这份库的 manifest 里写着 value=reflectance，
                # 收进来就是自己违反自己的约定。
                _log("!!! %s：|y| 的 95 分位 %.4g，不是小数反射率，整份跳过"
                     % (stem, level))
                skipped_units.append((stem, round(level, 4)))
                continue
            bid = "%s_%s" % (source, stem)
            payload["block/%s.f2" % bid] = values.astype(STORE_DTYPE).tobytes()
            blocks[bid] = {"grid": gid, "rows": int(values.shape[0]),
                           "source": source, "file": stem}

            parse = PARSERS[source]
            for row, raw in enumerate(names):
                info = parse(raw)
                path_, csrc = classify(source, material, info, aster_classes)
                class_source_count[csrc or "unknown"] += 1
                finite = np.isfinite(values[row])
                if not finite.any():
                    continue                      # 整条都是无效值，不入库
                lo = float(wl[finite][0])
                hi = float(wl[finite][-1])
                item: Dict[str, object] = {
                    "id": "%s/%s/%d" % (source, stem, row),
                    "name": info.get("name", raw),
                    "raw": raw,
                    "cls": path_,
                    "src": source,
                    "blk": bid,
                    "row": row,
                    "wl": [round(lo, 2), round(hi, 2)],
                    "n": int(finite.sum()),
                }
                for key in ("sample", "group", "instrument", "formula", "state"):
                    if info.get(key):
                        item[key] = info[key]
                if csrc:
                    item["csrc"] = csrc
                if path_.startswith("mineral"):
                    tags = alteration_of(info.get("name", ""))
                    if tags:
                        item["alt"] = tags
                if source == "usgs_v6":
                    item["superseded"] = 1
                entries.append(item)

    _log("==> 共 %d 条光谱，%d 个波长网格，%d 个数据块"
         % (len(entries), len(grids), len(blocks)))
    _log("    分类来源：%s" % dict(class_source_count))
    if skipped_units:
        _log("    因量纲不是小数反射率而整份跳过：%s"
             % "、".join("%s(p95=%.4g)" % kv for kv in skipped_units))

    manifest = {
        "format": "lithospectra-speclib",
        "version": 1,
        "title": "LithoSpectra 光谱库",
        "built": datetime.date.today().isoformat(),
        "count": len(entries),
        "wavelength_unit": "nm",
        "value": "reflectance",
        "dtype": STORE_DTYPE,
        "sources": {k: {kk: vv for kk, vv in SOURCES[k].items() if kk != "dir"}
                    for k in INCLUDE_SOURCES},
        "grids": {gid: {"n": int(w.size), "lo": round(float(w[0]), 2),
                        "hi": round(float(w[-1]), 2)}
                  for gid, w in grids.items()},
        "blocks": blocks,
        "categories": _category_table(entries),
        "groups": [dict(key=k, cn=v["cn"], en=v["en"], note=v["note"])
                   for k, v in ALTERATION_GROUPS.items()],
        "notes": (
            "分类路径 cls：ASTER 那套取自源数据名字里的方括号；USGS / IGCP 的矿物"
            "先查 ASTER 收来的矿物名表，查不到再查脚本里手工补的 MINERAL_CLASS。"
            "每条的 csrc 字段记着它是哪来的（aster / curated / file，空表示未分类）。"
            "alt 字段是检索用的蚀变组合，属人为归堆，不是矿物学分类。"),
        "skipped": [{"file": f, "p95": v} for f, v in skipped_units],
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("manifest.json",
                   json.dumps(manifest, ensure_ascii=False, indent=1))
        z.writestr("index.json",
                   json.dumps({"entries": entries}, ensure_ascii=False))
        for name, blob in payload.items():
            z.writestr(name, blob)
    os.replace(tmp, OUT_PATH)
    _log("==> 写好 %s（%.1f MB）" % (OUT_PATH, os.path.getsize(OUT_PATH) / 1e6))


def _category_table(entries: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """把出现过的分类路径整理成一棵树的节点清单，带中文名与条数。"""
    count: Counter = Counter()
    for e in entries:
        parts = str(e["cls"]).split("/")
        for i in range(1, len(parts) + 1):
            count["/".join(parts[:i])] += 1

    def sort_key(path: str) -> Tuple[int, int, str]:
        head = path.split("/")[0]
        rank = ROOT_ORDER.index(head) if head in ROOT_ORDER else len(ROOT_ORDER)
        return (rank, path.count("/"), path)

    out = []
    for path in sorted(count, key=sort_key):
        leaf = path.split("/")[-1]
        out.append({"path": path, "cn": CATEGORY_CN.get(leaf, leaf),
                    "en": leaf.replace("_", " ").title(), "count": count[path]})
    return out


if __name__ == "__main__":
    build()
