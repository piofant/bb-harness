// Pure logic for the «Недавние» page: no I/O, no SDK imports, so both the
// server and the tests can use it.

/** The subset of bb's thread DTO the page needs. */
export interface RecentThread {
  id: string;
  title: string | null;
  titleFallback?: string | null;
  status: string;
  parentThreadId: string | null;
  archivedAt: number | null;
  visibility?: string;
  projectId: string | null;
  lastReadAt: number | null;
  latestAttentionAt: number;
  createdAt: number;
  updatedAt: number;
  hasPendingInteraction?: boolean;
  pinnedAt?: number | null;
  pinSortKey?: string | null;
}

export interface Area {
  key: string;
  /** Pill text: the area's `section`, else its `label`. */
  display: string;
  label: string | null;
  aliases: string[];
  projects: string[];
}

const DATED_TITLE = /^\d\d\.\d\d \| (.+) \| (.+)$/;

/** «дд.мм | тема | область» → «тема»; anything else unchanged. */
export function cleanTitle(title: string): string {
  const m = DATED_TITLE.exec(title);
  return m ? m[1]!.trim() : title;
}

/** The trailing area label of a dated title, or null. */
export function titleAreaLabel(title: string | null | undefined): string | null {
  if (!title) return null;
  const m = DATED_TITLE.exec(title);
  const label = m?.[2]?.trim();
  return label ? label : null;
}

export function displayTitle(t: Pick<RecentThread, "title" | "titleFallback">): string {
  const title = t.title?.trim();
  if (title) return title;
  const fallback = t.titleFallback?.trim();
  return fallback ? fallback : "Без названия";
}

/** bb's own read check: read when lastReadAt ≥ latestAttentionAt. */
export function isUnread(t: Pick<RecentThread, "lastReadAt" | "latestAttentionAt">): boolean {
  return (t.lastReadAt ?? 0) < t.latestAttentionAt;
}

/** Statuses where bb's sidebar shows the unread dot (agent is done). */
const SETTLED = new Set(["idle", "error"]);

function isListed(t: RecentThread): boolean {
  return t.parentThreadId === null && t.archivedAt === null && t.visibility !== "hidden";
}

/**
 * Unseen = bb's sidebar dot (root, unread, idle/error) or the thread is
 * waiting on the user (pending interaction).
 */
export function isUnseen(t: RecentThread): boolean {
  if (t.parentThreadId !== null || t.archivedAt !== null) return false;
  if (t.hasPendingInteraction === true) return true;
  return isUnread(t) && SETTLED.has(t.status);
}

/** Sort/display time: running threads are "now", others their last attention. */
export function activityAt(t: RecentThread, now: number): number {
  return SETTLED.has(t.status) ? t.latestAttentionAt : now;
}

export interface SplitResult<T> {
  unseen: T[];
  recent: T[];
  recentTotal: number;
  hasMore: boolean;
}

/** Unseen threads older than this stay in «Недавние» (with their dot) instead of the top block. */
export const UNSEEN_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;

export function split<T extends RecentThread>(
  threads: readonly T[],
  { now, limit, offset }: { now: number; limit: number; offset: number },
): SplitResult<T> {
  const unseen: T[] = [];
  const rest: T[] = [];
  for (const t of threads) {
    if (!isListed(t)) continue;
    (isUnseen(t) && now - t.latestAttentionAt <= UNSEEN_WINDOW_MS ? unseen : rest).push(t);
  }
  unseen.sort((a, b) => b.latestAttentionAt - a.latestAttentionAt || a.id.localeCompare(b.id));
  rest.sort((a, b) => activityAt(b, now) - activityAt(a, now) || b.createdAt - a.createdAt || a.id.localeCompare(b.id));
  const start = Math.max(0, Math.floor(offset) || 0);
  const count = Math.max(0, Math.floor(limit) || 0);
  const recent = rest.slice(start, start + count);
  return { unseen, recent, recentTotal: rest.length, hasMore: start + count < rest.length };
}

/** Thread change kinds that can move a row, its title or its dot. */
const RELEVANT_CHANGES = new Set([
  "archived-changed",
  "interactions-changed",
  "parent-changed",
  "read-state-changed",
  "status-changed",
  "thread-created",
  "thread-deleted",
  "title-changed",
]);

/**
 * Whether a `thread:changed` realtime event should refetch the page. Streaming
 * (`events-appended`) fires every second on running threads and is ignored;
 * an event without a readable `changes` list refetches to stay safe.
 */
export function shouldRefetch(event: unknown): boolean {
  const changes = (event as { changes?: unknown } | null)?.changes;
  if (!Array.isArray(changes) || changes.length === 0) return true;
  return changes.some((c) => typeof c === "string" && RELEVANT_CHANGES.has(c));
}

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

export function relTime(at: number, now: number): string {
  const diff = now - at;
  if (diff < MIN) return "сейчас";
  if (diff < HOUR) return `${Math.floor(diff / MIN)} мин`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)} ч`;
  if (diff < 2 * DAY) return "вчера";
  return `${Math.floor(diff / DAY)} д`;
}

function parseJson(text: string | null | undefined): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() !== "" ? v.trim() : null;
}

function strings(v: unknown): string[] {
  return Array.isArray(v) ? v.flatMap((x) => (str(x) === null ? [] : [str(x)!])) : [];
}

/** Parse chat-names areas.json; anything malformed yields no areas. */
export function parseAreas(text: string | null | undefined): Area[] {
  const root = parseJson(text);
  if (root === null || typeof root !== "object" || Array.isArray(root)) return [];
  const list = (root as { areas?: unknown }).areas;
  if (!Array.isArray(list)) return [];
  const out: Area[] = [];
  for (const raw of list) {
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) continue;
    const r = raw as Record<string, unknown>;
    const key = str(r.key);
    const label = str(r.label);
    const display = str(r.section) ?? label;
    if (key === null || display === null) continue;
    out.push({ key, display, label, aliases: strings(r.aliases), projects: strings(r.projects) });
  }
  return out;
}

/** Parse area-cache.json ({thread_id: area_key}). */
export function parseAreaCache(text: string | null | undefined): Record<string, string> {
  const root = parseJson(text);
  if (root === null || typeof root !== "object" || Array.isArray(root)) return {};
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(root as Record<string, unknown>)) if (typeof v === "string") out[k] = v;
  return out;
}

const norm = (s: string) => s.trim().toLocaleLowerCase("ru");

/** Area pill for a thread: title label → project → cache; null if none. */
export function areaLabel(
  t: Pick<RecentThread, "id" | "title" | "projectId">,
  areas: readonly Area[],
  cache: Readonly<Record<string, string>>,
): string | null {
  if (areas.length === 0) return null;
  const fromTitle = titleAreaLabel(t.title);
  if (fromTitle !== null) {
    const want = norm(fromTitle);
    const hit = areas.find((a) => [a.key, a.label ?? "", ...a.aliases].some((n) => n !== "" && norm(n) === want));
    if (hit) return hit.display;
  }
  if (t.projectId) {
    const hit = areas.find((a) => a.projects.includes(t.projectId!));
    if (hit) return hit.display;
  }
  const cached = Object.prototype.hasOwnProperty.call(cache, t.id) ? cache[t.id] : undefined;
  if (cached) {
    const hit = areas.find((a) => a.key === cached);
    if (hit) return hit.display;
  }
  return null;
}

/** Pinned root threads (topic hubs) in bb's sidebar order — the only thing the minimal sidebar shows. */
export function pinned<T extends RecentThread>(threads: readonly T[]): T[] {
  return threads
    .filter((t) => t.pinnedAt && !t.archivedAt && !t.parentThreadId && t.visibility !== "hidden")
    .sort((a, b) => (a.pinSortKey ?? "").localeCompare(b.pinSortKey ?? "") || (a.pinnedAt ?? 0) - (b.pinnedAt ?? 0));
}
