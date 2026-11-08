#!/usr/bin/env node
// Рисует шесть пиксельных фей в SVG: общий шаблон тела 16×21 пикселей,
// у каждой свои причёска, цвета, крылья и атрибут стихии. Оригинальная
// графика в стиле таверны, не кадры из мультфильма.
// Запуск: node scripts/gen-fairies.mjs → assets/fairies/*.svg + lib/art.generated.ts
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

// h — волосы, s — кожа, e — глаз, m — рот, k — румянец, d/D — платье и тень, b — туфли
const BODY = [
  "................",
  ".....hhhhhh.....",
  "....hhhhhhhh....",
  "...hhhhhhhhhh...",
  "...hhssssssh h..",
  "...hseessees h..",
  "...hseessees h..",
  "...hksssssskh...",
  "....sssmmsss....",
  "......ssss......",
  ".....dddddd.....",
  "....dddddddd....",
  "...sddddddddds..",
  "...s.dddddd.s...",
  "....DddddddD....",
  "...DddddddddD...",
  "..DDdddddddddD..",
  "..DDDDDDDDDDDD..",
  "......s..s......",
  "......s..s......",
  ".....bb..bb.....",
].map((r) => r.replace(/ /g, "h").padEnd(16, ".").slice(0, 16));

/** Причёски: какие клетки добавить волосами поверх шаблона. */
const HAIR = {
  long: (x, y) => (y >= 4 && y <= 15 && (x === 2 || x === 3 || x === 12 || x === 13)) || (y >= 13 && y <= 16 && x === 1),
  ponytail: (x, y) => (y >= 2 && y <= 13 && x === 13) || (y >= 3 && y <= 12 && x === 14),
  buns: (x, y) => (y >= 0 && y <= 2 && (x === 2 || x === 3 || x === 12 || x === 13)) || (y === 1 && (x === 1 || x === 14)),
  bob: (x, y) => y >= 4 && y <= 8 && (x === 2 || x === 13),
  curly: (x, y) => (y >= 2 && y <= 14 && (x === 1 || x === 2 || x === 13 || x === 14) && (x + y) % 2 === 0) || (y >= 3 && y <= 13 && (x === 2 || x === 13)),
  flowers: (x, y) => (y >= 4 && y <= 14 && (x === 2 || x === 3 || x === 12 || x === 13)),
};

const FAIRIES = {
  bloom: { hair: "#ff6a2b", hairD: "#c9431a", dress: "#3cc8e6", dressD: "#2394b0", wing: "#7fe3ff", bg: "#ff8a4c", style: "long", prop: "flame" },
  stella: { hair: "#ffd84a", hairD: "#e0a92a", dress: "#ff9d2e", dressD: "#d9741a", wing: "#fff2a8", bg: "#ffcf4a", style: "ponytail", prop: "sun" },
  flora: { hair: "#c98a4b", hairD: "#9a6430", dress: "#ff8ac4", dressD: "#e05c9e", wing: "#b8f5a6", bg: "#7fd27f", style: "flowers", prop: "flower" },
  musa: { hair: "#283a7a", hairD: "#18234f", dress: "#e0344a", dressD: "#a8202f", wing: "#c8b4ff", bg: "#9a78e6", style: "buns", prop: "notes" },
  tecna: { hair: "#c04ad8", hairD: "#8d2ca3", dress: "#7a5af0", dressD: "#5338c4", wing: "#9ff7d8", bg: "#3fc2d6", style: "bob", prop: "chip" },
  aisha: { hair: "#3a2418", hairD: "#22140c", dress: "#22c9a8", dressD: "#16957c", wing: "#9fe8ff", bg: "#33b39b", style: "curly", prop: "wave" },
};

const SKIN = { s: "#ffd9b8", k: "#ff9e9e", e: "#2a1a3a", m: "#c2405a", b: "#5a2a4a" };
const PX = 10; // размер пикселя в SVG
const OX = 44; // отступ тела от левого края, чтобы поместились крылья
const OY = 60;

function pixels(f) {
  const out = [];
  for (let y = 0; y < BODY.length; y += 1) {
    for (let x = 0; x < 16; x += 1) {
      let c = BODY[y][x];
      if (c === "." && HAIR[f.style](x, y)) c = "h";
      if (c === ".") continue;
      let fill =
        c === "h" ? (x + y) % 5 === 0 ? f.hairD : f.hair
        : c === "d" ? f.dress
        : c === "D" ? f.dressD
        : SKIN[c];
      // Флоре — цветы в волосах.
      if (f.style === "flowers" && c === "h" && (x * 7 + y * 3) % 11 === 0) fill = "#ff5fa2";
      out.push(`<rect x="${OX + x * PX}" y="${OY + y * PX}" width="${PX}" height="${PX}" fill="${fill}"/>`);
    }
  }
  // Блики в глазах.
  for (const ex of [5, 9]) out.push(`<rect x="${OX + ex * PX}" y="${OY + 5 * PX}" width="4" height="4" fill="#fff"/>`);
  return out.join("");
}

function wings(f) {
  const cx = OX + 8 * PX;
  const cy = OY + 12 * PX;
  const w = (dx, dy, rx, ry, rot) =>
    `<ellipse cx="${cx + dx}" cy="${cy + dy}" rx="${rx}" ry="${ry}" transform="rotate(${rot} ${cx + dx} ${cy + dy})" fill="${f.wing}" fill-opacity="0.55" stroke="#fff" stroke-opacity="0.8" stroke-width="2"/>`;
  return [w(-58, -30, 52, 30, -30), w(58, -30, 52, 30, 30), w(-46, 22, 34, 20, 25), w(46, 22, 34, 20, -25)].join("");
}

function prop(f) {
  const x = OX + 16 * PX + 8;
  const y = OY + 12 * PX;
  switch (f.prop) {
    case "flame":
      return `<path d="M${x} ${y + 20} q-18 -20 0 -52 q6 18 16 10 q8 22 -16 42z" fill="#ff7a1a"/><path d="M${x + 2} ${y + 16} q-8 -12 0 -26 q4 10 8 6 q2 12 -8 20z" fill="#ffe14a"/>`;
    case "sun": {
      const rays = Array.from({ length: 8 }, (_, i) => `<rect x="${x + 8}" y="${y - 38}" width="4" height="10" fill="#ffe14a" transform="rotate(${i * 45} ${x + 10} ${y - 10})"/>`).join("");
      return `${rays}<circle cx="${x + 10}" cy="${y - 10}" r="14" fill="#ffd23a" stroke="#ff9d2e" stroke-width="3"/><rect x="${x + 8}" y="${y + 4}" width="4" height="34" fill="#d99a2a"/>`;
    }
    case "flower":
      return [0, 72, 144, 216, 288]
        .map((a) => `<ellipse cx="${x + 10}" cy="${y - 22}" rx="7" ry="13" fill="#ff8ac4" transform="rotate(${a} ${x + 10} ${y - 10})"/>`)
        .join("") + `<circle cx="${x + 10}" cy="${y - 10}" r="7" fill="#ffe14a"/><rect x="${x + 8}" y="${y}" width="4" height="34" fill="#4caf50"/>`;
    case "notes":
      return `<g fill="#fff"><rect x="${x}" y="${y - 40}" width="4" height="30"/><rect x="${x}" y="${y - 40}" width="22" height="5"/><rect x="${x + 18}" y="${y - 40}" width="4" height="26"/><circle cx="${x - 4}" cy="${y - 10}" r="7"/><circle cx="${x + 14}" cy="${y - 14}" r="7"/><rect x="${x + 30}" y="${y + 2}" width="3" height="22"/><circle cx="${x + 26}" cy="${y + 24}" r="6"/></g>`;
    case "chip":
      return `<rect x="${x - 4}" y="${y - 40}" width="44" height="34" rx="3" fill="#0e3a4a" fill-opacity="0.85" stroke="#9ff7d8" stroke-width="2"/><g fill="#9ff7d8"><rect x="${x + 2}" y="${y - 32}" width="20" height="3"/><rect x="${x + 2}" y="${y - 25}" width="30" height="3"/><rect x="${x + 2}" y="${y - 18}" width="14" height="3"/></g>`;
    case "wave":
      return `<path d="M${x - 6} ${y} q12 -16 24 0 t24 0" stroke="#9fe8ff" stroke-width="6" fill="none" stroke-linecap="round"/><path d="M${x - 2} ${y - 18} q10 -14 20 0 t20 0" stroke="#e6fbff" stroke-width="4" fill="none" stroke-linecap="round"/><circle cx="${x + 30}" cy="${y - 34}" r="5" fill="#9fe8ff"/><circle cx="${x + 12}" cy="${y - 40}" r="3" fill="#e6fbff"/>`;
    default:
      return "";
  }
}

function sparkles(id) {
  let seed = [...id].reduce((s, c) => s * 31 + c.charCodeAt(0), 7) >>> 0;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) >>> 0) / 4294967296);
  return Array.from({ length: 14 }, () => {
    const x = 10 + rnd() * 280;
    const y = 10 + rnd() * 300;
    const r = 2 + rnd() * 4;
    return `<path d="M${x} ${y - r * 2} L${x + r / 2} ${y - r / 2} L${x + r * 2} ${y} L${x + r / 2} ${y + r / 2} L${x} ${y + r * 2} L${x - r / 2} ${y + r / 2} L${x - r * 2} ${y} L${x - r / 2} ${y - r / 2}z" fill="#fff" fill-opacity="${(0.5 + rnd() * 0.5).toFixed(2)}"/>`;
  }).join("");
}

function svg(id, f) {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 320" shape-rendering="crispEdges">
<defs><radialGradient id="g-${id}" cx="50%" cy="45%" r="70%"><stop offset="0" stop-color="#fff" stop-opacity="0.9"/><stop offset="0.45" stop-color="${f.bg}" stop-opacity="0.85"/><stop offset="1" stop-color="${f.bg}"/></radialGradient></defs>
<rect width="300" height="320" rx="20" fill="url(#g-${id})"/>
<g shape-rendering="geometricPrecision">${sparkles(id)}${wings(f)}</g>
${pixels(f)}
<g shape-rendering="geometricPrecision">${prop(f)}</g>
</svg>`;
}

mkdirSync(join(root, "assets/fairies"), { recursive: true });
const uris = {};
for (const [id, f] of Object.entries(FAIRIES)) {
  const text = svg(id, f);
  writeFileSync(join(root, "assets/fairies", `${id}.svg`), text);
  uris[id] = `data:image/svg+xml;base64,${Buffer.from(text).toString("base64")}`;
}
writeFileSync(
  join(root, "lib/art.generated.ts"),
  `// Сгенерировано scripts/gen-fairies.mjs — не править руками.\nexport const FAIRY_ART: Record<string, string> = ${JSON.stringify(uris, null, 1)};\n`,
);
console.log(`fairies: ${Object.keys(uris).length}`);
