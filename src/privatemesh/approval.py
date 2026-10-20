# Ported from the author's prior private assistant (Jarvis); adapted for PrivateMesh.
"""Approval — turning a NEEDS_APPROVAL policy decision into a human yes/no, exactly once.

Ported from the prior assistant's `kernel/approval.py`: approval is per-action and specific —
bound to a task id, the exact tool, its **exact arguments**, and the moment it was requested,
and it never generalizes to a later action.

What PrivateMesh adds is the part that module left to its callers: a **single-use** store.
`ApprovalStore.consume` re-derives the argument fingerprint and refuses unless the approval is
approved, unconsumed, and for *this* task, *this* skill and *these* arguments. Approving
"send Alex the offer" therefore cannot be replayed to send a different body, to a different
recipient, or a second time in the same turn. That refusal is the demo's central claim, so it
lives in the data structure rather than in a caller's discipline.

Two hardening passes on top of that, both from the adversarial review:

1. **The fingerprint is a contract, not a best effort.** `args_fingerprint` accepts only the JSON
   value domain and rejects everything else, and it canonicalises with an explicit tag per type
   so `1` and `1.0` and `True` are three different actions. The earlier version used
   `json.dumps(..., default=str)` with a `repr()` fallback, which meant two distinct Python
   objects could share one authorization identity. There is no fallback now: a value that cannot
   be canonicalised is a value that cannot be approved.
2. **Approvals outlive the process.** The store is a SQLite table, and both decisions
   (`approve`/`deny`) and the spend (`consume`) are single-statement compare-and-set updates —
   the write that flips the state is the same write that checks it, so two racing callers cannot
   both win. Restarting no longer discards a pending decision or strands the turn that was
   waiting on it: pending and approved-but-unconsumed approvals are recoverable, with the turn
   they paused linked to them.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import sqlite3
import threading
import uuid
from dataclasses import dataclass

PENDING, APPROVED, DENIED, CONSUMED = "pending", "approved", "denied", "consumed"

# Deep enough for any real skill argument, shallow enough that a hostile payload cannot turn
# validation into a stack overflow.
_MAX_DEPTH = 32


class ApprovalError(RuntimeError):
    """An approval could not be granted, or was presented for an action it does not cover."""


def deep_copy_args(value, _depth: int = 0):
    """A detached copy of `value`, or `ApprovalError` if it is not a JSON value.

    Two jobs in one pass. It enforces the domain — `None`, bool, int, str, finite float, list,
    and dict with string keys, and nothing else — so no object with a custom `__repr__` or a
    mutable `__eq__` can ever become part of an authorization identity. And it copies, so the
    store never holds a reference to a caller's object that can be mutated after the owner said
    yes to it."""
    if _depth > _MAX_DEPTH:
        raise ApprovalError("arguments are nested too deeply to fingerprint")
    if value is None or isinstance(value, (bool, int, str)):
        return value                      # bool is checked before int by isinstance, but the
                                          # copy is identity either way; ordering matters only
                                          # for the tag, which `_canonical` applies.
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ApprovalError(
                "arguments contain a non-finite number, which has no exact JSON form to bind to")
        return value
    if isinstance(value, (list, tuple)):
        return [deep_copy_args(v, _depth + 1) for v in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ApprovalError(
                    f"arguments contain a {type(key).__name__} key; JSON object keys must be "
                    f"strings, and a coerced key is not an identity")
            out[key] = deep_copy_args(item, _depth + 1)
        return out
    raise ApprovalError(
        f"arguments contain {type(value).__name__}, which is not a JSON value — an approval "
        f"binds to JSON arguments or to nothing")


def _canonical(value) -> str:
    """An explicit, tagged, order-insensitive encoding of a validated JSON value.

    The tags are the point: `1`, `1.0` and `True` must not collide, and `"1"` must not collide
    with either. `json.dumps(default=str)` could not promise that."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return f"int:{value}"
    if isinstance(value, float):
        return f"float:{value!r}"
    if isinstance(value, str):
        return "str:" + json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{_canonical(v)}"
                          for k, v in sorted(value.items())) + "}"


def args_fingerprint(tool: str, arguments: dict | None) -> str:
    """A stable digest of (skill, exact arguments). Order-insensitive at the key level, but any
    change to a value — or a new key — produces a different digest.

    Raises `ApprovalError` for anything outside the JSON value domain. That is deliberate: the
    caller wants an identity it can bind an owner's consent to, and the honest answer for a value
    with no exact canonical form is "this cannot be approved", not a digest of its `repr`."""
    safe = deep_copy_args({} if arguments is None else arguments)
    if not isinstance(safe, dict):
        raise ApprovalError(f"arguments must be a JSON object, got {type(safe).__name__}")
    payload = "tool:" + json.dumps(str(tool), ensure_ascii=False) + "|" + _canonical(safe)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    task_id: str
    tool: str
    arguments: dict
    requested_at: str
    args_hash: str
    status: str = PENDING
    decided_at: str | None = None
    consumed_at: str | None = None
    turn_id: str = ""                # the paused turn this approval belongs to, when there is one

    def as_dict(self) -> dict:
        return {"id": self.id, "task_id": self.task_id, "tool": self.tool,
                "arguments": self.arguments, "requested_at": self.requested_at,
                "args_hash": self.args_hash, "status": self.status,
                "decided_at": self.decided_at, "consumed_at": self.consumed_at,
                "turn_id": self.turn_id}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals(
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tool TEXT NOT NULL, arguments TEXT NOT NULL,
    requested_at TEXT NOT NULL, args_hash TEXT NOT NULL, status TEXT NOT NULL,
    decided_at TEXT, consumed_at TEXT, turn_id TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_approvals_turn ON approvals(turn_id);
"""

_COLUMNS = ("id", "task_id", "tool", "arguments", "requested_at", "args_hash", "status",
            "decided_at", "consumed_at", "turn_id")


class ApprovalStore:
    """Pending and decided approvals, in SQLite.

    Durable on purpose. The earlier in-memory version failed closed for *replay* — a forgotten
    approval cannot be spent — but it also forgot the owner's pending decisions and the turn that
    was waiting on one, so a restart between "approve" and "execute" left the owner believing an
    action had been handled while it was silently abandoned.

    Every state transition is a compare-and-set: one `UPDATE ... WHERE status = <expected>` whose
    row count is the answer. The check and the write are the same statement, so nothing can slip
    between them."""

    def __init__(self, conn: sqlite3.Connection | None = None, *,
                 path: str | None = None) -> None:
        self._lock = threading.Lock()
        if conn is not None:
            self._conn = conn
        else:
            self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- write -------------------------------------------------------------------------------
    def request(self, task_id: str, tool: str, arguments: dict) -> ApprovalRequest:
        """Create a NEW pending approval for this exact action. Always a fresh id: two calls with
        identical arguments get two approvals, and granting one never grants the other."""
        safe = deep_copy_args({} if arguments is None else arguments)
        req = ApprovalRequest(id=uuid.uuid4().hex, task_id=task_id, tool=tool, arguments=safe,
                              requested_at=_now(), args_hash=args_fingerprint(tool, safe))
        with self._lock:
            self._conn.execute(
                f"INSERT INTO approvals({','.join(_COLUMNS)}) "
                f"VALUES({','.join('?' * len(_COLUMNS))})",
                (req.id, req.task_id, req.tool, json.dumps(req.arguments, ensure_ascii=False),
                 req.requested_at, req.args_hash, req.status, None, None, req.turn_id))
            self._conn.commit()
        return req

    def link_turn(self, approval_id: str, turn_id: str) -> bool:
        """Bind an approval to the paused turn it belongs to, so a restart can find it again."""
        with self._lock:
            cur = self._conn.execute("UPDATE approvals SET turn_id=? WHERE id=?",
                                     (turn_id or "", approval_id))
            self._conn.commit()
        return cur.rowcount > 0

    def _decide(self, approval_id: str, status: str) -> ApprovalRequest:
        """Record a decision as a compare-and-set on `status = pending`.

        Granting twice is idempotent (the API endpoint and the resume path both mark it granted,
        and the single-use guarantee comes from `consume`, not from here); reversing a decision,
        or deciding a spent one, is refused."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET status=?, decided_at=? WHERE id=? AND status=?",
                (status, _now(), approval_id, PENDING))
            self._conn.commit()
        if cur.rowcount == 1:
            return self.get(approval_id)
        req = self.get(approval_id)
        if req is None:
            raise ApprovalError(f"no such approval '{approval_id}'")
        if req.status == status and status == APPROVED:
            return req
        raise ApprovalError(f"approval '{approval_id}' is already {req.status}")

    def approve(self, approval_id: str) -> ApprovalRequest:
        return self._decide(approval_id, APPROVED)

    def deny(self, approval_id: str) -> ApprovalRequest:
        return self._decide(approval_id, DENIED)

    def consume(self, approval_id: str, *, task_id: str, tool: str, arguments: dict) -> ApprovalRequest:
        """Spend an approval on ONE action. Raises `ApprovalError` unless the approval is approved,
        unused, and covers exactly this (task_id, skill, arguments).

        The spend is one compare-and-set on every part of the binding at once — status, task,
        skill and argument digest — so the single-use guarantee is enforced by the database
        rather than by a read happening to still be true when the write lands.

        Every rejection reason is a separate branch on purpose: "wrong arguments" and "already
        used" mean different things to an owner reading the audit trail."""
        current = args_fingerprint(tool, arguments)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET status=?, consumed_at=? "
                "WHERE id=? AND status=? AND task_id=? AND tool=? AND args_hash=?",
                (CONSUMED, _now(), approval_id, APPROVED, task_id, tool, current))
            self._conn.commit()
            spent = cur.rowcount == 1
        if spent:
            return self.get(approval_id)
        # The compare-and-set refused. Re-read only to say WHY — the refusal itself already
        # happened, so a concurrent spend can never be talked into succeeding by this branch.
        req = self.get(approval_id)
        if req is None:
            raise ApprovalError(f"no such approval '{approval_id}'")
        if req.status == CONSUMED:
            raise ApprovalError(
                f"approval '{approval_id}' was already used at {req.consumed_at} — "
                f"an approval covers one action, not a class of actions")
        if req.status != APPROVED:
            raise ApprovalError(f"approval '{approval_id}' is {req.status}, not approved")
        if req.task_id != task_id:
            raise ApprovalError(
                f"approval '{approval_id}' was requested for task '{req.task_id}', not '{task_id}'")
        if req.tool != tool:
            raise ApprovalError(
                f"approval '{approval_id}' covers skill '{req.tool}', not '{tool}'")
        raise ApprovalError(
            f"approval '{approval_id}' does not cover these arguments "
            f"(approved {req.args_hash[:12]}…, presented {current[:12]}…)")

    # ---- read --------------------------------------------------------------------------------
    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM approvals WHERE id=?",
                                     (approval_id,)).fetchone()
        return _from_row(row)

    def list(self, *, status: str | None = None) -> list[ApprovalRequest]:
        sql = "SELECT * FROM approvals" + (" WHERE status=?" if status is not None else "")
        params = (status,) if status is not None else ()
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        reqs = [r for r in (_from_row(row) for row in rows) if r is not None]
        return sorted(reqs, key=lambda r: (r.requested_at, r.id))

    def recoverable(self) -> dict[str, list[ApprovalRequest]]:
        """What a restart must not lose: decisions still waiting on the owner, and decisions the
        owner already made that no skill has spent yet. Both are answers that exist only here."""
        pending = self.list(status=PENDING)
        unconsumed = [r for r in self.list(status=APPROVED) if r.consumed_at is None]
        return {"pending": pending, "approved_unconsumed": unconsumed}

    def turn_for(self, approval_id: str) -> str:
        """The paused turn this approval paused, or '' when it was never linked to one."""
        req = self.get(approval_id)
        return req.turn_id if req else ""


def _from_row(row: sqlite3.Row | None) -> ApprovalRequest | None:
    if row is None:
        return None
    try:
        arguments = json.loads(row["arguments"])
    except (TypeError, ValueError):
        arguments = {}
    return ApprovalRequest(
        id=row["id"], task_id=row["task_id"], tool=row["tool"],
        arguments=arguments if isinstance(arguments, dict) else {},
        requested_at=row["requested_at"], args_hash=row["args_hash"], status=row["status"],
        decided_at=row["decided_at"], consumed_at=row["consumed_at"],
        turn_id=row["turn_id"] or "")
