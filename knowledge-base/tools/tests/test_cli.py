"""CLI целиком через subprocess (как из systemd/хука): чистое окружение, симлинк, права."""
import os
import stat
import subprocess

from conftest import PY, TOOLS, card_text, write

KB = os.path.join(TOOLS, "kb.py")


def run(p, *args, extra=None):
    env = {"PATH": "/usr/bin:/bin", **p["env"], **(extra or {})}  # без HOME, как env -i
    return subprocess.run([PY, KB, *args], capture_output=True, text=True, env=env, timeout=120)


def test_cli_full_cycle_clean_env(kb, tmp_path):
    cfg, p = kb
    r = run(p, "ingest", "all")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "claude-vps" in r.stdout and "коммит:" in r.stdout
    f = os.path.join(cfg.raw, "codex-vps/MEMORY.md")
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600  # umask 077
    write(os.path.join(cfg.cards, "infra/deploy-app-stable.md"),
          card_text("deploy-app-stable", "process", desc="деплой app", keys="[деплой]"))
    r = run(p, "lint")
    assert r.returncode == 0 and "1 карточек, 0 ошибок" in r.stdout
    r = run(p, "index")
    assert r.returncode == 0, r.stdout + r.stderr
    r = run(p, "find", "деплой")
    assert r.returncode == 0 and r.stdout.startswith("cards/infra/deploy-app-stable.md")
    r = run(p, "find", "--raw", "деплой", "--limit", "3")
    assert "[сырьё] raw/claude-vps/-home-app/project_deploy.md" in r.stdout
    r = run(p, "show", "deploy-app-stable")
    assert r.returncode == 0 and "name: deploy-app-stable" in r.stdout
    r = run(p, "health")
    assert r.returncode == 0 and r.stdout.startswith("status: ")
    assert os.path.isfile(os.path.join(cfg.state, "health.json"))


def test_cli_via_symlink(kb, tmp_path):
    cfg, p = kb
    link = tmp_path / "kb-link"
    os.symlink(KB, link)
    env = {"PATH": "/usr/bin:/bin", **p["env"]}
    r = subprocess.run([str(link), "lint"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([str(link)], capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "kb find" in r.stdout


def test_cli_lint_fails_on_error(kb):
    cfg, p = kb
    write(os.path.join(cfg.cards, "product/x.md"), "без frontmatter\n")
    r = run(p, "lint")
    assert r.returncode == 1 and "ошибка: cards/product/x.md: нет frontmatter" in r.stdout
