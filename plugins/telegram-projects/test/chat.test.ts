import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  ChatBridge,
  inputSchema,
  splitText,
  type ChatInput,
  type TelegramFile,
} from "../chat";
import { TelegramFailure, type Store } from "../model";
const OWNER_ID = 100000001;
function fixture(
  opts: {
    single?: boolean;
    pinned?: boolean;
    confirm?: boolean;
    fetchFile?: (f: TelegramFile, signal: AbortSignal) => Promise<Uint8Array>;
    filesDeadlineMs?: number;
    retryMs?: number[];
  } = {},
) {
  const data = new Map<string, any>();
  const calls: any[] = [];
  const sent: any[] = [];
  let message = 10;
  let failDelivery = false;
  let gate: Promise<void> | undefined;
  let rejectHtml = false;
  let events: any[] = [];
  let interactions: any[] = [];
  const thread = {
    id: "thr_one",
    projectId: "proj_one",
    title: "Первый чат",
    status: "idle",
    providerId: "codex",
    visibility: "visible",
    environmentId: "env_one",
    archivedAt: null,
    deletedAt: null,
  };
  const store: Store = {
    get: (k) => structuredClone(data.get(k)),
    put: (k, v) => data.set(k, structuredClone(v)),
    del: (k) => {
      data.delete(k);
    },
    list: (p) =>
      [...data]
        .filter(([k]) => k.startsWith(p))
        .map(([key, value]) => ({ key, value: structuredClone(value) })),
    atomic: (f) => f(),
  };
  store.put("topic:proj_one", {
    key: "proj_one",
    name: "Проект",
    threadId: 100,
  });
  const sdk: any = {
    projects: {
      list: async () => [{ id: "proj_one", name: "Проект", kind: "standard" }],
      get: async () => ({ id: "proj_one", name: "Проект" }),
      attachments: {
        upload: async (a: any) => {
          calls.push(["upload", a]);
          return {
            type: String(a.mimeType).startsWith("image/") ? "localImage" : "localFile",
            name: a.filename,
            path: a.projectId + "/" + a.filename,
            sizeBytes: a.clientFile.length,
            mimeType: a.mimeType,
          };
        },
      },
    },
    environments: { get: async () => ({ path: "/work/project" }) },
    providers: {
      list: async () => [
        { id: "codex", displayName: "Codex", available: true },
      ],
      models: async () => ({
        models: [{ model: "model-a", displayName: "Модель A" }],
        modelLoadError: null,
      }),
    },
    plugins: {
      list: async () => ({ plugins: [] }),
      callRpc: async () => ({
        folders: [
          {
            id: "folder",
            projectId: "proj_one",
            hostId: "mini",
            parentId: null,
            name: "Раздел",
            path: "/work/project/section",
          },
        ],
        roots: [],
      }),
    },
    threads: {
      defaultExecutionOptions: async () => ({ model: "model-a" }),
      get: async ({ threadId }: any) => ({ ...thread, id: threadId }),
      list: async () => [thread],
      spawn: async (a: any) => {
        calls.push(["spawn", a]);
        return thread;
      },
      send: async (a: any) => {
        calls.push(["send", a]);
      },
      stop: async (a: any) => calls.push(["stop", a]),
      unarchive: async (a: any) => calls.push(["unarchive", a]),
      update: async (a: any) => calls.push(["update", a]),
      output: async () => ({ output: "Ответ из BB" }),
      events: {
        list: async (a: any) =>
          a.order === "desc"
            ? [{ seq: 99 }]
            : events.filter((e) => e.seq > Number(a.afterSeq)),
      },
      interactions: {
        list: async () => interactions,
        get: async ({ interactionId }: any) =>
          interactions.find((i) => i.id === interactionId),
        resolve: async (a: any) => {
          calls.push(["resolve", a]);
          interactions[0].status = "resolved";
        },
      },
    },
  };
  const dir = mkdtempSync(join(tmpdir(), "bb-chat-test-"));
  const bridge = new ChatBridge({
    store,
    sdk,
    tg: async (method, payload) => {
      if (gate) {
        const wait = gate;
        gate = undefined;
        await wait;
      }
      if (rejectHtml && (payload as any).parse_mode === "HTML") {
        rejectHtml = false;
        throw new TelegramFailure("telegram_400", 0, false);
      }
      if (failDelivery) {
        failDelivery = false;
        throw new TelegramFailure("telegram_429", 20, false);
      }
      sent.push({ method, ...payload });
      return { message_id: ++message } as any;
    },
    spool: dir,
    owner: () => OWNER_ID,
    baseUrl: "https://bb.example",
    signal: new AbortController().signal,
    rich: () => false,
    single: () => opts.single === true,
    pinned: () => opts.pinned === true,
    confirmNewChat: () => opts.confirm === true,
    fetchFile: opts.fetchFile ?? (async () => new Uint8Array([1, 2, 3])),
    filesDeadlineMs: opts.filesDeadlineMs,
    retryMs: opts.retryMs ?? [1, 1],
  });
  let update = 0;
  const input = (text: string, topicId = opts.single ? 0 : 100): ChatInput => ({
    ownerId: OWNER_ID,
    chatId: OWNER_ID,
    topicId,
    messageId: ++update,
    updateId: update,
    text,
  });
  const press = async (kind: string) => {
    const action = [...data]
      .reverse()
      .find(([k, v]) => k.startsWith("chat:button:") && v.kind === kind);
    assert.ok(action, "button " + kind);
    await bridge.handle({
      ...input(""),
      callback: "bb:" + action[0].slice("chat:button:".length),
    });
  };
  return {
    data,
    store,
    calls,
    sent,
    sdk,
    thread,
    bridge,
    input,
    press,
    failNextDelivery: () => {
      failDelivery = true;
    },
    /** Telegram refuses the next HTML message as unparsable. */
    rejectNextHtml: () => {
      rejectHtml = true;
    },
    /** The next Telegram call waits for this promise. */
    holdNextDelivery: (p: Promise<void>) => {
      gate = p;
    },
    setEvents: (v: any[]) => {
      events = v;
    },
    setQuestions: (v: any[]) => {
      interactions = v;
    },
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}
test("new message creates visible BB chat; subsequent message reuses it", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("Первый запрос"));
    await f.bridge.handle(f.input("Продолжим"));
    assert.equal(f.calls[0][0], "spawn");
    assert.equal(f.calls[0][1].visibility, "visible");
    assert.deepEqual(f.calls[0][1].environment, { type: "project-default" });
    assert.equal(f.calls[1][0], "send");
    assert.equal(f.calls[1][1].threadId, "thr_one");
  } finally {
    f.cleanup();
  }
});
test("custom Telegram topic can bind a project before sending", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("/project", 777));
    const [key] = [...f.data].find(
      ([k, v]) => k.startsWith("chat:button:") && v.kind === "project",
    )!;
    await f.bridge.handle({
      ...f.input("", 777),
      callback: "bb:" + key.slice(12),
    });
    await f.bridge.handle(f.input("Из новой темы", 777));
    assert.equal(f.calls[0][1].projectId, "proj_one");
    assert.equal(
      f.bridge.bindings().find((x) => x.topicId === 777)?.threadId,
      "thr_one",
    );
  } finally {
    f.cleanup();
  }
});
test("foreign identity rejected at ingress", () => {
  const f = fixture();
  try {
    assert.throws(() => f.bridge.accept({ ...f.input("hi"), ownerId: 22 }));
    assert.equal(f.data.size, 1);
  } finally {
    f.cleanup();
  }
});
test("new chat and disconnect never delete or stop original", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("hello"));
    await f.bridge.handle(f.input("/new"));
    assert.equal(f.bridge.bindings()[0].threadId, null);
    await f.bridge.handle(f.input("/disconnect"));
    await f.bridge.handle(f.input("must not send"));
    assert.equal(f.calls.length, 1);
  } finally {
    f.cleanup();
  }
});
test("connecting an existing chat baselines old events and rejects stale controls", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("/chats"));
    await f.press("inspect");
    const button = [...f.data].find(
      ([k, v]) => k.startsWith("chat:button:") && v.kind === "connect",
    )!;
    await f.press("connect");
    assert.equal(f.bridge.bindings()[0].cursor, 99);
    await f.bridge.handle({
      ...f.input(""),
      callback: "bb:" + button[0].slice(12),
    });
    assert.equal(f.calls.length, 0);
    assert.ok([...f.data.values()].some((v) => v.text?.includes("устарела")));
  } finally {
    f.cleanup();
  }
});
test("stop requires a specific confirmation", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("hello"));
    await f.bridge.handle(f.input("/stop"));
    assert.equal(f.calls.length, 1);
    await f.press("stop");
    assert.equal(f.calls[1][0], "stop");
  } finally {
    f.cleanup();
  }
});
test("section choice sets exact host and folder for native spawn", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("/section"));
    const entry = [...f.data].find(
      ([k, v]) =>
        k.startsWith("chat:button:") &&
        v.kind === "folder" &&
        v.arg === "folder",
    )!;
    await f.bridge.handle({
      ...f.input(""),
      callback: "bb:" + entry[0].slice(12),
    });
    await f.bridge.handle(f.input("Run"));
    assert.deepEqual(f.calls[0][1].environment, {
      type: "host",
      hostId: "mini",
      workspace: { type: "unmanaged", path: "/work/project/section" },
    });
  } finally {
    f.cleanup();
  }
});
test("model menu uses BB catalog and does not alter other chats", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("/model"));
    const p = [...f.data].find(
      ([k, v]) =>
        k.startsWith("chat:button:") &&
        v.kind === "provider" &&
        v.arg === "codex",
    )!;
    await f.bridge.handle({ ...f.input(""), callback: "bb:" + p[0].slice(12) });
    await f.press("model");
    await f.bridge.handle(f.input("Run"));
    assert.equal(f.calls[0][1].model, "model-a");
    assert.equal(f.calls[0][1].providerId, "codex");
  } finally {
    f.cleanup();
  }
});
test("only final agent text sent once, not tool commentary or previous turns", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("Run"));
    f.setEvents([
      {
        seq: 1,
        type: "item/completed",
        scope: { kind: "turn", turnId: "turn" },
        data: { item: { type: "agentMessage", text: "Проверяю" } },
      },
      {
        seq: 2,
        type: "item/completed",
        scope: { kind: "turn", turnId: "turn" },
        data: { item: { type: "agentMessage", text: "Готовый ответ" } },
      },
      {
        seq: 3,
        type: "turn/completed",
        scope: { kind: "turn", turnId: "turn" },
        data: { status: "completed" },
      },
    ]);
    await f.bridge.observe();
    await f.bridge.observe();
    await f.bridge.flush();
    assert.equal(f.sent.filter((x) => x.text === "Готовый ответ").length, 1);
    assert.equal(f.sent.filter((x) => x.text === "Проверяю").length, 0);
  } finally {
    f.cleanup();
  }
});
test("queued answer from old binding never enters new conversation", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("Run"));
    f.setEvents([
      {
        seq: 1,
        type: "item/completed",
        scope: { kind: "turn", turnId: "turn" },
        data: { item: { type: "agentMessage", text: "Старый ответ" } },
      },
      {
        seq: 2,
        type: "turn/completed",
        scope: { kind: "turn", turnId: "turn" },
        data: { status: "completed" },
      },
    ]);
    await f.bridge.observe();
    await f.bridge.handle(f.input("/new"));
    await f.bridge.flush();
    assert.equal(f.sent.filter((x) => x.text === "Старый ответ").length, 0);
  } finally {
    f.cleanup();
  }
});
test("duplicate accepted update and recovery never replay unknown mutation", () => {
  const f = fixture();
  try {
    const input = f.input("Run");
    f.bridge.accept(input);
    f.bridge.accept(input);
    assert.equal(f.store.list("chat:in:").length, 1);
    f.store.put("chat:in:" + input.updateId, {
      input,
      state: "claimed",
      at: Date.now(),
    });
    f.bridge.recover();
    assert.equal(f.store.get<any>("chat:in:" + input.updateId).state, "done");
    assert.equal(f.calls.length, 0);
  } finally {
    f.cleanup();
  }
});
test("approval callback resolves once; later stale tap does not execute again", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("Run"));
    f.setQuestions([
      {
        id: "ask",
        status: "pending",
        payload: {
          kind: "approval",
          reason: "Команда",
          subject: { kind: "command", command: "ls" },
          availableDecisions: ["allow_once", "deny"],
        },
      },
    ]);
    await f.bridge.observe();
    await f.press("resolve");
    assert.equal(f.calls.filter((x) => x[0] === "resolve").length, 1);
    await f.press("resolve");
    assert.equal(f.calls.filter((x) => x[0] === "resolve").length, 1);
  } finally {
    f.cleanup();
  }
});
test("pending BB question prevents normal text from steering past it", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("Run"));
    f.setQuestions([
      {
        id: "ask",
        status: "pending",
        payload: { kind: "plugin", title: "Form" },
      },
    ]);
    await f.bridge.handle(f.input("yes"));
    assert.equal(f.calls.length, 1);
  } finally {
    f.cleanup();
  }
});
test("long text splitting preserves content and surrogate pairs", () => {
  const text = "a".repeat(3499) + "😀" + "b".repeat(4000);
  const chunks = splitText(text);
  assert.equal(chunks.join(""), text);
  assert.ok(
    chunks.every((x) => x.length <= 3500 && !/[\uD800-\uDBFF]$/.test(x)),
  );
});

test("progress edits its previous card instead of adding a new one", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("Run"));
    await f.bridge.observe();
    await f.bridge.flush();
    f.thread.status = "error";
    await f.bridge.observe();
    await f.bridge.flush();
    assert.equal(
      f.sent.filter((x) => x.method === "editMessageText").length,
      1,
    );
  } finally {
    f.cleanup();
  }
});
test("429 retains message and confirmed receipts avoid replay", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("/menu"));
    const entry = f.store.list<any>("chat:out:")[0];
    f.failNextDelivery();
    await f.bridge.flush();
    assert.equal(f.sent.length, 0);
    assert.equal(f.store.get<any>(entry.key).error, "telegram_429");
    await f.bridge.flush();
    assert.equal(f.sent.length, 0);
    f.store.put(entry.key, { ...entry.value, retryAt: 0 });
    await f.bridge.flush();
    await f.bridge.flush();
    assert.equal(f.sent.length, 1);
  } finally {
    f.cleanup();
  }
});
test("free text answers exact pending question; closed question never reopens", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("Run"));
    const q = {
      id: "q",
      status: "pending",
      payload: {
        kind: "user_question",
        questions: [
          {
            id: "q1",
            prompt: "Ответ?",
            allowFreeText: true,
            multiSelect: false,
            options: [],
          },
        ],
      },
    };
    f.setQuestions([q]);
    await f.bridge.observe();
    await f.bridge.flush();
    const record = f.store.list<any>("chat:question:")[0];
    assert.ok(record);
    const replyTo = Number(record.key.split(":").at(-1));
    await f.bridge.handle({ ...f.input("Мой ответ"), replyTo });
    assert.equal(f.calls.filter((x) => x[0] === "resolve").length, 1);
    assert.equal(f.calls.at(-1)[1].resolution.answers.q1.freeText, "Мой ответ");
    await f.bridge.handle({ ...f.input("Ещё ответ"), replyTo });
    assert.equal(f.calls.filter((x) => x[0] === "resolve").length, 1);
  } finally {
    f.cleanup();
  }
});

test("folder button hides host path; callback menu edits original card", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("/section"));
    await f.bridge.flush();
    const labels = f.sent
      .at(-1)
      .reply_markup.inline_keyboard.flat()
      .map((b: any) => b.text);
    assert.ok(
      labels.every((s: string) => !s.includes("/")),
      JSON.stringify(labels),
    );
    const action = [...f.data]
      .reverse()
      .find(([k, v]) => k.startsWith("chat:button:") && v.kind === "menu")!;
    await f.bridge.handle({
      ...f.input(""),
      messageId: 12345,
      callback: "bb:" + action[0].slice(12),
    });
    await f.bridge.flush();
    assert.equal(f.sent.at(-1).method, "editMessageText");
    assert.equal(f.sent.at(-1).message_id, 12345);
  } finally {
    f.cleanup();
  }
});

test("CLI profile selection follows model and travels only in initial native prompt", async () => {
  const f = fixture();
  try {
    f.sdk.projects.get = async () => ({
      id: "proj_one",
      name: "Проект",
      sources: [{ hostId: "host_one", path: "/work/project", isDefault: true }],
    });
    f.sdk.plugins.list = async () => ({
      plugins: [{ id: "cli-agents", status: "running" }],
    });
    const previous = f.sdk.plugins.callRpc;
    f.sdk.plugins.callRpc = async (a: any) => {
      if (a.pluginId !== "cli-agents") return previous(a);
      if (a.method === "catalog")
        return {
          supported: true,
          warnings: [],
          agents: [{ id: "reviewer", description: "Review" }],
        };
      assert.equal(a.input.hostId, "host_one");
      assert.equal(a.input.projectId, "proj_one");
      assert.equal(a.input.agentId, "reviewer");
      return {
        token: "11111111-1111-4111-8111-111111111111",
        label: "Agent: reviewer",
      };
    };
    await f.bridge.handle(f.input("/model"));
    await f.press("provider");
    await f.press("model");
    assert.ok(
      [...f.data.values()].some(
        (v) => v.kind === "profile" && v.arg === "reviewer",
      ),
    );
    const reviewer = [...f.data].find(
      ([k, v]) =>
        k.startsWith("chat:button:") &&
        v.kind === "profile" &&
        v.arg === "reviewer",
    )!;
    await f.bridge.handle({
      ...f.input(""),
      callback: "bb:" + reviewer[0].slice(12),
    });
    await f.bridge.flush();
    assert.ok(f.sent.at(-1).text.includes("🎭 Профиль: reviewer"));
    assert.ok(f.sent.at(-1).text.includes("🧠 Модель: Модель A"));
    assert.ok(
      !f.sent
        .at(-1)
        .reply_markup.inline_keyboard.flat()
        .some((b: any) => b.text.includes("Остановить")),
    );
    await f.bridge.handle(f.input("Check this"));
    assert.equal(
      f.calls[0][1].prompt,
      "[cli-agents-selection:11111111-1111-4111-8111-111111111111]\nCheck this",
    );
    assert.equal(f.calls[0][1].model, "model-a");
    await f.bridge.handle(f.input("Next"));
    assert.equal(f.calls[1][1].input[0].text, "Next");
  } finally {
    f.cleanup();
  }
});

test("profile pages show eight agents, paired buttons, bounded navigation and default", async () => {
  const f = fixture();
  try {
    f.sdk.projects.get = async () => ({
      id: "proj_one",
      name: "Project",
      sources: [{ hostId: "host_one", path: "/work", isDefault: true }],
    });
    f.sdk.plugins.list = async () => ({
      plugins: [{ id: "cli-agents", status: "running" }],
    });
    f.sdk.plugins.callRpc = async () => ({
      supported: true,
      warnings: [],
      agents: Array.from({ length: 19 }, (_, i) => ({
        id: "role-" + i,
        description: "",
      })),
    });
    await f.bridge.handle(f.input("/model"));
    await f.press("provider");
    await f.press("model");
    await f.bridge.flush();
    const buttons = () => f.sent.at(-1).reply_markup.inline_keyboard;
    assert.deepEqual(
      buttons()
        .slice(0, 4)
        .map((r: any) => r.length),
      [2, 2, 2, 2],
    );
    assert.ok(f.sent.at(-1).text.includes("1 / 3"));
    assert.ok(!JSON.stringify(buttons()).includes("role-8"));
    const clickNext = async () => {
      const next = [...f.data]
        .reverse()
        .find(
          ([k, v]) =>
            k.startsWith("chat:button:") &&
            v.kind === "profiles" &&
            v.arg === String(f.sent.at(-1).text.includes("1 / 3") ? 8 : 16),
        );
      assert.ok(next);
      await f.bridge.handle({
        ...f.input(""),
        callback: "bb:" + next[0].slice(12),
      });
      await f.bridge.flush();
    };
    await clickNext();
    assert.ok(f.sent.at(-1).text.includes("2 / 3"));
    assert.equal(buttons()[0][0].text, "role-8");
    await clickNext();
    assert.ok(f.sent.at(-1).text.includes("3 / 3"));
    assert.equal(buttons()[0][0].text, "role-16");
    assert.ok(
      !buttons()
        .flat()
        .some((b: any) => b.text.includes("Далее")),
    );
    assert.ok(
      buttons()
        .flat()
        .some((b: any) => b.text === "По умолчанию BB"),
    );
  } finally {
    f.cleanup();
  }
});

test("Projects & Sections target is passed to CLI Agents; absent CLI Agents skips picker", async () => {
  const f = fixture();
  try {
    f.sdk.plugins.list = async () => ({
      plugins: [
        { id: "project-folders", status: "running" },
        { id: "cli-agents", status: "running" },
      ],
    });
    f.sdk.environments.list = async () => [
      {
        id: "env_section",
        hostId: "mini",
        projectId: "proj_one",
        path: "/work/project/section",
      },
    ];
    const original = f.sdk.plugins.callRpc;
    let seen: any;
    f.sdk.plugins.callRpc = async (a: any) => {
      if (a.pluginId === "project-folders") return original(a);
      seen = a.input;
      return {
        supported: true,
        warnings: [],
        agents: [{ id: "section-role", description: "" }],
      };
    };
    await f.bridge.handle(f.input("/section"));
    const section = [...f.data].find(
      ([k, v]) =>
        k.startsWith("chat:button:") &&
        v.kind === "folder" &&
        v.arg === "folder",
    )!;
    await f.bridge.handle({
      ...f.input(""),
      callback: "bb:" + section[0].slice(12),
    });
    await f.bridge.handle(f.input("/model"));
    await f.press("provider");
    await f.press("model");
    await f.bridge.flush();
    assert.equal(seen.hostId, "mini");
    assert.equal(seen.environmentId, "env_section");
    assert.equal(seen.cwd, "/work/project/section");
    assert.ok(f.sent.at(-1).text.includes("Выбери профиль"));
    f.sdk.plugins.list = async () => ({
      plugins: [{ id: "project-folders", status: "running" }],
    });
    seen = null;
    await f.bridge.handle(f.input("/model"));
    await f.press("provider");
    await f.press("model");
    await f.bridge.flush();
    assert.equal(seen, null);
    assert.ok(f.sent.at(-1).text.includes("Напиши первое сообщение"));
    assert.ok(!f.sent.at(-1).text.includes("Выбери профиль"));
  } finally {
    f.cleanup();
  }
});

test("server selection routes catalogs and new chat to that project's remote source", async () => {
  const f = fixture();
  try {
    f.sdk.hosts = {
      list: async () => [{ id: "ovh", name: "OVH", status: "connected" }],
      get: async () => ({ id: "ovh", name: "OVH", status: "connected" }),
    };
    f.sdk.projects.get = async () => ({
      id: "proj_one",
      sources: [{ hostId: "ovh", path: "/srv/project", isDefault: false }],
    });
    const routes: any[] = [];
    f.sdk.providers.list = async (a: any) => {
      routes.push(a);
      return [{ id: "codex", displayName: "Codex", available: true }];
    };
    await f.bridge.handle(f.input("/server"));
    await f.press("host");
    await f.press("provider");
    await f.press("model");
    await f.bridge.handle(f.input("Hello remote"));
    const spawn = f.calls.find((c) => c[0] === "spawn")[1];
    assert.deepEqual(spawn.environment, {
      type: "host",
      hostId: "ovh",
      workspace: { type: "unmanaged", path: "/srv/project" },
    });
    assert.ok(routes.some((r) => r.hostId === "ovh"));
  } finally {
    f.cleanup();
  }
});

test("empty native profile catalog explains fallback after model selection", async () => {
  const f = fixture();
  try {
    f.sdk.projects.get = async () => ({
      id: "proj_one",
      sources: [{ hostId: "mini", path: "/work/project", isDefault: true }],
    });
    f.sdk.plugins.list = async () => ({
      plugins: [{ id: "cli-agents", status: "running" }],
    });
    f.sdk.plugins.callRpc = async () => ({
      supported: true,
      agents: [],
      warnings: [],
    });
    await f.bridge.handle(f.input("/model"));
    await f.press("provider");
    await f.press("model");
    await f.bridge.flush();
    assert.ok(f.sent.at(-1).text.includes("профили не найдены"));
  } finally {
    f.cleanup();
  }
});

test("single chat: first text without a binding asks for a project, then chats in topic 0", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("Привет", 0));
    assert.equal(f.calls.length, 0);
    assert.ok([...f.data.values()].some((v) => v.text?.includes("Выбери проект для нового чата")));
    await f.press("project");
    await f.bridge.handle(f.input("Первый запрос", 0));
    assert.equal(f.calls[0][0], "spawn");
    assert.equal(f.bridge.bindings()[0].topicId, 0);
    assert.ok(f.store.get("chat:touched:thr_one"));
  } finally {
    f.cleanup();
  }
});
test("single chat: a reply to an agent card switches to that thread and sends there", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый запрос", 0));
    f.bridge.remember(555, "thr_other", "proj_one");
    await f.bridge.handle({ ...f.input("Ответ агенту", 0), replyTo: 555 });
    const send = f.calls.find((c) => c[0] === "send");
    assert.equal(send?.[1].threadId, "thr_other");
    assert.equal(f.bridge.bindings()[0].threadId, "thr_other");
    assert.equal(f.bridge.bindings()[0].cursor, 99);
    assert.ok([...f.data.values()].some((v) => v.text?.startsWith("↪️ Пишу в чат")));
    assert.ok(f.store.get("chat:touched:thr_other"));
    // A plain message afterwards stays in the switched thread.
    await f.bridge.handle(f.input("Ещё", 0));
    assert.equal(f.calls.at(-1)?.[1].threadId, "thr_other");
  } finally {
    f.cleanup();
  }
});
test("single chat: a reply to an unknown message just continues the current thread", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый", 0));
    await f.bridge.handle({ ...f.input("Второй", 0), replyTo: 9999 });
    assert.equal(f.calls.at(-1)?.[0], "send");
    assert.equal(f.calls.at(-1)?.[1].threadId, "thr_one");
  } finally {
    f.cleanup();
  }
});
test("single chat: old topic bindings are inert and their queued messages are dropped", async () => {
  const f = fixture({ single: true });
  try {
    f.store.put("chat:binding:100", { topicId: 100, projectId: "proj_one", threadId: "thr_one", revision: "r", ready: false, cursor: 0 });
    f.store.put("chat:out:old", { topicId: 100, text: "старое", retryAt: 0, at: 1 });
    assert.equal(f.bridge.bindings().length, 0);
    await f.bridge.flush();
    assert.equal(f.sent.length, 0);
    assert.equal(f.store.get<any>("chat:out:old").sentId, -1);
  } finally {
    f.cleanup();
  }
});
test("single chat: an agent reply sent to Telegram is remembered for reply routing", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый", 0));
    f.setEvents([
      { seq: 1, type: "item/completed", scope: { kind: "turn", turnId: "t1" }, data: { item: { type: "agentMessage", text: "Готово" } } },
      { seq: 2, type: "turn/completed", scope: { kind: "turn", turnId: "t1" }, data: { status: "completed" } },
    ]);
    await f.bridge.observe();
    await f.bridge.flush();
    const reply = f.sent.find((m) => m.text === "Готово");
    assert.ok(reply);
    assert.equal(reply.message_thread_id, undefined);
    const id = [...f.data].find(([k, v]) => k.startsWith("chat:out:event:") && v.sentId)?.[1].sentId;
    assert.equal(f.store.get<any>("chat:msgthread:" + id)?.threadId, "thr_one");
  } finally {
    f.cleanup();
  }
});
test("single chat: every part of a long agent answer routes replies to its thread", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый", 0));
    const long = "а".repeat(3000) + "\n" + "б".repeat(3000);
    f.setEvents([
      { seq: 1, type: "item/completed", scope: { kind: "turn", turnId: "t1" }, data: { item: { type: "agentMessage", text: long } } },
      { seq: 2, type: "turn/completed", scope: { kind: "turn", turnId: "t1" }, data: { status: "completed" } },
    ]);
    await f.bridge.observe();
    await f.bridge.flush();
    const parts = [...f.data].filter(([k]) => k.startsWith("chat:receipt:chat:out:event:")).map(([, v]) => v);
    assert.equal(parts.length, 2);
    for (const id of parts) assert.equal(f.store.get<any>("chat:msgthread:" + id)?.threadId, "thr_one");
  } finally {
    f.cleanup();
  }
});
test("single chat: connect button on a card from another project switches project and thread", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) =>
      a.threadId === "thr_q" ? { ...(await realGet(a)), projectId: "proj_two" } : realGet(a);
    f.sdk.projects.get = async ({ projectId }: any) => ({ id: projectId, name: projectId });
    const key = f.bridge.connectButton(0, "thr_q");
    assert.ok(key?.callback_data);
    await f.bridge.handle({ ...f.input("", 0), callback: key!.callback_data });
    const b = f.bridge.bindings()[0];
    assert.equal(b.projectId, "proj_two");
    assert.equal(b.threadId, "thr_q");
  } finally {
    f.cleanup();
  }
});
test("single chat: a reply to an archived thread's card is refused and keeps the current chat", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый", 0));
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) =>
      a.threadId === "thr_old" ? { ...(await realGet(a)), archivedAt: "2026-10-01" } : realGet(a);
    f.bridge.remember(777, "thr_old", "proj_one");
    const before = f.calls.length;
    await f.bridge.handle({ ...f.input("Туда", 0), replyTo: 777 });
    assert.equal(f.calls.length, before);
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
    assert.ok([...f.data.values()].some((v) => v.text?.includes("недоступен")));
  } finally {
    f.cleanup();
  }
});
test("single chat: a reply to an old question card answers it after switching back", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый", 0));
    f.setQuestions([
      {
        id: "q1",
        status: "pending",
        payload: { kind: "user_question", questions: [{ id: "a", question: "Как назвать?", allowFreeText: true, options: [] }] },
      },
    ]);
    await f.bridge.observe();
    await f.bridge.flush();
    const card = [...f.data].find(([k]) => k.startsWith("chat:question:0:"))!;
    const cardId = Number(card[0].split(":").at(-1));
    f.setQuestions([]);
    f.bridge.remember(888, "thr_other", "proj_one");
    await f.bridge.handle({ ...f.input("Сюда", 0), replyTo: 888 });
    assert.equal(f.bridge.bindings()[0].threadId, "thr_other");
    f.setQuestions([
      {
        id: "q1",
        status: "pending",
        payload: { kind: "user_question", questions: [{ id: "a", question: "Как назвать?", allowFreeText: true, options: [] }] },
      },
    ]);
    await f.bridge.handle({ ...f.input("Луна", 0), replyTo: cardId });
    const resolved = f.calls.find((c) => c[0] === "resolve");
    assert.equal(resolved?.[1].threadId, "thr_one");
    assert.equal(resolved?.[1].resolution.answers.a.freeText, "Луна");
  } finally {
    f.cleanup();
  }
});

async function pinnedChat() {
  const f = fixture({ single: true, pinned: true });
  await f.bridge.handle(f.input("/project", 0));
  await f.press("project");
  await f.bridge.handle(f.input("Первый", 0));
  assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
  return f;
}
const texts = (f: ReturnType<typeof fixture>) =>
  [...f.data].filter(([k]) => k.startsWith("chat:out:")).map(([, v]) => String(v.text));
test("pinned chat: a reply to another chat's card goes there once; the next message returns to the pinned chat", async () => {
  const f = await pinnedChat();
  try {
    f.bridge.remember(555, "thr_other", "proj_one");
    await f.bridge.handle({ ...f.input("Туда", 0), replyTo: 555 });
    assert.deepEqual(f.calls.at(-1), ["send", { threadId: "thr_other", mode: "auto", input: [{ type: "text", text: "Туда", mentions: [] }] }]);
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
    assert.ok(f.store.get("chat:touched:thr_other"));
    assert.ok(texts(f).some((t) => t.includes("Закреплённый чат не меняется")));
    assert.ok(!texts(f).some((t) => t.startsWith("↪️ Пишу в чат")));
    await f.bridge.handle(f.input("Ещё", 0));
    assert.equal(f.calls.at(-1)?.[0], "send");
    assert.equal(f.calls.at(-1)?.[1].threadId, "thr_one");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: report cards carry no connect button; without one chat the setting changes nothing", async () => {
  const f = await pinnedChat();
  const topics = fixture({ pinned: true });
  try {
    assert.equal(f.bridge.connectButton(0, "thr_q"), undefined);
    assert.ok(topics.bridge.connectButton(100, "thr_q")?.callback_data);
  } finally {
    f.cleanup();
    topics.cleanup();
  }
});
test("pinned chat: an archived chat whose environment lives comes back and gets the message", async () => {
  const f = await pinnedChat();
  try {
    let archived = true;
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) =>
      a.threadId === "thr_one" && archived ? { ...(await realGet(a)), archivedAt: 1 } : realGet(a);
    f.sdk.threads.unarchive = async (a: any) => {
      f.calls.push(["unarchive", a]);
      archived = false;
    };
    f.sdk.environments.get = async () => ({ path: "/work/project", status: "ready" });
    await f.bridge.handle(f.input("Снова", 0));
    assert.deepEqual(f.calls.slice(-2).map((c) => [c[0], c[1].threadId]), [["unarchive", "thr_one"], ["send", "thr_one"]]);
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
    assert.ok(texts(f).some((t) => t.startsWith("♻️ Вернул из архива закреплённый чат «Первый чат»")));
  } finally {
    f.cleanup();
  }
});
test("pinned chat: an archived chat whose worktree is torn down gives way to a new chat with the message", async () => {
  const f = await pinnedChat();
  try {
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) =>
      a.threadId === "thr_one" ? { ...(await realGet(a)), archivedAt: 1 } : realGet(a);
    f.sdk.environments.get = async () => ({ path: "/work/project", status: "destroyed" });
    f.sdk.threads.spawn = async (a: any) => {
      f.calls.push(["spawn", a]);
      return { ...f.thread, id: "thr_new" };
    };
    const old = f.bridge.bindings()[0];
    await f.bridge.handle(f.input("Новое", 0));
    assert.ok(!f.calls.some((c) => c[0] === "unarchive"));
    const spawn = f.calls.at(-1);
    assert.equal(spawn?.[0], "spawn");
    assert.equal(spawn?.[1].prompt, "Новое");
    assert.equal(spawn?.[1].projectId, "proj_one");
    const b = f.bridge.bindings()[0];
    assert.equal(b.threadId, "thr_new");
    assert.notEqual((b as any).revision, (old as any).revision);
    assert.ok(texts(f).some((t) => t.includes("начинаю новый с этим сообщением")));
    assert.ok(f.store.get("chat:touched:thr_new"));
  } finally {
    f.cleanup();
  }
});
test("pinned chat: a chat gone from BB (404) is replaced; a passing failure keeps the chat and reports an error", async () => {
  const f = await pinnedChat();
  try {
    f.sdk.threads.get = async () => {
      throw Object.assign(new Error("boom"), { status: 503 });
    };
    await assert.rejects(f.bridge.handle(f.input("Пока сбой", 0)));
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
    assert.ok(!f.calls.some((c) => c[0] === "spawn" && c[1].prompt === "Пока сбой"));
    f.sdk.threads.get = async () => {
      throw Object.assign(new Error("HTTP 404: Thread not found"), { status: 404, code: "thread_not_found" });
    };
    await f.bridge.handle(f.input("После удаления", 0));
    assert.equal(f.calls.at(-1)?.[0], "spawn");
    assert.equal(f.calls.at(-1)?.[1].prompt, "После удаления");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: after /disconnect a message starts a new chat right away, without the menu", async () => {
  const f = await pinnedChat();
  try {
    await f.bridge.handle(f.input("/disconnect", 0));
    assert.equal(f.bridge.bindings()[0].threadId, null);
    const before = f.calls.length;
    await f.bridge.handle(f.input("Дальше", 0));
    assert.equal(f.calls.length, before + 1);
    assert.equal(f.calls.at(-1)?.[0], "spawn");
    assert.equal(f.calls.at(-1)?.[1].prompt, "Дальше");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: /history and /stop in a reply act on the replied chat, never on the pinned one", async () => {
  const f = await pinnedChat();
  try {
    f.bridge.remember(556, "thr_other", "proj_one");
    await f.bridge.handle({ ...f.input("/history", 0), replyTo: 556 });
    const peek = [...f.data.values()].find((v) => String(v.text).startsWith("📖 Последний ответ"));
    assert.ok(peek?.keys?.[0]?.[0]?.url?.endsWith("/threads/thr_other"));
    await f.bridge.handle({ ...f.input("/stop", 0), replyTo: 556 });
    assert.ok(texts(f).some((t) => t.startsWith("Остановить запуск в чате, на чью карточку ты ответил?")));
    await f.press("stopOnce");
    assert.deepEqual(f.calls.at(-1), ["stop", { threadId: "thr_other" }]);
    assert.ok(!f.calls.some((c) => c[0] === "stop" && c[1].threadId === "thr_one"));
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: a reply to a previous pinned chat's question answers it without moving the chat", async () => {
  const f = await pinnedChat();
  try {
    f.setQuestions([
      {
        id: "q1",
        status: "pending",
        payload: { kind: "user_question", questions: [{ id: "a", question: "Как назвать?", allowFreeText: true, options: [] }] },
      },
    ]);
    await f.bridge.observe();
    await f.bridge.flush();
    const card = [...f.data].find(([k]) => k.startsWith("chat:question:0:"))!;
    const cardId = Number(card[0].split(":").at(-1));
    // The pinned chat moves on to another thread; the old question is still answerable by reply.
    f.store.put("chat:binding:0", { ...f.bridge.bindings()[0], threadId: "thr_two" });
    await f.bridge.handle({ ...f.input("Луна", 0), replyTo: cardId });
    const resolved = f.calls.find((c) => c[0] === "resolve");
    assert.equal(resolved?.[1].threadId, "thr_one");
    assert.equal(resolved?.[1].resolution.answers.a.freeText, "Луна");
    assert.equal(f.bridge.bindings()[0].threadId, "thr_two");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: /menu on a closed pinned chat explains what the next message does instead of failing", async () => {
  const f = await pinnedChat();
  try {
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) => ({ ...(await realGet(a)), archivedAt: 1 });
    await f.bridge.handle(f.input("/menu", 0));
    const menu = [...f.data.values()].find((v) => String(v.text).includes("Закреплённый чат закрыт в BB"));
    assert.ok(menu);
    assert.deepEqual(menu.keys.map((row: any[]) => row.map((k) => k.text)), [["✚ Новый чат", "💬 Чаты"], ["📂 Все проекты"]]);
  } finally {
    f.cleanup();
  }
});
test("one chat without the pin: an archived bound chat still fails the message, as before", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый", 0));
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) => ({ ...(await realGet(a)), archivedAt: 1 });
    await assert.rejects(f.bridge.handle(f.input("Второй", 0)), /thread_not_available/);
    assert.ok(!f.calls.some((c) => c[0] === "unarchive"));
  } finally {
    f.cleanup();
  }
});

const httpError = (status: number, code?: string) => Object.assign(new Error("HTTP " + status), { status, code });
test("pinned chat: a passing failure reading the environment keeps the chat; a 404 environment starts a new one", async () => {
  const f = await pinnedChat();
  try {
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) =>
      a.threadId === "thr_one" ? { ...(await realGet(a)), archivedAt: 1 } : realGet(a);
    f.sdk.environments.get = async () => {
      throw httpError(503);
    };
    await assert.rejects(f.bridge.handle(f.input("Сбой", 0)));
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
    assert.ok(!f.calls.some((c) => c[0] === "unarchive" || (c[0] === "spawn" && c[1].prompt === "Сбой")));
    f.sdk.environments.get = async () => {
      throw httpError(404, "environment_not_found");
    };
    await f.bridge.handle(f.input("Заново", 0));
    assert.equal(f.calls.at(-1)?.[0], "spawn");
    assert.equal(f.calls.at(-1)?.[1].prompt, "Заново");
    assert.equal(f.bridge.status().pinEvents[0].kind, "replaced:environment_gone");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: a live chat whose environment is torn down gives way to a new chat", async () => {
  const f = await pinnedChat();
  try {
    f.sdk.environments.get = async () => ({ path: "/work/project", status: "destroyed" });
    await f.bridge.handle(f.input("Дальше", 0));
    assert.ok(!f.calls.some((c) => c[0] === "unarchive" || (c[0] === "send" && c[1].input[0].text === "Дальше")));
    assert.equal(f.calls.at(-1)?.[0], "spawn");
    assert.equal(f.calls.at(-1)?.[1].prompt, "Дальше");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: BB refusing the unarchive starts a new chat; a passing unarchive failure keeps the chat", async () => {
  const f = await pinnedChat();
  try {
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) =>
      a.threadId === "thr_one" ? { ...(await realGet(a)), archivedAt: 1 } : realGet(a);
    f.sdk.environments.get = async () => ({ path: "/work/project", status: "ready" });
    f.sdk.threads.unarchive = async () => {
      throw httpError(503);
    };
    await assert.rejects(f.bridge.handle(f.input("Пока нет", 0)));
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
    assert.ok(!f.calls.some((c) => c[0] === "spawn" && c[1].prompt === "Пока нет"));
    f.sdk.threads.unarchive = async () => {
      throw httpError(409, "lifecycle_owner_archived");
    };
    await f.bridge.handle(f.input("Тогда новый", 0));
    assert.equal(f.calls.at(-1)?.[0], "spawn");
    assert.equal(f.calls.at(-1)?.[1].prompt, "Тогда новый");
    assert.equal(f.bridge.status().pinEvents[0].kind, "replaced:unarchive_refused");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: a deleted, then a hidden chat each give way to a new chat", async () => {
  const f = await pinnedChat();
  try {
    let spawned = 0;
    f.sdk.threads.spawn = async (a: any) => {
      f.calls.push(["spawn", a]);
      return { ...f.thread, id: "thr_s" + ++spawned };
    };
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) => {
      const t = await realGet(a);
      if (a.threadId === "thr_one") return { ...t, deletedAt: 1 };
      if (a.threadId === "thr_s1") return { ...t, visibility: "hidden" };
      return t;
    };
    await f.bridge.handle(f.input("Раз", 0));
    assert.equal(f.bridge.bindings()[0].threadId, "thr_s1");
    await f.bridge.handle(f.input("Два", 0));
    assert.equal(f.bridge.bindings()[0].threadId, "thr_s2");
    assert.deepEqual(f.bridge.status().pinEvents.map((e: any) => e.kind), ["replaced:hidden", "replaced:deleted"]);
    assert.deepEqual((f.bridge.bindings()[0] as any).before, ["thr_s1", "thr_one"]);
  } finally {
    f.cleanup();
  }
});
test("pinned chat: a report card's button sent before the pin no longer moves it; /chats still does", async () => {
  const opts = { single: true, pinned: false };
  const f = fixture(opts);
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый", 0));
    const key = f.bridge.connectButton(0, "thr_q");
    assert.ok(key?.callback_data);
    opts.pinned = true;
    await f.bridge.handle({ ...f.input("", 0), callback: key!.callback_data });
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
    assert.ok(texts(f).some((t) => t.startsWith("Закреплённый чат не меняется")));
    await f.bridge.handle(f.input("/use thr_q", 0));
    await f.press("connect");
    assert.equal(f.bridge.bindings()[0].threadId, "thr_q");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: a reply to a closed earlier pinned chat reaches the pinned one; a closed foreign chat is refused", async () => {
  const f = await pinnedChat();
  try {
    f.sdk.threads.spawn = async (a: any) => {
      f.calls.push(["spawn", a]);
      return { ...f.thread, id: "thr_new" };
    };
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) => {
      const t = await realGet(a);
      if (a.threadId === "thr_one" || a.threadId === "thr_x") return { ...t, archivedAt: 1 };
      if (a.threadId === "thr_new") return { ...t, environmentId: "env_new" };
      return t;
    };
    f.sdk.environments.get = async ({ environmentId }: any) => ({
      path: "/work/project",
      status: environmentId === "env_one" ? "destroyed" : "ready",
    });
    await f.bridge.handle(f.input("Новое", 0));
    assert.equal(f.bridge.bindings()[0].threadId, "thr_new");
    f.bridge.remember(600, "thr_one", "proj_one");
    await f.bridge.handle({ ...f.input("Продолжим", 0), replyTo: 600 });
    assert.deepEqual(f.calls.at(-1), ["send", { threadId: "thr_new", mode: "auto", input: [{ type: "text", text: "Продолжим", mentions: [] }] }]);
    assert.ok(texts(f).some((t) => t === "Тот чат закрыт в BB — пишу в закреплённый."));
    f.bridge.remember(601, "thr_x", "proj_one");
    const before = f.calls.length;
    await f.bridge.handle({ ...f.input("Туда", 0), replyTo: 601 });
    await f.bridge.handle({ ...f.input("/stop", 0), replyTo: 600 });
    assert.equal(f.calls.length, before);
    assert.equal(texts(f).filter((t) => t.includes("недоступен")).length, 2);
    assert.ok(!texts(f).some((t) => t.startsWith("Остановить текущий запуск")));
  } finally {
    f.cleanup();
  }
});
test("pinned chat: an answer to a closed pinned chat's question never revives it or starts a new chat", async () => {
  const f = await pinnedChat();
  try {
    const q1 = {
      id: "q1",
      status: "pending",
      payload: { kind: "user_question", questions: [{ id: "a", question: "Как назвать?", allowFreeText: true, options: [] }] },
    };
    f.setQuestions([q1]);
    await f.bridge.observe();
    await f.bridge.flush();
    const card = [...f.data].find(([k]) => k.startsWith("chat:question:0:"))!;
    const cardId = Number(card[0].split(":").at(-1));
    f.setQuestions([{ ...q1, status: "canceled" }]);
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) => ({ ...(await realGet(a)), archivedAt: 1 });
    f.sdk.environments.get = async () => ({ path: "/work/project", status: "destroyed" });
    const before = f.calls.length;
    await f.bridge.handle({ ...f.input("Да", 0), replyTo: cardId });
    assert.equal(f.calls.length, before);
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
    assert.ok(texts(f).some((t) => t === "Этот вопрос уже закрыт."));
  } finally {
    f.cleanup();
  }
});
test("pinned chat with confirmNewChat: the notice asks instead of promising a start", async () => {
  const f = fixture({ single: true, pinned: true, confirm: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый", 0));
    await f.bridge.handle(f.input("да", 0));
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
    const spawns = f.calls.filter((c) => c[0] === "spawn").length;
    f.sdk.threads.get = async () => {
      throw httpError(404, "thread_not_found");
    };
    await f.bridge.handle(f.input("Второй", 0));
    assert.equal(f.calls.filter((c) => c[0] === "spawn").length, spawns);
    assert.ok(texts(f).some((t) => t === "Закреплённый чат закрыт в BB."));
    assert.ok(!texts(f).some((t) => t.includes("начинаю новый")));
    assert.ok(texts(f).some((t) => t.startsWith("Начать новый чат BB с этим сообщением?")));
  } finally {
    f.cleanup();
  }
});
test("pinned chat: when the chat's project is gone the owner picks a project instead of a failing loop", async () => {
  const f = await pinnedChat();
  try {
    f.sdk.threads.get = async () => {
      throw httpError(404, "thread_not_found");
    };
    f.sdk.projects.get = async () => {
      throw httpError(404, "project_not_found");
    };
    await f.bridge.handle(f.input("Куда", 0));
    assert.equal(f.bridge.bindings().length, 0);
    assert.ok(!f.calls.some((c) => c[0] === "spawn" && c[1].prompt === "Куда"));
    assert.ok(texts(f).some((t) => t.startsWith("📂 Выбери проект для нового чата")));
    assert.equal(f.bridge.status().pinEvents[0].kind, "project_gone");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: replacing the chat still sends the old chat's queued answer", async () => {
  const f = await pinnedChat();
  try {
    const rev = (f.bridge.bindings()[0] as any).revision;
    f.store.put("chat:out:event:" + rev + ":last", { topicId: 0, text: "Последний ответ старого чата", revision: rev, retryAt: 0, at: 1 });
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) => ({ ...(await realGet(a)), deletedAt: 1 });
    await f.bridge.handle(f.input("Новое", 0));
    await f.bridge.flush();
    assert.ok(f.sent.some((m) => m.text === "Последний ответ старого чата"));
  } finally {
    f.cleanup();
  }
});

test("pinned chat: an environment in error is not gone — a live chat keeps the message, an archived one comes back", async () => {
  const f = await pinnedChat();
  try {
    f.sdk.environments.get = async () => ({ path: "/work/project", status: "error" });
    await f.bridge.handle(f.input("Живой", 0));
    assert.equal(f.calls.at(-1)?.[0], "send");
    assert.equal(f.calls.at(-1)?.[1].threadId, "thr_one");
    let archived = true;
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) => ({ ...(await realGet(a)), archivedAt: archived ? 1 : null });
    f.sdk.threads.unarchive = async (a: any) => {
      f.calls.push(["unarchive", a]);
      archived = false;
    };
    await f.bridge.handle(f.input("Из архива", 0));
    assert.deepEqual(f.calls.slice(-2).map((c) => c[0]), ["unarchive", "send"]);
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: an archived chat whose worktree is mid-teardown gives way to a new chat", async () => {
  const f = await pinnedChat();
  try {
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) => ({ ...(await realGet(a)), archivedAt: 1 });
    f.sdk.environments.get = async () => ({ path: "/work/project", status: "ready", lifecycle: { phase: "teardown" } });
    await f.bridge.handle(f.input("Новый", 0));
    assert.ok(!f.calls.some((c) => c[0] === "unarchive"));
    assert.equal(f.calls.at(-1)?.[0], "spawn");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: a passing environment read failure never blocks a live chat", async () => {
  const f = await pinnedChat();
  try {
    f.sdk.environments.get = async () => {
      throw httpError(503);
    };
    await f.bridge.handle(f.input("Всё равно", 0));
    assert.equal(f.calls.at(-1)?.[0], "send");
    assert.equal(f.calls.at(-1)?.[1].threadId, "thr_one");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: a passing unarchive failure other than BB's refusal keeps the chat", async () => {
  const f = await pinnedChat();
  try {
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) => ({ ...(await realGet(a)), archivedAt: 1 });
    f.sdk.environments.get = async () => ({ path: "/work/project", status: "ready" });
    f.sdk.threads.unarchive = async () => {
      throw httpError(429);
    };
    await assert.rejects(f.bridge.handle(f.input("Позже", 0)));
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
    assert.ok(!f.calls.some((c) => c[0] === "spawn" && c[1].prompt === "Позже"));
  } finally {
    f.cleanup();
  }
});
test("pinned chat: a passing project read failure after a closed chat keeps the binding and its events", async () => {
  const f = await pinnedChat();
  try {
    const old = f.bridge.bindings()[0];
    f.sdk.threads.get = async () => {
      throw httpError(404, "thread_not_found");
    };
    f.sdk.projects.get = async () => {
      throw httpError(503);
    };
    await assert.rejects(f.bridge.handle(f.input("Сбой проекта", 0)));
    assert.deepEqual(f.bridge.bindings()[0], old);
    assert.deepEqual(f.bridge.status().pinEvents, []);
  } finally {
    f.cleanup();
  }
});
test("pinned chat: the reply fallback refuses a passing failure and an earlier pinned chat's question", async () => {
  const f = await pinnedChat();
  try {
    f.setQuestions([
      {
        id: "q1",
        status: "pending",
        payload: { kind: "user_question", questions: [{ id: "a", question: "Как назвать?", allowFreeText: true, options: [] }] },
      },
    ]);
    await f.bridge.observe();
    await f.bridge.flush();
    const card = [...f.data].find(([k]) => k.startsWith("chat:question:0:"))!;
    const cardId = Number(card[0].split(":").at(-1));
    f.setQuestions([]);
    f.sdk.threads.spawn = async (a: any) => {
      f.calls.push(["spawn", a]);
      return { ...f.thread, id: "thr_new" };
    };
    await f.bridge.handle(f.input("/new", 0));
    await f.bridge.handle(f.input("Новый", 0));
    assert.equal(f.bridge.bindings()[0].threadId, "thr_new");
    assert.deepEqual((f.bridge.bindings()[0] as any).before, ["thr_one"]);
    f.bridge.remember(700, "thr_one", "proj_one");
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) => {
      if (a.threadId === "thr_one") throw httpError(503);
      return realGet(a);
    };
    const before = f.calls.length;
    await f.bridge.handle({ ...f.input("При сбое", 0), replyTo: 700 });
    f.sdk.threads.get = async (a: any) =>
      a.threadId === "thr_one" ? { ...(await realGet(a)), archivedAt: 1 } : realGet(a);
    await f.bridge.handle({ ...f.input("Луна", 0), replyTo: cardId });
    assert.equal(f.calls.length, before);
    assert.equal(texts(f).filter((t) => t.includes("недоступен")).length, 2);
  } finally {
    f.cleanup();
  }
});
test("pinned chat: /disconnect and /chats remember the earlier pinned chat; the fallback notice fits a chat yet to start", async () => {
  const f = await pinnedChat();
  try {
    await f.bridge.handle(f.input("/use thr_q", 0));
    await f.press("connect");
    assert.equal(f.bridge.bindings()[0].threadId, "thr_q");
    assert.deepEqual((f.bridge.bindings()[0] as any).before, ["thr_one"]);
    await f.bridge.handle(f.input("/disconnect", 0));
    assert.deepEqual((f.bridge.bindings()[0] as any).before, ["thr_q", "thr_one"]);
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) =>
      a.threadId === "thr_one" ? { ...(await realGet(a)), archivedAt: 1 } : realGet(a);
    f.bridge.remember(800, "thr_one", "proj_one");
    await f.bridge.handle({ ...f.input("С нуля", 0), replyTo: 800 });
    assert.ok(texts(f).some((t) => t === "Тот чат закрыт в BB — сообщение пойдёт в новый закреплённый чат."));
    assert.equal(f.calls.at(-1)?.[0], "spawn");
    assert.equal(f.calls.at(-1)?.[1].prompt, "С нуля");
  } finally {
    f.cleanup();
  }
});
test("pinned chat: an answer kept while an earlier message is still sending is delivered, not dropped", async () => {
  const f = await pinnedChat();
  try {
    const rev = (f.bridge.bindings()[0] as any).revision;
    f.store.put("chat:out:a-first", { topicId: 0, text: "Раньше", retryAt: 0, at: 1 });
    f.store.put("chat:out:event:" + rev + ":last", { topicId: 0, text: "Последний ответ старого чата", revision: rev, retryAt: 0, at: 2 });
    let release!: () => void;
    f.holdNextDelivery(new Promise<void>((r) => (release = r)));
    const flushing = f.bridge.flush();
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) => ({ ...(await realGet(a)), deletedAt: 1 });
    await f.bridge.handle(f.input("Новое", 0));
    release();
    await flushing;
    assert.ok(f.sent.some((m) => m.text === "Последний ответ старого чата"));
  } finally {
    f.cleanup();
  }
});

test("chat-pin: pins the bot chat to a thread from the CLI, remembers the earlier one and records the pin", async () => {
  const f = await pinnedChat();
  try {
    assert.deepEqual(await f.bridge.pin("thr_tg"), { threadId: "thr_tg", projectId: "proj_one" });
    const b = f.bridge.bindings()[0] as any;
    assert.equal(b.threadId, "thr_tg");
    assert.equal(b.hostId, undefined);
    assert.deepEqual(b.before, ["thr_one"]);
    assert.ok(texts(f).some((t) => t.startsWith("↪️ Пишу в чат")));
    assert.ok(f.store.get("chat:touched:thr_tg"));
    assert.equal(f.bridge.status().pinEvents[0].kind, "pinned_cli");
    assert.equal(f.bridge.status().pinEvents[0].threadId, "thr_tg");
    await f.bridge.handle(f.input("Привет", 0));
    assert.equal(f.calls.at(-1)?.[0], "send");
    assert.equal(f.calls.at(-1)?.[1].threadId, "thr_tg");
  } finally {
    f.cleanup();
  }
});
test("chat-pin: pinning the chat already pinned changes nothing", async () => {
  const f = await pinnedChat();
  try {
    const old = f.bridge.bindings()[0];
    const before = texts(f).length;
    await f.bridge.pin("thr_one");
    assert.deepEqual(f.bridge.bindings()[0], old);
    assert.equal(texts(f).length, before);
    assert.deepEqual(f.bridge.status().pinEvents, []);
  } finally {
    f.cleanup();
  }
});
test("chat-pin: refuses an archived or missing thread, a busy chat and a chat without the pin, changing nothing", async () => {
  const f = await pinnedChat();
  const single = fixture({ single: true });
  const topics = fixture({ pinned: true });
  try {
    const old = f.bridge.bindings()[0];
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) => {
      if (a.threadId === "thr_gone") throw httpError(404, "thread_not_found");
      const t = await realGet(a);
      return a.threadId === "thr_arch" ? { ...t, archivedAt: 1 } : t;
    };
    await assert.rejects(f.bridge.pin("thr_arch"), /thread_not_available/);
    await assert.rejects(f.bridge.pin("thr_gone"), /thread_not_found/);
    (f.bridge as any).lanes.add(0);
    await assert.rejects(f.bridge.pin("thr_tg"), /chat_busy/);
    (f.bridge as any).lanes.delete(0);
    assert.equal((f.bridge as any).lanes.size, 0);
    assert.deepEqual(f.bridge.bindings()[0], old);
    await assert.rejects(single.bridge.pin("thr_tg"), /pinned_chat_required/);
    await assert.rejects(topics.bridge.pin("thr_tg"), /pinned_chat_required/);
  } finally {
    f.cleanup();
    single.cleanup();
    topics.cleanup();
  }
});
test("agent replies go out as Telegram HTML without Rich Messages; a part Telegram cannot parse goes as plain text", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый", 0));
    await f.bridge.flush();
    f.sent.length = 0;
    const reply = "**Текст:**\n> **Цитата:** «да»\n\n---\n*Оценка* — сам";
    f.setEvents([
      { seq: 1, type: "item/completed", scope: { kind: "turn", turnId: "t1" }, data: { item: { type: "agentMessage", text: reply } } },
      { seq: 2, type: "turn/completed", scope: { kind: "turn", turnId: "t1" }, data: { status: "completed" } },
    ]);
    await f.bridge.observe();
    await f.bridge.flush();
    const html = f.sent.find((m) => m.parse_mode === "HTML");
    assert.equal(html?.text, "<b>Текст:</b>\n<blockquote><b>Цитата:</b> «да»</blockquote>\n\n———\n<i>Оценка</i> — сам");
    // Service texts stay plain.
    assert.ok(f.sent.filter((m) => m.parse_mode !== "HTML").every((m) => !/<\w/.test(m.text ?? "")));
    f.sent.length = 0;
    f.rejectNextHtml();
    f.setEvents([
      { seq: 3, type: "item/completed", scope: { kind: "turn", turnId: "t2" }, data: { item: { type: "agentMessage", text: "**второй**" } } },
      { seq: 4, type: "turn/completed", scope: { kind: "turn", turnId: "t2" }, data: { status: "completed" } },
    ]);
    await f.bridge.observe();
    await f.bridge.flush();
    assert.deepEqual(f.sent.map((m) => [m.text, m.parse_mode]), [["**второй**", undefined]]);
    // «Последний ответ» is Markdown too.
    f.sent.length = 0;
    f.sdk.threads.output = async () => ({ output: "**итог**" });
    await f.bridge.handle(f.input("/history", 0));
    await f.bridge.flush();
    assert.ok(f.sent.some((m) => m.parse_mode === "HTML" && m.text.includes("<b>итог</b>")));
  } finally {
    f.cleanup();
  }
});
test("a long reply split in parts keeps a code block open across the parts", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый", 0));
    await f.bridge.flush();
    f.sent.length = 0;
    const reply = "Код:\n```\n" + "строка кода **не жирная**\n".repeat(200) + "```\nпосле **кода**";
    f.setEvents([
      { seq: 1, type: "item/completed", scope: { kind: "turn", turnId: "t1" }, data: { item: { type: "agentMessage", text: reply } } },
      { seq: 2, type: "turn/completed", scope: { kind: "turn", turnId: "t1" }, data: { status: "completed" } },
    ]);
    await f.bridge.observe();
    await f.bridge.flush();
    const parts = f.sent.filter((m) => m.parse_mode === "HTML").map((m) => m.text as string);
    assert.ok(parts.length >= 2);
    for (const p of parts) assert.equal((p.match(/<pre>/g) ?? []).length, (p.match(/<\/pre>/g) ?? []).length);
    assert.ok(parts[1].startsWith("<pre>строка кода **не жирная**"));
    assert.ok(parts.at(-1)!.endsWith("после <b>кода</b>"));
    assert.ok(!parts.join("").includes("<b>не жирная</b>"));
  } finally {
    f.cleanup();
  }
});

test("a reply whose last part is only a closing fence is not sent as an empty message", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый", 0));
    await f.bridge.flush();
    f.sent.length = 0;
    const body = "x".repeat(3480);
    f.setEvents([
      { seq: 1, type: "item/completed", scope: { kind: "turn", turnId: "t1" }, data: { item: { type: "agentMessage", text: "```\n" + body + "\n```" } } },
      { seq: 2, type: "turn/completed", scope: { kind: "turn", turnId: "t1" }, data: { status: "completed" } },
    ]);
    await f.bridge.observe();
    await f.bridge.flush();
    assert.ok(f.sent.length >= 1);
    for (const m of f.sent) assert.ok(String(m.text).replace(/<[^>]+>/g, "").trim().length > 0, JSON.stringify(m.text));
    assert.ok(f.sent.at(-1)?.reply_markup);
  } finally {
    f.cleanup();
  }
});
test("chat-pin into another project leaves the old project's server behind", async () => {
  const f = await pinnedChat();
  try {
    f.store.put("chat:binding:0", { ...f.bridge.bindings()[0], hostId: "mini" });
    const realGet = f.sdk.threads.get;
    f.sdk.threads.get = async (a: any) =>
      a.threadId === "thr_two" ? { ...(await realGet(a)), projectId: "proj_two" } : realGet(a);
    await f.bridge.pin("thr_two");
    const b = f.bridge.bindings()[0] as any;
    assert.equal(b.projectId, "proj_two");
    assert.equal(b.hostId, undefined);
  } finally {
    f.cleanup();
  }
});

const photo = (over: Partial<TelegramFile> = {}): TelegramFile => ({
  kind: "photo",
  fileId: "ph1",
  name: "photo-1.jpg",
  mime: "image/jpeg",
  size: 1000,
  ...over,
});
const doc = (over: Partial<TelegramFile> = {}): TelegramFile => ({
  kind: "document",
  fileId: "d1",
  name: "Лекция 5.pdf",
  mime: "application/pdf",
  size: 2000,
  ...over,
});
/** Waits for the inbox work a tick started. */
const drain = (f: ReturnType<typeof fixture>) =>
  Promise.allSettled([...((f.bridge as any).work as Set<Promise<void>>)]);
const sends = (f: ReturnType<typeof fixture>) => f.calls.filter((x) => x[0] === "send");
test("files: a photo with a caption reaches the pinned chat as an image with its words", async () => {
  const f = await pinnedChat();
  try {
    await f.bridge.handle({ ...f.input("Что на фото?", 0), files: [photo()] });
    const up = f.calls.find((x) => x[0] === "upload")![1];
    assert.equal(up.projectId, "proj_one");
    assert.equal(up.filename, "photo-1.jpg");
    assert.equal(up.mimeType, "image/jpeg");
    assert.deepEqual([...up.clientFile], [1, 2, 3]);
    assert.deepEqual(f.calls.at(-1), [
      "send",
      {
        threadId: "thr_one",
        mode: "auto",
        input: [
          { type: "text", text: "Что на фото?", mentions: [] },
          { type: "localImage", path: "proj_one/photo-1.jpg" },
        ],
      },
    ]);
  } finally {
    f.cleanup();
  }
});
test("files: a file without a caption starts a new chat with it; the words and title name the file", async () => {
  const f = fixture();
  try {
    await f.bridge.handle({ ...f.input(""), files: [doc()] });
    const spawn = f.calls.find((x) => x[0] === "spawn")![1];
    assert.equal(spawn.prompt, undefined);
    assert.deepEqual(spawn.input, [
      { type: "text", text: "📎 Из Telegram: Лекция 5.pdf", mentions: [] },
      {
        type: "localFile",
        path: "proj_one/Лекция 5.pdf",
        name: "Лекция 5.pdf",
        mimeType: "application/pdf",
        sizeBytes: 3,
      },
    ]);
    assert.equal(spawn.title, "📎 Из Telegram: Лекция 5.pdf");
    // The upload comes first: a file never follows its message.
    assert.deepEqual(f.calls.map((x) => x[0]), ["upload", "spawn"]);
  } finally {
    f.cleanup();
  }
});
test("files: a file over 20 MB is refused before any download and nothing reaches BB", async () => {
  let fetched = 0;
  const f = fixture({
    fetchFile: async () => {
      fetched++;
      return new Uint8Array(1);
    },
  });
  try {
    await f.bridge.handle(f.input("Первый"));
    const before = f.calls.length;
    await f.bridge.handle({
      ...f.input("Сделай рекап"),
      files: [photo(), doc({ kind: "audio", name: "lecture.m4a", mime: "audio/mp4", size: 85 * 1048576 })],
    });
    assert.equal(fetched, 0);
    assert.equal(f.calls.length, before);
    const said = texts(f).at(-1)!;
    assert.match(said, /до 20 МБ/);
    assert.match(said, /lecture\.m4a — 85\.0 МБ/);
    assert.doesNotMatch(said, /photo-1/);
  } finally {
    f.cleanup();
  }
});
test("files: a file Telegram will not hand over, or a failed download, cancels the whole message", async () => {
  const outcomes: Array<Error | Uint8Array> = [
    Error("file_too_large"),
    new Uint8Array([7]),
    Error("file_download_failed"),
  ];
  const f = fixture({
    // Once the list runs out, every download keeps failing: tries again do not help.
    fetchFile: async () => {
      const next = outcomes.shift() ?? Error("file_download_failed");
      if (next instanceof Error) throw next;
      return next;
    },
  });
  try {
    await f.bridge.handle(f.input("Первый"));
    const before = sends(f).length;
    await f.bridge.handle({ ...f.input("Вот"), files: [doc({ size: 0 })] });
    // An unknown size is not shown.
    assert.match(texts(f).at(-1)!, /до 20 МБ, а этот больше:\n• Лекция 5\.pdf\n/);
    // The first file of an album downloads, the second fails: nothing is sent.
    await f.bridge.handle({ ...f.input("Вот"), files: [photo(), doc()] });
    assert.match(texts(f).at(-1)!, /Не удалось скачать файл из Telegram: Лекция 5\.pdf/);
    assert.equal(sends(f).length, before);
  } finally {
    f.cleanup();
  }
});
test("files: an album goes to BB as one message once it settles, and a later message waits for it", async () => {
  const f = await pinnedChat();
  try {
    const now = Date.now();
    const put = (input: ChatInput, at: number) =>
      f.store.put("chat:in:" + input.updateId, { input, state: "pending", at });
    const first = { ...f.input("", 0), files: [photo({ fileId: "a", name: "a.jpg" })], group: "G1" };
    const second = { ...f.input("Сравни два слайда", 0), files: [photo({ fileId: "b", name: "b.jpg" })], group: "G1" };
    const later = f.input("И ещё вопрос", 0);
    put(first, now);
    put(second, now);
    put(later, now);
    const before = sends(f).length;
    await f.bridge.tick();
    await drain(f);
    assert.equal(sends(f).length, before, "a fresh album waits, and so does the message after it");
    put(first, now - 5000);
    put(second, now - 5000);
    await f.bridge.tick();
    await drain(f);
    assert.deepEqual(sends(f).slice(before).map((x) => x[1].input), [
      [
        { type: "text", text: "Сравни два слайда", mentions: [] },
        { type: "localImage", path: "proj_one/a.jpg" },
        { type: "localImage", path: "proj_one/b.jpg" },
      ],
    ]);
    for (const x of [first, second]) {
      const kept = f.store.get<any>("chat:in:" + x.updateId);
      assert.equal(kept.state, "done");
      assert.equal(kept.input.files, undefined);
      assert.equal(kept.input.text, undefined);
    }
    await f.bridge.tick();
    await drain(f);
    assert.deepEqual(sends(f).at(-1)![1].input, [{ type: "text", text: "И ещё вопрос", mentions: [] }]);
  } finally {
    f.cleanup();
  }
});
test("files: a file in reply to a question card is refused and the question stays open", async () => {
  const f = fixture();
  try {
    await f.bridge.handle(f.input("Run"));
    f.setQuestions([
      {
        id: "q",
        status: "pending",
        payload: {
          kind: "user_question",
          questions: [{ id: "q1", prompt: "Ответ?", allowFreeText: true, multiSelect: false, options: [] }],
        },
      },
    ]);
    await f.bridge.observe();
    await f.bridge.flush();
    const record = f.store.list<any>("chat:question:")[0];
    const replyTo = Number(record.key.split(":").at(-1));
    await f.bridge.handle({ ...f.input("Вот файл"), replyTo, files: [doc()] });
    assert.equal(f.calls.filter((x) => x[0] === "resolve" || x[0] === "upload").length, 0);
    assert.match(texts(f).at(-1)!, /отвечай текстом/);
  } finally {
    f.cleanup();
  }
});
test("files: a caption is words for the agent, never a bot command", async () => {
  const f = await pinnedChat();
  try {
    await f.bridge.handle({ ...f.input("/new", 0), files: [doc()] });
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
    assert.deepEqual(sends(f).at(-1)![1].input[0], { type: "text", text: "/new", mentions: [] });
  } finally {
    f.cleanup();
  }
});
test("files: with confirmNewChat a file draft keeps its files until «да»", async () => {
  const f = fixture({ confirm: true });
  try {
    await f.bridge.handle({ ...f.input("Разбери"), files: [doc()] });
    assert.equal(f.calls.length, 0);
    assert.deepEqual(f.store.get("chat:draft:100"), { text: "Разбери", files: [doc()] });
    await f.bridge.handle(f.input("да"));
    const spawn = f.calls.find((x) => x[0] === "spawn")![1];
    assert.equal(spawn.input[0].text, "Разбери");
    assert.equal(spawn.input[1].type, "localFile");
    // A text draft stays a plain string.
    await f.bridge.handle(f.input("/new"));
    await f.bridge.handle(f.input("Просто текст"));
    assert.equal(f.store.get("chat:draft:100"), "Просто текст");
  } finally {
    f.cleanup();
  }
});
test("files: a restart during an album gives one notice for it and keeps no file ids", () => {
  const f = fixture();
  try {
    const items = [
      { ...f.input(""), files: [photo()], group: "G2" },
      { ...f.input(""), files: [photo({ fileId: "x" })], group: "G2" },
      f.input("Текст"),
    ];
    for (const input of items)
      f.store.put("chat:in:" + input.updateId, { input, state: "claimed", at: Date.now() });
    f.bridge.recover();
    assert.equal(texts(f).filter((t) => t.includes("перезапущен")).length, 2);
    for (const input of items) {
      const kept = f.store.get<any>("chat:in:" + input.updateId);
      assert.equal(kept.state, "done");
      assert.equal(kept.input.files, undefined);
    }
  } finally {
    f.cleanup();
  }
});
test("files: the input schema takes up to ten files and an album id", () => {
  const base = { updateId: 1, ownerId: 1, chatId: 1, topicId: 0, messageId: 1 };
  assert.ok(inputSchema.safeParse({ ...base, files: [photo()], group: "123" }).success);
  assert.equal(
    inputSchema.safeParse({ ...base, files: Array.from({ length: 11 }, () => photo()) }).success,
    false,
  );
  assert.equal(inputSchema.safeParse({ ...base, files: [{ ...photo(), kind: "sticker" }] }).success, false);
});
test("files: BB refusing an upload says so, sends nothing, and leaves a reason in the status", async () => {
  const f = await pinnedChat();
  try {
    const before = sends(f).length;
    f.sdk.projects.attachments.upload = async () => {
      throw Object.assign(new Error("invalid_request"), { status: 400 });
    };
    await f.bridge.handle({ ...f.input("Вот", 0), files: [doc()] });
    assert.equal(sends(f).length, before);
    assert.match(texts(f).at(-1)!, /BB не принял файл «Лекция 5\.pdf»/);
    assert.equal(f.bridge.status().lastError, "attachment_upload_failed");
  } finally {
    f.cleanup();
  }
});
test("files: an iPhone HEIC photo goes up as a plain file, which BB accepts", async () => {
  const f = await pinnedChat();
  try {
    await f.bridge.handle({
      ...f.input("", 0),
      files: [doc({ name: "IMG_0001.HEIC", mime: "image/heic" })],
    });
    const up = f.calls.find((x) => x[0] === "upload")![1];
    assert.equal(up.mimeType, "application/octet-stream");
    assert.equal(sends(f).at(-1)![1].input[1].type, "localFile");
  } finally {
    f.cleanup();
  }
});
test("files: a failed download leaves its code in the status; the download gets one deadline for the message", async () => {
  const signals: unknown[] = [];
  const f = fixture({
    fetchFile: async (_file, signal?: AbortSignal) => {
      signals.push(signal);
      throw Error("file_download_failed");
    },
  } as any);
  try {
    await f.bridge.handle(f.input("Первый"));
    await f.bridge.handle({ ...f.input("Вот"), files: [doc()] });
    assert.equal(f.bridge.status().lastError, "file_download_failed");
    assert.ok(signals[0] instanceof AbortSignal);
  } finally {
    f.cleanup();
  }
});
test("files: several files over the limit are named together", async () => {
  const f = fixture();
  try {
    await f.bridge.handle({
      ...f.input("Вот"),
      files: [doc({ name: "a.mp4", size: 30 * 1048576 }), doc({ name: "b.mp4", size: 40 * 1048576 })],
    });
    assert.match(texts(f).at(-1)!, /а эти больше:\n• a\.mp4 — 30\.0 МБ\n• b\.mp4 — 40\.0 МБ/);
  } finally {
    f.cleanup();
  }
});
test("files: one chat — a file in reply to another chat's question card is refused before the chat moves", async () => {
  const f = fixture({ single: true });
  try {
    await f.bridge.handle(f.input("/project", 0));
    await f.press("project");
    await f.bridge.handle(f.input("Первый", 0));
    f.store.put("chat:question:0:777", { threadId: "thr_other", interactionId: "i1", revision: "r" });
    f.store.put("chat:thread-project:thr_other", "proj_one");
    await f.bridge.handle({ ...f.input("Вот", 0), replyTo: 777, files: [doc()] });
    assert.equal(f.bridge.bindings()[0].threadId, "thr_one");
    assert.match(texts(f).at(-1)!, /отвечай текстом/);
    assert.equal(f.calls.filter((x) => x[0] === "upload").length, 0);
  } finally {
    f.cleanup();
  }
});
test("files: an album file that comes after its album was taken goes later as its own message", async () => {
  const f = await pinnedChat();
  try {
    const put = (input: ChatInput, at: number) =>
      f.store.put("chat:in:" + input.updateId, { input, state: "pending", at });
    const old = Date.now() - 5000;
    const first = { ...f.input("Альбом", 0), files: [photo({ fileId: "a", name: "a.jpg" })], group: "G3" };
    put(first, old);
    await f.bridge.tick();
    await drain(f);
    const before = sends(f).length;
    const late = { ...f.input("", 0), files: [photo({ fileId: "c", name: "c.jpg" })], group: "G3" };
    put(late, Date.now());
    await f.bridge.tick();
    await drain(f);
    assert.equal(sends(f).length, before);
    put(late, old);
    await f.bridge.tick();
    await drain(f);
    assert.deepEqual(sends(f).at(-1)![1].input.at(-1), { type: "localImage", path: "proj_one/c.jpg" });
  } finally {
    f.cleanup();
  }
});
test("take: a message the schema refuses is false and never stored; another owner still throws", () => {
  const f = fixture();
  try {
    const bad = { ...f.input("x"), files: [{ ...photo(), mime: "a/" + "b".repeat(300) }] };
    assert.equal(f.bridge.take(bad), false);
    assert.equal(f.store.list("chat:in:").length, 0);
    assert.throws(() => f.bridge.take({ ...f.input("hi"), ownerId: 22 }), /foreign_identity/);
    assert.equal(f.bridge.take(f.input("ok")), true);
    assert.equal(f.store.list("chat:in:").length, 1);
  } finally {
    f.cleanup();
  }
});
test("files: downloads that outrun the message's deadline say so, apart from a failed download", async () => {
  let downloads = 0;
  const f = fixture({
    filesDeadlineMs: 10,
    fetchFile: async (_file, signal) => {
      downloads++;
      await new Promise((r) => setTimeout(r, 40));
      if (signal.aborted) throw Error("file_download_failed");
      return new Uint8Array(1);
    },
  });
  try {
    await f.bridge.handle(f.input("Первый"));
    const before = sends(f).length;
    await f.bridge.handle({ ...f.input("Вот"), files: [doc()] });
    assert.equal(f.bridge.status().lastError, "file_download_timeout");
    assert.match(texts(f).at(-1)!, /дольше трёх минут/);
    assert.equal(downloads, 1);
    assert.equal(sends(f).length, before);
  } finally {
    f.cleanup();
  }
});
test("files: a passing download or BB failure is tried again; a refusal is not", async () => {
  let downloads = 0;
  const f = fixture({
    fetchFile: async () => {
      downloads++;
      if (downloads === 1) throw Error("file_download_failed");
      return new Uint8Array([5]);
    },
  });
  try {
    await f.bridge.handle(f.input("Первый"));
    // Telegram drops the first download, BB the first upload: the message still goes, once.
    let uploads = 0;
    const realUpload = f.sdk.projects.attachments.upload;
    f.sdk.projects.attachments.upload = async (a: any) => {
      uploads++;
      if (uploads === 1) throw new TypeError("fetch failed");
      return realUpload(a);
    };
    const before = sends(f).length;
    await f.bridge.handle({ ...f.input("Вот"), files: [doc()] });
    assert.equal(downloads, 2);
    assert.equal(uploads, 2);
    assert.equal(sends(f).length, before + 1);
    assert.equal(sends(f).at(-1)![1].input[1].type, "localFile");
    // A refusal (4xx) and a file too big are final: one attempt each.
    uploads = 0;
    f.sdk.projects.attachments.upload = async () => {
      uploads++;
      throw Object.assign(new Error("invalid_request"), { status: 400 });
    };
    await f.bridge.handle({ ...f.input("Ещё"), files: [doc()] });
    assert.equal(uploads, 1);
    assert.match(texts(f).at(-1)!, /BB не принял файл/);
  } finally {
    f.cleanup();
  }
});
test("files: a file Telegram calls too big is not downloaded again", async () => {
  let downloads = 0;
  const f = fixture({
    fetchFile: async () => {
      downloads++;
      throw Error("file_too_large");
    },
  });
  try {
    await f.bridge.handle({ ...f.input("Вот"), files: [doc({ size: 0 })] });
    assert.equal(downloads, 1);
  } finally {
    f.cleanup();
  }
});
test("files: tries stop at three, cover 5xx and 429, skip a BB request timeout, and are counted", async () => {
  const f = await pinnedChat();
  try {
    let uploads = 0;
    const failWith = (error: unknown) => {
      uploads = 0;
      f.sdk.projects.attachments.upload = async () => {
        uploads++;
        throw error;
      };
    };
    const before = f.bridge.status().fileRetries;
    failWith(Object.assign(new Error("unavailable"), { status: 503 }));
    await f.bridge.handle({ ...f.input("Вот", 0), files: [doc()] });
    assert.equal(uploads, 3);
    assert.equal(f.bridge.status().fileRetries, before + 2);
    failWith(Object.assign(new Error("slow down"), { status: 429 }));
    await f.bridge.handle({ ...f.input("Вот", 0), files: [doc()] });
    assert.equal(uploads, 3);
    // BB gave up on its own after 75 s: the request most likely reached it, and another try only holds the chat.
    failWith(Object.assign(new Error("request timed out"), { name: "BbRequestTimeoutError" }));
    await f.bridge.handle({ ...f.input("Вот", 0), files: [doc()] });
    assert.equal(uploads, 1);
  } finally {
    f.cleanup();
  }
});
test("files: the deadline running out during a pause starts no new try", async () => {
  const f = fixture({ filesDeadlineMs: 30, retryMs: [1000, 1000] });
  try {
    await f.bridge.handle(f.input("Первый"));
    let uploads = 0;
    f.sdk.projects.attachments.upload = async () => {
      uploads++;
      throw new TypeError("fetch failed");
    };
    const started = Date.now();
    const retries = f.bridge.status().fileRetries;
    await f.bridge.handle({ ...f.input("Вот"), files: [doc()] });
    assert.equal(uploads, 1);
    assert.equal(f.bridge.status().fileRetries, retries, "a try cut by the deadline is not counted");
    assert.ok(Date.now() - started < 900, "the pause ends with the deadline");
    assert.match(texts(f).at(-1)!, /BB не принял файл/);
  } finally {
    f.cleanup();
  }
});
