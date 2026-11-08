# Clickable Reads

*Makes "Read file …" rows in a bb thread clickable: images open full-screen (copy/download), text and code open in the bb viewer.*

Плагин bb: строки «Read file …» в ленте треда становятся кликабельными. Клик показывает файл, который прочитал агент, — прямо во время работы и после.

- Картинка открывается на весь экран, её можно скопировать или скачать.
- Текст и код показываются в просмотрщике bb с подсветкой.
- PDF и другие файлы можно открыть в боковой панели bb.
- Работают чтения Claude Code (инструмент Read) и Codex (команды `cat`, `sed -n` и т. п. с подписью «Read <путь>»). Относительный путь считается от папки, где выполнялась команда; файл читается на той машине, где работал тред.

Заголовки групп («Explored 7 files») и раскрываемые команды ведут себя как раньше.

## Установка

```
bb plugin install ./plugins/clickable-reads --yes
```

## Как устроено

- `lib/read-rows.ts` — какие строки ленты открываются: по `data-timeline-row-id` (`thr_…:file-read:…` или команда без кнопки с подписью «Read …»).
- `lib/read-row-clicks.ts` — content script: стиль курсора и перехват клика, событие для просмотрщика.
- `app.tsx` — просмотрщик (`experimental_appOverlay`).
- `server.ts` — RPC `read_open({ rowId })`: берёт путь из записи этой строки — для чтений Claude из событий треда (`bb.sdk.threads.events.list`), для команд Codex из ленты (`bb.sdk.threads.timeline`, строки завершённых субагентов догружаются через `timelineTurnSummaryDetails`) — и читает файл через `bb.sdk.files.read` на машине треда. Клиент передаёт только id строки, поэтому открывается лишь путь, записанный в этой строке; чтения, которые пользователь запретил, не открываются.
- Ограничения: лимиты bb на чтение (картинки до 10 МБ, остальное до 25 МБ; для больших файлов есть кнопка «Открыть в панели»), текст показывается до 400 000 символов.

Строки ленты рисует сам bb, плагин опирается на их разметку (bb 0.47). После крупного обновления bb может понадобиться поправить `lib/read-rows.ts`.

## Разработка

```
npm install
npm test          # vitest
npm run typecheck
bb plugin build
bb plugin install . --yes   # или bb plugin reload clickable-reads
```
