import { describe, it, expect, vi } from "vitest";
import { loadPluginApp } from "@get-bb/plugin-sdk/testing/app";
import {
  formatPhase,
  formatDate,
  fallbackThreadAction,
  ConversationsNavPanel,
  FallbackComposerBanner,
} from "../app";

describe("Frontend Claude Codex Fallback App", () => {
  it("registers navPanel, threadAction and composer customization correctly via loadPluginApp", async () => {
    const app = await loadPluginApp(() => import("../app"));

    // NavPanel registration
    const conversationsPanel = app.navPanels.find((p) => p.id === "conversations");
    expect(conversationsPanel).toBeDefined();
    expect(conversationsPanel?.path).toBe("conversations");
    expect(conversationsPanel?.title).toBe("Автопереход");
    expect(conversationsPanel?.icon).toBe("Repeat");

    // Thread action registration
    const action = app.threadActions.find((a) => a.id === "fallback-chat");
    expect(action).toBeDefined();
    expect(action?.title).toBe("Автопереход");
    expect(action?.icon).toBe("Repeat");
    expect(action?.placement).toBe("header");
    expect(action?.group).toBe("actions");

    // Composer customization registration
    const composer = app.composerCustomizations.find((c) => c.id === "fallback-banner");
    expect(composer).toBeDefined();
    expect(composer?.scopes).toContain("thread");
    expect(composer?.banners).toHaveLength(1);
    expect(composer?.banners?.[0].id).toBe("fallback-banner-item");
  });

  describe("formatPhase helper", () => {
    it("formats ready phase", () => {
      const res = formatPhase("ready");
      expect(res.label).toBe("Готов");
      expect(res.tone).toContain("emerald");
    });

    it("formats switching phase", () => {
      const res = formatPhase("switching");
      expect(res.label).toBe("Переключение...");
      expect(res.tone).toContain("blue");
    });

    it("formats waiting-retry phase", () => {
      const res = formatPhase("waiting-retry");
      expect(res.label).toBe("Ожидание повтора");
      expect(res.tone).toContain("amber");
    });

    it("formats blocked phase", () => {
      const res = formatPhase("blocked");
      expect(res.label).toBe("Заблокирован");
      expect(res.tone).toContain("rose");
    });

    it("formats exhausted phase", () => {
      const res = formatPhase("exhausted");
      expect(res.label).toBe("Попытки исчерпаны");
      expect(res.tone).toContain("destructive");
    });
  });

  describe("formatDate helper", () => {
    it("handles zero/falsy timestamp", () => {
      expect(formatDate(0)).toBe("");
    });

    it("formats valid timestamp to time string", () => {
      const formatted = formatDate(1700000000000);
      expect(formatted).toMatch(/\d{2}:\d{2}/);
    });
  });

  describe("fallbackThreadAction", () => {
    it("returns correct item presentation and navigates on run", async () => {
      const mockNavigate = {
        toPluginPanel: vi.fn(),
        toThread: vi.fn(),
      };

      const item = fallbackThreadAction.item({
        thread: { id: "thr_test_42" } as any,
        navigate: mockNavigate as any,
        sdk: {} as any,
        data: undefined,
        experimental_panel: {} as any,
      });

      expect(item).toBeDefined();
      expect(item?.label).toBe("Автопереход");
      expect(item?.icon).toBe("Repeat");

      await item?.run?.({ requestRename: vi.fn() });
      expect(mockNavigate.toPluginPanel).toHaveBeenCalledWith("conversations", {
        subPath: "thr_test_42",
      });
    });
  });

  describe("components export check", () => {
    it("exports all necessary views and components", () => {
      expect(typeof ConversationsNavPanel).toBe("function");
      expect(typeof FallbackComposerBanner).toBe("function");
    });
  });
});
