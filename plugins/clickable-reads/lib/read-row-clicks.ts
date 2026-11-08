// The content script's DOM side: styles read rows as clickable and turns a
// click on one into a window event for the viewer overlay.
import {
  openReadEventName,
  READ_ROW_STYLE,
  readRowForClickTarget,
} from "./read-rows";

/** Installs the row styles and the click listener; returns the disposer. */
export function mountReadRowClicks(
  pluginId: string,
  signal: AbortSignal,
): () => void {
  const style = document.createElement("style");
  style.setAttribute("data-bb-plugin-style", pluginId);
  style.textContent = READ_ROW_STYLE;
  document.head.append(style);

  const onClick = (event: MouseEvent) => {
    if (event.button !== 0 || event.defaultPrevented) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const click = readRowForClickTarget(event.target);
    if (click === null) return;
    // A drag that selected row text is a copy, not an open.
    const selection = window.getSelection();
    if (selection !== null && !selection.isCollapsed && selection.toString().trim() !== "") {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    window.dispatchEvent(
      new CustomEvent(openReadEventName(pluginId), { detail: click }),
    );
  };
  document.addEventListener("click", onClick, { capture: true, signal });

  return () => {
    document.removeEventListener("click", onClick, { capture: true });
    style.remove();
  };
}
