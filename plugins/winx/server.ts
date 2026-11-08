// bb-plugin-winx — «Какая ты сегодня фея Винкс».
//
// Раз в московские сутки (или по кнопке) берём конспекты тредов за сегодня —
// превью просьб и итогов — и спрашиваем `claude -p`, чья это фея. Дел за день
// нет — фея по дате, без модели. Итог и неделя фей лежат в kv плагина.
import { defineRpcContract, type BbPluginApi } from "@get-bb/plugin-sdk";
import { z } from "zod";
import { resolveClaudeBin, runClaude } from "./lib/claude";
import { FAIRY_IDS } from "./lib/fairies";
import {
  dayStart,
  moscowDay,
  parseVerdict,
  prompt,
  SCHEMA,
  SYSTEM,
  verdictByDate,
  type ThreadDigest,
  type Verdict,
} from "./lib/day";

const fairyId = z.enum(FAIRY_IDS);
const verdictSchema = z.object({
  fairy: fairyId,
  why: z.string(),
  spell: z.string(),
  mix: z.array(z.object({ fairy: fairyId, pct: z.number() })),
  source: z.enum(["day", "date"]),
});

const stateSchema = z.object({
  today: z.object({ day: z.string(), verdict: verdictSchema, threads: z.number(), at: z.number() }).nullable(),
  week: z.array(z.object({ day: z.string(), fairy: fairyId })),
  status: z.enum(["idle", "running", "failed"]),
});
export type FairyState = z.infer<typeof stateSchema>;

export const rpcContract = defineRpcContract({
  fairy_state: { input: z.null(), output: stateSchema },
  /** Пересчитать фею сейчас — по делам на этот момент. */
  fairy_reroll: { input: z.null(), output: z.object({ started: z.boolean() }) },
});

const CHANGED = "winx-changed";
const TODAY_KEY = "today-v1";
const WEEK_KEY = "week-v1";
const MAX_THREADS = 20;

interface Stored {
  day: string;
  verdict: Verdict;
  threads: number;
  at: number;
}

export default async function plugin(bb: BbPluginApi) {
  const settings = bb.settings.define({
    model: { type: "string", label: "Модель для `claude -p`", default: "sonnet" },
    claudeBin: { type: "string", label: "Команда claude", default: "claude" },
  });

  /** Неделя — это последние 7 календарных дней, а не 7 последних записей. */
  const weekStart = () => moscowDay(Date.now() - 6 * 86_400_000);

  let status: "idle" | "running" | "failed" = "idle";
  let busy = false;
  let failedAt = 0;

  async function digestsForToday(day: string): Promise<ThreadDigest[]> {
    const since = dayStart(day);
    // Чтение тредов с таймаутом: зависший запрос не должен держать «колдуем…» вечно.
    const threads = await bb.sdk.threads.list({ limit: 300, hasParent: false, signal: AbortSignal.timeout(20_000) });
    const picks = threads
      .filter((t) => t.updatedAt >= since && t.parentThreadId === null && t.visibility === "visible")
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, MAX_THREADS);
    const out: Array<ThreadDigest | null> = await Promise.all(
      picks.map(async (t) => {
        try {
          const { items } = await bb.sdk.threads.conversationOutline({
            threadId: t.id,
            signal: AbortSignal.timeout(20_000),
          });
          const asks = items.filter((m) => m.role === "user").map((m) => m.preview);
          if (asks.length === 0) return null;
          return {
            title: t.title ?? t.titleFallback ?? "без названия",
            // Только последние просьбы: тред могли начать неделю назад,
            // а сегодня в нём было лишь продолжение.
            asks: asks.slice(-3),
            outcome: items.filter((m) => m.role === "assistant").map((m) => m.preview).slice(-1),
          };
        } catch (error) {
          bb.log.warn(`outline failed ${t.id}: ${String(error).slice(0, 200)}`);
          return null;
        }
      }),
    );
    return out.filter((d): d is ThreadDigest => d !== null);
  }

  async function decide(force: boolean): Promise<void> {
    if (busy) return;
    if (!force && status === "failed" && Date.now() - failedAt < 30 * 60_000) return;
    busy = true;
    const day = moscowDay(Date.now());
    // Настройки читаем на каждый запуск: смена модели — без перезагрузки.
    const config = await settings.get();
    const model = config.model.trim() || "sonnet";
    const bin = resolveClaudeBin(config.claudeBin.trim() || "claude");
    let threads = 0;
    try {
      const stored = await bb.storage.kv.get<Stored>(TODAY_KEY);
      if (!force && stored?.day === day) return;
      status = "running";
      bb.realtime.publish(CHANGED, {});
      const digests = await digestsForToday(day);
      threads = digests.length;
      let verdict: Verdict;
      let ms = 0;
      if (threads === 0) {
        verdict = verdictByDate(day);
      } else {
        const run = await runClaude({
          bin,
          model,
          system: SYSTEM,
          prompt: prompt(digests),
          schema: SCHEMA,
          timeoutMs: 120_000,
          budgetUsd: 0.2,
        });
        ms = run.ms;
        verdict = parseVerdict(run.stdout);
      }
      await bb.storage.kv.set(TODAY_KEY, { day, verdict, threads, at: Date.now() } satisfies Stored);
      const week = (await bb.storage.kv.get<Array<{ day: string; fairy: string }>>(WEEK_KEY)) ?? [];
      const next = [...week.filter((w) => w.day !== day), { day, fairy: verdict.fairy }]
        .filter((w) => w.day >= weekStart())
        .sort((a, b) => a.day.localeCompare(b.day));
      await bb.storage.kv.set(WEEK_KEY, next);
      status = "idle";
      bb.log.info(`fairy ok: ${verdict.fairy} source=${verdict.source} model=${model} threads=${threads} ms=${ms} forced=${force}`);
    } catch (error) {
      status = "failed";
      failedAt = Date.now();
      bb.log.warn(`fairy failed: model=${model} bin=${bin} threads=${threads} forced=${force} error=${String(error).slice(0, 300)}`);
    } finally {
      busy = false;
      bb.realtime.publish(CHANGED, {});
    }
  }

  async function state(): Promise<FairyState> {
    const stored = await bb.storage.kv.get<Stored>(TODAY_KEY);
    const today = stored && stored.day === moscowDay(Date.now()) ? stored : null;
    // Открыли страницу, а феи на сегодня нет — считаем в фоне.
    if (today === null && !busy) decide(false).catch((e: unknown) => bb.log.warn(`fairy crashed: ${String(e)}`));
    const week = ((await bb.storage.kv.get<FairyState["week"]>(WEEK_KEY)) ?? []).filter((w) => w.day >= weekStart());
    return { today, week, status: busy ? "running" : status };
  }

  bb.rpc.register(rpcContract, {
    fairy_state: () => state(),
    fairy_reroll: () => {
      if (busy) return { started: false };
      decide(true).catch((e: unknown) => bb.log.warn(`fairy crashed: ${String(e)}`));
      return { started: true };
    },
  });

  bb.cli.register({
    name: "winx",
    summary: "Какая ты сегодня фея Винкс",
    commands: [{ name: "today", summary: "Фея дня (--reroll — пересчитать)", usage: "bb winx today [--reroll]" }],
    async run(argv) {
      if (argv.includes("--reroll")) await decide(true);
      const s = await state();
      if (!s.today) return { exitCode: 0, stdout: `Фея ещё считается (${s.status}).` };
      const v = s.today.verdict;
      return {
        exitCode: 0,
        stdout: [
          `${v.fairy} (${v.source}, по ${s.today.threads} сессиям)`,
          v.why,
          `Заклинание: ${v.spell}`,
          `Состав дня: ${v.mix.map((m) => `${m.fairy} ${m.pct}%`).join(", ")}`,
          `Неделя: ${s.week.map((w) => `${w.day.slice(5)} ${w.fairy}`).join(" · ")}`,
        ].join("\n"),
      };
    },
  });
}
