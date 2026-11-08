// Телевизор над таверной: спортивная сводка «над чем работали помогаторы».
// Чистая логика: табло из тредов и вызовов скиллов, промпт для заголовков
// и разбор ответа модели. Запуск модели — в lib/claude.ts.
import { callsIn, moscowDay, type UsageMap } from "./usage";

/** Московская полночь даты «ГГГГ-ММ-ДД», мс. */
export function dayStart(day: string): number {
  return Date.parse(`${day}T00:00:00+03:00`);
}

export interface TvThread {
  id: string;
  title: string | null;
  projectId: string;
  updatedAt: number;
  hidden: boolean;
  /** Подтред сабагента — в счёт идёт, в бегущую строку нет. */
  child: boolean;
}

export interface Team {
  projectId: string;
  name: string;
  /** «Голы» — треды, в которых была работа за период. */
  threads: number;
  /** Вызовы скиллов в этих тредах за период — «удары по воротам». */
  skillCalls: number;
}

export interface Board {
  days: number;
  teams: Team[];
  /** Игрок периода — самый часто вызываемый скилл. */
  mvp: { slug: string; calls: number } | null;
  topSkills: Array<{ slug: string; calls: number }>;
  /** Названия свежих тредов — бегущая строка. */
  ticker: Array<{ id: string; title: string }>;
  totalThreads: number;
  totalCalls: number;
}

export function buildBoard(input: {
  now: number;
  days: string[];
  threads: readonly TvThread[];
  projectNames: ReadonlyMap<string, string>;
  usage: UsageMap;
  /** Скиллы, которые не участвуют в зачёте (трактирщик). */
  exclude?: ReadonlySet<string>;
}): Board {
  // Окно — те же московские сутки, что у вызовов, а не скользящие 24 ч.
  const since = dayStart(input.days[input.days.length - 1] ?? moscowDay(input.now));
  const recent = input.threads.filter((t) => t.updatedAt >= since);
  const callsByThread = new Map<string, number>();
  const skills: Array<{ slug: string; calls: number }> = [];
  for (const [slug, use] of Object.entries(input.usage)) {
    if (input.exclude?.has(slug)) continue;
    const calls = callsIn(use, input.days);
    if (calls === 0) continue;
    skills.push({ slug, calls });
    // Поштучно вызовы по тредам не храним — относим к треду последнего вызова.
    callsByThread.set(use.threadId, (callsByThread.get(use.threadId) ?? 0) + calls);
  }
  skills.sort((a, b) => b.calls - a.calls || a.slug.localeCompare(b.slug));

  const teams = new Map<string, Team>();
  for (const t of recent) {
    const team = teams.get(t.projectId) ?? {
      projectId: t.projectId,
      name: input.projectNames.get(t.projectId) ?? "проект",
      threads: 0,
      skillCalls: 0,
    };
    team.threads += 1;
    team.skillCalls += callsByThread.get(t.id) ?? 0;
    teams.set(t.projectId, team);
  }

  const ticker = recent
    .filter((t) => !t.hidden && !t.child && t.title)
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, 15)
    .map((t) => ({ id: t.id, title: t.title!.slice(0, 90) }));

  return {
    days: input.days.length,
    teams: [...teams.values()].sort((a, b) => b.threads - a.threads || a.name.localeCompare(b.name)),
    mvp: skills[0] ?? null,
    topSkills: skills.slice(0, 5),
    ticker,
    totalThreads: recent.length,
    totalCalls: skills.reduce((sum, s) => sum + s.calls, 0),
  };
}

/** Почты и телефоны в модель не отправляем. */
export function redact(text: string): string {
  return text
    .replace(/[\w.+-]+@[\w-]+(\.[\w-]+)+/g, "[почта]")
    // Телефон — от десяти цифр; даты «2026-11-04» и тикеты не трогаем.
    .replace(/(?<![\d.-])\+?\d(?:[\s()-]*\d){9,}(?![\d-]|\.\d)/g, "[телефон]");
}

/** Конспект одного треда для обзора: что просили и чем закончилось. */
export interface ThreadDigest {
  id: string;
  title: string;
  project: string;
  updatedAt: number;
  /** Превью сообщений пользователя, по порядку. */
  asks: string[];
  /** Превью последних ответов агента. */
  outcome: string[];
}

export type ThemeStatus = "done" | "in_progress" | "blocked";

export interface Overview {
  summary: string;
  themes: Array<{ title: string; project: string; status: ThemeStatus; what: string; threadIds: string[] }>;
  next: Array<{ text: string; why: string; threadId: string | null }>;
}

export const OVERVIEW_SYSTEM = [
  "Ты пишешь очень короткую сводку для владельца рабочих сессий с ИИ-агентами.",
  "На входе конспекты тредов: что просил пользователь (ASK) и чем ответил агент (OUT).",
  "Пойми по смыслу, над чем шла работа и что из этого вышло.",
  "",
  "Пиши предельно кратко, как подписи на табло:",
  "summary — одна фраза до 120 символов: главное за период.",
  "themes — 3–5 тем, связанные треды склей. title — до 35 символов, суть работы.",
  "status: done (результат есть), in_progress (не доведено), blocked (упёрлись).",
  "what — до 80 символов: итог или где застряли, один факт, без перечислений. refs — номера тредов вида T3.",
  "",
  "По-русски, без канцелярита, без эмодзи. Не выдумывай. Служебные мелочи (проверки связи, названия чатов) не выноси в темы.",
].join("\n");

export const OVERVIEW_SCHEMA = {
  type: "object",
  properties: {
    summary: { type: "string" },
    themes: {
      type: "array",
      minItems: 1,
      maxItems: 5,
      items: {
        type: "object",
        properties: {
          title: { type: "string" },
          project: { type: "string" },
          status: { type: "string", enum: ["done", "in_progress", "blocked"] },
          what: { type: "string" },
          refs: { type: "array", items: { type: "string" } },
        },
        required: ["title", "status", "what", "refs"],
      },
    },
  },
  required: ["summary", "themes"],
} as const;

const clip = (text: string, n: number) => {
  const flat = redact(text).replace(/\s+/g, " ").trim();
  return flat.length > n ? `${flat.slice(0, n - 1)}…` : flat;
};

/**
 * Промпт обзора. Треды нумеруются T1…Tn, а не своими id: короткие ссылки
 * модель путает реже, а ответ мы сверяем со списком и чужие номера отбрасываем.
 */
export function overviewPrompt(days: number, digests: readonly ThreadDigest[], topSkills: Board["topSkills"]): string {
  const lines = [`Период: последние ${days} дн. Тредов: ${digests.length}.`, ""];
  digests.forEach((d, i) => {
    lines.push(`T${i + 1} · ${clip(d.project, 40)} · «${clip(d.title, 100)}»`);
    for (const a of d.asks) lines.push(`  ASK: ${clip(a, 260)}`);
    for (const o of d.outcome) lines.push(`  OUT: ${clip(o, 600)}`);
    lines.push("");
  });
  if (topSkills.length > 0) lines.push("Чаще всего звали скиллы: " + topSkills.map((s) => `${s.slug} (${s.calls})`).join(", "));
  return lines.join("\n");
}

function payloadOf(stdout: string): unknown {
  const outer = JSON.parse(stdout) as { structured_output?: unknown; result?: unknown; is_error?: boolean };
  if (outer.is_error) throw new Error(`claude: ${String(outer.result ?? "error")}`.slice(0, 300));
  if (outer.structured_output !== undefined) return outer.structured_output;
  if (typeof outer.result === "string") {
    const match = /\{[\s\S]*\}/.exec(outer.result);
    if (match) return JSON.parse(match[0]);
  }
  throw new Error("в ответе нет JSON");
}

const str = (v: unknown, n: number) => (typeof v === "string" ? v.replace(/\s+/g, " ").trim().slice(0, n) : "");

/** Разобрать ответ модели; ссылки T<n> переводим в id тредов, чужие выкидываем. */
export function parseOverview(stdout: string, digests: readonly ThreadDigest[]): Overview {
  const raw = payloadOf(stdout) as Record<string, unknown>;
  const ref = (r: unknown): string | null => {
    const m = /^T(\d+)$/i.exec(str(r, 8));
    const d = m ? digests[Number(m[1]) - 1] : undefined;
    return d?.id ?? null;
  };
  const statuses: ThemeStatus[] = ["done", "in_progress", "blocked"];
  const themes = (Array.isArray(raw.themes) ? raw.themes : [])
    .map((t: Record<string, unknown>) => ({
      title: str(t.title, 80),
      project: str(t.project, 40),
      status: statuses.includes(t.status as ThemeStatus) ? (t.status as ThemeStatus) : "in_progress",
      what: str(t.what, 300),
      threadIds: [...new Set((Array.isArray(t.refs) ? t.refs : []).map(ref).filter((x): x is string => x !== null))],
    }))
    .filter((t) => t.title !== "" && t.what !== "")
    .slice(0, 5);
  const next = (Array.isArray(raw.next) ? raw.next : [])
    .map((n: Record<string, unknown>) => ({ text: str(n.text, 120), why: str(n.why, 160), threadId: ref(n.ref) }))
    .filter((n) => n.text !== "")
    .slice(0, 3);
  const summary = str(raw.summary, 500);
  if (summary === "" || themes.length === 0) throw new Error("пустой обзор");
  return { summary, themes, next };
}
