import assert from "node:assert/strict";
import { chmod, copyFile, mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";
import readline from "node:readline";
import test from "node:test";
import { renderWrapperScript } from "../wrapper-script.ts";

const BB_INSTRUCTIONS = "<system_instructions>\nbb rules here\n</system_instructions> ";

// A UserPromptSubmit hook in the Claude/Codex contract: JSON on stdin, additionalContext on stdout.
const HOOK = `
const fs = require("node:fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
fs.appendFileSync(process.env.HOOK_LOG, JSON.stringify(input) + "\\n");
const p = input.prompt;
if (p.includes("молчи")) process.exit(0);
if (p.includes("сломайся")) { process.stdout.write("not json"); process.exit(0); }
if (p.includes("упади")) process.exit(3);
if (p.includes("усни")) { setTimeout(() => {}, 20000); return; }
if (p.includes("много")) { process.stdout.write(JSON.stringify({ hookSpecificOutput: { additionalContext: "я".repeat(50000) } })); return; }
process.stdout.write(JSON.stringify({ hookSpecificOutput: { hookEventName: "UserPromptSubmit",
  additionalContext: process.argv[2] + ": " + p + " | " + input.session_id } }));
`;

async function setup(config) {
  const root = await mkdtemp(join(tmpdir(), "agy-prompt-hooks-"));
  const geminiHome = join(root, "gemini");
  await mkdir(join(geminiHome, "config"), { recursive: true });
  const hook = join(root, "hook.cjs");
  await writeFile(hook, HOOK);
  if (config !== undefined) {
    const text = typeof config === "string" ? config : JSON.stringify(config(hook));
    await writeFile(join(geminiHome, "config", "prompt_hooks.json"), text);
  }
  const wrapper = join(root, "agy_acp_server.par");
  await writeFile(wrapper, renderWrapperScript({ nodePath: process.execPath, realBinaryPath: null }), { mode: 0o755 });
  const fake = join(root, "echo-agent.mjs");
  await copyFile(join(process.cwd(), "test/fixtures/echo-agent.mjs"), fake);
  await chmod(fake, 0o755);
  return { root, geminiHome, wrapper, fake, hookLog: join(root, "hook.log") };
}

async function sendPrompt(ctx, prompt, env = {}) {
  const child = spawn(ctx.wrapper, [], {
    env: { ...process.env, ANTIGRAVITY_REAL_SERVER_PATH: ctx.fake, GEMINI_HOME: ctx.geminiHome,
      HOOK_LOG: ctx.hookLog, BB_THREAD_ID: "thr_test123", ...env },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const lines = [];
  let stderr = "";
  child.stderr.on("data", (c) => { stderr += c; });
  readline.createInterface({ input: child.stdout }).on("line", (l) => lines.push(JSON.parse(l)));
  const sent = JSON.stringify({ jsonrpc: "2.0", id: 3, method: "session/prompt", params: { sessionId: "session-echo", prompt } });
  const started = Date.now();
  child.stdin.write(sent + "\n");
  child.stdin.end();
  const code = await new Promise((resolve) => child.on("close", resolve));
  const received = lines.find((l) => l.method === "test/received")?.params;
  return { code, stderr, sent, received, elapsed: Date.now() - started, response: lines.find((l) => l.id === 3) };
}

const hooksConfig = (...tags) => (hook) => ({
  UserPromptSubmit: tags.map((tag) => ({ type: "command", command: `"${process.execPath}" "${hook}" ${tag}`, timeout: 2 })),
});

test("appends UserPromptSubmit hook context to the prompt, without bb's instructions in the hook input", async () => {
  const ctx = await setup(hooksConfig("kb-hint", "second"));
  const prompt = [
    { type: "text", text: BB_INSTRUCTIONS + "как выкатывать nanobot?" },
    { type: "resource_link", uri: "file:///tmp/x.png", name: "x.png" },
  ];
  const r = await sendPrompt(ctx, prompt);
  assert.equal(r.code, 0, r.stderr);
  assert.deepEqual(r.received.prompt, [
    ...prompt,
    { type: "text", text: '<hook_context source="UserPromptSubmit">\nkb-hint: как выкатывать nanobot? | thr_test123\n\nsecond: как выкатывать nanobot? | thr_test123\n</hook_context>' },
  ]);
  const inputs = (await readFile(ctx.hookLog, "utf8")).trim().split("\n").map((l) => JSON.parse(l));
  assert.deepEqual(inputs[0], { hook_event_name: "UserPromptSubmit", prompt: "как выкатывать nanobot?", session_id: "thr_test123", cwd: process.cwd() });
  assert.deepEqual(r.response.result, { stopReason: "end_turn" });
});

test("falls back to the ACP session id without BB_THREAD_ID", async () => {
  const ctx = await setup(hooksConfig("kb-hint"));
  const r = await sendPrompt(ctx, [{ type: "text", text: "вопрос" }], { BB_THREAD_ID: "" });
  assert.match(r.received.prompt.at(-1).text, /\nkb-hint: вопрос \| session-echo\n/);
});

for (const [name, config, text] of [
  ["no hooks file", undefined, "вопрос"],
  ["invalid hooks file", "{not json", "вопрос"],
  ["hooks file without UserPromptSubmit", () => ({ Other: [] }), "вопрос"],
  ["empty hook output", hooksConfig("kb-hint"), "молчи"],
  ["garbage hook output", hooksConfig("kb-hint"), "сломайся"],
  ["failing hook", hooksConfig("kb-hint"), "упади"],
  ["prompt that is only bb instructions", hooksConfig("kb-hint"), ""],
]) {
  test(`forwards the prompt byte-for-byte on ${name}`, async () => {
    const ctx = await setup(config);
    const r = await sendPrompt(ctx, [{ type: "text", text: BB_INSTRUCTIONS + text }]);
    assert.equal(r.code, 0, r.stderr);
    assert.equal(r.received.raw, r.sent);
  });
}

test("a hanging hook is cut off at its timeout and the prompt still goes through", async () => {
  const ctx = await setup(hooksConfig("kb-hint"));
  const r = await sendPrompt(ctx, [{ type: "text", text: "усни" }]);
  assert.equal(r.code, 0, r.stderr);
  assert.equal(r.received.raw, r.sent);
  assert.ok(r.elapsed < 5000, `took ${r.elapsed} ms`);
});

test("non-prompt messages are never touched", async () => {
  const ctx = await setup(hooksConfig("kb-hint"));
  const child = spawn(ctx.wrapper, [], {
    env: { ...process.env, ANTIGRAVITY_REAL_SERVER_PATH: ctx.fake, GEMINI_HOME: ctx.geminiHome, HOOK_LOG: ctx.hookLog },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const lines = [];
  readline.createInterface({ input: child.stdout }).on("line", (l) => lines.push(JSON.parse(l)));
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: 1 } }) + "\n");
  child.stdin.end();
  await new Promise((resolve) => child.on("close", resolve));
  assert.deepEqual(lines, [{ jsonrpc: "2.0", id: 1, result: { protocolVersion: 1 } }]);
  await assert.rejects(readFile(ctx.hookLog, "utf8"));
});

test("hook context is capped", async () => {
  const ctx = await setup(hooksConfig("kb-hint"));
  const r = await sendPrompt(ctx, [{ type: "text", text: "много" }]);
  const added = r.received.prompt.at(-1).text;
  assert.ok(added.length <= 10_100, `${added.length} chars`);
  assert.match(added, /^<hook_context source="UserPromptSubmit">/);
});

test("all hooks share one time budget", async () => {
  const ctx = await setup((hook) => ({ UserPromptSubmit: [1, 2, 3].map(() => (
    { command: `"${process.execPath}" "${hook}" t`, timeout: 4 })) }));
  const r = await sendPrompt(ctx, [{ type: "text", text: "усни" }]);
  assert.equal(r.received.raw, r.sent);
  assert.ok(r.elapsed < 7000, `took ${r.elapsed} ms`);  // 3 × 4 s without a shared budget
});

test("a hook that leaves a background child still contributes its answer", async () => {
  const ctx = await setup(() => ({ UserPromptSubmit: [{ command:
    `sleep 20 & printf '%s' '{"hookSpecificOutput":{"additionalContext":"bg-ok"}}'`, timeout: 1 }] }));
  const r = await sendPrompt(ctx, [{ type: "text", text: "вопрос" }]);
  assert.match(r.received.prompt.at(-1).text, /bg-ok/);
});
