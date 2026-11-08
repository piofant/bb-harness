// Персонажи таверны: у каждого скилла имя и спрайт.
//
// Порядок: имя, которое пользователь дал сам в карточке → персонаж с
// piofant.github.io/skills (data/tavern-characters.json) → имя из словаря ниже
// → имя, собранное из слага. Спрайт — из персонажа таверны, иначе по хешу
// слага, а на сцене спрайты разводим, чтобы два одинаковых не стояли рядом.

import tavern from "../data/tavern-characters.json" with { type: "json" };

export const SPRITES = [
  "BlackSorcerer", "BlueNinja", "BlueSamurai", "DarkNinja", "Flam", "GoldKnight",
  "GrayNinja", "GreenCamouflage", "GreenNinja", "Inspector", "Knight", "Lion",
  "MaskedNinja", "Master", "Monk", "Monk2", "Noble", "OldMan", "OldMan2",
  "OrangeSorcerer", "RedCamouflage", "RedFighter", "RedSamurai", "Skeleton",
  "Spirit", "Villager2", "Villager3", "Woman",
] as const;
export type Sprite = (typeof SPRITES)[number];

interface TavernCharacter {
  name: string;
  sprite?: string | null;
  line?: string | null;
  power?: string | null;
}
const TAVERN = tavern as Record<string, TavernCharacter>;

/**
 * Имена для скиллов, которых нет в таверне на сайте, но которые часто зовут
 * в bb. Тот же голос, что у Тгши, Питчи и Календи: уменьшительное от сути
 * скилла, одно слово.
 */
export const NAMES: Record<string, string> = {
  "bb-plugin-authoring": "Плагинка",
  "bold-move": "Смельча",
  "idea-generation": "Идейка",
  "strategy-red-team": "Красноха",
  "fresh-reader": "Свежа",
  "skill-creator": "Мастеря",
  "skill-builder": "Мастеря",
  "chat-names": "Именя",
  dataviz: "Графя",
  secrets: "Тайна",
  "claude-auth-recovery": "Ключник",
  "claude-code-provider": "Клодя",
  "rytp-scenarist": "Пупа",
  browser: "Браузя",
  "ru-proxy": "Проксь",
  "advanced-autoresearch": "Ральфыч",
  "eval-forge": "Кузня",
  automations: "Будильня",
  "gsheets-report": "Табличка",
  "notion-cli": "Ноуша",
  "wiki-page": "Викуша",
  "joke-engineering": "Шутя",
  "agent-self-evaluation": "Самогляд",
  "code-review": "Ревьюша",
  "gemini-subagent": "Близня",
  "codex-provider": "Кодя",
  "white-pdf": "Белка",
  graphify: "Графоня",
  "session-vault": "Архивя",
  "bb-cli": "Бибиша",
  "google-calendar": "Гуглендя",
  "gws-calendar": "Гуглендя",
  "deep-thinking": "Думка",
  "frontend-design": "Рисуня",
  "user-story": "Сказя",
  "release-notes": "Релизя",
  "answer-first-report": "Ответя",
  "net-doctor": "Сетедок",
  "claude-api": "Апишка",
  "inline-vis": "Картиня",
  tracker: "Трекуша",
  puncher: "Панчер",
  recommendations: "Советя",
  "task-observer": "Смотря",
  "fan-clip-machine": "Нарезка",
  "tg-research": "Тгша",
  "telegram-projects": "Телеграня",
  "provider-retry": "Повторя",
  "concurrency-limit": "Очередя",
  "push-notifications": "Звоня",
  "share-server-links": "Ссылка",
  "update-config": "Настроя",
};

/** Транслит латиницы в кириллицу — грубый, ради узнаваемого корня. */
const TRANSLIT: Array<[RegExp, string]> = [
  [/sh/g, "ш"], [/ch/g, "ч"], [/zh/g, "ж"], [/kh/g, "х"], [/ts/g, "ц"],
  [/yu/g, "ю"], [/ya/g, "я"], [/yo/g, "ё"], [/ee/g, "и"], [/oo/g, "у"],
  [/th/g, "т"], [/ph/g, "ф"], [/ck/g, "к"], [/qu/g, "кв"], [/x/g, "кс"],
  [/a/g, "а"], [/b/g, "б"], [/c/g, "к"], [/d/g, "д"], [/e/g, "е"], [/f/g, "ф"],
  [/g/g, "г"], [/h/g, "х"], [/i/g, "и"], [/j/g, "дж"], [/k/g, "к"], [/l/g, "л"],
  [/m/g, "м"], [/n/g, "н"], [/o/g, "о"], [/p/g, "п"], [/q/g, "к"], [/r/g, "р"],
  [/s/g, "с"], [/t/g, "т"], [/u/g, "у"], [/v/g, "в"], [/w/g, "в"], [/y/g, "й"],
  [/z/g, "з"],
];

/** Служебные куски слага, из которых имя не делают. */
const NOISE = new Set([
  "bb", "skill", "skills", "agent",
  "the", "and", "for", "to", "of", "my", "gws", "ya", "cli", "v2", "v3",
]);

/** Стабильный 32-битный хеш строки (FNV-1a). */
export function hash(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

const SUFFIXES = ["ша", "ня", "ся", "чи", "ка", "ля"];

/**
 * Имя из слага в духе таверны: корень первого содержательного слова, обрезанный
 * до слога-двух, плюс ласкательный суффикс. `mac-bridge` → «Макся»,
 * `browser` → «Браузня». Детерминированно: одно и то же имя при каждой загрузке.
 */
export function inventName(slug: string): string {
  const words = slug.toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 1 && !NOISE.has(w));
  const word = words[0] ?? slug.replace(/[^a-z]/gi, "").toLowerCase();
  if (word === "") return "Безымянка";
  let ru = word;
  for (const [from, to] of TRANSLIT) ru = ru.replace(from, to);
  ru = ru.replace(/[^а-яё]/g, "");
  if (ru === "") return "Безымянка";
  // Корень до второй гласной включительно согласных за ней: «браузер» → «брауз».
  const m = /^[^аеёиоуыэюя]*[аеёиоуыэюя]+[^аеёиоуыэюя]*/.exec(ru);
  let root = (m?.[0] ?? ru).slice(0, 6);
  if (root.length < 3) root = ru.slice(0, 4);
  root = root.replace(/[йь]$/, "");
  const suffix = SUFFIXES[hash(slug) % SUFFIXES.length]!;
  const name = /[аеёиоуыэюя]$/.test(root) ? root.slice(0, -1) + suffix : root + suffix;
  return name.charAt(0).toUpperCase() + name.slice(1);
}

export interface Persona {
  name: string;
  sprite: Sprite;
  /** Реплика или суперсила из таверны, если персонаж оттуда. */
  line: string | null;
  /** Откуда имя: своё, с сайта таверны, из словаря или придумано. */
  origin: "custom" | "tavern" | "named" | "invented";
}

function isSprite(value: unknown): value is Sprite {
  return typeof value === "string" && (SPRITES as readonly string[]).includes(value);
}

export function personaFor(slug: string, custom: Record<string, string> = {}): Persona {
  const t = TAVERN[slug];
  const sprite: Sprite = isSprite(t?.sprite) ? t.sprite : SPRITES[hash(slug) % SPRITES.length]!;
  const line = t?.line ?? t?.power ?? null;
  const own = custom[slug]?.trim();
  if (own) return { name: own, sprite, line, origin: "custom" };
  if (t?.name) return { name: t.name, sprite, line, origin: "tavern" };
  if (NAMES[slug]) return { name: NAMES[slug], sprite, line, origin: "named" };
  return { name: inventName(slug), sprite, line, origin: "invented" };
}

/**
 * Развести спрайты на сцене: первый по порядку держит свой, следующие с тем же
 * спрайтом сдвигаются на ближайший свободный. Двадцать восемь спрайтов на
 * двадцать с небольшим мест — хватает, при переполнении повторы допустимы.
 */
export function distinctSprites<T extends { sprite: Sprite }>(items: T[]): T[] {
  const used = new Set<Sprite>();
  return items.map((item) => {
    let sprite = item.sprite;
    if (used.has(sprite) && used.size < SPRITES.length) {
      let i = SPRITES.indexOf(sprite);
      do i = (i + 1) % SPRITES.length;
      while (used.has(SPRITES[i]!));
      sprite = SPRITES[i]!;
    }
    used.add(sprite);
    return sprite === item.sprite ? item : { ...item, sprite };
  });
}
