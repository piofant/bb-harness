// bb-plugin-winx — страница «Фея дня» в левой панели bb.
import { useCallback, useEffect, useRef, useState } from "react";
import { definePluginApp, useRealtime, useRpc } from "@get-bb/plugin-sdk/app";
import type { FairyState, rpcContract } from "./server";
import { FAIRIES, type FairyId } from "./lib/fairies";
import { FAIRY_ART } from "./lib/art.generated";

function useFairy() {
  const rpc = useRpc<typeof rpcContract>();
  const [state, setState] = useState<FairyState | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Поздний старый ответ не перетирает свежий.
  const seq = useRef(0);
  const refetch = useCallback(() => {
    const id = ++seq.current;
    rpc.call("fairy_state", null).then(
      (next) => {
        if (id !== seq.current) return;
        setState(next);
        setError(null);
      },
      (cause: unknown) => setError(cause instanceof Error ? cause.message : String(cause)),
    );
  }, [rpc]);
  useEffect(() => refetch(), [refetch]);
  useRealtime("winx-changed", refetch);
  const reroll = useCallback(() => {
    rpc.call("fairy_reroll", null).catch((cause: unknown) => setError(cause instanceof Error ? cause.message : String(cause)));
  }, [rpc]);
  return { state, error, reroll };
}

const WEEKDAY = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
const weekday = (day: string) => WEEKDAY[new Date(`${day}T12:00:00+03:00`).getUTCDay()]!;

function FairyPage() {
  const { state, error, reroll } = useFairy();
  const today = state?.today ?? null;
  const v = today?.verdict ?? null;
  const fairy = v ? FAIRIES[v.fairy] : null;
  const running = state?.status === "running";

  return (
    <div className="h-full min-h-0 flex-1 overflow-y-auto">
      <style>{CSS}</style>
      <div className="mx-auto box-border w-full max-w-2xl px-4 pb-6 pt-4 md:px-5">
        {error ? (
          <p role="alert" className="mb-3 text-sm text-destructive">
            {error}
          </p>
        ) : null}

        {!v || !fairy ? (
          <div className="winx-card winx-wait">
            <div className="winx-sparkle">✦</div>
            <p>
              {state?.status === "failed"
                ? "Оракул задумался и не ответил. Нажми «Пересчитать» чуть позже."
                : "Оракул смотрит, что было сегодня…"}
            </p>
            {state?.status === "failed" ? (
              <button type="button" className="winx-retry" onClick={reroll}>
                Пересчитать
              </button>
            ) : null}
          </div>
        ) : (
          <article className="winx-card" style={{ "--fairy": fairy.color } as React.CSSProperties}>
            <img className="winx-art" src={FAIRY_ART[fairy.id]} alt={`Фея ${fairy.name}`} />
            <div className="winx-text">
              <div className="winx-kicker">сегодня ты</div>
              <h1 className="winx-name">{fairy.name}</h1>
              <div className="winx-power">{fairy.power}</div>
              <p className="winx-why">{v.why}</p>
              {v.spell ? <p className="winx-spell">✦ {v.spell}</p> : null}
              {v.mix.length > 1 ? (
                <div className="winx-mix" aria-label="Из кого состоит день">
                  <div className="winx-bar">
                    {v.mix.map((m) => (
                      <span key={m.fairy} style={{ width: `${m.pct}%`, background: FAIRIES[m.fairy].color }} />
                    ))}
                  </div>
                  <div className="winx-legend">
                    {v.mix.map((m) => (
                      <span key={m.fairy}>
                        <i style={{ background: FAIRIES[m.fairy].color }} />
                        {FAIRIES[m.fairy].name} {m.pct}%
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}
              <div className="winx-foot">
                <span>
                  {v.source === "day" ? `по ${today!.threads} сессиям сегодня` : "сегодня в bb тихо — по звёздам"}
                </span>
                <button type="button" disabled={running} onClick={reroll}>
                  {running ? "колдуем…" : state?.status === "failed" ? "Не вышло, ещё раз" : "Пересчитать"}
                </button>
              </div>
            </div>
          </article>
        )}

        {state && state.week.length > 1 ? (
          <section className="winx-week" aria-label="Неделя фей">
            {state.week.map((w) => (
              <div key={w.day} className={w.day === today?.day ? "is-today" : ""} title={`${w.day}: ${FAIRIES[w.fairy as FairyId].name}`}>
                <img src={FAIRY_ART[w.fairy]} alt="" />
                <span>{weekday(w.day)}</span>
              </div>
            ))}
          </section>
        ) : null}
      </div>
    </div>
  );
}

const CSS = `
.winx-card {
  display: grid; grid-template-columns: 220px 1fr; gap: 20px; align-items: center;
  padding: 18px; border-radius: 18px;
  background: linear-gradient(135deg, color-mix(in srgb, var(--fairy, #c77dff) 22%, transparent), color-mix(in srgb, var(--fairy, #c77dff) 6%, transparent));
  border: 1px solid color-mix(in srgb, var(--fairy, #c77dff) 45%, transparent);
}
.winx-wait { display: block; text-align: center; padding: 48px 18px; color: var(--muted-foreground, #999); }
.winx-sparkle { font-size: 34px; color: #c77dff; animation: winx-spin 2.4s linear infinite; display: inline-block; }
@keyframes winx-spin { to { transform: rotate(360deg); } }
.winx-art { width: 100%; border-radius: 14px; image-rendering: pixelated; animation: winx-float 3.2s ease-in-out infinite; }
@keyframes winx-float { 50% { transform: translateY(-6px); } }
.winx-kicker { font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; opacity: 0.6; }
.winx-name { margin: 2px 0 0; font-size: 34px; line-height: 1.1; font-weight: 800; color: var(--fairy); }
.winx-power { font-size: 13px; opacity: 0.75; }
.winx-why { margin: 12px 0 0; font-size: 15px; line-height: 1.45; }
.winx-spell { margin: 8px 0 0; font-size: 13px; font-style: italic; color: var(--fairy); }
.winx-mix { margin-top: 14px; }
.winx-bar { display: flex; height: 8px; border-radius: 999px; overflow: hidden; background: rgba(127,127,127,0.2); }
.winx-legend { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 6px; font-size: 12px; opacity: 0.8; }
.winx-legend i { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; }
.winx-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 16px; font-size: 12px; opacity: 0.85; }
.winx-foot button {
  border: 1px solid color-mix(in srgb, var(--fairy) 60%, transparent); background: none; color: inherit;
  border-radius: 999px; padding: 4px 12px; font-size: 12px; cursor: pointer;
}
.winx-retry { margin-top: 10px; border: 1px solid rgba(199,125,255,0.6); background: none; color: inherit; border-radius: 999px; padding: 4px 14px; cursor: pointer; }
.winx-foot button:disabled { opacity: 0.5; cursor: default; }
.winx-week { display: flex; gap: 8px; justify-content: center; margin-top: 16px; }
.winx-week div { display: grid; justify-items: center; gap: 2px; font-size: 11px; opacity: 0.6; }
.winx-week div.is-today { opacity: 1; font-weight: 700; }
.winx-week img { width: 40px; border-radius: 8px; }
@media (max-width: 560px) {
  .winx-card { grid-template-columns: 1fr; justify-items: center; text-align: center; }
  .winx-art { width: 200px; }
  .winx-bar { margin: 0 auto; }
  .winx-legend, .winx-foot { justify-content: center; }
  .winx-foot { flex-direction: column; }
}
@media (prefers-reduced-motion: reduce) { .winx-art, .winx-sparkle { animation: none; } }
`;

export default definePluginApp((app) => {
  app.slots.navPanel({
    id: "fairy",
    title: "Фея дня",
    icon: "winx/wand",
    path: "fairy",
    component: FairyPage,
  });
});
