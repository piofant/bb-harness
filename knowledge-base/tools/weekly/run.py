"""`kb weekly` — недельный конвейер: К1 сводчик → (К5 раз в 4 недели) → К2 предложения → К3 обзор → доставка.

Падение шага не роняет конвейер: обзор пишется по тому, что есть, со строкой о сбое. Каждый шаг — строка
`cmd: weekly` в state/runs.jsonl. Крон никогда не пишет в about/, decisions.md, decisions/, projects/*.md:
его выход — только reviews/<дата>.md и proposals/<дата>-<проект>.md (коммит явным pathspec под state/repo.lock).

С ежедневным тихим прогоном (`kb daily`, daily.py) недельный делит state/weekly.lock (второй ждёт первого до
LOCK_WAIT), не гоняет К1, если сводчик уже отработал сегодня (UTC), и делает К2 только за хвост после последнего
К2 (курсор в runs.jsonl). Обзор при этом видит всю неделю: изменения карточек за окно и все ждущие ответа файлы
proposals/ недели, в том числе ежедневные.
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from kbcore import (LockTimeout, commit_paths, current_branch, log_run, new_run_id, parse_iso,  # noqa: E402
                    read_runs, repo_lock)

from . import deliver, proposals, review, skills_drift  # noqa: E402
from . import truth as T  # noqa: E402

K1_TIMEOUT = float(os.environ.get("KB_WEEKLY_K1_TIMEOUT", str(4 * 3600)))
LOCK_WAIT = float(os.environ.get("KB_WEEKLY_LOCK_WAIT", str(4 * 3600)))
# К1 считается свежим, если успешный боевой прогон закончился не раньше K1_FRESH секунд назад: суббота —
# ежедневный в 06:00, недельный в 07:00 (или после ожидания lock); ночной догон или ручной вечерний запуск — нет
K1_FRESH = float(os.environ.get("KB_K1_FRESH_S", str(3 * 3600)))
PIPELINES = ("daily", "weekly")
K2_COVERED = ("ok", "partial", "covered")      # статусы шага k2, после которых окно считается разобранным
K2_SEEN = "k2-seen.json"
FORBIDDEN_PREFIXES = ("about/", "decisions/", "projects/")
FORBIDDEN_FILES = ("decisions.md",)


class Step:
    def __init__(self, cfg, run_id, dry, cmd="weekly"):
        self.cfg, self.run_id, self.dry, self.cmd = cfg, run_id, dry, cmd
        self.failed = []

    def log(self, step, status, t0, **kw):
        if status not in ("ok", "skipped", "dry_run", "no_changes", "not_due", "covered", "deferred", "no_units"):
            self.failed.append(step)
        return log_run(self.cfg, cmd=self.cmd, run_id=self.run_id, step=step, status=status,
                       mode="dry-run" if self.dry else "run", duration_s=round(time.time() - t0, 2), **kw)


def default_window(cfg, now):
    """Окно крона: с конца прошлого боевого прогона (если он был за 14 дней), иначе последние 7 дней.

    Берётся прогон с наибольшим until_ts среди окон «по умолчанию» (ручной бэкфилл с --since/--until не сдвигает
    расписание). Перезапуск через < 2 суток после прогона повторяет его окно целиком, а не даёт пустой обзор."""
    recs = [r for r in read_runs(cfg) if r.get("cmd") == "weekly" and r.get("step") == "done"
            and r.get("mode") == "run" and r.get("auto_window") and r.get("until_ts")]
    since = None
    if recs:
        last = max(recs, key=lambda r: float(r["until_ts"]))
        u = float(last["until_ts"])
        if now - u < 2 * 86400 and last.get("since_ts") and float(last["since_ts"]) < now:
            since = float(last["since_ts"])
        elif 0 < now - u <= 14 * 86400:
            since = u
    if since is None or since >= now:
        since = now - 7 * 86400
    return T.Window(since, now)


def prev_truth(cfg, w):
    """Правда прошлого боевого прогона, чьё окно стыкуется с этим (для отсечки повторов на стыке)."""
    best = None
    for r in read_runs(cfg):
        if r.get("cmd") == "weekly" and r.get("step") == "done" and r.get("mode") == "run" and r.get("truth_file") \
                and r.get("until_ts") and float(r["until_ts"]) <= w.since_ts + 1:
            if best is None or float(r["until_ts"]) > float(best["until_ts"]):
                best = r
    if not best:
        return None
    try:
        with open(os.path.join(cfg.state, "weekly", os.path.basename(best["truth_file"])), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------- общее для kb daily и kb weekly ----------

def k1_fresh(cfg, now):
    """Последний успешный боевой К1 (daily или weekly), закончившийся за K1_FRESH до now; None — свежего нет."""
    best = None
    for r in read_runs(cfg):
        ts = parse_iso(r.get("ts"))
        if r.get("cmd") in PIPELINES and r.get("step") == "k1" and r.get("status") == "ok" \
                and r.get("mode") == "run" and ts is not None and now - K1_FRESH <= ts <= now + 60:
            best = r
    return best


def k1_week(cfg, w):
    """Сводчик за окно по журналу: боевые прогоны К1 обоих конвейеров (пропуски не считаются; отложенные по порогу
    — отдельным счётчиком, не сбой)."""
    n = failed = cards = deferred = idle = 0
    for r in read_runs(cfg):
        if r.get("cmd") in PIPELINES and r.get("step") == "k1" and r.get("mode") == "run" \
                and w.has_ts(parse_iso(r.get("ts"))) and r.get("status") != "skipped":
            if r.get("status") == "deferred":       # отложен по порогу
                deferred += 1
                continue
            if r.get("status") == "no_units":       # порог включён, сводить было нечего
                idle += 1
                continue
            n += 1
            if r.get("status") == "ok":
                cards += int(r.get("cards_touched") or 0)
            else:
                failed += 1
    return {"runs": n, "failed": failed, "cards_touched": cards, "deferred": deferred, "no_units": idle}


def k2_cursor(cfg):
    """Конец окна последнего разобранного боевого К2 (daily или weekly) → until_ts или None."""
    best = None
    for r in read_runs(cfg):
        if r.get("cmd") in PIPELINES and r.get("step") == "k2" and r.get("mode") == "run" \
                and r.get("status") in K2_COVERED and r.get("k2_until_ts"):
            best = max(best or 0.0, float(r["k2_until_ts"]))
    return best


def load_k2_seen(cfg, cursor):
    """Ключи событий, уже показанных К2 до курсора (цепочка окон: день стыка входит в оба окна)."""
    try:
        with open(os.path.join(cfg.state, "weekly", K2_SEEN), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return set()
    if cursor is None or abs(float(d.get("until_ts") or 0) - cursor) > 1:
        return set()
    return {tuple(k) for k in d.get("keys") or [] if isinstance(k, list)}


def save_k2_seen(cfg, w, prev, truth):
    """Старые ключи с даты начала окна и новые события окна; файл перезаписывается каждым боевым К2."""
    keys = {k for k in prev if k and k[0] == "ev" and len(k) == 4 and k[2] >= w.since_date}
    keys |= {k for k in T.seen_keys(truth) if k[0] == "ev"}
    d = os.path.join(cfg.state, "weekly")
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, K2_SEEN + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"until_ts": w.until_ts, "keys": sorted(list(k) for k in keys)}, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(d, K2_SEEN))


def k2_window(cfg, w, explicit):
    """Окно К2 недельного: хвост после последнего К2 (ежедневные уже разобрали остальное) или всё окно.
    → (окно или None — всё уже разобрано, seen для К2 или None — брать seen обзора)."""
    if explicit:
        return w, None
    cur = k2_cursor(cfg)
    if cur is None or cur <= w.since_ts:
        return w, None
    if cur >= w.until_ts - 1:
        return None, None
    return T.Window(cur, w.until_ts), load_k2_seen(cfg, cur)


def run_k2(cfg, st, w, k1, seen, out_root, llm_run, out, dry, drift=None, health_now=None, advance=True):
    """Шаг К2 → файлы предложений этого прогона. Боевой прогон по окну «до сейчас» двигает курсор К2 и цепочку
    seen; ручной бэкфилл (advance=False) — нет: его окно в прошлом или кончается в будущем (23:59 даты)."""
    t0 = time.time()
    advance = advance and not dry
    win = {"k2_since_ts": w.since_ts, "k2_until_ts": w.until_ts} if advance else \
        {"window_since_ts": w.since_ts, "window_until_ts": w.until_ts}
    props = []
    try:
        truth0 = T.collect(cfg, w, consolidate=k1, skills_drift=drift, health_now=health_now, seen=seen)
        res = proposals.make(cfg, truth0, os.path.join(out_root, "proposals"), run=llm_run,
                             dedup_dirs=[os.path.join(out_root, "proposals"), os.path.join(cfg.root, "proposals")])
        props = [r for r in res if r.get("file")]
        bad = [r for r in res if r.get("error")]
        status = "ok" if not bad else ("partial" if props else "error")
        if advance and status != "error":
            save_k2_seen(cfg, w, seen, truth0)
        st.log("k2", status, t0,
               projects={r["project"]: r.get("status") for r in res}, files=[r.get("file") for r in props],
               llm_calls=sum(r["llm_calls"] for r in res), tokens_in=sum(r["tokens_in"] for r in res),
               tokens_out=sum(r["tokens_out"] for r in res), duplicates=sum(r.get("duplicates") or 0 for r in res),
               error="; ".join(f"{r['project']}: {r['error']}" for r in bad)[:300] or None, **win)
        for r in res:
            out(f"К2 {r['project']}: {r.get('status')}, карточек {r['cards']}, предложений {r['n']}"
                + (f", повторов отброшено {r['duplicates']}" if r.get("duplicates") else "")
                + (f" → {r['file']}" if r.get("file") else "") + (f" ({r['error']})" if r.get("error") else ""))
    except Exception as e:  # noqa: BLE001
        st.log("k2", "error", t0, error=f"{type(e).__name__}: {str(e)[:200]}", **win)
        out(f"К2: сбой {type(e).__name__}: {e}")
    return props


def run_k1(cfg, dry, out, cmd=None, timeout=None, min_units=0):
    """Сводчик тем же путём, что ручной запуск: `kb consolidate` (worktree, гейты, слияние под lock).
    Свой run_id сводчику — через KB_RUN_ID (запись в журнале ищется по нему, а не по времени).
    min_units — порог единиц к LLM (`--min-units`): у ежедневного 3, у недельного 0 (сводит всё, что есть).
    Тайм-аут гасит всю группу процессов, включая дочерние `claude -p`."""
    timeout = timeout or K1_TIMEOUT
    t0 = time.time()
    rid = new_run_id()
    cmd = cmd or [sys.executable, os.path.join(TOOLS, "kb.py"), "consolidate"] + (["--dry-run"] if dry else []) + \
        (["--min-units", str(int(min_units))] if min_units else [])
    env = {**os.environ, "KB_RUN_ID": rid}
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cfg.state,
                             env=env, start_new_session=True)
        try:
            o, _ = p.communicate(timeout=timeout)
            rc, tail = p.returncode, (o or "").strip()[-600:]
        except subprocess.TimeoutExpired:
            _killpg(p)
            rc, tail = 124, f"тайм-аут {int(timeout)} с"
        except BaseException:   # Ctrl-C при ручном запуске: своя сессия сигнала не получит — гасим сами
            _killpg(p)
            raise
    except OSError as e:
        rc, tail = 127, str(e)
    rec = None
    for r in read_runs(cfg):
        if r.get("cmd") == "consolidate" and r.get("run_id") == rid:
            rec = r
    res = {"rc": rc, "duration_s": round(time.time() - t0, 2), "run_id": rid}
    if rec:
        res.update(status=rec.get("status"),
                   cards_touched=len(rec.get("cards_touched") or []), conflicts_new=rec.get("conflicts_new") or 0,
                   merged=rec.get("merged"), tokens_in=rec.get("tokens_in"), tokens_out=rec.get("tokens_out"),
                   error="; ".join(map(str, (rec.get("errors") or [])[:2]))[:300] or None)
        if rec.get("deferred"):
            res.update(units_to_llm=rec["deferred"].get("units"), min_units=rec["deferred"].get("min_units"),
                       reason=rec["deferred"].get("reason"))
        if dry and res["status"] == "ok":
            res["status"] = "dry_run"
    else:
        res.update(status="error" if rc else "ok", error=tail[-300:] if rc else None)
    if rc and res.get("status") in ("ok", "dry_run"):
        res["status"] = "error"
        res["error"] = res.get("error") or tail[-300:]
    out(f"К1 сводчик: {res['status']} (rc={rc}, {res['duration_s']} с)" +
        (f" — {res['reason']}" if res.get("reason") else ""))
    return res


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _killpg(p):
    """SIGTERM всей группе, 30 с на выход, затем SIGKILL тем, кто остался (лидер мог выйти раньше детей)."""
    pgid = p.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.time() + 30
    while time.time() < deadline and _group_alive(pgid):
        try:
            p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    if _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def assert_safe(paths):
    """Страховка контракта: конвейер пишет только в reviews/ и proposals/ (путь нормализуется до записи)."""
    for p in paths:
        n = os.path.normpath(p)
        if os.path.isabs(n) or n.startswith("..") or not n.startswith(("reviews/", "proposals/")) \
                or n.startswith(FORBIDDEN_PREFIXES) or n in FORBIDDEN_FILES:
            raise RuntimeError(f"конвейер не пишет в {p}")


def _weekly_lock(cfg, wait=0.0, out=None):
    """state/weekly.lock — общий для kb daily и kb weekly. Занят — ждём до wait секунд (в субботу недельный
    ждёт ежедневный, а не теряет неделю). → fd или None."""
    os.makedirs(cfg.state, exist_ok=True)
    fd = os.open(os.path.join(cfg.state, "weekly.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.time() + max(0.0, wait)
    said = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.time() >= deadline:
                os.close(fd)
                return None
            if out and not said:
                out(f"жду state/weekly.lock (идёт kb daily или kb weekly), до {int(wait)} с")
                said = True
            time.sleep(min(2.0, max(0.05, deadline - time.time())))


def main(cfg, a, out=print, llm_run=None, send=None):
    run_id = new_run_id()
    t_all = time.time()
    dry = bool(a.dry_run)
    st = Step(cfg, run_id, dry)
    os.environ["KB_HINT"] = "0"          # подсказки базы не нужны модели обзора
    os.environ.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    lock = _weekly_lock(cfg, LOCK_WAIT, out)
    if lock is None:
        out("СТОП: state/weekly.lock занят дольше KB_WEEKLY_LOCK_WAIT (kb daily или kb weekly)")
        log_run(cfg, cmd="weekly", run_id=run_id, step="done", status="busy", mode="dry-run" if dry else "run",
                duration_s=round(time.time() - t_all, 2))
        return 4
    try:
        return _main(cfg, a, out, llm_run, send or deliver.send, run_id, t_all, dry, st)
    finally:
        os.close(lock)


def _main(cfg, a, out, llm_run, send, run_id, t_all, dry, st):
    explicit = bool(a.since or a.until)
    try:
        if explicit:
            T.Window.from_dates(a.since, a.until)      # проверка дат до любых трат
    except ValueError as e:
        out(f"ошибка: {e}")
        return 2
    if not dry:
        br = current_branch(cfg)
        if br != "main":   # правда недели и коммит — только по main; иначе неделя потеряется из расписания
            out(f"СТОП: основной checkout не на main ({br or '?'})")
            log_run(cfg, cmd="weekly", run_id=run_id, step="done", status="error", mode="run",
                    error=f"ветка {br}", duration_s=0)
            return 2
    out(f"kb weekly {run_id}" + (" (dry-run: без отправки и коммита, файлы — state/weekly/dry/)" if dry else ""))
    # выход dry-run — не в живое дерево (его может зеркалить Obsidian): state/weekly/dry/
    out_root = os.path.join(cfg.state, "weekly", "dry") if dry else cfg.root

    # К1
    t0 = time.time()
    fresh = None if a.skip_consolidate else k1_fresh(cfg, time.time())
    if a.skip_consolidate:
        k1 = {"status": "skipped"}
        st.log("k1", "skipped", t0)
        out("К1 сводчик: пропущен (--skip-consolidate)")
    elif fresh:      # в субботу ежедневный прогон (06:00) уже свёл сырьё — второй раз подряд не гоняем
        k1 = {"status": "ok", "source": fresh.get("cmd"), "cards_touched": fresh.get("cards_touched"),
              "run_id": fresh.get("consolidate_run_id")}
        st.log("k1", "skipped", t0, reason=f"сводчик только что отработал ({fresh.get('cmd')} {fresh.get('run_id')}, "
                                           f"{fresh.get('ts')})", consolidate_run_id=fresh.get("consolidate_run_id"))
        out(f"К1 сводчик: только что отработал в kb {fresh.get('cmd')} ({fresh.get('ts')}) — не повторяю")
    else:
        try:
            k1 = run_k1(cfg, dry, out)
        except Exception as e:  # noqa: BLE001
            k1 = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}
        st.log("k1", k1["status"] if k1["status"] in ("ok", "dry_run") else k1["status"] or "error", t0,
               consolidate_run_id=k1.get("run_id"), tokens_in=k1.get("tokens_in"), tokens_out=k1.get("tokens_out"),
               cards_touched=k1.get("cards_touched"), error=k1.get("error"))

    # К5 (раз в 4 недели)
    t0 = time.time()
    drift = None
    if skills_drift.due(read_runs(cfg), time.time()) or a.skills_check:
        try:
            drift, stats = skills_drift.check(cfg, save=not dry)
            st.log("k5", "ok", t0, **{k: v for k, v in stats.items() if not isinstance(v, list)},
                   new=stats["new"][:20], removed=stats["removed"][:20])
            out(f"К5 скиллы: установлено {stats['installed']}, без маршрута {stats['missing_doc']}, "
                f"не установлены {stats['gone']}")
        except Exception as e:  # noqa: BLE001
            st.log("k5", "error", t0, error=f"{type(e).__name__}: {str(e)[:200]}")
    else:
        st.log("k5", "not_due", t0)

    # окно — после К1: карточки, которые сводчик только что слил в main, входят в эту неделю
    now = time.time()
    w = T.Window.from_dates(a.since, a.until) if explicit else default_window(cfg, now)
    out(f"окно {w.since_date}..{w.until_date}")
    seen = set() if explicit else T.seen_keys(prev_truth(cfg, w))
    health_now = os.path.join(cfg.state, "health.json") if w.until_ts >= now - 7200 else None
    k1 = {**k1, "week": k1_week(cfg, w)}

    # К2: только хвост после последнего К2 (ежедневные прогоны разобрали остальное); повторы режет proposals
    k2w, k2seen = k2_window(cfg, w, explicit)
    if k2w is None:
        st.log("k2", "covered", time.time())     # курсор не трогаем: его уже поставил ежедневный
        out("К2: окно уже разобрано ежедневными прогонами")
        props = []
    else:
        if k2w is not w:
            out(f"К2: хвост после ежедневных {k2w.since_date}..{k2w.until_date}")
        props = run_k2(cfg, st, k2w, k1, seen if k2seen is None else k2seen, out_root, llm_run, out, dry,
                       drift=drift, health_now=health_now, advance=not explicit)

    # К3: все ждущие ответа предложения окна — и ежедневные, и этого прогона
    t0 = time.time()
    review_rel = f"reviews/{w.until_date}.md"
    assert_safe([review_rel])
    week_props = []
    try:
        week_props = proposals.pending([os.path.join(out_root, "proposals"), os.path.join(cfg.root, "proposals")],
                                       w.since_date, w.until_date)
    except Exception as e:  # noqa: BLE001 — без списка недели обзор всё равно пишется (по файлам прогона)
        out(f"К3: список предложений недели не собран: {type(e).__name__}: {e}")
    listed = {f for p in week_props for f in p["files"]}
    week_props += [{"project": r["project"], "file": r["file"], "n": r["n"], "fallback": r["fallback"],
                    "preview": r.get("preview", [])} for r in props if r["file"] not in listed]
    try:
        truth = T.collect(cfg, w, consolidate=k1, skills_drift=drift, health_now=health_now, seen=seen,
                          proposals=week_props)
        tdir = os.path.join(cfg.state, "weekly")
        os.makedirs(tdir, exist_ok=True)
        with open(os.path.join(tdir, f"{w.until_date}-truth.json"), "w", encoding="utf-8") as f:
            json.dump(truth, f, ensure_ascii=False)
        md, meta = review.generate(truth, cwd=cfg.state, run=llm_run)
    except Exception as e:  # noqa: BLE001 — даже без правды обзор с честной строкой
        meta = {"status": "error", "errors": [f"{type(e).__name__}: {str(e)[:200]}"], "llm_calls": 0,
                "tokens_in": 0, "tokens_out": 0}
        md = (f"# Неделя {w.title()}\n\n## 4. Здоровье базы\n- Обзор не собран: {meta['errors'][0]} "
              f"([журнал прогонов](state/runs.jsonl)).\n")
    os.makedirs(os.path.join(out_root, "reviews"), exist_ok=True)
    with open(os.path.join(out_root, review_rel), "w", encoding="utf-8") as f:
        f.write(md)
    chk = meta.get("check") or {}
    st.log("k3", meta["status"], t0, file=review_rel, lines=review.count_lines(md), llm_calls=meta["llm_calls"],
           tokens_in=meta["tokens_in"], tokens_out=meta["tokens_out"],
           dropped=len(chk.get("dropped_no_link", [])) + len(chk.get("dropped_bad_link", [])),
           trimmed=chk.get("trimmed", 0), error="; ".join(meta.get("errors") or [])[:300] or None)
    out(f"К3 обзор: {meta['status']}, {review.count_lines(md)} строк → {os.path.join(out_root, review_rel)}")

    # доставка
    t0 = time.time()
    if dry:
        st.log("deliver", "dry_run", t0)
        out("доставка: dry-run — ничего не отправлено")
    elif os.environ.get("KB_WEEKLY_SEND", "1") == "0":
        st.log("deliver", "skipped", t0, error="KB_WEEKLY_SEND=0")
        out("доставка: выключена (KB_WEEKLY_SEND=0)")
    else:
        try:
            res = send(md)
            res = res if isinstance(res, dict) else {"messages": res, "channel": "test"}
            st.log("deliver", "ok", t0, messages=res["messages"], channel=res["channel"],
                   error=res.get("fallback_error"))
            out(f"доставка: отправлено сообщений {res['messages']} ({res['channel']})")
        except Exception as e:  # noqa: BLE001
            st.log("deliver", "error", t0, error=f"{type(e).__name__}: {str(e)[:200]}")
            out(f"доставка: сбой {type(e).__name__}: {e}")

    # коммит выхода конвейера
    t0 = time.time()
    # и свои файлы, и ждущие файлы недели (если ежедневный не смог закоммитить — подберём; без изменений — no-op)
    paths = list(dict.fromkeys([review_rel] + [r["file"] for r in props]
                               + [f for p in week_props for f in p.get("files", [p["file"]])
                                  if os.path.exists(os.path.join(cfg.root, f))]))
    assert_safe(paths)
    if dry:
        st.log("commit", "dry_run", t0, files=paths)
    else:
        try:
            br = current_branch(cfg)
            if br != "main":   # ветку проверили в начале; сменилась за время прогона
                st.log("commit", "skipped_branch", t0, error=f"ветка {br}", files=paths)
            else:
                with repo_lock(cfg):
                    sha = commit_paths(cfg, paths, f"weekly {w.until_date}: обзор и предложения ({run_id})")
                st.log("commit", "ok", t0, commit=sha, files=paths)
                out(f"коммит: {sha or 'без изменений'}")
        except LockTimeout:
            st.log("commit", "lock_timeout", t0, files=paths)
        except Exception as e:  # noqa: BLE001
            st.log("commit", "error", t0, error=f"{type(e).__name__}: {str(e)[:200]}", files=paths)

    status = "ok" if not st.failed else "partial"
    st.log("done", status, t_all, since=w.since_date, until=w.until_date, since_ts=w.since_ts, until_ts=w.until_ts,
           auto_window=not explicit, review=review_rel, truth_file=f"{w.until_date}-truth.json",
           proposals=[r["file"] for r in props], week_proposals=sorted(listed),
           failed_steps=list(st.failed))
    out(f"итог: {status}" + (f", сбои: {', '.join(st.failed)}" if st.failed else ""))
    return 0 if status == "ok" else 1
