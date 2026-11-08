#!/usr/bin/env node
// Собирает карту библиотеки из тайлов Ninja Adventure (CC0) — тем же способом,
// что и зал таверны (pioblog-source/scripts/build-hall.mjs): композиция —
// массив координат в коде. Пять разделов базы — пять стеллажей вдоль стены,
// в середине читальные столы.
// Запуск: node scripts/build-library.mjs <каталог с тайлсетами>
//   (тайлсеты Ninja Adventure, CC0: https://pixel-boy.itch.io/ninja-adventure-asset-pack)
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const TILES = process.argv[2] ?? process.env.TILESETS;
if (!TILES) throw new Error("укажите каталог тайлсетов Ninja Adventure: node scripts/build-library.mjs <каталог>");
const OUT = join(root, "assets/library.png");
export const MAP = { width: 416, height: 256 };

const PARTS = {
  floor: ["TilesetInteriorFloor.png", "16x16+240+210"],
  rug: ["TilesetElement.png", "48x48+208+64"],
  counter: ["TilesetElement.png", "48x16+176+16"],
  shelf: ["TilesetElement.png", "32x32+128+112"],
  plant: ["TilesetElement.png", "16x24+0+120"],
  pot: ["TilesetElement.png", "16x16+16+0"],
  crate: ["TilesetElement.png", "16x16+96+0"],
};

// Стеллажи разделов: по два шкафа на раздел, x — левый край пары.
export const SECTION_X = [12, 94, 176, 258, 340];
const LAYOUT = [
  ...SECTION_X.flatMap((x) => [["shelf", x, 20], ["shelf", x + 32, 20]]),
  // читальные столы
  ["counter", 40, 124], ["counter", 184, 124], ["counter", 328, 124],
  ["counter", 112, 184], ["counter", 256, 184],
  // ковёр и мелочи
  ["rug", 184, 196],
  ["plant", 0, 220], ["plant", 400, 220], ["plant", 90, 120], ["plant", 310, 120],
  ["pot", 236, 126], ["crate", 20, 186], ["crate", 380, 186],
];

function im(args) {
  execFileSync("convert", args, { stdio: ["ignore", "pipe", "pipe"] });
}

for (const [name, [file]] of Object.entries(PARTS)) {
  if (!existsSync(join(TILES, file))) throw new Error(`нет тайлсета ${file} для «${name}»`);
}
const tmp = mkdtempSync(join(tmpdir(), "library-"));
try {
  for (const [name, [file, crop]] of Object.entries(PARTS)) {
    im([join(TILES, file), "-crop", crop, "+repage", join(tmp, `${name}.png`)]);
  }
  im(["-size", `${MAP.width}x${MAP.height}`, `tile:${join(tmp, "floor.png")}`, join(tmp, "floor-band.png")]);
  const args = ["-size", `${MAP.width}x${MAP.height}`, "xc:#241c15", join(tmp, "floor-band.png"), "-geometry", "+0+0", "-composite"];
  for (const [part, x, y] of LAYOUT) args.push(join(tmp, `${part}.png`), "-geometry", `+${x}+${y}`, "-composite");
  args.push(OUT);
  im(args);
  console.log(`library: ${MAP.width}×${MAP.height}, кусков ${LAYOUT.length} → assets/library.png`);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
