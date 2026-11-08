"""К2: предложения правок «Текущего состояния» карточек проектов → proposals/<дата>-<проект>.md.

Файлы projects/*.md крон НЕ трогает (только читает). Нет изменившихся карточек области — нет файла.
Каждое предложение проверяется: основание — одна из изменившихся карточек, цитата дословно есть в ней, «было» —
дословно есть в текущем разделе. Модель упала или вернула мусор → детерминированные предложения из событий недели
(«собрано без модели»).

Один и тот же пункт — один раз: К2 гоняют и `kb daily`, и `kb weekly`, поэтому перед записью предложение сверяется
с файлами проекта за последние DEDUP_DAYS дней (тот же текст «предлагаю» или та же пара основание + цитата —
повтор). Файл за ту же дату уже есть (ежедневный и недельный в субботу) — новые пункты дописываются в его конец,
написанное раньше не переписывается.
"""
import datetime as dt
import json
import os
import re
import sys

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from consolidate import llm  # noqa: E402
from kbcore import parse_frontmatter  # noqa: E402

from . import truth as T  # noqa: E402

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proposal_prompt.md")
MODEL = os.environ.get("KB_WEEKLY_MODEL") or os.environ.get("KB_S2_MODEL") or "opus"
TIMEOUT = 900
MAX_CARDS = 20
MAX_ITEMS = 6
DEDUP_DAYS = 30
PENDING = "ждёт подтверждения"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SUBSECTIONS = ["Действующие решения и почему", "Что подтверждено", "Существенное неизвестное и риски",
               "Следующий шаг"]


def _n(s):
    return re.sub(r"\s+", " ", (s or "").replace("ё", "е").replace("Ё", "Е")).strip().lower().strip("«»\"' .")


def candidates(truth, slug):
    """Изменившиеся за окно карточки проекта, самые значимые сначала."""
    p = truth["projects"][slug]
    by_id = {it["id"]: it for it in truth["items"]}
    cards = [by_id[i] for i in p["items"] if by_id[i].get("kind") == "card"
             and (by_id[i].get("change") or by_id[i].get("events"))]
    cards.sort(key=lambda x: (-x.get("score", 0), x["name"]))
    return cards[:MAX_CARDS]


def card_block(c):
    ev = "\n".join(f"  - {e['date']}: {e['text']}" for e in c.get("events") or [])
    ch = "\n".join(f"  - {e['date']}: {e['text']}" for e in c.get("changes") or [])
    return (f"### {c['name']} ({c.get('type')}, {c.get('change') or 'без правки в базе'})\n"
            f"описание: {c.get('description')}\nсуть: {c.get('essence')}\n"
            + (f"события недели:\n{ev}\n" if ev else "") + (f"что поменялось:\n{ch}\n" if ch else ""))


def card_text(c):
    return " ".join([c.get("description") or "", c.get("essence") or ""] +
                    [e["text"] for e in (c.get("events") or []) + (c.get("changes") or [])])


def build_prompt(truth, slug, current, decisions, cards):
    with open(os.environ.get("KB_PROPOSAL_PROMPT") or PROMPT_PATH, encoding="utf-8") as f:
        tpl = f.read()
    w = truth["window"]
    return tpl.format(title=truth["projects"][slug]["title"], window=f"{w['since']}..{w['until']}",
                      section=current or "(раздела нет)", decisions=decisions or "(нет)",
                      cards="\n".join(card_block(c) for c in cards), max_items=MAX_ITEMS)


def parse(text):
    t = (text or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("нет JSON")
    d = json.loads(m.group(0))
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        raise ValueError("нет items")
    return d["items"]


def was_line_ok(was, current):
    """«Было» — это строка раздела (почти целиком), а не одно слово из него."""
    w = _n(was)
    return len(w) >= 15 and any(w in _n(ln) and len(w) >= 0.6 * len(_n(ln)) for ln in current.splitlines()
                                if _n(ln))


def keys(x):
    """Ключи повтора предложения: текст «предлагаю» и пара основание + цитата (цитата в файле обрезана до 220)."""
    prop, basis, quote = _n(x.get("propose")), str(x.get("basis") or "").strip(), _n(T._clip(x.get("quote"), 220))
    return ({("p", prop)} if prop else set()) | ({("q", basis, quote)} if basis and quote else set())


def validate(items, cards, current, already=None):
    """→ (годные, отброшенные с причиной). already — ключи уже предложенного раньше (см. keys)."""
    by_name = {c["name"]: c for c in cards}
    cur = _n(current)
    already = already or set()
    ok, bad, seen = [], [], set()
    for x in items:
        if not isinstance(x, dict):
            bad.append(("не объект", str(x)[:80]))
            continue
        sub, was, prop = str(x.get("subsection") or ""), str(x.get("was") or ""), str(x.get("propose") or "")
        basis, quote = str(x.get("basis") or "").strip().strip("[]"), str(x.get("quote") or "")
        why = None
        if sub not in SUBSECTIONS:
            why = f"раздел «{sub}»"
        elif basis not in by_name:
            why = f"основание {basis} не из изменившихся карточек"
        elif len(_n(quote)) < 15 or _n(quote) not in _n(card_text(by_name[basis])):
            why = "цитаты нет в карточке"
        elif was.strip() not in ("", "—", "-") and not was_line_ok(was, current):
            why = "«было» нет в текущем разделе целой строкой"
        elif not prop.strip() or len(prop) > 600:
            why = "пустое или слишком длинное предложение"
        elif _n(prop) in cur:
            why = "уже есть в разделе"
        elif _n(prop) in seen:
            why = "повтор"
        elif keys({"propose": prop, "basis": basis, "quote": quote}) & already:
            why = "уже предложено раньше"
        if why:
            bad.append((why, prop[:80]))
            continue
        seen.add(_n(prop))
        ok.append({"subsection": sub, "was": "" if was.strip() in ("—", "-") else was.strip(),
                   "propose": prop.strip(), "basis": basis, "quote": quote.strip()})
    return ok[:MAX_ITEMS], bad


def fallback_items(cards):
    out = []
    for c in cards:
        ev = (c.get("events") or [])
        if not ev:
            continue
        e = ev[-1]
        out.append({"subsection": "Что подтверждено", "was": "",
                    "propose": f"{T._clip(e['text'], 200)} ({e['date']}, [[{c['name']}]])",
                    "basis": c["name"], "quote": e["text"]})
        if len(out) >= 3:
            break
    return out


def render_items(items, start=1):
    L = []
    for i, x in enumerate(items, start):
        L += ["", f"{i}. **Раздел «{x['subsection']}».**",
              f"   - Было: «{x['was']}»" if x["was"] else "   - Было: нет (новая строка)",
              f"   - Предлагаю: {x['propose']}",
              f"   - Основание: [[{x['basis']}]] — «{T._clip(x['quote'], 220)}»"]
    return L


def render(truth, slug, items, fallback=False):
    p = truth["projects"][slug]
    w = truth["window"]
    L = ["---", f"name: proposal-{w['until']}-{slug}", "type: proposal", f"project: {slug}",
         f"created: {w['until']}", f"window: {w['since']}..{w['until']}", "status: ждёт подтверждения", "---",
         f"# Предложения в «Текущее состояние»: {p['title']}", "",
         f"Крон не правит [карточку проекта](../{p['file']}) сам. По каждому пункту ответ владельца: «да» / «нет» / правка."]
    if fallback:
        L.append(FALLBACK_NOTE)
    L += render_items(items)
    return "\n".join(L) + "\n"


def render_append(truth, items, start, fallback=False):
    """Дописка к файлу за ту же дату: заголовок окна и пункты с продолжением нумерации."""
    w = truth["window"]
    L = ["", f"Дополнено: окно {w['since']}..{w['until']}" + (f" — {FALLBACK_NOTE}" if fallback else ".")]
    return "\n".join(L + render_items(items, start)) + "\n"


# ---------- уже написанные файлы ----------

FALLBACK_NOTE = "Модель была недоступна — предложения собраны без неё из событий недели."
ITEM_RX = re.compile(r"^(\d+)\. \*\*Раздел «(.+?)»\.\*\*\s*$")
WAS_RX = re.compile(r"^\s+- Было: (?:«(.*)»|нет \(новая строка\))\s*$")
PROP_RX = re.compile(r"^\s+- Предлагаю: (.*)$")
BASIS_RX = re.compile(r"^\s+- Основание: \[\[([^\]]+)\]\] — «(.*)»\s*$")


def parse_file(text):
    """Файл предложений → (frontmatter, [{subsection, was, propose, basis, quote}]). Ручные правки не мешают:
    пункт, который не разобрался, просто не попадёт в сверку повторов."""
    fm, _ = parse_frontmatter(text)
    out, cur = [], None
    for ln in text.splitlines():
        m = ITEM_RX.match(ln)
        if m:
            cur = {"subsection": m.group(2), "was": "", "propose": "", "basis": "", "quote": ""}
            out.append(cur)
        elif cur is not None:
            if (m := WAS_RX.match(ln)):
                cur["was"] = m.group(1) or ""
            elif (m := PROP_RX.match(ln)):
                cur["propose"] = m.group(1).strip()
            elif (m := BASIS_RX.match(ln)):
                cur["basis"], cur["quote"] = m.group(1).strip(), m.group(2)
    return fm or {}, out


def project_files(dirs, slug=None):
    """[(rel 'proposals/<имя>', путь, frontmatter, пункты)] по каталогам dirs; одно имя — из первого каталога."""
    out, names = [], set()
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})-([a-z0-9][a-z0-9-]*)\.md$", fn)
            if not m or fn in names:
                continue
            path = os.path.join(d, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            fm, items = parse_file(text)
            if str(fm.get("type") or "") != "proposal" or (slug and str(fm.get("project") or "") != slug):
                continue
            names.add(fn)
            fm.setdefault("created", m.group(1))
            fm["_fallback"] = FALLBACK_NOTE in text
            out.append((f"proposals/{fn}", path, fm, items))
    return out


def already_keys(dirs, slug, until_date, days=DEDUP_DAYS):
    """Ключи всего, что уже предложено по проекту за days дней до until_date (любой статус: «нет» владельца — тоже
    ответ, повторять не надо)."""
    lo = (dt.date.fromisoformat(until_date) - dt.timedelta(days=days)).isoformat()
    out = set()
    for rel, path, fm, items in project_files(dirs, slug):
        if str(fm.get("created")) >= lo:
            for x in items:
                out |= keys(x)
    return out


def pending(dirs, since_date, until_date):
    """Ждущие ответа предложения, созданные в окне дат, по проектам → [{project, files, file, n, fallback,
    preview}] (для обзора: все предложения недели, в том числе ежедневные)."""
    by = {}
    for rel, path, fm, items in project_files(dirs):
        created = str(fm.get("created"))
        if not (since_date <= created <= until_date) or not str(fm.get("status") or "").startswith(PENDING) \
                or not items:
            continue
        p = by.setdefault(str(fm.get("project") or ""), {"files": [], "items": [], "fallback": False})
        p["files"].append(rel)
        p["items"] += items
        p["fallback"] |= fm["_fallback"]
    out = []
    for slug, p in sorted(by.items()):
        files = sorted(p["files"])
        out.append({"project": slug, "files": files, "file": files[-1], "n": len(p["items"]),
                    "fallback": p["fallback"], "preview": [preview(x) for x in reversed(p["items"])]})
    return out


def preview(x):
    return (f"раздел «{x['subsection']}»: " + (f"«{T._clip(x['was'], 60)}» → " if x["was"] else "новая строка → ")
            + T._clip(x["propose"], 110))


def same_day_file(out_dir, date, slug):
    """Файл за дату: ждущий ответа — дописываем в него; на него уже ответили — следующий <дата>-<проект>-N.md."""
    for i in range(1, 50):
        name = f"{date}-{slug}.md" if i == 1 else f"{date}-{slug}-{i}.md"
        path = os.path.join(out_dir, name)
        if not os.path.exists(path):
            return name, path
        try:
            with open(path, encoding="utf-8") as f:
                fm = parse_file(f.read())[0]
        except OSError:
            continue
        if str(fm.get("project") or slug) != slug:     # чужой проект с именем «<slug>-N» — не наш файл
            continue
        if str(fm.get("status") or "").startswith(PENDING):
            return name, path
    raise RuntimeError(f"слишком много файлов предложений за {date} по {slug}")


def make(cfg, truth, out_dir, run=None, model=None, log=None, dedup_dirs=None):
    """Для каждого проекта с изменениями — файл предложений. → [{project, file, n, fallback, preview, ...}].
    dedup_dirs — где искать уже предложенное (по умолчанию out_dir и proposals/ базы)."""
    run = run or llm.run
    dedup_dirs = dedup_dirs or [out_dir, os.path.join(cfg.root, "proposals")]
    res = []
    try:
        with open(os.path.join(cfg.root, "decisions.md"), encoding="utf-8") as f:
            decisions = T._clip(f.read(), 6000)
    except OSError:
        decisions = ""
    for slug, p in truth["projects"].items():
        if not SLUG.match(slug):           # slug идёт в имя файла: только [a-z0-9-]
            res.append({"project": slug, "cards": 0, "n": 0, "fallback": False, "llm_calls": 0, "tokens_in": 0,
                        "tokens_out": 0, "rejected": [], "error": "недопустимое имя проекта", "status": "error"})
            continue
        try:
            res.append(_one(cfg, truth, slug, p, decisions, out_dir, run, model, dedup_dirs))
        except Exception as e:  # noqa: BLE001 — сбой одного проекта не роняет остальные
            res.append({"project": slug, "cards": 0, "n": 0, "fallback": False, "llm_calls": 0, "tokens_in": 0,
                        "tokens_out": 0, "rejected": [], "error": f"{type(e).__name__}: {str(e)[:200]}",
                        "status": "error"})
    return res


def _one(cfg, truth, slug, p, decisions, out_dir, run, model, dedup_dirs):
    """Предложения по одному проекту. → info."""
    cards = candidates(truth, slug)
    info = {"project": slug, "cards": len(cards), "n": 0, "fallback": False, "llm_calls": 0,
            "tokens_in": 0, "tokens_out": 0, "rejected": [], "error": None, "duplicates": 0}
    if not cards:
        info["status"] = "no_changes"
        return info
    with open(os.path.join(cfg.root, p["file"]), encoding="utf-8") as f:   # только чтение
        current = T.section(f.read(), "3. Текущее состояние")
    already = already_keys(dedup_dirs, slug, truth["window"]["until"])
    items = None
    try:
        r = run(build_prompt(truth, slug, current, decisions, cards), model or MODEL, timeout=TIMEOUT,
                cwd=cfg.state)
        info["llm_calls"] = 1
        info["tokens_in"], info["tokens_out"] = r.get("tokens_in") or 0, r.get("tokens_out") or 0
        items, rej = validate(parse(r.get("result")), cards, current, already)
        info["rejected"] = [f"{a}: {b}" for a, b in rej][:10]
        info["duplicates"] = sum(1 for a, _ in rej if a == "уже предложено раньше")
    except (llm.LLMError, llm.LimitHit, ValueError) as e:
        info["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    if items is None:
        items = []
        for x in fallback_items(cards):
            if keys(x) & already:
                info["duplicates"] += 1
            else:
                items.append(x)
        info["fallback"] = True
    info["n"] = len(items)
    if items:
        os.makedirs(out_dir, exist_ok=True)
        name, path = same_day_file(out_dir, truth["window"]["until"], slug)
        if os.path.exists(path):          # тот же день (ежедневный и недельный): дописать, не переписывать
            with open(path, encoding="utf-8") as f:
                start = len(parse_file(f.read())[1]) + 1
            with open(path, "a", encoding="utf-8") as f:
                f.write(render_append(truth, items, start, info["fallback"]))
            info["appended"] = True
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(render(truth, slug, items, info["fallback"]))
        info["file"] = f"proposals/{name}"
        info["preview"] = [preview(x) for x in items]
        info["status"] = "ok" if not info["fallback"] else "fallback"
    else:
        info["status"] = "nothing_to_propose" if not info["duplicates"] else "duplicates_only"
    return info
