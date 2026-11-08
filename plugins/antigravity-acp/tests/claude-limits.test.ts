import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { getClaudeLimits, parseRateLimitHeaders, probeClaudeLimits, resetClaudeLimitsCache, type ClaudeLimits } from "../claude-limits.ts";

test("parseRateLimitHeaders: five-hour, weekly and scoped windows with resets", () => {
  const windows = parseRateLimitHeaders({
    "anthropic-ratelimit-unified-7d-utilization": "0.38",
    "anthropic-ratelimit-unified-7d-reset": "1794038400",
    "anthropic-ratelimit-unified-5h-utilization": "1.02",
    "anthropic-ratelimit-unified-5h-reset": "1793800000",
    "anthropic-ratelimit-unified-7d_opus-utilization": "0.5",
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-fallback-percentage": "0.5",
  });
  assert.deepEqual(windows, [
    { id: "claude-5h", kind: "five-hour", label: "Five-hour limit", usedPercent: 102, resetsAt: new Date(1793800000 * 1000).toISOString(), model: null },
    { id: "claude-7d", kind: "weekly", label: "Weekly limit", usedPercent: 38, resetsAt: "2026-11-07T08:00:00.000Z", model: null },
    { id: "claude-7d_opus", kind: "custom", label: "7d · opus", usedPercent: 50, resetsAt: null, model: null },
  ]);
});

test("parseRateLimitHeaders: no limit headers gives no windows", () => {
  assert.deepEqual(parseRateLimitHeaders({ "content-type": "application/json", "anthropic-ratelimit-unified-7d-utilization": "n/a" }), []);
});

let home: string;
const realFetch = globalThis.fetch;
const realEnv = process.env.CLAUDE_CODE_OAUTH_TOKEN;

beforeEach(() => {
  home = fs.mkdtempSync(path.join(os.tmpdir(), "claude-limits-"));
  fs.mkdirSync(path.join(home, ".claude"));
  delete process.env.CLAUDE_CODE_OAUTH_TOKEN;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  if (realEnv === undefined) delete process.env.CLAUDE_CODE_OAUTH_TOKEN;
  else process.env.CLAUDE_CODE_OAUTH_TOKEN = realEnv;
  fs.rmSync(home, { recursive: true, force: true });
});

function writeLogin() {
  fs.writeFileSync(path.join(home, ".claude", ".credentials.json"), JSON.stringify({
    claudeAiOauth: { accessToken: "stored-token", subscriptionType: "max", rateLimitTier: "default_claude_max_20x" },
  }));
  fs.writeFileSync(path.join(home, ".claude.json"), JSON.stringify({
    oauthAccount: { emailAddress: "user@example.com", accountUuid: "11111111-1111-1111-1111-111111111111" },
  }));
}

test("probeClaudeLimits: no token anywhere is unauthenticated and makes no request", async () => {
  globalThis.fetch = (async () => assert.fail("must not call the API")) as typeof fetch;
  const result = await probeClaudeLimits(home);
  assert.equal(result.status, "unauthenticated");
});

test("probeClaudeLimits: settings env token wins over stored login; headers become windows", async () => {
  writeLogin();
  fs.writeFileSync(path.join(home, ".claude", "settings.json"), JSON.stringify({ env: { CLAUDE_CODE_OAUTH_TOKEN: "settings-token" } }));
  let auth = "";
  globalThis.fetch = (async (_url: unknown, init: any) => {
    auth = init.headers.Authorization;
    return new Response("{}", { status: 200, headers: { "anthropic-ratelimit-unified-7d-utilization": "0.38", "anthropic-ratelimit-unified-7d-reset": "1794038400" } });
  }) as typeof fetch;
  const result = await probeClaudeLimits(home);
  assert.equal(auth, "Bearer settings-token");
  assert.equal(result.status, "ok");
  assert.deepEqual(result.plan, { id: "max", multiplier: 20 });
  assert.equal(result.accountEmail, "user@example.com");
  assert.deepEqual(result.windows.map((w) => [w.id, w.usedPercent]), [["claude-7d", 38]]);
});

test("probeClaudeLimits: 401 is expired; 429 with headers still reports limits", async () => {
  writeLogin();
  globalThis.fetch = (async () => new Response("{}", { status: 401 })) as typeof fetch;
  assert.equal((await probeClaudeLimits(home)).status, "expired");

  globalThis.fetch = (async () => new Response("{}", { status: 429, headers: { "anthropic-ratelimit-unified-5h-utilization": "1" } })) as typeof fetch;
  const limited = await probeClaudeLimits(home);
  assert.equal(limited.status, "ok");
  assert.equal(limited.windows[0].usedPercent, 100);
});

test("probeClaudeLimits: network failure or missing headers is an error, never leaking the token", async () => {
  writeLogin();
  globalThis.fetch = (async () => { throw new Error("connect ECONNREFUSED"); }) as typeof fetch;
  const failed = await probeClaudeLimits(home);
  assert.equal(failed.status, "error");
  assert.ok(!failed.message?.includes("stored-token"));

  globalThis.fetch = (async () => new Response("{}", { status: 500 })) as typeof fetch;
  assert.equal((await probeClaudeLimits(home)).message, "Claude limit check failed (HTTP 500).");
});

function limits(status: ClaudeLimits["status"], usedPercent = 0): ClaudeLimits {
  return {
    status, message: status === "error" ? "boom" : null, accountEmail: null, accountUuid: null, plan: null,
    windows: status === "ok" ? [{ id: "claude-7d", kind: "weekly", label: "Weekly limit", usedPercent, resetsAt: null, model: null }] : [],
  };
}

test("getClaudeLimits: one probe a minute even when probes fail; TTL without refresh", async (t) => {
  resetClaudeLimitsCache();
  let now = 1_000_000;
  t.mock.method(Date, "now", () => now);
  const calls: string[] = [];
  const sequence = [limits("ok", 10), limits("error"), limits("error"), limits("ok", 20)];
  const probe = async () => { const v = sequence[calls.length]; calls.push(v.status); return v; };

  assert.equal((await getClaudeLimits(true, probe)).value.windows[0].usedPercent, 10);
  now += 30_000; // under the 60 s floor: no probe even on refresh
  await getClaudeLimits(true, probe);
  assert.equal(calls.length, 1);

  now += 40_000; // floor passed: probe fails, last good reading is kept
  const kept = await getClaudeLimits(true, probe);
  assert.equal(calls.length, 2);
  assert.equal(kept.value.status, "ok");

  now += 10_000; // the failed attempt also resets the floor
  await getClaudeLimits(true, probe);
  assert.equal(calls.length, 2);

  now += 4 * 60_000; // the good reading is now past the 5 min TTL: a plain poll probes again
  await getClaudeLimits(false, probe);
  assert.equal(calls.length, 3);
});

test("getClaudeLimits: an expired login replaces the last good reading", async (t) => {
  resetClaudeLimitsCache();
  let now = 2_000_000;
  t.mock.method(Date, "now", () => now);
  const sequence = [limits("ok", 10), limits("expired")];
  let i = 0;
  const probe = async () => sequence[i++];
  await getClaudeLimits(true, probe);
  now += 61_000;
  assert.equal((await getClaudeLimits(true, probe)).value.status, "expired");
});

test("getClaudeLimits: concurrent callers share one probe", async () => {
  resetClaudeLimitsCache();
  let calls = 0;
  const probe = async () => { calls++; await new Promise((r) => setTimeout(r, 10)); return limits("ok", 5); };
  const [a, b] = await Promise.all([getClaudeLimits(true, probe), getClaudeLimits(true, probe)]);
  assert.equal(calls, 1);
  assert.equal(a.value, b.value);
});
