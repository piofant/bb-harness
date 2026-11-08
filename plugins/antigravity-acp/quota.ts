import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import http from "node:http";
import https from "node:https";
import { execSync } from "node:child_process";
import { createHash } from "node:crypto";

export interface QuotaWindow {
  id: string;
  kind: "five-hour" | "daily" | "weekly" | "custom";
  label: string;
  usedPercent: number;
  resetsAt: string | null;
  model: string | null;
}

export interface QuotaResult {
  ok: boolean;
  error: string | null;
  accountEmail: string | null;
  planLabel: string | null;
  windows: QuotaWindow[];
}

function findDaemon(): { pid: number | null; csrf: string | null } {
  let psOutput = "";
  try {
    psOutput = execSync("ps -ww -eo pid,command 2>/dev/null || ps -eo pid,args 2>/dev/null", {
      encoding: "utf8",
    });
  } catch {
    return { pid: null, csrf: null };
  }

  let pid: number | null = null;
  let csrf: string | null = null;

  for (const line of psOutput.split("\n")) {
    if (line.includes("language_server") && line.includes("--csrf_token")) {
      const parts = line.trim().split(/\s+/);
      const parsedPid = parseInt(parts[0], 10);
      if (!Number.isNaN(parsedPid)) {
        pid = parsedPid;
        const match = line.match(/--csrf_token\s+([^\s]+)/);
        if (match) csrf = match[1];
        break;
      }
    }
  }

  return { pid, csrf };
}

function findListeningPorts(pid: number): number[] {
  const ports: number[] = [];
  try {
    const lsof = execSync(`lsof -nP -a -iTCP -sTCP:LISTEN -p ${pid} 2>/dev/null`, {
      encoding: "utf8",
    });
    for (const line of lsof.split("\n")) {
      const match = line.match(/:(\d+)\s+\(LISTEN\)/);
      if (match) {
        const port = parseInt(match[1], 10);
        if (!ports.includes(port)) ports.push(port);
      }
    }
  } catch {}

  if (ports.length === 0) {
    try {
      const ss = execSync(`ss -tlnp 2>/dev/null | grep "pid=${pid},"`, {
        encoding: "utf8",
      });
      for (const line of ss.split("\n")) {
        const match = line.match(/:(\d+)\s+/);
        if (match) {
          const port = parseInt(match[1], 10);
          if (!ports.includes(port)) ports.push(port);
        }
      }
    } catch {}
  }

  return ports;
}

function queryRpc(
  port: number,
  rpcPath: string,
  csrf: string,
  useHttps: boolean,
  timeoutMs = 4000,
): Promise<any> {
  const mod = useHttps ? https : http;
  return new Promise((resolve, reject) => {
    const req = mod.request(
      {
        hostname: "127.0.0.1",
        port,
        path: rpcPath,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-codeium-csrf-token": csrf,
        },
        rejectUnauthorized: false,
        timeout: timeoutMs,
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => {
          data += chunk;
        });
        res.on("end", () => {
          if (res.statusCode === 200) {
            try {
              resolve(JSON.parse(data));
            } catch (err) {
              reject(err);
            }
          } else {
            reject(new Error(`HTTP ${res.statusCode}`));
          }
        });
      },
    );

    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy(new Error("Timeout"));
    });
    req.write(JSON.stringify({}));
    req.end();
  });
}

function getStoredAccountEmail(): string | null {
  try {
    const acctFile = path.join(os.homedir(), ".gemini", "google_accounts.json");
    if (fs.existsSync(acctFile)) {
      const data = JSON.parse(fs.readFileSync(acctFile, "utf8"));
      if (typeof data.active === "string" && data.active.trim()) {
        return data.active.trim();
      }
    }
  } catch {}
  return null;
}

// Headless hosts (e.g. a VPS) have no Antigravity app and no language_server:
// read the quota straight from Google with the ACP server's own OAuth login.
const CLOUD_API = "https://cloudcode-pa.googleapis.com/v1internal";
// Without an Antigravity User-Agent the API answers 403 "no valid license". The version is the
// Antigravity CLI release this was verified with (03.11.2026); bump it if that 403 comes back.
const CLOUD_USER_AGENT = `antigravity/1.4.12 ${process.platform}/${process.arch}`;
// Refresh + parallel calls must stay well inside the server's 15 s host call.
const CLOUD_TIMEOUT_MS = 5000;

let cloudToken: { key: string; accessToken: string; expiresAt: number } | null = null;

class CloudAuthExpired extends Error {}

function readAcpToken(): any | null {
  try {
    const file = path.join(os.homedir(), ".gemini", "antigravity-acp", "acp_token.json");
    const data = JSON.parse(fs.readFileSync(file, "utf8"));
    return data?.refresh_token && data?.client_id && data?.token_uri ? data : null;
  } catch {
    return null;
  }
}

async function cloudAccessToken(acp: any): Promise<string> {
  // Keyed by the login so a re-login as another account never reuses the old token.
  const key = createHash("sha256").update(String(acp.refresh_token)).digest("hex");
  if (cloudToken && cloudToken.key === key && Date.now() < cloudToken.expiresAt - 60_000) {
    return cloudToken.accessToken;
  }
  const res = await fetch(acp.token_uri, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: acp.client_id,
      client_secret: acp.client_secret ?? "",
      refresh_token: acp.refresh_token,
      grant_type: "refresh_token",
    }),
    signal: AbortSignal.timeout(CLOUD_TIMEOUT_MS),
  });
  // invalid_grant: the ACP login was revoked or expired.
  if (res.status === 400 || res.status === 401) throw new CloudAuthExpired();
  if (!res.ok) throw new Error(`Google token refresh failed (HTTP ${res.status}).`);
  const body: any = await res.json();
  if (typeof body.access_token !== "string") throw new Error("Google token refresh returned no access token.");
  cloudToken = {
    key,
    accessToken: body.access_token,
    expiresAt: Date.now() + (Number(body.expires_in) || 3000) * 1000,
  };
  return cloudToken.accessToken;
}

async function cloudCall(token: string, method: string, payload: unknown): Promise<any> {
  const res = await fetch(`${CLOUD_API}:${method}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "User-Agent": CLOUD_USER_AGENT,
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(CLOUD_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`Antigravity ${method} failed (HTTP ${res.status}).`);
  return res.json();
}

// Proto3 JSON drops zero values, so a quota with a reset time but no fraction is exhausted.
function remainingFraction(quota: any): number {
  const value = quota?.remainingFraction;
  if (typeof value === "number" && Number.isFinite(value)) return Math.max(0, Math.min(1, value));
  return typeof quota?.resetTime === "string" ? 0 : 1;
}

function quotaWindow(id: string, label: string, kind: QuotaWindow["kind"], quota: any, model: string): QuotaWindow {
  return {
    id,
    kind,
    label,
    usedPercent: Math.round((1 - remainingFraction(quota)) * 100),
    resetsAt: typeof quota?.resetTime === "string" ? quota.resetTime : null,
    model,
  };
}

// kind stays "custom": for other kinds bb replaces the label with a long
// "Five-hour limit · <model>" that gets truncated in the panel.
// Only Gemini: bb runs Antigravity through ACP, which serves Gemini models only, so the
// account's Claude/GPT quota could never be spent from bb and is left out of the panel.
/** Gemini window from fetchAvailableModels. */
export function cloudQuotaWindows(models: any): QuotaWindow[] {
  // Gemini models carry per-model 5-hour quota; show the most exhausted one.
  let gemini: any = null;
  for (const info of Object.values<any>(models?.models ?? {})) {
    const quota = info?.quotaInfo;
    if (info?.modelProvider !== "MODEL_PROVIDER_GOOGLE" || typeof quota?.resetTime !== "string") continue;
    if (!gemini || remainingFraction(quota) < remainingFraction(gemini)) gemini = quota;
  }
  return gemini ? [quotaWindow("gemini-5h", "5h", "custom", gemini, "gemini")] : [];
}

/** null when this host has no ACP login to read the quota with. */
export async function probeCloudQuota(): Promise<QuotaResult | null> {
  const acp = readAcpToken();
  if (!acp) return null;
  try {
    const token = await cloudAccessToken(acp);
    const project = acp.project_id || undefined;
    const [models, assist] = await Promise.all([
      cloudCall(token, "fetchAvailableModels", { project }),
      cloudCall(token, "loadCodeAssist", { metadata: { ideType: "ANTIGRAVITY" } }).catch(() => null),
    ]);
    const windows = cloudQuotaWindows(models);
    if (windows.length === 0) throw new Error("Google returned no Gemini quota.");
    const info = assist;
    const tier: string = info?.paidTier?.name || info?.currentTier?.name || "";
    const planLabel = tier.includes("Ultra") ? "Ultra" : tier.includes("Pro") ? "Pro" : tier.includes("Free") ? "Free" : null;
    const emailMatch = /[?&]Email=([^&]+)/.exec(info?.manageSubscriptionUri ?? "");
    return {
      ok: true,
      error: null,
      accountEmail: emailMatch ? decodeURIComponent(emailMatch[1]) : getStoredAccountEmail(),
      planLabel,
      windows,
    };
  } catch (err) {
    cloudToken = null;
    return {
      ok: false,
      error: err instanceof CloudAuthExpired
        ? "expired"
        : (err as Error).message || "Failed to query Antigravity quota from Google.",
      accountEmail: getStoredAccountEmail(),
      planLabel: null,
      windows: [],
    };
  }
}

export async function probeQuota(): Promise<QuotaResult> {
  const local = await probeLanguageServerQuota();
  if (local.ok) return local;
  return (await probeCloudQuota()) ?? local;
}

async function probeLanguageServerQuota(): Promise<QuotaResult> {
  const { pid, csrf } = findDaemon();
  const storedEmail = getStoredAccountEmail();

  if (!pid || !csrf) {
    if (storedEmail) {
      return {
        ok: false,
        error: "Antigravity language server is not running.",
        accountEmail: storedEmail,
        planLabel: null,
        windows: [],
      };
    }
    return {
      ok: false,
      error: "not_installed",
      accountEmail: null,
      planLabel: null,
      windows: [],
    };
  }

  const ports = findListeningPorts(pid);
  if (ports.length === 0) {
    return {
      ok: false,
      error: "Could not find listening port for Antigravity daemon.",
      accountEmail: storedEmail,
      planLabel: null,
      windows: [],
    };
  }

  let quotaSummary: any = null;
  let userStatus: any = null;

  for (const port of ports) {
    for (const useHttps of [false, true]) {
      try {
        quotaSummary = await queryRpc(
          port,
          "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary",
          csrf,
          useHttps,
        );
        try {
          userStatus = await queryRpc(
            port,
            "/exa.language_server_pb.LanguageServerService/GetUserStatus",
            csrf,
            useHttps,
          );
        } catch {}
        break;
      } catch {}
    }
    if (quotaSummary) break;
  }

  if (!quotaSummary) {
    return {
      ok: false,
      error: "Failed to query Antigravity language server.",
      accountEmail: storedEmail,
      planLabel: null,
      windows: [],
    };
  }

  const accountEmail =
    userStatus?.userStatus?.email ||
    storedEmail ||
    null;

  const rawTier =
    userStatus?.userStatus?.userTier?.name ||
    userStatus?.userStatus?.planStatus?.planInfo?.teamsTier ||
    "";
  let planLabel = "Pro";
  if (rawTier.includes("Ultra")) planLabel = "Ultra";
  else if (rawTier.includes("Pro")) planLabel = "Pro";
  else if (rawTier.includes("Free")) planLabel = "Free";

  const windows: QuotaWindow[] = [];
  const groups = quotaSummary.response?.groups || [];

  for (const g of groups) {
    const isGemini = /gemini/i.test(g.displayName || "");
    const is3p = /claude|gpt|3p/i.test(g.displayName || "");
    // Claude/GPT quota cannot be spent through ACP; keep the panel to what bb can use.
    if (is3p) continue;
    const modelName = isGemini ? "Gemini" : (g.displayName || "Model");

    for (const b of g.buckets || []) {
      const isWeekly = b.window === "weekly" || /weekly/i.test(b.bucketId || "");
      const is5h = b.window === "5h" || /5h/i.test(b.bucketId || "");
      const windowPrefix = is5h ? "5h" : isWeekly ? "7d" : b.window || "Quota";
      const label = `${windowPrefix} · ${modelName}`;
      const fraction = typeof b.remainingFraction === "number" ? b.remainingFraction : 1;
      const usedPercent = Math.max(0, Math.min(100, Math.round((1 - fraction) * 100)));

      windows.push({
        id: b.bucketId || `${g.displayName}:${windowPrefix}`,
        kind: "custom",
        label,
        usedPercent,
        resetsAt: b.resetTime || null,
        model: modelName.toLowerCase(),
      });
    }
  }

  return {
    ok: true,
    error: null,
    accountEmail,
    planLabel,
    windows,
  };
}
