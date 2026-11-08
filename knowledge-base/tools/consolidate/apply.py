"""Применение операций стадии 2 к карточкам в worktree."""
import os
import re
import sys

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from kbcore import LIMIT_CARD_BYTES, LIMIT_HUB_LINES, as_list, iter_cards, parse_frontmatter  # noqa: E402

from . import atoms as AT  # noqa: E402
from . import ledger as L  # noqa: E402
from .stage2 import (NAME_RX, OPS, TYPES, sensitive_hit, _ids, ensure_history, render_card,  # noqa: E402
                     review_after, sources_for, strip_markers, taken_reason, today, unsupported_lines)

LINK_RX = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


class Ctx:
    def __init__(self, cfg_wt, ledger, area, run_id, core_names=(), stats=None, private_names=(),
                 record_rejections=True, names_hidden=False):
        self.cfg = cfg_wt
        self.ledger = ledger
        self.area = area
        self.run_id = run_id
        self.core_names = set(core_names)
        self.stats = stats if stats is not None else {}
        self.conflicts = []        # строки для CONFLICTS.md
        self.touched = set()       # имена карточек, записанных в этом прогоне
        self.rejected = []         # (имя, причина)
        self.rejected_ids = set()  # утверждения отклонённых операций — пойдут на повтор (до MAX_ATTEMPTS)
        self.private_names = set(private_names)  # имена cards/private/ из main (в worktree их нет: .gitignore)
        self.record_rejections = record_rejections
        self.names_hidden = names_hidden  # KB_S2_CTX=trimmed_index: списка занятых имён в промпте нет
        self.written = {}          # имя → тело карточки, записанной в этом ответе модели (для переноса деталей)

    def bump(self, k, n=1):
        self.stats[k] = self.stats.get(k, 0) + n


def card_map(cfg):
    """имя → Card по всей базе (включая private)."""
    return {c.name: c for c in iter_cards(cfg, include_private=True)}


def _valid_claims(ledger, ids):
    got = ledger.claims_by_ids(ids)
    return {i: c for i, c in got.items()
            if c["quote_ok"] and not c["sensitive"]
            and c["status"] not in (L.STALE, L.NO_QUOTE, L.SENSITIVE, L.SUPERSEDED)}


def _dir_ok(d, area):
    parts = d.strip("/").split("/")
    if area.startswith("projects/"):
        return d.strip("/") == area
    return parts[0] == area and len(parts) <= 2 and all(NAME_RX.match(p) for p in parts)


def _write(cfg, rel_dir, name, text):
    p = os.path.join(cfg.cards, rel_dir, name + ".md")
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    tmp = p + ".kbtmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)
    return p


def _live_claims(ledger, ids):
    return [c for c in ledger.claims_by_ids(ids).values()
            if c["quote_ok"] and not c["sensitive"]
            and c["status"] not in (L.STALE, L.NO_QUOTE, L.SENSITIVE, L.SUPERSEDED)]


def lost_claims(ledger, card_name, old_body, new_body, exclude=(), moved_to=None):
    """Гейт «утверждение не пропало»: у утверждений, сведённых в карточку, каждая деталь (атом: флаг, путь, число,
    логин…), которая была в прежнем тексте, должна остаться в новом; утверждение без атомов должно по-прежнему
    находиться (цитата или основы слов). Деталь, перенесённая в карточку этого же ответа (moved_to: имя → тело),
    не считается потерянной. → ([(утверждение, [пропавшие токены])], {id утверждения: карточка-приёмник})."""
    old_n, new_n = AT.norm_text(old_body), AT.norm_text(new_body)
    dests = {k: AT.norm_text(v) for k, v in (moved_to or {}).items()}
    out, moved = [], {}
    for c in _live_claims(ledger, ledger.card_claims(card_name)):
        if c["id"] in exclude:
            continue
        miss = AT.missing_details(c, old_n, new_n)
        if not miss:
            continue
        has_atoms = bool(AT.claim_keys(c)[0])
        dest = next((k for k, t in dests.items()
                     if (all(AT.has_atom(x, t) for x in miss) if has_atoms else AT.fully_present(c, t))), None)
        if dest is not None:
            moved[c["id"]] = dest
            continue
        out.append((c, miss))
    return sorted(out, key=lambda x: x[0]["id"]), moved


def _lost_reason(lost):
    return ("верни дословно или перенеси в новую карточку этого же ответа — потеряны детали, которые были "
            "в карточке: " + "; ".join(f"c{c['id']} ({AT._safe_hint(', '.join(f'`{t}`' for t in miss[:3]))})"
                                      for c, miss in lost[:8]) + (" …" if len(lost) > 8 else ""))


VARIANTS_H = "## Варианты и устарело"


def _keys_suffix(claims, what):
    """Ключевые токены утверждений конфликта, которых нет в тексте строки, — чтобы гейт защищал строку."""
    nt = AT.norm_text(what)
    ks = []
    for c in claims:
        at, words = AT.claim_keys(c)
        ks += [a for a in at if not AT.has_atom(a, nt)][:2]
    ks = [k for k in dict.fromkeys(ks) if AT._safe_hint(k) != "—"][:4]
    # подпись не «ключи:» — гейт секретов читает «ключ: <длинное>» как секрет
    out = (" (детали: " + ", ".join(f"`{k}`" for k in ks) + ")") if ks else ""
    return out if AT._safe_hint(out) != "—" else ""


def add_variant_line(cfg, card, line):
    """Дописать строку в раздел «Варианты и устарело» карточки (перед «Историей»). → True, если записано."""
    fm, body = parse_frontmatter(card.text)
    fm = dict(fm or {})
    if line in body:
        return False
    lines = body.rstrip("\n").split("\n")
    if VARIANTS_H in [ln.strip() for ln in lines]:
        h = next(i for i, ln in enumerate(lines) if ln.strip() == VARIANTS_H)
        j = next((k for k in range(h + 1, len(lines)) if lines[k].startswith("## ")), len(lines))
        while j > h + 1 and not lines[j - 1].strip():
            j -= 1
        lines = lines[:j] + [line] + lines[j:]
    else:
        h = next((i for i, ln in enumerate(lines) if ln.strip() == "## История"), len(lines))
        block = [VARIANTS_H, line, ""]
        if h > 0 and lines[h - 1].strip():
            block = [""] + block
        lines = lines[:h] + block + lines[h:]
    for k in ("keys", "sources", "supersedes", "superseded_by", "applies_to"):
        if k in fm:
            fm[k] = as_list(fm[k])
    text = render_card(fm, "\n".join(lines).lstrip("\n"))
    if len(text.encode("utf-8")) > LIMIT_CARD_BYTES:
        return False
    tmp = card.abs + ".kbtmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, card.abs)
    return True


def mark_superseded(cfg, card, new_name, run_id):
    fm, body = parse_frontmatter(card.text)
    fm = dict(fm or {})
    fm["status"] = "superseded"
    sb = as_list(fm.get("superseded_by"))
    if new_name not in sb:
        sb.append(new_name)
    fm["superseded_by"] = sb
    for k in ("keys", "sources", "supersedes", "applies_to"):
        if k in fm:
            fm[k] = as_list(fm[k])
    body = body.rstrip("\n") + f"\n- {today()} — superseded: заменена [[{new_name}]] (сводчик, прогон {run_id})\n" \
        if "## История" in body else body.rstrip("\n") + \
        f"\n\n## История\n- {today()} — superseded: заменена [[{new_name}]] (сводчик, прогон {run_id})\n"
    tmp = card.abs + ".kbtmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(render_card(fm, body.lstrip("\n")))
    os.replace(tmp, card.abs)


def apply_card_op(ctx, op, chunk_ids, cards_by_name, shown, allowed=None, extra_baseline=None):
    """allowed — id утверждений, которые модель видела в промпте (новые + связанные с показанными карточками);
    extra_baseline — {имя: тело} карточек, чьи детали тоже должны остаться (слияние дублей)."""
    kind = op["op"]
    name = str(op.get("name") or "").strip()

    def rej(why):
        ctx.rejected.append((name or "?", why))
        if ctx.record_rejections:
            ctx.ledger.add_rejection(name or "?", ctx.area, why, ctx.run_id)
        ctx.bump("rejected")
        ctx.rejected_ids |= set(_ids(op.get("claims"))) & set(chunk_ids)
        ctx.rejected_ids |= {i for _, ids, _ in strip_markers(str(op.get("body") or ""))[1] for i in ids} & set(chunk_ids)
        return None
    if not NAME_RX.match(name):
        return rej("плохое имя")
    d = str(op.get("dir") or ctx.area).strip().strip("/")
    if d.startswith("cards/"):
        d = d[len("cards/"):]
    if not _dir_ok(d, ctx.area):
        return rej(f"dir `{d}` вне области {ctx.area}")
    type_ = str(op.get("type") or "").strip()
    if type_ not in TYPES:
        return rej(f"type `{type_}`")
    existing = cards_by_name.get(name)
    if name in ctx.private_names:
        return rej("имя уже занято (private)")
    if existing is not None and kind == "ADD":
        if existing.area == ctx.area and name in shown:
            kind = "UPDATE"
        else:
            # trimmed_index: списка имён в промпте нет — подсказка повтора называет занявшую карточку
            return rej(taken_reason(existing) if ctx.names_hidden and not existing.private else "имя уже занято")
    if kind in ("UPDATE",) and existing is None:
        kind = "ADD"
    if kind in ("UPDATE", "SUPERSEDE") and existing is not None and name not in shown:
        return rej("UPDATE карточки, которой модель не видела целиком")

    body, meta = strip_markers(str(op.get("body") or ""))
    marker_ids = {i for _, ids, _ in meta for i in ids}
    op_ids = set(_ids(op.get("claims")))
    prev_ids = set(ctx.ledger.card_claims(name)) if existing is not None else set()
    all_ref = marker_ids | op_ids
    raw = ctx.ledger.claims_by_ids(all_ref)
    if any(c["sensitive"] for c in raw.values()):
        return rej("ссылается на sensitive-утверждение")
    seen = set(chunk_ids) | prev_ids | set(allowed or ())
    foreign = all_ref - seen
    if foreign:
        return rej(f"ссылки на утверждения вне промпта: {sorted(foreign)[:5]}")
    hit = sensitive_hit(body + "\n" + " ".join([str(op.get("description") or "")] + as_list(op.get("keys"))))
    if hit:   # раньше проверки опоры: в причину отказа не должен попасть сырой текст
        return rej(f"медицинское/контакт в тексте ({hit})")
    type_prev = prev_ids if type_ != "topic" else set()   # хаб опирается только на текущие ссылки, sources не копятся
    valid = _valid_claims(ctx.ledger, all_ref | type_prev)
    bad = unsupported_lines(meta, set(valid))
    if bad:
        return rej(f"строки без опоры: {bad[:3]}")
    if not body.strip().startswith("**Суть"):
        return rej("нет «Сути» первой строкой")
    status = str(op.get("status") or "active")
    if status not in ("active", "superseded", "disputed"):
        status = "active"
    superseded_by = [s for s in as_list(op.get("superseded_by")) if NAME_RX.match(s)]
    if status == "superseded" and not superseded_by:
        return rej("superseded без superseded_by")
    used = {i: c for i, c in valid.items() if i in all_ref or i in type_prev}
    if not used:
        return rej("нет ни одного утверждения с цитатой")
    live = any(c["src"].startswith("verified/") for c in used.values())
    core = name in ctx.core_names or (existing is not None and existing.core)
    sens = str(op.get("sensitivity") or (existing.get("sensitivity") if existing is not None else "") or "internal")
    if sens not in ("internal", "restricted"):
        sens = "internal"
    if d.endswith("/people"):
        sens = "restricted"
    keys = [k for k in as_list(op.get("keys")) if k.strip()] or name.split("-")
    v = today()
    fm = {"name": name, "description": str(op.get("description") or "").strip() or name,
          "type": type_, "status": status}
    sup = [s for s in as_list(op.get("supersedes")) if NAME_RX.match(s) and s != name]
    if existing is not None:   # прежние связи замены не теряем
        sup = list(dict.fromkeys([s for s in as_list(existing.get("supersedes")) if s != name] + sup))
    if sup:
        fm["supersedes"] = sup
    if superseded_by:
        fm["superseded_by"] = superseded_by
    applies = [a for a in as_list(op.get("applies_to")) if a.strip()] or \
        (as_list(existing.get("applies_to")) if existing is not None else [])
    if applies:
        fm["applies_to"] = applies
    fm.update({"keys": keys[:10], "sources": sources_for(list(used.values())), "verified": v,
               "verified_how": "live" if live else "source", "review_after": review_after(type_, ctx.area, v),
               "sensitivity": sens,
               "importance": "high" if core else (str(op.get("importance") or "normal")
                                                  if str(op.get("importance")) in ("high", "normal", "low")
                                                  else "normal"),
               "core": core})
    body = ensure_history(body, kind, ctx.run_id)
    if type_ == "topic" and len(body.strip("\n").splitlines()) > LIMIT_HUB_LINES:
        return rej("хаб длиннее 40 строк")
    text = render_card(fm, body)
    if len(text.encode("utf-8")) > LIMIT_CARD_BYTES:
        return rej(f"размер {len(text.encode())} Б > {LIMIT_CARD_BYTES}")

    # прежние опоры карточки можно объявить устаревшими только операцией, которая сводит новое утверждение
    sup_ids = _ids(op.get("superseded_claims"))
    old_claims = [i for i in sup_ids if i in chunk_ids or (i in prev_ids and all_ref & set(chunk_ids))]
    if len(old_claims) < len([i for i in sup_ids if i in chunk_ids or i in prev_ids]):
        ctx.bump("superseded_prev_refused")
    moved = {}
    gate_type = existing.type if existing is not None and existing.type else type_   # тип модели гейт не выключает
    if existing is not None and gate_type != "topic":
        _, old_body = parse_frontmatter(existing.text)
        lost, moved = lost_claims(ctx.ledger, name, old_body or "", body, exclude=set(old_claims),
                                  moved_to={k: v for k, v in ctx.written.items() if k != name})
        for extra_name, extra_body in (extra_baseline or {}).items():   # слияние дублей: детали второй карточки
            lost += lost_claims(ctx.ledger, extra_name, extra_body, body, exclude=set(old_claims))[0]
        lost = list({c["id"]: (c, m) for c, m in lost}.values())
        if lost:
            ctx.bump("rejected_lost_claims")
            return rej(_lost_reason(lost))
    # новые утверждения засчитываются, только если их деталь есть в тексте карточки
    nb = AT.norm_text(body)
    unreflected = sorted(i for i in used if i in chunk_ids and i not in old_claims and gate_type != "topic"
                         and not AT.claim_present(used[i], "", nb))

    if existing is not None and os.path.dirname(existing.abs) != os.path.join(ctx.cfg.cards, d):
        os.remove(existing.abs)
    _write(ctx.cfg, d, name, text)
    ctx.touched.add(name)
    if gate_type != "topic":   # хаб гейтом не охраняется — перенос детали в хаб не засчитывается
        ctx.written[name] = body
    ctx.bump(f"op_{kind}")
    if unreflected:
        why = ("утверждения не отражены в тексте (не засчитаны, повтор): " +
               "; ".join(f"c{i} ({AT.key_hint(used[i])})" for i in unreflected[:6]))
        ctx.rejected.append((name, why))
        if ctx.record_rejections:
            ctx.ledger.add_rejection(name, ctx.area, why, ctx.run_id)
        ctx.rejected_ids |= set(unreflected)
        ctx.bump("claims_unreflected", len(unreflected))
    # связь пишется для всех опор (строки карточки на них ссылаются), но сведёнными считаются только отражённые;
    # неотражённые остаются new и в конце apply_ops уходят на повтор (rejected, счётчик попыток)
    new_ids = [i for i in used if i in chunk_ids and i not in unreflected]
    ctx.ledger.link(list(used), name, kind, ctx.run_id)
    for cid, dest in moved.items():   # перенесённая деталь теперь опора карточки-приёмника — гейт охраняет её там
        ctx.ledger.link([cid], dest, "MOVED", ctx.run_id)
    if moved:
        ctx.bump("claims_moved", len(moved))
    ctx.ledger.set_claim_status(new_ids, L.CONSOLIDATED)
    ctx.ledger.set_card_topics(name, ctx.area, [str(t) for t in as_list(op.get("topics"))] +
                               [c["topic"] for c in used.values()])
    # SUPERSEDE: пометить старые карточки и старые утверждения
    if sup:
        cards_now = card_map(ctx.cfg)
        for old in sup:
            oc = cards_now.get(old)
            # помечать устаревшей можно только карточку своей области, которую модель видела целиком
            if oc is not None and oc.status != "superseded" and oc.area == ctx.area and \
                    (old in shown or old in ctx.touched):
                mark_superseded(ctx.cfg, oc, name, ctx.run_id)
                ctx.touched.add(old)
    if old_claims:   # старые версии (новые из куска или прежние опоры карточки) — из гейта и из опор
        ctx.ledger.link(old_claims, name, "SUPERSEDED_BY", ctx.run_id)
        ctx.ledger.set_claim_status(old_claims, L.SUPERSEDED)
    return name


def _needs_others(ctx, ops, cards_by_name):
    """id() операций UPDATE, которые теряют детали своей карточки, но эти детали есть в теле другой операции
    ответа: такие идут после остальных, чтобы приёмник уже был записан (перенос не зависит от порядка)."""
    bodies = {id(o): strip_markers(str(o.get("body") or ""))[0] for o in ops}
    out = set()
    for o in ops:
        name = str(o.get("name") or "")
        card = cards_by_name.get(name)
        if card is None or str(o.get("op", "")).upper() not in ("UPDATE", "SUPERSEDE", "ADD"):
            continue
        _, old_body = parse_frontmatter(card.text)
        lost, _ = lost_claims(ctx.ledger, name, old_body or "", bodies[id(o)])
        others = [b for k, b in bodies.items() if k != id(o)]
        if lost and others and lost_claims(ctx.ledger, name, old_body or "", bodies[id(o)],
                                           moved_to=dict(enumerate(others)))[1]:
            out.add(id(o))
    return out


def apply_ops(ctx, ops, chunk, shown, allowed=None):
    """ops — список dict от модели; chunk — утверждения этого вызова; shown — имена карточек, показанных целиком;
    allowed — id утверждений, показанных в промпте вместе с карточками."""
    chunk_ids = {c["id"] for c in chunk}
    cards_by_name = card_map(ctx.cfg)
    ctx.written = {}
    # сначала ADD (в т.ч. карточки, куда UPDATE переносит детали, и заглушки superseded), потом UPDATE/SUPERSEDE,
    # потом остальное
    order = {"ADD": 0, "UPDATE": 1, "SUPERSEDE": 2, "CONFLICT": 3, "NOOP": 4}
    area_text = None
    ops = [o for o in ops if isinstance(o, dict)]
    needs = _needs_others(ctx, ops, cards_by_name)
    for op in sorted(ops, key=lambda o: (order.get(str(o.get("op", "")).upper(), 9), id(o) in needs)):
        kind = str(op.get("op") or "").upper()
        op["op"] = kind
        if kind not in OPS:
            ctx.bump("op_unknown")
            continue
        if kind == "NOOP":
            ids = [i for i in _ids(op.get("claims")) if i in chunk_ids]
            # NOOP не должен терять детали: утверждение с атомами (флаг, путь, число…), которых нет ни в одной
            # карточке области, в первый раз уходит на повтор с причиной; повторный NOOP принимается
            if area_text is None:
                area_text = AT.norm_text("\n".join(c.text for c in iter_cards(ctx.cfg) if c.area == ctx.area))
            back = []
            for c in ctx.ledger.claims_by_ids(ids).values():
                at, _ = AT.claim_keys(c)
                if at and not (c.get("attempts") or 0) and not any(AT.has_atom(a, area_text) for a in at):
                    back.append(c)
            if back:
                why = ("сведи их в карточку или повтори NOOP, если это разовая деталь — NOOP утверждений с деталями, "
                       "которых нет ни в одной карточке: " + "; ".join(f"c{c['id']} ({AT.key_hint(c)})" for c in back[:8]))
                ctx.rejected.append(("NOOP", why))
                if ctx.record_rejections:
                    ctx.ledger.add_rejection("NOOP", ctx.area, why, ctx.run_id)
                ctx.rejected_ids |= {c["id"] for c in back}
                ctx.bump("claims_noop_uncovered", len(back))
                back_ids = {c["id"] for c in back}
                ids = [i for i in ids if i not in back_ids]
            # NOOP не перетирает уже сведённое в этом же ответе
            ids = [i for i, c in ctx.ledger.claims_by_ids(ids).items() if c["status"] in (L.NEW, L.REJECTED)]
            ctx.ledger.set_claim_status(ids, L.SKIPPED)
            ctx.ledger.link(ids, "", "NOOP", ctx.run_id)
            ctx.bump("op_NOOP")
            ctx.bump("claims_noop", len(ids))
            continue
        if kind == "CONFLICT":
            ids = [i for i in _ids(op.get("claims")) if i in chunk_ids]
            valid = _valid_claims(ctx.ledger, ids)
            what = str(op.get("conflict") or "").replace("\n", " ").strip()
            title = str(op.get("name") or ctx.area).replace("\n", " ").strip()[:80]
            if sensitive_hit(what + " " + title):
                ctx.bump("rejected")
                ctx.rejected.append((title, "CONFLICT: медицинское/контакт в тексте"))
                ctx.rejected_ids |= set(ids)
                continue
            if not valid or not what:
                ctx.bump("rejected")
                ctx.rejected.append((str(op.get("name") or "?"), "CONFLICT без утверждений/текста"))
                continue
            srcs = ", ".join(sources_for(list(valid.values())))
            ctx.conflicts.append(f"- [ ] {today()} {title} — {what} ({srcs})")
            # конфликт не теряется: строка в саму карточку, к которой он относится
            card = cards_by_name.get(title)
            if card is not None and not card.private and card.area == ctx.area and card.status != "superseded" \
                    and add_variant_line(ctx.cfg, card, f"- Вариант/конфликт ({today()}): {what} — см. CONFLICTS.md" +
                                         _keys_suffix(valid.values(), what)):
                ctx.touched.add(title)
                ctx.bump("conflict_lines_in_cards")
                cards_by_name = card_map(ctx.cfg)
            ctx.ledger.link(list(valid), str(op.get("name") or ""), "CONFLICT", ctx.run_id)
            ctx.ledger.set_claim_status(list(valid), L.CONSOLIDATED)
            ctx.bump("op_CONFLICT")
            continue
        apply_card_op(ctx, op, chunk_ids, cards_by_name, shown, allowed)
        cards_by_name = card_map(ctx.cfg)
    # утверждения отклонённых операций — на повтор (счётчик попыток), остальные неразобранные — пропущены
    still = {r["id"] for r in ctx.ledger.claims_by_ids(chunk_ids).values() if r["status"] in (L.NEW, L.REJECTED)}
    retry = still & ctx.rejected_ids
    if retry:
        ctx.ledger.bump_attempts(sorted(retry), L.REJECTED)
        ctx.bump("claims_rejected", len(retry))
    left = sorted(still - retry)
    if left:
        ctx.ledger.set_claim_status(left, L.SKIPPED)
        ctx.bump("claims_unassigned", len(left))


def append_conflicts(cfg, lines):
    if not lines:
        return 0
    p = os.path.join(cfg.root, "CONFLICTS.md")
    text = open(p, encoding="utf-8").read() if os.path.exists(p) else \
        "# Конфликты\n\n## Открытые\n\n## Закрытые\n"
    existing = set(text.splitlines())
    new = [line for line in lines if line not in existing]
    if not new:
        return 0
    lines_ = text.split("\n")
    try:
        h = next(i for i, ln in enumerate(lines_) if ln.strip() == "## Открытые")
    except StopIteration:
        lines_ += ["", "## Открытые", ""]
        h = len(lines_) - 2
    nxt = next((i for i in range(h + 1, len(lines_)) if lines_[i].startswith("## ")), len(lines_))
    block = [ln for ln in lines_[h + 1:nxt] if ln.strip()]
    lines_ = lines_[:h + 1] + [""] + block + new + [""] + lines_[nxt:]
    text = "\n".join(lines_).rstrip("\n") + "\n"
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return len(new)
