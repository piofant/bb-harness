// Какая ты сегодня фея: промпт по конспектам тредов дня и разбор ответа.
import { FAIRIES, FAIRY_IDS, fairyByDate, type FairyId } from "./fairies";

export interface ThreadDigest {
  title: string;
  asks: string[];
  outcome: string[];
}

export interface Verdict {
  fairy: FairyId;
  /** Почему она — с отсылкой к делам дня. */
  why: string;
  /** Короткое заклинание-совет на остаток дня. */
  spell: string;
  /** Из кого ещё состоит день, проценты (сумма ≈ 100). */
  mix: Array<{ fairy: FairyId; pct: number }>;
  /** По делам или по дате (дел не было). */
  source: "day" | "date";
}

/** Московская дата «ГГГГ-ММ-ДД». */
export function moscowDay(ms: number): string {
  return new Date(ms + 3 * 3_600_000).toISOString().slice(0, 10);
}

/** Московская полночь даты, мс. */
export function dayStart(day: string): number {
  return Date.parse(`${day}T00:00:00+03:00`);
}

/** Почты и телефоны в модель не отправляем. */
export function redact(text: string): string {
  return text
    // Ключи и токены, вставленные в чат, — до почт, чтобы не порвать их.
    .replace(/\b(sk-[\w-]{16,}|ghp_\w{20,}|gh[ousr]_\w{20,}|xox[abp]-[\w-]{10,}|AIza[\w-]{30,}|y[0-3]_[\w-]{30,})/g, "[ключ]")
    .replace(/\bBearer\s+[\w.~+/-]{16,}=*/gi, "Bearer [ключ]")
    .replace(/[\w.+-]+@[\w-]+(\.[\w-]+)+/g, "[почта]")
    .replace(/(?<![\d.-])\+?\d(?:[\s()-]*\d){9,}(?![\d-]|\.\d)/g, "[телефон]");
}

export const SYSTEM = [
  "Ты — весёлый оракул, который по делам человека за день решает, какая он сегодня фея Винкс.",
  "Феи и чей это день:",
  ...FAIRY_IDS.map((id) => `- ${id} (${FAIRIES[id].name}, ${FAIRIES[id].power}): ${FAIRIES[id].day}`),
  "",
  "На входе конспекты рабочих сессий, тронутых сегодня: последние просьбы (ASK) и итог (OUT). Опирайся на свежее.",
  "Выбери одну фею, которая лучше всего описывает день по смыслу, а не по количеству тредов.",
  "why — до 140 символов, на «ты», тепло и с юмором, обязательно с отсылкой к 1–2 конкретным делам дня.",
  "spell — заклинание-совет на остаток дня, до 80 символов, в духе превращения фей.",
  "mix — 2–3 феи с процентами (сумма 100), первая — выбранная.",
  "По-русски, без эмодзи, без канцелярита. Не выдумывай дел, которых нет.",
  "Пол человека неизвестен: не используй родовые окончания прошедшего времени и прилагательных",
  "(не «сделала/сделал», не «уставшая»). Пиши в настоящем времени или безлично: «за день — от … до …», «у тебя сегодня …».",
].join("\n");

export const SCHEMA = {
  type: "object",
  properties: {
    fairy: { type: "string", enum: [...FAIRY_IDS] },
    why: { type: "string" },
    spell: { type: "string" },
    mix: {
      type: "array",
      maxItems: 3,
      items: {
        type: "object",
        properties: { fairy: { type: "string", enum: [...FAIRY_IDS] }, pct: { type: "number" } },
        required: ["fairy", "pct"],
      },
    },
  },
  required: ["fairy", "why", "spell", "mix"],
} as const;

const clip = (text: string, n: number) => {
  const flat = redact(text).replace(/\s+/g, " ").trim();
  return flat.length > n ? `${flat.slice(0, n - 1)}…` : flat;
};

export function prompt(digests: readonly ThreadDigest[]): string {
  const lines = [`Сессий за день: ${digests.length}.`, ""];
  digests.forEach((d, i) => {
    lines.push(`${i + 1}. «${clip(d.title, 100)}»`);
    for (const a of d.asks) lines.push(`  ASK: ${clip(a, 220)}`);
    for (const o of d.outcome) lines.push(`  OUT: ${clip(o, 400)}`);
  });
  return lines.join("\n");
}

const isFairy = (v: unknown): v is FairyId => typeof v === "string" && (FAIRY_IDS as readonly string[]).includes(v);
const str = (v: unknown, n: number) => (typeof v === "string" ? v.replace(/\s+/g, " ").trim().slice(0, n) : "");

/** Разобрать JSON-ответ `claude -p --output-format json`. */
export function parseVerdict(stdout: string): Verdict {
  const outer = JSON.parse(stdout) as { structured_output?: unknown; result?: unknown; is_error?: boolean };
  if (outer.is_error) throw new Error(`claude: ${String(outer.result ?? "error")}`.slice(0, 300));
  let raw = outer.structured_output as Record<string, unknown> | undefined;
  if (raw === undefined && typeof outer.result === "string") {
    const m = /\{[\s\S]*\}/.exec(outer.result);
    raw = m ? (JSON.parse(m[0]) as Record<string, unknown>) : undefined;
  }
  if (!raw || !isFairy(raw.fairy)) throw new Error("в ответе нет феи");
  const why = str(raw.why, 200);
  if (why === "") throw new Error("пустое объяснение");
  return {
    fairy: raw.fairy,
    why,
    spell: str(raw.spell, 120),
    mix: normalizeMix(raw.fairy, Array.isArray(raw.mix) ? raw.mix : []),
    source: "day",
  };
}

/** Проценты: выбранная первой, без повторов, целые, в сумме 100. */
export function normalizeMix(main: FairyId, raw: unknown[]): Verdict["mix"] {
  const seen = new Map<FairyId, number>();
  for (const item of raw) {
    const it = item as { fairy?: unknown; pct?: unknown };
    if (!isFairy(it.fairy) || seen.has(it.fairy)) continue;
    // Выбранная фея не может уйти в ноль и пропасть из состава дня.
    const raw = typeof it.pct === "number" && Number.isFinite(it.pct) ? Math.max(0, it.pct) : 0;
    const pct = it.fairy === main ? Math.max(raw, 1) : raw;
    seen.set(it.fairy, pct);
  }
  if (!seen.has(main)) seen.set(main, Math.max(50, ...seen.values()));
  const entries = [...seen.entries()].sort((a, b) => (a[0] === main ? -1 : b[0] === main ? 1 : b[1] - a[1])).slice(0, 3);
  const total = entries.reduce((s, [, p]) => s + p, 0) || 1;
  const out = entries.map(([fairy, p]) => ({ fairy, pct: Math.round((p / total) * 100) }));
  out[0]!.pct += 100 - out.reduce((s, x) => s + x.pct, 0);
  return out.filter((x) => x.pct > 0);
}

/** Если за день дел не было — фея по дате, без модели. */
export function verdictByDate(day: string): Verdict {
  const fairy = fairyByDate(day);
  return {
    fairy,
    why: `Сегодня в bb тихо, так что звёзды решили сами: день ${FAIRIES[fairy].name}.`,
    spell: "Начни одно маленькое дело — и крылья появятся.",
    mix: [{ fairy, pct: 100 }],
    source: "date",
  };
}
