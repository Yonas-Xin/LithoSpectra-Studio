# -*- coding: utf-8 -*-
"""随包光谱库里矿物、岩石规则覆盖不到的条目的中文名：植被、人造材料、有机物、陨石、
土壤、液体与少数罕见矿物。

USGS / ASTER / ECOSTRESS 的条目名是英文自由文本，没有统一规则，这里按条目名逐条
对照。括号里的部位、状态（``Pinyon Pine (Grey Bark)``）拆开分别翻译；月壤按样品号写。
"""

from __future__ import annotations

import re
from typing import Dict

#: 条目名（小写）→ 中文名
NAMES: Dict[str, str] = {
    # ---------------------------------------------------------------- 人造材料
    "asphaltic concrete": "沥青混凝土", "black gloss paint": "黑色亮光漆", "black paint": "黑漆",
    "construction asphalt": "建筑沥青", "construction concrete": "建筑混凝土", "construction tar": "建筑焦油",
    "pine wood": "松木", "aluminum metal": "铝", "bare red brick": "裸红砖", "black tar paper": "黑色油毡",
    "cinders ashen": "灰色炉渣", "copper metal": "铜", "galvanized steel metal": "镀锌钢",
    "olive green gloss paint": "橄榄绿亮光漆", "olive green paint": "橄榄绿漆",
    "oxidized galvanized steel metal": "氧化镀锌钢", "red smooth-faced brick": "光面红砖",
    "reddish asphalt roofing shingle": "红色沥青屋面瓦", "reddish asphalt shingle": "红色沥青瓦",
    "terra cotta tiles": "赤陶瓦", "weathered red brick": "风化红砖", "black unspecified rubber": "黑色橡胶",
    "plate window glass": "平板窗玻璃", "slate stone shingle": "板岩屋面瓦", "white marble": "白色大理石",
    "white fiberglass unspecified rubber": "白色玻璃纤维橡胶", "white rubberized coating": "白色橡胶涂层",
    "asphalt shingle": "沥青瓦", "asphalt roofing shingle": "沥青屋面瓦", "flat black paint": "哑光黑漆",
    "gloss black paint": "亮光黑漆", "glossy black paint": "亮光黑漆", "lamp (ebony) black paint": "灯黑漆",
    "metallic silver paint": "银色金属漆", "ultra flat black paint": "超哑光黑漆", "black nextel paint": "Nextel 黑漆",
    "brass plate": "黄铜板", "gold plate": "金板", "alizarin": "茜素", "aluminum": "铝", "asphalt": "沥青",
    "bone": "骨", "brick": "砖", "brilliant(hansa)yellowgds785": "汉莎黄颜料", "burlap": "粗麻布",
    "burnt": "焦土色颜料", "cadmium": "镉颜料", "cardboard": "纸板", "cedar": "雪松", "cerium": "铈",
    "cerulean": "天蓝颜料", "cinder": "炉渣", "coated": "涂层材料", "cobalt": "钴蓝颜料", "concrete": "混凝土",
    "cotton": "棉", "dysprosium": "镝", "erbium": "铒", "europium": "铕", "fiberglass": "玻璃纤维",
    "gadolinium": "钆", "galvanizedsheetmetal": "镀锌板", "holmium": "钬", "ironpowder": "铁粉", "iron": "铁",
    "italian": "意大利土颜料", "lanthanum": "镧", "lazurite": "青金石颜料", "lead-tin": "铅锡黄颜料",
    "lead": "铅", "lutetium": "镥", "malachite": "孔雀石颜料", "manganese": "锰", "naples": "那不勒斯黄颜料",
    "nat.madder": "天然茜草颜料", "natural": "天然颜料", "neodymium": "钕", "nickel": "镍", "nylon": "尼龙",
    "oil": "油", "oiledmarshplant": "油污沼泽植物", "oiled": "油污样品", "oxide": "氧化物颜料",
    "painted": "涂漆表面", "paper": "纸", "particle": "刨花板", "phosphoricacid": "磷酸",
    "phthalocyaninie": "酞菁颜料", "plastic": "塑料", "plywood": "胶合板", "polyester": "聚酯",
    "polystyrene": "聚苯乙烯", "praseodymium": "镨", "prussian": "普鲁士蓝", "roofing": "屋面材料",
    "rusted": "锈蚀表面", "samarium": "钐", "scandium": "钪", "sheet": "板材",
    "spectralon99whiteref": "Spectralon 99% 白板", "strand": "定向刨花板", "tar": "焦油", "terbium": "铽",
    "terra": "赭土颜料", "terrycloth": "毛巾布", "thulium": "铥", "titanium": "钛白颜料",
    "ultramarine": "群青颜料", "venetian": "威尼斯红颜料", "verdigris": "铜绿颜料",
    "vermiculite": "蛭石", "vermillion": "朱砂颜料", "viridian": "铬绿颜料", "wtc": "世贸中心粉尘",
    "wood": "木材", "yellow": "黄色颜料", "ytterbium": "镱", "yttrium": "钇", "zinc": "锌",
    "potassium": "钾盐", "sodium": "钠盐", "ammonium": "铵盐", "cyanide": "氰化物", "green": "绿色颜料",
    "kerogen": "干酪根", "renyolds": "Reynolds 铝材", "mylar": "聚酯薄膜",
    # ---------------------------------------------------------------- 陨石
    "alh": "阿伦山陨石", "alha": "阿伦山陨石", "alais": "阿莱陨石", "bholghati": "博尔加蒂陨石",
    "bialystok": "比亚韦斯托克陨石", "bununu": "布努努陨石", "dingo pup donga": "丁戈帕普冬加陨石",
    "eeta": "伊丽莎白山陨石", "happy canyon": "快乐峡谷陨石", "haraiya": "哈赖亚陨石", "isna": "伊斯纳陨石",
    "ivuna": "伊武纳陨石", "johnstown": "约翰斯敦陨石", "la criolla": "拉克里奥利亚陨石", "marilia": "马里利亚陨石",
    "murray": "默里陨石", "new concord": "新康科德陨石", "olivenza": "奥利文萨陨石", "ornans": "奥尔南陨石",
    "pca": "派珀斯克拉格陨石", "petersburg": "彼得斯堡陨石", "sao jose do rio preto": "圣若泽杜里奥普雷托陨石",
    "shelburn": "谢尔本陨石", "til": "蒂尔陨石", "tierra blanca": "蒂耶拉布兰卡陨石", "uberaba": "乌贝拉巴陨石",
    "ucera": "乌塞拉陨石", "warrenton": "沃伦顿陨石", "winona": "威诺纳陨石", "yamato": "大和陨石",
    "allende": "阿连德陨石", "karoonda": "卡龙达陨石", "lance": "朗瑟陨石", "millen": "米伦陨石",
    "murchison": "默奇森陨石", "orgueil": "奥盖尔陨石",
    # ---------------------------------------------------------------- 罕见矿物
    "richterite": "钠透闪石", "howlite": "硅硼钙石", "eugsterite": "钠钙矾", "kaliborite": "钾硼镁石",
    "rivadavite": "镁钠硼石", "smaragdite": "绿闪石", "karpatite": "黄地蜡", "butlerite": "基铁矾",
    "dipyre": "针柱石", "fassaite": "斜辉石（富铝）", "mizzonite": "中柱石", "pitch": "沥青",
    "prochlorite": "蠕绿泥石", "thuringite": "鳞绿泥石", "olivine basalt porphyry": "橄榄玄武斑岩",
    # ---------------------------------------------------------------- 土壤
    "black loam": "黑色壤土", "brown fine sandy loam": "棕色细砂壤土", "brown loamy fine sand": "棕色壤质细砂土",
    "brown sandy loam": "棕色砂壤土", "brown silty loam": "棕色粉壤土",
    "brown to dark brown gravelly fine sandy loam": "棕—暗棕色砾质细砂壤土",
    "brown to dark brown gravelly loam": "棕—暗棕色砾质壤土", "brown to dark brown loamy sand": "棕—暗棕色壤质砂土",
    "brown to dark brown sand": "棕—暗棕色砂土", "brown to dark brown sandy loam": "棕—暗棕色砂壤土",
    "dark brown fine sandy loam": "暗棕色细砂壤土", "dark grayish brown silty loam": "暗灰棕色粉壤土",
    "dark reddish brown fine sandy loam": "暗红棕色细砂壤土",
    "dark reddish brown organic-rich silty loam": "暗红棕色富有机质粉壤土",
    "dark yellowish brown micaceous loam": "暗黄棕色含云母壤土", "gray silty clay": "灰色粉质黏土",
    "gray/dark brown extremely stoney coarse sandy": "灰—暗棕色多石粗砂土", "grayish brown loam": "灰棕色壤土",
    "pale brown silty loam": "浅棕色粉壤土", "reddish brown fine sandy loam": "红棕色细砂壤土",
    "vary dark grayish brown loam": "极暗灰棕色壤土", "very dark grayish brown loam": "极暗灰棕色壤土",
    "very dark grayish brown silty loam": "极暗灰棕色粉壤土", "white gypsum dune sand.": "白色石膏沙丘砂",
    "brown gravelly sandy loam": "棕色砾质砂壤土", "brown to dark brown clay": "棕—暗棕色黏土",
    "brown to dark brown silt loam": "棕—暗棕色粉壤土", "dark brown interior moist clay loam": "暗棕色湿润黏壤土",
    "dark yellowish brown silty clay": "暗黄棕色粉质黏土", "light yellowish brown clay": "浅黄棕色黏土",
    "light yellowish brown interior dry gravelly loam": "浅黄棕色干燥砾质壤土",
    "light yellowish brown loam": "浅黄棕色壤土", "light yellowish brown loamy sand": "浅黄棕色壤质砂土",
    "pale brown dry silty clay loam": "浅棕色干燥粉质黏壤土", "very dark grayish brown loamy sand": "极暗灰棕色壤质砂土",
    "very pale brown to brownish yellow interior dry gravelly silt loam": "极浅棕—棕黄色干燥砾质粉壤土",
    "alluvial sand": "冲积砂", "fluvial deposit": "河流沉积物", "grey calcareous silty soil": "灰色钙质粉土",
    "red-orange sandy loam": "红橙色砂壤土", "red-orange sandy loam/alluvium": "红橙色砂壤土（冲积）",
    "red-orange sandy soil": "红橙色砂土", "red-orange sandy soil.": "红橙色砂土",
    "sandy loam/alluvium": "砂壤土（冲积）", "acid": "酸性土", "burnarea": "火烧迹地", "hematitic": "含赤铁矿土",
    "hydrated": "水合土", "limestone": "石灰岩", "muscovite-low-al": "白云母（低铝）",
    "muscovite-medhi-al": "白云母（中高铝）", "muscovite-medlowal": "白云母（中低铝）", "opalized": "蛋白石化土",
    "sand": "砂", "stonewall": "石墙", "white": "白色土", "blue": "蓝色土",
    "actinolite-hornfels": "阳起石角岩", "cordierite-qtzmus": "堇青石—石英—白云母",
    "chlorite.67+calcit.33": "绿泥石 67% + 方解石 33%", "calcite.7+kaolin-wxl.3": "方解石 70% + 高岭石 30%",
    "erionite+merlinoite": "毛沸石 + 麦钾沸石", "alun366+.50pyropys1a": "明矾石 + 叶蜡石", "teepleite+trona": "氯硼钠石 + 天然碱",
    # ---------------------------------------------------------------- 植被
    "dry grass": "干草", "drygrass": "干草", "conifer": "针叶树", "decidous": "落叶树", "grass": "草",
    "aspen": "山杨", "buckbrush": "鹿灌木", "cactus": "仙人掌", "chamise": "鹰爪灌木", "cheatgrass": "旱雀麦",
    "d.spicata": "穗状盐草", "engelmann-spruce": "恩格曼云杉", "flower": "花", "gray-pine": "灰松",
    "hyacinth": "风信子", "j.roemer.": "罗氏灯心草", "leafyspurge": "乳浆大戟", "lichen": "地衣",
    "lodgepole-pine": "扭叶松", "lythr.lineare": "线叶千屈菜", "manzanita": "熊果", "marshwater": "沼泽水",
    "marsh": "沼泽植被", "oak": "栎树", "p.aus.": "芦苇", "p.austr.": "芦苇", "p.australis": "芦苇",
    "rangeland": "草原牧场", "s.altern.": "互花米草", "s.alternif.": "互花米草", "s.alterniflora": "互花米草",
    "s.america.": "美洲藨草", "s.americanus": "美洲藨草", "sagebrush": "山艾", "spar.patens": "狐米草",
    "toyon": "柳叶石楠", "willow": "柳", "yerba": "草本植物", "bacterial": "细菌", "cattail": "香蒲",
    "conifer-meadow-mix": "针叶林—草甸混合", "douglas-fir": "花旗松", "espruce-sfir": "恩格曼云杉—亚高山冷杉",
    "grass-fescueneedlg": "羊茅—针茅草地", "grass-fescuewheatg": "羊茅—冰草草地", "grass-smoothbrome": "无芒雀麦",
    "sedge": "莎草", "wetland": "湿地植被", "whitebarkpine": "白皮松", "willow-sedge-mix": "柳—莎草混合",
    "blackbrush": "黑灌木", "juniper": "刺柏", "lawngrass": "草坪草", "lawn": "草坪", "maple": "枫树",
    "pinon": "矮松", "rabbitbrush": "兔灌木", "russian": "俄罗斯橄榄", "saltbrush": "滨藜", "stonwll": "石墙",
    "tumbleweed": "风滚草", "walnut": "胡桃", "greenveg": "绿色植被", "spruce cellulose": "云杉纤维素",
    "cotton cellulose": "棉纤维素", "wet cotton cellulose": "湿棉纤维素", "loblolly pine lignin": "火炬松木质素",
    "sweetgum lignin": "枫香木质素", "b-glucan": "β-葡聚糖", "xylan (oat)": "木聚糖（燕麦）",
    "arabinogalactan (larch)": "阿拉伯半乳聚糖（落叶松）", "citrus pectin": "柑橘果胶", "apple pectin": "苹果果胶",
    "carnauba wax": "巴西棕榈蜡", "tannic acid": "单宁酸", "d-rib 1 5-diph carboxylase": "核酮糖二磷酸羧化酶",
    "humic acid": "腐殖酸", "giant wildrye": "大赖草", "bigberry manzanita": "大果熊果", "mormon tea": "麻黄",
    "big sagebrush": "三齿蒿", "white peppermint": "白椒薄荷桉", "pinyon pine": "矮松", "sycamore": "悬铃木",
    "ca buckwheat": "加州荞麦", "arroyo willow": "溪柳", "bay laurel": "加州月桂", "blue oak": "蓝栎",
    "california valley oak": "加州谷栎", "c. buckeye": "加州七叶树", "coast redwood": "北美红杉",
    "coast sage": "海岸鼠尾草", "common buck bush": "鹿灌木", "coyote bush": "郊狼灌木", "dove weed": "鸽草",
    "leather oak": "革叶栎", "live oak": "常绿栎", "madrone": "浆果鹃", "red willow": "红柳", "tarweed": "焦油草",
    "jasper ridge butano sandstone": "贾斯珀岭布塔诺砂岩", "jasper ridge grassland soil": "贾斯珀岭草地土壤",
    "jasper ridge gravel": "贾斯珀岭砾石", "jasper ridge serpentine": "贾斯珀岭蛇纹岩",
    # ---------------------------------------------------------------- 涂层、液体
    "blck": "黑色涂层", "desert": "荒漠漆", "melting": "融雪", "red": "红色液体",
    # ---------------------------------------------------------------- 有机物
    "acetylene": "乙炔", "benzene": "苯", "butane": "丁烷", "decane": "癸烷", "ethane": "乙烷",
    "ethylene": "乙烯", "heptane": "庚烷", "hexane": "己烷", "methane": "甲烷", "naphthalene": "萘",
    "nonane": "壬烷", "octane": "辛烷", "paraffin": "石蜡", "pentane": "戊烷", "peptidogylcan": "肽聚糖",
    "propane": "丙烷", "propene": "丙烯", "propyne": "丙炔", "toluene": "甲苯", "xylene(s)": "二甲苯",
    "1-2-3-trimethylbenzene": "1,2,3-三甲苯", "2-4-diaminopyrimidine": "2,4-二氨基嘧啶",
    "2-4diamino6hydroxypyrim": "2,4-二氨基-6-羟基嘧啶", "2-4diamino6hydroxypyrimidine": "2,4-二氨基-6-羟基嘧啶",
    "2-4diamino6hydroxypyrimidin": "2,4-二氨基-6-羟基嘧啶", "2-6-diaminopurine": "2,6-二氨基嘌呤",
    "2-aminopurine": "2-氨基嘌呤", "2-methylnaptha": "2-甲基萘", "2-methylnapthalen": "2-甲基萘",
    "4-3h-pyrimidin": "4(3H)-嘧啶酮", "4-3h-pyrimidinone": "4(3H)-嘧啶酮", "4-3h-pyrimidi": "4(3H)-嘧啶酮",
    "5-aminoorotic": "5-氨基乳清酸", "5-aminooroticacid": "5-氨基乳清酸", "5-aminouracil": "5-氨基尿嘧啶",
    "5-hydroxymethyluracil": "5-羟甲基尿嘧啶", "5-phenyl-1h-tetrazole": "5-苯基四唑",
    "5-phenyl-1h-tetrz": "5-苯基四唑", "5-phenyl-1h-tetrazol": "5-苯基四唑", "6-cyanopurine": "6-氰基嘌呤",
    "7-8-benzoquino": "7,8-苯并喹啉", "7-8-benzoquinoline": "7,8-苯并喹啉", "9-anthronitrile": "9-蒽甲腈",
    "9anthronitrile": "9-蒽甲腈", "acridine": "吖啶", "adenine": "腺嘌呤", "allylbenzene": "烯丙基苯",
    "amylose": "直链淀粉", "anthrone": "蒽酮", "antipyrine": "安替比林", "benzopyrene": "苯并芘",
    "biphenyl": "联苯", "camphene": "莰烯", "cellulose": "纤维素", "coronene": "晕苯", "cytosine": "胞嘧啶",
    "dl-alanine": "DL-丙氨酸", "dibenzothiophene": "二苯并噻吩", "dibenzothiophn": "二苯并噻吩",
    "dihydrouracil": "二氢尿嘧啶", "ellagic": "鞣花酸", "flavone": "黄酮", "flouranthene": "荧蒽",
    "fluoranthene": "荧蒽", "fluorene": "芴", "gallic": "没食子酸", "glucose": "葡萄糖", "glycine": "甘氨酸",
    "guanine": "鸟嘌呤", "hypoxanthine": "次黄嘌呤", "indole": "吲哚", "isocytosine": "异胞嘧啶",
    "l-alanine": "L-丙氨酸", "l-cysteine": "L-半胱氨酸", "l-glutamine": "L-谷氨酰胺", "l-histidine": "L-组氨酸",
    "l-lysine": "L-赖氨酸", "l-proline": "L-脯氨酸", "l-tyrosine": "L-酪氨酸", "lignin": "木质素",
    "naringenin": "柚皮素", "pentacene": "并五苯", "perylene": "苝", "phenanthraquin": "菲醌",
    "phenanthraquinone": "菲醌", "phenanthrene": "菲", "phenazine": "吩嗪", "phenol": "苯酚",
    "protein": "蛋白质", "purine": "嘌呤", "pyrazine": "吡嗪", "pyrene": "芘", "pyrimidine": "嘧啶",
    "pyrogallol": "邻苯三酚", "quercetin": "槲皮素", "starch": "淀粉", "tannic": "单宁酸", "taurine": "牛磺酸",
    "tetracene": "并四苯", "theobromine": "可可碱", "thymine": "胸腺嘧啶", "triphenylene": "三亚苯",
    "uracil": "尿嘧啶", "violanthrone79": "紫蒽酮", "xanthine": "黄嘌呤", "cis-stilbene": "顺式二苯乙烯",
    "o-terphenyl": "邻三联苯", "trans-beta-methylstyrene": "反式-β-甲基苯乙烯", "2-3-benzanthracene": "2,3-苯并蒽",
    "anthracene": "蒽", "basalt": "玄武岩", "dotriacontane": "三十二烷", "ethylbenzene": "乙苯",
    "hexatriacontane": "三十六烷", "nitrobenzene": "硝基苯", "nonadecane": "十九烷", "phenylacetylen": "苯乙炔",
    "pyridazine": "哒嗪", "pyridine": "吡啶", "styrene": "苯乙烯", "tetracosane": "二十四烷",
}

#: 括号里的部位与状态
PARTS: Dict[str, str] = {
    "grey": "灰色", "gray": "灰色", "brown": "褐色", "yellow": "黄色", "black": "黑色", "red": "红色",
    "tan": "棕黄", "green": "绿色", "dry": "干", "senesced": "枯", "decayed": "腐", "lower": "下部",
    "upper": "上部", "bark": "树皮", "wood": "木质", "leaf": "叶", "leaves": "叶", "stem": "茎", "stems": "茎",
    "flower": "花", "flowers": "花", "cone": "球果", "needles": "针叶", "sap": "树脂", "pericarp": "果皮",
    "seed": "种子", "capsules": "蒴果", "oat": "燕麦", "larch": "落叶松",
}

_PAREN = re.compile(r"^(.*?)\s*\((.+)\)\)?\s*$")
_NUM = re.compile(r"^(.*?)[\s.]?(\d+)$")
_LUNAR = re.compile(r"^\d{5}\.\d+$")
_BHVO = re.compile(r"^bhvo-2f\+([\d.]+)%(.*)$")
_PCT_MIX = re.compile(r"^([a-z]+)\+([\d.]+)wt%amph\.$")
_MONT = re.compile(r"^montmorill\+(.+)$")
_OIL = re.compile(r"^oil(\d+)$")


def _part(text: str) -> str:
    words = [w for w in re.split(r"[\s\[\]]+", text.lower()) if w]
    if words and all(w in PARTS for w in words):
        return "".join(PARTS[w] for w in words)
    return ""


def extra_name(name: str, category: str = "") -> str:
    """补充表里的中文名；查不到返回空串。"""
    raw = (name or "").strip()
    key = raw.lower().rstrip("*").strip()
    key = re.sub(r"\s+[a-z0-9]+\([a-z0-9()]+\).*$", "", key) if " " in key and "(" in key and key not in NAMES else key
    if key in NAMES:
        return NAMES[key]
    if category.startswith("soil/lunar") or _LUNAR.match(key):
        return "月壤样品 %s" % key.split(".")[0]
    m = _BHVO.match(key)
    if m:
        extra = {"dimethylformamide": "二甲基甲酰胺", "propionitrile": "丙腈"}.get(m.group(2).strip(), "有机物")
        return "玄武岩粉 BHVO-2 + %s%% %s" % (m.group(1), extra)
    m = _PCT_MIX.match(key)
    if m and m.group(1) in NAMES:
        return "%s + %s%% 闪石" % (NAMES[m.group(1)], m.group(2))
    m = _MONT.match(key)
    if m:
        org = {"benzene": "苯", "tce": "三氯乙烯", "toluene": "甲苯", "trichlor": "三氯乙烯", "unleaded": "无铅汽油"}
        return "蒙脱石 + %s" % org.get(m.group(1), m.group(1))
    m = _OIL.match(key)
    if m:
        return "原油 %s" % m.group(1)
    if key.startswith("water+"):
        return "水 + 蒙脱石"
    if key.startswith("antigorite+"):
        return "叶蛇纹石 + 干草"
    if key.startswith("alunite") and ("k+" in key or "musc" in key or "pyro" in key):
        return "明矾石混合物" if "+" in key and not re.match(r"alunite[\d.]+k\+", key) else "明矾石（钾钠）"
    if key.startswith("kaol") and "musc" in key:
        return "高岭石 + 白云母"
    if key.startswith("gyp") and "kjar" in key:
        return "石膏 + 黄钾铁矾 + 白云母"
    m = _PAREN.match(raw)
    if m:
        head = extra_name(m.group(1), category)
        part = _part(m.group(2))
        if head:
            return "%s（%s）" % (head, part) if part else head
    m = _NUM.match(key)
    if m and m.group(1) in NAMES:
        return NAMES[m.group(1)]
    m = re.match(r"^(.*?)\s+\d+$", key)
    if m and m.group(1) in NAMES:
        return NAMES[m.group(1)]
    first = key.split(" ", 1)[0]
    if category.startswith("mineral") and first in NAMES:      # 名字后面跟着化学式
        return NAMES[first]
    return ""


__all__ = ["extra_name", "NAMES", "PARTS"]
