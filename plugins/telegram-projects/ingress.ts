import type { ChatInput } from "./chat";

type Attachment = NonNullable<ChatInput["files"]>[number];
const extension: Record<string, string> = {
  "audio/mpeg": "mp3",
  "audio/mp4": "m4a",
  "audio/x-m4a": "m4a",
  "audio/ogg": "ogg",
  "audio/wav": "wav",
  "audio/x-wav": "wav",
  "audio/flac": "flac",
  "video/mp4": "mp4",
  "video/quicktime": "mov",
  "video/webm": "webm",
};
const ext = (mime: string) =>
  extension[mime] ?? /^(?:audio|video|image)\/(?:x-)?([a-z0-9]{1,8})$/.exec(mime)?.[1] ?? "bin";
/** A file name the agent can read: no folders, no control characters. */
const fileName = (name: unknown, fallback: string) =>
  (typeof name === "string" ? name : "")
    .replace(/[\u0000-\u001f\u007f/\\]+/g, " ")
    .trim()
    .slice(0, 200) || fallback;
/** The sender sets the mime type: a strange or over-long one is dropped, so the update always fits the bridge's schema. */
const mimeOf = (value: unknown, max: number) =>
  typeof value === "string" && value.length <= max && /^[\w.+-]+\/[\w.+-]+$/.test(value)
    ? value.toLowerCase()
    : undefined;
const count = (value: unknown) => Math.max(0, Number(value) || 0);
/** The file a message carries: a photo (its largest size), a document, an audio, a video or a video note. Voice stays voice. */
export function attachmentOf(m: any): Attachment | undefined {
  const photo = Array.isArray(m?.photo) ? m.photo[m.photo.length - 1] : undefined;
  if (typeof photo?.file_id === "string")
    return {
      kind: "photo",
      fileId: photo.file_id,
      name: `photo-${m.message_id}.jpg`,
      mime: "image/jpeg",
      size: count(photo.file_size),
    };
  // An animation (GIF) also carries its document: the document is enough.
  const [kind, f, mime] = m?.document
    ? (["document", m.document, "application/octet-stream"] as const)
    : m?.audio
      ? (["audio", m.audio, "audio/mpeg"] as const)
      : m?.video
        ? (["video", m.video, "video/mp4"] as const)
        : m?.video_note
          ? (["video_note", m.video_note, "video/mp4"] as const)
          : [];
  if (!kind || typeof f?.file_id !== "string") return;
  const type = mimeOf(f.mime_type, 255) ?? mime;
  return {
    kind,
    fileId: f.file_id,
    name: fileName(f.file_name, `${kind.replace("_", "-")}-${m.message_id}.${ext(type)}`),
    mime: type,
    size: count(f.file_size),
  };
}

/** A phase takes words: a reply with a file or a voice note is refused — once for a whole album. */
export function phaseReply(
  input: { files?: unknown[]; voice?: unknown; group?: string },
  refusedAlbums: Set<string>,
): "text" | "refuse" | "quiet" {
  if (!input.files?.length && !input.voice) return "text";
  if (!input.group) return "refuse";
  if (refusedAlbums.has(input.group)) return "quiet";
  if (refusedAlbums.size >= 100) refusedAlbums.clear();
  refusedAlbums.add(input.group);
  return "refuse";
}

export type Ingress =
  | { kind: "chat"; input: ChatInput; callbackId?: string }
  | { kind: "tasks"; topicId: number }
  | { kind: "pair"; userId: number }
  | { kind: "stranger"; chatId: number }
  | { kind: "refuse"; topicId: number }
  | { kind: "ignore"; callbackId?: string };

// The plugin is the bot's only getUpdates receiver. The owner gate precedes everything:
// groups and other users are ignored. Before pairing, only "/start <code>" in a
// private chat is accepted, and only with the code shown on the plugin page.
export function routeUpdate(
  u: any,
  owner: number | undefined,
  pairCode?: string,
  single = false,
): Ingress {
  const cq = u?.callback_query;
  const m = cq ? cq.message : u?.message;
  const from = cq ? cq.from : u?.message?.from;
  const callbackId = cq ? String(cq.id) : undefined;
  if (!m || m.chat?.type !== "private" || from?.id !== m.chat?.id)
    return { kind: "ignore", callbackId };
  if (owner === undefined) {
    const code = /^\/start(?:@\w+)?\s+(\S+)\s*$/.exec(m.text ?? "")?.[1];
    return !cq && pairCode && code === pairCode
      ? { kind: "pair", userId: from.id }
      : { kind: "ignore", callbackId };
  }
  // A stranger chat gets one sentence (the server says it once per person), never the conversation.
  if (from.id !== owner) return cq ? { kind: "ignore", callbackId } : { kind: "stranger", chatId: m.chat.id };
  const callback: string | undefined = cq?.data;
  if (callback && !callback.startsWith("bb:") && !callback.startsWith("ph:"))
    return { kind: "ignore", callbackId };
  // A file goes to BB as an attachment; its caption is the message's words.
  const file = callback ? undefined : attachmentOf(m);
  const text: string | undefined =
    typeof m.text === "string"
      ? m.text
      : file && typeof m.caption === "string"
        ? m.caption
        : m.forum_topic_created && !single
          ? "/project"
          : undefined;
  // One chat without topics: everything is topic 0, whatever thread Telegram reports.
  const topicId = single ? 0 : (m.message_thread_id ?? 0);
  if (!callback && !file && text?.match(/^\/tasks(?:@\w+)?(?:\s|$)/))
    return { kind: "tasks", topicId };
  // Not text: a sticker, a location, a contact — a polite refusal instead of silence.
  if (!callback && !text && !m.voice && !file)
    return { kind: "refuse", topicId };
  const v = m.voice;
  return {
    kind: "chat",
    callbackId,
    input: {
      updateId: u.update_id,
      ownerId: owner,
      chatId: owner,
      topicId,
      messageId: m.message_id,
      text: callback ? undefined : text?.slice(0, 16000),
      callback,
      replyTo: m.reply_to_message?.message_id,
      voice: v
        ? {
            fileId: v.file_id,
            mime: mimeOf(v.mime_type, 80) ?? "audio/ogg",
            size: count(v.file_size),
            duration: count(v.duration),
          }
        : undefined,
      // An album comes as one update per file with a shared media group: the bridge sends it as one message.
      ...(file
        ? {
            files: [file],
            ...(m.media_group_id ? { group: String(m.media_group_id).slice(0, 64) } : {}),
          }
        : {}),
    },
  };
}
