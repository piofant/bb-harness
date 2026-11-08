"""Оркестратор `kb consolidate`: нарезка → стадия 1 → проверка цитат → стадия 2 → гейты → слияние."""
import concurrent.futures as cf
import datetime as dt
import json
import hashlib
import os
import re
import sys
import tempfile
import threading
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from kbcore import (AREAS, WORK_AREAS, LIMIT_CARD_BYTES, LIMIT_HUB_LINES, LockTimeout, as_list, free_gb,  # noqa: E402
                    iter_cards, log_run, new_run_id, parse_frontmatter, repo_lock)

from . import apply as A  # noqa: E402
from . import gates as G  # noqa: E402
from . import atoms as AT  # noqa: E402
from . import gitops, llm, stage1, stage2  # noqa: E402
from .areas import select_units  # noqa: E402
from .ledger import Ledger  # noqa: E402
from .units import collapse, iter_units  # noqa: E402

MODEL_S1 = os.environ.get("KB_S1_MODEL", "sonnet")
MODEL_S2 = os.environ.get("KB_S2_MODEL", "opus")
S1_TIMEOUT = 1800
CTX_ALL_BYTES = 60_000     # область меньше — показываем все карточки целиком
CTX_MAX_BYTES = 110_000    # иначе — core + карточки тех же единиц + тех же тем, до этого объёма
CTX_CLAIMS_PER_CARD = 40
# KB_S2_CTX=trimmed_index: карточки целиком — как обычно; индекс области — KB_S2_CTX_INDEX (60) похожих строк + хаб;
# без списка всех занятых имён (уникальность проверяет apply, отказ «имя занято» называет занявшую карточку)
CTX_MODE = os.environ.get("KB_S2_CTX", "full")
CTX_MODE = CTX_MODE if CTX_MODE in ("full", "trimmed_index") else "full"   # опечатка — прежний режим
CTX_INDEX_LINES = os.environ.get("KB_S2_CTX_INDEX", "60")
CTX_INDEX_LINES = max(1, int(CTX_INDEX_LINES)) if re.fullmatch(r"-?\d+", CTX_INDEX_LINES.strip()) else 60
HUB_MIN_CARDS = {"projects": 5}   # хаб маленького проекта не нужен
HUB_LLM_CHURN = 10          # новых и снятых карточек с прошлой пересборки хаба LLM — пора перегруппировать
HUB_LLM_ERROR_RETRY_H = 12  # после системного сбоя LLM на хабе (тайм-аут, авторизация) — повтор не раньше
HUB_LLM_COOLDOWN_DAYS = 7   # после устойчивого отказа LLM на хабе пересборки «по желанию» ждут столько дней
HUB_LLM_LINES, HUB_LLM_BYTES = 31, 3200   # бюджет тела хаба от LLM: остальное до 38 строк — новым карточкам
S2_RETRY_ROUNDS = 1         # отклонённое гейтами уходит на повтор в этом же прогоне
DEFAULT_AREAS = list(WORK_AREAS)

# Пилотный режим (--pilot): маршруты стадии 2 (утверждение про тему уходит в область, где живёт её карточка),
# подсказки обязательных карточек по областям и имена обязательных карточек. Заполняются под свою базу:
# ROUTES = [(re.compile(r"<тема>", re.I), "<область>", ("<откуда>", …)), …]
# HINTS = {"<область>": "- `<имя>` (process, dir <область>, core): что должно быть в карточке; keys: …"}
ROUTES = []
HINTS = {}
CORE = set()


def hub_name(area):
    return area.replace("/", "-") + "-hub"


class Run:
    def __init__(self, cfg, areas, max_batches, dry_run=False, parallel=4, batch_size=18, no_merge=False,
                 max_conflicts=10, pilot=False, stages="12", out=print, min_units=0):
        self.cfg = cfg
        self.areas = areas
        self.max_batches = max_batches
        self.dry = dry_run
        self.parallel = parallel
        self.batch_size = batch_size
        self.no_merge = no_merge
        self.max_conflicts = max_conflicts
        self.pilot = pilot
        self.stages = stages
        self.out = out
        # порог: меньше min_units единиц сырья к LLM — прогон откладывается без вызовов LLM (kb daily: 3)
        self.min_units = max(0, int(min_units or 0))
        self.req_projects = "projects" in areas
        self.deferred = None
        self.branch = None          # ветка прогона: к ней привязаны ожидающие базы хабов
        self.hub_bases = set()      # области, чьи базы хабов записаны в этом прогоне (подтвердить после слияния)
        self.no_units = False      # порог включён, а к LLM нет ни одной единицы: прогон без работы, не «свежий К1»
        # KB_RUN_ID — run_id от вызывающего (kb weekly ищет итог сводчика в журнале по нему)
        self.run_id = re.sub(r"[^0-9a-z]", "", os.environ.get("KB_RUN_ID", ""))[:24] or new_run_id()
        self.stats = {}
        self.errors = []
        self.lock = threading.Lock()
        self.limit_hit = False
        self.tokens = {"in": 0, "out": 0, "cost_usd": 0.0}
        self.llm_calls = 0
        self.conflicts = []
        self.touched = set()
        self.distinct = set()     # вердикты «разные» этого прогона: тексты могут поменяться позже (unlink_missing)
        self.rejected = []
        self.atom_cov = []
        self.partial_uids = []     # единицы, извлечённые частично (оборванный ответ на одной единице)

    def bump(self, k, n=1):
        self.stats[k] = self.stats.get(k, 0) + n

    def _tok(self, r):
        with self.lock:
            self.llm_calls += 1
            self.tokens["in"] += r.get("tokens_in") or 0
            self.tokens["out"] += r.get("tokens_out") or 0
            self.tokens["cost_usd"] += r.get("cost_usd") or 0.0

    # ---------- стадия 1 ----------

    def s1_todo(self, ledger, units):
        """Единицы, которые стадия 1 отправила бы в LLM (без лимита батчей)."""
        todo = [u for u in units if ledger.needs_extraction(u, stage1.PROMPT_VER)]
        if self.pilot:
            live = [u for u in todo if u.kind == "live"]
            sel = select_units([u for u in todo if u.kind != "live"], self.areas)
            todo = live + sel
        return todo

    def units_to_llm(self, ledger, units):
        """Порог смотрит на то, что реально пошло бы в LLM: единицы к извлечению (стадия 1) и единицы,
        чьи утверждения ждут сведения (стадия 2: новые и повторы отклонённого, attempts < MAX). → множество uid."""
        out = {u.uid for u in self.s1_todo(ledger, units)} if "1" in self.stages else set()
        if "2" in self.stages:
            out |= {c.get("uid") or c.get("src") for c in ledger.pending_claims(expand_areas(self.areas, ledger))}
        return out

    def stage1(self, ledger, units):
        todo = self.s1_todo(ledger, units)
        batches = stage1.make_batches(todo, size=self.batch_size)
        self.stats["s1_units_pending"] = len(todo)
        self.stats["s1_batches_pending"] = len(batches)
        batches = batches[:self.max_batches] if self.max_batches is not None else batches
        self.stats["s1_batches_run"] = len(batches)
        if self.dry or not batches:
            return
        state_dir = self.cfg.state

        def work(i, b):
            t = time.time()
            r = llm.run(stage1.build_prompt(b), MODEL_S1, timeout=S1_TIMEOUT, cwd=state_dir)
            return i, b, r, round(time.time() - t, 2)

        with cf.ThreadPoolExecutor(max_workers=self.parallel) as ex:
            futs = {}
            it = iter(enumerate(batches))
            for _ in range(self.parallel):
                nxt = next(it, None)
                if nxt:
                    futs[ex.submit(work, *nxt)] = nxt
            while futs:
                done, _ = cf.wait(futs, return_when=cf.FIRST_COMPLETED)
                for f in done:
                    i, b = futs.pop(f)
                    t0 = time.time()
                    rec = {"cmd": "consolidate_batch", "run_id": self.run_id, "stage": 1, "batch": i,
                           "units": len(b), "model": MODEL_S1}
                    try:
                        _, _, r, dur = f.result()
                    except llm.LimitHit as e:
                        self.limit_hit = True
                        log_run(self.cfg, **rec, status="limit", error=str(e)[:200],
                                duration_s=round(time.time() - t0, 2))
                        continue
                    except llm.LLMError as e:
                        self.errors.append(f"s1 batch {i}: {e}")
                        log_run(self.cfg, **rec, status="error", error=str(e)[:200], duration_s=0)
                        continue
                    self._tok(r)
                    parsed = stage1.parse_output(r["result"])
                    truncated = r.get("stop_reason") == "max_tokens" or stage1.looks_truncated(r["result"])
                    if truncated:
                        # оборванный ответ: целиком разобраны все встреченные единицы, кроме последней;
                        # остальные не помечаем извлечёнными — повтор в следующий раз. Если не спасти ничего,
                        # а оборвана одна огромная единица, — берём её частичное извлечение, иначе она
                        # переизвлекалась бы каждый прогон без конца.
                        keep = stage1.complete_units(parsed, b)
                        seen = stage1.seen_units(parsed, b)
                        if not keep and len(seen) == 1:
                            keep = set(seen)
                            self.bump("s1_partial_units")
                            self.partial_uids.append(seen[0])
                        b_ok = [u for u in b if u.uid in keep]
                        self.errors.append(f"s1 batch {i}: ответ оборван, спасено единиц {len(b_ok)}/{len(b)}")
                        self.bump("s1_truncated")
                        # утверждения неспасённых единиц не привязываем по цитате к спасённым
                        drop = {u.uid for u in b} - keep
                        parsed = [x for x in parsed if str(x.get("unit") or "").strip() not in drop]
                    else:
                        b_ok = list(b)
                    by_unit, orphans = stage1.assign(parsed, b_ok)
                    n_claims = n_drop = n_sens = under = 0
                    cov = []
                    for u in b_ok:
                        cl = by_unit.get(u.uid, [])
                        n_drop += ledger.add_extraction(u, stage1.PROMPT_VER, cl, self.run_id)
                        n_claims += len(cl)
                        n_sens += sum(1 for c in cl if c["sensitive"])
                        ok_cl = [c for c in cl if c["quote_ok"]]
                        if len(ok_cl) < AT.target_claims(u.text):
                            under += 1
                        a = AT.atom_coverage(u.text, ok_cl)
                        if a is not None:
                            cov.append(a)
                    self.bump("s1_batches_ok" if not truncated else "s1_batches_salvaged")
                    self.bump("s1_units", len(b_ok))
                    self.bump("s1_claims", n_claims)
                    self.bump("s1_dropped_quote", n_drop)
                    self.bump("s1_sensitive", n_sens)
                    self.bump("s1_orphans", orphans)
                    self.bump("s1_under_norm", under)
                    self.bump("s1_target", sum(AT.target_claims(u.text) for u in b_ok))
                    with self.lock:
                        self.atom_cov += cov
                    atom_cov = round(sum(cov) / len(cov), 3) if cov else None
                    if truncated:
                        rec["partial_uids"] = [u.uid for u in b_ok if u.uid in self.partial_uids]
                    log_run(self.cfg, **rec, status="truncated" if truncated else "ok", units_ok=len(b_ok),
                            claims=n_claims, dropped_quote=n_drop, sensitive=n_sens, orphans=orphans,
                            under_norm=under, atom_coverage=atom_cov, prompt_ver=stage1.PROMPT_VER,
                            tokens_in=r["tokens_in"], tokens_out=r["tokens_out"], cost_usd=r.get("cost_usd"),
                            duration_s=dur)
                    self.out(f"  стадия 1, батч {i}: {len(b_ok)}/{len(b)} ед. → {n_claims} утв., без цитаты {n_drop}, "
                             f"sensitive {n_sens}, ниже нормы {under}, атомы {atom_cov} ({dur} с)")
                if not self.limit_hit:
                    for _ in range(len(done)):
                        nxt = next(it, None)
                        if nxt:
                            futs[ex.submit(work, *nxt)] = nxt

    # ---------- стадия 2 ----------

    def route(self, ledger):
        moved = 0
        if not self.pilot:
            self.stats["s2_routed"] = 0
            return
        for rx, target, from_areas in ROUTES:
            if target not in self.areas:
                continue
            for c in ledger.pending_claims(list(from_areas)):
                if rx.search(c["text"] + " " + c["quote"]):
                    ledger.db.execute("UPDATE claims SET area=? WHERE id=?", (target, c["id"]))
                    moved += 1
        ledger.db.commit()
        self.stats["s2_routed"] = moved

    def _area_context(self, cfg_wt, ledger, area, chunk):
        cs = [c for c in iter_cards(cfg_wt) if c.area == area]
        index = [f"- [[{c.name}]] ({c.type}, {c.status}) `{c.rel}` — {c.description}" for c in cs]
        topics = sorted({c["topic"] for c in chunk})
        rel = ledger.cards_for_topics(area, topics)
        # карточки, уже опирающиеся на те же единицы сырья (переизвлечение новой версией промпта)
        by_unit = ledger.cards_for_uids({c["uid"] for c in chunk if c.get("uid")})
        total = sum(c.size for c in cs)
        if total <= CTX_ALL_BYTES:
            show = list(cs)
        else:
            pri = lambda c: (0 if c.core else 1 if c.name in by_unit else 2 if c.name in rel else 3, c.name)  # noqa: E731
            show, b = [], 0
            for c in sorted([c for c in cs if c.core or c.name in rel or c.name in by_unit], key=pri):
                if b + c.size > CTX_MAX_BYTES and not c.core:
                    continue
                show.append(c)
                b += c.size
        full = []
        for c in show:
            ids = ledger.card_claims(c.name)
            cl = sorted([x for x in ledger.claims_by_ids(ids).values()
                         if x["quote_ok"] and not x["sensitive"] and x["status"] not in ("stale", "superseded")],
                        key=lambda x: x["id"])[:CTX_CLAIMS_PER_CARD]
            full.append({"rel": c.rel, "text": c.text, "claims": cl, "name": c.name})
        names = {c.name for c in iter_cards(cfg_wt, include_private=True)}
        allowed = {x["id"] for f in full for x in f["claims"]}
        if CTX_MODE == "trimmed_index":
            index = stage2.trimmed_index(cs, chunk, hub_name(area), CTX_INDEX_LINES)
            names = None
        return index, full, names, allowed

    def stage2_area(self, cfg_wt, ledger, area):
        with self.lock:
            pending = ledger.pending_claims([area])
        ctx = A.Ctx(cfg_wt, ledger, area, self.run_id, CORE, self.stats, self.private_names(),
                    names_hidden=CTX_MODE == "trimmed_index")
        changed = False
        k = -1
        for rnd in range(1 + S2_RETRY_ROUNDS):
            if rnd:
                # повтор в этом же прогоне: отклонённое гейтами (потеря деталей, не отражено, NOOP деталей)
                with self.lock:
                    pending = [c for c in ledger.pending_claims([area]) if c["id"] in ctx.rejected_ids
                               and c["status"] == "rejected"]
                if not pending or self.limit_hit:
                    break
                with self.lock:
                    self.bump("s2_retry_claims", len(pending))
            stop, changed_r, k = self._stage2_chunks(cfg_wt, ledger, area, ctx, stage2.chunk_claims(pending), k)
            changed = changed or changed_r
            if stop:
                break
        self._stage2_finish(cfg_wt, ledger, area, ctx, changed)

    @staticmethod
    def _rejections_for(ledger, area, chunk, limit=15):
        """Причины отказов последнего прогона: сначала те, что называют утверждения этого куска."""
        allr = ledger.last_rejections(area, limit=400)
        ids = {c["id"] for c in chunk}
        own = [r for r in allr if {int(x) for x in re.findall(r"\bc(\d+)", r[1])} & ids]
        rest = [r for r in allr if r not in own]
        return (own + rest)[:limit]

    def _stage2_chunks(self, cfg_wt, ledger, area, ctx, chunks, k):
        """→ (остановить область, были ли записи, номер последнего куска)."""
        changed = False
        for chunk in chunks:
            k += 1
            if self.limit_hit:
                return True, changed, k
            with self.lock:
                index, full, names, allowed = self._area_context(cfg_wt, ledger, area, chunk)
            with self.lock:
                rej = self._rejections_for(ledger, area, chunk) if any(c.get("attempts") for c in chunk) else []
            prompt = stage2.build_prompt(area, index, full, chunk, names, HINTS.get(area, "") if self.pilot else "",
                                         rejections=rej)
            rec = {"cmd": "consolidate_batch", "run_id": self.run_id, "stage": 2, "area": area, "chunk": k,
                   "claims": len(chunk), "model": MODEL_S2, "prompt_ver": stage2.PROMPT_VER, "ctx": CTX_MODE,
                   "cards_shown": len(full), "index_lines": sum(1 for x in index if x.startswith("- [[")),
                   "prompt_bytes": len(prompt.encode())}
            t = time.time()
            try:
                r = llm.run(prompt, MODEL_S2, timeout=1800, cwd=self.cfg.state)
            except llm.LimitHit as e:
                self.limit_hit = True
                log_run(self.cfg, **rec, status="limit", error=str(e)[:200], duration_s=round(time.time() - t, 2))
                return True, changed, k
            except llm.LLMError as e:
                # системный сбой (авторизация, запуск, тайм-аут) — попытки не сжигаем, область прерываем
                self.errors.append(f"s2 {area}#{k}: {e}")
                log_run(self.cfg, **rec, status="error", error=str(e)[:200], duration_s=round(time.time() - t, 2))
                return True, changed, k
            self._tok(r)
            try:
                ops = stage2.parse_json(r["result"]).get("ops") or []
            except ValueError as e:
                self.errors.append(f"s2 {area}#{k}: разбор JSON: {e}")
                with self.lock:
                    ledger.bump_attempts([c["id"] for c in chunk])
                log_run(self.cfg, **rec, status="error", error=f"json: {e}"[:200], tokens_in=r["tokens_in"],
                        tokens_out=r["tokens_out"], duration_s=r["duration_s"])
                continue
            with self.lock:
                before = dict(self.stats)
                A.apply_ops(ctx, ops, chunk, {c["name"] for c in full}, allowed)
                delta = {k2: self.stats.get(k2, 0) - before.get(k2, 0) for k2 in self.stats
                         if k2.startswith(("op_", "rejected", "claims_"))
                         and self.stats.get(k2, 0) != before.get(k2, 0)}
            changed = changed or bool(ctx.touched)
            log_run(self.cfg, **rec, status="ok", ops=delta, tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
                    cost_usd=r.get("cost_usd"), duration_s=r["duration_s"])
            self.out(f"  стадия 2, {area} #{k}: {len(chunk)} утв. → {delta} ({r['duration_s']} с)")
        return False, changed, k

    def _stage2_finish(self, cfg_wt, ledger, area, ctx, changed):
        # хаб — отдельным шагом после слияния дублей (hub_step): он видит итоговый состав области
        with self.lock:
            self.conflicts += ctx.conflicts
            self.touched |= ctx.touched
            self.rejected += [(area, n, w) for n, w in ctx.rejected]

    def private_names(self):
        """Имена cards/private/ из основного checkout: в worktree их нет (.gitignore)."""
        return {c.name for c in iter_cards(self.cfg, include_private=True) if c.private}

    # ---------- хабы областей ----------

    def _hub_base_path(self, area):
        return os.path.join(hubs_dir(self.cfg), area.replace("/", "--") + ".json")

    def _load_hub_file(self, area):
        try:
            with open(self._hub_base_path(area), encoding="utf-8") as f:
                b = json.load(f)
            return b if isinstance(b, dict) else None
        except (OSError, ValueError):
            return None

    def _write_hub_file(self, area, b):
        p = self._hub_base_path(area)
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".hub-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(b, f, ensure_ascii=False, indent=0, sort_keys=True)
            os.replace(tmp, p)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def hub_base(self, cfg_wt, area, hub):
        """База хаба — hubs/<область>.json рядом с реестром: состав области (имя → [статус, description]), по
        которому шаг хабов последний раз записал хаб, churn (новых и снятых с прошлой пересборки LLM) и память
        о неудачах LLM. Коммиты хаба (unlink_missing, ручная правка) её не двигают.
        Запись хаба в ветке — сначала «ожидающая» база (pending), к которой после коммита ветки приписывается коммит
        (stamp_hub_bases). Коммит уже в main — переносится в подтверждённую; коммит в истории worktree (ветку
        продолжают) — действует ожидающая; иначе (коммита нет: сбой до коммита, ветку удалили и начали от main
        заново, даже с тем же именем) — выбрасывается: хаб main этих правок не видел.
        Подтверждённого состава нет (файла нет или это заглушка памяти о неудачах) — разовая база из git: состав на
        коммите последней записи хаба, сразу сохраняется. → (файл базы, база для плана: {"cards", "churn"})."""
        b = self._load_hub_file(area)
        if b is None or not isinstance(b.get("cards"), dict):
            ref = gitops.cards_at_last_write(cfg_wt, hub.rel, "cards/" + area)
            if ref is None:   # хаба нет в истории — новым считать нечего, сверяем только снятые
                ref = {c.name: (c.status, c.description) for c in iter_cards(cfg_wt) if c.area == area}
            b = {**(b or {}), "cards": {k: list(v) for k, v in ref.items()}, "churn": 0}
            self._write_hub_file(area, b)
        pend = b.get("pending")
        if pend:
            c = pend.get("commit")
            if c and gitops.is_ancestor(self.cfg.root, c, "main"):
                b.update(cards=pend["cards"], churn=pend.get("churn", 0), pending=None)
                self._write_hub_file(area, b)
                pend = None
            elif not (c and gitops.is_ancestor(cfg_wt.root, c, "HEAD")):
                b["pending"] = None
                self._write_hub_file(area, b)
                pend = None
        eff = pend or b
        return b, {"cards": eff["cards"], "churn": int(eff.get("churn") or 0)}

    def save_hub_base(self, cfg_wt, area, churn, **mem):
        """Успешная запись хаба: ожидающая база = текущий состав области (коммит — stamp_hub_bases после коммита
        ветки); mem — поля памяти о неудачах. Подтверждённый состав («cards») здесь не заводится."""
        b = self._load_hub_file(area) or {}
        b["pending"] = {"branch": self.branch, "run_id": self.run_id, "churn": churn, "commit": None,
                        "cards": {c.name: [c.status, c.description] for c in iter_cards(cfg_wt) if c.area == area}}
        b.update(mem, updated=stage2.today())
        self._write_hub_file(area, b)
        self.hub_bases.add(area)

    def stamp_hub_bases(self, cfg_wt):
        """После коммита ветки: ожидающим базам этого прогона — коммит, в котором лежит записанный хаб."""
        head = gitops.head(cfg_wt.root)
        for area in sorted(self.hub_bases):
            b = self._load_hub_file(area)
            pend = (b or {}).get("pending")
            if pend and pend.get("run_id") == self.run_id and head:
                pend["commit"] = head
                self._write_hub_file(area, b)

    def note_hub_failure(self, area, outcome, sig):
        """Неудача LLM на хабе: устойчивая (мусор вместо JSON, отказ гейтов) — план запоминается (failed_sig) и пауза
        HUB_LLM_COOLDOWN_DAYS для пересборок «по желанию» (устарел, накопилось); llm_failed_since — с какого дня
        отказы идут подряд (сигнал health через 7 дней). Системная (тайм-аут, запуск, авторизация) — повтор не раньше
        чем через HUB_LLM_ERROR_RETRY_H часов. Лимит не запоминается — пройдёт сам. Заглушка без «cards»."""
        if outcome not in ("bad", "error"):
            return
        b = self._load_hub_file(area) or {}
        if outcome == "bad":
            b.update(failed_sig=sig, llm_failed_at=stage2.today())
            if not b.get("llm_failed_since"):
                b["llm_failed_since"] = stage2.today()
        else:
            b["llm_error_at"] = time.time()
        self._write_hub_file(area, b)

    def llm_gate(self, area, sig, optional):
        """Можно ли звать LLM на хаб → (да/нет, почему нет). optional — пересборка «по желанию»: код справился бы."""
        b = self._load_hub_file(area) or {}
        try:
            err_at = float(b.get("llm_error_at") or 0)
        except (TypeError, ValueError):
            err_at = 0.0
        if time.time() - err_at < HUB_LLM_ERROR_RETRY_H * 3600:
            return False, f"LLM на этом хабе падал (сбой) меньше {HUB_LLM_ERROR_RETRY_H} ч назад"
        if sig and b.get("failed_sig") == sig:
            return False, "LLM на этом плане уже отказал"
        fa = b.get("llm_failed_at")
        if fa and (optional or not sig):
            try:
                if (dt.date.today() - dt.date.fromisoformat(fa)).days < HUB_LLM_COOLDOWN_DAYS:
                    return False, f"пауза после отказа LLM {fa}"
            except ValueError:
                pass
        return True, ""

    def confirm_hub_bases(self):
        """Ветка слита в main — все ожидающие базы, чей коммит уже в main, становятся подтверждёнными."""
        d = hubs_dir(self.cfg)
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if not fn.endswith(".json"):
                continue
            area = fn[:-5].replace("--", "/")
            b = self._load_hub_file(area)
            pend = (b or {}).get("pending")
            if pend and pend.get("commit") and gitops.is_ancestor(self.cfg.root, pend["commit"], "main"):
                b.update(cards=pend["cards"], churn=pend.get("churn", 0), pending=None)
                self._write_hub_file(area, b)

    def hub_plan(self, cfg_wt, area, hub, ref):
        """Что поменялось в составе области для хаба относительно базы ref (имя → [статус, description])
        → (add [(имя, description)], gone {имена}, relabel {имя: description}).
        gone — перечисленные в хабе карточки, которых больше нет, которые стали superseded или ушли в другую область;
        relabel — перечисленные, у которых сменился description; add — активные карточки области, которых в базе не
        было (или были superseded), и замены снятых. Правка тела без смены description хаб не трогает."""
        cards = A.card_map(cfg_wt)
        private = self.private_names()
        linked = stage2.hub_links(hub.body)
        gone = {n for n in linked if (n not in cards and n not in private)
                or (n in cards and (cards[n].status == "superseded" or cards[n].area != area))}
        # подпись берётся из description по hub_label: сменилась только часть после разделителя — подпись та же
        relabel = {n: cards[n].description for n in linked if n in cards and n not in gone and n in ref
                   and stage2.hub_label(ref[n][1]) != stage2.hub_label(cards[n].description)}

        def fits(c):
            return c is not None and c.area == area and not c.private and c.name != hub.name \
                and c.status != "superseded" and c.name not in linked

        new = [c for c in cards.values() if fits(c) and (c.name not in ref or ref[c.name][0] == "superseded")]
        for n in sorted(gone):        # замена снятой карточки — в хаб вместо неё
            for r in as_list(cards[n].get("superseded_by")) if n in cards else []:
                if fits(cards.get(r)):
                    new.append(cards[r])
        new = sorted({c.name: c for c in new}.values(),
                     key=lambda c: (not (c.core or c.name in CORE), c.importance != "high", c.name))
        return [(c.name, c.description) for c in new], gone, relabel

    def hub_step(self, cfg_wt, ledger, area, allow_llm=True):
        """Хаб области после стадии 2 и слияния дублей (и в прогоне ниже порога — только кодом, allow_llm=False).
        Хаба нет (а область не меньше HUB_MIN_CARDS) — собирает LLM. Поменялся состав относительно базы (hub_base) —
        список правится кодом: «Суть» и отобранные ссылки прежние, новые — в «### Новые карточки» с вытеснением
        старых новых (они остаются в `_index.md`; core и importance: high не вытесняются). LLM — если правка кодом
        невозможна (нет раздела, не помещаются прежние или важные, не показана ни одна новая), хаб устарел
        (review_after) или накопилось HUB_LLM_CHURN новых и снятых с прошлой пересборки; память о неудачах — llm_gate.
        Хаб пишется только целиком; не вышло — pending, база не двигается, повтор следующим прогоном.
        Исход хаба — ровно один из hubs_llm / hubs_code / hubs_skipped / hubs_pending; hubs_llm_failed — отдельный
        счётчик неудачных вызовов LLM (хаб после них может уйти и в code, и в pending)."""
        name = hub_name(area)
        area_cards = [c for c in iter_cards(cfg_wt) if c.area == area]
        hub = next((c for c in area_cards if c.name == name), None)
        if hub is None:
            big = len([c for c in area_cards if c.name != name]) >= HUB_MIN_CARDS.get(area.split("/")[0], 1)
            if not big or not allow_llm:
                self.bump("hubs_skipped")
                return "skipped"
            ok, why = self.llm_gate(area, None, optional=False)
            if ok and not self.limit_hit:
                res = self.hub(cfg_wt, ledger, area, "нет хаба")
                if res == "ok":
                    self.save_hub_base(cfg_wt, area, 0, failed_sig=None, llm_failed_at=None, llm_failed_since=None)
                    return "llm"
                self.note_hub_failure(area, res, None)
            self.bump("hubs_pending")
            return "pending"
        if hub.status == "superseded":
            self.bump("hubs_skipped")
            return "skipped"
        _, base = self.hub_base(cfg_wt, area, hub)
        add, gone, relabel = self.hub_plan(cfg_wt, area, hub, base["cards"])
        if not (add or gone or relabel):
            self.bump("hubs_skipped")
            return "skipped"
        t = time.time()
        sig = __import__("hashlib").sha256(json.dumps(
            [area, sorted(n for n, _ in add), sorted(gone), sorted(relabel.items()), hub.text],
            ensure_ascii=False).encode()).hexdigest()[:16]
        churn = base["churn"] + len(add) + len(gone)
        cm = A.card_map(cfg_wt)
        must = {n for n, c in cm.items() if c.area == area and (n in CORE or c.core or c.importance == "high")}
        note = (f"- {stage2.today()} — список обновлён кодом (прогон {self.run_id}): +{{shown}}, "
                f"только в _index.md {{only}}, −{len(gone)}, подписи {len(relabel)}")
        res = stage2.update_hub_body(hub.body, area, add, gone, relabel, note, must=must)
        if res is not None and add and not res[1] and not (gone or relabel):
            res = None          # ни одна новая не показана — переписывать хаб ради строки истории незачем
        ra = str(hub.get("review_after") or "")
        if res is None:
            reason = ("нет раздела «## Карточки»" if stage2.HUB_CARDS_H not in [ln.strip() for ln in hub.body.split("\n")]
                      else f"правка кодом не помещается (прежние или важные карточки; +{len(add)}, подписи {len(relabel)})")
        elif ra and ra < stage2.today():
            reason = "хаб устарел (review_after)"
        elif churn >= HUB_LLM_CHURN:
            reason = f"накопилось изменений состава {churn} ≥ {HUB_LLM_CHURN}"
        else:
            reason = None
        if reason and allow_llm and not self.limit_hit:
            ok, why = self.llm_gate(area, sig, optional=res is not None)
            if not ok:
                self.out(f"  хаб {area}: LLM не зову — {why} ({reason})")
            else:
                out = self.hub(cfg_wt, ledger, area, reason)
                if out == "ok":
                    self.save_hub_base(cfg_wt, area, 0, failed_sig=None, llm_failed_at=None, llm_failed_since=None)
                    return "llm"
                self.note_hub_failure(area, out, sig)
        if res is None:
            return self._hub_pending(area, add, gone, relabel, t, reason or "правка кодом не применяется")
        body, shown, evicted = res
        if not self._hub_write(cfg_wt, area, hub, body, add, gone, relabel, shown, evicted, t):
            return "pending"
        self.save_hub_base(cfg_wt, area, churn)
        return "code"

    def _hub_pending(self, area, add, gone, relabel, t, reason):
        self.bump("hubs_pending")
        log_run(self.cfg, cmd="consolidate_batch", run_id=self.run_id, stage="2-hub", area=area, mode="code",
                status="pending", reason=reason, added=[], removed=[], relabeled=[],
                pending_added=[n for n, _ in add], pending_removed=sorted(gone), pending_relabeled=sorted(relabel),
                duration_s=round(time.time() - t, 2))
        self.out(f"  хаб {area}: не изменён ({reason}); повтор в следующем прогоне")
        return "pending"

    def _hub_write(self, cfg_wt, area, hub, body, add, gone, relabel, shown, evicted, t):
        fm, _ = parse_frontmatter(hub.text)
        fm = dict(fm or {})
        for k in ("keys", "sources", "supersedes", "superseded_by", "applies_to"):
            if k in fm:
                fm[k] = as_list(fm[k])
        text = stage2.render_card(fm, body)
        if len(text.encode("utf-8")) > LIMIT_CARD_BYTES or len(body.strip("\n").splitlines()) > LIMIT_HUB_LINES:
            self._hub_pending(area, add, gone, relabel, t, "хаб вне пределов")
            return False
        if text != hub.text:
            tmp = hub.abs + ".kbtmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, hub.abs)
            with self.lock:
                self.touched.add(hub.name)
        self.bump("hubs_code")
        log_run(self.cfg, cmd="consolidate_batch", run_id=self.run_id, stage="2-hub", area=area, mode="code",
                status="ok", added=shown, evicted=evicted, removed=sorted(gone), relabeled=sorted(relabel),
                only_index=[n for n, _ in add if n not in shown], tokens_in=0, tokens_out=0,
                duration_s=round(time.time() - t, 2))
        self.out(f"  хаб {area}: список обновлён кодом (+{len(shown)}, только в _index.md {len(add) - len(shown)}, "
                 f"−{len(gone)}, подписи {len(relabel)})")
        return True

    def hub(self, cfg_wt, ledger, area, reason=""):
        """Пересборка хаба LLM: «Суть», группировка и подписи; тело ужимается до бюджета промпта (31 строка,
        3200 Б) — остальное место под «### Новые карточки». → "ok" — записан; "limit"; "error" — системный сбой
        вызова (тайм-аут, запуск, авторизация); "bad" — мусор вместо JSON или отказ гейтов; "empty" — нечего собирать."""
        name = hub_name(area)
        ctx = A.Ctx(cfg_wt, ledger, area, self.run_id, CORE, self.stats, self.private_names())
        with self.lock:
            info = []
            for c in iter_cards(cfg_wt):
                if c.area != area or c.name == name or c.status == "superseded":
                    continue
                sut = next((ln for ln in c.body.splitlines() if ln.strip().startswith("**Суть")), "")
                cl = [x for x in ledger.claims_by_ids(ledger.card_claims(c.name)).values()
                      if x["quote_ok"] and not x["sensitive"]]
                info.append({"name": c.name, "type": c.type, "description": c.description, "sut": sut[:300],
                             "claims": cl})
        if not info:
            return "empty"
        rec = {"cmd": "consolidate_batch", "run_id": self.run_id, "stage": "2-hub", "area": area, "model": MODEL_S2,
               "mode": "llm", "reason": reason}
        t = time.time()
        try:
            r = llm.run(stage2.build_hub_prompt(area, name, info), MODEL_S2, timeout=900, cwd=self.cfg.state)
            self._tok(r)
            h = stage2.parse_json(r["result"])
        except llm.LimitHit as e:
            self.limit_hit = True
            self.bump("hubs_llm_failed")
            log_run(self.cfg, **rec, status="limit", error=str(e)[:200], duration_s=round(time.time() - t, 2))
            return "limit"
        except (llm.LLMError, ValueError) as e:
            self.errors.append(f"hub {area}: {e}")
            self.bump("hubs_llm_failed")
            log_run(self.cfg, **rec, status="error", error=str(e)[:200], duration_s=round(time.time() - t, 2))
            return "bad" if isinstance(e, ValueError) else "error"
        op = {"op": "UPDATE", "name": name, "dir": area, "type": "topic", "status": "active",
              "description": h.get("description") or f"Хаб области {area}", "keys": h.get("keys") or [area],
              "claims": h.get("claims") or [], "body": h.get("body") or "", "importance": "normal"}
        # ссылки хаба — только на существующие карточки
        with self.lock:
            names = {c.name for c in iter_cards(cfg_wt, include_private=True)} | {name} | self.private_names()
        op["body"] = "\n".join(ln for ln in op["body"].split("\n")
                               if all(m in names for m in A.LINK_RX.findall(ln)))
        # −1 строка: apply_card_op допишет строку «Истории» прогона
        op["body"] = stage2.trim_hub(op["body"], area, max_lines=HUB_LLM_LINES - 1, max_bytes=HUB_LLM_BYTES - 120)
        with self.lock:
            cards_by_name = A.card_map(cfg_wt)
            allowed = {x["id"] for c in info for x in c["claims"][:4]}
            res = A.apply_card_op(ctx, op, set(), cards_by_name, {name}, allowed)
            self.touched |= ctx.touched
            self.rejected += [(area, n, w) for n, w in ctx.rejected]
        self.bump("hubs_llm" if res else "hubs_llm_failed")
        log_run(self.cfg, **rec, status="ok" if res else "rejected", tokens_in=r["tokens_in"],
                tokens_out=r["tokens_out"], cost_usd=r.get("cost_usd"), duration_s=r["duration_s"])
        self.out(f"  хаб {area} (LLM: {reason}): {'записан' if res else 'отклонён: ' + str(ctx.rejected[-1:])}")
        return "ok" if res else "bad"

    # ---------- почти-дубли и битые ссылки ----------

    def dedup(self, cfg_wt, ledger):
        """Почти-дубли среди активных карточек ветки: Opus сливает в одну, вторая → superseded.
        Смотрим только пары с карточкой, изменённой в ветке: пары main уже разбирали прошлые прогоны.
        Пары разных областей сливаем, если обе области в прогоне (иначе такая пара валит гейт каждый день навсегда).
        Пары, которые модель признала разными сущностями, гейт не валит (distinct_pairs по реестру);
        остальные оставленные (две core, область вне прогона, слияние отклонено) валят гейт и ждут человека."""
        with self.lock:
            cs = [c for c in iter_cards(cfg_wt) if c.status != "superseded"]
            focus = G.changed_vs_main(cs, self.cfg)
        by = {c.name: c for c in cs}
        done = set()
        for pair in G.near_duplicates(cs, focus=focus):
            a, b = pair.split(" (")[0].split(" ~ ")
            if a in done or b in done or a not in by or b not in by:
                continue
            ca, cb = by[a], by[b]
            # обе области из прогона; две core — не трогаем; core всегда остаётся
            if ca.area not in self.areas or cb.area not in self.areas:
                continue
            if {hub_name(ca.area), hub_name(cb.area)} & {a, b}:   # хабы ведёт hub_step, сливать их нельзя
                continue
            core_a, core_b = ca.core or a in CORE, cb.core or b in CORE
            if core_a and core_b:
                continue
            force_keep = a if core_a else (b if core_b else None)
            memo = f"{a}~{b}"
            sig = dedup_sig(ca.text, cb.text)
            with self.lock:
                if dedup_seen(ledger, ca, cb):
                    continue          # эту пару в этом виде уже пробовали — ждём изменений или человека
            if self.limit_hit:
                break
            done |= {a, b}
            info = []
            for c in (by[a], by[b]):
                cl = [x for x in ledger.claims_by_ids(ledger.card_claims(c.name)).values()
                      if x["quote_ok"] and not x["sensitive"] and x["status"] != "stale"]
                info.append({"rel": c.rel, "text": c.text, "claims": cl, "name": c.name})
            rec = {"cmd": "consolidate_batch", "run_id": self.run_id, "stage": "2-dedup", "pair": [a, b],
                   "model": MODEL_S2}
            t = time.time()
            try:
                r = llm.run(stage2.build_dedup_prompt(info, force_keep), MODEL_S2, timeout=900, cwd=self.cfg.state)
                self._tok(r)
                h = stage2.parse_json(r["result"])
            except llm.LimitHit as e:
                self.limit_hit = True
                log_run(self.cfg, **rec, status="limit", error=str(e)[:200], duration_s=round(time.time() - t, 2))
                break
            except (llm.LLMError, ValueError) as e:
                self.errors.append(f"dedup {a}~{b}: {e}")
                log_run(self.cfg, **rec, status="error", error=str(e)[:200], duration_s=round(time.time() - t, 2))
                continue
            if h.get("distinct"):
                with self.lock:
                    ledger.add_rejection(memo, "dedup", f"разные сущности [{sig}]: {str(h.get('reason'))[:150]}",
                                         self.run_id)
                self.distinct.add(f"{a} ~ {b}")
                log_run(self.cfg, **rec, status="distinct", tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
                        duration_s=r["duration_s"])
                self.out(f"  дубли {a} ~ {b}: модель считает разными — оставлено гейту")
                continue
            keep = force_keep or (h.get("keep") if h.get("keep") in (a, b) else a)
            other = b if keep == a else a
            kc = by[keep]
            op = {"op": "UPDATE", "name": keep, "dir": "/".join(kc.dir_parts), "type": h.get("type") or kc.type,
                  "status": "active", "description": h.get("description") or kc.description,
                  "keys": h.get("keys") or kc.keys, "applies_to": h.get("applies_to") or [],
                  "importance": h.get("importance") or kc.importance, "claims": h.get("claims") or [],
                  "body": h.get("body") or "", "supersedes": [other], "sensitivity": kc.get("sensitivity")}
            ctx = A.Ctx(cfg_wt, ledger, kc.area, self.run_id, CORE, self.stats, self.private_names(),
                        record_rejections=False)
            allowed = {x["id"] for i in info for x in i["claims"]}
            with self.lock:
                ob = A.parse_frontmatter(by[other].text)[1] or ""
                res = A.apply_card_op(ctx, op, set(), A.card_map(cfg_wt), {keep}, allowed, extra_baseline={other: ob})
                if res:
                    oc = A.card_map(cfg_wt).get(other)
                    if oc is not None and oc.status != "superseded":
                        A.mark_superseded(cfg_wt, oc, keep, self.run_id)
                    ledger.link(ledger.card_claims(other), keep, "MERGE", self.run_id)
                    self.touched |= {keep, other}
                    self.bump("op_MERGE")
                else:
                    self.rejected += [(kc.area, n, w) for n, w in ctx.rejected]
                    ledger.add_rejection(memo, "dedup", f"слияние отклонено [{sig}]: {ctx.rejected[-1][1][:150]}",
                                         self.run_id)
            log_run(self.cfg, **rec, status="ok" if res else "rejected", keep=keep, tokens_in=r["tokens_in"],
                    tokens_out=r["tokens_out"], duration_s=r["duration_s"])
            self.out(f"  дубли {a} ~ {b}: {'слиты в ' + keep if res else 'слияние отклонено'}")

    def unlink_missing(self, cfg_wt):
        cs = iter_cards(cfg_wt, include_private=True)
        names = {c.name for c in cs} | self.private_names()
        n = 0
        for c in cs:
            new = stage2.unlink_missing(c.text, names)
            if new != c.text:
                tmp = c.abs + ".kbtmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(new)
                os.replace(tmp, c.abs)
                n += 1
        self.stats["links_unlinked"] = n

    # ---------- проверка использованных цитат по файлам ----------

    def recheck_quotes(self, cfg_wt, ledger):
        """Перепроверка: каждое утверждение, на которое опираются карточки, дословно есть в своём src."""
        cache = {}
        used = ok = 0
        bad = []
        for c in iter_cards(cfg_wt):
            ids = ledger.card_claims(c.name)
            for cl in ledger.claims_by_ids(ids).values():
                if cl["status"] in ("superseded", "stale"):
                    continue
                used += 1
                src = cl["src"]
                if src not in cache:
                    base = cfg_wt.root          # снимок ветки: raw/ и verified/ те же, что видела стадия 1
                    try:
                        with open(os.path.join(base, src), encoding="utf-8", errors="replace") as f:
                            cache[src] = f.read()
                    except OSError:
                        cache[src] = ""
                if stage1.quote_in(cl["quote"], cache[src]):
                    ok += 1
                else:
                    bad.append(f"c{cl['id']} → {c.name} ({src})")
        return used, ok, bad

    # ---------- целиком ----------

    def execute(self):
        t0 = time.time()
        cfg = self.cfg
        if free_gb(cfg.root) < cfg.min_free_gb:
            log_run(cfg, cmd="consolidate", run_id=self.run_id, status="disk_low", duration_s=0)
            self.out("СТОП: мало места на диске")
            return 3
        branch = os.environ.get("KB_CONSOLIDATE_BRANCH") or f"consolidate/{dt.date.today().isoformat()}"
        wt = None
        status = "ok"
        result = {}
        ledger = None
        try:
            if not self.dry:
                wt, branch = gitops.prepare_worktree(cfg, branch)
                self.branch = branch
                cfg_wt = gitops.wt_cfg(cfg, wt)
            else:
                cfg_wt = cfg
            if self.dry:
                with repo_lock(cfg):
                    units_all = iter_units(cfg.raw, cfg.root, [os.path.join(cfg.root, "verified")])
            else:  # снимок ветки: в неё только что слит свежий main
                units_all = iter_units(cfg_wt.raw, cfg_wt.root, [os.path.join(cfg_wt.root, "verified")])
            units, aliases = collapse(units_all)
            self.stats["units_total"] = len(units_all)
            self.stats["units_unique"] = len(units)
            ledger = Ledger(ledger_path(cfg))
            if not self.dry:
                self.stats["units_changed"] = ledger.sync_units(units, aliases)
            # порог: к LLM меньше min_units единиц — LLM не зовём вовсе, единицы ждут следующего прогона
            n_llm = len(self.units_to_llm(ledger, units))
            self.stats["units_to_llm"] = n_llm
            below = n_llm < self.min_units
            if below:
                self.stats["min_units"] = self.min_units
                if n_llm:
                    self.deferred = {"units": n_llm, "min_units": self.min_units,
                                     "reason": f"к сводке {n_llm} ед. < порога {self.min_units}; ждут следующего прогона"}
                else:
                    self.no_units = True
                self.out(f"порог: к LLM {n_llm} ед. < {self.min_units} — " +
                         ("прогон отложен, LLM не вызывается" if n_llm else "сводить нечего"))
            if "1" in self.stages and (self.dry or not below):
                self.stage1(ledger, units)
            self.areas = expand_areas(self.areas, ledger)
            if self.atom_cov:
                self.stats["s1_atom_coverage"] = round(sum(self.atom_cov) / len(self.atom_cov), 3)
            if self.dry:
                pend = ledger.pending_claims(self.areas)
                self.stats["s2_pending_claims"] = len(pend)
                self.out(f"dry-run: единиц {len(units_all)} (уникальных {len(units)}), к извлечению "
                         f"{self.stats.get('s1_units_pending', 0)} в {self.stats.get('s1_batches_pending', 0)} батчах "
                         f"(запуск {self.stats.get('s1_batches_run', 0)}), утверждений к сведению {len(pend)}, "
                         f"единиц к LLM {n_llm}" + (f" < порога {self.min_units} → отложено" if self.deferred else ""))
                # запись в журнал — одна, в finally (раньше dry-run писал две: вторую без mode и со статусом ok)
                status = "deferred" if self.deferred else "no_units" if self.no_units else "ok"
                result["mode"] = "dry-run"
                return 0
            if "2" in self.stages and not self.limit_hit and self.areas and not below:
                self.route(ledger)
                with cf.ThreadPoolExecutor(max_workers=min(3, len(self.areas))) as ex:
                    list(ex.map(lambda a: self.stage2_area(cfg_wt, ledger, a), self.areas))
            if "2" in self.stages and not self.limit_hit and not below:
                self.dedup(cfg_wt, ledger)
            if "2" in self.stages:
                # хабы — после дублей: состав области окончательный; правка кодом идёт и после лимита,
                # и ниже порога (тогда без LLM): карточки из main не ждут прогона с LLM. С `projects` — и хабы
                # проектов без несведённых утверждений (кодом): ручная карточка проекта не ждёт его сырья
                extra = sorted({c.area for c in iter_cards(cfg_wt) if c.area.startswith("projects/")
                                and c.name == hub_name(c.area)} - set(self.areas)) if self.req_projects else []
                for a in self.areas + extra:
                    self.hub_step(cfg_wt, ledger, a, allow_llm=not below and a in self.areas)
            self.unlink_missing(cfg_wt)
            n_conf = A.append_conflicts(cfg_wt, self.conflicts)
            used, ok, bad = self.recheck_quotes(cfg_wt, ledger)
            prior_unmerged = not gitops.branch_merged(cfg, branch)   # в ветке работа прошлых прогонов
            commit = gitops.commit_branch(cfg, wt, f"consolidate {self.run_id}: карточки {len(self.touched)}, "
                                                   f"конфликтов {n_conf}")
            self.stamp_hub_bases(cfg_wt)
            n_conf_branch = gitops.new_conflicts_on_branch(cfg, branch)
            g = G.run_gates(cfg_wt, {"claims_used": used, "claims_used_quote_ok": ok}, n_conf_branch,
                            self.max_conflicts, self.private_names(), known_dups=sorted(set(distinct_pairs(ledger, cfg_wt)) | self.distinct),
                            main_cfg=cfg)   # cfg — main до слияния
            g["quote_bad"] = bad[:20]
            result.update(gates=g, commit=commit, branch=branch, conflicts_new=n_conf)
            merged = gitops.branch_merged(cfg, branch)
            if g["ok"] and not self.no_merge and not merged:
                msh, ish, changed = gitops.merge_to_main(cfg, branch, f"consolidate: слияние {branch} ({self.run_id})")
                result.update(merged=msh, index_commit=ish)
                log_run(cfg, cmd="index", run_id=self.run_id, status="ok", changed=len(changed), commit=ish,
                        duration_s=0)
                self.confirm_hub_bases()
            elif merged and g["ok"]:
                result.update(merged="уже в main")
                self.confirm_hub_bases()
            elif not g["ok"]:
                status = "gates_failed"
            if self.limit_hit:
                status = "limit"
            elif self.errors and status == "ok":
                status = "partial"
            elif self.deferred and status == "ok":
                status = "deferred"
            elif self.no_units and status == "ok":
                status = "no_units"
            if status in ("deferred", "no_units") and result.get("merged") and result["merged"] != "уже в main":
                # LLM не звали, но git-работа прогона слила вчерашнюю несслитую ветку — это видно в причине
                note = (f"слита несслитая ветка {branch} ({result['merged']})" if prior_unmerged else
                        f"слиты правки без LLM (хабы, ссылки) {branch} ({result['merged']})")
                if self.deferred:
                    self.deferred["reason"] += "; " + note
                else:
                    self.deferred = {"units": 0, "min_units": self.min_units, "reason": "к сводке 0 ед.; " + note}
        except LockTimeout:
            status = "lock_timeout"
            self.errors.append("state/repo.lock занят")
        except Exception as e:  # noqa: BLE001 — любой сбой честно в runs.jsonl, работа сохраняется в ветке
            status = "error"
            self.errors.append(f"{type(e).__name__}: {str(e)[:300]}")
            if wt and os.path.isdir(wt):
                try:
                    gitops.commit_branch(cfg, wt, f"consolidate {self.run_id}: частичное сохранение после ошибки")
                    if self.hub_bases:     # хаб в частичном коммите — ожидающая база привязана к нему
                        self.stamp_hub_bases(gitops.wt_cfg(cfg, wt))
                except Exception as e2:  # noqa: BLE001
                    self.errors.append(f"сохранение ветки: {str(e2)[:200]}")
        finally:
            led_stats = ledger.stats() if ledger else {}
            if ledger:
                ledger.close()
            rec = log_run(cfg, cmd="consolidate", run_id=self.run_id, status=status, areas=self.areas,
                          max_batches=self.max_batches, stats=self.stats, ledger=led_stats,
                          llm_calls=self.llm_calls, tokens_in=self.tokens["in"], tokens_out=self.tokens["out"],
                          cost_usd_list=round(self.tokens["cost_usd"], 3), cards_touched=sorted(self.touched),
                          rejected=[f"{a}/{n}: {w}"[:400] for a, n, w in self.rejected][:40],
                          errors=self.errors[:10], duration_s=round(time.time() - t0, 2),
                          hubs={k: self.stats.get(f"hubs_{k}", 0) for k in ("llm", "code", "skipped", "pending",
                                                                              "llm_failed")},
                          deferred=self.deferred, min_units=self.min_units,
                          **{k: v for k, v in result.items() if k != "gates"},
                          gates={k: v for k, v in (result.get("gates") or {}).items()} or None)
        self.report(rec)
        return 0 if status in ("ok", "deferred", "no_units") else (5 if status == "limit" else 1)

    def report(self, rec):
        o = self.out
        o(f"сводчик {rec['run_id']}: {rec['status']} за {rec['duration_s']} с; вызовов LLM {rec['llm_calls']}, "
          f"токенов ~{rec['tokens_in']} вход / {rec['tokens_out']} выход")
        if rec.get("deferred"):
            o(f"  отложено: {rec['deferred']['reason']}")
        h = rec.get("hubs") or {}
        if any(h.values()):
            o(f"  хабы: LLM {h.get('llm', 0)}, кодом {h.get('code', 0)}, не тронуты {h.get('skipped', 0)}, "
              f"отложены {h.get('pending', 0)}"
              + (f", LLM не смог {h['llm_failed']}" if h.get("llm_failed") else ""))
        o(f"  статистика: {rec['stats']}")
        o(f"  реестр: {rec['ledger']}")
        if rec.get("cards_touched"):
            o(f"  карточки: {', '.join(rec['cards_touched'])}")
        g = rec.get("gates")
        if g:
            o(f"  гейты: ok={g['ok']}, lint ошибок {len(g['lint_errors'])}, почти-дублей {len(g['near_duplicates'])} "
              f"(+ известных из main {len(g.get('near_duplicates_known') or [])}), "
              f"sensitive {len(g['sensitive'])}, секретов {len(g['secrets'])}, цитаты {g['claims_used_quote_ok']}/"
              f"{g['claims_used']}, новых конфликтов {g['new_conflicts']}")
            for k in ("lint_errors", "lint_errors_known", "near_duplicates", "near_duplicates_known", "sensitive",
                      "sensitive_known", "secrets", "quote_bad"):
                for x in g.get(k) or []:
                    o(f"    {k}: {x}")
        for r in rec.get("rejected") or []:
            o(f"  отклонено: {r}")
        for e in rec.get("errors") or []:
            o(f"  ошибка: {e}")
        if rec.get("merged"):
            o(f"  слито в main: {rec['merged']}, индексы: {rec.get('index_commit')}")
        elif rec.get("branch"):
            o(f"  ветка: {rec['branch']} (не слита)")


def dedup_sig(text_a, text_b):
    return hashlib.sha256((text_a + "\0" + text_b).encode()).hexdigest()[:16]


def dedup_seen(ledger, ca, cb, kind=""):
    """Пару в этом виде (те же тексты) dedup уже разбирал — в любом порядке имён. kind: «разные сущности» —
    только вердикт «разные»; пусто — любой исход (разные или слияние отклонено)."""
    for x, y in ((ca, cb), (cb, ca)):
        if ledger.db.execute("SELECT 1 FROM rejections WHERE area='dedup' AND card=? AND reason LIKE ?",
                             (f"{x.name}~{y.name}", f"{kind}%[{dedup_sig(x.text, y.text)}]%")).fetchone():
            return True
    return False


def distinct_pairs(ledger, cfg_wt):
    """Пары, которые dedup признал разными сущностями и чьи тексты с тех пор не менялись, — для гейта как известные.
    По реестру, а не по тому, дошёл ли dedup до пары в этом прогоне (отложенный прогон, лимит)."""
    rows = ledger.db.execute("SELECT DISTINCT card FROM rejections WHERE area='dedup' "
                             "AND reason LIKE 'разные сущности [%'").fetchall()
    if not rows:
        return []
    by = {c.name: c for c in iter_cards(cfg_wt) if c.status != "superseded"}
    out = []
    for (memo,) in rows:
        a, _, b = memo.partition("~")
        if a in by and b in by and dedup_seen(ledger, by[a], by[b], "разные сущности"):
            out.append(f"{a} ~ {b}")
    return out


def ledger_path(cfg):
    """Реестр: state/ledger.sqlite; эксперимент в своей ветке ведёт свой реестр (KB_LEDGER), чтобы не трогать main.
    Свой реестр при первом запуске — копия реестра main: иначе у карточек main нет опор и гейт «утверждение
    не пропало» их не защищает."""
    main = os.path.join(cfg.state, "ledger.sqlite")
    p = os.environ.get("KB_LEDGER")
    if not p:
        return main
    p = os.path.abspath(p)
    if not os.path.exists(p) and os.path.exists(main):
        import sqlite3
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        src = sqlite3.connect(f"file:{main}?mode=ro", uri=True, timeout=60)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)          # копия появляется только целиком
    return p


def hubs_dir(cfg):
    """База хабов (<каталог>/<область>.json): у боевого реестра — state/hubs, у эксперимента со своим KB_LEDGER —
    каталог по имени его реестра (state/ledger-x.sqlite → state/ledger-x.hubs), чтобы не задеть боевую базу."""
    p = os.environ.get("KB_LEDGER")
    if not p or os.path.abspath(p) == os.path.abspath(os.path.join(cfg.state, "ledger.sqlite")):
        return os.path.join(cfg.state, "hubs")
    p = os.path.abspath(p)
    return os.path.splitext(p)[0] + ".hubs"


def experiment_error(cfg):
    """Режим эксперимента требует всё сразу: свой реестр (не main), свой worktree и ветку не из consolidate/*
    — иначе утверждения пометятся сведёнными в реестре main или ветку подхватит боевой прогон. → текст ошибки."""
    br, led, wt = (os.environ.get(k) for k in ("KB_CONSOLIDATE_BRANCH", "KB_LEDGER", "KB_CONSOLIDATE_WT"))
    if not (br or led):
        return None
    main = os.path.abspath(os.path.join(cfg.state, "ledger.sqlite"))
    if not led or os.path.abspath(led) == main:
        return "эксперимент: нужен свой KB_LEDGER (не state/ledger.sqlite)"
    if not br or br.startswith("consolidate/"):
        return "эксперимент: нужна своя ветка KB_CONSOLIDATE_BRANCH (не consolidate/*)"
    if not wt or os.path.abspath(wt) == os.path.abspath(gitops.wt_path_default(cfg)):
        return "эксперимент: нужен свой KB_CONSOLIDATE_WT (не боевой worktree сводчика)"
    return None


def experiment_mode():
    """Своя ветка или свой реестр — эксперимент: в main не сливается никогда (сливает координатор вручную)."""
    return bool(os.environ.get("KB_CONSOLIDATE_BRANCH") or os.environ.get("KB_LEDGER"))


def expand_areas(areas, ledger):
    """`projects` → все области projects/<slug>, по которым в реестре есть несведённые утверждения."""
    if "projects" not in areas:
        return areas
    rows = ledger.db.execute("SELECT DISTINCT area FROM claims WHERE area LIKE 'projects/%' "
                             "AND status IN ('new','rejected') ORDER BY area").fetchall()
    out = [a for a in areas if a != "projects"]
    return out + [r["area"] for r in rows if r["area"] not in out and re.match(r"^projects/[a-z0-9][a-z0-9-]*$",
                                                                               r["area"])]


def parse_areas(s):
    if not s:
        return list(DEFAULT_AREAS)
    out = [a.strip() for a in s.split(",") if a.strip()]
    for a in out:
        if a == "private":
            raise ValueError("private/ сводчик пока не пишет (sensitive — отброс)")
        if a not in AREAS and not re.match(r"^projects/[a-z0-9][a-z0-9-]*$", a):
            raise ValueError(f"неизвестная область {a}")
    return out


def main(cfg, a, out=print):
    try:
        areas = parse_areas(a.areas)
    except ValueError as e:
        out(f"ошибка: {e}")
        return 2
    err = experiment_error(cfg)
    if err:
        out(f"ошибка: {err}")
        return 2
    if experiment_mode() and not a.no_merge:
        out("эксперимент (KB_CONSOLIDATE_BRANCH/KB_LEDGER): слияние в main выключено (--no-merge)")
    r = Run(cfg, areas, a.max_batches, dry_run=a.dry_run, parallel=a.parallel, batch_size=a.batch_size,
            no_merge=a.no_merge or experiment_mode(), max_conflicts=a.max_conflicts, pilot=a.pilot,
            stages=a.stages, out=out, min_units=getattr(a, "min_units", 0) or 0)
    return r.execute()
