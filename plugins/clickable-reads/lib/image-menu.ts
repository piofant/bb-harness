// A right click on a picture inside a viewer dialog (bb's image lightbox or
// this plugin's viewer) opens the plugin's menu. bb desktop's native menu
// offers only "Select All" there, and it cannot be extended from a page.

export interface ImageMenuRequest {
  src: string;
  alt: string;
  x: number;
  y: number;
}

export function imageMenuEventName(pluginId: string): string {
  return `${pluginId}:image-menu`;
}

export function isImageMenuRequest(value: unknown): value is ImageMenuRequest {
  if (typeof value !== "object" || value === null) return false;
  const { src, alt, x, y } = value as Record<string, unknown>;
  return (
    typeof src === "string" &&
    src.length > 0 &&
    typeof alt === "string" &&
    typeof x === "number" &&
    typeof y === "number"
  );
}

/**
 * Installs the picture right-click listener in bb desktop; returns the
 * disposer. Browsers and phones keep their own, richer picture menu, and the
 * native menu stays unless the plugin's menu takes the event.
 */
export function mountImageMenu(pluginId: string, signal: AbortSignal): () => void {
  const desktop = (window as { bbDesktop?: unknown }).bbDesktop;
  if (typeof desktop !== "object" || desktop === null) return () => {};
  const onContextMenu = (event: MouseEvent) => {
    const target = event.target;
    if (!(target instanceof HTMLImageElement)) return;
    if (target.closest('[role="dialog"][aria-modal="true"]') === null) return;
    const src = target.currentSrc || target.src;
    if (!src) return;
    const request: ImageMenuRequest = { src, alt: target.alt, x: event.clientX, y: event.clientY };
    const taken = !window.dispatchEvent(
      new CustomEvent(imageMenuEventName(pluginId), { detail: request, cancelable: true }),
    );
    if (taken) event.preventDefault();
  };
  document.addEventListener("contextmenu", onContextMenu, { capture: true, signal });
  return () => document.removeEventListener("contextmenu", onContextMenu, { capture: true });
}
