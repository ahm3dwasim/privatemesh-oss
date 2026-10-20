"""Standing limits — the owner's standing yes, and the exact clauses it covers.

Approving every change forever turns "teach it once" into "click forever", and the second failure
is worse than the first: an owner who has approved four hundred times stops reading. So the owner
may set a standing answer for the meeting-change routine. Three presets, and the middle one is the
only one that acts:

* `ask_always` — every change is prepared and waits for the owner. **The default, and the only mode
  an install that predates this file can be in**: a standing yes is something an owner grants, never
  something a new column grants on their behalf.
* `within_limits` — a change whose ENTIRE action bundle fits the clauses below runs after
  `prepare` without asking, and the owner is told afterwards. Anything outside them falls back to
  `ask_always` for that change, and the decision log says which clause failed.
* `off` — the routine still watches and prepares; nothing executes. See `OFF_MEANS` for why this
  refuses an explicit approval rather than quietly ignoring one.

The clauses are a FIXED, ENUMERATED list, not a policy language. That is the design decision this
module exists to hold: a policy language would be a second thing to get right, and the owner would
have to trust the interpreter. Six sentences can be read, argued with, and checked line by line
against the code that enforces them (`ChangeEngine.limit_review`).

Two rules about the enforcement, and both are the point:

* **The model never decides whether something is inside the limit.** The clauses are checked by
  trusted code, at the moment execution would happen, from the world rather than from the plan.
* **A clause is a sentence the UI shows verbatim**, with an "It cannot…" list beside it. The same
  string is the `rule` on the decision rows, so what the owner read and what the engine enforced
  cannot drift into two different claims — `clauses()` is the single source for both.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass

ASK_ALWAYS, WITHIN_LIMITS, OFF = "ask_always", "within_limits", "off"
MODES = (ASK_ALWAYS, WITHIN_LIMITS, OFF)

#: What a build with no stored answer does, and what an existing install gets on upgrade. Named
#: rather than written as a literal at each call site so that "the safe one" has one spelling.
DEFAULT_MODE = ASK_ALWAYS

#: Why `off` refuses an approval instead of discarding it. A mode that silently swallowed the
#: owner's yes would leave the review screen saying "approved" over a meeting that never moved —
#: the failure this product exists to not have. Refusing is loud, and loud is recoverable.
OFF_MEANS = ("The routine is watching and preparing, but execution is off, so nothing was run. "
             "Set the limits mode back to ask_always or within_limits to act on an approval.")


class UnknownMode(ValueError):
    """A mode that is not one of `MODES`. Refused rather than coerced to a default: a typo that
    silently becomes `ask_always` looks like the setting did not save, and a typo that silently
    becomes `within_limits` is the dangerous direction."""


@dataclass(frozen=True)
class Clause:
    """One line of the standing yes: what it permits, and what it does not.

    `id` is stable and is what a decision row and a UI both key on. `text` is shown verbatim. The
    `cannot` list is the same sentence's boundary, written out, because a permission an owner
    cannot see the edges of is a permission they cannot judge."""

    id: str
    text: str
    cannot: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "cannot": list(self.cannot)}


#: The enumerated limit. Six clauses, in the order the UI numbers them. Every one of them is
#: enforced in `ChangeEngine.limit_review`, and the enforcement reads this tuple for its `rule`
#: string, so the sentence the owner reads and the sentence on the decision row are one string.
CLAUSES: tuple[Clause, ...] = (
    Clause(
        "1",
        "Move only the meeting this email is about (the pinned event), on the day it's already on.",
        ("a different meeting on the same day, or any other day",
         "a meeting that recurs, or one somebody else organizes",
         "a meeting the change was not raised from"),
    ),
    Clause(
        "2",
        "Only to a time the validator accepts (protected commitment, buffer, day window, busy).",
        ("a time inside your protected commitment or its buffer",
         "a time outside your day window, or one already booked",
         "a time in the past"),
    ),
    Clause(
        "3",
        "Only the person who wrote is told — by the reply and by the invite update.",
        ("anyone the thread did not come from",
         "a cc, a bcc, or a reply-to that redirects the answer",
         "a second recipient of any kind",
         "an attendee the invite update would email — Google notifies the event's guests, so a "
         "three-person meeting tells three people"),
    ),
    Clause(
        "4",
        "The reply is composed only from the allowlisted fragments; no free text from the model.",
        ("prose the model wrote that is not a declared fragment",
         "a fragment whose source is not on the outbound allowlist",
         "a body that is not the render of its own fragments"),
    ),
    Clause(
        "5",
        "At most one calendar edit and one reply per email.",
        ("a second edit to the same meeting, or a second reply",
         "an edit or a reply for an email that already had one",
         "a plan that already ran being run again"),
    ),
    Clause(
        "6",
        "Nothing else: no other events, no other emails, no searches beyond the research cap.",
        ("any event other than the one this change is about",
         "any email other than the one reply",
         "an invite update that tells anyone the reply does not",
         "an update whose notification setting this build cannot read",
         "more searches than the research cap allows"),
    ),
    Clause(
        "7",
        "Care actions — a family digest, anything about someone you care for — go only to your "
        "care circle, and only to the members you listed for that kind.",
        ("an address you did not add yourself",
         "a member added by an email, a model answer or a taught skill",
         "a member named for something you did not list them for",
         "a digest more than once a day"),
    ),
)

_BY_ID = {c.id: c for c in CLAUSES}

ALLOWED, REFUSED = "allowed", "refused"

#: Recorded in place of a clause's own sentence when a caller asks for an id this build does not
#: have. It is not a clause and must never be shown as one; it is a bug made visible.
UNKNOWN_CLAUSE = "A clause this build does not know"


def clause(clause_id: str) -> Clause:
    """The clause with this id. Raises `KeyError` — a missing clause is a code defect, and a
    default here would enforce a rule nobody wrote."""
    return _BY_ID[str(clause_id)]


def clauses() -> list[dict]:
    """The catalogue, as the contract's `clauses` array."""
    return [c.as_dict() for c in CLAUSES]


#: The `send_updates` values this build understands. Anything else is a notification setting whose
#: audience this code cannot predict, and clause 6 refuses it rather than passing it through: the
#: provider decides who a value it invented means, and "the provider decides who is emailed" is the
#: exact thing the standing yes must never delegate.
SEND_UPDATES = ("none", "all")


def grant_digest(change, cs) -> str:
    """A hash over exactly the state the six clauses were decided on.

    The clauses read two mutable records — the change and the bundle it points at — and a review is
    only an authorization if the thing reviewed is still the thing that runs. The bundle's own
    etag/boundary CAS does not cover this: the recipient list, the reply's fragments, the search
    count, the action count and the notification setting all live outside it, and a concurrent
    writer (or a second process, or an edit) can move any of them between the review and the
    effect. Recording this digest with the approval and re-deriving it immediately before each
    effect is what turns "the checks passed" into "the checks still hold".

    `change` and `cs` are read by attribute rather than by type so this stays a pure function of
    the two records, with no import back into the engine that owns them. Volatile fields are
    excluded on purpose: `updated_at`, the store `revision` and the bundle's `status` all move as a
    *consequence* of authorizing the run, and hashing them would make every grant invalidate
    itself.
    """
    payload = {
        "change": {
            "id": str(getattr(change, "id", "")),
            "message_id": str(getattr(change, "message_id", "")),
            "sender_pinned": str(getattr(change, "sender_pinned", "")),
            "event_pinned": str(getattr(change, "event_pinned", "")),
            "stage": str(getattr(change, "stage", "")),
            "version": int(getattr(change, "version", 0)),
            "change_set_id": str(getattr(change, "change_set_id", "")),
            "searches_used": int(getattr(change, "searches_used", 0)),
            "reply_fragments": list(getattr(change, "reply_fragments", []) or []),
            "reply_template": str(getattr(change, "reply_template", "")),
        },
        "bundle": {
            "id": str(getattr(cs, "id", "")),
            "version": int(getattr(cs, "version", 0)),
            "calendar": dict(getattr(getattr(cs, "calendar", None), "arguments", {}) or {}),
            "email": dict(getattr(getattr(cs, "email", None), "arguments", {}) or {}),
        },
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ClauseVerdict:
    """What one clause concluded about one change."""

    id: str
    text: str
    verdict: str
    detail: str

    @property
    def refused(self) -> bool:
        return self.verdict == REFUSED

    def as_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "verdict": self.verdict, "detail": self.detail}


@dataclass(frozen=True)
class LimitReview:
    """Every clause's verdict for one change, and the one answer that matters.

    All six are kept, not only the failure. The clauses are the owner's standing yes; a run that
    cites six checks and shows the one that failed is a run the owner can audit without trusting
    that the other five were looked at.
    """

    change_id: str
    verdicts: tuple[ClauseVerdict, ...]

    @property
    def in_limit(self) -> bool:
        return bool(self.verdicts) and not any(v.refused for v in self.verdicts)

    @property
    def failed(self) -> ClauseVerdict | None:
        """The first clause that refused, in the order the owner reads them. First rather than all:
        a fallback names ONE reason, and the earliest clause is the one to fix."""
        return next((v for v in self.verdicts if v.refused), None)

    def as_dict(self) -> dict:
        return {"change_id": self.change_id, "in_limit": self.in_limit,
                "failed": self.failed.id if self.failed else None,
                "clauses": [v.as_dict() for v in self.verdicts]}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class LimitStore:
    """The owner's standing answer, in SQLite beside the routine's cursor and the boundary.

    A store rather than a field because "mode survives a restart" is the whole difference between a
    standing answer and a per-session accident. It is deliberately tiny: one row, two values, and
    no history — the audit chain is where a mode change is recorded (`V2Service.set_limits`), and a
    second copy of that history here would be a second thing to keep in step.
    """

    def __init__(self, conn: sqlite3.Connection | None = None, *, path: str | None = None,
                 now=None) -> None:
        self._lock = threading.Lock()
        self._conn = conn if conn is not None else sqlite3.connect(
            path or ":memory:", check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._clock = now or (lambda: _dt.datetime.now().astimezone())
        # The default is written ONCE, with an empty `updated_at`: an install that has never
        # chosen a mode has not "updated" anything, and stamping the default with the moment the
        # process started would make the default look like a decision the owner made.
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO limits(key, value) VALUES('mode', ?)",
                               (DEFAULT_MODE,))
            self._conn.execute("INSERT OR IGNORE INTO limits(key, value) VALUES('updated_at', '')")
            self._conn.execute("INSERT OR IGNORE INTO limits(key, value) VALUES('revision', '0')")
            self._conn.commit()

    # ---- the mode -------------------------------------------------------------------------------
    def mode(self) -> str:
        """The owner's standing answer. A stored value this build does not understand reads as the
        DEFAULT rather than as itself: a mode from a newer build is not a licence to act, and the
        safe direction for an unreadable permission is the one that asks."""
        value = self._value("mode")
        return value if value in MODES else DEFAULT_MODE

    def set_mode(self, mode: str) -> dict:
        """Set the standing answer, or refuse. Returns the state the contract serves."""
        if mode not in MODES:
            raise UnknownMode(f"unknown limits mode {mode!r} — expected one of {', '.join(MODES)}")
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO limits(key, value) VALUES('mode', ?)",
                               (mode,))
            self._conn.execute("INSERT OR REPLACE INTO limits(key, value) VALUES('updated_at', ?)",
                               (self._clock().isoformat(timespec="seconds"),))
            # Bumped on EVERY set, including one that sets the mode it already had. This is what a
            # standing-limit grant is bound to, and the binding has to survive the flip that gets
            # undone: `within_limits -> off -> within_limits` inside one second leaves the mode
            # string identical and `updated_at` (second resolution) identical too, so a grant keyed
            # on either would still look current after the owner had revoked and re-granted the
            # standing yes. A counter is not a timestamp, so it cannot round two decisions into one.
            # Read inline rather than through `revision()`, which takes this same lock: the counter
            # is bumped by the one thread that holds it, so the read-modify-write needs no second
            # lock and must not try to take this one again.
            row = self._conn.execute(
                "SELECT value FROM limits WHERE key='revision'").fetchone()
            try:
                current = int(row["value"]) if row is not None else 0
            except (TypeError, ValueError):
                current = 0
            self._conn.execute(
                "INSERT OR REPLACE INTO limits(key, value) VALUES('revision', ?)",
                (str(current + 1),))
            self._conn.commit()
        return self.state()

    def revision(self) -> int:
        """How many times the owner has changed the standing answer. Read together with `mode()`
        by anything that has to tell "this mode is still the one I was authorized under" from "this
        mode is merely equal to it"."""
        raw = self._value("revision")
        try:
            return int(raw)
        except (TypeError, ValueError):
            # An unreadable counter is not a licence to treat the grant as current. Zero is
            # returned, and every recorded grant carries a revision of at least one, so they all
            # stop matching — the same fail-closed direction `mode()` takes for an unknown value.
            return 0

    def state(self) -> dict:
        """`docs/API_V2.md`: `{mode, clauses, updated_at}`. `updated_at` is `None` — not an empty
        string — until the owner has chosen a mode, so a client can tell "never set" from "set"."""
        return {"mode": self.mode(), "clauses": clauses(),
                "updated_at": self._value("updated_at") or None}

    def _value(self, key: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM limits WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else ""
