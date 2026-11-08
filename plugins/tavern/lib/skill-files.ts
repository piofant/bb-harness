// Описания скиллов из SKILL.md на диске сервера bb.
import { readdir, readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

/** Каталоги, где живут скиллы агентов на этой машине. */
export const SKILL_DIRS = [".agents/skills", ".claude/skills", ".codex/skills", ".gemini/skills"].map((d) =>
  join(homedir(), d),
);

/** `description:` из YAML-фронтматтера; поддерживает и `>`/`|` блоки. */
export function parseDescription(text: string): string | null {
  const fm = /^---\r?\n([\s\S]*?)\r?\n---/.exec(text);
  if (!fm) return null;
  const lines = fm[1]!.split(/\r?\n/);
  const i = lines.findIndex((l) => /^description\s*:/.test(l));
  if (i < 0) return null;
  let value = lines[i]!.replace(/^description\s*:\s*/, "").trim();
  if (value === "" || /^[>|][-+]?$/.test(value)) {
    const block: string[] = [];
    for (const l of lines.slice(i + 1)) {
      if (!/^\s+/.test(l)) break;
      block.push(l.trim());
    }
    value = block.join(" ");
  }
  value = value.replace(/^["']|["']$/g, "").trim();
  return value === "" ? null : value;
}

export async function readSkillDescriptions(dirs: readonly string[]): Promise<Map<string, string | null>> {
  const out = new Map<string, string | null>();
  for (const dir of dirs) {
    let names: string[];
    try {
      names = await readdir(dir);
    } catch {
      continue;
    }
    for (const name of names) {
      const key = name.toLowerCase();
      if (out.get(key)) continue;
      try {
        // readFile идёт по симлинкам: ~/.claude/skills/* часто ссылки в ~/.agents.
        const text = await readFile(join(dir, name, "SKILL.md"), "utf8");
        out.set(key, parseDescription(text.slice(0, 8000)));
      } catch {
        // не каталог скилла
      }
    }
  }
  return out;
}
