"""«Правда недели» — детерминированный сбор фактов за окно дат. LLM получает её готовой и только формулирует.

Источники:
- git: чистая дельта `cards/` `projects/` `decisions/` `decisions.md` между срезами на начало и конец окна,
  значимые коммиты окна;
- «## История» карточек: строки с датой внутри окна — события недели (служебные строки сводчика отдельно);
- `state/runs.jsonl` (прогоны и сбои), `CONFLICTS.md` (новые и закрытые споры), `state/health.json`;
- то, что посчитал сам конвейер: итог К1, предложения К2 (`proposals/`), сверка скиллов К5.

У каждого пункта — ссылка (`ref`): `[[карточка]]`, `[текст](путь)` или `коммит <sha>`. Пункт без ссылки не создаётся.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from kbcore import GIT_ID, parse_frontmatter, parse_iso, read_runs  # noqa: E402

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
TRACKED = ["cards", "projects", "decisions", "decisions.md", "CONFLICTS.md"]
TOP_LAYER = ("MOC.md", "projects/", "about/", "skills.md", "integrations.md", "decisions.md", "decisions/",
             "lessons.md", "templates/", "automation-routines.md")
MECH_COMMIT = re.compile(r"^(ingest all|index:|health:|consolidate[ :]|Merge branch|raw:)", re.I)
HIST_LINE = re.compile(r"^-\s+(\d{4}-\d{2}-\d{2})\S*\s+[—–-]+\s+(.+)$")
MECH_HIST = re.compile(r"^(add|update|supersede|merge|dedup|слияние|хаб|создана)\b|сводчик|прогон [0-9a-f]{6,}", re.I)
RUN_ID = re.compile(r"\s*\((сводчик, )?прогон [0-9a-f]{6,}\)\s*")
CONFLICT_LINE = re.compile(r"^- \[( |x)\] (\d{4}-\d{2}-\d{2}) (\S+) — (.+)$")
DECISION_LINE = re.compile(r"^- \*\*(\d{4}-\d{2}-\d{2}) · (.+)$")
# сбой прогона: всё, что не «ок / без изменений / пропущено / машина офлайн»
OK_STATUSES = {"ok", "unchanged", "skipped", "skipped_offline", "mac_offline", "offline", "distinct", "dry_run", "deferred", "no_units",
               "skipped_recent", "skipped_branch", "not_due", "no_changes", "covered", None}
SIGNIFICANT_CARDS = 12
SIGNIFICANT_COMMITS = 6
MAX_ITEMS_LLM = 60


# ---------- окно ----------

class Window:
    """[since, until] в UTC. Даты YYYY-MM-DD включительно: since 00:00, until 23:59:59."""

    def __init__(self, since_ts, until_ts):
        self.since_ts = float(since_ts)
        self.until_ts = float(until_ts)

    @classmethod
    def from_dates(cls, since=None, until=None, now=None, days=7):
        now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
        u = _day_end(until) if until else now
        s = _day_start(since) if since else (_day_start(until) - (days - 1) * 86400 if until else u - days * 86400)
        if s >= u:
            raise ValueError(f"пустое окно: {since}..{until}")
        return cls(s, u)

    @property
    def since_date(self):
        return _date(self.since_ts)

    @property
    def until_date(self):
        return _date(self.until_ts)

    def has_date(self, d):
        """Дата внутри окна, обе границы включительно. День стыка с прошлым окном входит в оба; повторы
        отсекает `seen` — ключи пунктов прошлого обзора (см. collect), а не дата: иначе потерялось бы то,
        что появилось в этот день после прошлого прогона."""
        return self.since_date <= d <= self.until_date

    def has_ts(self, ts):
        return ts is not None and self.since_ts <= ts <= self.until_ts

    def title(self):
        s, u = self.since_date, self.until_date
        return f"{s[8:10]}.{s[5:7]}–{u[8:10]}.{u[5:7]}"

    def as_dict(self):
        return {"since": self.since_date, "until": self.until_date, "since_ts": self.since_ts,
                "until_ts": self.until_ts, "title": self.title()}


def _day_start(d):
    return dt.datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()


def _day_end(d):
    return _day_start(d) + 86400 - 1


def _date(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- git ----------

def _git(root, *args, check=True, input_text=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    p = subprocess.run(["git", "-C", root, *GIT_ID, *args], capture_output=True, env=env,
                       input=input_text.encode() if input_text is not None else None)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} rc={p.returncode}: {p.stderr.decode(errors='replace')[:300]}")
    return p


def _out(root, *args, check=True):
    return _git(root, *args, check=check).stdout.decode("utf-8", errors="replace")


def commit_before(root, ts):
    """Последний коммит первой линии HEAD не позже ts; None — истории до ts нет."""
    sha = _out(root, "rev-list", "-1", "--first-parent", f"--before={_iso(ts)}", "HEAD", check=False).strip()
    return sha or None


def read_blobs(root, ref, paths):
    """{path: text} для путей на коммите ref (git cat-file --batch). Нет коммита — пусто."""
    if not ref or not paths:
        return {}
    req = "".join(f"{ref}:{p}\n" for p in paths)
    data = _git(root, "cat-file", "--batch", input_text=req).stdout
    out, i = {}, 0
    for p in paths:
        nl = data.index(b"\n", i)
        head = data[i:nl].decode(errors="replace")
        i = nl + 1
        if head.endswith((" missing", " ambiguous")):   # «<имя> missing»: в имени бывают пробелы
            continue
        size = int(head.rsplit(" ", 1)[-1])                # «<sha> blob <размер>»
        out[p] = data[i:i + size].decode("utf-8", errors="replace")
        i += size + 1
    return out


def ls_tree(root, ref, *prefixes):
    if not ref:
        return []
    return [p for p in _out(root, "ls-tree", "-r", "--name-only", ref, "--", *prefixes).splitlines() if p]


def delta(root, base, end, paths=TRACKED):
    """Чистая дельта между срезами: [(status A/M/D, path)] (переименование = D + A)."""
    if not end:
        return []
    out = _out(root, "diff", "--name-status", "--no-renames", base or EMPTY_TREE, end, "--", *paths)
    res = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0][:1] in "AMD":
            res.append((parts[0][:1], parts[-1]))
    return res


def added_lines(root, base, end, path):
    if not end:
        return []
    out = _out(root, "diff", "-U0", base or EMPTY_TREE, end, "--", path, check=False)
    return [ln[1:] for ln in out.splitlines() if ln.startswith("+") and not ln.startswith("+++")]


def window_commits(root, w, end):
    """Коммиты окна (вся достижимая история): [{sha, date, subject, merge, first_parent, files}]."""
    if not end:
        return []
    fmt = "%x1e%h%x1f%cI%x1f%P%x1f%s"
    raw = _out(root, "log", f"--since={_iso(w.since_ts)}", f"--until={_iso(w.until_ts)}", f"--format={fmt}",
               "--name-only", end)
    fp = set(_out(root, "log", "--first-parent", f"--since={_iso(w.since_ts)}", f"--until={_iso(w.until_ts)}",
                  "--format=%h", end).split())
    out = []
    for chunk in raw.split("\x1e"):
        if not chunk.strip():
            continue
        head, _, files = chunk.partition("\n")
        sha, date, parents, subject = (head.split("\x1f") + ["", "", "", ""])[:4]
        out.append({"sha": sha, "date": date[:10], "subject": subject.strip(), "merge": len(parents.split()) > 1,
                    "first_parent": sha in fp, "files": [f for f in files.splitlines() if f.strip()]})
    return out


# ---------- карточки ----------

def card_name(path, text):
    fm, _ = parse_frontmatter(text)
    return str((fm or {}).get("name") or os.path.splitext(os.path.basename(path))[0])


def card_area(path):
    parts = path.split("/")
    if len(parts) >= 4 and parts[1] == "projects":
        return "projects/" + parts[2]
    return parts[1] if len(parts) >= 3 else ""


def is_card_path(path):
    base = os.path.basename(path)
    return (path.startswith("cards/") and path.endswith(".md") and not base.startswith(("_", "."))
            and not path.startswith(("cards/private/", "cards/_templates/")))


def history(text):
    """→ [(дата, текст, служебная?)] из раздела «## История»."""
    out, on = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            on = line.strip() == "## История"
            continue
        if on:
            m = HIST_LINE.match(line.strip())
            if m:
                t = m.group(2).strip()
                out.append((m.group(1), RUN_ID.sub(" ", t).strip(" .;"), bool(MECH_HIST.search(t))))
    return out


def essence(text):
    _, body = parse_frontmatter(text)
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("**Суть.**"):
            return _clip(s[len("**Суть.**"):].strip(), 300)
    return ""


def _clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def links(text):
    return set(m.strip() for m in re.findall(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]", text))


# ---------- проекты ----------

def projects_at(root, ref):
    """{slug: {title, file, area, text, links}} по projects/*.md на коммите ref (или рабочее дерево, если ref=None)."""
    if ref:
        paths = [p for p in ls_tree(root, ref, "projects") if p.endswith(".md")]
        blobs = read_blobs(root, ref, paths)
    else:
        d = os.path.join(root, "projects")
        blobs = {}
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if fn.endswith(".md"):
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    blobs["projects/" + fn] = f.read()
    out = {}
    for path, text in sorted(blobs.items()):
        fm, body = parse_frontmatter(text)
        fm = fm or {}
        slug = str(fm.get("name") or os.path.splitext(os.path.basename(path))[0])
        m = re.search(r"cards/([a-z0-9][a-z0-9/-]*)", str(fm.get("source") or ""))
        title = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), slug)
        out[slug] = {"slug": slug, "title": title, "file": path, "area": m.group(1).rstrip("/") if m else None,
                     "text": text, "links": links(text)}
    return out


def project_of(path, name, projects):
    for slug, p in projects.items():
        if p["area"] and path.startswith(f"cards/{p['area']}/"):
            return slug
    for slug, p in projects.items():
        if name in p["links"]:
            return slug
    return None


def section(text, heading_prefix):
    """Текст раздела `## <heading_prefix>…` до следующего `## `."""
    out, on = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            if on:
                break
            on = line[3:].strip().split(" ", 1)[-1].startswith(heading_prefix) or \
                line[3:].strip().startswith(heading_prefix)
            continue
        if on:
            out.append(line)
    return "\n".join(out).strip()


# ---------- сбор ----------

def _dm(path):
    """proposals/2026-10-24-blog.md → 24.10 (подпись ссылки)."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", os.path.basename(path))
    return f"{m.group(3)}.{m.group(2)}" if m else os.path.basename(path)


def seen_keys(truth):
    """Ключи пунктов прошлого обзора: события карточек, решения, споры — чтобы не повторять их на стыке окон."""
    out = set()
    for it in (truth or {}).get("items") or []:
        if it.get("kind") == "card":
            out |= {("ev", it["name"], e["date"], e["text"]) for e in it.get("events") or []}
        elif it.get("kind") in ("decision", "conflict_new"):
            out.add((it["kind"], it.get("name") or "", it.get("text") or ""))
    return out


def collect(cfg, w, cards_ref=None, consolidate=None, proposals=None, skills_drift=None, health_now=None,
            seen=None):
    """→ правда недели (dict, сериализуемый в JSON).

    cards_ref — коммит, с которого читаются карточки для событий «## История» (по умолчанию конец окна; эвал
    прошлых недель берёт фиксированный срез — «синтетическая» неделя). health_now — читать рабочий
    state/health.json (обзор текущей недели), иначе срез из git на конец окна."""
    root = cfg.root
    seen = seen or set()
    base = commit_before(root, w.since_ts)
    end = commit_before(root, w.until_ts)
    cref = cards_ref or end
    projects = projects_at(root, cref) if cref else projects_at(root, None)

    # карточки на срезе: события «## История» и метаданные
    card_paths = [p for p in ls_tree(root, cref, "cards") if is_card_path(p)] if cref else []
    blobs = read_blobs(root, cref, card_paths)
    names = {}
    for p, t in blobs.items():
        names[p] = card_name(p, t)
    d = delta(root, base, end)
    changed = {p: st for st, p in d if is_card_path(p)}
    if end and end != cref:  # метаданные изменённых карточек — с конца окна
        extra = [p for p, st in changed.items() if st != "D" and p not in blobs]
        for p, t in read_blobs(root, end, extra).items():
            blobs[p] = t
            names[p] = card_name(p, t)
    deleted_blobs = read_blobs(root, base, [p for p, st in changed.items() if st == "D"]) if base else {}
    for p, t in deleted_blobs.items():
        names[p] = card_name(p, t)

    items = []
    by_area = {}
    for p, st in changed.items():
        a = by_area.setdefault(card_area(p), {"added": 0, "modified": 0, "deleted": 0})
        a[{"A": "added", "M": "modified", "D": "deleted"}[st]] += 1

    for p in sorted(set(blobs) | set(changed)):
        text = blobs.get(p) or deleted_blobs.get(p) or ""
        if not text:
            continue
        fm, _ = parse_frontmatter(text)
        fm = fm or {}
        name = names.get(p) or card_name(p, text)
        hist = history(text)
        events = [{"date": dd, "text": _clip(t, 240)} for dd, t, mech in hist if not mech and w.has_date(dd)
                  and ("ev", name, dd, _clip(t, 240)) not in seen]
        st = changed.get(p)
        changes = [{"date": dd, "text": _clip(t, 240)} for dd, t, mech in hist if mech and w.has_date(dd)
                   and not t.lower().startswith(("add", "создана"))] if st else []
        if not events and not st:
            continue
        proj = project_of(p, name, projects)
        it = {"kind": "card", "ref": f"[[{name}]]", "name": name, "path": p, "area": card_area(p),
              "project": proj, "change": {"A": "added", "M": "modified", "D": "deleted"}.get(st),
              "type": str(fm.get("type") or ""), "status": str(fm.get("status") or ""),
              "importance": str(fm.get("importance") or "normal"),
              "core": str(fm.get("core")).lower() == "true", "verified_how": str(fm.get("verified_how") or ""),
              "description": _clip(str(fm.get("description") or ""), 240), "essence": essence(text),
              "events": events[-4:], "changes": changes[-3:]}
        it["score"] = card_score(it, projects)
        items.append(it)
    items.sort(key=lambda x: (-x["score"], x["name"]))
    for i, it in enumerate(items):
        it["significant"] = i < SIGNIFICANT_CARDS and it["score"] >= 3

    commits = window_commits(root, w, end)
    items += commit_items(commits, projects)
    items += [it for it in decision_items(root, w, base, end, cref) if ("decision", "", it["text"]) not in seen]
    conflicts = conflicts_info(root, w, base, end)
    cnew = conflicts.pop("items")
    items += [it for it in cnew if (it["kind"], it.get("name") or "", it["text"]) not in seen]
    runs = runs_info(cfg, w)
    items += runs.pop("items")
    health = health_info(root, end, health_now)
    items.append(health.pop("item"))
    if consolidate is not None:
        items.append(consolidate_item(consolidate))
    for pr in proposals or []:
        files = pr.get("files") or [pr["file"]]
        ref = f"[предложения]({files[-1]})" if len(files) == 1 else \
            "предложения " + ", ".join(f"[{_dm(f)}]({f})" for f in files)
        items.append({"kind": "proposal", "ref": ref, "project": pr["project"],
                      "text": f"{pr['n']} правок раздела «Текущее состояние» карточки {pr['project']}"
                              + (f" (файлов за окно: {len(files)})" if len(files) > 1 else "")
                              + (" (собрано без модели)" if pr.get("fallback") else ""),
                      "items": pr.get("preview", [])[:3], "significant": True, "score": 9})
    if skills_drift:
        items.append(skills_drift)
    for i, it in enumerate(items, 1):
        it["id"] = f"F{i}"

    proj_out = {}
    for slug, p in projects.items():
        proj_out[slug] = {"title": p["title"], "file": p["file"], "area": p["area"],
                          "items": [it["id"] for it in items if it.get("project") == slug],
                          "open_questions": open_questions(p["text"])}
    totals = {k: sum(a[k] for a in by_area.values()) for k in ("added", "modified", "deleted")}
    return {
        "window": w.as_dict(), "base_commit": base, "end_commit": end, "cards_ref": cref,
        "base": {"cards": totals, "by_area": dict(sorted(by_area.items())), "commits": len(commits),
                 "commits_meaningful": sum(1 for c in commits if not MECH_COMMIT.match(c["subject"]))},
        "projects": proj_out, "conflicts": conflicts, "runs": runs, "health": health,
        "items": items,
        "significant": [it["id"] for it in items if it.get("significant")],
    }


def card_score(it, projects):
    s = 0.0
    if it["events"]:
        s += 3 + min(len(it["events"]) - 1, 2)
    s += {"modified": 2, "added": 0.5, "deleted": 1}.get(it["change"], 0)
    if it["changes"]:
        s += 1
    if it["status"] == "superseded" and it["change"]:
        s += 1
    if it["type"] == "decision":
        s += 2
    if it["core"]:
        s += 2
    s += {"high": 2, "low": -1}.get(it["importance"], 0)
    if any(it["name"] in p["links"] for p in projects.values()):
        s += 1
    if it["verified_how"] == "live":
        s += 1
    if it["name"].endswith("-hub"):
        s -= 2
    return round(s, 2)


def commit_items(commits, projects):
    """Значимые коммиты: слияния экспериментов и прямые правки верхнего слоя; служебные — только счётчиком."""
    out = []
    picked = [c for c in commits if not MECH_COMMIT.match(c["subject"])
              and (c["merge"] or (c["first_parent"] and any(f.startswith(TOP_LAYER) for f in c["files"])))]
    picked = [c for c in picked if not (c["merge"] and c["subject"].lower().startswith("merge branch"))]
    rest = [c for c in commits if not MECH_COMMIT.match(c["subject"]) and c not in picked and c["first_parent"]]
    for i, c in enumerate(picked):
        proj = next((s for s, p in projects.items() if p["file"] in c["files"]), None)
        out.append({"kind": "commit", "ref": f"коммит {c['sha']}", "sha": c["sha"], "date": c["date"],
                    "project": proj, "text": _clip(c["subject"], 240),
                    "top_layer": sorted({f.split("/")[0] for f in c["files"] if f.startswith(TOP_LAYER)}),
                    "significant": i < SIGNIFICANT_COMMITS, "score": 5})
    for c in rest[:10]:
        out.append({"kind": "commit", "ref": f"коммит {c['sha']}", "sha": c["sha"], "date": c["date"],
                    "project": None, "text": _clip(c["subject"], 200), "significant": False, "score": 1})
    return out


def decision_items(root, w, base, end, cref):
    out = []
    ref = end or cref
    for p in ls_tree(root, ref, "decisions"):
        if not p.endswith(".md") or p.endswith("README.md"):
            continue
        m = re.match(r"decisions/(\d{4}-\d{2}-\d{2})", p)
        if not m or not w.has_date(m.group(1)):
            continue
        text = read_blobs(root, ref, [p]).get(p, "")
        _, body = parse_frontmatter(text)
        title = next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("#")), p)
        out.append({"kind": "decision", "ref": f"[решение]({p})", "date": m.group(1), "project": None,
                    "text": _clip(title, 240), "significant": True, "score": 8})
    text = read_blobs(root, ref, ["decisions.md"]).get("decisions.md", "") if ref else ""
    for line in text.splitlines():
        m = DECISION_LINE.match(line.strip())
        if m and w.has_date(m.group(1)):
            out.append({"kind": "decision", "ref": "[decisions.md](decisions.md)", "date": m.group(1),
                        "project": None, "text": _clip(re.sub(r"\*\*", "", m.group(2)), 260),
                        "significant": True, "score": 7})
    return out


def conflicts_info(root, w, base, end):
    text = read_blobs(root, end, ["CONFLICTS.md"]).get("CONFLICTS.md", "") if end else ""
    opened = [m for m in (CONFLICT_LINE.match(ln.strip()) for ln in text.splitlines()) if m and m.group(1) == " "]
    added = set(ln.strip() for ln in added_lines(root, base, end, "CONFLICTS.md"))
    items, new, closed = [], [], []
    for m in opened:
        if w.has_date(m.group(2)):
            new.append(m)
    for ln in added:
        m = CONFLICT_LINE.match(ln)
        if m and m.group(1) == "x":
            closed.append(m)
    new.sort(key=lambda m: m.group(2), reverse=True)
    for i, m in enumerate(new):
        items.append({"kind": "conflict_new", "ref": "[CONFLICTS.md](CONFLICTS.md)", "date": m.group(2),
                      "name": m.group(3), "project": None, "text": _clip(_no_sources(m.group(4)), 260),
                      "significant": i < 5, "score": 6})
    for m in closed:
        to = re.search(r"→ (decisions/\S+\.md)", m.group(4))
        items.append({"kind": "conflict_closed", "ref": f"[решение]({to.group(1)})" if to else
                      "[CONFLICTS.md](CONFLICTS.md)", "date": m.group(2), "name": m.group(3), "project": None,
                      "text": _clip(_no_sources(m.group(4)), 200), "significant": False, "score": 3})
    oldest = min((m.group(2) for m in opened), default=None)
    return {"open_total": len(opened), "new_in_window": len(new), "closed_in_window": len(closed),
            "oldest_open": oldest, "items": items}


def _no_sources(s):
    s = re.sub(r"\s*\((raw|cards)/[^()]*\)\s*$", "", s)
    return re.sub(r"\s*→ decisions/\S+", "", s)


def runs_info(cfg, w):
    by = {}
    fails = {}
    for r in read_runs(cfg):
        ts = parse_iso(r.get("ts"))
        if not w.has_ts(ts):
            continue
        cmd = r.get("cmd") or "?"
        st = r.get("status")
        by.setdefault(cmd, {})
        by[cmd][str(st)] = by[cmd].get(str(st), 0) + 1
        if cmd.endswith("_batch"):
            continue  # батчи сводчика — внутри итоговой строки consolidate
        if st not in OK_STATUSES:
            k = (cmd, str(st), r.get("step"))
            f = fails.setdefault(k, {"cmd": cmd, "status": st, "step": r.get("step"), "count": 0})
            f["count"] += 1
            f["last_ts"] = r.get("ts")
            err = r.get("error") or "; ".join(map(str, (r.get("errors") or [])[:2]))
            if err:
                f["last_error"] = _clip(str(err), 200)
    items = []
    for f in sorted(fails.values(), key=lambda x: (x["cmd"], str(x["status"]))):
        what = f"{f['cmd']}" + (f"/{f['step']}" if f.get("step") else "")
        items.append({"kind": "run_failure", "ref": "[журнал прогонов](state/runs.jsonl)", "project": None,
                      "date": (f.get("last_ts") or "")[:10],
                      "text": f"{what}: {f['status']} ×{f['count']}" + (f" — {f['last_error']}" if f.get("last_error")
                                                                        else ""),
                      "significant": f["cmd"] in ("consolidate", "ingest", "weekly", "daily", "index"),
                      "score": 6})
    return {"by_cmd": by, "failures": len(fails), "items": items}


def health_info(root, end, health_now):
    h, src = None, None
    if health_now:
        try:
            with open(health_now, encoding="utf-8") as f:
                h, src = json.load(f), "state/health.json"
        except (OSError, ValueError):
            h = None
    if h is None and end:
        t = read_blobs(root, end, ["state/health.json"]).get("state/health.json")
        if t:
            try:
                h, src = json.loads(t), f"state/health.json@{end[:7]}"
            except ValueError:
                h = None
    if not isinstance(h, dict):
        item = {"kind": "health", "ref": "[HEALTH.md](HEALTH.md)", "project": None, "text": "здоровье за окно: нет данных",
                "significant": True, "score": 6}
        return {"status": None, "problems": [], "source": None, "item": item}
    probs = [{"level": p.get("level"), "what": _clip(str(p.get("what") or ""), 160)}
             for p in (h.get("problems") or []) if isinstance(p, dict)]
    st = h.get("status")
    text = f"здоровье базы: {st}" + ("; " + "; ".join(p["what"] for p in probs[:4]) if probs else "")
    item = {"kind": "health", "ref": "[HEALTH.md](HEALTH.md)", "project": None, "text": text, "significant": True,
            "score": 6}
    return {"status": st, "problems": probs, "source": src, "generated": h.get("generated") or h.get("ts"),
            "item": item}


def consolidate_item(c):
    st = c.get("status")
    if c.get("source") in ("daily", "weekly"):
        text = (f"сводчик только что отработал в {'ежедневном' if c['source'] == 'daily' else 'прошлом недельном'} "
                f"прогоне (kb {c['source']}): карточек затронуто {c.get('cards_touched') or 0}")
    elif st == "skipped":
        text = "сводчик в этом прогоне не запускался (--skip-consolidate)"
    elif st in ("ok",):
        text = (f"сводчик: ok, карточек затронуто {c.get('cards_touched', 0)}, новых споров {c.get('conflicts_new', 0)}"
                + (f", слито в main {c['merged']}" if c.get("merged") else ""))
    elif st == "dry_run":
        text = "сводчик: только план (--dry-run), без записи"
    elif st == "deferred":
        text = "сводчик отложен по порогу: " + (c.get("reason") or "мало новых единиц сырья") + " — не сбой"
    elif st == "no_units":
        text = "сводчик: новых единиц сырья к сводке нет — LLM не вызывался"
    else:
        text = f"сводчик: сбой ({st})" + (f" — {_clip(c.get('error') or '', 200)}" if c.get("error") else "") + \
            "; обзор собран по тому, что уже было в базе"
    wk = c.get("week")
    if wk and wk.get("runs"):
        text += (f"; за окно прогонов сводчика {wk['runs']}" + (f", из них со сбоем {wk['failed']}" if wk.get("failed")
                                                                 else "")
                 + f", правок карточек по прогонам {wk.get('cards_touched') or 0}"
                 + (f", отложено по порогу {wk['deferred']}" if wk.get("deferred") else ""))
    return {"kind": "consolidate", "ref": "[журнал прогонов](state/runs.jsonl)", "project": None, "text": text,
            "status": st, "significant": st not in ("skipped", "dry_run", "deferred", "no_units") or bool(c.get("source")),
            "score": 7}


def open_questions(text):
    sec = section(text, "Открытые вопросы")
    out = []
    for line in sec.splitlines():
        m = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if m:
            out.append(_clip(m.group(1), 220))
    return out[:3]


def for_llm(truth, max_items=MAX_ITEMS_LLM):
    """Компактная версия для промпта: значимые пункты целиком + лучшие из остальных до лимита."""
    sig = [it for it in truth["items"] if it.get("significant")]
    rest = sorted((it for it in truth["items"] if not it.get("significant")), key=lambda x: -x.get("score", 0))
    keep = sig + rest[:max(0, max_items - len(sig))]
    order = {it["id"]: i for i, it in enumerate(truth["items"])}
    keep.sort(key=lambda it: order[it["id"]])
    drop = ("path", "score", "verified_how", "importance", "core", "sha", "top_layer")
    slim = [{k: v for k, v in it.items() if k not in drop and v not in (None, "", [], False)} for it in keep]
    return {"window": truth["window"], "base": truth["base"], "projects": truth["projects"],
            "conflicts": truth["conflicts"], "runs": {"failures": truth["runs"]["failures"]},
            "health": truth["health"], "items": slim, "significant": truth["significant"],
            "omitted_items": len(truth["items"]) - len(keep)}


def known_refs(truth):
    """Ссылки, которые разрешено ставить в обзоре: из пунктов правды и служебные файлы."""
    refs = set()
    for it in truth["items"]:
        refs.add(it["ref"])
    for p in truth["projects"].values():
        refs.add(f"[карточка]({p['file']})")
    return refs
