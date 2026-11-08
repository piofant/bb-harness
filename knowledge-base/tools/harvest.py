"""kb harvest — ночное извлечение «итоги сессии → kb note» (ЭКСПЕРИМЕНТ, по умолчанию выключено).

  kb harvest [--dry-run] [--days N] [--max-sessions N]

Включение: `touch state/harvest.on` или KB_HARVEST=1; без этого боевой прогон — статус `disabled`, ничего не пишет.
`--dry-run` работает всегда: печатает кандидатов, ничего не пишет (так меряется эвал).

Что берётся: законченные (не менялись ≥ MIN_IDLE_S) основные сессии Claude (`~/.claude/projects/*/*.jsonl`) и
Codex (`~/.codex/sessions/**.jsonl`) за --days; без субагентов и сайдчейнов, с работой (≥ MIN_TOOLS вызовов
инструментов), не обработанные раньше (курсор `state/harvest.json`: путь → размер; обработанная сессия повторно
не берётся, даже если её продолжили, — иначе почти-дубли заметок; вычищаются только записи об удалённых файлах). Шаблонные
запросы (саммери для Telegram, судьи) и сессии с признаками личного/терапии (consolidate.areas.SENSITIVE_HINT
в репликах пользователя) отсекаются до LLM; заметки с этими признаками не пишутся.
Сессия сжимается (реплики, ошибки инструментов, итог) и уходит в `claude -p` (sonnet, без инструментов);
модель решает, была ли это работа (личное — ноль заметок), и выписывает до MAX_NOTES заметок
«симптом → причина → фикс», решение владельца, маршрут. Заметки пишутся тем же `note.note_vps` (гейт секретов,
дедуп, источник `сессия <host>:<id>`): в raw попадают только выписанные факты, не тексты сессии.
Телеметрия — `cmd: harvest` в runs.jsonl: на сессию (заметок, токены, длительность) и итог; текста нет.
"""
import json
import os
import re
import time

from consolidate.areas import SENSITIVE_HINT
from kbcore import AREA_HELP, log_run, new_run_id

MODEL = os.environ.get("KB_HARVEST_MODEL", "sonnet")
MIN_IDLE_S = 2 * 3600
MIN_TOOLS = 10
MAX_NOTES = 6
MAX_CHARS = 60000
SKIP_FIRST = re.compile(r"^(?:Сделай саммери ответа агента|Ты\s+[—-]\s+судья|Ты судья|You are a judge|"
                        r"Base directory for this skill)", re.I)
ERR_RX = re.compile(r"(?i)traceback|error|ошибк|failed|exit code [1-9]|not found|denied|refused|timed? ?out|"
                    r"no such file|command not found")
NOISE_USER = re.compile(r"^\s*<(?:system-reminder|command-|local-command|environment_context|user_instructions|"
                        r"permissions|turn_aborted)", re.I)

PROMPT_VER = "h1"
PROMPT = """Ниже сжатый транскрипт сессии ИИ-агента с владельцем базы знаний. USER — реплика владельца, AGENT — текст агента, TOOL — вызов инструмента, ERR — вывод с ошибкой.

1. work: true — агент выполнял рабочую задачу; false — личный разговор (здоровье, психология, отношения, дневник)
   или работы не было. При false — notes пустой.
2. notes — до {max_notes} заметок для базы знаний агентов: только то, без чего следующий агент в похожей задаче
   снова потратит время или сделает не так, как хочет владелец:
   - спотыкание: симптом → причина → что починило (дословно команда, флаг, путь, код ошибки);
   - решение или правило владельца, которое действует дальше;
   - рабочий маршрут/команда до цели, которую агент искал;
   - устойчивое предпочтение владельца по формату или процессу.
   Каждая заметка — самодостаточная, 1–3 строки, до 500 символов, по-русски, с конкретикой дословно.
   НЕЛЬЗЯ: личное, медицинское, контакты; тексты тикетов, вики, писем; данные пользователей продукта;
   секреты, токены, пароли и их части; разовые факты этой задачи («сделано», «отчёт готов»); догадки.
   Лучше ноль заметок, чем слабые.
   area — {areas}.
Ответ — только JSON: {{"work": true, "notes": [{{"text": "...", "area": "..."}}]}}

ТРАНСКРИПТ:
{tx}
"""


def enabled(cfg):
    return os.environ.get("KB_HARVEST") == "1" or os.path.exists(os.path.join(cfg.state, "harvest.on"))


# ---------- сессии ----------

def _text(content):
    if isinstance(content, str):
        return content
    return "\n".join((c.get("text") or "") for c in content or []
                     if isinstance(c, dict) and c.get("type") in ("text", "input_text", "output_text"))


def _short(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def parse_session(path):
    """→ (события [(вид, текст)], субагент?). Claude и Codex JSONL; битые строки пропускаются."""
    ev, side, codex = [], False, False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            if d.get("type") == "session_meta":
                codex = True
                s = (d.get("payload") or {}).get("source")
                side = side or (isinstance(s, dict) and "subagent" in s)
                continue
            if codex or "payload" in d:
                codex = True
                p = d.get("payload") or {}
                if d.get("type") != "response_item":
                    continue
                pt = p.get("type")
                if pt == "message":
                    t = _text(p.get("content"))
                    if p.get("role") == "user" and t.strip() and not NOISE_USER.match(t) \
                            and not t.lstrip().startswith("# AGENTS.md"):
                        ev.append(("USER", t))
                    elif p.get("role") == "assistant" and t.strip():
                        ev.append(("AGENT", t))
                elif pt in ("function_call", "custom_tool_call"):
                    ev.append(("TOOL", f"{p.get('name')}: {p.get('arguments') or p.get('input') or ''}"))
                elif pt in ("function_call_output", "custom_tool_call_output"):
                    o = p.get("output")
                    o = o.get("output") if isinstance(o, dict) else o
                    o = str(o or "")
                    m = re.search(r"(?:Exit code|exit_code\"?:)\s*(\d+)", o[:300])
                    if (m and m.group(1) != "0") or ERR_RX.search(o[:600]):
                        ev.append(("ERR", o))
                continue
            if d.get("isSidechain"):
                side = True
            msg = d.get("message") or {}
            c = msg.get("content")
            if d.get("type") == "user":
                if isinstance(c, str):
                    if not NOISE_USER.match(c):
                        ev.append(("USER", c))
                    continue
                for x in c or []:
                    if not isinstance(x, dict):
                        continue
                    if x.get("type") == "text" and not NOISE_USER.match(x.get("text") or ""):
                        ev.append(("USER", x.get("text")))
                    elif x.get("type") == "tool_result":
                        b = x.get("content")
                        b = _text(b) if isinstance(b, list) else str(b or "")
                        if x.get("is_error") or ERR_RX.search(b[:600]):
                            ev.append(("ERR", b))
            elif d.get("type") == "assistant":
                for x in c or []:
                    if isinstance(x, dict) and x.get("type") == "text" and (x.get("text") or "").strip():
                        ev.append(("AGENT", x["text"]))
                    elif isinstance(x, dict) and x.get("type") == "tool_use":
                        i = x.get("input") or {}
                        ev.append(("TOOL", f"{x.get('name')}: {i.get('command') or i.get('file_path') or ''}"))
    return ev, side


LIM = {"USER": 1200, "AGENT": 500, "TOOL": 140, "ERR": 240}


def condense(ev, max_chars=MAX_CHARS):
    """Компактный транскрипт: серии вызовов вдали от ошибок схлопываются; длинное — начало и конец."""
    near = set()
    for i, e in enumerate(ev):
        if e[0] == "ERR":
            near.update(range(i - 2, i + 4))
    lines, run = [], 0
    for i, (k, t) in enumerate(ev):
        if k == "TOOL" and i not in near:
            run += 1
            continue
        if run:
            lines.append(f"TOOL×{run}")
            run = 0
        lines.append(f"{k}: {_short(t, LIM[k])}")
    if run:
        lines.append(f"TOOL×{run}")
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:int(max_chars * 0.45)] + "\n…\n" + out[-int(max_chars * 0.5):]
    return out


def candidates(cfg, days, now=None):
    """Пути законченных основных сессий за окно, ещё не обработанных в этом размере."""
    now = now or time.time()
    roots = [os.environ.get("KB_HARVEST_CLAUDE", os.path.expanduser("~/.claude/projects")),
             os.environ.get("KB_HARVEST_CODEX", os.path.expanduser("~/.codex/sessions"))]
    seen = load_cursor(cfg)
    out = []
    for root in roots:
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in ("subagents", "memory") and not d.startswith(".")]
            for fn in files:
                if not fn.endswith(".jsonl") or fn.startswith("agent-"):
                    continue
                p = os.path.join(dp, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if now - st.st_mtime < MIN_IDLE_S or now - st.st_mtime > days * 86400:
                    continue
                if p in seen:
                    continue
                out.append((st.st_mtime, p, st.st_size))
    return [(p, size) for _, p, size in sorted(out)]


def _cursor_path(cfg):
    return os.path.join(cfg.state, "harvest.json")


def load_cursor(cfg):
    try:
        with open(_cursor_path(cfg), encoding="utf-8") as f:
            d = json.load(f)
        return d.get("seen") or {} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cursor(cfg, seen, prune=False):
    """Атомарно; prune — вычистить записи о файлах, которых больше нет (существующие не трогаем никогда:
    обработанная сессия не берётся повторно при любом --days)."""
    if prune:
        for p in list(seen):
            if not os.path.exists(p):
                del seen[p]
    os.makedirs(cfg.state, exist_ok=True)
    tmp = _cursor_path(cfg) + ".kbtmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ver": PROMPT_VER, "seen": seen}, f)
    os.replace(tmp, _cursor_path(cfg))


def session_id(path):
    m = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27,})", os.path.basename(path))
    return (m.group(1) if m else os.path.basename(path))[:36]


def extract(tx, llm_run=None):
    """→ (work, [{"text", "area"}], llm-ответ). Ответ без JSON — LLMError."""
    from consolidate import llm
    run = llm_run or llm.run
    r = run(PROMPT.format(max_notes=MAX_NOTES, areas=AREA_HELP, tx=tx), MODEL, timeout=900, cwd=os.environ.get("TMPDIR", "/tmp"))
    t = r["result"].strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    t = m.group(1) if m else t
    i, j = t.find("{"), t.rfind("}")
    try:
        d = json.loads(t[i:j + 1])
    except ValueError:
        raise llm.LLMError("harvest: нет JSON в ответе")
    if not isinstance(d, dict):
        raise llm.LLMError("harvest: ответ не объект")
    work = d.get("work") is True
    raw_notes = d.get("notes") if isinstance(d.get("notes"), list) else []
    notes = [n for n in raw_notes if isinstance(n, dict) and isinstance(n.get("text"), str) and n["text"].strip()
             and not SENSITIVE_HINT.search(n["text"])]
    return work, (notes[:MAX_NOTES] if work else []), r


def _flush_commit(cfg, n, out):
    """Очередь → raw/inbox/vps одним коммитом; lock занят — перенесёт ближайший kb ingest."""
    import note
    from kbcore import LockTimeout, commit_paths, current_branch, repo_lock
    try:
        if current_branch(cfg) != "main":
            return None
        with repo_lock(cfg, timeout=120):
            note.flush_pending(cfg, "vps")
            return commit_paths(cfg, ["raw/inbox/vps"], f"harvest: заметок {n} из сессий")
    except (LockTimeout, RuntimeError) as e:
        out(f"kb harvest: перенос очереди отложен до kb ingest ({type(e).__name__})")
        return None


def main(cfg, a, out=print, llm_run=None, now=None):
    import note
    from consolidate import llm
    run_id = new_run_id()
    t0 = time.time()
    dry = bool(getattr(a, "dry_run", False))
    os.environ["KB_HINT"] = "0"
    os.environ.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    if not dry and not enabled(cfg):
        log_run(cfg, cmd="harvest", run_id=run_id, status="disabled", duration_s=0)
        out("kb harvest выключен (эксперимент): включить — touch state/harvest.on или KB_HARVEST=1; "
            "посмотреть кандидатов без записи — kb harvest --dry-run")
        return 0
    seen = load_cursor(cfg)
    todo = candidates(cfg, a.days, now)[: a.max_sessions]
    tot = {"sessions": 0, "work": 0, "notes": 0, "written": 0, "dup": 0, "quarantined": 0, "invalid": 0,
           "skipped": 0}
    status = "ok"
    for path, size in todo:
        t = time.time()
        sid = session_id(path)
        host = "codex" if "/.codex/" in path or os.path.basename(path).startswith("rollout-") else "claude"
        try:
            ev, side = parse_session(path)
        except OSError as e:
            log_run(cfg, cmd="harvest", run_id=run_id, step="session", status="error", error=str(e)[:200])
            continue
        users = [x for x in ev if x[0] == "USER"]
        ntool = sum(1 for x in ev if x[0] == "TOOL")
        if side or not users or ntool < MIN_TOOLS or SKIP_FIRST.match(users[0][1].strip()) \
                or any(SENSITIVE_HINT.search(u[1]) for u in users):
            tot["skipped"] += 1
            if not dry:
                seen[path] = size
                save_cursor(cfg, seen)
            continue
        tot["sessions"] += 1
        try:
            work, notes, r = extract(condense(ev), llm_run)
        except llm.LimitHit as e:
            status = "limit"
            log_run(cfg, cmd="harvest", run_id=run_id, step="session", status="limit", error=str(e)[:200])
            break
        except llm.LLMError as e:
            log_run(cfg, cmd="harvest", run_id=run_id, step="session", status="error", error=str(e)[:200],
                    duration_s=round(time.time() - t, 2))
            continue
        tot["work"] += int(work)
        tot["notes"] += len(notes)
        written = []
        for n in notes:
            area = n.get("area") if isinstance(n.get("area"), str) else None
            area = (area or "").strip() or None
            if area and not note.AREA_RX.match(area):
                area = None
            if dry:
                out(f"[{sid[:8]}] ({area or '—'}) {n['text'].strip()}")
                continue
            res = {}
            note.note_vps(cfg, n["text"], area=area, source=f"сессия {host}:{sid}", out=lambda *_: None,
                          flush=False, result=res)
            st = res.get("status")
            if st == "duplicate":
                tot["dup"] += 1
            elif st == "quarantined":
                tot["quarantined"] += 1
            elif st == "invalid":
                tot["invalid"] += 1
            elif st in ("ok", "pending", "uncommitted"):
                tot["written"] += 1
                written.append(1)
        if not dry:
            seen[path] = size
            save_cursor(cfg, seen)              # после каждой сессии: убитый прогон не повторит её
        log_run(cfg, cmd="harvest", run_id=run_id, step="session", status="ok", mode="dry-run" if dry else "run",
                work=work, notes=len(notes), written=len(written), tokens_in=r.get("tokens_in"),
                tokens_out=r.get("tokens_out"), duration_s=round(time.time() - t, 2))
    commit = None
    if not dry:
        save_cursor(cfg, seen, prune=True)
        if tot["written"]:
            commit = _flush_commit(cfg, tot["written"], out)
    log_run(cfg, cmd="harvest", run_id=run_id, step="done", commit=commit, status=status, mode="dry-run" if dry else "run",
            candidates=len(todo), **tot, duration_s=round(time.time() - t0, 2))
    out(f"kb harvest: сессий {tot['sessions']} (пропущено {tot['skipped']}), рабочих {tot['work']}, "
        f"заметок {tot['notes']}" + ("" if dry else f", записано {tot['written']}, дублей {tot['dup']}, "
                                                    f"в карантин {tot['quarantined']}"))
    return 0 if status == "ok" else 5
