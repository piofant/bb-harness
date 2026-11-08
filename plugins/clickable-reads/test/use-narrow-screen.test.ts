// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NARROW_SCREEN_QUERY, useNarrowScreen } from "../lib/use-narrow-screen";

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(window, "matchMedia");
});

function stubMatchMedia(initial: boolean) {
  let listener: (() => void) | null = null;
  const media = {
    matches: initial,
    addEventListener: vi.fn((_type: string, next: () => void) => {
      listener = next;
    }),
    removeEventListener: vi.fn(),
  };
  const matchMedia = vi.fn(() => media);
  Object.defineProperty(window, "matchMedia", { value: matchMedia, configurable: true });
  return {
    matchMedia,
    media,
    change(next: boolean) {
      media.matches = next;
      listener?.();
    },
  };
}

describe("useNarrowScreen", () => {
  it("is false where the browser has no matchMedia", () => {
    expect(renderHook(() => useNarrowScreen()).result.current).toBe(false);
  });

  it("uses Tailwind's sm breakpoint and follows changes", () => {
    const media = stubMatchMedia(true);
    const { result, unmount } = renderHook(() => useNarrowScreen());
    expect(media.matchMedia).toHaveBeenCalledWith(NARROW_SCREEN_QUERY);
    expect(NARROW_SCREEN_QUERY).toBe("not all and (min-width: 40rem)");
    expect(result.current).toBe(true);
    act(() => media.change(false));
    expect(result.current).toBe(false);
    unmount();
    expect(media.media.removeEventListener).toHaveBeenCalled();
  });

  it("catches a change between the first render and subscribing", () => {
    const media = stubMatchMedia(false);
    const original = media.media.addEventListener;
    media.media.addEventListener = vi.fn((type: string, next: () => void) => {
      media.media.matches = true;
      original(type, next);
    });
    const { result } = renderHook(() => useNarrowScreen());
    expect(result.current).toBe(true);
  });
});
