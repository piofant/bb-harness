// Сцена библиотеки (по образцу таверны): где кто стоит и куда идёт. Чистая логика без DOM — браузер
// в фоновой вкладке не даёт кадров, поэтому поведение проверяем тестами.
//
// Карта — hall.png с piofant.github.io/skills: 416×320, зал сверху (0–224),
// веранда снизу (224–320). Координата персонажа — точка под ногами.

export const MAP = { w: 416, h: 256 } as const;

export interface Point {
  x: number;
  y: number;
}

/** Читальные столы (assets/library.png): к ним библиотекарь несёт карточку. */
export const WORK_SPOTS: Point[] = [
  { x: 64, y: 152 },
  { x: 208, y: 152 },
  { x: 352, y: 152 },
  { x: 136, y: 212 },
  { x: 280, y: 212 },
];

/** Места у стеллажей разделов — дом библиотекаря. */
export const HOME_SPOTS: Point[] = [44, 126, 208, 290, 372].map((x) => ({ x, y: 74 }));

/** Отдыха в библиотеке нет — отдыхающий стоит у своего стеллажа. */
export const REST_SPOTS: Point[] = HOME_SPOTS;
export const INNKEEPER_SPOT: Point = { x: 244, y: 150 };

/** Колонки спрайт-листа — куда персонаж смотрит. */
export const DIR = { down: 0, up: 1, left: 2, right: 3 } as const;

/** Скорость ходьбы, пикселей карты в секунду. */
export const SPEED = 20;
/** Сколько работающих может идти одновременно — иначе зал похож на муравейник. */
export const MAX_WALKING = 3;

export type Role = "working" | "resting" | "innkeeper";
export type UnitState = "idle" | "toWork" | "working" | "toHome";

export interface Unit {
  key: string;
  role: Role;
  /** Тред персонажа крутится прямо сейчас — работает дольше и чаще. */
  busy: boolean;
  home: Point;
  work: Point;
  x: number;
  y: number;
  state: UnitState;
  /** Секунд до следующей смены состояния. */
  wait: number;
  dir: number;
  /** Кадр спрайта 0..3 и накопитель для его смены. */
  frame: number;
  step: number;
  /** Сдвиг фазы покачивания, чтобы не качались строем. */
  phase: number;
}

/** Детерминированная «случайность» от ключа и счётчика — сцена воспроизводима. */
export function noise(key: string, n: number): number {
  let h = 2166136261 ^ n;
  for (let i = 0; i < key.length; i += 1) h = Math.imul(h ^ key.charCodeAt(i), 16777619);
  h = Math.imul(h ^ (h >>> 15), 2246822507);
  h = Math.imul(h ^ (h >>> 13), 3266489909);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}

export function createUnit(key: string, role: Role, index: number, busy = false): Unit {
  const home =
    role === "innkeeper"
      ? INNKEEPER_SPOT
      : role === "resting"
        ? REST_SPOTS[index % REST_SPOTS.length]!
        : HOME_SPOTS[index % HOME_SPOTS.length]!;
  const work = role === "working" ? WORK_SPOTS[index % WORK_SPOTS.length]! : home;
  return {
    key,
    role,
    busy,
    home: { ...home },
    work: { ...work },
    x: home.x,
    y: home.y,
    state: "idle",
    // Первый выход разносим, чтобы зал не срывался с места разом.
    wait: 1 + noise(key, 0) * (busy ? 3 : 8),
    dir: DIR.down,
    frame: 0,
    step: 0,
    phase: noise(key, 1) * Math.PI * 2,
  };
}

/**
 * Пересобрать состав, сохранив позиции тех, кто уже на сцене: обновление
 * данных раз в полминуты не должно телепортировать персонажей.
 */
export function syncUnits(
  prev: readonly Unit[],
  cast: ReadonlyArray<{ key: string; role: Role; busy: boolean }>,
): Unit[] {
  const counters: Record<Role, number> = { working: 0, resting: 0, innkeeper: 0 };
  return cast.map((c) => {
    const index = counters[c.role]++;
    const fresh = createUnit(c.key, c.role, index, c.busy);
    const old = prev.find((u) => u.key === c.key);
    if (old === undefined) return fresh;
    if (old.role !== c.role) {
      // Сменил роль (ушёл на веранду или вернулся в зал) — идёт пешком.
      return { ...fresh, x: old.x, y: old.y, dir: old.dir, state: "toHome" };
    }
    // Тот же персонаж в той же роли: место могло смениться, позиция — нет.
    const keep = { ...old, busy: c.busy, home: fresh.home, work: fresh.work };
    if (keep.state === "idle" && (keep.x !== keep.home.x || keep.y !== keep.home.y)) {
      keep.state = "toHome";
    }
    return keep;
  });
}

function faceTowards(u: Unit, target: Point): void {
  const dx = target.x - u.x;
  const dy = target.y - u.y;
  u.dir = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? DIR.right : DIR.left) : dy > 0 ? DIR.down : DIR.up;
}

/** Шаг к цели; true — дошёл. */
function walk(u: Unit, target: Point, dt: number): boolean {
  const dx = target.x - u.x;
  const dy = target.y - u.y;
  const dist = Math.hypot(dx, dy);
  const step = SPEED * (u.busy ? 1.3 : 1) * dt;
  faceTowards(u, target);
  u.step += dt * 8;
  u.frame = Math.floor(u.step) % 4;
  if (dist <= step) {
    u.x = target.x;
    u.y = target.y;
    return true;
  }
  u.x += (dx / dist) * step;
  u.y += (dy / dist) * step;
  return false;
}

/**
 * Один шаг сцены на `dt` секунд. Возвращает, сколько персонажей идёт:
 * по нему ограничиваем поток.
 */
export function stepScene(units: Unit[], dt: number, cycle = 0): number {
  let walking = units.filter((u) => u.state === "toWork" || u.state === "toHome").length;
  for (const u of units) {
    if (u.role !== "working") {
      // Отдыхающие и трактирщик стоят на месте, к зрителю лицом.
      if (u.state !== "idle") {
        if (walk(u, u.home, dt)) {
          u.state = "idle";
          u.dir = DIR.down;
          u.frame = 0;
        }
      }
      continue;
    }
    switch (u.state) {
      case "idle":
        u.wait -= dt;
        if (u.wait <= 0) {
          if (walking >= MAX_WALKING) {
            u.wait = 0.5 + noise(u.key, cycle) * 1.5;
            break;
          }
          u.state = "toWork";
          walking += 1;
        }
        break;
      case "toWork":
        if (walk(u, u.work, dt)) {
          u.state = "working";
          walking -= 1;
          u.dir = DIR.up; // лицом к мебели
          u.wait = (u.busy ? 8 : 4) + noise(u.key, cycle + 7) * 6;
        }
        break;
      case "working":
        // Работа — переминание у станции: кадры ходьбы на месте, медленно.
        u.step += dt * 3;
        u.frame = Math.floor(u.step) % 4;
        u.wait -= dt;
        if (u.wait <= 0) {
          if (walking >= MAX_WALKING) {
            u.wait = 0.5;
            break;
          }
          u.state = "toHome";
          walking += 1;
        }
        break;
      case "toHome":
        if (walk(u, u.home, dt)) {
          u.state = "idle";
          walking -= 1;
          u.dir = DIR.down;
          u.frame = 0;
          u.wait = (u.busy ? 2 : 6) + noise(u.key, cycle + 13) * 10;
        }
        break;
    }
  }
  return walking;
}
