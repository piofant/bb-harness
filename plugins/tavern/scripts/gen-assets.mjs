#!/usr/bin/env node
// Упаковывает зал и спрайты из assets/ в lib/assets.generated.ts как data:-URI.
// Картинки маленькие (≈200 КБ на всё), а так фронтенду не нужен HTTP-маршрут
// и токен: бандл самодостаточен. Запуск: node scripts/gen-assets.mjs
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const uri = (path) => `data:image/png;base64,${readFileSync(path).toString("base64")}`;
const sprites = Object.fromEntries(
  readdirSync(join(root, "assets/sprites"))
    .filter((f) => f.endsWith(".png"))
    .sort()
    .map((f) => [f.slice(0, -4), uri(join(root, "assets/sprites", f))]),
);
const out = [
  "// Сгенерировано scripts/gen-assets.mjs — не править руками.",
  "// Ninja Adventure Asset Pack (pixel-boy), CC0: assets/LICENSE-ninja-adventure.md",
  `export const HALL_PNG = ${JSON.stringify(uri(join(root, "assets/hall.png")))};`,
  `export const SPRITE_PNG: Record<string, string> = ${JSON.stringify(sprites, null, 1)};`,
  "",
].join("\n");
writeFileSync(join(root, "lib/assets.generated.ts"), out);
console.log(`assets: hall + ${Object.keys(sprites).length} sprites, ${(out.length / 1024).toFixed(0)} KB`);
