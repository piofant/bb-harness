# bb-harness

Мой рабочий харнесс для bb и агентов Claude Code / Codex: плагины bb, инструкции агентам и движок базы знаний, которую агенты пополняют сами. Всё обезличено — бери и подстраивай под себя.

Скиллы лежат отдельно: [piofant/skills](https://github.com/piofant/skills) · витрина [piofant.github.io/skills](https://piofant.github.io/skills/).

## Что внутри

### Плагины bb — [`plugins/`](plugins)

| Плагин | Что делает |
|---|---|
| [tavern](plugins/tavern) | Пиксельная таверна в боковой панели: скиллы из твоих сессий — персонажи (работающие ходят, остальные отдыхают), день и ночь по солнцу, телевизор с обзором последних трёх дней и тредами, которые ждут ответа |
| [library](plugins/library) | Живая библиотека для базы знаний: стеллажи по разделам, что агенты читают и что нового, здоровье конвейера, очередь решений по конфликтам, треды, которые ничего не сохранили |
| [winx](plugins/winx) | «Какая ты сегодня фея Винкс» — по твоему дню в bb, с пиксельными картинками и полосой за неделю |
| [clickable-reads](plugins/clickable-reads) | Клик по строке «Read file …» в ленте открывает файл; копирование картинок |
| [telegram-projects](plugins/telegram-projects) | Мост bb ↔ Telegram: темы по проектам, чат с тредом, файлы и голосовые. Форк [VKirill/bb-plugin-telegram-projects](https://github.com/VKirill/bb-plugin-telegram-projects) |
| [antigravity-acp](plugins/antigravity-acp) | Провайдер Gemini через Antigravity ACP с квотой и лимитами Claude в панели. Форк [rawizhere/bb-plugin-antigravity-acp](https://github.com/rawizhere/bb-plugin-antigravity-acp) |

Установка любого:

```sh
cd plugins/<плагин>
npm install
bb plugin install . --yes
```

### Инструкции агентам — [`agents/`](agents)

- [AGENTS.md](agents/AGENTS.md) — общие правила: работа по риску, автономность, скоуп, тестовая лестница, одно независимое ревью, секреты.
- [CLAUDE.md](agents/CLAUDE.md) — пайплайн Claude Code: product thinking → brainstorm → spec → implement → review → test → e2e → deploy → verify.
- [bb-AGENTS.md](agents/bb-AGENTS.md) — правила для bb: названия чатов, ссылки на файлы.
- [claude-settings.example.json](agents/claude-settings.example.json) — хук, который подкладывает агенту подходящие карточки базы знаний.

### База знаний агентов — [`knowledge-base/`](knowledge-base)

Карточки в git, которые агенты читают перед работой и пополняют сами: `kb find` / `kb show` / `kb note`, сбор заметок из сессий Claude и Codex, сводчик заметок в карточки с проверками качества, поиск, хук-подсказка, ежедневные и недельные таймеры. Без содержимого — только движок, скелет и два примера. Подробно — в [README](knowledge-base/README.md).

## Требования

- bb 0.48+ (Plugin SDK 0.6.104+), Node.js 20+
- `claude` CLI — для обзоров в таверне, феи дня и сводчика базы знаний
- Python 3.12 — для базы знаний (проверено на нём)

## Лицензия

MIT, см. [LICENSE](LICENSE). У форков — лицензии их авторов в папках плагинов. Пиксельная графика — [Ninja Adventure](https://pixel-boy.itch.io/ninja-adventure-asset-pack) (CC0).
