// «Ждут тебя»: треды, где ход за человеком. Не догадки модели, а то, что
// видно по самому bb: ошибка, открытый вопрос агента, непрочитанный ответ.

export interface WaitingInput {
  id: string;
  title: string | null;
  updatedAt: number;
  status: string;
  hasPendingInteraction: boolean;
  lastReadAt: number | null;
  latestAttentionAt: number;
  archived: boolean;
  hidden: boolean;
  child: boolean;
}

export type WaitReason = "error" | "pending" | "question" | "unread";

export const REASON_LABEL: Record<WaitReason, string> = {
  error: "упал",
  pending: "ждёт ответа",
  question: "вопрос",
  unread: "не прочитан",
};

/** Порядок важности: сломанное и заблокированное выше просто непрочитанного. */
const RANK: Record<WaitReason, number> = { error: 0, pending: 1, question: 2, unread: 3 };

const BUSY = new Set(["active", "starting", "pending", "stopping"]);

/** Кандидаты без текста: треды периода, которые не крутятся прямо сейчас. */
export function candidates(threads: readonly WaitingInput[], since: number): WaitingInput[] {
  return threads.filter(
    (t) => !t.archived && !t.hidden && !t.child && t.updatedAt >= since && (!BUSY.has(t.status) || t.hasPendingInteraction),
  );
}

/** Код, ссылки и markdown-разметка — не текст: в них «?» не вопрос. */
export function plainText(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`[^`]*`/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/^\s*(#+|[-*]|\d+\.)\s+/gm, "")
    .replace(/[*_>]/g, "");
}

function sentences(text: string): string[] {
  return (
    plainText(text)
      .replace(/\s+/g, " ")
      .match(/[^.!?…]+[.!?…]+|[^.!?…]+$/g)
      ?.map((s) => s.trim())
      .filter((s) => s.length > 2) ?? []
  );
}

/**
 * Вопрос к человеку — в конце ответа: последние два предложения. Вопрос в
 * начале («Почему падало? Причина в…») риторический и не считается.
 */
export function lastQuestion(answer: string): string | null {
  const qs = sentences(answer).slice(-2).filter((s) => s.endsWith("?"));
  return qs.at(-1) ?? null;
}

/** Строка под тредом: вопрос агента, иначе начало его ответа. */
export function hookLine(preview: string | null): string {
  if (!preview) return "";
  const line = lastQuestion(preview) ?? sentences(preview)[0] ?? preview;
  return line.length > 140 ? `${line.slice(0, 139)}…` : line;
}

export function classify(t: WaitingInput, lastAnswer: string | null): WaitReason | null {
  const unread = t.latestAttentionAt > (t.lastReadAt ?? 0);
  // Ошибку, которую уже видели, не держим в списке весь период.
  if (t.status === "error") return unread ? "error" : null;
  if (t.hasPendingInteraction) return "pending";
  if (lastAnswer && lastQuestion(lastAnswer)) return "question";
  if (unread) return "unread";
  return null;
}

export interface WaitingItem {
  threadId: string;
  title: string;
  reason: WaitReason;
  line: string;
  updatedAt: number;
}

export function rankWaiting(items: WaitingItem[], limit: number): WaitingItem[] {
  return [...items].sort((a, b) => RANK[a.reason] - RANK[b.reason] || b.updatedAt - a.updatedAt).slice(0, limit);
}
