// bb-plugin-recent — «Недавние»: a focus page with unseen and recent root
// threads. The server lists threads through bb's SDK, splits them with the
// pure helpers in lib/recent.ts, and resolves each row's area pill from the
// chat-names files on disk (read per call; any error → no pills).
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { defineRpcContract, type BbPluginApi } from "@get-bb/plugin-sdk";
import { z } from "zod";
import {
  isUnseen,
  activityAt,
  areaLabel,
  cleanTitle,
  displayTitle,
  parseAreaCache,
  parseAreas,
  pinned,
  split,
  type RecentThread,
} from "./lib/recent";

const AREAS_PATH = process.env.BB_AREAS_PATH ?? join(homedir(), ".agents/skills/chat-names/areas.json");
const AREA_CACHE_PATH = process.env.BB_AREA_CACHE_PATH ?? join(homedir(), ".local/state/chat-names/area-cache.json");
/** threads.list page size and a hard cap on pages (safety, ~4000 threads). */
const PAGE = 200;
const MAX_PAGES = 20;

const rowSchema = z.object({
  id: z.string(),
  title: z.string(),
  fullTitle: z.string(),
  area: z.string().nullable(),
  unseen: z.boolean(),
  at: z.number(),
});
export type RecentRow = z.infer<typeof rowSchema>;

const recentSchema = z.object({
  pinned: z.array(rowSchema),
  unseen: z.array(rowSchema),
  recent: z.array(rowSchema),
  recentTotal: z.number(),
  hasMore: z.boolean(),
});
export type RecentResult = z.infer<typeof recentSchema>;

export const rpcContract = defineRpcContract({
  recent: {
    input: z.object({
      limit: z.number().int().min(1).max(500),
      offset: z.number().int().min(0).max(100_000),
    }),
    output: recentSchema,
  },
});

async function readText(path: string): Promise<string | null> {
  try {
    return await readFile(path, "utf8");
  } catch {
    return null;
  }
}

export default async function plugin(bb: BbPluginApi) {
  async function listRootThreads(): Promise<RecentThread[]> {
    const all: RecentThread[] = [];
    for (let page = 0; page < MAX_PAGES; page++) {
      const batch = await bb.sdk.threads.list({ hasParent: false, limit: PAGE, offset: page * PAGE });
      all.push(...(batch as RecentThread[]));
      if (batch.length < PAGE) break;
    }
    return all;
  }

  async function recent(limit: number, offset: number): Promise<RecentResult> {
    const [threads, areasText, cacheText] = await Promise.all([
      listRootThreads(),
      readText(AREAS_PATH),
      readText(AREA_CACHE_PATH),
    ]);
    // Parsers never throw; malformed files just mean no pills.
    const areas = parseAreas(areasText);
    const cache = parseAreaCache(cacheText);
    const now = Date.now();
    const out = split(threads, { now, limit, offset });
    const row = (t: RecentThread, unseen: boolean): RecentRow => {
      const fullTitle = displayTitle(t);
      let area: string | null = null;
      try {
        area = areaLabel(t, areas, cache);
      } catch {
        area = null;
      }
      return { id: t.id, title: cleanTitle(fullTitle), fullTitle, area, unseen, at: activityAt(t, now) };
    };
    return {
      pinned: pinned(threads).map((t) => row(t, isUnseen(t))),
      unseen: out.unseen.map((t) => row(t, true)),
      recent: out.recent.map((t) => row(t, isUnseen(t))),
      recentTotal: out.recentTotal,
      hasMore: out.hasMore,
    };
  }

  bb.rpc.register(rpcContract, {
    recent: ({ limit, offset }) => recent(limit, offset),
  });
}
