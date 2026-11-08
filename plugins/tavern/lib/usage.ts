// Какие скиллы звали в сессиях bb и когда. Чистая логика без SDK:
// разбор одного события ленты и раскладка скиллов на «работают» и «отдыхают».
//
// Скилл в ленте виден двумя способами:
//   - Claude Code зовёт инструмент Skill: { tool: "Skill", arguments: { skill } };
//   - Codex, Gemini и Pi просто читают SKILL.md — строкой fileRead или
//     командой вроде `sed -n 1,200p ~/.agents/skills/<имя>/SKILL.md`.

export interface SkillUse {
  /** Время последнего вызова, мс. */
  last: number;
  /** Сколько раз звали за окно истории. */
  count: number;
  /** Тред последнего вызова — на него ведёт кнопка в карточке. */
  threadId: string;
  /** Вызовы по московским дням, «2026-11-04» → 3. Для табло телевизора. */
  days?: Record<string, number>;
}

export type UsageMap = Record<string, SkillUse>;

/** Путь к SKILL.md внутри каталога скиллов: `…/skills/<имя>/SKILL.md`. */
const SKILL_PATH = /\/skills\/([a-z0-9][a-z0-9._-]{0,63})\/SKILL\.md\b/gi;

/**
 * Каталоги, чтение из которых точно означает «агент взял скилл в работу».
 * Остальные SKILL.md (чужой репозиторий, который изучает skill-scout,
 * черновик в песочнице) засчитываем, только если такой скилл установлен.
 */
const SKILL_HOMES = /\/\.(agents|claude|codex|gemini|pi|bb)\/(?:[^/\s'"]+\/)*skills\//;

/** Имя скилла без префикса плагина: `bb-global-skills:automations` → `automations`. */
export function normalizeSkill(raw: string): string | null {
  const name = raw.trim().split(":").pop()?.trim().replace(/^\//, "").toLowerCase() ?? "";
  return /^[a-z0-9][a-z0-9._-]{0,63}$/.test(name) ? name : null;
}

function namesFromText(text: string, installed: ReadonlySet<string>): string[] {
  const out = new Set<string>();
  for (const match of text.matchAll(SKILL_PATH)) {
    const name = normalizeSkill(match[1] ?? "");
    if (name === null) continue;
    const start = match.index ?? 0;
    // Каталог скилла — то, что стоит перед `/skills/<имя>/`.
    const prefix = text.slice(Math.max(0, start - 200), start + "/skills/".length);
    if (SKILL_HOMES.test(prefix) || installed.has(name)) out.add(name);
  }
  return [...out];
}

/**
 * Скиллы, которые использует один элемент ленты (`item` из события
 * `item/completed`). Неизвестные формы молча пропускаем: лента принадлежит
 * провайдеру, и новый тип элемента не должен ронять таверну.
 */
export function skillsFromItem(item: unknown, installed: ReadonlySet<string> = new Set()): string[] {
  if (item === null || typeof item !== "object") return [];
  const it = item as Record<string, unknown>;
  if (it.type === "toolCall" && it.tool === "Skill") {
    const args = it.arguments as Record<string, unknown> | undefined;
    const name = typeof args?.skill === "string" ? normalizeSkill(args.skill) : null;
    return name === null ? [] : [name];
  }
  if (it.type === "fileRead" && typeof it.path === "string") {
    return namesFromText(it.path, installed);
  }
  if (it.type === "commandExecution") {
    const command = typeof it.command === "string" ? it.command : JSON.stringify(it.command ?? "");
    // Каждый кусок конвейера отдельно: засчитываем путь, только если его
    // читает сам кусок (`cat`, `sed -n`, `head`…) и пишет не в файл. Запись в
    // SKILL.md — работа над скиллом, а `grep …/SKILL.md | head` — поиск, не вызов.
    const out = new Set<string>();
    for (const segment of command.split(/\|\||&&|[;|\n]/)) {
      if (!/^\s*(sudo\s+)?(cat|sed\s+-n|head|tail|less|bat|nl)\b/.test(segment)) continue;
      if (/(^|[^0-9&])>/.test(segment) || /\btee\b|sed\s+-i/.test(segment)) continue;
      for (const name of namesFromText(segment, installed)) out.add(name);
    }
    return [...out];
  }
  return [];
}

/** Московская дата «ГГГГ-ММ-ДД»: Москва круглый год в UTC+3. */
export function moscowDay(ms: number): string {
  return new Date(ms + 3 * 3_600_000).toISOString().slice(0, 10);
}

/** Последние `n` московских дат, начиная с сегодняшней. */
export function lastDays(now: number, n: number): string[] {
  return Array.from({ length: n }, (_, i) => moscowDay(now - i * 86_400_000));
}

/** Сколько раз скилл звали в эти дни. */
export function callsIn(use: SkillUse, days: readonly string[]): number {
  return days.reduce((sum, d) => sum + (use.days?.[d] ?? 0), 0);
}

/** Записать вызов; более старое событие не перетирает свежий тред. */
export function recordUse(map: UsageMap, name: string, at: number, threadId: string): void {
  const day = moscowDay(at);
  const prev = map[name];
  if (prev === undefined) {
    map[name] = { last: at, count: 1, threadId, days: { [day]: 1 } };
    return;
  }
  prev.count += 1;
  prev.days = { ...prev.days, [day]: (prev.days?.[day] ?? 0) + 1 };
  if (at >= prev.last) {
    prev.last = at;
    prev.threadId = threadId;
  }
}

/** Выкинуть вызовы старше окна истории, чтобы хранилище не росло вечно. */
export function pruneUsage(map: UsageMap, since: number): UsageMap {
  const out: UsageMap = {};
  const firstDay = moscowDay(since);
  for (const [name, use] of Object.entries(map)) {
    if (use.last < since) continue;
    const days = Object.fromEntries(Object.entries(use.days ?? {}).filter(([d]) => d >= firstDay));
    // Счётчик — сумма по оставшимся дням, иначе «за месяц» копил бы всю жизнь.
    const count = Object.values(days).reduce((sum, n) => sum + n, 0);
    out[name] = { ...use, days, count: use.days ? count : use.count };
  }
  return out;
}

export interface RankOptions {
  now: number;
  /** Сколько часов скилл считается «в работе» после вызова. */
  activeWindowHours: number;
  /** Минимум работающих: если в окне меньше, добираем самыми свежими. */
  minActive: number;
  /** Потолок работающих — больше на карту не помещается. */
  maxActive: number;
  /** Сколько отдыхающих показывать. */
  maxResting: number;
  /** Скиллы, которых на сцене нет (их рисуем отдельно или не рисуем вовсе). */
  exclude?: ReadonlySet<string>;
}

export interface Ranked {
  working: Array<{ name: string; use: SkillUse }>;
  resting: Array<{ name: string; use: SkillUse }>;
}

/**
 * Работают — все, кого звали за последние `activeWindowHours`, но не меньше
 * `minActive` (добираем самыми свежими) и не больше `maxActive`.
 * Отдыхают — следующие по свежести, до `maxResting`.
 */
export function rankSkills(map: UsageMap, opts: RankOptions): Ranked {
  const sorted = Object.entries(map)
    .filter(([name]) => !opts.exclude?.has(name))
    .map(([name, use]) => ({ name, use }))
    .sort((a, b) => b.use.last - a.use.last || a.name.localeCompare(b.name));
  const since = opts.now - opts.activeWindowHours * 3_600_000;
  const inWindow = sorted.filter((s) => s.use.last >= since).length;
  const workingCount = Math.min(opts.maxActive, Math.max(opts.minActive, inWindow), sorted.length);
  return {
    working: sorted.slice(0, workingCount),
    resting: sorted.slice(workingCount, workingCount + opts.maxResting),
  };
}
