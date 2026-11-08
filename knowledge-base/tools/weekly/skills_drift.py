"""К5: сверка skills.md с реально установленными скиллами — раз в 4 недели, внутри недельного конвейера.

Только предложение в обзор: skills.md не правится. Личные и терапевтические скиллы не учитываются.
Снимок установленного — state/skills-snapshot.json: со второго прогона видно, что появилось и что пропало.
"""
import datetime as dt
import json
import os
import re
import shutil

# личные скиллы (KB_SKILLS_EXCLUDE — regex) не учитываются; копии-бэкапы (.x.bak-…) — не скиллы
_EXTRA = os.environ.get("KB_SKILLS_EXCLUDE", "")
EXCLUDE = re.compile(r"^\.|\.bak" + (f"|{_EXTRA}" if _EXTRA else ""))
EVERY_DAYS = 28
NAME = re.compile(r"`([a-z0-9][a-z0-9-]{2,}(?::[a-z0-9-]+)?)`")


def skill_dirs():
    env = os.environ.get("KB_SKILL_DIRS")
    if env:
        return [d for d in env.split(":") if d]
    home = os.path.expanduser("~")
    return [os.path.join(home, ".agents/skills"), os.path.join(home, ".claude/skills")]


def installed(dirs=None):
    out = set()
    for d in dirs or skill_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if os.path.isfile(os.path.join(d, n, "SKILL.md")) and not EXCLUDE.search(n):
                out.add(n)
    return out


def is_command(name, dirs=None):
    """`some-cli`, `other-cli` — команды (в PATH или scripts/ скилла), а не скиллы."""
    if shutil.which(name):
        return True
    for d in dirs or skill_dirs():
        try:
            for sk in os.listdir(d):
                if os.path.isfile(os.path.join(d, sk, "scripts", name)):
                    return True
        except OSError:
            continue
    return False


def mentioned(text):
    """→ (упомянутые имена, имена с пометкой «только на другой машине»)."""
    names, remote_only = set(), set()
    for line in text.splitlines():
        found = set(NAME.findall(line))
        names |= found
        if "только на другой машине" in line:
            remote_only |= found
    return names, remote_only


def due(runs, now_ts):
    """Пора ли: последний удачный шаг k5 конвейера старше 28 дней (или его не было)."""
    last = None
    for r in runs:
        if r.get("cmd") == "weekly" and r.get("step") == "k5" and r.get("status") == "ok" \
                and r.get("mode") != "dry-run":   # dry-run снимок не сохраняет и цикл не сдвигает
            last = r.get("ts")
    if not last:
        return True
    try:
        t = dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return True
    return now_ts - t >= EVERY_DAYS * 86400 - 3600


def check(cfg, dirs=None, save=True):
    """→ пункт правды (kind skills) или None, если расхождений нет."""
    with open(os.path.join(cfg.root, "skills.md"), encoding="utf-8") as f:
        text = f.read()
    inst = installed(dirs)
    names, remote_only = mentioned(text)
    names = {n for n in names if not EXCLUDE.search(n)}
    known_all = inst | names
    missing_doc = sorted(inst - names)                               # стоит, но в маршрутах нет
    # в маршрутах есть, здесь не стоит; команды (`some-cli`) — не скиллы
    gone = sorted(n for n in names - inst - remote_only if "-" in n and not is_command(n, dirs))
    snap_path = os.path.join(cfg.state, "skills-snapshot.json")
    prev = None
    try:
        with open(snap_path, encoding="utf-8") as f:
            prev = set(json.load(f).get("installed") or [])
    except (OSError, ValueError):
        prev = None
    new = sorted(inst - prev) if prev is not None else []
    removed = sorted(prev - inst) if prev is not None else []
    if save:
        os.makedirs(cfg.state, exist_ok=True)
        tmp = snap_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"installed": sorted(inst), "ts": dt.datetime.now(dt.timezone.utc).isoformat()}, f)
        os.replace(tmp, snap_path)
    stats = {"installed": len(inst), "mentioned": len(names & known_all), "missing_doc": len(missing_doc),
             "gone": len(gone), "new": new, "removed": removed}
    if not (missing_doc or gone or new or removed):
        return None, stats
    parts = []
    if new:
        parts.append(f"новые с прошлой сверки: {', '.join(new[:6])}" + (" и др." if len(new) > 6 else ""))
    if removed:
        parts.append(f"удалены: {', '.join(removed[:6])}")
    if gone:
        parts.append(f"упомянуты, но не установлены на VPS: {', '.join(gone[:6])}" + (" и др." if len(gone) > 6 else ""))
    if missing_doc and not new:
        parts.append(f"установлены, но без маршрута: {len(missing_doc)} (например {', '.join(missing_doc[:4])})")
    item = {"kind": "skills", "ref": "[skills.md](skills.md)", "project": None,
            "text": "предлагаю обновить маршруты skills.md — " + "; ".join(parts),
            "significant": True, "score": 6, "stats": {k: v for k, v in stats.items() if not isinstance(v, list)}}
    return item, stats
