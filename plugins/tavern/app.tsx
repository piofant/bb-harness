// bb-plugin-tavern — страница таверны в левой панели bb.
//
// Зал и спрайты — с piofant.github.io/skills (Ninja Adventure, CC0). Кто
// работает и кто отдыхает, приходит с сервера (server.ts); здесь только сцена:
// ходьба по залу, свет по московскому солнцу, часы в углу и карточка по клику.
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent } from "react";
import { definePluginApp, useBbNavigate, useRealtime, useRpc } from "@get-bb/plugin-sdk/app";
import type { Character, TavernState, TvState, rpcContract } from "./server";
import { HALL_PNG, SPRITE_PNG } from "./lib/assets.generated";
import { REASON_LABEL } from "./lib/waiting";
import { MAP, stepScene, syncUnits, type Role, type Unit } from "./lib/scene";
import { formatMoscow, lighting, moscowClock, sunAltitude, sunTimes, type DayPhase } from "./lib/sun";

/* ───────────────────────── время: сейчас или превью ───────────────────────── */

/**
 * Кнопка в шапке листает превью времени суток, а сцена его читает. Это два
 * разных компонента, поэтому общий стор на модуле, а не useState.
 */
type Preview = null | "dawn" | "day" | "dusk" | "night";
const PREVIEW_ORDER: Preview[] = [null, "dawn", "day", "dusk", "night"];
const PREVIEW_LABEL: Record<Exclude<Preview, null>, string> = {
  dawn: "рассвет",
  day: "день",
  dusk: "закат",
  night: "ночь",
};
let preview: Preview = null;
const previewListeners = new Set<() => void>();
function setPreview(next: Preview) {
  preview = next;
  for (const l of previewListeners) l();
}
function usePreview(): Preview {
  return useSyncExternalStore(
    (l) => {
      previewListeners.add(l);
      return () => previewListeners.delete(l);
    },
    () => preview,
  );
}

/** Момент, который показываем: реальный или середина выбранной фазы сегодня. */
function previewTime(now: number, p: Preview): number {
  if (p === null) return now;
  const { sunrise, sunset } = sunTimes(now);
  const rise = sunrise ?? now;
  const set = sunset ?? now;
  switch (p) {
    case "dawn":
      return rise + 10 * 60_000;
    case "day":
      return (rise + set) / 2;
    case "dusk":
      return set + 5 * 60_000;
    case "night":
      return set + 5 * 3_600_000;
  }
}

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/* ───────────────────────────────── данные ───────────────────────────────── */

function useTavern() {
  const rpc = useRpc<typeof rpcContract>();
  const [state, setState] = useState<TavernState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const refetch = useCallback(() => {
    const id = ++seq.current;
    rpc.call("tavern_state", null).then(
      (next) => {
        if (id !== seq.current) return;
        setState(next);
        setError(null);
      },
      (cause: unknown) => setError(cause instanceof Error ? cause.message : String(cause)),
    );
  }, [rpc]);
  useEffect(() => refetch(), [refetch]);
  useRealtime("tavern-changed", refetch);
  return { rpc, state, error, refetch };
}

function ago(ms: number, now: number): string {
  const min = Math.max(0, Math.round((now - ms) / 60_000));
  if (min < 1) return "только что";
  if (min < 60) return `${min} мин назад`;
  const h = Math.round(min / 60);
  if (h < 24) return `${h} ч назад`;
  return `${Math.round(h / 24)} дн назад`;
}

function plural(n: number, one: string, few: string, many: string): string {
  const m10 = n % 10;
  const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

const pct = (v: number, of: number) => `${(v / of) * 100}%`;

/* ───────────────────────────────── свет ───────────────────────────────── */

/** Сколько притушить сцену днём; на сайте таверны было 0.34. */
const DAY_DIM = 0.38;

/** Где горит огонь: очаг, свечи на стойках, фонари у перил веранды. */
const FIRES = [
  { x: 361, y: 50, r: 78, big: true }, // очаг
  { x: 40, y: 118, r: 34, big: false },
  { x: 148, y: 42, r: 34, big: false },
  { x: 262, y: 42, r: 34, big: false },
  { x: 150, y: 114, r: 34, big: false },
  { x: 270, y: 114, r: 34, big: false },
  { x: 90, y: 228, r: 40, big: false }, // фонари веранды
  { x: 310, y: 228, r: 40, big: false },
];

function Lights({ at }: { at: number }) {
  const light = lighting(at);
  return (
    <>
      {/* Приглушка есть всегда, даже днём: пиксельный пол на тёмной теме bb
          иначе бьёт в глаза. Ночью поверх неё синяя тень. */}
      <div className="tavern-layer" style={{ background: `rgba(16, 10, 5, ${DAY_DIM})` }} />
      <div
        className="tavern-layer"
        style={{ background: `rgba(10, 14, 44, ${(light.darkness * 0.55).toFixed(3)})` }}
      />
      <div
        className="tavern-layer"
        style={{
          top: pct(MAP.verandaTop, MAP.h),
          background: `rgba(6, 10, 34, ${(light.darkness * 0.22).toFixed(3)})`,
        }}
      />
      {/* Рассвет и закат: тёплый оттенок поверх всего. */}
      <div
        className="tavern-layer"
        style={{
          background: `rgba(255, 138, 64, ${(light.warmth * 0.2).toFixed(3)})`,
          mixBlendMode: "soft-light",
        }}
      />
      {/* Огонь: круги света и язычки пламени. Днём их нет. */}
      {light.candles > 0.02
        ? FIRES.map((f, i) => (
            <div key={i}>
              <div
                className="tavern-glow"
                style={{
                  left: pct(f.x - f.r, MAP.w),
                  top: pct(f.y - f.r, MAP.h),
                  width: pct(f.r * 2, MAP.w),
                  height: pct(f.r * 2, MAP.h),
                  opacity: light.candles * (f.big ? 0.95 : 0.7),
                  animationDelay: `${(i * 0.37) % 1.3}s`,
                }}
              />
              {light.candles > 0.15 ? (
                <div
                  className="tavern-flame"
                  style={{
                    left: pct(f.x - (f.big ? 3 : 1), MAP.w),
                    top: pct(f.y - (f.big ? 2 : 3), MAP.h),
                    width: pct(f.big ? 6 : 2, MAP.w),
                    height: pct(f.big ? 6 : 3, MAP.h),
                    animationDelay: `${(i * 0.53) % 0.9}s`,
                  }}
                />
              ) : null}
            </div>
          ))
        : null}
    </>
  );
}

/* ───────────────────────────────── часы ───────────────────────────────── */

const PHASE_LABEL: Record<DayPhase, string> = {
  dawn: "рассвет",
  day: "день",
  dusk: "закат",
  night: "ночь",
};

function Clock({ at, isPreview }: { at: number; isPreview: boolean }) {
  const { h, m } = moscowClock(at);
  const { sunrise, sunset } = sunTimes(at);
  const phase = lighting(at).phase;
  const up = sunAltitude(at) > -0.833;
  const hourAngle = ((h % 12) + m / 60) * 30;
  const minAngle = m * 6;
  return (
    <div className="tavern-clock" aria-label={`Москва ${formatMoscow(at)}, ${PHASE_LABEL[phase]}`}>
      <svg viewBox="0 0 32 32" width="34" height="34" aria-hidden="true">
        <circle cx="16" cy="16" r="14" fill="#2a1d12" stroke="#ffd9a8" strokeWidth="2" />
        {Array.from({ length: 12 }, (_, i) => (
          <rect
            key={i}
            x="15.5"
            y="3.5"
            width="1"
            height={i % 3 === 0 ? 3 : 1.5}
            fill="#ffd9a8"
            transform={`rotate(${i * 30} 16 16)`}
          />
        ))}
        <line
          x1="16"
          y1="16"
          x2="16"
          y2="9"
          stroke="#ffd9a8"
          strokeWidth="2"
          strokeLinecap="round"
          transform={`rotate(${hourAngle} 16 16)`}
        />
        <line
          x1="16"
          y1="16"
          x2="16"
          y2="6"
          stroke="#ffb347"
          strokeWidth="1.2"
          strokeLinecap="round"
          transform={`rotate(${minAngle} 16 16)`}
        />
        <circle cx="16" cy="16" r="1.4" fill="#ffb347" />
      </svg>
      <div>
        <div className="tavern-clock-time">
          <span aria-hidden="true">{up ? "☀" : "☾"}</span> {formatMoscow(at)}{" "}
          <span className="tavern-dim">МСК</span>
        </div>
        <div className="tavern-clock-sun">
          ↑ {sunrise === null ? "—" : formatMoscow(sunrise)} · ↓ {sunset === null ? "—" : formatMoscow(sunset)}
        </div>
        <div className="tavern-clock-phase">
          {PHASE_LABEL[phase]}
          {isPreview ? " · превью" : ""}
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────────────── сцена ───────────────────────────────── */

const SIZE = { normal: 20, innkeeper: 22 };

interface CastEntry {
  c: Character;
  role: Role;
}

function Scene({
  cast,
  at,
  selected,
  onPick,
}: {
  cast: CastEntry[];
  at: number;
  selected: string | null;
  onPick: (slug: string) => void;
}) {
  const unitsRef = useRef<Unit[]>([]);
  const elsRef = useRef(new Map<string, HTMLButtonElement>());
  const light = lighting(at);
  const night = light.darkness > 0.6;

  // Состав поменялся — пересобрать юнитов, не телепортируя тех, кто уже тут.
  const castKey = cast.map((e) => `${e.c.slug}:${e.role}:${e.c.busy ? 1 : 0}`).join("|");
  const units = useMemo(() => {
    unitsRef.current = syncUnits(
      unitsRef.current,
      cast.map((e) => ({ key: e.c.slug, role: e.role, busy: e.c.busy })),
    );
    return unitsRef.current;
    // cast сравниваем по castKey: новый массив с тем же составом не повод пересобирать.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [castKey]);

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let raf = 0;
    let last = performance.now();
    let cycle = 0;
    const paint = () => {
      const t = performance.now() / 1000;
      for (const u of unitsRef.current) {
        const el = elsRef.current.get(u.key);
        if (!el) continue;
        el.style.setProperty("--x", String(u.x));
        el.style.setProperty("--y", String(u.y));
        const body = el.firstElementChild as HTMLElement | null;
        if (!body) continue;
        body.style.backgroundPosition = `${(u.dir * 100) / 3}% ${(u.frame * 100) / 3}%`;
        // Стоящие покачиваются: отдыхающие медленно, работающие дома — живее.
        const resting = u.role === "resting";
        const sway = u.state === "idle" ? Math.sin(t * (resting ? 0.9 : 1.6) + u.phase) * (resting ? 4 : 7) : 0;
        body.style.transform = sway ? `rotate(${sway.toFixed(2)}deg)` : "none";
      }
    };
    const frame = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      cycle += 1;
      if (!reduced) stepScene(unitsRef.current, dt, cycle);
      paint();
      raf = requestAnimationFrame(frame);
    };
    paint();
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, []);

  return (
    <div
      className="tavern-scene"
      style={{ backgroundImage: `url(${HALL_PNG})`, "--map-w": MAP.w, "--map-h": MAP.h } as CSSProperties}
    >
      <div className="tavern-zone-label" style={{ left: "1.5%", top: "1.5%" }}>
        зал · работают
      </div>
      <div className="tavern-zone-label" style={{ left: "1.5%", top: pct(MAP.verandaTop + 3, MAP.h) }}>
        веранда · отдыхают
      </div>
      <Lights at={at} />
      {cast.map(({ c, role }) => {
        const unit = units.find((u) => u.key === c.slug);
        const size = role === "innkeeper" ? SIZE.innkeeper : SIZE.normal;
        return (
          <button
            key={c.slug}
            type="button"
            ref={(el) => {
              if (el) elsRef.current.set(c.slug, el);
              else elsRef.current.delete(c.slug);
            }}
            className={`tavern-unit${selected === c.slug ? " is-picked" : ""}`}
            style={
              {
                "--x": unit?.x ?? 0,
                "--y": unit?.y ?? 0,
                "--s": size,
                filter: `brightness(${(1 - light.darkness * 0.3).toFixed(3)})`,
              } as CSSProperties
            }
            aria-label={`${c.name} — ${c.slug}`}
            onClick={() => onPick(c.slug)}
          >
            <span className="tavern-body" style={{ backgroundImage: `url(${SPRITE_PNG[c.sprite]})` }} />
            <span className="tavern-tag">{c.name}</span>
            {c.busy && role === "working" ? <span className="tavern-bubble tavern-busy">!</span> : null}
            {role === "resting" && night ? <span className="tavern-bubble tavern-zzz">z</span> : null}
          </button>
        );
      })}
    </div>
  );
}

/* ─────────────────────────────── карточка ─────────────────────────────── */

const ROLE_LABEL: Record<Role, string> = {
  working: "работает в зале",
  resting: "отдыхает на веранде",
  innkeeper: "трактирщик: зовут в каждой сессии",
};

function CharacterCard({
  entry,
  now,
  onClose,
  onRename,
}: {
  entry: CastEntry;
  now: number;
  onClose: () => void;
  onRename: (name: string) => Promise<void>;
}) {
  const { c, role } = entry;
  const navigate = useBbNavigate();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(c.name);
  useEffect(() => {
    setDraft(c.name);
    setEditing(false);
  }, [c.slug, c.name]);

  const save = async () => {
    if (!editing) return;
    setEditing(false);
    if (draft.trim() !== c.name) await onRename(draft.trim());
  };
  const onKey = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") void save();
    if (e.key === "Escape") {
      // Escape закрывает только редактирование, а не всю страницу.
      e.preventDefault();
      e.stopPropagation();
      setEditing(false);
      setDraft(c.name);
    }
  };

  return (
    <aside className="tavern-card" aria-label={`Карточка: ${c.name}`}>
      <button type="button" className="tavern-card-close" aria-label="Закрыть" onClick={onClose}>
        ×
      </button>
      <div className="tavern-card-head">
        <span className="tavern-card-sprite" style={{ backgroundImage: `url(${SPRITE_PNG[c.sprite]})` }} />
        <div style={{ minWidth: 0 }}>
          {editing ? (
            <input
              className="tavern-card-input"
              value={draft}
              maxLength={24}
              autoFocus
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onKey}
              onBlur={() => void save()}
              aria-label="Имя персонажа"
              placeholder="пусто — вернуть прежнее"
            />
          ) : (
            <button type="button" className="tavern-card-name" title="Переименовать" onClick={() => setEditing(true)}>
              {c.name} <span className="tavern-dim">✎</span>
            </button>
          )}
          <div className="tavern-card-slug">/{c.slug}</div>
          <div className="tavern-card-role">
            {ROLE_LABEL[role]}
            {c.busy ? " · тред крутится" : ""}
          </div>
        </div>
      </div>
      {c.line ? <p className="tavern-card-line">«{c.line}»</p> : null}
      {c.description ? <p className="tavern-card-desc">{c.description}</p> : null}
      <p className="tavern-card-meta">
        Звали {c.count} {plural(c.count, "раз", "раза", "раз")} за месяц · последний раз {ago(c.lastUsedAt, now)}
      </p>
      {c.threadId ? (
        <button type="button" className="tavern-card-thread" onClick={() => navigate.toThread(c.threadId)}>
          Открыть тред{c.threadTitle ? ` «${c.threadTitle}»` : ""} →
        </button>
      ) : null}
    </aside>
  );
}

/* ─────────────────────────────── страница ─────────────────────────────── */

function Roster({
  title,
  items,
  now,
  selected,
  onPick,
}: {
  title: string;
  items: Character[];
  now: number;
  selected: string | null;
  onPick: (slug: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <section>
      <h2 className="mb-1.5 text-xs font-medium text-muted-foreground">{title}</h2>
      <div className="flex flex-wrap gap-1.5">
        {items.map((c) => (
          <button
            key={c.slug}
            type="button"
            onClick={() => onPick(c.slug)}
            className={`tavern-chip${selected === c.slug ? " is-picked" : ""}`}
            title={`/${c.slug} · ${ago(c.lastUsedAt, now)}`}
          >
            <span className="tavern-chip-sprite" style={{ backgroundImage: `url(${SPRITE_PNG[c.sprite]})` }} />
            <span className="font-medium">{c.name}</span>
            <span className="text-muted-foreground">{ago(c.lastUsedAt, now)}</span>
            {c.busy ? <span className="tavern-chip-busy" aria-label="тред крутится" /> : null}
          </button>
        ))}
      </div>
    </section>
  );
}

/* ─────────────────────────────── телевизор ─────────────────────────────── */

function useTv() {
  const rpc = useRpc<typeof rpcContract>();
  const [tv, setTv] = useState<TvState | null>(null);
  // Ответы могут прийти не по порядку: поздний старый не перетирает свежий.
  const seq = useRef(0);
  const refetch = useCallback(() => {
    const id = ++seq.current;
    rpc.call("tavern_tv", null).then(
      (next) => {
        if (id === seq.current) setTv(next);
      },
      () => undefined,
    );
  }, [rpc]);
  useEffect(() => refetch(), [refetch]);
  useRealtime("tavern-changed", refetch);
  return { rpc, tv, refetch };
}

type Channel = "overview" | "next" | "board";
const CHANNELS: Array<{ id: Channel; label: string }> = [
  { id: "overview", label: "1 · обзор" },
  { id: "next", label: "2 · ждут тебя" },
  { id: "board", label: "3 · табло" },
];
/** Цвет точки «ждут тебя»: красное — сломано, жёлтое — ждёт решения, серое — просто не прочитано. */
const WAIT_DOT: Record<"error" | "pending" | "question" | "unread", string> = {
  error: "is-blocked",
  pending: "is-wip",
  question: "is-wip",
  unread: "is-muted",
};

const STATUS: Record<"done" | "in_progress" | "blocked", { label: string; cls: string }> = {
  done: { label: "готово", cls: "is-done" },
  in_progress: { label: "в работе", cls: "is-wip" },
  blocked: { label: "застряло", cls: "is-blocked" },
};

function Television({ onPick }: { onPick: (slug: string) => void }) {
  const { rpc, tv } = useTv();
  const navigate = useBbNavigate();
  const now = useNow(60_000);
  const [channel, setChannel] = useState<Channel>("overview");
  if (!tv) return null;
  const o = tv.overview;
  const writing = tv.overviewState === "running";
  const [home, away] = tv.teams;

  return (
    <section className="tv" aria-label={`Сводка за ${tv.days} дн`}>
      <div className="tv-screen">
        <div className="tv-top">
          <span className="tv-live">● LIVE</span>
          <span
            className="tv-channel"
            title={tv.overviewAt ? `Обзор ${ago(tv.overviewAt, now)} по ${tv.overviewThreads} тредам` : undefined}
          >
            {tv.days} дн
          </span>
          <button
            type="button"
            className="tv-refresh"
            title="Переписать обзор сейчас (≈1 мин)"
            disabled={writing}
            onClick={() => void rpc.call("tavern_tv_refresh", null)}
          >
            {writing ? "пишем…" : "↻"}
          </button>
        </div>

        <div className="tv-body">
          {channel === "overview" ? (
            o ? (
              <div>
                <p className="tv-summary">{o.summary}</p>
                <ul className="tv-themes">
                  {o.themes.map((t, i) => (
                    <li key={i}>
                      <button
                        type="button"
                        disabled={t.threadIds.length === 0}
                        onClick={() => t.threadIds[0] && navigate.toThread(t.threadIds[0])}
                        title={`${STATUS[t.status].label}: ${t.what}`}
                      >
                        <span className={`tv-dot ${STATUS[t.status].cls}`} aria-label={STATUS[t.status].label} />
                        <span className="tv-theme-title">{t.title}</span>
                        <span className="tv-theme-what">{t.what}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="tv-summary tv-dim">
                {writing
                  ? "Комментатор читает треды за последние дни и пишет обзор…"
                  : tv.overviewState === "failed"
                    ? "Обзор не получился — попробуй ↻ или загляни в лог плагина."
                    : "Обзора ещё нет — нажми ↻, он появится примерно через минуту."}
              </p>
            )
          ) : null}

          {channel === "next" ? (
            tv.waiting.length > 0 ? (
              <ul className="tv-themes">
                {tv.waiting.map((w) => (
                  <li key={w.threadId}>
                    <button type="button" onClick={() => navigate.toThread(w.threadId)} title={w.line || w.title}>
                      <span className={`tv-dot ${WAIT_DOT[w.reason]}`} aria-label={REASON_LABEL[w.reason]} />
                      <span className="tv-theme-title">{w.title}</span>
                      <span className="tv-theme-what">
                        {REASON_LABEL[w.reason]}
                        {w.line ? ` · ${w.line}` : ""}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="tv-summary tv-dim">Агенты ничего от тебя не ждут.</p>
            )
          ) : null}

          {channel === "board" ? (
            <div className="tv-boardgrid">
              <div>
                {home ? (
                  <div className="tv-score">
                    <span className="tv-team">{home.name}</span>
                    <span className="tv-goals">{home.threads}</span>
                    <span className="tv-colon">:</span>
                    <span className="tv-goals">{away?.threads ?? 0}</span>
                    <span className="tv-team">{away?.name ?? "—"}</span>
                  </div>
                ) : (
                  <div className="tv-score tv-dim">тишина в эфире</div>
                )}
                <div className="tv-others">
                  {tv.teams.slice(2, 6).map((t) => (
                    <span key={t.projectId}>
                      {t.name} {t.threads}
                    </span>
                  ))}
                </div>
                <div className="tv-stats">
                  тредов {tv.totalThreads} · вызовов скиллов {tv.totalCalls}
                </div>
              </div>
              <div>
                {tv.mvp ? (
                  <button type="button" className="tv-mvp" onClick={() => onPick(tv.mvp!.slug)} title={`/${tv.mvp.slug}`}>
                    <span className="tv-mvp-sprite" style={{ backgroundImage: `url(${SPRITE_PNG[tv.mvp.sprite]})` }} />
                    <span>
                      <span className="tv-label">игрок периода</span>
                      <span className="tv-mvp-name">
                        {tv.mvp.name} · {tv.mvp.calls} {plural(tv.mvp.calls, "вызов", "вызова", "вызовов")}
                      </span>
                    </span>
                  </button>
                ) : null}
                <div className="tv-top5">
                  {tv.topSkills.slice(1).map((s) => (
                    <span key={s.slug}>
                      {s.name} {s.calls}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          ) : null}
        </div>

      </div>
      <div className="tv-knobs" role="tablist" aria-label="Каналы">
        {CHANNELS.map((c) => (
          <button
            key={c.id}
            type="button"
            role="tab"
            aria-selected={channel === c.id}
            className={channel === c.id ? "is-on" : ""}
            onClick={() => setChannel(c.id)}
          >
            {c.label}
          </button>
        ))}
      </div>
    </section>
  );
}

function TavernPage() {
  const { rpc, state, error, refetch } = useTavern();
  const now = useNow(1000);
  const p = usePreview();
  const at = previewTime(now, p);
  const [selected, setSelected] = useState<string | null>(null);

  const cast: CastEntry[] = useMemo(() => {
    if (!state) return [];
    return [
      ...(state.innkeeper ? [{ c: state.innkeeper, role: "innkeeper" as const }] : []),
      ...state.working.map((c) => ({ c, role: "working" as const })),
      ...state.resting.map((c) => ({ c, role: "resting" as const })),
    ];
  }, [state]);
  const picked = cast.find((e) => e.c.slug === selected) ?? null;
  const pick = useCallback((slug: string) => setSelected((cur) => (cur === slug ? null : slug)), []);

  const rename = async (name: string) => {
    if (!picked) return;
    await rpc.call("tavern_rename", { slug: picked.c.slug, name });
    refetch();
  };

  return (
    <div className="h-full min-h-0 flex-1 overflow-y-auto">
      <style>{CSS}</style>
      <div className="mx-auto box-border w-full max-w-5xl px-4 pb-6 pt-3 md:px-5 md:pt-4">
        {error ? (
          <p role="alert" className="mb-3 text-sm text-destructive">
            {error}
          </p>
        ) : null}
        <Television onPick={pick} />
        <div className="tavern-frame">
          {state === null ? (
            <div className="tavern-loading">Открываем таверну…</div>
          ) : (
            <Scene cast={cast} at={at} selected={selected} onPick={pick} />
          )}
          <Clock at={at} isPreview={p !== null} />
          {picked ? (
            <CharacterCard entry={picked} now={now} onClose={() => setSelected(null)} onRename={rename} />
          ) : null}
        </div>

        {state ? (
          <div className="mt-4 space-y-3 text-sm">
            <Roster
              title={`Работают · за ${state.activeWindowHours} ч`}
              items={state.working}
              now={now}
              selected={selected}
              onPick={pick}
            />
            <Roster title="Отдыхают" items={state.resting} now={now} selected={selected} onPick={pick} />
            {state.working.length === 0 && state.resting.length === 0 ? (
              <p className="text-muted-foreground">
                {state.scannedAt === null
                  ? "Таверна читает историю сессий — первый проход занимает до пары минут."
                  : "В сессиях пока не звали ни одного скилла."}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function PreviewButton() {
  const p = usePreview();
  const next = PREVIEW_ORDER[(PREVIEW_ORDER.indexOf(p) + 1) % PREVIEW_ORDER.length]!;
  return (
    <button
      type="button"
      className="rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
      title="Посмотреть таверну в другое время суток"
      onClick={() => setPreview(next)}
    >
      {p === null ? "Время: сейчас" : `Превью: ${PREVIEW_LABEL[p]}`}
    </button>
  );
}

/* Стили сцены свои: пиксельная графика и свет живут вне темы bb. Тёплые
   цвета — те же, что у таверны на сайте (#ffd9a8, #ffb347). */
const CSS = `
.tavern-frame { position: relative; border-radius: 10px; overflow: hidden; background: #1a120b; }
.tavern-loading { aspect-ratio: 416 / 320; display: grid; place-items: center; color: #ffd9a8; font: 12px ui-monospace, monospace; }
.tavern-scene {
  position: relative; width: 100%; aspect-ratio: 416 / 320;
  background-size: 100% 100%; background-repeat: no-repeat; image-rendering: pixelated;
}
.tavern-layer { position: absolute; inset: 0; pointer-events: none; z-index: 1; transition: background 1.2s linear; }
.tavern-glow {
  position: absolute; pointer-events: none; z-index: 2; border-radius: 50%;
  background: radial-gradient(circle, rgba(255, 196, 120, 0.75) 0%, rgba(255, 150, 60, 0.28) 38%, rgba(255, 120, 40, 0) 70%);
  mix-blend-mode: screen; animation: tavern-flicker 2.6s ease-in-out infinite;
}
.tavern-flame {
  position: absolute; pointer-events: none; z-index: 3; border-radius: 1px;
  background: linear-gradient(#fff2b0, #ff9a2e 60%, #e0461b);
  box-shadow: 0 0 6px 2px rgba(255, 170, 70, 0.8);
  animation: tavern-flame 0.9s steps(3) infinite;
}
@keyframes tavern-flicker { 0%,100% { transform: scale(1); filter: brightness(1); } 40% { transform: scale(1.04); filter: brightness(1.12); } 70% { transform: scale(0.97); filter: brightness(0.92); } }
@keyframes tavern-flame { 0% { transform: scaleY(1); } 33% { transform: scaleY(1.35) translateY(-10%); } 66% { transform: scaleY(0.85); } }
.tavern-zone-label {
  position: absolute; z-index: 4; pointer-events: none;
  font: 10px ui-monospace, 'SF Mono', Monaco, monospace; letter-spacing: 0.08em;
  color: rgba(255, 217, 168, 0.8); text-shadow: 0 1px 0 rgba(0,0,0,0.6);
}
.tavern-unit {
  position: absolute; z-index: 5; padding: 0; border: 0; background: transparent; cursor: pointer;
  left: calc(var(--x) / var(--map-w) * 100%); top: calc(var(--y) / var(--map-h) * 100%);
  width: calc(var(--s) * var(--k, 1) / var(--map-w) * 100%); aspect-ratio: 1; transform: translate(-50%, -100%);
}
.tavern-unit::before {
  content: ''; position: absolute; left: 50%; bottom: 4%; transform: translateX(-50%);
  width: 52%; height: 11%; border-radius: 50%; background: rgba(6, 4, 2, 0.45); pointer-events: none;
}
.tavern-body {
  display: block; width: 100%; height: 100%; background-size: 400% 400%; background-position: 0 0;
  image-rendering: pixelated; transform-origin: 50% 100%;
}
.tavern-tag {
  position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%); margin-bottom: 2px;
  font: 600 10px ui-monospace, 'SF Mono', Monaco, monospace; white-space: nowrap;
  background: #ffd9a8; color: #1a1208; border-radius: 3px; padding: 1px 4px;
  opacity: 0; transition: opacity 0.15s; pointer-events: none;
}
.tavern-unit:hover .tavern-tag, .tavern-unit:focus-visible .tavern-tag, .tavern-unit.is-picked .tavern-tag { opacity: 1; }
.tavern-unit.is-picked .tavern-tag { background: #ffb347; }
.tavern-unit.is-picked .tavern-body, .tavern-unit:hover .tavern-body { filter: drop-shadow(0 0 5px #ffd9a8); }
.tavern-unit:focus-visible { outline: 1px solid #ffd9a8; outline-offset: 1px; }
.tavern-bubble {
  position: absolute; right: -18%; top: -22%; pointer-events: none;
  font: 700 9px ui-monospace, monospace; line-height: 1; padding: 1px 3px; border-radius: 3px;
}
.tavern-busy { background: #ffb347; color: #1a1208; animation: tavern-bob 0.8s ease-in-out infinite alternate; }
.tavern-zzz { color: #cfe3ff; animation: tavern-zzz 2.4s ease-in-out infinite; }
@keyframes tavern-bob { from { transform: translateY(0); } to { transform: translateY(-25%); } }
@keyframes tavern-zzz { 0% { opacity: 0; transform: translate(0, 0); } 30% { opacity: 1; } 100% { opacity: 0; transform: translate(40%, -120%); } }
.tavern-clock {
  position: absolute; z-index: 6; top: 8px; right: 8px; display: flex; gap: 6px; align-items: center;
  padding: 5px 8px 5px 5px; border-radius: 8px; background: rgba(26, 18, 11, 0.82);
  border: 1px solid rgba(255, 217, 168, 0.25); color: #ffd9a8; font: 11px ui-monospace, 'SF Mono', Monaco, monospace;
}
.tavern-clock-time { font-size: 13px; font-weight: 600; }
.tavern-clock-sun { opacity: 0.85; }
.tavern-clock-phase { opacity: 0.7; font-size: 10px; letter-spacing: 0.06em; }
.tavern-dim { opacity: 0.6; font-weight: 400; }
.tavern-card {
  position: absolute; z-index: 7; left: 8px; bottom: 8px; width: min(320px, calc(100% - 16px));
  max-height: calc(100% - 16px); overflow-y: auto; box-sizing: border-box;
  padding: 10px 12px; border-radius: 10px; background: rgba(26, 18, 11, 0.94);
  border: 1px solid rgba(255, 217, 168, 0.35); color: #f6e7d2; font-size: 12px; line-height: 1.45;
}
.tavern-card-close { position: absolute; top: 4px; right: 8px; border: 0; background: none; color: #ffd9a8; font-size: 18px; cursor: pointer; }
.tavern-card-head { display: flex; gap: 10px; align-items: center; padding-right: 16px; }
.tavern-card-sprite { flex: none; width: 48px; height: 48px; background-size: 400% 400%; background-position: 0 0; image-rendering: pixelated; }
.tavern-card-name { border: 0; background: none; padding: 0; color: #ffd9a8; font-weight: 700; font-size: 15px; cursor: text; text-align: left; }
.tavern-card-input { width: 100%; box-sizing: border-box; border-radius: 4px; border: 1px solid #ffb347; background: #120c07; color: #ffd9a8; font-weight: 700; font-size: 14px; padding: 1px 4px; }
.tavern-card-slug { font-family: ui-monospace, monospace; color: #ffb347; font-size: 11px; }
.tavern-card-role { opacity: 0.7; font-size: 11px; }
.tavern-card-line { margin: 8px 0 0; font-style: italic; color: #ffd9a8; }
.tavern-card-desc { margin: 6px 0 0; opacity: 0.85; display: -webkit-box; -webkit-line-clamp: 5; -webkit-box-orient: vertical; overflow: hidden; }
.tavern-card-meta { margin: 6px 0 0; opacity: 0.7; font-size: 11px; }
.tavern-card-thread {
  margin-top: 8px; width: 100%; text-align: left; border-radius: 6px; padding: 5px 8px; cursor: pointer;
  border: 1px solid rgba(255, 179, 71, 0.6); background: rgba(255, 179, 71, 0.12); color: #ffd9a8; font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tavern-card-thread:hover { background: rgba(255, 179, 71, 0.25); }
.tavern-chip {
  display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px 2px 3px; border-radius: 999px;
  border: 1px solid rgba(127, 127, 127, 0.3); background: transparent; cursor: pointer; font-size: 12px; color: inherit;
}
.tavern-chip:hover, .tavern-chip.is-picked { border-color: #ffb347; }
.tavern-chip-sprite { width: 20px; height: 20px; background-size: 400% 400%; background-position: 0 0; image-rendering: pixelated; }
.tavern-chip-busy { width: 7px; height: 7px; border-radius: 50%; background: #ffb347; }
.tv {
  position: relative; margin: 14px 0 14px; padding: 8px; border-radius: 12px;
  background: linear-gradient(#4a2e18, #321e10); border: 2px solid #1a0f07;
  box-shadow: inset 0 0 0 2px rgba(255, 217, 168, 0.12), 0 6px 0 #1a0f07;
}
.tv::before, .tv::after {
  content: ''; position: absolute; top: -14px; width: 2px; height: 16px; background: #6b4a2e;
}
.tv::before { left: 46%; transform: rotate(-24deg); }
.tv::after { left: 54%; transform: rotate(24deg); }
.tv-screen {
  position: relative; overflow: hidden; border-radius: 8px; padding: 8px 12px 0;
  background: radial-gradient(ellipse at center, #17301f 0%, #0b1a11 75%, #07120b 100%);
  color: #d9f5d0; font: 12px ui-monospace, 'SF Mono', Monaco, monospace;
  box-shadow: inset 0 0 24px rgba(0, 0, 0, 0.7);
}
.tv-screen::after {
  content: ''; position: absolute; inset: 0; pointer-events: none;
  background: repeating-linear-gradient(to bottom, rgba(0,0,0,0.18) 0 1px, transparent 1px 3px);
}
.tv-top { display: flex; align-items: center; gap: 10px; font-size: 10px; letter-spacing: 0.08em; opacity: 0.9; }
.tv-live { color: #ff5a4a; font-weight: 700; animation: tv-blink 1.6s steps(2) infinite; }
@keyframes tv-blink { 50% { opacity: 0.35; } }
.tv-channel { flex: 1; color: #9fd89a; }
.tv-refresh { position: relative; z-index: 1; border: 0; background: none; color: #9fd89a; cursor: pointer; font-size: 13px; }
.tv-refresh:disabled { opacity: 0.4; cursor: default; }
.tv-body { padding: 6px 0 10px; position: relative; z-index: 1; }
.tv-boardgrid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
@media (max-width: 640px) { .tv-boardgrid { grid-template-columns: 1fr; } }
.tv-summary { margin: 0 0 6px; font: 600 13px/1.4 system-ui, sans-serif; color: #fff4c2; }
.tv-themes, .tv-next { list-style: none; margin: 0; padding: 0; display: grid; gap: 1px; }
.tv-themes button, .tv-next button {
  width: 100%; display: flex; align-items: center; gap: 8px; min-width: 0; text-align: left;
  border: 0; background: none; color: inherit; border-radius: 4px; padding: 3px 4px; cursor: pointer;
  font: 12px/1.4 system-ui, sans-serif;
}
.tv-themes button:disabled, .tv-next button:disabled { cursor: default; }
.tv-themes button:not(:disabled):hover, .tv-next button:not(:disabled):hover { background: rgba(159, 216, 154, 0.1); }
.tv-dot { flex: none; width: 7px; height: 7px; border-radius: 50%; }
.tv-dot.is-done { background: #6fcf7a; }
.tv-dot.is-wip { background: #e8b04a; }
.tv-dot.is-blocked { background: #ff6a5a; }
.tv-dot.is-muted { background: rgba(159, 216, 154, 0.45); }
.tv-theme-title { flex: none; font-weight: 600; color: #ffd9a8; }
.tv-theme-what, .tv-next-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tv-theme-what { opacity: 0.7; }
.tv-next { counter-reset: tvn; }
.tv-next li { counter-increment: tvn; }
.tv-next-text { color: #fff4c2; }
.tv-next-text::before { content: counter(tvn) ". "; color: #9fd89a; }
.tv-next-go { margin-left: auto; flex: none; color: #9fd89a; }
.tv-top5 { display: flex; flex-wrap: wrap; gap: 4px 10px; margin-top: 6px; font-size: 10px; opacity: 0.75; }
.tv-knobs { display: flex; gap: 6px; justify-content: center; padding-top: 7px; }
.tv-knobs button { border: 1px solid #1a0f07; background: linear-gradient(#6b4a2e, #4a2e18); color: #ffd9a8; border-radius: 4px; padding: 2px 10px; font: 600 11px ui-monospace, monospace; cursor: pointer; box-shadow: 0 2px 0 #1a0f07; }
.tv-knobs button.is-on { background: linear-gradient(#ffb347, #e8902a); color: #1a1208; transform: translateY(1px); box-shadow: 0 1px 0 #1a0f07; }
.tv-score { display: flex; align-items: baseline; gap: 8px; font-weight: 700; }
.tv-team { text-transform: uppercase; font-size: 12px; letter-spacing: 0.06em; color: #ffd9a8; max-width: 9em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tv-goals { font-size: 26px; color: #fff4c2; text-shadow: 0 0 8px rgba(255, 220, 120, 0.5); }
.tv-colon { font-size: 22px; color: #fff4c2; opacity: 0.7; }
.tv-others { display: flex; gap: 10px; flex-wrap: wrap; font-size: 10px; opacity: 0.75; text-transform: uppercase; }
.tv-stats { margin-top: 2px; font-size: 10px; opacity: 0.7; }
.tv-dim { opacity: 0.6; }
.tv-mvp { position: relative; z-index: 1; display: flex; gap: 8px; align-items: center; margin-top: 8px; padding: 0; border: 0; background: none; color: inherit; cursor: pointer; text-align: left; font: inherit; }
.tv-mvp-sprite { flex: none; width: 32px; height: 32px; background-size: 400% 400%; background-position: 0 0; image-rendering: pixelated; animation: tv-bob 1.2s steps(2) infinite; }
@keyframes tv-bob { 50% { transform: translateY(-2px); } }
.tv-label { display: block; font-size: 9px; letter-spacing: 0.12em; text-transform: uppercase; color: #9fd89a; opacity: 0.85; }
.tv-mvp-name { color: #ffd9a8; font-weight: 700; }
/* Телефон: сцена ужимается до ~360 px, поэтому персонажи крупнее, часы —
   одной строкой в углу, подписи зон убраны, итог темы уходит на вторую строку. */
@media (max-width: 640px) {
  .tavern-unit { --k: 1.45; }
  .tavern-zone-label { display: none; }
  .tavern-clock { top: 4px; right: 4px; padding: 2px 6px; gap: 0; font-size: 10px; border-radius: 6px; }
  .tavern-clock svg, .tavern-clock-phase { display: none; }
  .tavern-clock-time { font-size: 11px; }
  .tavern-clock-sun { font-size: 9px; }
  .tavern-tag { font-size: 9px; }
  .tv { padding: 6px; margin-top: 10px; }
  .tv-screen { padding: 6px 8px 0; }
  .tv-themes button { flex-wrap: wrap; row-gap: 0; }
  .tv-theme-what { flex-basis: 100%; padding-left: 15px; }
  .tv-knobs button { padding: 2px 7px; font-size: 10px; }
  .tavern-card { width: calc(100% - 8px); left: 4px; bottom: 4px; }
}
@media (prefers-reduced-motion: reduce) { .tavern-glow, .tavern-flame, .tavern-busy, .tavern-zzz, .tv-live, .tv-mvp-sprite { animation: none; } }
`;

export default definePluginApp((app) => {
  app.slots.navPanel({
    id: "tavern",
    title: "Таверна",
    icon: "tavern/mug",
    path: "tavern",
    component: TavernPage,
    headerContent: PreviewButton,
  });
});
