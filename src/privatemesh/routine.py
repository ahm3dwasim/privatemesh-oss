"""The durable watcher: one bounded poll, a persisted cursor, and a message classified once.

"Always on" is a claim about work that happens while nobody is looking, so it is the one part of
this product that must survive the process:

* the **cursor** is on disk, so a restart resumes rather than re-reading the mailbox;
* every polled message id is recorded, so the same email cannot become two proposals;
* a **pause** stops the polling itself — it is not a flag that hides results;
* `last_checked_at` is updated on every poll, because "Last checked" is only worth showing if it
  is a fact rather than a decoration.

The watcher owns no policy. It finds mail, asks the engine whether it is a change, and records
what it did — an unattended run never grants authority to send, and everything it raises still
needs the owner's exact approval.

**A classification the engine could not read is not a "no".** An unattended watcher that treats a
malformed model answer as "this email is not a meeting change" drops real mail silently, which is
the one failure mode nobody is present to notice. So an unreadable answer leaves the message
UNSEEN and is retried on the next tick, up to `MAX_CLASSIFY_ATTEMPTS`; past that bound the message
is marked seen WITH the reason recorded, so it stops being re-read without ever being pretended
away.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import threading

from .changes import ModelAnswerInvalid

DEFAULT_INTERVAL_SECONDS = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS routine_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routine_seen(
    message_id TEXT PRIMARY KEY, change_id TEXT NOT NULL DEFAULT '', seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routine_attempts(
    message_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0, last_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '');
"""

#: A bounded interval, not a polling loop. The floor keeps a mistyped environment variable from
#: turning the watcher into a way to spend the owner's search budget in a tight loop.
MIN_INTERVAL_SECONDS = 30

#: How many ticks may retry a message whose classification came back unreadable before the watcher
#: stops asking. Three is the bound `learn.MAX_ATTEMPTS` already uses for "hand the failure back
#: and try again": past it, retrying stops being a retry and starts being a bill. The message is
#: then marked seen with the reason on the audit chain — the owner has to be able to find out that
#: mail was skipped, and why.
MAX_CLASSIFY_ATTEMPTS = 3


class Routine:
    def __init__(self, engine, mail, *, conn: sqlite3.Connection | None = None,
                 path: str | None = None, interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
                 audit=None, now=None) -> None:
        self.engine = engine
        self.mail = mail
        self.interval_seconds = max(MIN_INTERVAL_SECONDS, int(interval_seconds))
        self.audit = audit
        self._clock = now or (lambda: _dt.datetime.now().astimezone())
        self._lock = threading.Lock()
        self._conn = conn if conn is not None else sqlite3.connect(
            path or ":memory:", check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._set_default("paused", "0")
        self._set_default("cursor", "")
        self._set_default("last_checked_at", "")

    # ---- the tick -----------------------------------------------------------------------------
    def tick(self) -> list:
        """Poll once. Returns the new changes; never raises for a provider that returns nothing."""
        if self.paused:
            return []
        messages, cursor = self.mail.list_new(self._value("cursor") or None)
        found = []
        retry_pending = False
        for message in messages:
            if self.seen(message.id):
                continue
            try:
                change = self.engine.classify(message)
            except ModelAnswerInvalid as exc:
                # NOT marked seen. The engine's answer named no verdict — `None` read off a
                # missing key used to be indistinguishable from "not a change" — so treating it
                # as one would drop real mail with nobody watching. Leave it unseen and retry.
                retry_pending = self._note_unreadable(message.id, exc) or retry_pending
                continue
            # Marked seen AFTER a successful classification: a crash mid-classification leaves
            # the message unseen so the next tick retries it, and a message that is not a change
            # is still marked so it is not re-read forever.
            self._mark_seen(message.id, change.id if change else "")
            if change is not None:
                found.append(change)
        # The cursor moves only once nothing in this batch is still owed a retry. `list_new`
        # returns messages NEWER than the cursor, so advancing past an unreadable one would put it
        # permanently out of reach and the retry above could never happen — the bound would be
        # enforced against a message the watcher never asked about again. Holding the cursor back
        # is safe because the seen-set, not the cursor, is what stops a re-read from becoming a
        # second proposal; it is already on disk and already consulted above.
        if cursor and not retry_pending:
            self._set("cursor", cursor)
        self._set("last_checked_at", self._clock().isoformat(timespec="seconds"))
        if self.audit is not None and found:
            self.audit.record("routine.detected", detail={"count": len(found),
                                                          "change_ids": [c.id for c in found]})
        return found

    # ---- controls -----------------------------------------------------------------------------
    def pause(self) -> dict:
        self._set("paused", "1")
        return self.state()

    def resume(self) -> dict:
        self._set("paused", "0")
        return self.state()

    @property
    def paused(self) -> bool:
        return self._value("paused") == "1"

    def seen(self, message_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM routine_seen WHERE message_id=?",
                                     (message_id,)).fetchone()
        return row is not None

    # ---- a classification nobody could read ------------------------------------------------
    def attempts(self, message_id: str) -> int:
        """How many ticks have found this message's classification unreadable."""
        with self._lock:
            row = self._conn.execute("SELECT attempts FROM routine_attempts WHERE message_id=?",
                                     (message_id,)).fetchone()
        return int(row["attempts"]) if row else 0

    def unreadable_reason(self, message_id: str) -> str:
        """Why the watcher stopped asking about this message, or "" if it never had to.

        Kept here as well as on the audit chain because this is what a `/status` has to be able to
        answer without replaying the chain: "was any mail skipped, and why".
        """
        with self._lock:
            row = self._conn.execute("SELECT reason FROM routine_attempts WHERE message_id=?",
                                     (message_id,)).fetchone()
        return str(row["reason"]) if row else ""

    def _note_unreadable(self, message_id: str, exc: ModelAnswerInvalid) -> bool:
        """One unreadable classification. Returns True while the message is still owed a retry."""
        attempts = self._bump_attempts(message_id)
        reason = (f"the {exc.phase} answer was missing {', '.join(exc.missing)} "
                  f"(keys received: {', '.join(exc.received) or 'none'})")
        detail = {"message_id": message_id, "phase": exc.phase, "missing": list(exc.missing),
                  "received": list(exc.received), "attempt": attempts}
        if attempts >= MAX_CLASSIFY_ATTEMPTS:
            self._record_reason(message_id, reason)
            if self.audit is not None:
                self.audit.record("routine.classify_abandoned", detail={**detail, "reason": reason})
            # Marked seen only NOW, with the reason on the chain: the message stops being re-read,
            # and nothing about it is lost — it was never classified as a non-change.
            self._mark_seen(message_id, "")
            return False
        if self.audit is not None:
            self.audit.record("routine.classify_retry", detail=detail)
        return True

    def _bump_attempts(self, message_id: str) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO routine_attempts(message_id, attempts, last_at) VALUES(?,1,?) "
                "ON CONFLICT(message_id) DO UPDATE SET attempts=attempts+1, "
                "last_at=excluded.last_at",
                (message_id, self._clock().isoformat(timespec="seconds")))
            self._conn.commit()
            row = self._conn.execute("SELECT attempts FROM routine_attempts WHERE message_id=?",
                                     (message_id,)).fetchone()
        return int(row["attempts"]) if row else 0

    def _record_reason(self, message_id: str, reason: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE routine_attempts SET reason=? WHERE message_id=?",
                               (reason, message_id))
            self._conn.commit()

    def state(self) -> dict:
        return {"enabled": True, "paused": self.paused, "cursor": self._value("cursor"),
                "last_checked_at": self._value("last_checked_at"),
                "interval_seconds": self.interval_seconds}

    # ---- storage ------------------------------------------------------------------------------
    def _mark_seen(self, message_id: str, change_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO routine_seen(message_id, change_id, seen_at) "
                "VALUES(?,?,?)",
                (message_id, change_id, self._clock().isoformat(timespec="seconds")))
            self._conn.commit()

    def _value(self, key: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM routine_state WHERE key=?",
                                     (key,)).fetchone()
        return row["value"] if row else ""

    def _set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO routine_state(key, value) VALUES(?,?)",
                               (key, value))
            self._conn.commit()

    def _set_default(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO routine_state(key, value) VALUES(?,?)",
                               (key, value))
            self._conn.commit()
