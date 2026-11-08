import { describe, expect, it } from "vitest";
import { buildBoard, overviewPrompt, parseOverview, redact, type ThreadDigest, type TvThread } from "../lib/tv";
import { recordUse, type UsageMap } from "../lib/usage";

const now = Date.parse("2026-11-04T09:00:00Z");
const days = ["2026-11-04", "2026-11-03", "2026-11-02"];
const h = 3_600_000;
const threads: TvThread[] = [
  { id: "t1", title: "таверна скиллов", projectId: "p1", updatedAt: now - h, hidden: false, child: false },
  { id: "t2", title: "починка названий", projectId: "p1", updatedAt: now - 30 * h, hidden: false, child: false },
  { id: "t3", title: "сабагент", projectId: "p1", updatedAt: now - 2 * h, hidden: true, child: true },
  { id: "t4", title: "онбординг", projectId: "p2", updatedAt: now - 5 * h, hidden: false, child: false },
  { id: "t5", title: "давнишнее", projectId: "p2", updatedAt: now - 200 * h, hidden: false, child: false },
];
const names = new Map([["p1", "bb"], ["p2", "bot"]]);

describe("табло", () => {
  const usage: UsageMap = {};
  recordUse(usage, "mac-bridge", now - h, "t1");
  recordUse(usage, "mac-bridge", now - 2 * h, "t1");
  recordUse(usage, "holst", now - 5 * h, "t4");
  recordUse(usage, "chat-names", now - h, "t1");
  recordUse(usage, "old", now - 200 * h, "t5");
  const b = buildBoard({ now, days, threads, projectNames: names, usage, exclude: new Set(["chat-names"]) });

  it("команды — проекты, счёт — треды за период", () => {
    expect(b.teams.map((t) => [t.name, t.threads])).toEqual([["bb", 3], ["bot", 1]]);
    expect(b.totalThreads).toBe(4);
  });
  it("игрок периода — самый частый скилл, трактирщик и старые не в зачёте", () => {
    expect(b.mvp).toEqual({ slug: "mac-bridge", calls: 2 });
    expect(b.topSkills.map((s) => s.slug)).toEqual(["mac-bridge", "holst"]);
    expect(b.totalCalls).toBe(3);
  });
  it("бегущая строка: свежие видимые треды без сабагентов", () => {
    expect(b.ticker.map((t) => t.id)).toEqual(["t1", "t4", "t2"]);
  });
  it("пустой период — пустое табло", () => {
    const empty = buildBoard({ now, days, threads: [], projectNames: names, usage: {} });
    expect(empty).toMatchObject({ mvp: null, teams: [], ticker: [], totalThreads: 0 });
  });
});

const digests: ThreadDigest[] = [
  { id: "thr_a", title: "таверна", project: "bb", updatedAt: now, asks: ["сделай плагин", "почта user@example.com"], outcome: ["Готово: плагин стоит"] },
  { id: "thr_b", title: "онбординг", project: "bot", updatedAt: now, asks: ["почини онбординг"], outcome: ["Упёрлись в таймаут"] },
];
const wrap = (o: unknown) => JSON.stringify({ structured_output: o });

describe("обзор", () => {
  it("промпт нумерует треды T1…, несёт просьбы и итоги, без почт", () => {
    const p = overviewPrompt(3, digests, [{ slug: "holst", calls: 2 }]);
    expect(p).toContain("T1 · bb · «таверна»");
    expect(p).toContain("ASK: сделай плагин");
    expect(p).toContain("OUT: Упёрлись в таймаут");
    expect(p).toContain("[почта]");
    expect(p).not.toContain("user@example.com");
    expect(p).toContain("holst (2)");
  });

  it("ссылки T<n> превращаются в id, выдуманные выкидываются, статус проверяется", () => {
    const o = parseOverview(
      wrap({
        summary: "Собрали таверну.",
        themes: [
          { title: "Таверна", project: "bb", status: "done", what: "Плагин стоит", refs: ["T1", "T9", "T1"] },
          { title: "Онбординг", status: "weird", what: "Таймаут", refs: ["t2"] },
          { title: "", status: "done", what: "пусто", refs: [] },
        ],
        next: [{ text: "Проверить таймаут", why: "застряли", ref: "T2" }, { text: "Без треда", why: "просто", ref: "" }],
      }),
      digests,
    );
    expect(o.themes).toHaveLength(2);
    expect(o.themes[0]!.threadIds).toEqual(["thr_a"]);
    expect(o.themes[1]).toMatchObject({ status: "in_progress", threadIds: ["thr_b"] });
    expect(o.next.map((n) => n.threadId)).toEqual(["thr_b", null]);
  });

  it("пустой обзор и ошибка модели — исключение", () => {
    expect(() => parseOverview(wrap({ summary: "", themes: [], next: [] }), digests)).toThrow();
    expect(() => parseOverview(JSON.stringify({ is_error: true, result: "budget" }), digests)).toThrow(/budget/);
    expect(() => parseOverview("не json", digests)).toThrow();
  });
});

describe("редактура перед моделью", () => {
  it("почты и телефоны вырезаются, даты и номера тикетов остаются", () => {
    expect(redact("доступ к календарю someone@example.com?")).toBe("доступ к календарю [почта]?");
    expect(redact("позвони +7 (999) 123-45-67")).toBe("позвони [телефон]");
    expect(redact("04.11 | PROJ-1485 | bb")).toBe("04.11 | PROJ-1485 | bb");
    expect(redact("отчёт 2026-11-04 и 1793789605993")).toBe("отчёт 2026-11-04 и [телефон]");
    // ревью: номер в конце предложения
    expect(redact("тел 89161234567.")).toBe("тел [телефон].");
    expect(redact("звони 8-916-123-45-67.")).toBe("звони [телефон].");
    expect(redact("+7 (916) 123 45 67.")).toBe("[телефон].");
  });
});

describe("окно табло", () => {
  it("треды считаются по тем же московским суткам, что и вызовы", () => {
    const at = Date.parse("2026-11-03T21:05:00Z"); // 00:05 МСК 4 ноября
    const t = [{ id: "x", title: "вчера вечером", projectId: "p1", updatedAt: at - 2 * 3_600_000, hidden: false, child: false }];
    const b = buildBoard({ now: at, days: ["2026-11-04"], threads: t, projectNames: names, usage: {} });
    expect(b.totalThreads).toBe(0);
  });
});
