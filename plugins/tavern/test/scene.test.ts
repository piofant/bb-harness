import { describe, expect, it } from "vitest";
import { createUnit, HOME_SPOTS, MAP, MAX_WALKING, REST_SPOTS, stepScene, syncUnits, WORK_SPOTS } from "../lib/scene";

describe("сцена", () => {
  it("все места внутри своей зоны карты", () => {
    for (const p of [...WORK_SPOTS, ...HOME_SPOTS]) {
      expect(p.x).toBeGreaterThan(8);
      expect(p.x).toBeLessThan(MAP.w - 8);
      expect(p.y).toBeLessThan(MAP.verandaTop);
    }
    for (const p of REST_SPOTS) {
      expect(p.y).toBeGreaterThan(MAP.verandaTop + 20);
      expect(p.y).toBeLessThan(MAP.h - 18);
    }
  });

  it("работающий доходит до станции и возвращается домой", () => {
    const [u] = [createUnit("a", "working", 0)];
    const seen = new Set<string>();
    for (let i = 0; i < 60 * 60; i += 1) {
      stepScene([u!], 1 / 60, i);
      seen.add(u!.state);
    }
    expect(seen).toEqual(new Set(["idle", "toWork", "working", "toHome"]));
  });

  it("одновременно идут не больше трёх", () => {
    const units = Array.from({ length: 12 }, (_, i) => createUnit(`u${i}`, "working", i));
    let max = 0;
    for (let i = 0; i < 60 * 120; i += 1) {
      stepScene(units, 1 / 60, i);
      const walking = units.filter((u) => u.state === "toWork" || u.state === "toHome").length;
      max = Math.max(max, walking);
    }
    expect(max).toBeLessThanOrEqual(MAX_WALKING);
    expect(max).toBeGreaterThan(0);
  });

  it("отдыхающие стоят на месте", () => {
    const u = createUnit("r", "resting", 2);
    for (let i = 0; i < 600; i += 1) stepScene([u], 1 / 60, i);
    expect({ x: u.x, y: u.y }).toEqual(REST_SPOTS[2]);
  });

  it("обновление состава не телепортирует, а переход в отдых уводит на веранду пешком", () => {
    const u = createUnit("a", "working", 0);
    u.x = 123;
    u.y = 45;
    const kept = syncUnits([u], [{ key: "a", role: "working", busy: true }]);
    expect(kept[0]).toMatchObject({ x: 123, y: 45, busy: true });
    const moved = syncUnits([u], [{ key: "a", role: "resting", busy: false }]);
    expect(moved[0]).toMatchObject({ x: 123, y: 45, role: "resting", state: "toHome", home: REST_SPOTS[0] });
    for (let i = 0; i < 60 * 30; i += 1) stepScene(moved, 1 / 60, i);
    expect(moved[0]).toMatchObject({ x: REST_SPOTS[0]!.x, y: REST_SPOTS[0]!.y, state: "idle" });
  });
});
