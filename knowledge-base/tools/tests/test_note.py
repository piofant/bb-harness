"""kb note: захват знания агентом → raw/inbox → сводчик (единицы), другая машина — локальная папка входящих."""
import json
import os
import subprocess
import sys

import pytest

from conftest import PY, TOOLS, git_log, runs, write

import kbcore  # noqa: E402

KB = os.path.join(TOOLS, "kb.py")
NOTE = os.path.join(TOOLS, "note.py")
FACT = "app-stable рестартить только через systemctl restart app-stable; app.service не запускать"
SECRET = "ключ для поиска: sk-proj-" + "Ab3dE5gH7jK9mN1pQ3sT5vX7z" + "Ab3dE5gH7jK9"


def kb_cli(paths, *args, env_extra=None):
    env = {**os.environ, **paths["env"], **(env_extra or {})}
    return subprocess.run([PY, KB, *args], capture_output=True, text=True, env=env)


def inbox_files(cfg, host="vps"):
    d = os.path.join(cfg.raw, "inbox", host)
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def read_inbox(cfg, host="vps"):
    d = os.path.join(cfg.raw, "inbox", host)
    return "".join(open(os.path.join(d, f), encoding="utf-8").read() for f in inbox_files(cfg, host))


def notes_runs(cfg):
    return [r for r in runs(cfg) if r.get("cmd") == "note"]


# ---------- VPS ----------

def test_note_writes_inbox_commits_and_logs_without_text(kb):
    cfg, paths = kb
    p = kb_cli(paths, "note", FACT, "--area", "infra", "--source", "тред thr_x")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "записано n-" in p.stdout and "kb find --raw" in p.stdout
    body = read_inbox(cfg)
    assert FACT in body and "· infra · n-" in body and "источник: тред thr_x" in body
    assert any(s.startswith("note vps: n-") for s in git_log(cfg))
    st = subprocess.run(["git", "-C", cfg.root, "status", "--porcelain", "--", "raw/inbox"],
                        capture_output=True, text=True).stdout
    assert st == ""
    r = notes_runs(cfg)[-1]
    assert r["status"] == "ok" and r["host"] == "vps" and r["area"] == "infra" and r["commit"]
    assert FACT not in json.dumps(r, ensure_ascii=False) and "thr_x" not in json.dumps(r, ensure_ascii=False)
    # pending после переноса пуст
    pend = os.path.join(cfg.state, "inbox-pending", "vps")
    assert not os.path.isdir(pend) or not os.listdir(pend)


def test_note_dedup_ignores_case_spaces_and_final_dot(kb):
    cfg, paths = kb
    assert kb_cli(paths, "note", FACT).returncode == 0
    p = kb_cli(paths, "note", "  " + FACT.upper().replace(" ", "   ") + ".")
    assert p.returncode == 0 and "уже записано" in p.stdout
    assert read_inbox(cfg).count("## ") == 1
    assert notes_runs(cfg)[-1]["status"] == "duplicate"


def test_note_secret_goes_to_quarantine_not_raw_value_not_logged(kb):
    cfg, paths = kb
    p = kb_cli(paths, "note", SECRET, "--area", "infra")
    assert p.returncode == 3 and "карантин" in p.stdout
    assert inbox_files(cfg) == []
    q = os.path.join(cfg.quarantine, "inbox", "vps")
    assert os.listdir(q) and "sk-proj-" in open(os.path.join(q, os.listdir(q)[0])).read()
    r = notes_runs(cfg)[-1]
    assert r["status"] == "quarantined" and "sk_api_key" in r["patterns"]
    with open(cfg.runs, encoding="utf-8") as f:
        assert "Ab3dE5gH7jK9" not in f.read()
    assert not any("raw/.quarantine" in f for f in subprocess.run(
        ["git", "-C", cfg.root, "ls-files"], capture_output=True, text=True).stdout.splitlines())


@pytest.mark.parametrize("area,ok", [("product", True), ("projects/blog", True), ("tools", True),
                                     ("private", False), ("Product", False), ("projects/", False), ("misc", False)])
def test_note_area_validation(kb, area, ok):
    cfg, paths = kb
    p = kb_cli(paths, "note", FACT + " " + area, "--area", area)
    assert (p.returncode == 0) is ok, p.stdout
    if not ok:
        assert p.returncode == 2 and notes_runs(cfg)[-1]["status"] == "invalid"


def test_note_too_long_or_empty_is_rejected(kb):
    cfg, paths = kb
    assert kb_cli(paths, "note", "x" * 701).returncode == 2
    assert kb_cli(paths, "note", "a\nb\nc\nd\ne").returncode == 2
    assert kb_cli(paths, "note", "   ").returncode == 2
    assert inbox_files(cfg) == []


def test_note_lock_busy_stays_pending_then_ingest_flushes(kb):
    cfg, paths = kb
    import fcntl
    fd = os.open(cfg.lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        p = kb_cli(paths, "note", FACT, env_extra={"KB_NOTE_LOCK_WAIT": "0.3"})
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert p.returncode == 0 and "в очередь" in p.stdout
    assert inbox_files(cfg) == []
    assert notes_runs(cfg)[-1]["status"] == "pending"
    # дубль, пока заметка в очереди, тоже ловится
    assert "уже записано" in kb_cli(paths, "note", FACT).stdout
    i = kb_cli(paths, "ingest", "vps")
    assert i.returncode == 0, i.stdout
    assert FACT in read_inbox(cfg)
    assert any("inbox/vps" in s for s in git_log(cfg) if s.startswith("ingest"))
    assert not os.listdir(os.path.join(cfg.state, "inbox-pending", "vps"))
    rec = [r for r in runs(cfg) if r.get("cmd") == "ingest" and r.get("source") == "inbox/vps"][-1]
    assert rec["status"] == "ok" and rec["added"] == 1


def test_flush_regates_pending_block(kb):
    """Шаблоны гейта обновились, пока заметка ждала: при переносе — карантин, не raw."""
    cfg, paths = kb
    import note
    write(os.path.join(cfg.state, "inbox-pending", "vps", "2026-10-25.md"),
          "# Заметки агентов — vps, 2026-10-25\n\n## 2026-10-25 10:00Z · infra · n-0123456789ab\n" + SECRET + "\n\n")
    moved, quarantined = note.flush_pending(cfg, "vps")
    assert (moved, quarantined) == (0, 1)
    assert inbox_files(cfg) == []


# ---------- сводчик: единицы ----------

def test_units_split_inbox_per_note_and_collapse_across_hosts(kb):
    cfg, paths = kb
    from consolidate.units import collapse, iter_units
    kb_cli(paths, "note", FACT, "--area", "infra")
    kb_cli(paths, "note", "трекер: CreateIssue в PROJ закрыт — заводить через форму", "--area", "tools")
    units = [u for u in iter_units(cfg.raw, cfg.root) if u.kind == "inbox"]
    assert len(units) == 2
    assert all(u.src.startswith("raw/inbox/vps/") and "#n-" in u.uid and u.origin.startswith("inbox:")
               for u in units)
    first = {u.uid: u.sha for u in units}
    kb_cli(paths, "note", "третья заметка про bb server и порт 2222")
    units2 = [u for u in iter_units(cfg.raw, cfg.root) if u.kind == "inbox"]
    assert len(units2) == 3
    assert all(first[u.uid] == u.sha for u in units2 if u.uid in first)   # старые единицы не изменились
    # та же заметка пришла с другой машины → одна единица
    mac = os.path.join(cfg.raw, "inbox", "mac", "2026-10-26.md")
    vps_text = read_inbox(cfg)
    block = vps_text[vps_text.index("## "):].split("\n\n")[0] + "\n"
    write(mac, "# Заметки агентов — mac, 2026-10-26\n\n" + block + "\n")
    allu = iter_units(cfg.raw, cfg.root)
    keep, dup = collapse(allu)
    inbox_keep = [u for u in keep if u.kind == "inbox"]
    assert len(inbox_keep) == 3 and len(dup) >= 1


def test_note_and_units_header_regex_agree():
    import note
    from consolidate import units
    b = note.render("факт", "agents", None, 1792900000)
    assert note.HEAD_RX.search(b) and units.NOTE_RX.search(b)
    assert [i for i, _ in note.split_blocks(b)] == [i for i, _ in units.split_notes(b)]


# ---------- другая машина ----------

def test_mac_standalone_script_writes_local_inbox(tmp_path):
    inbox = tmp_path / "inbox"
    env = {**os.environ, "HOME": str(tmp_path), "KB_NOTE_INBOX": str(inbox)}
    p = subprocess.run([PY, NOTE, FACT, "--area", "infra"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    files = [f for f in os.listdir(inbox) if f.endswith(".md")]
    assert len(files) == 1 and FACT in (inbox / files[0]).read_text()
    p = subprocess.run([PY, NOTE, FACT], capture_output=True, text=True, env=env)
    assert "уже записано" in p.stdout
    p = subprocess.run([PY, NOTE, SECRET], capture_output=True, text=True, env=env)
    assert p.returncode == 3
    assert "sk-proj" not in (inbox / files[0]).read_text()
    assert (inbox / ".quarantine").is_dir()
    tele = (tmp_path / ".cache/agent-knowledge/kb-note.jsonl").read_text()
    assert FACT not in tele and "Ab3dE5" not in tele and '"status": "ok"' in tele


# ---------- инструкция агентам ----------

def test_find_raw_shows_note_block_above_big_raw_files(kb):
    """Пока сводчик не сделал карточку, заметка находится `kb find --raw` первой, а не тонет в MEMORY.md."""
    cfg, paths = kb
    big = "database locked " * 50 + "\n"
    write(os.path.join(cfg.raw, "codex-vps", "MEMORY.md"), "# Task Group: x\n" + big)
    kb_cli(paths, "note", "Telegram MCP: «database is locked» при параллельных вызовах — вызывать последовательно")
    p = kb_cli(paths, "find", "--raw", "--limit", "3", "telegram", "database", "locked")
    raw = [ln for ln in p.stdout.splitlines() if ln.startswith("[")]
    assert raw and raw[0].startswith("[заметка] raw/inbox/vps/") and "#n-" in raw[0]
    assert "последовательно" in raw[0] and "источник" not in raw[0]
    assert sum(1 for ln in raw if "raw/inbox" in ln) == 1        # файл входящих не дублируется общим поиском


# ---------- находки ревью ----------

def test_review_commit_failure_is_committed_by_next_ingest(kb, monkeypatch):
    """#1: перенос в raw прошёл, коммит упал → заметку коммитит ближайший kb ingest, а не теряет до следующей."""
    cfg, paths = kb
    import note
    import kbcore as K
    real = K.commit_paths

    def boom(*a, **k):
        raise RuntimeError("git commit rc=128: index.lock")
    monkeypatch.setattr(K, "commit_paths", boom)
    msgs = []
    assert note.note_vps(cfg, FACT, out=msgs.append) == 0
    assert "коммит не удался" in msgs[-1]
    assert notes_runs(cfg)[-1]["status"] == "uncommitted"
    monkeypatch.setattr(K, "commit_paths", real)
    import ingest
    assert ingest.run_ingest(cfg, "vps") == 0
    st = subprocess.run(["git", "-C", cfg.root, "status", "--porcelain", "--", "raw/inbox"],
                        capture_output=True, text=True).stdout
    assert st == "" and FACT in read_inbox(cfg)


def _mac_entry(*blocks):
    body = "# Заметки агентов — mac, 2026-10-25\n\n" + "".join(blocks)
    return {"2026-10-25.md": (body.encode(), 1.0)}


def _blk(nid, text):
    return f"## 2026-10-25 10:00Z · infra · n-{nid}\n{text}\n\n"


def test_review_header_like_line_rejected(kb):
    """#4: строка-заголовок внутри заметки резала бы её на два блока."""
    cfg, paths = kb
    p = kb_cli(paths, "note", "факт\n## 2026-01-01 00:00Z · infra · n-0123456789ab\nхвост")
    assert p.returncode == 2 and inbox_files(cfg) == []


def test_review_invalid_area_not_logged_raw(kb):
    """#10: значение --area до проверки в журнал не попадает."""
    cfg, paths = kb
    p = kb_cli(paths, "note", FACT, "--area", "Ab3dE5gH7jK9mN1pQ3sT5")
    assert p.returncode == 2
    with open(cfg.runs, encoding="utf-8") as f:
        assert "Ab3dE5gH7jK9mN1pQ3sT5" not in f.read()


def test_review_max_three_lines(kb):
    cfg, paths = kb
    assert kb_cli(paths, "note", "a1 строка\nb2 строка\nc3 строка").returncode == 0
    assert kb_cli(paths, "note", "a\nb\nc\nd").returncode == 2


def test_review_find_notes_dedup_across_hosts_and_fresh_first(kb):
    """#8: одна заметка с двух машин — одна строка; свежее по дате файла выше, независимо от хоста."""
    cfg, paths = kb
    write(os.path.join(cfg.raw, "inbox", "vps", "2026-10-20.md"), "# x\n\n" + _blk("a" * 12, "telegram locked старое"))
    write(os.path.join(cfg.raw, "inbox", "mac", "2026-10-20.md"), "# x\n\n" + _blk("a" * 12, "telegram locked старое"))
    write(os.path.join(cfg.raw, "inbox", "mac", "2026-10-25.md"), "# x\n\n" + _blk("e" * 12, "telegram locked новое"))
    p = kb_cli(paths, "find", "--raw", "telegram", "locked")
    notes = [ln for ln in p.stdout.splitlines() if ln.startswith("[заметка]")]
    assert len(notes) == 2 and "новое" in notes[0]


def test_rereview_hash_lines_allowed_headers_rejected(kb):
    cfg, paths = kb
    assert kb_cli(paths, "note", "#123 в трекере — дубль, закрывать ссылкой").returncode == 0
    assert kb_cli(paths, "note", "### заголовок\nтекст").returncode == 2


def test_rereview_note_oserror_is_reported_not_traceback(kb, monkeypatch):
    cfg, paths = kb
    import note

    def boom(*a, **k):
        raise OSError("диск")
    monkeypatch.setattr(note, "flush_pending", boom)
    res = {}
    assert note.note_vps(cfg, FACT, out=lambda *_: None, result=res) == 0
    assert res["status"] == "pending"
