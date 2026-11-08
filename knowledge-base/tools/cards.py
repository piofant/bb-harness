"""Карточки: lint, index, find, show."""
import difflib
import os
import re
import sys

import search

from kbcore import (AREAS, LIMIT_CARD_BYTES, LIMIT_CORE_TOKENS, LIMIT_HUB_LINES,
                    LIMIT_INDEX_LINES, REQUIRED, SENSITIVITY, STATUSES, TYPES, VERIFIED_HOW,
                    as_list, est_tokens, iter_cards, norm)

DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9-]*$")
LINK_RX = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


# ---------- lint ----------

def lint(cfg):
    """→ (errors, warnings): списки строк `путь: проблема`."""
    errors, warnings = [], []
    cards = iter_cards(cfg, include_private=True)
    by_name = {}
    for c in cards:
        by_name.setdefault(c.name, []).append(c)
    names = set(by_name)

    for c in cards:
        e = lambda msg: errors.append(f"{c.rel}: {msg}")  # noqa: E731
        if not c.text.startswith("---"):
            e("нет frontmatter")
            continue
        for k in REQUIRED:
            v = c.fm.get(k)
            if v is None or v == "" or (k in ("keys", "sources") and not as_list(v)):
                e(f"нет обязательного поля `{k}`")
        if c.fm.get("name") and c.fm["name"] != c.stem:
            e(f"name `{c.fm['name']}` не совпадает с именем файла `{c.stem}`")
        if not NAME_RX.match(c.name):
            e(f"name `{c.name}` — только латиница, цифры и дефис")
        if c.type and c.type not in TYPES:
            e(f"type `{c.type}` не из {TYPES}")
        if c.status and c.status not in STATUSES:
            e(f"status `{c.status}` не из {STATUSES}")
        vh = str(c.fm.get("verified_how") or "")
        if vh and vh not in VERIFIED_HOW:
            e(f"verified_how `{vh}` не из {VERIFIED_HOW}")
        sens = str(c.fm.get("sensitivity") or "")
        if sens and sens not in SENSITIVITY:
            e(f"sensitivity `{sens}` не из {SENSITIVITY}")
        if sens == "private" and not c.private:
            e("sensitivity: private — карточка должна лежать в cards/private/")
        for k in ("verified", "review_after"):
            v = str(c.fm.get(k) or "")
            if v and not DATE_RX.match(v):
                e(f"{k} `{v}` — нужен формат ГГГГ-ММ-ДД")
        if not c.area:
            e("карточка вне папки области")
        elif c.dir_parts[0] not in AREAS:
            e(f"область `{c.dir_parts[0]}` не из {AREAS}")
        if len(c.dir_parts) > 2:
            e("глубже 2 уровней папок")
        if c.status == "superseded" and not as_list(c.fm.get("superseded_by")):
            e("status: superseded без superseded_by")
        for ref in as_list(c.fm.get("superseded_by")) + as_list(c.fm.get("supersedes")):
            if ref not in names:
                warnings.append(f"{c.rel}: ссылка supersede на несуществующую `{ref}`")
        if c.size > LIMIT_CARD_BYTES:
            e(f"размер {c.size} Б > {LIMIT_CARD_BYTES} Б — разделить")
        if c.type == "topic":
            n = len(c.body.strip("\n").splitlines())
            if n > LIMIT_HUB_LINES:
                e(f"хаб {n} строк > {LIMIT_HUB_LINES}")
        for target in LINK_RX.findall(c.body):
            t = target.strip()
            if t not in names:
                e(f"битая ссылка [[{t}]]")

    for name, cs in by_name.items():
        if len(cs) > 1:
            errors.append(f"имя `{name}` не уникально: " + ", ".join(c.rel for c in cs))

    idx = os.path.join(cfg.root, "INDEX.md")
    if os.path.isfile(idx):
        n = len(open(idx, encoding="utf-8").read().splitlines())
        if n > LIMIT_INDEX_LINES:
            errors.append(f"INDEX.md: {n} строк > {LIMIT_INDEX_LINES}")
    core = os.path.join(cfg.root, "INDEX-core.md")
    if os.path.isfile(core):
        t = est_tokens(open(core, encoding="utf-8").read())
        if t > LIMIT_CORE_TOKENS:
            errors.append(f"INDEX-core.md: ~{t} токенов > {LIMIT_CORE_TOKENS}")
    return errors, warnings


# ---------- index ----------

def _clean(s):
    return re.sub(r"[\t\r\n]+", " ", str(s)).strip()


def _line(c):
    d = _clean(c.description)
    v = f" (проверено {c.verified})" if c.verified else ""
    return f"- [[{c.name}]] `{c.rel}` — {d}{v}"


def _area_of_dir(rel_dir):
    parts = rel_dir.split("/")
    return parts[0] if parts[0] != "projects" or len(parts) < 2 else "/".join(parts[:2])


def _hot(cards):
    """Горячие: core: true, затем preference/gotcha с importance: high. Свежие сверху."""
    act = [c for c in cards if c.status == "active"]
    core = sorted([c for c in act if c.core], key=lambda c: c.verified, reverse=True)
    # K6: опорность важнее свежести — карточка с большим числом источников центральнее
    rest = sorted([c for c in act if not c.core and c.type in ("preference", "gotcha", "process")
                   and c.importance == "high"],
                  key=lambda c: (len(c.get("sources") or []) if isinstance(c.get("sources"), list) else 0, c.verified), reverse=True)
    return core + rest


def build_index(cfg):
    """Генерирует INDEX.md, INDEX-core.md, INDEX.tsv, cards/<area>/_index.md. → список изменённых."""
    all_cards = iter_cards(cfg, include_private=True)
    public = [c for c in all_cards if not c.private]
    areas = {}
    for c in all_cards:
        areas.setdefault(c.area, []).append(c)
    files = {}

    # области: все существующие папки areas + projects/<x>
    area_dirs = []
    for a in AREAS:
        p = os.path.join(cfg.cards, a)
        if os.path.isdir(p):
            area_dirs.append(a)
            if a == "projects":
                for sub in sorted(os.listdir(p)):
                    if os.path.isdir(os.path.join(p, sub)) and not sub.startswith((".", "_")):
                        area_dirs.append(f"projects/{sub}")

    def count(a):
        return len([c for c in all_cards if c.area == a or c.area.startswith(a + "/")])

    # INDEX.md
    hubs = [c for c in public if c.type == "topic" and c.status == "active"]
    hot = _hot(public)
    head = [
        "# База знаний агентов — INDEX",
        "",
        "Генерируется `kb index` — руками не править. Поиск: `kb find <запрос>` (карточки),"
        " `kb find --raw <запрос>` (+ сырьё), `kb show <name>`.",
        "Приоритет: `decisions/` (решения владельца) > карточки `status: active` > сырьё `raw/`."
        " Конфликты — `CONFLICTS.md`, источники — `SOURCES.md`, здоровье — `HEALTH.md`.",
        "",
        "## Разделы",
    ]
    for a in area_dirs:
        if a == "private":
            continue
        head.append(f"- {a} — {count(a)} карт. → `cards/{a}/_index.md`")
    budget = LIMIT_INDEX_LINES - len(head) - 6
    hub_lines = [_line(c) for c in sorted(hubs, key=lambda c: c.name)]
    rule_lines = [_line(c) for c in hot if c.type != "topic"]
    hub_take = hub_lines[:max(0, min(len(hub_lines), budget // 2 if rule_lines else budget))]
    rule_take = rule_lines[:max(0, budget - len(hub_take))]
    body = ["", "## Хабы тем"] + (hub_take or ["- пока нет"])
    if len(hub_lines) > len(hub_take):
        body.append(f"- … ещё {len(hub_lines) - len(hub_take)} — см. `INDEX.tsv`")
    body += ["", "## Жёсткие правила и грабли"] + (rule_take or ["- пока нет"])
    if len(rule_lines) > len(rule_take):
        body.append(f"- … ещё {len(rule_lines) - len(rule_take)} — см. `INDEX.tsv`")
    files["INDEX.md"] = "\n".join((head + body)[:LIMIT_INDEX_LINES]) + "\n"

    # INDEX-core.md
    core = [
        "# База знаний агентов — ядро",
        f"Полная база: `{cfg.root}` (`INDEX.md`, `kb find <запрос>`, `kb show <name>`)."
        " Решения владельца (`decisions/`) главнее карточек.",
        "",
        "Разделы: " + ", ".join(f"{a} ({count(a)})" for a in area_dirs
                                if a != "private" and "/" not in a),
        "",
        "## Горячие карточки",
    ]
    added = 0
    for c in hot:
        line = f"- {c.name}: {_clean(c.description)} — `{c.rel}`"
        if est_tokens("\n".join(core + [line])) > LIMIT_CORE_TOKENS - 20:
            break
        core.append(line)
        added += 1
    if not added:
        core.append("- пока нет")
    files["INDEX-core.md"] = "\n".join(core) + "\n"

    # INDEX.tsv (без private)
    cols = ["name", "area", "type", "status", "path", "description", "keys", "verified",
            "review_after", "sensitivity"]
    rows = ["\t".join(cols)]
    for c in public:
        rows.append("\t".join(_clean(x) for x in [
            c.name, c.area, c.type, c.status, c.rel, c.description, ", ".join(c.keys),
            c.verified, c.get("review_after"), c.get("sensitivity")]))
    files["INDEX.tsv"] = "\n".join(rows) + "\n"

    # cards/<area>/_index.md
    for a in area_dirs:
        cs = [c for c in all_cards if c.area == a]
        if a.startswith("projects/") and not cs:
            continue  # проект без карточек — индекс не нужен (старый удалится ниже)
        lines = [f"# {a} — индекс", "", "Генерируется `kb index`.", ""]
        act = [c for c in cs if c.status != "superseded"]
        old = [c for c in cs if c.status == "superseded"]
        for t in ["topic"] + [t for t in sorted({c.type for c in act}) if t != "topic"]:
            group = sorted([c for c in act if c.type == t], key=lambda c: c.name)
            if group:
                lines += [f"## {t}"] + [_line(c) + (" **[disputed]**" if c.status == "disputed" else "")
                                        for c in group] + [""]
        if old:
            lines += ["## Устаревшие (superseded)"]
            lines += [f"- {c.name} → {', '.join(as_list(c.get('superseded_by')))}" for c in old]
            lines.append("")
        if not cs:
            lines += ["Карточек пока нет.", ""]
        files[os.path.join("cards", a, "_index.md")] = "\n".join(lines).rstrip("\n") + "\n"

    changed = []
    # устаревшие _index.md: областей, где не осталось карточек, и старых раскладок (#14)
    for dirpath, dirs, fnames in os.walk(cfg.cards):
        if os.path.relpath(dirpath, cfg.cards).split(os.sep)[0] == "_templates":
            continue
        if "_index.md" in fnames:
            rel = os.path.relpath(os.path.join(dirpath, "_index.md"), cfg.root)
            if rel not in files:
                os.remove(os.path.join(cfg.root, rel))
                changed.append(rel)
    for rel, content in files.items():
        p = os.path.join(cfg.root, rel)
        old = None
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                old = f.read()
        if old != content:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".kbtmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, p)
            changed.append(rel)
    build_hint_index(cfg)
    return changed


def _hint_module():
    """tools/hooks/kb_hint.py — единственный источник логики подсказки (хук самодостаточен)."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "hooks", "kb_hint.py")
    spec = importlib.util.spec_from_file_location("kb_hint_for_index", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_hint_index(cfg):
    """state/hint-index.json для хука kb-hint (вне git). Сбой не ломает `kb index`: хук сам
    заметит отсутствие/устаревание индекса и посчитает подсказку из INDEX.tsv. → путь или None."""
    try:
        return _hint_module().write_hint_index(cfg.root)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"предупреждение: hint-index не собран: {type(e).__name__}: {e}\n")
        return None


# ---------- find ----------

def _tokens(q):
    return [t for t in re.findall(r"[\w./-]+", norm(q)) if len(t) >= 2] or [norm(q).strip()]


def iter_raw(cfg):
    for dirpath, dirs, files in os.walk(cfg.raw):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fn in sorted(files):
            if fn.endswith(".md"):
                yield os.path.join(dirpath, fn)


def _tag(type_, status, superseded_by, verified):
    tag = f"{type_ or '?'}"
    if status and status != "active":
        tag += f", {status}"
        if superseded_by:
            tag += " → " + ", ".join(superseded_by)
    if verified:
        tag += f", проверено {verified}"
    return tag


def format_hit(d, brief=False):
    """Запись поиска → строка (brief: как раньше) или строка + «Суть» / «устарело → …»."""
    line = (f"{d['rel']} — {_clean(d['description'])} "
            f"[{_tag(d['type'], d['status'], d['superseded_by'], d['verified'])}]")
    if brief:
        return line
    if d["status"] == "superseded":
        return line + "\n  устарело → " + (", ".join(d["superseded_by"]) or "?")
    return line + (f"\n  Суть: {d['sut']}" if d["sut"] else "")


def _find_notes(cfg, toks, limit):
    """Заметки `kb note` (raw/inbox/**): по блоку, а не по файлу — короткая заметка не тонет среди больших
    MEMORY.md, пока сводчик не сделал из неё карточку. Порог — половина слов запроса."""
    from consolidate.units import split_notes
    base = os.path.join(cfg.raw, "inbox")
    hits = {}
    if not os.path.isdir(base) or not toks:
        return []
    for p in iter_raw(cfg):
        if not os.path.relpath(p, cfg.raw).startswith("inbox" + os.sep):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                blocks = split_notes(f.read())
        except OSError:
            continue
        for nid, block in blocks:
            low = norm(block)
            covered = sum(1 for t in toks if t in low)
            if covered * 2 >= len(toks) and nid not in hits:   # одна заметка с двух машин — одна строка
                body = " ".join(ln for ln in block.splitlines()[1:] if not ln.startswith("источник:"))
                hits[nid] = (-covered, os.path.basename(p),
                             f"[заметка] {os.path.relpath(p, cfg.root)}#n-{nid} — {_clean(body)[:200]}")
    hs = sorted(hits.values(), key=lambda x: x[1], reverse=True)   # свежие (имя файла — дата) выше
    hs.sort(key=lambda x: x[0])                                     # стабильно: больше совпавших слов — выше
    return [h[2] for h in hs[:limit]]


def find(cfg, query, raw=False, limit=10, private=False, brief=False):
    """→ (карточки, сырьё). Карточки — BM25 (search.py), по записи на карточку:
    живые сверху, устаревшие ниже с пометкой «устарело → …»."""
    out = [format_hit(d, brief) for d in search.search(cfg, query, limit=limit, private=private)]
    toks = _tokens(query)
    phrase = norm(query).strip()
    raw_out = []
    if raw:
        raw_out = _find_notes(cfg, toks, limit)
        rres = []
        for p in iter_raw(cfg):
            if os.path.relpath(p, cfg.raw).startswith("inbox" + os.sep):
                continue  # заметки — поблочно выше
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            low = norm(text)
            covered = sum(1 for t in toks if t in low)
            if not covered:
                continue
            occ = sum(min(low.count(t), 20) for t in toks)
            s = covered * 10 + occ + (8 if phrase in low and len(toks) > 1 else 0)
            rres.append((s, p, text))
        rres.sort(key=lambda x: (-x[0], x[1]))
        for s, p, text in rres[:limit]:
            snippet = ""
            for line in text.splitlines():
                if any(t in norm(line) for t in toks):
                    snippet = _clean(line)[:160]
                    break
            raw_out.append(f"[сырьё] {os.path.relpath(p, cfg.root)} — {snippet}")
    return out, raw_out


# ---------- show ----------

def _lookup(cards, name):
    target = name[:-3] if name.endswith(".md") else name
    for c in cards:
        if c.name == target or c.rel == name or c.rel == f"cards/{name}" or c.stem == target:
            return c
    return None


def _suggest(cards, name):
    target = name[:-3] if name.endswith(".md") else name
    sugg = difflib.get_close_matches(target, [c.name for c in cards], n=5, cutoff=0.5)
    sugg += [c.name for c in cards if target in c.name and c.name not in sugg][:5]
    return sugg


def show(cfg, name):
    """→ (текст | None, подсказки)."""
    cards = iter_cards(cfg, include_private=True)
    c = _lookup(cards, name)
    return (c.text, []) if c else (None, _suggest(cards, name))


def card_links(c):
    """[[ссылки]] из тела и superseded_by — уникальные, в порядке появления."""
    seen, out = set(), []
    for t in [x.strip() for x in LINK_RX.findall(c.body)] + as_list(c.get("superseded_by")):
        if t and t not in seen and t != c.name:
            seen.add(t)
            out.append(t)
    return out


def show_links(cfg, name):
    """→ (текст карточки + «Суть» каждой карточки из её [[ссылок]] | None, подсказки)."""
    cards = iter_cards(cfg, include_private=True)
    me = _lookup(cards, name)
    if me is None:
        return None, _suggest(cards, name)
    by_name = {c.name: c for c in cards}
    links = card_links(me)
    out = [me.text.rstrip("\n"), "", f"--- ссылки карточки: {len(links)} ---"]
    for t in links:
        c = by_name.get(t)
        if c is None:
            out.append(f"[[{t}]] — карточка не найдена")
            continue
        out.append(f"[[{t}]] {c.rel} — {_clean(c.description)} "
                   f"[{_tag(c.type, c.status, as_list(c.get('superseded_by')), c.verified)}]")
        if c.status == "superseded":
            out.append("  устарело → " + (", ".join(as_list(c.get("superseded_by"))) or "?"))
        else:
            s = search.sut(c.body)
            if s:
                out.append(f"  Суть: {s}")
    return "\n".join(out) + "\n", []
