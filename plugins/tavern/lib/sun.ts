// Солнце над Москвой: высота над горизонтом, рассвет и закат.
//
// Чистая математика без DOM и без сети — по ней таверна решает, светло в зале
// или пора зажигать очаг. Формулы — упрощённый алгоритм NOAA: точности до
// минуты хватает, а таблицы и внешние API для часов в углу не нужны.

export const MOSCOW = { lat: 55.7558, lon: 37.6173, tz: "Europe/Moscow" } as const;

const RAD = Math.PI / 180;
const DAY_MS = 86_400_000;

/** Юлианский век от J2000 — базовая единица формул NOAA. */
function julianCentury(ms: number): number {
  return (ms / DAY_MS + 2440587.5 - 2451545) / 36525;
}

/** Склонение солнца (градусы) и уравнение времени (минуты) на момент. */
function solarPosition(ms: number): { decl: number; eqTime: number } {
  const t = julianCentury(ms);
  const l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360;
  const m = 357.52911 + t * (35999.05029 - 0.0001537 * t);
  const e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t);
  const c =
    Math.sin(m * RAD) * (1.914602 - t * (0.004817 + 0.000014 * t)) +
    Math.sin(2 * m * RAD) * (0.019993 - 0.000101 * t) +
    Math.sin(3 * m * RAD) * 0.000289;
  const trueLong = l0 + c;
  const omega = 125.04 - 1934.136 * t;
  const lambda = trueLong - 0.00569 - 0.00478 * Math.sin(omega * RAD);
  const eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60;
  const eps = eps0 + 0.00256 * Math.cos(omega * RAD);
  const decl = Math.asin(Math.sin(eps * RAD) * Math.sin(lambda * RAD)) / RAD;
  const y = Math.tan((eps / 2) * RAD) ** 2;
  const eqTime =
    4 *
    (y * Math.sin(2 * l0 * RAD) -
      2 * e * Math.sin(m * RAD) +
      4 * e * y * Math.sin(m * RAD) * Math.cos(2 * l0 * RAD) -
      0.5 * y * y * Math.sin(4 * l0 * RAD) -
      1.25 * e * e * Math.sin(2 * m * RAD)) /
    RAD;
  return { decl, eqTime };
}

/** Высота солнца над горизонтом в градусах; отрицательная — солнце под ним. */
export function sunAltitude(ms: number, lat: number = MOSCOW.lat, lon: number = MOSCOW.lon): number {
  const { decl, eqTime } = solarPosition(ms);
  const d = new Date(ms);
  const utcMin = d.getUTCHours() * 60 + d.getUTCMinutes() + d.getUTCSeconds() / 60;
  const trueSolarMin = (((utcMin + eqTime + 4 * lon) % 1440) + 1440) % 1440;
  const hourAngle = trueSolarMin / 4 - 180;
  const cosZenith =
    Math.sin(lat * RAD) * Math.sin(decl * RAD) +
    Math.cos(lat * RAD) * Math.cos(decl * RAD) * Math.cos(hourAngle * RAD);
  return 90 - Math.acos(Math.min(1, Math.max(-1, cosZenith))) / RAD;
}

export interface SunTimes {
  /** Рассвет и закат в миллисекундах; null в полярный день или ночь. */
  sunrise: number | null;
  sunset: number | null;
}

/** Москва живёт в UTC+3 круглый год, без перехода на летнее время. */
const MOSCOW_OFFSET_MS = 3 * 3_600_000;

/**
 * Рассвет и закат в московские календарные сутки, в которые попадает `ms`.
 * Дату берём по Москве, а не по UTC: в 00:30 по Москве в UTC ещё вчера,
 * и часы показали бы вчерашний рассвет.
 */
export function sunTimes(ms: number, lat: number = MOSCOW.lat, lon: number = MOSCOW.lon): SunTimes {
  const d = new Date(ms + MOSCOW_OFFSET_MS);
  const noonUtc = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 12);
  const { decl, eqTime } = solarPosition(noonUtc);
  // 90.833° — зенит с поправкой на рефракцию и радиус диска.
  const cosH =
    Math.cos(90.833 * RAD) / (Math.cos(lat * RAD) * Math.cos(decl * RAD)) -
    Math.tan(lat * RAD) * Math.tan(decl * RAD);
  if (cosH > 1 || cosH < -1) return { sunrise: null, sunset: null };
  const h = Math.acos(cosH) / RAD;
  const midnight = noonUtc - 12 * 3_600_000;
  const solarNoonMin = 720 - 4 * lon - eqTime;
  return {
    sunrise: midnight + (solarNoonMin - 4 * h) * 60_000,
    sunset: midnight + (solarNoonMin + 4 * h) * 60_000,
  };
}

export type DayPhase = "night" | "dawn" | "day" | "dusk";

/**
 * Время суток по высоте солнца. Сумерки — гражданские, от −6° до +6°:
 * в это окно зал тёплый, свечи уже горят, а снаружи ещё видно.
 * Утро от вечера отличаем по тому, растёт солнце или садится.
 */
export function dayPhase(ms: number): DayPhase {
  const alt = sunAltitude(ms);
  if (alt >= 6) return "day";
  if (alt <= -6) return "night";
  return sunAltitude(ms + 600_000) > alt ? "dawn" : "dusk";
}

export interface Lighting {
  phase: DayPhase;
  /** 0 — полдень, 1 — глухая ночь: насколько темнить сцену. */
  darkness: number;
  /** 0..1 — тёплый закатный/рассветный оттенок. */
  warmth: number;
  /** 0..1 — насколько ярко горят очаг и свечи. */
  candles: number;
}

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));

/** Плавный свет без ступенек: темнота растёт от +8° до −12° высоты солнца. */
export function lighting(ms: number): Lighting {
  const alt = sunAltitude(ms);
  const darkness = clamp01((8 - alt) / 20);
  const warmth = clamp01(1 - Math.abs(alt - 1) / 9);
  const candles = clamp01((4 - alt) / 10);
  return { phase: dayPhase(ms), darkness, warmth, candles };
}

/** Часы и минуты в московском времени, «07:41». */
export function formatMoscow(ms: number): string {
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone: MOSCOW.tz,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(ms);
}

/** Часы и минуты по Москве числами — для стрелок циферблата. */
export function moscowClock(ms: number): { h: number; m: number; s: number } {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: MOSCOW.tz,
    hour: "numeric",
    minute: "numeric",
    second: "numeric",
    hourCycle: "h23",
  }).formatToParts(ms);
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? 0);
  return { h: get("hour"), m: get("minute"), s: get("second") };
}
