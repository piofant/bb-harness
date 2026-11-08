// Дешёвый вызов `claude -p` для заголовков телевизора: без системного
// промпта Claude Code, CLAUDE.md, скиллов и MCP — около $0.002 за вызов.
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export interface ClaudeRun {
  stdout: string;
  ms: number;
}

/** `claude` из PATH, а если сервер bb запущен без него — из ~/.local/bin. */
export function resolveClaudeBin(configured: string): string {
  if (configured.includes("/")) return configured;
  const local = join(homedir(), ".local/bin", configured);
  return existsSync(local) ? local : configured;
}

export function runClaude(opts: {
  bin: string;
  model: string;
  system: string;
  prompt: string;
  schema: object;
  timeoutMs: number;
  /** Потолок цены одного вызова, $. */
  budgetUsd?: number;
  signal?: AbortSignal;
}): Promise<ClaudeRun> {
  const started = Date.now();
  const args = [
    "-p",
    "--model", opts.model,
    "--tools", "",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--setting-sources", "",
    "--system-prompt", opts.system,
    "--output-format", "json",
    "--json-schema", JSON.stringify(opts.schema),
    "--max-budget-usd", String(opts.budgetUsd ?? 0.05),
  ];
  return new Promise((resolve, reject) => {
    const child = spawn(opts.bin, args, { stdio: ["pipe", "pipe", "pipe"], signal: opts.signal });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => child.kill("SIGKILL"), opts.timeoutMs);
    child.stdout.on("data", (d: Buffer) => {
      if (stdout.length < 1_000_000) stdout += d.toString("utf8");
    });
    child.stderr.on("data", (d: Buffer) => {
      if (stderr.length < 4_000) stderr += d.toString("utf8");
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code, sig) => {
      clearTimeout(timer);
      const ms = Date.now() - started;
      if (code === 0) resolve({ stdout, ms });
      else reject(new Error(`claude exited ${code ?? sig} after ${ms}ms: ${stderr.trim().slice(0, 300)}`));
    });
    // Если claude закрылся раньше, чем дочитал промпт (не залогинен, плохой
    // флаг), запись даёт EPIPE. Без обработчика это роняет весь процесс
    // плагина; причину и так отдаст обработчик close.
    child.stdin.on("error", () => undefined);
    child.stdin.end(opts.prompt);
  });
}
