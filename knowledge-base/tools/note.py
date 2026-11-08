#!/usr/bin/env python3
"""kb note — быстрый захват знания агентом: одна мысль 1–3 строки → входящие базы → сводчик → карточка.

Сервер с базой (`kb note …`): гейт секретов → `state/inbox-pending/vps/<дата>.md` (под state/inbox.lock) → сразу перенос
                    в `raw/inbox/vps/<дата>.md` + коммит под state/repo.lock (ждём до KB_NOTE_LOCK_WAIT, 15 с);
                    lock занят или checkout не на main — запись остаётся в pending, её переносит ближайший
                    `kb ingest` (раз в час).
Другая машина (`python3 note.py …`, этот же файл рядом с gate.py и kbcore.py): гейт →
                    `~/agent-knowledge-inbox/<дата>.md`; доставить папку в `raw/inbox/<машина>/` своим способом
                    (rsync, scp, git) — только добавление, без удаления.

Запись — блок `## <время> · <область> · n-<id>`; id — sha256 нормализованного текста (регистр, пробелы,
финальная пунктуация), повтор того же текста — `duplicate`, второй раз не пишется. Сводчик режет файл на
единицы по блокам (`consolidate/units.py`, kind `inbox`, origin `inbox:<id>`): одинаковая заметка с двух машин —
одна единица, новая заметка не меняет sha старых.
Находка гейта → блок в карантин (`raw/.quarantine/inbox/<host>/`, на другой машине `~/agent-knowledge-inbox/.quarantine/`),
в телеметрию — только имена шаблонов. Текст заметки в телеметрию не пишется.

python3.9+ stdlib.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import gate  # noqa: E402
from kbcore import AREA_ALT, AREA_HELP  # noqa: E402

MAX_CHARS = 700
MAX_LINES = 3
AREA_RX = re.compile(r"^(" + AREA_ALT + r"|projects/[a-z0-9][a-z0-9-]{0,40})$")
HEAD_RX = re.compile(r"^## (?P<ts>\S+ \S+) · (?P<area>[^·\n]+?) · n-(?P<id>[0-9a-f]{12})\s*$", re.M)
LOCAL_INBOX = "~/agent-knowledge-inbox"
LOCK_WAIT = float(os.environ.get("KB_NOTE_LOCK_WAIT", "15"))


class NoteError(Exception):
    """Невалидная заметка: код 2, текст — агенту."""


def norm_key(text: str) -> str:
    t = re.sub(r"\s+", " ", text.replace(" ", " ")).strip().lower()
    return re.sub(r"[\s.!;,…]+$", "", t)


def note_id(text: str) -> str:
    return hashlib.sha256(norm_key(text).encode("utf-8")).hexdigest()[:12]


def validate(text: str, area: str | None, source: str | None):
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise NoteError("пустая заметка")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if any(re.match(r"\s*#{1,6}\s", ln) for ln in lines):
        raise NoteError("без заголовков markdown (`# …`, `## …`): заметка — обычный текст")
    if len(lines) > MAX_LINES or len(text) > MAX_CHARS:
        raise NoteError(f"заметка — одна мысль в 1–3 строки (≤{MAX_CHARS} символов); "
                        f"сейчас {len(lines)} строк, {len(text)} символов — разбей на несколько kb note")
    if area is not None:
        if area == "private":
            raise NoteError("личное, здоровье, контакты через kb note не пишутся")
        if not AREA_RX.match(area):
            raise NoteError("область: " + AREA_HELP)
    source = re.sub(r"\s+", " ", source or "").strip()[:200] or None
    return "\n".join(lines), area, source


def render(text: str, area: str | None, source: str | None, ts: float) -> str:
    when = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    out = f"## {when} · {area or '—'} · n-{note_id(text)}\n{text}\n"
    if source:
        out += f"источник: {source}\n"
    return out + "\n"


def day_file(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d") + ".md"


def ids_in(dirs) -> set:
    """id всех заметок в *.md этих каталогов (без скрытых)."""
    out = set()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".md") and not fn.startswith("."):
                try:
                    with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
                        out.update(m.group("id") for m in HEAD_RX.finditer(f.read()))
                except OSError:
                    continue
    return out


def split_blocks(text: str):
    """Файл входящих → [(id, блок)]; шапка до первого `## …` отбрасывается."""
    ms = list(HEAD_RX.finditer(text))
    out = []
    for i, m in enumerate(ms):
        e = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append((m.group("id"), text[m.start():e].rstrip() + "\n"))
    return out


def _append(path: str, block: str, header: str):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write(header)
        f.write(block)
    os.chmod(path, 0o600)


def _header(host: str, fn: str) -> str:
    return f"# Заметки агентов — {host}, {fn[:-3]}\n\n"


class _Flock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


# ---------- VPS: база рядом ----------

def pending_dir(cfg, host="vps"):
    return os.path.join(cfg.state, "inbox-pending", host)


def inbox_dir(cfg, host="vps"):
    return os.path.join(cfg.raw, "inbox", host)


def flush_pending(cfg, host="vps"):
    """Перенос pending → raw/inbox/<host>/ (вызывать под repo_lock). Повторный гейт: шаблоны могли обновиться.
    → (перенесено, в карантин). Pending-файл удаляется только после записи всех его блоков."""
    moved = quarantined = 0
    pdir = pending_dir(cfg, host)
    if not os.path.isdir(pdir):
        return moved, quarantined
    with _Flock(os.path.join(cfg.state, "inbox.lock")):
        have = ids_in([inbox_dir(cfg, host)])
        for fn in sorted(os.listdir(pdir)):
            p = os.path.join(pdir, fn)
            if not fn.endswith(".md") or fn.startswith(".") or not os.path.isfile(p):
                continue
            with open(p, encoding="utf-8", errors="replace") as f:
                blocks = split_blocks(f.read())
            for nid, block in blocks:
                if nid in have:
                    continue
                if gate.scan_text(block):
                    _append(os.path.join(cfg.quarantine, "inbox", host, fn), block, "")
                    quarantined += 1
                    continue
                _append(os.path.join(inbox_dir(cfg, host), fn), block, _header(host, fn))
                have.add(nid)
                moved += 1
            os.remove(p)
    return moved, quarantined


def safe_area(area):
    """Область для журнала: только валидная (сырое значение --area могло оказаться чем угодно)."""
    return area if area is None or AREA_RX.match(area) else "invalid"


def note_vps(cfg, text, area=None, source=None, host="vps", out=print, now=None, flush=True, result=None):
    """→ код: 0 записано/дубль, 2 невалидно, 3 карантин. flush=False — только в очередь (пакетная запись
    kb harvest переносит очередь одним коммитом сама). result (dict) — сюда кладётся status."""
    result = {} if result is None else result
    from kbcore import LockTimeout, commit_paths, current_branch, log_run, new_run_id, repo_lock

    t0 = time.time()
    run_id = new_run_id()
    rec = {"cmd": "note", "run_id": run_id, "host": host, "area": safe_area(area)}
    try:
        text, area, source = validate(text, area, source)
    except NoteError as e:
        result["status"] = "invalid"
        log_run(cfg, **rec, status="invalid", error=str(e)[:200], duration_s=round(time.time() - t0, 3))
        out(f"kb note: {e}")
        return 2
    ts = now or time.time()
    nid, fn = note_id(text), day_file(ts)
    rec.update(id=f"n-{nid}", chars=len(text), source_set=bool(source))
    block = render(text, area, source, ts)
    hits = gate.scan_text(block)
    if hits:
        _append(os.path.join(cfg.quarantine, "inbox", host, fn), block, "")
        os.chmod(cfg.quarantine, 0o700)
        result["status"] = "quarantined"
        log_run(cfg, **rec, status="quarantined", patterns=sorted({h[0] for h in hits}),
                duration_s=round(time.time() - t0, 3))
        out(f"kb note: похоже на секрет ({', '.join(sorted({h[0] for h in hits}))}) — заметка в карантине, "
            f"в базу не пошла. Перепиши без значения (имя переменной или путь к файлу, но не сам ключ).")
        return 3
    with _Flock(os.path.join(cfg.state, "inbox.lock")):
        if nid in ids_in([inbox_dir(cfg, host), pending_dir(cfg, host)]):
            result["status"] = "duplicate"
            log_run(cfg, **rec, status="duplicate", duration_s=round(time.time() - t0, 3))
            out(f"kb note: уже записано (n-{nid}) — второй раз не пишу")
            return 0
        _append(os.path.join(pending_dir(cfg, host), fn), block, _header(host, fn))
    status, sha, err = "pending", None, None
    try:
        if not flush:
            err = "пакетная запись"
        elif current_branch(cfg) != "main":
            err = "checkout не на main"
        else:
            with repo_lock(cfg, timeout=LOCK_WAIT):
                flush_pending(cfg, host)
                status = "uncommitted"      # уже в raw/inbox; коммит упадёт — закоммитит kb ingest
                sha = commit_paths(cfg, [f"raw/inbox/{host}"], f"note {host}: n-{nid}")
                status = "ok"
    except LockTimeout:
        err = "state/repo.lock занят"
    except (RuntimeError, OSError) as e:   # git или диск: заметка остаётся в очереди / в raw для ingest
        err = f"{type(e).__name__}: {e}"[:200]
    result["status"] = status
    log_run(cfg, **rec, status=status, commit=sha, error=err, duration_s=round(time.time() - t0, 3))
    rel = f"raw/inbox/{host}/{fn}"
    if status == "ok":
        out(f"kb note: записано n-{nid} → {rel}" + (f" (коммит {sha})" if sha else "")
            + ". Сразу ищется через `kb find --raw`; в карточку — со следующим сводчиком (ежедневно 06:00 UTC).")
    elif status == "uncommitted":
        out(f"kb note: записано n-{nid} → {rel}, но коммит не удался ({err[:80]}); закоммитит ближайший "
            f"kb ingest (≤1 ч). Повторять не нужно.")
    else:
        out(f"kb note: записано n-{nid} в очередь ({err}); в {rel} перенесёт ближайший kb ingest (≤1 ч).")
    return 0


# ---------- другая машина: локальная папка входящих ----------

def note_local(text, area=None, source=None, inbox=LOCAL_INBOX, host=None, out=print, now=None):
    t0 = time.time()
    inbox = os.path.expanduser(inbox)
    tele = os.path.expanduser("~/.cache/agent-knowledge/kb-note.jsonl")
    host = host or socket.gethostname().split(".")[0].lower()[:40] or "remote"

    def log(**kw):
        try:
            os.makedirs(os.path.dirname(tele), exist_ok=True)
            with open(tele, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "cmd": "note", "host": host, "hostname": socket.gethostname()[:40], **kw,
                                    "duration_s": round(time.time() - t0, 3)}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    try:
        text, area, source = validate(text, area, source)
    except NoteError as e:
        log(status="invalid", area=safe_area(area), error=str(e)[:200])
        out(f"kb note: {e}")
        return 2
    ts = now or time.time()
    nid, fn = note_id(text), day_file(ts)
    block = render(text, area, source, ts)
    hits = gate.scan_text(block)
    if hits:
        _append(os.path.join(inbox, ".quarantine", fn), block, "")
        os.chmod(os.path.join(inbox, ".quarantine"), 0o700)
        log(status="quarantined", id=f"n-{nid}", area=area, patterns=sorted({h[0] for h in hits}))
        out(f"kb note: похоже на секрет ({', '.join(sorted({h[0] for h in hits}))}) — заметка в карантине, "
            f"на сервер не уйдёт. Перепиши без значения.")
        return 3
    with _Flock(os.path.join(inbox, ".lock")):
        if nid in ids_in([inbox]):
            log(status="duplicate", id=f"n-{nid}", area=area)
            out(f"kb note: уже записано (n-{nid})")
            return 0
        _append(os.path.join(inbox, fn), block, _header(host, fn))
    log(status="ok", id=f"n-{nid}", area=area, chars=len(text))
    out(f"kb note: записано n-{nid} → {inbox}/{fn}; в базу — после доставки папки, "
        f"в карточку — со следующим сводчиком.")
    return 0


def add_args(p):
    p.add_argument("text", nargs="+", help="факт одной–тремя строками (кавычками)")
    p.add_argument("--area", help=AREA_HELP)
    p.add_argument("--source", help="откуда: путь, тред, тикет-ключ (не текст тикета)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="kb_note.py", description="Записать устойчивое знание во входящие базы")
    add_args(p)
    p.add_argument("--inbox", default=os.environ.get("KB_NOTE_INBOX", LOCAL_INBOX))
    a = p.parse_args(argv)
    return note_local(" ".join(a.text), a.area, a.source, inbox=a.inbox)


if __name__ == "__main__":
    sys.exit(main())
