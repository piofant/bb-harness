"""Недельный конвейер (К1–К3, К5), доставка: правда недели, контракт обзора,
failure-path (К1 упал, модель упала или вернула мусор, нет изменений), dry-run ничего не шлёт."""
import argparse
import json
import os
import re
import subprocess
import time

import pytest

import kbcore
from conftest import PY, TOOLS, card_text, runs, write
from weekly import deliver, proposals, review, skills_drift
from weekly import run as wrun
from weekly import truth as T

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_weekly_claude.py")
W = ("2026-10-17", "2026-10-24")

PROJECT = """---
name: product
type: project
updated: 2026-10-10
source: сводка карточек cards/product
---
# Продукт

## 1. Что это и зачем
- Ассистент (2026-10-01, [[product-old]]).

## 3. Текущее состояние

### Что подтверждено
- Старый факт про онбординг (2026-10-01, [[product-old]]).

### Следующий шаг
1. Замерить качество (2026-10-01, [[product-old]]).

## 7. Открытые вопросы к владельцу
1. Катим ли в ноябре?
"""


def gitc(root, date, msg):
    env = {**os.environ, "GIT_AUTHOR_DATE": f"{date}T12:00:00+00:00", "GIT_COMMITTER_DATE": f"{date}T12:00:00+00:00"}
    subprocess.run(["git", "-C", str(root), *kbcore.GIT_ID, "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), *kbcore.GIT_ID, "commit", "-q", "--allow-empty", "-m", msg],
                   check=True, env=env)


def hist_card(name, hist, desc="описание", type_="fact"):
    return card_text(name, type_, desc=desc, body=f"**Суть.** {desc} подробно.\n\n## История\n" +
                     "".join(f"- {d} — {t}\n" for d, t in hist))


@pytest.fixture
def week(kb):
    """База с неделей 17–24.10: событие карточки, правка, решение, спор, сбой прогона, здоровье."""
    cfg, p = kb
    root = p["root"]
    env = {**os.environ, "GIT_COMMITTER_DATE": "2026-09-01T00:00:00+00:00"}
    subprocess.run(["git", "-C", str(root), *kbcore.GIT_ID, "commit", "-q", "--amend", "--no-edit",
                    "--date", "2026-09-01T00:00:00+00:00"], check=True, env=env)
    write(os.path.join(root, "projects/product.md"), PROJECT)
    write(os.path.join(cfg.cards, "product/product-old.md"),
          hist_card("product-old", [("2026-09-15", "найдено при разборе")], desc="старая карточка"))
    write(os.path.join(cfg.cards, "infra/infra-x.md"), hist_card("infra-x", [("2026-10-01", "найдено")], desc="порт"))
    write(os.path.join(root, "decisions.md"), "# Решения\n\n- **2026-10-01 · Старое решение.** Почему: так.\n")
    write(os.path.join(root, "skills.md"), "# Скиллы\n- `white-pdf` — пдф.\n- `remote-only-skill` — только на другой машине.\n")
    write(os.path.join(cfg.state, "health.json"), json.dumps({"status": "yellow", "problems": [
        {"level": "yellow", "what": "карантин: 1 файл"}]}))
    gitc(root, "2026-10-01", "база до окна")
    write(os.path.join(cfg.cards, "product/product-new.md"), hist_card(
        "product-new", [("2026-10-19", "выкачен новый онбординг на Stable"),
                      ("2026-10-20", "add: сводчик (прогон abcdef123456)")], desc="новый онбординг"))
    gitc(root, "2026-10-20", "consolidate abcdef123456: карточки 1, конфликтов 0")
    write(os.path.join(cfg.cards, "infra/infra-x.md"), hist_card(
        "infra-x", [("2026-10-01", "найдено"), ("2026-10-21", "update: новый порт 443")], desc="порт"))
    with open(os.path.join(root, "CONFLICTS.md"), "a", encoding="utf-8") as f:
        f.write("- [ ] 2026-10-21 product-new — А против Б (raw/x.md@2026-10-21)\n")
    gitc(root, "2026-10-21", "consolidate fedcba654321: карточки 1, конфликтов 1")
    with open(os.path.join(root, "decisions.md"), "a", encoding="utf-8") as f:
        f.write("- **2026-10-22 · Новое решение владельца.** Почему: проверено.\n")
    gitc(root, "2026-10-22", "decisions: новое решение")
    with open(cfg.runs, "a", encoding="utf-8") as f:
        for r in [{"ts": "2026-10-20T10:00:00Z", "cmd": "ingest", "event": "done", "status": "ok", "mac_status": "ok"},
                  {"ts": "2026-10-21T10:00:00Z", "cmd": "consolidate", "status": "error", "errors": ["boom"]},
                  {"ts": "2026-10-10T10:00:00Z", "cmd": "consolidate", "status": "error", "errors": ["вне окна"]}]:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    monkey_env = {"KB_CLAUDE_BIN": FAKE, "FAKE_CLAUDE_LOG": str(p["tmp"] / "claude.jsonl"),
                  "KB_SKILL_DIRS": str(p["tmp"] / "skills")}
    for k, v in monkey_env.items():
        os.environ[k] = v
    os.makedirs(p["tmp"] / "skills/white-pdf", exist_ok=True)
    write(str(p["tmp"] / "skills/white-pdf/SKILL.md"), "x")
    write(str(p["tmp"] / "skills/new-skill/SKILL.md"), "x")
    write(str(p["tmp"] / "skills/personal-diary-v2/SKILL.md"), "x")
    yield cfg, p
    for k in monkey_env:
        os.environ.pop(k, None)
    os.environ.pop("FAKE_WEEKLY_MODE", None)


def collect(cfg, **kw):
    return T.collect(cfg, T.Window.from_dates(*W), **kw)


def args(**kw):
    d = {"dry_run": False, "since": W[0], "until": W[1], "skip_consolidate": True, "skills_check": False}
    d.update(kw)
    return argparse.Namespace(**d)


def by_kind(truth, kind):
    return [it for it in truth["items"] if it["kind"] == kind]


# ---------- правда недели ----------

def test_truth_collects_week(week):
    cfg, p = week
    t = collect(cfg)
    cards = {it["name"]: it for it in by_kind(t, "card")}
    assert set(cards) == {"product-new", "infra-x"}            # product-old: ни правки, ни события в окне
    assert cards["product-new"]["change"] == "added" and cards["product-new"]["project"] == "product"
    assert cards["product-new"]["events"] == [{"date": "2026-10-19", "text": "выкачен новый онбординг на Stable"}]
    assert cards["infra-x"]["change"] == "modified" and cards["infra-x"]["project"] is None
    assert cards["infra-x"]["changes"][0]["text"].startswith("update: новый порт")
    assert t["base"]["cards"] == {"added": 1, "modified": 1, "deleted": 0}
    assert [it["text"] for it in by_kind(t, "decision")] == ["Новое решение владельца. Почему: проверено."]
    assert [it["name"] for it in by_kind(t, "conflict_new")] == ["product-new"]
    assert t["conflicts"]["open_total"] == 1
    fails = by_kind(t, "run_failure")
    assert len(fails) == 1 and "boom" in fails[0]["text"]     # сбой вне окна не попал
    assert t["health"]["status"] == "yellow"
    assert all(it["ref"] for it in t["items"])
    assert set(t["significant"]) >= {it["id"] for it in by_kind(t, "decision")}
    assert t["projects"]["product"]["open_questions"] == ["Катим ли в ноябре?"]
    assert by_kind(t, "commit") and all(it["ref"].startswith("коммит ") for it in by_kind(t, "commit"))
    json.dumps(t, ensure_ascii=False)


def test_truth_deterministic(week):
    cfg, p = week
    assert json.dumps(collect(cfg), sort_keys=True) == json.dumps(collect(cfg), sort_keys=True)


def test_truth_empty_window(week):
    cfg, p = week
    t = T.collect(cfg, T.Window.from_dates("2026-09-05", "2026-09-10"))
    assert not by_kind(t, "card") and not by_kind(t, "decision") and t["base"]["cards"]["added"] == 0


def test_truth_synthetic_window_from_fixed_snapshot(week):
    """Эвал прошлых недель: карточки с фиксированного среза, события — по датам «## Истории»."""
    cfg, p = week
    head = T._out(cfg.root, "rev-parse", "HEAD").strip()
    t = T.collect(cfg, T.Window.from_dates("2026-09-12", "2026-09-18"), cards_ref=head)
    cards = by_kind(t, "card")
    assert [it["name"] for it in cards] == ["product-old"] and cards[0]["change"] is None
    assert cards[0]["events"] == [{"date": "2026-09-15", "text": "найдено при разборе"}]
    assert T.collect(cfg, T.Window.from_dates("2026-09-12", "2026-09-18"))["items"][0]["kind"] != "card"


# ---------- К3: обзор ----------

def test_review_ok_contract(week):
    cfg, p = week
    t = collect(cfg)
    md, meta = review.generate(t, cwd=cfg.state)
    assert meta["status"] == "ok", meta
    assert md.startswith("# Неделя 17.10–24.10\n")
    for s in review.SECTIONS:
        assert s in md
    assert "### Продукт — [карточка](projects/product.md)" in md
    assert review.count_lines(md) <= 40
    assert review.items_without_link(md) == []
    calls = [json.loads(x) for x in open(p["tmp"] / "claude.jsonl")]
    assert calls[-1]["env_kb_hint"] in (None, "0")
    argv = calls[-1]["argv"]
    assert "--no-session-persistence" in argv and argv[argv.index("--tools") + 1] == ""


def test_review_drops_unlinked_and_invented_links(week):
    cfg, p = week
    os.environ["FAKE_WEEKLY_MODE"] = "nolinks"
    md, meta = review.generate(collect(cfg), cwd=cfg.state)
    assert meta["status"] == "ok"
    assert "без всякой ссылки" not in md and "no-such-card" not in md and "deadbee" not in md
    chk = meta["check"]
    assert len(chk["dropped_no_link"]) == 1 and len(chk["dropped_bad_link"]) == 2
    assert review.items_without_link(md) == []


def test_review_trimmed_to_40_lines(week):
    cfg, p = week
    os.environ["FAKE_WEEKLY_MODE"] = "long"
    md, meta = review.generate(collect(cfg), cwd=cfg.state)
    assert meta["status"] == "ok" and meta["check"]["trimmed"] > 0
    assert review.count_lines(md) <= 40
    assert "## 4. Здоровье базы" in md and "HEALTH.md" in md


@pytest.mark.parametrize("mode", ["garbage", "error"])
def test_review_llm_failure_falls_back(week, mode):
    cfg, p = week
    os.environ["FAKE_WEEKLY_MODE"] = mode
    md, meta = review.generate(collect(cfg), cwd=cfg.state)
    assert meta["status"] == "fallback" and meta["errors"]
    assert "Обзор собран без модели" in md
    assert review.count_lines(md) <= 40 and review.items_without_link(md) == []
    assert "### Продукт — [карточка](projects/product.md)" in md and "[[product-new]]" in md


def test_finalize_drops_paragraphs_fake_headings_and_extra_sections(week):
    cfg, p = week
    t = collect(cfg)
    md = "\n".join(["# Неделя x", "## Коротко", "- выдумка в лишнем разделе ([[product-new]])",
                     "## 1. Что продвинулось", "### Продукт — [карточка](projects/product.md)",
                     "* звёздочка без ссылки", "абзац без ссылки", "- онбординг ([[product-new]])",
                     "### Выдуманный — [карточка](projects/fake.md)", "- пункт выдуманного ([[product-new]])",
                     "## 2. Ждёт решения владельца", "1. спор ([CONFLICTS.md](CONFLICTS.md))",
                     "## 3. Ждёт подтверждения: предложенные правки карточек проектов", "- предложений нет",
                     "## 4. Здоровье базы", "- yellow ([HEALTH.md](HEALTH.md))"])
    out, rep = review.finalize(md, t)
    assert "Коротко" not in out and "лишнем разделе" not in out and "Выдуманный" not in out
    assert "звёздочка" not in out and "абзац без ссылки" not in out and "пункт выдуманного" not in out
    assert out.count("### Продукт") == 1 and "онбординг" in out
    sec4 = out.split("## 4. Здоровье базы")[1]
    assert "HEALTH.md" in sec4 and "CONFLICTS.md" in sec4


def test_finalize_missing_project_gets_truth_items_not_no_changes(week):
    cfg, p = week
    t = collect(cfg)
    md = "\n".join(["# Неделя x", "## 1. Что продвинулось", "## 2. Ждёт решения владельца", "- нет",
                     "## 3. Ждёт подтверждения: предложенные правки карточек проектов", "- предложений нет",
                     "## 4. Здоровье базы", "- yellow ([HEALTH.md](HEALTH.md))"])
    out, rep = review.finalize(md, t)
    product = out.split("### Продукт")[1].split("##")[0]
    assert "[[product-new]]" in product and "без изменений" not in product and "proposals/" not in product


def test_finalize_joins_wrapped_items_and_drops_stray_h1(week):
    cfg, p = week
    md = "\n".join(["# Неделя x", "## 1. Что продвинулось", "### Продукт — [карточка](projects/product.md)",
                     "- онбординг выкачен", "  на Stable ([[product-new]])", "# Посторонний заголовок",
                     "## 2. Ждёт решения владельца", "- нет",
                     "## 3. Ждёт подтверждения: предложенные правки карточек проектов", "- предложений нет",
                     "## 4. Здоровье базы", "- yellow ([HEALTH.md](HEALTH.md))"])
    out, rep = review.finalize(md, collect(cfg))
    assert "- онбординг выкачен на Stable ([[product-new]])" in out and "Посторонний" not in out


def test_window_boundary_day_kept_but_repeats_dropped(week):
    """День стыка входит в оба окна (не теряем позднее событие), а уже показанное отсекается по ключу."""
    cfg, p = week
    w1 = T.Window.from_dates("2026-10-12", "2026-10-19")
    w2 = T.Window(w1.until_ts - 3600, w1.until_ts + 6 * 86400)       # начало не в полночь, день 19.10 общий
    assert w2.has_date("2026-10-19")
    head = T._out(cfg.root, "rev-parse", "HEAD").strip()
    t1 = T.collect(cfg, w1, cards_ref=head)
    assert any(e["date"] == "2026-10-19" for it in by_kind(t1, "card") for e in it["events"])
    assert any(e["date"] == "2026-10-19" for it in by_kind(T.collect(cfg, w2, cards_ref=head), "card")
               for e in it["events"])
    t2 = T.collect(cfg, w2, cards_ref=head, seen=T.seen_keys(t1))
    assert not any(e["date"] == "2026-10-19" for it in by_kind(t2, "card") for e in it["events"])
    w = T.Window.from_dates(None, "2026-10-24")
    assert w.since_date == "2026-10-18" and w.has_date("2026-10-18") and not w.has_date("2026-10-17")


def test_weekly_second_run_does_not_repeat_boundary_events(week):
    cfg, p = week
    a1 = args(since=None, until=None)
    now = time.time()
    kbcore.log_run(cfg, cmd="weekly", step="done", status="ok", mode="run", auto_window=True,
                   since_ts=now - 10 * 86400, until_ts=now - 3 * 86400, truth_file="prev-truth.json")
    prev = {"items": [{"kind": "decision", "text": "Новое решение владельца. Почему: проверено."}]}
    os.makedirs(os.path.join(cfg.state, "weekly"), exist_ok=True)
    json.dump(prev, open(os.path.join(cfg.state, "weekly", "prev-truth.json"), "w"))
    assert wrun.prev_truth(cfg, wrun.default_window(cfg, now)) == prev
    assert ("decision", "", "Новое решение владельца. Почему: проверено.") in T.seen_keys(prev)


def test_killpg_kills_children_ignoring_sigterm():
    import signal as sg
    pp = subprocess.Popen(["bash", "-c", "(trap '' TERM; exec sleep 300) & sleep 300"], start_new_session=True)
    time.sleep(0.5)
    wrun._killpg(pp)
    time.sleep(0.3)
    left = subprocess.run(["pgrep", "-g", str(pp.pid)], capture_output=True, text=True).stdout.split()
    assert left == [], left
    assert sg.SIGKILL


def test_read_blobs_path_with_space_and_missing(week):
    cfg, p = week
    write(os.path.join(cfg.cards, "product/с пробелом.md"), card_text("probel"))
    gitc(p["root"], "2026-10-23", "карточка с пробелом")
    head = T._out(cfg.root, "rev-parse", "HEAD").strip()
    got = T.read_blobs(cfg.root, head, ["cards/product/нет такого.md", "cards/product/с пробелом.md"])
    assert list(got) == ["cards/product/с пробелом.md"] and "name: probel" in got["cards/product/с пробелом.md"]


def test_finalize_rejects_broken_structure(week):
    cfg, p = week
    md, rep = review.finalize("просто текст без структуры", collect(cfg))
    assert md is None and "заголовка" in rep["error"]


def test_fallback_fits_on_huge_week(week):
    cfg, p = week
    t = collect(cfg)
    extra = [{"id": f"X{i}", "kind": "card", "ref": f"[[product-new]]", "name": "product-new", "project": None,
              "description": "очень длинное описание " * 10, "events": [], "significant": True, "score": 9,
              "change": "added"} for i in range(50)]
    t["items"] += extra
    t["items"] += [{"id": f"C{i}", "kind": "conflict_new", "ref": "[CONFLICTS.md](CONFLICTS.md)", "name": f"c{i}",
                    "text": "спор", "significant": True, "score": 6} for i in range(20)]
    md = review.fallback(t, "тест")
    assert review.count_lines(md) <= 40 and review.items_without_link(md) == []


# ---------- К2: предложения ----------

def test_k2_writes_valid_proposal_and_never_touches_projects(week):
    cfg, p = week
    before = open(os.path.join(cfg.root, "projects/product.md"), "rb").read()
    res = proposals.make(cfg, collect(cfg), os.path.join(cfg.root, "proposals"))
    r = {x["project"]: x for x in res}["product"]
    assert r["status"] == "ok" and r["n"] == 1 and r["file"] == "proposals/2026-10-24-product.md"
    assert any("цитаты нет" in x for x in r["rejected"])
    text = open(os.path.join(cfg.root, r["file"])).read()
    assert "[[product-new]]" in text and "выкачен новый онбординг" in text and "выдумка" not in text
    assert "Было: нет (новая строка)" in text
    assert open(os.path.join(cfg.root, "projects/product.md"), "rb").read() == before
    st = subprocess.run(["git", "-C", cfg.root, "status", "--porcelain", "--", "projects"], capture_output=True,
                        text=True).stdout
    assert st == ""


def test_k2_no_changes_no_file(week):
    cfg, p = week
    t = T.collect(cfg, T.Window.from_dates("2026-10-02", "2026-10-05"))
    assert "product" in t["projects"] and not by_kind(t, "card")
    res = proposals.make(cfg, t, os.path.join(cfg.root, "proposals"))
    assert res[0]["status"] == "no_changes" and res[0]["llm_calls"] == 0 and not os.path.exists(os.path.join(cfg.root, "proposals"))


@pytest.mark.parametrize("mode", ["garbage", "error"])
def test_k2_llm_failure_fallback(week, mode):
    cfg, p = week
    os.environ["FAKE_WEEKLY_MODE"] = mode
    res = proposals.make(cfg, collect(cfg), os.path.join(cfg.root, "proposals"))
    r = res[0]
    assert r["fallback"] and r["error"] and r["n"] == 1
    text = open(os.path.join(cfg.root, r["file"])).read()
    assert "без неё" in text and "[[product-new]]" in text


def test_k2_validate_was_must_exist():
    cards = [{"name": "c1", "description": "описание карточки длинное", "essence": "", "events": [], "changes": []}]
    ok, bad = proposals.validate([{"subsection": "Что подтверждено", "was": "строки нет в разделе",
                                   "propose": "новое", "basis": "c1", "quote": "описание карточки длинное"}],
                                 cards, "текущий раздел")
    assert not ok and "«было»" in bad[0][0]


# ---------- конвейер целиком ----------

def test_weekly_dry_run_sends_nothing_commits_nothing(week):
    cfg, p = week
    sent = []
    head = T._out(cfg.root, "rev-parse", "HEAD").strip()
    rc = wrun.main(cfg, args(dry_run=True), out=lambda *a: None, send=lambda md: sent.append(md) or 1)
    assert rc == 0 and sent == []
    dry = os.path.join(cfg.state, "weekly", "dry")
    assert os.path.isfile(os.path.join(dry, "reviews/2026-10-24.md"))
    assert os.path.isfile(os.path.join(dry, "proposals/2026-10-24-product.md"))
    assert not os.path.exists(os.path.join(cfg.root, "reviews")) and not os.path.exists(os.path.join(cfg.root, "proposals"))
    assert T._out(cfg.root, "rev-parse", "HEAD").strip() == head
    steps = {r["step"]: r for r in runs(cfg) if r.get("cmd") == "weekly"}
    assert {"k1", "k2", "k3", "k5", "deliver", "commit", "done"} <= set(steps)
    assert steps["deliver"]["status"] == "dry_run" and steps["done"]["status"] == "ok"
    assert steps["k3"]["lines"] <= 40 and steps["k3"]["tokens_in"] > 0
    assert all(r.get("mode") == "dry-run" for r in steps.values())


def test_weekly_run_sends_and_commits_only_outputs(week):
    cfg, p = week
    sent = []
    rc = wrun.main(cfg, args(), out=lambda *a: None, send=lambda md: sent.append(md) or 1)
    assert rc == 0 and len(sent) == 1 and sent[0].startswith("# Неделя")
    files = T._out(cfg.root, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(files) == ["proposals/2026-10-24-product.md", "reviews/2026-10-24.md"]
    done = [r for r in runs(cfg) if r.get("step") == "done"][-1]
    assert done["mode"] == "run" and done["until_ts"] == T.Window.from_dates(*W).until_ts


def test_weekly_k1_failure_still_reviews(week, monkeypatch):
    cfg, p = week
    monkeypatch.setattr(wrun, "run_k1", lambda cfg, dry, out: {"status": "error", "error": "сводчик упал: boom"})
    rc = wrun.main(cfg, args(skip_consolidate=False), out=lambda *a: None, send=lambda md: 1)
    md = open(os.path.join(cfg.root, "reviews/2026-10-24.md")).read()
    assert rc == 1 and "сводчик: сбой (error)" in md
    steps = {r["step"]: r for r in runs(cfg) if r.get("cmd") == "weekly"}
    assert steps["k1"]["status"] == "error" and steps["done"]["status"] == "partial"
    assert steps["k3"]["status"] == "ok"


def test_weekly_k1_real_consolidate_dry_run(week):
    cfg, p = week
    res = wrun.run_k1(cfg, True, out=lambda *a: None)
    assert res["status"] == "dry_run" and res["rc"] == 0
    assert any(r.get("cmd") == "consolidate" and r.get("run_id") == res["run_id"] for r in runs(cfg))


def test_weekly_deliver_failure_logged(week):
    cfg, p = week

    def boom(md):
        raise deliver.DeliveryError("sendMessage: URLError")
    rc = wrun.main(cfg, args(), out=lambda *a: None, send=boom)
    steps = {r["step"]: r for r in runs(cfg) if r.get("cmd") == "weekly"}
    assert rc == 1 and steps["deliver"]["status"] == "error" and steps["commit"]["status"] == "ok"


def test_weekly_llm_garbage_whole_pipeline(week):
    cfg, p = week
    os.environ["FAKE_WEEKLY_MODE"] = "garbage"
    rc = wrun.main(cfg, args(dry_run=True), out=lambda *a: None, send=lambda md: 1)
    steps = {r["step"]: r for r in runs(cfg) if r.get("cmd") == "weekly"}
    assert steps["k3"]["status"] == "fallback" and steps["k2"]["status"] == "partial"   # К2: файл без модели
    assert rc == 1
    md = open(os.path.join(cfg.state, "weekly/dry/reviews/2026-10-24.md")).read()
    assert review.count_lines(md) <= 40 and review.items_without_link(md) == []


def test_weekly_cli_dry_run(week):
    cfg, p = week
    env = {"PATH": "/usr/bin:/bin", **p["env"], "KB_CLAUDE_BIN": FAKE, "KB_SKILL_DIRS": str(p["tmp"] / "skills")}
    r = subprocess.run([PY, os.path.join(TOOLS, "kb.py"), "weekly", "--dry-run", "--skip-consolidate",
                        "--since", W[0], "--until", W[1]], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dry-run — ничего не отправлено" in r.stdout and "weekly/dry/reviews/2026-10-24.md" in r.stdout


def test_weekly_default_window_continues_last_run(week):
    cfg, p = week
    now = time.time()
    kbcore.log_run(cfg, cmd="weekly", step="done", status="ok", mode="run", auto_window=True,
                   since_ts=now - 13 * 86400, until_ts=now - 6 * 86400)
    w = wrun.default_window(cfg, now)
    assert abs(w.since_ts - (now - 6 * 86400)) < 1 and w.until_ts == now
    kbcore.log_run(cfg, cmd="weekly", step="done", status="ok", mode="dry-run", auto_window=True,
                   since_ts=now - 2 * 86400, until_ts=now - 86400)
    assert abs(wrun.default_window(cfg, now).since_ts - (now - 6 * 86400)) < 1   # dry-run окно не сдвигает
    # ручной бэкфилл с явными датами расписание не сдвигает, даже если записан позже
    kbcore.log_run(cfg, cmd="weekly", step="done", status="ok", mode="run", auto_window=False,
                   since_ts=now - 30 * 86400, until_ts=now - 20 * 86400)
    assert abs(wrun.default_window(cfg, now).since_ts - (now - 6 * 86400)) < 1


def test_weekly_rerun_same_day_repeats_window(week):
    """Перезапуск через минуты после прогона — то же окно целиком, а не пустой обзор за 10 минут."""
    cfg, p = week
    now = time.time()
    kbcore.log_run(cfg, cmd="weekly", step="done", status="partial", mode="run", auto_window=True,
                   since_ts=now - 7 * 86400 - 600, until_ts=now - 600)
    w = wrun.default_window(cfg, now)
    assert abs(w.since_ts - (now - 7 * 86400 - 600)) < 1


def test_weekly_window_built_after_k1(week, monkeypatch):
    """Карточка, которую сводчик слил во время К1, входит в обзор этой же недели."""
    cfg, p = week
    root = p["root"]
    today = kbcore.iso()[:10]

    def k1(cfg, dry, out):
        write(os.path.join(cfg.cards, "product/product-fresh.md"),
              hist_card("product-fresh", [(today, "свежее событие от сводчика")], desc="свежая"))
        subprocess.run(["git", "-C", str(root), *kbcore.GIT_ID, "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), *kbcore.GIT_ID, "commit", "-q", "-m", "consolidate: слияние"],
                       check=True)
        return {"status": "ok", "cards_touched": 1}
    monkeypatch.setattr(wrun, "run_k1", k1)
    rc = wrun.main(cfg, args(since=None, until=None, skip_consolidate=False), out=lambda *a: None,
                   send=lambda md: 1)
    t = json.load(open(os.path.join(cfg.state, "weekly", f"{today}-truth.json")))
    assert "product-fresh" in [it.get("name") for it in t["items"]], rc


def test_weekly_refuses_non_main(week):
    cfg, p = week
    subprocess.run(["git", "-C", cfg.root, "checkout", "-q", "-b", "other"], check=True)
    sent = []
    rc = wrun.main(cfg, args(), out=lambda *a: None, send=lambda md: sent.append(md) or 1)
    assert rc == 2 and sent == [] and not os.path.exists(os.path.join(cfg.root, "reviews"))
    assert [r for r in runs(cfg) if r.get("cmd") == "weekly"][-1]["status"] == "error"


def test_k1_timeout_kills_process_group(week, monkeypatch, tmp_path):
    cfg, p = week
    pidf = tmp_path / "child.pid"
    from conftest import script
    s = script(str(tmp_path / "hang.py"), f"""
        import subprocess, time
        c = subprocess.Popen(["sleep", "300"])
        open({str(pidf)!r}, "w").write(str(c.pid))
        time.sleep(300)
        """)
    monkeypatch.setattr(wrun, "K1_TIMEOUT", 2)
    res = wrun.run_k1(cfg, False, out=lambda *a: None, cmd=[s])
    assert res["rc"] == 124 and res["status"] == "error"
    pid = int(pidf.read_text())
    time.sleep(0.5)
    try:
        os.kill(pid, 0)
        alive = open(f"/proc/{pid}/stat").read().split()[2] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        alive = False
    assert not alive


def test_assert_safe():
    for bad in ["projects/product.md", "about/owner-profile.md", "decisions.md", "decisions/x.md",
                "proposals/../projects/x.md", "/etc/passwd", "MOC.md"]:
        with pytest.raises(RuntimeError):
            wrun.assert_safe([bad])
    wrun.assert_safe(["reviews/2026-10-24.md", "proposals/2026-10-24-product.md"])


# ---------- К5 ----------

def test_skills_drift(week, monkeypatch):
    cfg, p = week
    monkeypatch.setattr(skills_drift, "EXCLUDE", re.compile(r"^\.|\.bak|personal-diary"))
    item, stats = skills_drift.check(cfg)
    assert item["kind"] == "skills" and item["ref"] == "[skills.md](skills.md)"
    assert stats["installed"] == 2                      # personal-diary-v2 — исключение (KB_SKILLS_EXCLUDE)
    assert "personal-diary" not in item["text"] and "remote-only-skill" not in item["text"]
    assert "new-skill" in item["text"]
    write(str(p["tmp"] / "skills/newer/SKILL.md"), "x")
    item, stats = skills_drift.check(cfg)
    assert stats["new"] == ["newer"]
    assert open(os.path.join(cfg.root, "skills.md")).read().startswith("# Скиллы")


def test_skills_due_every_4_weeks():
    now = time.time()
    assert skills_drift.due([], now)
    assert skills_drift.due([{"cmd": "weekly", "step": "k5", "status": "ok", "mode": "dry-run",
                              "ts": kbcore.iso(now - 86400)}], now)      # dry-run цикл не сбрасывает
    rec = {"cmd": "weekly", "step": "k5", "status": "ok", "ts": kbcore.iso(now - 7 * 86400)}
    assert not skills_drift.due([rec], now)
    rec["ts"] = kbcore.iso(now - 28 * 86400)
    assert skills_drift.due([rec], now)


# ---------- доставка ----------

def _mcp_server(responses):
    """Фейковый MCP: initialize → ok, tools/call → по очереди из responses (dict результата или Exception-код)."""
    import http.server
    import threading
    calls = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            calls.append(body)
            if body["method"] == "initialize":
                res = {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}}
            else:
                r = responses.pop(0)
                if isinstance(r, int):
                    self.send_response(r)
                    self.end_headers()
                    return
                res = {"jsonrpc": "2.0", "id": body["id"], "result": r}
            data = ("event: message\ndata: " + json.dumps(res) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, calls


def _ok(mid):
    return {"content": [{"type": "text", "text": json.dumps({"message_id": mid, "status": "sent"})}]}


def test_deliver_saved_messages_html_escaped(monkeypatch):
    srv, calls = _mcp_server([_ok(1), _ok(2)])
    monkeypatch.setenv("KB_TG_MCP_URL", f"http://127.0.0.1:{srv.server_port}/v1/mcp")
    text = ("<b>x</b> & [ссылка](projects/product.md)\n" + "y" * 100 + "\n") * 40
    res = deliver.send(text)
    srv.shutdown()
    assert res["messages"] == 2 and res["channel"].startswith("saved_messages")
    args = [c["params"]["arguments"] for c in calls if c["method"] == "tools/call"]
    assert all(a["chat_id"] == "me" and a["parse_mode"] == "html" for a in args)
    assert "&lt;b&gt;x&lt;/b&gt; &amp;" in args[0]["message"] and "<b>" not in args[0]["message"]


def test_deliver_falls_back_to_bot_only_if_nothing_sent(monkeypatch):
    srv, calls = _mcp_server([{"content": [{"type": "text", "text": json.dumps({"ok": False, "error": "flood"})}]}])
    monkeypatch.setenv("KB_TG_MCP_URL", f"http://127.0.0.1:{srv.server_port}/v1/mcp")
    sent = []
    monkeypatch.setattr(deliver, "send_bot", lambda text, timeout=20: sent.append(text) or 1)
    res = deliver.send("коротко")
    assert sent == ["коротко"] and "запасной" in res["channel"] and "flood" in res["fallback_error"]
    # вторая часть упала после первой — бот не дублирует, ошибка честная
    srv2, _ = _mcp_server([_ok(1), 500])
    monkeypatch.setenv("KB_TG_MCP_URL", f"http://127.0.0.1:{srv2.server_port}/v1/mcp")
    with pytest.raises(deliver.DeliveryError) as e:
        deliver.send(("z" * 100 + "\n") * 60)
    srv.shutdown(); srv2.shutdown()
    assert "отправлено частей 1" in str(e.value) and len(sent) == 1


def test_deliver_mcp_unreachable_uses_bot(monkeypatch):
    monkeypatch.setenv("KB_TG_MCP_URL", "http://127.0.0.1:9/v1/mcp")
    monkeypatch.setattr(deliver, "send_bot", lambda text, timeout=20: 3)
    res = deliver.send("x")
    assert res["messages"] == 3 and "initialize: URLError" in res["fallback_error"]


def test_deliver_ambiguous_tools_call_not_duplicated(monkeypatch):
    """Ответ на tools/call потерян (500 после отправки, битый JSON, без message_id) — бот не дублирует."""
    sent = []
    monkeypatch.setattr(deliver, "send_bot", lambda text, timeout=20: sent.append(text) or 1)
    for bad in (500, {"content": [{"type": "text", "text": "[1, 2]"}]}, {"content": []}):
        srv, _ = _mcp_server([bad])
        monkeypatch.setenv("KB_TG_MCP_URL", f"http://127.0.0.1:{srv.server_port}/v1/mcp")
        with pytest.raises(deliver.DeliveryError) as e:
            deliver.send("x")
        srv.shutdown()
        assert e.value.ambiguous and e.value.sent == 0
    assert sent == []


def test_deliver_both_channels_fail_keeps_both_reasons(monkeypatch):
    monkeypatch.setenv("KB_TG_MCP_URL", "http://127.0.0.1:9/v1/mcp")

    def boom(text, timeout=20):
        raise deliver.DeliveryError("sendMessage: HTTP 401")
    monkeypatch.setattr(deliver, "send_bot", boom)
    with pytest.raises(deliver.DeliveryError) as e:
        deliver.send("x")
    assert "initialize" in str(e.value) and "HTTP 401" in str(e.value)


def test_deliver_chunks_and_no_token_in_error(tmp_path, monkeypatch):
    envf = write(str(tmp_path / "bot.env"), "TELEGRAM_BOT_TOKEN=SECRET123\n")
    monkeypatch.setenv("KB_TG_ENV", envf)
    monkeypatch.setenv("KB_TG_API", "http://127.0.0.1:9")    # закрытый порт
    with pytest.raises(deliver.DeliveryError) as e:
        deliver.send_bot("строка\n" * 10, timeout=2)
    assert "SECRET123" not in str(e.value)
    parts = deliver.chunks(("x" * 100 + "\n") * 100)
    assert len(parts) == 3 and all(len(c) <= deliver.CHUNK for c in parts)
    monkeypatch.setenv("KB_TG_ENV", str(tmp_path / "нет"))
    with pytest.raises(deliver.DeliveryError):
        deliver.send_bot("x")


def test_deliver_http_error_has_code_and_description(tmp_path, monkeypatch):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({"ok": False, "description": "Bad Request: chat not found"}).encode()
            self.send_response(400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("KB_TG_ENV", write(str(tmp_path / "bot.env"), "TELEGRAM_BOT_TOKEN=SECRET123\n"))
    monkeypatch.setenv("KB_TG_CHAT_ID", "1")
    monkeypatch.setenv("KB_TG_API", f"http://127.0.0.1:{srv.server_port}")
    try:
        with pytest.raises(deliver.DeliveryError) as e:
            deliver.send_bot("привет", timeout=5)
    finally:
        srv.shutdown()
    assert "HTTP 400" in str(e.value) and "chat not found" in str(e.value) and "SECRET123" not in str(e.value)


# ---------- юниты systemd ----------

def test_systemd_units_present():
    d = os.path.join(TOOLS, "systemd")
    names = sorted(os.listdir(d))
    for n in ["agent-knowledge-weekly", "agent-knowledge-daily", "agent-knowledge-harvest"]:
        assert f"{n}.service" in names and f"{n}.timer" in names
    weekly = open(os.path.join(d, "agent-knowledge-weekly.timer")).read()
    assert "OnCalendar=Sat *-*-* 07:00:00 UTC" in weekly and "Persistent=true" in weekly
    svc = open(os.path.join(d, "agent-knowledge-weekly.service")).read()
    assert "KB_HINT=0" in svc and "tools/kb weekly" in svc


def test_deliver_mcp_4xx_on_call_falls_back(monkeypatch):
    sent = []
    monkeypatch.setattr(deliver, "send_bot", lambda text, timeout=20: sent.append(text) or 1)
    srv, _ = _mcp_server([400])
    monkeypatch.setenv("KB_TG_MCP_URL", f"http://127.0.0.1:{srv.server_port}/v1/mcp")
    res = deliver.send("x")
    srv.shutdown()
    assert sent == ["x"] and "HTTP 400" in res["fallback_error"]


def test_for_telegram_strips_internal_links():
    md = "- Решено ([[team-logins]], [решение](decisions/x.md), [[a|b]]); вики [тут](https://wiki.example/p)"
    assert deliver.for_telegram(md) == "- Решено (team-logins, решение, a); вики тут (https://wiki.example/p)"


def test_send_passes_telegram_text_to_both_channels(monkeypatch):
    got = []
    monkeypatch.setattr(deliver, "send_saved", lambda text, timeout=60: got.append(("saved", text)) or 1)
    deliver.send("см. [[team-logins]] и [тут](HTTPS://X.ru)")
    def fail(text, timeout=60):
        raise deliver.DeliveryError("MCP initialize: URLError")
    monkeypatch.setattr(deliver, "send_saved", fail)
    monkeypatch.setattr(deliver, "send_bot", lambda text, timeout=20: got.append(("bot", text)) or 1)
    deliver.send("см. [[team-logins]]")
    assert got == [("saved", "см. team-logins и тут (HTTPS://X.ru)"), ("bot", "см. team-logins")]
