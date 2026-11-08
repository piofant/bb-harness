import { describe, expect, it } from "vitest";
import { cardBody, parseGitLog, parseHealth, parseHints, parseInbox, parseIndex } from "../lib/kb";

describe("индекс", () => {
  it("берёт карточки разделов, пропускает приватные, чужие пути и мусор", () => {
    const tsv = [
      "name\tarea\ttype\tstatus\tpath\tdescription\tkeys\tverified\treview_after\tsensitivity",
      "deploy-steps\tinfra\tfact\tactive\tcards/infra/deploy-steps.md\tКак выкатывать\tk\t\t\tinternal",
      "secret\tinfra\tfact\tactive\tcards/infra/secret.md\tтайна\tk\t\t\tprivate",
      "evil\tagents\tfact\tactive\tcards/../../etc/passwd.md\tx\tk\t\t\tinternal",
      "tavern\tprojects/skills-tavern\tfact\tactive\tcards/projects/skills-tavern/tavern.md\tТаверна\tk\t\t\tinternal",
      "passwords\tinfra\tfact\tactive\tcards/infra/passwords.md\tПароли\tk\t\t\trestricted",
      "odd\tprivate\tfact\tactive\tcards/private/odd.md\tx\tk\t\t\tinternal",
      "",
    ].join("\n");
    expect(parseIndex(tsv).map((c) => [c.name, c.area])).toEqual([["deploy-steps", "infra"], ["tavern", "projects"]]);
  });
});

describe("что читают", () => {
  it("считает подсказки по карточкам в окне, свежие сверху", () => {
    const jsonl = [
      '{"ts":"2026-11-05T10:00:00Z","status":"hint","cards":["a","b"]}',
      '{"ts":"2026-11-05T12:00:00Z","status":"hint","cards":["a"]}',
      '{"ts":"2026-11-01T12:00:00Z","status":"hint","cards":["old"]}',
      '{"ts":"2026-11-05T12:30:00Z","status":"skip","cards":[]}',
      "{битая строка hint",
    ].join("\n");
    const r = parseHints(jsonl, Date.parse("2026-11-04T00:00:00Z"));
    expect(r.map((x) => [x.name, x.count])).toEqual([["a", 2], ["b", 1]]);
  });
});

describe("новое", () => {
  it("git log: одна запись на карточку, «добавлена» важнее «обновлена», служебные _index пропускаются", () => {
    const log = [
      "@1793800000 index: пересборка",
      "M\tcards/agents/_index.md",
      "@1793790000 card: x",
      "M\tcards/agents/x.md",
      "@1793700000 card: x",
      "A\tcards/agents/x.md",
      "A\tcards/infra/deploy-steps.md",
      "R100\tcards/infra/old.md\tcards/infra/renamed.md",
    ].join("\n");
    const c = parseGitLog(log);
    expect(c).toEqual([
      { name: "x", area: "agents", kind: "added", at: 1793790000000 },
      { name: "deploy-steps", area: "infra", kind: "added", at: 1793700000000 },
      { name: "renamed", area: "infra", kind: "updated", at: 1793700000000 },
    ]);
  });

  it("заметки во входящих: без строки источника", () => {
    const md = "# Заметки\n\n## 2026-11-05 05:15Z · infra · n-abc123\nДиск забит.\nисточник: сессия x\n## мусор\n";
    expect(parseInbox(md)).toEqual([{ id: "n-abc123", area: "infra", at: Date.parse("2026-11-05T05:15:00Z"), text: "Диск забит." }]);
  });
});

describe("здоровье и карточка", () => {
  it("здоровье: статус и проблемы, битый файл — unknown", () => {
    expect(parseHealth('{"status":"red","generated_at":"2026-11-05T19:04:26Z","problems":[{"level":"red","what":"диск"}]}')).toMatchObject({
      status: "red",
      problems: [{ level: "red", what: "диск" }],
    });
    expect(parseHealth("{").status).toBe("unknown");
    // ревью: одна битая запись не обнуляет весь отчёт
    expect(parseHealth('{"status":"yellow","problems":[null,{"level":"yellow","what":"lint"}]}')).toMatchObject({ status: "yellow", problems: [{ what: "lint" }] });
  });
  it("тело карточки без фронтматтера", () => {
    expect(cardBody("---\nname: x\n---\n**Суть.** Текст")).toBe("**Суть.** Текст");
  });
});
