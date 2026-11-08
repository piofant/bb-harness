---
name: kb-ingest-disk-stop
description: Пример карточки-граблей: ingest стоит со статусом disk_low, когда на диске меньше порога
type: gotcha
status: active
applies_to: [vps]
keys: [disk_low, kb ingest, KB_MIN_FREE_GB, диск]
sources: [raw/inbox/vps/2026-01-01.md@2026-01-01]
verified: 2026-01-01
verified_how: source
review_after: 2026-02-01
sensitivity: internal
---

**Суть.** `kb ingest` пишет `status: disk_low` и ничего не забирает, если свободно меньше `KB_MIN_FREE_GB` (по умолчанию 5 ГБ); `kb health` при этом красный.

## Симптом
В `state/runs.jsonl` строки `"cmd": "ingest", "status": "disk_low"`, в «Библиотеке» bb — «мало места на диске».

## Причина
Жёсткий стоп, чтобы git и сводчик не упали посреди записи.

## Решение
Освободить место (или временно понизить `KB_MIN_FREE_GB`), затем `kb ingest all && kb index && kb health`.

## История
- 2026-01-01 — пример из шаблона репозитория
