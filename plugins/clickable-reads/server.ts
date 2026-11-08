// bb-plugin-clickable-reads — backend entry.
//
// The content script in app.tsx turns a click on a "Read file …" timeline row
// into a `read_open` call with that row's id. The server looks the row up in
// the thread's own timeline and opens only the path recorded in that row,
// read on the host where the thread ran. Reads the user denied stay closed.
import { homedir } from "node:os";
import { posix as pathPosix } from "node:path";
import { defineRpcContract, type BbPluginApi } from "@get-bb/plugin-sdk";
import { z } from "zod";
import { hostContract, MAX_CLIPBOARD_PNG_BASE64 } from "./contract";
import { MAX_ROW_ID_LENGTH, parseReadRowId } from "./lib/read-rows";
import {
  describeReadError,
  isSizeLimitError,
  fileReadTarget,
  isHomeRelativeRead,
  fileViewFor,
  findCollapsedDelegation,
  findRowById,
  readTargetForRow,
  type ReadTarget,
  type TimelineRowLike,
} from "./lib/read-target";

// bb's host read already caps files by type (10 MB for raster images).
export const MAX_TEXT_CHARS = 400_000;
const SEGMENTS_PER_PAGE = 40;
const MAX_TIMELINE_PAGES = 25;
const MAX_DELEGATION_DEPTH = 4;
const MAX_DETAIL_PAGES = 20;
const EVENTS_PER_PAGE = 100; // bb's maximum page
const MIN_EVENTS_PER_PAGE = 10;
const MAX_EVENT_PAGES = 50;
const LOCATED_CACHE_SIZE = 500;

const fileFields = {
  name: z.string(),
  path: z.string(),
  hostId: z.string().nullable(),
  sizeBytes: z.number(),
};

export const readOpenResultSchema = z.discriminatedUnion("status", [
  z.object({
    status: z.literal("image"),
    ...fileFields,
    mimeType: z.string(),
    dataBase64: z.string(),
  }),
  z.object({
    status: z.literal("text"),
    ...fileFields,
    text: z.string(),
    truncated: z.boolean(),
  }),
  z.object({
    status: z.literal("other"),
    ...fileFields,
    mimeType: z.string().nullable(),
  }),
  // A command row that searched or listed rather than read a file.
  z.object({ status: z.literal("not-read") }),
  z.object({
    status: z.literal("error"),
    message: z.string(),
    name: z.string().nullable(),
    path: z.string().nullable(),
    hostId: z.string().nullable(),
    // bb refused the file only for its size, so its own panel is worth a try.
    canOpenInPanel: z.boolean(),
  }),
]);
export type ReadOpenResult = z.infer<typeof readOpenResultSchema>;

export const rpcContract = defineRpcContract({
  read_open: {
    input: z
      .object({ rowId: z.string().min(1).max(MAX_ROW_ID_LENGTH) })
      .strict(),
    output: readOpenResultSchema,
  },
  // bb desktop pages on a plain-http address have no Clipboard API; the
  // picture goes to the system clipboard of the connected Mac instead.
  copy_image_to_mac: {
    input: z
      .object({ pngBase64: z.string().min(1).max(MAX_CLIPBOARD_PNG_BASE64) })
      .strict(),
    output: z.discriminatedUnion("status", [
      z.object({ status: z.literal("copied") }),
      z.object({ status: z.literal("error"), message: z.string() }),
    ]),
  },
});

type Located =
  | { kind: "path"; path: string; hostId: string | null }
  | { kind: "error"; message: string }
  | { kind: "not-read" };

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** bb's "Event response exceeds the 8 MiB limit" answer. */
function isPayloadTooLarge(error: unknown): boolean {
  return (
    (error as { status?: unknown } | null)?.status === 413 ||
    /^HTTP 413\b/.test(errorMessage(error))
  );
}

export default function plugin(bb: BbPluginApi) {
  // Row id → resolved file. A row's path never changes, so repeat clicks skip
  // the timeline walk. Other answers are not cached: the row may still change.
  const located = new Map<string, Extract<Located, { kind: "path" }>>();

  function remember(rowId: string, value: Extract<Located, { kind: "path" }>): void {
    located.delete(rowId);
    located.set(rowId, value);
    if (located.size > LOCATED_CACHE_SIZE) {
      located.delete(located.keys().next().value!);
    }
  }

  /** Walks the timeline from the newest page back; recent rows are found first. */
  async function findTimelineRow(
    threadId: string,
    rowId: string,
  ): Promise<TimelineRowLike | null> {
    let cursor: { anchorId: string; anchorSeq: number } | null = null;
    for (let page = 0; page < MAX_TIMELINE_PAGES; page += 1) {
      const response = await bb.sdk.threads.timeline({
        threadId,
        includeNestedRows: "true",
        deferContent: "true",
        segmentLimit: String(SEGMENTS_PER_PAGE),
        ...(cursor === null
          ? {}
          : {
              beforeAnchorId: cursor.anchorId,
              beforeAnchorSeq: String(cursor.anchorSeq),
            }),
      });
      const row = await findInRows(
        threadId,
        response.rows as readonly TimelineRowLike[],
        rowId,
      );
      if (row !== null) return row;
      cursor = response.timelinePage.olderCursor;
      if (cursor === null) return null;
    }
    return null;
  }

  /** Searches rows, loading the child rows bb omits for finished subagents. */
  async function findInRows(
    threadId: string,
    rows: readonly TimelineRowLike[],
    rowId: string,
  ): Promise<TimelineRowLike | null> {
    let scope = rows;
    for (let depth = 0; ; depth += 1) {
      const row = findRowById(scope, rowId);
      if (row !== null) return row;
      if (depth === MAX_DELEGATION_DEPTH) return null;
      const delegation = findCollapsedDelegation(scope, rowId);
      if (delegation === null) return null;
      const loaded = await loadDelegationRows(threadId, delegation, rowId);
      if (loaded === null) return null;
      scope = loaded;
    }
  }

  /**
   * A finished subagent's rows, newest page first, stopping at the page that
   * holds the wanted row (or one of its collapsed ancestors).
   */
  async function loadDelegationRows(
    threadId: string,
    delegation: TimelineRowLike,
    rowId: string,
  ): Promise<readonly TimelineRowLike[] | null> {
    if (
      delegation.callId === undefined ||
      delegation.turnId == null ||
      delegation.sourceSeqStart === undefined ||
      delegation.sourceSeqEnd === undefined
    ) {
      return null;
    }
    let beforeCursor: string | undefined;
    for (let page = 0; page < MAX_DETAIL_PAGES; page += 1) {
      const details = await bb.sdk.threads.timelineTurnSummaryDetails({
        threadId,
        turnId: delegation.turnId,
        sourceSeqStart: String(delegation.sourceSeqStart),
        sourceSeqEnd: String(delegation.sourceSeqEnd),
        itemId: delegation.callId,
        deferContent: "true",
        ...(beforeCursor === undefined ? {} : { beforeCursor }),
      });
      const rows = details.rows as readonly TimelineRowLike[];
      if (findRowById(rows, rowId) !== null || findCollapsedDelegation(rows, rowId) !== null) {
        return rows;
      }
      if (!details.olderCursor) return null;
      beforeCursor = details.olderCursor;
    }
    return null;
  }

  /**
   * The path of a file tool read, straight from the thread's stored events.
   * Much cheaper than building timeline pages, so old reads open quickly.
   */
  async function findFileReadItem(
    threadId: string,
    callId: string,
  ): Promise<{ path: string | null; status: string } | undefined> {
    let beforeSeq: number | undefined;
    let pageSize = EVENTS_PER_PAGE;
    for (let page = 0; page < MAX_EVENT_PAGES; page += 1) {
      let events: Awaited<ReturnType<typeof bb.sdk.threads.events.list>>;
      try {
        events = await bb.sdk.threads.events.list({
          threadId,
          types: ["item/started", "item/completed"],
          order: "desc",
          limit: String(pageSize),
          ...(beforeSeq === undefined ? {} : { beforeSeq: String(beforeSeq) }),
        });
      } catch (error) {
        // Pages carry full command outputs; bb answers 413 when one is too big.
        if (!isPayloadTooLarge(error) || pageSize <= MIN_EVENTS_PER_PAGE) throw error;
        pageSize = Math.max(MIN_EVENTS_PER_PAGE, Math.floor(pageSize / 2));
        page -= 1;
        continue;
      }
      // Newest first: the first event for the item tells whether it finished.
      for (const event of events) {
        const item = (
          event.data as { item?: { type?: unknown; id?: unknown; path?: unknown; status?: unknown } }
        ).item;
        if (item?.type === "fileRead" && item.id === callId) {
          return {
            path: typeof item.path === "string" ? item.path : null,
            status:
              event.type === "item/completed" && typeof item.status === "string"
                ? item.status
                : "pending",
          };
        }
      }
      const oldest = events[events.length - 1];
      if (events.length < pageSize || oldest === undefined) return undefined;
      beforeSeq = oldest.seq;
      pageSize = Math.min(EVENTS_PER_PAGE, pageSize * 2);
    }
    return undefined;
  }

  // Cached once known; a failed or empty answer is asked again next time.
  let primaryHostId: string | null = null;

  /** The server's own home folder, when the thread ran on the server itself. */
  async function serverHomeFor(hostId: string | null): Promise<string | null> {
    if (hostId === null) return homedir();
    if (primaryHostId === null) {
      try {
        primaryHostId = (await bb.sdk.system.config()).primaryHostId;
      } catch (error) {
        bb.log.warn(`read_open could not read bb's config: ${errorMessage(error)}`);
        return null;
      }
    }
    return primaryHostId === hostId ? homedir() : null;
  }

  /** The machine the thread's environment lives on; null means the server's own. */
  async function hostForThread(threadId: string): Promise<string | null> {
    const thread = await bb.sdk.threads.get({ threadId });
    if (thread.environmentId === null) return null;
    const environment = await bb.sdk.environments.get({
      environmentId: thread.environmentId,
    });
    return environment.hostId;
  }

  async function locate(
    threadId: string,
    rowId: string,
    kind: "file-read" | "command",
    callId: string,
  ): Promise<Located> {
    const cached = located.get(rowId);
    if (cached !== undefined) return cached;
    let hostId: string | null | undefined;
    const threadHost = async () => {
      if (hostId === undefined) hostId = await hostForThread(threadId);
      return hostId;
    };
    let target: ReadTarget | null = null;
    if (kind === "file-read") {
      try {
        // Only a finished read takes the fast path; any other state is
        // settled by bb's own timeline row (interrupted turns, late events).
        const item = await findFileReadItem(threadId, callId);
        if (item?.status === "completed") target = fileReadTarget(item.path, item.status);
      } catch (error) {
        bb.log.warn(`read_open events lookup failed, using the timeline: ${errorMessage(error)}`);
      }
    }
    if (target === null) {
      // Shell reads need bb's parsed command; only the timeline has it.
      const row = await findTimelineRow(threadId, rowId);
      if (row === null) {
        return { kind: "error", message: "Не нашёл эту строку в ленте треда." };
      }
      const hostHome = isHomeRelativeRead(row) ? await serverHomeFor(await threadHost()) : null;
      target = readTargetForRow(row, hostHome);
    }
    if (target.kind !== "path") return target;
    const result = {
      kind: "path" as const,
      path: target.path,
      hostId: await threadHost(),
    };
    remember(rowId, result);
    return result;
  }

  async function openRow(rowId: string): Promise<ReadOpenResult> {
    const startedAt = Date.now();
    const parsed = parseReadRowId(rowId);
    const finish = (result: ReadOpenResult): ReadOpenResult => {
      const bytes = "sizeBytes" in result ? result.sizeBytes : 0;
      const line = `read_open kind=${parsed?.kind ?? "unknown"} status=${result.status} bytes=${bytes} ms=${Date.now() - startedAt}`;
      if (result.status === "error") bb.log.warn(`${line} error=${result.message}`);
      else bb.log.info(line);
      return result;
    };
    if (parsed === null) {
      return finish({
        status: "error",
        message: "Это не строка чтения файла.",
        name: null,
        path: null,
        hostId: null,
        canOpenInPanel: false,
      });
    }

    let target: Located;
    try {
      target = await locate(parsed.threadId, rowId, parsed.kind, parsed.callId);
    } catch (error) {
      return finish({
        status: "error",
        message: `Не удалось прочитать ленту треда: ${errorMessage(error)}`,
        name: null,
        path: null,
        hostId: null,
        canOpenInPanel: false,
      });
    }
    if (target.kind === "not-read") return finish({ status: "not-read" });
    if (target.kind === "error") {
      return finish({
        status: "error",
        message: target.message,
        name: null,
        path: null,
        hostId: null,
        canOpenInPanel: false,
      });
    }

    const name = pathPosix.basename(target.path) || target.path;
    let file: Awaited<ReturnType<typeof bb.sdk.files.read>>;
    try {
      file = await bb.sdk.files.read({
        path: target.path,
        ...(target.hostId === null ? {} : { hostId: target.hostId }),
      });
    } catch (error) {
      return finish({
        status: "error",
        message: describeReadError(error),
        name,
        path: target.path,
        hostId: target.hostId,
        canOpenInPanel: target.hostId !== null && isSizeLimitError(error),
      });
    }

    const common = {
      name,
      path: target.path,
      hostId: target.hostId,
      sizeBytes: file.sizeBytes,
    };
    const view = fileViewFor(target.path, file.mimeType, file.contentEncoding);
    if (view.kind === "image") {
      const dataBase64 =
        file.contentEncoding === "base64"
          ? file.content
          : Buffer.from(file.content, "utf8").toString("base64");
      return finish({
        status: "image",
        ...common,
        mimeType: view.mimeType,
        dataBase64,
      });
    }
    if (view.kind === "text") {
      const truncated = file.content.length > MAX_TEXT_CHARS;
      return finish({
        status: "text",
        ...common,
        text: truncated ? file.content.slice(0, MAX_TEXT_CHARS) : file.content,
        truncated,
      });
    }
    return finish({ status: "other", ...common, mimeType: view.mimeType });
  }

  const host = bb.hosts.experimental_client({ contract: hostContract });
  // Host id → platform (never changes); failed lookups are retried after a minute.
  const hostPlatforms = new Map<string, string>();
  const describeFailedAt = new Map<string, number>();
  // One clipboard write at a time per Mac, so pictures land in click order.
  const writeChains = new Map<string, Promise<unknown>>();

  async function describeHost(hostId: string, name: string) {
    const failedAt = describeFailedAt.get(hostId);
    if (failedAt !== undefined && Date.now() - failedAt < 60_000) return null;
    try {
      const info = await host.call("describe", null, { hostId, timeoutMs: 3_000 });
      hostPlatforms.set(hostId, info.platform);
      describeFailedAt.delete(hostId);
      return info;
    } catch (error) {
      describeFailedAt.set(hostId, Date.now());
      bb.log.warn(`copy_image_to_mac could not ask ${name} about itself: ${errorMessage(error)}`);
      return null;
    }
  }

  /** The one Mac to copy to: the only connected one, else the one running bb desktop. */
  async function pickMac(): Promise<{ hostId: string } | { message: string }> {
    const connected = (await bb.sdk.hosts.list()).filter((machine) => machine.status === "connected");
    const platforms = await Promise.all(
      connected.map(async (machine) => ({
        machine,
        platform: hostPlatforms.get(machine.id) ?? (await describeHost(machine.id, machine.name))?.platform,
      })),
    );
    const macs = platforms.filter((entry) => entry.platform === "darwin").map((entry) => entry.machine);
    if (macs.length === 0) {
      return { message: "Не нашёл подключённый к bb Мак, куда положить картинку." };
    }
    if (macs.length === 1) return { hostId: macs[0]!.id };
    const running = (
      await Promise.all(
        macs.map(async (machine) => ({ machine, info: await describeHost(machine.id, machine.name) })),
      )
    ).filter((entry) => entry.info?.desktopRunning === true);
    if (running.length === 1) return { hostId: running[0]!.machine.id };
    return {
      message: "К bb подключено несколько Маков с открытым bb — не знаю, на какой положить картинку.",
    };
  }

  function serializedWrite(hostId: string, pngBase64: string): Promise<unknown> {
    const previous = writeChains.get(hostId) ?? Promise.resolve();
    const next = previous.then(() => host.call("writeClipboardImage", { pngBase64 }, { hostId }));
    writeChains.set(hostId, next.catch(() => undefined));
    return next;
  }

  async function copyImageToMac(pngBase64: string, callerKind: string) {
    const startedAt = Date.now();
    const bytes = Math.floor((pngBase64.length * 3) / 4);
    const target = await pickMac();
    if ("message" in target) {
      bb.log.warn(`copy_image_to_mac caller=${callerKind} status=error error=${target.message}`);
      return { status: "error" as const, message: target.message };
    }
    try {
      await serializedWrite(target.hostId, pngBase64);
    } catch (error) {
      const reason = errorMessage(error);
      bb.log.warn(`copy_image_to_mac caller=${callerKind} host=${target.hostId} bytes=${bytes} ms=${Date.now() - startedAt} error=${reason}`);
      return {
        status: "error" as const,
        message: `Не получилось положить картинку в буфер Мака: ${reason}`,
      };
    }
    bb.log.info(`copy_image_to_mac caller=${callerKind} host=${target.hostId} bytes=${bytes} ms=${Date.now() - startedAt}`);
    return { status: "copied" as const };
  }

  bb.rpc.register(rpcContract, {
    read_open: ({ rowId }) => openRow(rowId),
    copy_image_to_mac: async ({ pngBase64 }, context) => {
      // The viewer runs in bb's own page; other plugins have no reason to write here.
      if (context.experimental_caller.kind === "plugin") {
        return {
          status: "error" as const,
          message: "Класть картинки в буфер Мака может только интерфейс bb.",
        };
      }
      return copyImageToMac(pngBase64, context.experimental_caller.kind);
    },
  });
}
