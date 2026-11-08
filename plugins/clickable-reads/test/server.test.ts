import { homedir } from "node:os";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createFakePluginHost } from "@get-bb/plugin-sdk/testing";
import plugin, { MAX_TEXT_CHARS, type ReadOpenResult } from "../server";

const THREAD = "thr_abc123";
const READ_ROW = `${THREAD}:file-read:da1-i1643`;
const PNG_BASE64 = Buffer.from("fake-png-bytes").toString("base64");

interface Page {
  rows: unknown[];
  olderCursor: { anchorId: string; anchorSeq: number } | null;
}

function timelinePage({ rows, olderCursor }: Page) {
  return {
    rows,
    timelinePage: {
      historySnapshot: "{}",
      olderCursor,
      olderRowsSourceSeqEnd: null,
      returnedSegmentCount: 1,
    },
  };
}

type FileStub = {
  content: string;
  contentEncoding: "base64" | "utf8";
  mimeType?: string;
  sizeBytes: number;
};

let dispose: (() => Promise<void>) | null = null;
afterEach(async () => {
  await dispose?.();
  dispose = null;
});

async function setup({
  pages,
  file,
  readError,
  hostId = "host_vps",
  details,
  events = [],
  maxEventPage = 100,
}: {
  pages: Page[];
  file?: FileStub;
  readError?: Error;
  hostId?: string | null;
  details?: (args: { itemId?: string; beforeCursor?: string }) => { rows: unknown[]; olderCursor?: string | null };
  events?: unknown[];
  maxEventPage?: number;
}) {
  const { bb, harness } = createFakePluginHost({
    pluginId: "clickable-reads",
    sdk: {
      threads: {
        timeline: async (args: { beforeAnchorId?: string }) => {
          const index = args.beforeAnchorId === undefined ? 0 : Number(args.beforeAnchorId.slice(1));
          return timelinePage(pages[index] ?? { rows: [], olderCursor: null });
        },
        get: async () => ({ id: THREAD, environmentId: hostId === null ? null : "env_1" }),
        timelineTurnSummaryDetails: async (args: { itemId?: string; beforeCursor?: string }) => {
          const page = details?.(args) ?? { rows: [] };
          return { rows: page.rows, olderCursor: page.olderCursor ?? null };
        },
        events: {
          // bb rejects pages over 100 events with HTTP 400, and pages whose
          // stored data is too big with HTTP 413.
          list: async (args: { limit?: string }) => {
            if (Number(args.limit) > 100) throw new Error("HTTP 400: Thread event limit cannot exceed 100");
            if (Number(args.limit) > maxEventPage) throw new Error("HTTP 413: event_data_too_large");
            return events;
          },
        },
      },
      environments: {
        get: async (args: { environmentId: string }) => ({ id: args.environmentId, hostId }),
      },
      files: {
        read: async (args: { path: string }) => {
          if (readError !== undefined) throw readError;
          return { path: args.path, sha256: "x", ...(file ?? { content: "", contentEncoding: "utf8", sizeBytes: 0 }) };
        },
      },
    },
  });
  await plugin(bb);
  dispose = () => harness.lifecycle.dispose();
  const open = (rowId: string) =>
    harness.behavior.callRpc("read_open", { rowId }) as Promise<ReadOpenResult>;
  return { harness, open };
}

const readRow = (path: string, id = READ_ROW, status = "completed") => ({
  id,
  kind: "work",
  workKind: "file-read",
  status,
  path,
});

describe("read_open", () => {
  it("returns an image read from the thread's host", async () => {
    const { harness, open } = await setup({
      pages: [{ rows: [readRow("/home/me/shots/wb_e_example.png")], olderCursor: null }],
      file: { content: PNG_BASE64, contentEncoding: "base64", mimeType: "image/png", sizeBytes: 14 },
    });
    expect(await open(READ_ROW)).toEqual({
      status: "image",
      name: "wb_e_example.png",
      path: "/home/me/shots/wb_e_example.png",
      hostId: "host_vps",
      sizeBytes: 14,
      mimeType: "image/png",
      dataBase64: PNG_BASE64,
    });
    expect(harness.inspection.sdk.callsTo("files.read")[0]?.[0]).toMatchObject({
      path: "/home/me/shots/wb_e_example.png",
      hostId: "host_vps",
    });
    expect(harness.inspection.sdk.callsTo("threads.timeline")[0]?.[0]).toMatchObject({
      threadId: THREAD,
      includeNestedRows: "true",
      deferContent: "true",
      segmentLimit: "40",
    });
  });

  it("reads from the server machine when the thread has no host", async () => {
    const { harness, open } = await setup({
      pages: [{ rows: [readRow("/tmp/a.txt")], olderCursor: null }],
      file: { content: "hello", contentEncoding: "utf8", sizeBytes: 5 },
      hostId: null,
    });
    expect(await open(READ_ROW)).toMatchObject({ status: "text", text: "hello", hostId: null });
    expect(harness.inspection.sdk.callsTo("files.read")[0]?.[0]).not.toHaveProperty("hostId");
  });

  it("encodes a utf8 SVG as base64 for the image view", async () => {
    const svg = "<svg xmlns='http://www.w3.org/2000/svg'/>";
    const { open } = await setup({
      pages: [{ rows: [readRow("/a/logo.svg")], olderCursor: null }],
      file: { content: svg, contentEncoding: "utf8", sizeBytes: svg.length },
    });
    expect(await open(READ_ROW)).toMatchObject({
      status: "image",
      mimeType: "image/svg+xml",
      dataBase64: Buffer.from(svg).toString("base64"),
    });
  });

  it("truncates long text", async () => {
    const { open } = await setup({
      pages: [{ rows: [readRow("/a/big.log")], olderCursor: null }],
      file: { content: "x".repeat(MAX_TEXT_CHARS + 10), contentEncoding: "utf8", sizeBytes: MAX_TEXT_CHARS + 10 },
    });
    const result = await open(READ_ROW);
    expect(result.status).toBe("text");
    if (result.status !== "text") return;
    expect(result.truncated).toBe(true);
    expect(result.text).toHaveLength(MAX_TEXT_CHARS);
  });

  it("explains bb's size limit and keeps the panel option", async () => {
    const { open } = await setup({
      pages: [{ rows: [readRow("/a/huge.png")], olderCursor: null }],
      readError: new Error("File size 12000000 bytes exceeds the 10 MB limit"),
    });
    expect(await open(READ_ROW)).toEqual({
      status: "error",
      message: "Файл больше 10 МБ — bb не отдаёт такие для просмотра.",
      name: "huge.png",
      path: "/a/huge.png",
      hostId: "host_vps",
      canOpenInPanel: true,
    });
  });

  it("offers other binaries to bb's panel", async () => {
    const { open } = await setup({
      pages: [{ rows: [readRow("/a/doc.pdf")], olderCursor: null }],
      file: { content: PNG_BASE64, contentEncoding: "base64", mimeType: "application/pdf", sizeBytes: 14 },
    });
    expect(await open(READ_ROW)).toEqual({
      status: "other",
      name: "doc.pdf",
      path: "/a/doc.pdf",
      hostId: "host_vps",
      sizeBytes: 14,
      mimeType: "application/pdf",
    });
  });

  it("pages back through the timeline and caches the row", async () => {
    const { harness, open } = await setup({
      pages: [
        { rows: [{ id: `${THREAD}:user-seed:9`, kind: "conversation" }], olderCursor: { anchorId: "p1", anchorSeq: 5 } },
        { rows: [{ id: `${THREAD}:t1:turn:0`, kind: "turn", children: [readRow("/a/old.txt")] }], olderCursor: null },
      ],
      file: { content: "old", contentEncoding: "utf8", sizeBytes: 3 },
    });
    expect(await open(READ_ROW)).toMatchObject({ status: "text", path: "/a/old.txt" });
    const calls = harness.inspection.sdk.callsTo("threads.timeline");
    expect(calls).toHaveLength(2);
    expect(calls[1]?.[0]).toMatchObject({ beforeAnchorId: "p1", beforeAnchorSeq: "5" });
    await open(READ_ROW);
    expect(harness.inspection.sdk.callsTo("threads.timeline")).toHaveLength(2);
  });

  it("resolves a Codex read command against its folder", async () => {
    const row = `${THREAD}:command:call_7`;
    const { harness, open } = await setup({
      pages: [
        {
          rows: [
            {
              id: row,
              kind: "work",
              workKind: "command",
              status: "completed",
              cwd: "/Users/me/project",
              activityIntents: [{ type: "read", command: "sed -n 1,80p AGENTS.md", name: "sed", path: "AGENTS.md" }],
            },
          ],
          olderCursor: null,
        },
      ],
      file: { content: "# agents", contentEncoding: "utf8", sizeBytes: 8 },
    });
    expect(await open(row)).toMatchObject({ status: "text", path: "/Users/me/project/AGENTS.md" });
    expect(harness.inspection.sdk.callsTo("files.read")[0]?.[0]).toMatchObject({
      path: "/Users/me/project/AGENTS.md",
    });
  });

  it("answers not-read for a search command without reading files", async () => {
    const row = `${THREAD}:command:call_8`;
    const { harness, open } = await setup({
      pages: [
        {
          rows: [
            {
              id: row,
              kind: "work",
              workKind: "command",
              cwd: "/w",
              activityIntents: [{ type: "search", command: "rg x", query: "x", path: "/w" }],
            },
          ],
          olderCursor: null,
        },
      ],
    });
    expect(await open(row)).toEqual({ status: "not-read" });
    expect(harness.inspection.sdk.callsTo("files.read")).toHaveLength(0);
  });

  it("loads the rows of a finished subagent to find its read", async () => {
    const delegationId = `${THREAD}:delegation:da1-i530`;
    const childRow = `${delegationId}:child:${THREAD}:command:da1-i531`;
    const { harness, open } = await setup({
      pages: [
        {
          rows: [
            {
              id: delegationId,
              kind: "work",
              workKind: "delegation",
              callId: "da1-i530",
              turnId: "da1-t5",
              sourceSeqStart: 100,
              sourceSeqEnd: 180,
              childRows: null,
            },
          ],
          olderCursor: null,
        },
      ],
      details: (args) => ({
        rows:
        args.itemId === "da1-i530"
          ? [
              {
                id: delegationId,
                kind: "work",
                workKind: "delegation",
                callId: "da1-i530",
                childRows: [
                  {
                    id: childRow,
                    kind: "work",
                    workKind: "command",
                    status: "completed",
                    cwd: "/w",
                    approvalStatus: null,
                    activityIntents: [{ type: "read", path: "SKILL.md" }],
                  },
                ],
              },
            ]
          : [],
      }),
      file: { content: "# skill", contentEncoding: "utf8", sizeBytes: 7 },
    });
    expect(await open(childRow)).toMatchObject({ status: "text", path: "/w/SKILL.md" });
    expect(harness.inspection.sdk.callsTo("threads.timelineTurnSummaryDetails")[0]?.[0]).toMatchObject({
      threadId: THREAD,
      turnId: "da1-t5",
      sourceSeqStart: "100",
      sourceSeqEnd: "180",
      itemId: "da1-i530",
      deferContent: "true",
    });
  });

  it("does not open a read the user denied", async () => {
    const row = `${THREAD}:command:call_9`;
    const { harness, open } = await setup({
      pages: [
        {
          rows: [
            {
              id: row,
              kind: "work",
              workKind: "command",
              cwd: "/home/me",
              approvalStatus: "denied",
              activityIntents: [{ type: "read", path: "/home/me/.aws/credentials" }],
            },
          ],
          olderCursor: null,
        },
      ],
    });
    expect(await open(row)).toMatchObject({ status: "error", path: null });
    expect(harness.inspection.sdk.callsTo("files.read")).toHaveLength(0);
  });

  it("re-reads a row whose earlier answer was not a file path", async () => {
    const row = `${THREAD}:command:call_10`;
    let intents: unknown[] = [];
    const { bb, harness } = createFakePluginHost({
      pluginId: "clickable-reads",
      sdk: {
        threads: {
          timeline: async () =>
            timelinePage({
              rows: [{ id: row, kind: "work", workKind: "command", status: "completed", cwd: "/w", approvalStatus: null, activityIntents: intents }],
              olderCursor: null,
            }),
          get: async () => ({ id: THREAD, environmentId: null }),
          events: { list: async () => [] },
        },
        files: {
          read: async (args: { path: string }) => ({ path: args.path, sha256: "x", content: "ok", contentEncoding: "utf8", sizeBytes: 2 }),
        },
      },
    });
    await plugin(bb);
    dispose = () => harness.lifecycle.dispose();
    expect(await harness.behavior.callRpc("read_open", { rowId: row })).toEqual({ status: "not-read" });
    intents = [{ type: "read", path: "notes.md" }];
    expect(await harness.behavior.callRpc("read_open", { rowId: row })).toMatchObject({
      status: "text",
      path: "/w/notes.md",
    });
  });

  it("finds a Claude read in the thread's events without building the timeline", async () => {
    const { harness, open } = await setup({
      pages: [],
      events: [
        { seq: 900, type: "item/started", data: { item: { type: "commandExecution", id: "da1-i1642" } } },
        { seq: 950, type: "item/completed", data: { item: { type: "fileRead", id: "da1-i1643", path: "/w/old.png", status: "completed" } } },
        { seq: 899, type: "item/started", data: { item: { type: "fileRead", id: "da1-i1643", path: "/w/old.png", status: "pending" } } },
      ],
      file: { content: PNG_BASE64, contentEncoding: "base64", mimeType: "image/png", sizeBytes: 14 },
    });
    expect(await open(READ_ROW)).toMatchObject({ status: "image", path: "/w/old.png" });
    expect(harness.inspection.sdk.callsTo("threads.timeline")).toHaveLength(0);
    expect(harness.inspection.sdk.callsTo("threads.events.list")[0]?.[0]).toMatchObject({
      threadId: THREAD,
      types: ["item/started", "item/completed"],
      order: "desc",
    });
  });

  it("falls back to the timeline when the events lookup fails", async () => {
    const { bb, harness } = createFakePluginHost({
      pluginId: "clickable-reads",
      sdk: {
        threads: {
          events: { list: async () => { throw new Error("HTTP 500: boom"); } },
          timeline: async () => timelinePage({ rows: [readRow("/w/a.txt")], olderCursor: null }),
          get: async () => ({ id: THREAD, environmentId: null }),
        },
        files: {
          read: async (args: { path: string }) => ({ path: args.path, sha256: "x", content: "a", contentEncoding: "utf8", sizeBytes: 1 }),
        },
      },
    });
    await plugin(bb);
    dispose = () => harness.lifecycle.dispose();
    expect(await harness.behavior.callRpc("read_open", { rowId: READ_ROW })).toMatchObject({
      status: "text",
      path: "/w/a.txt",
    });
  });

  it("searches every page of a large finished subagent", async () => {
    const delegationId = `${THREAD}:delegation:da1-i530`;
    const childRow = `${delegationId}:child:${THREAD}:command:da1-i531`;
    const child = {
      id: childRow,
      kind: "work",
      workKind: "command",
      status: "completed",
      cwd: "/w",
      approvalStatus: null,
      activityIntents: [{ type: "read", path: "early.md" }],
    };
    const { harness, open } = await setup({
      pages: [
        {
          rows: [
            { id: delegationId, kind: "work", workKind: "delegation", callId: "da1-i530", turnId: "t5", sourceSeqStart: 1, sourceSeqEnd: 9, childRows: null },
          ],
          olderCursor: null,
        },
      ],
      details: (args) =>
        args.beforeCursor === undefined
          ? { rows: [{ id: delegationId, kind: "work", workKind: "delegation", callId: "da1-i530", childRows: [] }], olderCursor: "c2" }
          : { rows: [{ id: delegationId, kind: "work", workKind: "delegation", callId: "da1-i530", childRows: [child] }], olderCursor: null },
      file: { content: "early", contentEncoding: "utf8", sizeBytes: 5 },
    });
    expect(await open(childRow)).toMatchObject({ status: "text", path: "/w/early.md" });
    expect(harness.inspection.sdk.callsTo("threads.timelineTurnSummaryDetails")[1]?.[0]).toMatchObject({
      beforeCursor: "c2",
    });
  });

  it("asks the timeline when the events show a read that has not finished", async () => {
    const { harness, open } = await setup({
      pages: [{ rows: [readRow("/w/secret.txt", READ_ROW, "pending")], olderCursor: null }],
      events: [
        { seq: 899, type: "item/started", data: { item: { type: "fileRead", id: "da1-i1643", path: "/w/secret.txt", status: "pending" } } },
      ],
    });
    expect(await open(READ_ROW)).toMatchObject({
      status: "error",
      message: "Агент ещё не дочитал этот файл — открой чуть позже.",
    });
    expect(harness.inspection.sdk.callsTo("files.read")).toHaveLength(0);
  });

  it("does not open a read that failed or was declined", async () => {
    const { harness, open } = await setup({
      pages: [{ rows: [readRow("/w/secret.txt", READ_ROW, "error")], olderCursor: null }],
      events: [
        { seq: 950, type: "item/completed", data: { item: { type: "fileRead", id: "da1-i1643", path: "/w/secret.txt", status: "failed" } } },
      ],
    });
    expect(await open(READ_ROW)).toMatchObject({
      status: "error",
      message: "Агент не прочитал этот файл, поэтому он не открывается.",
    });
    expect(harness.inspection.sdk.callsTo("files.read")).toHaveLength(0);
  });

  it("follows bb's own status when the events disagree", async () => {
    const { open } = await setup({
      pages: [{ rows: [readRow("/w/a.txt", READ_ROW, "completed")], olderCursor: null }],
      events: [
        { seq: 950, type: "item/completed", data: { item: { type: "fileRead", id: "da1-i1643", path: "/w/a.txt", status: "pending" } } },
      ],
      file: { content: "a", contentEncoding: "utf8", sizeBytes: 1 },
    });
    expect(await open(READ_ROW)).toMatchObject({ status: "text", path: "/w/a.txt" });
  });

  it("does not shrink event pages for errors that only mention 413", async () => {
    const { bb, harness } = createFakePluginHost({
      pluginId: "clickable-reads",
      sdk: {
        threads: {
          events: {
            list: async () => {
              throw new Error("HTTP 500: failed at event #14130");
            },
          },
          timeline: async () => timelinePage({ rows: [readRow("/w/a.txt")], olderCursor: null }),
          get: async () => ({ id: THREAD, environmentId: null }),
        },
        files: {
          read: async (args: { path: string }) => ({ path: args.path, sha256: "x", content: "a", contentEncoding: "utf8", sizeBytes: 1 }),
        },
      },
    });
    await plugin(bb);
    dispose = () => harness.lifecycle.dispose();
    expect(await harness.behavior.callRpc("read_open", { rowId: READ_ROW })).toMatchObject({ status: "text" });
    expect(harness.inspection.sdk.callsTo("threads.events.list")).toHaveLength(1);
  });

  it("retries the events lookup with smaller pages when bb says a page is too big", async () => {
    const { harness, open } = await setup({
      pages: [],
      maxEventPage: 25,
      events: [
        { seq: 950, type: "item/completed", data: { item: { type: "fileRead", id: "da1-i1643", path: "/w/a.txt", status: "completed" } } },
      ],
      file: { content: "a", contentEncoding: "utf8", sizeBytes: 1 },
    });
    expect(await open(READ_ROW)).toMatchObject({ status: "text", path: "/w/a.txt" });
    expect(harness.inspection.sdk.callsTo("threads.timeline")).toHaveLength(0);
  });

  it("uses the server's home for ~ when the thread ran on the server itself", async () => {
    const row = `${THREAD}:command:call_home`;
    const { bb, harness } = createFakePluginHost({
      pluginId: "clickable-reads",
      sdk: {
        threads: {
          events: { list: async () => [] },
          timeline: async () =>
            timelinePage({
              rows: [{ id: row, kind: "work", workKind: "command", status: "completed", cwd: "/opt/work", approvalStatus: null, activityIntents: [{ type: "read", path: "~/notes.md" }] }],
              olderCursor: null,
            }),
          get: async () => ({ id: THREAD, environmentId: "env_1" }),
        },
        environments: { get: async () => ({ id: "env_1", hostId: "host_server" }) },
        system: { config: async () => ({ primaryHostId: "host_server" }) },
        files: {
          read: async (args: { path: string }) => ({ path: args.path, sha256: "x", content: "n", contentEncoding: "utf8", sizeBytes: 1 }),
        },
      },
    });
    await plugin(bb);
    dispose = () => harness.lifecycle.dispose();
    expect(await harness.behavior.callRpc("read_open", { rowId: row })).toMatchObject({
      status: "text",
      path: `${homedir()}/notes.md`,
    });
  });

  it("survives a failed config lookup and does not keep the failure", async () => {
    const row = `${THREAD}:command:call_home2`;
    let configCalls = 0;
    const { bb, harness } = createFakePluginHost({
      pluginId: "clickable-reads",
      sdk: {
        threads: {
          events: { list: async () => [] },
          timeline: async () =>
            timelinePage({
              rows: [{ id: row, kind: "work", workKind: "command", status: "completed", cwd: "", approvalStatus: null, activityIntents: [{ type: "read", path: "~/notes.md" }] }],
              olderCursor: null,
            }),
          get: async () => ({ id: THREAD, environmentId: "env_1" }),
        },
        environments: { get: async () => ({ id: "env_1", hostId: "host_server" }) },
        system: {
          config: async () => {
            configCalls += 1;
            if (configCalls === 1) throw new Error("ECONNRESET");
            return { primaryHostId: "host_server" };
          },
        },
        files: {
          read: async (args: { path: string }) => ({ path: args.path, sha256: "x", content: "n", contentEncoding: "utf8", sizeBytes: 1 }),
        },
      },
    });
    await plugin(bb);
    dispose = () => harness.lifecycle.dispose();
    expect(await harness.behavior.callRpc("read_open", { rowId: row })).toMatchObject({
      status: "error",
      message: "Путь «~/notes.md» начинается с домашней папки, а её на машине треда определить не удалось.",
    });
    expect(await harness.behavior.callRpc("read_open", { rowId: row })).toMatchObject({
      status: "text",
      path: `${homedir()}/notes.md`,
    });
    expect(harness.inspection.sdk.callsTo("threads.get")).toHaveLength(2);
  });

  it("explains a missing file", async () => {
    const { open } = await setup({
      pages: [{ rows: [readRow("/gone.png")], olderCursor: null }],
      readError: new Error("ENOENT: no such file or directory, open '/gone.png'"),
    });
    expect(await open(READ_ROW)).toEqual({
      status: "error",
      message: "Файла уже нет: его удалили или переместили.",
      name: "gone.png",
      path: "/gone.png",
      hostId: "host_vps",
      canOpenInPanel: false,
    });
  });

  it("explains a row that is not in the timeline", async () => {
    const { open } = await setup({ pages: [{ rows: [], olderCursor: null }] });
    expect(await open(READ_ROW)).toMatchObject({
      status: "error",
      message: "Не нашёл эту строку в ленте треда.",
    });
  });

  it("rejects ids of other rows without touching the timeline", async () => {
    const { harness, open } = await setup({ pages: [] });
    expect(await open(`${THREAD}:t1:work-summary:${READ_ROW}`)).toMatchObject({
      status: "error",
      message: "Это не строка чтения файла.",
    });
    expect(harness.inspection.sdk.callsTo("threads.timeline")).toHaveLength(0);
  });

  it("logs each open with its outcome", async () => {
    const { harness, open } = await setup({
      pages: [{ rows: [readRow("/a.txt")], olderCursor: null }],
      file: { content: "a", contentEncoding: "utf8", sizeBytes: 1 },
    });
    await open(READ_ROW);
    expect(
      harness.inspection.logEntries.some((entry) =>
        /read_open kind=file-read status=text bytes=1 ms=\d+/.test(entry.message),
      ),
    ).toBe(true);
  });
});

describe("copy_image_to_mac", () => {
  const PNG_BASE64 = Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    Buffer.from("png"),
  ]).toString("base64");

  type Machine = { platform: string; desktop?: boolean; off?: boolean };

  async function setupHosts(
    machines: Record<string, Machine>,
    write: (hostId: string) => unknown = () => ({ ok: true }),
  ) {
    const calls: { method: string; hostId: string }[] = [];
    const { bb, harness } = createFakePluginHost({
      pluginId: "clickable-reads",
      sdk: {
        hosts: {
          list: async () =>
            Object.entries(machines).map(([id, machine]) => ({
              id,
              name: id,
              status: machine.off ? "disconnected" : "connected",
            })),
        },
      },
      experimental_callHostRpc: async ({ method, hostId }: { method: string; hostId: string }) => {
        calls.push({ method, hostId });
        const machine = machines[hostId]!;
        if (method === "describe") {
          return { platform: machine.platform, desktopRunning: machine.desktop ?? false };
        }
        return write(hostId);
      },
    });
    await plugin(bb);
    dispose = () => harness.lifecycle.dispose();
    const copy = (caller?: { kind: "plugin"; pluginId: string }) =>
      harness.behavior.callRpc(
        "copy_image_to_mac",
        { pngBase64: PNG_BASE64 },
        caller === undefined ? undefined : { experimental_caller: caller },
      ) as Promise<{ status: "copied" } | { status: "error"; message: string }>;
    const writes = () => calls.filter((call) => call.method === "writeClipboardImage");
    return { calls, copy, writes };
  }

  it("puts the picture on the clipboard of the one connected Mac", async () => {
    const { calls, copy, writes } = await setupHosts({
      host_server: { platform: "linux" },
      host_mac: { platform: "darwin" },
      host_old_mac: { platform: "darwin", off: true },
    });
    expect(await copy()).toEqual({ status: "copied" });
    expect(writes()).toEqual([{ method: "writeClipboardImage", hostId: "host_mac" }]);
    await copy();
    // Each machine's platform is asked once.
    expect(calls.filter((call) => call.method === "describe")).toHaveLength(2);
  });

  it("picks the Mac where bb desktop runs when several are connected", async () => {
    const { copy, writes } = await setupHosts({
      host_mac_mini: { platform: "darwin" },
      host_macbook: { platform: "darwin", desktop: true },
    });
    expect(await copy()).toEqual({ status: "copied" });
    expect(writes()).toEqual([{ method: "writeClipboardImage", hostId: "host_macbook" }]);
  });

  it("refuses to guess between Macs that both run bb desktop", async () => {
    const { copy, writes } = await setupHosts({
      host_a: { platform: "darwin", desktop: true },
      host_b: { platform: "darwin", desktop: true },
    });
    expect(await copy()).toEqual({
      status: "error",
      message: "К bb подключено несколько Маков с открытым bb — не знаю, на какой положить картинку.",
    });
    expect(writes()).toHaveLength(0);
  });

  it("explains when no Mac is connected", async () => {
    const { copy } = await setupHosts({ host_server: { platform: "linux" } });
    expect(await copy()).toEqual({
      status: "error",
      message: "Не нашёл подключённый к bb Мак, куда положить картинку.",
    });
  });

  it("reports a failed write", async () => {
    const { copy } = await setupHosts({ host_mac: { platform: "darwin" } }, () => {
      throw new Error("osascript failed");
    });
    expect(await copy()).toEqual({
      status: "error",
      message: "Не получилось положить картинку в буфер Мака: osascript failed",
    });
  });

  it("does not let other plugins write to the clipboard", async () => {
    const { copy, writes } = await setupHosts({ host_mac: { platform: "darwin" } });
    expect(await copy({ kind: "plugin", pluginId: "other" })).toEqual({
      status: "error",
      message: "Класть картинки в буфер Мака может только интерфейс bb.",
    });
    expect(writes()).toHaveLength(0);
  });

  it("writes to one Mac one picture at a time, in order", async () => {
    const order: string[] = [];
    let releaseFirst!: () => void;
    let writeCount = 0;
    const { copy } = await setupHosts({ host_mac: { platform: "darwin" } }, async () => {
      writeCount += 1;
      const id = `write${writeCount}`;
      order.push(`${id}:start`);
      if (writeCount === 1) await new Promise<void>((resolve) => (releaseFirst = resolve));
      order.push(`${id}:end`);
      return { ok: true };
    });
    const first = copy();
    await vi.waitFor(() => expect(order).toEqual(["write1:start"]));
    const second = copy();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(order).toEqual(["write1:start"]);
    releaseFirst();
    await Promise.all([first, second]);
    expect(order).toEqual(["write1:start", "write1:end", "write2:start", "write2:end"]);
  });
});
