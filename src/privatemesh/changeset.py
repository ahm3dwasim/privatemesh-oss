"""The change set — one review, two effects, one version.

The direction asks for approval that binds each capability to the owner, the recipients, the
exact content, the event id, the event ETag, the private-boundary version and an expiry, and for
any edit to invalidate a prior approval. That is this module.

A change set is a VERSIONED bundle, not a flag. Editing the reply, or the time, mints version
n+1 and invalidates version n — and so does anything outside the bundle that it was built
against: a moved event (the ETag changed) or an edited protected commitment (the boundary
version changed, which is why the boundary is versioned at all — the meeting's ETag does not
move when the owner's constraint does).

Each action carries its OWN approval id from `approval.ApprovalStore`, so the bundle never
becomes a class of permissions: `execute` spends them one at a time, and `approval.consume`
refuses an approval presented for different arguments even if the version check were somehow
bypassed. When an edit supersedes an action, its approval is not merely forgotten — it is
spent or denied, because an approval nobody can spend is still an approval somebody could.

`approve` never trusts what it is told about the world: the caller passes the event ETag and the
boundary version it just READ, and this module compares them to what the bundle was built
against. Reading fresh state immediately before admission is the caller's job; refusing to
believe a stale claim is this module's.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass

from .approval import APPROVED, CONSUMED, PENDING, ApprovalStore
from .txn import transaction

PENDING_STATUS, APPROVED_STATUS = "pending", "approved"
EXECUTING_STATUS, DONE_STATUS, PARTIAL_STATUS, STOPPED_STATUS = (
    "executing", "done", "partial", "stopped")

CALENDAR_UPDATE, EMAIL_REPLY = "calendar_update", "email_reply"

#: How long an unspent bundle stays approvable. Long enough for the walk to the phone and back,
#: short enough that "yes" cannot be pinned to a plan from last week.
DEFAULT_TTL_SECONDS = 15 * 60


class StaleChangeSet(RuntimeError):
    """The bundle the owner is looking at is not the bundle that would run. HTTP 409."""


@dataclass(frozen=True)
class Action:
    kind: str
    arguments: dict
    approval_id: str
    display: dict

    def as_dict(self) -> dict:
        return {"kind": self.kind, **self.display}


@dataclass(frozen=True)
class ChangeSet:
    id: str
    change_id: str
    task_id: str
    version: int
    status: str
    calendar: Action
    email: Action
    event_etag: str
    boundary_id: str
    boundary_version: int
    created_at: str
    expires_at: str
    approved_at: str | None = None
    outcome: dict | None = None
    #: The standing-limit grant this bundle was approved under, or `None` when the owner approved it
    #: themselves. It carries the mode and its revision, the clause verdicts, and a digest of the
    #: exact change+bundle state those verdicts were reached on. Written by the SAME statement that
    #: moves the bundle to `approved`, so "the limits said yes" and "this version is approved"
    #: cannot be true of two different versions — the review's High is what happens when they can.
    limit_grant: dict | None = None

    def actions(self) -> list[Action]:
        """Calendar first. The order is the execution order, and it is stated once, here."""
        return [self.calendar, self.email]

    def action(self, kind: str) -> Action:
        for a in self.actions():
            if a.kind == kind:
                return a
        raise KeyError(kind)

    def as_dict(self) -> dict:
        return {"change_id": self.change_id, "version": self.version, "status": self.status,
                "created_at": self.created_at, "expires_at": self.expires_at,
                "approved_at": self.approved_at, "boundary_version": self.boundary_version,
                "limit_grant": self.limit_grant,
                "actions": [a.as_dict() for a in self.actions()]}


#: `{t}` is the table's qualified name — `changesets` normally, `change_sets.changesets` when the
#: store shares the change store's connection. Formatted rather than string-replaced: a blind
#: `.replace("changesets", ...)` also rewrites the index name and produces `idx_change_sets.…`.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t}(
    id TEXT NOT NULL, version INTEGER NOT NULL, change_id TEXT NOT NULL, task_id TEXT NOT NULL,
    status TEXT NOT NULL, payload TEXT NOT NULL, event_etag TEXT NOT NULL,
    boundary_id TEXT NOT NULL, boundary_version INTEGER NOT NULL, created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, approved_at TEXT, outcome TEXT,
    PRIMARY KEY (id, version));
CREATE INDEX IF NOT EXISTS {idx} ON changesets(change_id);
"""

#: `CREATE INDEX` cannot qualify the TABLE — SQLite puts the schema on the index name and
#: resolves the table inside that same schema — so the two placeholders differ by design.
_INDEX = {"": "idx_changesets_change", "change_sets.": "change_sets.idx_changesets_change"}


class ChangeSetStore:
    def __init__(self, approvals: ApprovalStore, *, conn: sqlite3.Connection | None = None,
                 path: str | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 schema: str = "") -> None:
        self.approvals = approvals
        self.ttl_seconds = int(ttl_seconds)
        self._lock = threading.Lock()
        self._conn = conn if conn is not None else sqlite3.connect(
            path or ":memory:", check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        #: The attached database this table lives in, when the caller shares one connection with
        #: the change store. Empty means "this connection's own database", which is how a
        #: standalone store (and every test that builds one) still works. The point of sharing is
        #: that a change's version bump and the change set it points at become ONE transaction —
        #: SQLite transactions are per-connection, so two files opened separately can never give
        #: that guarantee no matter how the writes are ordered.
        self._schema = f"{schema}." if schema else ""
        self._table = f"{self._schema}changesets"
        self._conn.executescript(
            _SCHEMA.format(t=self._table, idx=_INDEX.get(self._schema, "idx_changesets_change")))
        self._conn.commit()

    # ---- write -------------------------------------------------------------------------------
    def create(self, *, change_id: str, task_id: str, calendar_args: dict, email_args: dict,
               approval_ids: tuple[str, str], event_etag: str, boundary_id: str,
               boundary_version: int, calendar_display: dict | None = None,
               email_display: dict | None = None, now: str | None = None) -> ChangeSet:
        cal_id, mail_id = approval_ids
        return self._insert(
            id=uuid.uuid4().hex, change_id=change_id, task_id=task_id, version=1,
            calendar=Action(CALENDAR_UPDATE, dict(calendar_args), cal_id,
                            calendar_display or _calendar_display(calendar_args)),
            email=Action(EMAIL_REPLY, dict(email_args), mail_id,
                         email_display or _email_display(email_args)),
            event_etag=event_etag, boundary_id=boundary_id, boundary_version=int(boundary_version),
            now=now)

    def edit(self, cs_id: str, *, approval_ids, calendar_args: dict | None = None,
             email_args: dict | None = None, calendar_display: dict | None = None,
             email_display: dict | None = None, now: str | None = None) -> ChangeSet:
        """Mint version n+1 from an edit, and make sure version n cannot be spent.

        The superseded action's approval is DENIED if it is still pending and SPENT if the owner
        had already said yes: either way it stops being an approval. Leaving it approved would
        leave a live authorization pointing at arguments the owner has since changed."""
        old = self.latest(cs_id)
        if old is None:
            raise KeyError(f"no change set '{cs_id}'")
        new_cal_args = dict(calendar_args if calendar_args is not None else old.calendar.arguments)
        new_mail_args = dict(email_args if email_args is not None else old.email.arguments)
        keep = set(approval_ids)
        for action in old.actions():
            if action.approval_id not in keep:
                self._invalidate(action)
        cal_id, mail_id = approval_ids
        return self._insert(
            id=old.id, change_id=old.change_id, task_id=old.task_id, version=old.version + 1,
            calendar=Action(CALENDAR_UPDATE, new_cal_args, cal_id,
                            calendar_display or _calendar_display(new_cal_args)),
            email=Action(EMAIL_REPLY, new_mail_args, mail_id,
                         email_display or _email_display(new_mail_args)),
            event_etag=old.event_etag, boundary_id=old.boundary_id,
            boundary_version=old.boundary_version, now=now)

    def _invalidate(self, action: Action) -> None:
        current = self.approvals.get(action.approval_id)
        if current is None or current.status in (CONSUMED, "denied"):
            return
        if current.status == APPROVED:
            # Spend it on its own exact arguments: the single-use rule already knows how to make
            # an approval dead, and reusing it keeps one mechanism instead of two.
            self.approvals.consume(action.approval_id, task_id=current.task_id, tool=current.tool,
                                   arguments=current.arguments)
        else:
            self.approvals.deny(action.approval_id)

    def _insert(self, *, id: str, change_id: str, task_id: str, version: int, calendar: Action,
                email: Action, event_etag: str, boundary_id: str, boundary_version: int,
                now: str | None) -> ChangeSet:
        created = _now(now)
        expires = _iso(_dt.datetime.fromisoformat(created)
                       + _dt.timedelta(seconds=self.ttl_seconds))
        cs = ChangeSet(id=id, change_id=change_id, task_id=task_id, version=version,
                       status=PENDING_STATUS, calendar=calendar, email=email,
                       event_etag=event_etag, boundary_id=boundary_id,
                       boundary_version=int(boundary_version), created_at=created,
                       expires_at=expires)
        with self._lock, transaction(self._conn):
            self._conn.execute(
                "INSERT INTO {t}(id, version, change_id, task_id, status, payload, "
                "event_etag, boundary_id, boundary_version, created_at, expires_at, approved_at, "
                "outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)".format(t=self._table),
                (cs.id, cs.version, cs.change_id, cs.task_id, cs.status, _dump(cs),
                 cs.event_etag, cs.boundary_id, cs.boundary_version, cs.created_at, cs.expires_at,
                 None, None))
        return cs

    def approve(self, cs_id: str, version: int, *, current_event_etag: str,
                current_boundary_version: int, now: str | None = None,
                limit_grant: dict | None = None) -> ChangeSet:
        """Say yes to exactly one version of exactly one bundle, or refuse with a reason.

        Every refusal names what moved, because "stale" alone would leave the owner unable to
        tell an edit they made from an event somebody else moved.

        `limit_grant` is the standing limits' yes instead of the owner's. It is written by the same
        `UPDATE` that sets `approved`, for the reason the whole module exists: an authorization and
        the version it authorizes have to be one write, or a reader can find a grant describing a
        version that is not the one approved."""
        cs = self.latest(cs_id)
        if cs is None:
            raise KeyError(f"no change set '{cs_id}'")
        stamp = _now(now)
        if int(version) != cs.version:
            raise StaleChangeSet(
                f"version {version} is not the current bundle (version {cs.version} is) — "
                f"the plan changed since it was shown")
        if cs.status != PENDING_STATUS:
            raise StaleChangeSet(f"this bundle is already {cs.status}")
        if _dt.datetime.fromisoformat(stamp) >= _dt.datetime.fromisoformat(cs.expires_at):
            raise StaleChangeSet(f"this bundle expired at {cs.expires_at}; prepare it again")
        if current_event_etag != cs.event_etag:
            raise StaleChangeSet(
                f"the invite changed since you reviewed it (etag {cs.event_etag} -> "
                f"{current_event_etag}); nothing was sent")
        if int(current_boundary_version) != int(cs.boundary_version):
            raise StaleChangeSet(
                f"your protected commitment changed since you reviewed this (version "
                f"{cs.boundary_version} -> {current_boundary_version}); nothing was sent")
        self._refuse_a_notifying_bundle_that_names_nobody(cs)
        for action in cs.actions():
            self.approvals.approve(action.approval_id)
        return self.mark(cs.id, cs.version, APPROVED_STATUS, approved_at=stamp,
                         limit_grant=limit_grant)

    def _refuse_a_notifying_bundle_that_names_nobody(self, cs: ChangeSet) -> None:
        """An update that emails the guest list may not be approved without one.

        `docs/API_V2.md` has said this since the agreements feature was written — "Missing calendar
        attendees prevents approval when notifications are enabled" — and until now nothing
        enforced it. The consequence was not a broken screen: an approved bundle is what the
        agreement is recorded FROM, so a notifying bundle that never named its guests produced a
        record whose guest list could only be filled in from the provider's read-back. That is the
        value an editor racing this write controls, and recording it would state, as the owner's
        own decision, an invitation the owner never saw.

        A bundle that does NOT notify is a different thing and is still approvable: `notifies: []`
        is a claim the produce-side can stand behind, and the agreement it leaves is recorded
        `unchecked` for its guests with a note saying why rather than being invented from a read.
        Refusing those too would refuse a plan that is honestly specifying "tell nobody".
        """
        args = cs.calendar.arguments
        if args.get("send_updates", "all") == "none":
            return
        if cs.calendar.display.get("attendees") is not None:
            return
        raise StaleChangeSet(
            "this update would email the invite's guests, but the plan does not say who they are "
            "— approving it would record an agreement to notify people nobody reviewed. Prepare "
            "it again with the invite's attendee list, or set send_updates to 'none'")

    def mark(self, cs_id: str, version: int, status: str, *, approved_at: str | None = None,
             outcome: dict | None = None, limit_grant: dict | None = None) -> ChangeSet:
        # Read BEFORE the lock: `get` takes the same non-re-entrant lock this method is about to
        # hold, so reading the row from inside the frame would deadlock against itself.
        payload = None
        if limit_grant is not None:
            current = self.get(cs_id, version)
            if current is not None:
                payload = _dump(dataclasses.replace(current, limit_grant=limit_grant))
        with self._lock, transaction(self._conn):
            self._conn.execute(
                "UPDATE {t} SET status=?, approved_at=COALESCE(?, approved_at), "
                "outcome=COALESCE(?, outcome), payload=COALESCE(?, payload) "
                "WHERE id=? AND version=?".format(t=self._table),
                (status, approved_at, json.dumps(outcome) if outcome is not None else None,
                 payload, cs_id, int(version)))
        return self.get(cs_id, version)

    def transaction(self):
        """This store's connection as ONE write transaction, for a caller that has to read state
        from elsewhere and then commit a decision about it.

        The standing limits are the caller: the mode lives in its own file, so its read cannot join
        this transaction, but the RE-READ of it can happen inside the frame — which is what makes
        "the mode was still `within_limits` when this bundle was approved" a fact about one
        ordered write rather than about two calls that happened to be adjacent."""
        return transaction(self._conn)

    # ---- read --------------------------------------------------------------------------------
    def latest(self, ref: str) -> ChangeSet | None:
        """The newest version of one bundle, by bundle id OR by change id.

        Two callers hold two different handles and mean the same thing. The executor holds a
        bundle id; the UI holds a change. Resolving both here — bundle id first, then the newest
        bundle for that change — keeps "the plan as it now stands" one question with one answer,
        instead of a lookup each caller has to get right.

        Versions share their bundle id, so this always returns the CURRENT version of it: a
        superseded version is reachable only by asking for it explicitly with `get`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM {t} WHERE id=? ORDER BY version DESC LIMIT 1".format(
                    t=self._table),
                (ref,)).fetchone()
        return _from_row(row) if row is not None else self.for_change(ref)

    def for_change(self, change_id: str) -> ChangeSet | None:
        """The bundle the UI should show for a change: the newest version of the newest bundle.

        `created_at` is stamped to the SECOND, so two bundles for one change created inside the same
        second tie on it — and the tie used to be broken by whatever order the table happened to
        return, which is not "newest" and is not stable. `rowid` is insertion order, so it is the
        tie-break that makes this question have one answer. That matters beyond the UI now: the
        effect ports bind their outbound fields to the bundle this returns, so an ambiguous answer
        here would be an ambiguous answer to "what did the owner approve"."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM {t} WHERE change_id=? ORDER BY created_at DESC, "
                "version DESC, rowid DESC LIMIT 1".format(t=self._table), (change_id,)).fetchone()
        return _from_row(row)

    def get(self, cs_id: str, version: int | None = None) -> ChangeSet | None:
        if version is None:
            return self.latest(cs_id)
        with self._lock:
            row = self._conn.execute(f"SELECT * FROM {self._schema}changesets WHERE id=? AND version=?",
                                     (cs_id, int(version))).fetchone()
        return _from_row(row)


def _calendar_display(args: dict, attendees=None) -> dict:
    """What the calendar action will do, including who it will EMAIL.

    `send_updates` is not a private detail of the plan: Google mails every attendee when the invite
    moves, so approving a calendar edit approves an outbound message to each of them. The review's
    Critical is that the standing limits authorized that fan-out without ever looking at the guest
    list; the fix has two halves and this is the half the OWNER sees. `notifies` names the
    addresses, so `ask_always` is an informed approval rather than one that has to be discovered in
    somebody else's inbox. The other half is in `ChangeEngine._clause_three`, which reads the guest
    list from the provider and never from here — a display the owner is shown can be incomplete
    without making the limit unsafe, and a limit that trusted the display would be.

    `attendees=None` means the caller did not know them, and is reported as such rather than as an
    empty list: "nobody will be told" and "this build did not record who will be told" are
    different claims, and only one of them is safe to render as an empty array.

    The `attendees` key is the same list under the name the REVIEW SCREEN's contract uses
    (`docs/API_V2.md`: "Calendar actions include `attendees: string[]`"), and it is the key
    `agreements.approved_view` reads to learn who the owner reviewed. A fallback that carried the
    list only under `notifies` would make a bundle built through this path record an agreement with
    no reviewed guests at all — the state `record` now refuses to fill in from the read-back.
    """
    notifying = args.get("send_updates", "all") != "none"
    return {"event_id": args.get("event_id", ""),
            "attendees": None if attendees is None else list(attendees),
            "diff": {"start": [None, args.get("start")], "end": [None, args.get("end")],
                     "location": [None, args.get("location")]},
            # Google notifies attendees unless told otherwise; the approval says so out loud
            # rather than letting the side effect be discovered in someone else's inbox.
            "notifies_attendees": notifying,
            "notifies": (None if attendees is None
                         else (list(attendees) if notifying else []))}


def _email_display(args: dict) -> dict:
    return {"to": list(args.get("to") or ()), "subject": args.get("subject", ""),
            "body": args.get("body", "")}


def _dump(cs: ChangeSet) -> str:
    return json.dumps({"calendar": {"kind": cs.calendar.kind, "arguments": cs.calendar.arguments,
                                    "approval_id": cs.calendar.approval_id,
                                    "display": cs.calendar.display},
                       "email": {"kind": cs.email.kind, "arguments": cs.email.arguments,
                                 "approval_id": cs.email.approval_id,
                                 "display": cs.email.display},
                       "limit_grant": cs.limit_grant}, ensure_ascii=False)


def _from_row(row: sqlite3.Row | None) -> ChangeSet | None:
    if row is None:
        return None
    blob = json.loads(row["payload"])
    cal, mail = blob["calendar"], blob["email"]
    return ChangeSet(
        id=row["id"], change_id=row["change_id"], task_id=row["task_id"],
        version=int(row["version"]), status=row["status"],
        calendar=Action(cal["kind"], cal["arguments"], cal["approval_id"], cal["display"]),
        email=Action(mail["kind"], mail["arguments"], mail["approval_id"], mail["display"]),
        event_etag=row["event_etag"], boundary_id=row["boundary_id"],
        boundary_version=int(row["boundary_version"]), created_at=row["created_at"],
        expires_at=row["expires_at"], approved_at=row["approved_at"],
        outcome=json.loads(row["outcome"]) if row["outcome"] else None,
        # `.get` rather than `[...]`: a bundle row written by a build that predates the grant has
        # no such key, and "no grant recorded" is the correct reading of it — an owner-approved
        # bundle, which is what every bundle was before the standing limits existed.
        limit_grant=blob.get("limit_grant"))


def _now(now: str | None = None) -> str:
    return now or _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _iso(moment: _dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")
