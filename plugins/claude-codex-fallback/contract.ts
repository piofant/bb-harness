import { defineRpcContract } from "@get-bb/plugin-sdk";
import { z } from "zod";

export const targetSchema = z.object({
  providerId: z.enum(["codex", "acp-antigravity"]),
  model: z.string().min(1),
  reasoningLevel: z.enum(["low", "medium", "high", "xhigh", "max"]),
});
export const segmentSchema = z.object({
  threadId: z.string(), providerId: z.string(), model: z.string(), createdAt: z.number(),
});
export const chatSchema = z.object({
  rootThreadId: z.string(), title: z.string(), projectId: z.string(),
  activeThreadId: z.string(), enabled: z.boolean(),
  phase: z.enum(["ready", "waiting-retry", "switching", "blocked", "exhausted"]),
  notice: z.string().nullable(), segments: z.array(segmentSchema),
  targets: z.array(targetSchema), pendingQueueCount: z.number(), updatedAt: z.number(),
});
export type Chat = z.infer<typeof chatSchema>;
export type Target = z.infer<typeof targetSchema>;
export const rpcContract = defineRpcContract({
  list: { input: z.null(), output: z.object({ chats: z.array(chatSchema) }) },
  get: { input: z.object({ threadId: z.string().min(1) }), output: chatSchema.nullable() },
  enable: {
    input: z.object({ threadId: z.string().min(1), targets: z.array(targetSchema).min(1).max(2).optional() }),
    output: chatSchema,
  },
  disable: { input: z.object({ threadId: z.string().min(1) }), output: chatSchema },
  recover: { input: z.object({ threadId: z.string().min(1) }), output: chatSchema },
});
