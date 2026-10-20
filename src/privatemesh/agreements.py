"""Agreements — what the owner agreed to, and whether the world still matches it.

PrivateMesh keeps what the owner approved. Nothing until now noticed when the other side later
edited the invite: moved it, swapped the venue, added a guest. The organisers' ask is that the
product "remembers context across sessions and takes real action", and remembering is only half of
it — a memory nobody checks is a memory that quietly becomes false.

So two things happen here, and they are deliberately different in kind:

* **When a change EXECUTES, the agreement is RECORDED** — the event, its title, and the start, end,
  place and guest list **as the owner approved them**. Only on the path where the invite actually
  moved and the read-back confirmed it: a failed effect leaves nothing recorded, because there is
  nothing the owner agreed to.
* **`check` RE-READS the world and compares**, field by field, through the connector the product
  already has. Read-only.

Three rules this module exists to hold:

* **Drift never causes an action.** The comparison's only output is information for the owner. A
  feature that reacted to drift would hand whoever can edit an invite a way to make this product
  send mail, which is the exact inversion of the product's claim. (`docs/API_V2.md` says how a fix
  would be PROPOSED, which is a different thing from being performed.)
* **An unreadable event is `unchecked`, never `holding`.** A meeting nobody can read is not a
  meeting that still matches. The two are different answers and only one of them is honest.
* **The event's text is untrusted and never reaches a model here.** This module holds no model
  client and makes no model call; the title is carried to the owner's own screen and nowhere else.

One agreement per EVENT, not one per execution. The second move of a meeting is what the owner has
now agreed to; keeping the first alongside it would report the superseded window as drift forever,
which is a false alarm that never goes away. `record` therefore replaces.

Two more rules, both of them about a race rather than about a calendar:

* **A check may not overwrite a newer agreement.** `check` reads a record, goes out to the
  provider (which takes time), and writes back what it found. If an execution recorded a newer
  agreement for the same event while that read was in flight, the check is holding an answer about
  a record that no longer exists — and writing it would restore the superseded window as the
  owner's agreement, permanently. Every write therefore carries the `revision` the caller read and
  is dropped when the stored row has moved past it (`AgreementStore.save`).
* **Neither may an OLDER execution.** Two executions of the same event can overlap — one moves the
  meeting, pauses, and the other moves it again and records first. The one that resumes is holding
  the OBSOLETE window, and replacing the newer record with it would leave the owner holding an
  agreement the world has already left. An execution therefore carries the sequence number it was
  given when it started (`AgreementStore.begin_execution`), and a record whose sequence is not
  ahead of the stored one is dropped rather than written.
* **The swap is one SQL statement, not a read and a write.** `save` reads the row, decides in
  Python, and then writes conditionally on the row still being the exact bytes it read
  (`UPDATE ... WHERE id=? AND payload=?`, `INSERT` for a first write, rowcount decides). Two
  connections — two server workers, two processes — can both read revision 1 and both try to write
  revision 2, and exactly one of them wins. The read-then-write pair is what the review reproduced
  as a silently lost write; a per-instance Python lock never covered it, because the second writer
  is in another process.
* **What is recorded as agreed is what the OWNER APPROVED, never what the provider read back.**
  The reviewed window, place, title and guest list come from the change set the owner said yes to.
  A read-back that disagrees is evidence about the world, not a new agreement, so it is compared
  and reported (`drifted`) rather than adopted.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, replace

log = logging.getLogger(__name__)

HOLDING, DRIFTED, UNCHECKED = "holding", "drifted", "unchecked"
STATUSES = (HOLDING, DRIFTED, UNCHECKED)

#: What `unchecked` means when nothing has ever looked. The other way to arrive at `unchecked` is a
#: read that failed, and that one carries the failure instead — the owner is owed the difference
#: between "not yet" and "cannot", because only one of them is going to resolve itself.
NOTE_NEVER_CHECKED = "not checked yet — nothing has compared this with the calendar"

#: The four fields compared, in the order the drift list reports them. The title is recorded and
#: shown but is NOT compared: `observed` is the same four fields by contract, and a meeting
#: somebody renamed has not been moved, re-placed or re-peopled.
FIELDS = ("start", "end", "location", "attendees")

#: The shape of a stored record, written into every row. A row carrying a LATER number was written
#: by a build that knows something this one does not, and reading it as if it were this one's shape
#: would be inventing a downgrade. Such a row is unreadable here — which means it is served as
#: `unchecked` and never rewritten (`_load`).
SCHEMA_VERSION = 1

#: Every key `_dump` writes. Anything else in a stored payload came from a build that is not this
#: one, and is treated the same way as a later `v`: unreadable, and therefore never overwritten.
_FIELDS_WRITTEN = frozenset({
    "v", "id", "change_id", "event_id", "title", "start", "end", "location", "attendees",
    "status", "observed", "drift", "checked_at", "error", "revision", "sequence", "unreviewed"})

#: Why an agreement is `unchecked` when the bundle never recorded the list the owner reviewed.
#: Distinct from `NOTE_NEVER_CHECKED`: this one will not resolve itself by checking again.
NOTE_NO_REVIEWED_GUESTS = (
    "the bundle did not record who the update would notify, so nobody can be held to a guest "
    "list; nothing here has compared this with the calendar")

#: How long one compare-and-swap may keep retrying a row that keeps changing under it.
#:
#: This is a DEADLINE, not an attempt budget, and the distinction is the whole of it. How many
#: other writers will touch a row is not a number this store knows, so any fixed number of attempts
#: is a rule that the next writer loses — and the next writer is the newest execution, whose
#: calendar state is the final one. A write that is still the newest must not be dropped for being
#: slow, so what stops the retry is the clock, and it RAISES: `None` is this store's word for "an
#: older write correctly lost", and a caller that read a timeout as that would report an agreement
#: as superseded when it was never written at all.
DEFAULT_CAS_TIMEOUT_SECONDS = 5.0


class AgreementStoreBusy(RuntimeError):
    """A compare-and-swap could not be completed inside its deadline. A store failure, like any
    other — raised rather than answered, so that no caller can mistake it for a superseded write."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agreements (
    id      TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agreement_sequence (
    n INTEGER NOT NULL
);
"""


def _row_id(row) -> str:
    """The key a stored row lives under. Split out so `save` and `scan` cannot disagree about
    which column is the row's identity — the whole of the id-binding rule below rests on it."""
    return str(row["id"])


def _observed(event) -> dict:
    """The world's answer, in the four fields the contract compares.

    Guests come out sorted so that two reads of an unchanged event serve byte-identical rows: the
    provider's array order is not information, and a client diffing two responses should not see a
    change because Google reordered a list.
    """
    return {"start": event.start, "end": event.end, "location": event.location,
            "attendees": sorted(str(a) for a in event.attendees)}


def _moment(value: str):
    """An ISO instant, or None when the string is not one. `Z` and an explicit offset both parse;
    a date-only value parses as naive midnight, which is what an all-day event is."""
    try:
        return _dt.datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def same_instant(agreed: str, observed: str) -> bool:
    """Two times are equal when they are the same MOMENT.

    `15:00:00+01:00` and `14:00:00Z` are one instant written two ways, and a provider that switches
    between the two forms has not moved the meeting — reporting that as drift would be a false
    alarm about nothing the owner can act on. A value that will not parse falls back to string
    equality, so an unparseable time is compared as what it is rather than crashing the check. An
    aware and a naive value are never equal: an event that lost its timezone is not the same
    commitment, and calling it one would be inventing agreement.

    Public because the EXECUTOR needs the same rule to decide whether a write landed: it compares
    the window it wrote against the window it read back, and Google may hand back `14:00:00Z` for a
    `15:00:00+01:00` it accepted. That comparison is the prerequisite for recording an agreement at
    all, so it must be the same one the agreement is later judged by — a second, string-shaped
    copy of this rule would refuse to record a move that happened exactly as approved.
    """
    a, b = _moment(agreed), _moment(observed)
    if a is None or b is None:
        return str(agreed).strip() == str(observed).strip()
    if (a.tzinfo is None) != (b.tzinfo is None):
        return False
    return a == b


def _same_guests(agreed, observed) -> bool:
    """A guest list is a SET of addresses, compared caselessly.

    The order is an artefact of how the provider serialises the array and the case is an artefact
    of how somebody typed an address. Neither is the other side changing the meeting, and a feature
    that cried wolf on either would be turned off within a week.

    `casefold()` and not `lower()`: the review's Low is that "case-insensitive" was only true of
    the ASCII alphabet. `straße@example.com` and `STRASSE@example.com` are one address written two
    ways under Unicode caseless matching, and `lower()` calls them different — a false drift
    reported about a change nobody made.
    """
    norm = lambda values: {str(v).strip().casefold() for v in (values or ()) if str(v).strip()}
    return norm(agreed) == norm(observed)


def _same_location(agreed: str, observed: str) -> bool:
    """Exact, and deliberately so: nothing here invents a notion of "the same place".

    Two spellings of one venue are two different strings, and this module has no honest way to say
    they are the same building. Under-reporting a venue edit is a smaller failure than telling the
    owner a meeting is unchanged when the place in it was rewritten.
    """
    return str(agreed) == str(observed)


def compare(agreement: "Agreement", event) -> tuple[str, dict, list[dict]]:
    """(status, observed, drift) for one agreement against the world as it now reads.

    Pure: it takes the event and returns what differs. The caller decides what to do with it, which
    is nothing — the whole point of the feature is that noticing is not acting.
    """
    observed = _observed(event)
    drift: list[dict] = []
    if not same_instant(agreement.start, observed["start"]):
        drift.append({"field": "start", "agreed": agreement.start, "observed": observed["start"]})
    if not same_instant(agreement.end, observed["end"]):
        drift.append({"field": "end", "agreed": agreement.end, "observed": observed["end"]})
    if not _same_location(agreement.location, observed["location"]):
        drift.append({"field": "location", "agreed": agreement.location,
                      "observed": observed["location"]})
    if not _same_guests(agreement.attendees, observed["attendees"]):
        drift.append({"field": "attendees", "agreed": [str(a) for a in agreement.attendees],
                      "observed": observed["attendees"]})
    return (DRIFTED if drift else HOLDING), observed, drift


@dataclass(frozen=True)
class Agreement:
    """One meeting the owner agreed to, and the last thing anyone saw of it.

    `error` is the read failure that made this `unchecked`, and is separate from `note` because it
    is stored and the note is derived — a note that were stored could go stale against the status
    it explains, and a note that explains a status is exactly the thing that must not drift.
    """

    id: str
    change_id: str
    event_id: str
    title: str
    start: str
    end: str
    location: str
    attendees: tuple[str, ...]
    status: str = UNCHECKED
    observed: dict | None = None
    drift: tuple[dict, ...] = ()
    checked_at: str = ""
    error: str = ""
    revision: int = 0
    #: The execution that recorded this row, in the order the executions STARTED. It is what makes
    #: "the newer agreement wins" a fact about the run rather than about who happened to write
    #: last: a run that began earlier and finished later carries the smaller number and is dropped.
    #: Stored in the payload, like `revision`, for the same reason — a number two writers keep in
    #: memory is a number they can disagree about.
    sequence: int = 0
    #: True for a row recorded from a bundle that did not say who the update would notify. The
    #: guest list is then UNKNOWN rather than empty, and the row is `unchecked` because nothing can
    #: be compared against a list nobody wrote down. Not the same fact as `corrupt`: this row is
    #: this build's own record, and a later execution replaces it normally.
    unreviewed: bool = False
    #: True only for a row built from a stored record this build cannot PARSE. Such a row is never
    #: written — it is the store's way of serving a record it cannot read as a row the owner can
    #: see, instead of answering "agreements: []" over it. Kept out of `_dump`/`_load` on purpose:
    #: nothing should ever be able to persist this flag, and a payload that carried it would be a
    #: claim about the READER rather than about the record.
    corrupt: bool = False

    @property
    def note(self) -> str:
        """Why this is not checked, or "" when the status speaks for itself.

        Only ever populated for `unchecked`: "holding" and "drifted" are answers, and a note
        attached to an answer is a second thing the owner has to read in order to disbelieve the
        first.
        """
        if self.status != UNCHECKED:
            return ""
        if self.unreviewed:
            return NOTE_NO_REVIEWED_GUESTS
        if self.corrupt:
            return (f"this stored record could not be read ({self.error}), so nothing has compared "
                    f"it; the record itself is still on disk and has not been overwritten")
        if self.error:
            return (f"the event could not be read, so nothing was compared: {self.error}")
        return NOTE_NEVER_CHECKED

    def agreed(self) -> dict:
        """What the owner agreed to, in the contract's four fields."""
        return {"start": self.start, "end": self.end, "location": self.location,
                "attendees": list(self.attendees)}

    def as_dict(self) -> dict:
        """The API shape. `docs/API_V2.md` is the contract and this is the one place it is built.

        `event_id` is served on top of the documented shape: it is what makes the row actionable
        (the client can name the event), it is not a secret the owner cannot already read, and it
        is additive — a client written against the documented subset is unaffected."""
        out = {"id": self.id, "change_id": self.change_id, "event_id": self.event_id,
               "title": self.title, "status": self.status, "agreed": self.agreed(),
               "observed": dict(self.observed) if self.observed is not None else None,
               "drift": [dict(d) for d in self.drift], "checked_at": self.checked_at}
        note = self.note
        if note:
            out["note"] = note
        return out


def _dump(agreement: Agreement) -> str:
    """Every field, explicitly. Not `as_dict()`: the stored record and the served shape are
    different things, and sharing one encoder is how a field added for the wire becomes a field the
    store silently depends on.

    `revision` is in here because the store's compare-and-swap needs it INSIDE the row: a revision
    the writer kept in memory and the reader never saw is a number two processes can disagree
    about. `corrupt` is deliberately not: it describes this build's inability to read a row, not
    the row.
    """
    return json.dumps({"v": SCHEMA_VERSION,
                       "id": agreement.id, "change_id": agreement.change_id,
                       "event_id": agreement.event_id, "title": agreement.title,
                       "start": agreement.start, "end": agreement.end,
                       "location": agreement.location,
                       "attendees": [str(a) for a in agreement.attendees],
                       "status": agreement.status, "observed": agreement.observed,
                       "drift": [dict(d) for d in agreement.drift],
                       "checked_at": agreement.checked_at, "error": agreement.error,
                       "revision": int(agreement.revision),
                       "sequence": int(agreement.sequence),
                       "unreviewed": bool(agreement.unreviewed)},
                      ensure_ascii=False)


def _load(payload: str, row_id: str) -> Agreement:
    """One stored payload, or `UnreadableAgreement` for anything this build cannot read.

    `row_id` is the key the row lives under, and it is a REQUIRED argument rather than an optional
    cross-check: a caller that forgot it would silently restore the trust this parameter exists to
    remove. The review's Medium is a row keyed `agr-A` whose payload claims `id="agr-B"`: served,
    it is a duplicate of the real B, and checking it writes its disguised payload over B's own
    record. A row whose key and payload disagree is a corrupt row, not a row about B.

    Three kinds of payload are refused rather than interpreted, and the shared reason is that
    interpreting any of them would destroy the only copy of somebody else's record:

    * a key that disagrees with the payload's own id;
    * a status this build does not have — `unchecked` would be a DOWNGRADE of a record a newer build
      called something specific, and the next check would write the downgrade back;
    * a payload from a later schema (a higher `v`, or any field this build does not write). Fields
      this build has never heard of are the newer build's record of something, and a record read as
      a subset of itself and then written back is that something, deleted.
    """
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise UnreadableAgreement(f"agreement {row_id}: stored payload is not an object")
    if str(raw.get("id")) != str(row_id):
        raise UnreadableAgreement(
            f"agreement {row_id}: the stored payload names itself {raw.get('id')!r}, so this row is "
            f"not a record of {row_id} and cannot be served or rewritten as one")
    unknown = set(raw) - _FIELDS_WRITTEN
    if unknown:
        raise UnreadableAgreement(
            f"agreement {row_id}: written by a build that knows fields this one does not "
            f"({', '.join(sorted(unknown))})")
    version = int(raw.get("v") or 1)
    if version > SCHEMA_VERSION:
        raise UnreadableAgreement(
            f"agreement {row_id}: stored as schema v{version}, this build reads v{SCHEMA_VERSION}")
    status = str(raw["status"])
    if status not in STATUSES:
        raise UnreadableAgreement(
            f"agreement {row_id}: status {status!r} is not one this build has; reading it as "
            f"{UNCHECKED} would discard what a newer build recorded and write the downgrade back")
    # `unreviewed` decides whether this row is CHECKED at all, so a value this build did not write
    # must not be interpreted as a decision about that. `bool("false")` is True, so the coercion
    # this replaces turned a structurally invalid value into "skip this row forever", silently —
    # which is the one answer that never resolves itself. Absent is a different thing and stays
    # readable: a payload without the field is a payload from before the field, exactly as
    # `revision` and `sequence` are read as 0 when they are not there.
    unreviewed = raw.get("unreviewed", False)
    if not isinstance(unreviewed, bool):
        raise UnreadableAgreement(
            f"agreement {row_id}: the stored `unreviewed` is {unreviewed!r}, which is not a "
            f"boolean, and this field decides whether the row is checked at all")
    return Agreement(
        id=str(raw["id"]), change_id=str(raw.get("change_id") or ""),
        event_id=str(raw["event_id"]), title=str(raw.get("title") or ""),
        start=str(raw.get("start") or ""), end=str(raw.get("end") or ""),
        location=str(raw.get("location") or ""),
        attendees=tuple(str(a) for a in (raw.get("attendees") or ())),
        status=status,
        observed=raw.get("observed") if isinstance(raw.get("observed"), dict) else None,
        drift=tuple(d for d in (raw.get("drift") or ()) if isinstance(d, dict)),
        checked_at=str(raw.get("checked_at") or ""), error=str(raw.get("error") or ""),
        # A row written before this field existed reads as revision 0 — no write has recorded a
        # revision for it yet — and the next write through `save` moves it to 1.
        revision=int(raw.get("revision") or 0),
        sequence=int(raw.get("sequence") or 0),
        unreviewed=unreviewed)


class UnreadableAgreement(RuntimeError):
    """A row this build cannot parse. Raised instead of overwritten, so the only copy of whatever
    was written there survives a build that cannot read it."""


def _parse_row(agreement_id: str, payload: str) -> Agreement:
    """`_load`, with every failure turned into `UnreadableAgreement`.

    The distinction `_load` draws — a row this build cannot read versus a row it can — is the one
    that decides whether a write may happen at all, so a `KeyError` from a truncated payload and a
    deliberate refusal have to arrive at `save` as the same answer. Anything else would make "this
    build does not understand that record" a traceback out of a write path.
    """
    try:
        return _load(payload, agreement_id)
    except UnreadableAgreement:
        raise
    except Exception as exc:                                          # noqa: BLE001
        raise UnreadableAgreement(
            f"agreement {agreement_id} exists on disk but this build cannot read it; refusing to "
            f"replace the only copy of that record") from exc


class AgreementStore:
    """The agreements, in SQLite beside the other live stores.

    A store rather than a field because "the product remembers what you agreed to" is a claim about
    a restart, and an in-memory list makes it a claim about a session. One row per agreement,
    replaced whole — a record is read as a unit or not at all, and a field-at-a-time update would
    only buy the chance of reading half of one.
    """

    def __init__(self, *, conn: sqlite3.Connection | None = None, path: str | None = None,
                 now=None, cas_timeout: float = DEFAULT_CAS_TIMEOUT_SECONDS) -> None:
        self._lock = threading.Lock()
        #: How long `save` keeps retrying before it raises `AgreementStoreBusy`. A parameter rather
        #: than a constant so the deadline can be tested without a test that sleeps for it.
        self._cas_timeout = float(cas_timeout)
        self._conn = conn if conn is not None else sqlite3.connect(
            path or ":memory:", check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # The execution counter starts at zero once, and is then the store's own fact: a restart
        # must not hand out a number an earlier run already used, because the ordering it carries
        # would then be a lie about which execution began first.
        self._conn.execute("INSERT INTO agreement_sequence(n) "
                           "SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM agreement_sequence)")
        self._conn.commit()
        self._clock = now or (lambda: _dt.datetime.now().astimezone())
        #: Records this store could not read, one entry per row, refreshed on every scan. Reported
        #: rather than skipped in silence: an agreement nobody can parse is an agreement that is no
        #: longer protecting anything, and a store that answered "agreements: []" over one would be
        #: reporting a clean slate it does not have.
        self.unreadable: list[dict] = []

    # ---- writing ------------------------------------------------------------------------------
    def begin_execution(self) -> int:
        """This execution's sequence number, in the order executions START.

        The number is taken before the provider is touched, which is the whole of its value: the
        run that began first keeps the smaller number however long it then takes, so a run that
        paused and resumed after a newer one recorded its agreement cannot put the obsolete window
        back. Handed out by the STORE rather than by the clock so that two server workers, and two
        processes, agree on what "earlier" means.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("UPDATE agreement_sequence SET n = n + 1")
                row = self._conn.execute("SELECT n FROM agreement_sequence").fetchone()
            except BaseException:                                     # noqa: BLE001
                self._conn.rollback()
                raise
            self._conn.commit()
        return int(row[0])

    def record(self, *, change_id: str, approved: dict, event,
               sequence: int | None = None) -> Agreement:
        """The agreement a just-executed change leaves behind.

        Called only from the path where the invite moved AND the read-back confirmed it — but what
        is written down is what the OWNER APPROVED, taken from the bundle the owner said yes to
        (`approved`, built by `approved_view`), and NOT what the provider read back afterwards.

        The difference is not pedantry. The write and the read-back are two calls, and anyone with
        write access to the invite can edit it in between: the review's High is a stranger added to
        the guest list after the approved PATCH and before the GET, recorded as though the owner had
        agreed to invite them. A record that adopted the read-back would put that name in the list
        the owner is holding the other side to — a false statement about the owner's own decision,
        made from a value the adversary controls.

        So the read-back is COMPARED, not adopted. The approved values are stored, and the world as
        it now reads is kept beside them, so a difference lands as `drifted` on the very first
        reading rather than being invisible until the next check. An agreement that overrode the
        owner's decision with whatever the provider last served would be the one failure this whole
        feature exists to prevent, arriving through the front door.
        """
        observed = _observed(event)
        reviewed = approved.get("attendees")
        agreement = Agreement(
            id=f"agr-{approved['event_id']}", change_id=change_id,
            event_id=str(approved["event_id"]), title=str(approved.get("title") or ""),
            start=str(approved.get("start") or ""), end=str(approved.get("end") or ""),
            location=str(approved.get("location") or ""),
            attendees=() if reviewed is None else tuple(str(a) for a in reviewed),
            status=HOLDING, observed=observed, drift=(), unreviewed=reviewed is None,
            checked_at=self._clock().isoformat(timespec="seconds"))
        if reviewed is None:
            # This bundle did not record the list the owner reviewed (`notifies: None` is the same
            # fact stated for the client). Two things must NOT happen here, and the review's Medium
            # is that the first one did: adopting `observed["attendees"]` would store the provider's
            # guest list — the one an editor racing this write controls — as the owner's decision.
            # The second is claiming `holding`: a match cannot be established against a list nobody
            # wrote down. So the row is recorded `unchecked`, with no reading attached, and says
            # why. An execution whose bundle NOTIFIES cannot reach this branch at all, because
            # `ChangeSetStore.approve` refuses one that does not name who it will email.
            return self._store(replace(agreement, status=UNCHECKED, observed=None), sequence)
        # The just-taken read is the comparison. Recording `holding` without making it would be
        # asserting a match this run never established, which is how the racing guest list above
        # would have been signed off as agreed.
        status, _now, drift = compare(agreement, event)
        return self._store(replace(agreement, status=status, drift=tuple(drift)), sequence)

    def mark_unchecked(self, *, change_id: str, approved: dict, error: str,
                       sequence: int | None = None) -> Agreement | None:
        """Best effort, after the invite moved and the agreement could not be written.

        The owner is owed the difference between "nothing has compared this" and "the world was
        changed and the record of what was agreed could not be written". This writes the second
        one: an `unchecked` row carrying the failure, so the next `check` reports it instead of the
        event simply being absent from a list the owner reads as complete.

        Best effort is the contract. This is called on a path where the calendar effect has already
        been confirmed `DONE`, and an exception from here would abort a run whose external effect
        really happened — the failure mode the review's Medium is about. A row this build cannot
        read is already served as `unchecked`, which is the answer this method exists to produce,
        so `UnreadableAgreement` is swallowed rather than escalated.

        The reviewed guest list is carried over exactly as `record` carries it, and the reason is
        the review's Medium. `()` is not "unknown" in this store: it is the claim that the owner
        agreed to NO guests, and the next check measures the calendar against it. A fallback row
        that wrote `()` therefore invented a baseline nobody agreed to, and reported the owner's own
        guest list as drift from it. Where the bundle named nobody there is no baseline at all, and
        the row says so the same way `record` does — `unreviewed`, which is what stops a list nobody
        wrote down from being compared against. The failure this row carries is in `error` and in
        the decision log; the guest list is not the thing to spend on saying it.
        """
        reviewed = approved.get("attendees")
        agreement = Agreement(
            id=f"agr-{approved.get('event_id', '')}", change_id=change_id,
            event_id=str(approved.get("event_id") or ""), title=str(approved.get("title") or ""),
            start=str(approved.get("start") or ""), end=str(approved.get("end") or ""),
            location=str(approved.get("location") or ""),
            attendees=() if reviewed is None else tuple(str(a) for a in reviewed),
            status=UNCHECKED, unreviewed=reviewed is None,
            error=error, checked_at=self._clock().isoformat(timespec="seconds"))
        try:
            return self.save(agreement, newer_than=sequence)
        except UnreadableAgreement:
            return None

    def _store(self, agreement: Agreement, sequence: int | None) -> Agreement:
        """`save`, and then the truth about the row: a write dropped because an older execution lost
        to a newer one leaves the NEWER record in place, and that is what this returns."""
        saved = self.save(agreement, newer_than=sequence)
        if saved is not None:
            return saved
        return self.get(agreement.id) or agreement

    def _read_row(self, agreement_id: str):
        """The stored payload for one row, or None.

        A method rather than an inline query because it is the seam the concurrency test
        interleaves two writers at: whether the read and the write are ONE step is invisible to a
        single-threaded test, and this is the only point at which two writers can be made to
        collide in the window the review reproduced.
        """
        return self._conn.execute("SELECT payload FROM agreements WHERE id=?",
                                  (agreement_id,)).fetchone()

    def save(self, agreement: Agreement, *, expect: int | None = None,
             newer_than: int | None = None) -> Agreement | None:
        """One row per agreement, replaced whole, under a compare-and-swap.

        `expect` is the revision the caller READ, and a mismatch means somebody else wrote this
        record while the caller was away — which for a check means it is holding an answer about an
        agreement that is no longer the owner's. The write is then DROPPED and `None` is returned,
        rather than raised: a check that raced an execution has nothing to report as an error, and
        the honest outcome is the newer record standing and the stale reading going nowhere.

        `expect=None` means "unconditional, but still bump the revision", which is what `record`
        wants: an execution IS the newer truth for that event, so it does not compare against
        anything — it moves the row on.

        `newer_than` is the sequence the caller's EXECUTION was given when it started, and it is a
        different question from `expect`. An execution does not compare revisions — it IS the newer
        truth for that event and moves the row on — but it may not be the newest execution, and the
        write is dropped when the stored row already came from a run that began later. The two
        cannot be folded together: a revision orders WRITES, and the review's High is precisely the
        case where the later write carries the earlier intent.

        The swap is one statement. The row is read, the decision is made in Python, and the write is
        conditional on the row still being the exact bytes that decision was made about
        (`WHERE id=? AND payload=?`, with the `INSERT` guarded by the primary key for a first
        write) — so two connections that both read revision 1 cannot both write revision 2. The
        read must stay outside a transaction for the payload comparison to be the thing that
        decides: a lock held from the read would serialise these callers but would not make the
        decision atomic for a writer in another process that never takes it.

        A row this build cannot read is never replaced either way: the original bytes are the only
        evidence of what was there, and `_parse_row` refuses before any write is attempted.

        A collision is retried until one of three things is true, and a fixed number of ATTEMPTS is
        not among them: the number of writers a row has is not a number this store knows, so an
        attempt budget is a rule that the (budget + 1)th writer loses — and that writer is the
        newest execution, whose record of the meeting is the final one. The loop therefore ends
        when our write lands, when the row we read is one `_may_replace` refuses (a stored execution
        at or after ours, or a revision a check did not read — the one case where dropping the write
        is the right answer), or when the deadline passes and `AgreementStoreBusy` is raised.
        """
        deadline = time.monotonic() + self._cas_timeout
        while True:
            row = self._read_row(agreement.id)
            stored_payload = None if row is None else row["payload"]
            # The row was fetched by this key, so the key IS `agreement.id` — passing anything else
            # would be checking the payload against a name this call invented.
            stored = None if row is None else _parse_row(agreement.id, stored_payload)
            if not self._may_replace(agreement.id, stored, expect=expect, newer_than=newer_than):
                return None
            written = replace(
                agreement, revision=(0 if stored is None else stored.revision) + 1,
                # An execution stamps the number it was given; a check keeps the one it read, so
                # that a check never re-orders runs it did not take part in.
                sequence=(int(newer_than) if newer_than is not None else int(agreement.sequence)))
            if stored is None:
                try:
                    self._conn.execute("INSERT INTO agreements(id, payload) VALUES(?, ?)",
                                       (written.id, _dump(written)))
                    self._conn.commit()
                    return written
                except sqlite3.IntegrityError:
                    # Somebody created the row between the read and the insert. The primary key is
                    # what makes a first write safe; the loop decides against what they wrote.
                    pass
            else:
                cursor = self._conn.execute(
                    "UPDATE agreements SET payload=? WHERE id=? AND payload=?",
                    (_dump(written), written.id, stored_payload))
                self._conn.commit()
                if cursor.rowcount == 1:
                    return written
                # The row is no longer the bytes this decision was made about: another writer got
                # there first. Re-read and decide against THEIR record, rather than against the one
                # this call happened to see.
            if time.monotonic() >= deadline:
                raise AgreementStoreBusy(
                    f"agreement {agreement.id}: another writer kept replacing this row for longer "
                    f"than {self._cas_timeout:g}s, so this write was not made and the stored row "
                    f"stands (revision {stored.revision if stored is not None else 'gone'})")

    def _may_replace(self, agreement_id: str, stored: Agreement | None, *, expect: int | None,
                     newer_than: int | None) -> bool:
        """Whether this write is still allowed, given what was read."""
        if expect is not None and (stored is None or stored.revision != int(expect)):
            log.info("dropping a stale agreement check: id=%s read=%s stored=%s",
                     agreement_id, expect, "gone" if stored is None else stored.revision)
            return False
        if newer_than is not None and stored is not None and stored.sequence >= int(newer_than):
            log.info("dropping an agreement from an older execution: id=%s started=%s stored=%s",
                     agreement_id, newer_than, stored.sequence)
            return False
        return True

    # ---- reading ------------------------------------------------------------------------------
    def scan(self) -> list[Agreement]:
        """Every READABLE agreement, oldest id first, refreshing `unreadable` as it goes.

        Ordered rather than incidental: a client that renders this list must not reorder it between
        two identical reads, and `SELECT` without `ORDER BY` is free to.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, payload FROM agreements ORDER BY id").fetchall()
        found, unreadable = [], []
        for row in rows:
            try:
                found.append(_load(row["payload"], _row_id(row)))
            except Exception as exc:                                  # noqa: BLE001
                log.warning("unreadable agreement record: id=%s error=%s", row["id"], exc)
                unreadable.append({"id": row["id"], "error": f"{type(exc).__name__}"})
        self.unreadable = unreadable
        return found

    def rows(self) -> list[Agreement]:
        """`scan()`, plus one row per record this build cannot read.

        The review's Medium: `scan` logged the unreadable rows and then answered with the readable
        ones, so a store holding nothing but a corrupt payload served `{"agreements": []}` — a
        clean slate it did not have, and the owner's only warning that their record is gone. A row
        that cannot be parsed is still a row the owner has, so it is served as what it is: an
        `unchecked` agreement, under the stored row's own id, with a note naming the failure. The status is not
        invented for the occasion — `unchecked` is this module's word for "nothing here has
        compared it", and nothing has.
        """
        found = self.scan()
        ghosts = [Agreement(id=str(row["id"]), change_id="", event_id="", title="", start="",
                            end="", location="", attendees=(), error=str(row["error"]),
                            corrupt=True)
                  for row in self.unreadable]
        return sorted(found + ghosts, key=lambda a: a.id)

    def list(self) -> list[Agreement]:
        return self.scan()

    def get(self, agreement_id: str) -> Agreement | None:
        for agreement in self.scan():
            if agreement.id == agreement_id:
                return agreement
        return None


def approved_view(action) -> dict:
    """What the owner approved, read out of the bundle's own calendar action.

    Two halves of one action, and they answer different questions. `arguments` is what will be
    WRITTEN — the window and the place the owner pressed yes to, and the fields the executor's
    read-back check confirms with `same_instant`. `display` is what the owner was SHOWN, and it is
    the only place the reviewed title and guest list exist at all: neither is an argument of a
    calendar update (the update moves a meeting, it does not rename or re-invite one), so neither
    is anywhere else in the bundle.

    `attendees` is `None` when this build did not record who the update would notify. That is not
    an empty list: "nobody will be told" and "nobody wrote down who will be told" are different
    claims, and `record` treats them differently. A bundle that notifies cannot be approved with
    the list missing, so this is the shape of a producer that did not record it, not of a plan the
    owner reviewed without guests.
    """
    args, display = action.arguments, action.display
    reviewed = display.get("attendees")
    return {"event_id": str(args.get("event_id", "")),
            "title": str(display.get("title") or ""),
            "start": str(args.get("start", "")), "end": str(args.get("end", "")),
            "location": str(args.get("location", "")),
            "attendees": (None if reviewed is None
                          else tuple(str(a) for a in reviewed))}


class AgreementChecker:
    """One pass over the agreements, comparing each against the world.

    Read-only by construction: the only methods it may call on the calendar are reads, and it holds
    no mail client, no model and no broker at all. That is not a convention to be remembered — it
    is the absence of the collaborators an action would need, so "drift causes no action" is a
    property of the object graph rather than a rule someone has to keep.
    """

    def __init__(self, store: AgreementStore, calendar, *, now=None) -> None:
        self.store, self.calendar = store, calendar
        self._clock = now or (lambda: _dt.datetime.now().astimezone())

    def _stamp(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def check(self) -> list[Agreement]:
        """Every agreement, after re-reading the world. Never raises for an unreadable event: one
        meeting whose provider call failed must not stop the others being checked, and turning a
        partial check into a total failure would hide the ones that did work."""
        out = []
        for agreement in self.store.list():
            out.append(self._one(agreement))
        return out

    def _one(self, agreement: Agreement) -> Agreement:
        if agreement.unreviewed:
            # Nothing to compare AGAINST: the bundle never said who the owner reviewed, so the one
            # field a read-back could be measured on has no other side. Comparing anyway would
            # report `drifted` on the guests of an agreement nobody made, which is a false alarm
            # the owner cannot act on — and it would quietly replace the note that says why with a
            # drift list that says nothing true.
            return agreement
        try:
            event = self.calendar.get_event(agreement.event_id)
        except Exception as exc:                                      # noqa: BLE001
            # `unchecked`, and the failure is carried rather than summarised: "it could not be
            # read" and "it was read and still matches" are the two answers it would be worst to
            # confuse, and `observed` is cleared so no stale reading is served as a current one.
            return self._write(agreement, replace(
                agreement, status=UNCHECKED, observed=None, drift=(),
                error=f"{type(exc).__name__}: {exc}", checked_at=self._stamp()))
        status, observed, drift = compare(agreement, event)
        return self._write(agreement, replace(agreement, status=status, observed=observed,
                                              drift=tuple(drift), error="", checked_at=self._stamp()))

    def _write(self, read: Agreement, result: Agreement) -> Agreement:
        """Persist one check result, unless the agreement moved while the world was being read.

        The swap is against the revision `read` was loaded at. A mismatch means an execution
        recorded a NEWER agreement for this event between the read and the write, and the result in
        hand describes a record the owner no longer holds — writing it would resurrect the
        superseded window, and because every later check would compare against that resurrected
        baseline, the mistake would not be a transient one. The stale result is dropped.

        What is returned is therefore what is STORED now, which for a dropped result is the newer
        agreement that won. `check()` promises each caller the truth about the row, and the truth
        after a lost race is the winner's record, not the reading this pass happened to take.
        """
        saved = self.store.save(result, expect=read.revision)
        if saved is not None:
            return saved
        return self.store.get(result.id) or result
