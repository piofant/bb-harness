import { describe, expect, it } from "vitest";
import { distinctSprites, inventName, personaFor, SPRITES } from "../lib/personas";

describe("персонажи", () => {
  it("персонаж с сайта таверны сохраняет имя и спрайт", () => {
    expect(personaFor("tg-research")).toMatchObject({ name: "Тгша", sprite: "DarkNinja", origin: "tavern" });
  });
  it("частый скилл без персонажа берёт имя из словаря", () => {
    expect(personaFor("code-review")).toMatchObject({ name: "Ревьюша", origin: "named" });
  });
  it("своё имя главнее всех", () => {
    expect(personaFor("tg-research", { "tg-research": "Шпион" })).toMatchObject({ name: "Шпион", origin: "custom" });
  });
  it("незнакомому скиллу имя придумывается, кириллицей и стабильно", () => {
    const a = personaFor("weather-forecast");
    expect(a.origin).toBe("invented");
    expect(a.name).toMatch(/^[А-ЯЁ][а-яё]+$/);
    expect(personaFor("weather-forecast")).toEqual(a);
    expect(inventName("acme-zzz-browser")).toMatch(/^[А-ЯЁ][а-яё]+$/);
    expect(inventName("123")).toBe("Безымянка");
    expect(SPRITES).toContain(a.sprite);
  });
  it("на сцене одинаковые спрайты разводятся", () => {
    const items = Array.from({ length: 10 }, () => ({ sprite: "Monk" as const }));
    const out = distinctSprites(items);
    expect(new Set(out.map((i) => i.sprite)).size).toBe(10);
    expect(out[0]!.sprite).toBe("Monk");
  });
});
