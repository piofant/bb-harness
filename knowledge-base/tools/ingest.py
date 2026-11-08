"""kb ingest: сырьё этой машины → raw/<src>/ через гейт секретов, коммит при диффе.

Источники:
  claude-vps  ~/.claude/projects/*/memory/*.md            → raw/claude-vps/<project>/
  codex-vps   ~/.codex/memories/{MEMORY.md,memory_summary.md,rollout_summaries/*.md}
  inbox/vps   заметки `kb note` (state/inbox-pending/vps → raw/inbox/vps; обычно переносит сам kb note)

Сырьё других машин кладётся рядом своим способом (rsync и т.п.): raw/claude-<машина>/, raw/codex-<машина>/,
raw/inbox/<машина>/ — сводчик читает все такие папки.
"""
import json
import os
import posixpath
import time

import gate
from kbcore import (commit_paths, free_gb, git, log_run, new_run_id, repo_lock,
                    LockTimeout)

MAX_FILE = 5 * 1024 * 1024
MAX_TOTAL = 300 * 1024 * 1024
VPS_SOURCES = ["claude-vps", "codex-vps"]
VPS_INBOX = "inbox/vps"
MASS_DELETE_RATIO = 0.5
MASS_DELETE_MIN = 20


class SourceError(Exception):
    pass


# ---------- чтение источников ----------

def _read_file(path):
    st = os.lstat(path)
    if not os.path.isfile(path) or os.path.islink(path) or st.st_size > MAX_FILE:
        return None
    with open(path, "rb") as f:
        return f.read(), st.st_mtime


def read_tree(base, exclude_rel=()):
    """Все *.md под base (без симлинков и скрытых папок) → {rel: (bytes, mtime)}."""
    out = {}
    for dirpath, dirs, files in os.walk(base, followlinks=False):
        rel_dir = os.path.relpath(dirpath, base)
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in dirs:
            r = os.path.join(rel_dir, d) if rel_dir else d
            if d.startswith(".") or r in exclude_rel or os.path.islink(os.path.join(dirpath, d)):
                continue
            keep.append(d)
        dirs[:] = sorted(keep)
        for fn in sorted(files):
            if not fn.endswith(".md") or fn.startswith("."):
                continue
            item = _read_file(os.path.join(dirpath, fn))
            if item:
                out[os.path.join(rel_dir, fn) if rel_dir else fn] = item
    return out


def collect_claude_vps(cfg):
    base = cfg.claude_projects
    if not os.path.isdir(base):
        raise SourceError(f"нет каталога {base}")
    out = {}
    for proj in sorted(os.listdir(base)):
        mem = os.path.join(base, proj, "memory")
        if not os.path.isdir(mem) or os.path.islink(mem):
            continue
        for fn in sorted(os.listdir(mem)):
            p = os.path.join(mem, fn)
            if fn.endswith(".md") and not fn.startswith("."):
                item = _read_file(p)
                if item:
                    out[f"{proj}/{fn}"] = item
    return out


def collect_codex_vps(cfg):
    base = cfg.codex_memories
    if not os.path.isdir(base):
        raise SourceError(f"нет каталога {base}")
    out = {}
    for fn in ("MEMORY.md", "memory_summary.md"):
        p = os.path.join(base, fn)
        if os.path.isfile(p):
            item = _read_file(p)
            if item:
                out[fn] = item
    rs = os.path.join(base, "rollout_summaries")
    if os.path.isdir(rs):
        for rel, item in read_tree(rs).items():
            out["rollout_summaries/" + rel] = item
    return out


def safe_rel(rel):
    n = posixpath.normpath(rel.replace(os.sep, "/"))
    if (n.startswith(("/", "../")) or n in ("..", ".") or "/../" in f"/{n}/"
            or any(part.startswith(".") for part in n.split("/"))):
        raise SourceError(f"небезопасный путь: {rel!r}")
    return n


def _write_atomic(path, data, mtime):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".kbtmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.utime(tmp, (mtime, mtime))
    os.replace(tmp, path)


def _list_files(base):
    out = set()
    if not os.path.isdir(base):
        return out
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            out.add(os.path.relpath(os.path.join(dirpath, fn), base).replace(os.sep, "/"))
    return out


def _prune_empty(base):
    if not os.path.isdir(base):
        return
    for dirpath, dirs, files in os.walk(base, topdown=False):
        if dirpath != base and not os.listdir(dirpath):
            os.rmdir(dirpath)


GATE_CACHE_MAX = 50000


def _gate_cache_path(cfg):
    return os.path.join(cfg.state, "gate-cache.json")


def _load_gate_cache(cfg):
    """sha256 файлов, уже прошедших гейт этой версии (чтобы не гонять регулярки каждый час)."""
    try:
        with open(_gate_cache_path(cfg), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("version") == gate.VERSION and isinstance(d.get("clean"), list):
            return set(d["clean"])
    except (OSError, ValueError, AttributeError):
        pass
    return set()


def _save_gate_cache(cfg, cache):
    if len(cache) > GATE_CACHE_MAX:
        cache = set()
    path = _gate_cache_path(cfg)
    os.makedirs(cfg.state, exist_ok=True)
    tmp = path + ".kbtmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": gate.VERSION, "clean": sorted(cache)}, f)
    os.replace(tmp, path)


def sync_source(cfg, src, entries, run_id, mirror=True, pregated=False):
    """Зеркалит entries в raw/<src>; подозрительное — в raw/.quarantine/<src>.
    mirror=False (входящие заметки) — только добавление и обновление: пропавшее в источнике не удаляется.
    pregated=True — гейт уже пройден поблочно (_merge_inbox): файл целиком не перепроверяется, иначе обновлённый
    шаблон на старом принятом блоке уводил бы в карантин весь день вместе с новыми заметками."""
    dest = os.path.join(cfg.raw, src)
    qdest = os.path.join(cfg.quarantine, src)
    allow = gate.load_allowlist(cfg.allowlist)
    existing = _list_files(dest)
    q_existing = _list_files(qdest)
    stats = {"files": 0, "added": 0, "changed": 0, "deleted": 0, "quarantined": 0, "bytes": 0}

    clean = {}
    flagged = {}
    cache = _load_gate_cache(cfg)
    for rel, (data, mtime) in entries.items():
        rel = safe_rel(rel)
        digest = gate.sha256(data)
        if digest in cache or pregated:
            hits = []
        else:
            hits = gate.scan_bytes(data)
            if not hits:
                cache.add(digest)
        if hits and digest not in allow:
            flagged[rel] = (data, mtime, hits)
        else:
            clean[rel] = (data, mtime)

    _save_gate_cache(cfg, cache)
    to_delete = existing - set(clean) if mirror else set()
    if mirror and not entries and existing:
        raise SourceError("источник пуст — отказ стирать raw/")
    if (len(existing) >= MASS_DELETE_MIN and len(to_delete) > MASS_DELETE_RATIO * len(existing)
            and os.environ.get("KB_ALLOW_MASS_DELETE") != "1"):
        raise SourceError(f"удаление {len(to_delete)} из {len(existing)} — сработала защита "
                          f"(KB_ALLOW_MASS_DELETE=1 для разового разрешения)")

    for rel, (data, mtime, hits) in flagged.items():
        target = os.path.join(qdest, rel)
        stats["quarantined"] += 1
        old = None
        if os.path.isfile(target):
            with open(target, "rb") as f:
                old = f.read()
        if old != data:
            _write_atomic(target, data, mtime)
            log_run(cfg, cmd="ingest", event="quarantine", run_id=run_id, source=src,
                    path=f"raw/{src}/{rel}", patterns=sorted({h[0] for h in hits}),
                    lines=sorted({h[1] for h in hits})[:20])
    # из карантина убираем только то, что теперь чисто и легло в raw/; пропавшее из источника
    # остаётся до разбора человеком (карантина нет в git — потерять его нельзя)
    for rel in q_existing & set(clean):
        os.remove(os.path.join(qdest, rel))

    for rel, (data, mtime) in clean.items():
        target = os.path.join(dest, rel)
        stats["files"] += 1
        if rel in existing:
            with open(target, "rb") as f:
                if f.read() == data:
                    continue
            stats["changed"] += 1
        else:
            stats["added"] += 1
        _write_atomic(target, data, mtime)
        stats["bytes"] += len(data)
    for rel in to_delete:
        os.remove(os.path.join(dest, rel))
        stats["deleted"] += 1
    _prune_empty(dest)
    _prune_empty(qdest)
    return stats


# ---------- оркестрация ----------

def _do_source(cfg, run_id, src, mode, collect, mirror=True):
    t = time.time()
    rec = {"cmd": "ingest", "run_id": run_id, "source": src, "mode": mode}
    try:
        entries = collect() if callable(collect) else collect
        stats = sync_source(cfg, src, entries, run_id, mirror=mirror)
        rec.update(stats, status="ok", error=None)
    except Exception as e:  # noqa: BLE001 — любой сбой источника не должен ронять прогон
        rec.update(status="error", error=f"{type(e).__name__}: {e}"[:300])
        _restore_source(cfg, src)
    rec["duration_s"] = round(time.time() - t, 3)
    log_run(cfg, **rec)
    return rec


def _flush_vps_inbox(cfg, run_id):
    """Заметки kb note, не перенесённые сразу (lock был занят), → raw/inbox/vps (под lock ingest).
    Очередь пуста — None (без строки в журнале: обычный час без заметок не шумит)."""
    import note
    pdir = note.pending_dir(cfg, "vps")
    if not os.path.isdir(pdir) or not any(f.endswith(".md") for f in os.listdir(pdir)):
        return None
    t = time.time()
    rec = {"cmd": "ingest", "run_id": run_id, "source": VPS_INBOX, "mode": "vps", "files": 0, "changed": 0,
           "deleted": 0, "bytes": 0}
    try:
        moved, quarantined = note.flush_pending(cfg, "vps")
        rec.update(added=moved, quarantined=quarantined, status="ok", error=None)
    except Exception as e:  # noqa: BLE001 — сбой входящих не роняет прогон, pending остаётся
        rec.update(added=0, quarantined=0, status="error", error=f"{type(e).__name__}: {e}"[:300])
    rec["duration_s"] = round(time.time() - t, 3)
    log_run(cfg, **rec)
    return rec


def _restore_source(cfg, src):
    """Откат частично записанного raw/<src> к HEAD: в git не должна уйти половина зеркала."""
    path = f"raw/{src}"
    git(cfg, "checkout", "HEAD", "--", path, check=False)
    git(cfg, "clean", "-fdq", "--", path, check=False)


def run_ingest(cfg, target="all"):
    """→ код выхода: 0 ок, 1 ошибка источника, 3 мало диска, 4 не взят lock."""
    run_id = new_run_id()
    t0 = time.time()
    free = free_gb(cfg.root)
    if free < cfg.min_free_gb:
        log_run(cfg, cmd="ingest", event="done", run_id=run_id, target=target, status="disk_low",
                error=f"свободно {free:.1f} ГБ < {cfg.min_free_gb:g} ГБ", duration_s=0)
        print(f"СТОП: свободно {free:.1f} ГБ < {cfg.min_free_gb:g} ГБ")
        return 3
    results = []
    try:
        with repo_lock(cfg):
            os.makedirs(cfg.raw, exist_ok=True)
            os.makedirs(cfg.quarantine, mode=0o700, exist_ok=True)
            os.chmod(cfg.quarantine, 0o700)
            if target in ("vps", "all"):
                results.append(_do_source(cfg, run_id, "claude-vps", "vps",
                                          lambda: collect_claude_vps(cfg)))
                results.append(_do_source(cfg, run_id, "codex-vps", "vps",
                                          lambda: collect_codex_vps(cfg)))
                rec = _flush_vps_inbox(cfg, run_id)
                if rec:
                    results.append(rec)
            sha = None
            commit_err = None
            try:
                changed = [r for r in results if r.get("status") == "ok"
                           and (r.get("added") or r.get("changed") or r.get("deleted"))]
                specs = [f"raw/{r['source']}" for r in changed]
                # заметки kb note, перенесённые в raw, но не закоммиченные (упал git) — подхватываем всегда
                if target in ("vps", "all") and os.path.isdir(os.path.join(cfg.raw, VPS_INBOX)) \
                        and f"raw/{VPS_INBOX}" not in specs:
                    specs.append(f"raw/{VPS_INBOX}")
                if specs:
                    summary = ", ".join(f"{r['source']} +{r['added']}~{r['changed']}-{r['deleted']}"
                                        for r in changed) or VPS_INBOX
                    sha = commit_paths(cfg, specs, f"ingest {target}: {summary}")
            except RuntimeError as e:
                commit_err = str(e)[:300]
    except LockTimeout:
        log_run(cfg, cmd="ingest", event="done", run_id=run_id, target=target, status="lock_timeout",
                error="state/repo.lock занят", duration_s=round(time.time() - t0, 2))
        print("СТОП: state/repo.lock занят")
        return 4
    bad = [r for r in results if r.get("source") in VPS_SOURCES and r.get("status") != "ok"]
    status = "error" if (bad or commit_err) else "ok"
    log_run(cfg, cmd="ingest", event="done", run_id=run_id, target=target, status=status,
            commit=sha,
            error=commit_err or (bad[0].get("error") if bad else None),
            duration_s=round(time.time() - t0, 2))
    for r in results:
        line = f"{r['source']:<11} {r['mode']:<8} {r['status']:<11}"
        if r["status"] == "ok":
            line += (f" файлов {r['files']}, +{r['added']} ~{r['changed']} -{r['deleted']},"
                     f" карантин {r['quarantined']}, {r['bytes']} Б")
        else:
            line += f" {r.get('error') or ''}"
        print(line)
    print(f"коммит: {sha or 'нет изменений'}" + (f" (ошибка: {commit_err})" if commit_err else ""))
    return 1 if status == "error" else 0


def _skip(cfg, run_id, src, mode, status, error, t0, details=None):
    rec = {"cmd": "ingest", "run_id": run_id, "source": src, "mode": mode, "status": status,
           "error": error, "duration_s": round(time.time() - t0, 3)}
    if details:
        rec["details"] = details
    log_run(cfg, **rec)
    return rec
