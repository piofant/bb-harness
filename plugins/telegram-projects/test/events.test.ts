import test from "node:test";
import assert from "node:assert/strict";
import {
  classifyThread,
  defaultEvents,
  eventKind,
  formatThreadCard,
  readEvents,
  route,
  sectionFor,
  taskKindsOn,
} from "../events";
test("a finished turn, a stop, an error and a pending question are told apart", () => {
  assert.equal(classifyThread(["turn/completed"], "idle", 0), "done");
  assert.equal(classifyThread(["turn/completed"], "active", 0), null);
  assert.equal(classifyThread(["system/thread/interrupted"], "idle", 0), "stopped");
  assert.equal(classifyThread([], "error", 0, "active"), "stopped");
  assert.equal(classifyThread([], "error", 0, "error"), null);
  assert.equal(classifyThread([], "error", 0), null);
  assert.equal(classifyThread(["turn/completed"], "idle", 1), "attention");
  assert.equal(classifyThread(["item/started"], "active", 0), null);
});
test("task events map to their rule; done is separate from other statuses", () => {
  assert.equal(eventKind({ kind: "status", status: "done" }), "task_done");
  assert.equal(eventKind({ kind: "status", status: "canceled" }), "task_status");
  assert.equal(eventKind({ kind: "created", status: "todo" }), "task_created");
  assert.equal(eventKind({ kind: "worker_error", status: "blocked" }), "worker_error");
  assert.equal(eventKind({ kind: "thread_done", status: "idle" }), "thread_done");
  assert.equal(eventKind({ kind: "agency_notice", status: "" }), null);
});
test("project filter and sound follow the rule", () => {
  const ev = defaultEvents();
  ev.task_done = { on: true, sound: true, projects: ["proj_a"] };
  assert.deepEqual(route(ev, "task_done", "proj_a"), { send: true, sound: true });
  assert.deepEqual(route(ev, "task_done", "proj_b"), { send: false, sound: false });
  ev.thread_done.on = false;
  assert.equal(route(ev, "thread_done", "proj_a").send, false);
});
test("former flags seed the first configuration; broken rules fall back one by one", () => {
  const ev = readEvents(null, { notifyTasks: false, soundOnReview: false });
  assert.equal(taskKindsOn(ev), false);
  assert.equal(ev.thread_done.on, true);
  assert.equal(ev.thread_attention.sound, false);
  const mixed = readEvents({ task_done: { on: false, sound: false, projects: null }, worker_error: "x" });
  assert.equal(mixed.task_done.on, false);
  assert.equal(mixed.worker_error.on, true);
});
test("thread card is signed with project, section, thread and agent and escapes HTML", () => {
  const text = formatThreadCard(
    {
      outcome: "done",
      status: "idle",
      project: "Клиенты",
      section: "Сайт <b>",
      title: "Лендинг & форма",
      agent: "claude-code",
      reply: "Готово: <script>",
      at: Date.UTC(2026, 9, 2, 12, 0),
    },
    "ru",
  );
  assert.match(text, /✅ Агент закончил работу/);
  assert.match(text, /📂 Клиенты › Сайт &lt;b&gt;/);
  assert.match(text, /🧵 <b>Лендинг &amp; форма<\/b>/);
  assert.match(text, /🤖 Claude Code · /);
  assert.match(text, /<blockquote expandable>Готово: &lt;script&gt;<\/blockquote>/);
});
test("the deepest folder containing the working directory is the section", () => {
  const folders = [
    { id: "a", path: "/p" },
    { id: "b", path: "/p/site" },
    { id: "c", path: "/p/site-old" },
  ];
  assert.equal(sectionFor(folders, "/p/site/src")?.id, "b");
  assert.equal(sectionFor(folders, "/p/site")?.id, "b");
  assert.equal(sectionFor(folders, "/p/other")?.id, "a");
  assert.equal(sectionFor(folders, "/q"), undefined);
});
test("agent Markdown becomes Telegram HTML: bold, code, tables, lists, links", async () => {
  const { markdownExcerpt } = await import("../events");
  const html = markdownExcerpt(
    "## Итог\nОн сам (`orchestrator`) и **writer**.\n\n| День | Всего |\n|---|---|\n| 27.09 | 25 |\n\n- пункт <x>\n[док](https://e.x/a?b=1&c=2)\nsnake_case_name",
  );
  assert.equal(
    html,
    '<b>Итог</b>\nОн сам (<code>orchestrator</code>) и <b>writer</b>.\n\n<b>День · Всего</b>\n27.09 · 25\n\n• пункт &lt;x&gt;\n<a href="https://e.x/a?b=1&amp;c=2">док</a>\nsnake_case_name',
  );
  const long = markdownExcerpt("строка **жирная**\n".repeat(60), 100);
  assert.ok(long.endsWith("…"));
  assert.equal((long.match(/<b>/g) ?? []).length, (long.match(/<\/b>/g) ?? []).length);
});
test("rich card keeps the agent Markdown, escapes metadata and closes a cut code fence", async () => {
  const { formatThreadRich } = await import("../events");
  const md = formatThreadRich(
    {
      outcome: "done",
      status: "idle",
      project: "Клиенты",
      section: null,
      title: "Fix *bold* | table",
      agent: "codex",
      reply: "| a | b |\n|---|---|\n| 1 | 2 |\n\n```\n" + "x\n".repeat(2000),
      at: Date.UTC(2026, 9, 2, 12, 0),
    },
    "ru",
  );
  assert.match(md, /^### ✅ Агент закончил работу\n\n\*\*Fix \\\*bold\\\* \\\| table\*\*\n\n📂 Клиенты · 🤖 Codex · 🕒 /);
  assert.match(md, /<details><summary>Ответ агента<\/summary>\n\n\| a \| b \|/);
  assert.equal((md.match(/^\s*```/gm) ?? []).length % 2, 0);
  assert.ok(md.endsWith("</details>"));
});
test("summary card shows the summary openly and names the model; the prompt forbids tools", async () => {
  const { formatThreadRich, summaryPrompt } = await import("../events");
  const md = formatThreadRich(
    {
      outcome: "done",
      status: "idle",
      project: "Клиенты",
      section: null,
      title: "T",
      agent: "codex",
      reply: "- сделано\n- проверено",
      at: 0,
      summaryModel: "claude-code / haiku",
    },
    "ru",
  );
  assert.match(md, /<details><summary>Саммери<\/summary>\n\n- сделано\n- проверено\n\n<\/details>$/);
  const prompt = summaryPrompt("Тред", "x".repeat(20000), "ru");
  assert.match(prompt, /Не используй инструменты/);
  assert.ok(prompt.length < 12500);
});
test("an unknown time zone falls back to the server zone instead of throwing", async () => {
  const { safeTimeZone } = await import("../events");
  assert.equal(safeTimeZone("Europe/Madrid"), "Europe/Madrid");
  assert.equal(safeTimeZone("Mars/Base"), "");
  assert.equal(safeTimeZone(""), "");
});

test("a whole agent reply becomes Telegram HTML: quotes, rules, code blocks, strikethrough, italics", async () => {
  const { markdownHtml } = await import("../events");
  assert.equal(
    markdownHtml(
      "- **Раздел:** заметки · **Теги:** пусто\n\n**Текст:**\n> **Цитата:** «Первый пункт…»\n> · «Второй пункт?»\n\n---\n\n*Оценку (0–10) поставь сам.*\n~~старое~~ и _курсив_ и snake_case_name\n```js\nif (a < b) { **x** }\n```\n## Итог\n| A | B |\n|---|---|\n| 1 | 2 |",
    ),
    "• <b>Раздел:</b> заметки · <b>Теги:</b> пусто\n\n<b>Текст:</b>\n<blockquote><b>Цитата:</b> «Первый пункт…»\n· «Второй пункт?»</blockquote>\n\n———\n\n<i>Оценку (0–10) поставь сам.</i>\n<s>старое</s> и <i>курсив</i> и snake_case_name\n<pre>if (a &lt; b) { **x** }</pre>\n<b>Итог</b>\n<b>A · B</b>\n1 · 2",
  );
});
test("a reply part that starts inside a code block stays code; an unclosed block is closed", async () => {
  const { markdownHtml } = await import("../events");
  assert.equal(markdownHtml("x = 1\n```\nпосле **жирно**", true), "<pre>x = 1</pre>\nпосле <b>жирно</b>");
  assert.equal(markdownHtml("Код:\n```\na < b"), "Код:\n<pre>a &lt; b</pre>");
});

test("emphasis around code and links renders; paths, URLs, globs and arithmetic stay as written", async () => {
  const { markdownHtml } = await import("../events");
  const cases: [string, string][] = [
    ["- **`events.ts`** — конвертер", "• <b><code>events.ts</code></b> — конвертер"],
    ["**Файл `chat.ts`:** готов", "<b>Файл <code>chat.ts</code>:</b> готов"],
    ["[`PR #12`](https://x.com/pr/12)", '<a href="https://x.com/pr/12"><code>PR #12</code></a>'],
    ["[x.ts](/r/src/__tests__/x.ts) и [MOC.md](/srv/MOC.md)", "x.ts и MOC.md"],
    ["[init](https://g.com/pkg/__init__.py)", '<a href="https://g.com/pkg/__init__.py">init</a>'],
    ["см. https://x.com/__a__/b_c_d.", "см. https://x.com/__a__/b_c_d."],
    ["*.ts и *.tsx", "*.ts и *.tsx"],
    ["x ** 2 + y ** 2", "x ** 2 + y ** 2"],
    ["**kwargs и **options", "**kwargs и **options"],
    ["ключ sk-******** и my__init__.py", "ключ sk-******** и my__init__.py"],
    ["«*никогда*» и **_важное_ уточнение**", "«<i>никогда</i>» и <b><i>важное</i> уточнение</b>"],
    ["a < b & c > d", "a &lt; b &amp; c &gt; d"],
    ["> ## Заголовок\n> - пункт", "<blockquote><b>Заголовок</b>\n• пункт</blockquote>"],
    ["~~~\n# не заголовок\n~~~", "<pre># не заголовок</pre>"],
    ["До\n```\n\n```\nПосле", "До\nПосле"],
  ];
  for (const [md, html] of cases) assert.equal(markdownHtml(md), html, md);
});
test("misnested emphasis drops to plain text but keeps code and links, so Telegram never refuses it", async () => {
  const { markdownHtml, markdownExcerpt } = await import("../events");
  assert.equal(markdownHtml("**bold *italic*** и `код`"), "**bold *italic*** и <code>код</code>");
  assert.equal(markdownExcerpt("_a **b_ c**"), "_a **b_ c**");
});
test("any Markdown-like text gives Telegram HTML with balanced tags and no raw angle brackets", async () => {
  const { markdownHtml, markdownExcerpt } = await import("../events");
  const alphabet = ["*", "**", "_", "__", "~~", "`", "[", "]", "(", ")", "https://x.com/a_b", "/p/a_b", "<", ">", "&", "\"", "> ", "- ", "# ", "|", "---", "```", "~~~", "\n", " ", "слово", "word", "1", "«", "»", "\u0000"];
  let seed = 7;
  const rnd = (n: number) => ((seed = (seed * 1103515245 + 12345) % 2147483648), seed % n);
  const check = (html: string, src: string) => {
    const open: string[] = [];
    const tags = /<(\/?)(b|i|s|code|a|pre|blockquote)\b[^>]*>/g;
    for (const [, close, tag] of html.matchAll(tags))
      if (!close) open.push(tag);
      else assert.equal(open.pop(), tag, JSON.stringify(src));
    assert.equal(open.length, 0, JSON.stringify(src));
    assert.ok(!/[<>]/.test(html.replace(tags, "")), JSON.stringify(src));
  };
  for (let n = 0; n < 3000; n++) {
    const src = Array.from({ length: 1 + rnd(24) }, () => alphabet[rnd(alphabet.length)]).join("");
    check(markdownHtml(src), src);
    check(markdownHtml(src, true), src);
    check(markdownExcerpt(src), src);
  }
});
