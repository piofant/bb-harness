#!/usr/bin/python3
"""Фейковый `claude -p` для недельного конвейера: обзор (К3) и предложения (К2).

Читает промпт из stdin, пишет вызов в $FAKE_CLAUDE_LOG. $FAKE_WEEKLY_MODE:
  ok (по умолчанию) — обзор по шаблону со ссылками из правды; предложения — одно годное и одно с выдуманной цитатой;
  garbage — не-JSON / не обзор; error — ошибка CLI; nolinks — обзор, где пункты без ссылок и с выдуманной ссылкой;
  long — обзор на 70 строк.
"""
import json
import os
import re
import sys

prompt = sys.stdin.read()
argv = sys.argv[1:]
mode = os.environ.get("FAKE_WEEKLY_MODE", "ok")
stage = "k2" if "Текущее состояние» карточки проекта" in prompt else "k3"
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"stage": stage, "argv": argv, "env_kb_hint": os.environ.get("KB_HINT")},
                           ensure_ascii=False) + "\n")


def emit(result, is_error=False, rc=0):
    print(json.dumps({"type": "result", "is_error": is_error, "result": result,
                      "usage": {"input_tokens": len(prompt) // 4, "output_tokens": len(result) // 4},
                      "total_cost_usd": 0.0}, ensure_ascii=False))
    sys.exit(rc)


if mode == "error":
    emit("API Error: 500 internal", is_error=True, rc=1)
if mode == "garbage":
    print("not json at all")
    sys.exit(0)

if stage == "k2":
    names = re.findall(r"^### ([a-z0-9-]+) \(", prompt, re.M)
    quote = re.search(r"^  - \d{4}-\d{2}-\d{2}: (.+)$", prompt, re.M)
    items = []
    if names and quote:
        items.append({"subsection": "Что подтверждено", "was": "", "propose": "**Новое.** " + quote.group(1),
                      "basis": names[0], "quote": quote.group(1)[:120]})
        items.append({"subsection": "Что подтверждено", "was": "", "propose": "выдумка",
                      "basis": names[0], "quote": "этой фразы нет ни в одной карточке совсем"})
    emit(json.dumps({"items": items}, ensure_ascii=False))

m = re.search(r"Правда недели:\n(\{.*\})\s*$", prompt, re.S)
truth = json.loads(m.group(1))
title = truth["window"]["title"]
L = [f"# Неделя {title}", "", "## 1. Что продвинулось"]
for slug, p in truth["projects"].items():
    L.append(f"### {p['title']} — [карточка]({p['file']})")
    its = [it for it in truth["items"] if it["id"] in p["items"]]
    L += [f"- {it.get('description') or it.get('text')} ({it['ref']})" for it in its[:3]] or ["- без изменений"]
other = [it for it in truth["items"] if not it.get("project") and it["kind"] in ("card", "decision", "commit")]
if other:
    L.append("### Вне карточек проектов")
    L += [f"- {it.get('description') or it.get('text')} ({it['ref']})" for it in other[:3]]
L += ["", "## 2. Ждёт решения владельца"]
L += [f"1. {it['text']} ({it['ref']})" for it in truth["items"] if it["kind"] == "conflict_new"][:3] or ["- нет"]
L += ["", "## 3. Ждёт подтверждения: предложенные правки карточек проектов"]
L += [f"1. {it['text']} ({it['ref']})" for it in truth["items"] if it["kind"] in ("proposal", "skills")] or \
     ["- предложений нет"]
L += ["", "## 4. Здоровье базы", f"- Статус: {truth['health'].get('status')} ([HEALTH.md](HEALTH.md))",
      f"- Открытых споров: {truth['conflicts']['open_total']} — [CONFLICTS.md](CONFLICTS.md)"]
L += [f"- {it['text']} ({it['ref']})" for it in truth["items"] if it["kind"] in ("run_failure", "consolidate")]
if mode == "nolinks":
    L.insert(4, "- пункт без всякой ссылки")
    L.insert(5, "- выдуманный факт ([[no-such-card]])")
    L.insert(6, "- выдуманный коммит (коммит deadbee)")
if mode == "long":
    L[4:4] = [f"- пункт номер {i} ({truth['items'][0]['ref']})" for i in range(70)]
emit("```markdown\n" + "\n".join(L) + "\n```")
