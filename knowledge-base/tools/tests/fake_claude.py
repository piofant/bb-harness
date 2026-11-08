#!/usr/bin/python3
"""Фейковый `claude -p` для тестов сводчика.

Читает промпт из stdin, пишет вызов в $FAKE_CLAUDE_LOG (argv, модель, стадия).
Стадия 1: в каждой единице строки `CLAIM:<area>:<topic>: текст` → утверждение с цитатой = текст;
          `NOQUOTE:<area>: текст` → утверждение с выдуманной цитатой; `SENS: текст` → sensitive.
Стадия 2: $FAKE_S2_OPS — JSON {area: [ops]}; строки вида `@подстрока` в claims/superseded_claims и `⟦@подстрока⟧`
          в body заменяются id утверждения (нового или связанного с карточкой), чей текст содержит подстроку. Нет области в файле → ADD по темам.
Хаб: Суть с первым id из промпта и ссылки на все карточки.
$FAKE_CLAUDE_MODE=limit → ошибка лимита (limit-s2 — кроме стадии 1, limit-hub — только хаб); =garbage → не-JSON (garbage-s2 / garbage-hub — только на этой стадии);
=badjson-hub → хаб: ответ без JSON внутри валидного результата.
"""
import json
import os
import re
import sys

prompt = sys.stdin.read()
argv = sys.argv[1:]
model = argv[argv.index("--model") + 1] if "--model" in argv else ""
mode = os.environ.get("FAKE_CLAUDE_MODE", "")
if "извлекаешь знания" in prompt:
    stage = "s1"
elif "Собери хаб" in prompt:
    stage = "hub"
elif "почти дублируют" in prompt:
    stage = "dedup"
else:
    stage = "s2"
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"stage": stage, "model": model, "argv": argv, "prompt_len": len(prompt)},
                           ensure_ascii=False) + "\n")


def emit(result, is_error=False):
    print(json.dumps({"type": "result", "is_error": is_error, "result": result,
                      "usage": {"input_tokens": len(prompt) // 4, "output_tokens": len(result) // 4},
                      "total_cost_usd": 0.0}, ensure_ascii=False))


if mode == "limit" or (mode == "limit-s2" and stage != "s1") or (mode == "limit-hub" and stage == "hub"):
    emit("Claude AI usage limit reached|1792712808", is_error=True)
    sys.exit(1)
if mode == "garbage" or (mode == "garbage-s2" and stage == "s2") or (mode == "garbage-hub" and stage == "hub"):
    print("not json at all")
    sys.exit(0)

if stage == "s1":
    out = []
    for m in re.finditer(r"===== UNIT (\S+) \| kind=\S+ \| date=(\S*)(?: \| [^\n=]*)? =====\n(.*?)(?=\n===== UNIT |\Z)", prompt, re.S):
        uid, date, text = m.group(1), m.group(2), m.group(3)
        for line in text.splitlines():
            line = line.strip()
            c = re.match(r"CLAIM:([\w/-]+):([\w-]+):\s*(.+)$", line)
            if c:
                out.append({"unit": uid, "text": c.group(3), "kind": "rule", "topic": c.group(2),
                            "area": c.group(1), "date": date or "2026-10-01", "quote": c.group(3),
                            "sensitive": False})
            c = re.match(r"NOQUOTE:([\w/-]+):\s*(.+)$", line)
            if c:
                out.append({"unit": uid, "text": c.group(2), "kind": "fact", "topic": "misc", "area": c.group(1),
                            "date": date, "quote": "этой фразы нет в источнике ни в каком виде", "sensitive": False})
            c = re.match(r"SENS:\s*(.+)$", line)
            if c:
                out.append({"unit": uid, "text": c.group(1), "kind": "fact", "topic": "health", "area": "private",
                            "date": date, "quote": c.group(1), "sensitive": True})
    lines = [json.dumps(x, ensure_ascii=False) for x in out]
    if mode == "truncate":
        lines.append('{"unit": "x", "text": "оборва')
    emit("\n".join(lines))
    sys.exit(0)

claims = {}
for m in re.finditer(r"^c(\d+) \[(\w+)\|([\w-]+)\|[^\]]*\] (.+)$", prompt, re.M):
    claims[int(m.group(1))] = {"id": int(m.group(1)), "topic": m.group(3), "text": m.group(4)}


# утверждения, уже связанные с показанными карточками (короткий формат `cN [kind|date] текст`), — тоже по `@`
known = dict(claims)
for m in re.finditer(r"^c(\d+) \[(\w+)\|[^|\]]*\] (.+)$", prompt, re.M):
    known.setdefault(int(m.group(1)), {"id": int(m.group(1)), "topic": "", "text": m.group(3)})


def resolve(s):
    for cid, c in known.items():
        if s in c["text"]:
            return cid
    return 999999


if stage == "dedup" and mode == "distinct":
    emit(json.dumps({"distinct": True, "reason": "разные"}, ensure_ascii=False))
    sys.exit(0)
if stage == "dedup":
    rels = re.findall(r"^### cards/\S+/([a-z0-9-]+)\.md$", prompt, re.M)
    ids = sorted(claims)
    emit(json.dumps({"keep": rels[0], "type": "fact", "description": "слитая карточка", "keys": ["x"],
                     "claims": ids, "body": "**Суть.** Слито " + "".join(f"⟦c{i}⟧" for i in ids[:1]) +
                     "\n\n## Подробности\n- " + " ".join(f"{claims[i]['text']} ⟦c{i}⟧" for i in ids)}, ensure_ascii=False))
    sys.exit(0)

if stage == "hub" and mode == "badjson-hub":     # вызов удался, но вместо JSON — текст (устойчивый отказ)
    emit("Вот хаб, но без JSON")
    sys.exit(0)
if stage == "hub":
    area = re.search(r"Собери хаб области `([^`]+)`", prompt).group(1)
    name = re.search(r"имя `([^`]+)`", prompt).group(1)
    ids = [int(x) for x in re.findall(r"\bc(\d+):", prompt)]
    names = re.findall(r"^- \[\[([^\]]+)\]\]", prompt, re.M)
    body = (f"**Суть.** Область {area}: главное ⟦c{ids[0] if ids else 0}⟧\n\n## Карточки\n" +
            "\n".join(f"- [[{n}]] — открыть" for n in names) + "\n\n## История\n- 2026-10-23 — создана")
    emit(json.dumps({"description": f"Хаб области {area}: с чего начать", "keys": [area],
                     "claims": ids[:1], "body": body}, ensure_ascii=False))
    sys.exit(0)

area = re.search(r"Область: `([^`]+)`", prompt).group(1)
spec_path = os.environ.get("FAKE_S2_OPS")
spec = json.load(open(spec_path)) if spec_path and os.path.exists(spec_path) else {}
if area in spec:
    ops = json.loads(json.dumps(spec[area]))
    for op in ops:
        for k in ("claims", "superseded_claims"):
            if k in op:
                op[k] = [resolve(x[1:]) if isinstance(x, str) and x.startswith("@") else x for x in op[k]]
        if "body" in op:
            op["body"] = re.sub(r"⟦@([^⟧]+)⟧", lambda mm: f"⟦c{resolve(mm.group(1))}⟧", op["body"])
else:
    by_topic = {}
    for c in claims.values():
        by_topic.setdefault(c["topic"], []).append(c)
    ops = []
    for topic, cs in sorted(by_topic.items()):
        lines = "\n".join(f"{c['text']} ⟦c{c['id']}⟧" for c in cs)
        ops.append({"op": "ADD", "name": f"{area.replace('/', '-')}-{topic}", "dir": area, "type": "fact",
                    "status": "active", "description": cs[0]["text"][:80],
                    "keys": [topic], "claims": [c["id"] for c in cs], "topics": [topic],
                    "body": f"**Суть.** {lines}\n\n## Подробности\n- см. источники\n\n## История\n- 2026-10-23 — создана"})
emit(json.dumps({"ops": ops}, ensure_ascii=False))
