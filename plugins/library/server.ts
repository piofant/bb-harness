// bb-plugin-library — «Библиотека»: база знаний агентов в стиле таверны.
//
// Раз в минуту перечитывает файлы базы (настройка root, по умолчанию
// $KB_ROOT или ~/agent-knowledge): индекс
// карточек, лог подсказок (что подкладывали агентам), git-историю карточек,
// заметки во входящих и отчёт о здоровье. В bb за историей не ходит.
import { execFile } from "node:child_process";
import { readFile, readdir, stat, open, realpath } from "node:fs/promises";
import { join, resolve } from "node:path";
import { promisify } from "node:util";
import { defineRpcContract, type BbPluginApi } from "@get-bb/plugin-sdk";
import { z } from "zod";
import {
  AREAS,
  cardBody,
  parseGitLog,
  parseHealth,
  parseHints,
  parseInbox,
  parseIndex,
  type Area,
  type Card,
  type Change,
  type Health,
  type Note,
  type Read,
} from "./lib/kb";
import {
  decisionPrompt,
  notedSessions,
  parseConflicts,
  pipeline,
  RUN_ARGS,
  savePrompt,
  sessionSource,
  harvestedSessions,
  type Choice,
  type Conflict,
  type Step,
  type StepId,
} from "./lib/manage";
import { spawn } from "node:child_process";
import { homedir, hostname } from "node:os";

/** Каталог базы по умолчанию: $KB_ROOT или ~/agent-knowledge. */
const DEFAULT_ROOT = process.env.KB_ROOT || join(homedir(), "agent-knowledge");

const run = promisify(execFile);
const area = z.enum(AREAS);

const stateSchema = z.object({
  totals: z.array(z.object({ area, cards: z.number(), reads1h: z.number(), reads24h: z.number() })),
  reads: z.array(
    z.object({ name: z.string(), area: area.nullable(), description: z.string(), count: z.number(), last: z.number() }),
  ),
  changes: z.array(
    z.object({ name: z.string(), area, kind: z.enum(["added", "updated"]), at: z.number(), description: z.string() }),
  ),
  notes: z.array(z.object({ id: z.string(), area: z.string(), at: z.number(), text: z.string() })),
  health: z.object({
    status: z.enum(["green", "yellow", "red", "unknown"]),
    at: z.number().nullable(),
    problems: z.array(z.object({ level: z.string(), what: z.string() })),
  }),
  loadedAt: z.number().nullable(),
  error: z.string().nullable(),
});
export type LibraryState = z.infer<typeof stateSchema>;

const manageSchema = z.object({
  /** Хост сервера bb — чтобы открыть карточку в панели предпросмотра (Obsidian at Home). */
  hostId: z.string().nullable(),
  root: z.string(),
  conflicts: z.array(
    z.object({
      slug: z.string(),
      date: z.string(),
      text: z.string(),
      /** Решение уже отдано агенту: его тред и что выбрал пользователь. */
      pending: z.object({ threadId: z.string(), choice: z.string(), at: z.number(), stale: z.boolean() }).nullable(),
    }),
  ),
  steps: z.array(
    z.object({
      id: z.enum(["ingest", "consolidate", "index", "daily", "weekly"]),
      label: z.string(),
      at: z.number().nullable(),
      status: z.string(),
      level: z.enum(["ok", "warn", "bad", "unknown"]),
      why: z.string(),
      runnable: z.boolean(),
      running: z.boolean(),
      lastRun: z.string().nullable(),
    }),
  ),
  unsaved: z.array(z.object({ threadId: z.string(), title: z.string(), updatedAt: z.number(), requested: z.boolean() })),
});
export type ManageState = z.infer<typeof manageSchema>;

export const rpcContract = defineRpcContract({
  library_manage: { input: z.null(), output: manageSchema },
  library_host: { input: z.null(), output: z.object({ hostId: z.string().nullable() }) },
  /** Решение пользователя по конфликту — агент оформит его по правилам базы. */
  library_decide: {
    input: z.object({
      slug: z.string().regex(/^[\w.-]{1,120}$/),
      choice: z.enum(["first", "second", "both", "custom"]),
      note: z.string().trim().max(600),
    }),
    output: z.object({ threadId: z.string() }),
  },
  library_run: {
    input: z.object({ step: z.enum(["ingest", "consolidate", "index"]) }),
    output: z.object({ started: z.boolean() }),
  },
  library_save: { input: z.object({ threadId: z.string().regex(/^thr_\w+$/) }), output: z.object({ ok: z.literal(true) }) },
  library_state: { input: z.null(), output: stateSchema },
  library_card: {
    input: z.object({ name: z.string().regex(/^[\w.-]{1,120}$/) }),
    output: z.object({ name: z.string(), area, path: z.string(), body: z.string() }).nullable(),
  },
});

const CHANGED = "library-changed";
/** Сколько хвоста лога подсказок читаем: ~1 МБ — это недели истории. */
const HINT_TAIL = 1_000_000;

async function tail(path: string, bytes: number): Promise<string> {
  const fh = await open(path, "r");
  try {
    const { size } = await fh.stat();
    const start = Math.max(0, size - bytes);
    const buf = Buffer.alloc(size - start);
    await fh.read(buf, 0, buf.length, start);
    const text = buf.toString("utf8");
    // Первая строка после отреза может быть обрубком.
    return start > 0 ? text.slice(text.indexOf("\n") + 1) : text;
  } finally {
    await fh.close();
  }
}

export default async function plugin(bb: BbPluginApi) {
  const settings = bb.settings.define({
    root: { type: "string", label: "Каталог базы знаний", default: DEFAULT_ROOT },
    projectId: {
      type: "string",
      label: "Проект для тредов-поручений",
      description: "В каком проекте создавать тред, который оформляет решение. Пусто — первый проект.",
      default: "",
    },
    unsavedDays: {
      type: "number",
      label: "«Ничего не сохранили» — за сколько дней",
      experimental_schema: z.number().int().min(1).max(30),
      default: 7,
    },
    days: {
      type: "number",
      label: "«Новое» — за сколько дней",
      experimental_schema: z.number().int().min(1).max(14),
      default: 3,
    },
  });
  const lifetime = new AbortController();
  bb.onDispose(() => lifetime.abort());

  let cards = new Map<string, Card>();
  let reads: Read[] = [];
  let changes: Change[] = [];
  let notes: Note[] = [];
  let health: Health = { status: "unknown", at: null, problems: [] };
  let loadedAt: number | null = null;
  let error: string | null = null;
  let fingerprint = "";
  let conflicts: Conflict[] = [];
  let steps: Step[] = [];
  let unsaved: Array<{ threadId: string; title: string; updatedAt: number }> = [];
  let hostId: string | null = null;
  let kbRoot = DEFAULT_ROOT;
  /** id сессии провайдера по треду — не меняется, кешируем навсегда. */
  const providerIds = new Map<string, string>();
  /** Провайдер треда (claude-code, codex…) и статус — для источника заметки и «решить заново». */
  const threadMeta = new Map<string, { providerId: string; status: string }>();
  const running = new Map<StepId, boolean>();
  /** Один прогон kb за раз: шаги берут одну блокировку репозитория базы. */
  let kbBusy = false;
  /** Замки от двойного клика и двух вкладок. */
  const deciding = new Set<string>();
  const saving = new Set<string>();
  const lastRun = new Map<StepId, string>();

  async function load(): Promise<void> {
    const cfg = await settings.get();
    const root = cfg.root.trim() || DEFAULT_ROOT;
    const now = Date.now();
    const since = now - cfg.days * 86_400_000;
    const problems: string[] = [];
    try {
      cards = new Map(parseIndex(await readFile(join(root, "INDEX.tsv"), "utf8")).map((c) => [c.name, c]));
    } catch (e) {
      problems.push(`индекс: ${String(e).slice(0, 120)}`);
    }
    try {
      reads = parseHints(await tail(join(root, "state/kb-hint.jsonl"), HINT_TAIL), now - 7 * 86_400_000);
    } catch (e) {
      problems.push(`подсказки: ${String(e).slice(0, 120)}`);
    }
    try {
      const { stdout } = await run(
        "git",
        ["-C", root, "log", `--since=${cfg.days} days ago`, "--name-status", "--pretty=@%ct %s", "--", "cards"],
        { timeout: 20_000, maxBuffer: 8_000_000, signal: lifetime.signal },
      );
      // Пока индекс не прочитан — показываем всё, иначе список был бы пуст.
      changes = parseGitLog(stdout).filter((c) => cards.size === 0 || cards.has(c.name));
    } catch (e) {
      problems.push(`git: ${String(e).slice(0, 120)}`);
    }
    try {
      const inbox = join(root, "raw/inbox");
      const machines = await readdir(inbox);
      const found: Note[] = [];
      for (const m of machines) {
        const dir = join(inbox, m);
        if (!(await stat(dir)).isDirectory()) continue;
        for (const f of (await readdir(dir)).filter((x) => /^\d{4}-\d{2}-\d{2}\.md$/.test(x)).sort().slice(-cfg.days - 1)) {
          found.push(...parseInbox(await readFile(join(dir, f), "utf8")));
        }
      }
      notes = found.filter((n) => n.at >= since).sort((a, b) => b.at - a.at);
    } catch (e) {
      problems.push(`входящие: ${String(e).slice(0, 120)}`);
    }
    try {
      health = parseHealth(await readFile(join(root, "state/health.json"), "utf8"));
    } catch (e) {
      problems.push(`здоровье: ${String(e).slice(0, 120)}`);
    }
    kbRoot = root;
    await loadManage(root, cfg.unsavedDays).catch((e: unknown) => problems.push(`управление: ${String(e).slice(0, 120)}`));
    loadedAt = now;
    error = problems.length > 0 ? problems.join("; ") : null;
    if (error) bb.log.warn(`load: ${error}`);
    // Сигналим странице, только если что-то поменялось.
    const fp = [conflicts.map((c) => c.slug).join(), steps.map((x) => x.status + x.at).join(), unsaved.length, cards.size, reads[0]?.last, reads.length, changes[0]?.at, notes[0]?.id, health.at, error].join("|");
    if (fp !== fingerprint) {
      fingerprint = fp;
      bb.realtime.publish(CHANGED, {});
    }
  }

  function state(): LibraryState {
    const now = Date.now();
    const per = new Map<Area, { cards: number; reads1h: number; reads24h: number }>(
      AREAS.map((a) => [a, { cards: 0, reads1h: 0, reads24h: 0 }]),
    );
    for (const c of cards.values()) per.get(c.area)!.cards += 1;
    for (const r of reads) {
      const c = cards.get(r.name);
      if (!c) continue;
      if (r.last >= now - 3_600_000) per.get(c.area)!.reads1h += 1;
      if (r.last >= now - 86_400_000) per.get(c.area)!.reads24h += 1;
    }
    return {
      totals: AREAS.map((a) => ({ area: a, ...per.get(a)! })),
      // Только карточки из индекса: закрытые (restricted) и удалённые не показываем даже названием.
      reads: reads.filter((r) => cards.has(r.name)).slice(0, 40).map((r) => {
        const c = cards.get(r.name);
        return { ...r, area: c?.area ?? null, description: c?.description ?? "" };
      }),
      changes: changes.slice(0, 40).map((c) => ({ ...c, description: cards.get(c.name)?.description ?? "" })),
      notes: notes.slice(0, 30),
      health,
      loadedAt,
      error,
    };
  }

  async function card(name: string) {
    const c = cards.get(name);
    if (!c) return null;
    const root = await realpath(resolve((await settings.get()).root.trim() || DEFAULT_ROOT));
    // Путь из индекса проверен в parseIndex; realpath — чтобы и симлинк не
    // увёл за пределы каталога карточек.
    const full = await realpath(resolve(root, c.path)).catch(() => null);
    if (full === null || !full.startsWith(join(root, "cards") + "/")) return null;
    const body = cardBody(await readFile(full, "utf8")).slice(0, 60_000);
    return { name: c.name, area: c.area, path: full, body };
  }

  async function loadManage(root: string, unsavedDays: number): Promise<void> {
    conflicts = parseConflicts(await readFile(join(root, "CONFLICTS.md"), "utf8").catch(() => ""));
    steps = pipeline(await tail(join(root, "state/runs.jsonl"), 400_000).catch(() => ""), Date.now());
    if (hostId === null) {
      const hosts = await bb.sdk.hosts.list({ signal: lifetime.signal }).catch(() => []);
      // Карточки лежат на машине сервера bb — берём хост с её именем.
      hostId = (hosts.find((h) => h.name === hostname()) ?? hosts[0])?.id ?? null;
    }
    // Треды без сохранённого знания: недавние, спокойные, не крошечные, из
    // сессий которых в базу не пришло ни одной заметки.
    const noted = new Set<string>();
    const inbox = join(root, "raw/inbox");
    for (const m of await readdir(inbox).catch(() => [] as string[])) {
      const files = (await readdir(join(inbox, m)).catch(() => [] as string[])).filter((f) => /^\d{4}-\d{2}-\d{2}\.md$/.test(f)).sort().slice(-(unsavedDays + 7));
      for (const f of files) for (const id of notedSessions(await readFile(join(inbox, m, f), "utf8").catch(() => ""))) noted.add(id);
    }
    // Сессии, которые сборщик уже разобрал, тоже не «забыты»: он решил, что сохранять нечего.
    for (const id of harvestedSessions(await readFile(join(root, "state/harvest.json"), "utf8").catch(() => ""))) noted.add(id);
    const since = Date.now() - unsavedDays * 86_400_000;
    const threads = await bb.sdk.threads.list({ limit: 300, hasParent: false, signal: lifetime.signal });
    threadMeta.clear();
    for (const t of threads) threadMeta.set(t.id, { providerId: t.providerId, status: t.status });
    const picks = threads
      .filter(
        (t) =>
          t.visibility === "visible" &&
          t.archivedAt === null &&
          t.status === "idle" &&
          t.updatedAt >= since &&
          t.updatedAt - t.createdAt > 15 * 60_000 &&
          // Свои треды-поручения («БЗ: решение …») не в счёт.
          !(t.title ?? "").startsWith("БЗ: ") &&
          (t.title ?? t.titleFallback),
      )
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, 30);
    for (const t of picks) {
      if (providerIds.has(t.id)) continue;
      const rows = await bb.sdk.threads.events
        .list({ threadId: t.id, limit: "1", order: "desc", types: ["item/completed"], signal: lifetime.signal })
        .catch(() => []);
      const pid = (rows[0]?.data as { providerThreadId?: unknown } | undefined)?.providerThreadId;
      // Кешируем только найденное: сбой или пустая лента — спросим в следующий раз.
      if (typeof pid === "string") providerIds.set(t.id, pid);
    }
    const keep = new Set(picks.map((t) => t.id));
    for (const id of providerIds.keys()) if (!keep.has(id)) providerIds.delete(id);
    unsaved = picks
      .filter((t) => {
        const pid = providerIds.get(t.id);
        return pid !== undefined && !noted.has(pid);
      })
      .slice(0, 8)
      .map((t) => ({ threadId: t.id, title: (t.title ?? t.titleFallback)!, updatedAt: t.updatedAt }));
  }

  async function manage(): Promise<ManageState> {
    const decided = (await bb.storage.kv.get<Record<string, { threadId: string; choice: string; at: number }>>("decisions-v1")) ?? {};
    const requested = new Set((await bb.storage.kv.get<string[]>("save-requested-v1")) ?? []);
    return {
      hostId,
      root: kbRoot,
      conflicts: conflicts.map((c) => {
        const p = decided[c.slug];
        if (!p) return { ...c, pending: null };
        // Конфликт всё ещё открыт, а тред агента упал или давно закончил —
        // значит, не оформил: даём решить заново.
        const st = threadMeta.get(p.threadId)?.status;
        const stale = st === "error" || (st === "idle" && Date.now() - p.at > 20 * 60_000) || Date.now() - p.at > 6 * 3_600_000;
        return { ...c, pending: { ...p, stale } };
      }),
      steps: steps.map((st) => ({ ...st, running: running.get(st.id) ?? false, lastRun: lastRun.get(st.id) ?? null })),
      unsaved: unsaved.map((u) => ({ ...u, requested: requested.has(u.threadId) })),
    };
  }

  async function projectForTasks(): Promise<string> {
    const configured = (await settings.get()).projectId.trim();
    if (configured) return configured;
    const projects = await bb.sdk.projects.list({ signal: lifetime.signal });
    const id = projects[0]?.id;
    if (!id) throw new Error("нет проекта для треда-поручения — задай projectId в настройках плагина");
    return id;
  }

  async function decide(slug: string, choice: Choice, note: string): Promise<{ threadId: string }> {
    const c = conflicts.find((x) => x.slug === slug);
    if (!c) throw new Error("Такого открытого конфликта нет — возможно, его уже закрыли.");
    if (choice === "custom" && !note) throw new Error("Напиши свой ответ.");
    if (deciding.has(slug)) throw new Error("Решение уже отправляется.");
    const before = (await bb.storage.kv.get<Record<string, { threadId: string; choice: string; at: number }>>("decisions-v1")) ?? {};
    const prev = before[slug];
    if (prev) {
      const st = threadMeta.get(prev.threadId)?.status;
      const stale = st === "error" || (st === "idle" && Date.now() - prev.at > 20 * 60_000) || Date.now() - prev.at > 6 * 3_600_000;
      if (!stale) throw new Error("Агент уже оформляет это решение.");
    }
    deciding.add(slug);
    try {
    const projectId = await projectForTasks();
    const thread = await bb.sdk.threads.spawn({
      projectId,
      environment: { type: "project-default" },
      prompt: decisionPrompt(c, choice, note, kbRoot),
      title: `БЗ: решение · ${slug}`,
      pluginMetadata: { conflict: slug, choice },
    });
    const decided = (await bb.storage.kv.get<Record<string, { threadId: string; choice: string; at: number }>>("decisions-v1")) ?? {};
    decided[slug] = { threadId: thread.id, choice, at: Date.now() };
    await bb.storage.kv.set("decisions-v1", decided);
    bb.log.info(`decision: ${slug} choice=${choice} thread=${thread.id}`);
    bb.realtime.publish(CHANGED, {});
    return { threadId: thread.id };
    } finally {
      deciding.delete(slug);
    }
  }

  function runStep(step: StepId): boolean {
    const args = RUN_ARGS[step];
    if (!args || kbBusy || lifetime.signal.aborted) return false;
    kbBusy = true;
    running.set(step, true);
    const started = Date.now();
    bb.realtime.publish(CHANGED, {});
    // Своя группа процессов: при таймауте и выгрузке плагина гасим kb вместе
    // с его дочерними процессами (LLM-вызовы сводчика), а не только Python.
    const child = spawn("kb", args, { stdio: ["ignore", "pipe", "pipe"], detached: true });
    let out = "";
    const keep = (d: Buffer) => {
      out = (out + d.toString("utf8")).slice(-2000);
    };
    child.stdout.on("data", keep);
    child.stderr.on("data", keep);
    const killGroup = () => {
      if (child.pid === undefined || child.exitCode !== null) return;
      try {
        process.kill(-child.pid, "SIGTERM");
        setTimeout(() => {
          try {
            if (child.exitCode === null) process.kill(-child.pid!, "SIGKILL");
          } catch {
            // уже завершился
          }
        }, 15_000).unref();
      } catch {
        // уже завершился
      }
    };
    // `kb daily` обычно идёт около получаса — даём с запасом.
    const timer = setTimeout(killGroup, 90 * 60_000);
    const onAbort = () => killGroup();
    lifetime.signal.addEventListener("abort", onAbort, { once: true });
    let done = false;
    const finish = (code: number | null, err?: unknown) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      lifetime.signal.removeEventListener("abort", onAbort);
      kbBusy = false;
      running.set(step, false);
      const tailLine = out.trim().split("\n").at(-1) ?? "";
      lastRun.set(step, err ? `не запустился: ${String(err).slice(0, 120)}` : `${code === 0 ? "готово" : `код ${code}`}: ${tailLine.slice(0, 160)}`);
      bb.log.info(`kb ${args.join(" ")}: code=${code} ms=${Date.now() - started}${err ? ` error=${String(err).slice(0, 200)}` : ""}`);
      if (!lifetime.signal.aborted) load().catch(() => undefined);
    };
    child.on("error", (e) => finish(null, e));
    child.on("close", (code) => finish(code));
    return true;
  }

  async function requestSave(threadId: string): Promise<void> {
    if (!unsaved.some((u) => u.threadId === threadId)) throw new Error("Этот тред не в списке «ничего не сохранили».");
    if (saving.has(threadId)) throw new Error("Уже попросил.");
    const requested = (await bb.storage.kv.get<string[]>("save-requested-v1")) ?? [];
    if (requested.includes(threadId)) throw new Error("Уже попросил этот тред.");
    const pid = providerIds.get(threadId);
    if (!pid) throw new Error("Не знаю сессию этого треда — попробуй через минуту.");
    saving.add(threadId);
    try {
      const source = sessionSource(threadMeta.get(threadId)?.providerId ?? "claude", pid);
      await bb.sdk.threads.send({ threadId, input: [{ type: "text", text: savePrompt(source), mentions: [] }], mode: "queue-if-active" });
      const now = (await bb.storage.kv.get<string[]>("save-requested-v1")) ?? [];
      await bb.storage.kv.set("save-requested-v1", [...new Set([...now, threadId])].slice(-200));
    } finally {
      saving.delete(threadId);
    }
    bb.log.info(`save requested: ${threadId}`);
    bb.realtime.publish(CHANGED, {});
  }

  bb.rpc.register(rpcContract, {
    library_manage: () => manage(),
    library_host: () => ({ hostId }),
    library_decide: ({ slug, choice, note }) => decide(slug, choice, note),
    library_run: ({ step }) => ({ started: runStep(step) }),
    library_save: async ({ threadId }) => {
      await requestSave(threadId);
      return { ok: true as const };
    },
    library_state: () => state(),
    library_card: ({ name }) => card(name),
  });

  bb.cli.register({
    name: "library",
    summary: "Библиотека базы знаний: что читают агенты, что нового, здоровье",
    commands: [{ name: "status", summary: "Сводка", usage: "bb library status" }],
    async run() {
      await load();
      const s = state();
      const ago = (ms: number) => `${Math.round((Date.now() - ms) / 60_000)} мин назад`;
      return {
        exitCode: 0,
        stdout: [
          `Здоровье: ${s.health.status} (${s.health.problems.length} проблем)`,
          ...s.totals.map((t) => `  ${t.area}: ${t.cards} карточек, читали за час ${t.reads1h}, за сутки ${t.reads24h}`),
          "Читают:",
          ...s.reads.slice(0, 6).map((r) => `  ${r.name} ×${r.count} · ${ago(r.last)}`),
          `Новое: карточек ${s.changes.length}, заметок ${s.notes.length}`,
          ...s.changes.slice(0, 5).map((c) => `  [${c.kind}] ${c.name}`),
          s.error ? `Ошибки: ${s.error}` : "",
        ].join("\n"),
      };
    },
  });

  bb.background.service("loader", {
    async start(signal) {
      while (!signal.aborted) {
        try {
          await load();
        } catch (e) {
          bb.log.warn(`load failed: ${String(e)}`);
        }
        await new Promise<void>((r) => {
          const t = setTimeout(r, 60_000);
          signal.addEventListener("abort", () => (clearTimeout(t), r()), { once: true });
        });
      }
    },
  });
}
