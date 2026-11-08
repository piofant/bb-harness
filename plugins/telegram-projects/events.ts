import { z } from "zod";
import { escapeHtml, type Event } from "./model";

export const EVENT_KINDS = [
  "task_created",
  "task_started",
  "task_status",
  "task_done",
  "worker_error",
  "thread_done",
  "thread_attention",
  "thread_stopped",
  "phase_stage",
  "phase_question",
  "phase_word",
  "phase_closed",
] as const;
export type EventKind = (typeof EVENT_KINDS)[number];
export const eventRuleSchema = z
  .object({
    on: z.boolean(),
    sound: z.boolean(),
    /** null: every project; otherwise only the listed project IDs. */
    projects: z.array(z.string().max(64)).max(200).nullable(),
  })
  .strict();
export type EventRule = z.infer<typeof eventRuleSchema>;
export const eventsSchema = z
  .object(
    Object.fromEntries(EVENT_KINDS.map((k) => [k, eventRuleSchema])) as Record<
      EventKind,
      typeof eventRuleSchema
    >,
  )
  .strict();
export type Events = z.infer<typeof eventsSchema>;

const rule = (on: boolean, sound: boolean): EventRule => ({
  on,
  sound,
  projects: null,
});
// Former flags notifyTasks / notifyWorkerErrors / soundOnReview seed the first configuration.
export function defaultEvents(old?: {
  notifyTasks?: boolean;
  notifyWorkerErrors?: boolean;
  soundOnReview?: boolean;
}): Events {
  const tasks = old?.notifyTasks ?? true;
  const sound = old?.soundOnReview ?? true;
  return {
    task_created: rule(tasks, false),
    task_started: rule(tasks, false),
    task_status: rule(tasks, false),
    task_done: rule(tasks, false),
    worker_error: rule(tasks && (old?.notifyWorkerErrors ?? true), sound),
    thread_done: rule(true, false),
    thread_attention: rule(true, sound),
    thread_stopped: rule(true, sound),
    phase_stage: rule(true, false),
    phase_question: rule(true, sound),
    phase_word: rule(true, sound),
    phase_closed: rule(true, false),
  };
}
export function readEvents(
  raw: unknown,
  old?: Parameters<typeof defaultEvents>[0],
) {
  const base = defaultEvents(old);
  if (!raw || typeof raw !== "object") return base;
  // Unknown or broken rules fall back to defaults one by one; new kinds appear enabled.
  for (const k of EVENT_KINDS) {
    const r = eventRuleSchema.safeParse((raw as Record<string, unknown>)[k]);
    if (r.success) base[k] = r.data;
  }
  return base;
}
export function eventKind(e: Pick<Event, "kind" | "status">): EventKind | null {
  if (e.kind.startsWith("thread_") || e.kind.startsWith("phase_")) return e.kind as EventKind;
  if (e.kind === "worker_error") return "worker_error";
  if (e.kind === "created") return "task_created";
  if (e.kind === "started") return "task_started";
  if (e.kind === "status")
    return e.status === "done" ? "task_done" : "task_status";
  return null;
}
export function route(events: Events, kind: EventKind, projectId: string) {
  const r = events[kind];
  const send = r.on && (!r.projects || r.projects.includes(projectId));
  return { send, sound: send && r.sound };
}
export const taskKindsOn = (events: Events) =>
  EVENT_KINDS.some((k) => !k.startsWith("thread_") && !k.startsWith("phase_") && events[k].on);
// Lane Pilot is read when its task events or its phase events are wanted.
export const laneKindsOn = (events: Events) =>
  taskKindsOn(events) || EVENT_KINDS.some((k) => k.startsWith("phase_") && events[k].on);

export type ThreadOutcome = "done" | "attention" | "stopped";
// BB reports what happened in the thread; the current status decides whether the turn really ended.
// An error counts only as a fresh transition: restarts re-announce old failed threads otherwise.
export function classifyThread(
  types: readonly string[],
  status: string,
  pending: number,
  previous?: string,
): ThreadOutcome | null {
  if (pending > 0) return "attention";
  if (
    types.includes("system/thread/interrupted") ||
    (status === "error" && previous !== undefined && previous !== "error")
  )
    return "stopped";
  if (status === "idle" && types.includes("turn/completed")) return "done";
  return null;
}
export type ThreadCard = {
  outcome: ThreadOutcome;
  status: string;
  project: string;
  section: string | null;
  title: string;
  agent: string;
  reply: string | null;
  at: number;
  /** Set when reply holds a model-written summary instead of the agent's own text. */
  summaryModel?: string;
};
const agents: Record<string, string> = {
  "claude-code": "Claude Code",
  codex: "Codex",
  opencode: "OpenCode",
  gemini: "Gemini",
};
function cardParts(c: ThreadCard, language: "ru" | "en", timeZone = "") {
  const t = (ru: string, en: string) => (language === "en" ? en : ru);
  return {
    t,
    heading:
      c.outcome === "done"
        ? t("✅ Агент закончил работу", "✅ Agent finished")
        : c.outcome === "attention"
          ? t("✋ Агент ждёт ответа", "✋ Agent is waiting for you")
          : c.status === "error"
            ? t(
                "🔴 Тред остановился с ошибкой",
                "🔴 Thread stopped with an error",
              )
            : t("⏹ Тред остановлен", "⏹ Thread stopped"),
    place: [c.project, c.section].filter(Boolean).join(" › "),
    title: c.title.slice(0, 200),
    agent: agents[c.agent] ?? c.agent,
    when: new Date(c.at).toLocaleString(language === "en" ? "en-GB" : "ru-RU", {
      ...(timeZone ? { timeZone } : {}),
      hour: "2-digit",
      minute: "2-digit",
      day: "2-digit",
      month: "2-digit",
    }),
  };
}
// HTML fallback for clients or chats where Rich Messages are refused.
export function formatThreadCard(
  c: ThreadCard,
  language: "ru" | "en",
  timeZone = "",
) {
  const p = cardParts(c, language, timeZone);
  const reply = c.reply?.trim()
    ? "\n\n<blockquote expandable>" +
      markdownExcerpt(c.reply) +
      "</blockquote>"
    : "";
  return (
    "<b>" +
    p.heading +
    "</b>\n📂 " +
    escapeHtml(p.place) +
    "\n🧵 <b>" +
    escapeHtml(p.title) +
    "</b>\n🤖 " +
    escapeHtml(p.agent) +
    " · " +
    escapeHtml(p.when) +
    reply
  );
}
const mdEscape = (s: string) => s.replace(/([\\`*_[\]|<>~=#])/g, "\\$1");
// Native Rich Message: Telegram renders the agent's Markdown itself (tables, code, headings).
export function formatThreadRich(
  c: ThreadCard,
  language: "ru" | "en",
  timeZone = "",
) {
  const p = cardParts(c, language, timeZone);
  let reply = c.reply?.trim() ?? "";
  if (reply.length > 3000) {
    const n = reply.lastIndexOf("\n", 3000);
    reply = reply.slice(0, n > 1500 ? n : 3000) + "\n\n…";
  }
  if ((reply.match(/^\s*```/gm) ?? []).length % 2) reply += "\n```";
  // Blank lines separate paragraphs: single newlines are joined by Markdown.
  return (
    "### " +
    p.heading +
    "\n\n**" +
    mdEscape(p.title) +
    "**\n\n📂 " +
    mdEscape(p.place) +
    " · 🤖 " +
    mdEscape(p.agent) +
    " · 🕒 " +
    p.when +
    (!reply
      ? ""
      : c.summaryModel
        ? "\n\n<details><summary>" +
          p.t("Саммери", "Summary") +
          "</summary>\n\n" +
          reply +
          "\n\n</details>"
        : "\n\n<details><summary>" +
          p.t("Ответ агента", "Agent reply") +
          "</summary>\n\n" +
          reply +
          "\n\n</details>")
  );
}
// The deepest folder that contains the thread's working directory names its section.
export function sectionFor<T extends { path: string }>(
  folders: T[],
  path: string | null | undefined,
): T | undefined {
  if (!path) return undefined;
  return folders
    .filter(
      (f) =>
        path === f.path || path.startsWith(f.path.replace(/\/$/, "") + "/"),
    )
    .sort((a, b) => b.path.length - a.path.length)[0];
}

// Agent replies are Markdown; Telegram HTML has no tables, headings or lists.
// Emphasis needs its markers to hug the text: `*.ts`, `x ** 2`, `__init__` or `sk-****` stay as written.
function emphasis(html: string) {
  return html
    .replace(/\*\*([^\s*](?:.*?[^\s*])?)\*\*/gu, "<b>$1</b>")
    .replace(/(?<![\p{L}\p{N}_])__([^\s_](?:.*?[^\s_])?)__(?![\p{L}\p{N}_])/gu, "<b>$1</b>")
    .replace(/~~([^\s~](?:.*?[^\s~])?)~~/gu, "<s>$1</s>")
    .replace(/(?<![\p{L}\p{N}*])\*([^\s*](?:[^*]*?[^\s*])?)\*(?![\p{L}\p{N}*])/gu, "<i>$1</i>")
    .replace(/(?<![\p{L}\p{N}_])_([^\s_](?:[^_]*?[^\s_])?)_(?![\p{L}\p{N}_])/gu, "<i>$1</i>");
}
/** Telegram refuses misnested tags: formatting stays only where every tag closes in order. */
function balanced(html: string) {
  const open: string[] = [];
  for (const [, close, tag] of html.matchAll(/<(\/?)(b|i|s|code|a)\b[^>]*>/g))
    if (!close) open.push(tag);
    else if (open.pop() !== tag) return false;
  return open.length === 0;
}
function inline(text: string) {
  // Code, links and bare addresses step aside first: nothing inside them is emphasis.
  const kept: string[] = [];
  const keep = (html: string) => "\u0000" + (kept.push(html) - 1) + "\u0000";
  const restore = (html: string): string =>
    html.replace(/\u0000(\d+)\u0000/g, (_, i: string) => restore(kept[Number(i)]));
  const plain = text
    .replace(/\u0000/g, "")
    .replace(/`([^`]+)`/g, (_, code: string) =>
      keep("<code>" + escapeHtml(code) + "</code>"),
    )
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label: string, href: string) => {
      const shown = emphasis(escapeHtml(label));
      const name = balanced(restore(shown)) ? shown : escapeHtml(label);
      // A file path opens nowhere in Telegram: only its name is shown.
      return keep(
        /^https?:\/\//.test(href)
          ? '<a href="' + escapeHtml(href) + '">' + name + "</a>"
          : name,
      );
    })
    .replace(/https?:\/\/[^\s<>"«»\u0000]*[^\s<>"«».,:;!?)\]\u0000]/g, (url) =>
      keep(escapeHtml(url)),
    );
  const html = restore(emphasis(escapeHtml(plain)));
  return balanced(html) ? html : restore(escapeHtml(plain));
}
const tableSeparator = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
export function markdownExcerpt(md: string, limit = 700) {
  let src = md.trim();
  let cut = false;
  if (src.length > limit) {
    const n = src.lastIndexOf("\n", limit);
    src = src.slice(0, n > limit / 2 ? n : limit);
    cut = true;
  }
  const lines = src.split("\n");
  const out: string[] = [];
  let code = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      code = !code;
      continue;
    }
    if (code) {
      out.push("<code>" + escapeHtml(line) + "</code>");
      continue;
    }
    if (tableSeparator.test(line)) continue;
    out.push(textLine(lines, i));
  }
  return out.join("\n").trim() + (cut ? "…" : "");
}
/** One Markdown line outside code and quotes: a table row, a heading, a list item or plain text. */
function textLine(lines: string[], i: number) {
  const line = lines[i];
  const row = line.match(/^\s*\|(.*)\|\s*$/);
  if (row) {
    const cells = row[1].split("|").map((x) => inline(x.trim()));
    const header = tableSeparator.test(lines[i + 1] ?? "");
    const text = cells.join(" · ");
    return header ? "<b>" + text + "</b>" : text;
  }
  const heading = line.match(/^\s*#{1,6}\s+(.*)$/);
  if (heading) return "<b>" + inline(heading[1]) + "</b>";
  const item = line.match(/^(\s*)[-*+]\s+(.*)$/);
  if (item) return item[1] + "• " + inline(item[2]);
  return inline(line);
}
/**
 * A whole agent reply as Telegram HTML, for the chat: what markdownExcerpt does, plus code blocks, quotes and
 * rules. `inCode`: the text goes on inside a code block opened in an earlier part of the same reply.
 */
export function markdownHtml(md: string, inCode = false) {
  const lines = md.split("\n");
  const out: string[] = [];
  let code: string[] | null = inCode ? [] : null;
  let quote: string[] = [];
  const endQuote = () => {
    if (quote.length)
      out.push("<blockquote>" + quote.join("\n") + "</blockquote>");
    quote = [];
  };
  // Telegram refuses an empty block: a code block with nothing in it is left out.
  const endCode = () => {
    if (code?.join("").trim())
      out.push("<pre>" + escapeHtml(code.join("\n")) + "</pre>");
    code = null;
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (codeFence.test(line)) {
      if (code) endCode();
      else {
        endQuote();
        code = [];
      }
      continue;
    }
    if (code) {
      code.push(line);
      continue;
    }
    const quoted = line.match(/^\s*>\s?(.*)$/);
    if (quoted) {
      quote.push(textLine([quoted[1]], 0));
      continue;
    }
    endQuote();
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) out.push("———");
    else if (!tableSeparator.test(line)) out.push(textLine(lines, i));
  }
  endQuote();
  endCode();
  return out.join("\n").trim();
}
/** A line that opens or closes a code block: ``` or ~~~. */
export const codeFence = /^\s*(```|~~~)/;

export function summaryPrompt(
  title: string,
  reply: string,
  language: "ru" | "en",
) {
  const body = reply.length > 12000 ? reply.slice(0, 12000) + "\n…" : reply;
  return language === "en"
    ? `Summarize this coding agent's reply for a Telegram notification. Give 3–6 short Markdown bullet points: what was done, the result, and anything that needs the user's attention. No introduction, no closing remarks. Do not use tools or read files: work only with the text below. Write in English.\n\nThread: ${title}\n\nAgent reply:\n${body}`
    : `Сделай саммери ответа агента для уведомления в Telegram. 3–6 коротких пунктов списком в Markdown: что сделано, итог и что требует внимания пользователя. Без вступления и без заключения. Не используй инструменты и не читай файлы: работай только с текстом ниже. Пиши по-русски.\n\nТред: ${title}\n\nОтвет агента:\n${body}`;
}

/** An unknown zone would make toLocaleString throw and block delivery: fall back to the server's. */
export function safeTimeZone(zone: string) {
  if (!zone) return "";
  try {
    new Intl.DateTimeFormat("en", { timeZone: zone });
    return zone;
  } catch {
    return "";
  }
}
