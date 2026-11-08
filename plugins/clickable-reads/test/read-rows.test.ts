// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  isReadRowClick,
  openReadEventName,
  parseReadRowId,
  readRowForClickTarget,
} from "../lib/read-rows";
import { mountReadRowClicks } from "../lib/read-row-clicks";
import { imageMenuEventName, mountImageMenu } from "../lib/image-menu";

// Shapes copied from bb 0.47.1's rendered timeline.
const SUMMARY_ID = "thr_abc123:da1-t56:work-summary:thr_abc123:file-read:da1-i1640";
const READ_ID = "thr_abc123:file-read:da1-i1640";

function render(html: string): void {
  document.body.innerHTML = html;
}

function bundleWithFileRead(): string {
  return `
    <div data-timeline-row-id="${SUMMARY_ID}">
      <div><div><button type="button" aria-expanded="true" id="summary">Ran 1 command, explored 7 files</button></div>
      <div data-timeline-row-id="${READ_ID}">
        <div><div><span><span title="Read file /home/me/x/wb_e_example.png" id="read-label"><span>Read file</span> <span>wb_e_example.png</span></span></span></div></div>
      </div>
      <div data-timeline-row-id="thr_abc123:command:da1-i1639">
        <div><div><button type="button" aria-expanded="false" id="command">Ran cd /root && python3</button></div></div>
      </div></div>
    </div>`;
}

function element(id: string): HTMLElement {
  const found = document.getElementById(id);
  if (found === null) throw new Error(`missing #${id}`);
  return found;
}

afterEach(() => {
  document.body.innerHTML = "";
  document.head.innerHTML = "";
});

describe("parseReadRowId", () => {
  it("accepts file reads and commands with the outer thread id", () => {
    expect(parseReadRowId(READ_ID)).toEqual({
      threadId: "thr_abc123",
      kind: "file-read",
      callId: "da1-i1640",
    });
    expect(parseReadRowId("thr_abc123:command:call_9")).toEqual({
      threadId: "thr_abc123",
      kind: "command",
      callId: "call_9",
    });
  });

  it("accepts delegation children", () => {
    expect(
      parseReadRowId(
        "thr_outer1:delegation:da1-i530:child:thr_outer1:command:da1-i531",
      ),
    ).toEqual({ threadId: "thr_outer1", kind: "command", callId: "da1-i531" });
  });

  it("rejects bundle headers, other rows and oversized ids", () => {
    expect(parseReadRowId(SUMMARY_ID)).toBeNull();
    expect(parseReadRowId("thr_abc123:user-seed:12462")).toBeNull();
    expect(
      parseReadRowId(
        "thr_abc123:op:reasoning:kind:reasoning|turn:da1-t53|parent:root|item:da1-i1606",
      ),
    ).toBeNull();
    expect(parseReadRowId("thr_abc123:file-change:da1-i3")).toBeNull();
    expect(parseReadRowId(`thr_abc123:file-read:${"a".repeat(1100)}`)).toBeNull();
    expect(parseReadRowId("")).toBeNull();
  });
});

describe("readRowForClickTarget", () => {
  it("opens a file read row inside an expanded bundle", () => {
    render(bundleWithFileRead());
    expect(readRowForClickTarget(element("read-label"))).toEqual({
      rowId: READ_ID,
      kind: "file-read",
    });
  });

  it("leaves bundle headers and expandable commands to bb", () => {
    render(bundleWithFileRead());
    expect(readRowForClickTarget(element("summary"))).toBeNull();
    expect(readRowForClickTarget(element("command"))).toBeNull();
  });

  it("opens a bare command row titled Read, but not a search row", () => {
    render(`
      <div data-timeline-row-id="thr_c0dex1:command:i5"><span title="Read AGENTS.md" id="read">Read AGENTS.md</span></div>
      <div data-timeline-row-id="thr_c0dex1:command:i3"><span title="Searched for x in MEMORY.md" id="search">Searched</span></div>`);
    expect(readRowForClickTarget(element("read"))).toEqual({
      rowId: "thr_c0dex1:command:i5",
      kind: "command",
    });
    expect(readRowForClickTarget(element("search"))).toBeNull();
  });

  it("keeps links inside a row working", () => {
    render(`<div data-timeline-row-id="${READ_ID}"><a href="https://example.com" id="link">link</a></div>`);
    expect(readRowForClickTarget(element("link"))).toBeNull();
  });

  it("ignores clicks outside timeline rows", () => {
    render(`<p id="free">text</p>`);
    expect(readRowForClickTarget(element("free"))).toBeNull();
    expect(readRowForClickTarget(null)).toBeNull();
  });
});

describe("isReadRowClick", () => {
  it("validates event details", () => {
    expect(isReadRowClick({ rowId: READ_ID, kind: "file-read" })).toBe(true);
    expect(isReadRowClick({ rowId: READ_ID, kind: "command" })).toBe(false);
    expect(isReadRowClick({ rowId: SUMMARY_ID, kind: "file-read" })).toBe(false);
    expect(isReadRowClick("thr_abc123:file-read:x")).toBe(false);
    expect(isReadRowClick(null)).toBe(false);
  });
});

describe("mountReadRowClicks", () => {
  function mount() {
    const controller = new AbortController();
    const dispose = mountReadRowClicks("clickable-reads", controller.signal);
    const opened = vi.fn();
    window.addEventListener(openReadEventName("clickable-reads"), (event) =>
      opened((event as CustomEvent).detail),
    );
    return { controller, dispose, opened };
  }

  it("announces a click on a read row and stops bb from handling it", () => {
    render(bundleWithFileRead());
    const { dispose, opened } = mount();
    const click = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
    element("read-label").dispatchEvent(click);
    expect(opened).toHaveBeenCalledWith({ rowId: READ_ID, kind: "file-read" });
    expect(click.defaultPrevented).toBe(true);
    dispose();
  });

  it("lets bundle headers, modified clicks and other buttons through", () => {
    render(bundleWithFileRead());
    const { dispose, opened } = mount();
    const header = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
    element("summary").dispatchEvent(header);
    element("read-label").dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, metaKey: true }),
    );
    element("read-label").dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true, button: 1 }),
    );
    expect(opened).not.toHaveBeenCalled();
    expect(header.defaultPrevented).toBe(false);
    dispose();
  });

  it("injects the row style and removes it with the listener on dispose", () => {
    render(bundleWithFileRead());
    const { dispose, opened } = mount();
    expect(document.head.querySelector('style[data-bb-plugin-style="clickable-reads"]')).not.toBeNull();
    dispose();
    expect(document.head.querySelector("style")).toBeNull();
    element("read-label").dispatchEvent(new MouseEvent("click", { bubbles: true, button: 0 }));
    expect(opened).not.toHaveBeenCalled();
  });

  it("stops listening when the host aborts the mount", () => {
    render(bundleWithFileRead());
    const { controller, opened } = mount();
    controller.abort();
    element("read-label").dispatchEvent(new MouseEvent("click", { bubbles: true, button: 0 }));
    expect(opened).not.toHaveBeenCalled();
  });
});

describe("mountImageMenu", () => {
  afterEach(() => Reflect.deleteProperty(window, "bbDesktop"));

  function setupPictures() {
    render(`
      <div role="dialog" aria-modal="true"><img id="pic" src="/api/v1/a.png" alt="chart"></div>
      <img id="loose" src="/api/v1/b.png" alt="other">`);
  }

  it("replaces bb desktop's native menu on a viewer picture once the menu takes it", () => {
    Object.defineProperty(window, "bbDesktop", { value: { platform: "macos" }, configurable: true });
    setupPictures();
    const controller = new AbortController();
    const dispose = mountImageMenu("clickable-reads", controller.signal);
    const opened = vi.fn();
    const take = (event: Event) => {
      event.preventDefault();
      opened((event as CustomEvent).detail);
    };
    window.addEventListener(imageMenuEventName("clickable-reads"), take);
    try {
      const onPicture = new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 10, clientY: 20 });
      element("pic").dispatchEvent(onPicture);
      expect(onPicture.defaultPrevented).toBe(true);
      expect(opened).toHaveBeenCalledWith({
        src: "http://localhost:3000/api/v1/a.png",
        alt: "chart",
        x: 10,
        y: 20,
      });
      const elsewhere = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
      element("loose").dispatchEvent(elsewhere);
      expect(elsewhere.defaultPrevented).toBe(false);
      expect(opened).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(imageMenuEventName("clickable-reads"), take);
      dispose();
    }
  });

  it("keeps the native menu when nothing takes the event", () => {
    Object.defineProperty(window, "bbDesktop", { value: { platform: "macos" }, configurable: true });
    setupPictures();
    const dispose = mountImageMenu("clickable-reads", new AbortController().signal);
    try {
      const onPicture = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
      element("pic").dispatchEvent(onPicture);
      expect(onPicture.defaultPrevented).toBe(false);
    } finally {
      dispose();
    }
  });

  it("leaves browsers and phones their own picture menu", () => {
    setupPictures();
    const dispose = mountImageMenu("clickable-reads", new AbortController().signal);
    const opened = vi.fn((event: Event) => event.preventDefault());
    window.addEventListener(imageMenuEventName("clickable-reads"), opened);
    try {
      const onPicture = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
      element("pic").dispatchEvent(onPicture);
      expect(onPicture.defaultPrevented).toBe(false);
      expect(opened).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener(imageMenuEventName("clickable-reads"), opened);
      dispose();
    }
  });
});
