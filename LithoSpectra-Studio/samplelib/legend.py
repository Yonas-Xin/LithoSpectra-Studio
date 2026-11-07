# -*- coding: utf-8 -*-
"""岩性图例库：把岩性名称认成标准岩性类，取出它的颜色与花纹号。

为什么需要它
------------
岩性矢量的属性值是人写出来的自然语言——"灰色中细粒黑云母二长花岗岩"、
"下二叠统栖霞组灰岩"、"石英砂岩夹粉砂岩"。按唯一值赋色时给它们随机分配调色板
颜色，图面上花岗岩可能是蓝的、灰岩可能是红的，跟任何一份地质图的读图习惯都对
不上。这里把名称先归到标准岩性类，再取该类的标准配色。

匹配规则
--------
按可信度从高到低，命中即止（``LegendMatch.rule`` 记下走的是哪一条）：

1. **精确**——整串就等于某个岩性名或别名。
2. **剥修饰词后精确**——把颜色、粒度、结构、成分前缀、蚀变这些限定词剥掉再比。
   "灰色中细粒黑云母二长花岗岩" 剥成 "二长花岗岩"。
3. **最长后缀**——中文岩石名是"偏正结构，中心词在后"："斑状二长花岗岩"的中心词
   是"二长花岗岩"，"含砾粗砂岩"的中心词是"砂岩"。取能匹配上的**最长**后缀，
   保证"二长花岗岩"不会被更短的"花岗岩"抢走。这一条覆盖面最广。
4. **最长子串**——中心词不在末尾时兜底（"灰岩夹页岩"、"砂岩透镜体"）。取最长的
   命中词；长度相同时按岩性名本身的长度排，避免被"砂"这种单字词抢走。

匹配不上就返回 ``None``，由调用方退回自动配色——**宁可不赋色，也不要猜错岩性**：
一个错的标准色比一个随机色更容易误导人。

数据来自 ``data/lithology_legend.json``，由 ``tools/build_legend_library.py``
生成，源头是 IUGS-CGI SimpleLithology 词表与 Macrostrat 的配色（都是 CC-BY）。
GB/T 958-2015 不公开，需要严格按国标出图时在生成脚本里覆盖配色。
"""

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from . import config

DATA_PATH = config.resource_path(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "lithology_legend.json"),
    "legend", "lithology_legend.json")

# 名称里常见的、与岩性无关的前缀：地层代号、编号、时代
_LEADING_CODE = re.compile(r"^[A-Za-z]{1,3}[0-9]{0,2}[a-z]{0,3}[\s\-_·]+")
_BRACKETS = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")
_NOISE = re.compile(r"[\s　,，;；:：/、。.\-_+]+")


@dataclass
class LithologyClass:
    """图例库里的一个标准岩性类。"""

    id: str
    name_en: str
    name_zh: str
    aliases_zh: List[str] = field(default_factory=list)
    broader: Optional[str] = None
    rock_class: str = "other"
    rock_type: str = ""
    color: str = "#BFBFBF"
    fgdc_pattern: Optional[int] = None
    definition: str = ""

    @property
    def display(self) -> str:
        return self.name_zh or self.name_en

    def names(self) -> List[str]:
        out = [n for n in ([self.name_zh] + list(self.aliases_zh)) if n]
        if self.name_en:
            out.append(self.name_en)
        return out


@dataclass
class LegendMatch:
    """一次匹配的结果。``rule`` 说明是怎么认出来的，便于人工复核。"""

    value: str                     # 原始属性值
    cls: LithologyClass
    matched: str                   # 命中的那个岩性名
    rule: str                      # exact / stripped / suffix / substring
    confidence: float

    @property
    def color(self) -> str:
        return self.cls.color


_RULE_CONFIDENCE = {"exact": 1.0, "stripped": 0.9, "suffix": 0.8, "substring": 0.6}
_RULE_CN = {"exact": "完全一致", "stripped": "剥掉修饰词后一致",
            "suffix": "以该岩性名结尾", "substring": "名称中含该岩性名"}


def rule_label(rule: str) -> str:
    return _RULE_CN.get(rule, rule)


class LithologyLegend:
    """岩性图例库。``load()` 之后调 :meth:`match` 即可。"""

    def __init__(self, doc: dict) -> None:
        self.version = doc.get("version", "")
        self.generated = doc.get("generated", "")
        self.sources = doc.get("sources", [])
        self.notes = doc.get("notes", [])
        self.chronostrat = doc.get("chronostrat", [])
        self.classes: Dict[str, LithologyClass] = {}
        for row in doc.get("classes", []):
            self.classes[row["id"]] = LithologyClass(
                id=row["id"], name_en=row.get("name_en", ""),
                name_zh=row.get("name_zh", ""),
                aliases_zh=list(row.get("aliases_zh") or ()),
                broader=row.get("broader"),
                rock_class=row.get("rock_class", "other"),
                rock_type=row.get("rock_type", ""),
                color=row.get("color") or "#BFBFBF",
                fgdc_pattern=row.get("fgdc_pattern"),
                definition=row.get("definition", ""))

        # 名称 -> 岩性类。同名冲突时保留先登记的（主名优先于别名）
        self._by_name: Dict[str, LithologyClass] = {}
        for cls in self.classes.values():
            for name in cls.names():
                self._by_name.setdefault(self._key(name), cls)
        # 后缀/子串匹配按名字长度从长到短试，"二长花岗岩"必须排在"花岗岩"前面
        self._ordered: List[Tuple[str, LithologyClass]] = sorted(
            self._by_name.items(), key=lambda kv: -len(kv[0]))

        mods = doc.get("modifiers") or {}
        words = [w for group in mods.values() for w in group]
        # 长的排前面，等长的按字典序——``set`` 的遍历顺序每个进程都不一样，
        # 不把等长的也定死，同一份数据在两次运行里会剥出不同的结果。
        self._modifiers = sorted(set(words), key=lambda w: (-len(w), w))

    # ------------------------------------------------------------------ 载入
    @classmethod
    def load(cls, path: str = "") -> "LithologyLegend":
        path = path or DATA_PATH
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    @classmethod
    def shared(cls) -> Optional["LithologyLegend"]:
        """进程内共享的一份。数据文件缺失时返回 None，调用方自行退回自动配色。"""
        if not hasattr(cls, "_shared"):
            try:
                cls._shared = cls.load()
            except (OSError, ValueError):
                cls._shared = None
        return cls._shared

    # ------------------------------------------------------------------ 归一
    @staticmethod
    def _key(text: str) -> str:
        """归一化成比较用的键：全角转半角、去括号内容、去空白与标点、英文小写。"""
        s = unicodedata.normalize("NFKC", str(text or "")).strip()
        s = _BRACKETS.sub("", s)
        s = _LEADING_CODE.sub("", s)
        s = _NOISE.sub("", s)
        return s.lower()

    def strip_modifiers(self, key: str) -> str:
        """反复剥掉修饰词，直到剥不动为止。

        每剥掉一个就重头再来，**不能接着往下扫**：剥完"中细粒"，剩下的
        "黑云母二长花岗岩" 必须重新从最长的修饰词试起。接着扫下去的话，"黑云母"
        在列表里的位置已经走过了，轮到更短的"黑云"时就把它咬掉一半，剩一个孤零零
        的"母"字，后面怎么查都查不到。
        """
        changed = True
        while changed and key:
            changed = False
            for word in self._modifiers:                 # 已按长度降序
                w = word.lower()
                if len(key) <= len(w):
                    continue
                if key.startswith(w):
                    key = key[len(w):]
                elif key.endswith(w):
                    key = key[:-len(w)]
                else:
                    continue
                changed = True
                break
        return key

    # ------------------------------------------------------------------ 匹配
    def match(self, value: str) -> Optional[LegendMatch]:
        key = self._key(value)
        if not key:
            return None

        cls = self._by_name.get(key)
        if cls is not None:
            return self._make(value, cls, key, "exact")

        bare = self.strip_modifiers(key)
        if bare and bare != key:
            cls = self._by_name.get(bare)
            if cls is not None:
                return self._make(value, cls, bare, "stripped")

        for name, cls in self._ordered:           # 已按长度降序
            if len(name) >= 2 and key.endswith(name):
                return self._make(value, cls, name, "suffix")

        for name, cls in self._ordered:
            if len(name) >= 2 and name in key:
                return self._make(value, cls, name, "substring")
        return None

    @staticmethod
    def _make(value, cls, matched, rule) -> LegendMatch:
        return LegendMatch(value=value, cls=cls, matched=matched, rule=rule,
                           confidence=_RULE_CONFIDENCE[rule])

    def match_all(self, values: Iterable[str]) -> Dict[str, Optional[LegendMatch]]:
        return {v: self.match(v) for v in values}

    def color_map(self, values: Iterable[str]) -> Tuple[Dict[str, str], List[str]]:
        """``({取值: 颜色}, [没认出来的取值])``，可直接喂给唯一值赋色。

        撞色的自动错开明度。撞色有两种来源：几个取值归到同一个岩性类（"石英砂岩"
        和"长石砂岩"都是砂岩），以及两个不同的类本来就共用一个色值（花岗岩与二长
        花岗岩在配色表里是同一个红）。两种都得错开——图例上两行一模一样的颜色，
        等于没分类；错开之后仍在同一个色系里，一眼还能看出它们是一族。
        """
        colors: Dict[str, str] = {}
        unknown: List[str] = []
        used: Dict[str, int] = {}
        for value in values:
            hit = self.match(value)
            if hit is None:
                unknown.append(value)
                continue
            n = used.get(hit.color, 0)
            used[hit.color] = n + 1
            picked = hit.color if n == 0 else _shade(hit.color, n)
            # 错开之后仍可能撞上别的类，一步步往下让
            guard = n
            while picked in colors.values() and guard < 12:
                guard += 1
                picked = _shade(hit.color, guard)
            colors[value] = picked
        return colors, unknown

    # ------------------------------------------------------------------ 查询
    def get(self, class_id: str) -> Optional[LithologyClass]:
        return self.classes.get(class_id)

    def path_of(self, class_id: str) -> List[LithologyClass]:
        """从顶层到该类的层级链，用于界面上说明"它被归到哪一类"。"""
        chain, seen = [], set()
        cur = class_id
        while cur and cur in self.classes and cur not in seen:
            seen.add(cur)
            chain.append(self.classes[cur])
            cur = self.classes[cur].broader
        return list(reversed(chain))


def _shade(hexcolor: str, step: int) -> str:
    """同色系微调明度。step 越大越亮/越暗，交替偏移，避免越调越白。"""
    hexcolor = (hexcolor or "#BFBFBF").lstrip("#")
    if len(hexcolor) != 6:
        return "#" + hexcolor
    rgb = [int(hexcolor[i:i + 2], 16) for i in (0, 2, 4)]
    delta = 26 * ((step + 1) // 2) * (1 if step % 2 else -1)
    out = [max(0, min(255, c + delta)) for c in rgb]
    return "#%02x%02x%02x" % tuple(out)
