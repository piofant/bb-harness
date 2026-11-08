// Which timeline rows open the file viewer. Shared by the content script
// (DOM rows) and the server (timeline rows).
//
// bb puts each row's id on its wrapper as `data-timeline-row-id`:
// - `thr_x:file-read:<callId>` — a file read (Claude Code's Read tool);
// - `thr_x:command:<callId>` — a shell command; bb titles the ones that only
//   read a file "Read <path>" and gives them no expand button (Codex reads
//   files this way);
// - delegation children prefix `thr_x:delegation:<callId>:child:` per level;
// - bundle headers ("Explored 7 files") embed their first child's id after
//   `:work-summary:`, so they must not match: clicking them expands the bundle.

export const ROW_ID_ATTRIBUTE = "data-timeline-row-id";
export const MAX_ROW_ID_LENGTH = 1024;

export type ReadRowKind = "file-read" | "command";

export interface ReadRowClick {
  rowId: string;
  kind: ReadRowKind;
}

const READ_ROW_ID =
  /^(thr_[A-Za-z0-9]+):(?:delegation:[^:\s]+:child:thr_[A-Za-z0-9]+:)*(file-read|command):([^:\s]+)$/;

/**
 * The outer thread (whose timeline holds the row), the row kind and the
 * provider item id, or null for any other row.
 */
export function parseReadRowId(
  rowId: string,
): { threadId: string; kind: ReadRowKind; callId: string } | null {
  if (rowId.length > MAX_ROW_ID_LENGTH) return null;
  const match = READ_ROW_ID.exec(rowId);
  if (match === null) return null;
  return { threadId: match[1]!, kind: match[2] as ReadRowKind, callId: match[3]! };
}

const INTERACTIVE_SELECTOR =
  "button, a[href], input, textarea, select, summary, [role='button'], [contenteditable='true']";
const READ_TITLE_SELECTOR = '[title^="Read "]';

/** The read row a click landed on, or null when bb (or nothing) should handle it. */
export function readRowForClickTarget(
  target: EventTarget | null,
): ReadRowClick | null {
  if (!(target instanceof Element)) return null;
  const rowElement = target.closest(`[${ROW_ID_ATTRIBUTE}]`);
  if (rowElement === null) return null;
  const rowId = rowElement.getAttribute(ROW_ID_ATTRIBUTE) ?? "";
  const parsed = parseReadRowId(rowId);
  if (parsed === null) return null;
  const interactive = target.closest(INTERACTIVE_SELECTOR);
  if (interactive !== null && rowElement.contains(interactive)) return null;
  if (parsed.kind === "command") {
    // An expandable command has a header button; only bare "Read <path>" rows open.
    if (rowElement.querySelector("button") !== null) return null;
    if (rowElement.querySelector(READ_TITLE_SELECTOR) === null) return null;
  }
  return { rowId, kind: parsed.kind };
}

export function isReadRowClick(value: unknown): value is ReadRowClick {
  if (typeof value !== "object" || value === null) return false;
  const { rowId, kind } = value as Record<string, unknown>;
  if (typeof rowId !== "string" || (kind !== "file-read" && kind !== "command"))
    return false;
  return parseReadRowId(rowId)?.kind === kind;
}

/** Window event the content script sends and the viewer overlay receives. */
export function openReadEventName(pluginId: string): string {
  return `${pluginId}:open-read-row`;
}

const FILE_READ_ROW = `[${ROW_ID_ATTRIBUTE}*=":file-read:"]:not([${ROW_ID_ATTRIBUTE}*="-summary:"])`;
const COMMAND_READ_ROW = `[${ROW_ID_ATTRIBUTE}*=":command:"]:not([${ROW_ID_ATTRIBUTE}*="-summary:"]):not(:has(button)):has(${READ_TITLE_SELECTOR})`;

/** Marks read rows as clickable; injected by the content script. */
export const READ_ROW_STYLE = `
${FILE_READ_ROW},
${COMMAND_READ_ROW} {
  cursor: pointer;
}
${FILE_READ_ROW}:hover ${READ_TITLE_SELECTOR},
${COMMAND_READ_ROW}:hover ${READ_TITLE_SELECTOR} {
  text-decoration: underline;
  text-underline-offset: 2px;
}
`;
