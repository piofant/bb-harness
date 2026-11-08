// bb-plugin-library — страница «Библиотека» в левой панели bb.
//
// Пять стеллажей — пять разделов базы знаний, у каждого свой библиотекарь.
// Если карточки его раздела подкладывали агентам за последний час, он носит
// их к читальным столам. Сверху доска: что читают, что нового, здоровье базы.
import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { definePluginApp, useBbNavigate, useRealtime, useRpc } from "@get-bb/plugin-sdk/app";
import type { LibraryState, ManageState, rpcContract } from "./server";
import { CHOICE_LABEL, type Choice } from "./lib/manage";
import { AREA_INFO, AREAS, type Area } from "./lib/kb";
import { LIBRARY_PNG, SPRITE_PNG } from "./lib/assets.generated";
import { createUnit, HOME_SPOTS, MAP, stepScene, type Unit } from "./lib/scene";

/* ───────────────────────────────── данные ───────────────────────────────── */

function useLibrary() {
  const rpc = useRpc<typeof rpcContract>();
  const [state, setState] = useState<LibraryState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const refetch = useCallback(() => {
    const id = ++seq.current;
    rpc.call("library_state", null).then(
      (next) => {
        if (id !== seq.current) return;
        setState(next);
        setError(null);
      },
      (cause: unknown) => {
        if (id === seq.current) setError(cause instanceof Error ? cause.message : String(cause));
      },
    );
  }, [rpc]);
  useEffect(() => refetch(), [refetch]);
  useRealtime("library-changed", refetch);
  // Время «N мин назад» стареет и без новых данных.
  useEffect(() => {
    const t = setInterval(refetch, 60_000);
    return () => clearInterval(t);
  }, [refetch]);
  return { rpc, state, error };
}

function ago(ms: number): string {
  const min = Math.max(0, Math.round((Date.now() - ms) / 60_000));
  if (min < 1) return "только что";
  if (min < 60) return `${min} мин`;
  const h = Math.round(min / 60);
  return h < 24 ? `${h} ч` : `${Math.round(h / 24)} дн`;
}

const AREA_COLOR: Record<Area, string> = {
  agents: "#6fa8ff",
  infra: "#6fbf73",
  projects: "#c78bff",
  tools: "#ffd84a",
  notes: "#ff9d5c",
};

/* ──────────────────────────── читалка карточки ──────────────────────────── */

/** Строка карточки с **жирным**, `кодом` и [[ссылками]] на другие карточки. */
function inline(text: string, open: (name: string) => void): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[\[[\w.-]+\]\])/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    if (t.startsWith("**")) out.push(<b key={k++}>{t.slice(2, -2)}</b>);
    else if (t.startsWith("`")) out.push(<code key={k++}>{t.slice(1, -1)}</code>);
    else {
      const name = t.slice(2, -2);
      out.push(
        <button key={k++} type="button" className="lib-link" onClick={() => open(name)}>
          {name}
        </button>,
      );
    }
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function Markdown({ body, open }: { body: string; open: (name: string) => void }) {
  const lines = body.split("\n");
  const out: ReactNode[] = [];
  let code: string[] | null = null;
  lines.forEach((line, i) => {
    if (line.startsWith("```")) {
      if (code) {
        out.push(<pre key={i}>{code.join("\n")}</pre>);
        code = null;
      } else code = [];
      return;
    }
    if (code) return void code.push(line);
    if (/^#{1,6} /.test(line)) out.push(<h3 key={i}>{inline(line.replace(/^#+ /, ""), open)}</h3>);
    else if (/^\s*[-*] /.test(line)) out.push(<li key={i}>{inline(line.replace(/^\s*[-*] /, ""), open)}</li>);
    else if (line.trim()) out.push(<p key={i}>{inline(line, open)}</p>);
  });
  return <div className="lib-md">{out}</div>;
}

function Reader({
  name,
  hostId,
  onClose,
  onOpen,
}: {
  name: string;
  hostId: string | null;
  onClose: () => void;
  onOpen: (n: string) => void;
}) {
  const rpc = useRpc<typeof rpcContract>();
  const navigate = useBbNavigate();
  // Клиент RPC может быть новым на каждой отрисовке; запрос зависит только
  // от имени карточки, иначе ответ → отрисовка → новый запрос по кругу.
  const rpcRef = useRef(rpc);
  rpcRef.current = rpc;
  const [card, setCard] = useState<{ name: string; area: Area; path: string; body: string } | null | undefined>(undefined);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    setCard(undefined);
    setFailed(false);
    rpcRef.current.call("library_card", { name }).then(
      (c) => alive && setCard(c),
      () => {
        if (!alive) return;
        setFailed(true);
        setCard(null);
      },
    );
    return () => {
      alive = false;
    };
  }, [name]);
  return (
    <aside className="lib-reader" aria-label={`Карточка ${name}`}>
      <div className="lib-reader-head">
        {card ? <span className="lib-dot" style={{ background: AREA_COLOR[card.area] }} /> : null}
        <span className="lib-reader-name">{name}</span>
        {card && hostId ? (
          <button
            type="button"
            className="lib-open"
            title="Открыть в панели bb (Obsidian at Home: ссылки, превью, правка)"
            onClick={() => {
              const ok = navigate.experimental_openFilePreview({ target: { kind: "host", hostId, path: card.path }, location: null });
              if (ok) onClose();
            }}
          >
            в панели ↗
          </button>
        ) : null}
        <button type="button" aria-label="Закрыть" onClick={onClose}>
          ×
        </button>
      </div>
      {card === undefined ? (
        <p className="lib-dim">Достаём с полки…</p>
      ) : card === null ? (
        <p className="lib-dim">{failed ? "Не получилось достать карточку — попробуй ещё раз." : "Такой карточки в индексе нет."}</p>
      ) : (
        <Markdown body={card.body} open={onOpen} />
      )}
    </aside>
  );
}

/* ───────────────────────────────── доска ───────────────────────────────── */

/* ─────────────────────────────── управление ─────────────────────────────── */

function useManage() {
  const rpc = useRpc<typeof rpcContract>();
  const [m, setM] = useState<ManageState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const refetch = useCallback(() => {
    const id = ++seq.current;
    rpc.call("library_manage", null).then(
      (next) => {
        if (id === seq.current) setM(next);
      },
      (cause: unknown) => {
        if (id === seq.current) setError(cause instanceof Error ? cause.message : String(cause));
      },
    );
  }, [rpc]);
  useEffect(() => refetch(), [refetch]);
  useRealtime("library-changed", refetch);
  // Статусы тредов-поручений и прогонов меняются и без событий базы.
  useEffect(() => {
    const t = setInterval(refetch, 60_000);
    return () => clearInterval(t);
  }, [refetch]);
  /** Что сейчас отправляется: кнопка с этим ключом неактивна до ответа. */
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const act = useCallback(
    (key: string, run: () => Promise<unknown>) => {
      if (busy.has(key)) return;
      setError(null);
      setBusy((b) => new Set(b).add(key));
      run()
        .then(refetch, (cause: unknown) => setError(cause instanceof Error ? cause.message : String(cause)))
        .finally(() =>
          setBusy((b) => {
            const n = new Set(b);
            n.delete(key);
            return n;
          }),
        );
    },
    [refetch, busy],
  );
  return { rpc, m, error, act, busy };
}

const LEVEL_COLOR = { ok: "#6fcf7a", warn: "#e8b04a", bad: "#ff6a5a", unknown: "#888" } as const;

function ConflictRow({
  c,
  sending,
  onDecide,
}: {
  c: ManageState["conflicts"][number];
  sending: boolean;
  onDecide: (choice: Choice, note: string) => void;
}) {
  const navigate = useBbNavigate();
  const [open, setOpen] = useState(false);
  const [choice, setChoice] = useState<Choice | null>(null);
  const [note, setNote] = useState("");
  return (
    <li className="lib-conflict">
      <button type="button" className="lib-conflict-head" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span className="lib-dot" style={{ background: c.pending && !c.pending.stale ? LEVEL_COLOR.ok : c.pending?.stale ? LEVEL_COLOR.bad : LEVEL_COLOR.warn }} />
        <span className="lib-main">{c.slug}</span>
        <span className="lib-meta">
          {c.pending ? (c.pending.stale ? "не оформилось" : "оформляется") : c.date.slice(5).split("-").reverse().join(".")}
        </span>
      </button>
      {open ? (
        <div className="lib-conflict-body">
          <p>{c.text}</p>
          {c.pending ? (
            <button type="button" className="lib-btn" onClick={() => navigate.toThread(c.pending!.threadId)}>
              Ты решил: {CHOICE_LABEL[c.pending.choice as Choice] ?? c.pending.choice} — открыть тред агента →
            </button>
          ) : null}
          {c.pending?.stale ? <p className="lib-why">Агент не закрыл конфликт — посмотри его тред или реши заново.</p> : null}
          {c.pending && !c.pending.stale ? null : choice === null ? (
            <div className="lib-choices">
              {(["first", "second", "both", "custom"] as const).map((ch) => (
                <button key={ch} type="button" className="lib-btn" onClick={() => setChoice(ch)}>
                  {CHOICE_LABEL[ch]}
                </button>
              ))}
            </div>
          ) : (
            <div className="lib-confirm">
              <input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                maxLength={600}
                placeholder={choice === "custom" ? "твой ответ" : "уточнение (необязательно)"}
                autoFocus
              />
              <button
                type="button"
                className="lib-btn is-main"
                disabled={sending || (choice === "custom" && !note.trim())}
                onClick={() => onDecide(choice, note.trim())}
              >
                {sending ? "отправляю…" : `Отдать агенту: «${CHOICE_LABEL[choice]}»`}
              </button>
              <button type="button" className="lib-btn" onClick={() => setChoice(null)}>
                отмена
              </button>
            </div>
          )}
        </div>
      ) : null}
    </li>
  );
}

function Manage() {
  const { rpc, m, error, act, busy } = useManage();
  const navigate = useBbNavigate();
  if (!m) return <p className="lib-dim">{error ?? "Смотрим, что требует решения…"}</p>;
  return (
    <div className="lib-manage">
      {error ? <p className="lib-err">{error}</p> : null}
      <h4>Решить ({m.conflicts.length})</h4>
      {m.conflicts.length === 0 ? (
        <p className="lib-dim">Противоречий нет.</p>
      ) : (
        <ul className="lib-list">
          {m.conflicts.map((c) => (
            <ConflictRow
              key={c.slug}
              c={c}
              sending={busy.has(`decide:${c.slug}`)}
              onDecide={(choice, note) => act(`decide:${c.slug}`, () => rpc.call("library_decide", { slug: c.slug, choice, note }))}
            />
          ))}
        </ul>
      )}
      <h4>Конвейер</h4>
      <ul className="lib-list">
        {m.steps.map((st) => (
          <li key={st.id} className="lib-step">
            <span className="lib-dot" style={{ background: LEVEL_COLOR[st.level] }} />
            <span className="lib-main" title={st.lastRun ?? st.why}>
              {st.label}
              {st.why ? <span className="lib-why"> — {st.why}</span> : null}
              {st.lastRun ? <span className="lib-why"> · последний запуск: {st.lastRun}</span> : null}
            </span>
            <span className="lib-meta">{st.at ? ago(st.at) : ""}</span>
            {st.runnable ? (
              <button
                type="button"
                className="lib-btn"
                disabled={st.running || m.steps.some((x) => x.running) || busy.has("run")}
                title={m.steps.some((x) => x.running) && !st.running ? "Сначала дождись идущего прогона: шаги делят одну блокировку базы" : undefined}
                onClick={() => act("run", () => rpc.call("library_run", { step: st.id as "ingest" | "consolidate" | "index" }))}
              >
                {st.running ? "идёт…" : "прогнать"}
              </button>
            ) : null}
          </li>
        ))}
      </ul>
      <h4>Ничего не сохранили ({m.unsaved.length})</h4>
      {m.unsaved.length === 0 ? (
        <p className="lib-dim">Из всех недавних тредов что-то попало в базу.</p>
      ) : (
        <ul className="lib-list">
          {m.unsaved.map((u) => (
            <li key={u.threadId} className="lib-step">
              <span className="lib-dot" style={{ background: u.requested ? LEVEL_COLOR.ok : "#888" }} />
              <button type="button" className="lib-main lib-linkish" onClick={() => navigate.toThread(u.threadId)} title={u.title}>
                {u.title}
              </button>
              <span className="lib-meta">{ago(u.updatedAt)}</span>
              <button
                type="button"
                className="lib-btn"
                disabled={u.requested || busy.has(`save:${u.threadId}`)}
                title="Попросить агента этого треда записать устойчивое в базу через kb note"
                onClick={() => act(`save:${u.threadId}`, () => rpc.call("library_save", { threadId: u.threadId }))}
              >
                {u.requested ? "попросил ✓" : "сохранить в базу"}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

type Tab = "reads" | "new" | "health" | "manage";
const HEALTH_LABEL = { green: "всё хорошо", yellow: "есть замечания", red: "нужна помощь", unknown: "нет данных" };
const HEALTH_COLOR = { green: "#6fcf7a", yellow: "#e8b04a", red: "#ff6a5a", unknown: "#888" };

function Board({
  state,
  filter,
  clearFilter,
  open,
}: {
  state: LibraryState;
  filter: Area | null;
  clearFilter: () => void;
  open: (name: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("reads");
  useEffect(() => {
    if (filter) setTab("reads");
  }, [filter]);
  const reads = state.reads.filter((r) => !filter || r.area === filter).slice(0, 8);
  const row = (key: string, color: string, main: string, meta: string, onClick?: () => void) => (
    <li key={key}>
      <button type="button" disabled={!onClick} onClick={onClick} title={main}>
        <span className="lib-dot" style={{ background: color }} />
        <span className="lib-main">{main}</span>
        <span className="lib-meta">{meta}</span>
      </button>
    </li>
  );
  return (
    <section className="lib-board">
      <div className="lib-board-inner">
        {filter ? (
          <button type="button" className="lib-filter" onClick={clearFilter}>
            {AREA_INFO[filter].label} ×
          </button>
        ) : null}
        {tab === "reads" ? (
          reads.length > 0 ? (
            <ul className="lib-list">
              {reads.map((r) =>
                row(r.name, r.area ? AREA_COLOR[r.area] : "#888", r.description || r.name, `×${r.count} · ${ago(r.last)}`, () =>
                  open(r.name),
                ),
              )}
            </ul>
          ) : (
            <p className="lib-dim">За неделю агентам ничего не подкладывали.</p>
          )
        ) : null}
        {tab === "new" ? (
          <ul className="lib-list">
            {state.changes.slice(0, 6).map((c) =>
              row(c.name, AREA_COLOR[c.area], c.description || c.name, c.kind === "added" ? `новая · ${ago(c.at)}` : `обновлена · ${ago(c.at)}`, () =>
                open(c.name),
              ),
            )}
            {state.notes.length > 0
              ? row("notes", "#888", state.notes[0]!.text, `заметок ждут сводчика: ${state.notes.length}`)
              : null}
            {state.changes.length === 0 && state.notes.length === 0 ? <p className="lib-dim">Ничего нового.</p> : null}
          </ul>
        ) : null}
        {tab === "manage" ? <Manage /> : null}
        {tab === "health" ? (
          <ul className="lib-list">
            {row("status", HEALTH_COLOR[state.health.status], `База: ${HEALTH_LABEL[state.health.status]}`, state.health.at ? ago(state.health.at) : "")}
            {state.health.problems.slice(0, 6).map((p, i) =>
              row(String(i), p.level === "red" ? HEALTH_COLOR.red : HEALTH_COLOR.yellow, p.what, ""),
            )}
          </ul>
        ) : null}
      </div>
      <div className="lib-tabs" role="tablist">
        {(
          [
            ["reads", "1 · читают"],
            ["new", "2 · новое"],
            ["health", "3 · здоровье"],
            ["manage", "4 · управление"],
          ] as const
        ).map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} className={tab === id ? "is-on" : ""} onClick={() => setTab(id)}>
            {label}
            {id === "health" ? <span className="lib-dot" style={{ background: HEALTH_COLOR[state.health.status] }} /> : null}
          </button>
        ))}
      </div>
    </section>
  );
}

/* ───────────────────────────────── сцена ───────────────────────────────── */

function Scene({ state, onPick, picked }: { state: LibraryState; onPick: (a: Area) => void; picked: Area | null }) {
  const unitsRef = useRef<Unit[]>([]);
  const elsRef = useRef(new Map<string, HTMLButtonElement>());
  // 0 — отдыхает, 1 — носит карточки, 2 — носит часто (быстрее ходит).
  const busyKey = state.totals.map((t) => (t.reads1h > 2 ? 2 : t.reads1h > 0 ? 1 : 0)).join("");

  // Библиотекарь всегда у своего стеллажа (индекс раздела), а с карточками
  // за последний час — ходит к столам. Смена роли — пешком, без телепорта.
  useEffect(() => {
    unitsRef.current = state.totals.map((t, i) => {
      const role = t.reads1h > 0 ? "working" : "resting";
      const fresh = createUnit(t.area, role, i, t.reads1h > 2);
      const old = unitsRef.current.find((u) => u.key === t.area);
      if (!old) return fresh;
      if (old.role === role) return { ...old, busy: fresh.busy };
      return { ...fresh, x: old.x, y: old.y, dir: old.dir, state: "toHome" };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busyKey]);

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let raf = 0;
    let last = performance.now();
    let cycle = 0;
    const frame = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      if (!reduced) stepScene(unitsRef.current, dt, ++cycle);
      for (const u of unitsRef.current) {
        const el = elsRef.current.get(u.key);
        if (!el) continue;
        el.style.setProperty("--x", String(u.x));
        el.style.setProperty("--y", String(u.y));
        const body = el.firstElementChild as HTMLElement | null;
        if (body) body.style.backgroundPosition = `${(u.dir * 100) / 3}% ${(u.frame * 100) / 3}%`;
      }
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, []);

  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  const lamp = HEALTH_COLOR[state.health.status];
  return (
    <div className="lib-scene" style={{ backgroundImage: `url(${LIBRARY_PNG})` }}>
      {/* Лампа здоровья базы на столе в середине зала. */}
      <div
        className="lib-lamp"
        style={{ left: pct(244, MAP.w), top: pct(124, MAP.h), "--lamp": lamp } as CSSProperties}
        title={`Здоровье базы: ${HEALTH_LABEL[state.health.status]}`}
      />
      {state.totals.map((t, i) => (
        <div key={t.area} className="lib-shelf-label" style={{ left: pct(HOME_SPOTS[i]!.x, MAP.w), top: pct(4, MAP.h) }}>
          {AREA_INFO[t.area].label} · {t.cards}
        </div>
      ))}
      {state.totals.map((t) => {
        const info = AREA_INFO[t.area];
        return (
          <button
            key={t.area}
            type="button"
            ref={(el) => {
              if (el) elsRef.current.set(t.area, el);
              else elsRef.current.delete(t.area);
            }}
            className={`lib-unit${picked === t.area ? " is-picked" : ""}`}
            style={{ "--x": HOME_SPOTS[AREAS.indexOf(t.area)]!.x, "--y": HOME_SPOTS[0]!.y } as CSSProperties}
            aria-label={`${info.keeper}, раздел ${info.label}: за час ${t.reads1h}, за сутки ${t.reads24h}`}
            title={`${info.keeper} · ${info.label}: за час ${t.reads1h}, за сутки ${t.reads24h}`}
            onClick={() => onPick(t.area)}
          >
            <span className="lib-body" style={{ backgroundImage: `url(${SPRITE_PNG[info.sprite]})` }} />
            <span className="lib-tag">{info.keeper}</span>
            {t.reads1h > 0 ? <span className="lib-bubble">{t.reads1h}</span> : null}
          </button>
        );
      })}
    </div>
  );
}

/* ─────────────────────────────── страница ─────────────────────────────── */

function LibraryPage() {
  const { state, error } = useLibrary();
  const [filter, setFilter] = useState<Area | null>(null);
  const [reading, setReading] = useState<string | null>(null);
  const rpcForHost = useRpc<typeof rpcContract>();
  const [hostId, setHostId] = useState<string | null>(null);
  useEffect(() => {
    // Хост узнаётся на первой загрузке сервера — переспрашиваем, пока не ответит.
    if (hostId !== null) return;
    let alive = true;
    const ask = () => rpcForHost.call("library_host", null).then((r) => alive && setHostId(r.hostId), () => undefined);
    ask();
    const t = setInterval(ask, 15_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostId]);
  return (
    <div className="h-full min-h-0 flex-1 overflow-y-auto">
      <style>{CSS}</style>
      <div className="mx-auto box-border w-full max-w-5xl px-4 pb-6 pt-3 md:px-5 md:pt-4">
        {error ? (
          <p role="alert" className="mb-3 text-sm text-destructive">
            {error}
          </p>
        ) : null}
        {state === null ? (
          <p className="lib-dim">Открываем библиотеку…</p>
        ) : (
          <>
            <Board state={state} filter={filter} clearFilter={() => setFilter(null)} open={setReading} />
            <div className="lib-frame">
              <Scene state={state} picked={filter} onPick={(a) => setFilter((cur) => (cur === a ? null : a))} />
              {reading ? <Reader name={reading} hostId={hostId} onClose={() => setReading(null)} onOpen={setReading} /> : null}
            </div>
            {state.error ? <p className="lib-dim mt-2 text-xs">Не всё прочиталось: {state.error}</p> : null}
          </>
        )}
      </div>
    </div>
  );
}

const CSS = `
.lib-dim { opacity: 0.65; font-size: 13px; }
.lib-board {
  margin: 4px 0 14px; padding: 8px; border-radius: 12px;
  background: linear-gradient(#4a2e18, #321e10); border: 2px solid #1a0f07; box-shadow: 0 5px 0 #1a0f07;
}
.lib-board-inner {
  position: relative; overflow: hidden; border-radius: 8px; padding: 8px 10px; min-height: 96px;
  background: #f3e6c8; color: #2a1c0e; box-shadow: inset 0 0 18px rgba(120, 80, 30, 0.35);
}
.lib-list { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: minmax(0, 1fr); gap: 1px; }
.lib-list button {
  width: 100%; display: flex; align-items: center; gap: 8px; min-width: 0; text-align: left;
  border: 0; background: none; color: inherit; border-radius: 4px; padding: 3px 4px; font: 13px/1.4 system-ui, sans-serif; cursor: pointer;
}
.lib-list button:disabled { cursor: default; }
.lib-list button:not(:disabled):hover { background: rgba(120, 80, 30, 0.12); }
.lib-dot { flex: none; display: inline-block; width: 8px; height: 8px; border-radius: 50%; }
.lib-main { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lib-meta { flex: none; font-size: 11px; opacity: 0.6; }
.lib-filter { position: absolute; top: 6px; right: 8px; border: 1px solid rgba(42,28,14,0.3); background: #fff8e8; border-radius: 999px; padding: 0 8px; font-size: 11px; cursor: pointer; color: inherit; z-index: 1; }
.lib-tabs { display: flex; gap: 6px; justify-content: center; padding-top: 7px; }
.lib-tabs button { display: inline-flex; align-items: center; gap: 6px; border: 1px solid #1a0f07; background: linear-gradient(#6b4a2e, #4a2e18); color: #ffd9a8; border-radius: 4px; padding: 2px 10px; font: 600 11px ui-monospace, monospace; cursor: pointer; box-shadow: 0 2px 0 #1a0f07; }
.lib-tabs button.is-on { background: linear-gradient(#ffb347, #e8902a); color: #1a1208; transform: translateY(1px); box-shadow: 0 1px 0 #1a0f07; }
.lib-frame { position: relative; border-radius: 10px; overflow: hidden; background: #1a120b; }
.lib-scene { position: relative; width: 100%; aspect-ratio: 416 / 256; background-size: 100% 100%; image-rendering: pixelated; --map-w: 416; --map-h: 256; }
.lib-scene::after { content: ''; position: absolute; inset: 0; background: rgba(16, 10, 5, 0.32); pointer-events: none; z-index: 1; }
.lib-shelf-label { position: absolute; z-index: 3; transform: translateX(-50%); font: 10px ui-monospace, monospace; color: #ffd9a8; background: rgba(26,18,11,0.75); border-radius: 3px; padding: 0 4px; white-space: nowrap; pointer-events: none; }
.lib-lamp { position: absolute; z-index: 2; width: 10%; aspect-ratio: 1; transform: translate(-50%, -50%); border-radius: 50%; pointer-events: auto;
  background: radial-gradient(circle, var(--lamp) 0 12%, color-mix(in srgb, var(--lamp) 45%, transparent) 25%, transparent 70%);
  mix-blend-mode: screen; animation: lib-pulse 2.8s ease-in-out infinite; }
@keyframes lib-pulse { 50% { opacity: 0.65; transform: translate(-50%, -50%) scale(0.92); } }
.lib-unit { position: absolute; z-index: 4; padding: 0; border: 0; background: none; cursor: pointer;
  left: calc(var(--x) / 416 * 100%); top: calc(var(--y) / 256 * 100%); width: calc(22 * var(--k, 1) / 416 * 100%); aspect-ratio: 1; transform: translate(-50%, -100%); }
.lib-body { display: block; width: 100%; height: 100%; background-size: 400% 400%; image-rendering: pixelated; }
.lib-tag { position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%); font: 600 10px ui-monospace, monospace; background: #ffd9a8; color: #1a1208; border-radius: 3px; padding: 1px 4px; white-space: nowrap; opacity: 0; transition: opacity .15s; pointer-events: none; }
.lib-unit:hover .lib-tag, .lib-unit.is-picked .lib-tag { opacity: 1; }
.lib-unit.is-picked .lib-body, .lib-unit:hover .lib-body { filter: drop-shadow(0 0 5px #ffd9a8); }
.lib-bubble { position: absolute; right: -20%; top: -20%; background: #ffb347; color: #1a1208; font: 700 9px ui-monospace, monospace; border-radius: 3px; padding: 1px 3px; }
/* Читалка — выезжающая панель поверх страницы: в маленькой сцене длинная карточка не читалась бы. */
.lib-reader { position: fixed; z-index: 50; top: 64px; right: 12px; bottom: 12px; width: min(460px, calc(100vw - 24px)); overflow-y: auto; box-sizing: border-box;
  background: #f3e6c8; color: #2a1c0e; border-radius: 10px; padding: 10px 14px; box-shadow: 0 6px 24px rgba(0,0,0,0.45); font: 13px/1.5 system-ui, sans-serif; }
.lib-reader-head { display: flex; align-items: center; gap: 8px; position: sticky; top: -10px; background: #f3e6c8; padding: 2px 0 6px; }
.lib-reader-name { flex: 1; font: 700 13px ui-monospace, monospace; }
.lib-reader-head button { border: 0; background: none; font-size: 20px; cursor: pointer; color: inherit; }
.lib-md h3 { margin: 10px 0 4px; font-size: 14px; }
.lib-md p { margin: 4px 0; }
.lib-md li { margin: 2px 0 2px 16px; }
.lib-md code { background: rgba(120,80,30,0.15); padding: 0 3px; border-radius: 3px; font-size: 12px; }
.lib-md pre { background: rgba(120,80,30,0.12); padding: 6px 8px; border-radius: 6px; overflow-x: auto; font-size: 12px; }
.lib-link { border: 0; background: none; padding: 0; color: #9a3d12; text-decoration: underline; cursor: pointer; font: inherit; }
.lib-manage h4 { margin: 8px 0 3px; font: 700 11px ui-monospace, monospace; letter-spacing: 0.08em; text-transform: uppercase; opacity: 0.7; }
.lib-manage h4:first-of-type { margin-top: 0; }
.lib-manage { max-height: 380px; overflow-y: auto; padding-right: 2px; }
.lib-err { color: #a3261b; font-size: 12px; margin: 0 0 6px; }
.lib-step { display: flex; align-items: center; gap: 8px; min-width: 0; padding: 3px 4px; font: 13px/1.4 system-ui, sans-serif; }
.lib-why { opacity: 0.65; font-size: 12px; }
.lib-step .lib-main { white-space: nowrap; }
.lib-list .lib-linkish { width: auto; flex: 1; border: 0; background: none; padding: 0; text-align: left; color: inherit; cursor: pointer; font: inherit; }
.lib-linkish:hover { text-decoration: underline; }
.lib-list .lib-btn, .lib-btn { flex: none; width: auto; display: inline-flex; gap: 4px; border: 1px solid rgba(42,28,14,0.35); background: #fff8e8; color: inherit; border-radius: 999px; padding: 1px 9px; font: 12px system-ui, sans-serif; cursor: pointer; }
.lib-btn:hover:not(:disabled) { background: #ffe9c2; }
.lib-btn:disabled { opacity: 0.55; cursor: default; }
.lib-btn.is-main { background: #e8902a; border-color: #b8681a; color: #1a1208; font-weight: 600; }
.lib-list .lib-conflict-head { width: 100%; display: flex; align-items: center; gap: 8px; border: 0; background: none; padding: 3px 4px; color: inherit; cursor: pointer; text-align: left; font: 13px/1.4 system-ui, sans-serif; border-radius: 4px; }
.lib-conflict-head:hover { background: rgba(120,80,30,0.12); }
.lib-conflict-body { margin: 2px 0 8px 20px; font: 13px/1.45 system-ui, sans-serif; }
.lib-conflict-body p { margin: 0 0 6px; }
.lib-choices, .lib-confirm { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }
.lib-confirm input { flex: 1 1 220px; min-width: 0; border: 1px solid rgba(42,28,14,0.35); border-radius: 6px; padding: 3px 7px; background: #fffdf6; color: inherit; font: 13px system-ui, sans-serif; }
.lib-open { border: 1px solid rgba(42,28,14,0.3) !important; border-radius: 999px; font-size: 11px !important; padding: 1px 8px !important; }
@media (max-width: 640px) {
  .lib-step { flex-wrap: wrap; }
  .lib-step .lib-main { white-space: normal; flex-basis: calc(100% - 20px); }
  .lib-unit { --k: 1.45; }
  .lib-shelf-label { font-size: 8px; padding: 0 2px; }
  .lib-meta { display: none; }
  .lib-reader { top: 56px; left: 6px; right: 6px; bottom: 6px; width: auto; }
}
@media (prefers-reduced-motion: reduce) { .lib-lamp { animation: none; } }
`;

export default definePluginApp((app) => {
  app.slots.navPanel({
    id: "library",
    title: "Библиотека",
    icon: "library/book",
    path: "library",
    component: LibraryPage,
  });
});
