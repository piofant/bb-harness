import json
import os
import subprocess
import sys
import textwrap

import pytest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOLS)

import kbcore  # noqa: E402

PY = "/usr/bin/python3"


def write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)
    return path


def script(path, body):
    return write(path, f"#!{PY}\n" + textwrap.dedent(body), mode=0o755)


def card_text(name, type_="fact", status="active", desc="описание", keys="[ключ]",
              extra="", body="**Суть.** текст.\n"):
    return (f"---\nname: {name}\ndescription: {desc}\ntype: {type_}\nstatus: {status}\n"
            f"keys: {keys}\nsources: [raw/claude-vps/x/y.md@2026-10-01]\nverified: 2026-10-20\n"
            f"verified_how: source\nreview_after: 2026-12-01\nsensitivity: internal\n{extra}---\n\n{body}")


@pytest.fixture
def kb(tmp_path, monkeypatch):
    """Пустая база + фейковые источники и утилиты. Возвращает (cfg, paths)."""
    root = tmp_path / "kb"
    for d in ["raw", "cards/_templates", "cards/product", "cards/infra", "cards/tools",
              "cards/agents", "cards/projects", "cards/private", "decisions", "state"]:
        (root / d).mkdir(parents=True, exist_ok=True)
    write(str(root / ".gitignore"),
          "state/*\n!state/health.json\nraw/.quarantine/\ncards/private/\n.incoming/\n*.kbtmp\n")
    write(str(root / "CONFLICTS.md"), "# Конфликты\n\n## Открытые\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), *kbcore.GIT_ID, "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), *kbcore.GIT_ID, "commit", "-q", "-m", "init"], check=True)

    src = tmp_path / "src"
    claude = src / "claude-projects"
    write(str(claude / "-home-app/memory/MEMORY.md"), "# Индекс памяти\n- [x](a.md)\n")
    write(str(claude / "-home-app/memory/project_deploy.md"), "деплой app-stable\n")
    write(str(claude / "-home-app/memory/notes.txt"), "не md\n")
    write(str(claude / "-home-blog/memory/feedback_x.md"), "x\n")
    (claude / "-tmp-empty/memory").mkdir(parents=True)
    codex = src / "codex-memories"
    write(str(codex / "MEMORY.md"), "codex memory\n")
    write(str(codex / "memory_summary.md"), "summary\n")
    write(str(codex / "raw_memories.md"), "сырое, не брать\n")
    write(str(codex / "rollout_summaries/2026-10-01-a.md"), "итог a\n")
    write(str(codex / "extensions/x.md"), "не брать\n")
    write(str(codex / "skills/s.md"), "не брать\n")
    bin_ = tmp_path / "bin"
    bb_state = tmp_path / "bb_status"
    bb_state.write_text("connected")
    script(str(bin_ / "bb"), f"""
        import json, sys
        st = open({str(bb_state)!r}).read().strip()
        if st == "fail":
            sys.exit(1)
        print(json.dumps({{"id": "host_example", "status": st, "lastSeenAt": 1792712808064}}))
        """)
    settings = write(str(tmp_path / "settings.json"), json.dumps({"cleanupPeriodDays": 90}))

    env = {
        "KB_ROOT": str(root), "KB_CLAUDE_PROJECTS": str(claude), "KB_CODEX_MEMORIES": str(codex),
        "KB_BB_BIN": str(bin_ / "bb"), "KB_CLAUDE_SETTINGS": settings, "KB_MIN_FREE_GB": "0.001", "KB_LOCK_TIMEOUT": "5",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("KB_ALLOW_MASS_DELETE", raising=False)
    cfg = kbcore.Cfg()
    paths = {"root": root, "claude": claude, "codex": codex, "bin": bin_,
             "bb_state": bb_state, "tmp": tmp_path, "env": env}
    return cfg, paths


def runs(cfg):
    return kbcore.read_runs(cfg)


def git_log(cfg):
    return subprocess.run(["git", "-C", cfg.root, "log", "--format=%s"], capture_output=True,
                          text=True).stdout.splitlines()


def git_files(cfg):
    return subprocess.run(["git", "-C", cfg.root, "ls-files"], capture_output=True,
                          text=True).stdout.splitlines()
