// @vitest-environment jsdom
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { loadPluginApp, renderSlot } from "@get-bb/plugin-sdk/testing/app";
import type { ReadOpenResult } from "../server";

const PLUGIN = "test-plugin";
const READ_ROW = "thr_abc123:file-read:da1-i1643";
const COMMAND_ROW = "thr_abc123:command:call_7";

let app: Awaited<ReturnType<typeof loadPluginApp>>;

beforeAll(async () => {
  app = await loadPluginApp(() => import("../app"));
});

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => "blob:test/1");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "clipboard");
  Reflect.deleteProperty(window, "bbDesktop");
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

function render(
  readOpen: () => ReadOpenResult | Promise<ReadOpenResult>,
  openFilePreview?: () => boolean,
  copyToMac: (input: unknown) => unknown = () => ({ status: "copied" }),
) {
  return renderSlot(
    app.appOverlays[0]!,
    {},
    {
      pluginId: PLUGIN,
      rpc: { read_open: readOpen, copy_image_to_mac: copyToMac },
      ...(openFilePreview === undefined ? {} : { openFilePreview }),
    },
  );
}

function openImageMenu(src: string, alt: string): boolean {
  let handled = false;
  act(() => {
    handled = !window.dispatchEvent(
      new CustomEvent(`${PLUGIN}:image-menu`, { detail: { src, alt, x: 40, y: 50 }, cancelable: true }),
    );
  });
  return handled;
}

function openRow(rowId: string, kind: "file-read" | "command") {
  act(() => {
    window.dispatchEvent(
      new CustomEvent(`${PLUGIN}:open-read-row`, { detail: { rowId, kind } }),
    );
  });
}

const image = (overrides: Partial<Extract<ReadOpenResult, { status: "image" }>> = {}) =>
  ({
    status: "image",
    name: "shot.png",
    path: "/w/shot.png",
    hostId: "host_vps",
    sizeBytes: 3,
    mimeType: "image/png",
    dataBase64: btoa("png"),
    ...overrides,
  }) satisfies ReadOpenResult;

describe("read viewer", () => {
  it("shows a loading state at once for a command read and closes when it is not a read", async () => {
    const answer = deferred<ReadOpenResult>();
    render(() => answer.promise);
    openRow(COMMAND_ROW, "command");
    expect(await screen.findByText("Открываю файл…")).toBeTruthy();
    await act(async () => answer.resolve({ status: "not-read" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("shows a raster image through a blob URL", async () => {
    render(() => image());
    openRow(READ_ROW, "file-read");
    const picture = await screen.findByRole("img", { name: "shot.png" });
    expect(picture.getAttribute("src")).toBe("blob:test/1");
  });

  it("shows an SVG through a data URL so it can never open as a page", async () => {
    const svg = btoa("<svg xmlns='http://www.w3.org/2000/svg'/>");
    render(() =>
      image({ name: "logo.svg", path: "/w/logo.svg", mimeType: "image/svg+xml", dataBase64: svg }),
    );
    openRow(READ_ROW, "file-read");
    const picture = await screen.findByRole("img", { name: "logo.svg" });
    expect(picture.getAttribute("src")).toBe(`data:image/svg+xml;base64,${svg}`);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("reports a failed copy inside the viewer", async () => {
    render(() => image());
    openRow(READ_ROW, "file-read");
    await screen.findByRole("img", { name: "shot.png" });
    fireEvent.click(screen.getByRole("button", { name: "Копировать" }));
    expect((await screen.findByRole("status")).textContent).toContain(
      "Не получилось скопировать картинку",
    );
  });

  it("reports a successful copy inside the viewer", async () => {
    const write = vi.fn(async () => undefined);
    vi.stubGlobal(
      "ClipboardItem",
      class {
        constructor(readonly items: Record<string, unknown>) {}
      },
    );
    Object.defineProperty(navigator, "clipboard", { value: { write }, configurable: true });
    render(() => image());
    openRow(READ_ROW, "file-read");
    await screen.findByRole("img", { name: "shot.png" });
    fireEvent.click(screen.getByRole("button", { name: "Копировать" }));
    expect((await screen.findByRole("status")).textContent).toBe("Картинка скопирована");
    expect(write).toHaveBeenCalledTimes(1);
  });

  it("copies through the Mac when the desktop app has no Clipboard API", async () => {
    Object.defineProperty(window, "bbDesktop", { value: { platform: "macos" }, configurable: true });
    const copyToMac = vi.fn((_input: unknown) => ({ status: "copied" }));
    render(() => image(), undefined, copyToMac);
    openRow(READ_ROW, "file-read");
    await screen.findByRole("img", { name: "shot.png" });
    fireEvent.click(screen.getByRole("button", { name: "Копировать" }));
    expect((await screen.findByRole("status")).textContent).toBe("Картинка скопирована");
    expect(copyToMac).toHaveBeenCalledWith({ pngBase64: btoa("png") });
  });

  it("offers copy and download on a right click on a picture in bb's viewer", async () => {
    Object.defineProperty(window, "bbDesktop", { value: { platform: "macos" }, configurable: true });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, blob: async () => new Blob(["png"], { type: "image/png" }) })),
    );
    const copyToMac = vi.fn((_input: unknown) => ({ status: "copied" }));
    render(() => image(), undefined, copyToMac);
    openImageMenu("/api/v1/attachments/chart.png", "Динамика изменения веса");
    const menu = await screen.findByRole("menu");
    expect(menu.textContent).toContain("Скачать картинку");
    fireEvent.click(screen.getByRole("menuitem", { name: "Копировать картинку" }));
    expect((await screen.findByRole("status")).textContent).toBe("Картинка скопирована");
    expect(copyToMac).toHaveBeenCalledWith({ pngBase64: btoa("png") });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("takes the right-click event so bb desktop hides its native menu", async () => {
    render(() => image());
    expect(openImageMenu("/api/v1/attachments/chart.png", "chart")).toBe(true);
    await screen.findByRole("menu");
  });

  it("swallows the click that dismisses the menu so bb's viewer stays open", async () => {
    const behind = document.createElement("button");
    const clicked = vi.fn();
    behind.addEventListener("click", clicked);
    document.body.append(behind);
    try {
      render(() => image());
      openImageMenu("/api/v1/attachments/chart.png", "chart");
      await screen.findByRole("menu");
      fireEvent.pointerDown(behind);
      fireEvent.click(behind);
      expect(clicked).not.toHaveBeenCalled();
      await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
      fireEvent.click(behind);
      expect(clicked).toHaveBeenCalledTimes(1);
    } finally {
      behind.remove();
    }
  });

  it("keeps arrow keys from switching bb's picture under the menu", async () => {
    const pageKeys = vi.fn();
    window.addEventListener("keydown", pageKeys);
    try {
      render(() => image());
      openImageMenu("/api/v1/attachments/chart.png", "chart");
      const copy = await screen.findByRole("menuitem", { name: "Копировать картинку" });
      fireEvent.keyDown(copy, { key: "ArrowRight" });
      expect(pageKeys).not.toHaveBeenCalled();
      fireEvent.keyDown(copy, { key: "ArrowDown" });
      expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Скачать картинку" }));
    } finally {
      window.removeEventListener("keydown", pageKeys);
    }
  });

  it("lets the menu keep focus over the plugin's viewer", async () => {
    render(() => image());
    openRow(READ_ROW, "file-read");
    await screen.findByRole("img", { name: "shot.png" });
    openImageMenu("/api/v1/attachments/chart.png", "chart");
    const copy = await screen.findByRole("menuitem", { name: "Копировать картинку" });
    await waitFor(() => expect(document.activeElement).toBe(copy));
  });

  it("does not route a non-Mac desktop app to a Mac", async () => {
    Object.defineProperty(window, "bbDesktop", { value: { platform: "windows" }, configurable: true });
    const copyToMac = vi.fn((_input: unknown) => ({ status: "copied" }));
    render(() => image(), undefined, copyToMac);
    openRow(READ_ROW, "file-read");
    await screen.findByRole("img", { name: "shot.png" });
    fireEvent.click(screen.getByRole("button", { name: "Копировать" }));
    expect((await screen.findByRole("status")).textContent).toContain("Не получилось скопировать картинку");
    expect(copyToMac).not.toHaveBeenCalled();
  });

  it("explains a picture from another site", async () => {
    Object.defineProperty(window, "bbDesktop", { value: { platform: "macos" }, configurable: true });
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    render(() => image());
    openImageMenu("https://example.com/pic.png", "pic");
    fireEvent.click(await screen.findByRole("menuitem", { name: "Копировать картинку" }));
    expect((await screen.findByRole("status")).textContent).toBe(
      "Не получилось скопировать картинку (картинка с другого сайта — браузер не даёт её прочитать)",
    );
  });

  it("closes the picture menu on Escape", async () => {
    render(() => image());
    openImageMenu("/api/v1/attachments/chart.png", "chart");
    await screen.findByRole("menu");
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("says when the picture cannot be shown", async () => {
    render(() => image());
    openRow(READ_ROW, "file-read");
    fireEvent.error(await screen.findByRole("img", { name: "shot.png" }));
    expect(
      await screen.findByText("Не удалось показать картинку: файл повреждён или это не картинка."),
    ).toBeTruthy();
  });

  it("offers bb's panel for a file bb refused as too large", async () => {
    const openFilePreview = vi.fn(() => true);
    const slot = render(
      () => ({
        status: "error",
        message: "Файл больше 10 МБ — bb не отдаёт такие для просмотра.",
        name: "huge.png",
        path: "/w/huge.png",
        hostId: "host_vps",
        canOpenInPanel: true,
      }),
      openFilePreview,
    );
    openRow(READ_ROW, "file-read");
    fireEvent.click(await screen.findByRole("button", { name: "Открыть в панели" }));
    expect(openFilePreview).toHaveBeenCalledWith({
      target: { kind: "host", hostId: "host_vps", path: "/w/huge.png" },
      location: null,
    });
    expect(slot.inspection.navigateCalls.some((call) => call.method === "experimental_openFilePreview")).toBe(true);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("offers no panel for a file that is gone", async () => {
    render(() => ({
      status: "error",
      message: "Файла уже нет: его удалили или переместили.",
      name: "gone.png",
      path: "/w/gone.png",
      hostId: "host_vps",
      canOpenInPanel: false,
    }));
    openRow(READ_ROW, "file-read");
    expect(await screen.findByText("Файла уже нет: его удалили или переместили.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Открыть в панели" })).toBeNull();
  });

  it("pulls focus back when it leaves the viewer", async () => {
    const outside = document.createElement("button");
    outside.textContent = "bb behind the viewer";
    document.body.prepend(outside);
    render(() => image());
    openRow(READ_ROW, "file-read");
    await screen.findByRole("img", { name: "shot.png" });
    outside.focus();
    expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
    outside.remove();
  });

  it("lets another modal dialog keep focus", async () => {
    render(() => image());
    openRow(READ_ROW, "file-read");
    await screen.findByRole("img", { name: "shot.png" });
    const other = document.createElement("div");
    other.setAttribute("role", "dialog");
    other.setAttribute("aria-modal", "true");
    const field = document.createElement("input");
    other.append(field);
    document.body.append(other);
    try {
      field.focus();
      expect(document.activeElement).toBe(field);
    } finally {
      other.remove();
    }
  });

  it("keeps its controls clear of phone system bars and bb's swipe gesture", async () => {
    render(() => image());
    openRow(READ_ROW, "file-read");
    await screen.findByRole("img", { name: "shot.png" });
    const dialog = screen.getByRole("dialog");
    expect(dialog.hasAttribute("data-no-sidebar-swipe")).toBe(true);
    expect(dialog.className).toContain("pointer-events-auto");
    const header = dialog.querySelector("header");
    // A gap below the status bar, so a slightly high tap still hits the button.
    expect(header?.className).toContain("calc(0.5rem+env(safe-area-inset-top))");
    // bb's Android app reports the navigation bar through its own variable.
    expect(dialog.innerHTML).toContain("var(--bb-safe-area-bottom,env(safe-area-inset-bottom))");
    expect(screen.getByRole("button", { name: "Закрыть" }).className).toContain("size-11");
  });

  it("stays clickable over the macOS title bar and clears the window buttons", async () => {
    render(() => image());
    openRow(READ_ROW, "file-read");
    await screen.findByRole("img", { name: "shot.png" });
    const dialog = screen.getByRole("dialog");
    // bb's desktop title bar is a window-drag region; clicks there never reach
    // the page unless the overlay opts out, as bb's own overlays do.
    expect(dialog.className).toContain("[app-region:no-drag]");
    expect(dialog.className).toContain("[-webkit-app-region:no-drag]");
    // bb sets this on <html> only in its macOS desktop app.
    expect(dialog.querySelector("header")?.className).toContain("var(--bb-macos-traffic-light-reserve,");
  });

  it("reports inside the viewer when bb's panel refuses the file", async () => {
    render(
      () => ({ status: "other", name: "doc.pdf", path: "/w/doc.pdf", hostId: "host_vps", sizeBytes: 3, mimeType: "application/pdf" }),
      () => false,
    );
    openRow(READ_ROW, "file-read");
    fireEvent.click(await screen.findByRole("button", { name: "Открыть в панели" }));
    expect((await screen.findByRole("status")).textContent).toBe("bb не смог открыть этот файл в панели");
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("ignores an answer that arrives after the viewer closed", async () => {
    const answer = deferred<ReadOpenResult>();
    render(() => answer.promise);
    openRow(READ_ROW, "file-read");
    await screen.findByText("Открываю файл…");
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await act(async () => answer.resolve(image()));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps keyboard focus inside the viewer", async () => {
    render(() => image());
    openRow(READ_ROW, "file-read");
    await screen.findByRole("img", { name: "shot.png" });
    const close = screen.getByRole("button", { name: "Закрыть" });
    const copy = screen.getByRole("button", { name: "Копировать" });
    close.focus();
    fireEvent.keyDown(close, { key: "Tab" });
    expect(document.activeElement).toBe(copy);
    fireEvent.keyDown(copy, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(close);
  });
});
