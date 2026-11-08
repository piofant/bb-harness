import test from "node:test";
import assert from "node:assert/strict";
import { routeUpdate as route } from "../ingress";
const routeUpdate = (u: unknown) => route(u, OWNER_ID);
const OWNER_ID = 100000001;
const chat = { id: OWNER_ID, type: "private" };
const from = { id: OWNER_ID };
test("owner text in a topic becomes a chat input", () => {
  const r = routeUpdate({
    update_id: 7,
    message: {
      message_id: 3,
      chat,
      from,
      message_thread_id: 42,
      text: "привет",
    },
  });
  assert.equal(r.kind, "chat");
  assert.deepEqual(r.kind === "chat" && r.input, {
    updateId: 7,
    ownerId: OWNER_ID,
    chatId: OWNER_ID,
    topicId: 42,
    messageId: 3,
    text: "привет",
    callback: undefined,
    replyTo: undefined,
    voice: undefined,
  });
});
test("other users, groups and foreign callbacks are ignored", () => {
  const other = { message_id: 1, chat, from: { id: 1 }, text: "x" };
  assert.equal(routeUpdate({ update_id: 1, message: other }).kind, "ignore");
  const group = {
    message_id: 1,
    chat: { id: OWNER_ID, type: "group" },
    from,
    text: "x",
  };
  assert.equal(routeUpdate({ update_id: 1, message: group }).kind, "ignore");
  const cb = routeUpdate({
    update_id: 2,
    callback_query: {
      id: "9",
      from,
      data: "sms:copy",
      message: { message_id: 1, chat },
    },
  });
  assert.deepEqual(cb, { kind: "ignore", callbackId: "9" });
});
test("/tasks is answered by the plugin; bb: callbacks and new topics reach the bridge", () => {
  assert.deepEqual(
    routeUpdate({
      update_id: 3,
      message: {
        message_id: 1,
        chat,
        from,
        message_thread_id: 5,
        text: "/tasks@example_bot",
      },
    }),
    { kind: "tasks", topicId: 5 },
  );
  const cb = routeUpdate({
    update_id: 4,
    callback_query: {
      id: "8",
      from,
      data: "bb:menu",
      message: { message_id: 2, chat, message_thread_id: 5 },
    },
  });
  assert.equal(cb.kind === "chat" && cb.input.callback, "bb:menu");
  assert.equal(cb.kind === "chat" && cb.callbackId, "8");
  const created = routeUpdate({
    update_id: 5,
    message: {
      message_id: 3,
      chat,
      from,
      message_thread_id: 6,
      forum_topic_created: { name: "x" },
    },
  });
  assert.equal(created.kind === "chat" && created.input.text, "/project");
});
test("before pairing only /start with the page code binds the owner", () => {
  const u = (text: string, id = 42) => ({
    update_id: 1,
    message: { message_id: 1, chat: { id, type: "private" }, from: { id }, text },
  });
  assert.deepEqual(route(u("/start 123456"), undefined, "123456"), { kind: "pair", userId: 42 });
  assert.equal(route(u("/start 999999"), undefined, "123456").kind, "ignore");
  assert.equal(route(u("привет"), undefined, "123456").kind, "ignore");
  assert.equal(route(u("/start 123456"), undefined, undefined).kind, "ignore");
  // Once paired, another person's private chat is a stranger: one sentence, never the conversation.
  assert.deepEqual(route(u("/start 123456", 7), 42, "123456"), { kind: "stranger", chatId: 7 });
});
test("the owner's sticker gets a polite refusal", () => {
  const sticker = { update_id: 5, message: { message_id: 9, chat, from, message_thread_id: 3, sticker: { file_id: "s" } } };
  assert.deepEqual(routeUpdate(sticker), { kind: "refuse", topicId: 3 });
});

test("single chat layout puts every message in topic 0 and ignores topic creation", () => {
  const msg = (extra: any) => ({ update_id: 5, message: { message_id: 9, chat: { id: 7, type: "private" }, from: { id: 7 }, ...extra } });
  const r = route(msg({ text: "hi", message_thread_id: 44 }), 7, undefined, true);
  assert.equal(r.kind === "chat" && r.input.topicId, 0);
  const created = route(msg({ forum_topic_created: { name: "x" } }), 7, undefined, true);
  assert.equal(created.kind, "refuse");
  const topics = route(msg({ text: "hi", message_thread_id: 44 }), 7);
  assert.equal(topics.kind === "chat" && topics.input.topicId, 44);
});

test("a photo, a document, an audio, a video and a video note become files; the caption is the words", () => {
  const msg = (extra: any) => ({ update_id: 9, message: { message_id: 21, chat, from, ...extra } });
  const shot = routeUpdate(
    msg({
      caption: "Что тут?",
      media_group_id: "13579",
      photo: [
        { file_id: "small", file_size: 10, width: 90, height: 90 },
        { file_id: "big", file_size: 900, width: 1280, height: 960 },
      ],
    }),
  );
  assert.equal(shot.kind, "chat");
  assert.equal(shot.kind === "chat" && shot.input.text, "Что тут?");
  assert.deepEqual(shot.kind === "chat" && shot.input.files, [
    { kind: "photo", fileId: "big", name: "photo-21.jpg", mime: "image/jpeg", size: 900 },
  ]);
  assert.equal(shot.kind === "chat" && shot.input.group, "13579");
  const file = (extra: any) => {
    const r = routeUpdate(msg(extra));
    assert.equal(r.kind, "chat");
    return r.kind === "chat" ? r.input : undefined;
  };
  const pdf = file({ document: { file_id: "d", file_name: "Лекция 5.pdf", mime_type: "application/pdf", file_size: 5 } });
  assert.deepEqual(pdf?.files, [{ kind: "document", fileId: "d", name: "Лекция 5.pdf", mime: "application/pdf", size: 5 }]);
  assert.equal(pdf?.text, undefined);
  assert.equal(pdf?.group, undefined);
  // An audio file is a file for the agent now, not a voice note to transcribe.
  const audio = file({ audio: { file_id: "a", mime_type: "audio/mp4", file_size: 7, duration: 5400 } });
  assert.equal(audio?.voice, undefined);
  assert.deepEqual(audio?.files, [{ kind: "audio", fileId: "a", name: "audio-21.m4a", mime: "audio/mp4", size: 7 }]);
  assert.equal(file({ video: { file_id: "v", file_size: 3 } })?.files?.[0].name, "video-21.mp4");
  assert.equal(file({ video_note: { file_id: "n", length: 240 } })?.files?.[0].name, "video-note-21.mp4");
  assert.equal(file({ audio: { file_id: "a", mime_type: "audio/aac" } })?.files?.[0].name, "audio-21.aac");
  // A GIF carries its document too: one file, not two.
  assert.equal(
    file({ animation: { file_id: "g" }, document: { file_id: "g", file_name: "fun.gif.mp4", mime_type: "video/mp4" } })?.files?.length,
    1,
  );
  // A voice note stays voice.
  const voice = file({ voice: { file_id: "o", mime_type: "audio/ogg", file_size: 4, duration: 3 } });
  assert.equal(voice?.voice?.fileId, "o");
  assert.equal(voice?.files, undefined);
});
test("a file name loses folders and control characters; a strange mime type falls back", () => {
  const r = routeUpdate({
    update_id: 9,
    message: {
      message_id: 4,
      chat,
      from,
      document: { file_id: "d", file_name: "../../etc/\u0007passwd", mime_type: "bad type" },
    },
  });
  const f = r.kind === "chat" ? r.input.files?.[0] : undefined;
  assert.equal(f?.name, ".. .. etc passwd");
  assert.equal(f?.mime, "application/octet-stream");
  const unnamed = routeUpdate({ update_id: 9, message: { message_id: 4, chat, from, document: { file_id: "d" } } });
  assert.equal(unnamed.kind === "chat" && unnamed.input.files?.[0].name, "document-4.bin");
});
test("a file whose caption is /tasks goes to the agent, not to the task list", () => {
  const r = routeUpdate({
    update_id: 9,
    message: { message_id: 4, chat, from, caption: "/tasks", document: { file_id: "d", file_name: "t.txt" } },
  });
  assert.equal(r.kind, "chat");
  assert.equal(r.kind === "chat" && r.input.text, "/tasks");
});
test("an over-long or odd mime type falls back, so the update always fits the bridge's schema", async () => {
  const { inputSchema } = await import("../chat");
  const long = "application/" + "x".repeat(300);
  const doc = routeUpdate({
    update_id: 9,
    message: { message_id: 4, chat, from, document: { file_id: "d", file_name: "a.bin", mime_type: long, file_size: -5 } },
  });
  assert.equal(doc.kind === "chat" && doc.input.files?.[0].mime, "application/octet-stream");
  assert.equal(doc.kind === "chat" && doc.input.files?.[0].size, 0);
  assert.ok(doc.kind === "chat" && inputSchema.safeParse(doc.input).success);
  const voice = routeUpdate({
    update_id: 10,
    message: { message_id: 5, chat, from, voice: { file_id: "o", mime_type: "audio/" + "y".repeat(100), file_size: 4, duration: 3 } },
  });
  assert.equal(voice.kind === "chat" && voice.input.voice?.mime, "audio/ogg");
  assert.ok(voice.kind === "chat" && inputSchema.safeParse(voice.input).success);
});
