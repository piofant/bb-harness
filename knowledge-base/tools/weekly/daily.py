"""`kb daily` — ежедневный тихий прогон: К1 сводчик по новому сырью → К2 предложения правок карточек проектов
за сутки → коммит proposals/. Без обзора и без какой-либо отправки наружу (этот модуль не импортирует deliver).

Тот же путь, что у недельного: сводчик — `kb consolidate` (worktree, гейты, слияние в main под lock), К2 —
proposals.make с дедупом по уже написанным файлам. Окно К2 — от конца последнего разобранного К2 (курсор в
runs.jsonl, daily или weekly) до сейчас, см. daily_window.

Суббота: ежедневный идёт и в субботу (06:00 UTC), недельный (07:00) делит с ним state/weekly.lock и ждёт его,
К1 второй раз подряд не гоняет (успешный К1 закончился меньше K1_FRESH = 3 ч назад), К2 делает только за хвост
после ежедневного. Если К1 только что отработал в недельном (догон после простоя), ежедневный его не повторяет.

Каждый шаг — строка `cmd: daily` в state/runs.jsonl: k1, k2, commit, done.
"""
import os
import sys
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from kbcore import (LockTimeout, commit_paths, current_branch, log_run, new_run_id, read_runs,  # noqa: E402
                    repo_lock)

from . import run as R  # noqa: E402
from . import truth as T  # noqa: E402

K1_TIMEOUT = float(os.environ.get("KB_DAILY_K1_TIMEOUT", str(3 * 3600)))
# решение владельца: «сводку можно от трёх» — меньше трёх единиц сырья к LLM ежедневный К1 откладывает (deferred),
# единицы ждут следующего дня; недельный сводит всё, что есть
def _min_units_env(default=3):
    try:
        return max(0, int(os.environ.get("KB_DAILY_MIN_UNITS", "") or default))
    except ValueError:      # кривое значение в юните не роняет ежедневный прогон при импорте
        return default


MIN_UNITS = _min_units_env()
MAX_CATCHUP = 14 * 86400        # как у недельного окна: дольше простой — начало окна прижимается к этому
FIRST_RUN = 7 * 86400           # истории нет совсем — неделя, чтобы недельный хвост не оставил дыры


def daily_window(cfg, now):
    """→ (окно, seen) К2. Начало — курсор последнего К2; его нет — конец последнего недельного окна (первый запуск
    после выкатки); ничего нет — неделя назад. Не раньше now − MAX_CATCHUP. Обычный день — сутки от курсора."""
    cur = R.k2_cursor(cfg)
    seen = R.load_k2_seen(cfg, cur) if cur is not None else set()
    if cur is None:
        last = [float(r["until_ts"]) for r in read_runs(cfg) if r.get("cmd") == "weekly" and r.get("step") == "done"
                and r.get("mode") == "run" and r.get("auto_window") and r.get("until_ts")]
        cur = max(last) if last else now - FIRST_RUN
    since = max(cur, now - MAX_CATCHUP)
    if since >= now:              # курсор «в будущем» (часы) — последние сутки
        since, seen = now - 86400, set()
    return T.Window(since, now), seen


def main(cfg, a, out=print, llm_run=None, now=None):
    run_id = new_run_id()
    t_all = time.time()
    dry = bool(getattr(a, "dry_run", False))
    st = R.Step(cfg, run_id, dry, cmd="daily")
    os.environ["KB_HINT"] = "0"
    os.environ.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    lock = R._weekly_lock(cfg, R.LOCK_WAIT, out)
    if lock is None:
        out("СТОП: state/weekly.lock занят дольше KB_WEEKLY_LOCK_WAIT (kb daily или kb weekly)")
        log_run(cfg, cmd="daily", run_id=run_id, step="done", status="busy", mode="dry-run" if dry else "run",
                duration_s=round(time.time() - t_all, 2))
        return 4
    try:
        return _main(cfg, a, out, llm_run, run_id, t_all, dry, st, now)
    finally:
        os.close(lock)


def _main(cfg, a, out, llm_run, run_id, t_all, dry, st, now):
    if not dry:
        br = current_branch(cfg)
        if br != "main":
            out(f"СТОП: основной checkout не на main ({br or '?'})")
            log_run(cfg, cmd="daily", run_id=run_id, step="done", status="error", mode="run",
                    error=f"ветка {br}", duration_s=0)
            return 2
    out(f"kb daily {run_id}" + (" (dry-run: без коммита, файлы — state/weekly/dry/)" if dry else "")
        + " — тихий прогон, ничего не отправляется")
    out_root = os.path.join(cfg.state, "weekly", "dry") if dry else cfg.root

    # К1
    t0 = time.time()
    fresh = None if a.skip_consolidate else R.k1_fresh(cfg, now or time.time())
    if a.skip_consolidate:
        k1 = {"status": "skipped"}
        st.log("k1", "skipped", t0)
        out("К1 сводчик: пропущен (--skip-consolidate)")
    elif fresh:
        k1 = {"status": "ok", "cards_touched": fresh.get("cards_touched"), "run_id": fresh.get("consolidate_run_id")}
        st.log("k1", "skipped", t0, reason=f"сводчик только что отработал ({fresh.get('cmd')} {fresh.get('run_id')}, "
                                           f"{fresh.get('ts')})", consolidate_run_id=fresh.get("consolidate_run_id"))
        out(f"К1 сводчик: только что отработал в kb {fresh.get('cmd')} ({fresh.get('ts')}) — не повторяю")
    else:
        try:
            mu = getattr(a, "min_units", None)
            k1 = R.run_k1(cfg, dry, out, timeout=K1_TIMEOUT, min_units=MIN_UNITS if mu is None else mu)
        except Exception as e:  # noqa: BLE001 — К2 идёт по тому, что уже в базе
            k1 = {"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}
        st.log("k1", k1.get("status") or "error", t0, consolidate_run_id=k1.get("run_id"),
               tokens_in=k1.get("tokens_in"), tokens_out=k1.get("tokens_out"), cards_touched=k1.get("cards_touched"),
               error=k1.get("error"), units_to_llm=k1.get("units_to_llm"), min_units=k1.get("min_units"),
               reason=k1.get("reason"))

    # К2 — окно после К1: только что слитые карточки входят в эти сутки
    now = now or time.time()
    w, seen = daily_window(cfg, now)
    out(f"окно К2 {T._iso(w.since_ts)}..{T._iso(w.until_ts)}")
    health_now = os.path.join(cfg.state, "health.json")
    props = R.run_k2(cfg, st, w, k1, seen, out_root, llm_run, out, dry, health_now=health_now)

    # коммит только proposals/
    t0 = time.time()
    paths = [r["file"] for r in props]
    R.assert_safe(paths)
    if not paths:
        st.log("commit", "no_changes", t0)
    elif dry:
        st.log("commit", "dry_run", t0, files=paths)
    else:
        try:
            br = current_branch(cfg)
            if br != "main":
                st.log("commit", "skipped_branch", t0, error=f"ветка {br}", files=paths)
            else:
                with repo_lock(cfg):
                    sha = commit_paths(cfg, paths,
                                       f"daily {w.until_date}: предложения по карточкам проектов ({run_id})")
                st.log("commit", "ok", t0, commit=sha, files=paths)
                out(f"коммит: {sha or 'без изменений'}")
        except LockTimeout:
            st.log("commit", "lock_timeout", t0, files=paths)
        except Exception as e:  # noqa: BLE001
            st.log("commit", "error", t0, error=f"{type(e).__name__}: {str(e)[:200]}", files=paths)

    status = "ok" if not st.failed else "partial"
    st.log("done", status, t_all, window_since_ts=w.since_ts, window_until_ts=w.until_ts, proposals=paths,
           failed_steps=list(st.failed))
    out(f"итог: {status}" + (f", сбои: {', '.join(st.failed)}" if st.failed else "") + "; отправки нет")
    return 0 if status == "ok" else 1
