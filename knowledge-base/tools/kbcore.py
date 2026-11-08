"""Общие части kb: конфиг, frontmatter, карточки, блокировка, телеметрия, git.

Только stdlib. Все пути переопределяются переменными окружения KB_* (для тестов).
"""
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
import uuid


def _default_root():
    v = os.environ.get("KB_ROOT")
    return v if v else os.path.join(os.path.expanduser("~"), "agent-knowledge")


# Рабочие области базы (папки cards/<область>/). Свои — файлом areas.json в корне базы (или KB_AREAS_FILE):
# {"<область>": {"description": "что сюда относится", "rules": [["<regex>", <вес>], …]}, …}
# или просто списком имён в KB_AREAS («product,tools,infra,agents»). projects/<slug> и private есть всегда.
DEFAULT_AREA_INFO = {
    "product": {
        "description": "основной продукт: сценарии, пользователи, метрики, релизы, команда и роли",
        "rules": [[r"релиз|release", 2], [r"метрик|metric", 2], [r"онбординг|onboarding", 2],
                  [r"пользовател|\busers?\b", 1], [r"команд[аеуы]\b|\bteam\b", 1], [r"роли? в команде", 4]],
    },
    "tools": {
        "description": "рабочие инструменты и сервисы: трекер задач, вики, репозитории, их API и грабли",
        "rules": [[r"трекер|tracker|jira|linear", 3], [r"тикет|ticket|issue", 2], [r"вики|\bwiki\b|notion", 2],
                  [r"\bapi\b", 1], [r"github|gitlab|pull request", 2], [r"\b4\d\d\b", 1]],
    },
    "infra": {
        "description": "серверы и доставка: VPS, рабочие машины, bb, деплой, systemd, бэкапы, сеть, безопасность",
        "rules": [[r"деплой|deploy|выкат", 2], [r"systemctl|systemd|journalctl", 2], [r"\bvps\b|\bvm\b|сервер", 2],
                  [r"\bbb\b|bb server", 2], [r"бэкап|backup", 3], [r"tailscale|ufw|docker|туннел", 2],
                  [r"worktree|checkout", 2], [r"рестарт|restart", 2]],
    },
    "agents": {
        "description": "как работают сами агенты: Claude Code, Codex, Gemini, скиллы, пайплайны, память агентов",
        "rules": [[r"claude|codex|gemini", 2], [r"скилл|skill", 2], [r"агент|agent", 1], [r"промпт|prompt", 1],
                  [r"хук|hook", 2], [r"mcp", 2]],
    },
}
_AREA_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def _load_area_info():
    path = os.environ.get("KB_AREAS_FILE") or os.path.join(_default_root(), "areas.json")
    info = None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            info = {k: (v if isinstance(v, dict) else {}) for k, v in d.items()}
    except (OSError, ValueError):
        pass
    names = [a.strip() for a in os.environ.get("KB_AREAS", "").split(",") if a.strip()]
    if names:
        base = info or DEFAULT_AREA_INFO
        info = {a: base.get(a, {}) for a in names}
    info = info or DEFAULT_AREA_INFO
    return {a: v for a, v in info.items() if _AREA_NAME.match(a) and a not in ("projects", "private")}


AREA_INFO = _load_area_info()
WORK_AREAS = list(AREA_INFO)
AREAS = WORK_AREAS + ["projects", "private"]
AREA_ALT = "|".join(re.escape(a) for a in WORK_AREAS)   # для регулярок «область или projects/<slug>»
AREA_HELP = " | ".join(WORK_AREAS + ["projects/<slug>"])


def area_description(area):
    return (AREA_INFO.get(area) or {}).get("description") or area
TYPES = ["process", "fact", "decision", "gotcha", "person", "preference", "topic"]
STATUSES = ["active", "superseded", "disputed"]
VERIFIED_HOW = ["live", "source"]
SENSITIVITY = ["internal", "restricted", "private"]
REQUIRED = ["name", "description", "type", "status", "keys", "sources",
            "verified", "verified_how", "review_after", "sensitivity"]

LIMIT_INDEX_LINES = 80
LIMIT_HUB_LINES = 40
LIMIT_CARD_BYTES = 6 * 1024
LIMIT_CORE_TOKENS = 1500

BB_CANDIDATES = [
    os.path.expanduser("~/.local/bin/bb"),
    "/usr/local/bin/bb",
]


def _env(name, default):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


class Cfg:
    def __init__(self):
        home = os.path.expanduser("~")
        self.root = os.path.abspath(_default_root())
        self.claude_projects = _env("KB_CLAUDE_PROJECTS", os.path.join(home, ".claude/projects"))
        self.codex_memories = _env("KB_CODEX_MEMORIES", os.path.join(home, ".codex/memories"))
        self.bb_bin = _env("KB_BB_BIN", "") or shutil.which("bb") or next(
            (p for p in BB_CANDIDATES if os.access(p, os.X_OK)), "bb")
        self.claude_settings = _env("KB_CLAUDE_SETTINGS", os.path.join(home, ".claude/settings.json"))
        self.min_free_gb = float(_env("KB_MIN_FREE_GB", "5"))
        self.lock_timeout = float(_env("KB_LOCK_TIMEOUT", "600"))

    # производные пути
    @property
    def raw(self):
        return os.path.join(self.root, "raw")

    @property
    def quarantine(self):
        return os.path.join(self.root, "raw", ".quarantine")

    @property
    def cards(self):
        return os.path.join(self.root, "cards")

    @property
    def state(self):
        return os.path.join(self.root, "state")

    @property
    def runs(self):
        return os.path.join(self.state, "runs.jsonl")

    @property
    def lock(self):
        return os.path.join(self.state, "repo.lock")

    @property
    def incoming(self):
        return os.path.join(self.root, ".incoming")

    @property
    def allowlist(self):
        return os.path.join(self.state, "quarantine-allow.txt")


# ---------- время ----------

def now_ts():
    return time.time()


def iso(ts=None):
    if ts is None:
        ts = time.time()
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    """ISO-время из runs.jsonl → unix ts; None при ошибке."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S"):
        try:
            d = dt.datetime.strptime(s, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.timestamp()
        except ValueError:
            continue
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


def new_run_id():
    return uuid.uuid4().hex[:12]


# ---------- телеметрия ----------

def log_run(cfg, **rec):
    rec = {"ts": iso(), **rec}
    os.makedirs(cfg.state, exist_ok=True)
    with open(cfg.runs, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read_runs(cfg):
    out = []
    try:
        with open(cfg.runs, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


# ---------- блокировка ----------

class LockTimeout(Exception):
    pass


@contextlib.contextmanager
def repo_lock(cfg, timeout=None):
    """flock(2) на state/repo.lock — совместим с `flock state/repo.lock ...`."""
    timeout = cfg.lock_timeout if timeout is None else timeout
    os.makedirs(cfg.state, exist_ok=True)
    fd = os.open(cfg.lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(fd).st_mode & 0o777 != 0o600:
            os.fchmod(fd, 0o600)
    except OSError:
        pass
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise LockTimeout(cfg.lock)
                time.sleep(0.2)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------- диск ----------

def free_gb(path):
    p = path
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return shutil.disk_usage(p).free / 1024 ** 3


# ---------- git ----------

GIT_ID = ["-c", "user.name=agent-knowledge", "-c", "user.email=agent-knowledge@localhost",
          "-c", "commit.gpgsign=false"]


def git(cfg, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    p = subprocess.run(["git", "-C", cfg.root, *GIT_ID, *args], capture_output=True,
                       text=True, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} rc={p.returncode}: {p.stderr.strip()[:300]}")
    return p


def current_branch(cfg):
    p = git(cfg, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if p.returncode != 0:
        # репозиторий без коммитов
        p = git(cfg, "symbolic-ref", "--short", "HEAD", check=False)
    return p.stdout.strip()


def commit_paths(cfg, pathspecs, message):
    """git add + commit только указанных путей; коммит только при диффе. Вернёт sha или None."""
    git(cfg, "add", "-A", "--", *pathspecs)
    diff = git(cfg, "diff", "--cached", "--quiet", "--", *pathspecs, check=False)
    if diff.returncode == 0:
        return None
    git(cfg, "commit", "-q", "-m", message, "--", *pathspecs)
    return git(cfg, "rev-parse", "--short", "HEAD").stdout.strip()


# ---------- frontmatter ----------

def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _strip_comment(v):
    """Убирает ` # комментарий`, если `#` не внутри кавычек."""
    q = None
    for i, ch in enumerate(v):
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "#" and (i == 0 or v[i - 1] in " \t"):
            return v[:i].rstrip()
    return v.rstrip()


def _split_list(inner):
    items, cur, q = [], [], None
    for ch in inner:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur.append(ch)
        elif ch == ",":
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    items.append("".join(cur))
    return [_unquote(x) for x in items if x.strip()]


def _value(v):
    v = _strip_comment(v).strip()
    if v.startswith("[") and v.endswith("]"):
        return _split_list(v[1:-1])
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return _unquote(v)


def parse_frontmatter(text):
    """Подмножество YAML: `key: value`, `[a, b]`, блочные списки `- x`. → (dict|None, body)."""
    if not text.startswith("---"):
        return None, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return None, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, text
    fm, key = {}, None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if stripped.startswith("- ") or stripped == "-":
            if key is not None:
                if not isinstance(fm.get(key), list):
                    fm[key] = []
                item = _value(stripped[1:])
                if item != "":
                    fm[key].append(item)
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", raw)
        if not m:
            continue
        key = m.group(1)
        fm[key] = _value(m.group(2))
    return fm, "\n".join(lines[end + 1:])


def as_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


# ---------- карточки ----------

class Card:
    def __init__(self, cfg, abspath):
        self.abs = abspath
        self.rel = os.path.relpath(abspath, cfg.root)
        rel_cards = os.path.relpath(abspath, cfg.cards)
        parts = rel_cards.split(os.sep)
        self.dir_parts = parts[:-1]
        if not self.dir_parts:
            self.area = ""
        elif self.dir_parts[0] == "projects" and len(self.dir_parts) >= 2:
            self.area = "projects/" + self.dir_parts[1]
        else:
            self.area = self.dir_parts[0]
        self.stem = os.path.splitext(parts[-1])[0]
        with open(abspath, "rb") as f:
            data = f.read()
        self.size = len(data)
        self.text = data.decode("utf-8", errors="replace")
        self.fm, self.body = parse_frontmatter(self.text)
        self.fm = self.fm or {}

    def get(self, k, default=""):
        return self.fm.get(k, default)

    @property
    def name(self):
        return str(self.fm.get("name") or self.stem)

    @property
    def description(self):
        return str(self.fm.get("description") or "")

    @property
    def type(self):
        return str(self.fm.get("type") or "")

    @property
    def status(self):
        return str(self.fm.get("status") or "")

    @property
    def keys(self):
        return as_list(self.fm.get("keys"))

    @property
    def verified(self):
        return str(self.fm.get("verified") or "")

    @property
    def private(self):
        return bool(self.dir_parts) and self.dir_parts[0] == "private"

    @property
    def core(self):
        return self.fm.get("core") is True or str(self.fm.get("core")).lower() == "true"

    @property
    def importance(self):
        return str(self.fm.get("importance") or "normal")


def iter_cards(cfg, include_private=False):
    if not os.path.isdir(cfg.cards):
        return []
    out = []
    for dirpath, dirs, files in os.walk(cfg.cards):
        rel = os.path.relpath(dirpath, cfg.cards)
        if rel == ".":
            dirs[:] = [d for d in dirs if d != "_templates" and not d.startswith(".")
                       and (include_private or d != "private")]
        else:
            dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
        for fn in sorted(files):
            if fn.endswith(".md") and not fn.startswith(("_", ".")):
                p = os.path.join(dirpath, fn)
                if os.path.isfile(p) and not os.path.islink(p):
                    out.append(Card(cfg, p))
    out.sort(key=lambda c: c.rel)
    return out


def est_tokens(text):
    """Грубая оценка с запасом: кириллица ≈2 символа на токен."""
    return (len(text) + 1) // 2


def norm(s):
    return s.lower().replace("ё", "е")
