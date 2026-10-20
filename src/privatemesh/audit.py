# Ported from the author's prior private assistant (Jarvis); adapted for PrivateMesh.
"""The audit trail — an append-only, hash-chained store, and the receipt the product promises.

Ported from the prior assistant's `kernel/audit.py`: the `PolicyRecord` shape (who / which tool /
what risk / what decision / approved / executed / ok / how long) emitted at the single choke point,
which was the broker here.

IMPROVED, deliberately, in two ways that module got wrong for a product whose pitch is proof:

1. **Append-only and hash-chained.** Every row carries `sha256(prev_hash + canonical_json(row))`.
   Changing any field of any past row breaks every hash after it, and `verify()` reports the FIRST
   broken row rather than a boolean. The prior assistant's store was a plain mutable table.
2. **A failed write surfaces.** The prior assistant swallowed audit-write errors
   ("best-effort — a turn must never fail because of it"). That is the wrong trade for this
   product: an action that cannot be receipted must not be reported as done. `record()` raises
   `AuditError`, and the broker lets it propagate.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

GENESIS = "0" * 64
_COLUMNS = ("id", "ts", "task_id", "event", "detail")

# A writer that loses the race for the write lock waits this long before trying again. WAL plus
# BEGIN IMMEDIATE already serializes the chain-head read with the insert; the retry exists so a
# competing writer is a short pause rather than a failed action (see the module docstring).
_BUSY_RETRIES = 60
_BUSY_SLEEP_S = 0.01

# Where the signed tip witness and its key live by default: beside the database, never inside it —
# a witness the store can rewrite is not a witness.
CHECKPOINT_SUFFIX = ".checkpoint"
KEY_SUFFIX = ".auditkey"
AUDIT_KEY_ENV = "PM_AUDIT_KEY"


class AuditError(RuntimeError):
    """An audit write failed. Raised, never swallowed — an unreceipted action is not a done action."""


class CheckpointError(AuditError):
    """The external witness for the chain tip is missing, unreadable, or does not match the store."""


def canonical(payload: dict) -> str:
    """Deterministic JSON for hashing: sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def row_hash(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256((prev_hash + canonical(payload)).encode("utf-8")).hexdigest()


def _is_busy(exc: sqlite3.Error) -> bool:
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


def _rollback(conn: sqlite3.Connection) -> None:
    """Best-effort rollback: after a failed BEGIN there is no transaction to roll back."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _signature(key: bytes, payload: dict) -> str:
    return hmac.new(key, canonical(payload).encode("utf-8"), hashlib.sha256).hexdigest()


class AuditCheckpoint:
    """The chain tip, witnessed OUTSIDE the database.

    A hash chain proves that no *interior* row was changed: every row commits to its predecessor,
    so removing row 2 leaves row 3 pointing at a hash that is no longer there. It cannot prove
    anything about the LAST row, because a deleted tip has no successor to notice it. An attacker
    who can write to the database can therefore discard the newest receipt — precisely the one
    that records what just happened — and `verify()` walks a shorter chain and reports success.

    The witness closes that hole. After every append the log writes `(row_count, tip_hash)` to a
    separate file, signed with HMAC-SHA256 under a key the database does not contain. `verify()`
    then cross-checks the store against it and fails on a missing file, a bad signature, a count
    that disagrees in either direction, or a tip that does not match — so a truncated chain is a
    loud failure rather than a quiet one.

    Key handling: `PM_AUDIT_KEY` if set, otherwise a 0600 key file generated on first use beside
    the checkpoint. The key is read, never logged and never written into the repository.
    """

    def __init__(self, db_path: str, *, path: str | None = None, key: bytes | str | None = None):
        self.path = Path(path or f"{db_path}{CHECKPOINT_SUFFIX}")
        self.key = self._resolve_key(key)
        self._last_tip: str | None = None

    def _resolve_key(self, key: bytes | str | None) -> bytes:
        if isinstance(key, bytes) and key:
            return key
        if isinstance(key, str) and key:
            return key.encode("utf-8")
        env = (os.environ.get(AUDIT_KEY_ENV) or "").strip()
        if env:
            return env.encode("utf-8")
        key_file = Path(f"{self.path}{KEY_SUFFIX}")
        try:
            if key_file.exists():
                material = key_file.read_text().strip()
                if material:
                    return material.encode("utf-8")
            material = secrets.token_hex(32)
            fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, material.encode("utf-8"))
            finally:
                os.close(fd)
            return material.encode("utf-8")
        except FileExistsError:      # lost a race with another first-run writer; use theirs
            return key_file.read_text().strip().encode("utf-8")
        except OSError as exc:
            raise CheckpointError(
                f"could not create the audit checkpoint key at {key_file}: {exc!r}") from exc

    def read(self) -> dict | None:
        """The signed checkpoint, or None when the file does not exist.

        Raises `CheckpointError` for a file that exists but cannot be trusted — an unreadable or
        forged witness is a failure to report, never a reason to fall back to trusting the store."""
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CheckpointError(f"could not read the audit checkpoint at {self.path}: {exc!r}") from exc
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise CheckpointError(
                f"the audit checkpoint at {self.path} is not readable JSON: {exc}") from exc
        if not isinstance(record, dict) or not isinstance(record.get("sig"), str):
            raise CheckpointError(f"the audit checkpoint at {self.path} is malformed (no signature)")
        payload = {k: v for k, v in record.items() if k != "sig"}
        if not hmac.compare_digest(_signature(self.key, payload), record["sig"]):
            raise CheckpointError(
                f"the audit checkpoint at {self.path} failed its HMAC — it was edited, or it was "
                f"signed with a different key than {AUDIT_KEY_ENV} provides")
        if not isinstance(payload.get("rows"), int) or not isinstance(payload.get("tip"), str):
            raise CheckpointError(f"the audit checkpoint at {self.path} is missing rows/tip")
        return payload

    def write(self, *, rows: int, tip: str) -> None:
        """Publish a new signed witness. Atomic: a reader never sees a half-written file.

        The temp file is unique per writer. Two processes sharing an audit database also share
        this path, and a fixed `.tmp` name meant one writer's `os.replace` could land between
        another's `open` and `write` — the checkpoint then failed with ENOENT and took a
        committed audit row down with it."""
        payload = {"rows": int(rows), "tip": tip, "prev_tip": self._last_tip,
                   "ts": _dt.datetime.now().isoformat(timespec="seconds")}
        record = {**payload, "sig": _signature(self.key, payload)}
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, canonical(record).encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(str(tmp), str(self.path))
        except OSError as exc:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise CheckpointError(
                f"the audit row could not be checkpointed at {self.path}: {exc!r}") from exc
        self._last_tip = tip

    def snapshot(self) -> tuple[int, str] | None:
        """The currently published (rows, tip), or None.

        Deliberately unverified and total: this exists so a failed transaction can put the file
        back the way it found it, and a witness that cannot be read is a witness to remove, not
        an error to raise on the way out of another failure."""
        try:
            record = json.loads(self.path.read_text())
            return int(record["rows"]), str(record["tip"])
        except Exception:
            return None

    def restore(self, snapshot: tuple[int, str] | None) -> None:
        """Undo a publish whose transaction did not commit, so the witness never attests a row
        the store does not have."""
        if snapshot is None:
            try:
                os.unlink(str(self.path))
            except OSError:
                pass
            return
        self.write(rows=snapshot[0], tip=snapshot[1])


@dataclass(frozen=True)
class PolicyRecord:
    """One gated action's full authority trail. Bools are tri-state where the action may not reach
    that stage: `approved` is None when approval wasn't required, `ok` is None when the skill never
    ran. `tier`/`risk` are the human names ("act", "MEDIUM")."""

    task_id: str
    tool: str
    gate_name: str
    tier: str
    risk: str
    sensitive: bool
    decision: str       # allow | needs_approval | deny
    reason: str
    approved: bool | None    # None = approval not required; True/False = the owner's answer
    executed: bool           # did the skill function actually run?
    ok: bool | None          # None = never ran; else the ToolResult.ok
    duration_ms: int | None  # None = never ran
    detail: str = ""         # free-form extra, e.g. the approval id or the skill's summary

    def as_row(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AuditVerification:
    ok: bool
    broken_id: int | None = None
    note: str = ""


class AuditLog:
    """Append-only, hash-chained event store over SQLite.

    One `record()` per event. The chain is computed under a lock so two concurrent writers cannot
    interleave a prev_hash; the id is chosen explicitly for the same reason (an AUTOINCREMENT id
    would be assigned after the hash that commits to it)."""

    def __init__(self, path: str = ":memory:", *, checkpoint_path: str | None = None,
                 audit_key: bytes | str | None = None, checkpoint: bool | None = None) -> None:
        self._lock = threading.Lock()
        # A file-backed log gets an external tip witness; an in-memory one has nothing to witness
        # (and no second reader to deceive), so `verify()` stays a pure chain walk there.
        if checkpoint is None:
            checkpoint = path != ":memory:"
        self.checkpoint: AuditCheckpoint | None = (
            AuditCheckpoint(path, path=checkpoint_path, key=audit_key) if checkpoint else None)
        try:
            self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0,
                                         isolation_level=None)   # explicit transactions below
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '',
                    event TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                    prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_audit_task ON audit(task_id);
                CREATE INDEX IF NOT EXISTS idx_audit_event ON audit(event);
                """
            )
            self._conn.commit()
            empty = self._conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0
        except sqlite3.Error as exc:
            raise AuditError(f"could not open the audit store: {exc!r}") from exc
        # A brand-new, EMPTY log is witnessed at birth (rows=0, tip=GENESIS), so a fresh install
        # verifies clean. A log that already holds rows is never re-witnessed here: its missing
        # checkpoint stays a verify() failure, which is the whole point of the witness. (Wiping
        # BOTH the store and the checkpoint looks like a fresh install; only an external anchor
        # could tell those apart, and README says so.)
        if self.checkpoint is not None and empty and self.checkpoint.read() is None:
            self.checkpoint.write(rows=0, tip=GENESIS)

    # ---- write -------------------------------------------------------------------------------
    def record(self, event: str, *, task_id: str = "", detail=None, ts: str | None = None) -> dict:
        """Append one event. Returns the stored row. Raises `AuditError` on any failure — this is
        the deliberate difference from the prior assistant, which logged and dropped."""
        if not event:
            raise AuditError("an audit event needs a name")
        detail_text = detail if isinstance(detail, str) else canonical(detail if detail is not None else {})
        try:
            with self._lock:
                snapshot = self.checkpoint.snapshot() if self.checkpoint is not None else None
                try:
                    payload, prev_hash, digest = self._append(
                        event, task_id or "", detail_text,
                        ts or _dt.datetime.now().isoformat(timespec="seconds"))
                except BaseException:
                    if self.checkpoint is not None:
                        try:
                            self.checkpoint.restore(snapshot)
                        except Exception:      # a failed restore must not mask the real failure
                            pass
                    raise
        except sqlite3.Error as exc:
            raise AuditError(f"audit write failed for event '{event}': {exc!r}") from exc
        return {**payload, "prev_hash": prev_hash, "hash": digest}

    def _append(self, event: str, task_id: str, detail_text: str, ts: str) -> tuple[dict, str, str]:
        """Select the chain head and append under ONE write transaction.

        `BEGIN IMMEDIATE` takes the database's write lock before the head is read, so two writers
        — two `AuditLog` objects, or two processes on the same file — cannot both read the same
        tip and then disagree about what comes next. The lock is held across read + insert, which
        is why a losing writer gets a clean, retryable refusal instead of a duplicate id."""
        last_exc: sqlite3.Error | None = None
        for attempt in range(_BUSY_RETRIES):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                last_exc = exc
                time.sleep(_BUSY_SLEEP_S * min(attempt + 1, 20))
                continue
            try:
                last = self._conn.execute(
                    "SELECT id, hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
                prev_hash = last["hash"] if last else GENESIS
                next_id = (int(last["id"]) + 1) if last else 1
                payload = {"id": next_id, "ts": ts, "task_id": task_id, "event": event,
                           "detail": detail_text}
                digest = row_hash(prev_hash, payload)
                self._conn.execute(
                    "INSERT INTO audit(id,ts,task_id,event,detail,prev_hash,hash) VALUES(?,?,?,?,?,?,?)",
                    (payload["id"], payload["ts"], payload["task_id"], payload["event"],
                     payload["detail"], prev_hash, digest))
                if self.checkpoint is not None:
                    # Published INSIDE the write transaction, while this writer holds the
                    # database's write lock. That is what orders checkpoint files the same way
                    # as the rows they attest: without it, two writers could commit rows 89 and
                    # 90 and then publish their witnesses in the opposite order, leaving the
                    # store permanently "mismatched" against a stale but honestly-signed file.
                    rows = int(self._conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0])
                    self.checkpoint.write(rows=rows, tip=digest)
                self._conn.execute("COMMIT")
                return payload, prev_hash, digest
            except BaseException:
                _rollback(self._conn)
                raise
        raise AuditError(
            f"audit write failed for event '{event}': the write lock stayed busy for "
            f"{_BUSY_RETRIES} attempts ({last_exc!r})")

    # ---- read --------------------------------------------------------------------------------
    def rows(self, *, task_id: str | None = None, event: str | None = None, limit: int | None = None) -> list[dict]:
        clauses, params = [], []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if event is not None:
            clauses.append("event = ?")
            params.append(event)
        sql = "SELECT * FROM audit" + (f" WHERE {' AND '.join(clauses)}" if clauses else "") + " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        try:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]
        except sqlite3.Error as exc:
            raise AuditError(f"audit read failed: {exc!r}") from exc

    def verify(self) -> AuditVerification:
        """Re-walk the chain. Returns the FIRST broken row (id + why), or ok with the row count.

        Detects a mutated field (the recomputed digest no longer matches the stored one) and a
        deleted row (the next row's `prev_hash` no longer points at its predecessor).

        Then it cross-checks the result against the signed external checkpoint. The chain alone
        cannot see a deleted TIP — the newest row has no successor to notice it is gone — so for
        a file-backed log the checkpoint is what makes that case fail instead of pass."""
        prev_hash = GENESIS
        count = 0
        try:
            rows = self._conn.execute("SELECT * FROM audit ORDER BY id").fetchall()
        except sqlite3.Error as exc:
            return AuditVerification(False, None, f"could not read the audit store: {exc!r}")
        for r in rows:
            count += 1
            if r["prev_hash"] != prev_hash:
                return AuditVerification(
                    False, int(r["id"]),
                    f"row {r['id']} does not chain to its predecessor (expected prev_hash "
                    f"{prev_hash[:12]}…, found {r['prev_hash'][:12]}…) — a row was removed or reordered")
            payload = {"id": int(r["id"]), "ts": r["ts"], "task_id": r["task_id"],
                       "event": r["event"], "detail": r["detail"]}
            if row_hash(prev_hash, payload) != r["hash"]:
                return AuditVerification(
                    False, int(r["id"]),
                    f"row {r['id']} was modified after it was written (its content no longer "
                    f"hashes to the stored digest)")
            prev_hash = r["hash"]
        if self.checkpoint is not None:
            return self._verify_against_checkpoint(rows, count, prev_hash)
        return AuditVerification(True, None, f"{count} rows verified intact")

    def _verify_against_checkpoint(self, rows, count: int, tip: str) -> AuditVerification:
        try:
            attested = self.checkpoint.read()
        except AuditError as exc:
            return AuditVerification(False, None, str(exc))
        if attested is None:
            return AuditVerification(
                False, None,
                f"no signed checkpoint at {self.checkpoint.path} — the chain tip has no external "
                f"witness, so a deleted final row would be invisible")
        if int(attested["rows"]) != count:
            direction = "fewer" if count < int(attested["rows"]) else "more"
            return AuditVerification(
                False, None,
                f"the store holds {count} rows but the signed checkpoint attests "
                f"{attested['rows']} — {direction} rows than were receipted, so the log was "
                f"truncated or appended to outside it")
        if attested["tip"] != tip:
            return AuditVerification(
                False, (int(rows[-1]["id"]) if rows else None),
                f"the newest row does not match the signed checkpoint (checkpoint attests tip "
                f"{attested['tip'][:12]}…, the store ends at {tip[:12]}…) — the final row was "
                f"removed or replaced")
        return AuditVerification(True, None,
                                 f"{count} rows verified intact against the signed checkpoint")


# A sink is given a finished record; it must never raise back into the turn — EXCEPT where the
# caller has decided an unreceipted action is worse than a failed one (see AuditLog.record).
PolicySink = Callable[[PolicyRecord], None]


def bound_record_fn(audit: AuditLog, event: str = "policy.decision") -> PolicySink:
    """Adapt an `AuditLog` into a `PolicySink`. Deliberately NOT swallowing: an audit failure here
    raises, so the caller learns the action could not be receipted."""

    def sink(record: PolicyRecord) -> None:
        audit.record(event, task_id=record.task_id, detail=record.as_row())

    return sink
