import { describe, expect, it } from "vitest";
import { runClaude } from "../lib/claude";

describe("запуск claude", () => {
  it("ревью: процесс, закрывшийся раньше, чем дочитал большой промпт, даёт отказ, а не падение", async () => {
    await expect(
      runClaude({ bin: "false", model: "x", system: "s", prompt: "я".repeat(300_000), schema: {}, timeoutMs: 5000 }),
    ).rejects.toThrow(/exited 1/);
  });
  it("нет бинарника — отказ", async () => {
    await expect(
      runClaude({ bin: "/nonexistent/claude", model: "x", system: "s", prompt: "p", schema: {}, timeoutMs: 5000 }),
    ).rejects.toThrow();
  });
});
