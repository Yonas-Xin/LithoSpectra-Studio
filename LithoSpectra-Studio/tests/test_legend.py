# -*- coding: utf-8 -*-
"""岩性图例库的自测。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from samplelib.legend import LithologyLegend

def check(c, w):
    if not c: raise AssertionError(w)
    print("  ✓ %s" % w)

print("[1] 图例库")
lg = LithologyLegend.load()
check(len(lg.classes) > 200, "载入 %d 个岩性类" % len(lg.classes))
check(len(lg.chronostrat) > 90, "载入 %d 个年代区间" % len(lg.chronostrat))
check(all(c.color.startswith("#") and len(c.color) == 7 for c in lg.classes.values()),
      "每个类都有合法颜色")

cases = {
    "花岗岩": ("granite", "exact"),
    "灰色中细粒黑云母二长花岗岩": ("monzogranite", "stripped"),
    "含砾粗砂岩": ("clastic_sandstone", "suffix"),
    "灰岩": ("limestone", "exact"),
    "深灰色泥晶灰岩": ("carbonate_mudstone", "stripped"),
    "斜长角闪岩": ("amphibolite", "exact"),
    "灰岩夹页岩": ("shale", "suffix"),
    "J2b 砂岩": ("clastic_sandstone", "exact"),
    "GRANITE": ("granite", "exact"),
}
for text, (cid, rule) in cases.items():
    m = lg.match(text)
    check(m is not None and m.cls.id == cid and m.rule == rule,
          "「%s」-> %s（%s）" % (text, m.cls.id if m else None, m.rule if m else "-"))
for text in ("水体", "居民地", "未知XYZ", "", "   "):
    check(lg.match(text) is None, "「%s」认不出来，不硬套" % text)

colors, unknown = lg.color_map(["石英砂岩", "长石砂岩", "花岗岩", "二长花岗岩", "灰岩", "水体"])
check(unknown == ["水体"], "color_map 把认不出的单列出来")
check(len(set(colors.values())) == len(colors) == 5,
      "5 个认出的取值各有各的颜色（同类与撞色都错开）：%s" % list(colors.values()))
check(lg.path_of("monzogranite")[-1].id == "monzogranite"
      and len(lg.path_of("monzogranite")) > 2,
      "层级链：%s" % " / ".join(c.id for c in lg.path_of("monzogranite")))

print("\n岩性图例库测试通过")
