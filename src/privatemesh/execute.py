"""Execution — two external effects that are not a transaction, described as what they are.

The calendar update goes first because the reply says the invite moved; sending the reply first
would make the email the claim and the calendar the follow-up. After it, the event is READ BACK:
a provider that answers 200 and applies nothing is a real failure mode, and "the API said ok" is
not evidence that the meeting moved.

After that the reply is sent. If it fails, the summary says what actually happened —
"Invite updated. Reply not sent." — and if the failure is AMBIGUOUS (a timeout: the provider may
well have taken it) the send is reconciled against `MailPort.find_sent(idempotency_key)` before
anything else is decided, because the one outcome nobody can undo is the duplicate reply.

Every skill here is a normal registered skill, so each effect passes through the broker: a policy
decision, an intent receipt BEFORE the call, a completion receipt after, and an approval that
`approval.consume` will only spend once. The executor holds no side door to the provider — it
holds the broker.

States are per effect and honest: `unknown` exists because "we do not know" is a state, and
reporting it as failed would be as wrong as reporting it as done.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import secrets
from zoneinfo import ZoneInfo

from .approval import ApprovalError
from .agreements import approved_view, same_instant
from .audit import AuditLog
from .boundary import (RULE_CALENDAR_NOTE, ApprovedArgumentsRefused, CalendarDayUnknown,
                       CalendarRefused, Fragment,
                       OutboundEmail, foreign_fragments_of)
from .changeset import (APPROVED_STATUS, DONE_STATUS, EXECUTING_STATUS,
                        PARTIAL_STATUS, STOPPED_STATUS, ChangeSet, ChangeSetStore,
                        StaleChangeSet)
from .decisions import RULE_APPROVAL, RULE_DIGEST, RULE_EXECUTE, RULE_RECIPIENTS
from .ports import EtagMismatch, MailMessage, ReauthRequired, sender_identity
from .skills import Risk, Tool, ToolResult, guarded

log = logging.getLogger(__name__)

PENDING, RUNNING, DONE, FAILED, UNKNOWN = "pending", "running", "done", "failed", "unknown"

SUMMARY_BOTH = "Invite updated. Reply sent."
SUMMARY_PARTIAL = "Invite updated. Reply not sent."
SUMMARY_NONE = "Invite not updated. Reply not sent."

#: The outcome heading, one sentence per effect, each true of THAT effect's own state.
#:
#: The version this replaces asked one question — "is the email `done`?" — and answered every other
#: state with "Reply not sent." Two of those answers are not just coarse, they are false:
#:
#: * `unknown`, which is set when a send timed out after the request left. The message may already
#:   be in the recipient's inbox while the page's largest sentence says it was not sent. The sixth
#:   review's finding is that the receipt two clicks away already read "whether the reply was sent
#:   is unknown" while the heading over it said the opposite — and `web/app/page.tsx` renders this
#:   string as that heading.
#: * `running`, where the request is in flight and may have reached the provider. `pending` keeps
#:   "Reply not sent." because it is simply true — the step has not been attempted — and the three
#:   sentences the product has always used for the finished combinations are unchanged.
#:
#: Every state has its own entry rather than a fall-through, so a state added later is a missing key
#: rather than a default that reads like a fact.
CALENDAR_SENTENCE = {
    DONE: "Invite updated.",
    FAILED: "Invite not updated.",
    UNKNOWN: "Whether the invite was updated is unknown.",
    PENDING: "Invite not updated.",
    RUNNING: "The invite update is still running.",
}
EMAIL_SENTENCE = {
    DONE: "Reply sent.",
    FAILED: "Reply not sent.",
    UNKNOWN: "Whether the reply was sent is unknown.",
    PENDING: "Reply not sent.",
    RUNNING: "The reply is still being sent.",
}

#: Failures that leave the outcome genuinely unknown. A timeout is not a failure — it is the
#: absence of an answer, and the answer is what decides whether the message exists.
_AMBIGUOUS_RE = re.compile(
    r"timeout|timed out|timedout|connection reset|connection aborted|broken pipe|"
    r"temporarily unavailable|\b(502|503|504)\b", re.IGNORECASE)

CALENDAR_SKILL, EMAIL_SKILL = "calendar_update_event", "email_reply"


class NotApproved(RuntimeError):
    """The bundle is not in a state that may touch a provider. Nothing ran."""


class DigestLedgerMissing(RuntimeError):
    """A digest was asked for on a build with no ledger to count it against.

    Its own exception rather than a bare `RuntimeError` because "the once-a-day limit cannot be
    checked" is a policy refusal, not a defect report: the caller has to be able to say so, and a
    digest that went out because the counter was missing is the exact failure this exists for."""


class Effect:
    __slots__ = ("kind", "state", "detail", "provider_url", "idempotency_key")

    def __init__(self, kind: str, state: str = PENDING, detail: str = "", provider_url: str = "",
                 idempotency_key: str = "") -> None:
        self.kind, self.state, self.detail = kind, state, detail
        self.provider_url, self.idempotency_key = provider_url, idempotency_key

    def as_dict(self) -> dict:
        out = {"kind": self.kind, "state": self.state, "detail": self.detail}
        if self.provider_url:
            out["provider_url"] = self.provider_url
        return out


class Outcome:
    def __init__(self, change_id: str, version: int, effects: list[Effect], summary: str) -> None:
        self.change_id, self.version, self.effects, self.summary = (
            change_id, version, effects, summary)

    def state(self, kind: str) -> str:
        return next((e.state for e in self.effects if e.kind == kind), PENDING)

    def as_dict(self) -> dict:
        return {"change_id": self.change_id, "version": self.version, "summary": self.summary,
                "effects": [e.as_dict() for e in self.effects]}


def build_effect_skills(calendar, mail, *, gate=None, decisions=None, changes=None,
                        care_circle=None, timezone: str = "", bundles=None) -> list[Tool]:
    """The two external effects, as ordinary broker-gated skills.

    `gate` is the second, independent checkpoint on the way out: the reply was composed from
    allowlisted fragments upstream, and it is checked again here, immediately before the port,
    because the thing that leaves the machine is this call's arguments and nothing else.

    It is also where the AUDIENCE is checked, and against the provider rather than the plan: the
    recipient must be the sender of the message this change was raised from, read back from the
    mail port now. Checking it against the plan would only confirm that the plan agreed with itself.

    `changes` answers "which change is this?" by id — `ChangeEngine.get`. Without it the audience
    cannot be bound to an approved change at all, and `_permitted_recipients` permits nobody, which
    refuses every send rather than allowing one on an unverified identity. Fail closed is the right
    direction for the one call that puts words in the owner's mouth, but it is worth knowing which
    way it fails: a build with no resolver does not send, it does not send to the wrong person.

    `timezone` is the OWNER's configured zone, and it is a parameter rather than a lookup for the
    same reason `changes` is: both ports derive the day they reason about from trusted state, and
    the owner's zone is one of the two pieces of it — the other being the pinned event. A port that
    read the offset off the bundle would let the thing being checked choose the window it is checked
    in, which is the fifth review's second finding.

    `bundles` answers "which arguments did the owner APPROVE?" — the sixth review's second and third
    findings, which are the same finding at two ports. The approval authorised a set of values; the
    port ran on the arguments it was handed; nothing compared them, so `In-Reply-To`, `References`,
    the idempotency header, `event_id`, `start`, `end` and `send_updates` could all be written with
    values the owner never saw. Reading the approved bundle is what makes "the approval authorized
    THIS call" a check rather than a convention, and an unattached resolver refuses every write
    rather than permitting one on a comparison that did not happen."""

    def update_event(args: dict) -> ToolResult:
        # THE CALENDAR FIELDS GO THROUGH THE GATE, immediately before the port. `location` is the
        # field that matters: it is the venue the model chose, and the calendar is a disclosure
        # route — an invite the owner's colleagues can see is a place a home address can leave
        # from without ever being in an email. The plan-building path guards these too; this is the
        # check that cannot be skipped by a bundle that reached the port some other way, because it
        # is on the arguments this call is about to write and nothing else.
        if gate is not None:
            # The day the write lands on, read HERE, at the last moment before the port — the same
            # reasoning as the audience check below it: the question "is this venue another
            # meeting's place?" is about the calendar as it is now, not as the plan found it, and a
            # bundle that reached the port some other way is exactly what this moment exists for.
            # FAIL CLOSED. The fourth review wrote a malformed `start` into an approved bundle and
            # the version of this that set `window = ()` reported it as clean: the day's events
            # came back empty, the foreign set came back empty, `guard_calendar_foreign` returned on
            # `not foreign`, and `Room 2, Town Hall` — another meeting's place — was written to the
            # invite. The empty set was never evidence about that venue. It was the check failing to
            # ask, and a check that cannot ask must refuse rather than pass.
            #
            # THE DAY AND THE OFFSET COME FROM TRUSTED STATE, AND THE ARGUMENTS ARE NOT IT.
            #
            # This used to read `_day_window(slot[:10], slot)` — the day off the start being written
            # and the OFFSET off the same stamp, which is a value the bundle carries and can
            # therefore choose. That is the fifth review's second finding arriving at the OTHER
            # port: the reviewer demonstrated it on the reply, where a `-12:00` stamp moved the
            # queried window twelve hours and the day's other meetings fell outside it, so the
            # foreign set came back empty and both guards read the emptiness as innocence while a
            # foreign place went out. The same line was sitting here, guarding `location` — the one
            # field on this path a model used to author end to end — and a bundle that stamped the
            # write at `-12:00` emptied this window the same way.
            #
            # So the day is read off the PINNED EVENT as the provider serves it now, placed in the
            # OWNER's configured zone, and the offset comes from that zone rather than from the
            # stamp. Neither is a value a bundle can write.
            #
            # The write's own `start` still has to be an instant this build can place. That is a
            # separate question from the window — a calendar write whose start names no day is not
            # a write to make — and it is asked first so the refusal names the malformed field.
            # A refusal rather than a shrug: the protected-value half is not reached, which is the
            # right trade: nothing leaves on this path at all, and the owner gets a sentence naming
            # what could not be read rather than a write that happened because the question was
            # malformed.
            slot = str(args.get("start") or "")
            day, window = "", ()
            if _placeable(slot):
                try:
                    target = calendar.get_event(str(args.get("event_id") or ""))
                except Exception:                                    # noqa: BLE001
                    target = None
                day = _local_day(getattr(target, "start", "") if target is not None else "",
                                 timezone)
                if day:
                    try:
                        # Same `now` as the reply port, and for the same reason: it is the pinned
                        # event's own provider-issued stamp, so a build configured with an offset
                        # rather than a zone still asks about the OWNER's day rather than the
                        # host's. See `send_reply`.
                        window = _day_window(day, getattr(target, "start", ""), timezone=timezone)
                    except (ValueError, TypeError):
                        window = ()
            if not window:
                # No `decisions.record` here, and the reason is not that the refusal should be
                # quiet. This tool's arguments carry no `change_id` — `_plan` writes the calendar
                # bundle WITHOUT one (`changes.py`, `calendar_args`), unlike the reply's — so a note
                # filed from inside the skill could only land in `DecisionLog.orphans()`, which is
                # not where the owner reads. The refusal reaches them the way every other failed
                # effect does: the executor's `_transition` files `The invite: failed`, verdict
                # `refused`, against the change, with this exception's sentence as the detail. The
                # reply side DOES carry a change id, and `send_reply` files its own row there.
                raise CalendarDayUnknown(
                    "refused: this calendar write names a start that cannot be placed on a day, so "
                    "the day's other meetings cannot be checked against it. A write that cannot "
                    "answer the question is not a write that answered it."
                    if not _placeable(slot) else
                    "refused: the meeting this calendar write is about cannot be placed on a day, "
                    "so the day's other meetings cannot be checked against it. A write that cannot "
                    "answer the question is not a write that answered it.")
            # THE FIELD MUST BE A RENDER, NOT MERELY A SURVIVOR.
            #
            # The two checks below ask whether this `location` carries the owner's protected
            # commitment or another meeting's place. Both are questions about a string somebody
            # already produced, and the fifth review's third finding is what that leaves open: a
            # venue the model invented matches neither — it is not the owner's secret and not
            # another meeting's address — and it was written to the invite with
            # `send_updates="all"`, which notifies every guest of the meeting it names.
            #
            # The engine now builds the calendar field from a catalogue of pages it actually
            # fetched, and the model's whole contribution is which entry. So the port asks the
            # question that has an answer for every string rather than for the strings we thought
            # of: is this the render of the record this change chose? A bundle built by the engine
            # always says which record that is, and `_prepare` and `revise` are the only writers of
            # `venue_id` — a location that is not that record's render did not come from the
            # engine, whatever it happens to say.
            #
            # The check binds EVERY write that carries a place. A change that carries no selection —
            # a bundle assembled by hand, or a plan whose research found no venue — was the gap the
            # sixth review named, and the seventh closed it: `_venue_not_rendered` now REFUSES rather
            # than answering "", because "there is no record to compare against" is not "the value
            # was checked". The one state that still passes is a write carrying no location at all,
            # which is a sentence in that function rather than an omission here. See
            # `docs/THREAT_MODEL.md` item 28.
            change = None
            change_id = str(args.get("change_id", "") or "")
            if changes is not None and change_id:
                try:
                    change = changes.get(change_id)
                except Exception:                                    # noqa: BLE001
                    change = None
            # THE ARGUMENTS MUST BE THE APPROVED ONES. `event_id`, `start`, `end`, `location`,
            # `send_updates` and `if_match_etag` all reached the provider straight off this call's
            # arguments, and the approval that authorised the call was raised over a bundle's OWN
            # copy of them — two values nothing compared. The sixth review's third finding is what
            # that leaves open: `start`/`end` need not equal the approved slot, `event_id` need not
            # equal the meeting the change pinned, and `send_updates` decides who gets notified
            # without having been rendered or approved by anything.
            #
            # BOUND TO THE BUNDLE'S ARGUMENTS rather than re-derived from the change: the approval
            # is over THESE values, and "the engine could have computed the same ones" is not the
            # same claim as "the owner saw these ones". FAIL CLOSED — no resolver, no bundle, no
            # action, no field means the comparison did not happen, and a comparison that did not
            # happen is not one that passed. The same direction `_venue_not_rendered` takes one
            # check below, and the same one the day window takes above.
            bound = _approved_bundle(bundles, change_id, "calendar")
            if bound is None:
                raise CalendarRefused(
                    "refused: this calendar write cannot be matched against the plan the owner "
                    "approved, so there is nothing that says these are the values they said yes "
                    "to. A write whose approval cannot be read is not a write that was approved.")
            approved, bundle = bound
            for name in CALENDAR_BOUND_FIELDS:
                _bind_field(args, approved, name, port="calendar write", change_id=change_id)
            refusal = _venue_not_rendered(change, str(args.get("location") or ""))
            if refusal:
                raise CalendarRefused(refusal)
            gate.guard_calendar_fields(
                change_id=change_id,
                event_id=args["event_id"], if_match_etag=args["if_match_etag"],
                start=args["start"], end=args["end"], location=args["location"],
                send_updates=args.get("send_updates", "none"),
                timezone=str(args.get("timezone", "") or ""),
                # The event being written IS the pinned meeting, so the fragments that must not
                # appear in its fields are every OTHER event's — which is what `pinned` buys here.
                pinned=args["event_id"],
                foreign=foreign_fragments_of(calendar.list_events(*window),
                                             pinned=args["event_id"]))
            # THE LAST THING BEFORE THE PROVIDER. Everything above bound this call to a version of
            # the plan; a revision since then would leave those bounds describing a plan the owner
            # has already moved off, and the write would carry v1's meeting into a world the owner
            # has since changed. Asked here rather than earlier because here is the call.
            _still_at_the_bound_version(bundles, change_id, "calendar", bundle,
                                        port="calendar write")
        try:
            event = calendar.update_event(
                args["event_id"], args["if_match_etag"], start=args["start"], end=args["end"],
                location=args["location"], send_updates=args.get("send_updates", "none"),
                timezone=str(args.get("timezone", "") or ""))
        except EtagMismatch as exc:
            return ToolResult(ok=False, error=f"the invite changed: {exc}",
                              meta={"code": "etag_mismatch"})
        except ReauthRequired as exc:
            return ToolResult(ok=False, error=f"the calendar connection needs re-authorising: {exc}",
                              meta={"code": "reauth_required"})
        return ToolResult(ok=True, content=f"updated {event.id}",
                          meta={"event_id": event.id, "etag": event.etag})

    def send_reply(args: dict) -> ToolResult:
        # The audience, re-derived from the provider and bound to the approved change. An empty set
        # means the binding did not hold — wrong message, wrong thread, a foreign record, an
        # unusable address, or a sender who is not the person this change came from — and empty is
        # not permission to send: an audience nobody could establish is not an audience, and a reply
        # that goes out because the check failed open is the exact failure this product exists to
        # not have.
        change_id = args.get("change_id", "")
        change = None
        if changes is not None and change_id:
            try:
                change = changes.get(change_id)
            except Exception:                                        # noqa: BLE001
                change = None
        permitted, why, about = _permitted_recipients(
            mail, change, thread_id=args.get("thread_id", ""),
            message_id=args.get("in_reply_to", ""))
        if not permitted and decisions is not None and change_id:
            # Recorded here rather than left to the gate, because the gate can say "this address is
            # not permitted" and cannot say WHY the permitted set was empty. The reason is the part
            # the owner needs: it names which of the bindings failed.
            decisions.record(change_id, stage="send", source="system",
                             subject="The reply: who may receive it", verdict="refused",
                             rule=RULE_RECIPIENTS, detail=why)
        if gate is not None:
            # THE PORT ASKS THE WHOLE QUESTION, ON EVERY FIELD.
            #
            # The fifth review walked in through the door this used to leave open. The composer and
            # `guard_email` both enforce that an outbound reply is the render of its own declared
            # fragments — and `guard_email` also asks the foreign-event question of the SUBJECT and
            # the body together. This method did neither: it ran `check_text` on two of the fields
            # and then handed `args["body"]` and `args["subject"]` straight to the provider. The
            # reviewer's probe sent a body no fragment declared and a subject naming another
            # meeting, and both left.
            #
            # So the port no longer re-implements a piece of the check. It rebuilds the message the
            # bundle CLAIMS to be — the change's own decomposition, the change's own pin, the day's
            # other meetings read fresh from the provider — and hands it to `guard_email`, which is
            # the one function that knows what an outbound email has to satisfy. A caller that
            # built the arguments some other way is refused by the same rule rather than by a
            # second, thinner one written here.
            #
            # WHY `guard_email` AND NOT `check_text`: `check_text` answers "does this disclose
            # something the owner keeps private". It cannot answer "is this text the render of the
            # pieces the engine declared" or "does this subject describe another meeting", and
            # those are different failures with different names. Asking only the first and calling
            # it the check was the finding.
            pinned = str(getattr(change, "event_pinned", "") or "")
            # THE DAY COMES FROM THE PINNED EVENT AND THE OWNER'S ZONE, NEVER FROM THE CALLER.
            #
            # This used to take the day off the change record and the OFFSET off the proposal's own
            # timestamp — a value a bundle carries and can therefore choose. The fifth review's
            # second finding is what that costs: a `-12:00` stamp moved the queried window twelve
            # hours, the day's other meetings fell outside it, the foreign set came back empty, and
            # both guards read that emptiness as innocence while `Room 2, Town Hall` and a reply
            # naming another meeting's eleven-fifteen went out. A check whose window is chosen by
            # the thing being checked is not a check.
            #
            # So the day is read off the event the change PINNED — the meeting this reply is about,
            # fetched from the provider as it is now — and the offset comes from the owner's
            # configured timezone rather than from the host's or the stamp's. Two pieces of trusted
            # state, neither of which a bundle can write.
            #
            # There is no `care_day` fallback any more. It was the same hole from the other side: a
            # caller-supplied day is a caller-chosen window, and the field existed only so a bundle
            # could name the day the port was meant to reason about.
            window, day = (), ""
            if pinned:
                try:
                    target = calendar.get_event(pinned)
                except Exception:                                     # noqa: BLE001
                    target = None
                # The event's own start, placed in the OWNER's zone: the day a reply lands on is
                # the owner's day, and a process running in UTC for an owner in London must still
                # ask about the owner's Tuesday.
                day = _local_day(target.start if target is not None else "", timezone)
                if day:
                    try:
                        # `now` is the PINNED EVENT's own stamp, and it is here for the build whose
                        # owner's zone is configured as an offset rather than an IANA name — the
                        # sample product's own configuration. `_owner_zone` has nothing to attach
                        # there, and the fallback is then this instant's offset, which is the offset
                        # the PROVIDER issued the meeting at. The host's clock is not consulted at
                        # all: on a machine that is not in the owner's zone it is simply wrong, and
                        # "the host happened to agree" is not a guard.
                        window = _day_window(day, target.start, timezone=timezone)
                    except (ValueError, TypeError):
                        # A day string nothing can place. Same rule as the offset: a check that
                        # asks about the wrong day is worse than one that says it cannot ask.
                        window = ()
            if not pinned or not window:
                if decisions is not None and change_id:
                    decisions.record(change_id, stage="send", source="system",
                                     subject="The reply: which meeting it is about",
                                     verdict="refused", rule=RULE_RECIPIENTS,
                                     detail=("the reply does not name the meeting it is about and "
                                             "the day it lands on, so the day's other meetings "
                                             "cannot be checked against it"))
                raise CalendarDayUnknown(
                    "refused: this reply does not name the meeting it is about and the day it "
                    "lands on, so the day's other meetings cannot be checked against its text. A "
                    "reply that cannot answer the question is not a reply that answered it.")
            # The day's events are re-READ rather than taken from the change record, so a meeting
            # that appeared between composition and the port is covered too — and the record is
            # deliberately not asked, because the record holding those meetings' text would be the
            # leak it is checking for, on a screen the owner reads.
            #
            # ...and the message this change is about is exempt from that set, because it is the
            # one the recipient wrote. See `foreign_fragments_of`: by the time a reply reaches this
            # moment the calendar move has already happened, so the pinned meeting no longer
            # carries the place its own thread is named after, and `Re: Our usual cafe has closed`
            # would otherwise match a second meeting that never moved.
            foreign = foreign_fragments_of(
                calendar.list_events(*window), pinned=pinned,
                exempt_text=(f"{getattr(about, 'subject', '') or ''}\n"
                             f"{getattr(about, 'body_text', '') or ''}") if about else "")
            # The message the bundle CLAIMS to be: its own stored decomposition, rendered. The
            # fragments come off the change rather than off the arguments, which is what makes the
            # equality below a check rather than a tautology — `body == render(body)` would compare
            # the arguments against themselves.
            outbound = _outbound_from_bundle(change, args, pinned=pinned, foreign=foreign)
            gate.guard_email(outbound, change_id=change_id, permitted=permitted, stage="send")
            # Clause 7 at the port, and the kind is DERIVED from the address rather than believed
            # from the bundle. A reply to somebody on the care circle is a care action whether or
            # not the bundle said so, which is what makes the clause unskippable by omission: the
            # finding was a reply to a member carrying no care metadata, judged by the scheduling
            # clauses alone and cleared to auto-run. `care_kind` is read only to answer the SECOND
            # question — which grant the owner gave them — never the first.
            if care_circle is not None:
                members = [a for a in (args.get("to") or ()) if care_circle.knows(a)]
                if members:
                    gate.guard_care_recipients(
                        members, kind=str(args.get("care_kind") or "") or "reply",
                        change_id=change_id, stage="send")
        # THE ENVELOPE IS BOUND TO THE APPROVED PLAN, and this runs whether or not a gate is
        # attached — it is a provenance check, not a privacy one, and the sixth review's second
        # finding is that nothing was asking it. `In-Reply-To`, `References` and the idempotency
        # header are all built from these arguments, and every one of them reached the provider with
        # a caller-supplied value: a reply could be filed into a thread nobody approved, and the key
        # a retry reconciles on was the caller's choice rather than this change's version.
        bound_mail = _approved_bundle(bundles, change_id, "email")
        if bound_mail is None:
            raise ApprovedArgumentsRefused(
                "refused: this reply cannot be matched against the plan the owner approved, so "
                "there is nothing that says these are the arguments they said yes to. A message "
                "whose approval cannot be read is not a message that was approved.")
        approved_mail, mail_bundle = bound_mail
        for name in EMAIL_BOUND_FIELDS:
            _bind_field(args, approved_mail, name, port="reply", change_id=change_id)
        # `In-Reply-To` and `References` are not read off the caller at any point. They are read off
        # the PROVIDER'S OWN COPY of the message this change is about — the object
        # `_permitted_recipients` just checked the id, the thread and the sender of — because a
        # header that decides which conversation the reply lands in must describe the record that
        # exists, and the approved value is required to be that same record rather than trusted to
        # be. A provider that serves no `Message-ID` for it is refused rather than sent without one:
        # `about is None` means the pin did not hold.
        if about is None:
            raise ApprovedArgumentsRefused(
                "refused: the message this reply answers could not be read back from the provider, "
                "so the headers that file the reply into its thread have nothing to describe. A "
                "reply that cannot say which conversation it belongs to is not sent.")
        approved_id = str(approved_mail.get("rfc822_message_id") or "")
        provider_id = str(getattr(about, "rfc822_message_id", "") or "")
        if approved_id != provider_id:
            raise ApprovedArgumentsRefused(
                "refused: the message id this reply threads on is not the one the owner approved "
                "against the message the provider is serving. A reply is filed by headers, and "
                "headers built from a value nobody approved file it somewhere nobody chose.")
        original = MailMessage(id=args["in_reply_to"], thread_id=args.get("thread_id", ""),
                               sender="", to=tuple(args.get("to") or ()),
                               subject=args["subject"], body_text="", received_at="",
                               rfc822_message_id=provider_id,
                               references=tuple(getattr(about, "references", ()) or ()))
        # THE LAST THING BEFORE THE PROVIDER, for the same reason as the calendar's: a revision
        # landing between the binding and the send leaves this reply carrying v1's words under an
        # approval the owner has since replaced. The reply is the effect nobody can recall.
        _still_at_the_bound_version(bundles, change_id, "email", mail_bundle, port="reply")
        sent = mail.send_reply(original, tuple(args["to"]), args["subject"], args["body"],
                               args["idempotency_key"])
        return ToolResult(ok=True, content=f"sent {sent.id}",
                          meta={"message_id": sent.rfc822_message_id, "sent_id": sent.id})

    return [
        Tool(name=CALENDAR_SKILL,
             description="Update one owner-organized calendar event, conditional on its ETag.",
             parameters={"type": "object", "required": ["event_id", "if_match_etag", "start",
                                                        "end", "location"],
                         "properties": {"event_id": {"type": "string"},
                                        "if_match_etag": {"type": "string"},
                                        "start": {"type": "string"}, "end": {"type": "string"},
                                        "location": {"type": "string"},
                                        "send_updates": {"type": "string"},
                                        "change_id": {"type": "string"}}},
             risk=Risk.HIGH, fn=guarded(CALENDAR_SKILL, update_event), gate_name="SendMessage",
             scope="calendar.write", approval_required=True),
        Tool(name=EMAIL_SKILL,
             description="Send exactly the reviewed reply on the owner's behalf.",
             parameters={"type": "object", "required": ["to", "subject", "body", "in_reply_to",
                                                        "idempotency_key"],
                         "properties": {"to": {"type": "array"}, "subject": {"type": "string"},
                                        "body": {"type": "string"},
                                        "in_reply_to": {"type": "string"},
                                        "change_id": {"type": "string"},
                                        "care_kind": {"type": "string"},
                                        "care_recipients": {"type": "array"},
                                        "care_day": {"type": "string"},
                                        "idempotency_key": {"type": "string"}}},
             risk=Risk.HIGH, fn=guarded(EMAIL_SKILL, send_reply), gate_name="SendMessage",
             scope="message.send", approval_required=True),
    ]


class Executor:
    def __init__(self, *, broker, changesets: ChangeSetStore, calendar, mail,
                 boundary=None, audit: AuditLog | None = None, decisions=None,
                 limits=None, changes=None, agreements=None, gate=None, care_circle=None,
                 digests=None, timezone: str = "") -> None:
        #: The owner's configured zone, and NOT a default. Both ports derive the day they reason
        #: about from trusted state and this is half of it — the other half is an event read back
        #: from the provider. Empty means the build was not told, and the ports then fall back to
        #: the offset the provider's own stamp carries, which for an instant the calendar issued is
        #: the owner's offset. What they never do is take it from the bundle.
        self.timezone = str(timezone or "")
        self.broker = broker
        self.changesets = changesets
        self.calendar = calendar
        self.mail = mail
        self.boundary = boundary
        self.audit = audit
        self.decisions = decisions
        #: The egress gate, the care circle and the digest ledger — the three things the digest
        #: path needs and the change path already had. `gate` is the SAME gate the engine holds,
        #: not a second one: two gates would be two `ProtectedFactStore`s to keep in step, and the
        #: one that fell behind would be the one that let something out.
        self.gate = gate
        self.care_circle = care_circle
        self.digests = digests
        #: This executor's own key, minted per instance and never exported. `build_digest` marks
        #: the plan it composed with it and `send_digest` verifies the mark, which is what makes
        #: "the engine composed this" a fact the PORT can check rather than one it takes on trust:
        #: `compose_digest` is public, so its own seal answers a weaker question. Per instance and
        #: not per process so that a second executor — a test's, a second tenant's — cannot send
        #: the first one's plans, and a caller holding the `care` module still cannot mint one.
        self._digest_key = secrets.token_bytes(32)
        #: Where what the owner agreed to is written down, so that a later run can ask whether the
        #: world still matches it. This is the ONLY place an agreement is recorded, and it is the
        #: one place both execution paths meet: an owner approval and a run inside the standing
        #: limits produce the same agreement, because both go through `execute`.
        self.agreements = agreements
        #: The standing limits, and the engine that issued the grant being re-checked. Both are
        #: only consulted for a bundle carrying a grant; an owner approval never reaches them.
        #: Absent on a build that has neither, and a bundle WITH a grant refuses there rather than
        #: proceeding unchecked — see `ChangeEngine.limit_grant_intact`.
        self.limits = limits
        self.changes = changes

    # ---- the one entry point ------------------------------------------------------------------
    def _note(self, change_id: str, *, stage: str, source: str, subject: str, verdict: str,
              rule: str, detail: str | None = None) -> None:
        if self.decisions is not None and change_id:
            self.decisions.record(change_id, stage=stage, source=source, subject=subject,
                                  verdict=verdict, rule=rule, detail=detail)

    def execute(self, cs_id: str, *, version: int | None = None,
                now: str | None = None) -> Outcome:
        cs = self.changesets.latest(cs_id)
        if cs is None:
            raise KeyError(f"no change set '{cs_id}'")
        if version is not None and int(version) != cs.version:
            raise StaleChangeSet(f"version {version} is not the current bundle "
                                 f"(version {cs.version} is)")
        if cs.status != APPROVED_STATUS:
            raise NotApproved(
                f"bundle {cs.id} v{cs.version} is {cs.status}: only an approved bundle may touch "
                f"a provider")
        self._admit(cs)                       # fresh reads; raises StaleChangeSet, runs nothing

        effects = [Effect(CALENDAR_UPDATE_KIND, PENDING, "waiting"),
                   Effect(EMAIL_REPLY_KIND, PENDING, "waiting")]
        self.changesets.mark(cs.id, cs.version, EXECUTING_STATUS)

        calendar_effect = effects[0]
        # Taken BEFORE the provider is touched, which is the whole of its value: a run that pauses
        # after its write and resumes after a newer run has recorded cannot put its obsolete window
        # back, because the newer run began later and carries the larger number. A store that
        # cannot hand one out stops the run here rather than leaving it unnumbered — see
        # `_begin_sequence`, and `_refuse_unsequenced` for what the owner is told.
        try:
            sequence = self._begin_sequence()
        except Exception as exc:                                      # noqa: BLE001
            return self._refuse_unsequenced(cs, effects, exc)
        self._transition(cs, calendar_effect, RUNNING, "updating the invite")
        inv = self._decide(cs, cs.calendar)
        if not inv.ran:
            self._transition(cs, calendar_effect, FAILED, inv.error or "the update did not run")
        elif not inv.ok:
            state = UNKNOWN if _ambiguous(inv.error) else FAILED
            self._transition(cs, calendar_effect, state, inv.error or "the provider refused")
        else:
            self._confirm_readback(cs, calendar_effect, sequence)

        email_effect = effects[1]
        if calendar_effect.state != DONE:
            # The reply asserts the invite moved. Sending it now would make the email a claim
            # the calendar does not support.
            self._transition(cs, email_effect, PENDING,
                             "not attempted: the invite update did not complete")
        elif (veto := self._limit_veto(cs)) is not None:
            # The grant stopped holding between the invite and the reply. The invite has already
            # moved, so this is not "nothing happened" and must not be reported as such: the reply
            # is refused, recorded, and the run settles as the partial thing it is.
            self._transition(cs, email_effect, FAILED, f"not sent: {veto}")
        else:
            self._send(cs, email_effect)

        return self._settle(cs, effects)

    def _settle(self, cs: ChangeSet, effects: list[Effect]) -> Outcome:
        """Report the run as it actually ended, and persist that report.

        Shared with `_refuse_unsequenced` rather than spelled twice: a run that never reached a
        provider still has to settle its bundle and hand its caller an outcome, and two copies of
        "what an outcome is" is how the refused path ends up quietly disagreeing with the run path
        about the bundle's status.
        """
        calendar_effect, email_effect = effects[0], effects[1]
        summary = _summary(calendar_effect, email_effect)
        outcome = Outcome(cs.change_id, cs.version, effects, summary)
        self.changesets.mark(cs.id, cs.version, _final_status(calendar_effect, email_effect),
                             outcome=outcome.as_dict())
        return outcome

    def _refuse_unsequenced(self, cs: ChangeSet, effects: list[Effect], exc: Exception) -> Outcome:
        """The run could not be given its place in the order of executions, so it does not run.

        This is the fail-closed half of `_begin_sequence`, and the trade is deliberate: the owner
        loses one run, and in exchange nothing has happened to the world. The alternative — running
        unnumbered — is what the review found, and it is worse in the direction that cannot be
        undone, because the run would record its agreement unconditionally and overwrite a newer
        execution's record of a meeting only that execution saw the final state of.

        So the invite is not touched at all rather than attempted-and-reported: there is no effect to
        describe, no partial run, and no exception out of `execute` — a bundle left mid-flight over a
        failure that happened before anything ran would be the invention this path exists to avoid.
        The reason reaches the owner in both places they read: the decision log, and the run's own
        report of the two effects it did not attempt.
        """
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("no execution sequence could be taken for %s: %s", cs.change_id, detail)
        self._note(cs.change_id, stage="execute", source="system",
                   subject="this run could not be given its place in the order of executions",
                   verdict="refused",
                   rule="an execution is numbered before it touches a provider, so that a run that "
                        "began earlier cannot overwrite a newer run's agreement",
                   detail=f"nothing was attempted: {detail}")
        self._transition(cs, effects[0], FAILED,
                         f"not attempted: this run could not be numbered ({detail})")
        self._transition(cs, effects[1], PENDING,
                         "not attempted: the invite update did not run")
        return self._settle(cs, effects)

    # ---- admission ----------------------------------------------------------------------------
    def _admit(self, cs: ChangeSet) -> None:
        """Re-read the world immediately before the first write. Nothing is trusted from review
        time except the version numbers that must MATCH it."""
        try:
            event = self.calendar.get_event(cs.calendar.arguments["event_id"])
        except KeyError:
            raise StaleChangeSet(f"the event {cs.calendar.arguments['event_id']} no longer exists")
        if event.etag != cs.event_etag:
            self._note(cs.change_id, stage="approve", source="owner",
                       subject="Re-checked the invite before writing", verdict="refused",
                       rule=RULE_APPROVAL,
                       detail="the invite moved after you approved it; nothing was sent")
            raise StaleChangeSet(
                f"the invite changed since you approved it (etag {cs.event_etag} -> "
                f"{event.etag}); nothing was sent")
        if self.boundary is not None:
            current = self.boundary.version_of(cs.boundary_id)
            if current != cs.boundary_version:
                self._note(cs.change_id, stage="approve", source="owner",
                           subject="Re-checked your protected commitment", verdict="refused",
                           rule=RULE_APPROVAL,
                           detail="the commitment changed after you approved this; nothing was sent")
                raise StaleChangeSet(
                    f"your protected commitment changed since you approved this (version "
                    f"{cs.boundary_version} -> {current}); nothing was sent")
        self._note(cs.change_id, stage="approve", source="owner",
                   subject="Re-checked the invite and your commitment", verdict="allowed",
                   rule=RULE_APPROVAL,
                   detail="both match what you approved; the change may run")
        refusal = self._limit_veto(cs)
        if refusal:
            # Raised, not returned: nothing has run, the bundle is still `approved`, and the
            # caller's fallback is to hand it back to the owner. That is the honest outcome for a
            # standing yes that stopped being true before the first effect.
            self._note(cs.change_id, stage="approve", source="owner",
                       subject="Re-checked the standing limits", verdict="refused",
                       rule=RULE_APPROVAL, detail=refusal)
            raise StaleChangeSet(refusal)

    def _limit_veto(self, cs: ChangeSet) -> str | None:
        """Whether the standing-limit grant on this bundle still holds, immediately before an
        effect. `None` means yes, or that this bundle carries no grant at all.

        Called twice: once in `_admit`, where a refusal stops both effects, and once before the
        reply, where the invite has already moved and the honest report is a partial run rather
        than a rollback that cannot happen. The mode, its revision, the bundle version, the
        change's version and the digest of the reviewed state are all re-read here — the review's
        two Highs are that none of them were.
        """
        if self.changes is None:
            # No engine to ask. A grant with nothing able to re-check it is refused by
            # `limit_grant_intact` when `limits` is present, so this branch is a build that has
            # neither — and it must still not run a grant-bearing bundle unchecked.
            return ("this run was authorized by the standing limits, but this build has nothing "
                    "left to re-check them with") if getattr(cs, "limit_grant", None) else None
        return self.changes.limit_grant_intact(cs.change_id, cs, self.limits)

    # ---- effects ------------------------------------------------------------------------------
    def _decide(self, cs: ChangeSet, action):
        """Spend the action's approval and run it, through the broker like any other skill."""
        try:
            return self.broker.decide(action.approval_id, approve=True)
        except ApprovalError as exc:
            from .broker import Invocation
            return Invocation(task_id=cs.task_id, skill=action.kind, arguments=action.arguments,
                              status="denied", decision="needs_approval", reason=str(exc),
                              ok=None, error=f"the approval could not be spent: {exc}")

    def _begin_sequence(self) -> int | None:
        """This execution's place in the order executions started, or None on a build that has no
        agreements store at all.

        FAIL CLOSED. A store that cannot hand out a number does NOT leave the run unnumbered: an
        unnumbered run records with `newer_than=None`, which makes the write unconditional, so the
        run would not merely lose the ordering — it would remove the protection and let a run that
        paused and resumed overwrite the newer execution's agreement. That is the review's High, so
        the store's own failure is raised to `execute`, which refuses the run before the provider is
        touched. `None` is left for the build with no store, where nothing is ever recorded and
        there is no order to keep.
        """
        if self.agreements is None:
            return None
        return self.agreements.begin_execution()

    def _confirm_readback(self, cs: ChangeSet, effect: Effect, sequence: int | None = None) -> None:
        args = cs.calendar.arguments
        try:
            event = self.calendar.get_event(args["event_id"])
        except Exception as exc:                                    # noqa: BLE001
            self._transition(cs, effect, UNKNOWN,
                             f"the update was accepted but reading the event back failed: {exc}")
            return
        # Instants, not strings — the same rule the drift comparator uses, and for the same reason.
        # Google may serve `14:00:00Z` for the `15:00:00+01:00` it accepted; comparing the raw
        # strings calls that a failed write, which would turn a move that happened exactly as
        # approved into `unknown` and record no agreement at all. The place stays exact: two
        # spellings of a venue are two strings, and nothing here invents "the same place".
        if not (same_instant(event.start, args["start"])
                and same_instant(event.end, args["end"])
                and event.location == args["location"]):
            self._transition(
                cs, effect, UNKNOWN,
                "the provider accepted the update but the read back did not show it "
                "(the invite may be in either state)")
            return
        self._transition(cs, effect, DONE,
                         f"the invite now reads {event.start}–{event.end} at {event.location}")
        self._record_agreement(cs, event, sequence)

    def _record_agreement(self, cs: ChangeSet, event, sequence: int | None = None) -> None:
        """Write down what the owner has now agreed to — what they APPROVED, not what was read.

        Here and nowhere else, because this is the moment it is true: the invite moved, the
        provider accepted it, and the read-back confirmed the window and the place are the ones
        that were written. Every other path — a failed update, a refused one, an accepted update
        that never showed up in the read-back — leaves nothing behind, because an agreement
        recorded over an invite that did not move is a promise this product never got kept.

        `event` is the read-back, and it is handed over as the OBSERVED state rather than as the
        agreement: the record has to be the bundle the owner said yes to, because the two calls are
        not one call and anybody with write access to the invite can edit it in between. `record`
        compares the approved values against this read and lands the row `drifted` when they
        disagree, so a guest added in that window is reported instead of being signed off as agreed.
        """
        if self.agreements is None:
            return
        try:
            approved = approved_view(cs.calendar)
        except Exception as exc:                                      # noqa: BLE001
            self._agreement_failed(cs, None, exc, sequence)
            return
        try:
            self.agreements.record(change_id=cs.change_id, approved=approved, event=event,
                                   sequence=sequence)
        except Exception as exc:                                      # noqa: BLE001
            self._agreement_failed(cs, approved, exc, sequence)

    def _agreement_failed(self, cs: ChangeSet, approved: dict | None, exc: Exception,
                          sequence: int | None) -> None:
        """The invite moved and the agreement could not be written: say so, and finish the run.

        By the time this is reached the calendar effect is `DONE` — a real, externally visible
        change has happened. Raising here would abort the run before the reply is sent and before
        an outcome is persisted, leaving a bundle stuck in `executing` over an effect that really
        happened: the run would be lying about the world in the more expensive direction. So the
        failure is recorded where the owner reads failures (the decision log), and the row is
        marked `unchecked` with the error, which is the answer that claims the least about a
        meeting nothing has managed to compare.

        Best effort all the way down, and `mark_unchecked` is called under its own guard: the store
        that just failed is the store this would fail on, and a second failure must not become the
        way this path finally raises.
        """
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("the agreement for %s could not be recorded after the invite moved: %s",
                    cs.change_id, detail)
        self._note(cs.change_id, stage="agreement", source="system",
                   subject="what the owner agreed to could not be written down",
                   verdict="unchecked", rule="agreements are recorded after the effect completes",
                   detail=detail)
        if approved is None or self.agreements is None:
            return
        try:
            self.agreements.mark_unchecked(change_id=cs.change_id, approved=approved, error=detail,
                                           sequence=sequence)
        except Exception as exc2:                                     # noqa: BLE001
            log.warning("the agreement for %s could not even be marked unchecked: %s",
                        cs.change_id, exc2)

    def _send(self, cs: ChangeSet, effect: Effect) -> None:
        effect.idempotency_key = cs.email.arguments.get("idempotency_key", "")
        self._transition(cs, effect, RUNNING, "sending the reviewed reply")
        inv = self._decide(cs, cs.email)
        if inv.ran and inv.ok:
            effect.provider_url = str(inv.meta.get("message_id", ""))
            self._transition(cs, effect, DONE, f"sent ({inv.meta.get('sent_id', '')})")
            return
        error = inv.error or "the send did not run"
        if not _ambiguous(error):
            self._transition(cs, effect, FAILED, error)
            return
        found = self._reconcile(effect.idempotency_key)
        if found:
            self._transition(cs, effect, DONE,
                             f"reconciled: the provider already has this reply ({found}), so it "
                             f"was not sent again")
        else:
            self._transition(cs, effect, FAILED,
                             f"reconciled after an ambiguous failure: no such send exists at the "
                             f"provider, and it was not resent ({error})")

    def _reconcile(self, idempotency_key: str):
        if not idempotency_key:
            return None
        try:
            return self.mail.find_sent(idempotency_key)
        except Exception:                                            # noqa: BLE001
            return None

    # ---- the digest ---------------------------------------------------------------------------
    def build_digest(self, day: str, *, now: str = "") -> "object":
        """The day's digest, assembled from RECORDS — the calendar and the change log — and never
        from a caller's sentences.

        Every line is a `care.DigestLine`, which means every line names the record it was rendered
        from. That is the finding 8 repair seen from the producing end: with a bare string accepted,
        an allowlisted source was a label anybody could put on any text, so a compromised model
        could write the disclosure itself and sign it `care_happened`. A model cannot produce a line
        here at all, because it has no record to name.

        The audience is read from the circle, filtered to the members the owner listed for
        `digest`. There is no parameter for it: a digest addressed by its CALLER is a digest whose
        audience is whatever the caller said, and the whole feature is that the audience is the
        owner's list.

        Fails CLOSED on a missing circle, an empty audience, or a missing day. Each of those means
        "there is nobody this may be sent to" or "the once-a-day limit cannot be checked", and both
        are refusals rather than defaults.
        """
        from . import care as care_rules
        from .boundary import CareAudienceRefused

        day = str(day or "").strip()
        if not day:
            raise care_rules.DigestDayMissing(
                "refused: a digest has to say which day it is for, so the once-a-day limit can be "
                "checked")
        if self.care_circle is None:
            raise CareAudienceRefused(
                "refused: this build has no care circle, so there is nobody a digest may be sent "
                "to.")
        recipients = [m.address for m in self.care_circle.members()
                      if self.care_circle.may_receive(m.address, "digest")]
        if not recipients:
            # A circle with nobody listed for the digest is the same state as no circle: there is
            # no audience. An empty recipient list sent onward would be a digest to nobody that
            # still spent the owner's one-a-day, so it refuses here.
            raise CareAudienceRefused(
                "refused: nobody on your care circle is listed to receive the digest, so there is "
                "nobody to send it to.")

        # The window is built at the OWNER's offset: the digest reads a real clock, and the clock
        # the executor was built with is the owner's — the host's zone is only the last resort for
        # a build that was told neither.
        start, end = _day_window(day, now, timezone=self.timezone)
        day_events = list(self.calendar.list_events(start, end))
        open_changes = self._open_changes(day)
        settled = self._settled_today(day)
        # THE RENDER OF EACH RECORD, built ONCE and used twice: to write the line and to prove the
        # line. The sixth review's fourth finding is that the record ids were a membership check
        # rather than provenance — `DigestLine(record_id="known-event", text="<anything>")` passed,
        # because a known id is a citation and not evidence. `compose_digest` now compares each
        # line's words with this mapping, and a mapping built from a SECOND renderer would be a
        # check of a value the line was not written from. One function per source, called here, and
        # the lines below are the mapping read back rather than a parallel spelling of it.
        records = _digest_records(day_events, open_changes, settled)
        coming = [care_rules.DigestLine("care_coming", e.id, records[str(e.id)])
                  for e in day_events if str(getattr(e, "id", "") or "") in records]
        needs = [care_rules.DigestLine("care_needs_you", c.id, records[str(c.id)])
                 for c in open_changes if str(getattr(c, "id", "") or "") in records]
        happened = [care_rules.DigestLine(
            "care_happened", str(r.get("change_id") or r.get("task_id") or ""),
            records[str(r.get("change_id") or r.get("task_id") or "")])
            for r in settled if str(r.get("change_id") or r.get("task_id") or "") in records]
        # MARKED FOR THIS PORT. `compose_digest` seals what it built, and every argument above
        # came off this executor's own reads — the circle, the calendar, the change store. The port
        # seal is what carries that fact to `send_digest`: without it the port could only establish
        # that SOMEBODY called the composer, and `compose_digest` accepts a caller's record set.
        return care_rules.seal_for_port(
            care_rules.compose_digest(day=day, recipients=recipients, happened=happened,
                                      coming=coming, needs_owner=needs, records=records),
            key=self._digest_key)

    def _open_changes(self, day: str) -> list:
        """The changes still waiting on the owner, as the records have them.

        `stage` is the filter and `received_at` is the day, and both come off the stored Change
        rather than off anything the caller said. A change with no readable arrival day is included
        for every day: erring toward showing the owner something twice is the direction that does
        not lose a decision, and the alternative — dropping a record whose timestamp did not parse
        — hides exactly the change most likely to be broken.
        """
        settled = {"sent", "refused", "stopped", "done", "expired"}
        out = []
        for change in (self.changes.changes() if self.changes is not None else []):
            if getattr(change, "stage", "") in settled:
                continue
            arrived = str(getattr(change, "received_at", "") or "")[:10]
            if arrived and arrived != day:
                continue
            out.append(change)
        return out

    def _settled_today(self, day: str) -> list[dict]:
        """What actually ran, read from the effect log rather than from an intention.

        The digest says what HAPPENED, and the only record of that is the row an execution wrote
        when it finished. A plan that was approved and then failed is not something to tell the
        family about; a plan that is still waiting is in `_open_changes` instead.
        """
        if self.audit is None:
            return []
        out = []
        for row in self.audit.rows(event="effect.state", limit=200):
            detail = row.get("detail")
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail)
                except ValueError:
                    detail = {}
            detail = detail if isinstance(detail, dict) else {}
            if str(detail.get("state", "")) != "done":
                continue
            if str(row.get("at", ""))[:10] != day:
                continue
            out.append({"change_id": detail.get("change_id", ""), "task_id": row.get("task_id", ""),
                        "subject": detail.get("kind", ""), "state": detail.get("state", "")})
        return out

    def send_digest(self, plan, *, change_id: str = "") -> dict:
        """Send the day's digest, and record it in the ledger ONLY once it has actually gone.

        This is the production path the review found missing: `digest_email` existed and nothing
        called it, so the once-a-day cap in clause 7 was a clause about a table nothing ever wrote
        to. The order here is what makes the cap real, and every step of it is deliberate:

        1. `claim` takes the day ATOMICALLY, before anything is composed or sent. Two runs that
           both believe the day is free produce one row and one `DigestAlreadySent` — a count read
           a moment earlier would let both proceed and report one digest when two went out.
        2. The plan is composed into an email by `digest_email`, which reads the circle ONCE into a
           snapshot and checks the audience against it. A digest is never composed from a caller's
           list of names.
        3. The circle is read AGAIN, fresh, immediately before the port. The snapshot proves the
           message was composed for a circle that existed; this second read is what catches a
           member REMOVED between composition and send, which a frozen snapshot cannot see. Both
           halves are needed and neither is sufficient.
        4. Only a send that came back is `confirm`ed. A failure `release`s the claim, so the day is
           given back and the owner has not spent their one digest on an error — which is the
           difference between "the ledger records what was sent" and "the ledger records what was
           attempted".

        Fails CLOSED on a missing ledger or a missing circle. Both mean the same sentence — the
        once-a-day limit cannot be checked — and "cannot be checked" is a refusal.
        """
        from . import care as care_rules
        from .boundary import CareAudienceRefused

        if self.digests is None:
            raise DigestLedgerMissing(
                "refused: this build has no digest ledger, so the once-a-day limit cannot be "
                "checked and a digest cannot be sent")
        if self.care_circle is None:
            raise CareAudienceRefused(
                "refused: this build has no care circle, so there is nobody a digest may be sent "
                "to.")
        # PROVENANCE FIRST, before the ledger and before anything is rendered: the port has to be
        # able to prove the plan it is sending is one THIS engine built. The eighth review's first
        # finding is what its absence cost — a caller-constructed `DigestPlan` went through this
        # method and put `OWNER NEVER APPROVED THIS` in a message to the owner's care circle, with
        # `compose_digest`'s provenance protecting only the normal path. Nothing about the plan's
        # content could have refused it: the pieces were well-formed, the sources allowlisted, the
        # text the render of the record it named. The question is about ORIGIN, so it is asked of
        # the seal rather than of the words, and it is asked here because this is the door.
        if not care_rules.plan_is_port_sealed(plan, key=self._digest_key):
            raise care_rules.DigestPlanForged(
                "refused: this digest plan was not built by this engine from its own records, so "
                "there is nothing that says a record produced its sentences or that the owner's "
                "circle was read for it. A digest is composed from the calendar and the change "
                "records; a plan handed in already built is not one.")
        if not str(getattr(plan, "day", "") or "").strip():
            raise care_rules.DigestDayMissing(
                "refused: this digest does not say which day it is for, so the once-a-day limit "
                "cannot be checked")
        recipients = tuple(str(r) for r in getattr(plan, "recipients", ()))
        artifact = care_rules.artifact_id(plan)
        entry_id = self.digests.claim(day=plan.day, recipients=recipients, artifact=artifact)
        try:
            email = care_rules.digest_email(plan, gate=self.gate,
                                            change_id=change_id, care_circle=self.care_circle)
            if self.gate is not None:
                self.gate.guard_email(email, change_id=change_id, permitted=recipients,
                                      stage="send", artifact=artifact, care_kind="digest")
                # The FRESH read. `digest_email` checked the circle it composed for; a member
                # revoked in between is only visible here, against the circle as it is NOW.
                self.gate.guard_care_recipients(
                    email.to, kind="digest", change_id=change_id, stage="send",
                    snapshot=self.care_circle.snapshot())
            # A digest is not a reply to anything. The parent is empty on purpose: no In-Reply-To
            # and no References, so the message is not filed into a thread it has nothing to do
            # with — and it goes through `mail.send_reply` anyway, because that is the ONE send
            # method the port has, and a second one would be a second header set to keep in step.
            parent = MailMessage(id="", thread_id="", sender="", to=(), subject="", body_text="",
                                 received_at="", rfc822_message_id="")
            sent = self.mail.send_reply(parent, tuple(email.to), email.subject, email.body,
                                        f"pm-digest-{plan.day}-{artifact}")
        except BaseException:
            # The day is given BACK. An error is not a digest, and a claim left behind would burn
            # the owner's one digest for a day on which nothing was sent.
            self.digests.release(entry_id)
            raise
        self.digests.confirm(entry_id)
        self._note(change_id, stage="send", source="system", subject="The family digest",
                   verdict="allowed", rule=RULE_DIGEST,
                   detail=f"{len(email.to)} care-circle member(s), day {plan.day}")
        return {"entry_id": entry_id, "day": plan.day, "artifact": artifact,
                "recipients": list(email.to), "sent_id": sent.id,
                "message_id": sent.rfc822_message_id}

    def _transition(self, cs: ChangeSet, effect: Effect, state: str, detail: str) -> None:
        effect.state, effect.detail = state, detail
        # Every effect state is a decision the owner can read: the calendar and the reply are the
        # only two things this product does to the outside world, and "what happened to them" is
        # the answer to the question the review screen was asking.
        self._note(cs.change_id, stage="execute" if effect.kind == CALENDAR_UPDATE_KIND else "send",
                   source="system",
                   subject=("The invite" if effect.kind == CALENDAR_UPDATE_KIND else "The reply")
                           + f": {state}",
                   verdict="refused" if state == FAILED else "allowed",
                   rule=RULE_EXECUTE, detail=detail)
        if self.audit is not None:
            self.audit.record("effect.state", task_id=cs.task_id,
                              detail={"change_id": cs.change_id, "version": cs.version,
                                      "kind": effect.kind, "state": state, "detail": detail})


CALENDAR_UPDATE_KIND, EMAIL_REPLY_KIND = "calendar_update", "email_reply"


def _digest_records(day_events, open_changes, settled) -> dict:
    """What each record RENDERS AS, keyed by the id a line would name it by.

    One renderer per source, and they are the only place these sentences are built — the lines
    `build_digest` composes are read back out of this mapping, so "the line is the render of the
    record it names" is true by construction rather than by two spellings agreeing.

    An id two records both claim, rendering differently, is a REFUSAL rather than a race: a line
    naming it could not say which record its sentence came from, and picking one would be this
    function deciding provenance on the owner's behalf.
    """
    from . import care as care_rules

    out: dict[str, str] = {}
    for rid, text in ([(_id_of(e), _digest_coming_text(e)) for e in day_events]
                      + [(_id_of(c), _digest_needs_text(c)) for c in open_changes]
                      + [(_id_of_row(r), _digest_happened_text(r)) for r in settled]):
        if not rid:
            continue
        if rid in out and out[rid] != text:
            raise care_rules.DigestBadLine(
                f"two records both answer to {rid!r} and render differently, so a digest line "
                f"naming it could not say which one it came from")
        out[rid] = text
    return out


def _id_of(record) -> str:
    return str(getattr(record, "id", "") or "")


def _id_of_row(row: dict) -> str:
    return str(row.get("change_id") or row.get("task_id") or "")


def _digest_coming_text(event) -> str:
    """What a meeting on the day reads as in the digest."""
    return f"{event.summary} at {_clock_time(event.start)}"


def _digest_needs_text(change) -> str:
    """What a change waiting on the owner reads as."""
    return f"{change.subject} is waiting on you"


def _digest_happened_text(row: dict) -> str:
    """What a finished effect reads as."""
    return f"{row.get('subject') or 'A change'} was {row.get('state')}"


class ApprovedBundles:
    """Answers "what did the owner approve for this change?", for the ports.

    The ports need the approved ARGUMENTS, and they are built before the change-set store exists —
    the registry is built from the skills, the broker is built from the registry, and the store is
    built after both. A genuine cycle, broken the way `wiring._ChangeResolver` breaks its own: hold
    nothing until something attaches a store, and let the unattached state mean REFUSAL rather than
    permission. A port that cannot reach the approved bundle has not established that its arguments
    are the approved ones, and "could not check" is not a check that passed.

    `for_change` returns the LATEST bundle for the change, which is the one the approval on the
    effect belongs to — `changesets.edit` mints version n+1 and makes version n unspendable, so
    there is exactly one live bundle per change at the moment an approval can be spent."""

    __slots__ = ("_store",)

    def __init__(self) -> None:
        self._store = None

    def attach(self, store) -> None:
        self._store = store

    def for_change(self, change_id: str):
        if self._store is None:
            return None
        try:
            return self._store.for_change(change_id)
        except Exception:                                            # noqa: BLE001
            # A store that cannot answer is not a store that said yes.
            return None


def _approved_bundle(bundles, change_id: str, action: str):
    """The approved arguments AND the bundle version they were read off, or None.

    The version is not decoration. An approval is consumed before the skill runs, and `edit` mints
    version n+1 without invalidating an approval that has already been spent — so a revision that
    lands while this call is in flight leaves the port holding arguments from a bundle that is no
    longer the change's current one, and nothing compared the two. Carrying the version out of the
    same read the arguments came from is what lets the port ask, at the call, whether the change
    has moved — see `_still_at_the_bound_version`.

    `None` is returned for every way the question can fail to have an answer: no resolver attached,
    no change id, no bundle, no such action. The CALLER decides what that means, and on both ports
    it means refusal — one helper so the two ports cannot disagree about it, and so the failure
    mode is written once."""
    if bundles is None or not str(change_id or ""):
        return None
    try:
        found = bundles.for_change(change_id)
    except Exception:                                                # noqa: BLE001
        return None
    if found is None:
        return None
    graded = getattr(found, action, None)
    arguments = getattr(graded, "arguments", None)
    if not isinstance(arguments, dict):
        return None
    return dict(arguments), found


def _approved_arguments(bundles, change_id: str, action: str) -> dict | None:
    """Just the arguments — `_approved_bundle` for callers that do not need the version."""
    found = _approved_bundle(bundles, change_id, action)
    return found[0] if found is not None else None


def _still_at_the_bound_version(bundles, change_id: str, action: str, bundle, *,
                                port: str) -> None:
    """Refuse unless the change is still on the version this call bound its fields to.

    The eighth review's second finding, at the last moment it can be refused. The approval is
    consumed before the skill runs, `edit` mints version n+1 without invalidating a spent approval,
    and the ports bound their fields once and then called the provider — so a revision landing in
    that window produced v2 while v1's bytes were already on their way out.

    RE-READ, not remembered: the bundle this call bound to is asked against the change as the store
    now has it, so the revision is seen rather than assumed absent. Compared on the VERSION and not
    on the arguments, because a revision may legitimately keep some arguments and change others —
    "different arguments" is the other binding's question, and this one is "is this still the
    version the owner's approval was raised over".

    What this does not close, stated rather than implied: the window between this check and the
    provider's own receipt. Closing that needs a conditional write the provider honours, which is
    why the calendar's `If-Match` exists and why the reply's idempotency key does — both are bound
    to the same approval, and neither is something this process can widen."""
    try:
        now = bundles.for_change(change_id) if bundles is not None else None
    except Exception:                                                # noqa: BLE001
        now = None
    bound = getattr(bundle, "version", None)
    current = getattr(now, "version", None)
    if bound is None or current is None or int(current) != int(bound):
        raise ApprovedArgumentsRefused(
            f"refused: the plan for this change was revised while this {port} was in flight, so "
            f"the arguments this call is holding are no longer the ones the owner's approval was "
            f"raised over. An approval authorises one version of a plan; carrying it out after the "
            f"plan has moved would send what the owner changed their mind about.")


def _bind_field(args: dict, approved: dict, name: str, *, port: str, change_id: str):
    """One outbound value, refused unless it is the value the owner approved.

    Compared with `!=` on the raw values rather than on a normalised form, and that is deliberate
    on both ports: `start`/`end` are placed on instants by `_placeable` and by the provider's own
    ETag arithmetic, but the APPROVED value is what the approval names, and "the same instant
    spelled differently" is still a different argument than the one the owner read on the review
    screen. The one place a looser comparison is right is the read-back AFTER the write, where the
    question is whether the provider applied it — see `_confirm_readback`, which uses
    `same_instant` for exactly that reason and not this one.

    The refusal names the FIELD and never the value: the value on this path can be a place or a
    time, and a receipt quoting it would be the disclosure."""
    want = approved.get(name)
    got = args.get(name)
    if want == got:
        return
    raise ApprovedArgumentsRefused(
        f"refused: the {name} on this {port} call is not the value the owner approved for this "
        f"change. An approval authorises the arguments it was raised over; a call carrying "
        f"different ones was never authorised, whatever it says.")


#: Every field of the calendar write that comes from the approved plan, in one place so the port
#: and the door-probe table cannot disagree about what "bound" covers. `location` is here as well as
#: in `_venue_not_rendered`, and the two answer different questions: that one asks whether the place
#: is the render of the record the change CHOSE, this one whether the change's choice is the one the
#: owner approved. A rebuild of the catalogue between approval and send would satisfy the first and
#: fail the second, which is the case the second exists for.
#:
#: `timezone` is the eighth review's fourth finding. The adapter added `start.timeZone` and
#: `end.timeZone` from the event it read at the moment of the call, outside this list entirely — so
#: the one field that tells the provider how to READ the approved instant was the one field nobody
#: approved. It is carried on the bundle now, from the pinned event the plan was built against, and
#: it is compared here like every other engine field.
CALENDAR_BOUND_FIELDS = ("event_id", "if_match_etag", "start", "end", "location", "send_updates",
                         "timezone")

#: Every field of the reply that comes from the approved plan, minus the two the gate already binds
#: on its own: `subject`/`body` against the change's own decomposition (`_outbound_from_bundle`).
#: What is left is the audience and the envelope state — who the message reaches, the ids it threads
#: by, and the key a retry matches on.
#:
#: `to` is the eighth review's third finding, and it is here because the gate's own check is
#: DELIBERATELY looser than this one. `guard_recipients` compares bare addresses, which is right for
#: its question — a display name must not decide who is allowed — and wrong as the only question
#: asked of the field, because the bare address is not what the adapter serialises. `To` is built
#: with `", ".join(to)`, so `Finance <alex@example.com>` reached the wire under an approval raised
#: over `alex@example.com`: same address, different header, and the display name is the part a
#: recipient reads. Bound by equality with the approved representation, which is the string the
#: owner saw on the review screen.
EMAIL_BOUND_FIELDS = ("to", "in_reply_to", "idempotency_key")


def _venue_not_rendered(change, location: str) -> str:
    """Why this calendar `location` is not the render of the change's own venue record, or "".

    The fifth review's third finding, at the last moment it can be refused. `location` was the one
    field on the calendar path a model authored end to end — `choice["address"]` verbatim — and the
    value checks either side of it ask whether the string is the owner's secret or another
    meeting's address. A model-invented place is neither, so both answered "no" and the invite was
    written with `send_updates="all"`: the notification to every guest of the meeting whose address
    had just gone out.

    Under composition the question changes shape. The engine renders the field from a catalogue
    entry it fetched, and the change records which entry — so the port does not have to recognise a
    bad value, it has to confirm a good one: is this string the render of the record this change
    chose? A value with no record behind it has no answer but "no".

    Empty string means the write may proceed. That is the honest state of exactly ONE thing, and
    the sixth review's fourth finding is that it used to cover two: a change that carries NO venue
    selection was reported as clean, so a hand-built bundle — or a plan whose research found no
    venue — wrote `location` with nothing having rendered it. "There is no record to compare
    against" is not "the value was checked"; it is the check failing to ask, and the rule for that
    on this path is the rule the window already follows: a check that cannot ask must refuse.

    The one state that still passes with an empty answer is a write that carries NO LOCATION AT
    ALL. That is not a hole and it is not this function's business: a calendar write that states no
    place writes no place, and the fields it does carry are checked by the protected-value and
    foreign-event checks on the same call. It is named here so the boundary of this rule is a
    sentence rather than an omission."""
    if change is None:
        return ("refused: this calendar write names a change this build cannot read, so the venue "
                "it is about cannot be compared against the record it was chosen from. A write "
                "whose provenance cannot be established is not a write that established it.")
    if not str(location or "").strip():
        return ""
    catalogue = list(getattr(change, "venue_catalogue", None) or ())
    venue_id = str(getattr(change, "venue_id", "") or "")
    if not catalogue:
        return ("refused: this calendar write carries a location, and the change it belongs to "
                "records no venue catalogue to render it from. A place is selected from the pages "
                "this build fetched; a build that fetched none cannot have selected one.")
    if not venue_id:
        return ("refused: this calendar write carries a location, and the change it belongs to "
                "names no venue record that renders it. A location with no record behind it has no "
                "answer but no.")
    from .changes import render_venue, venue_record

    record = venue_record(catalogue, venue_id)
    if record is None:
        return (f"refused: this change says its venue is {venue_id!r}, and the catalogue it was "
                f"chosen from has no such entry. A calendar field is rendered from a record this "
                f"build fetched; a record that does not exist cannot be one.")
    allowed = render_venue(record)
    if str(location) != allowed:
        return ("refused: the location on this calendar write is not the place the researched "
                "record renders. A venue is selected from the pages this build fetched and written "
                "from that selection, never stated on the way out.")
    return ""


def _offset_of(stamp: str) -> _dt.timedelta:
    """The UTC offset a stamp carries, as a timedelta, and a REFUSAL when it carries none.

    A naive stamp is not "UTC by default". The owner's day is a local day, and an appointment at
    23:30 in a +01:00 zone is on the PREVIOUS day in UTC — so defaulting to UTC does not shift a
    digest by an hour, it moves appointments across the boundary the digest is about. Raising is
    the honest answer: the caller has a stamp it cannot place in a day, and there is no day to
    build a digest for.
    """
    try:
        offset = _dt.datetime.fromisoformat(str(stamp)).utcoffset()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"a stamp with no readable time: {type(exc).__name__}") from exc
    if offset is None:
        raise ValueError(
            "a timestamp with no UTC offset cannot be placed in a day, and a digest is about a "
            "day")
    return offset


def _clock_time(stamp: str) -> str:
    """`14:30` — the wall-clock time the owner would read on the invite, not an instant.

    The offset is dropped rather than converted, because the question this answers is "what time
    does the calendar show", and the calendar shows the event's own local time.
    """
    try:
        return _dt.datetime.fromisoformat(str(stamp)).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def _owner_zone(timezone: str):
    """The owner's configured zone as a `ZoneInfo`, or None when it is not an IANA name.

    `None` is a real state and not a failure: the sample product is configured with an OFFSET
    (`"+01:00"`), which is a perfectly good answer to "what time is it for the owner" and not a
    zone anything can be attached to. The callers fall back to the instant's own offset there, which
    for a stamp the provider issued IS the owner's offset."""
    if not timezone:
        return None
    try:
        return ZoneInfo(str(timezone))
    except Exception:                                                # noqa: BLE001
        return None


def _placeable(stamp: str) -> bool:
    """Is this an instant this build can place on a day? An OFFSET is required, not assumed.

    A naive stamp is not "UTC by default": it is "this build cannot say which day this is", and the
    calendar port refuses the write rather than guessing — see `update_event`, where the alternative
    was a window that came back empty and a foreign place that went out because the question was
    malformed rather than answered.
    """
    try:
        return _dt.datetime.fromisoformat(str(stamp or "")).tzinfo is not None
    except (TypeError, ValueError):
        return False


def _local_day(stamp: str, timezone: str = "") -> str:
    """The day `stamp` falls on IN THE OWNER'S ZONE, or "" when the stamp cannot be placed.

    The day a reply lands on is the owner's day, and it is read from a stamp the PROVIDER issued
    rather than from anything a bundle carries. The conversion is the point: an event at 23:30
    `+01:00` is on the previous day in UTC, and a window built from the wrong one asks about a day
    the meeting is not on — which answers "no other meetings" every time.

    A naive stamp is refused rather than assumed: "no offset" is not "UTC", it is "this build cannot
    say which day this is", and the caller turns "" into the same refusal it uses for a missing
    pin."""
    text = str(stamp or "")
    if not text:
        return ""
    try:
        moment = _dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return ""
    zone = _owner_zone(timezone)
    if moment.tzinfo is None:
        if zone is None:
            return ""
        moment = moment.replace(tzinfo=zone)
    elif zone is not None:
        moment = moment.astimezone(zone)
    return moment.strftime("%Y-%m-%d")


def _day_window(day: str, now: str = "", *, timezone: str = "") -> tuple[str, str]:
    """The half-open window `[00:00, next 00:00)` of `day`, at the OWNER's offset.

    Three sources, in the order of how much they can be trusted:

    * `timezone`, the owner's configured zone. Resolved for the DAY being asked about rather than
      for today, so a day either side of a daylight-saving boundary gets its own offset — a window
      built from the wrong one starts or ends an hour outside the day it names;
    * `now`, the instant the caller is reasoning from. This is what the DIGEST uses — it reads a real
      clock, and the clock's own offset is the owner's — and what both PORTS pass: they hand over the
      PINNED EVENT's stamp, so a build configured with an offset rather than a zone still gets the
      offset the provider issued the meeting at. The host's clock is not consulted on that path;
      "the host happened to be in the owner's zone" is not a guard;
    * the host's offset. Last, and only because a build with neither has to ask about SOME window.

    `now` is deliberately not consulted when `timezone` resolves to a zone. The fifth review's second
    finding is exactly that: an offset taken from a caller-supplied stamp is a caller-chosen window,
    and a stamp that says `-12:00` moves the queried day twelve hours and empties the foreign set —
    which both guards then read as innocence while a foreign place and a foreign time went out."""
    start = _dt.datetime.fromisoformat(f"{day}T00:00:00")
    zone = _owner_zone(timezone)
    offset = start.replace(tzinfo=zone).utcoffset() if zone is not None else None
    if offset is None:
        try:
            offset = _offset_of(now)
        except (ValueError, TypeError):
            offset = _dt.datetime.now().astimezone().utcoffset() or _dt.timedelta(0)
    start = start.replace(tzinfo=_dt.timezone(offset))
    return (start.isoformat(timespec="seconds"),
            (start + _dt.timedelta(days=1)).isoformat(timespec="seconds"))


def _outbound_from_bundle(change, args: dict, *, pinned: str, foreign) -> OutboundEmail:
    """The email a bundle CLAIMS to be, rebuilt from the change's own decomposition.

    The port's whole job is to judge what is about to leave, and the only way to judge "is this the
    reply the engine composed" is to reconstruct that reply from where the engine wrote it down —
    `change.reply_fragments` and `change.reply_template` — and compare. Reconstructing it from
    `args` would compare the arguments against themselves, which is the shape of check that made
    the fifth review's first finding invisible: the port ran `check_text` over a body it had taken
    on trust and never asked whether any fragment declared it.

    A change with no decomposition at all yields an empty render, and `guard_email` refuses a body
    that is not the render of its fragments — so a hand-built bundle is refused rather than sent,
    which is the fail-closed direction. That is the same choice `_permitted_recipients` already
    makes about the audience: an unestablished fact is not permission.

    `foreign` is passed in rather than rebuilt here because the caller reads it from the provider
    under the window IT derived, and the two must be the same reading — a second fetch is a second
    answer about a calendar that may have moved."""
    from .changes import _reply_fragment_source, subject_parts

    fragments = tuple(Fragment(source=str(f.get("source") or ""), text=str(f.get("text") or ""),
                               event_id=str(f.get("event_id") or ""))
                      for f in (getattr(change, "reply_fragments", None) or () if change else ()))
    template = str(getattr(change, "reply_template", "") or "") if change else ""
    # The SUBJECT's pieces come off the change for exactly the reason the body's do, and the sixth
    # review's first finding is what their absence cost: `subject` was read from `args` and handed
    # to the provider with nothing having rendered it, so a novel subject passed the only check it
    # faced — a scan for protected values, which cannot see a subject that is merely untrue.
    # `guard_email` compares `args["subject"]` against this render, so the equality the body has had
    # since the fifth review is now asked of the header the recipient actually reads.
    subj_template, subj_pieces = subject_parts(change) if change else ("", ())
    return OutboundEmail(
        to=tuple(args.get("to") or ()), subject=str(args.get("subject") or ""),
        body=str(args.get("body") or ""), fragments=fragments,
        in_reply_to=str(args.get("in_reply_to") or ""),
        template_body=template,
        subject_template=subj_template, subject_fragments=subj_pieces,
        # Only meaningful when there IS a template, and `compose_email`/`guard_email` both check it
        # against the allowlist — so the label has to be the one the composer recorded rather than
        # a guess made here. A change whose template carries no recorded source is refused by the
        # gate, which is right: a fixed sentence nobody can attribute is the unchecked channel the
        # source check exists to close.
        template_source=_reply_fragment_source(change) if template else "",
        pinned_event=pinned, foreign_fragments=tuple(foreign or ()))


def _permitted_recipients(mail, change, *, thread_id: str,
                          message_id: str) -> tuple[set[str], str, object | None]:
    """Who may receive this reply — bound to the APPROVED CHANGE, not to the plan or the thread.

    The reviewed version returned the sender of any message in the provider's thread whose `id`
    happened to equal `in_reply_to`. That is not a verified sender identity; it is "whoever
    currently occupies that provider record", and the review's two examples both defeat it: an
    attacker-controlled message planted in the thread under the referenced id, and a message this
    assistant itself sent, which lives in the same thread because that is what threading means.
    Either one becomes the permitted recipient, which is the whole audience of the reply.

    The pin is therefore re-derived, every time, from the change the owner approved, and it holds
    only if all of this is true of the provider's own copy of the message:

    * the reply names **this change's own inbound message** (`change.message_id`) — not merely a
      message with a matching id;
    * it names **this change's thread** (`change.thread_id`);
    * the provider serves that message **within that thread** — a record carrying the right id but
      belonging to another thread is a foreign record, and it fails here;
    * the sender is a **syntactically valid address** — an empty or malformed `From` is not an
      audience;
    * the sender is **the person this change was raised from**, compared against the provider's
      copy rather than against the plan that is being checked. This is also what excludes an
      assistant-authored message: a reply this product sent is *from* the owner, and the owner is
      never the correspondent whose inbound message created the change.

    Anything else returns an EMPTY set, which `guard_recipients` permits nobody against, so the
    send is refused. Empty is never "allow": a check that could not establish an audience has not
    established an audience. `why` is returned rather than logged so the caller can record it — a
    refusal the owner cannot read is the failure this module's log exists to prevent.

    The third element is the provider's copy of the message it just validated — the same object the
    id, the thread id and the sender were read off, and never a second read. The port needs it for
    one thing: the words of the message the change is about are the words its own author will
    receive back, so they are not another meeting's detail (`foreign_fragments_of`'s `exempt_text`).
    Returning it rather than letting the caller fetch it again is what keeps that exemption bound to
    the record the pin was checked against — a second lookup would be a second, unchecked answer.

    What this does not establish, stated rather than implied: this build stores no address for the
    owner's own mailbox, so "not the owner" is enforced as "the correspondent this change came
    from" and not by comparing against a known self-address. An owner who wrote to themselves
    would be their own correspondent, and the reply would go to the owner — which is not a
    disclosure. The review's two named attacks are both caught by the sender comparison above.

    The address compared against is `change.sender_pinned` — captured and validated when the
    change was DETECTED — and not `change.from_address` re-read out of the plan. That distinction
    is the second review's Medium finding: a comparison whose expected value comes from the same
    mutable surface as the value being compared establishes agreement, not identity. The provider
    is asked to agree with a pin it has never been able to write to; a change with no pin refuses,
    because "no pinned correspondent" is not "any correspondent"."""
    if change is None:
        return set(), "the change this reply belongs to could not be read", None
    if not message_id or message_id != str(getattr(change, "message_id", "")):
        return set(), ("the reply names a different message than the one this change was raised "
                       "from"), None
    if not thread_id or thread_id != str(getattr(change, "thread_id", "")):
        return set(), "the reply names a different thread than the one this change lives in", None
    try:
        thread = mail.get_thread(thread_id)
    except Exception as exc:                                          # noqa: BLE001
        return set(), f"the thread could not be read: {exc}", None
    for message in thread or ():
        if str(getattr(message, "id", "")) != message_id:
            continue
        if str(getattr(message, "thread_id", thread_id)) != thread_id:
            return set(), (f"the provider served message {message_id} under a different thread, "
                           f"so it is not this change's message"), None
        sender = sender_identity(getattr(message, "sender", ""))
        if not sender:
            return set(), "the message being replied to has no usable sender address", None
        # The PIN, not the plan's own `from_address`. The review's Medium finding is that
        # re-reading the expected identity off a record the provider can rewrite makes the check
        # answerable by whoever rewrote it. `sender_pinned` was captured and validated when the
        # change was DETECTED and has not been near a provider since; the provider is asked to
        # agree with it, and nothing here reads an expected address back out of provider content.
        pinned = str(getattr(change, "sender_pinned", "") or "")
        if not pinned:
            return set(), ("this change was raised without pinning a correspondent, so there is "
                           "no identity for the provider to be checked against"), None
        if sender != pinned:
            return set(), ("the message being replied to was not written by the person this "
                           "change was raised from"), None
        return ({sender}, f"the reply goes to {sender}, who wrote the message this change is about",
                message)
    return set(), "the message this change is about is not in its thread", None


def _valid_address(value: str) -> bool:
    """Whether this is an address at all. The rule now lives in `ports.sender_identity` — one
    implementation, shared with ingestion, so "the sender" cannot mean two things at the two ends
    of the same change. Kept under this name because the recipient-pin tests import it."""
    return bool(sender_identity(str(value or "")))


def _bare_address(value: str) -> str:
    """The text inside a `From`-style header, unvalidated and case-preserved.

    NOT on the live path, and deliberately so: `_permitted_recipients` goes through
    `ports.sender_identity`, which validates, and the difference between "the characters between
    the angle brackets" and "an address this build accepts" is the whole of one review finding.
    This exists because the mutation control for that finding restores the REVIEWED resolution —
    which called this function — and a control that reconstructed the old code from memory would
    be testing the reconstruction rather than the fix."""
    text = str(value or "").strip()
    match = re.search(r"<([^<>]+)>", text)
    if match:
        text = match.group(1)
    return text.strip().strip("<>").strip()


def _ambiguous(error: str | None) -> bool:
    return bool(error) and bool(_AMBIGUOUS_RE.search(error or ""))


def _summary(calendar_effect: Effect, email_effect: Effect) -> str:
    """Both effects, each in its own state's words.

    Built from the two clause tables rather than from a chain of comparisons, because the chain had
    a shape that could only express two answers: every state that was not `done` fell through to
    "not sent", including the one state whose whole purpose is that nothing is known. Reading each
    effect's own state is the same rule `web/lib/feed.ts` applies per effect, so the heading and the
    receipt under it cannot describe the same run differently.
    """
    calendar = CALENDAR_SENTENCE.get(calendar_effect.state, CALENDAR_SENTENCE[UNKNOWN])
    email = EMAIL_SENTENCE.get(email_effect.state, EMAIL_SENTENCE[UNKNOWN])
    return f"{calendar} {email}"


def _final_status(calendar_effect: Effect, email_effect: Effect) -> str:
    if calendar_effect.state == DONE and email_effect.state == DONE:
        return DONE_STATUS
    if calendar_effect.state == DONE:
        return PARTIAL_STATUS
    return PARTIAL_STATUS if email_effect.state == DONE else STOPPED_STATUS
