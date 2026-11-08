"""health: статусы, источники, диск, коммит при смене статуса."""
import json
import os
import re
import time

import pytest

import health
import ingest
import kbcore
from conftest import git_log, runs, write



def statuses(h):
    return {p["what"]: p["level"] for p in h["problems"]}


def test_green_after_ingest(kb):
    cfg, p = kb
    ingest.run_ingest(cfg, "all")
    h = health.run_health(cfg, quiet=True)
    assert h["status"] == "green", h["problems"]
    assert h["sources"]["claude-vps"]["state"] == "ok"
    assert h["sources"]["claude-vps"]["files"] == 3
    assert h["consolidator"]["note"] == "ещё не запускался"
    hj = json.load(open(os.path.join(cfg.state, "health.json")))
    assert hj["status"] == "green"
    md = open(os.path.join(cfg.root, "HEALTH.md")).read()
    assert md.startswith("# Здоровье базы знаний — зелёный")
    assert "| claude-vps | 3 | ok |" in md


def test_never_ingested_is_red(kb):
    cfg, _ = kb
    h = health.compute(cfg)
    assert h["status"] == "red"
    assert "нет успешного ingest больше 24 ч" in statuses(h)


def test_disk_low_red(kb, monkeypatch):
    cfg, _ = kb
    ingest.run_ingest(cfg, "all")
    monkeypatch.setenv("KB_MIN_FREE_GB", "1000000")
    h = health.compute(kbcore.Cfg())
    assert h["status"] == "red"
    assert any(w.startswith("диск: свободно") for w in statuses(h))


def test_quarantine_conflicts_lint_consolidator(kb):
    cfg, p = kb
    ingest.run_ingest(cfg, "all")
    write(os.path.join(cfg.quarantine, "claude-vps/x.md"), "x")
    write(os.path.join(cfg.root, "CONFLICTS.md"),
          "## Открытые\n- [ ] 2026-01-01 a — b\n- [ ] 2026-10-20 c\n- [x] 2026-01-01 закрыт\n")
    write(os.path.join(cfg.cards, "product/bad.md"), "без frontmatter")
    kbcore.log_run(cfg, cmd="consolidate", status="error")
    h = health.compute(cfg)
    s = statuses(h)
    assert h["quarantine"]["files"] == 1 and any(w.startswith("карантин: 1") for w in s)
    assert h["conflicts"]["open"] == 2 and h["conflicts"]["oldest"] == "2026-01-01"
    assert any(w.startswith("CONFLICTS: 2") for w in s)
    assert h["lint"]["errors"] == 1
    assert "сводчик: последний прогон error" in s
    assert h["status"] == "yellow"


def test_health_commits_only_on_status_change(kb):
    cfg, _ = kb
    ingest.run_ingest(cfg, "all")
    health.run_health(cfg, quiet=True)
    n = len([l for l in git_log(cfg) if l.startswith("health:")])
    assert n == 1
    health.run_health(cfg, quiet=True)
    assert len([l for l in git_log(cfg) if l.startswith("health:")]) == 1
    write(os.path.join(cfg.quarantine, "claude-vps/x.md"), "x")
    health.run_health(cfg, quiet=True)
    assert git_log(cfg)[0] == "health: green → yellow"




def _exec(block, health_path, state, now=None):
    sent = []
    g = {"json": json, "os": os, "now": now or time.time(), "state": state,
         "send": lambda t: sent.append(t)}
    exec(block.replace("/opt/agent-knowledge/state/health.json", str(health_path)), g)
    return sent


# ---------- находки ревью ----------

def test_health_commit_retried_after_failure(kb, monkeypatch):
    """#11: коммит смены статуса не прошёл — следующий прогон повторяет его."""
    cfg, _ = kb
    ingest.run_ingest(cfg, "all")
    real = health.commit_paths
    monkeypatch.setattr(health, "commit_paths",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git сломан")))
    health.run_health(cfg, quiet=True)
    assert not [l for l in git_log(cfg) if l.startswith("health:")]
    monkeypatch.setattr(health, "commit_paths", real)
    health.run_health(cfg, quiet=True)
    assert [l for l in git_log(cfg) if l.startswith("health:")] == ["health: — → green"]


def test_quarantine_acked_files_not_yellow(kb):
    cfg, p = kb
    ingest.run_ingest(cfg, "all")
    write(os.path.join(cfg.quarantine, "notes/a.md"), "x")
    write(os.path.join(cfg.quarantine, ".ack"), "# решение владельца\nnotes/a.md\n")
    h = health.compute(cfg)
    assert h["quarantine"] == {"files": 0, "acked": 1}
    assert not any(w.startswith("карантин") for w in statuses(h))
    assert "1 отложено решением владельца" in health.render_md(h)
    write(os.path.join(cfg.quarantine, "notes/b.md"), "новый")      # новый файл — снова сигнал
    h = health.compute(cfg)
    assert h["quarantine"] == {"files": 1, "acked": 1} and any(w.startswith("карантин: 1") for w in statuses(h))


def test_quarantine_ack_sha_and_prefix(kb):
    import hashlib
    cfg, p = kb
    ingest.run_ingest(cfg, "all")
    f = write(os.path.join(cfg.quarantine, "notes/a.md"), "x")
    sha = hashlib.sha256(b"x").hexdigest()
    write(os.path.join(cfg.quarantine, ".ack"), f"{sha}  raw/.quarantine/notes/a.md\n")
    assert health.compute(cfg)["quarantine"] == {"files": 0, "acked": 1}
    write(os.path.join(cfg.quarantine, ".ack"), f"{sha.upper()} *./raw/.quarantine/notes/a.md\n")
    assert health.compute(cfg)["quarantine"] == {"files": 0, "acked": 1}
    write(f, "изменился")                                               # новое содержимое — снова сигнал
    assert health.compute(cfg)["quarantine"] == {"files": 1, "acked": 0}


def test_consolidator_deferred_is_not_a_problem(kb):
    """Отложенный по порогу прогон (kb daily, < 3 единиц) — не сбой; но и не прячет прошлый сбой."""
    cfg, _ = kb
    kbcore.log_run(cfg, cmd="consolidate", status="ok")
    kbcore.log_run(cfg, cmd="consolidate", status="deferred", deferred={"units": 2, "min_units": 3})
    h = health.compute(cfg)
    assert not [p for p in h["problems"] if "сводчик" in p["what"]]
    assert h["consolidator"]["last_status"] == "deferred"
    kbcore.log_run(cfg, cmd="consolidate", status="gates_failed")
    kbcore.log_run(cfg, cmd="consolidate", status="deferred", deferred={"units": 1, "min_units": 3})
    h = health.compute(cfg)
    assert [p for p in h["problems"] if "сводчик: последний прогон gates_failed" in p["what"]]


def test_consolidator_stuck_gates_shows_reason_and_turns_red(kb):
    """Ветка сводчика не вливается несколько дней — красный флаг с причиной, а не безликое «gates_failed»."""
    cfg, _ = kb
    old = kbcore.iso(kbcore.now_ts() - 5 * 86400)
    kbcore.log_run(cfg, cmd="consolidate", status="ok")
    with open(os.path.join(cfg.state, "runs.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": old, "cmd": "consolidate", "status": "gates_failed"}) + "\n")
    kbcore.log_run(cfg, cmd="consolidate", status="gates_failed",
                   gates={"near_duplicates": ["a-card ~ b-card (name, 0.93)"], "sensitive": [], "new_conflicts": 4})
    h = health.compute(cfg)
    p = [p for p in h["problems"] if "сводчик: последний прогон gates_failed" in p["what"]]
    assert p and p[0]["level"] == "red", h["problems"]
    assert "a-card ~ b-card" in p[0]["what"] and "не вливаются" in p[0]["what"]


def test_consolidator_partial_run_that_merged_is_not_stuck(kb):
    """partial/limit/deferred прогон, который слил ветку, — правки в main идут: не красный и без «не вливаются»."""
    cfg, _ = kb
    old = kbcore.iso(kbcore.now_ts() - 5 * 86400)
    with open(os.path.join(cfg.state, "runs.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": old, "cmd": "consolidate", "status": "gates_failed"}) + "\n")
    kbcore.log_run(cfg, cmd="consolidate", status="deferred", merged="abc123")
    kbcore.log_run(cfg, cmd="consolidate", status="partial", merged="def456")
    h = health.compute(cfg)
    p = [p for p in h["problems"] if "сводчик" in p["what"]]
    assert not p, p                       # последний прогон слил ветку — сводчик не застрял

