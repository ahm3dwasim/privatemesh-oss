"""The command layer for owner-facing memory mutations.

Memory is the owner's own data, not a skill, so it does not go through the permission broker —
there is no risk tier to weigh and no third party to approve. But it IS a mutation surface, and
the adversarial review was right that it was the one place in the API that changed state and
audited afterwards: if the audit write failed, a memory row existed that nothing had a receipt
for, and the caller saw an error describing a change that had in fact happened.

So these commands are ordered the way the broker's effects are. Each one reserves an intent
receipt FIRST, applies the change only if that succeeded, then writes the outcome. The guarantee
is the same one the broker gives skills; only the policy decision is missing, because for the
owner's own memory there is nothing to decide.
"""
from __future__ import annotations

import logging
import sqlite3

from . import memory as mem
from .audit import AuditLog

log = logging.getLogger(__name__)

INTENT_EVENT, OUTCOME_EVENT = "memory.intent", "memory.outcome"
WRITE, CONSENT, FORGET = "write", "consent", "forget"


class MemoryCommands:
    def __init__(self, conn: sqlite3.Connection, audit: AuditLog) -> None:
        self.conn = conn
        self.audit = audit

    def write(self, *, type: str, text: str, entity: str = "", project: str = "",
              importance: int = 3, approved: bool = False, source: str = "api") -> int:
        """Store one memory. Returns the new id, or 0 when there was nothing worth storing.

        A 0 is a real answer, not a failure: the text was empty or entirely redaction markers.
        The outcome receipt says so, so "nothing was stored" is distinguishable from "the write
        broke" without reading application logs."""
        intent = self._intent(WRITE, {"type": type, "approved": bool(approved),
                                      "chars": len(text or "")})
        new_id = mem.write_memory(self.conn, type=type, text=text, entity=entity, project=project,
                                  importance=importance, source=source, approved=approved)
        self._outcome(intent, WRITE, {"memory_id": new_id, "stored": bool(new_id),
                                      "approved": bool(approved)})
        return new_id

    def set_consent(self, memory_id: int, *, approved: bool = True) -> bool:
        """Grant or revoke consent for one memory. False when no such row."""
        intent = self._intent(CONSENT, {"memory_id": int(memory_id), "approved": bool(approved)})
        changed = mem.approve(self.conn, memory_id, approved=approved)
        self._outcome(intent, CONSENT, {"memory_id": int(memory_id), "approved": bool(approved),
                                        "changed": bool(changed)})
        return changed

    def forget(self, memory_id: int, *, text: str = "") -> bool:
        """Invalidate one memory (bi-temporal: `invalid_at` is set, the row stays)."""
        intent = self._intent(FORGET, {"memory_id": int(memory_id)})
        changed = mem.forget_id(self.conn, memory_id)
        self._outcome(intent, FORGET, {"memory_id": int(memory_id), "changed": bool(changed),
                                       "text": text})
        return changed

    # ---- receipts ------------------------------------------------------------------------------
    def _intent(self, op: str, detail: dict) -> dict:
        """Reserve the receipt. Raises `AuditError` — and the caller must let that stop the
        mutation, because an unreceipted change to the owner's memory is exactly what this layer
        exists to prevent."""
        return self.audit.record(INTENT_EVENT, detail={"op": op, **detail})

    def _outcome(self, intent: dict, op: str, detail: dict) -> dict:
        return self.audit.record(OUTCOME_EVENT, detail={
            "op": op, "intent_id": intent.get("id"), **detail})
