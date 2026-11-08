"""Стадия 1: извлечение утверждений с дословной цитатой (Sonnet, батчи, параллельно)."""
import json
import re

from kbcore import AREA_ALT, WORK_AREAS, area_description

from .atoms import target_claims
from .units import normalize

# s1-v2: норма пропорционально объёму, операционные атомы — отдельными утверждениями, без обобщения.
# Смена версии инвалидирует кэш только для заново извлекаемых единиц (см. Ledger.add_extraction).
PROMPT_VER = "s1-v2"
KINDS = ["rule", "fact", "gotcha", "howto", "event", "person", "preference"]
AREAS = WORK_AREAS + ["private"]
AREA_RX = re.compile(r"^(" + AREA_ALT + r"|private|projects/[a-z0-9][a-z0-9-]*)$")

PROMPT = """Ты извлекаешь знания для базы знаний агентов (Claude/Codex) владельца базы.
Ниже {n} единиц сырья (память агентов, итоги сессий, дайджесты). Извлеки из них УТВЕРЖДЕНИЯ —
устойчивые, переиспользуемые знания, на которые другой агент сможет опереться через месяц.

Что брать:
- правила и запреты (rule), факты об устройстве систем (fact), грабли «симптом → причина → решение» (gotcha),
  рецепты и шаги (howto), люди и их рабочие роли/логины (person), предпочтения владельца в работе (preference);
- event — только если событие меняет правило или фиксирует дату решения (например «22.10 решили: сначала PR, потом коммент»).

НОРМА ОБЪЁМА. В заголовке каждой единицы стоит `норма≥N` — минимум утверждений для неё (≈1 на 15–25 содержательных
строк, минимум 2). Потолка нет: длинная единица с множеством деталей даёт столько утверждений, сколько в ней
отдельных знаний. Разбирай единицу целиком, до конца, включая разделы «How to apply», «Грабли», «UPDATE», «История».

ОПЕРАЦИОННЫЕ ДЕТАЛИ — НЕ ТЕРЯТЬ. Каждая из этих деталей должна попасть ДОСЛОВНО (символ в символ) и в `text`,
и в `quote` хотя бы одного утверждения kind fact/gotcha/howto:
- команда и её флаги CLI (`tool install <slug> --root /tmp/x`, `--changelog`, `--accept-dns=false`);
- путь или имя файла (`calls.jsonl`, `~/bin/netdiag`, `server-moved.json`), имя скрипта, дашборда, таймера;
- число с единицей, порог, лимит, расписание («8 с на попытку», «300 → 1800 с», «14–37 КБ/с», «02:00», «3 копии»);
- код ошибки и текст ошибки/строки лога (`426 client_outdated`, «Processing message», NXDOMAIN, 403);
- версия («0.8.0 → 11.0.0»), имя сервиса, хоста, бота, логин или ник (@example_login, app-stable);
- пометка «устарело / больше не / раньше было / не путать / РЕШЕНО» — отдельным утверждением: что именно устарело
  и что вместо него.
Разные детали про разное — разные утверждения. Команда вместе со своими флагами и путями — одно утверждение.
НЕ ОБОБЩАЙ: «проверить версию» вместо «сервер сам назначил 0.8.0 → 11.0.0» — плохо; «есть лимиты» вместо
«потолок 8 с, breaker 300 → 1800 с» — плохо. Конкретика важнее краткости.

Что НЕ брать: хэши коммитов, id прогонов, счётчики тестов, секреты и токены, содержимое тел тикетов и вики,
тексты пользователей бота, пересказ без пользы. Пустая единица — ноль утверждений.

Поля каждого утверждения (одна JSON-строка на утверждение, без обёрток):
{{"unit": "<id единицы из заголовка>", "text": "<утверждение по-русски, самодостаточное, 1–2 предложения, с деталями дословно>",
  "kind": "rule|fact|gotcha|howto|event|person|preference", "topic": "<короткий slug темы латиницей, kebab>",
  "area": "<область>", "date": "ГГГГ-ММ-ДД", "quote": "<ДОСЛОВНЫЙ фрагмент единицы>", "sensitive": false}}

area — одна из:
{areas}
- projects/<slug> — отдельные проекты (например projects/blog);
- private — личное владельца, здоровье, психология, терапия, 1-1 и HR, контакты (телефоны, почты, адреса).
topic — короткий slug (deploy, release, team, onboarding, tracker-api, backup, …);
у близких утверждений — одинаковый topic.
date — дата, когда это было верно/решено (из текста или заголовка единицы).

quote — ОБЯЗАТЕЛЬНО дословная подстрока текста этой единицы (можно отличаться только пробелами/переносами),
10–200 символов, из ОДНОГО места, без многоточий, без перевода, без своих слов. Это проверяется программно:
утверждение без точной цитаты выбрасывается. Копируй символ в символ, включая `обратные кавычки`, **, регистр.

sensitive: true — медицина, лекарства, психология и терапия, личная жизнь, конфликты с людьми, HR и оценка
коллег, контакты (телефон, почта, адрес). Такое ставь в area "private". Рабочие логины коллег в рабочих сервисах
и их роли — НЕ sensitive (это рабочие данные, рабочая область).

Выведи ТОЛЬКО JSON-строки, по одной на строку. Без markdown, без пояснений.

{units}"""


def render_units(units):
    parts = []
    for u in units:
        parts.append(f"===== UNIT {u.uid} | kind={u.kind} | date={u.date} | норма≥{target_claims(u.text)} =====\n"
                     f"{u.text.strip()}\n")
    return "\n".join(parts)


def build_prompt(units):
    areas = "\n".join(f"- {a} — {area_description(a)};" for a in WORK_AREAS)
    return PROMPT.format(n=len(units), units=render_units(units), areas=areas)


def make_batches(units, size=18, max_bytes=110_000):
    batches, cur, cur_b = [], [], 0
    for u in units:
        b = len(u.text.encode("utf-8"))
        if cur and (len(cur) >= size or cur_b + b > max_bytes):
            batches.append(cur)
            cur, cur_b = [], 0
        cur.append(u)
        cur_b += b
    if cur:
        batches.append(cur)
    return batches


def looks_truncated(text):
    """Ответ оборван: последняя непустая строка начинается как JSON, но не разбирается."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.strip().startswith("```")]
    if not lines:
        return False
    last = lines[-1].rstrip(",")
    if not last.startswith("{"):
        return False
    try:
        json.loads(last)
        return False
    except ValueError:
        return True


def seen_units(parsed, units):
    """uid единиц батча в порядке первого появления в ответе."""
    by_uid = {u.uid for u in units}
    order = []
    for r in parsed:
        uid = str(r.get("unit") or "").strip()
        if uid in by_uid and uid not in order:
            order.append(uid)
    return order


def complete_units(parsed, units):
    """Оборванный ответ: единицы идут по порядку, поэтому все встреченные, кроме последней, разобраны целиком.
    → множество uid, которые можно пометить извлечёнными (остальные — в следующем прогоне)."""
    return set(seen_units(parsed, units)[:-1])


def parse_output(text):
    """JSONL (терпимо к ```-обёрткам и мусору) → список dict."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def quote_in(quote, text):
    """Детерминированная проверка: цитата дословно в тексте после нормализации пробелов."""
    q = normalize(str(quote or ""))
    if len(q) < 15:
        return False
    return q in normalize(text)


def clean_claim(raw, unit):
    """Приводит поля; src/origin берутся из единицы, а не из модели. → dict."""
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in KINDS:
        kind = "fact"
    area = str(raw.get("area") or "").strip().lower()
    sensitive = bool(raw.get("sensitive")) or area == "private"
    if not AREA_RX.match(area):
        area = "agents"
    if sensitive:
        area = "private"
    date = str(raw.get("date") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        date = unit.date
    topic = re.sub(r"[^a-z0-9-]+", "-", str(raw.get("topic") or "misc").lower()).strip("-") or "misc"
    quote = str(raw.get("quote") or "")
    return {"text": str(raw.get("text") or "").strip(), "kind": kind, "topic": topic[:40], "area": area,
            "date": date, "quote": quote, "src": unit.src, "origin": unit.origin, "sensitive": sensitive,
            "quote_ok": quote_in(quote, unit.text)}


def assign(parsed, units):
    """Раскладывает утверждения по единицам. Утверждения с неизвестным unit пробуем привязать по цитате."""
    by_uid = {u.uid: u for u in units}
    out = {u.uid: [] for u in units}
    orphans = 0
    for r in parsed:
        uid = str(r.get("unit") or "").strip()
        u = by_uid.get(uid)
        if u is None:
            # модель могла обрезать id — ищем единицу, где есть цитата
            u = next((x for x in units if quote_in(str(r.get("quote") or ""), x.text)), None)
        if u is None:
            orphans += 1
            continue
        c = clean_claim(r, u)
        if c["text"]:
            out[u.uid].append(c)
    return out, orphans
