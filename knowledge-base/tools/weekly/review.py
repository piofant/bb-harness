"""К3: недельный обзор по templates/weekly-review.md.

Генератор = промпт `review_prompt.md` (его крутит петля эвала) + проверка результата здесь:
- структура шаблона (заголовок, разделы 1–4, подраздел на каждый проект);
- каждая строка-пункт со ссылкой, и ссылка — из правды недели (выдуманная ссылка → строка выбрасывается);
- ≤ 40 строк (лишнее срезается с конца самых длинных подразделов).
Модель упала или вернула мусор → детерминированный обзор из той же правды с честной строкой о сбое.
"""
import json
import os
import re
import sys

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from consolidate import llm  # noqa: E402

from . import truth as T  # noqa: E402

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_prompt.md")
MODEL = os.environ.get("KB_WEEKLY_MODEL") or os.environ.get("KB_S2_MODEL") or "opus"
TIMEOUT = 900
MAX_LINES = 40
SECTIONS = ["## 1. Что продвинулось", "## 2. Ждёт решения владельца",
            "## 3. Ждёт подтверждения: предложенные правки карточек проектов", "## 4. Здоровье базы"]
OUTSIDE = "### Вне карточек проектов"
LINK_RX = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]|\[([^\]]+)\]\(([^)\s]+)\)|коммит ([0-9a-f]{7,40})")
ITEM_RX = re.compile(r"^\s*(?:-|\d+\.)\s+")
NO_LINK_OK = re.compile(r"^\s*(?:-|\d+\.)\s+(без изменений|предложений нет|нет)\.?\s*$", re.I)
FIXED_TARGETS = {"HEALTH.md", "CONFLICTS.md", "decisions.md", "state/runs.jsonl", "skills.md", "MOC.md",
                 "integrations.md", "automation-routines.md"}


def prompt_path():
    return os.environ.get("KB_WEEKLY_PROMPT") or PROMPT_PATH


def build_prompt(truth, path=None):
    with open(path or prompt_path(), encoding="utf-8") as f:
        tpl = f.read()
    slim = T.for_llm(truth)
    return tpl.replace("{window.title}", truth["window"]["title"]).replace(
        "{truth_json}", json.dumps(slim, ensure_ascii=False, indent=1))


# ---------- проверка ----------

def allowed(truth):
    names, targets, shas = set(), set(FIXED_TARGETS), set()
    for it in truth["items"]:
        if it.get("kind") == "card":
            names.add(it["name"])
        elif it.get("kind") == "commit":
            shas.add(it["sha"])
        for m in LINK_RX.finditer(it.get("ref", "")):
            if m.group(3):
                targets.add(m.group(3))
    for p in truth["projects"].values():
        targets.add(p["file"])
    return names, targets, shas


def link_ok(m, names, targets, shas):
    if m.group(1) is not None:
        return m.group(1).strip() in names
    if m.group(3) is not None:
        t = m.group(3).split("#")[0]
        t = t[3:] if t.startswith("../") else t
        return t in targets
    sha = m.group(4)
    return any(s.startswith(sha) or sha.startswith(s) for s in shas)


def _strip_fence(md):
    md = (md or "").strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", md, re.S)
    return m.group(1).strip() if m else md


def finalize(md, truth):
    """→ (md, отчёт) или (None, отчёт), если структура не спасается."""
    rep = {"dropped_no_link": [], "dropped_bad_link": [], "trimmed": 0, "added": []}
    md = _strip_fence(md)
    lines = md.splitlines()
    head = next((i for i, ln in enumerate(lines) if ln.startswith("# Неделя")), None)
    if head is None:
        rep["error"] = "нет заголовка «# Неделя»"
        return None, rep
    lines = lines[head:]
    lines[0] = f"# Неделя {truth['window']['title']}"
    pos = []
    for s in SECTIONS:
        i = next((k for k, ln in enumerate(lines) if ln.strip().startswith(s[:6]) and ln.startswith("## ")), None)
        if i is None:
            rep["error"] = f"нет раздела «{s}»"
            return None, rep
        lines[i] = s
        pos.append(i)
    if pos != sorted(pos):
        rep["error"] = "разделы не по порядку"
        return None, rep
    names, targets, shas = allowed(truth)
    project_files = [p["file"] for p in truth["projects"].values()]
    out, sec, skip_sub = [lines[0]], None, False
    joined = []
    for ln in lines[1:]:   # перенос пункта на следующую строку с отступом — часть того же пункта
        if joined and ln[:1] in (" ", "\t") and ln.strip() and ITEM_RX.match(joined[-1]) and not ITEM_RX.match(ln):
            joined[-1] = joined[-1].rstrip() + " " + ln.strip()
        else:
            joined.append(ln)
    for ln in joined:
        if ln.startswith("# "):
            rep.setdefault("dropped_sections", []).append(ln.strip()[:80])   # заголовок «# Неделя» — только первый
            continue
        if ln.startswith("## "):
            sec = next((k for k, x in enumerate(SECTIONS) if ln.strip() == x), None)
            if sec is None:
                rep.setdefault("dropped_sections", []).append(ln.strip()[:80])
            else:
                out.append(ln)
            skip_sub = False
            continue
        if sec is None or not ln.strip():
            continue                                   # вне разделов шаблона — ничего
        if ln.startswith("### "):
            ok = sec == 0 and (ln.startswith(OUTSIDE) or any(f in ln for f in project_files))
            if not ok:
                rep.setdefault("dropped_sections", []).append(ln.strip()[:80])
            skip_sub = not ok
            if ok:
                out.append(ln.rstrip())
            continue
        if skip_sub:
            continue
        if not NO_LINK_OK.match(ln):
            ms = list(LINK_RX.finditer(ln))
            if not ms:
                rep["dropped_no_link"].append(ln.strip()[:160])
                continue
            bad = [m.group(0) for m in ms if not link_ok(m, names, targets, shas)]
            if bad:
                rep["dropped_bad_link"].append(f"{ln.strip()[:120]} ← {', '.join(bad)[:80]}")
                continue
        out.append(ln.rstrip())
    blocks = split_blocks(out)
    ensure_projects(blocks, truth, rep)
    ensure_sections(blocks, truth, rep)
    lines = render_blocks(blocks)
    lines = trim(lines, rep)
    if len(lines) > MAX_LINES:
        rep["error"] = f"не ужалось до {MAX_LINES} строк ({len(lines)})"
        return None, rep
    return "\n".join(lines) + "\n", rep


def split_blocks(lines):
    """→ {"title": str, "sections": [[header, [подблоки: [header|None, [строки]]]]]}."""
    title, secs = lines[0], []
    for ln in lines[1:]:
        if ln.startswith("## "):
            secs.append([ln, [[None, []]]])
        elif not secs:
            continue
        elif ln.startswith("### "):
            secs[-1][1].append([ln, []])
        elif ln.strip():
            secs[-1][1][-1][1].append(ln)
    return {"title": title, "sections": secs}


def ensure_projects(blocks, truth, rep):
    sec = blocks["sections"][0]
    subs = sec[1]
    for slug, p in truth["projects"].items():
        if not any(h and p["file"] in h for h, _ in subs):
            subs.append([f"### {p['title']} — [карточка]({p['file']})", []])
            rep["added"].append(f"подраздел {slug}")
    by_id = {it["id"]: it for it in truth["items"]}
    for h, body in subs:
        if h and not h.startswith(OUTSIDE) and not body:
            p = next((p for p in truth["projects"].values() if p["file"] in h), None)
            its = [by_id[i] for i in (p or {}).get("items", []) if by_id[i].get("kind") in ("card", "commit", "decision")
                   and (by_id[i].get("significant") or by_id[i].get("events"))]
            its.sort(key=lambda x: (not x.get("significant"), -x.get("score", 0)))
            if its:   # модель пропустила проект с изменениями — не «без изменений», а пункты из правды
                body += [item_line(x) for x in its[:2]]
                rep["added"].append(f"пункты проекта {p['file']}")
            else:
                body.append("- без изменений")
    # «Вне карточек проектов» — в конец раздела, пустой убрать
    outside = [s for s in subs if s[0] and s[0].startswith(OUTSIDE)]
    rest = [s for s in subs if s not in outside and (s[0] or s[1])]
    sec[1] = rest + [s for s in outside if s[1]]


def ensure_sections(blocks, truth, rep):
    secs = blocks["sections"]
    if not any(b for _, b in secs[1][1]):
        secs[1][1] = [[None, ["- нет"]]]
    if not any(b for _, b in secs[2][1]):
        secs[2][1] = [[None, ["- предложений нет"]]]
    body = [ln for _, b in secs[3][1] for ln in b]
    if not any("HEALTH.md" in ln for ln in body):
        secs[3][1][0][1].insert(0, health_line(truth))
        rep["added"].append("строка здоровья")
    if not any("CONFLICTS.md" in ln for ln in body):
        secs[3][1][0][1].insert(1, conflicts_line(truth))
        rep["added"].append("строка споров")


def render_blocks(blocks):
    out = [blocks["title"]]
    for h, subs in blocks["sections"]:
        out += ["", h]
        for sh, body in subs:
            if sh:
                out.append(sh)
            out += body
    return out


def trim(lines, rep):
    """Срезать до MAX_LINES: последние пункты самых длинных подразделов раздела 1, затем хвост раздела 2."""
    while len(lines) > MAX_LINES:
        blocks = split_blocks(lines)
        subs = blocks["sections"][0][1]
        cand = [b for _, b in subs if len(b) > 1]
        if cand:
            max(cand, key=len).pop()
        else:
            s2 = blocks["sections"][1][1][0][1]
            if len(s2) > 2:
                s2.pop()
            else:
                break
        rep["trimmed"] += 1
        lines = render_blocks(blocks)
    return lines


# ---------- детерминированный обзор (сбой модели) ----------

def ddmm(d):
    return f"{d[8:10]}.{d[5:7]}" if d and len(d) >= 10 else ""


def item_line(it):
    date = (it.get("events") or [{}])[-1].get("date") or it.get("date") or ""
    if it.get("kind") == "card":
        ev = (it.get("events") or [])
        text = ev[-1]["text"] if ev else it.get("description") or it.get("name")
        what = {"added": "новая карточка", "modified": "карточка обновлена", "deleted": "карточка удалена"}.get(
            it.get("change"))
        if not ev and what:
            text = f"{what}: {it.get('description') or it['name']}"
    else:
        text = it.get("text", "")
    text = T._clip(text, 170)
    return f"- {text} ({ddmm(date) + ', ' if date else ''}{it['ref']})"


def health_line(truth):
    h = truth["health"]
    st = h.get("status") or "нет данных"
    probs = "; ".join(p["what"] for p in h.get("problems", [])[:2])
    return f"- Статус: {st}" + (f" — {T._clip(probs, 160)}" if probs else "") + " ([HEALTH.md](HEALTH.md))."


def conflicts_line(truth):
    c = truth["conflicts"]
    return (f"- Открытых споров: {c['open_total']}, новых за неделю {c['new_in_window']}, закрыто "
            f"{c['closed_in_window']} — [CONFLICTS.md](CONFLICTS.md).")


def fallback(truth, reason):
    items = truth["items"]
    by_id = {it["id"]: it for it in items}
    L = [f"# Неделя {truth['window']['title']}", "", SECTIONS[0]]
    for slug, p in truth["projects"].items():
        L.append(f"### {p['title']} — [карточка]({p['file']})")
        its = [by_id[i] for i in p["items"] if by_id[i].get("kind") in ("card", "commit", "decision")]
        its.sort(key=lambda x: (not x.get("significant"), -x.get("score", 0)))
        its = [x for x in its if x.get("significant") or x.get("events")][:3]
        L += [item_line(x) for x in its] or ["- без изменений"]
    other = [it for it in items if it.get("significant") and not it.get("project")
             and it["kind"] in ("card", "commit", "decision")][:5]
    if other:
        L.append(OUTSIDE)
        L += [item_line(x) for x in other]
    L += ["", SECTIONS[1]]
    q = [it for it in items if it["kind"] == "conflict_new"][:3]
    L += [f"{i}. Спор «{it['name']}»: {T._clip(it['text'], 150)} ({it['ref']})" for i, it in enumerate(q, 1)] or ["- нет"]
    L += ["", SECTIONS[2]]
    pr = [it for it in items if it["kind"] in ("proposal", "skills")]
    L += [f"{i}. {T._clip(it['text'], 170)} ({it['ref']})" for i, it in enumerate(pr, 1)] or ["- предложений нет"]
    L += ["", SECTIONS[3], health_line(truth), conflicts_line(truth)]
    for it in items:
        if it["kind"] in ("run_failure", "consolidate") and it.get("significant"):
            L.append(f"- {T._clip(it['text'], 170)} ({it['ref']}).")
    L.append(f"- Обзор собран без модели: {T._clip(reason, 120)} ([журнал прогонов](state/runs.jsonl)).")
    md, rep = finalize("\n".join(L), truth)
    if md is None:  # не должно случаться; жёсткий срез как последняя страховка
        md = "\n".join(L[:MAX_LINES]) + "\n"
    return md


# ---------- генерация ----------

def generate(truth, cwd, run=None, model=None, prompt_file=None, attempts=2):
    """→ (md, meta). run — функция вызова модели (как consolidate.llm.run); тесты подменяют её."""
    run = run or llm.run
    meta = {"model": model or MODEL, "llm_calls": 0, "tokens_in": 0, "tokens_out": 0, "status": "ok",
            "errors": [], "prompt": os.path.basename(prompt_file or prompt_path())}
    prompt = build_prompt(truth, prompt_file)
    for _ in range(attempts):
        try:
            r = run(prompt, model or MODEL, timeout=TIMEOUT, cwd=cwd)
        except (llm.LLMError, llm.LimitHit) as e:
            meta["errors"].append(f"{type(e).__name__}: {str(e)[:200]}")
            if isinstance(e, llm.LimitHit):
                break
            continue
        meta["llm_calls"] += 1
        meta["tokens_in"] += r.get("tokens_in") or 0
        meta["tokens_out"] += r.get("tokens_out") or 0
        md, rep = finalize(r.get("result") or "", truth)
        meta["check"] = rep
        if md:
            return md, meta
        meta["errors"].append(f"проверка: {rep.get('error')}")
    meta["status"] = "fallback"
    return fallback(truth, meta["errors"][-1] if meta["errors"] else "модель недоступна"), meta


def count_lines(md):
    return len(md.rstrip("\n").splitlines())


def items_without_link(md):
    return [ln for ln in md.splitlines() if ITEM_RX.match(ln) and not NO_LINK_OK.match(ln)
            and not LINK_RX.search(ln)]
