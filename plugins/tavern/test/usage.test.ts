import { describe, expect, it } from "vitest";
import { callsIn, lastDays, moscowDay, normalizeSkill, pruneUsage, rankSkills, recordUse, skillsFromItem, type UsageMap } from "../lib/usage";

describe("какие скиллы в ленте", () => {
  it("Claude: инструмент Skill, префикс плагина отрезаем", () => {
    expect(skillsFromItem({ type: "toolCall", tool: "Skill", arguments: { skill: "bb-global-skills:automations" } })).toEqual(["automations"]);
    expect(skillsFromItem({ type: "toolCall", tool: "Skill", arguments: { skill: "mac-bridge", args: "x" } })).toEqual(["mac-bridge"]);
  });

  it("Codex: чтение SKILL.md из каталога скиллов", () => {
    expect(skillsFromItem({ type: "fileRead", path: "/home/me/.agents/skills/holst/SKILL.md" })).toEqual(["holst"]);
    expect(skillsFromItem({ type: "fileRead", path: "/home/me/.claude/plugins/cache/x/skills/dataviz/SKILL.md" })).toEqual(["dataviz"]);
  });

  it("чужой репозиторий засчитываем, только если скилл установлен", () => {
    const item = { type: "fileRead", path: "/tmp/scout/repo/skills/github-triage/SKILL.md" };
    expect(skillsFromItem(item)).toEqual([]);
    expect(skillsFromItem(item, new Set(["github-triage"]))).toEqual(["github-triage"]);
  });

  it("команда: чтение засчитываем, запись — нет", () => {
    expect(skillsFromItem({ type: "commandExecution", command: "sed -n 1,200p /home/me/.agents/skills/ru-proxy/SKILL.md" })).toEqual(["ru-proxy"]);
    expect(skillsFromItem({ type: "commandExecution", command: "cat > /home/me/.agents/skills/x/SKILL.md <<EOF" })).toEqual([]);
    expect(skillsFromItem({ type: "commandExecution", command: "sed -i s/a/b/ /home/me/.agents/skills/x/SKILL.md" })).toEqual([]);
  });

  it("ревью: stderr-редирект не мешает, grep по нескольким SKILL.md — не вызов", () => {
    expect(skillsFromItem({ type: "commandExecution", command: "cat ~/.agents/skills/holst/SKILL.md 2>/dev/null" })).toEqual(["holst"]);
    expect(
      skillsFromItem({ type: "commandExecution", command: "grep -l foo /home/me/.agents/skills/a/SKILL.md /home/me/.agents/skills/b/SKILL.md | head" }),
    ).toEqual([]);
    expect(skillsFromItem({ type: "commandExecution", command: "ls x && sed -n 1,80p /home/me/.agents/skills/demo-skill/SKILL.md" })).toEqual(["demo-skill"]);
  });

  it("мусор и чужие типы не роняют разбор", () => {
    expect(skillsFromItem(null)).toEqual([]);
    expect(skillsFromItem({ type: "agentMessage", text: "/home/me/.agents/skills/x/SKILL.md" })).toEqual([]);
    expect(skillsFromItem({ type: "toolCall", tool: "Skill", arguments: {} })).toEqual([]);
    expect(normalizeSkill("../etc/passwd")).toBeNull();
  });
});

describe("раскладка: работают и отдыхают", () => {
  const now = Date.UTC(2026, 10, 4, 12);
  const h = 3_600_000;
  function usage(entries: Array<[string, number]>): UsageMap {
    const map: UsageMap = {};
    for (const [name, hoursAgo] of entries) recordUse(map, name, now - hoursAgo * h, "thr_" + name);
    return map;
  }
  const opts = { now, activeWindowHours: 3, minActive: 5, maxActive: 12, maxResting: 10 };

  it("в окне двое — работают пятеро самых свежих, остальные отдыхают", () => {
    const map = usage([["a", 1], ["b", 2], ["c", 5], ["d", 9], ["e", 20], ["f", 30], ["g", 40]]);
    const r = rankSkills(map, opts);
    expect(r.working.map((x) => x.name)).toEqual(["a", "b", "c", "d", "e"]);
    expect(r.resting.map((x) => x.name)).toEqual(["f", "g"]);
  });

  it("в окне много — работают все из окна, но не больше потолка", () => {
    const map = usage(Array.from({ length: 20 }, (_, i) => [`s${i}`, i * 0.1] as [string, number]));
    const r = rankSkills(map, opts);
    expect(r.working).toHaveLength(12);
    expect(r.resting).toHaveLength(8);
  });

  it("отдыхающих не больше десяти, трактирщик исключён", () => {
    const map = usage(Array.from({ length: 40 }, (_, i) => [`s${i}`, 10 + i] as [string, number]));
    const r = rankSkills(map, { ...opts, exclude: new Set(["s0"]) });
    expect(r.working.map((x) => x.name)).not.toContain("s0");
    expect(r.working).toHaveLength(5);
    expect(r.resting).toHaveLength(10);
  });

  it("повторный вызов: счётчик растёт, тред — от самого свежего", () => {
    const map: UsageMap = {};
    recordUse(map, "x", 200, "thr_new");
    recordUse(map, "x", 100, "thr_old");
    expect(map.x).toMatchObject({ last: 200, count: 2, threadId: "thr_new", days: { "1970-01-01": 2 } });
    expect(pruneUsage(map, 300)).toEqual({});
  });
});

describe("вызовы по дням", () => {
  it("день считается по Москве, а не по UTC", () => {
    expect(moscowDay(Date.parse("2026-11-03T22:30:00Z"))).toBe("2026-11-04");
    expect(lastDays(Date.parse("2026-11-04T09:00:00Z"), 3)).toEqual(["2026-11-04", "2026-11-03", "2026-11-02"]);
  });
  it("callsIn суммирует только выбранные дни, pruneUsage режет старые дни", () => {
    const map: UsageMap = {};
    recordUse(map, "x", Date.parse("2026-11-04T09:00:00Z"), "t");
    recordUse(map, "x", Date.parse("2026-11-01T09:00:00Z"), "t");
    expect(callsIn(map.x!, ["2026-11-04", "2026-11-03"])).toBe(1);
    const pruned = pruneUsage(map, Date.parse("2026-11-02T00:00:00Z"));
    expect(pruned.x!.days).toEqual({ "2026-11-04": 1 });
    // ревью: «за месяц» — сумма по оставшимся дням, а не вся жизнь
    expect(pruned.x!.count).toBe(1);
  });
});
