#!/usr/bin/env node
import readline from "node:readline";

// Echoes every session/prompt it receives, so tests can see what the wrapper forwarded.
const sessionId = "session-echo";
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

function send(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

rl.on("line", (line) => {
  const message = JSON.parse(line);
  if (message.method === "initialize") {
    send({ jsonrpc: "2.0", id: message.id, result: { protocolVersion: 1 } });
  } else if (message.method === "session/new") {
    send({ jsonrpc: "2.0", id: message.id, result: { sessionId } });
  } else if (message.method === "session/prompt") {
    send({ jsonrpc: "2.0", method: "test/received", params: { raw: line, prompt: message.params.prompt } });
    send({ jsonrpc: "2.0", id: message.id, result: { stopReason: "end_turn" } });
  }
});

rl.on("close", () => process.exit(0));
