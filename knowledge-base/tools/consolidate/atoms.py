"""Операционные атомы и ключевые токены — детерминированно, без LLM.

Атом — деталь, которую сводчик не имеет права потерять: команда в `обратных кавычках`, флаг CLI, путь или
имя файла, число с единицей, версия, код ошибки, имя сервиса/таймера, @логин. Используется:
  - стадией 1 — норма извлечения и телеметрия покрытия атомов;
  - стадией 2 — ключевые токены утверждения и гейт «утверждение не пропало».
"""
import math
import re

from .units import normalize

_UNITS = (r"мс|ms|сек|с|s|мин|min|ч|h|дн|дней|КБ/с|МБ/с|KB/s|MB/s|КБ|МБ|ГБ|KB|MB|GB|TB|%|₽|\$|"
          r"раз|ток|токенов|tokens|строк|байт|Б")
ATOM_RULES = [
    ("code", re.compile(r"`([^`\n]{2,80})`")),
    ("flag", re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]{1,40}(?:=[\w.:/-]+)?)", re.I)),
    # путь: от корня/домашней/текущей папки, или ≥2 слэшей, или с расширением файла («вход/выход» — не путь)
    ("path", re.compile(r"(?<![\w/])((?:~|\.{1,2})?/[\w.@-]+(?:/[\w.@-]+)*/?"
                        r"|[\w.-]+/[\w.@-]+/[\w.@/-]+"
                        r"|[\w.-]+(?:/[\w.@-]+)+\.[a-z]{1,6})")),
    ("file", re.compile(r"\b([\w-]+\.(?:jsonl|json|md|py|sh|ya?ml|toml|service|timer|sqlite|txt|log|cjs|mjs|js|ts|"
                        r"env|plist|conf))\b", re.I)),
    ("num", re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?(?:\s?[–-]\s?\d+(?:[.,]\d+)?)?\s?(?:" + _UNITS +
                       r"))(?![\wа-яё])", re.I)),
    ("ver", re.compile(r"(?<![\d.])(v?\d+\.\d+\.\d+)(?![\d.])")),
    ("bignum", re.compile(r"(?<![\w.:/,-])(\d{3,6})(?![\w:/-]|[.,]\d)")),   # порты, счётчики, коды
    ("http", re.compile(r"(?<![\w.])([45]\d\d)(?=\s+[A-Za-z_]|\s*[(«\"])")),
    ("login", re.compile(r"(?<![\w.])(@[A-Za-z][\w]{3,})")),
]
# «1 800 с» → «1800 с»: только группа 1–3 цифр + 3 цифры + единица («порты 8080 443» не склеиваются)
_THOUS = re.compile(r"(?<![\d.,])(\d{1,3}) (?=\d{3}(?!\d)\s?(?:" + _UNITS + r")(?![\wа-яё]))", re.I)
# «8 с» = «8с»: пробел убирается только перед единицей измерения («8080 открыт», «2.6.0 работает» — как есть)
_UNITGLUE = re.compile(r"(\d) (?=(?:" + _UNITS + r")(?![\wа-яё]))", re.I)
_SQUASH = re.compile(r"[\s`'\"=]+")
_DATE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{2,4}$")
_YEAR = re.compile(r"^(?:19|20)\d\d$")
# служебные пути, которые не несут знания
_JUNK = re.compile(r"^(?:\d+/\d+|[./]+|https?:)$|^/tmp/[\w.-]{0,3}$")

STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было
вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж
вам ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто
чего раз тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем
чтобы нее сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через
эти нас про всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя
такой им более всегда конечно всю между это этого нужно через чтобы который которые которая также the and for
with from that this into only when then than are was not use""".split())
WORD_RX = re.compile(r"[A-Za-zА-Яа-яЁё][\w-]{5,}")


def atoms(text):
    """→ упорядоченный список уникальных атомов текста (как в тексте)."""
    out, seen = [], set()
    text = _THOUS.sub(r"\1", text or "")
    for _, rx in ATOM_RULES:
        for m in rx.finditer(text):
            a = m.group(1).strip().strip(".,;:")
            if len(a) < 3 or _JUNK.match(a) or _DATE.match(a) or _YEAR.match(a):
                continue
            k = a.lower()
            if k not in seen:
                seen.add(k)
                out.append(a)
    return out


def _norm(text):
    """Нормализация для сравнения: пробелы, регистр, ё, «1800 с» = «1800с», «1 800» = «1800»."""
    t = _THOUS.sub(r"\1", normalize(text or "").lower().replace("ё", "е"))
    return _UNITGLUE.sub(r"\1", t)


def has_atom(atom, nt):
    """Атом есть в нормализованном тексте — с границами («8 с» не находится в «8 строк», «300» — в «13000»).
    Многословный атом (команда из `code`) засчитывается и после переформатирования (кавычки, `=`, пробелы),
    и если сохранены все вложенные атомы (флаги, пути)."""
    na = _norm(atom)
    if not na:
        return False
    if re.search(r"(?<![\w.])" + re.escape(na) + r"(?!\w)", nt):
        return True
    if " " in na or "=" in na:
        sq = _SQUASH.sub("", na)
        if len(sq) >= 4 and sq in _SQUASH.sub("", nt):
            return True
        subs = [x for x in atoms(atom) if _norm(x) != na]
        if subs and all(has_atom(x, nt) for x in subs):
            return True
    return False


MIN_WORD = 6


def _stem(w):
    """Основа: слово без двух последних букв (окончание), не короче 5."""
    w = w.lower().replace("ё", "е")
    return w[:max(5, len(w) - 2)]


def claim_keys(claim):
    """Ключевые токены утверждения: атомы его текста (иначе — атомы цитаты, которые есть в тексте),
    иначе 3 самых длинных содержательных слова (основы). → (атомы, основы слов)."""
    text = claim.get("text") or ""
    at = atoms(text)
    if not at:
        qt = atoms(claim.get("quote") or "")
        nt = _norm(text)
        at = [a for a in qt if has_atom(a, nt)]
    words = []
    if not at:
        cand = sorted({w for w in WORD_RX.findall(text) if w.lower() not in STOP and len(w) >= MIN_WORD},
                      key=lambda w: (-len(w), w))
        words = [_stem(w) for w in cand[:3]]
    return at, words


def _words_in(nt):
    return {w.lower() for w in WORD_RX.findall(nt)}


def _word_hits(words, nt):
    toks = _words_in(nt)
    return [w for w in words if any(t.startswith(w) for t in toks)]


def claim_present(claim, text, norm_text=None):
    """Утверждение находится в тексте: дословная цитата, или хотя бы один атом, или (утверждение без атомов)
    2 из 3 основ ключевых слов (1 из 1, если слово одно)."""
    nt = norm_text if norm_text is not None else _norm(text)
    q = _norm(claim.get("quote") or "")
    if len(q) >= 15 and q in nt:
        return True
    at, words = claim_keys(claim)
    if at:
        return any(has_atom(a, nt) for a in at)
    if words:
        return len(_word_hits(words, nt)) >= min(2, len(words))
    return False


def fully_present(claim, nt):
    """Строже claim_present — для засчёта переноса: дословная цитата, все атомы или все основы слов."""
    q = _norm(claim.get("quote") or "")
    if len(q) >= 15 and q in nt:
        return True
    at, words = claim_keys(claim)
    if at:
        return all(has_atom(a, nt) for a in at)
    return bool(words) and len(_word_hits(words, nt)) == len(words)


def missing_details(claim, old_nt, new_nt):
    """Гейт «утверждение не пропало»: какие детали утверждения были в старом тексте и пропали из нового.
    С атомами — КАЖДЫЙ атом, который был в старом тексте, должен остаться; без атомов — утверждение должно
    по-прежнему находиться (цитата или основы слов). → список пропавших токенов (пусто — не пропало)."""
    at, words = claim_keys(claim)
    if at:
        return [a for a in at if has_atom(a, old_nt) and not has_atom(a, new_nt)]
    if claim_present(claim, "", old_nt) and not claim_present(claim, "", new_nt):
        hit = set(_word_hits(words, new_nt))
        return [w for w in words if w not in hit][:2] or ["цитата"]
    return []


def key_hint(claim, n=2):
    """Короткая подсказка для причины отказа: какие токены искали (без похожего на секрет)."""
    at, words = claim_keys(claim)
    return _safe_hint(", ".join(f"`{x}`" for x in (at or words)[:n]) or "—")


def _safe_hint(s):
    try:
        import gate as secret_gate
        if secret_gate.scan_text(s):
            return "—"
    except ImportError:   # gate.py лежит в tools/; без него — как есть
        pass
    return s


def norm_text(text):
    return _norm(text)


# ---------- норма извлечения стадии 1 ----------

LINES_PER_CLAIM = 20     # ориентир 1 утверждение на ~15–25 содержательных строк
MIN_CLAIMS = 2
LINE_WRAP = 200          # длинная строка (абзац итога Codex) считается за несколько


def content_lines(text):
    """Содержательные строки: непустые, не разделители; длинные — с переносом по 200 символов."""
    n = 0
    in_fm = False
    for i, ln in enumerate((text or "").splitlines()):
        s = ln.strip()
        if i == 0 and s == "---":
            in_fm = True
            continue
        if in_fm:
            if s == "---":
                in_fm = False
            continue
        if len(s) < 3 or set(s) <= set("-=`*#|_ "):
            continue
        n += max(1, math.ceil(len(s) / LINE_WRAP))
    return n


def target_claims(text):
    """Норма: не меньше 2, ~1 на 20 содержательных строк, без потолка."""
    return max(MIN_CLAIMS, math.ceil(content_lines(text) / LINES_PER_CLAIM))


def atom_coverage(unit_text, claims):
    """Доля атомов единицы, которые попали дословно в текст или цитату хотя бы одного утверждения."""
    at = atoms(unit_text)
    if not at:
        return None
    blob = _norm(" ".join((c.get("text") or "") + " " + (c.get("quote") or "") for c in claims))
    hit = sum(1 for a in at if _norm(a) in blob)
    return hit / len(at)
