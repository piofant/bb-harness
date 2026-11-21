import type { BbPluginApi } from "@get-bb/plugin-sdk";
import { z } from "zod";
import { chatSchema, type Chat } from "../contract.js";

export const failureSchema = z.object({
  threadId: z.string(), requestId: z.string(), reason: z.string(), observedAt: z.number(),
});
export type Failure = z.infer<typeof failureSchema>;
const recordSchema = chatSchema.extend({
  failure: failureSchema.nullable(), operation: z.string().nullable(),
});
export type RecordState = z.infer<typeof recordSchema>;

export function createStore(bb: BbPluginApi) {
  const db = bb.storage.database();
  bb.storage.migrate(db, [
    "CREATE TABLE conversations (root_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at INTEGER NOT NULL)",
    "CREATE TABLE members (thread_id TEXT PRIMARY KEY, root_id TEXT NOT NULL)",
    "CREATE INDEX members_root ON members(root_id)",
    "CREATE TABLE relays (source_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, destination_id TEXT, snapshot TEXT NOT NULL, status TEXT NOT NULL)",
  ]);
  const parse = (row: unknown): RecordState | null => {
    const value = z.object({ state: z.string() }).optional().parse(row);
    return value ? recordSchema.parse(JSON.parse(value.state)) : null;
  };
  const get = (threadId: string) => parse(db.prepare(
    "SELECT c.state FROM conversations c JOIN members m ON c.root_id=m.root_id WHERE m.thread_id=?",
  ).get(threadId));
  const list = () => db.prepare("SELECT state FROM conversations ORDER BY updated_at DESC").all().map(parse).filter((r: RecordState | null): r is RecordState => r !== null);
  const save = (record: RecordState) => {
    const value = recordSchema.parse({ ...record, updatedAt: Date.now() });
    db.transaction(() => {
      db.prepare("INSERT INTO conversations VALUES (?, ?, ?) ON CONFLICT(root_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at")
        .run(value.rootThreadId, JSON.stringify(value), value.updatedAt);
      for (const segment of value.segments) db.prepare("INSERT INTO members VALUES (?, ?) ON CONFLICT(thread_id) DO NOTHING").run(segment.threadId, value.rootThreadId);
    })();
    bb.realtime.publish("changed", { rootThreadId: value.rootThreadId });
    return value;
  };
  const publicChat = (record: RecordState): Chat => chatSchema.parse(record);
  return { db, get, list, save, publicChat };
}
export type Store = ReturnType<typeof createStore>;
