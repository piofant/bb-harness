import { describe, expect, it } from "vitest";
import { normalizeMix, parseVerdict, prompt, redact, verdictByDate, moscowDay } from "../lib/day";
import { FAIRY_IDS, fairyByDate } from "../lib/fairies";

const wrap = (o: unknown) => JSON.stringify({ structured_output: o });

describe("фея дня", () => {
  it("ответ модели: фея, объяснение, смесь в сумме 100 с выбранной первой", () => {
    const v = parseVerdict(
      wrap({ fairy: "tecna", why: "Чинила сервер и плагины", spell: "Код, превращайся!", mix: [{ fairy: "musa", pct: 30 }, { fairy: "tecna", pct: 60 }, { fairy: "musa", pct: 5 }] }),
    );
    expect(v).toMatchObject({ fairy: "tecna", source: "day" });
    expect(v.mix[0]!.fairy).toBe("tecna");
    expect(v.mix.reduce((s, m) => s + m.pct, 0)).toBe(100);
    expect(new Set(v.mix.map((m) => m.fairy)).size).toBe(v.mix.length);
  });

  it("чужая фея, пустое объяснение, ошибка модели — исключение", () => {
    expect(() => parseVerdict(wrap({ fairy: "darcy", why: "x", spell: "", mix: [] }))).toThrow();
    expect(() => parseVerdict(wrap({ fairy: "bloom", why: " ", spell: "", mix: [] }))).toThrow();
    expect(() => parseVerdict(JSON.stringify({ is_error: true, result: "budget" }))).toThrow(/budget/);
  });

  it("смесь без выбранной феи и с мусором всё равно корректна", () => {
    const mix = normalizeMix("flora", [{ fairy: "nope", pct: 99 }, { fairy: "aisha", pct: -5 }]);
    expect(mix[0]).toEqual({ fairy: "flora", pct: 100 });
  });

  it("ревью: выбранная фея с 0% не выпадает из состава", () => {
    const mix = normalizeMix("tecna", [{ fairy: "tecna", pct: 0 }, { fairy: "musa", pct: 80 }, { fairy: "flora", pct: 20 }]);
    expect(mix[0]!.fairy).toBe("tecna");
    expect(mix.reduce((s, m) => s + m.pct, 0)).toBe(100);
  });

  it("ревью: ключи и токены вырезаются", () => {
    expect(redact("ключ sk-abcdefghijklmnopqrstuv и ghp_abcdefghijklmnopqrstuvwxyz12")).toBe("ключ [ключ] и [ключ]");
    expect(redact("Authorization: Bearer abcdefghijklmnop1234")).toBe("Authorization: Bearer [ключ]");
  });

  it("тихий день — фея по дате, одна и та же весь день", () => {
    const a = verdictByDate("2026-11-05");
    expect(a.source).toBe("date");
    expect(FAIRY_IDS).toContain(a.fairy);
    expect(fairyByDate("2026-11-05")).toBe(a.fairy);
  });

  it("промпт: просьбы и итоги, без почт и телефонов", () => {
    const p = prompt([{ title: "письмо user@example.com", asks: ["позвони +7 916 123-45-67"], outcome: ["Готово"] }]);
    expect(p).toContain("ASK:");
    expect(p).not.toContain("user@example.com");
    expect(p).not.toContain("916");
    expect(redact("2026-11-05")).toBe("2026-11-05");
  });

  it("день по Москве", () => {
    expect(moscowDay(Date.parse("2026-11-04T22:30:00Z"))).toBe("2026-11-05");
  });
});
