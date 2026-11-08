import { describe, expect, it } from "vitest";
import { dayPhase, formatMoscow, lighting, moscowClock, sunAltitude, sunTimes } from "../lib/sun";

const msk = (iso: string) => Date.parse(`${iso}+03:00`);

describe("солнце над Москвой", () => {
  it("рассвет и закат 4 ноября 2026 — около 07:42 и 16:43 (полдень 12:13, день 9 ч)", () => {
    const { sunrise, sunset } = sunTimes(msk("2026-11-04T12:00:00"));
    expect(Math.abs(sunrise! - msk("2026-11-04T07:42:00"))).toBeLessThan(3 * 60_000);
    expect(Math.abs(sunset! - msk("2026-11-04T16:43:00"))).toBeLessThan(3 * 60_000);
  });

  it("летнее солнцестояние: рассвет около 03:44, закат около 21:18", () => {
    const { sunrise, sunset } = sunTimes(msk("2026-06-21T12:00:00"));
    expect(Math.abs(sunrise! - msk("2026-06-21T03:44:00"))).toBeLessThan(4 * 60_000);
    expect(Math.abs(sunset! - msk("2026-06-21T21:18:00"))).toBeLessThan(4 * 60_000);
  });

  it("сразу после полуночи по Москве берёт рассвет того же московского дня", () => {
    const { sunrise } = sunTimes(msk("2026-11-04T00:30:00"));
    expect(formatMoscow(sunrise!)).toMatch(/^07:4\d$/);
  });

  it("фазы: полдень — день, полночь — ночь, у рассвета — рассвет, у заката — закат", () => {
    expect(dayPhase(msk("2026-11-04T12:00:00"))).toBe("day");
    expect(dayPhase(msk("2026-11-04T23:59:00"))).toBe("night");
    expect(dayPhase(msk("2026-11-04T07:45:00"))).toBe("dawn");
    expect(dayPhase(msk("2026-11-04T16:45:00"))).toBe("dusk");
  });

  it("свет: днём темноты и свечей нет, ночью темно и свечи горят", () => {
    const noon = lighting(msk("2026-11-04T12:30:00"));
    expect(noon.darkness).toBe(0);
    expect(noon.candles).toBe(0);
    const night = lighting(msk("2026-11-04T23:00:00"));
    expect(night.darkness).toBe(1);
    expect(night.candles).toBe(1);
    expect(sunAltitude(msk("2026-11-04T23:00:00"))).toBeLessThan(-30);
  });

  it("часы показывают московское время независимо от зоны машины", () => {
    expect(moscowClock(msk("2026-11-04T14:05:09"))).toEqual({ h: 14, m: 5, s: 9 });
    expect(formatMoscow(msk("2026-11-04T07:05:00"))).toBe("07:05");
  });
});
