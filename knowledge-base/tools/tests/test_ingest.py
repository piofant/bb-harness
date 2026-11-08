"""ingest: источники этой машины, гейт секретов, коммит, диск, lock, защита от стирания."""
import io
import json
import os
import subprocess
import tarfile

import pytest

import ingest
import kbcore
from conftest import git_files, git_log, runs, script, write

SECRET = "sk-" + "proj-" + "aB3dE5gH7jK9mN1pQ2rS4tU6vW8xY0zA"


def raw(cfg, rel):
    return os.path.join(cfg.raw, rel)


def test_vps_mapping_and_commit(kb):
    cfg, p = kb
    assert ingest.run_ingest(cfg, "vps") == 0
    assert os.path.isfile(raw(cfg, "claude-vps/-home-app/MEMORY.md"))
    assert os.path.isfile(raw(cfg, "claude-vps/-home-app/project_deploy.md"))
    assert os.path.isfile(raw(cfg, "claude-vps/-home-blog/feedback_x.md"))
    assert not os.path.exists(raw(cfg, "claude-vps/-home-app/notes.txt"))
    assert not os.path.exists(raw(cfg, "claude-vps/-tmp-empty"))
    assert os.path.isfile(raw(cfg, "codex-vps/MEMORY.md"))
    assert os.path.isfile(raw(cfg, "codex-vps/memory_summary.md"))
    assert os.path.isfile(raw(cfg, "codex-vps/rollout_summaries/2026-10-01-a.md"))
    for bad in ("raw_memories.md", "extensions/x.md", "skills/s.md"):
        assert not os.path.exists(raw(cfg, "codex-vps/" + bad))
    assert git_log(cfg)[0].startswith("ingest vps: claude-vps +3")
    assert "raw/codex-vps/MEMORY.md" in git_files(cfg)
    # повтор без изменений — без коммита
    n = len(git_log(cfg))
    assert ingest.run_ingest(cfg, "vps") == 0
    assert len(git_log(cfg)) == n
    recs = [r for r in runs(cfg) if r.get("source") == "claude-vps"]
    assert recs[-1]["added"] == 0 and recs[-1]["status"] == "ok"


def test_change_and_delete_mirrored(kb):
    cfg, p = kb
    ingest.run_ingest(cfg, "vps")
    write(str(p["claude"] / "-home-app/memory/project_deploy.md"), "новое\n")
    os.remove(str(p["claude"] / "-home-blog/memory/feedback_x.md"))
    assert ingest.run_ingest(cfg, "vps") == 0
    rec = [r for r in runs(cfg) if r.get("source") == "claude-vps"][-1]
    assert (rec["added"], rec["changed"], rec["deleted"]) == (0, 1, 1)
    assert open(raw(cfg, "claude-vps/-home-app/project_deploy.md")).read() == "новое\n"
    assert not os.path.exists(raw(cfg, "claude-vps/-home-blog"))


def test_telemetry_fields(kb):
    cfg, _ = kb
    ingest.run_ingest(cfg, "vps")
    rec = [r for r in runs(cfg) if r.get("source") == "codex-vps"][-1]
    for k in ("ts", "run_id", "cmd", "source", "mode", "files", "added", "changed", "deleted",
              "bytes", "quarantined", "duration_s", "status", "error"):
        assert k in rec, k
    assert rec["bytes"] > 0
    done = [r for r in runs(cfg) if r.get("event") == "done"][-1]
    assert done["status"] == "ok" and done["commit"]


def test_quarantine(kb):
    cfg, p = kb
    write(str(p["claude"] / "-home-app/memory/leak.md"), "строка\nключ " + SECRET + "\n")
    ingest.run_ingest(cfg, "vps")
    assert not os.path.exists(raw(cfg, "claude-vps/-home-app/leak.md"))
    q = os.path.join(cfg.quarantine, "claude-vps/-home-app/leak.md")
    assert os.path.isfile(q)
    assert os.stat(cfg.quarantine).st_mode & 0o777 == 0o700
    assert not any(".quarantine" in f for f in git_files(cfg))
    qrec = [r for r in runs(cfg) if r.get("event") == "quarantine"]
    assert len(qrec) == 1
    assert qrec[0]["patterns"] == ["sk_api_key"] and qrec[0]["lines"] == [2]
    assert qrec[0]["path"] == "raw/claude-vps/-home-app/leak.md"
    assert SECRET not in open(cfg.runs).read()
    # повтор — без повторной записи о карантине
    ingest.run_ingest(cfg, "vps")
    assert len([r for r in runs(cfg) if r.get("event") == "quarantine"]) == 1
    # человек проверил → allowlist → файл проходит, карантин чистится
    import gate
    data = open(str(p["claude"] / "-home-app/memory/leak.md"), "rb").read()
    write(cfg.allowlist, f"{gate.sha256(data)}  raw/claude-vps/-home-app/leak.md\n")
    ingest.run_ingest(cfg, "vps")
    assert os.path.isfile(raw(cfg, "claude-vps/-home-app/leak.md"))
    assert not os.path.exists(q)


def test_file_turning_secret_leaves_raw(kb):
    cfg, p = kb
    ingest.run_ingest(cfg, "vps")
    write(str(p["codex"] / "MEMORY.md"), "TELEGRAM_BOT_TOKEN=" + "aB3dE5gH7jK9mN1pQ2rS4\n")
    ingest.run_ingest(cfg, "vps")
    assert not os.path.exists(raw(cfg, "codex-vps/MEMORY.md"))
    assert os.path.isfile(os.path.join(cfg.quarantine, "codex-vps/MEMORY.md"))


def test_disk_low_hard_stop(kb, monkeypatch):
    cfg, _ = kb
    monkeypatch.setenv("KB_MIN_FREE_GB", "1000000")
    cfg = kbcore.Cfg()
    assert ingest.run_ingest(cfg, "all") == 3
    assert not os.path.exists(raw(cfg, "claude-vps"))
    assert runs(cfg)[-1]["status"] == "disk_low"


def test_lock_timeout(kb, monkeypatch):
    cfg, _ = kb
    monkeypatch.setenv("KB_LOCK_TIMEOUT", "0.3")
    cfg = kbcore.Cfg()
    import fcntl
    fd = os.open(cfg.lock, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert ingest.run_ingest(cfg, "vps") == 4
    finally:
        os.close(fd)
    assert runs(cfg)[-1]["status"] == "lock_timeout"
    assert ingest.run_ingest(cfg, "vps") == 0


def test_missing_source_is_error_not_wipe(kb, monkeypatch):
    cfg, p = kb
    ingest.run_ingest(cfg, "vps")
    monkeypatch.setenv("KB_CODEX_MEMORIES", str(p["tmp"] / "nope"))
    cfg = kbcore.Cfg()
    assert ingest.run_ingest(cfg, "vps") == 1
    assert os.path.isfile(raw(cfg, "codex-vps/MEMORY.md"))
    rec = [r for r in runs(cfg) if r.get("source") == "codex-vps"][-1]
    assert rec["status"] == "error" and "нет каталога" in rec["error"]


def test_empty_source_and_mass_delete_guard(kb, monkeypatch):
    cfg, p = kb
    with pytest.raises(ingest.SourceError):
        os.makedirs(raw(cfg, "x"), exist_ok=True)
        write(raw(cfg, "x/a.md"), "a")
        ingest.sync_source(cfg, "x", {}, "r")
    entries = {f"f{i}.md": (b"x", 1.0) for i in range(30)}
    ingest.sync_source(cfg, "y", entries, "r")
    with pytest.raises(ingest.SourceError, match="защита"):
        ingest.sync_source(cfg, "y", {"f0.md": (b"x", 1.0)}, "r")
    monkeypatch.setenv("KB_ALLOW_MASS_DELETE", "1")
    st = ingest.sync_source(cfg, "y", {"f0.md": (b"x", 1.0)}, "r")
    assert st["deleted"] == 29


def test_safe_rel():
    assert ingest.safe_rel("a/b.md") == "a/b.md"
    for bad in ("../x.md", "/etc/passwd", "a/../../x", ".hidden/x.md", "a/._b.md", "a/.git/x.md"):
        with pytest.raises(ingest.SourceError):
            ingest.safe_rel(bad)


def _calls(path):
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


def test_all_target_runs_everything(kb):
    cfg, _ = kb
    assert ingest.run_ingest(cfg, "all") == 0
    srcs = {r["source"] for r in runs(cfg) if r.get("source")}
    assert srcs == {"claude-vps", "codex-vps"}
    assert len([l for l in git_log(cfg) if l.startswith("ingest all")]) == 1


def test_gate_cache_skips_unchanged_and_resets_on_version(kb, monkeypatch):
    cfg, p = kb
    import gate
    calls = []
    real = gate.scan_bytes
    monkeypatch.setattr(gate, "scan_bytes", lambda d: calls.append(1) or real(d))
    ingest.run_ingest(cfg, "vps")
    first = len(calls)
    assert first > 0
    calls.clear()
    ingest.run_ingest(cfg, "vps")
    assert calls == []  # всё уже проверено этой версией гейта
    write(str(p["codex"] / "MEMORY.md"), "изменилось\n")
    ingest.run_ingest(cfg, "vps")
    assert len(calls) == 1
    calls.clear()
    monkeypatch.setattr(gate, "VERSION", "other")
    ingest.run_ingest(cfg, "vps")
    assert len(calls) == first  # новая версия шаблонов — проверить заново


def test_gate_cache_never_whitelists_secret(kb):
    cfg, p = kb
    write(str(p["claude"] / "-home-app/memory/leak.md"), SECRET + "\n")
    ingest.run_ingest(cfg, "vps")
    ingest.run_ingest(cfg, "vps")
    import gate
    assert gate.sha256((SECRET + "\n").encode()) not in ingest._load_gate_cache(cfg)
    assert not os.path.exists(raw(cfg, "claude-vps/-home-app/leak.md"))


# ---------- находки ревью ----------

def test_partial_sync_rolled_back_not_committed(kb, monkeypatch):
    """#9: источник упал посреди записи — raw/<src> возвращается к HEAD, в коммит не идёт."""
    cfg, p = kb
    ingest.run_ingest(cfg, "vps")
    write(str(p["codex"] / "MEMORY.md"), "новое codex\n")
    write(str(p["codex"] / "rollout_summaries/2026-10-02-b.md"), "итог b\n")
    write(str(p["claude"] / "-home-app/memory/project_deploy.md"), "новое claude\n")
    real = ingest._write_atomic
    def flaky(path, data, mtime):
        if "codex-vps" in path and path.endswith("2026-10-02-b.md"):
            raise OSError("диск отвалился")
        return real(path, data, mtime)
    monkeypatch.setattr(ingest, "_write_atomic", flaky)
    assert ingest.run_ingest(cfg, "vps") == 1
    assert open(raw(cfg, "codex-vps/MEMORY.md")).read() == "codex memory\n"
    assert not os.path.exists(raw(cfg, "codex-vps/rollout_summaries/2026-10-02-b.md"))
    show = subprocess.run(["git", "-C", cfg.root, "show", "--stat", "HEAD"], capture_output=True,
                          text=True).stdout
    assert "claude-vps" in show and "codex-vps" not in show


def test_quarantine_not_wiped_when_source_vanishes(kb):
    """#10: файл ушёл из источника — копия в карантине остаётся до разбора человеком."""
    cfg, p = kb
    leak = p["claude"] / "-home-app/memory/leak.md"
    write(str(leak), SECRET + "\n")
    ingest.run_ingest(cfg, "vps")
    os.remove(str(leak))
    ingest.run_ingest(cfg, "vps")
    assert os.path.isfile(os.path.join(cfg.quarantine, "claude-vps/-home-app/leak.md"))


def test_lock_file_mode_600(kb):
    """#15: state/repo.lock — 0600, даже если его создал `flock` с umask 022."""
    cfg, _ = kb
    os.close(os.open(cfg.lock, os.O_CREAT | os.O_RDWR, 0o644))
    os.chmod(cfg.lock, 0o644)
    ingest.run_ingest(cfg, "vps")
    assert os.stat(cfg.lock).st_mode & 0o777 == 0o600
