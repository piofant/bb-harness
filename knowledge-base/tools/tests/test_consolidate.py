"""Сводчик: нарезка, ledger и идемпотентность, отброс без цитаты, SUPERSEDE против CONFLICT,
sensitive вне private, worktree и lock. LLM — фейковый `claude` (tests/fake_claude.py)."""
import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import time

import pytest

import health
import kbcore
from conftest import git_log, write
from consolidate import gates, llm, stage1, stage2
from consolidate import run as crun
from consolidate.ledger import Ledger
from consolidate.units import collapse, iter_units, normalize, sha, split_task_groups

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_claude.py")
TODAY = dt.date.today().isoformat()


def git(root, *a):
    return subprocess.run(["git", "-C", str(root), *kbcore.GIT_ID, *a], capture_output=True, text=True)


def commit_all(root, msg="raw"):
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)


@pytest.fixture
def ck(kb, tmp_path, monkeypatch):
    """База с сырьём + фейковый claude + отдельный путь worktree."""
    cfg, paths = kb
    root = paths["root"]
    raw = root / "raw"
    write(str(raw / "claude-vps/-home-app/MEMORY.md"), "# Индекс\n- [deploy](project_deploy.md)\n")
    write(str(raw / "claude-vps/-home-app/project_deploy.md"),
          "---\nname: deploy\n---\nCLAIM:infra:deploy: Деплой Stable — рестарт только app-stable.\n"
          "NOQUOTE:infra: Выдуманный факт без цитаты.\n")
    write(str(raw / "codex-vps/MEMORY.md"),
          "# Task Group: /root skill release\n\nscope: x\nCLAIM:product:skill-release: Комментарий в тикет — "
          "только после pr_url.\n\n# Task Group: /root medical\n\nSENS: Пример медицинского эпизода.\n")
    write(str(raw / "codex-vps/memory_summary.md"), "CLAIM:agents:pipeline: Ревью делает отдельный агент.\n")
    write(str(raw / "codex-vps/rollout_summaries/2026-10-20T01-01-01-abc-x.md"),
          "updated_at: 2026-10-20T01:01:01Z\nCLAIM:tools:tracker: Логины в трекере берутся из Directory.\n")
    write(str(raw / "claude-vps/-home-product/project_team.md"),
          "CLAIM:product:team: Команда Продукта ведёт тикеты в очереди PROJ.\n")
    write(str(raw / "claude-vps/-home-product/_index.md"), "индекс\n")
    commit_all(root)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("KB_CLAUDE_BIN", FAKE)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("KB_CONSOLIDATE_WT", str(tmp_path / "wt" / "consolidate"))
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.delenv("FAKE_S2_OPS", raising=False)
    paths["log"] = log
    paths["wt"] = tmp_path / "wt" / "consolidate"
    return cfg, paths


def calls(paths):
    if not os.path.exists(paths["log"]):
        return []
    return [json.loads(x) for x in open(paths["log"]) if x.strip()]


def args(**kw):
    d = dict(areas="product,tools,infra,agents", max_batches=20, dry_run=False, parallel=2, batch_size=3,
             no_merge=False, max_conflicts=10, pilot=False, stages="12")
    d.update(kw)
    return argparse.Namespace(**d)


def run(cfg, **kw):
    out = []
    rc = crun.main(cfg, args(**kw), out=out.append)
    return rc, out


def main_card(cfg, name):
    for c in kbcore.iter_cards(cfg, include_private=True):
        if c.name == name:
            return c
    return None


def last_consolidate(cfg):
    return [r for r in kbcore.read_runs(cfg) if r.get("cmd") == "consolidate"][-1]


# ---------- нарезка ----------

def test_units_slicing(ck):
    cfg, _ = ck
    us = iter_units(cfg.raw, cfg.root)
    uids = [u.uid for u in us]
    assert "raw/claude-vps/-home-app/MEMORY.md" not in uids          # индекс исключён
    assert "raw/claude-vps/-home-product/_index.md" not in uids
    tg = [u for u in us if u.kind == "codex-task-group"]
    assert [u.uid for u in tg] == ["raw/codex-vps/MEMORY.md#tg:root-medical",
                                   "raw/codex-vps/MEMORY.md#tg:root-skill-release"]
    assert all(u.src == "raw/codex-vps/MEMORY.md" for u in tg)
    assert len({u.sha for u in tg}) == 2
    kinds = {u.kind for u in us}
    assert kinds == {"claude-memory", "codex-task-group", "codex-summary", "codex-rollout"}
    ro = next(u for u in us if u.kind == "codex-rollout")
    assert ro.date == "2026-10-20"


def test_split_task_groups_and_hash_normalization():
    text = "шапка\n# Task Group: A\nтело a\n# Task Group: B\nтело b\n"
    assert [h for h, _ in split_task_groups(text)] == ["A", "B"]
    assert sha("a  b\r\n c") == sha("a b c") and normalize(" x\n\ty ") == "x y"


# ---------- проверка цитаты ----------

def test_quote_check():
    src = "Деплой  Stable —\nрестарт только `app-stable`."
    assert stage1.quote_in("Stable — рестарт только `app-stable`", src)
    assert not stage1.quote_in("рестарт только app-stable", src)              # без обратных кавычек — не дословно
    assert not stage1.quote_in("", src)


# ---------- полный прогон, ledger, идемпотентность ----------

def test_full_run_merges_and_is_idempotent(ck):
    cfg, paths = ck
    rc, out = run(cfg)
    assert rc == 0, "\n".join(out)
    cs = calls(paths)
    assert {c["stage"] for c in cs} == {"s1", "s2", "hub"}
    assert all(c["model"] == "sonnet" for c in cs if c["stage"] == "s1")
    assert all(c["model"] == "opus" for c in cs if c["stage"] != "s1")
    # карточки слиты в main, индексы пересобраны на main
    deploy = main_card(cfg, "infra-deploy")
    assert deploy is not None and deploy.status == "active"
    assert len(deploy.fm["sources"]) == 1
    assert deploy.fm["sources"][0].startswith("raw/claude-vps/-home-app/project_deploy.md@20")
    assert "⟦" not in deploy.text
    assert main_card(cfg, "infra-hub") is not None and main_card(cfg, "product-hub") is not None
    assert "infra-deploy" in open(os.path.join(cfg.root, "INDEX.tsv")).read()
    log = git_log(cfg)
    assert any(s.startswith("consolidate: слияние consolidate/") for s in log)
    assert log[0].startswith("index:")
    rec = last_consolidate(cfg)
    assert rec["status"] == "ok" and rec["gates"]["ok"] and rec["gates"]["quote_100"]
    assert rec["stats"]["s1_dropped_quote"] == 1
    assert rec["merged"]
    batches = [r for r in kbcore.read_runs(cfg) if r.get("cmd") == "consolidate_batch"]
    assert batches and all("duration_s" in b and "tokens_in" in b for b in batches if b["status"] == "ok")
    n = len(cs)
    # повторный прогон без изменений → 0 вызовов LLM
    rc, out = run(cfg)
    assert rc == 0, "\n".join(out)
    assert len(calls(paths)) == n
    assert last_consolidate(cfg)["llm_calls"] == 0
    # изменили одну единицу → одна стадия 1 по ней, стадия 2 только её области
    p = paths["root"] / "raw/claude-vps/-home-product/project_team.md"
    p.write_text("CLAIM:product:team: Команда Продукта ведёт тикеты в очереди PROJ.\n"
                 "CLAIM:product:team: В команде есть менеджер проектов.\n")
    commit_all(paths["root"])
    rc, out = run(cfg)
    assert rc == 0, "\n".join(out)
    new = calls(paths)[n:]
    assert [c["stage"] for c in new if c["stage"] == "s1"] == ["s1"]
    assert {c["stage"] for c in new} <= {"s1", "s2", "hub"}
    led = Ledger(os.path.join(cfg.state, "ledger.sqlite"))
    st = led.stats()
    led.close()
    assert st.get("stale", 0) >= 1


def test_quote_dropped_never_reaches_cards(ck):
    cfg, paths = ck
    run(cfg)
    led = Ledger(os.path.join(cfg.state, "ledger.sqlite"))
    rows = [dict(r) for r in led.db.execute("SELECT * FROM claims WHERE status='no_quote'")]
    linked = [r for r in led.db.execute(
        "SELECT * FROM claim_cards cc JOIN claims c ON c.id=cc.claim_id WHERE c.status='no_quote'")]
    led.close()
    assert len(rows) == 1 and rows[0]["text"].startswith("Выдуманный")
    assert not linked
    for c in kbcore.iter_cards(cfg, include_private=True):
        assert "Выдуманный" not in c.text


def test_dry_run_no_llm_no_writes(ck):
    cfg, paths = ck
    head = git(paths["root"], "rev-parse", "HEAD").stdout
    rc, out = run(cfg, dry_run=True)
    assert rc == 0
    assert calls(paths) == []
    assert git(paths["root"], "rev-parse", "HEAD").stdout == head
    assert not os.path.exists(paths["wt"])
    assert "dry-run" in out[0]


def test_max_batches_and_limit_resume(ck, monkeypatch):
    cfg, paths = ck
    rc, out = run(cfg, max_batches=1, stages="1", batch_size=2)
    assert len([c for c in calls(paths) if c["stage"] == "s1"]) == 1
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    rc, out = run(cfg, stages="1", batch_size=2)
    assert rc == 5
    assert last_consolidate(cfg)["status"] == "limit"
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    rc, out = run(cfg, batch_size=2)
    assert rc == 0, "\n".join(out)
    led = Ledger(os.path.join(cfg.state, "ledger.sqlite"))
    n_ext = led.db.execute("SELECT count(*) FROM extractions WHERE status='ok'").fetchone()[0]
    led.close()
    assert n_ext == len(collapse(iter_units(cfg.raw, cfg.root))[0])   # всё извлечено, без повторов


# ---------- SUPERSEDE против CONFLICT ----------

def _seed_old_card(paths):
    wt_cards = paths["root"] / "cards/infra"
    write(str(wt_cards / "restart-both-bots.md"),
          "---\nname: restart-both-bots\ndescription: старое правило рестарта двух ботов\ntype: process\n"
          "status: active\nkeys: [рестарт]\nsources: [raw/claude-vps/-home-app/project_deploy.md@2026-06-08]\n"
          "verified: 2026-06-08\nverified_how: source\nreview_after: 2026-09-08\nsensitivity: internal\n---\n\n"
          "**Суть.** Рестартить оба бота.\n\n## История\n- 2026-06-08 — создана\n")
    commit_all(paths["root"], "старая карточка")


def test_supersede_marks_old_and_no_conflict(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _seed_old_card(paths)
    ops = {"infra": [{"op": "SUPERSEDE", "name": "deploy-app-stable", "dir": "infra", "type": "process",
                      "description": "Как выкатывать app на Stable", "keys": ["деплой", "app-stable"],
                      "supersedes": ["restart-both-bots"], "claims": ["@рестарт только"],
                      "body": "**Суть.** Только app-stable. ⟦@рестарт только⟧\n\n## Шаги\n"
                              "1. Рестарт app-stable ⟦@рестарт только⟧\n\n## История\n- 2026-10-23 — новое правило"}]}
    spec = tmp_path / "ops.json"
    spec.write_text(json.dumps(ops, ensure_ascii=False))
    monkeypatch.setenv("FAKE_S2_OPS", str(spec))
    conflicts_before = open(os.path.join(cfg.root, "CONFLICTS.md")).read()
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    old, new = main_card(cfg, "restart-both-bots"), main_card(cfg, "deploy-app-stable")
    assert old.status == "superseded" and old.fm["superseded_by"] == ["deploy-app-stable"]
    assert new.status == "active" and new.fm["supersedes"] == ["restart-both-bots"]
    assert "⟦" not in new.text and "1. Рестарт app-stable" in new.text
    assert open(os.path.join(cfg.root, "CONFLICTS.md")).read() == conflicts_before


def test_conflict_appends_line_in_health_format(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    ops = {"infra": [{"op": "CONFLICT", "name": "deploy-app-stable",
                      "conflict": "одно правило против другого", "claims": ["@рестарт только"]}]}
    spec = tmp_path / "ops.json"
    spec.write_text(json.dumps(ops, ensure_ascii=False))
    monkeypatch.setenv("FAKE_S2_OPS", str(spec))
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    text = open(os.path.join(cfg.root, "CONFLICTS.md")).read()
    line = f"- [ ] {TODAY} deploy-app-stable — одно правило против другого"
    assert line in text
    assert text.index(line) > text.index("## Открытые")
    conf = health.conflicts(cfg, kbcore.now_ts())
    assert conf["open"] == 1


def test_many_conflicts_block_merge(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    ops = {"infra": [{"op": "CONFLICT", "name": f"t{i}", "conflict": f"конфликт {i}", "claims": ["@рестарт только"]}
                     for i in range(3)]}
    spec = tmp_path / "ops.json"
    spec.write_text(json.dumps(ops, ensure_ascii=False))
    monkeypatch.setenv("FAKE_S2_OPS", str(spec))
    rc, out = run(cfg, areas="infra", max_conflicts=2)
    assert rc == 1
    rec = last_consolidate(cfg)
    assert rec["status"] == "gates_failed" and not rec.get("merged")
    assert "конфликт 0" not in open(os.path.join(cfg.root, "CONFLICTS.md")).read()      # main не тронут
    assert "конфликт 0" in open(os.path.join(paths["wt"], "CONFLICTS.md")).read()     # ветка осталась
    assert git(paths["root"], "rev-parse", "--verify", f"consolidate/{TODAY}").returncode == 0


# ---------- sensitive ----------

def test_sensitive_claims_never_leave_private(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    rc, out = run(cfg, stages="1")
    led = Ledger(os.path.join(cfg.state, "ledger.sqlite"))
    sens = [dict(r) for r in led.db.execute("SELECT * FROM claims WHERE sensitive=1")]
    led.close()
    assert len(sens) == 1 and sens[0]["status"] == "sensitive" and sens[0]["area"] == "private"
    sid = sens[0]["id"]
    ops = {"product": [
        {"op": "ADD", "name": "leak", "dir": "product", "type": "fact", "description": "утечка",
         "keys": ["x"], "claims": [sid], "body": f"**Суть.** Эпизод ⟦c{sid}⟧"},
        {"op": "ADD", "name": "leak-text", "dir": "product", "type": "fact", "description": "утечка текстом",
         "keys": ["x"], "claims": ["@pr_url"], "body": "**Суть.** Принимал антидепрессант ⟦@pr_url⟧"},
        {"op": "ADD", "name": "skill-release-to-ticket", "dir": "product", "type": "process",
         "description": "Релиз скилла в тикет", "keys": ["PROJ-778"], "claims": ["@pr_url"],
         "body": "**Суть.** Коммент после PR ⟦@pr_url⟧\n\n## Шаги\n1. Дождаться PR ⟦@pr_url⟧"},
    ]}
    spec = tmp_path / "ops.json"
    spec.write_text(json.dumps(ops, ensure_ascii=False))
    monkeypatch.setenv("FAKE_S2_OPS", str(spec))
    rc, out = run(cfg, areas="product")
    assert rc == 0, "\n".join(out)
    s2 = [c for c in calls(paths) if c["stage"] == "s2"]
    assert s2
    assert main_card(cfg, "leak") is None and main_card(cfg, "leak-text") is None
    assert main_card(cfg, "skill-release-to-ticket") is not None
    rec = last_consolidate(cfg)
    assert any("leak:" in r and "sensitive" in r for r in rec["rejected"]), rec["rejected"]
    assert any("leak-text:" in r and "медицин" in r for r in rec["rejected"]), rec["rejected"]
    for c in kbcore.iter_cards(cfg):
        assert "медицинск" not in c.text.lower()


def test_sensitive_claim_not_in_stage2_prompt(ck, monkeypatch):
    cfg, paths = ck
    prompts = []
    real = llm.run

    def spy(prompt, model, timeout, cwd):
        prompts.append(prompt)
        return real(prompt, model, timeout, cwd)
    monkeypatch.setattr(llm, "run", spy)
    run(cfg)
    assert prompts and not any("медицинского эпизода" in p for p in prompts if "извлекаешь знания" not in p)


def test_gate_flags_sensitive_card_outside_private(ck):
    cfg, paths = ck
    write(str(paths["root"] / "cards/product/bad.md"),
          "---\nname: bad\ndescription: d\ntype: fact\nstatus: active\nkeys: [x]\nsources: [raw/a.md]\n"
          "verified: 2026-10-23\nverified_how: source\nreview_after: 2026-11-23\nsensitivity: internal\n---\n\n"
          "**Суть.** Телефон +7 (999) 123-45-67.\n")
    g = gates.run_gates(cfg, {"claims_used": 1, "claims_used_quote_ok": 1}, 0)
    assert not g["ok"] and g["sensitive"]


def test_unsupported_line_rejects_card(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    ops = {"infra": [{"op": "ADD", "name": "no-support", "dir": "infra", "type": "process",
                      "description": "без опоры", "keys": ["x"], "claims": ["@рестарт только"],
                      "body": "**Суть.** Строка с опорой ⟦@рестарт только⟧\n\n## Шаги\n1. Выдуманный шаг без маркера"}]}
    spec = tmp_path / "ops.json"
    spec.write_text(json.dumps(ops, ensure_ascii=False))
    monkeypatch.setenv("FAKE_S2_OPS", str(spec))
    run(cfg, areas="infra")
    assert main_card(cfg, "no-support") is None
    assert any("без опоры" in r for r in last_consolidate(cfg)["rejected"])


def test_near_duplicate_gate(ck):
    cfg, paths = ck
    for n in ("deploy-a", "deploy-b"):
        write(str(paths["root"] / f"cards/infra/{n}.md"),
              f"---\nname: {n}\ndescription: Как выкатывать app на Stable\ntype: fact\nstatus: active\n"
              "keys: [x]\nsources: [raw/a.md]\nverified: 2026-10-23\nverified_how: source\n"
              "review_after: 2026-11-23\nsensitivity: internal\n---\n\n**Суть.** x\n")
    g = gates.run_gates(cfg, {}, 0)
    assert not g["ok"] and g["near_duplicates"]
    known = gates.near_duplicates(gates.iter_cards(cfg))
    g = gates.run_gates(cfg, {}, 0, known_dups=known)                 # пара уже была в main — не валит
    assert g["ok"] and not g["near_duplicates"] and g["near_duplicates_known"]
    write(str(paths["root"] / "cards/infra/deploy-c.md"),
          "---\nname: deploy-c\ndescription: Как выкатывать app на Stable\ntype: fact\nstatus: active\n"
          "keys: [x]\nsources: [raw/a.md]\nverified: 2026-10-23\nverified_how: source\n"
          "review_after: 2026-11-23\nsensitivity: internal\n---\n\n**Суть.** x\n")
    g = gates.run_gates(cfg, {}, 0, known_dups=known)                 # новая пара — валит
    assert not g["ok"] and all("deploy-c" in p for p in g["near_duplicates"])
    g = gates.run_gates(cfg, {}, 0, main_cfg=cfg)                     # main = это же дерево: ветка ничего не меняла
    assert g["ok"] and not g["near_duplicates"]


def test_gate_known_issues_from_main_do_not_block(ck, tmp_path):
    """Проблемы, которые уже лежат в main (sensitive, ошибки lint, почти-дубли), ветку не валят — иначе одна
    старая карточка блокирует слияние навсегда (застревание consolidate/2026-10-27). Секреты валят всегда."""
    cfg, paths = ck
    import copy
    import shutil
    fm = ("---\nname: {n}\ndescription: {d}\ntype: fact\nstatus: active\nkeys: [x]\nsources: [raw/a.md]\n"
          "verified: 2026-10-23\nverified_how: {vh}\nreview_after: 2026-11-23\nsensitivity: internal\n---\n\n"
          "**Суть.** {s}\n")
    write(str(paths["root"] / "cards/infra/legal.md"),
          fm.format(n="legal", d="тикет юристам", vh="source", s="логин someone@gmail.com"))
    write(str(paths["root"] / "cards/infra/bad-lint.md"),
          fm.format(n="bad-lint", d="ошибка формата", vh="как-то проверено", s="x"))
    for n in ("pair-a", "pair-b"):
        write(str(paths["root"] / f"cards/infra/{n}.md"), fm.format(n=n, d="Как выкатывать app на Stable",
                                                                     vh="source", s="x"))
    main_root = tmp_path / "main-copy"
    shutil.copytree(paths["root"], main_root)
    main_cfg = copy.copy(cfg)
    main_cfg.root = str(main_root)
    g = gates.run_gates(cfg, {}, 0, main_cfg=main_cfg)
    assert g["ok"], g
    assert g["sensitive_known"] and not g["sensitive"]
    assert g["lint_errors_known"] and not g["lint_errors"]
    # новая проблема в ветке валит
    write(str(paths["root"] / "cards/infra/legal.md"),
          fm.format(n="legal", d="тикет юристам", vh="source", s="новый текст, почта ivan@gmail.com"))
    write(str(paths["root"] / "cards/infra/pair-c.md"),
          fm.format(n="pair-c", d="Как выкатывать app на Stable", vh="source", s="x"))
    g = gates.run_gates(cfg, {}, 0, main_cfg=main_cfg)
    assert not g["ok"] and g["sensitive"] and all("pair-c" in p for p in g["near_duplicates"])
    # пары, признанные dedup разными сущностями, не валят
    g = gates.run_gates(cfg, {}, 0, known_dups=[p for p in g["near_duplicates"]], main_cfg=main_cfg)
    assert not g["near_duplicates"] and g["sensitive"]


def test_near_duplicates_focus_and_prefilter():
    from types import SimpleNamespace as NS
    mk = lambda n, d, t="fact": NS(name=n, description=d, type=t, status="active")
    cs = [mk("deploy-a", "Как выкатывать app на Stable"), mk("deploy-b", "Как выкатывать app на Stable"),
          mk("other", "Совсем другая карточка про холст"), mk("deploy-c", "Как выкатывать app на Stable!")]
    full = gates.near_duplicates(cs)
    assert len([p for p in full if "(description" in p]) == 3
    assert all("deploy-c" in p for p in gates.near_duplicates(cs, focus={"deploy-c"}))
    assert gates.near_duplicates(cs, focus=set()) == []


def test_known_main_duplicate_does_not_block_merge(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _card(paths, "cards/product/old-pair-a.md", "old-pair-a", "Старая пара почти одинаковых карточек про отчёт")
    _card(paths, "cards/agents/old-pair-b.md", "old-pair-b", "Старая пара почти одинаковых карточек про отчёт")
    commit_all(paths["root"], "старая пара в main")
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "ADD", "name": "fresh-card", "dir": "infra", "type": "fact", "description": "новая карточка прогона",
         "keys": ["x"], "claims": ["@рестарт только"], "body": "**Суть.** a ⟦@рестарт только⟧"}]})
    rc, out = run(cfg, areas="infra")
    rec = last_consolidate(cfg)
    assert rc == 0 and rec["status"] == "ok", "\n".join(out)
    assert rec["gates"]["ok"] and not rec["gates"]["near_duplicates"]   # пары main ветка не трогала — не проверяются
    assert main_card(cfg, "fresh-card").status == "active"


# ---------- worktree и lock ----------

def test_worktree_branch_and_lock(ck):
    cfg, paths = ck
    fd = os.open(cfg.lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        head = git(paths["root"], "rev-parse", "HEAD").stdout
        rc, out = run(cfg)
        assert rc == 1
        assert last_consolidate(cfg)["status"] == "lock_timeout"
        assert git(paths["root"], "rev-parse", "HEAD").stdout == head
        assert calls(paths) == []
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    rc, out = run(cfg, no_merge=True)
    assert rc == 0, "\n".join(out)
    wt = paths["wt"]
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == f"consolidate/{TODAY}"
    assert (wt / "cards/infra/infra-deploy.md").exists()
    assert not (paths["root"] / "cards/infra/infra-deploy.md").exists()           # без слияния main не тронут
    assert not os.path.exists(os.path.join(wt, "INDEX.tsv")) or \
        "infra-deploy" not in open(os.path.join(wt, "INDEX.tsv")).read()           # индексы — только на main
    rc, out = run(cfg)                                                              # следующий прогон сливает
    assert rc == 0, "\n".join(out)
    assert (paths["root"] / "cards/infra/infra-deploy.md").exists()
    assert "infra-deploy" in open(os.path.join(cfg.root, "INDEX.tsv")).read()


def test_respects_external_flock(ck, monkeypatch):
    """Запись в общий .git и main — под тем же flock, что и ingest: внешний `flock` держит → lock_timeout."""
    cfg, paths = ck
    run(cfg, no_merge=True)
    p = subprocess.Popen(["flock", cfg.lock, "sleep", "3"])
    try:
        import time
        time.sleep(0.3)
        monkeypatch.setenv("KB_LOCK_TIMEOUT", "1")
        rc, out = run(kbcore.Cfg())
        assert rc == 1 and last_consolidate(cfg)["status"] == "lock_timeout"
    finally:
        p.wait()


# ---------- вызов claude ----------

def test_llm_cmd_flags_and_stdin(ck):
    cfg, paths = ck
    r = llm.run("извлекаешь знания\n", "sonnet", timeout=30, cwd=cfg.state)
    c = calls(paths)[-1]
    a = c["argv"]
    assert a[0] == "-p" and "--no-session-persistence" in a and "--strict-mcp-config" in a
    assert a[a.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert a[a.index("--setting-sources") + 1] == ""
    assert a[-2] == "--disallowedTools" and "Bash" in a[-1]                   # промпт не позиционный — stdin
    assert c["prompt_len"] > 0 and r["tokens_in"] >= 0


def test_llm_limit_and_garbage(ck, monkeypatch):
    cfg, _ = ck
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    with pytest.raises(llm.LimitHit):
        llm.run("x", "sonnet", timeout=30, cwd=cfg.state)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    with pytest.raises(llm.LLMError):
        llm.run("x", "sonnet", timeout=30, cwd=cfg.state)


def test_stage2_parse_and_markers():
    body, meta = stage2.strip_markers("**Суть.** A ⟦c1, c2⟧\nB ⟦c3⟧\n\n## Шаги\n1. шаг ⟦c4⟧\n2. без\n\n## Грабли\n- г")
    assert "⟦" not in body
    assert stage2.unsupported_lines(meta, {1, 3, 4}) == ["2. без"]
    assert stage2.parse_json("```json\n{\"ops\": []}\n```") == {"ops": []}


# ---------- находки ревью (RED → GREEN) ----------

def _spec(tmp_path, monkeypatch, ops):
    spec = tmp_path / "ops.json"
    spec.write_text(json.dumps(ops, ensure_ascii=False))
    monkeypatch.setenv("FAKE_S2_OPS", str(spec))


def test_unmerged_branch_continues_next_day(ck, monkeypatch):
    """Ветка с несслитой работой не теряется при смене даты: следующий прогон продолжает её и сливает."""
    cfg, paths = ck
    rc, out = run(cfg, no_merge=True)
    assert rc == 0, "\n".join(out)
    tomorrow = dt.date.today() + dt.timedelta(days=1)

    class FakeDate(dt.date):
        @classmethod
        def today(cls):
            return tomorrow
    monkeypatch.setattr(crun, "dt", type("M", (), {"date": FakeDate}))
    rc, out = run(cfg)
    assert rc == 0, "\n".join(out)
    assert main_card(cfg, "infra-deploy") is not None
    assert last_consolidate(cfg)["branch"] == f"consolidate/{TODAY}"


def test_conflict_gate_counts_branch_not_run(ck, tmp_path, monkeypatch):
    """Повторный прогон не обходит гейт конфликтов: считаются строки ветки относительно main."""
    cfg, paths = ck
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "CONFLICT", "name": f"t{i}", "conflict": f"конфликт {i}", "claims": ["@рестарт только"]}
        for i in range(3)]})
    run(cfg, areas="infra", max_conflicts=2)
    rc, out = run(cfg, areas="infra", max_conflicts=2)
    assert rc == 1
    assert last_consolidate(cfg)["gates"]["new_conflicts"] == 3
    assert "конфликт 0" not in open(os.path.join(cfg.root, "CONFLICTS.md")).read()


def test_rejected_claims_are_retried_then_given_up(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "ADD", "name": "no-support", "dir": "infra", "type": "process", "description": "без опоры",
         "keys": ["x"], "claims": ["@рестарт только"],
         "body": "**Суть.** С опорой ⟦@рестарт только⟧\n\n## Шаги\n1. Шаг без маркера"}]})
    for _ in range(4):
        run(cfg, areas="infra")
    s2 = [c for c in calls(paths) if c["stage"] == "s2"]
    assert len(s2) == 3                                       # MAX_ATTEMPTS попыток, потом стоп
    led = Ledger(os.path.join(cfg.state, "ledger.sqlite"))
    row = dict(led.db.execute("SELECT * FROM claims WHERE text LIKE '%рестарт только%'").fetchone())
    led.close()
    assert row["status"] == "rejected" and row["attempts"] == 3


def test_truncated_stage1_not_marked_extracted(ck, monkeypatch):
    """Оборванный ответ: последняя встреченная единица батча (и не встреченные) не помечаются извлечёнными;
    единицы до неё разобраны целиком и спасаются. Следующий прогон добирает остальные."""
    cfg, paths = ck
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "truncate")
    run(cfg, stages="1", batch_size=3)                       # 6 единиц → 2 батча, в каждом спасено 2 из 3
    led = Ledger(os.path.join(cfg.state, "ledger.sqlite"))
    n_ok = led.db.execute("SELECT count(*) FROM extractions WHERE status='ok'").fetchone()[0]
    led.close()
    assert n_ok == 4
    assert any(r.get("status") == "truncated" for r in kbcore.read_runs(cfg) if r.get("cmd") == "consolidate_batch")
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    run(cfg, stages="1", batch_size=3)
    led = Ledger(os.path.join(cfg.state, "ledger.sqlite"))
    n_ok = led.db.execute("SELECT count(*) FROM extractions WHERE status='ok'").fetchone()[0]
    led.close()
    assert n_ok == 6


def test_unexpected_exception_is_error_and_branch_saved(ck, monkeypatch):
    cfg, paths = ck

    def boom(*a, **k):
        raise KeyError("сломалось")
    from consolidate import apply as A
    monkeypatch.setattr(A, "append_conflicts", boom)
    rc, out = run(cfg)
    assert rc == 1
    rec = last_consolidate(cfg)
    assert rec["status"] == "error" and any("KeyError" in e for e in rec["errors"])
    assert git(paths["wt"], "status", "--porcelain", "--", "cards").stdout.strip() == ""


def test_sensitive_in_description_and_conflict_rejected(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "ADD", "name": "phone-in-desc", "dir": "infra", "type": "fact", "description": "звонить +7 999 123-45-67",
         "keys": ["x"], "claims": ["@рестарт только"], "body": "**Суть.** x ⟦@рестарт только⟧"},
        {"op": "CONFLICT", "name": "t", "conflict": "приём антидепрессанта против режима", "claims": ["@рестарт только"]}]})
    run(cfg, areas="infra")
    assert main_card(cfg, "phone-in-desc") is None
    assert "антидепрессант" not in open(os.path.join(cfg.root, "CONFLICTS.md")).read()


def test_supersede_cannot_touch_other_area(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    write(str(paths["root"] / "cards/product/product-old.md"),
          "---\nname: product-old\ndescription: старая карточка продукта\ntype: fact\nstatus: active\nkeys: [x]\n"
          "sources: [raw/a.md]\nverified: 2026-10-01\nverified_how: source\nreview_after: 2026-12-01\n"
          "sensitivity: internal\n---\n\n**Суть.** старое\n")
    commit_all(paths["root"], "product-old")
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "SUPERSEDE", "name": "infra-new", "dir": "infra", "type": "fact", "description": "новое правило",
         "keys": ["x"], "supersedes": ["product-old"], "claims": ["@рестарт только"],
         "body": "**Суть.** новое ⟦@рестарт только⟧"}]})
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    assert main_card(cfg, "product-old").status == "active"


def test_foreign_claim_ids_rejected(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    run(cfg, stages="1")
    led = Ledger(os.path.join(cfg.state, "ledger.sqlite"))
    foreign = led.db.execute("SELECT id FROM claims WHERE area='product' AND status='new'").fetchone()[0]
    led.close()
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "ADD", "name": "foreign", "dir": "infra", "type": "fact", "description": "чужая опора",
         "keys": ["x"], "claims": [foreign], "body": f"**Суть.** x ⟦c{foreign}⟧"}]})
    run(cfg, areas="infra")
    assert main_card(cfg, "foreign") is None
    assert any("вне промпта" in r for r in last_consolidate(cfg)["rejected"])


def test_ledger_removed_unit_and_revert(tmp_path):
    from consolidate.units import Unit
    led = Ledger(str(tmp_path / "l.sqlite"))
    a = Unit("raw/x.md", "raw/x.md", "notes", "notes:x.md", "текст A достаточно длинный", "2026-10-01")
    claim = {"text": "t", "kind": "fact", "topic": "t", "area": "infra", "date": "2026-10-01",
             "quote": "текст A достаточно", "sensitive": False, "quote_ok": True}
    led.sync_units([a], {})
    led.add_extraction(a, "v", [claim], "r1")
    b = Unit("raw/x.md", "raw/x.md", "notes", "notes:x.md", "текст B другой совсем", "2026-10-02")
    led.sync_units([b], {})
    assert [r["status"] for r in led.db.execute("SELECT status FROM claims")] == ["stale"]
    led.sync_units([a], {})                                    # A→B→A: утверждения A оживают
    assert [r["status"] for r in led.db.execute("SELECT status FROM claims")] == ["new"]
    assert not led.needs_extraction(a, "v")
    led.sync_units([], {})                                     # единица пропала из raw/
    assert [r["status"] for r in led.db.execute("SELECT status FROM claims")] == ["stale"]
    led.close()


def test_near_duplicates_are_merged_by_dedup_pass(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "ADD", "name": "deploy-stable", "dir": "infra", "type": "fact", "description": "Как выкатывать app на Stable",
         "keys": ["x"], "claims": ["@рестарт только"], "body": "**Суть.** a ⟦@рестарт только⟧"},
        {"op": "ADD", "name": "deploy-stable-bot", "dir": "infra", "type": "fact",
         "description": "Как выкатывать app на Stable-бот", "keys": ["y"], "claims": ["@рестарт только"],
         "body": "**Суть.** b ⟦@рестарт только⟧"}]})
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    pair = [main_card(cfg, "deploy-stable"), main_card(cfg, "deploy-stable-bot")]
    keep = [c for c in pair if c.status == "active"]
    other = [c for c in pair if c.status == "superseded"]
    assert len(keep) == 1 and len(other) == 1
    assert other[0].fm["superseded_by"] == [keep[0].name]
    assert last_consolidate(cfg)["stats"]["op_MERGE"] == 1


def test_hub_trimmed_and_missing_links_unlinked(ck, tmp_path, monkeypatch):
    from consolidate import stage2 as s2
    body = "**Суть.** x\n\n## Карточки\n" + "\n".join(f"- [[c-{i}]] — карточка {i} " + "слово " * 10
                                                       for i in range(80)) + "\n\n## История\n- 2026-10-23 — x"
    t = s2.trim_hub(body, "infra")
    assert len(t.strip().splitlines()) <= 40 and len(t.encode()) <= 4200
    assert "cards/infra/_index.md" in t and t.strip().endswith("— x")
    cfg, paths = ck
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "ADD", "name": "with-link", "dir": "infra", "type": "fact", "description": "со ссылкой",
         "keys": ["x"], "claims": ["@рестарт только"], "body": "**Суть.** a ⟦@рестарт только⟧\n\n## Подробности\n- см. [[nowhere]]"}]})
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    assert "`nowhere`" in main_card(cfg, "with-link").text


def test_rejection_reason_fed_back_on_retry(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "ADD", "name": "no-support", "dir": "infra", "type": "process", "description": "без опоры",
         "keys": ["x"], "claims": ["@рестарт только"],
         "body": "**Суть.** С опорой ⟦@рестарт только⟧\n\n## Шаги\n1. Шаг без маркера"}]})
    prompts = []
    real = llm.run

    def spy(prompt, model, timeout, cwd):
        prompts.append(prompt)
        return real(prompt, model, timeout, cwd)
    monkeypatch.setattr(llm, "run", spy)
    run(cfg, areas="infra")
    s2 = [p for p in prompts if "Область: `infra`" in p]
    assert len(s2) == 2                                       # повтор отклонённого — в этом же прогоне
    assert "ОТКЛОНИЛА" not in s2[0] and "no-support: строки без опоры" in s2[1]


# ---------- повторное ревью (RED → GREEN) ----------

def _card(paths, rel, name, desc, extra=""):
    write(str(paths["root"] / rel),
          f"---\nname: {name}\ndescription: {desc}\ntype: fact\nstatus: active\nkeys: [x]\n"
          f"sources: [raw/a.md]\nverified: 2026-10-01\nverified_how: source\nreview_after: 2026-12-01\n"
          f"sensitivity: internal\n{extra}---\n\n**Суть.** старое\n")


def _near_dup_op(name="deploy-stable-bot"):
    return {"op": "ADD", "name": name, "dir": "infra", "type": "fact",
            "description": "Как выкатывать app на Stable-бот", "keys": ["y"], "claims": ["@рестарт только"],
            "body": "**Суть.** b: рестарт только app-stable ⟦@рестарт только⟧"}


def test_dedup_skips_cross_area_pairs(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _card(paths, "cards/product/deploy-stable.md", "deploy-stable", "Как выкатывать app на Stable")
    commit_all(paths["root"], "product card")
    _spec(tmp_path, monkeypatch, {"infra": [_near_dup_op()]})
    rc, out = run(cfg, areas="infra")
    assert not [c for c in calls(paths) if c["stage"] == "dedup"]
    assert main_card(cfg, "deploy-stable").status == "active"
    assert last_consolidate(cfg)["status"] == "gates_failed"


def test_dedup_merges_cross_area_pair_when_both_areas_in_run(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _card(paths, "cards/product/deploy-stable.md", "deploy-stable", "Как выкатывать app на Stable")
    commit_all(paths["root"], "product card")
    _spec(tmp_path, monkeypatch, {"infra": [_near_dup_op()]})
    rc, out = run(cfg, areas="infra,product")
    assert [c for c in calls(paths) if c["stage"] == "dedup"], "\n".join(out)
    assert last_consolidate(cfg)["status"] != "gates_failed", "\n".join(out)


def test_dedup_distinct_pair_does_not_block_gate(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _card(paths, "cards/infra/deploy-stable.md", "deploy-stable", "Как выкатывать app на Stable")
    commit_all(paths["root"], "card")
    _spec(tmp_path, monkeypatch, {"infra": [_near_dup_op()]})
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "distinct")
    run(cfg, areas="infra")
    rec = last_consolidate(cfg)
    assert rec["gates"]["near_duplicates_known"] and not rec["gates"]["near_duplicates"]
    run(cfg, areas="infra")                    # второй прогон: модель не зовём, пара всё ещё известная
    assert len([c for c in calls(paths) if c["stage"] == "dedup"]) == 1
    assert not last_consolidate(cfg)["gates"]["near_duplicates"]


def test_dedup_never_merges_hub_of_other_area(ck, tmp_path, monkeypatch):
    """Пара разных областей, где одна карточка — хаб своей области: dedup её не трогает."""
    cfg, paths = ck
    _card(paths, "cards/product/product-hub.md", "product-hub", "Как выкатывать app на Stable")
    commit_all(paths["root"], "hub-like card")
    _spec(tmp_path, monkeypatch, {"infra": [_near_dup_op()]})
    run(cfg, areas="infra,product")
    assert not [c for c in calls(paths) if c["stage"] == "dedup" and "product-hub" in json.dumps(c, ensure_ascii=False)]
    assert main_card(cfg, "product-hub") is None or main_card(cfg, "product-hub").status != "superseded"


def test_distinct_pairs_from_ledger_even_if_dedup_skipped(ck, tmp_path, monkeypatch):
    """Вердикт «разные» берётся из реестра: прогон, где dedup до пары не дошёл (лимит, порог), гейт не валит;
    порядок имён в паре роли не играет."""
    from consolidate import ledger as L
    from consolidate.run import distinct_pairs, dedup_sig
    cfg, paths = ck
    fm = ("---\nname: {n}\ndescription: Как выкатывать app на Stable\ntype: fact\nstatus: active\nkeys: [x]\n"
          "sources: [raw/a.md]\nverified: 2026-10-23\nverified_how: source\nreview_after: 2026-11-23\n"
          "sensitivity: internal\n---\n\n**Суть.** {n}\n")
    for n in ("pair-x", "pair-y"):
        write(str(paths["root"] / f"cards/infra/{n}.md"), fm.format(n=n))
    cs = {c.name: c for c in gates.iter_cards(cfg)}
    led = L.Ledger(os.path.join(cfg.state, "ledger-test.sqlite"))
    try:
        led.add_rejection("pair-y~pair-x", "dedup",
                          f"разные сущности [{dedup_sig(cs['pair-y'].text, cs['pair-x'].text)}]: разное", "r1")
        known = distinct_pairs(led, cfg)
        assert known == ["pair-y ~ pair-x"]
        g = gates.run_gates(cfg, {}, 0, known_dups=known)
        assert g["ok"] and g["near_duplicates_known"]
        write(str(paths["root"] / "cards/infra/pair-x.md"), fm.format(n="pair-x") + "новое\n")
        assert distinct_pairs(led, cfg) == []           # текст поменялся — вердикт устарел
    finally:
        led.close()


def test_dedup_keeps_core_and_preserves_frontmatter(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _card(paths, "cards/infra/zz-deploy-stable.md", "zz-deploy-stable", "Как выкатывать app на Stable",
          extra="core: true\nsupersedes: [restart-both-bots]\n")
    commit_all(paths["root"], "core card")
    _spec(tmp_path, monkeypatch, {"infra": [_near_dup_op("aa-deploy-stable")]})
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    keep, other = main_card(cfg, "zz-deploy-stable"), main_card(cfg, "aa-deploy-stable")
    assert keep.status == "active" and keep.core and other.status == "superseded"
    assert keep.fm["supersedes"] == ["restart-both-bots", "aa-deploy-stable"]


def test_dedup_distinct_is_left_to_gate_and_not_retried(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _card(paths, "cards/infra/deploy-stable.md", "deploy-stable", "Как выкатывать app на Stable")
    commit_all(paths["root"], "card")
    _spec(tmp_path, monkeypatch, {"infra": [_near_dup_op()]})
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "distinct")
    run(cfg, areas="infra")
    run(cfg, areas="infra")
    assert len([c for c in calls(paths) if c["stage"] == "dedup"]) == 1
    assert main_card(cfg, "deploy-stable").status == "active"


def test_update_preserves_sensitivity_core_supersedes(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    write(str(paths["root"] / "cards/infra/kept.md"),
          "---\nname: kept\ndescription: карточка с ручными полями\ntype: fact\nstatus: active\n"
          "supersedes: [old-x]\napplies_to: [mac]\nkeys: [x]\nsources: [raw/a.md]\nverified: 2026-10-01\n"
          "verified_how: source\nreview_after: 2026-12-01\nsensitivity: restricted\ncore: true\n---\n\n**Суть.** x\n")
    commit_all(paths["root"], "kept")
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "UPDATE", "name": "kept", "dir": "infra", "type": "fact", "description": "карточка с ручными полями",
         "keys": ["x"], "claims": ["@рестарт только"], "body": "**Суть.** новое ⟦@рестарт только⟧"}]})
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    c = main_card(cfg, "kept")
    assert c.fm["sensitivity"] == "restricted" and c.core and c.fm["supersedes"] == ["old-x"]
    assert c.fm["applies_to"] == ["mac"]


def test_stage2_system_error_does_not_burn_attempts(ck, monkeypatch):
    cfg, paths = ck
    run(cfg, stages="1")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage-s2")
    for _ in range(4):
        run(cfg, stages="2", areas="infra")
    led = Ledger(os.path.join(cfg.state, "ledger.sqlite"))
    row = dict(led.db.execute("SELECT * FROM claims WHERE text LIKE '%рестарт только%'").fetchone())
    led.close()
    assert row["status"] == "new" and (row["attempts"] or 0) == 0


def test_private_names_respected_in_worktree(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _card(paths, "cards/private/secret-note.md", "secret-note", "личное")    # не в git → в worktree нет
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "ADD", "name": "secret-note", "dir": "infra", "type": "fact", "description": "дубль имени",
         "keys": ["x"], "claims": ["@рестарт только"], "body": "**Суть.** x ⟦@рестарт только⟧"},
        {"op": "ADD", "name": "links-private", "dir": "infra", "type": "fact", "description": "ссылка на private",
         "keys": ["x"], "claims": ["@рестарт только"],
         "body": "**Суть.** y ⟦@рестарт только⟧\n\n## Подробности\n- см. [[secret-note]]"}]})
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    assert "[[secret-note]]" in main_card(cfg, "links-private").text
    assert any("secret-note: имя уже занято" in r for r in last_consolidate(cfg)["rejected"])


def test_removed_worktree_continues_unmerged_branch(ck):
    cfg, paths = ck
    run(cfg, no_merge=True)
    git(paths["root"], "worktree", "remove", "--force", str(paths["wt"]))
    rc, out = run(cfg)
    assert rc == 0, "\n".join(out)
    assert main_card(cfg, "infra-deploy") is not None


# ---------- H6: норма извлечения, кэш по версии промпта, гейт «утверждение не пропало», конфликты ----------

from consolidate import atoms as AT  # noqa: E402
from consolidate.units import Unit  # noqa: E402


def _ledger(cfg):
    return Ledger(os.path.join(cfg.state, "ledger.sqlite"))


def _claim_row(cfg, like):
    led = _ledger(cfg)
    row = led.db.execute("SELECT * FROM claims WHERE text LIKE ? ORDER BY id DESC", (f"%{like}%",)).fetchone()
    out = dict(row) if row else None
    led.close()
    return out


def test_extraction_norm_proportional_without_ceiling():
    assert AT.target_claims("") == 2 and AT.target_claims("одна строка\n") == 2
    text = "\n".join(f"строка номер {i} с содержанием" for i in range(100))
    assert AT.target_claims(text) == 5                                   # ~1 на 20 строк
    assert AT.target_claims("\n".join(f"п{i} текст" for i in range(1000))) == 50   # без потолка
    assert AT.content_lines("---\nname: x\n---\n\n## Заголовок\n---\n" + "x" * 450) == 4  # шапка и разделители не в счёт
    u = Unit("raw/x.md", "raw/x.md", "notes", "notes:x.md", text, "2026-10-01")
    p = stage1.build_prompt([u])
    assert "норма≥5" in p and "ОПЕРАЦИОННЫЕ ДЕТАЛИ" in p and "НЕ ОБОБЩАЙ" in p
    assert "Не больше 8" not in p and stage1.PROMPT_VER == "s1-v2"
    a = AT.atoms("Номер версии выбирает бэкенд, рычаг — `--changelog`; проверка `store install x --skills-root /tmp/x`. "
                 "Задержки в memsvc_calls.jsonl. Потолок 8 с, breaker 300 → 1800 с, скорость 14–37 КБ/с, "
                 "замок server-moved.json, версия 0.8.0 → 11.0.0, ошибка 426 client_outdated, @colleague4_work")
    low = {x.lower() for x in a}
    for need in ("--changelog", "--skills-root", "memsvc_calls.jsonl", "8 с", "1800 с", "14–37 кб/с",
                 "server-moved.json", "0.8.0", "11.0.0", "426", "@colleague4_work"):
        assert need in low, need


def test_stage1_logs_norm_and_atom_coverage(ck):
    cfg, paths = ck
    run(cfg, stages="1")
    b = [r for r in kbcore.read_runs(cfg) if r.get("cmd") == "consolidate_batch" and r.get("stage") == 1]
    assert b and all(r["prompt_ver"] == "s1-v2" and "under_norm" in r and "atom_coverage" in r for r in b)
    st = last_consolidate(cfg)["stats"]
    assert st["s1_target"] >= 2 * st["s1_units"] and "s1_under_norm" in st


def test_prompt_version_invalidates_only_reextracted_units(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    a = Unit("raw/a.md", "raw/a.md", "notes", "notes:a.md", "единица А с достаточно длинным текстом", "2026-10-01")
    b = Unit("raw/b.md", "raw/b.md", "notes", "notes:b.md", "единица Б с достаточно длинным текстом", "2026-10-01")

    def cl(q):
        return {"text": q, "kind": "fact", "topic": "t", "area": "infra", "date": "2026-10-01", "quote": q,
                "sensitive": False, "quote_ok": True}
    led.sync_units([a, b], {})
    led.add_extraction(a, "s1-v1", [cl("единица А с достаточно"), cl("А с достаточно длинным")], "r1")
    led.add_extraction(b, "s1-v1", [cl("единица Б с достаточно")], "r1")
    a_ids = [r["id"] for r in led.db.execute("SELECT id FROM claims WHERE uid='raw/a.md' ORDER BY id")]
    led.set_claim_status([a_ids[0]], "consolidated")                  # одно уже в карточке
    assert led.needs_extraction(a, "s1-v2") and led.needs_extraction(b, "s1-v2")   # новая версия — кэш не годится
    led.add_extraction(a, "s1-v2", [cl("достаточно длинным текстом")], "r2")      # переизвлекли только А
    st = {r["id"]: (r["uid"], r["prompt_ver"], r["status"]) for r in led.db.execute("SELECT * FROM claims")}
    assert st[a_ids[0]][2] == "consolidated"                          # опора карточки не тронута
    assert st[a_ids[1]][2] == "reextracted"                           # несведённое старое заменено новым
    assert [v[2] for v in st.values() if v[0] == "raw/b.md"] == ["new"]   # Б не переизвлечена — живёт как раньше
    assert [v[2] for v in st.values() if v[1] == "s1-v2"] == ["new"]
    assert not led.needs_extraction(a, "s1-v2") and led.needs_extraction(b, "s1-v2")
    assert {r["id"] for r in led.pending_claims(["infra"])} == {i for i, v in st.items() if v[2] == "new"}
    led.close()


def test_prompt_version_bump_reextracts_within_budget(ck, monkeypatch):
    cfg, paths = ck
    monkeypatch.setattr(stage1, "PROMPT_VER", "s1-old")
    run(cfg, stages="1")
    n_old = len([c for c in calls(paths) if c["stage"] == "s1"])
    monkeypatch.setattr(stage1, "PROMPT_VER", "s1-new")
    run(cfg, stages="1", max_batches=1, batch_size=2)                # бюджет: один батч из двух единиц
    assert len([c for c in calls(paths) if c["stage"] == "s1"]) == n_old + 1
    led = _ledger(cfg)
    redone = {r["uid"] for r in led.db.execute("SELECT uid FROM extractions WHERE prompt_ver='s1-new'")}
    rows = [dict(r) for r in led.db.execute("SELECT uid, prompt_ver, status FROM claims")]
    led.close()
    assert len(redone) == 2
    for r in rows:
        if r["prompt_ver"] == "s1-old" and r["status"] in ("new", "reextracted"):
            assert (r["status"] == "reextracted") == (r["uid"] in redone), r
    run(cfg, stages="1", max_batches=1, batch_size=2)                # следующий прогон не повторяет сделанное
    led = _ledger(cfg)
    assert led.db.execute("SELECT count(*) FROM extractions WHERE prompt_ver='s1-new'").fetchone()[0] == 4
    led.close()


def _kp_raw(paths, extra=""):
    """Исходная единица про КП; extra — новая единица (отдельный файл: старые утверждения не устаревают)."""
    write(str(paths["root"] / "raw/claude-vps/-home-app/project_kp.md"),
          "CLAIM:infra:kp: Потолок 8 с на попытку, breaker 300 → 1800 с.\n"
          "CLAIM:infra:kp: Задержки пишутся в memsvc_calls.jsonl.\n")
    if extra:
        write(str(paths["root"] / "raw/claude-vps/-home-app/project_kp_proxy.md"), extra)
    commit_all(paths["root"], "kp")


def test_update_gate_rejects_lost_claim_and_retries(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _kp_raw(paths)
    rc, out = run(cfg, areas="infra")                                  # фейк: ADD infra-kp с обоими утверждениями
    assert rc == 0, "\n".join(out)
    card = main_card(cfg, "infra-kp")
    assert "memsvc_calls.jsonl" in card.text and "1800 с" in card.text
    _kp_raw(paths, "CLAIM:infra:kp: Для прокси нужен Accept-Encoding: identity.\n")
    bad = {"op": "UPDATE", "name": "infra-kp", "dir": "infra", "type": "fact", "description": "маршруты КП",
           "keys": ["kp"], "claims": ["@Accept-Encoding"],
           "body": "**Суть.** Потолок 8 с, breaker 300 → 1800 с ⟦@Потолок⟧\n\n## Подробности\n"
                   "- Accept-Encoding: identity ⟦@Accept-Encoding⟧"}            # memsvc_calls.jsonl потерян
    _spec(tmp_path, monkeypatch, {"infra": [bad]})
    prompts = []
    real = llm.run

    def spy(prompt, model, timeout, cwd):
        prompts.append(prompt)
        return real(prompt, model, timeout, cwd)
    monkeypatch.setattr(llm, "run", spy)
    run(cfg, areas="infra")
    card = main_card(cfg, "infra-kp")
    assert "memsvc_calls.jsonl" in card.text and "Accept-Encoding" not in card.text   # UPDATE отклонён
    rec = last_consolidate(cfg)
    assert any("потеряны детали" in r and "memsvc_calls.jsonl" in r for r in rec["rejected"]), rec["rejected"]
    assert rec["stats"]["rejected_lost_claims"] >= 1
    s2 = [p for p in prompts if "Область: `infra`" in p]
    assert len(s2) == 2 and "ОТКЛОНИЛА" in s2[1] and "memsvc_calls.jsonl" in s2[1]     # повтор в этом же прогоне
    row = _claim_row(cfg, "Accept-Encoding")
    assert row["status"] == "rejected" and row["attempts"] == 2
    # исправленный UPDATE (детали на месте) проходит
    good = dict(bad, body=bad["body"] + "\n- Задержки — в memsvc_calls.jsonl ⟦@memsvc_calls⟧",
                claims=["@Accept-Encoding", "@memsvc_calls"])
    _spec(tmp_path, monkeypatch, {"infra": [good]})
    run(cfg, areas="infra")
    card = main_card(cfg, "infra-kp")
    assert "memsvc_calls.jsonl" in card.text and "Accept-Encoding" in card.text
    assert _claim_row(cfg, "Accept-Encoding")["status"] == "consolidated"


def test_update_gate_allows_detail_moved_to_new_card(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _kp_raw(paths)
    run(cfg, areas="infra")
    _kp_raw(paths, "CLAIM:infra:kp: Для прокси нужен Accept-Encoding: identity.\n")
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "UPDATE", "name": "infra-kp", "dir": "infra", "type": "fact", "description": "маршруты КП",
         "keys": ["kp"], "claims": ["@Accept-Encoding"],
         "body": "**Суть.** Потолок 8 с, breaker 300 → 1800 с ⟦@Потолок⟧\n\n## Подробности\n"
                 "- Accept-Encoding: identity ⟦@Accept-Encoding⟧\n- Задержки: [[memsvc-latency]]"},
        {"op": "ADD", "name": "memsvc-latency", "dir": "infra", "type": "fact", "description": "задержки memsvc",
         "keys": ["memsvc"], "claims": ["@memsvc_calls"],
         "body": "**Суть.** Задержки — в memsvc_calls.jsonl ⟦@memsvc_calls⟧"}]})
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    assert "Accept-Encoding" in main_card(cfg, "infra-kp").text
    assert "memsvc_calls.jsonl" in main_card(cfg, "memsvc-latency").text


def test_unreflected_claim_not_consolidated(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _kp_raw(paths)
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "ADD", "name": "kp-limits", "dir": "infra", "type": "fact", "description": "лимиты КП",
         "keys": ["kp"], "claims": ["@Потолок", "@memsvc_calls"],
         "body": "**Суть.** Потолок 8 с, breaker 300 → 1800 с ⟦@Потолок⟧ ⟦@memsvc_calls⟧"}]})
    run(cfg, areas="infra")
    assert _claim_row(cfg, "Потолок")["status"] == "consolidated"
    row = _claim_row(cfg, "memsvc_calls")
    assert row["status"] == "rejected" and row["attempts"] >= 1                  # в тексте нет — не засчитано
    assert any("не отражены" in r and "memsvc_calls.jsonl" in r for r in last_consolidate(cfg)["rejected"])


def test_conflict_not_lost_goes_to_card_and_conflicts(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    run(cfg, areas="infra")                                           # фейк: ADD infra-deploy
    write(str(paths["root"] / "raw/claude-mac/-Users-user-cursor/dns.md"),
          "CLAIM:infra:deploy: Правильный фикс DNS — Split DNS в админке Tailscale.\n")
    commit_all(paths["root"], "dns")
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "CONFLICT", "name": "infra-deploy", "conflict": "Split DNS в админке против /etc/resolver",
         "claims": ["@Split DNS"]}]})
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    card = main_card(cfg, "infra-deploy")
    assert "## Варианты и устарело" in card.text
    assert f"- Вариант/конфликт ({TODAY}): Split DNS в админке против /etc/resolver" in card.text
    assert card.text.index("## Варианты и устарело") < card.text.index("## История")
    assert f"- [ ] {TODAY} infra-deploy — Split DNS в админке против /etc/resolver" in \
        open(os.path.join(cfg.root, "CONFLICTS.md")).read()
    led = _ledger(cfg)
    link = led.db.execute("SELECT cc.card, cc.op, c.status FROM claim_cards cc JOIN claims c ON c.id=cc.claim_id "
                          "WHERE c.text LIKE '%Split DNS%'").fetchone()
    led.close()
    assert tuple(link) == ("infra-deploy", "CONFLICT", "consolidated")
    assert last_consolidate(cfg)["gates"]["ok"]


def test_noop_of_uncovered_detail_retried_once(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _kp_raw(paths)
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "NOOP", "claims": ["@Потолок", "@memsvc_calls", "@рестарт только"], "reason": "покрыто"}]})
    run(cfg, areas="infra")
    s2 = [c for c in calls(paths) if c["stage"] == "s2"]
    assert len(s2) == 2                                               # NOOP деталей → один повтор
    assert _claim_row(cfg, "memsvc_calls")["status"] == "skipped"      # повторный NOOP принят
    assert _claim_row(cfg, "рестарт только")["status"] == "skipped"    # без атомов — NOOP сразу
    assert last_consolidate(cfg)["stats"]["claims_noop_uncovered"] == 2


def test_experiment_branch_and_separate_ledger(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    git(paths["root"], "branch", "consolidate/2026-01-01")             # чужая несслитая ветка сводчика
    git(paths["root"], "checkout", "-q", "consolidate/2026-01-01")
    write(str(paths["root"] / "cards/infra/other.md"), "x")
    commit_all(paths["root"], "чужая работа")
    git(paths["root"], "checkout", "-q", "main")
    wt = tmp_path / "wt-exp"
    monkeypatch.setenv("KB_CONSOLIDATE_WT", str(wt))
    monkeypatch.setenv("KB_CONSOLIDATE_BRANCH", "exp/h6-test")
    monkeypatch.setenv("KB_LEDGER", str(tmp_path / "ledger-exp.sqlite"))
    git(paths["root"], "worktree", "add", "-q", str(wt), "-b", "exp/h6-test")
    head = git(paths["root"], "rev-parse", "main").stdout
    rc, out = run(cfg, no_merge=True)
    assert rc == 0, "\n".join(out)
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "exp/h6-test"
    assert "consolidate" in git(wt, "log", "-1", "--format=%s").stdout
    assert (wt / "cards/infra/infra-deploy.md").exists() and not (wt / "cards/infra/other.md").exists()
    assert git(paths["root"], "rev-parse", "main").stdout == head                  # main не тронут
    assert os.path.exists(tmp_path / "ledger-exp.sqlite")
    assert not os.path.exists(os.path.join(cfg.state, "ledger.sqlite"))           # реестр main не тронут
    assert last_consolidate(cfg)["branch"] == "exp/h6-test" and not last_consolidate(cfg).get("merged")


def test_projects_area_expands_to_project_slugs(ck):
    cfg, paths = ck
    write(str(paths["root"] / "raw/claude-vps/-home-blog/p.md"), "CLAIM:projects/blog:clips: Клипы режутся по таймкоду.\n")
    commit_all(paths["root"], "proj")
    rc, out = run(cfg, areas="infra,projects")
    assert rc == 0, "\n".join(out)
    assert last_consolidate(cfg)["areas"] == ["infra", "projects/blog"]
    assert main_card(cfg, "projects-blog-clips") is not None
    assert main_card(cfg, "projects-blog-hub") is None                         # хаб малого проекта не нужен


# ---------- H6: находки ревью ----------

def test_atom_rules_precision():
    a = {x.lower() for x in AT.atoms("вход/выход, Claude/Codex, ADD/UPDATE, 22.10.2026, в 2026 году, "
                                      "--accept-dns=false, 1 800 с, порт 8118, product/rnd/dev-vm, ~/bin/netdiag")}
    assert not {"вход/выход", "claude/codex", "add/update", "22.10.2026", "2026"} & a
    assert {"--accept-dns=false", "1800 с", "8118", "product/rnd/dev-vm", "~/bin/netdiag"} <= a
    c = {"text": "Потолок 8 с, breaker 300 → 1800 с.", "quote": ""}
    assert AT.claim_present(c, "потолок 8с и 1 800 с")                       # пробелы в числах не мешают
    old, new = AT.norm_text("8 с, 300 → 1800 с"), AT.norm_text("8 с, 1800 с")
    assert AT.missing_details(c, old, new) == ["300"]                         # пропала одна деталь из трёх
    w = {"text": "Эскалация продуктовых вопросов идёт через руководителя направления", "quote": ""}
    assert not AT.claim_present(w, "эскалация — это важно")                   # одного слова из трёх мало
    assert AT.claim_present(w, "Эскалация: продуктовые вопросы → руководитель направления")


def _seed_kp_card(ck_, tmp_path, monkeypatch):
    cfg, paths = ck_
    _kp_raw(paths)
    run(cfg, areas="infra")
    return cfg, paths


def test_gate_requires_every_detail_and_type_cannot_bypass(ck, tmp_path, monkeypatch):
    cfg, paths = _seed_kp_card(ck, tmp_path, monkeypatch)
    _kp_raw(paths, "CLAIM:infra:kp: Для прокси нужен Accept-Encoding: identity.\n")
    body = ("**Суть.** Потолок 8 с, breaker 1800 с ⟦@Потолок⟧\n\n## Подробности\n- Accept-Encoding: identity "
            "⟦@Accept-Encoding⟧\n- Задержки — в memsvc_calls.jsonl ⟦@memsvc_calls⟧")       # «300» пропало
    for t in ("fact", "topic"):
        _spec(tmp_path, monkeypatch, {"infra": [
            {"op": "UPDATE", "name": "infra-kp", "dir": "infra", "type": t, "description": "маршруты КП",
             "keys": ["kp"], "claims": ["@Accept-Encoding"], "body": body}]})
        run(cfg, areas="infra")
        card = main_card(cfg, "infra-kp")
        assert "300" in card.text and "Accept-Encoding" not in card.text, t
        assert any("`300`" in r for r in last_consolidate(cfg)["rejected"]), t


def test_superseded_old_claim_of_card_may_be_dropped(ck, tmp_path, monkeypatch):
    cfg, paths = _seed_kp_card(ck, tmp_path, monkeypatch)
    _kp_raw(paths, "CLAIM:infra:kp: С 23.10 потолок 12 с на попытку вместо прежнего.\n")
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "UPDATE", "name": "infra-kp", "dir": "infra", "type": "fact", "description": "маршруты КП",
         "keys": ["kp"], "claims": ["@потолок 12 с"], "superseded_claims": ["@Потолок 8 с"],
         "body": "**Суть.** Потолок 12 с на попытку ⟦@потолок 12 с⟧\n\n## Подробности\n"
                 "- Задержки — в memsvc_calls.jsonl ⟦@memsvc_calls⟧"}]})
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    card = main_card(cfg, "infra-kp")
    assert "12 с" in card.text and "1800" not in card.text
    assert _claim_row(cfg, "Потолок 8 с")["status"] == "superseded"


def test_experiment_env_forces_no_merge_and_seeds_ledger(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    run(cfg, areas="infra")                                          # реестр main с опорами карточек
    main_led = os.path.join(cfg.state, "ledger.sqlite")
    n_main = sqlite_count(main_led)
    wt = tmp_path / "wt-exp"
    git(paths["root"], "worktree", "add", "-q", str(wt), "-b", "exp/h6-merge")
    monkeypatch.setenv("KB_CONSOLIDATE_WT", str(wt))
    monkeypatch.setenv("KB_CONSOLIDATE_BRANCH", "exp/h6-merge")
    monkeypatch.setenv("KB_LEDGER", str(tmp_path / "exp.sqlite"))
    write(str(paths["root"] / "raw/claude-vps/-home-app/project_new.md"), "CLAIM:infra:deploy: Новый факт про деплой.\n")
    commit_all(paths["root"], "new")
    head = git(paths["root"], "rev-parse", "main").stdout
    rc, out = run(cfg, areas="infra")                                # без --no-merge
    assert rc == 0, "\n".join(out)
    assert any("слияние в main выключено" in x for x in out)
    assert git(paths["root"], "rev-parse", "main").stdout == head
    assert sqlite_count(main_led) == n_main                          # реестр main не тронут
    assert sqlite_count(str(tmp_path / "exp.sqlite")) > n_main       # свой реестр — копия main + новое


def sqlite_count(p):
    import sqlite3
    d = sqlite3.connect(p)
    try:
        return d.execute("SELECT count(*) FROM claims").fetchone()[0]
    finally:
        d.close()


def test_revert_revives_only_latest_prompt_version(tmp_path):
    led = Ledger(str(tmp_path / "l.sqlite"))
    a = Unit("raw/x.md", "raw/x.md", "notes", "notes:x.md", "текст A достаточно длинный", "2026-10-01")
    b = Unit("raw/x.md", "raw/x.md", "notes", "notes:x.md", "текст B другой совсем", "2026-10-02")
    cl = {"text": "t", "kind": "fact", "topic": "t", "area": "infra", "date": "2026-10-01",
          "quote": "текст A достаточно", "sensitive": False, "quote_ok": True}
    led.sync_units([a], {})
    led.add_extraction(a, "s1-v1", [cl], "r1")
    led.db.execute("UPDATE extractions SET at='2026-10-01T00:00:00Z'")
    led.set_claim_status([1], "consolidated")                        # старое уже в карточке
    led.add_extraction(a, "s1-v2", [cl], "r2")
    led.sync_units([b], {})
    led.sync_units([a], {})                                          # A→B→A
    st = sorted((r["prompt_ver"], r["status"]) for r in led.db.execute("SELECT * FROM claims"))
    assert st == [("s1-v1", "stale"), ("s1-v2", "new")]
    led.link([1], "card-x", "ADD", "r1")                             # старая версия — опора карточки
    led.sync_units([b], {})
    led.sync_units([a], {})
    st = sorted((r["prompt_ver"], r["status"]) for r in led.db.execute("SELECT * FROM claims"))
    assert st == [("s1-v1", "consolidated"), ("s1-v2", "new")]
    led.close()


def test_experiment_mode_requires_own_ledger_branch_and_worktree(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    monkeypatch.setenv("KB_CONSOLIDATE_BRANCH", "exp/x")              # без своего реестра
    rc, out = run(cfg)
    assert rc == 2 and "KB_LEDGER" in out[-1]
    monkeypatch.setenv("KB_LEDGER", str(tmp_path / "e.sqlite"))
    monkeypatch.setenv("KB_CONSOLIDATE_BRANCH", "consolidate/exp")
    rc, out = run(cfg)
    assert rc == 2 and "KB_CONSOLIDATE_BRANCH" in out[-1]
    monkeypatch.setenv("KB_CONSOLIDATE_BRANCH", "exp/x")
    monkeypatch.delenv("KB_CONSOLIDATE_WT")
    rc, out = run(cfg)
    assert rc == 2 and "KB_CONSOLIDATE_WT" in out[-1]
    assert calls(paths) == []


def test_atoms_bounded_and_code_reformat():
    n = AT.norm_text
    assert AT.missing_details({"text": "Таймаут 8 с на попытку", "quote": ""}, n("8 строк"), n("x")) == []
    assert AT.missing_details({"text": "таймаут 5 с", "quote": ""}, n("5 с и 15 с"), n("15 с")) == ["5 с"]
    assert AT.claim_present({"text": "`store install <slug> --skills-root /tmp/x`", "quote": ""},
                            "store install `<slug>` --skills-root=/tmp/x")
    assert AT.claim_present({"text": "Лимиты задачи сервера", "quote": ""}, "лимитов задач серверов")
    assert {"8080", "443"} <= set(AT.atoms("порты 8080 443"))
    for t in ("Порт 8080 открыт наружу", "SDK 2.6.0 работает", "ответ 404 Not Found", "флаг `--port=18790`"):
        assert AT.claim_present({"text": t, "quote": ""}, t), t                  # дословный текст находится
    assert AT.claim_present({"text": "флаг `--port=18790`", "quote": ""}, "флаг --port 18790")
    old, new = AT.norm_text("порт 8080 открыт"), AT.norm_text("порт закрыт")
    assert AT.missing_details({"text": "Порт 8080 открыт наружу", "quote": ""}, old, new) == ["8080"]


def test_detail_moved_between_two_updates_regardless_of_order(ck, tmp_path, monkeypatch):
    cfg, paths = _seed_kp_card(ck, tmp_path, monkeypatch)
    _card(paths, "cards/infra/memsvc-notes.md", "memsvc-notes", "заметки memsvc")
    commit_all(paths["root"], "memsvc card")
    _kp_raw(paths, "CLAIM:infra:kp: Для прокси нужен Accept-Encoding: identity.\n")
    run(cfg, areas="infra", stages="1")
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "UPDATE", "name": "infra-kp", "dir": "infra", "type": "fact", "description": "маршруты КП",
         "keys": ["kp"], "claims": ["@Accept-Encoding"],
         "body": "**Суть.** Потолок 8 с, breaker 300 → 1800 с ⟦@Потолок⟧\n\n## Подробности\n"
                 "- Accept-Encoding: identity ⟦@Accept-Encoding⟧"},
        {"op": "UPDATE", "name": "memsvc-notes", "dir": "infra", "type": "fact", "description": "заметки memsvc",
         "keys": ["memsvc"], "claims": ["@memsvc_calls"],
         "body": "**Суть.** старое; задержки — в memsvc_calls.jsonl ⟦@memsvc_calls⟧"}]})
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    assert "Accept-Encoding" in main_card(cfg, "infra-kp").text, "\n".join(out)
    assert "memsvc_calls.jsonl" in main_card(cfg, "memsvc-notes").text
    led = _ledger(cfg)
    ops = {r["op"] for r in led.db.execute("SELECT cc.op FROM claim_cards cc JOIN claims c ON c.id=cc.claim_id "
                                            "WHERE c.text LIKE '%memsvc_calls%' AND cc.card='memsvc-notes'")}
    led.close()
    assert ops & {"MOVED", "UPDATE"}


def test_truncation_drops_unsalvaged_claims_and_accepts_single_huge_unit():
    us = [Unit(f"raw/{n}.md", f"raw/{n}.md", "notes", f"notes:{n}", f"текст единицы {n} общий кусок", "2026-10-01")
          for n in ("a", "b")]
    parsed = [{"unit": "raw/a.md", "quote": "текст единицы a общий", "text": "x"},
              {"unit": "raw/b.md", "quote": "общий кусок", "text": "y"}]
    assert stage1.complete_units(parsed, us) == {"raw/a.md"}
    assert stage1.seen_units(parsed[:1], us) == ["raw/a.md"] and stage1.complete_units(parsed[:1], us) == set()


def test_truncated_single_unit_partial_extraction(ck, monkeypatch):
    cfg, paths = ck
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "truncate")
    run(cfg, stages="1", batch_size=1)                               # в каждом батче одна единица — берётся частично
    led = _ledger(cfg)
    n_ok = led.db.execute("SELECT count(*) FROM extractions WHERE status='ok'").fetchone()[0]
    led.close()
    assert n_ok == 6
    assert last_consolidate(cfg)["stats"]["s1_partial_units"] == 6


def test_projects_without_pending_claims_does_not_crash(ck):
    cfg, paths = ck
    rc, out = run(cfg, areas="projects")
    assert rc == 0, "\n".join(out)
    assert last_consolidate(cfg)["areas"] == []


def test_noop_does_not_overwrite_claim_consolidated_in_same_response(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "ADD", "name": "deploy-x", "dir": "infra", "type": "fact", "description": "деплой",
         "keys": ["x"], "claims": ["@рестарт только"], "body": "**Суть.** Рестарт только app-stable ⟦@рестарт только⟧"},
        {"op": "NOOP", "claims": ["@рестарт только"], "reason": "дубль"}]})
    run(cfg, areas="infra")
    assert _claim_row(cfg, "рестарт только")["status"] == "consolidated"


def test_conflict_line_carries_claim_keys(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    run(cfg, areas="infra")
    write(str(paths["root"] / "raw/claude-mac/-Users-user-cursor/dns.md"),
          "CLAIM:infra:deploy: Быстрый обход — `tailscale set --accept-dns=false`.\n")
    commit_all(paths["root"], "dns")
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "CONFLICT", "name": "infra-deploy", "conflict": "обход DNS против запрета", "claims": ["@Быстрый обход"]}]})
    run(cfg, areas="infra")
    text = main_card(cfg, "infra-deploy").text
    assert "(детали: `tailscale set --accept-dns=false`" in text
    import gate as secret_gate
    assert secret_gate.scan_text(text) == []


def test_conflict_line_keys_do_not_trip_secret_gate():
    from consolidate.apply import _keys_suffix
    import gate as secret_gate
    sfx = _keys_suffix([{"text": "Порог `LIDWORK_LOW_BATT_THRESHOLD=15` для lidwork", "quote": ""}], "конфликт")
    assert "LIDWORK_LOW_BATT_THRESHOLD=15" in sfx and secret_gate.scan_text("- Вариант: x" + sfx) == []


# ---------- хабы: пересборка только при смене состава ----------

def _hub_calls(paths, n0=0):
    return [c for c in calls(paths)[n0:] if c["stage"] == "hub"]


def _hub_batches(cfg, rid):
    return [r for r in kbcore.read_runs(cfg) if r.get("cmd") == "consolidate_batch" and r.get("run_id") == rid
            and r.get("stage") == "2-hub"]


def _add_raw(paths, rel, text):
    write(str(paths["root"] / "raw" / rel), text)
    commit_all(paths["root"], f"raw {rel}")


def test_hub_body_code_update_pure():
    body = ("**Суть.** Главное ⟦c1⟧\n\n## Карточки\n### bb\n- [[a]] — про a\n### VPS\n- [[b]] — про b\n"
            "- [[c]] — про c\n- полный список — cards/infra/_index.md\n\n## История\n- 2026-10-01 — собран\n")
    t, shown, ev = stage2.update_hub_body(body, "infra", add=[("d", "Новая карточка про d: подробности длинные")],
                                          remove={"a"}, relabel={"c": "Свежее описание c"}, note="- 2026-10-25 — код")
    assert t.startswith("**Суть.** Главное ⟦c1⟧")                                # «Суть» не тронута
    assert stage2.hub_links(t) == ["b", "c", "d"] and shown == ["d"] and ev == []
    assert "### bb" not in t                                                     # опустевший подзаголовок снят
    assert "- [[c]] — Свежее описание c" in t and "- [[d]] — Новая карточка про d" in t
    assert t.index("### Новые карточки") < t.index("полный список")
    assert t.rstrip().endswith("- 2026-10-25 — код") and "2026-10-01 — собран" in t
    # повтор с новой карточкой — в тот же подраздел, без второго заголовка
    t2, _, _ = stage2.update_hub_body(t, "infra", add=[("e", "е")])
    assert t2.count("### Новые карточки") == 1 and stage2.hub_links(t2)[-1] == "e"
    assert stage2.update_hub_body("**Суть.** x\n\nсписок\n", "infra", add=[("d", "x")]) is None   # нет раздела


def _full_hub(n_links, label="карточка {i} " + "слово " * 8, fresh=()):
    links = "\n".join(f"- [[c-{i}]] — " + label.format(i=i) for i in range(n_links))
    new = ("\n### Новые карточки\n" + "\n".join(f"- [[{n}]] — новая {n}" for n in fresh)) if fresh else ""
    return f"**Суть.** x ⟦c1⟧\n\n## Карточки\n{links}{new}\n- полный список — cards/infra/_index.md\n\n" \
           f"## История\n- 2026-10-01 — собран\n"


def test_hub_new_cards_evict_oldest_new_and_keep_curated_links():
    """«### Новые карточки» — N последних с вытеснением: прежние ссылки не трогаются, лишние новые — только в _index."""
    body = _full_hub(26, fresh=("old-1", "old-2"))
    assert stage2.trim_hub(body, "infra") == body
    t, shown, ev = stage2.update_hub_body(body, "infra", add=[("n-1", "a"), ("n-2", "b"), ("n-3", "c")],
                                          note="- 2026-10-25 — код")
    links = stage2.hub_links(t)
    assert all(f"c-{i}" in links for i in range(26))                             # отобранные LLM — на месте
    assert stage2.trim_hub(t, "infra") == t                                       # в пределах
    assert ev and ev[0] == "old-1" and "n-3" in shown                              # вытесняются старые новые
    assert set(shown) | set(ev) >= {"n-1", "n-2", "n-3"} - set(shown) and not set(ev) & set(links)
    # важную (must) не вытесняют; прежние не помещаются вовсе — None (→ LLM)
    t, shown, ev = stage2.update_hub_body(body, "infra", add=[("core-x", "a"), ("n-2", "b"), ("n-3", "c")],
                                          must={"core-x"})
    assert "core-x" in stage2.hub_links(t)
    assert stage2.update_hub_body(_full_hub(36), "infra", add=[("core-x", "a")], must={"core-x"}) is None
    rm = stage2.update_hub_body(_full_hub(30), "infra", remove={"c-0"})
    assert rm is not None and "c-0" not in stage2.hub_links(rm[0])                # снятие — кодом


def test_hub_relabel_never_drops_existing_links_or_grows_label():
    """Смена подписи не выкидывает прежние карточки: рост → ужатие срезало бы хвост — тогда None (→ LLM)."""
    links = [f"- [[card-{i:02d}]] — к" for i in range(30)]
    body = "**Суть.** x ⟦c1⟧\n\n## Карточки\n" + "\n".join(links) + "\n\n## История\n- 2026-10-01 — собран\n"
    pad = 4200 - len(body.encode()) - 4
    body = body.replace("**Суть.** x", "**Суть.** x" + "ж" * (pad // 2), 1)
    assert len(body.encode()) <= 4200 and stage2.trim_hub(body, "infra") == body
    long_desc = "Очень длинное новое описание карточки, которое раньше раздувало подпись строки хаба"
    assert stage2.update_hub_body(body, "infra", relabel={"card-00": long_desc}) is None
    roomy = "**Суть.** x ⟦c1⟧\n\n## Карточки\n- [[a]] — коротко\n- [[b]] — про b\n\n## История\n- 2026-10-01 — x\n"
    t, _, _ = stage2.update_hub_body(roomy, "infra", relabel={"a": long_desc})
    lab = [ln for ln in t.splitlines() if ln.startswith("- [[a]]")][0].split("]] — ")[1]
    assert len(lab) <= 24 and stage2.hub_links(t) == ["a", "b"]           # подпись не длиннее max(старой, 24)
    # подпись с `_index.md` — не служебная строка; служебная от LLM в другом написании — служебная
    odd = ("**Суть.** x\n\n## Карточки\n- [[a]] — правка cards/x/_index.md руками\n"
           "- **Полный список:** `cards/infra/_index.md`\n")
    t, _, _ = stage2.update_hub_body(odd, "infra", add=[("n", "новая")])
    assert stage2.hub_links(t) == ["a", "n"] and t.index("[[n]]") < t.index("Полный список")



def test_hub_untouched_when_card_body_updated_without_description_change(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    assert run(cfg)[0] == 0
    rec = last_consolidate(cfg)
    assert rec["hubs"]["llm"] == 4 and rec["stats"]["hubs_llm"] == 4                      # хабов не было — LLM
    hub0 = main_card(cfg, "infra-hub").text
    desc = main_card(cfg, "infra-deploy").description
    n0 = len(calls(paths))
    _add_raw(paths, "claude-vps/-home-app/project_deploy2.md",
             "CLAIM:infra:deploy: Проверка выкатки — journalctl без Traceback.\n")
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "UPDATE", "name": "infra-deploy", "dir": "infra", "type": "fact", "description": desc,
         "keys": ["deploy"], "claims": ["@journalctl"],
         "body": "**Суть.** Деплой Stable — рестарт только app-stable. ⟦@рестарт только⟧\n"
                 "Проверка выкатки — journalctl без Traceback. ⟦@journalctl⟧"}]})
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    assert "journalctl" in main_card(cfg, "infra-deploy").text
    assert _hub_calls(paths, n0) == []                                                    # LLM на хаб не звали
    assert main_card(cfg, "infra-hub").text == hub0                                       # хаб не тронут
    rec = last_consolidate(cfg)
    assert rec["hubs"] == {"llm": 0, "code": 0, "skipped": 1, "pending": 0, "llm_failed": 0}
    assert "infra-hub" not in rec["cards_touched"]


def test_hub_lists_new_card_by_code_without_llm(ck):
    cfg, paths = ck
    assert run(cfg)[0] == 0
    sut0 = main_card(cfg, "infra-hub").body.strip().splitlines()[0]
    n0 = len(calls(paths))
    _add_raw(paths, "claude-vps/-home-app/project_proxy.md", "CLAIM:infra:proxy: Прокси на ноутбуке — mihomo на 7890.\n")
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    assert main_card(cfg, "infra-proxy") is not None
    hub = main_card(cfg, "infra-hub")
    assert stage2.hub_links(hub.body) == ["infra-deploy", "infra-proxy"]                  # добавили карточку → она в хабе
    assert "### Новые карточки" in hub.body and hub.body.strip().splitlines()[0] == sut0
    assert "список обновлён кодом" in hub.body and "⟦" not in hub.text
    assert _hub_calls(paths, n0) == []
    rec = last_consolidate(cfg)
    assert rec["hubs"]["code"] == 1 and rec["hubs"]["llm"] == 0 and "infra-hub" in rec["cards_touched"]
    b = _hub_batches(cfg, rec["run_id"])
    assert len(b) == 1 and b[0]["mode"] == "code" and b[0]["added"] == ["infra-proxy"] and b[0]["tokens_in"] == 0
    assert rec["gates"]["ok"]


def test_hub_drops_superseded_card_and_lists_replacement(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    assert run(cfg)[0] == 0
    n0 = len(calls(paths))
    _add_raw(paths, "claude-vps/-home-app/project_deploy2.md",
             "CLAIM:infra:deploy: Новый порядок выкатки — journalctl без Traceback.\n")
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "SUPERSEDE", "name": "deploy-v2", "dir": "infra", "type": "fact", "description": "Новый деплой Stable",
         "keys": ["deploy"], "supersedes": ["infra-deploy"], "claims": ["@journalctl"],
         "body": "**Суть.** Новый порядок выкатки — journalctl без Traceback. ⟦@journalctl⟧\n"
                 "Деплой Stable — рестарт только app-stable. ⟦@рестарт только⟧"}]})
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    assert main_card(cfg, "infra-deploy").status == "superseded"
    assert stage2.hub_links(main_card(cfg, "infra-hub").body) == ["deploy-v2"]
    assert _hub_calls(paths, n0) == []


def test_hub_rebuilt_by_llm_when_new_cards_do_not_fit(ck, monkeypatch):
    cfg, paths = ck
    assert run(cfg)[0] == 0
    n0 = len(calls(paths))
    real = stage2.trim_hub
    monkeypatch.setattr(stage2, "trim_hub", lambda body, area, **kw: real(body, area, max_lines=6))
    _add_raw(paths, "claude-vps/-home-app/project_proxy.md", "CLAIM:infra:proxy: Прокси на ноутбуке — mihomo на 7890.\n")
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    assert len(_hub_calls(paths, n0)) == 1
    rec = last_consolidate(cfg)
    assert rec["hubs"]["llm"] == 1 and rec["hubs"]["code"] == 0
    b = _hub_batches(cfg, rec["run_id"])
    assert b[0]["mode"] == "llm" and "не помещается" in b[0]["reason"]


def test_stale_hub_llm_failure_falls_back_to_code(ck, monkeypatch):
    """Хаб устарел (review_after) → LLM; LLM упал → список всё равно правится кодом, сбой честно в журнале."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    p = os.path.join(cfg.cards, "infra", "infra-hub.md")
    txt = open(p, encoding="utf-8").read()
    open(p, "w", encoding="utf-8").write(re.sub(r"review_after: \S+", "review_after: 2020-01-01", txt))
    commit_all(paths["root"], "хаб устарел")
    n0 = len(calls(paths))
    _add_raw(paths, "claude-vps/-home-app/project_proxy.md", "CLAIM:infra:proxy: Прокси на ноутбуке — mihomo на 7890.\n")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage-hub")
    rc, out = run(cfg, areas="infra")
    assert len(_hub_calls(paths, n0)) == 1
    rec = last_consolidate(cfg)
    assert rec["status"] == "partial" and any(e.startswith("hub infra") for e in rec["errors"])
    assert rec["hubs"]["llm_failed"] == 1 and rec["hubs"]["code"] == 1
    assert "infra-proxy" in stage2.hub_links(main_card(cfg, "infra-hub").body)
    b = _hub_batches(cfg, rec["run_id"])
    assert {x["mode"] for x in b} == {"llm", "code"} and "устарел" in b[0]["reason"]


# ---------- порог единиц (решение владельца: «сводку можно от трёх») ----------

def test_min_units_defers_below_three_and_runs_at_three(ck):
    cfg, paths = ck
    rc, out = run(cfg, min_units=3)                       # 6 единиц ≥ 3 — обычный прогон
    assert rc == 0 and last_consolidate(cfg)["status"] == "ok", "\n".join(out)
    n0 = len(calls(paths))
    _add_raw(paths, "claude-vps/-home-app/p1.md", "CLAIM:infra:p1: Первый факт про порт 8001.\n")
    _add_raw(paths, "claude-vps/-home-app/p2.md", "CLAIM:infra:p2: Второй факт про порт 8002.\n")
    for _ in range(2):                                    # 2 единицы → отложено; на следующий день они снова в очереди
        rc, out = run(cfg, min_units=3)
        assert rc == 0, "\n".join(out)
        rec = last_consolidate(cfg)
        assert rec["status"] == "deferred" and rec["llm_calls"] == 0 and len(calls(paths)) == n0
        assert rec["deferred"]["units"] == 2 and rec["deferred"]["min_units"] == 3 and rec["stats"]["units_to_llm"] == 2
        assert "порога 3" in rec["deferred"]["reason"] and not rec.get("cards_touched")
    _add_raw(paths, "claude-vps/-home-app/p3.md", "CLAIM:infra:p3: Третий факт про порт 8003.\n")
    rc, out = run(cfg, min_units=3)                       # 3 единицы → прогон
    assert rc == 0, "\n".join(out)
    rec = last_consolidate(cfg)
    assert rec["status"] == "ok" and rec["stats"]["units_to_llm"] == 3 and rec.get("deferred") is None
    assert [c["stage"] for c in calls(paths)[n0:] if c["stage"] == "s1"] == ["s1"]
    assert all(main_card(cfg, f"infra-p{i}") is not None for i in (1, 2, 3))


def test_min_units_zero_runs_single_unit(ck):
    """Ручной kb consolidate и недельный — без порога: одна единица сводится."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    n0 = len(calls(paths))
    _add_raw(paths, "claude-vps/-home-app/p1.md", "CLAIM:infra:p1: Единственный факт про порт 8001.\n")
    rc, out = run(cfg)
    assert rc == 0, "\n".join(out)
    rec = last_consolidate(cfg)
    assert rec["status"] == "ok" and rec["min_units"] == 0 and rec["llm_calls"] > 0
    assert len(calls(paths)) > n0 and main_card(cfg, "infra-p1") is not None


def test_min_units_counts_rejected_retries(ck):
    """Порог смотрит на то, что пошло бы в LLM: повторы отклонённого (стадия 2) — тоже единицы."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    led = Ledger(os.path.join(cfg.state, "ledger.sqlite"))
    uids = [r["uid"] for r in led.db.execute(
        "SELECT DISTINCT uid FROM claims WHERE status='consolidated' AND area='infra' OR (status='consolidated' "
        "AND area='product') ORDER BY uid LIMIT 2")]
    assert len(uids) == 2
    led.db.execute("UPDATE claims SET status='rejected', attempts=1 WHERE status='consolidated' AND uid IN (?,?)",
                   uids)
    led.db.commit()
    led.close()
    _add_raw(paths, "claude-vps/-home-app/p1.md", "CLAIM:infra:p1: Факт про порт 8001.\n")
    rc, out = run(cfg, min_units=3)
    rec = last_consolidate(cfg)
    assert rec["stats"]["units_to_llm"] == 3 and rec["status"] != "deferred", "\n".join(out)


def test_min_units_dry_run_reports_deferred(ck):
    cfg, paths = ck
    assert run(cfg)[0] == 0
    n0 = len(calls(paths))
    _add_raw(paths, "claude-vps/-home-app/p1.md", "CLAIM:infra:p1: Факт про порт 8001.\n")
    rc, out = run(cfg, dry_run=True, min_units=3)
    assert rc == 0 and len(calls(paths)) == n0
    rec = last_consolidate(cfg)
    assert rec["status"] == "deferred" and rec["mode"] == "dry-run" and rec["deferred"]["units"] == 1
    assert any("отложено" in ln for ln in out)
    assert len([r for r in kbcore.read_runs(cfg) if r.get("run_id") == rec["run_id"]
                and r.get("cmd") == "consolidate"]) == 1                     # одна запись на прогон, не две


# ---------- хабы: находки ревью (точка отсчёта — последняя запись хаба) ----------

def _manual_card(paths, rel, name, desc="Ручная карточка из другой сессии"):
    _card(paths, rel, name, desc)
    commit_all(paths["root"], f"ручная карточка {name}")


def test_hub_picks_up_card_added_in_main_and_is_idempotent(ck):
    cfg, paths = ck
    assert run(cfg)[0] == 0
    n0 = len(calls(paths))
    _manual_card(paths, "cards/infra/manual-card.md", "manual-card")
    rc, out = run(cfg, areas="infra")                       # сырья нет — LLM не нужен, хаб правится кодом
    assert rc == 0, "\n".join(out)
    hub = main_card(cfg, "infra-hub")
    assert "manual-card" in stage2.hub_links(hub.body) and len(calls(paths)) == n0
    assert last_consolidate(cfg)["hubs"]["code"] == 1
    rc, out = run(cfg, areas="infra")                       # третий прогон: ничего не менялось
    rec = last_consolidate(cfg)
    assert rc == 0 and rec["hubs"]["code"] == 0 and rec["hubs"]["skipped"] == 1
    assert main_card(cfg, "infra-hub").text == hub.text


def test_hub_change_not_applied_is_retried_next_run(ck, monkeypatch):
    """Правка не применилась (не влезла, LLM в лимите) — хаб не тронут, pending; следующий прогон её делает."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    hub0 = main_card(cfg, "infra-hub").text
    real = stage2.trim_hub
    monkeypatch.setattr(stage2, "trim_hub", lambda body, area, **kw: real(body, area, max_lines=6))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit-hub")
    _add_raw(paths, "claude-vps/-home-app/project_proxy.md", "CLAIM:infra:proxy: Прокси на ноутбуке — mihomo на 7890.\n")
    run(cfg, areas="infra")
    rec = last_consolidate(cfg)
    assert rec["status"] == "limit" and rec["hubs"]["pending"] == 1 and rec["hubs"]["code"] == 0
    assert main_card(cfg, "infra-proxy") is not None and main_card(cfg, "infra-hub").text == hub0
    b = _hub_batches(cfg, rec["run_id"])
    assert [x["status"] for x in b] == ["limit", "pending"] and b[1]["added"] == []
    assert b[1]["pending_added"] == ["infra-proxy"]
    monkeypatch.setattr(stage2, "trim_hub", real)
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    assert "infra-proxy" in stage2.hub_links(main_card(cfg, "infra-hub").body)
    assert last_consolidate(cfg)["hubs"]["code"] == 1


def test_hub_drops_deleted_card_and_card_moved_to_other_area(ck):
    cfg, paths = ck
    assert run(cfg)[0] == 0
    _manual_card(paths, "cards/infra/manual-a.md", "manual-a")
    _manual_card(paths, "cards/infra/manual-b.md", "manual-b")
    assert run(cfg, areas="infra,agents")[0] == 0
    assert {"manual-a", "manual-b"} <= set(stage2.hub_links(main_card(cfg, "infra-hub").body))
    os.remove(paths["root"] / "cards/infra/manual-a.md")                       # снятая без замены
    os.rename(paths["root"] / "cards/infra/manual-b.md", paths["root"] / "cards/agents/manual-b.md")
    commit_all(paths["root"], "удалили и перенесли")
    rc, out = run(cfg, areas="infra,agents")
    assert rc == 0, "\n".join(out)
    infra = stage2.hub_links(main_card(cfg, "infra-hub").body)
    assert "manual-a" not in infra and "manual-b" not in infra and "manual-a" not in main_card(cfg, "infra-hub").text
    assert "manual-b" in stage2.hub_links(main_card(cfg, "agents-hub").body)   # пришла в другую область — новая там


def test_hub_relabel_by_code_and_private_link_kept(ck):
    cfg, paths = ck
    assert run(cfg)[0] == 0
    # приватная карточка (только в основном checkout) в хабе остаётся
    write(os.path.join(cfg.cards, "private/priv-note.md"), card_text_private := (
        "---\nname: priv-note\ndescription: личное\ntype: fact\nstatus: active\nkeys: [x]\nsources: [raw/a.md]\n"
        "verified: 2026-10-01\nverified_how: source\nreview_after: 2026-12-01\nsensitivity: private\n---\n\n**Суть.** x\n"))
    assert card_text_private
    p = os.path.join(cfg.cards, "infra/infra-hub.md")
    txt = open(p, encoding="utf-8").read().replace("## Карточки\n", "## Карточки\n- [[priv-note]] — своё\n", 1)
    open(p, "w", encoding="utf-8").write(txt)
    d = os.path.join(cfg.cards, "infra/infra-deploy.md")
    t = open(d, encoding="utf-8").read()
    open(d, "w", encoding="utf-8").write(re.sub(r"^description: .*$", "description: Выкатка Stable — только app-stable",
                                                t, count=1, flags=re.M))
    commit_all(paths["root"], "хаб с приватной ссылкой; новое описание")
    # точка отсчёта — запись хаба выше, где описание уже новое; меняем описание ещё раз отдельным коммитом
    t = open(d, encoding="utf-8").read()
    open(d, "w", encoding="utf-8").write(t.replace("description: Выкатка Stable — только app-stable",
                                                   "description: Деплой бота: рестарт app-stable"))
    commit_all(paths["root"], "описание сменилось")
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    hub = main_card(cfg, "infra-hub")
    assert "- [[infra-deploy]] — Деплой бота" in hub.body
    assert "priv-note" in stage2.hub_links(hub.body)
    rec = last_consolidate(cfg)
    assert rec["hubs"]["code"] == 1 and _hub_batches(cfg, rec["run_id"])[0]["relabeled"] == ["infra-deploy"]


def test_project_area_hub_updated_by_code(ck):
    cfg, paths = ck
    _card(paths, "cards/projects/blog/yt-a.md", "yt-a", "Клипы: нарезка")
    write(str(paths["root"] / "cards/projects/blog/projects-blog-hub.md"),
          "---\nname: projects-blog-hub\ndescription: Хаб blog\ntype: topic\nstatus: active\nkeys: [blog]\n"
          "sources: [raw/a.md]\nverified: 2026-10-01\nverified_how: source\nreview_after: 2099-01-01\n"
          "sensitivity: internal\n---\n\n**Суть.** Клипы.\n\n## Карточки\n- [[yt-a]] — нарезка\n\n## История\n- 2026-10-01 — x\n")
    commit_all(paths["root"], "проект")
    _add_raw(paths, "claude-vps/-home-blog/p.md", "CLAIM:projects/blog:clips: Клипы режутся по таймкоду.\n")
    rc, out = run(cfg, areas="infra,projects")
    assert rc == 0, "\n".join(out)
    assert stage2.hub_links(main_card(cfg, "projects-blog-hub").body) == ["yt-a", "projects-blog-clips"]
    assert not [c for c in calls(paths) if c["stage"] == "hub" and "projects/blog" in json.dumps(c)]


def test_superseded_hub_left_alone(ck):
    cfg, paths = ck
    assert run(cfg)[0] == 0
    p = os.path.join(cfg.cards, "infra/infra-hub.md")
    txt = open(p, encoding="utf-8").read().replace("status: active", "status: superseded\nsuperseded_by: [infra-deploy]")
    open(p, "w", encoding="utf-8").write(txt)
    commit_all(paths["root"], "хаб заменён")
    _add_raw(paths, "claude-vps/-home-app/project_proxy.md", "CLAIM:infra:proxy: Прокси на ноутбуке — mihomo на 7890.\n")
    assert run(cfg, areas="infra")[0] == 0
    assert main_card(cfg, "infra-proxy") is not None and "infra-proxy" not in main_card(cfg, "infra-hub").text


def test_min_units_zero_pending_is_no_units_not_fresh_k1(ck):
    """Порог включён, а к LLM ничего нет — no_units: не сбой и не «свежий К1» для субботнего недельного."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    rc, out = run(cfg, min_units=3)
    rec = last_consolidate(cfg)
    assert rc == 0 and rec["status"] == "no_units" and rec["llm_calls"] == 0 and rec.get("deferred") is None
    assert not [p for p in health.compute(cfg)["problems"] if "сводчик" in p["what"]]


# ---------- хабы: база в state (третий раунд ревью) ----------

def _edit_hub(cfg, paths, fn, msg):
    p = os.path.join(cfg.cards, "infra/infra-hub.md")
    new = fn(open(p, encoding="utf-8").read())
    open(p, "w", encoding="utf-8").write(new)
    commit_all(paths["root"], msg)


def test_hub_base_survives_unlink_commit_in_no_units_run(ck):
    """Б1: прогон ниже порога (no_units) коммитит хаб через unlink_missing — база хаба от этого не двигается,
    шаг хабов идёт и тут (кодом, без LLM): ручная карточка попадает в хаб, мёртвая ссылка снимается."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    n0 = len(calls(paths))
    _edit_hub(cfg, paths, lambda t: t.replace("## Карточки\n", "## Карточки\n- [[old-x]] — мёртвая\n", 1), "мёртвая")
    _manual_card(paths, "cards/infra/manual-card.md", "manual-card")
    rc, out = run(cfg, areas="infra", min_units=3)
    rec = last_consolidate(cfg)
    assert rc == 0 and rec["status"] == "no_units" and len(calls(paths)) == n0, "\n".join(out)
    links = stage2.hub_links(main_card(cfg, "infra-hub").body)
    assert "manual-card" in links and "old-x" not in links and rec["hubs"]["code"] == 1
    text = main_card(cfg, "infra-hub").text
    assert run(cfg, areas="infra", min_units=3)[0] == 0 and main_card(cfg, "infra-hub").text == text
    assert os.path.exists(os.path.join(cfg.state, "hubs", "infra.json"))


def test_hub_manual_edit_does_not_move_base(ck):
    """В1: ручная правка хаба после появления карточки не делает карточку «увиденной»."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    _manual_card(paths, "cards/infra/manual-z.md", "manual-z")
    _edit_hub(cfg, paths, lambda t: t.replace("главное", "главное (правка руками)", 1), "ручная правка хаба")
    assert run(cfg, areas="infra")[0] == 0
    assert "manual-z" in stage2.hub_links(main_card(cfg, "infra-hub").body)


def test_hub_failed_llm_plan_not_retried_until_plan_changes(ck, monkeypatch):
    """В2: правка кодом невозможна, LLM на этом плане устойчиво отказал (не JSON) — с тем же планом LLM не зовём;
    план изменился — снова можно. Системный сбой (не ответ модели) — повтор не раньше завтра даже с новым планом."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    real = stage2.trim_hub
    monkeypatch.setattr(stage2, "trim_hub", lambda body, area, **kw: real(body, area, max_lines=6))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "badjson-hub")
    _manual_card(paths, "cards/infra/manual-a.md", "manual-a")
    n0 = len(calls(paths))
    rc, out = run(cfg, areas="infra")
    assert len(_hub_calls(paths, n0)) == 1 and last_consolidate(cfg)["hubs"]["pending"] == 1, "\n".join(out)
    n1 = len(calls(paths))
    run(cfg, areas="infra")
    rec = last_consolidate(cfg)
    assert _hub_calls(paths, n1) == [] and rec["hubs"]["pending"] == 1 and rec["status"] == "ok"
    _manual_card(paths, "cards/infra/manual-zz.md", "manual-zz", "Совсем другая тема про порты")   # план изменился
    rc, out = run(cfg, areas="infra")
    assert len(_hub_calls(paths, n1)) == 1, "\n".join(out)
    # системный сбой: повтор не сегодня, даже на новом плане
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage-hub")
    _manual_card(paths, "cards/infra/manual-yy.md", "manual-yy", "Ещё одна тема про диски")
    n2 = len(calls(paths))
    run(cfg, areas="infra")
    assert len(_hub_calls(paths, n2)) == 1
    _manual_card(paths, "cards/infra/manual-xx.md", "manual-xx", "Тема про сертификаты TLS")
    n3 = len(calls(paths))
    run(cfg, areas="infra")
    assert _hub_calls(paths, n3) == []


def test_hub_llm_regroup_after_churn_threshold(ck, monkeypatch):
    """В3: мелкие изменения — кодом; накопилось HUB_LLM_CHURN новых и снятых с прошлой пересборки — LLM."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    monkeypatch.setattr(crun, "HUB_LLM_CHURN", 2)
    n0 = len(calls(paths))
    _manual_card(paths, "cards/infra/manual-a.md", "manual-a")
    assert run(cfg, areas="infra")[0] == 0
    assert last_consolidate(cfg)["hubs"]["code"] == 1 and _hub_calls(paths, n0) == []
    _manual_card(paths, "cards/infra/manual-b.md", "manual-b")
    assert run(cfg, areas="infra")[0] == 0
    rec = last_consolidate(cfg)
    assert rec["hubs"]["llm"] == 1 and len(_hub_calls(paths, n0)) == 1
    assert "накопилось" in _hub_batches(cfg, rec["run_id"])[0]["reason"]
    base = json.load(open(os.path.join(cfg.state, "hubs", "infra.json")))
    assert base["churn"] == 0 and "manual-b" in base["cards"]


# ---------- хабы: третий раунд ревью ----------

def test_hub_base_confirmed_only_after_merge_dropped_with_branch(ck, monkeypatch):
    """1: хаб записан в ветке, гейты упали, ветку удалили — база не считает карточку «увиденной»: она в хабе main."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    real = crun.G.run_gates

    def failing(*a, **kw):
        g = real(*a, **kw)
        g["ok"] = False
        return g
    monkeypatch.setattr(crun.G, "run_gates", failing)
    _manual_card(paths, "cards/infra/manual-x.md", "manual-x")
    run(cfg, areas="infra")
    rec = last_consolidate(cfg)
    assert rec["status"] == "gates_failed" and "manual-x" not in main_card(cfg, "infra-hub").text
    base = json.load(open(os.path.join(cfg.state, "hubs", "infra.json")))
    assert "manual-x" not in base["cards"] and "manual-x" in base["pending"]["cards"]
    br = rec["branch"]
    git(paths["root"], "worktree", "remove", "--force", str(paths["wt"]))
    git(paths["root"], "branch", "-D", br)                                     # разблокировали: ветку удалили
    monkeypatch.setattr(crun.G, "run_gates", real)
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    assert "manual-x" in stage2.hub_links(main_card(cfg, "infra-hub").body)
    base = json.load(open(os.path.join(cfg.state, "hubs", "infra.json")))
    assert base.get("pending") is None and "manual-x" in base["cards"]           # подтверждена после слияния


def test_hubs_dir_of_experiment_never_the_main_one(tmp_path, monkeypatch):
    """2: эксперимент с KB_LEDGER=state/ledger-x.sqlite не пишет в боевую state/hubs."""
    cfg = kbcore.Cfg()
    monkeypatch.delenv("KB_LEDGER", raising=False)
    assert crun.hubs_dir(cfg) == os.path.join(cfg.state, "hubs")
    monkeypatch.setenv("KB_LEDGER", os.path.join(cfg.state, "ledger-x.sqlite"))
    assert crun.hubs_dir(cfg) == os.path.join(cfg.state, "ledger-x.hubs")
    monkeypatch.setenv("KB_LEDGER", str(tmp_path / "exp" / "ledger.sqlite"))
    assert crun.hubs_dir(cfg) == str(tmp_path / "exp" / "ledger.hubs")


def _core_card(paths, rel, name, desc):
    _card(paths, rel, name, desc, extra="core: true\nimportance: high\n")
    commit_all(paths["root"], f"core {name}")


def test_hub_core_card_in_new_section_not_evicted_next_run(ck, monkeypatch):
    """4: core-карточка, уже стоящая в «### Новые карточки», не вытесняется следующими новыми."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    _core_card(paths, "cards/infra/core-one.md", "core-one", "Жёсткое правило про периметр")
    assert run(cfg, areas="infra")[0] == 0
    hub = main_card(cfg, "infra-hub")
    assert "core-one" in stage2.hub_links(hub.body)
    n_lines = len(hub.body.strip().splitlines())
    real = stage2.trim_hub
    monkeypatch.setattr(stage2, "trim_hub", lambda body, area, **kw: real(body, area, max_lines=n_lines + 1))
    for i, topic in enumerate(["порты", "диски", "сертификаты"]):
        _manual_card(paths, f"cards/infra/plain-{i}.md", f"plain-{i}", f"Обычная карточка про {topic}")
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    links = stage2.hub_links(main_card(cfg, "infra-hub").body)
    assert "core-one" in links and "infra-deploy" in links
    b = _hub_batches(cfg, last_consolidate(cfg)["run_id"])[-1]
    assert b["mode"] == "code" and b["only_index"] and "core-one" not in b["evicted"]


def test_full_hub_not_rewritten_when_no_new_card_shown(ck, monkeypatch):
    """5: место есть только под строку истории — ни одна новая не показана; хаб не переписывается."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    hub0 = main_card(cfg, "infra-hub").text
    n_lines = len(main_card(cfg, "infra-hub").body.strip().splitlines())
    real = stage2.trim_hub
    monkeypatch.setattr(stage2, "trim_hub", lambda body, area, **kw: real(body, area, max_lines=n_lines + 1))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "badjson-hub")
    _manual_card(paths, "cards/infra/manual-a.md", "manual-a")
    run(cfg, areas="infra")
    assert main_card(cfg, "infra-hub").text == hub0
    assert last_consolidate(cfg)["hubs"]["pending"] == 1


def test_llm_hub_trimmed_to_prompt_budget(ck):
    """6: хаб от LLM ужимается до 31 строки — остальное место под новые карточки."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    for i in range(36):
        _card(paths, f"cards/infra/bulk-{i:02d}.md", f"bulk-{i:02d}", f"Пакетная карточка номер {i} про тему {i}")
    commit_all(paths["root"], "много карточек")
    rc, out = run(cfg, areas="infra")
    rec = last_consolidate(cfg)
    assert rec["hubs"]["llm"] == 1, "\n".join(out)
    body = main_card(cfg, "infra-hub").body
    assert len(body.strip().splitlines()) <= 31 and len(body.encode()) <= 3200


def test_project_hub_without_pending_claims_gets_manual_card(ck):
    cfg, paths = ck
    write(str(paths["root"] / "cards/projects/blog/projects-blog-hub.md"),
          "---\nname: projects-blog-hub\ndescription: Хаб blog\ntype: topic\nstatus: active\nkeys: [blog]\n"
          "sources: [raw/a.md]\nverified: 2026-10-01\nverified_how: source\nreview_after: 2099-01-01\n"
          "sensitivity: internal\n---\n\n**Суть.** Клипы.\n\n## Карточки\n\n## История\n- 2026-10-01 — x\n")
    commit_all(paths["root"], "хаб проекта")
    assert run(cfg, areas="infra,projects")[0] == 0          # база хаба проекта заведена
    _card(paths, "cards/projects/blog/yt-manual.md", "yt-manual", "Ручная карточка проекта")
    commit_all(paths["root"], "ручная карточка проекта")
    rc, out = run(cfg, areas="infra,projects")
    assert rc == 0, "\n".join(out)
    assert last_consolidate(cfg)["areas"] == ["infra"]       # утверждений по проекту нет
    assert "yt-manual" in stage2.hub_links(main_card(cfg, "projects-blog-hub").body)


# ---------- хабы: четвёртый раунд ревью ----------

def test_failure_stub_is_not_a_base_for_later_manual_hub(ck, monkeypatch):
    """Заглушка памяти о неудаче (хаба не было, LLM отказал) — не база: хаб, появившийся потом руками, не получает
    в «Новые карточки» все старые карточки области — работает разовая база из git."""
    cfg, paths = ck
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "badjson-hub")
    run(cfg, areas="infra")
    assert main_card(cfg, "infra-hub") is None
    stub = json.load(open(os.path.join(cfg.state, "hubs", "infra.json")))
    assert "cards" not in stub and stub["failed_sig"] is None and stub["llm_failed_at"]
    for i in range(4):
        _card(paths, f"cards/infra/old-{i}.md", f"old-{i}", f"Старая карточка {i} про тему {i}")
    write(str(paths["root"] / "cards/infra/infra-hub.md"),
          "---\nname: infra-hub\ndescription: Хаб infra руками\ntype: topic\nstatus: active\nkeys: [infra]\n"
          "sources: [raw/a.md]\nverified: 2026-10-01\nverified_how: source\nreview_after: 2099-01-01\n"
          "sensitivity: internal\n---\n\n**Суть.** Руками.\n\n## Карточки\n- [[infra-deploy]] — деплой\n\n"
          "## История\n- 2026-10-01 — руками\n")
    commit_all(paths["root"], "хаб руками")
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    hub0 = main_card(cfg, "infra-hub").text
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    assert main_card(cfg, "infra-hub").text == hub0 and last_consolidate(cfg)["hubs"]["skipped"] == 1


def test_pending_base_confirmed_by_commit_even_if_hub_changed_after_save(ck, monkeypatch):
    """Ожидающая база подтверждается по коммиту ветки: unlink_missing после записи хаба в том же прогоне,
    гейты упали, на следующий день ветку слили — карточка в подтверждённой базе."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    _edit_hub(cfg, paths, lambda t: t.replace("## История", "Смотри также [[ghost-card]].\n\n## История", 1), "призрак")
    real = crun.G.run_gates

    def failing(*a, **kw):
        g = real(*a, **kw)
        g["ok"] = False
        return g
    monkeypatch.setattr(crun.G, "run_gates", failing)
    _manual_card(paths, "cards/infra/manual-x.md", "manual-x")
    run(cfg, areas="infra")
    assert last_consolidate(cfg)["status"] == "gates_failed"
    monkeypatch.setattr(crun.G, "run_gates", real)
    rc, out = run(cfg, areas="infra")
    assert rc == 0 and last_consolidate(cfg).get("merged"), "\n".join(out)
    base = json.load(open(os.path.join(cfg.state, "hubs", "infra.json")))
    assert "manual-x" in base["cards"] and not base.get("pending")
    assert "manual-x" in stage2.hub_links(main_card(cfg, "infra-hub").body)


def test_llm_gate_error_window_12h_and_hub_tmp_cleanup(ck, monkeypatch):
    cfg, _ = ck
    r = crun.Run(cfg, ["infra"], None, out=lambda *a: None)
    r._write_hub_file("infra", {"llm_error_at": time.time() - 3600})
    assert r.llm_gate("infra", "s", optional=False)[0] is False
    r._write_hub_file("infra", {"llm_error_at": time.time() - 13 * 3600})
    assert r.llm_gate("infra", "s", optional=False)[0] is True              # суббота: weekly после daily — можно
    monkeypatch.setattr(crun.json, "dump", lambda *a, **k: (_ for _ in ()).throw(OSError("диск")))
    with pytest.raises(OSError):
        r._write_hub_file("infra", {"x": 1})
    assert not [f for f in os.listdir(os.path.join(cfg.state, "hubs")) if f.endswith(".tmp")]


def test_health_flags_hub_llm_failing_over_week(ck):
    """Горит: отказы подряд > 7 дней и последний свежий — и у области без хаба (failed_sig=None).
    Гаснет: LLM давно не звали (хаб ведётся кодом). Битый файл базы health не роняет."""
    cfg, _ = ck
    d = lambda n: (dt.date.today() - dt.timedelta(days=n)).isoformat()  # noqa: E731
    hb = os.path.join(cfg.state, "hubs", "infra.json")
    hub_flags = lambda: [p for p in health.compute(cfg)["problems"] if p["what"].startswith("хаб infra")]  # noqa: E731
    write(hb, json.dumps({"failed_sig": None, "llm_failed_since": d(9), "llm_failed_at": d(1)}))
    assert hub_flags() and hub_flags()[0]["what"].startswith("хаб infra: пересборка LLM отказывает")
    write(hb, json.dumps({"failed_sig": "abc", "llm_failed_since": d(30), "llm_failed_at": d(20)}))
    assert not hub_flags()                                               # перестали звать — погас
    write(hb, json.dumps({"failed_sig": "abc", "llm_failed_since": TODAY, "llm_failed_at": TODAY}))
    assert not hub_flags()
    for bad in ("[1, 2]", "не json", json.dumps({"llm_failed_since": "вчера", "llm_failed_at": "x"})):
        write(hb, bad)
        assert not hub_flags()


def test_partial_save_after_error_stamps_hub_base(ck, monkeypatch):
    """Сбой после записи хаба: частичный коммит ветки — ожидающая база привязана к нему, не выбрасывается."""
    cfg, paths = ck
    assert run(cfg)[0] == 0
    _manual_card(paths, "cards/infra/manual-p.md", "manual-p")
    monkeypatch.setattr(crun.Run, "unlink_missing", lambda self, cfg_wt: (_ for _ in ()).throw(RuntimeError("бах")))
    run(cfg, areas="infra")
    assert last_consolidate(cfg)["status"] == "error"
    pend = json.load(open(os.path.join(cfg.state, "hubs", "infra.json")))["pending"]
    assert pend["commit"] and "manual-p" in pend["cards"]


def test_hub_relabel_only_when_label_changes(ck):
    cfg, paths = ck
    assert run(cfg)[0] == 0
    _manual_card(paths, "cards/infra/manual-l.md", "manual-l", "Ручная карточка про порты: старое пояснение")
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    hub = main_card(cfg, "infra-hub").text
    d = os.path.join(cfg.cards, "infra/manual-l.md")
    t = open(d, encoding="utf-8").read().replace("старое пояснение", "новое пояснение")
    open(d, "w", encoding="utf-8").write(t)
    commit_all(paths["root"], "хвост описания")
    assert run(cfg, areas="infra")[0] == 0
    assert main_card(cfg, "infra-hub").text == hub and last_consolidate(cfg)["hubs"]["skipped"] == 1


# ---------- KB_S2_CTX=trimmed_index: сокращённый контекст стадии 2 ----------

def _trimmed(monkeypatch, n=None):
    monkeypatch.setenv("KB_S2_CTX", "trimmed_index")
    monkeypatch.setattr(crun, "CTX_MODE", "trimmed_index")
    if n is not None:
        monkeypatch.setattr(crun, "CTX_INDEX_LINES", n)


def _full_ctx(monkeypatch):
    monkeypatch.delenv("KB_S2_CTX", raising=False)
    monkeypatch.setattr(crun, "CTX_MODE", "full")


def _prompts(monkeypatch):
    prompts = []
    real = llm.run

    def spy(prompt, model, timeout, cwd):
        prompts.append(prompt)
        return real(prompt, model, timeout, cwd)
    monkeypatch.setattr(llm, "run", spy)
    return prompts


def _index_of(prompt):
    return prompt.split("## Индекс карточек области\n", 1)[1].split("\n\n## ", 1)[0]


def _seed_ctx_cards(paths):
    _card(paths, "cards/infra/restart-clone.md", "restart-clone", "Рестарт app-stable при деплое Stable")
    _card(paths, "cards/infra/deploy-notes.md", "deploy-notes", "Деплой: заметки")
    _card(paths, "cards/infra/grafana-ports.md", "grafana-ports", "Порты Grafana через tailscale")
    _card(paths, "cards/infra/infra-hub.md", "infra-hub", "Хаб области infra")
    _card(paths, "cards/product/product-only-name.md", "product-only-name", "Карточка другой области")
    commit_all(paths["root"], "карточки контекста")


def test_trimmed_index_keeps_similar_lines_and_hub(kb):
    cfg, paths = kb
    _seed_ctx_cards(paths)
    cs = [c for c in kbcore.iter_cards(cfg) if c.area == "infra"]
    chunk = [{"text": "Деплой Stable — рестарт только app-stable.", "topic": "deploy"}]
    lines = stage2.trimmed_index(cs, chunk, "infra-hub", n=1)
    assert len(lines) == 3 and lines[0].startswith("(показаны только 1 карточек")   # шапка + 1 похожая + хаб
    joined = "\n".join(lines)
    assert "[[restart-clone]] (fact, active) `cards/infra/restart-clone.md` — Рестарт app-stable" in joined
    assert "[[infra-hub]]" in joined
    assert "grafana-ports" not in joined and "deploy-notes" not in joined
    full = stage2.trimmed_index(cs, chunk, "infra-hub")                          # n=60: всё похожее, порядок индекса
    names = [re.search(r"\[\[([^\]]+)\]\]", ln).group(1) for ln in full[1:]]
    assert names == [c.name for c in cs if c.name in {"restart-clone", "deploy-notes", "infra-hub"}]
    assert stage2.trimmed_index([], chunk, "infra-hub") == []                    # пустая область — «(пусто)»
    other = [c for c in cs if c.name == "grafana-ports"]
    assert stage2.trimmed_index(other, [{"text": "рестарт", "topic": ""}, {}], "infra-hub") == []  # ни похожих, ни хаба


def test_trimmed_ctx_prompt_has_no_names_list(ck, monkeypatch):
    cfg, paths = ck
    _seed_ctx_cards(paths)
    _trimmed(monkeypatch, n=1)
    prompts = _prompts(monkeypatch)
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    s2 = [p for p in prompts if "Область: `infra`" in p]
    assert s2
    for p in s2:
        assert "Занятые имена карточек во всей базе" not in p and stage2.NAMES_RULE in p
        assert "product-only-name" not in p                                    # имя другой области не уходит
        idx = _index_of(p)
        assert "[[restart-clone]]" in idx and "Рестарт app-stable" in idx
        assert "grafana-ports" not in idx and "deploy-notes" not in idx
        assert "### cards/infra/grafana-ports.md" in p                       # выбор карточек целиком прежний
    recs = [r for r in kbcore.read_runs(cfg) if r.get("cmd") == "consolidate_batch" and r.get("stage") == 2]
    assert recs and all(r["ctx"] == "trimmed_index" and r["index_lines"] == 2 and r["prompt_bytes"] > 0
                        and r["cards_shown"] >= 4 for r in recs)


def test_default_ctx_prompt_keeps_full_index_and_names(ck, monkeypatch):
    cfg, paths = ck
    _seed_ctx_cards(paths)
    _full_ctx(monkeypatch)
    prompts = _prompts(monkeypatch)
    rc, out = run(cfg, areas="infra")
    assert rc == 0, "\n".join(out)
    p = [p for p in prompts if "Область: `infra`" in p][0]
    assert stage2.NAMES_RULE not in p and "(показаны только" not in p
    names_line = next(ln for ln in p.split("\n") if ln.startswith("Занятые имена карточек во всей базе: "))
    assert "product-only-name" in names_line and "restart-clone" in names_line
    assert "[[grafana-ports]]" in _index_of(p) and "[[deploy-notes]]" in _index_of(p)
    rec = [r for r in kbcore.read_runs(cfg) if r.get("cmd") == "consolidate_batch" and r.get("stage") == 2][0]
    assert rec["ctx"] == "full" and rec["index_lines"] == 4


def test_build_prompt_without_flag_is_byte_identical():
    """names — множество: строка имён прежняя («, » по алфавиту, пустое — «(нет)»), шаблон тот же."""
    def old(names):      # прежний build_prompt: «Занятые имена карточек во всей базе: {names}»
        return stage2.PROMPT.replace("{names_block}", "Занятые имена карточек во всей базе: {names}").format(
            area="infra", today=stage2.today(), hints="", type_bodies=stage2.TYPE_BODIES,
            names=", ".join(sorted(names)) or "(нет)", index="- [[a]] x", cards="(нет)", n=0, claims="")
    for names in ({"b", "a"}, set()):
        assert stage2.build_prompt("infra", ["- [[a]] x"], [], [], names) == old(names)
    assert "\nЗанятые имена карточек во всей базе: a, b\n\n## Индекс карточек области\n" in old({"a", "b"})


def test_trimmed_ctx_taken_name_retry_names_owner(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _card(paths, "cards/product/deploy-stable.md", "deploy-stable", "Как выкатывать app на Stable")
    commit_all(paths["root"], "product card")
    _trimmed(monkeypatch)
    _spec(tmp_path, monkeypatch, {"infra": [_near_dup_op("deploy-stable")]})
    prompts = _prompts(monkeypatch)
    rc, out = run(cfg, areas="infra")
    s2 = [p for p in prompts if "Область: `infra`" in p]
    assert len(s2) == 2                                                      # отказ → повтор в этом же прогоне
    why = "имя уже занято карточкой `cards/product/deploy-stable.md` (fact, active) — Как выкатывать app на Stable"
    assert why not in s2[0] and f"deploy-stable: {why}" in s2[1]
    assert any(r.startswith(f"infra/deploy-stable: {why}") for r in last_consolidate(cfg)["rejected"])


def test_full_ctx_taken_name_reason_unchanged(ck, tmp_path, monkeypatch):
    cfg, paths = ck
    _card(paths, "cards/product/deploy-stable.md", "deploy-stable", "Как выкатывать app на Stable")
    commit_all(paths["root"], "product card")
    _full_ctx(monkeypatch)
    _spec(tmp_path, monkeypatch, {"infra": [_near_dup_op("deploy-stable")]})
    run(cfg, areas="infra")
    rej = [r for r in last_consolidate(cfg)["rejected"] if "deploy-stable:" in r]
    assert rej and all(r.endswith("deploy-stable: имя уже занято") for r in rej)


def test_trimmed_ctx_private_owner_not_disclosed(ck, tmp_path, monkeypatch):
    """Имя занято private-карточкой: ни путь, ни описание не уходят в отказ и в промпт повтора — и тогда, когда
    карточка нашлась в worktree (страховка `existing.private`), а не в списке private-имён."""
    cfg, paths = ck
    _card(paths, "cards/private/secret-note.md", "secret-note", "личное описание")
    _trimmed(monkeypatch)
    _spec(tmp_path, monkeypatch, {"infra": [
        {"op": "ADD", "name": "secret-note", "dir": "infra", "type": "fact", "description": "дубль имени",
         "keys": ["x"], "claims": ["@рестарт только"], "body": "**Суть.** x ⟦@рестарт только⟧"}]})
    from consolidate import apply as A
    secret = main_card(cfg, "secret-note")
    real_map = A.card_map
    monkeypatch.setattr(A, "card_map", lambda c: {**real_map(c), "secret-note": secret})
    monkeypatch.setattr(crun.Run, "private_names", lambda self: set())
    prompts = _prompts(monkeypatch)
    run(cfg, areas="infra")
    rej = [r for r in last_consolidate(cfg)["rejected"] if "secret-note:" in r]
    assert rej and all(r.endswith("secret-note: имя уже занято") for r in rej)
    for p in prompts:
        assert "личное описание" not in p and "cards/private" not in p


def test_ctx_mode_is_one_source_for_prompt_and_rejection(ck, tmp_path, monkeypatch):
    """Режим берётся из crun.CTX_MODE и в промпте, и в причине отказа: окружение, сменившееся после импорта, не
    рассинхронизирует их (в промпте нет списка имён — отказ называет карточку, и наоборот)."""
    cfg, paths = ck
    _card(paths, "cards/product/deploy-stable.md", "deploy-stable", "Как выкатывать app на Stable")
    commit_all(paths["root"], "product card")
    _spec(tmp_path, monkeypatch, {"infra": [_near_dup_op("deploy-stable")]})
    monkeypatch.setattr(crun, "CTX_MODE", "trimmed_index")
    monkeypatch.delenv("KB_S2_CTX", raising=False)
    run(cfg, areas="infra")
    assert any("имя уже занято карточкой `cards/product/deploy-stable.md`" in r for r in last_consolidate(cfg)["rejected"])
    monkeypatch.setattr(crun, "CTX_MODE", "full")
    monkeypatch.setenv("KB_S2_CTX", "trimmed_index")
    run(cfg, areas="infra")
    assert any(r.endswith("deploy-stable: имя уже занято") for r in last_consolidate(cfg)["rejected"])


def test_ctx_env_values_are_validated():
    """Опечатка в KB_S2_CTX — режим full (и в записи), мусор в KB_S2_CTX_INDEX — 60, не падение импорта."""
    tools = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = "from consolidate import run; print(run.CTX_MODE, run.CTX_INDEX_LINES)"
    for env, want in (({"KB_S2_CTX": "trimmed", "KB_S2_CTX_INDEX": "abc"}, "full 60"),
                      ({"KB_S2_CTX": "trimmed_index", "KB_S2_CTX_INDEX": "-5"}, "trimmed_index 1"),
                      ({"KB_S2_CTX": "trimmed_index", "KB_S2_CTX_INDEX": "40"}, "trimmed_index 40")):
        r = subprocess.run(["python3", "-c", code], cwd=tools, capture_output=True, text=True,
                           env={**os.environ, **env})
        assert r.returncode == 0 and r.stdout.strip() == want, r.stderr
