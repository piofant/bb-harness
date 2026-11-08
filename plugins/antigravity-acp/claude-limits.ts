import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import type { QuotaWindow } from "./quota.js";

// bb's built-in Claude usage check calls /api/oauth/usage, which keeps answering 429 for
// the long-lived setup-token this server runs on. Every model response, however, carries
// the subscription limits in anthropic-ratelimit-unified-* headers, so a 1-token Haiku
// request reads them.
const MESSAGES_URL = "https://api.anthropic.com/v1/messages";
const PROBE_MODEL = "claude-haiku-5-5";
const TIMEOUT_MS = 10_000;
// A probe costs a real (tiny) request, so never run them back to back.
const MIN_PROBE_INTERVAL_MS = 60_000;
const CACHE_TTL_MS = 5 * 60_000;

export interface ClaudeLimits {
  status: "ok" | "unauthenticated" | "expired" | "error";
  message: string | null;
  accountEmail: string | null;
  accountUuid: string | null;
  plan: { id: string; multiplier: number | null } | null;
  windows: QuotaWindow[];
}

const WINDOW_NAMES: Record<string, { kind: QuotaWindow["kind"]; label: string }> = {
  "5h": { kind: "five-hour", label: "Five-hour limit" },
  "7d": { kind: "weekly", label: "Weekly limit" },
};

function resetIso(seconds: string | undefined): string | null {
  const value = Number(seconds);
  return Number.isFinite(value) && value > 0 ? new Date(value * 1000).toISOString() : null;
}

/** Windows from anthropic-ratelimit-unified-<window>-utilization / -reset headers. */
export function parseRateLimitHeaders(headers: Record<string, string>): QuotaWindow[] {
  const windows: QuotaWindow[] = [];
  for (const [name, value] of Object.entries(headers)) {
    // Only subscription windows: 5h, 7d and model-scoped ones like 7d_opus.
    const match = /^anthropic-ratelimit-unified-((?:5h|7d)(?:_[a-z0-9]+)?)-utilization$/.exec(name.toLowerCase());
    if (!match) continue;
    const utilization = Number(value);
    if (!Number.isFinite(utilization)) continue;
    const id = match[1];
    const known = WINDOW_NAMES[id];
    // Scoped windows look like 7d_opus: base window plus a model family.
    const [base, model] = id.split("_", 2);
    windows.push({
      id: `claude-${id}`,
      kind: known?.kind ?? "custom",
      label: known?.label ?? `${base}${model ? ` · ${model}` : ""}`,
      usedPercent: Math.max(0, Math.round(utilization * 100)),
      resetsAt: resetIso(headers[`anthropic-ratelimit-unified-${id}-reset`]),
      model: null,
    });
  }
  // Five-hour first, then weekly, then scoped windows.
  const order = (w: QuotaWindow) => (w.kind === "five-hour" ? 0 : w.kind === "weekly" ? 1 : 2);
  return windows.sort((a, b) => order(a) - order(b) || a.id.localeCompare(b.id));
}

function readJson(file: string): any {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

/** Same token order Claude Code itself uses: env, then user settings env, then stored login. */
function readToken(home: string): string | null {
  const fromEnv = process.env.CLAUDE_CODE_OAUTH_TOKEN?.trim();
  if (fromEnv) return fromEnv;
  const settings = readJson(path.join(home, ".claude", "settings.json"));
  const fromSettings = settings?.env?.CLAUDE_CODE_OAUTH_TOKEN;
  if (typeof fromSettings === "string" && fromSettings.trim()) return fromSettings.trim();
  const stored = readJson(path.join(home, ".claude", ".credentials.json"))?.claudeAiOauth?.accessToken;
  return typeof stored === "string" && stored.trim() ? stored.trim() : null;
}

function readPlan(home: string): ClaudeLimits["plan"] {
  const oauth = readJson(path.join(home, ".claude", ".credentials.json"))?.claudeAiOauth;
  const id = typeof oauth?.subscriptionType === "string" ? oauth.subscriptionType.toLowerCase() : null;
  if (!id) return null;
  const multiplier = Number(/max_(\d+)x/.exec(oauth?.rateLimitTier ?? "")?.[1]) || null;
  return { id, multiplier };
}

function headerRecord(headers: Headers): Record<string, string> {
  const record: Record<string, string> = {};
  headers.forEach((value, name) => {
    record[name.toLowerCase()] = value;
  });
  return record;
}

export async function probeClaudeLimits(home = os.homedir()): Promise<ClaudeLimits> {
  const account = readJson(path.join(home, ".claude.json"))?.oauthAccount;
  const base = {
    accountEmail: typeof account?.emailAddress === "string" ? account.emailAddress : null,
    accountUuid: typeof account?.accountUuid === "string" ? account.accountUuid : null,
    plan: readPlan(home),
  };
  const token = readToken(home);
  if (!token) return { ...base, status: "unauthenticated", message: null, windows: [] };

  try {
    const res = await fetch(MESSAGES_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "content-type": "application/json",
        "User-Agent": "claude-cli/2.1.0 (external, cli)",
      },
      body: JSON.stringify({
        model: PROBE_MODEL,
        max_tokens: 1,
        system: "You are Claude Code, Anthropic's official CLI for Claude.",
        messages: [{ role: "user", content: "hi" }],
      }),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    await res.body?.cancel().catch(() => {});
    if (res.status === 401) return { ...base, status: "expired", message: null, windows: [] };
    // A rate-limited (429) response still carries the limit headers.
    const windows = parseRateLimitHeaders(headerRecord(res.headers));
    if (windows.length > 0) return { ...base, status: "ok", message: null, windows };
    return {
      ...base,
      status: "error",
      message: res.ok
        ? "Anthropic returned no subscription limits."
        : `Claude limit check failed (HTTP ${res.status}).`,
      windows: [],
    };
  } catch (err) {
    return { ...base, status: "error", message: (err as Error).message || "Claude limit check failed.", windows: [] };
  }
}

let cached: { at: number; value: ClaudeLimits } | null = null;
let lastAttemptAt = 0;
let inFlight: Promise<ClaudeLimits> | null = null;

/** Test hook: forget cached readings and probe timing. */
export function resetClaudeLimitsCache(): void {
  cached = null;
  lastAttemptAt = 0;
  inFlight = null;
}

/** Cached probe: refresh asks for a fresh reading, but never more than one probe a minute. */
export async function getClaudeLimits(
  refresh: boolean,
  probe: () => Promise<ClaudeLimits> = () => probeClaudeLimits(),
): Promise<{ value: ClaudeLimits; observedAt: number }> {
  const sinceAttempt = Date.now() - lastAttemptAt;
  const age = cached ? Date.now() - cached.at : Infinity;
  // The floor counts failed attempts too, so an outage never turns into a request per poll.
  if (cached && (sinceAttempt < MIN_PROBE_INTERVAL_MS || (!refresh && age < CACHE_TTL_MS))) {
    return { value: cached.value, observedAt: cached.at };
  }
  if (!inFlight) {
    lastAttemptAt = Date.now();
    inFlight = probe().finally(() => {
      inFlight = null;
    });
  }
  const value = await inFlight;
  // Ride out a transient error on the last good reading; a dead login must show up at once.
  const keepGood = value.status === "error" && cached?.value.status === "ok";
  if (!keepGood) cached = { at: Date.now(), value };
  return { value: cached!.value, observedAt: cached!.at };
}
