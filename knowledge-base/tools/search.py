"""Поиск по карточкам: BM25 по полям, лёгкий русский стемминг, стоп-слова, кэш индекса.

Поля и веса: keys 3.0 > description 2.5 > name 2.0 > тело 1.0. Каждое поле считается
отдельным BM25 и складывается с весом. У ключей нет нормировки длины (b=0), поэтому одно
совпадение в ключах (3.0·idf) всегда выше любого числа повторов в теле (< 1.0·(k1+1)·idf).

Кэш: state/find-index.json (0600) — только публичные карточки (папки private/ на любом
уровне в кэш не пишутся никогда). Подпись — (путь, mtime, размер) каждой карточки,
INDEX.tsv и кода поиска (search.py, kbcore.py); любая правка пересобирает индекс.
"""
import hashlib
import json
import math
import os
import re

from kbcore import Card, as_list, norm

INDEX_VERSION = 2
INDEX_FILE = "find-index.json"
FIELDS = ("name", "keys", "desc", "body")
FIELD_W = {"name": 2.0, "keys": 3.0, "desc": 2.5, "body": 1.0}
K1 = 1.2
FIELD_B = {"name": 0.0, "keys": 0.0, "desc": 0.75, "body": 0.75}
SUT_CHARS = 200
MAX_OLD = 2  # устаревших карточек сверх лимита
CODE_FILES = tuple(os.path.join(os.path.dirname(os.path.abspath(__file__)), f)
                   for f in ("search.py", "kbcore.py"))

# токен: буквы/цифры, склеенные через - _ . / (my-service, PROJ-123, app.service)
WORD_RX = re.compile(r"[^\W_]+(?:[-_./][^\W_]+)*")
SPLIT_RX = re.compile(r"[-_./]")
CYR_RX = re.compile(r"[а-я]")
SUT_RX = re.compile(r"\*\*Суть\.?\*\*[.:]?\s*")

STOP_RU = set("""
а без более больше будет будто бы был была были было быть в вам вас ведь весь во вот все всего
всегда всех всю вы где да даже для до другой его ее ей ему если есть еще ж же за зачем здесь и из
или им иногда их к как какая какие какой когда конечно кто куда ли лучше между меня мне много может
можно мой моя мы на над надо наконец нас наш наша наше наши не него нее ней нельзя нет ни нибудь
никогда ним них ничего но ну нужен нужна нужно о об один он она они опять от перед по под после
потом потому почему почти при про раз разве с сам свою себе себя сейчас со совсем так также такой
там тебя тем теперь то тогда того тоже только том тот три тут ты у уж уже хоть хочу чего чем через
что чтоб чтобы чуть эта эти это этого этой этом этот эту я подскажи скажи расскажи напомни
либо таки кое
""".split())
STOP_EN = set("""
a about after all also an and any are as at be been being but by can could did do does for from
had has have he her his how i if in into is it its just me my no not of on or our out over she
should so than that the their them then there these they this those to too up us very was we were
what when where which who whom why will with would you your
""".split())
STOP = STOP_RU | STOP_EN

RU_SUFFIXES = sorted(set("""
иями ями ами иях ях ах иям ям ам ией ий ый ой ей ая яя ое ее ие ые ого его ому ему ыми ими ую юю
ом ем ов ев ия ья ье ью ию ии ться тся ать ять ить еть уть ешь ете ает яет еет ует ают яют уют
ила ило или ала али ало ела ели ыла ыли ал ил ял ел ть а я о е и ы у ю ь й
""".split()), key=len, reverse=True)
# короткие основы (бот, лог, код, кэш, тег, чат): окончание снимается, если остаётся
# ровно 3 буквы и последняя согласная — «бота/боту/ботом» → «бот»
RU_SHORT_SUFFIXES = sorted(set("ами ом ов ам ах ой ем а у ы е и о ю я".split()), key=len, reverse=True)
EN_SUFFIXES = ("ing", "ed", "es", "s")
RU_VOWELS = set("аеиоуыэюяйь")
MIN_STEM = 4


def _stem_ru(w):
    for suf in RU_SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= MIN_STEM:
            w = w[:-len(suf)]
            # «деплой»/«деплоя» → «депл»: хвостовую гласную после окончания тоже снять
            if len(w) > MIN_STEM and w[-1] in RU_VOWELS:
                w = w[:-1]
            return w
    for suf in RU_SHORT_SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) == 3 and w[-len(suf) - 1] not in RU_VOWELS:
            return w[:-len(suf)]
    if len(w) > MIN_STEM and w[-1] in RU_VOWELS:
        w = w[:-1]
    return w


def _stem_en(w):
    if w.endswith("ss"):
        return w
    for suf in EN_SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= MIN_STEM:
            w = w[:-len(suf)]
            break
    # service/services → servic, release/released → releas
    if w.endswith("e") and len(w) - 1 >= MIN_STEM:
        w = w[:-1]
    return w


def stem(w):
    """Лёгкий стемминг: одно окончание, основа не короче 4 символов (для русских основ
    из 3 согласных на конце — короткий список окончаний). Цифры не трогаем."""
    if len(w) <= 3 or any(ch.isdigit() for ch in w):
        return w
    if CYR_RX.search(w):
        return _stem_ru(w)
    if w.isascii() and w.isalpha():
        return _stem_en(w)
    return w


def _word_terms(w):
    """Простое слово → [основа] или [] для стоп-слов и однобуквенных."""
    if len(w) < 2 or w in STOP:
        return []
    return [stem(w)]


def terms(text):
    """Текст → список термов. Составной токен (my-service, PROJ-123) даёт себя целиком
    (русская последняя часть со стеммингом: слэш-команду → слэш-команд) и части по
    отдельности; составные только из стоп-слов (из-за, что-то) отбрасываются."""
    out = []
    for tok in WORD_RX.findall(norm(text or "")):
        parts = [p for p in SPLIT_RX.split(tok) if p]
        if len(parts) == 1:
            out.extend(_word_terms(tok))
            continue
        if all(len(p) < 2 or p in STOP for p in parts):  # из-за, что-то
            continue
        last = parts[-1]
        # слэш-команду → слэш-команд; латинские идентификаторы (app-stable) — как есть
        out.append(tok[:-len(last)] + stem(last) if CYR_RX.search(last) else tok)
        for p in parts:
            out.extend(_word_terms(p))
    return out


def query_terms(query):
    """Уникальные термы запроса в исходном порядке."""
    seen, out = set(), []
    for t in terms(query):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def sut(body, limit=SUT_CHARS):
    """Первые ~limit символов после `**Суть.**` (до следующего раздела); иначе первый абзац."""
    m = SUT_RX.search(body or "")
    if m:
        text = body[m.end():]
        cut = re.search(r"\n#{1,6} ", text)
        if cut:
            text = text[:cut.start()]
        lines = text.splitlines()
    else:
        lines = [ln for ln in (body or "").splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")][:3]
    text = ""
    for ln in (re.sub(r"\s+", " ", x).strip() for x in lines):
        if not ln:
            continue
        # строки без знака в конце — отдельные утверждения: разделить «; »
        text += ("" if not text else " " if text[-1] in ".!?:;…" else "; ") + ln
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    if sp > limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:—-") + "…"


def _tf(tokens):
    d = {}
    for t in tokens:
        d[t] = d.get(t, 0) + 1
    return d


def doc_of(c):
    """Карточка → запись индекса (поля как частоты термов + всё нужное для вывода)."""
    fields = {
        "name": terms(c.name),
        "keys": terms(" ; ".join(c.keys)),
        "desc": terms(c.description),
        "body": terms(c.body),
    }
    return {
        "rel": c.rel, "name": c.name, "type": c.type, "status": c.status,
        "verified": c.verified, "description": c.description,
        "superseded_by": as_list(c.get("superseded_by")),
        "sut": sut(c.body), "private": c.private,
        "tf": {f: _tf(v) for f, v in fields.items()},
        "len": {f: len(v) for f, v in fields.items()},
    }


# ---------- кэш ----------

def _card_paths(cfg, private):
    """Файлы карточек по правилам iter_cards. private=False — без папок private/ на любом
    уровне; private=True — только файлы внутри папок private/."""
    out = []
    if not os.path.isdir(cfg.cards):
        return out
    for dirpath, dirs, files in os.walk(cfg.cards):
        rel = os.path.relpath(dirpath, cfg.cards)
        in_private = "private" in rel.split(os.sep)
        if rel == ".":
            dirs[:] = [d for d in dirs if d != "_templates" and not d.startswith(".")]
        else:
            dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
        if private and not in_private:
            continue
        if not private:
            dirs[:] = [d for d in dirs if d != "private"]
        for fn in files:
            if fn.endswith(".md") and not fn.startswith(("_", ".")):
                p = os.path.join(dirpath, fn)
                if os.path.isfile(p) and not os.path.islink(p):
                    out.append(p)
    return sorted(out)


def signature(cfg, paths):
    h = hashlib.sha1(f"v{INDEX_VERSION}".encode())
    for p in list(CODE_FILES):
        try:
            st = os.stat(p)
            h.update(f"code:{os.path.basename(p)}|{st.st_mtime_ns}|{st.st_size}\n".encode())
        except OSError:
            h.update(f"code:{p}|-\n".encode())
    for p in [os.path.join(cfg.root, "INDEX.tsv")] + paths:
        try:
            st = os.stat(p)
        except OSError:
            h.update(f"{p}|-\n".encode())
            continue
        h.update(f"{os.path.relpath(p, cfg.root)}|{st.st_mtime_ns}|{st.st_size}\n".encode())
    return h.hexdigest()


def index_path(cfg):
    return os.path.join(cfg.state, INDEX_FILE)


def load_docs(cfg):
    """Публичные карточки из кэша; кэш устарел или битый — пересобрать и записать.
    → (docs, источник: 'cache' | 'built')."""
    paths = _card_paths(cfg, private=False)
    sig = signature(cfg, paths)
    path = index_path(cfg)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == INDEX_VERSION and data.get("signature") == sig:
            return data["docs"], "cache"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    docs = [doc_of(Card(cfg, p)) for p in paths]
    if os.path.isdir(cfg.state):
        tmp = f"{path}.{os.getpid()}.kbtmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": INDEX_VERSION, "signature": sig, "docs": docs}, f,
                          ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return docs, "built"


# ---------- ранжирование ----------

def bm25(docs, qterms):
    """→ [(score, doc)] для документов с хотя бы одним совпадением."""
    if not docs or not qterms:
        return []
    n = len(docs)
    avg = {f: (sum(d["len"][f] for d in docs) / n) or 1.0 for f in FIELDS}
    idf = {}
    for t in qterms:
        df = sum(1 for d in docs if any(t in d["tf"][f] for f in FIELDS))
        if df:
            idf[t] = math.log(1 + (n - df + 0.5) / (df + 0.5))
    if not idf:
        return []
    out = []
    for d in docs:
        s = 0.0
        for t, w in idf.items():
            for f in FIELDS:
                tf = d["tf"][f].get(t)
                if tf:
                    b = FIELD_B[f]
                    norm_len = 1 - b + b * d["len"][f] / avg[f]
                    s += FIELD_W[f] * w * tf * (K1 + 1) / (tf + K1 * norm_len)
        if s > 0:
            out.append((s, d))
    return out


def _date_num(v):
    return int(v.replace("-", "")) if re.match(r"^\d{4}-\d{2}-\d{2}$", v or "") else 0


def search(cfg, query, limit=10, private=False):
    """→ список doc: до limit живых по рангу, ниже — до MAX_OLD устаревших, которые
    стояли в выдаче выше последней взятой живой (сверх лимита, живые не вытесняют)."""
    docs, _ = load_docs(cfg)
    if private:
        docs = docs + [doc_of(Card(cfg, p)) for p in _card_paths(cfg, private=True)]
    res = bm25(docs, query_terms(query))
    res.sort(key=lambda x: (-x[0], -_date_num(x[1]["verified"]), x[1]["name"]))
    live, old = [], []
    for _, d in res:
        if len(live) >= max(limit, 0):
            break
        if d["status"] == "superseded":
            if len(old) < MAX_OLD:
                old.append(d)
        else:
            live.append(d)
    return live + old
