"""Нарезка raw/ на единицы — детерминированно.

Единица:
  - файл памяти Claude (raw/claude-<машина>/<project>/*.md), без индексов MEMORY.md/_index.md;
  - Task Group из Codex MEMORY.md (блок от `# Task Group:` до следующего);
  - memory_summary.md;
  - файл rollout_summaries/*.md;
  - заметка агента из raw/inbox/<host>/<дата>.md (`kb note`): блок `## … · n-<id>`, origin `inbox:<id>`.

Ключ — sha256 нормализованного текста. origin — откуда знание родом (копии схлопываются).
"""
import datetime as dt
import hashlib
import os
import re

INDEX_NAMES = {"MEMORY.md", "_index.md"}
DATE_IN_NAME = re.compile(r"(20\d\d-\d\d-\d\d)")
DATE_FIELD = re.compile(r"^(?:updated_at|modified|date|created)\s*:\s*\"?(20\d\d-\d\d-\d\d)", re.M)
TG_RX = re.compile(r"^# Task Group:[^\n]*$", re.M)


def normalize(text):
    """Нормализация для ключа и проверки цитат: CRLF, неразрывные пробелы, схлопнуть пробелы."""
    t = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", t).strip()


def sha(text):
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


class Unit:
    __slots__ = ("uid", "src", "kind", "origin", "text", "sha", "date")

    def __init__(self, uid, src, kind, origin, text, date):
        self.uid = uid          # уникальный id единицы: путь (+ #якорь для Task Group)
        self.src = src          # путь файла от корня базы: raw/...
        self.kind = kind        # claude-memory | codex-task-group | codex-summary | codex-rollout | inbox | live
        self.origin = origin
        self.text = text
        self.sha = sha(text)
        self.date = date

    def __repr__(self):
        return f"Unit({self.uid!r}, {self.kind}, {self.origin!r})"


def _date(fname, text, path):
    m = DATE_IN_NAME.search(fname)
    if m:
        return m.group(1)
    m = DATE_FIELD.search(text[:3000])
    if m:
        return m.group(1)
    try:
        return dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc).strftime("%Y-%m-%d")
    except OSError:
        return ""


def _slug(s, n=60):
    s = re.sub(r"[^\w]+", "-", s.lower(), flags=re.U).strip("-")
    return s[:n].strip("-") or "x"


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def split_task_groups(text):
    """MEMORY.md Codex → [(заголовок, блок)]. Текст до первого заголовка отбрасывается (это шапка)."""
    starts = [m.start() for m in TG_RX.finditer(text)]
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[s:e].rstrip() + "\n"
        header = block.split("\n", 1)[0][len("# Task Group:"):].strip()
        out.append((header, block))
    return out


# заголовок заметки kb note (tools/note.py HEAD_RX): `## 2026-10-25 14:03Z · infra · n-<12 hex>`
NOTE_RX = re.compile(r"^## \S+ \S+ · [^·\n]+? · n-([0-9a-f]{12})\s*$", re.M)


def split_notes(text):
    """Файл входящих → [(id, блок)]; шапка до первой заметки отбрасывается."""
    ms = list(NOTE_RX.finditer(text))
    out = []
    for i, m in enumerate(ms):
        e = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append((m.group(1), text[m.start():e].rstrip() + "\n"))
    return out


def _md_files(base):
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fn in sorted(files):
            p = os.path.join(dirpath, fn)
            if fn.endswith(".md") and not fn.startswith((".", "._")) and os.path.isfile(p) \
                    and not os.path.islink(p):
                yield p


def iter_units(raw_root, kb_root=None, verified_dirs=None):
    """Все единицы из raw/ (+ verified/). Порядок детерминирован (по uid)."""
    kb_root = kb_root or os.path.dirname(raw_root.rstrip("/"))
    units = []

    def rel(p):
        return os.path.relpath(p, kb_root)

    def hosts(prefix):
        # raw/claude-vps, raw/claude-<другая машина>, … — по одной папке на машину
        if not os.path.isdir(raw_root):
            return []
        return sorted(d for d in os.listdir(raw_root) if d.startswith(prefix) and not d.startswith("."))

    for host in hosts("claude-"):
        base = os.path.join(raw_root, host)
        if not os.path.isdir(base):
            continue
        for p in _md_files(base):
            fn = os.path.basename(p)
            if fn in INDEX_NAMES:
                continue
            r = os.path.relpath(p, base).split(os.sep)
            project = r[0] if len(r) > 1 else ""
            text = _read(p)
            units.append(Unit(rel(p), rel(p), "claude-memory", f"{host}:{project}/{fn}", text,
                              _date(fn, text, p)))

    for host in hosts("codex-"):
        base = os.path.join(raw_root, host)
        if not os.path.isdir(base):
            continue
        mem = os.path.join(base, "MEMORY.md")
        if os.path.isfile(mem):
            text = _read(mem)
            seen = {}
            for header, block in split_task_groups(text):
                slug = _slug(header)
                seen[slug] = seen.get(slug, 0) + 1
                anchor = slug if seen[slug] == 1 else f"{slug}-{seen[slug]}"
                m = DATE_FIELD.search(block) or re.search(r"updated_at=(20\d\d-\d\d-\d\d)", block)
                date = m.group(1) if m else _date("", "", mem)
                # одна и та же группа в MEMORY двух машин (импорт) — один origin
                units.append(Unit(f"{rel(mem)}#tg:{anchor}", rel(mem), "codex-task-group",
                                  f"codex-tg:{hashlib.sha256(normalize(header).encode()).hexdigest()[:16]}",
                                  block, date))
        summ = os.path.join(base, "memory_summary.md")
        if os.path.isfile(summ):
            text = _read(summ)
            units.append(Unit(rel(summ), rel(summ), "codex-summary", f"{host}:memory_summary", text,
                              _date("", text, summ)))
        rs = os.path.join(base, "rollout_summaries")
        if os.path.isdir(rs):
            for p in _md_files(rs):
                fn = os.path.basename(p)
                text = _read(p)
                units.append(Unit(rel(p), rel(p), "codex-rollout", f"codex-rollout:{fn}", text,
                                  _date(fn, text, p)))

    # заметки агентов (kb note): raw/inbox/<host>/<дата>.md; единица — блок заметки, origin — её id
    # (одна заметка с двух машин — одна единица; новая заметка в том же файле не меняет sha старых)
    base = os.path.join(raw_root, "inbox")
    if os.path.isdir(base):
        for p in _md_files(base):
            fn = os.path.basename(p)
            if fn in INDEX_NAMES:
                continue
            text = _read(p)
            date = _date(fn, text, p)
            for nid, block in split_notes(text):
                units.append(Unit(f"{rel(p)}#n-{nid}", rel(p), "inbox", f"inbox:{nid}", block, date))

    # живые проверки агентов (verified/*.md): факты, проверенные вживую; у карточки → verified_how: live
    for vdir in (verified_dirs or []):
        if not os.path.isdir(vdir):
            continue
        for p in _md_files(vdir):
            fn = os.path.basename(p)
            if fn in INDEX_NAMES or fn.lower() == "readme.md":
                continue
            text = _read(p)
            r = "verified/" + os.path.relpath(p, vdir)
            units.append(Unit(r, r, "live", f"live:{fn}", text, _date(fn, text, p)))

    units.sort(key=lambda u: u.uid)
    return units


def collapse(units):
    """Схлопнуть копии: по sha (одинаковый текст) и по origin (берём самую свежую по дате).

    → (уникальные единицы, {uid_копии: uid_основной})."""
    keep, dup = {}, {}

    def better(a, b):
        return (a.date, a.uid) > (b.date, b.uid)

    by_origin = {}
    for u in units:
        cur = by_origin.get(u.origin)
        if cur is None:
            by_origin[u.origin] = u
        elif better(u, cur):
            dup[cur.uid] = u.uid
            by_origin[u.origin] = u
        else:
            dup[u.uid] = cur.uid
    by_sha = {}
    for u in sorted(by_origin.values(), key=lambda x: x.uid):
        if u.sha in by_sha:
            dup[u.uid] = by_sha[u.sha].uid
            continue
        by_sha[u.sha] = u
        keep[u.uid] = u
    # переадресация цепочек
    for k in list(dup):
        v = dup[k]
        seen = set()
        while v in dup and v not in seen:
            seen.add(v)
            v = dup[v]
        dup[k] = v
    return sorted(keep.values(), key=lambda x: x.uid), dup
