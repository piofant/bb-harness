"""Дешёвый предфильтр единиц по областям: ключевые слова и пути, без LLM.

Счёт = сумма весов найденных шаблонов (каждый шаблон — один раз). Шаблоны и веса — в конфиге областей
(kbcore.AREA_INFO): шаблонам, без которых не собрать обязательные карточки, дают большой вес.
"""
import os
import re
import sys

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from kbcore import AREA_INFO  # noqa: E402

# правила областей — из конфига областей (kbcore.AREA_INFO, файл areas.json)
AREA_RULES = {a: [(rx, int(w)) for rx, w in (v.get("rules") or [])] for a, v in AREA_INFO.items()}

_COMPILED = {a: [(re.compile(rx, re.I | re.U), w) for rx, w in rules] for a, rules in AREA_RULES.items()}

SENSITIVE_HINT = re.compile(r"психотерап|антидепрессант|диагноз|психиатр|"
                            r"психолог|терапевт|терапи[июяей]\b", re.I)


def area_scores(text):
    out = {}
    for area, rules in _COMPILED.items():
        s = 0
        for rx, w in rules:
            if rx.search(text):
                s += w
        out[area] = s
    return out


def select_units(units, areas, min_score=4):
    """Единицы, относящиеся к областям: счёт ≥ min_score хотя бы в одной. Сортировка — по убыванию счёта."""
    scored = []
    for u in units:
        sc = area_scores(u.text)
        best = max((sc.get(a, 0) for a in areas), default=0)
        total = sum(sc.get(a, 0) for a in areas)
        if best >= min_score:
            # медицинское и терапевтическое без рабочих признаков — в конец очереди
            penalty = 10 if SENSITIVE_HINT.search(u.text) and best < 8 else 0
            scored.append((total - penalty, u))
    scored.sort(key=lambda x: (-x[0], x[1].uid))
    return [u for _, u in scored]
