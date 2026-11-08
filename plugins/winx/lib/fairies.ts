// Шесть фей и какой день каждой подходит. Картинки — оригинальные иллюстрации
// в духе мультфильма (assets/fairies), не кадры из него.

export const FAIRY_IDS = ["bloom", "stella", "flora", "musa", "tecna", "aisha"] as const;
export type FairyId = (typeof FAIRY_IDS)[number];

export interface Fairy {
  id: FairyId;
  name: string;
  power: string;
  /** Цвет карточки. */
  color: string;
  /** Какой день — её день. Это же уходит модели как правило выбора. */
  day: string;
}

export const FAIRIES: Record<FairyId, Fairy> = {
  bloom: {
    id: "bloom",
    name: "Блум",
    power: "Пламя дракона",
    color: "#ff7a3d",
    day: "много всего сразу, тушила пожары, тянула на себе, решительно лезла в самое сложное",
  },
  stella: {
    id: "stella",
    name: "Стелла",
    power: "Сияние солнца и луны",
    color: "#f5c242",
    day: "красота и подача: деки, визуал, дизайн, презентации, чтобы всё блестело",
  },
  flora: {
    id: "flora",
    name: "Флора",
    power: "Сила природы",
    color: "#6fbf73",
    day: "забота о себе и людях: личное, отношения, психология, дневник, спокойный рост",
  },
  musa: {
    id: "musa",
    name: "Муза",
    power: "Магия музыки",
    color: "#8e6bd8",
    day: "ритм и слова: тексты, посты, музыка, видео, монтаж, юмор, творчество",
  },
  tecna: {
    id: "tecna",
    name: "Текна",
    power: "Магия технологий",
    color: "#38b6c9",
    day: "код, серверы, отладка, инфраструктура, плагины, аналитика и цифры",
  },
  aisha: {
    id: "aisha",
    name: "Лейла",
    power: "Сила волн",
    color: "#2fa58f",
    day: "потоки и движение: автоматизации, процессы, коммуникация с людьми, много переключений, гибкость",
  },
};

/** Фея по дате — когда в bb за день ничего не было. Стабильна в течение дня. */
export function fairyByDate(day: string): FairyId {
  let h = 0;
  for (const ch of day) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return FAIRY_IDS[h % FAIRY_IDS.length]!;
}
