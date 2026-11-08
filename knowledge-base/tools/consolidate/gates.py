"""Механические гейты перед слиянием в main."""
import difflib
from collections import Counter
import os
import re
import sys

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import cards as kbcards  # noqa: E402
import gate as secret_gate  # noqa: E402
from kbcore import iter_cards, norm  # noqa: E402

from .stage2 import sensitive_hit  # noqa: E402

DUP_RATIO = 0.88


def _n(s):
    return re.sub(r"[\W_]+", " ", norm(s)).strip()


def _close(x, y):
    """→ коэффициент SequenceMatcher, если ≥ DUP_RATIO, иначе None. Дешёвые верхние границы — до полного ratio():
    попарный проход по ~1300 карточкам без них занимал десятки минут."""
    if not x or not y:
        return None
    if x == y:
        return 1.0
    lx, ly = len(x), len(y)
    if 2 * min(lx, ly) / (lx + ly) < DUP_RATIO:      # верхняя граница ratio по длинам
        return None
    sm = difflib.SequenceMatcher(None, x, y)
    if sm.real_quick_ratio() < DUP_RATIO or sm.quick_ratio() < DUP_RATIO:
        return None
    r = sm.ratio()
    return r if r >= DUP_RATIO else None


def near_duplicates(cs, focus=None):
    """Пары активных карточек с почти одинаковыми именами или описаниями.
    focus — множество имён: только пары, где хотя бы одна карточка из него (изменённые в ветке)."""
    act = [c for c in cs if c.status != "superseded"]
    it = [(c, _n(c.name), _n(c.description), c.type == "topic") for c in act]
    out = []
    for i in range(len(it)):
        a, an, ad, at = it[i]
        for j in range(i + 1, len(it)):
            b, bn, bd, bt = it[j]
            if focus is not None and a.name not in focus and b.name not in focus:
                continue
            r = _close(an, bn)
            if r is not None:
                out.append(f"{a.name} ~ {b.name} (name, {r:.2f})")
            if not (at and bt):                       # хабы разных областей похожи по форме описания
                r = _close(ad, bd)
                if r is not None:
                    out.append(f"{a.name} ~ {b.name} (description, {r:.2f})")
    return out


def changed_vs_main(cs, main_cfg):
    """Имена карточек ветки, которых нет в main или чей текст отличается. None — main не передан."""
    if main_cfg is None:
        return None
    main = {c.name: c.text for c in iter_cards(main_cfg)}     # private main не в счёт: одноимённая публичная — новая
    return {c.name for c in cs if main.get(c.name) != c.text}


def sensitive_outside_private(cs):
    out = []
    for c in cs:
        if c.private:
            continue
        if str(c.get("sensitivity")) == "private":
            out.append(f"{c.rel}: sensitivity private вне private/")
        hit = sensitive_hit(c.text)
        if hit:
            out.append(f"{c.rel}: похоже на медицинское/контакт ({hit})")
    return out


def secrets(cs):
    out = []
    for c in cs:
        for name, line in secret_gate.scan_text(c.text):
            out.append(f"{c.rel}:{line}: {name}")
    return out


def dup_key(pair):
    """«a ~ b (name, 0.92)» → frozenset({a, b}): пара без поля и коэффициента."""
    return frozenset(pair.split(" (")[0].split(" ~ "))


def run_gates(cfg_wt, stats, new_conflicts, max_conflicts=10, private_names=(), known_dups=(), main_cfg=None):
    """→ dict(ok, details...). stats: счётчики прогона (claims_used, claims_used_quote_ok).
    Гейт защищает main от того, что принесла ветка; то, что уже лежит в main, ветку не валит (иначе одна старая
    проблема блокирует каждый прогон навсегда), а идёт в *_known:
    - known_dups — пары, которые не валят: были в main или dedup признал их разными сущностями;
    - main_cfg — почти-дубли ищем только среди пар с карточкой, изменённой в ветке (остальные пары лежат в main
      как есть); пара из двух карточек main, которые и там почти-дубли, — известная; sensitive у карточки с тем же
      текстом, что в main, и ошибки lint, которые есть и в main, — известные.
    Секреты валят всегда."""
    cs = iter_cards(cfg_wt, include_private=True)
    errors, warnings = kbcards.lint(cfg_wt)
    # cards/private/ нет в worktree (.gitignore): ссылки на private-карточки main — не битые
    errors = [e for e in errors if not any(e.endswith(f"битая ссылка [[{n}]]") for n in private_names)]
    changed = changed_vs_main(cs, main_cfg)
    lint_known = []
    if main_cfg is not None and errors:
        in_main = Counter(kbcards.lint(main_cfg)[0])      # мультимножество: вторая такая же ошибка — новая
        rest = []
        for e in errors:
            if in_main[e] > 0:
                in_main[e] -= 1
                lint_known.append(e)
            else:
                rest.append(e)
        errors = rest
    sens, sens_known = [], []
    for c in cs:
        for s in sensitive_outside_private([c]):
            (sens_known if changed is not None and c.name not in changed else sens).append(s)
    res = {
        "lint_errors": errors,
        "lint_errors_known": lint_known,
        "lint_warnings": warnings,
        "near_duplicates": [],
        "near_duplicates_known": [],
        "sensitive": sens,
        "sensitive_known": sens_known,
        "secrets": secrets(cs),
        "claims_used": stats.get("claims_used", 0),
        "claims_used_quote_ok": stats.get("claims_used_quote_ok", 0),
        "new_conflicts": new_conflicts,
    }
    known = {dup_key(p) for p in known_dups}
    main = {c.name: c for c in iter_cards(main_cfg) if not c.private} if main_cfg is not None else {}
    for p in near_duplicates([c for c in cs if not c.private], focus=changed):
        k = dup_key(p)
        if k not in known and main and all(n in main for n in k) and near_duplicates([main[n] for n in k]):
            known.add(k)
        res["near_duplicates_known" if k in known else "near_duplicates"].append(p)
    quote_ok = res["claims_used"] == res["claims_used_quote_ok"]
    res["quote_100"] = quote_ok
    res["ok"] = (not errors and not res["near_duplicates"] and not res["sensitive"] and not res["secrets"]
                 and quote_ok and new_conflicts < max_conflicts)
    return res
