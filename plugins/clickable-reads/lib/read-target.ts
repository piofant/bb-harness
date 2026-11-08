// Server-side helpers: find a row in a thread timeline, work out which file
// it read, and decide how the viewer shows that file.
import path from "node:path";

interface ReadIntentLike {
  type: string;
  path?: string | null;
}

/** The subset of bb's TimelineRow the viewer reads. */
export interface TimelineRowLike {
  id: string;
  kind: string;
  workKind?: string;
  callId?: string;
  turnId?: string | null;
  sourceSeqStart?: number;
  sourceSeqEnd?: number;
  path?: string | null;
  cwd?: string | null;
  approvalStatus?: string | null;
  status?: string;
  activityIntents?: readonly ReadIntentLike[];
  children?: readonly TimelineRowLike[] | null;
  childRows?: readonly TimelineRowLike[] | null;
}

/** Depth-first search through turn children and delegation child rows. */
export function findRowById(
  rows: readonly TimelineRowLike[],
  rowId: string,
): TimelineRowLike | null {
  const stack = [...rows].reverse();
  while (stack.length > 0) {
    const row = stack.pop()!;
    if (row.id === rowId) return row;
    const nested = [...(row.children ?? []), ...(row.childRows ?? [])];
    for (let index = nested.length - 1; index >= 0; index -= 1) {
      stack.push(nested[index]!);
    }
  }
  return null;
}

/**
 * A finished subagent whose child rows bb left out of the page (deferred
 * content drops them) and that contains the wanted row.
 */
export function findCollapsedDelegation(
  rows: readonly TimelineRowLike[],
  rowId: string,
): TimelineRowLike | null {
  const stack = [...rows].reverse();
  while (stack.length > 0) {
    const row = stack.pop()!;
    if (
      row.workKind === "delegation" &&
      row.childRows == null &&
      rowId.startsWith(`${row.id}:child:`)
    ) {
      return row;
    }
    const nested = [...(row.children ?? []), ...(row.childRows ?? [])];
    for (let index = nested.length - 1; index >= 0; index -= 1) {
      stack.push(nested[index]!);
    }
  }
  return null;
}

export type ReadTarget =
  | { kind: "path"; path: string }
  | { kind: "error"; message: string }
  | { kind: "not-read" };

/**
 * Only finished reads open: a pending one may still be waiting for the
 * user's permission, and a failed one may have been declined.
 */
function unfinishedRead(status: string | undefined): ReadTarget | null {
  if (status === undefined || status === "completed") return null;
  if (status === "pending") {
    return { kind: "error", message: "Агент ещё не дочитал этот файл — открой чуть позже." };
  }
  return { kind: "error", message: "Агент не прочитал этот файл, поэтому он не открывается." };
}

function commandReadPaths(row: TimelineRowLike): string[] {
  return [
    ...new Set(
      (row.activityIntents ?? [])
        .filter((intent) => intent.type === "read")
        .map((intent) => intent.path?.trim() ?? "")
        .filter((candidate) => candidate.length > 0),
    ),
  ];
}

const HOME_PREFIX = /^(?:~|\$HOME|\$\{HOME\})(?=\/|$)/;

/** The home folder a path sits in: /Users/<name>, /home/<name> or /root. */
export function homeFromPath(folder: string | null): string | null {
  if (folder === null) return null;
  return (
    /^\/Users\/[^/]+(?=\/|$)/.exec(folder)?.[0] ??
    /^\/home\/[^/]+(?=\/|$)/.exec(folder)?.[0] ??
    /^\/root(?=\/|$)/.exec(folder)?.[0] ??
    null
  );
}

/** Whether this command read a path that starts with ~ or $HOME. */
export function isHomeRelativeRead(row: TimelineRowLike): boolean {
  const paths = row.workKind === "command" ? commandReadPaths(row) : [];
  return paths.length === 1 && HOME_PREFIX.test(paths[0]!);
}

export function readTargetForRow(row: TimelineRowLike, hostHome: string | null = null): ReadTarget {
  if (row.kind !== "work") return { kind: "not-read" };
  if (row.workKind === "file-read") return fileReadTarget(row.path ?? null, row.status);
  if (row.workKind === "command") {
    if (row.approvalStatus === "denied" || row.approvalStatus === "waiting_for_approval") {
      return { kind: "error", message: "Это чтение не было разрешено, файл не открываю." };
    }
    const paths = commandReadPaths(row);
    if (paths.length === 0) return { kind: "not-read" };
    const refusal = unfinishedRead(row.status);
    if (refusal !== null) return refusal;
    if (paths.length > 1) {
      return {
        kind: "error",
        message: `Команда прочитала несколько файлов: ${paths.join(", ")}.`,
      };
    }
    return resolveReadPath(paths[0]!, row.cwd ?? null, hostHome);
  }
  return { kind: "not-read" };
}

/** Target of a file tool read (bb's `file-read` row or `fileRead` item). */
export function fileReadTarget(
  filePath: string | null,
  status: string | undefined,
): ReadTarget {
  if (!filePath) {
    return { kind: "error", message: "bb не записал путь к этому файлу." };
  }
  return unfinishedRead(status) ?? literalReadPath(filePath);
}

/** A path the agent passed to a file tool verbatim; tools take absolute paths. */
function literalReadPath(value: string): ReadTarget {
  if (/^[A-Za-z]:[\\/]/.test(value)) return { kind: "path", path: value };
  if (value.startsWith("/")) {
    return { kind: "path", path: path.posix.normalize(value) };
  }
  return { kind: "error", message: `bb записал относительный путь «${value}».` };
}

/**
 * A path parsed out of a shell command, resolved against the command's
 * folder. ~ and $HOME use the host's known home, else the home folder the
 * command's folder sits in.
 */
export function resolveReadPath(
  raw: string,
  cwd: string | null,
  hostHome: string | null = null,
): ReadTarget {
  const value = raw.trim();
  const homePrefix = HOME_PREFIX.exec(value);
  if (homePrefix !== null) {
    const home = hostHome ?? homeFromPath(cwd);
    if (home === null) {
      return {
        kind: "error",
        message: `Путь «${value}» начинается с домашней папки, а её на машине треда определить не удалось.`,
      };
    }
    return { kind: "path", path: path.posix.join(home, value.slice(homePrefix[0].length)) };
  }
  if (value.startsWith("~")) {
    return {
      kind: "error",
      message: `Путь «${value}» ведёт в домашнюю папку другого пользователя, её не определить.`,
    };
  }
  if (/[$`]/.test(value)) {
    return {
      kind: "error",
      message: `Путь «${value}» зависит от переменной окружения, её значение неизвестно.`,
    };
  }
  if (/^[A-Za-z]:[\\/]/.test(value) || value.startsWith("/")) {
    return literalReadPath(value);
  }
  if (cwd !== null && cwd.startsWith("/")) {
    return { kind: "path", path: path.posix.resolve(cwd, value) };
  }
  return {
    kind: "error",
    message: `Путь «${value}» относительный, а папка, где выполнялась команда, неизвестна.`,
  };
}

const IMAGE_MIME_BY_EXTENSION: Readonly<Record<string, string>> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  bmp: "image/bmp",
  svg: "image/svg+xml",
  ico: "image/x-icon",
  avif: "image/avif",
};
const DISPLAYABLE_IMAGE_MIME = new Set(Object.values(IMAGE_MIME_BY_EXTENSION));

export type FileView =
  | { kind: "image"; mimeType: string }
  | { kind: "text" }
  | { kind: "other"; mimeType: string | null };

export function fileViewFor(
  filePath: string,
  mimeType: string | undefined,
  contentEncoding: "base64" | "utf8",
): FileView {
  const extension = path.posix.extname(filePath).slice(1).toLowerCase();
  const imageMime = IMAGE_MIME_BY_EXTENSION[extension];
  if (imageMime !== undefined) return { kind: "image", mimeType: imageMime };
  if (mimeType !== undefined && DISPLAYABLE_IMAGE_MIME.has(mimeType)) {
    return { kind: "image", mimeType };
  }
  if (contentEncoding === "utf8") return { kind: "text" };
  return { kind: "other", mimeType: mimeType ?? null };
}

/** bb refused the file for its size; bb's own panel may still show it. */
export function isSizeLimitError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /exceeds the \d+ MB limit/i.test(message);
}

/** A short, human message for a failed host file read (bb's own error texts). */
export function describeReadError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  // File errors first: their messages quote the path, which may contain any word.
  const limit = /exceeds the (\d+) MB limit/i.exec(message);
  if (limit !== null) {
    return `Файл больше ${limit[1]} МБ — bb не отдаёт такие для просмотра.`;
  }
  if (/ENOENT|path does not exist|no such file/i.test(message)) {
    return "Файла уже нет: его удалили или переместили.";
  }
  if (/is a directory|EISDIR/i.test(message)) return "Это папка, а не файл.";
  if (/EACCES|EPERM|permission denied/i.test(message)) {
    return "Нет прав на чтение этого файла.";
  }
  if (/^host not found/i.test(message)) {
    return "Машины, на которой работал тред, больше нет в bb.";
  }
  if (/\bhost\b.*\b(offline|suspended|not connected|disconnected|unreachable)\b/i.test(message)) {
    return "Машина, на которой работал агент, сейчас не в сети.";
  }
  return `Не удалось прочитать файл: ${message}`;
}
