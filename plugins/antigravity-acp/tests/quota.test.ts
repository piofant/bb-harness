import { test } from "node:test";
import assert from "node:assert/strict";
import { cloudQuotaWindows } from "../quota.ts";

const models = {
  models: {
    "gemini-3.8-flash-tiered": { modelProvider: "MODEL_PROVIDER_GOOGLE", quotaInfo: { remainingFraction: 0.4, resetTime: "2026-11-03T21:00:00Z" } },
    "gemini-3-flash": { modelProvider: "MODEL_PROVIDER_GOOGLE", quotaInfo: { remainingFraction: 0.9, resetTime: "2026-11-03T21:00:00Z" } },
    // Internal autocomplete models have no reset time and must not count.
    "tab_flash_lite_preview": { quotaInfo: { remainingFraction: 0 } },
    // Claude/GPT quota cannot be spent through ACP and stays out of the panel.
    "claude-opus-5-5-high": { modelProvider: "MODEL_PROVIDER_ANTHROPIC", quotaInfo: { remainingFraction: 0, resetTime: "2026-11-03T21:00:00Z" } },
  },
};

test("cloudQuotaWindows: one window for the most exhausted Gemini model", () => {
  assert.deepEqual(cloudQuotaWindows(models), [
    { id: "gemini-5h", kind: "custom", label: "5h", usedPercent: 60, resetsAt: "2026-11-03T21:00:00Z", model: "gemini" },
  ]);
});

test("cloudQuotaWindows: a reset time without a fraction means the quota is exhausted", () => {
  const exhausted = {
    models: {
      "gemini-3.8-flash-tiered": { modelProvider: "MODEL_PROVIDER_GOOGLE", quotaInfo: { resetTime: "2026-11-03T21:00:00Z" } },
      "gemini-3-flash": { modelProvider: "MODEL_PROVIDER_GOOGLE", quotaInfo: { remainingFraction: 1, resetTime: "2026-11-03T21:00:00Z" } },
    },
  };
  assert.deepEqual(cloudQuotaWindows(exhausted).map((w) => [w.id, w.usedPercent]), [["gemini-5h", 100]]);
});

test("cloudQuotaWindows: empty or malformed responses give no windows", () => {
  assert.deepEqual(cloudQuotaWindows(null), []);
  assert.deepEqual(cloudQuotaWindows({ models: { x: null } }), []);
});
