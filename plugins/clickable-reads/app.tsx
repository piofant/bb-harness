// bb-plugin-clickable-reads — frontend entry.
//
// bb draws "Read file …" timeline rows as plain text that cannot be opened.
// The content script makes those rows clickable and announces the click; the
// app overlay asks the server for the file and shows it on top of the app:
// an image full-screen, text in bb's source viewer.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  definePluginApp,
  experimental_copyToClipboard,
  experimental_saveFile,
  experimental_SourceCode as SourceCode,
  experimental_usePluginId,
  useBbNavigate,
  useRpc,
} from "@get-bb/plugin-sdk/app";
import type { ReadOpenResult, rpcContract } from "./server";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import { usePortalScopeProps } from "@/lib/portal-scope";
import {
  type ImageMenuRequest,
  imageMenuEventName,
  isImageMenuRequest,
  mountImageMenu,
} from "@/lib/image-menu";
import { mountReadRowClicks } from "@/lib/read-row-clicks";
import { MAX_CLIPBOARD_PNG_BASE64 } from "@/lib/limits";
import { isReadRowClick, openReadEventName } from "@/lib/read-rows";
import { useNarrowScreen } from "@/lib/use-narrow-screen";

type OpenedFile = Exclude<ReadOpenResult, { status: "not-read" }>;
type ViewerState =
  | { phase: "loading" }
  | { phase: "ready"; file: OpenedFile };
type Notice = { tone: "ok" | "error"; text: string };

const NOTICE_MS = 4000;
const FOCUSABLE =
  "button:not([disabled]), [href], input, textarea, select, [contenteditable='true'], [tabindex]:not([tabindex='-1'])";

function base64ToBlob(base64: string, mimeType: string): Blob {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new Blob([bytes], { type: mimeType });
}

async function toPngBlob(image: Blob): Promise<Blob> {
  if (image.type === "image/png") return image;
  const url = URL.createObjectURL(image);
  try {
    const element = new Image();
    element.src = url;
    await element.decode();
    const canvas = document.createElement("canvas");
    canvas.width = element.naturalWidth || 1024;
    canvas.height = element.naturalHeight || 1024;
    canvas.getContext("2d")?.drawImage(element, 0, 0, canvas.width, canvas.height);
    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob(
        (png) => (png ? resolve(png) : reject(new Error("PNG encoding failed"))),
        "image/png",
      );
    });
  } finally {
    URL.revokeObjectURL(url);
  }
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result);
      resolve(dataUrl.slice(dataUrl.indexOf(",") + 1));
    };
    reader.onerror = () => reject(reader.error ?? new Error("the picture could not be read"));
    reader.readAsDataURL(blob);
  });
}

/** bb's macOS desktop app; its Mac can take the picture when the page cannot. */
function isBbDesktopMac(): boolean {
  const desktop = (window as { bbDesktop?: { platform?: unknown } }).bbDesktop;
  return typeof desktop === "object" && desktop !== null && desktop.platform === "macos";
}

type CopyViaMac = (pngBase64: string) => Promise<string | null>;

/** Copies the server's Mac route: null on success, else why it failed. */
function useCopyViaMac(): CopyViaMac {
  const rpc = useRpc<typeof rpcContract>();
  return useCallback(
    async (pngBase64) => {
      const result = await rpc.call("copy_image_to_mac", { pngBase64 });
      return result.status === "copied" ? null : result.message;
    },
    [rpc],
  );
}

/**
 * Puts a picture on the clipboard. The SDK clipboard helper writes text only,
 * so pictures use the Clipboard API, as bb's own message copy does. bb desktop
 * on a plain-http address has no Clipboard API; there the picture goes through
 * the connected Mac. Resolves null on success, else a reason.
 */
async function copyPicture(image: Blob, copyViaMac: CopyViaMac): Promise<string | null> {
  if (typeof ClipboardItem !== "undefined" && typeof navigator.clipboard?.write === "function") {
    try {
      await navigator.clipboard.write([new ClipboardItem({ "image/png": toPngBlob(image) })]);
      return null;
    } catch (error) {
      if (!isBbDesktopMac()) return error instanceof Error ? error.message : String(error);
    }
  }
  if (!isBbDesktopMac()) return "браузер не даёт доступ к буферу обмена";
  try {
    const pngBase64 = await blobToBase64(await toPngBlob(image));
    if (pngBase64.length > MAX_CLIPBOARD_PNG_BASE64) return "картинка слишком большая для буфера";
    return await copyViaMac(pngBase64);
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
}

function copyFailedText(reason: string): string {
  return `Не получилось скопировать картинку (${reason}) — сохрани её кнопкой «Скачать»`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

/**
 * Where the <img> loads from. A blob URL shares bb's origin, so opening it in
 * a tab would run an SVG's scripts as bb; a data: URL opened as a page gets an
 * opaque origin instead, so SVG goes through one.
 */
function useImageSource(file: OpenedFile | null): {
  src: string | null;
  blob: Blob | null;
} {
  const blob = useMemo(
    () => (file?.status === "image" ? base64ToBlob(file.dataBase64, file.mimeType) : null),
    [file],
  );
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const isSvg = file?.status === "image" && file.mimeType === "image/svg+xml";
  useEffect(() => {
    if (blob === null || isSvg) {
      setBlobUrl(null);
      return;
    }
    const url = URL.createObjectURL(blob);
    setBlobUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [blob, isSvg]);
  if (file?.status !== "image") return { src: null, blob: null };
  return {
    src: isSvg ? `data:${file.mimeType};base64,${file.dataBase64}` : blobUrl,
    blob,
  };
}

function ToolbarButton({
  icon,
  label,
  onClick,
  autoFocus,
}: {
  icon: string;
  label: string;
  onClick: () => void;
  autoFocus?: boolean;
}) {
  return (
    <Button
      variant="ghost"
      size="sm"
      className="text-white hover:bg-white/15 hover:text-white max-sm:size-11 max-sm:px-0"
      aria-label={label}
      autoFocus={autoFocus}
      onClick={onClick}
    >
      <Icon name={icon} className="size-4" />
      <span className="hidden sm:inline">{label}</span>
    </Button>
  );
}

function MessageCard({ children }: { children: ReactNode }) {
  return (
    <div className="max-w-md rounded-lg border border-border bg-card p-5 text-sm text-card-foreground">
      {children}
    </div>
  );
}

function ReadViewer({ state, onClose }: { state: ViewerState; onClose: () => void }) {
  const scopeProps = usePortalScopeProps();
  const navigate = useBbNavigate();
  const copyViaMac = useCopyViaMac();
  const dialogRef = useRef<HTMLDivElement>(null);
  const narrow = useNarrowScreen();
  const file = state.phase === "ready" ? state.file : null;
  const image = useImageSource(file);
  const [imageBroken, setImageBroken] = useState(false);
  // bb's toasts sit under this full-screen layer, so feedback stays inside it.
  const [notice, setNotice] = useState<Notice | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showNotice = useCallback((next: Notice) => {
    setNotice(next);
    if (noticeTimer.current !== null) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), NOTICE_MS);
  }, []);
  useEffect(
    () => () => {
      if (noticeTimer.current !== null) clearTimeout(noticeTimer.current);
    },
    [],
  );
  useEffect(() => setImageBroken(false), [file]);

  // The viewer covers bb, so focus must not wander into the app behind it.
  // Another modal dialog (one bb opened on top) keeps its own focus, and the
  // guard never re-enters, so two focus traps cannot bounce focus forever.
  useEffect(() => {
    let refocusing = false;
    const onFocusIn = (event: FocusEvent) => {
      const dialog = dialogRef.current;
      const target = event.target;
      if (refocusing || dialog === null || !(target instanceof Element) || dialog.contains(target)) {
        return;
      }
      if (target.closest("[data-clickable-reads-menu]") !== null) return;
      if (target.closest('[aria-modal="true"]') !== null) return;
      refocusing = true;
      try {
        (dialog.querySelector<HTMLElement>(FOCUSABLE) ?? dialog).focus();
      } finally {
        refocusing = false;
      }
    };
    document.addEventListener("focusin", onFocusIn);
    return () => document.removeEventListener("focusin", onFocusIn);
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // An open picture menu closes first.
      if (document.querySelector("[data-clickable-reads-menu]") !== null) return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [onClose]);

  const keepFocusInside = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab") return;
    const focusable = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(FOCUSABLE));
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const onCopy = async () => {
    if (image.blob !== null) {
      const failure = await copyPicture(image.blob, copyViaMac);
      showNotice(
        failure === null
          ? { tone: "ok", text: "Картинка скопирована" }
          : { tone: "error", text: copyFailedText(failure) },
      );
      return;
    }
    if (file?.status === "text") {
      showNotice(
        (await experimental_copyToClipboard({ text: file.text }))
          ? { tone: "ok", text: "Текст скопирован" }
          : { tone: "error", text: "Не получилось скопировать текст" },
      );
    }
  };

  const onDownload = async () => {
    if (file?.status !== "image" && file?.status !== "text") return;
    const blob =
      image.blob ?? new Blob([file.status === "text" ? file.text : ""], { type: "text/plain;charset=utf-8" });
    if ((await experimental_saveFile({ fileName: file.name, blob })) === "failed") {
      showNotice({ tone: "error", text: "Не получилось скачать файл" });
    }
  };

  const panelTarget =
    file !== null &&
    (file.status === "text" || file.status === "other" || (file.status === "error" && file.canOpenInPanel)) &&
    file.path !== null &&
    file.hostId !== null
      ? { path: file.path, hostId: file.hostId }
      : null;
  const onOpenInPanel = () => {
    if (panelTarget === null) return;
    const opened = navigate.experimental_openFilePreview({
      target: { kind: "host", hostId: panelTarget.hostId, path: panelTarget.path },
      location: null,
    });
    if (opened) onClose();
    else showNotice({ tone: "error", text: "bb не смог открыть этот файл в панели" });
  };

  let body: ReactNode;
  if (file === null) {
    body = <p className="animate-pulse text-sm text-white/70">Открываю файл…</p>;
  } else if (file.status === "image") {
    body = imageBroken ? (
      <MessageCard>Не удалось показать картинку: файл повреждён или это не картинка.</MessageCard>
    ) : image.src === null ? (
      <p className="animate-pulse text-sm text-white/70">Открываю файл…</p>
    ) : (
      <img
        src={image.src}
        alt={file.name}
        onError={() => setImageBroken(true)}
        className="max-h-full max-w-full rounded-md object-contain shadow-2xl"
      />
    );
  } else if (file.status === "text") {
    body = (
      <div className="flex h-full w-full max-w-5xl flex-col overflow-hidden rounded-lg border border-border bg-card text-card-foreground">
        {file.truncated ? (
          <p className="border-b border-border px-4 py-2 text-xs text-muted-foreground">
            Файл большой: показаны первые 400 000 символов.
          </p>
        ) : null}
        <SourceCode
          content={file.text}
          path={file.path}
          overflow={narrow ? "wrap" : "scroll"}
          className="min-h-0 flex-1 overflow-auto"
        />
      </div>
    );
  } else if (file.status === "other") {
    body = (
      <MessageCard>
        Такой файл здесь не показать.
        {panelTarget !== null ? " Открой его в панели bb кнопкой сверху." : null}
      </MessageCard>
    );
  } else {
    body = <MessageCard>{file.message}</MessageCard>;
  }

  const name = file?.name ?? null;
  const path = file?.path ?? null;
  const canCopy = file?.status === "image" || file?.status === "text";
  const canDownload = canCopy && !(file?.status === "text" && file.truncated);

  return createPortal(
    <div
      {...scopeProps}
      ref={dialogRef}
      tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-label={name ?? "Файл"}
      // bb's sidebar swipe would otherwise claim touches on the viewer.
      data-no-sidebar-swipe=""
      // Like bb's own lightbox: bb paints edge to edge (viewport-fit=cover),
      // so controls keep clear of phone status and navigation bars. bb's
      // Android app reports the navigation bar as --bb-safe-area-bottom, and
      // its macOS app reserves room for the window buttons in
      // --bb-macos-traffic-light-reserve.
      // pointer-events-auto: a Radix modal opened meanwhile disables taps on <body>.
      // no-drag: bb's macOS title bar is a window-drag region that would
      // swallow clicks on the viewer's buttons; bb's own overlays opt out too.
      className="pointer-events-auto fixed inset-0 z-[2147483000] flex h-dvh w-full flex-col bg-black/85 text-white outline-none [app-region:no-drag] [-webkit-app-region:no-drag]"
      onKeyDown={keepFocusInside}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <header className="flex items-center gap-1 bg-black/60 pb-2 pl-[var(--bb-macos-traffic-light-reserve,max(1rem,env(safe-area-inset-left)))] pr-[max(0.5rem,env(safe-area-inset-right))] pt-[calc(0.5rem+env(safe-area-inset-top))] sm:gap-2">
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{name ?? "Файл"}</div>
          {path !== null ? (
            <div className="truncate text-xs text-white/60" title={path}>
              {path}
              {file !== null && "sizeBytes" in file ? ` · ${formatBytes(file.sizeBytes)}` : ""}
            </div>
          ) : null}
        </div>
        {canCopy ? <ToolbarButton icon="Copy" label="Копировать" onClick={() => void onCopy()} /> : null}
        {canDownload ? (
          <ToolbarButton icon="Download" label="Скачать" onClick={() => void onDownload()} />
        ) : null}
        {panelTarget !== null ? (
          <ToolbarButton icon="PanelRight" label="Открыть в панели" onClick={onOpenInPanel} />
        ) : null}
        <ToolbarButton icon="X" label="Закрыть" onClick={onClose} autoFocus />
      </header>
      <div
        className="flex min-h-0 flex-1 items-center justify-center pb-[max(1rem,var(--bb-safe-area-bottom,env(safe-area-inset-bottom)))] pl-[max(0.5rem,env(safe-area-inset-left))] pr-[max(0.5rem,env(safe-area-inset-right))] sm:pl-[max(1rem,env(safe-area-inset-left))] sm:pr-[max(1rem,env(safe-area-inset-right))]"
        onClick={(event) => {
          if (event.target === event.currentTarget) onClose();
        }}
      >
        {body}
      </div>
      {notice !== null ? (
        <p
          role="status"
          className={`absolute bottom-[calc(0.75rem+var(--bb-safe-area-bottom,env(safe-area-inset-bottom)))] left-1/2 w-max max-w-[90vw] -translate-x-1/2 rounded-2xl bg-black/80 px-4 py-2 text-center text-sm ${notice.tone === "ok" ? "text-white" : "text-red-300"}`}
        >
          {notice.text}
        </p>
      ) : null}
    </div>,
    document.body,
  );
}

const EXTENSION_BY_TYPE: Readonly<Record<string, string>> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/gif": "gif",
  "image/webp": "webp",
  "image/svg+xml": "svg",
  "image/avif": "avif",
  "image/bmp": "bmp",
};

/** A download name: the URL's file name, else the picture's caption and type. */
function pictureFileName(src: string, alt: string, type: string): string {
  try {
    const url = new URL(src, window.location.href);
    if (url.protocol !== "blob:" && url.protocol !== "data:") {
      const last = decodeURIComponent(url.pathname.split("/").pop() ?? "");
      if (/\.[A-Za-z0-9]+$/.test(last)) return last;
    }
  } catch {
    // An unparsable src falls through to the caption.
  }
  const base = alt.replace(/[\\/:*?"<>|]+/g, " ").trim().slice(0, 80) || "image";
  return `${base}.${EXTENSION_BY_TYPE[type] ?? "png"}`;
}

/** A reason a picture's bytes could not be read, in the user's words. */
function pictureReadFailure(src: string, error: unknown): string {
  if (error instanceof TypeError) {
    try {
      const url = new URL(src, window.location.href);
      if (/^https?:$/.test(url.protocol) && url.origin !== window.location.origin) {
        return "картинка с другого сайта — браузер не даёт её прочитать";
      }
    } catch {
      // Fall through to the raw message.
    }
  }
  return error instanceof Error ? error.message : String(error);
}

/** A short-lived pill above everything, for picture-menu results. */
function FloatingNotice({ notice }: { notice: Notice }) {
  const scopeProps = usePortalScopeProps();
  return createPortal(
    <p
      {...scopeProps}
      role="status"
      className={`pointer-events-none fixed bottom-[calc(1rem+var(--bb-safe-area-bottom,env(safe-area-inset-bottom)))] left-1/2 z-[2147483002] w-max max-w-[90vw] -translate-x-1/2 rounded-2xl bg-black/85 px-4 py-2 text-center text-sm shadow-lg ${notice.tone === "ok" ? "text-white" : "text-red-300"}`}
    >
      {notice.text}
    </p>,
    document.body,
  );
}

function PictureMenu({
  request,
  onCopy,
  onDownload,
  onClose,
}: {
  request: ImageMenuRequest;
  onCopy: () => void;
  onDownload: () => void;
  onClose: () => void;
}) {
  const scopeProps = usePortalScopeProps();
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const items = () => Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
        return;
      }
      // bb's viewer switches pictures on arrows; under the menu they move the selection.
      if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      event.stopPropagation();
      const list = items();
      const current = list.indexOf(document.activeElement as HTMLElement);
      const next =
        event.key === "ArrowDown"
          ? (current + 1) % list.length
          : event.key === "ArrowUp"
            ? (current - 1 + list.length) % list.length
            : event.key === "Home"
              ? 0
              : event.key === "End"
                ? list.length - 1
                : current;
      list[next]?.focus();
    };
    let swallowTimer: ReturnType<typeof setTimeout> | null = null;
    const swallowClick = (event: Event) => {
      event.preventDefault();
      event.stopPropagation();
      stopSwallowing();
    };
    const stopSwallowing = () => {
      document.removeEventListener("click", swallowClick, true);
      if (swallowTimer !== null) clearTimeout(swallowTimer);
    };
    // A click outside only dismisses the menu: bb's viewer would close on it too.
    const onPointerDown = (event: Event) => {
      if (menuRef.current !== null && event.target instanceof Node && menuRef.current.contains(event.target)) {
        return;
      }
      document.addEventListener("click", swallowClick, true);
      swallowTimer = setTimeout(stopSwallowing, 1_000);
      onClose();
    };
    window.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("blur", onClose);
    window.addEventListener("resize", onClose);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("blur", onClose);
      window.removeEventListener("resize", onClose);
      // The swallow outlives the menu by design; it removes itself.
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [onClose]);
  // Keep the menu on screen near the cursor.
  const left = Math.max(8, Math.min(request.x, window.innerWidth - 220));
  const top = Math.max(8, Math.min(request.y, window.innerHeight - 100));
  const item =
    "flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-sm text-popover-foreground hover:bg-state-hover focus-visible:bg-state-hover focus-visible:outline-none";
  return createPortal(
    <div
      {...scopeProps}
      ref={menuRef}
      role="menu"
      aria-label="Картинка"
      data-clickable-reads-menu=""
      data-no-sidebar-swipe=""
      style={{ left, top }}
      className="pointer-events-auto fixed z-[2147483001] min-w-48 rounded-lg border border-border bg-popover p-1 shadow-xl [app-region:no-drag] [-webkit-app-region:no-drag]"
    >
      <button type="button" role="menuitem" className={item} onClick={onCopy} autoFocus>
        <Icon name="Copy" className="size-4" />
        Копировать картинку
      </button>
      <button type="button" role="menuitem" className={item} onClick={onDownload}>
        <Icon name="Download" className="size-4" />
        Скачать картинку
      </button>
    </div>,
    document.body,
  );
}

function ReadViewerOverlay() {
  const pluginId = experimental_usePluginId();
  const rpc = useRpc<typeof rpcContract>();
  const [state, setState] = useState<ViewerState | null>(null);
  // Bumped on every open and close, so a slow answer never reopens the viewer.
  const requestRef = useRef(0);

  useEffect(() => {
    const onOpen = (event: Event) => {
      const click: unknown = (event as CustomEvent<unknown>).detail;
      if (!isReadRowClick(click)) return;
      requestRef.current += 1;
      const request = requestRef.current;
      // The content script only sends rows titled "Read …"; a command that
      // turns out to be a search closes the viewer again.
      setState({ phase: "loading" });
      rpc.call("read_open", { rowId: click.rowId }).then(
        (file) => {
          if (request !== requestRef.current) return;
          setState(file.status === "not-read" ? null : { phase: "ready", file });
        },
        (error: unknown) => {
          if (request !== requestRef.current) return;
          setState({
            phase: "ready",
            file: {
              status: "error",
              message: `Не удалось открыть файл: ${error instanceof Error ? error.message : String(error)}`,
              name: null,
              path: null,
              hostId: null,
              canOpenInPanel: false,
            },
          });
        },
      );
    };
    const eventName = openReadEventName(pluginId);
    window.addEventListener(eventName, onOpen);
    return () => window.removeEventListener(eventName, onOpen);
  }, [pluginId, rpc]);

  const close = useCallback(() => {
    requestRef.current += 1;
    setState(null);
  }, []);

  const copyViaMac = useCopyViaMac();
  const [menu, setMenu] = useState<ImageMenuRequest | null>(null);
  const [floatingNotice, setFloatingNotice] = useState<Notice | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showFloatingNotice = useCallback((notice: Notice) => {
    setFloatingNotice(notice);
    if (noticeTimer.current !== null) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setFloatingNotice(null), NOTICE_MS);
  }, []);
  useEffect(
    () => () => {
      if (noticeTimer.current !== null) clearTimeout(noticeTimer.current);
    },
    [],
  );

  useEffect(() => {
    const onMenu = (event: Event) => {
      const request: unknown = (event as CustomEvent<unknown>).detail;
      if (!isImageMenuRequest(request)) return;
      // Tells the content script to hide bb desktop's native menu.
      event.preventDefault();
      setMenu(request);
    };
    const eventName = imageMenuEventName(pluginId);
    window.addEventListener(eventName, onMenu);
    return () => window.removeEventListener(eventName, onMenu);
  }, [pluginId]);

  const closeMenu = useCallback(() => setMenu(null), []);

  const pictureBlob = async (src: string): Promise<Blob> => {
    const response = await fetch(src);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.blob();
  };

  const copyMenuPicture = async (request: ImageMenuRequest) => {
    setMenu(null);
    let failure: string | null;
    try {
      failure = await copyPicture(await pictureBlob(request.src), copyViaMac);
    } catch (error) {
      failure = pictureReadFailure(request.src, error);
    }
    showFloatingNotice(
      failure === null
        ? { tone: "ok", text: "Картинка скопирована" }
        : { tone: "error", text: `Не получилось скопировать картинку (${failure})` },
    );
  };

  const downloadMenuPicture = async (request: ImageMenuRequest) => {
    setMenu(null);
    try {
      const blob = await pictureBlob(request.src);
      const fileName = pictureFileName(request.src, request.alt, blob.type);
      if ((await experimental_saveFile({ fileName, blob })) === "failed") {
        showFloatingNotice({ tone: "error", text: "Не получилось скачать картинку" });
      }
    } catch (error) {
      showFloatingNotice({
        tone: "error",
        text: `Не получилось скачать картинку (${pictureReadFailure(request.src, error)})`,
      });
    }
  };

  return (
    <>
      {state === null ? null : <ReadViewer state={state} onClose={close} />}
      {menu === null ? null : (
        <PictureMenu
          request={menu}
          onCopy={() => void copyMenuPicture(menu)}
          onDownload={() => void downloadMenuPicture(menu)}
          onClose={closeMenu}
        />
      )}
      {floatingNotice === null ? null : <FloatingNotice notice={floatingNotice} />}
    </>
  );
}

export default definePluginApp((app) => {
  app.contentScripts.register({
    id: "clickable-read-rows",
    mount: ({ pluginId, signal }) => mountReadRowClicks(pluginId, signal),
  });
  app.contentScripts.register({
    id: "picture-menu",
    mount: ({ pluginId, signal }) => mountImageMenu(pluginId, signal),
  });
  app.slots.experimental_appOverlay({
    id: "read-viewer",
    component: ReadViewerOverlay,
  });
});
