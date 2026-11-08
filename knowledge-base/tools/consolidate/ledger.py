"""Реестр сводчика: state/ledger.sqlite. Единица → хэш → утверждения → карточки.

Идемпотентность: единица с тем же sha и той же версией промпта стадии 1 повторно не извлекается;
утверждение, уже сведённое (status != 'new'), повторно в стадию 2 не идёт.

Смена версии промпта стадии 1: кэш (extractions) ключуется (sha, prompt_ver), поэтому новая версия требует
переизвлечения, но старые утверждения единицы трогаются ТОЛЬКО когда единица реально переизвлечена новой
версией: ещё не сведённые (new/rejected) → `reextracted` (их заменяют новые), сведённые остаются опорой карточек.
Единицы, до которых новая версия ещё не дошла (лимит батчей), живут на старых утверждениях как раньше.
"""
import datetime as dt
import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
  uid TEXT PRIMARY KEY, src TEXT, kind TEXT, origin TEXT, sha TEXT, date TEXT,
  first_seen TEXT, last_seen TEXT, alias_of TEXT
);
CREATE TABLE IF NOT EXISTS extractions (
  sha TEXT, prompt_ver TEXT, uid TEXT, status TEXT, n_claims INTEGER, n_dropped INTEGER,
  run_id TEXT, at TEXT, PRIMARY KEY (sha, prompt_ver)
);
CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_sha TEXT, uid TEXT, src TEXT, origin TEXT, unit_date TEXT,
  text TEXT, kind TEXT, topic TEXT, area TEXT, date TEXT, quote TEXT,
  sensitive INTEGER, quote_ok INTEGER, status TEXT, run_id TEXT, created_at TEXT, prompt_ver TEXT
);
CREATE INDEX IF NOT EXISTS claims_status ON claims(status, area);
CREATE INDEX IF NOT EXISTS claims_unit ON claims(unit_sha);
CREATE INDEX IF NOT EXISTS claims_uid ON claims(uid);
CREATE TABLE IF NOT EXISTS claim_cards (
  claim_id INTEGER, card TEXT, op TEXT, run_id TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS rejections (
  card TEXT, area TEXT, reason TEXT, run_id TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS card_topics (
  card TEXT, area TEXT, topic TEXT, PRIMARY KEY (card, topic)
);
"""

# статусы утверждений
NEW, CONSOLIDATED, SKIPPED, STALE, SENSITIVE, NO_QUOTE, SUPERSEDED, REJECTED, REEXTRACTED = (
    "new", "consolidated", "skipped", "stale", "sensitive", "no_quote", "superseded", "rejected", "reextracted")
MAX_ATTEMPTS = 3   # сколько раз утверждение идёт в стадию 2 после отказа/сбоя


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new = not os.path.exists(path)
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(claims)")}
        if "attempts" not in cols:
            self.db.execute("ALTER TABLE claims ADD COLUMN attempts INTEGER DEFAULT 0")
        self.db.commit()
        if new:
            os.chmod(path, 0o600)

    def close(self):
        self.db.close()

    # ---------- единицы ----------

    def sync_units(self, units, aliases):
        """Записать текущие единицы. Утверждения прежнего хэша, удалённых единиц и копий → stale;
        возврат к прежнему хэшу (A→B→A) оживляет его утверждения. → число изменённых единиц."""
        ts = now()
        changed = 0
        cur = self.db.cursor()
        live = {u.uid for u in units}
        active = (NEW, CONSOLIDATED, SKIPPED, REJECTED)
        for u in units:
            row = cur.execute("SELECT sha FROM units WHERE uid=?", (u.uid,)).fetchone()
            if row is None:
                cur.execute("INSERT INTO units VALUES (?,?,?,?,?,?,?,?,NULL)",
                            (u.uid, u.src, u.kind, u.origin, u.sha, u.date, ts, ts))
                changed += 1
            else:
                if row["sha"] != u.sha:
                    changed += 1
                cur.execute("UPDATE units SET sha=?, date=?, src=?, origin=?, last_seen=?, alias_of=NULL "
                            "WHERE uid=?", (u.sha, u.date, u.src, u.origin, ts, u.uid))
            # тот же текст раньше жил под другим uid (сменилась основная копия) — утверждения переходят сюда
            for r in cur.execute("SELECT DISTINCT uid FROM claims WHERE unit_sha=? AND uid!=?",
                                 (u.sha, u.uid)).fetchall():
                if r["uid"] not in live:
                    cur.execute("UPDATE claims SET uid=?, src=?, origin=? WHERE unit_sha=? AND uid=?",
                                (u.uid, u.src, u.origin, u.sha, r["uid"]))
            # прежние хэши единицы больше не подтверждаются сырьём
            cur.execute("UPDATE claims SET status=? WHERE uid=? AND unit_sha!=? AND status IN (?,?,?,?)",
                        (STALE, u.uid, u.sha, *active))
            # возврат к прежнему хэшу: оживают утверждения только последнего извлечения этого хэша
            last = cur.execute("SELECT prompt_ver FROM extractions WHERE sha=? AND status='ok' ORDER BY at DESC "
                               "LIMIT 1", (u.sha,)).fetchone()
            if last is None:
                cur.execute("UPDATE claims SET status=? WHERE uid=? AND unit_sha=? AND status=? AND quote_ok=1 "
                            "AND sensitive=0", (NEW, u.uid, u.sha, STALE))
            else:
                cur.execute("UPDATE claims SET status=? WHERE uid=? AND unit_sha=? AND status=? AND quote_ok=1 "
                            "AND sensitive=0 AND COALESCE(prompt_ver,'')=?", (NEW, u.uid, u.sha, STALE, last[0]))
                # старые версии, на которые опираются карточки, снова опоры (без повторной сводки)
                cur.execute("UPDATE claims SET status=? WHERE uid=? AND unit_sha=? AND status=? AND quote_ok=1 "
                            "AND sensitive=0 AND COALESCE(prompt_ver,'')!=? AND id IN (SELECT claim_id FROM "
                            "claim_cards WHERE card!='' AND op NOT IN ('NOOP','SUPERSEDED_BY'))",
                            (CONSOLIDATED, u.uid, u.sha, STALE, last[0]))
        # единицы, пропавшие из raw/ или ставшие копиями, — их утверждения устарели
        for r in cur.execute("SELECT DISTINCT uid FROM claims WHERE status IN (?,?,?,?)", active).fetchall():
            if r["uid"] not in live:
                cur.execute("UPDATE claims SET status=? WHERE uid=? AND status IN (?,?,?,?)",
                            (STALE, r["uid"], *active))
        for uid, main in aliases.items():
            cur.execute("INSERT INTO units(uid, alias_of, last_seen) VALUES (?,?,?) "
                        "ON CONFLICT(uid) DO UPDATE SET alias_of=excluded.alias_of, last_seen=excluded.last_seen",
                        (uid, main, ts))
        self.db.commit()
        return changed

    def needs_extraction(self, unit, prompt_ver):
        row = self.db.execute("SELECT status FROM extractions WHERE sha=? AND prompt_ver=?",
                              (unit.sha, prompt_ver)).fetchone()
        return row is None or row["status"] != "ok"

    # ---------- утверждения ----------

    def add_extraction(self, unit, prompt_ver, claims, run_id):
        """claims: список dict с полями утверждения и quote_ok. Атомарно на единицу."""
        ts = now()
        cur = self.db.cursor()
        cur.execute("DELETE FROM claims WHERE unit_sha=? AND prompt_ver=? AND status=?",
                    (unit.sha, prompt_ver, NEW))
        # старая версия промпта по этой же единице: несведённое заменяется новым извлечением
        cur.execute("UPDATE claims SET status=? WHERE unit_sha=? AND COALESCE(prompt_ver,'')!=? AND status IN (?,?)",
                    (REEXTRACTED, unit.sha, prompt_ver, NEW, REJECTED))
        dropped = 0
        for c in claims:
            if not c.get("quote_ok"):
                status = NO_QUOTE
                dropped += 1
            elif c.get("sensitive"):
                status = SENSITIVE
            else:
                status = NEW
            cur.execute(
                "INSERT INTO claims(unit_sha, uid, src, origin, unit_date, text, kind, topic, area, date, "
                "quote, sensitive, quote_ok, status, run_id, created_at, prompt_ver) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (unit.sha, unit.uid, unit.src, unit.origin, unit.date, c.get("text", ""),
                 c.get("kind", ""), c.get("topic", ""), c.get("area", ""), c.get("date", ""),
                 c.get("quote", ""), 1 if c.get("sensitive") else 0, 1 if c.get("quote_ok") else 0,
                 status, run_id, ts, prompt_ver))
        cur.execute("INSERT OR REPLACE INTO extractions VALUES (?,?,?,?,?,?,?,?)",
                    (unit.sha, prompt_ver, unit.uid, "ok", len(claims), dropped, run_id, ts))
        self.db.commit()
        return dropped

    def mark_extraction_error(self, unit, prompt_ver, run_id):
        self.db.execute("INSERT OR IGNORE INTO extractions VALUES (?,?,?,?,?,?,?,?)",
                        (unit.sha, prompt_ver, unit.uid, "error", 0, 0, run_id, now()))
        self.db.commit()

    def pending_claims(self, areas):
        q = ("SELECT * FROM claims WHERE status IN (?,?) AND COALESCE(attempts,0) < ? AND area IN (%s) "
             "ORDER BY area, topic, id" % ",".join("?" * len(areas)))
        return [dict(r) for r in self.db.execute(q, (NEW, REJECTED, MAX_ATTEMPTS, *areas))]

    def bump_attempts(self, ids, status=None):
        """Неудачная попытка стадии 2 (отказ карточки или сбой вызова): счётчик +1, статус → rejected."""
        if status:
            self.db.executemany("UPDATE claims SET attempts=COALESCE(attempts,0)+1, status=? WHERE id=?",
                                [(status, i) for i in ids])
        else:
            self.db.executemany("UPDATE claims SET attempts=COALESCE(attempts,0)+1 WHERE id=?",
                                [(i,) for i in ids])
        self.db.commit()

    def claims_by_ids(self, ids):
        if not ids:
            return {}
        out = {}
        ids = list(ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = "SELECT * FROM claims WHERE id IN (%s)" % ",".join("?" * len(chunk))
            for r in self.db.execute(q, chunk):
                out[r["id"]] = dict(r)
        return out

    def set_claim_status(self, ids, status):
        self.db.executemany("UPDATE claims SET status=? WHERE id=?", [(status, i) for i in ids])
        self.db.commit()

    def link(self, claim_ids, card, op, run_id):
        ts = now()
        self.db.executemany("INSERT INTO claim_cards VALUES (?,?,?,?,?)",
                            [(i, card, op, run_id, ts) for i in claim_ids])
        self.db.commit()

    def add_rejection(self, card, area, reason, run_id):
        self.db.execute("INSERT INTO rejections VALUES (?,?,?,?,?)", (card, area, reason[:700], run_id, now()))
        self.db.commit()

    def last_rejections(self, area, limit=15):
        """Отказы последнего прогона, где они были, — подсказка модели при повторе."""
        row = self.db.execute("SELECT run_id FROM rejections WHERE area=? ORDER BY at DESC LIMIT 1", (area,)).fetchone()
        if not row:
            return []
        return [(r["card"], r["reason"]) for r in self.db.execute(
            "SELECT card, reason FROM rejections WHERE area=? AND run_id=? LIMIT ?", (area, row["run_id"], limit))]

    def card_claims(self, card):
        return [r["claim_id"] for r in self.db.execute(
            "SELECT DISTINCT claim_id FROM claim_cards WHERE card=?", (card,))]

    def cards_for_uids(self, uids):
        """Карточки, уже опирающиеся на утверждения этих единиц (любой версии промпта)."""
        uids = list(uids)
        out = set()
        for i in range(0, len(uids), 500):
            chunk = uids[i:i + 500]
            q = ("SELECT DISTINCT cc.card FROM claim_cards cc JOIN claims c ON c.id=cc.claim_id "
                 "WHERE cc.card!='' AND c.uid IN (%s)" % ",".join("?" * len(chunk)))
            out |= {r["card"] for r in self.db.execute(q, chunk)}
        return out

    def set_card_topics(self, card, area, topics):
        self.db.executemany("INSERT OR IGNORE INTO card_topics VALUES (?,?,?)",
                            [(card, area, t) for t in topics if t])
        self.db.commit()

    def cards_for_topics(self, area, topics):
        if not topics:
            return set()
        q = "SELECT DISTINCT card FROM card_topics WHERE area=? AND topic IN (%s)" % ",".join("?" * len(topics))
        return {r["card"] for r in self.db.execute(q, (area, *topics))}

    def stats(self):
        out = {}
        for r in self.db.execute("SELECT status, count(*) n FROM claims GROUP BY status"):
            out[r["status"]] = r["n"]
        out["units"] = self.db.execute("SELECT count(*) FROM units WHERE alias_of IS NULL").fetchone()[0]
        out["extracted"] = self.db.execute("SELECT count(*) FROM extractions WHERE status='ok'").fetchone()[0]
        return out

    def dump_json(self, sql, args=()):
        return json.dumps([dict(r) for r in self.db.execute(sql, args)], ensure_ascii=False)
