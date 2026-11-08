"""kb health: state/health.json + HEALTH.md (их читают сторож-алерты и панель bb «Библиотека»)."""
import datetime as dt
import hashlib
import json
import os
import re
import time

import cards as cardsmod
from ingest import VPS_SOURCES
from kbcore import (commit_paths, free_gb, git, iso, parse_iso, read_runs, repo_lock, LockTimeout)

HUB_LLM_COOLDOWN_DAYS = 7     # как consolidate.run.HUB_LLM_COOLDOWN_DAYS: пауза повтора пересборки хаба после отказа

H = 3600
D = 86400
ORDER = {"green": 0, "yellow": 1, "red": 2}


def raw_stats(cfg, src):
    base = os.path.join(cfg.raw, src)
    n, newest = 0, 0.0
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            n += 1
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, fn)))
            except OSError:
                pass
    return n, newest


def conflicts(cfg, now):
    """Открытые конфликты — строки `- [ ] ГГГГ-ММ-ДД …` в CONFLICTS.md."""
    n, oldest = 0, None
    try:
        text = open(os.path.join(cfg.root, "CONFLICTS.md"), encoding="utf-8").read()
    except OSError:
        return {"open": 0, "oldest": None}
    for line in text.splitlines():
        m = re.match(r"^\s*[-*] \[ \]\s*(\d{4}-\d{2}-\d{2})?", line)
        if m:
            n += 1
            if m.group(1) and (oldest is None or m.group(1) < oldest):
                oldest = m.group(1)
    res = {"open": n, "oldest": oldest}
    if oldest:
        ts = parse_iso(oldest + "T00:00:00Z")
        res["oldest_age_days"] = round((now - ts) / D, 1) if ts else None
    return res


# в карантине: «sha256  путь» (или просто путь — тогда без проверки содержимого) файлов, которые владелец решил
# не разбирать; # — комментарий; повтор пути — действует последняя строка
ACK_FILE = ".ack"


def _norm_rel(path):
    path = os.path.normpath(path.strip().lstrip("*"))   # `sha256sum -b` пишет «хэш *путь»
    if path.startswith("raw/.quarantine/"):
        path = path[len("raw/.quarantine/"):]
    return path


def quarantine_acked(cfg):
    """→ {путь: sha256 или None}. С хэшем подтверждение действует, только пока содержимое не изменилось."""
    out = {}
    try:
        with open(os.path.join(cfg.quarantine, ACK_FILE), encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                parts = ln.split(None, 1)
                if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                    out[_norm_rel(parts[1])] = parts[0].lower()
                else:
                    out[_norm_rel(ln)] = None
    except OSError:
        pass
    return out


def _sha256(path):
    """→ хэш или None, если файл пропал или не читается (тогда он не считается подтверждённым)."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 16), b""):
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def quarantine_count(cfg):
    """→ (ждут разбора, отложено решением владельца). Отложенные не дают yellow; новые файлы — дают."""
    acked = quarantine_acked(cfg)
    n = a = 0
    for dirpath, dirs, files in os.walk(cfg.quarantine):
        for name in files:
            rel = os.path.relpath(os.path.join(dirpath, name), cfg.quarantine)
            if rel == ACK_FILE:
                continue
            if rel in acked and (acked[rel] is None or acked[rel] == _sha256(os.path.join(dirpath, name))):
                a += 1
            else:
                n += 1
    return n, a


def compute(cfg, now=None):
    now = now or time.time()
    problems = []

    def flag(level, what):
        problems.append({"level": level, "what": what})

    runs = read_runs(cfg)
    ing = [r for r in runs if r.get("cmd") == "ingest"]

    sources = {}
    for src in VPS_SOURCES:
        recs = [r for r in ing if r.get("source") == src and not r.get("event")]
        last = recs[-1] if recs else None
        ok = [r for r in recs if r.get("status") == "ok"]
        last_ok_ts = parse_iso(ok[-1]["ts"]) if ok else None
        n, newest = raw_stats(cfg, src)
        info = {"files": n, "newest_content": iso(newest) if newest else None,
                "last_attempt": last.get("ts") if last else None,
                "last_status": last.get("status") if last else None,
                "last_error": last.get("error") if last else None,
                "last_ok": iso(last_ok_ts) if last_ok_ts else None,
                "ok_age_h": round((now - last_ok_ts) / H, 1) if last_ok_ts else None,
                "mode": last.get("mode") if last else None}
        age = (now - last_ok_ts) if last_ok_ts else None
        if age is None or age > 24 * H:
            info["state"] = "stale"
            flag("red", f"raw/{src}: не обновлялось больше 24 ч")
        elif age > 3 * H or (last and last.get("status") != "ok"):
            info["state"] = "stale"
            flag("yellow", f"raw/{src}: последняя попытка {info['last_status']}, "
                           f"удачная {info['ok_age_h']} ч назад")
        else:
            info["state"] = "ok"
        sources[src] = info

    done = [r for r in ing if r.get("event") == "done"]
    last_done = done[-1] if done else None
    ok_done = [r for r in done if r.get("status") == "ok"]
    ingest = {"last_run": last_done.get("ts") if last_done else None,
              "last_status": last_done.get("status") if last_done else None,
              "last_error": last_done.get("error") if last_done else None,
              "last_ok": ok_done[-1]["ts"] if ok_done else None,
              "last_commit": next((r.get("commit") for r in reversed(done) if r.get("commit")), None)}
    last_done_ts = parse_iso(ingest["last_run"])
    last_ok_done_ts = parse_iso(ingest["last_ok"])
    if last_done_ts is None or now - last_done_ts > 3 * H:
        flag("yellow", "ingest не запускался больше 3 ч")
    if last_done and last_done.get("status") in ("disk_low", "lock_timeout"):
        flag("red" if last_done["status"] == "disk_low" else "yellow",
             f"ingest остановлен: {last_done.get('error')}")
    if last_ok_done_ts is None or now - last_ok_done_ts > 24 * H:
        flag("red", "нет успешного ingest больше 24 ч")

    cons = [r for r in runs if r.get("cmd") == "consolidate"]
    consolidator = {"last_run": cons[-1].get("ts") if cons else None,
                    "last_status": cons[-1].get("status") if cons else None}
    # отложенный по порогу прогон (deferred, kb daily: < 3 единиц) — не сбой и не «запускался»: судим по последнему
    # настоящему прогону
    real = [r for r in cons if r.get("status") not in ("deferred", "no_units")]
    if real:
        ts = parse_iso(real[-1].get("ts"))
        if real[-1].get("status") not in ("ok", None) and not cons[-1].get("merged"):
            last = real[-1]
            why = ""
            g = last.get("gates") or {}
            if last.get("status") == "gates_failed" and g:
                parts = [f"{k}: {', '.join(str(x).split(' (')[0] for x in g[k][:3])}"
                         for k in ("near_duplicates", "sensitive", "lint_errors", "secrets") if g.get(k)]
                if g.get("quote_100") is False:
                    parts.append("не все цитаты подтверждены")
                if g.get("new_conflicts", 0) >= 10:          # порог гейта по умолчанию (max_conflicts)
                    parts.append(f"новых конфликтов {g['new_conflicts']}")
                why = " — почему: " + "; ".join(parts) if parts else ""
            # с какого сбоя правки не попадают в main: идём назад до прогона, который слил ветку (merged) или прошёл ok;
            # отложенные/холостые без слияния не прерывают и не считаются сбоем
            since = None
            for r in reversed(cons):
                if r.get("mode") == "dry-run" or r.get("dry_run"):
                    continue
                if r.get("merged") or r.get("status") == "ok":
                    break
                if r.get("status") not in ("deferred", "no_units") and parse_iso(r.get("ts")):
                    since = r.get("ts")
            stuck_d = (now - parse_iso(since)) / D if since else 0
            consolidator["stuck_since"] = since
            flag("red" if stuck_d >= 3 else "yellow",
                 f"сводчик: последний прогон {last.get('status')}"
                 + (f", правки не вливаются в main {stuck_d:.0f} дн" if stuck_d >= 1 else "") + why)
        elif ts and now - ts > 14 * D:
            flag("yellow", "сводчик не запускался больше 14 дней")
    elif cons:
        consolidator["note"] = "только отложенные по порогу прогоны"
    else:
        consolidator["note"] = "ещё не запускался"

    # хабы: LLM на хабе отказывает подряд больше 7 дней — пересборка стоит, нужен человек (state/hubs/*.json).
    # Отказы подряд — llm_failed_since (есть и у области без хаба); горит, пока отказ свежий: последний
    # llm_failed_at не старше паузы повтора + 1 день (перестали звать LLM — сигнал гаснет сам)
    hubs_dir = os.path.join(cfg.state, "hubs")
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    for fn in sorted(os.listdir(hubs_dir)) if os.path.isdir(hubs_dir) else []:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(hubs_dir, fn), encoding="utf-8") as f:
                hb = json.load(f)
            if not isinstance(hb, dict) or not hb.get("llm_failed_since") or not hb.get("llm_failed_at"):
                continue
            since = dt.date.fromisoformat(str(hb["llm_failed_since"]))
            last = dt.date.fromisoformat(str(hb["llm_failed_at"]))
        except (OSError, ValueError, TypeError):
            continue
        if (today - since).days > 7 and (today - last).days <= HUB_LLM_COOLDOWN_DAYS + 1:
            flag("yellow", f"хаб {fn[:-5].replace('--', '/')}: пересборка LLM отказывает с {since.isoformat()} "
                           f"(state/hubs/{fn})")

    conf = conflicts(cfg, now)
    if conf["open"] and (conf.get("oldest_age_days") or 0) > 14:
        flag("yellow", f"CONFLICTS: {conf['open']} открытых, самому старому {conf['oldest_age_days']} дн")

    q, q_acked = quarantine_count(cfg)
    if q:
        flag("yellow", f"карантин: {q} файлов ждут разбора (raw/.quarantine/)")

    free = round(free_gb(cfg.root), 1)
    disk = {"free_gb": free, "min_gb": cfg.min_free_gb}
    if free < cfg.min_free_gb:
        flag("red", f"диск: свободно {free} ГБ < {cfg.min_free_gb:g} ГБ")
    elif free < 2 * cfg.min_free_gb:
        flag("yellow", f"диск: свободно {free} ГБ")

    errs, warns = cardsmod.lint(cfg)
    lint = {"errors": len(errs), "warnings": len(warns),
            "cards": len(cardsmod.iter_cards(cfg, include_private=True))}
    if errs:
        flag("yellow", f"lint: {len(errs)} ошибок в карточках")

    status = max((p["level"] for p in problems), key=lambda l: ORDER[l], default="green")
    return {"generated_at": iso(now), "status": status, "problems": problems,
            "sources": sources, "ingest": ingest,
            "consolidator": consolidator, "conflicts": conf, "quarantine": {"files": q, "acked": q_acked},
            "disk": disk, "lint": lint}


LABEL = {"green": "зелёный", "yellow": "жёлтый", "red": "красный"}


def render_md(h):
    L = [f"# Здоровье базы знаний — {LABEL[h['status']]} ({h['status']})", "",
         f"Сгенерировано `kb health`: {h['generated_at']}. Машинная версия — `state/health.json`.", ""]
    L.append("## Проблемы")
    L += [f"- **{p['level']}** — {p['what']}" for p in h["problems"]] or ["- нет"]
    L += ["", "## Сырьё по источникам", "",
          "| Источник | Файлов | Состояние | Удачный забор | Новейшее содержимое | Последняя ошибка |",
          "|---|---|---|---|---|---|"]
    for src, s in h["sources"].items():
        L.append(f"| {src} | {s['files']} | {s.get('state')} | {s.get('last_ok') or '—'} | "
                 f"{s.get('newest_content') or '—'} | {(s.get('last_error') or '—')[:80]} |")
    notes = sorted({s["note"] for s in h["sources"].values() if s.get("note")})
    L += [f"- {n}" for n in notes]
    i, c = h["ingest"], h["consolidator"]
    L += ["", "## Прогоны",
          f"- ingest: {i.get('last_run') or '—'} ({i.get('last_status') or '—'}), "
          f"последний коммит {i.get('last_commit') or '—'}",
          f"- сводчик: {c.get('last_run') or c.get('note') or '—'} ({c.get('last_status') or '—'})",
          f"- CONFLICTS открытых: {h['conflicts']['open']}, самый старый {h['conflicts'].get('oldest') or '—'}",
          f"- карантин: {h['quarantine']['files']} файлов ждут разбора"
          + (f", {h['quarantine'].get('acked')} отложено решением владельца" if h['quarantine'].get('acked') else ""),
          f"- lint: {h['lint']['errors']} ошибок, {h['lint']['warnings']} предупреждений, "
          f"{h['lint']['cards']} карточек"]
    L += ["", "## Диск",
          f"- диск: свободно {h['disk']['free_gb']} ГБ (жёсткий стоп < {h['disk']['min_gb']:g} ГБ)"]
    return "\n".join(L) + "\n"


def run_health(cfg, quiet=False):
    h = compute(cfg)
    os.makedirs(cfg.state, exist_ok=True)
    hp = os.path.join(cfg.state, "health.json")
    # Предыдущий статус — из git (HEAD), а не из рабочего файла: если коммит смены статуса
    # не прошёл, следующий прогон увидит расхождение и повторит его (#11)
    prev = None
    try:
        shown = git(cfg, "show", "HEAD:state/health.json", check=False)
        if shown.returncode == 0:
            prev = json.loads(shown.stdout).get("status")
    except (OSError, ValueError, AttributeError):
        pass
    for path, content in ((hp, json.dumps(h, ensure_ascii=False, indent=2) + "\n"),
                          (os.path.join(cfg.root, "HEALTH.md"), render_md(h))):
        tmp = path + ".kbtmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    # В git — только смены статуса (история переходов без ежечасного шума)
    if prev != h["status"]:
        try:
            with repo_lock(cfg, timeout=60):
                commit_paths(cfg, ["state/health.json", "HEALTH.md"],
                             f"health: {prev or '—'} → {h['status']}")
        except (LockTimeout, RuntimeError):
            pass
    if not quiet:
        print(f"status: {h['status']}")
        for p in h["problems"]:
            print(f"  {p['level']:<6} {p['what']}")
    return h
