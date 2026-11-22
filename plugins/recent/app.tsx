// bb-plugin-recent — «Недавние» page: unseen + recent root threads in one
// centered column, bb's own new-thread composer pinned at the bottom.
import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  definePluginApp,
  experimental_NewThreadComposer as NewThreadComposer,
  useBbContext,
  useBbNavigate,
  useRealtimeConnectionState,
  useRpc,
  useSdk,
} from "@get-bb/plugin-sdk/app";
import type { RecentResult, RecentRow, rpcContract } from "./server";
import { relTime, shouldRefetch } from "./lib/recent";
import { cn } from "@/lib/utils";

const PAGE = 20;
const POLL_MS = 10_000;
const DEBOUNCE_MS = 800;

function useRecent(limit: number) {
  const rpc = useRpc<typeof rpcContract>();
  const sdk = useSdk();
  const connection = useRealtimeConnectionState();
  const [data, setData] = useState<RecentResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const refetch = useCallback(() => {
    const id = ++seq.current;
    rpc.call("recent", { limit, offset: 0 }).then(
      (next) => {
        if (id !== seq.current) return;
        setData(next);
        setError(null);
      },
      (cause: unknown) => {
        if (id === seq.current) setError(cause instanceof Error ? cause.message : String(cause));
      },
    );
  }, [rpc, limit]);

  useEffect(() => refetch(), [refetch]);

  // Live: a relevant thread change → one debounced refetch.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const unsubscribe = sdk.subscribe({
      event: "thread:changed",
      callback: (event) => {
        if (timer !== null || !shouldRefetch(event)) return;
        timer = setTimeout(() => {
          timer = null;
          refetch();
        }, DEBOUNCE_MS);
      },
    });
    return () => {
      if (timer !== null) clearTimeout(timer);
      unsubscribe();
    };
  }, [sdk, refetch]);

  // Without a live socket, poll; on reconnect, reconcile once.
  const wasConnected = useRef(connection === "connected");
  useEffect(() => {
    if (connection === "connected") {
      if (!wasConnected.current) refetch();
      wasConnected.current = true;
      return;
    }
    wasConnected.current = false;
    const t = setInterval(refetch, POLL_MS);
    return () => clearInterval(t);
  }, [connection, refetch]);

  return { data, error };
}

/** Re-render every minute so «N мин» stays honest without refetching. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);
  return now;
}

function Row({ row, now, onOpen }: { row: RecentRow; now: number; onOpen: (id: string) => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(row.id)}
        title={row.fullTitle}
        className="flex w-full min-w-0 items-center gap-2.5 rounded-md px-1.5 py-2.5 text-left text-[15px] outline-none transition-colors hover:bg-accent/60 focus-visible:bg-accent/60"
      >
        <span className={cn("min-w-0 flex-1 truncate", row.unseen ? "text-foreground" : "text-foreground/90")}>
          {row.title}
        </span>
        <span className="flex w-2 shrink-0 justify-center" aria-hidden={!row.unseen}>
          {row.unseen ? <span className="size-[7px] rounded-full bg-blue-500" aria-label="не просмотрено" /> : null}
        </span>
        <span className="w-11 shrink-0 text-right text-xs tabular-nums text-muted-foreground">{relTime(row.at, now)}</span>
      </button>
    </li>
  );
}

function LoadMore({ onVisible }: { onVisible: () => void }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const cb = useRef(onVisible);
  cb.current = onVisible;
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) cb.current();
    }, { rootMargin: "200px" });
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return <div ref={ref} className="h-6" aria-hidden />;
}

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-7 first:mt-0">
      <h3 className="mb-1.5 px-1.5 text-xs font-medium text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

function RecentPage() {
  const [limit, setLimit] = useState(PAGE);
  const { data, error } = useRecent(limit);
  const now = useNow();
  const navigate = useBbNavigate();
  const sdk = useSdk();
  const { projectId } = useBbContext();
  const open = useCallback((id: string) => navigate.toThread(id), [navigate]);

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <div className="flex min-h-0 flex-1 flex-col justify-end">
        <div className="mx-auto box-border flex min-h-0 w-full max-w-[640px] flex-col px-3 pt-5 md:px-4">
          {error === null ? null : (
            <p role="alert" className="mb-3 px-1.5 text-sm text-destructive">
              {error}
            </p>
          )}
          {data === null ? null : (
            <>
              <h3 className="mb-1.5 shrink-0 px-1.5 text-xs font-medium text-muted-foreground">Недавние</h3>
              {/* Scrolls inside; the end of the list loads the next page by itself. */}
              <div data-recent-scroll className="max-h-[min(42vh,360px)] min-h-0 overflow-y-auto overscroll-contain pb-3">
                <ul>
                  {[...data.unseen, ...data.recent].sort((a, b) => b.at - a.at).map((row) => (
                    <Row key={row.id} row={row} now={now} onOpen={open} />
                  ))}
                </ul>
                {data.hasMore ? <LoadMore key={data.unseen.length + data.recent.length} onVisible={() => setLimit((n) => n + PAGE)} /> : null}
              </div>
            </>
          )}
        </div>
      </div>
      <div className="shrink-0 bg-background">
        <div className="mx-auto box-border w-full max-w-[640px] px-2 pb-3 pt-1 md:px-3">
          <NewThreadComposer
            defaultProjectId={projectId ?? undefined}
            draftKey="recent-new-thread"
            onSubmit={async (request) => {
              const thread = await sdk.threads.spawn(request);
              navigate.toThread(thread.id);
            }}
          />
        </div>
      </div>
    </div>
  );
}

function MinimalSidebar() {
  // Deliberately empty: on the «Недавние» page nothing competes with the list (like Recent on Android).
  return (
    <nav className="flex h-full flex-col justify-end px-4 py-3">
      <p className="text-xs text-muted-foreground/70">Весь список тредов — значок домика</p>
    </nav>
  );
}

export default definePluginApp((app) => {
  app.slots.navPanel({
    id: "recent",
    title: "Недавние",
    icon: "Clock",
    path: "recent",
    component: RecentPage,
    experimental_sidebar: MinimalSidebar,
  });
});
