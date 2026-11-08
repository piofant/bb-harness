// Чтение базы знаний агентов (каталог из настройки «Каталог базы знаний»): чистые разборщики файлов.
// Плагин не ходит в bb за историей сессий — всё, что нужно, база пишет сама.

// Разделы базы — первая часть колонки area в INDEX.tsv. Пять стеллажей на
// карте; другие разделы — поменяйте список и подписи здесь.
export const AREAS = ["agents", "infra", "projects", "tools", "notes"] as const;
export type Area = (typeof AREAS)[number];

/** Раздел — стеллаж, у стеллажа свой библиотекарь. */
export const AREA_INFO: Record<Area, { label: string; keeper: string; sprite: string }> = {
  agents: { label: "Агенты", keeper: "Агентя", sprite: "BlueNinja" },
  infra: { label: "Инфра", keeper: "Серверыч", sprite: "OldMan" },
  projects: { label: "Проекты", keeper: "Проектя", sprite: "Noble" },
  tools: { label: "Инструменты", keeper: "Инструмент", sprite: "Inspector" },
  notes: { label: "Заметки", keeper: "Записка", sprite: "Woman" },
};

export const isArea = (v: string): v is Area => (AREAS as readonly string[]).includes(v);

export interface Card {
  name: string;
  area: Area;
  type: string;
  status: string;
  path: string;
  description: string;
}

/** INDEX.tsv → карточки. Приватный раздел и битые строки пропускаем. */
export function parseIndex(tsv: string): Card[] {
  const [header, ...rows] = tsv.split("\n");
  const cols = (header ?? "").split("\t");
  const at = (name: string) => cols.indexOf(name);
  const out: Card[] = [];
  for (const row of rows) {
    if (!row.trim()) continue;
    const f = row.split("\t");
    // У проектов раздел записан как «projects/<проект>» — стеллаж один.
    const area = (f[at("area")] ?? "").split("/")[0]!;
    const name = f[at("name")] ?? "";
    const path = f[at("path")] ?? "";
    if (!isArea(area) || !name || !/^cards\/[\w./-]+\.md$/.test(path) || path.includes("..")) continue;
    // Показываем только обычные карточки: «restricted» (логины, контакты,
    // люди) в библиотеку не попадают и не открываются.
    const sensitivity = f[at("sensitivity")] ?? "";
    if (sensitivity !== "internal" && sensitivity !== "") continue;
    out.push({
      name,
      area,
      type: f[at("type")] ?? "",
      status: f[at("status")] ?? "",
      path,
      description: f[at("description")] ?? "",
    });
  }
  return out;
}

export interface Read {
  name: string;
  count: number;
  last: number;
}

/**
 * Что читают агенты: подсказки из state/kb-hint.jsonl — какие карточки хук
 * подложил в сессию на запрос пользователя. Свежие сверху.
 */
export function parseHints(jsonl: string, since: number): Read[] {
  const map = new Map<string, Read>();
  for (const line of jsonl.split("\n")) {
    if (!line.includes('"hint"')) continue;
    let d: { ts?: string; status?: string; cards?: unknown };
    try {
      d = JSON.parse(line);
    } catch {
      continue;
    }
    if (d.status !== "hint" || !Array.isArray(d.cards)) continue;
    const ts = Date.parse(d.ts ?? "");
    if (!Number.isFinite(ts) || ts < since) continue;
    for (const c of d.cards) {
      if (typeof c !== "string") continue;
      const r = map.get(c) ?? { name: c, count: 0, last: 0 };
      r.count += 1;
      r.last = Math.max(r.last, ts);
      map.set(c, r);
    }
  }
  return [...map.values()].sort((a, b) => b.last - a.last || b.count - a.count);
}

export interface Change {
  name: string;
  area: Area;
  kind: "added" | "updated";
  at: number;
}

/**
 * `git log --name-status --pretty='@%ct %s' -- cards` → новые и обновлённые
 * карточки, по одной записи на карточку (самое свежее изменение).
 */
export function parseGitLog(out: string): Change[] {
  const seen = new Map<string, Change>();
  let at = 0;
  for (const line of out.split("\n")) {
    const head = /^@(\d+) /.exec(line);
    if (head) {
      at = Number(head[1]) * 1000;
      continue;
    }
    // A и M — путь сразу; R<процент> — «старый\tновый», берём новый как обновление.
    const m = /^([AM]|R\d*)\t(?:[^\t]+\t)?(cards\/([\w-]+)\/(?:[\w-]+\/)*([\w.-]+)\.md)$/.exec(line);
    if (!m || m[4]!.startsWith("_") || !isArea(m[3]!)) continue;
    const name = m[4]!;
    const prev = seen.get(name);
    const kind = m[1] === "A" ? "added" : "updated";
    // git log идёт от новых к старым: первое — самое свежее, но «добавлена»
    // важнее «обновлена», если карточка родилась в этом же окне.
    if (!prev) seen.set(name, { name, area: m[3] as Area, kind, at });
    else if (kind === "added") prev.kind = "added";
  }
  return [...seen.values()].sort((a, b) => b.at - a.at);
}

export interface Note {
  id: string;
  area: string;
  at: number;
  text: string;
}

/** Заметки агентов из raw/inbox/<машина>/<дата>.md: «## дата время · раздел · id». */
export function parseInbox(md: string): Note[] {
  const out: Note[] = [];
  const blocks = md.split(/^## /m).slice(1);
  for (const b of blocks) {
    const [head, ...rest] = b.split("\n");
    const m = /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})Z · ([\w-]+) · (n-[\w]+)/.exec(head ?? "");
    if (!m) continue;
    const text = rest
      .filter((l) => !l.startsWith("источник:"))
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
    out.push({ id: m[4]!, area: m[3]!, at: Date.parse(`${m[1]}T${m[2]}:00Z`), text });
  }
  return out;
}

export interface Health {
  status: "green" | "yellow" | "red" | "unknown";
  at: number | null;
  problems: Array<{ level: string; what: string }>;
}

export function parseHealth(json: string): Health {
  try {
    const d = JSON.parse(json) as { status?: string; generated_at?: string; problems?: unknown };
    const status = d.status === "green" || d.status === "yellow" || d.status === "red" ? d.status : "unknown";
    const problems = Array.isArray(d.problems)
      ? d.problems
          .filter((p: unknown): p is { level?: unknown; what?: unknown } => typeof p === "object" && p !== null)
          .map((p) => ({ level: String(p.level ?? ""), what: String(p.what ?? "") }))
          .filter((p) => p.what)
      : [];
    const at = Date.parse(d.generated_at ?? "");
    return { status, at: Number.isFinite(at) ? at : null, problems };
  } catch {
    return { status: "unknown", at: null, problems: [] };
  }
}

/** Карточка без YAML-фронтматтера — для чтения в панели. */
export function cardBody(md: string): string {
  return md.replace(/^---\n[\s\S]*?\n---\n?/, "").trim();
}
