"""Worktree сводчика и слияние в main под state/repo.lock."""
import copy
import os
import subprocess
import sys

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import cards as kbcards  # noqa: E402
from kbcore import GIT_ID, commit_paths, current_branch, parse_frontmatter, repo_lock  # noqa: E402

CARD_PATHS = ["cards", "CONFLICTS.md", "verified"]


def _git(root, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    p = subprocess.run(["git", "-C", root, *GIT_ID, *args], capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} rc={p.returncode}: {p.stderr.strip()[:300]}")
    return p


def wt_path_default(cfg):
    return os.path.join(os.path.dirname(cfg.root.rstrip("/")), os.path.basename(cfg.root.rstrip("/")) + "-wt",
                        "consolidate")


def wt_path(cfg):
    return os.environ.get("KB_CONSOLIDATE_WT") or wt_path_default(cfg)


def wt_cfg(cfg, path):
    c = copy.copy(cfg)
    c.root = path
    return c


def prepare_worktree(cfg, branch):
    """Создать/переиспользовать worktree на ветке `branch` и подтянуть в неё main. Под lock (трогает общий .git).

    Если в worktree уже стоит НЕслитая ветка сводчика (вчерашние гейты не прошли, --no-merge), работа
    продолжается на ней: реестр уже считает её утверждения сведёнными. → (путь, ветка).

    KB_CONSOLIDATE_BRANCH — фиксированная ветка эксперимента (например exp/h6-extract в своём worktree
    KB_CONSOLIDATE_WT): берётся ровно она, без подхвата несслитых веток consolidate/*."""
    path = wt_path(cfg)
    fixed = os.environ.get("KB_CONSOLIDATE_BRANCH")
    if fixed:
        branch = fixed
    with repo_lock(cfg):
        listed = _git(cfg.root, "worktree", "list", "--porcelain").stdout
        if f"worktree {path}\n" not in listed + "\n":
            _git(cfg.root, "worktree", "prune")
            # worktree удалён, а несслитая ветка сводчика осталась — продолжаем её, а не начинаем с main
            orphans = sorted(b.strip(" *+") for b in _git(cfg.root, "branch", "--no-merged", "main", "--list",
                                                            "consolidate/*").stdout.splitlines() if b.strip())
            if orphans and not fixed:
                branch = orphans[-1]
            if os.path.exists(path) and os.listdir(path):
                raise RuntimeError(f"{path} существует и не worktree")
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            exists = _git(cfg.root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False)
            if exists.returncode == 0:
                _git(cfg.root, "worktree", "add", "-q", path, branch)
            else:
                _git(cfg.root, "worktree", "add", "-q", "-b", branch, path, "main")
        else:
            cur = _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if not fixed and cur != branch and cur.startswith("consolidate/") and \
                    _git(cfg.root, "merge-base", "--is-ancestor", cur, "main", check=False).returncode != 0:
                branch = cur
            if cur != branch:
                dirty = _git(path, "status", "--porcelain").stdout.strip()
                if dirty:
                    raise RuntimeError(f"worktree на {cur} с незакоммиченными правками")
                exists = _git(path, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False)
                if exists.returncode == 0:
                    _git(path, "checkout", "-q", branch)
                else:
                    _git(path, "checkout", "-q", "-b", branch, "main")
        # свежий main в ветку (ingest пишет raw/, index — индексы; конфликтов с cards/ нет)
        m = _git(path, "merge", "-q", "--no-edit", "main", check=False)
        if m.returncode != 0:
            _git(path, "merge", "--abort", check=False)
            raise RuntimeError(f"merge main → {branch}: {m.stderr.strip()[:200]}")
    os.chmod(path, 0o700)
    return path, branch


def cards_at_last_write(cfg_wt, rel_path, cards_dir):
    """Состав папки карточек на коммите последней записи файла rel_path (хаба) в истории worktree — разовая
    начальная база хаба, пока своей базы (state/hubs/<область>.json) ещё нет.
    → {имя: (статус, description)} или None — файла в истории нет или git не ответил."""
    sha = _git(cfg_wt.root, "log", "-1", "--format=%H", "--", rel_path, check=False).stdout.strip()
    if not sha:
        return None
    ls = _git(cfg_wt.root, "ls-tree", "-r", "-z", "--name-only", sha, "--", cards_dir, check=False)
    if ls.returncode != 0:
        return None
    files = [f for f in ls.stdout.split("\0")
             if f.endswith(".md") and not any(p.startswith(("_", ".")) for p in f.split("/")[1:])]
    out = {}
    if not files:
        return out
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    p = subprocess.run(["git", "-C", cfg_wt.root, "cat-file", "--batch"], capture_output=True, env=env,
                       input="".join(f"{sha}:{f}\n" for f in files).encode("utf-8"))
    if p.returncode != 0:
        return None
    data, i = p.stdout, 0
    for f in files:
        j = data.find(b"\n", i)
        if j < 0:
            break
        head = data[i:j].split()
        i = j + 1
        if len(head) < 3 or head[1] != b"blob":
            continue
        n = int(head[2])
        fm, _ = parse_frontmatter(data[i:i + n].decode("utf-8", errors="replace"))
        i += n + 1
        fm = fm or {}
        name = str(fm.get("name") or os.path.splitext(os.path.basename(f))[0])
        out[name] = (str(fm.get("status") or ""), str(fm.get("description") or ""))
    return out


def is_ancestor(root, commit, ref):
    """Коммит в истории ref (main или HEAD worktree)? Нет коммита или ref — False."""
    return _git(root, "merge-base", "--is-ancestor", commit, ref, check=False).returncode == 0


def head(root):
    p = _git(root, "rev-parse", "HEAD", check=False)
    return p.stdout.strip() if p.returncode == 0 else None


def new_conflicts_on_branch(cfg, branch):
    """Сколько открытых конфликтов ветка добавляет к main (по диффу CONFLICTS.md, а не по прогону)."""
    d = _git(cfg.root, "diff", f"main...{branch}", "--", "CONFLICTS.md", check=False).stdout
    return sum(1 for ln in d.splitlines() if ln.startswith("+- [ ]"))


def commit_branch(cfg, wt, message):
    paths = [p for p in CARD_PATHS if os.path.exists(os.path.join(wt, p))]
    with repo_lock(cfg):
        return commit_paths(wt_cfg(cfg, wt), paths, message)


def branch_merged(cfg, branch):
    p = _git(cfg.root, "merge-base", "--is-ancestor", branch, "main", check=False)
    return p.returncode == 0


def merge_to_main(cfg, branch, message):
    """Слить ветку в main под lock, затем индексы на main (тот же lock). → (merge_sha, index_sha, changed)."""
    with repo_lock(cfg):
        br = current_branch(cfg)
        if br != "main":
            raise RuntimeError(f"основной checkout не на main ({br})")
        m = _git(cfg.root, "merge", "--no-ff", "-q", "-m", message, branch, check=False)
        if m.returncode != 0:
            _git(cfg.root, "merge", "--abort", check=False)
            raise RuntimeError(f"merge {branch} → main: {(m.stderr or m.stdout).strip()[:300]}")
        merge_sha = _git(cfg.root, "rev-parse", "--short", "HEAD").stdout.strip()
        changed = kbcards.build_index(cfg)
        idx_sha = commit_paths(cfg, ["INDEX.md", "INDEX-core.md", "INDEX.tsv", ":(glob)cards/**/_index.md"],
                               "index: пересборка индексов после сводчика")
    return merge_sha, idx_sha, changed
