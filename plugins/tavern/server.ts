// bb-plugin-tavern — бэкенд таверны.
//
// Раз в полминуты фоновый сервис дочитывает ленты тредов (только новые
// события после сохранённого курсора), находит в них вызовы скиллов и хранит
// «скилл → когда звали последний раз». Страница таверны спрашивает по RPC,
// кто сейчас работает, а кто отдыхает, и обновляется по realtime-сигналу.
import { defineRpcContract, type BbPluginApi } from "@get-bb/plugin-sdk";
import { z } from "zod";
import {
  pruneUsage,
  rankSkills,
  recordUse,
  skillsFromItem,
  type UsageMap,
} from "./lib/usage";
import { distinctSprites, personaFor, SPRITES } from "./lib/personas";
import { readSkillDescriptions, SKILL_DIRS } from "./lib/skill-files";
import {
  buildBoard,
  OVERVIEW_SCHEMA,
  OVERVIEW_SYSTEM,
  overviewPrompt,
  parseOverview,
  dayStart,
  type Overview,
  type ThreadDigest,
  type TvThread,
} from "./lib/tv";
import { resolveClaudeBin, runClaude } from "./lib/claude";
import { candidates, classify, hookLine, rankWaiting, type WaitingInput, type WaitingItem } from "./lib/waiting";
import { lastDays, moscowDay } from "./lib/usage";

const characterSchema = z.object({
  slug: z.string(),
  name: z.string(),
  sprite: z.enum(SPRITES),
  origin: z.enum(["custom", "tavern", "named", "invented"]),
  line: z.string().nullable(),
  description: z.string().nullable(),
  lastUsedAt: z.number(),
  count: z.number(),
  threadId: z.string(),
  threadTitle: z.string().nullable(),
  /** Тред последнего вызова прямо сейчас крутится. */
  busy: z.boolean(),
});
export type Character = z.infer<typeof characterSchema>;

const stateSchema = z.object({
  working: z.array(characterSchema),
  resting: z.array(characterSchema),
  innkeeper: characterSchema.nullable(),
  scannedAt: z.number().nullable(),
  scanning: z.boolean(),
  activeWindowHours: z.number(),
});
export type TavernState = z.infer<typeof stateSchema>;

const tvSchema = z.object({
  days: z.number(),
  teams: z.array(z.object({ projectId: z.string(), name: z.string(), threads: z.number(), skillCalls: z.number() })),
  mvp: characterSchema.extend({ calls: z.number() }).nullable(),
  topSkills: z.array(z.object({ slug: z.string(), name: z.string(), calls: z.number() })),
  ticker: z.array(z.object({ id: z.string(), title: z.string() })),
  totalThreads: z.number(),
  totalCalls: z.number(),
  overview: z
    .object({
      summary: z.string(),
      themes: z.array(
        z.object({
          title: z.string(),
          project: z.string(),
          status: z.enum(["done", "in_progress", "blocked"]),
          what: z.string(),
          threadIds: z.array(z.string()),
        }),
      ),
      next: z.array(z.object({ text: z.string(), why: z.string(), threadId: z.string().nullable() })),
    })
    .nullable(),
  overviewAt: z.number().nullable(),
  /** Сколько тредов прочитано для обзора. */
  overviewThreads: z.number(),
  overviewState: z.enum(["idle", "running", "failed"]),
  /** Треды, где ход за человеком: ошибка, вопрос агента, непрочитанный ответ. */
  waiting: z.array(
    z.object({
      threadId: z.string(),
      title: z.string(),
      reason: z.enum(["error", "pending", "question", "unread"]),
      line: z.string(),
      updatedAt: z.number(),
    }),
  ),
});
export type TvState = z.infer<typeof tvSchema>;

export const rpcContract = defineRpcContract({
  tavern_state: { input: z.null(), output: stateSchema },
  tavern_tv: { input: z.null(), output: tvSchema },
  /** Переписать заголовки сейчас, не дожидаясь следующего дня. */
  tavern_tv_refresh: { input: z.null(), output: z.object({ started: z.boolean() }) },
  tavern_rename: {
    input: z.object({
      slug: z.string().min(1).max(64),
      // Пустое имя — вернуть придуманное.
      name: z.string().trim().max(24),
    }),
    output: z.object({ ok: z.literal(true) }),
  },
});

/** Канал, по которому страница узнаёт, что состав таверны поменялся. */
const CHANGED = "tavern-changed";

// v2: в вызовах появились дни (для табло) — старый кеш перечитываем.
const SCAN_KEY = "scan-v3";
const OVERVIEW_KEY = "overview-v1";

interface StoredOverview {
  day: string;
  overview: Overview;
  threads: number;
  generatedAt: number;
}

/** Сколько свежих тредов читаем для обзора — больше не влезает в разумный промпт. */
const OVERVIEW_MAX_THREADS = 30;
const NAMES_KEY = "names-v1";

interface ThreadCursor {
  seq: number;
  updatedAt: number;
}
interface ScanState {
  threads: Record<string, ThreadCursor>;
  usage: UsageMap;
  scannedAt: number | null;
}

/** Потолок API: больше 100 событий за запрос сервер не отдаёт. */
const PAGE = 100;
/** Сколько тредов читаем одновременно. */
const PARALLEL = 4;

export default async function plugin(bb: BbPluginApi) {
  /** Живёт, пока загружен плагин: обрывает запросы и `claude -p` при reload. */
  const lifetime = new AbortController();
  bb.onDispose(() => lifetime.abort());
  const settings = bb.settings.define({
    minActive: {
      type: "number",
      label: "Минимум работающих",
      description: "Сколько скиллов всегда ходят по залу, даже если давно никого не звали.",
      experimental_schema: z.number().int().min(5).max(12),
      default: 5,
    },
    maxActive: {
      type: "number",
      label: "Максимум работающих",
      experimental_schema: z.number().int().min(5).max(12),
      default: 12,
    },
    activeWindowHours: {
      type: "number",
      label: "Окно «работает», часов",
      description: "Скилл, который звали за это время, работает в зале. Остальные отдыхают на веранде.",
      experimental_schema: z.number().min(0.5).max(72),
      default: 3,
    },
    maxResting: {
      type: "number",
      label: "Отдыхающих на веранде",
      experimental_schema: z.number().int().min(0).max(10),
      default: 10,
    },
    lookbackDays: {
      type: "number",
      label: "Глубина истории, дней",
      experimental_schema: z.number().int().min(1).max(90),
      default: 30,
    },
    innkeeper: {
      type: "string",
      label: "Трактирщик",
      description:
        "Скилл, который зовут в каждой сессии: он стоит у очага и не вытесняет остальных. Пусто — без трактирщика.",
      default: "chat-names",
    },
    tvDays: {
      type: "number",
      label: "Телевизор: за сколько дней сводка",
      experimental_schema: z.number().int().min(1).max(14),
      default: 3,
    },
    headlinesModel: {
      type: "string",
      label: "Телевизор: модель для обзора",
      description:
        "Модель для `claude -p`. Обзор пишется раз в день по конспектам свежих тредов (превью сообщений; почты и телефоны вырезаются).",
      default: "sonnet",
    },
    claudeBin: {
      type: "string",
      label: "Телевизор: команда claude",
      default: "claude",
    },
  });
  const config = await settings.get();
  const innkeeperSlug = config.innkeeper.trim().toLowerCase() || null;

  // Старые форматы кеша: 256 КБ на ключ — не держим мусор.
  for (const old of ["scan-v1", "scan-v2", "headlines-v1"]) await bb.storage.kv.delete(old);
  // Курсоры и вызовы лежат под разными ключами: у значения kv потолок 256 КБ.
  const savedMeta = await bb.storage.kv.get<{ scannedAt: number | null }>(`${SCAN_KEY}:meta`);
  let scan: ScanState = {
    threads: (await bb.storage.kv.get<Record<string, ThreadCursor>>(`${SCAN_KEY}:cursors`)) ?? {},
    usage: (await bb.storage.kv.get<UsageMap>(`${SCAN_KEY}:usage`)) ?? {},
    scannedAt: savedMeta?.scannedAt ?? null,
  };
  // Курсоры без вызовов означали бы потерянные вызовы — начинаем заново.
  if (savedMeta === null) scan = { threads: {}, usage: {}, scannedAt: null };
  let scanning = false;
  /** Треды из последнего list — для заголовков и статуса «крутится». */
  let threadInfo = new Map<string, { title: string | null; busy: boolean }>();
  /** Треды для телевизора и имена проектов-«команд». */
  let tvThreads: TvThread[] = [];
  let waitingSource: WaitingInput[] = [];
  /** Последний ответ агента по треду, кеш по updatedAt. */
  const answerCache = new Map<string, { updatedAt: number; preview: string | null; at: number }>();
  let waitingList: WaitingItem[] = [];
  let projectNames = new Map<string, string>();
  let projectNamesAt = 0;
  /** Описания установленных скиллов; обновляем раз в 10 минут. */
  let skillInfo = new Map<string, string | null>();
  let skillInfoAt = 0;

  /**
   * Описания скиллов — из фронтматтера SKILL.md в каталогах скиллов на этой
   * машине. `bb.sdk.skills.list` обходит хосты и отвечает 504 через 30 секунд,
   * а локальные файлы читаются за миллисекунды.
   */
  function refreshSkillInfo(): Promise<void> {
    if (Date.now() - skillInfoAt < 600_000) return Promise.resolve();
    skillInfoAt = Date.now();
    return readSkillDescriptions(SKILL_DIRS).then(
      (next) => {
        if (next.size > 0) skillInfo = next;
        // Пусто — повторим на следующем проходе, а не через 10 минут.
        else skillInfoAt = 0;
        bb.log.info(`skill descriptions: ${next.size} ms=${Date.now() - skillInfoAt}`);
      },
      (error: unknown) => {
        skillInfoAt = 0;
        bb.log.warn(`skill descriptions failed: ${String(error)}`);
      },
    );
  }

  async function scanThread(
    threadId: string,
    cursor: ThreadCursor,
    since: number,
    installed: ReadonlySet<string>,
  ): Promise<boolean> {
    let afterSeq = cursor.seq;
    // Вызовы копим локально и записываем вместе с курсором: если вторая
    // страница упадёт, первая не засчитается дважды на следующем проходе.
    const hits: Array<[string, number]> = [];
    for (;;) {
      const rows = await bb.sdk.threads.events.list({
        threadId,
        types: ["item/completed"],
        order: "asc",
        limit: String(PAGE),
        ...(afterSeq > 0 ? { afterSeq: String(afterSeq) } : {}),
        signal: lifetime.signal,
      });
      for (const row of rows) {
        afterSeq = Math.max(afterSeq, row.seq);
        if (row.createdAt < since) continue;
        const item = (row.data as { item?: unknown }).item;
        for (const name of skillsFromItem(item, installed)) hits.push([name, row.createdAt]);
      }
      if (rows.length < PAGE) break;
    }
    for (const [name, at] of hits) recordUse(scan.usage, name, at, threadId);
    cursor.seq = afterSeq;
    return hits.length > 0;
  }

  /** Идущий проход: второй вызов (CLI посреди фонового) ждёт его, а не выходит пустым. */
  let inFlight: Promise<void> | null = null;
  function refresh(signal?: AbortSignal): Promise<void> {
    inFlight ??= runScan(signal).finally(() => {
      inFlight = null;
    });
    return inFlight;
  }

  async function runScan(signal?: AbortSignal): Promise<void> {
    scanning = true;
    const started = Date.now();
    try {
      const since = Date.now() - config.lookbackDays * 86_400_000;
      const threads = await bb.sdk.threads.list({ includeHidden: true, limit: 2000, signal: lifetime.signal });
      // Список установленных скиллов нужен разбору (чтения SKILL.md вне
      // домашних каталогов засчитываются только для установленных), поэтому
      // первый проход его ждёт; дальше он обновляется в фоне.
      const skillsReady = refreshSkillInfo();
      if (skillInfo.size === 0) await skillsReady;
      waitingSource = threads.map((t) => ({
        id: t.id,
        title: t.title ?? t.titleFallback,
        updatedAt: t.updatedAt,
        status: t.status,
        hasPendingInteraction: t.hasPendingInteraction,
        lastReadAt: t.lastReadAt,
        latestAttentionAt: t.latestAttentionAt,
        archived: t.archivedAt !== null,
        hidden: t.visibility !== "visible",
        child: t.parentThreadId !== null,
      }));
      tvThreads = threads.map((t) => ({
        id: t.id,
        title: t.title ?? t.titleFallback,
        projectId: t.projectId,
        updatedAt: t.updatedAt,
        hidden: t.visibility !== "visible",
        child: t.parentThreadId !== null,
      }));
      if (Date.now() - projectNamesAt > 600_000) {
        try {
          const projects = await bb.sdk.projects.list({ includePersonal: true, signal: lifetime.signal });
          projectNames = new Map(projects.map((p) => [p.id, p.name]));
          projectNamesAt = Date.now();
        } catch (error) {
          bb.log.warn(`projects.list failed: ${String(error)}`);
        }
      }
      threadInfo = new Map(
        threads.map((t) => [
          t.id,
          {
            title: t.title ?? t.titleFallback,
            busy: t.status === "active" || t.status === "starting" || t.status === "pending",
          },
        ]),
      );
      const installed = new Set(skillInfo.keys());
      const due = threads.filter((t) => {
        if (t.updatedAt < since) return false;
        const cursor = scan.threads[t.id];
        // Крутящийся тред дочитываем каждый проход: дешёвый запрос после
        // курсора, и не зависим от того, двигает ли новое событие updatedAt.
        if (threadInfo.get(t.id)?.busy) return true;
        return cursor === undefined || t.updatedAt > cursor.updatedAt;
      });
      let changed = false;
      let failed = 0;
      for (let i = 0; i < due.length && !signal?.aborted && !lifetime.signal.aborted; i += PARALLEL) {
        await Promise.all(
          due.slice(i, i + PARALLEL).map(async (t) => {
            const cursor = scan.threads[t.id] ?? { seq: 0, updatedAt: 0 };
            try {
              if (await scanThread(t.id, cursor, since, installed)) changed = true;
              cursor.updatedAt = t.updatedAt;
              scan.threads[t.id] = cursor;
            } catch (error) {
              failed += 1;
              bb.log.warn(`thread scan failed ${t.id}: ${String(error)}`);
            }
          }),
        );
      }
      // Курсоры тредов, выпавших из окна истории, больше не нужны.
      // По возрасту, а не по отсутствию в списке: тред за пределом limit
      // без курсора при новой активности перечитался бы с нуля и задвоился.
      for (const [id, cursor] of Object.entries(scan.threads)) if (cursor.updatedAt < since) delete scan.threads[id];
      scan.usage = pruneUsage(scan.usage, since);
      scan.scannedAt = Date.now();
      // Плагин выгружают — не пишем в хранилище и не запускаем модель.
      if (lifetime.signal.aborted) return;
      await bb.storage.kv.set(`${SCAN_KEY}:usage`, scan.usage);
      await bb.storage.kv.set(`${SCAN_KEY}:cursors`, scan.threads);
      await bb.storage.kv.set(`${SCAN_KEY}:meta`, { scannedAt: scan.scannedAt });
      if (due.length > 0) {
        bb.log.info(
          `scan done: threads=${threads.length} scanned=${due.length} failed=${failed} ` +
            `skills=${Object.keys(scan.usage).length} ms=${Date.now() - started}`,
        );
      }
      // Статус тредов («крутится») меняется и без новых вызовов, поэтому
      // сигналим каждый проход; страница сама сверит, поменялось ли что-то.
      bb.realtime.publish(CHANGED, { changed });
      // Обзор пишем только по завершённому проходу: посреди перескана
      // табло неполное, и заголовки ему противоречили.
      await refreshWaiting();
      if (lifetime.signal.aborted) return;
      const force = overviewForce;
      overviewForce = false;
      maybeOverview(force).catch((error: unknown) => bb.log.warn(`overview crashed: ${String(error)}`));
    } finally {
      scanning = false;
    }
  }

  async function customNames(): Promise<Record<string, string>> {
    return (await bb.storage.kv.get<Record<string, string>>(NAMES_KEY)) ?? {};
  }

  function toCharacter(slug: string, custom: Record<string, string>): Character {
    const use = scan.usage[slug] ?? { last: 0, count: 0, threadId: "" };
    const persona = personaFor(slug, custom);
    const thread = threadInfo.get(use.threadId);
    const description = skillInfo.get(slug) ?? null;
    return {
      slug,
      name: persona.name,
      sprite: persona.sprite,
      origin: persona.origin,
      line: persona.line,
      description: description === null ? null : description.slice(0, 400),
      lastUsedAt: use.last,
      count: use.count,
      threadId: use.threadId,
      threadTitle: thread?.title ?? null,
      busy: thread?.busy ?? false,
    };
  }

  async function state(): Promise<TavernState> {
    const custom = await customNames();
    const ranked = rankSkills(scan.usage, {
      now: Date.now(),
      activeWindowHours: config.activeWindowHours,
      minActive: config.minActive,
      maxActive: Math.max(config.minActive, config.maxActive),
      maxResting: config.maxResting,
      exclude: new Set(innkeeperSlug ? [innkeeperSlug] : []),
    });
    const innkeeper =
      innkeeperSlug && scan.usage[innkeeperSlug] ? toCharacter(innkeeperSlug, custom) : null;
    // Спрайты разводим на всю сцену разом; трактирщик первым держит свой.
    const cast = distinctSprites([
      ...(innkeeper ? [innkeeper] : []),
      ...ranked.working.map((r) => toCharacter(r.name, custom)),
      ...ranked.resting.map((r) => toCharacter(r.name, custom)),
    ]);
    // Одинаковые имена на сцене (три дневника — все «Дневя») различаем
    // римской цифрой, иначе в зале непонятно, кто есть кто.
    const seen = new Map<string, number>();
    for (const c of cast) {
      const n = (seen.get(c.name) ?? 0) + 1;
      seen.set(c.name, n);
      if (n > 1) c.name = `${c.name} ${["", "", "II", "III", "IV", "V"][n] ?? n}`;
    }
    const offset = innkeeper ? 1 : 0;
    return {
      innkeeper: innkeeper ? cast[0]! : null,
      working: cast.slice(offset, offset + ranked.working.length),
      resting: cast.slice(offset + ranked.working.length),
      scannedAt: scan.scannedAt,
      scanning,
      activeWindowHours: config.activeWindowHours,
    };
  }

  function board() {
    return buildBoard({
      now: Date.now(),
      days: lastDays(Date.now(), config.tvDays),
      threads: tvThreads,
      projectNames,
      usage: scan.usage,
      exclude: new Set(innkeeperSlug ? [innkeeperSlug] : []),
    });
  }

  /** Конспекты тредов кешируем по updatedAt: тред не менялся — не перечитываем. */
  const digestCache = new Map<string, { updatedAt: number; digest: ThreadDigest | null }>();

  async function collectDigests(): Promise<ThreadDigest[]> {
    const since = dayStart(lastDays(Date.now(), config.tvDays).at(-1)!);
    const picks = tvThreads
      .filter((t) => !t.hidden && !t.child && t.title && t.updatedAt >= since)
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, OVERVIEW_MAX_THREADS);
    const out: Array<ThreadDigest | null> = new Array(picks.length).fill(null);
    for (let i = 0; i < picks.length; i += PARALLEL) {
      await Promise.all(
        picks.slice(i, i + PARALLEL).map(async (t, j) => {
          const cached = digestCache.get(t.id);
          if (cached && cached.updatedAt === t.updatedAt) {
            out[i + j] = cached.digest;
            return;
          }
          try {
            const { items } = await bb.sdk.threads.conversationOutline({ threadId: t.id });
            const asks = items.filter((m) => m.role === "user").map((m) => m.preview);
            const answers = items.filter((m) => m.role === "assistant").map((m) => m.preview);
            // Первые просьбы задают задачу, последние — куда она пришла.
            const pickedAsks = asks.length <= 4 ? asks : [...asks.slice(0, 2), ...asks.slice(-2)];
            const digest: ThreadDigest | null =
              asks.length === 0
                ? null
                : {
                    id: t.id,
                    title: t.title!,
                    project: projectNames.get(t.projectId) ?? "проект",
                    updatedAt: t.updatedAt,
                    asks: pickedAsks,
                    outcome: answers.slice(-2),
                  };
            digestCache.set(t.id, { updatedAt: t.updatedAt, digest });
            out[i + j] = digest;
          } catch (error) {
            bb.log.warn(`outline failed ${t.id}: ${String(error).slice(0, 200)}`);
          }
        }),
      );
    }
    return out.filter((d): d is ThreadDigest => d !== null);
  }

  /**
   * Обзор пишет `claude -p` раз в московские сутки (или по кнопке ↻) по
   * конспектам свежих тредов. Каждая попытка — в лог: модель, сколько тредов,
   * длительность, итог. Упала — телевизор показывает табло и прошлый обзор,
   * следующая попытка через 30 минут.
   */
  let overviewState: "idle" | "running" | "failed" = "idle";
  /** Кнопка ↻ нажата во время прохода — перепишем сразу после него. */
  let overviewForce = false;
  let overviewFailedAt = 0;
  /** Синхронный замок: фон и кнопка ↻ не запустят модель дважды. */
  let overviewBusy = false;
  async function maybeOverview(force: boolean): Promise<boolean> {
    if (overviewBusy) return false;
    if (!force && overviewState === "failed" && Date.now() - overviewFailedAt < 30 * 60_000) return false;
    overviewBusy = true;
    const bin = resolveClaudeBin(config.claudeBin.trim() || "claude");
    const model = config.headlinesModel.trim() || "sonnet";
    let threads = 0;
    let promptChars = 0;
    let ran = false;
    try {
      const today = moscowDay(Date.now());
      const stored = await bb.storage.kv.get<StoredOverview>(OVERVIEW_KEY);
      if (!force && stored?.day === today) return false;
      ran = true;
      // «Пишем…» показываем только когда правда пишем, а не на каждой проверке.
      overviewState = "running";
      bb.realtime.publish(CHANGED, { changed: true });
      const digests = await collectDigests();
      threads = digests.length;
      if (threads === 0) throw new Error("нет конспектов тредов за период");
      const prompt = overviewPrompt(config.tvDays, digests, board().topSkills);
      promptChars = prompt.length;
      const run = await runClaude({
        bin,
        model,
        system: OVERVIEW_SYSTEM,
        prompt,
        schema: OVERVIEW_SCHEMA,
        timeoutMs: 240_000,
        budgetUsd: 0.5,
        signal: lifetime.signal,
      });
      const overview = parseOverview(run.stdout, digests);
      await bb.storage.kv.set(OVERVIEW_KEY, {
        day: today,
        overview,
        threads,
        generatedAt: Date.now(),
      } satisfies StoredOverview);
      overviewState = "idle";
      bb.log.info(
        `overview ok: model=${model} threads=${threads} promptChars=${promptChars} ms=${run.ms} ` +
          `themes=${overview.themes.length} next=${overview.next.length} forced=${force}`,
      );
      return true;
    } catch (error) {
      // Любой сбой — «failed» с паузой 30 минут, а не вечное «пишем…».
      overviewState = "failed";
      overviewFailedAt = Date.now();
      bb.log.warn(
        `overview failed: model=${model} bin=${bin} threads=${threads} promptChars=${promptChars} forced=${force} ` +
          `error=${String(error).slice(0, 300)}`,
      );
      return false;
    } finally {
      overviewBusy = false;
      if (ran) bb.realtime.publish(CHANGED, { changed: true });
    }
  }

  function visibleOnly(o: Overview): Overview {
    const visible = new Set(tvThreads.filter((t) => !t.hidden).map((t) => t.id));
    // Пока список тредов не загружен — показываем как есть.
    if (visible.size === 0) return o;
    return {
      ...o,
      themes: o.themes.map((t) => ({ ...t, threadIds: t.threadIds.filter((id) => visible.has(id)) })),
      next: o.next.map((n) => ({ ...n, threadId: n.threadId && visible.has(n.threadId) ? n.threadId : null })),
    };
  }

  /** Весь последний ответ агента: вопрос к человеку обычно в конце, а превью — это начало. */
  async function lastAnswer(t: WaitingInput): Promise<string | null> {
    const cached = answerCache.get(t.id);
    // Неудачу тоже кешируем на минуту, чтобы не долбить сервер каждый проход.
    if (cached && cached.updatedAt === t.updatedAt && (cached.preview !== null || Date.now() - cached.at < 60_000)) {
      return cached.preview;
    }
    try {
      const { output } = await bb.sdk.threads.output({ threadId: t.id, signal: AbortSignal.any([lifetime.signal, AbortSignal.timeout(10_000)]) });
      answerCache.set(t.id, { updatedAt: t.updatedAt, preview: output, at: Date.now() });
      return output;
    } catch (error) {
      answerCache.set(t.id, { updatedAt: t.updatedAt, preview: null, at: Date.now() });
      bb.log.warn(`output failed ${t.id}: ${String(error).slice(0, 200)}`);
      return null;
    }
  }

  /**
   * «Ждут тебя» — считаем раз в проход, после свежего списка тредов, а не на
   * каждый запрос страницы. Без модели: по статусам тредов и их последним ответам.
   */
  async function refreshWaiting(): Promise<void> {
    const since = dayStart(lastDays(Date.now(), config.tvDays).at(-1)!);
    const cands = candidates(waitingSource, since)
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, 15);
    const keep = new Set(cands.map((t) => t.id));
    for (const id of answerCache.keys()) if (!keep.has(id)) answerCache.delete(id);
    const items = await Promise.all(
      cands.map(async (t): Promise<WaitingItem | null> => {
        const answer = await lastAnswer(t);
        const reason = classify(t, answer);
        if (reason === null) return null;
        return { threadId: t.id, title: t.title ?? "без названия", reason, line: hookLine(answer), updatedAt: t.updatedAt };
      }),
    );
    waitingList = rankWaiting(
      items.filter((x): x is WaitingItem => x !== null),
      6,
    );
  }

  async function tv(): Promise<TvState> {
    const b = board();
    const custom = await customNames();
    const stored = await bb.storage.kv.get<StoredOverview>(OVERVIEW_KEY);
    return {
      ...b,
      mvp: b.mvp ? { ...toCharacter(b.mvp.slug, custom), calls: b.mvp.calls } : null,
      topSkills: b.topSkills.map((s) => ({ ...s, name: personaFor(s.slug, custom).name })),
      // Тред, скрытый после обзора, не должен остаться ссылкой в нём.
      overview: stored ? visibleOnly(stored.overview) : null,
      overviewAt: stored?.generatedAt ?? null,
      overviewThreads: stored?.threads ?? 0,
      overviewState,
      waiting: waitingList,
    };
  }

  bb.rpc.register(rpcContract, {
    tavern_state: () => state(),
    tavern_tv: () => tv(),
    tavern_tv_refresh: () => {
      if (overviewBusy) return { started: false };
      // Во время прохода (и до первого) просим переписать сразу после него.
      if (inFlight !== null || scan.scannedAt === null || tvThreads.length === 0) overviewForce = true;
      else maybeOverview(true).catch((error: unknown) => bb.log.warn(`overview crashed: ${String(error)}`));
      return { started: true };
    },
    tavern_rename: async ({ slug, name }) => {
      const custom = await customNames();
      if (name === "") delete custom[slug];
      else custom[slug] = name;
      await bb.storage.kv.set(NAMES_KEY, custom);
      bb.realtime.publish(CHANGED, { changed: true });
      return { ok: true as const };
    },
  });

  bb.cli.register({
    name: "tavern",
    summary: "Кто сейчас работает и отдыхает в таверне скиллов",
    commands: [
      { name: "list", summary: "Показать состав таверны", usage: "bb tavern list [--json]" },
      { name: "scan", summary: "Перечитать ленты тредов сейчас", usage: "bb tavern scan" },
      { name: "tv", summary: "Что сейчас показывает телевизор", usage: "bb tavern tv [--refresh] [--json]" },
    ],
    async run(argv) {
      const json = argv.includes("--json");
      const [command] = argv.filter((arg) => arg !== "--json");
      if (command === "tv") {
        if (argv.includes("--refresh")) {
          if (inFlight !== null) await inFlight;
          await maybeOverview(true);
        }
        const t = await tv();
        if (json) return { exitCode: 0, stdout: JSON.stringify(t).slice(0, 60_000) };
        const lines = [
          `За ${t.days} дн: тредов ${t.totalThreads}, вызовов скиллов ${t.totalCalls}`,
          ...t.teams.map((x) => `  ${x.name}: ${x.threads} тредов, ${x.skillCalls} вызовов`),
          t.mvp ? `Игрок периода: ${t.mvp.name} (${t.mvp.slug}) — ${t.mvp.calls}` : "Игрока нет",
          `Обзор (${t.overviewState}, тредов ${t.overviewThreads}):`,
          ...(t.overview
            ? [
                `  ${t.overview.summary}`,
                ...t.overview.themes.map((x) => `  [${x.status}] ${x.title} — ${x.what} (${x.threadIds.join(", ")})`),

              ]
            : ["  —"]),
          "Ждут тебя:",
          ...t.waiting.map((w) => `  [${w.reason}] ${w.title} — ${w.line}`),
          "Бегущая строка:",
          ...t.ticker.slice(0, 8).map((x) => `  ${x.title}`),
        ];
        return { exitCode: 0, stdout: lines.join("\n") };
      }
      if (command === "scan") {
        await refresh();
        return { exitCode: 0, stdout: `Скиллов в истории: ${Object.keys(scan.usage).length}` };
      }
      if (command === undefined || command === "list") {
        const s = await state();
        if (json) return { exitCode: 0, stdout: JSON.stringify(s).slice(0, 60_000) };
        const ago = (ms: number) => `${Math.round((Date.now() - ms) / 60_000)} мин назад`;
        const row = (c: Character) =>
          `  ${c.name.padEnd(12)} ${c.slug.padEnd(32)} ${ago(c.lastUsedAt)}${c.busy ? "  [крутится]" : ""}`;
        const lines = [
          s.innkeeper ? `Трактирщик: ${s.innkeeper.name} (${s.innkeeper.slug})` : "Трактирщика нет",
          `Работают (${s.working.length}):`,
          ...s.working.map(row),
          `Отдыхают (${s.resting.length}):`,
          ...s.resting.map(row),
        ];
        return { exitCode: 0, stdout: lines.join("\n") };
      }
      return { exitCode: 1, stderr: "Использование: bb tavern [list|scan|tv] [--json]" };
    },
  });

  bb.background.service("scanner", {
    async start(signal) {
      // Остановка (reload, disable) не ждёт конца прохода: иначе bb считает,
      // что сервис не остановился, и плагин зависает в «degraded». Проход сам
      // бросит работу на следующей странице — сигнал уходит в каждый запрос.
      const stopped = new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
      while (!signal.aborted) {
        try {
          await Promise.race([refresh(signal), stopped]);
        } catch (error) {
          bb.log.warn(`scan failed: ${String(error)}`);
        }
        await new Promise<void>((resolve) => {
          const timer = setTimeout(resolve, 30_000);
          signal.addEventListener(
            "abort",
            () => {
              clearTimeout(timer);
              resolve();
            },
            { once: true },
          );
        });
      }
    },
  });
}
