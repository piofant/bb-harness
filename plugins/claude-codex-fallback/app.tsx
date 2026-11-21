import React, { useState, useEffect, useCallback, useRef } from "react";
import {
  definePluginApp,
  useRpc,
  useRealtime,
  useRealtimeConnectionState,
  useBbNavigate,
  useBbContext,
  useComposer,
  experimental_Icon as Icon,
  type PluginNavPanelProps,
  type PluginThreadActionRegistration,
} from "@get-bb/plugin-sdk/app";
import type { rpcContract, Chat } from "./contract";

// Follow a handoff only when it happens while this native thread is open.
// Opening an old source later must keep its history accessible.
export function ConnectedConversationRoute() {
  const { threadId } = useBbContext();
  const rpc = useRpc<typeof rpcContract>();
  const navigate = useBbNavigate();
  const connectionState = useRealtimeConnectionState();
  const previous = useRef<{ route: string; active: string } | null>(null);
  const [revision, setRevision] = useState(0);
  useRealtime("changed", () => setRevision((value) => value + 1));
  useEffect(() => {
    previous.current = null;
  }, [threadId]);
  useEffect(() => {
    if (!threadId) return;
    let cancelled = false;
    void rpc.call("get", { threadId }).then((chat) => {
      if (cancelled || !chat) return;
      const last = previous.current;
      previous.current = { route: threadId, active: chat.activeThreadId };
      if (last?.route === threadId && last.active === threadId && chat.activeThreadId !== threadId) {
        navigate.toThread(chat.activeThreadId);
      }
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [threadId, rpc, navigate, revision, connectionState]);
  return null;
}

export function providerName(providerId: string): string {
  return providerId === "codex" ? "Codex" : providerId === "acp-antigravity" ? "Gemini" : providerId === "claude-code" ? "Claude" : providerId;
}

export function formatPhase(phase: Chat["phase"]): { label: string; tone: string } {
  switch (phase) {
    case "ready":
      return {
        label: "Готов",
        tone: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/20",
      };
    case "switching":
      return {
        label: "Переключение...",
        tone: "bg-blue-500/10 text-blue-700 dark:text-blue-400 border-blue-500/20 animate-pulse",
      };
    case "waiting-retry":
      return {
        label: "Ожидание повтора",
        tone: "bg-amber-500/10 text-amber-700 dark:text-amber-400 border-amber-500/20",
      };
    case "blocked":
      return {
        label: "Заблокирован",
        tone: "bg-rose-500/10 text-rose-700 dark:text-rose-400 border-rose-500/20",
      };
    case "exhausted":
      return {
        label: "Попытки исчерпаны",
        tone: "bg-destructive/10 text-destructive border-destructive/20",
      };
    default:
      return {
        label: phase,
        tone: "bg-muted text-muted-foreground border-border",
      };
  }
}

export function formatDate(ts: number): string {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

export function ChatListView({
  onSelectChat,
}: {
  onSelectChat: (rootThreadId: string) => void;
}) {
  const rpc = useRpc<typeof rpcContract>();
  const navigate = useBbNavigate();
  const [chats, setChats] = useState<Chat[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [inputThreadId, setInputThreadId] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

  const fetchChats = useCallback(async () => {
    try {
      setError(null);
      const res = await rpc.call("list", null);
      setChats(res.chats ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось загрузить список чатов.");
    } finally {
      setLoading(false);
    }
  }, [rpc]);

  useEffect(() => {
    fetchChats();
  }, [fetchChats]);

  useRealtime("changed", () => {
    fetchChats();
  });

  const connectionState = useRealtimeConnectionState();
  useEffect(() => {
    if (connectionState === "connected") {
      fetchChats();
    }
  }, [connectionState, fetchChats]);

  const handleConnect = async (e: React.FormEvent) => {
    e.preventDefault();
    const targetId = inputThreadId.trim();
    if (!targetId) return;

    setConnecting(true);
    setConnectError(null);
    try {
      const chat = await rpc.call("enable", { threadId: targetId });
      setInputThreadId("");
      navigate.toThread(chat.activeThreadId);
    } catch (err) {
      setConnectError(err instanceof Error ? err.message : "Ошибка подключения треда.");
    } finally {
      setConnecting(false);
    }
  };

  return (
    <div className="flex flex-col h-full overflow-y-auto bg-background text-foreground p-6 max-w-5xl mx-auto space-y-6">
      <div className="flex flex-col space-y-1.5 border-b border-border pb-4">
        <div className="flex items-center gap-2">
          <Icon name="Repeat" className="h-6 w-6 text-primary" />
          <h1 className="text-xl font-semibold">Чаты с автопереходом</h1>
        </div>
        <p className="text-sm text-muted-foreground">
          Автоматический fallback между провайдерами (Claude → Codex → Antigravity) при ошибках и исчерпании лимитов.
        </p>
      </div>

      <div className="rounded-lg border border-border bg-card p-4 text-card-foreground shadow-sm">
        <h2 className="text-sm font-medium mb-2">Подключить существующий тред</h2>
        <form onSubmit={handleConnect} className="flex gap-2">
          <input
            type="text"
            value={inputThreadId}
            onChange={(e) => setInputThreadId(e.target.value)}
            placeholder="ID треда, например thr_..."
            className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            disabled={connecting}
          />
          <button
            type="submit"
            disabled={connecting || !inputThreadId.trim()}
            className="inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
          >
            {connecting ? "Подключение..." : "Подключить"}
          </button>
        </form>
        {connectError && (
          <div role="alert" className="mt-2 text-xs text-destructive">
            {connectError}
          </div>
        )}
      </div>

      {error && (
        <div role="alert" className="rounded-md border border-destructive/20 bg-destructive/10 p-4 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="space-y-3">
        <h2 className="text-sm font-semibold tracking-wide text-muted-foreground uppercase">
          Подключённые чаты ({chats.length})
        </h2>

        {loading ? (
          <div className="text-sm text-muted-foreground py-8 text-center">Загрузка чатов...</div>
        ) : chats.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
            Нет активных подключённых чатов. Подключите тред через форму выше или выберите «Автопереход» в меню треда.
          </div>
        ) : (
          <div className="grid gap-3">
            {chats.map((chat) => {
              const phaseInfo = formatPhase(chat.phase);
              const activeSegment = chat.segments.find((s) => s.threadId === chat.activeThreadId);

              return (
                <div
                  key={chat.rootThreadId}
                  onClick={() => navigate.toThread(chat.activeThreadId)}
                  className="group relative flex flex-col justify-between rounded-lg border border-border bg-card p-4 transition-all hover:border-primary/50 hover:shadow-md cursor-pointer"
                >
                  <div className="flex items-start justify-between gap-4">
                    <div className="space-y-1">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-base group-hover:text-primary transition-colors">
                          {chat.title || chat.rootThreadId}
                        </span>
                        <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${phaseInfo.tone}`}>
                          {phaseInfo.label}
                        </span>
                        {!chat.enabled && (
                          <span className="inline-flex items-center rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                            Автопереход выключен
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        Корневой тред: <code className="font-mono">{chat.rootThreadId}</code>
                        {chat.activeThreadId !== chat.rootThreadId && (
                          <> • Активный: <code className="font-mono text-primary">{chat.activeThreadId}</code></>
                        )}
                      </div>
                    </div>

                    <div className="flex items-center gap-2">
                      <button
                        onClick={async (e) => {
                          e.stopPropagation();
                          try {
                            await rpc.call(chat.enabled ? "disable" : "enable", { threadId: chat.rootThreadId });
                            await fetchChats();
                          } catch (e) { setError(e instanceof Error ? e.message : "Не удалось изменить автопереход."); }
                        }}
                        className="rounded-md border border-border px-2.5 py-1 text-xs hover:bg-accent"
                      >{chat.enabled ? "Выключить автопереход" : "Включить автопереход"}</button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          navigate.toThread(chat.activeThreadId);
                        }}
                        className="rounded-md border border-border bg-background px-2.5 py-1 text-xs font-medium hover:bg-accent transition-colors"
                        title="Открыть активный тред в редакторе"
                      >
                        В тред
                      </button>
                    </div>
                  </div>

                  {chat.notice && (
                    <div className="mt-3 rounded border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-800 dark:text-amber-200">
                      {chat.notice}
                    </div>
                  )}

                  <div className="mt-4 flex flex-wrap items-center gap-4 text-xs text-muted-foreground border-t border-border/60 pt-3">
                    <div>Сегментов: <span className="font-medium text-foreground">{chat.segments.length}</span></div>
                    {activeSegment && (
                      <div>
                        Провайдер: <span className="font-medium text-foreground">{activeSegment.providerId}</span> ({activeSegment.model})
                      </div>
                    )}
                    {chat.pendingQueueCount > 0 && (
                      <div className="text-amber-600 dark:text-amber-400 font-medium">
                        В очереди: {chat.pendingQueueCount} сообщ.
                      </div>
                    )}
                    <div className="ml-auto text-xs text-muted-foreground">
                      Обновлено: {formatDate(chat.updatedAt)}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

export function UnifiedChatView({ threadId, onBack }: { threadId: string; onBack: () => void }) {
  const rpc = useRpc<typeof rpcContract>();
  const navigate = useBbNavigate();
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void rpc.call("get", { threadId }).then((chat) => {
      if (!cancelled && chat) navigate.toThread(chat.activeThreadId);
    }).catch((e) => {
      if (!cancelled) setError(e instanceof Error ? e.message : "Не удалось проверить автопереход.");
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [rpc, navigate, threadId]);
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 p-6 text-center">
      <h1 className="text-lg font-medium">Автопереход между моделями</h1>
      <p className="max-w-md text-sm text-muted-foreground">При недоступности Claude продолжение появится под исходным чатом слева — на Codex, затем Gemini. История останется в исходном чате.</p>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <button disabled={loading || pending} className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground disabled:opacity-50" onClick={async () => {
        setPending(true); setError(null);
        try { const chat = await rpc.call("enable", { threadId }); navigate.toThread(chat.activeThreadId); }
        catch(e) { setError(e instanceof Error ? e.message : "Не удалось подключить чат."); }
        finally { setPending(false); }
      }}>{loading ? "Проверяем…" : pending ? "Подключаем…" : "Подключить этот тред"}</button>
      <button className="text-sm text-muted-foreground hover:text-foreground" onClick={onBack}>Все подключённые чаты</button>
    </div>
  );
}

export function ConversationsNavPanel({ subPath }: PluginNavPanelProps) {
  const navigate = useBbNavigate();
  const rawSubPath = subPath ? decodeURIComponent(subPath).replace(/^\/+|\/+$/g, "") : "";
  const isIndex = !rawSubPath || rawSubPath === "index";

  if (isIndex) {
    return (
      <ChatListView
        onSelectChat={(rootThreadId) => {
          navigate.toThread(rootThreadId);
        }}
      />
    );
  }

  return (
    <UnifiedChatView
      threadId={rawSubPath}
      onBack={() => {
        navigate.toPluginPanel("conversations", { subPath: "" });
      }}
    />
  );
}

export function FallbackComposerBanner() {
  const { scope } = useComposer();
  const navigate = useBbNavigate();
  const rpc = useRpc<typeof rpcContract>();
  const threadId = scope.kind === "thread" ? scope.threadId : null;
  const [chat, setChat] = useState<Chat | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [revision, setRevision] = useState(0);
  const connectionState = useRealtimeConnectionState();
  useRealtime("changed", () => setRevision((n) => n + 1));
  useEffect(() => {
    setChat(null); setError(null);
    if (!threadId) return;
    let cancelled = false;
    void rpc.call("get", { threadId }).then((data) => { if (!cancelled) setChat(data); }).catch(() => {});
    return () => { cancelled = true; };
  }, [rpc, threadId, revision, connectionState]);
  if (!threadId || !chat) return null;
  const historical = chat.activeThreadId !== threadId;
  const active = chat.segments.find(s => s.threadId === chat.activeThreadId);
  const needsRecovery = chat.phase === "blocked" || chat.phase === "exhausted";
  const label = historical ? `Этот чат продолжен на ${providerName(active?.providerId ?? "резервной модели")}`
    : needsRecovery ? "Автопереход остановлен"
    : chat.phase === "switching" ? "Переходим на резервную модель…"
    : chat.phase === "waiting-retry" ? "Проверяем доступность модели…" : null;
  // The active native thread already has BB's model picker and Parent link.
  // Do not add another status panel during normal conversation.
  if (!label && !error) return null;
  return <div className="mx-3 mb-2 flex flex-wrap items-center justify-between gap-2 rounded-md bg-muted/60 px-3 py-2 text-xs">
    <span>{label}</span>
    {historical && <button className="font-medium underline underline-offset-2" onClick={() => navigate.toThread(chat.activeThreadId)}>Открыть продолжение →</button>}
    {needsRecovery && <>
      <details className="w-full"><summary className="cursor-pointer text-muted-foreground">Почему переход остановился</summary><p className="mt-2">{chat.notice}</p></details>
      <button disabled={pending} className="font-medium underline underline-offset-2 disabled:opacity-50" onClick={async () => {
        setPending(true); setError(null);
        try { const next = await rpc.call("recover", { threadId: chat.rootThreadId }); setChat(next); if (next.activeThreadId !== threadId) navigate.toThread(next.activeThreadId); }
        catch(e) { setError(e instanceof Error ? e.message : "Не удалось восстановить переход."); }
        finally { setPending(false); }
      }}>Повторить переход</button>
    </>}
    {error && <span role="alert" className="w-full text-destructive">{error}</span>}
  </div>;
}

export const fallbackThreadAction: PluginThreadActionRegistration = {
  id: "fallback-chat",
  title: "Автопереход",
  icon: "Repeat",
  group: "actions",
  placement: "header",
  item: ({ thread, navigate }) => ({
    label: "Автопереход",
    icon: "Repeat",
    async run() {
      navigate.toPluginPanel("conversations", { subPath: thread.id });
    },
  }),
};

export default definePluginApp((app) => {
  app.slots.experimental_appOverlay({
    id: "connected-conversation-route",
    component: ConnectedConversationRoute,
  });
  app.slots.navPanel({
    id: "conversations",
    title: "Автопереход",
    icon: "Repeat",
    path: "conversations",
    component: ConversationsNavPanel,
  });

  app.slots.experimental_threadAction(fallbackThreadAction);

  app.composer.customize({
    id: "fallback-banner",
    scopes: ["thread"],
    banners: [
      {
        id: "fallback-banner-item",
        chrome: "bare",
        component: FallbackComposerBanner,
      },
    ],
  });
});
