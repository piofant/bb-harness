"""Стадия 2: сведение по темам (Opus). Операции ADD / UPDATE / SUPERSEDE / CONFLICT / NOOP.

Модель отдаёт JSON с операциями; frontmatter рендерится детерминированно здесь.
Каждая строка «Сути» и «Шагов» помечается маркером ⟦c12,c15⟧ — ссылкой на утверждения с проверенной
цитатой; маркеры проверяются и вырезаются при записи.
"""
import datetime as dt
import json
import os
import re

PROMPT_VER = "s2-v2"   # v2: UPDATE сохраняет детали (гейт «утверждение не пропало»), конфликты — в карточку
OPS = ("ADD", "UPDATE", "SUPERSEDE", "CONFLICT", "NOOP")
TYPES = ("process", "fact", "decision", "gotcha", "person", "preference", "topic")
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MARK_RX = re.compile(r"\s*⟦([^⟧]*)⟧")
ID_RX = re.compile(r"c?(\d+)")
TTL_DAYS = {"process": 90, "person": 180, "gotcha": 90, "decision": 180, "preference": 180,
            "topic": 90, "fact": 90}

# медицинское, терапия, контакты — вне private/ нельзя (детерминированная страховка поверх sensitive)
SENSITIVE_PATTERNS = [
    ("медицина/психология", re.compile(
        r"антидепрессант|птср|ptsd|диагноз|психотерап|психиатр|"
        r"терапевт|терапи[июяей]\b|психолог(?:а|у|ом|и|ов|ам|ами|ах)?\b|лекарств|"
        r"тревожн\w* расстройств|депресси|\bсдвг\b|\badhd\b|биполяр|паническ\w* атак|суицид",
        re.I)),
    ("телефон", re.compile(r"(?<![\w/-])(?:\+7|8)[\s(-]*9\d\d[\s)-]*\d{3}[\s-]*\d\d[\s-]*\d\d(?!\d)")),
    # почты людей; служебные адреса вида git@host и user@machine — рабочие данные
    ("почта", re.compile(r"[\w.+-]+@(?:gmail\.com|yandex\.(?:ru|com)|ya\.ru|mail\.ru|bk\.ru|inbox\.ru|list\.ru|"
                         r"icloud\.com|me\.com|outlook\.com|hotmail\.com|proton(?:mail)?\.\w+|rambler\.ru|"
                         r"yahoo\.com|googlemail\.com)",
                         re.I)),
]


def sensitive_hit(text):
    """→ категория найденного медицинского/контакта или None (значение не возвращается)."""
    for name, rx in SENSITIVE_PATTERNS:
        if rx.search(text or ""):
            return name
    return None


TYPE_BODIES = """Тело по типу (после frontmatter; frontmatter НЕ пиши — его соберёт программа):
- у всех: первая строка `**Суть.** …` (1–3 строки — текущий ответ), внизу `## История` датированными строками `- ГГГГ-ММ-ДД — …`;
- process: `## Шаги` (нумерованный список) → `## Проверка` → `## Грабли`;
- gotcha: `## Симптом` → `## Причина` → `## Решение`;
- decision: `## Контекст` → `## Решение` → `## Что заменило`;
- fact: `## Подробности`;
- любой тип, если есть устаревшие или спорные версии: раздел `## Варианты и устарело` — строки
  `- Устарело (до ГГГГ-ММ-ДД): <старое> → сейчас <новое> ⟦c..⟧` или `- Вариант: <другая версия> (<источник>) ⟦c..⟧`;
- person: `## Роль и контакты в работе` (только рабочие: логин, роль, зона; никаких телефонов/почт);
- preference: `## Как делать` → `## Почему`;
- topic (хаб): `**Суть.**` + `## Карточки` со ссылками `- [[имя]] — …`; всего ≤40 строк.
Размер карточки 0,5–4 КБ; ЖЁСТКИЙ предел — 6000 байт вместе с frontmatter (кириллица — 2 байта на символ, это ~2500 знаков),
длиннее программа отклонит; таблицы — коротко (логин — имя — роль), без пересказа. Одна мысль на карточку. Ссылки на другие карточки — `[[имя]]`, только на существующие или
добавляемые в этом ответе."""

PROMPT = """Ты — сводчик базы знаний агентов. Область: `{area}`. Сегодня {today}.
Тебе даны текущие карточки области и НОВЫЕ утверждения (каждое с дословной цитатой из сырья, проверенной программой).
Сведи утверждения в карточки операциями:
- ADD — новая карточка;
- UPDATE — карточка из раздела «Текущие карточки (полный текст)» переписывается целиком. ДОПИСЫВАЙ, а не пересказывай:
  все команды, флаги, пути, числа с единицами, коды ошибок, версии, логины, имена сервисов и пометки «устарело»
  старого текста остаются ДОСЛОВНО. Программа проверяет: каждое утверждение, которое уже было отражено в карточке,
  должно находиться в новом тексте (по дословной цитате или ключевому токену — флагу, пути, числу, логину, слову),
  иначе UPDATE отклоняется. Не помещается в 6000 байт — вынеси раздел в новую карточку (ADD в этом же ответе,
  с этими утверждениями и деталями) и поставь на неё ссылку `[[имя]]`.
- SUPERSEDE — новое явное правило заменяет старое (старую карточку или старое правило/событие из утверждений).
  Новая карточка получает `supersedes: [старые имена]`; существующие старые карточки программа пометит superseded.
  Если устаревшее правило есть только в утверждениях (карточки нет) — добавь ещё ADD для старой карточки со
  `status: superseded`, `superseded_by: [новая]`, телом «Суть: раньше было так, заменено …» — чтобы поиск по старой
  формулировке вёл к новой. В `superseded_claims` перечисли id утверждений со старым правилом.
  Правило: «новое явное правило заменяет старое событие/практику» — это SUPERSEDE, а НЕ CONFLICT.
- Противоречие НЕ отбрасывай молча (NOOP для противоречащего утверждения запрещён):
  - одна версия новее или главнее (живая проверка, решение, более поздняя дата) — UPDATE карточки: текущий ответ в «Сути»,
    старая версия строкой в `## Варианты и устарело`: «Устарело (до …): … → сейчас …» с маркером старого утверждения;
  - обе версии могут быть верны одновременно, или неясно, что новее/главнее — CONFLICT (`name` = имя карточки, к которой
    относится; программа допишет в неё строку «Вариант/конфликт» и строку в CONFLICTS.md), а в UPDATE этой карточки
    можно дать обе версии строками «Вариант: …». Не злоупотребляй CONFLICT: обычно хватает UPDATE или SUPERSEDE.
- NOOP — только если деталь утверждения (флаг, путь, число, логин) УЖЕ дословно есть в карточке, или это разовая
  деталь без пользы. Утверждение с конкретной деталью, которой нет ни в одной карточке, — не NOOP: сведи его.
Каждое новое утверждение должно попасть ровно в одну операцию (в `claims` или в NOOP). Утверждение из `claims`
должно быть отражено в теле: его деталь (флаг, путь, число, логин) — дословно; иначе программа его не засчитает.

Жёсткие правила:
1. Каждая непустая строка «**Суть.**» и каждая строка раздела «## Шаги» заканчивается маркером опоры `⟦c12,c15⟧` —
   id утверждений, на которые она опирается (из новых утверждений или из утверждений, уже связанных с карточкой).
   Без опоры строку не пиши. В остальных разделах маркеры желательны. Маркеры программа вырежет.
2. Только факты из утверждений; не выдумывай команды, пути, логины, даты. Не обобщай: «потолок 8 с, breaker
   300 → 1800 с», а не «есть таймауты»; `tool install <slug> --root /tmp/x`, а не «проверить установкой».
3. Медицинское, психологическое, личное, телефоны/почты/адреса — НЕЛЬЗЯ (такие утверждения сюда не попадают; не добавляй их).
4. Рабочие логины коллег (трекер, справочник сотрудников) и их роли — можно: карточки людей и логинов клади в `dir: "{area}/people"`
   с `sensitivity: restricted`. Остальное — `sensitivity: internal`.
5. Имена карточек: латиница kebab-case, уникальны по всей базе (занятые имена ниже). Не плоди почти-дубли: одна тема — одна карточка;
   описания (description) разных карточек не должны совпадать по смыслу и формулировке.
6. `keys` — 3–8 слов, по которым агент будет искать: ключи задач (PROJ-123), имена сервисов, команды, логины, русские слова-крючки.
7. `importance: high` — для жёстких правил и опасных граблей; `core: true` — только для карточек из списка обязательных ниже.
8. `description` — одна строка-крючок: о чём карточка и когда она нужна.
{hints}
{type_bodies}

Формат ответа — ТОЛЬКО один JSON-объект, без markdown-обёрток:
{{"ops": [
  {{"op": "ADD|UPDATE|SUPERSEDE", "name": "kebab-имя", "dir": "{area}" или "{area}/people", "type": "process|fact|decision|gotcha|person|preference|topic",
    "status": "active|superseded|disputed", "description": "…", "keys": ["…"], "applies_to": ["vps"],
    "supersedes": [], "superseded_by": [], "superseded_claims": [], "importance": "high|normal|low", "core": false,
    "sensitivity": "internal|restricted", "topics": ["topic-slug"], "claims": [12, 15],
    "body": "**Суть.** … ⟦c12⟧\\n\\n## Шаги\\n1. … ⟦c15⟧\\n…\\n\\n## История\\n- 2026-10-22 — …"}},
  {{"op": "CONFLICT", "name": "карточка или тема", "conflict": "что противоречит чему", "claims": [3, 9]}},
  {{"op": "NOOP", "claims": [4, 5], "reason": "…"}}
]}}

{names_block}

## Индекс карточек области
{index}

## Текущие карточки (полный текст; только их можно UPDATE)
{cards}

## Новые утверждения ({n})
{claims}
"""

HUB_PROMPT = """Ты — сводчик базы знаний агентов. Собери хаб области `{area}` (карточка type: topic, имя `{name}`). Сегодня {today}.
Хаб — точка входа: «**Суть.**» 1–3 строки (текущий главный ответ по области), затем `## Карточки` — 18–22 ссылки на САМЫЕ
важные карточки (жёсткие правила, процессы, частые грабли) в виде `- [[имя]] — 3–8 слов`, можно 3–5 подзаголовков `### …`
(раздел «### Новые карточки» не пиши — его ведёт программа);
последней строкой раздела — `- полный список — cards/{area}/_index.md`; затем `## История` (1–2 строки).
Всего тело ≤ 31 строки и ≤ 3200 байт: остальное место до 38 строк — новым карточкам, которые программа допишет сама. Каждая строка «Сути» заканчивается маркером `⟦c12⟧` с id утверждений из списка ниже.
Ссылки только на имена из списка.

Ответ — ТОЛЬКО JSON: {{"description": "…", "keys": ["…"], "claims": [..], "body": "**Суть.** … ⟦c1⟧\\n\\n## Карточки\\n- [[…]] — …\\n\\n## История\\n- {today} — …"}}

Карточки области:
{cards}
"""


DEDUP_PROMPT = """Две карточки базы знаний агентов почти дублируют друг друга (имя или описание). Если это ОДНА сущность — слей их
в одну. Если это разные сущности (разные люди, версии, процессы), ответь ровно `{{"distinct": true, "reason": "…"}}`. Сегодня {today}.
При слиянии выбери, какое имя оставить (`keep` — одно из двух{keep_rule}), напиши полный новый текст тела по правилам типа. Каждая строка «**Суть.**»
и «## Шаги» заканчивается маркером `⟦c12⟧` с id утверждений из списков ниже. Только факты из утверждений и карточек.
{type_bodies}

Ответ — ТОЛЬКО JSON: {{"keep": "имя", "type": "…", "description": "…", "keys": ["…"], "applies_to": ["vps"],
"importance": "high|normal|low", "claims": [..], "body": "**Суть.** … ⟦c1⟧\\n…\\n\\n## История\\n- …"}}

{cards}
"""


def build_dedup_prompt(cards, force_keep=None):
    blocks = []
    for c in cards:
        blocks.append(f"### {c['rel']}\n{c['text'].strip()}\nУтверждения:\n" +
                      "\n".join(render_claim(x) for x in c["claims"]))
    keep_rule = f"; оставить нужно `{force_keep}`" if force_keep else ""
    return DEDUP_PROMPT.format(today=today(), type_bodies=TYPE_BODIES, cards="\n\n".join(blocks), keep_rule=keep_rule)


def trim_hub(body, area, max_lines=38, max_bytes=4200):
    """Детерминированно ужать хаб: лишние ссылки раздела «Карточки» заменяются строкой на полный индекс."""
    lines = body.strip("\n").split("\n")
    if len(lines) <= max_lines and len(body.encode("utf-8")) <= max_bytes:
        return body
    try:
        h = next(i for i, ln in enumerate(lines) if ln.strip() == "## История")
    except StopIteration:
        h = len(lines)
    head, hist = lines[:h], lines[h:][:3]
    tail_line = f"- полный список — `cards/{area}/_index.md`"
    head = [ln for ln in head if not is_hub_tail(ln)]
    while head and (len(head) + len(hist) + 2 > max_lines or
                    len("\n".join(head + [tail_line, ""] + hist).encode("utf-8")) > max_bytes):
        # снимаем последнюю ссылку (или опустевший подзаголовок)
        idx = max((i for i, ln in enumerate(head) if ln.lstrip().startswith(("- [[", "### "))), default=None)
        if idx is None:
            break
        head.pop(idx)
    return "\n".join(head + [tail_line, ""] + hist) + "\n"


HUB_CARDS_H = "## Карточки"
HUB_NEW_H = "### Новые карточки"
HUB_LINK_RX = re.compile(r"^\s*- \[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
HUB_TAIL_RX = re.compile(r"^\s*[-*]\s*\**\s*полный список", re.I)


def is_hub_tail(line):
    """Служебная строка хаба «- полный список — cards/<область>/_index.md» в любом написании модели
    («Полный список», «**полный список**:»), а не подпись карточки, где встретился `_index.md`."""
    return bool(HUB_TAIL_RX.match(line))


def hub_links(body):
    """Имена карточек, перечисленных в разделе «## Карточки» хаба (строки `- [[имя]] — …`), по порядку."""
    out, inside = [], False
    for ln in (body or "").split("\n"):
        st = ln.strip()
        if st.startswith("## "):
            inside = st == HUB_CARDS_H
            continue
        m = HUB_LINK_RX.match(ln) if inside else None
        if m and m.group(1).strip() not in out:
            out.append(m.group(1).strip())
    return out


def hub_label(desc, limit=70):
    """Короткая подпись строки хаба из description: до первого разделителя (если остаётся ≥ 15 знаков),
    не длиннее limit знаков, по границе слова."""
    s = re.sub(r"\s+", " ", str(desc or "")).strip()
    for sep in (" — ", ": ", "; ", " ("):
        i = s.find(sep)
        if i >= 15:
            s = s[:i]
            break
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",.;:—- ") + "…"
    return s.rstrip(".")


def _hub_line(name, desc, limit=70):
    lab = hub_label(desc, limit)
    return f"- [[{name}]] — {lab}" if lab else f"- [[{name}]]"


def _link_name(line):
    m = HUB_LINK_RX.match(line)
    return m.group(1).strip() if m else None


def update_hub_body(body, area, add=(), remove=(), relabel=None, note="", must=()):
    """Правка списка хаба кодом, без LLM: «Суть» и группировка остаются прежними.
    Снимаются строки remove (и опустевшие подзаголовки); подписи relabel {имя: description} переписываются (не длиннее
    прежней подписи, минимум 24 знака); add [(имя, description)] идут в подраздел «### Новые карточки» в конце
    «## Карточки» (перед строкой полного списка). Подраздел — N последних новых с вытеснением: что не помещается
    в пределы trim_hub, вытесняется, начиная со старых, и остаётся только в `_index.md`; must — имена (core и
    важные), которые вытеснять нельзя. В «Историю» — строка note (последние 2 записи;
    `{shown}` / `{only}` в ней — сколько новых показано и сколько осталось только в `_index.md`).
    → (тело, показанные новые, вытесненные) или None — нужна пересборка LLM: раздела «## Карточки» нет, или не
    помещаются прежние (не снятые) карточки вне подраздела новых и must."""
    lines = (body or "").strip("\n").split("\n")
    try:
        k = next(i for i, ln in enumerate(lines) if ln.strip() == HUB_CARDS_H)
    except StopIteration:
        return None
    end = next((i for i in range(k + 1, len(lines)) if lines[i].strip().startswith("## ")), len(lines))
    remove, relabel, must = set(remove), dict(relabel or {}), set(must)
    sec = []
    for ln in lines[k + 1:end]:
        n = _link_name(ln)
        if n in remove:
            continue
        if n in relabel:
            old = ln.split("]] — ", 1)[1].strip() if "]] — " in ln else ""
            ln = _hub_line(n, relabel[n], limit=max(len(old), 24))
        sec.append(ln)
    while sec and not sec[-1].strip():
        sec.pop()
    # подраздел новых: от заголовка до следующего подзаголовка или служебной строки
    fresh, base = [], []
    inside = False
    for ln in sec:
        st = ln.strip()
        if st == HUB_NEW_H:
            inside = True
            continue
        if inside and (st.startswith("### ") or is_hub_tail(ln)):
            inside = False
        (fresh if inside and _link_name(ln) else base if not inside else []).append(ln)
    have = {_link_name(ln) for ln in base + fresh} - {None}
    cand = fresh + [_hub_line(n, d) for n, d in add if n not in have]
    tail = next((i for i, ln in enumerate(base) if is_hub_tail(ln)), len(base))
    rest = lines[end:]
    if note:
        try:
            h = next(i for i, ln in enumerate(rest) if ln.strip() == "## История")
            j = next((i for i in range(h + 1, len(rest)) if rest[i].strip().startswith("## ")), len(rest))
            hist = [ln for ln in rest[h + 1:j] if ln.strip() and ln.strip() != note.strip()]
            rest = rest[:h + 1] + (hist + [note])[-2:] + ([""] + rest[j:] if j < len(rest) else [])
        except StopIteration:
            rest = rest + ["", "## История", note]

    def build(chosen):
        s2 = base[:tail] + ([HUB_NEW_H] + chosen if chosen else []) + base[tail:]
        out = []   # опустевший подзаголовок: за ним другой подзаголовок, строка полного списка или конец раздела
        for i, ln in enumerate(s2):
            if ln.strip().startswith("### "):
                nxt = next((x for x in s2[i + 1:] if x.strip()), "")
                if not nxt.strip() or nxt.strip().startswith("### ") or is_hub_tail(nxt):
                    continue
            out.append(ln)
        t = "\n".join(lines[:k + 1] + out + [""] + rest).rstrip("\n") + "\n"
        n_shown = len({_link_name(ln) for ln in chosen} & {n for n, _ in add})
        t = t.replace("{shown}", str(n_shown)).replace("{only}", str(len(add) - n_shown))
        return re.sub(r"\n{3,}", "\n\n", t)

    for n_keep in range(len(cand), -1, -1):
        keep = set(cand[len(cand) - n_keep:]) if n_keep else set()
        chosen = [ln for ln in cand if ln in keep or _link_name(ln) in must]
        text = build(chosen)
        if trim_hub(text, area) == text:
            shown = {_link_name(ln) for ln in chosen}
            new_names = [n for n, _ in add]
            return (text, [n for n in new_names if n in shown],
                    [_link_name(ln) for ln in cand if _link_name(ln) not in shown])
    return None


def unlink_missing(text, names):
    """[[имя]] на несуществующую карточку → `имя` (битые ссылки ломают lint)."""
    return re.sub(r"\[\[([^\]|#]+)((?:[#|][^\]]*)?)\]\]",
                  lambda m: m.group(0) if m.group(1).strip() in names else f"`{m.group(1).strip()}`", text)


def today():
    return dt.date.today().isoformat()


def render_claim_short(c):
    """Связанное с карточкой утверждение — коротко: id и текст (цитата уже проверена программой)."""
    t = (c.get("text") or "").replace("\n", " ")
    if len(t) > 220:
        t = t[:220] + "…"
    return f"c{c['id']} [{c['kind']}|{c.get('date') or ''}] {t}"


def render_claim(c):
    q = c["quote"].replace("\n", " ")
    if len(q) > 240:
        q = q[:240] + "…"
    src = c["uid"] or c["src"]
    return (f"c{c['id']} [{c['kind']}|{c['topic']}|{c['date']}] {c['text']}\n"
            f"    цитата: «{q}» ({src})")


def chunk_claims(claims, max_n=110, max_bytes=45_000):
    out, cur, b = [], [], 0
    for c in claims:
        s = len(render_claim(c).encode())
        if cur and (len(cur) >= max_n or b + s > max_bytes):
            out.append(cur)
            cur, b = [], 0
        cur.append(c)
        b += s
    if cur:
        out.append(cur)
    return out


NAMES_RULE = ("Занятые имена карточек: полного списка нет — программа сама проверяет уникальность по всей базе; ADD с "
              "именем, занятым любой карточкой (в т.ч. не показанной), отклоняется. Перед ADD проверь индекс ниже: если там "
              "есть карточка о том же — не заводи новую.")


def trimmed_index(cards, chunk, hub, n=60):
    """KB_S2_CTX=trimmed_index: n строк индекса области, похожих на утверждения куска (BM25 по имени и описанию,
    отдельный запрос на утверждение — текст + тема, top-20, слияние RRF 1/(60+rank)), + хаб; порядок — как в индексе."""
    import search as S
    docs = []
    for c in cards:
        if c.name == hub:
            continue
        f = {"name": S.terms(c.name), "keys": [], "desc": S.terms(c.description), "body": []}
        docs.append({"name": c.name, "tf": {k: {t: v.count(t) for t in set(v)} for k, v in f.items()},
                     "len": {k: len(v) for k, v in f.items()}})
    score = {}
    for cl in chunk:
        res = S.bm25(docs, S.query_terms((cl.get("text") or "") + " " + (cl.get("topic") or "").replace("-", " ")))
        res.sort(key=lambda x: (-x[0], x[1]["name"]))
        for rank, (_, d) in enumerate(res[:20]):
            score[d["name"]] = score.get(d["name"], 0.0) + 1.0 / (61 + rank)
    top = set(sorted(score, key=lambda k: (-score[k], k))[:n])
    shown = [c for c in cards if c.name == hub or c.name in top]
    if not shown:
        return []
    lines = [f"(показаны только {len(top)} карточек области, похожих на новые утверждения, и хаб; полный список — "
             f"`cards/{shown[0].area}/_index.md`, его здесь нет)"]
    return lines + [f"- [[{c.name}]] ({c.type}, {c.status}) `{c.rel}` — {c.description}" for c in shown]


def taken_reason(card):
    """Причина отказа «имя занято» для подсказки повтора: какая карточка заняла имя и о чём она."""
    return (f"имя уже занято карточкой `{card.rel}` ({card.type}, {card.status}) — {card.description}. "
            f"Если утверждение о том же — не заводи новую карточку под другим именем: дополни её (UPDATE, если она "
            f"показана целиком) или NOOP со ссылкой; новое имя — только для другой сущности")


def build_prompt(area, index_lines, full_cards, claims, names, hints="", rejections=()):
    """names=None — режим trimmed_index: вместо списка всех занятых имён правило NAMES_RULE."""
    cards_txt = "\n\n".join(
        f"### {c['rel']}\n{c['text'].strip()}\n" +
        ("Связанные утверждения (уже в карточке; при UPDATE их детали должны остаться):\n" +
         "\n".join(render_claim_short(x) for x in c["claims"]) if c["claims"] else "")
        for c in full_cards) or "(нет)"
    if rejections:
        hints = (hints + "\n" if hints else "") + "Прошлая попытка программа ОТКЛОНИЛА (исправь, иначе снова отказ):\n" + \
            "\n".join(f"- {n}: {w}" for n, w in rejections)
    return PROMPT.format(area=area, today=today(), hints=("\nОбязательные карточки и подсказки:\n" + hints) if hints else "",
                         type_bodies=TYPE_BODIES,
                         names_block=NAMES_RULE if names is None else
                         "Занятые имена карточек во всей базе: " + (", ".join(sorted(names)) or "(нет)"),
                         index="\n".join(index_lines) or "(пусто)", cards=cards_txt, n=len(claims),
                         claims="\n".join(render_claim(c) for c in claims))


def build_hub_prompt(area, name, cards_info):
    blocks = []
    for c in cards_info:
        blocks.append(f"- [[{c['name']}]] ({c['type']}) — {c['description']}\n  Суть: {c['sut']}\n  "
                      f"утверждения: " + "; ".join(f"c{x['id']}: {x['text'][:160]}" for x in c["claims"][:4]))
    return HUB_PROMPT.format(area=area, name=name, today=today(), cards="\n".join(blocks) or "(нет)")


def parse_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("нет JSON-объекта")
    return json.loads(t[i:j + 1])


def _ids(v):
    out = []
    for x in (v if isinstance(v, list) else [v] if v not in (None, "") else []):
        m = ID_RX.search(str(x))
        if m:
            out.append(int(m.group(1)))
    return out


# ---------- проверка тела ----------

def strip_markers(body):
    """→ (тело без маркеров, [(строка, [id], секция)])."""
    lines_out, meta = [], []
    section = ""
    in_sut = False
    for line in body.replace("\r\n", "\n").split("\n"):
        ids = []
        for m in MARK_RX.finditer(line):
            ids += _ids([s for s in re.split(r"[,\s]+", m.group(1)) if s])
        clean = MARK_RX.sub("", line).rstrip()
        st = clean.strip()
        if st.startswith("## ") or st.startswith("### "):
            section = st.lstrip("#").strip().lower()
            in_sut = False
        elif st.startswith("**Суть.**") or st.startswith("**Суть**"):
            in_sut = True
            section = "суть"
        elif not st and in_sut:
            in_sut = False
        sec = "суть" if in_sut else section
        meta.append((st, ids, sec))
        lines_out.append(clean)
    return "\n".join(lines_out).strip("\n") + "\n", meta


def unsupported_lines(meta, valid_ids):
    """Строки «Сути» и «Шагов» без опоры на валидное утверждение."""
    bad = []
    for st, ids, sec in meta:
        if not st or sec not in ("суть", "шаги"):
            continue
        if st.startswith("#"):
            continue
        if not any(i in valid_ids for i in ids):
            bad.append(st[:120])
    return bad


# ---------- рендер карточки ----------

def _yaml_list(xs):
    out = []
    for x in xs:
        s = str(x).replace("\n", " ").strip()
        if not s:
            continue
        if re.search(r"[,\[\]#:\"']", s) or s != s.strip():
            s = '"' + s.replace('"', "'") + '"'
        out.append(s)
    return "[" + ", ".join(out) + "]"


def _yaml_str(s):
    s = str(s).replace("\n", " ").strip()
    if re.search(r"(^[\[\]{}>|*&!%@`\"'])|( #)|(: )", s):
        s = '"' + s.replace('"', "'") + '"'
    return s


def render_card(fm, body):
    order = ["name", "description", "type", "status", "supersedes", "superseded_by", "applies_to", "keys",
             "sources", "verified", "verified_how", "review_after", "sensitivity", "importance", "core"]
    lines = ["---"]
    for k in order:
        if k not in fm:
            continue
        v = fm[k]
        if isinstance(v, list):
            lines.append(f"{k}: {_yaml_list(v)}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            lines.append(f"{k}: {_yaml_str(v)}")
    for k, v in fm.items():                      # прочие ключи не теряем
        if k in order:
            continue
        if isinstance(v, list):
            lines.append(f"{k}: {_yaml_list(v)}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            lines.append(f"{k}: {_yaml_str(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip("\n") + "\n"


def review_after(type_, area, verified):
    days = TTL_DAYS.get(type_, 90)
    if area == "infra" and type_ == "fact":
        days = 30
    d = dt.date.fromisoformat(verified) + dt.timedelta(days=days)
    return d.isoformat()


def sources_for(claims):
    seen = {}
    for c in claims:
        key = c["uid"] or c["src"]
        d = c.get("unit_date") or c.get("date") or ""
        if key not in seen or d > seen[key]:
            seen[key] = d
    return [f"{k}@{v}" if v else k for k, v in sorted(seen.items())]


def ensure_history(body, op, run_id):
    t = today()
    line = f"- {t} — {op.lower()}: сводчик (прогон {run_id})"
    if "## История" not in body:
        return body.rstrip("\n") + f"\n\n## История\n{line}\n"
    if f"- {t}" in body.split("## История", 1)[1]:
        return body
    return body.rstrip("\n") + "\n" + line + "\n"
