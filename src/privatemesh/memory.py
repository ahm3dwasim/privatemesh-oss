# Ported from the author's earlier private assistant; adapted for PrivateMesh.
"""Long-term memory: typed rows in SQLite, FTS5 recall, bi-temporal validity, owner consent.

The patterns ported from the prior assistant (its M8 memory layer), minus the notes-table
backfill that only made sense against that project's history:

- **typed rows** — one `memories` table, type as a column, plus an **FTS5 external-content
  index** with sync triggers, so search ships inside the SQLite we already run (zero new deps);
- **bi-temporal validity** — `forget` and `supersede` set `invalid_at`, they never delete:
  "what's true now" is `invalid_at IS NULL`, and the history stays inspectable;
- **relevance x recency x importance** scoring over the FTS candidate pool, with a per-type
  recency half-life so episodic memories fade faster than preferences;
- **secrets never enter memory** — every free-text field is scrubbed on write.

NEW here (not in the prior assistant): the **`approved` consent flag**. A memory is written
pending (`approved = 0`) and recall returns ONLY rows the owner has approved. Consent is a
write-time decision the owner revokes, not a filter the model can talk its way around.

Pure functions over a `sqlite3.Connection`. Every function is total — a bad query or an FTS
error yields [] / a no-op, never a raise — with the single deliberate exception of `approve`,
whose caller (the API) wants a real answer about whether the row existed.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import sqlite3
from dataclasses import dataclass

from .redact import redact_secrets

log = logging.getLogger(__name__)

MEMORY_TYPES = ("semantic", "preference", "episodic", "project", "entity", "decision", "workflow")
# Types stable enough to sit in a cached system prefix (they change rarely).
DURABLE_TYPES = ("preference", "semantic")
DURABLE_LIMIT = 15
RECALL_K = 6

# Recency half-life (days) per type — episodic memories fade fast, facts/preferences slowly.
RECENCY_HALFLIFE_DAYS = {"episodic": 30, "workflow": 45, "decision": 365,
                         "semantic": 365, "preference": 365, "project": 120, "entity": 180}
DEFAULT_HALFLIFE_DAYS = 180
# Composite score weights (relevance leads; recency + importance boost). Sum ~1.
W_RELEVANCE, W_RECENCY, W_IMPORTANCE = 0.5, 0.3, 0.2
_MAX_QUERY_TERMS = 12
_REDACT_RE = re.compile(r"^\s*(?:\[REDACTED[^\]]*\]\s*)+$")  # text that is ONLY redaction markers
# Drop the commonest words from the FTS query so recall matches on CONTENT, not on "the"/"is"/… —
# otherwise nearly every turn would spuriously match some memory and attach a noisy block.
_STOPWORDS = frozenset(
    "a an and are as at be by do does did for from had has have how i in is it its me my of on "
    "or so than that the this to up us was were what when where which who why will with you your "
    "can could would should not no yes if but about your".split()
)


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """A connection with row access by name (every helper below indexes rows by column)."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@dataclass(frozen=True)
class MemoryHit:
    id: int
    type: str
    text: str
    entity: str
    project: str
    importance: int
    score: float
    ts: str
    approved: bool = False


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the memories table, its FTS5 index, and the sync triggers. Idempotent."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories(
            id INTEGER PRIMARY KEY, ts TEXT, type TEXT, text TEXT,
            entity TEXT DEFAULT '', project TEXT DEFAULT '', importance INTEGER DEFAULT 3,
            source TEXT DEFAULT 'chat', valid_from TEXT, invalid_at TEXT,
            superseded_by INTEGER, origin TEXT DEFAULT '', approved INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_memories_valid ON memories(invalid_at);
        CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            text, entity, project, content='memories', content_rowid='id');
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, text, entity, project)
            VALUES (new.id, new.text, new.entity, new.project);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, text, entity, project)
            VALUES('delete', old.id, old.text, old.entity, old.project);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, text, entity, project)
            VALUES('delete', old.id, old.text, old.entity, old.project);
            INSERT INTO memories_fts(rowid, text, entity, project)
            VALUES (new.id, new.text, new.entity, new.project);
        END;
        """
    )
    conn.commit()


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def write_memory(conn: sqlite3.Connection, *, type: str, text: str, entity: str = "",
                 project: str = "", importance: int = 3, source: str = "chat", origin: str = "",
                 approved: bool = False, ts: str | None = None) -> int:
    """Insert one typed memory (secret-scrubbed, consent-pending by default). Returns the new id,
    or 0 if there was nothing worth storing (empty, or only redaction markers after scrubbing).
    Never raises."""
    try:
        text = redact_secrets((text or "").strip())
        if not text or _REDACT_RE.match(text):
            return 0
        # Secrets must NEVER enter memory — ALL free-text fields are scrubbed, not just `text`.
        # entity/project are model-fillable, indexed into FTS, and never deleted, so an
        # un-redacted secret here would persist forever.
        entity = redact_secrets((entity or "").strip())
        project = redact_secrets((project or "").strip())
        origin = redact_secrets((origin or "").strip())
        stamp = ts or _now_iso()
        typ = type if type in MEMORY_TYPES else "semantic"
        imp = max(1, min(5, int(importance) if isinstance(importance, int) else 3))
        cur = conn.execute(
            "INSERT INTO memories(ts,type,text,entity,project,importance,source,valid_from,"
            "invalid_at,superseded_by,origin,approved) VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?,?)",
            (stamp, typ, text, entity, project, imp, source, stamp, origin, 1 if approved else 0),
        )
        conn.commit()
        return int(cur.lastrowid)
    except Exception as exc:
        log.warning("memories.write_failed: %r", exc)
        return 0


def approve(conn: sqlite3.Connection, memory_id: int, *, approved: bool = True) -> bool:
    """Grant or revoke the owner's consent for one memory. Returns False if no such row.
    Raises only if the store itself is broken — the caller is answering a yes/no question."""
    cur = conn.execute("UPDATE memories SET approved=? WHERE id=?",
                       (1 if approved else 0, int(memory_id)))
    conn.commit()
    return cur.rowcount > 0


def content_terms(text: str) -> set[str]:
    """The meaningful (non-stopword, len>1) word tokens of a text — used both to build the FTS query
    and to gate per-turn recall relevance (how many query terms a memory actually shares)."""
    return {t for t in re.findall(r"\w+", (text or "").lower()) if t not in _STOPWORDS and len(t) > 1}


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH: quoted word tokens joined by OR. '' when no usable
    token (caller then skips FTS). Quoting each token avoids FTS5 syntax errors on punctuation.
    Order-preserving + deduped so the same query always yields the same MATCH (determinism)."""
    seen: set[str] = set()
    tokens: list[str] = []
    for t in re.findall(r"\w+", (query or "").lower()):
        if t not in _STOPWORDS and len(t) > 1 and t not in seen:
            seen.add(t)
            tokens.append(t)
        if len(tokens) >= _MAX_QUERY_TERMS:
            break
    return " OR ".join(f'"{t}"' for t in tokens)


def _recency(ts: str, now: _dt.datetime, mem_type: str) -> float:
    try:
        age_days = max(0.0, (now - _dt.datetime.fromisoformat(ts)).total_seconds() / 86400.0)
    except Exception:
        return 0.5
    half = RECENCY_HALFLIFE_DAYS.get(mem_type, DEFAULT_HALFLIFE_DAYS)
    return 0.5 ** (age_days / half)


def _hit(r: sqlite3.Row, score: float) -> MemoryHit:
    return MemoryHit(id=int(r["id"]), type=r["type"], text=r["text"], entity=r["entity"] or "",
                     project=r["project"] or "", importance=int(r["importance"] or 3),
                     score=score, ts=r["ts"] or "", approved=bool(r["approved"]))


def recall(conn: sqlite3.Connection, query: str, *, types=None, entity: str | None = None,
           project: str | None = None, k: int = RECALL_K,
           now: _dt.datetime | None = None) -> list[MemoryHit]:
    """FTS5 keyword recall over APPROVED and VALID memories, re-ranked by
    relevance x recency x importance. Returns up to `k` hits, best first. Never raises ([] on any
    error, or when the query has no usable term).

    The `approved = 1 AND invalid_at IS NULL` clause is not a parameter: it is the whole point.
    A pending or forgotten memory is invisible to the model, not merely deprioritised."""
    match = _fts_query(query)
    if not match:
        return []
    now = now or _dt.datetime.now()
    clauses = ["m.invalid_at IS NULL", "m.approved = 1", "memories_fts MATCH ?"]
    params: list = [match]
    if types:
        clauses.append("m.type IN (%s)" % ",".join("?" * len(types)))
        params += list(types)
    if entity is not None:
        clauses.append("m.entity = ?")
        params.append(entity)
    if project is not None:
        clauses.append("m.project = ?")
        params.append(project)
    pool = max(k * 3, 12)
    params.append(pool)
    sql = (
        "SELECT m.id, m.type, m.text, m.entity, m.project, m.importance, m.ts, m.approved, "
        "bm25(memories_fts) AS rank FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
        f"WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT ?"
    )
    try:
        rows = list(conn.execute(sql, params))
    except Exception as exc:
        log.warning("memories.recall_failed: %r", exc)
        return []
    if not rows:
        return []
    # Rank-normalize relevance within the candidate pool (bm25 is best-first already), then blend.
    n = len(rows)
    hits = []
    for i, r in enumerate(rows):
        relevance = 1.0 - (i / n) if n > 1 else 1.0
        recency = _recency(r["ts"], now, r["type"])
        importance = int(r["importance"] or 3) / 5.0
        score = W_RELEVANCE * relevance + W_RECENCY * recency + W_IMPORTANCE * importance
        hits.append(_hit(r, round(score, 4)))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k]


def forget_matching(conn: sqlite3.Connection, description: str, *, at: str | None = None) -> list[MemoryHit]:
    """Invalidate the single best-matching VALID memory (bi-temporal — sets `invalid_at`, never
    deletes). Returns what was invalidated (empty if nothing matched). Conservative on purpose:
    forgetting is scoped to the closest match, and it's reversible. Never raises.

    Deliberately searches APPROVED rows: an unapproved memory was never part of the model's world,
    so a forget request that only matches one of those should not be reported as a real change."""
    hits = recall(conn, description, k=1)
    if not hits:
        return []
    try:
        conn.execute("UPDATE memories SET invalid_at=? WHERE id=? AND invalid_at IS NULL",
                     (at or _now_iso(), hits[0].id))
        conn.commit()
    except Exception as exc:
        log.warning("memories.forget_failed: %r", exc)
        return []
    return hits


def forget_id(conn: sqlite3.Connection, memory_id: int, *, at: str | None = None) -> bool:
    """Invalidate one exact memory by id — the owner pointing at a row rather than describing it.
    Bi-temporal, like `forget_matching`: the row stays, its `invalid_at` is set. Never raises."""
    try:
        cur = conn.execute("UPDATE memories SET invalid_at=? WHERE id=? AND invalid_at IS NULL",
                           (at or _now_iso(), int(memory_id)))
        conn.commit()
        return cur.rowcount > 0
    except Exception as exc:
        log.warning("memories.forget_id_failed: %r", exc)
        return False


def supersede(conn: sqlite3.Connection, old_id: int, new_id: int, *, at: str | None = None) -> bool:
    """Bi-temporal UPDATE: invalidate `old_id` and record that `new_id` replaced it. Only affects a
    currently-valid row (so it can't double-supersede). Returns True if it superseded. Never raises."""
    try:
        cur = conn.execute(
            "UPDATE memories SET invalid_at=?, superseded_by=? WHERE id=? AND invalid_at IS NULL",
            (at or _now_iso(), int(new_id), int(old_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception as exc:
        log.warning("memories.supersede_failed: %r", exc)
        return False


def get_memory(conn: sqlite3.Connection, memory_id: int) -> MemoryHit | None:
    """One memory by id, valid or not, approved or not — the owner's view. Never raises."""
    try:
        r = conn.execute(
            "SELECT id,type,text,entity,project,importance,ts,approved FROM memories WHERE id=?",
            (int(memory_id),)).fetchone()
    except Exception:
        return None
    return _hit(r, 0.0) if r else None


def counts_by_type(conn: sqlite3.Connection) -> dict:
    """{type: count} over VALID memories + 'total' and 'pending' (awaiting consent). Never raises."""
    try:
        rows = conn.execute(
            "SELECT type, COUNT(*) n FROM memories WHERE invalid_at IS NULL GROUP BY type").fetchall()
        out = {r["type"]: int(r["n"]) for r in rows}
        out["total"] = sum(out.values())
        out["pending"] = int(conn.execute(
            "SELECT COUNT(*) FROM memories WHERE invalid_at IS NULL AND approved=0").fetchone()[0])
        return out
    except Exception:
        return {"total": 0, "pending": 0}


def durable_memories(conn: sqlite3.Connection, limit: int = DURABLE_LIMIT) -> list[MemoryHit]:
    """Top APPROVED, valid preference/semantic memories for a stable system prefix, importance then
    recency. These change rarely, so they can live in the cached prefix. Never raises."""
    try:
        rows = conn.execute(
            "SELECT id,type,text,entity,project,importance,ts,approved FROM memories "
            "WHERE invalid_at IS NULL AND approved=1 AND type IN (%s) "
            "ORDER BY importance DESC, id DESC LIMIT ?" % ",".join("?" * len(DURABLE_TYPES)),
            (*DURABLE_TYPES, int(limit)),
        ).fetchall()
    except Exception:
        return []
    return [_hit(r, 0.0) for r in rows]


def recent_memories(conn: sqlite3.Connection, limit: int = 8, *, approved_only: bool = False) -> list[MemoryHit]:
    """Most-recently-written VALID memories, newest first. Set `approved_only=False` (the default)
    for the owner's memory panel, which must show pending rows so they can be approved. Never raises."""
    try:
        sql = ("SELECT id,type,text,entity,project,importance,ts,approved FROM memories "
               "WHERE invalid_at IS NULL" + (" AND approved=1" if approved_only else "") +
               " ORDER BY id DESC LIMIT ?")
        rows = conn.execute(sql, (int(limit),)).fetchall()
    except Exception:
        return []
    return [_hit(r, 0.0) for r in rows]


def forgotten_memories(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Memories the owner has forgotten, newest first, each carrying its `invalid_at`.

    `recent_memories` deliberately hides these — a forgotten memory must not be recallable. But the
    owner's memory panel has to keep showing them, struck through, because the product's claim is
    not only "I forgot it" but "here is the row proving I did, and when". So this reads the other
    half of the table, as plain dicts (the panel renders `invalid_at`, which `MemoryHit` does not
    carry). Never raises."""
    try:
        rows = conn.execute(
            "SELECT id,type,text,entity,project,importance,ts,approved,invalid_at FROM memories "
            "WHERE invalid_at IS NOT NULL ORDER BY invalid_at DESC, id DESC LIMIT ?",
            (int(limit),)).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]
