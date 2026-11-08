import { describe, expect, it } from "vitest";
import { parseDescription } from "../lib/skill-files";

describe("описание из SKILL.md", () => {
  it("строка, кавычки, блок", () => {
    expect(parseDescription("---\nname: x\ndescription: Делает X.\n---\nbody")).toBe("Делает X.");
    expect(parseDescription('---\ndescription: "в кавычках"\n---')).toBe("в кавычках");
    expect(parseDescription("---\ndescription: >\n  первая\n  вторая\nname: y\n---")).toBe("первая вторая");
    expect(parseDescription("без фронтматтера")).toBeNull();
  });
});
