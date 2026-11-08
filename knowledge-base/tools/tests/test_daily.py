"""Ежедневный тихий прогон `kb daily` и его стык с недельным: К1 → К2 за сутки, без обзора и без отправки;
failure-path (К1 упал, модель упала, нет изменений), курсор К2 и цепочка seen, общий lock, суббота (К1 не
повторяется), одно предложение — один раз, обзор видит все предложения недели."""
import argparse
import datetime as dt
import json
import os
import subprocess
import threading
import time

import pytest

import kbcore
from conftest import PY, TOOLS, runs, write
from test_weekly import FAKE, W, args, gitc, hist_card, week  # noqa: F401 — фикстура week
from weekly import daily, deliver, proposals
from weekly import run as wrun
from weekly import truth as T

NOW = dt.datetime(2026, 10, 24, 8, 0, tzinfo=dt.timezone.utc).timestamp()


def dargs(**kw):
    d = {"dry_run": False, "skip_consolidate": True}
    d.update(kw)
    return argparse.Namespace(**d)


def commit_at(root, iso_ts, msg):
    env = {**os.environ, "GIT_AUTHOR_DATE": iso_ts, "GIT_COMMITTER_DATE": iso_ts}
    subprocess.run(["git", "-C", str(root), *kbcore.GIT_ID, "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), *kbcore.GIT_ID, "commit", "-q", "-m", msg], check=True, env=env)


@pytest.fixture
def day(week, monkeypatch):
    """Неделя + карточка Продукта, слитая утром 24.10 (до прогона в 08:00); доставка падает при любом вызове."""
    cfg, p = week
    write(os.path.join(cfg.cards, "product/product-today.md"), hist_card(
        "product-today", [("2026-10-24", "пилот онбординга v3 подтверждён на тестерах")], desc="онбординг v3"))
    commit_at(p["root"], "2026-10-24T05:00:00+00:00", "consolidate 111111aaaaaa: карточки 1, конфликтов 0")
    # прошлый недельный (до выкатки ежедневного): курсора К2 ещё нет, первый ежедневный начинает с его конца
    kbcore.log_run(cfg, cmd="weekly", step="done", status="ok", mode="run", auto_window=True,
                   since_ts=NOW - 8 * 86400, until_ts=NOW - 86400)
    sent = []

    def boom(*a, **kw):
        sent.append(a)
        raise AssertionError("kb daily ничего не отправляет")
    for fn in ("send", "send_saved", "send_bot", "_mcp_call"):
        monkeypatch.setattr(deliver, fn, boom)
    monkeypatch.setattr(wrun, "LOCK_WAIT", 0.0)
    # main() ставит их прямо в os.environ; monkeypatch вернёт как было, иначе KB_HINT=0 протечёт в test_kb_hint
    monkeypatch.setenv("KB_HINT", os.environ.get("KB_HINT", "1"))
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "0"))
    yield cfg, p, sent


def steps(cfg, cmd="daily"):
    return [r for r in runs(cfg) if r.get("cmd") == cmd]


def by_step(cfg, cmd="daily"):
    return {r["step"]: r for r in steps(cfg, cmd)}


def product_items(cfg):
    d = os.path.join(cfg.root, "proposals")
    out = []
    for rel, path, fm, items in proposals.project_files([d], "product"):
        out += [x["propose"] for x in items]
    return out


# ---------- ежедневный прогон ----------

def test_daily_writes_proposals_commits_and_sends_nothing(day):
    cfg, p, sent = day
    rc = daily.main(cfg, dargs(), out=lambda *a: None, now=NOW)
    assert rc == 0 and sent == []
    st = by_step(cfg)
    assert set(st) == {"k1", "k2", "commit", "done"}                   # ни обзора, ни доставки
    assert st["k2"]["status"] == "ok" and st["k2"]["files"] == ["proposals/2026-10-24-product.md"]
    assert st["k2"]["k2_until_ts"] == NOW and st["k2"]["k2_since_ts"] == NOW - 86400
    assert all(r["mode"] == "run" for r in st.values())
    text = open(os.path.join(cfg.root, "proposals/2026-10-24-product.md")).read()
    assert "[[product-today]]" in text and "пилот онбординга v3" in text
    assert not os.path.exists(os.path.join(cfg.root, "reviews"))
    files = T._out(cfg.root, "show", "--name-only", "--format=", "HEAD").split()
    assert files == ["proposals/2026-10-24-product.md"]
    assert T._out(cfg.root, "log", "-1", "--format=%s").startswith("daily 2026-10-24")
    assert os.path.isfile(os.path.join(cfg.state, "weekly", wrun.K2_SEEN))
    calls = [json.loads(x) for x in open(p["tmp"] / "claude.jsonl")]
    assert [c["stage"] for c in calls] == ["k2"]                       # модель обзора не вызывалась


def test_daily_module_has_no_delivery():
    src = open(os.path.join(TOOLS, "weekly", "daily.py"), encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "import deliver" not in code and "deliver." not in code and "send(" not in code


def test_daily_k1_failure_still_proposes(day, monkeypatch):
    cfg, p, sent = day
    monkeypatch.setattr(wrun, "run_k1", lambda cfg, dry, out, **kw: {"status": "error", "error": "boom"})
    rc = daily.main(cfg, dargs(skip_consolidate=False), out=lambda *a: None, now=NOW)
    st = by_step(cfg)
    assert rc == 1 and st["k1"]["status"] == "error" and st["k1"]["error"] == "boom"
    assert st["k2"]["status"] == "ok" and st["commit"]["status"] == "ok"
    assert st["done"]["status"] == "partial" and st["done"]["failed_steps"] == ["k1"]
    assert sent == []


def test_daily_k1_exception_still_proposes(day, monkeypatch):
    cfg, p, sent = day

    def k1(cfg, dry, out, **kw):
        raise OSError("нет места")
    monkeypatch.setattr(wrun, "run_k1", k1)
    assert daily.main(cfg, dargs(skip_consolidate=False), out=lambda *a: None, now=NOW) == 1
    st = by_step(cfg)
    assert "OSError" in st["k1"]["error"] and st["k2"]["status"] == "ok"


@pytest.mark.parametrize("mode", ["error", "garbage"])
def test_daily_llm_failure_fallback_file(day, mode):
    cfg, p, sent = day
    os.environ["FAKE_WEEKLY_MODE"] = mode
    rc = daily.main(cfg, dargs(), out=lambda *a: None, now=NOW)
    st = by_step(cfg)
    assert rc == 1 and st["k2"]["status"] == "partial" and st["k2"]["error"]
    text = open(os.path.join(cfg.root, "proposals/2026-10-24-product.md")).read()
    assert "без неё" in text and "[[product-today]]" in text
    assert st["commit"]["status"] == "ok" and sent == []


def test_daily_no_changes_no_files(day):
    cfg, p, sent = day
    now = dt.datetime(2026, 10, 23, 8, 0, tzinfo=dt.timezone.utc).timestamp()   # 22.10 08:00 – 23.10 08:00
    head = T._out(cfg.root, "rev-parse", "HEAD").strip()
    rc = daily.main(cfg, dargs(), out=lambda *a: None, now=now)
    st = by_step(cfg)
    assert rc == 0 and st["k2"]["projects"] == {"product": "no_changes"} and st["k2"]["llm_calls"] == 0
    assert st["commit"]["status"] == "no_changes"
    assert not os.path.exists(os.path.join(cfg.root, "proposals"))
    assert T._out(cfg.root, "rev-parse", "HEAD").strip() == head
    assert not os.path.exists(p["tmp"] / "claude.jsonl")


def test_daily_dry_run_writes_to_state_and_keeps_cursor(day):
    cfg, p, sent = day
    head = T._out(cfg.root, "rev-parse", "HEAD").strip()
    assert daily.main(cfg, dargs(dry_run=True), out=lambda *a: None, now=NOW) == 0
    assert os.path.isfile(os.path.join(cfg.state, "weekly/dry/proposals/2026-10-24-product.md"))
    assert not os.path.exists(os.path.join(cfg.root, "proposals"))
    assert T._out(cfg.root, "rev-parse", "HEAD").strip() == head
    assert wrun.k2_cursor(cfg) is None                                # dry-run окно не сдвигает
    assert by_step(cfg)["commit"]["status"] == "dry_run" and sent == []


def test_daily_next_day_continues_from_cursor_without_repeats(day):
    cfg, p, sent = day
    assert daily.main(cfg, dargs(), out=lambda *a: None, now=NOW) == 0
    before = product_items(cfg)
    rc = daily.main(cfg, dargs(), out=lambda *a: None, now=NOW + 86400)
    k2 = [r for r in steps(cfg) if r["step"] == "k2"]
    assert rc == 0 and k2[-1]["k2_since_ts"] == NOW                  # с курсора, а не «минус сутки»
    assert k2[-1]["projects"] == {"product": "no_changes"}               # событие 24.10 уже показано (seen)
    assert product_items(cfg) == before


def test_daily_window_catch_up_rules(day):
    cfg, p, sent = day
    assert daily.daily_window(cfg, NOW)[0].since_ts == NOW - 86400          # первый: с конца прошлого недельного
    kbcore.log_run(cfg, cmd="daily", step="k2", status="ok", mode="run", k2_until_ts=NOW - 20 * 86400)
    w, seen = daily.daily_window(cfg, NOW)
    assert w.since_ts == NOW - daily.MAX_CATCHUP and seen == set()          # простой дольше 14 дней — 14 дней
    kbcore.log_run(cfg, cmd="daily", step="k2", status="ok", mode="run", k2_until_ts=NOW - 10 * 86400)
    assert daily.daily_window(cfg, NOW)[0].since_ts == NOW - 10 * 86400     # простой 10 дней — догоняем целиком
    kbcore.log_run(cfg, cmd="weekly", step="k2", status="ok", mode="run", k2_until_ts=NOW - 3 * 86400)
    assert daily.daily_window(cfg, NOW)[0].since_ts == NOW - 3 * 86400
    kbcore.log_run(cfg, cmd="daily", step="k2", status="error", mode="run", k2_until_ts=NOW - 3600)
    assert daily.daily_window(cfg, NOW)[0].since_ts == NOW - 3 * 86400   # упавший К2 окно не закрывает
    kbcore.log_run(cfg, cmd="daily", step="k2", status="ok", mode="dry-run", k2_until_ts=NOW - 3600)
    assert daily.daily_window(cfg, NOW)[0].since_ts == NOW - 3 * 86400   # dry-run тоже


def test_daily_first_run_without_any_history_takes_a_week(week):
    cfg, p = week
    assert daily.daily_window(cfg, NOW)[0].since_ts == NOW - daily.FIRST_RUN


def test_daily_skips_k1_if_ran_today(day, monkeypatch):
    cfg, p, sent = day
    called = []
    monkeypatch.setattr(wrun, "run_k1", lambda *a, **kw: called.append(1) or {"status": "ok"})
    kbcore.log_run(cfg, cmd="weekly", step="k1", status="ok", mode="run", cards_touched=3,
                   consolidate_run_id="abc")
    daily.main(cfg, dargs(skip_consolidate=False), out=lambda *a: None, now=time.time())
    assert called == [] and by_step(cfg)["k1"]["status"] == "skipped" and "weekly" in by_step(cfg)["k1"]["reason"]


def test_daily_busy_lock_logged(day, monkeypatch):
    cfg, p, sent = day
    fd = wrun._weekly_lock(cfg)
    try:
        assert daily.main(cfg, dargs(), out=lambda *a: None, now=NOW) == 4
    finally:
        os.close(fd)
    assert by_step(cfg)["done"]["status"] == "busy"


def test_lock_waits_for_other_pipeline(day, monkeypatch):
    """Недельный в субботу не теряет неделю, если ежедневный ещё держит lock: ждёт до LOCK_WAIT."""
    cfg, p, sent = day
    fd = wrun._weekly_lock(cfg)
    threading.Timer(0.6, lambda: os.close(fd)).start()
    t0 = time.time()
    got = wrun._weekly_lock(cfg, wait=10)
    assert got is not None and 0.5 < time.time() - t0 < 5
    os.close(got)
    fd2 = wrun._weekly_lock(cfg)
    try:
        assert wrun._weekly_lock(cfg, wait=0.3) is None
    finally:
        os.close(fd2)


def test_daily_refuses_non_main(day):
    cfg, p, sent = day
    subprocess.run(["git", "-C", cfg.root, "checkout", "-q", "-b", "other"], check=True)
    assert daily.main(cfg, dargs(), out=lambda *a: None, now=NOW) == 2
    assert not os.path.exists(os.path.join(cfg.root, "proposals"))


def test_daily_cli_dry_run(day):
    cfg, p, sent = day
    env = {"PATH": "/usr/bin:/bin", **p["env"], "KB_CLAUDE_BIN": FAKE, "KB_WEEKLY_LOCK_WAIT": "0"}
    r = subprocess.run([PY, os.path.join(TOOLS, "kb.py"), "daily", "--dry-run", "--skip-consolidate"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ничего не отправляется" in r.stdout and "отправки нет" in r.stdout
    assert [x["step"] for x in runs(cfg) if x.get("cmd") == "daily"] == ["k1", "k2", "commit", "done"]


# ---------- стык с недельным ----------

def test_weekly_skips_k1_after_daily_today(day, monkeypatch):
    cfg, p, sent = day
    kbcore.log_run(cfg, cmd="daily", run_id="d1", step="k1", status="ok", mode="run", cards_touched=4,
                   consolidate_run_id="c1")
    monkeypatch.setattr(wrun, "run_k1", lambda *a, **kw: pytest.fail("К1 второй раз за день"))
    got = []
    rc = wrun.main(cfg, args(skip_consolidate=False), out=lambda *a: None, send=lambda md: got.append(md) or 1)
    st = by_step(cfg, "weekly")
    assert rc == 0 and st["k1"]["status"] == "skipped" and "daily d1" in st["k1"]["reason"]
    truth = json.load(open(os.path.join(cfg.state, "weekly", "2026-10-24-truth.json")))
    c = [it for it in truth["items"] if it["kind"] == "consolidate"][0]
    assert c["text"].startswith("сводчик только что отработал в ежедневном прогоне (kb daily): карточек затронуто 4")
    assert c["significant"] and len(got) == 1


def test_weekly_reruns_k1_if_daily_k1_failed_today(day, monkeypatch):
    cfg, p, sent = day
    kbcore.log_run(cfg, cmd="daily", step="k1", status="error", mode="run")
    called = []
    monkeypatch.setattr(wrun, "run_k1", lambda cfg, dry, out: called.append(1) or {"status": "ok", "cards_touched": 0})
    wrun.main(cfg, args(skip_consolidate=False), out=lambda *a: None, send=lambda md: 1)
    assert called == [1]


def test_weekly_k2_no_duplicates_and_review_lists_week(day):
    """Ежедневные файлы недели + недельный К2 за то же окно: каждое предложение один раз, обзор видит все файлы."""
    cfg, p, sent = day
    t = T.collect(cfg, T.Window.from_dates("2026-10-17", "2026-10-20"))
    proposals.make(cfg, t, os.path.join(cfg.root, "proposals"))              # «ежедневный» 20.10
    assert daily.main(cfg, dargs(), out=lambda *a: None, now=NOW) == 0       # ежедневный 24.10
    daily_files = sorted(os.listdir(os.path.join(cfg.root, "proposals")))
    assert daily_files == ["2026-10-20-product.md", "2026-10-24-product.md"]
    before = product_items(cfg)
    got = []
    rc = wrun.main(cfg, args(), out=lambda *a: None, send=lambda md: got.append(md) or 1)   # 17..24.10 целиком
    items = product_items(cfg)
    assert len(items) == len(set(proposals._n(x) for x in items))           # ни одного повтора
    k2 = by_step(cfg, "weekly")["k2"]
    assert k2["duplicates"] >= 1 and items[:len(before)] == before
    truth = json.load(open(os.path.join(cfg.state, "weekly", "2026-10-24-truth.json")))
    pr = [it for it in truth["items"] if it["kind"] == "proposal"]
    assert len(pr) == 1 and "proposals/2026-10-20-product.md" in pr[0]["ref"] and \
        "proposals/2026-10-24-product.md" in pr[0]["ref"]
    assert pr[0]["text"].startswith(f"{len(items)} правок")
    assert rc == 0 and "proposals/2026-10-20-product.md" in got[0]
    done = by_step(cfg, "weekly")["done"]
    assert done["week_proposals"] == ["proposals/2026-10-20-product.md", "proposals/2026-10-24-product.md"]


def test_weekly_k2_window_is_tail_after_daily(day):
    cfg, p, sent = day
    w = T.Window(NOW - 7 * 86400, NOW + 3600)
    assert wrun.k2_window(cfg, w, explicit=False) == (w, None)                # ежедневных не было — всё окно
    assert daily.main(cfg, dargs(), out=lambda *a: None, now=NOW) == 0
    k2w, seen = wrun.k2_window(cfg, w, explicit=False)
    assert k2w.since_ts == NOW and k2w.until_ts == w.until_ts
    assert ("ev", "product-today", "2026-10-24", "пилот онбординга v3 подтверждён на тестерах") in seen
    assert wrun.k2_window(cfg, w, explicit=True) == (w, None)                 # ручной бэкфилл — всё окно
    assert wrun.k2_window(cfg, T.Window(NOW - 86400, NOW), explicit=False) == (None, None)   # всё разобрано


def test_weekly_review_after_daily_on_saturday_sees_whole_week(day):
    """Ежедневный 24.10 08:00 уже разобрал К2; недельный в тот же день — обзор по всей неделе с его файлом."""
    cfg, p, sent = day
    assert daily.main(cfg, dargs(), out=lambda *a: None, now=NOW) == 0
    got = []
    wrun.main(cfg, args(), out=lambda *a: None, send=lambda md: got.append(md) or 1)
    truth = json.load(open(os.path.join(cfg.state, "weekly", "2026-10-24-truth.json")))
    names = {it.get("name") for it in truth["items"] if it["kind"] == "card"}
    assert {"product-new", "infra-x", "product-today"} <= names                  # изменения карточек за 7 дней
    assert any(it["kind"] == "proposal" and "2026-10-24-product.md" in it["ref"] for it in truth["items"])


# ---------- К2: файлы ----------

def test_k2_same_day_file_is_appended_not_overwritten(week):
    cfg, p = week
    t = T.collect(cfg, T.Window.from_dates(*W))
    d = os.path.join(cfg.root, "proposals")
    other = {"subsection": "Следующий шаг", "was": "", "propose": "**Своё.** уже было утром",
             "basis": "product-old", "quote": "старая карточка подробно"}
    write(os.path.join(d, "2026-10-24-product.md"), proposals.render(t, "product", [other]))
    r = proposals.make(cfg, t, d)[0]
    text = open(os.path.join(d, "2026-10-24-product.md")).read()
    assert r["appended"] and "1. **Раздел «Следующий шаг».**" in text and "2. **Раздел «Что подтверждено».**" in text
    assert "Дополнено: окно 2026-10-17..2026-10-24" in text and "уже было утром" in text
    r2 = proposals.make(cfg, t, d)[0]                                          # тот же прогон ещё раз
    assert r2["status"] == "duplicates_only" and r2["n"] == 0 and r2["duplicates"] >= 1
    assert open(os.path.join(d, "2026-10-24-product.md")).read() == text


def test_k2_dedup_ignores_old_and_other_projects(week, tmp_path):
    d = str(tmp_path)
    t = {"window": {"since": "2026-08-01", "until": "2026-08-01"}, "projects": {"product": {"title": "Л", "file": "x"}}}
    x = {"subsection": "Следующий шаг", "was": "", "propose": "**Старое.** давно", "basis": "c",
         "quote": "цитата длинная очень"}
    write(os.path.join(d, "2026-08-01-product.md"), proposals.render(t, "product", [x]))
    t["window"]["until"] = "2026-10-20"
    write(os.path.join(d, "2026-10-20-other.md"), proposals.render(
        {**t, "projects": {"other": {"title": "О", "file": "y"}}}, "other", [x]))
    assert proposals.already_keys([d], "product", "2026-10-24") == set()       # старше 30 дней и чужой проект
    assert proposals.keys(x) <= proposals.already_keys([d], "other", "2026-10-24")


def test_pending_skips_answered_and_out_of_window(week, tmp_path):
    d = str(tmp_path)
    x = {"subsection": "Следующий шаг", "was": "", "propose": "**Пункт.** текст", "basis": "c",
         "quote": "цитата длинная очень"}
    for date in ("2026-10-16", "2026-10-18", "2026-10-22"):
        t = {"window": {"since": date, "until": date}, "projects": {"product": {"title": "Л", "file": "x"}}}
        write(os.path.join(d, f"{date}-product.md"), proposals.render(t, "product", [x]))
    path = os.path.join(d, "2026-10-22-product.md")
    write(path, open(path).read().replace("status: ждёт подтверждения", "status: принято"))
    pend = proposals.pending([d], "2026-10-17", "2026-10-24")
    assert [p["files"] for p in pend] == [["proposals/2026-10-18-product.md"]] and pend[0]["n"] == 1


def test_runs_info_daily_failure_significant_and_not_due_ok(week):
    cfg, p = week
    kbcore.log_run(cfg, cmd="daily", step="k1", status="gates_failed", mode="run")
    kbcore.log_run(cfg, cmd="weekly", step="k5", status="not_due", mode="run")
    kbcore.log_run(cfg, cmd="daily", step="commit", status="no_changes", mode="run")
    now = time.time()
    info = T.runs_info(cfg, T.Window(now - 3600, now + 60))
    fails = {(it["text"].split(":")[0]): it for it in info["items"]}
    assert set(fails) == {"daily/k1"} and fails["daily/k1"]["significant"]


def test_systemd_daily_units():
    d = os.path.join(TOOLS, "systemd")
    timer = open(os.path.join(d, "agent-knowledge-daily.timer")).read()
    assert "OnCalendar=*-*-* 06:00:00 UTC" in timer and "Persistent=true" in timer
    svc = open(os.path.join(d, "agent-knowledge-daily.service")).read()
    assert "ExecStart=/opt/agent-knowledge/tools/kb daily\n" in svc and "KB_HINT=0" in svc
    weekly = open(os.path.join(d, "agent-knowledge-weekly.timer")).read()
    assert "07:00:00 UTC" in weekly                                          # недельный — после ежедневного


def test_weekly_backfill_does_not_move_k2_cursor(day):
    """Ручной `kb weekly --since/--until` (окно до 23:59 даты или в прошлом) не сдвигает курсор ежедневного."""
    cfg, p, sent = day
    assert daily.main(cfg, dargs(), out=lambda *a: None, now=NOW) == 0
    seen_before = open(os.path.join(cfg.state, "weekly", wrun.K2_SEEN)).read()
    wrun.main(cfg, args(), out=lambda *a: None, send=lambda md: 1)
    assert wrun.k2_cursor(cfg) == NOW
    assert open(os.path.join(cfg.state, "weekly", wrun.K2_SEEN)).read() == seen_before
    k2 = by_step(cfg, "weekly")["k2"]
    assert "k2_until_ts" not in k2 and k2["window_until_ts"] == T.Window.from_dates(*W).until_ts


def test_k1_fresh_is_hours_not_calendar_day(day):
    """Ночной догон в 00:30 не отменяет К1 в 06:00/07:00; ручной вечерний запуск тоже гоняет сводчик."""
    cfg, p, sent = day
    now = time.time()
    kbcore.log_run(cfg, cmd="daily", step="k1", status="ok", mode="run", ts=kbcore.iso(now - 4 * 3600))
    assert wrun.k1_fresh(cfg, now) is None
    kbcore.log_run(cfg, cmd="daily", step="k1", status="ok", mode="dry-run")
    assert wrun.k1_fresh(cfg, now) is None
    kbcore.log_run(cfg, cmd="daily", step="k1", status="ok", mode="run", ts=kbcore.iso(now - 3600))
    assert wrun.k1_fresh(cfg, now)["cmd"] == "daily"


def test_saturday_auto_window_weekly_after_daily(day, monkeypatch):
    """Настоящий субботний путь (окно по умолчанию): ежедневный → недельный делает К2 только за хвост, двигает
    курсор, обзор видит всю неделю и файл ежедневного; следующий ежедневный продолжает с курсора недельного."""
    cfg, p, sent = day
    real0 = time.time()
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: NOW + 3600 + (real_time() - real0))   # часы — суббота 24.10, 09:00
    t_daily = time.time() - 600
    assert daily.main(cfg, dargs(), out=lambda *a: None, now=t_daily) == 0
    dfile = by_step(cfg)["k2"]["files"]
    assert dfile == [f"proposals/{T._date(t_daily)}-product.md"]
    got = []
    rc = wrun.main(cfg, args(since=None, until=None), out=lambda *a: None, send=lambda md: got.append(md) or 1)
    st = by_step(cfg, "weekly")
    assert rc == 0 and st["k2"]["status"] == "ok" and st["k2"]["k2_since_ts"] == t_daily
    assert st["k2"]["projects"] == {"product": "no_changes"} and st["k2"]["llm_calls"] == 0
    assert wrun.k2_cursor(cfg) == st["k2"]["k2_until_ts"] > t_daily
    truth = json.load(open(os.path.join(cfg.state, "weekly", f"{T._date(st['done']['until_ts'])}-truth.json")))
    assert truth["window"]["since_ts"] <= NOW - 86400                        # окно недели — с прошлого недельного
    assert "product-today" in {it.get("name") for it in truth["items"] if it["kind"] == "card"}
    assert any(it["kind"] == "proposal" and dfile[0] in it["ref"] for it in truth["items"])
    assert dfile[0] in got[0]
    nxt = st["k2"]["k2_until_ts"] + 3600
    daily.main(cfg, dargs(), out=lambda *a: None, now=nxt)
    assert [r for r in steps(cfg) if r["step"] == "k2"][-1]["k2_since_ts"] == st["k2"]["k2_until_ts"]


def test_k2_answered_same_day_file_gets_new_file(week):
    cfg, p = week
    t = T.collect(cfg, T.Window.from_dates(*W))
    d = os.path.join(cfg.root, "proposals")
    other = {"subsection": "Следующий шаг", "was": "", "propose": "**Своё.** уже было утром",
             "basis": "product-old", "quote": "старая карточка подробно"}
    path = write(os.path.join(d, "2026-10-24-product.md"), proposals.render(t, "product", [other]))
    answered = open(path).read().replace("status: ждёт подтверждения", "status: принято")
    write(path, answered)
    r = proposals.make(cfg, t, d)[0]
    assert r["file"] == "proposals/2026-10-24-product-2.md" and not r.get("appended")
    assert open(path).read() == answered
    pend = proposals.pending([d], "2026-10-17", "2026-10-24")
    assert [x["files"] for x in pend] == [["proposals/2026-10-24-product-2.md"]]
    assert proposals.keys(other) <= proposals.already_keys([d], "product", "2026-10-24")   # ответ — тоже не повторять


# ---------- порог К1: меньше 3 единиц — отложено (решение владельца «сводку можно от трёх») ----------

def test_daily_k1_threshold_3_and_deferred_is_not_failure(day, monkeypatch):
    cfg, p, sent = day
    got = []

    def k1(cfg, dry, out, **kw):
        got.append(kw)
        return {"status": "deferred", "run_id": "c9", "units_to_llm": 2, "min_units": 3,
                "reason": "к сводке 2 ед. < порога 3; ждут следующего прогона"}
    monkeypatch.setattr(wrun, "run_k1", k1)
    rc = daily.main(cfg, dargs(skip_consolidate=False), out=lambda *a: None, now=NOW)
    st = by_step(cfg)
    assert got[0]["min_units"] == 3                                               # порог по умолчанию у daily
    assert rc == 0 and st["k1"]["status"] == "deferred" and st["k1"]["units_to_llm"] == 2
    assert st["k1"]["min_units"] == 3 and "порога 3" in st["k1"]["reason"]
    assert st["k2"]["status"] == "ok" and st["done"]["status"] == "ok" and st["done"]["failed_steps"] == []
    daily.main(cfg, dargs(skip_consolidate=False, min_units=5), out=lambda *a: None, now=NOW + 86400)
    assert got[1]["min_units"] == 5                                               # параметр


def test_weekly_runs_k1_without_threshold_after_daily_deferred(day, monkeypatch):
    """Суббота: ежедневный К1 отложен по порогу → недельный сводит сам и без порога (даже 1–2 единицы)."""
    cfg, p, sent = day
    kbcore.log_run(cfg, cmd="daily", step="k1", status="deferred", mode="run", units_to_llm=2, min_units=3)
    got = []
    monkeypatch.setattr(wrun, "run_k1", lambda cfg, dry, out, **kw: got.append(kw) or
                        {"status": "ok", "cards_touched": 1})
    wrun.main(cfg, args(skip_consolidate=False), out=lambda *a: None, send=lambda md: 1)
    assert len(got) == 1 and not got[0].get("min_units")
    assert by_step(cfg, "weekly")["k1"]["status"] == "ok"


def test_run_k1_real_consolidate_min_units(day):
    """Настоящий `kb consolidate --min-units` подпроцессом: 1 единица при пороге 3 — deferred, без порога — план."""
    cfg, p, sent = day
    write(os.path.join(cfg.raw, "claude-vps/-home-app/project_one.md"), "CLAIM:infra:one: Порт 8001.\n")
    res = wrun.run_k1(cfg, True, out=lambda *a: None, min_units=3)
    assert res["rc"] == 0 and res["status"] == "deferred" and res["units_to_llm"] == 1 and res["min_units"] == 3
    res = wrun.run_k1(cfg, True, out=lambda *a: None)
    assert res["rc"] == 0 and res["status"] == "dry_run"


def test_deferred_not_a_failure_in_week_review(week):
    cfg, p = week
    kbcore.log_run(cfg, cmd="consolidate", status="deferred", deferred={"units": 2, "min_units": 3})
    kbcore.log_run(cfg, cmd="daily", step="k1", status="deferred", mode="run")
    info = T.runs_info(cfg, T.Window(time.time() - 3600, time.time() + 60))
    assert not [it for it in info["items"] if "deferred" in it["text"]]
    wk = wrun.k1_week(cfg, T.Window(time.time() - 3600, time.time() + 60))
    assert wk["deferred"] == 1 and wk["failed"] == 0 and wk["runs"] == 0 and wk["no_units"] == 0
    kbcore.log_run(cfg, cmd="daily", step="k1", status="no_units", mode="run")
    wk = wrun.k1_week(cfg, T.Window(time.time() - 3600, time.time() + 60))
    assert wk["deferred"] == 1 and wk["no_units"] == 1 and wk["failed"] == 0
    it = T.consolidate_item({"status": "deferred", "reason": "к сводке 2 ед. < порога 3"})
    assert "отложен" in it["text"] and "сбой" not in it["text"].replace("не сбой", "") and not it["significant"]


def test_daily_min_units_env_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("KB_DAILY_MIN_UNITS", "три")
    assert daily._min_units_env() == 3
    monkeypatch.setenv("KB_DAILY_MIN_UNITS", "5")
    assert daily._min_units_env() == 5
