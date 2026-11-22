import { describe, expect, it } from "vitest";
import {
  activityAt,
  areaLabel,
  cleanTitle,
  displayTitle,
  isUnread,
  isUnseen,
  parseAreaCache,
  parseAreas,
  relTime,
  shouldRefetch,
  split,
  titleAreaLabel,
  type RecentThread,
  pinned,
} from "../lib/recent";

const NOW = Date.UTC(2026, 10, 22, 12, 0, 0);
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

function thread(over: Partial<RecentThread> = {}): RecentThread {
  return {
    id: "thr_a",
    title: "Some thread",
    titleFallback: null,
    status: "idle",
    parentThreadId: null,
    archivedAt: null,
    visibility: "visible",
    projectId: "proj_1",
    lastReadAt: NOW - HOUR,
    latestAttentionAt: NOW - 2 * HOUR,
    createdAt: NOW - DAY,
    updatedAt: NOW - HOUR,
    hasPendingInteraction: false,
    ...over,
  };
}

describe("cleanTitle", () => {
  it("strips the date prefix and the trailing area label", () => {
    expect(cleanTitle("21.11 | установка reels-kit | скиллы")).toBe("установка reels-kit");
  });
  it("keeps inner pipes in the middle part, drops only the last part", () => {
    expect(cleanTitle("07.11 | a | b | telegram")).toBe("a | b");
  });
  it("leaves titles that do not match the pattern untouched", () => {
    expect(cleanTitle("Проверка фолбека с Claude на Codex")).toBe("Проверка фолбека с Claude на Codex");
    expect(cleanTitle("21.11 | только два куска")).toBe("21.11 | только два куска");
    expect(cleanTitle("1.11 | x | y")).toBe("1.11 | x | y");
    expect(cleanTitle("a | b | c")).toBe("a | b | c");
  });
  it("handles empty input", () => {
    expect(cleanTitle("")).toBe("");
  });
});

describe("titleAreaLabel", () => {
  it("returns the last part of a dated title", () => {
    expect(titleAreaLabel("21.11 | установка reels-kit | скиллы")).toBe("скиллы");
    expect(titleAreaLabel("07.11 | a | b | Telegram ")).toBe("Telegram");
  });
  it("returns null for other titles", () => {
    expect(titleAreaLabel("Просто тред")).toBeNull();
    expect(titleAreaLabel(null)).toBeNull();
  });
});

describe("displayTitle", () => {
  it("prefers title, then titleFallback, then a placeholder", () => {
    expect(displayTitle(thread({ title: "T" }))).toBe("T");
    expect(displayTitle(thread({ title: null, titleFallback: "fb" }))).toBe("fb");
    expect(displayTitle(thread({ title: "  ", titleFallback: null }))).toBe("Без названия");
  });
});

describe("isUnread / isUnseen (mirrors bb's sidebar dot)", () => {
  it("is unread when lastReadAt is before latestAttentionAt", () => {
    expect(isUnread(thread({ lastReadAt: NOW - 3 * HOUR, latestAttentionAt: NOW - 2 * HOUR }))).toBe(true);
    expect(isUnread(thread({ lastReadAt: null }))).toBe(true);
    expect(isUnread(thread({ lastReadAt: NOW - 2 * HOUR, latestAttentionAt: NOW - 2 * HOUR }))).toBe(false);
  });
  it("is unseen only for idle or error root threads that are unread", () => {
    const unread = { lastReadAt: null };
    expect(isUnseen(thread({ ...unread, status: "idle" }))).toBe(true);
    expect(isUnseen(thread({ ...unread, status: "error" }))).toBe(true);
    expect(isUnseen(thread({ ...unread, status: "active" }))).toBe(false);
    expect(isUnseen(thread({ ...unread, status: "starting" }))).toBe(false);
    expect(isUnseen(thread({ ...unread, parentThreadId: "thr_p" }))).toBe(false);
    expect(isUnseen(thread({ ...unread, archivedAt: NOW }))).toBe(false);
    expect(isUnseen(thread({ status: "idle" }))).toBe(false);
  });
  it("counts a thread waiting on the user as unseen even if read", () => {
    expect(isUnseen(thread({ status: "active", hasPendingInteraction: true }))).toBe(true);
  });
});

describe("activityAt", () => {
  it("uses latestAttentionAt for settled threads and now for running ones", () => {
    expect(activityAt(thread(), NOW)).toBe(NOW - 2 * HOUR);
    expect(activityAt(thread({ status: "active" }), NOW)).toBe(NOW);
  });
});

describe("split", () => {
  const threads = [
    thread({ id: "read-old", latestAttentionAt: NOW - 5 * HOUR, lastReadAt: NOW }),
    thread({ id: "unseen-old", latestAttentionAt: NOW - 6 * HOUR, lastReadAt: null }),
    thread({ id: "unseen-new", latestAttentionAt: NOW - 1 * HOUR, lastReadAt: null }),
    thread({ id: "read-new", latestAttentionAt: NOW - 10 * MIN, lastReadAt: NOW }),
    thread({ id: "running", status: "active", latestAttentionAt: NOW - 9 * HOUR, lastReadAt: null }),
    thread({ id: "child", parentThreadId: "read-new", lastReadAt: null }),
    thread({ id: "archived", archivedAt: NOW - HOUR, lastReadAt: null }),
    thread({ id: "hidden", visibility: "hidden", lastReadAt: null }),
  ];

  it("keeps only unseen threads from the last 7 days on top; older ones fall into recent", () => {
    const stale = thread({ id: "unseen-stale", latestAttentionAt: NOW - 8 * 24 * HOUR, lastReadAt: null });
    const out = split([...threads, stale], { now: NOW, limit: 12, offset: 0 });
    expect(out.unseen.map((t) => t.id)).toEqual(["unseen-new", "unseen-old"]);
    expect(out.recent.map((t) => t.id)).toContain("unseen-stale");
  });

  it("puts unseen root threads newest first and the rest by activity", () => {
    const out = split(threads, { now: NOW, limit: 12, offset: 0 });
    expect(out.unseen.map((t) => t.id)).toEqual(["unseen-new", "unseen-old"]);
    expect(out.recent.map((t) => t.id)).toEqual(["running", "read-new", "read-old"]);
    expect(out.recentTotal).toBe(3);
    expect(out.hasMore).toBe(false);
  });

  it("pages the recent block", () => {
    const out = split(threads, { now: NOW, limit: 2, offset: 0 });
    expect(out.recent.map((t) => t.id)).toEqual(["running", "read-new"]);
    expect(out.hasMore).toBe(true);
    const next = split(threads, { now: NOW, limit: 2, offset: 2 });
    expect(next.recent.map((t) => t.id)).toEqual(["read-old"]);
    expect(next.hasMore).toBe(false);
  });

  it("handles an empty list and bad paging values", () => {
    expect(split([], { now: NOW, limit: 12, offset: 0 })).toEqual({ unseen: [], recent: [], recentTotal: 0, hasMore: false });
    const out = split(threads, { now: NOW, limit: -3, offset: -1 });
    expect(out.recent).toEqual([]);
  });
});

describe("relTime", () => {
  it("formats in Russian", () => {
    expect(relTime(NOW - 20_000, NOW)).toBe("сейчас");
    expect(relTime(NOW + 5 * MIN, NOW)).toBe("сейчас");
    expect(relTime(NOW - 7 * MIN, NOW)).toBe("7 мин");
    expect(relTime(NOW - 59 * MIN, NOW)).toBe("59 мин");
    expect(relTime(NOW - 60 * MIN, NOW)).toBe("1 ч");
    expect(relTime(NOW - 23 * HOUR - 59 * MIN, NOW)).toBe("23 ч");
    expect(relTime(NOW - 24 * HOUR, NOW)).toBe("вчера");
    expect(relTime(NOW - 47 * HOUR, NOW)).toBe("вчера");
    expect(relTime(NOW - 48 * HOUR, NOW)).toBe("2 д");
    expect(relTime(NOW - 40 * DAY, NOW)).toBe("40 д");
  });
});

const AREAS_JSON = JSON.stringify({
  areas: [
    { key: "work", label: "работа", section: "Работа", folder: "work", aliases: ["app", "Работа КП"], projects: ["proj_work"] },
    { key: "server", label: "сервер", section: "Сервер", folder: "server", aliases: ["bb", "Telegram", "скиллы"] },
    { key: "life", label: "личное", section: "Личное", folder: "life", aliases: [] },
  ],
});

describe("parseAreas", () => {
  it("parses well-formed areas", () => {
    const areas = parseAreas(AREAS_JSON);
    expect(areas.map((a) => a.key)).toEqual(["work", "server", "life"]);
    expect(areas[0]?.projects).toEqual(["proj_work"]);
    expect(areas[1]?.projects).toEqual([]);
  });
  it("returns no areas for missing or malformed input", () => {
    expect(parseAreas(null)).toEqual([]);
    expect(parseAreas("")).toEqual([]);
    expect(parseAreas("{not json")).toEqual([]);
    expect(parseAreas("[]")).toEqual([]);
    expect(parseAreas('{"areas": 5}')).toEqual([]);
    expect(parseAreas("null")).toEqual([]);
  });
  it("skips broken entries and tolerates missing fields", () => {
    const areas = parseAreas(
      JSON.stringify({
        areas: [
          null,
          7,
          { label: "без ключа", section: "X" },
          { key: "nosection" },
          { key: "onlylabel", label: "Только ярлык" },
          { key: "ok", section: "Ок", aliases: ["a", 3, null], projects: "proj_x" },
        ],
      }),
    );
    expect(areas.map((a) => [a.key, a.display])).toEqual([
      ["onlylabel", "Только ярлык"],
      ["ok", "Ок"],
    ]);
    expect(areas[1]?.aliases).toEqual(["a"]);
    expect(areas[1]?.projects).toEqual([]);
  });
});

describe("parseAreaCache", () => {
  it("keeps only string values", () => {
    expect(parseAreaCache('{"thr_1":"work","thr_2":5}')).toEqual({ thr_1: "work" });
    expect(parseAreaCache(null)).toEqual({});
    expect(parseAreaCache("[1,2]")).toEqual({});
    expect(parseAreaCache("oops")).toEqual({});
  });
});

describe("areaLabel", () => {
  const areas = parseAreas(AREAS_JSON);
  it("resolves the title label case-insensitively via key, label or alias", () => {
    expect(areaLabel(thread({ title: "21.11 | x | скиллы" }), areas, {})).toBe("Сервер");
    expect(areaLabel(thread({ title: "21.11 | x | WORK" }), areas, {})).toBe("Работа");
    expect(areaLabel(thread({ title: "21.11 | x | Работа" }), areas, {})).toBe("Работа");
    expect(areaLabel(thread({ title: "21.11 | x | работа кп" }), areas, {})).toBe("Работа");
    expect(areaLabel(thread({ title: "21.11 | x | ЛИЧНОЕ" }), areas, {})).toBe("Личное");
  });
  it("falls back to the project, then the cache", () => {
    expect(areaLabel(thread({ title: "Без даты", projectId: "proj_work" }), areas, {})).toBe("Работа");
    expect(areaLabel(thread({ title: "21.11 | x | неизвестно", projectId: "proj_work" }), areas, {})).toBe("Работа");
    expect(areaLabel(thread({ id: "thr_c", title: "Без даты", projectId: "p" }), areas, { thr_c: "life" })).toBe("Личное");
    expect(areaLabel(thread({ id: "thr_c", title: "Без даты", projectId: "p" }), areas, { thr_c: "gone" })).toBeNull();
  });
  it("title wins over project and cache", () => {
    expect(areaLabel(thread({ id: "thr_c", title: "21.11 | x | bb", projectId: "proj_work" }), areas, { thr_c: "life" })).toBe("Сервер");
  });
  it("gives no label without areas", () => {
    expect(areaLabel(thread({ title: "21.11 | x | скиллы" }), [], { thr_a: "server" })).toBeNull();
    expect(areaLabel(thread({ title: "Без даты", projectId: null }), areas, {})).toBeNull();
  });
});

describe("shouldRefetch", () => {
  it("refetches on changes that move rows or dots", () => {
    for (const c of ["status-changed", "read-state-changed", "title-changed", "thread-created", "thread-deleted", "archived-changed", "parent-changed", "interactions-changed"]) {
      expect(shouldRefetch({ changes: ["events-appended", c] })).toBe(true);
    }
  });
  it("ignores streaming and unrelated changes", () => {
    expect(shouldRefetch({ changes: ["events-appended"] })).toBe(false);
    expect(shouldRefetch({ changes: ["storage-changed", "tabs-changed", "terminals-changed", "queue-changed", "pin-state-changed"] })).toBe(false);
  });
  it("refetches when the event shape is unknown", () => {
    expect(shouldRefetch({})).toBe(true);
    expect(shouldRefetch(null)).toBe(true);
    expect(shouldRefetch({ changes: [] })).toBe(true);
  });
});

describe("pinned", () => {
  it("returns pinned root threads in sidebar order, skipping archived and children", () => {
    const list = [
      thread({ id: "b", pinnedAt: NOW - 2, pinSortKey: "b" }),
      thread({ id: "a", pinnedAt: NOW - 1, pinSortKey: "a" }),
      thread({ id: "plain" }),
      thread({ id: "gone", pinnedAt: NOW, archivedAt: NOW }),
      thread({ id: "kid", pinnedAt: NOW, parentThreadId: "a" }),
    ];
    expect(pinned(list).map((t) => t.id)).toEqual(["a", "b"]);
  });
});
