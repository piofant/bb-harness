---
name: example-decision        # = имя файла, латиница kebab, уникально по базе
description: одна строка-крючок — о чём карточка и когда она нужна
type: decision
status: active                     # active|superseded|disputed
supersedes: []                     # имена карточек, которые эта заменяет
superseded_by: []                  # обязательно при status: superseded
applies_to: [vps]                  # vps|laptop|app-stable|...
keys: [ключевое-слово, PROJ-000]  # для хука-подсказки и kb find
sources: [raw/claude-vps/-home-app/example.md@2026-10-23]
verified: 2026-10-23
verified_how: source               # live|source
review_after: 2027-04-21           # TTL: infra-fact 30 дн, process 90, person 180
sensitivity: internal              # internal|restricted|private (private — только в cards/private/)
importance: normal                 # high|normal|low — high у preference/gotcha попадает в INDEX-core
core: false                        # true — всегда в INDEX-core.md
---

**Суть.** 1–3 строки: текущий ответ.

## Контекст
…

## Решение
…

## Что заменило
- …

## История
- 2026-10-23 — создана
