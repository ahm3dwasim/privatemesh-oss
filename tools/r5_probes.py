"""The reviewers' probes, re-run one by one against this build — the spellings, and the doors.

PART ONE (the fourth review) asks three questions per spelling, because the answer is not the same
for all three:

  BODY      can the spelling become a byte of a REPLY BODY? Item 1 says a body is the render of
            the engine's own catalogue, so the question is not "does the reader know it" but "is
            there a catalogue entry whose text is this". Asked of `eligible_reply_fragments`.
  READER    does `time_of_day_mentions` read it as the time it names? This is the SECOND net, and
            it is still the only check on the surfaces a fragment cannot reach.
  SURFACE   where the spelling would still matter if a model wrote it: the fields that are NOT
            fragment-composed — the invite's free-text fields and the reply's subject.

PART TWO (the fifth review, `--doors`) is the round-6 rule asked of every outbound field at the
port that writes it: **every field is either an engine-rendered fragment carrying the event id it
derives from, or the action is refused — and the day a guard reasons about comes from trusted
state, never from caller input.** One row per (port, field), each with an ATTACK and a CONTROL. An
attack that says SENT is an open door; a control that says REFUSED is a port that refuses
everything, which would make every attack row worthless.

ROUND SEVEN widened the table from "the fields a fragment renders" to EVERY field on the wire, in
the three groups the round's brief names:

  ITEM 1  the SUBJECT, at both ports that have one. It is a render of a template from a closed set
          plus named fragments, and `guard_email` compares the finished header against that render.
  ITEM 2  the ENVELOPE — `In-Reply-To`, `References`, `X-PrivateMesh-Key` — and the HEADER SET
          itself, which is closed: a header the builder did not produce is refused rather than
          merely absent. This is the one row here that drives `gmail.GmailClient.send_reply`, so
          the moment being probed is the client's own, after every adapter above it has had its say.
  ITEM 3  the CALENDAR ENGINE FIELDS — `event_id`, `start`, `end`, `location`, `send_updates`,
          `if_match_etag` — each bound to the arguments the owner APPROVED rather than to the ones
          the call happens to carry.

Every attack row below is a value the owner never approved; every control row is the same call
with the approved value in the same place.

Run: `.venv/bin/python tools/r5_probes.py [--end-to-end]`   (part one, JSON)
     `.venv/bin/python tools/r5_probes.py --doors`          (part two, the port table)
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "tests")

from privatemesh.boundary import (Fragment, eligible_reply_fragments, reply_catalogue,
                                  time_of_day_mentions)

#: The reviewer's list, with the time each one names. Every one of these is 11:15 in the round-4
#: review's framing — a second meeting's slot, which the reply must not describe.
PROBES: list[tuple[str, int]] = [
    ("09:15-09:45", 9 * 60 + 15),
    ("fifteen past eleven", 11 * 60 + 15),
    ("in fifteen minutes", 11 * 60 + 15),
    ("quarter after nine pm", 21 * 60 + 15),
    ("twelve fifteen pm", 12 * 60 + 15),
    ("9h15", 9 * 60 + 15),
    ("neuf heures quinze", 9 * 60 + 15),
    ("九点十五分", 9 * 60 + 15),
    ("MToxNSBhbQ==", 11 * 60 + 15),                      # base64 of "11:15 am"
    ("１１：１５", 11 * 60 + 15),                          # full-width digits
]


def catalogue():
    """The engine's own renderings for the pinned meeting, built the way `_prepare` builds them."""
    return reply_catalogue(pinned="ev1", greeting="Hi Alex,", recipient_name="Alex Doe",
                           slot_label="15:00-15:45", venue_name="The Standard",
                           venue_address="King's Cross", owner_name="Sam")


def end_to_end(text: str) -> dict:
    """The same sentence through the WHOLE engine: a draft that writes it, and what went out.

    The pure-function column above says the lookup would drop it. This says the run does — the
    draft is handed to `_prepare` as the model's answer, and the reply that ends up in the plan is
    read back. A `body` that reports ineligible but a run that sends it anyway is the failure this
    leg exists to make impossible to report by accident.
    """
    from test_changes import harness

    h = harness(script={"draft": {"subject": f"Re: Our usual cafe has closed",
                                  "template_body": f"Hi Alex, {text}.",
                                  "fragments": [{"source": "time",
                                                 "text": f"Hi Alex, {text}."}]}})
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    body = h.rec.text()
    return {"stage": change.stage, "defaulted": bool(change.reply_defaulted),
            "in_reply": text in json.dumps(change.reply_fragments, ensure_ascii=False)
                        or text in (change.reply_template or ""),
            "in_outbound": text in body}


# ==================================================== part two — one row per outbound field ------
#: The sentence every reply-door attack uses: a fact about the OTHER meeting of the day. It is
#: DECLARED as a fragment of the change, so the body the caller supplies is the render of its own
#: declared pieces — the body-equality check has nothing to say about it, which leaves exactly the
#: check under test as the thing that has to refuse it.
FOREIGN = "Your Team standup at 09:15 moves too."


def _two_meetings():
    """The day the doors are about: the pinned meeting, and a second one the reply must not name."""
    from fakes_v2 import invite
    return [invite(), invite("evt-standup", start="09:15", end="09:30", etag="etag-standup")]


def _declares(h, text: str, *, event_id: str = "evt-standup"):
    """Make the change DECLARE the sentence, so only the foreign check can refuse it.

    This is the strongest position an attack can be in: the bytes are the render of the change's own
    fragments, so the port's equality check passes, and the sentence is on the wire only if the
    foreign-event check is the one that fails. A build that has no such check sends it.
    """
    change = h.change_resolver.registered["c1"]
    change.event_pinned = "ev1"
    change.reply_template = ""
    change.reply_fragments = [{"source": "venue", "text": text, "event_id": event_id}]
    return change


def _run(h, *, over=None, cs=None):
    """Approve (the engine's own arguments, plus `over`) and run. Returns (harness, outcome)."""
    from test_redteam_guards import _approved
    cs = cs or _approved(h, **(over or {}))
    assert cs.status == "approved", "the fixture failed to approve, so nothing was tested"
    return h, h.executor.execute(cs.id)


def _window(h):
    """Watch the window the port actually asks the calendar for.

    A probe-local spy rather than a change to `FakeCalendar`: the recorder's payloads are asserted
    by other tests, and a probe that edited the fixture to see itself would be measuring itself.
    """
    seen = []
    inner = h.calendar.list_events

    def spy(day_start, day_end):
        seen.append((day_start, day_end))
        return inner(day_start, day_end)

    h.calendar.list_events = spy
    return seen


def _verdict(h, outcome, *, kind: str, exc: str) -> str:
    """What the port did, in one phrase, with the name of the refusal when it refused."""
    leaked = bool(h.mail.sent) if kind == "email_reply" else bool(h.rec.surfaces("calendar"))
    if leaked:
        return "SENT"
    detail = next((e.detail or "" for e in outcome.effects if e.kind == kind), "")
    return f"refused ({exc})" if exc in detail else f"refused (other: {detail[:56]!r})"


def _split(h, *, email_over=None):
    """Approve a bundle over the engine's own arguments, and raise the APPROVAL over different ones.

    This split IS the finding the round-7 rows are about. `ChangeSetStore.create` stores the
    arguments the owner reads on the review screen; the broker runs `req.arguments` — the ones the
    approval was requested over — and until this round nothing compared the two. The product's own
    `_prepare` raises both over one dict, so a probe that did the same would be asking whether a
    value equals itself; the attack is exactly the state where they differ.
    """
    from privatemesh.changes import _calendar_display
    from privatemesh.execute import CALENDAR_SKILL, EMAIL_SKILL

    approved = h.email_args()
    attacked = h.email_args(**(email_over or {}))
    cal_args = h.calendar_args()
    cal = h.approvals.request("task1", CALENDAR_SKILL, cal_args)
    mail = h.approvals.request("task1", EMAIL_SKILL, attacked)
    cs = h.changesets.create(change_id="c1", task_id="task1",
                             event_etag=h.calendar.get_event("ev1").etag,
                             boundary_id="pc1", boundary_version=h.boundary.version_of("pc1"),
                             calendar_args=cal_args, email_args=approved,
                             calendar_display=_calendar_display(h.calendar.get_event("ev1"),
                                                                cal_args),
                             approval_ids=(cal.id, mail.id))
    cs = h.changesets.approve(cs.id, cs.version,
                              current_event_etag=h.calendar.get_event("ev1").etag,
                              current_boundary_version=h.boundary.version_of("pc1"))
    assert cs.status == "approved", "the fixture failed to approve, so nothing was tested"
    return h, h.executor.execute(cs.id)


def _run_from(h, **over_and_cs):
    """`_run` with the change set built from a caller-supplied argument set."""
    return _run(h, **over_and_cs)


def door_rows() -> list[dict]:
    """Every outbound field, at the port that writes it, attacked and controlled."""
    from fakes_v2 import Harness
    from privatemesh.care import DigestBadLine, DigestLine, compose_digest
    from privatemesh.changes import render_venue, venue_record
    from privatemesh.sim import SAMPLE_DATE
    from test_changes import harness as engine_harness

    rows = []

    def row(port, field, attack, attack_result, control, control_result):
        rows.append({"port": port, "field": field, "attack": attack,
                     "attack_result": attack_result, "control": control,
                     "control_result": control_result})

    # ---- mail port: `body`, a sentence no fragment of the change declares ------------------------
    h, out = _run(Harness(events=_two_meetings()),
                  over={"body": "Hi Alex, I have moved the standup to 09:15 as well."})
    attacked = _verdict(h, out, kind="email_reply", exc="SchemaLeak")
    control_h, _ = _run(Harness(events=_two_meetings()))     # `email_args()` renders `c1` exactly
    control = "SENT" if control_h.mail.sent else "refused (the control was refused too)"

    row("mail.send_reply", "body",
        "a body that is not the render of the change's own fragments", attacked,
        "the body the change declares", control)

    # ---- mail port: `subject`, the header the reviewed check never read --------------------------
    # The subject is DECLARED by the change — the thread's own words, which is where a reply's
    # subject legitimately comes from — and those words name the day's other meeting. Declaring it
    # is the strongest position the attack can be in: the round-7 render check passes, so the only
    # thing that can refuse the send is the foreign-event question being asked of the HEADER.
    subject_h = Harness(events=_two_meetings())
    _declares(subject_h, FOREIGN)
    subject_h.change_resolver.registered["c1"].reply_subject_fragments = [
        {"source": "thread_subject", "text": FOREIGN}]
    h, out = _run(subject_h, over={"subject": f"Re: {FOREIGN}", "body": FOREIGN})
    row("mail.send_reply", "subject",
        "a subject naming the day's other meeting, with a clean body",
        _verdict(h, out, kind="email_reply", exc="ForeignEventLeak"),
        "the thread's own subject", control)

    # ---- mail port: `to`, an audience the change never established -------------------------------
    h, out = _run(Harness(events=_two_meetings()),
                  over={"to": ["alex@example.com", "dave@example.com"]})
    row("mail.send_reply", "to",
        "an approved audience wider than the correspondent",
        _verdict(h, out, kind="email_reply", exc="RecipientRefused"),
        "the correspondent who wrote the message", control)

    # ---- mail port: `in_reply_to`, a message this change is not about ----------------------------
    # ONE meeting on this day, deliberately: the audience binding is the door under test, and a
    # second meeting would let the foreign-event check refuse first and the row would report a
    # refusal that is not about the field it names.
    h, out = _run(Harness(events=[_two_meetings()[0]]), over={"in_reply_to": "m-other"})
    solo = "SENT" if _run(Harness(events=[_two_meetings()[0]]))[0].mail.sent else "refused"
    row("mail.send_reply", "in_reply_to",
        "a message id that is not this change's own inbound message",
        _verdict(h, out, kind="email_reply", exc="RecipientRefused"),
        "this change's own inbound message", solo)

    # ---- the window: the OFFSET half -------------------------------------------------------------
    # The bundle's proposal stamp is twelve hours from the owner's day. The port asks about the
    # owner's day anyway, so the second meeting stays in the foreign set and the reply is refused.
    # The CONTROL is the same shape with the PIN moved to the day the caller names: the window
    # follows the pin and moves with it — which is what makes the row above a statement about
    # provenance rather than about a window that could not have moved in the first place.
    def window_probe(*, stamp_day: str, pin_day: str, declared: bool):
        """`pin_day` moves the PINNED EVENT — the only thing the window is allowed to follow."""
        import dataclasses
        events = _two_meetings()
        events[0] = dataclasses.replace(events[0], start=f"{pin_day}T15:00:00+01:00",
                                        end=f"{pin_day}T15:45:00+01:00")
        hx = Harness(events=events)
        if declared:
            _declares(hx, FOREIGN)
        hx.change_resolver.registered["c1"].proposal = {"start": f"{stamp_day}T15:00:00-12:00"}
        hx.change_resolver.registered["c1"].plan_day = ""      # the bundle names no day at all
        seen = _window(hx)
        hx, outcome = _run(hx, over={"body": FOREIGN} if declared else None)
        return seen[0] if seen else None, (
            _verdict(hx, outcome, kind="email_reply", exc="ForeignEventLeak") if declared
            else "no attack")

    window_a, verdict_a = window_probe(stamp_day="2026-09-19", pin_day=SAMPLE_DATE, declared=True)
    window_b, _ = window_probe(stamp_day="2026-09-18", pin_day="2026-09-18", declared=False)
    row("calendar.list_events", "window (offset)",
        "a proposal stamp at -12:00 — the bundle chooses the offset",
        f"{verdict_a}; asked for {window_a}",
        "the same stamp with the PIN on the caller's day — the window is the pin's",
        f"asked for {window_b}")

    # ---- the window: the DAY half ----------------------------------------------------------------
    # `care_day` is the other way a bundle can name the day it is checked in. The plan day is blank
    # here and the caller supplies a day twelve hours off; the pin is untouched.
    hx = Harness(events=_two_meetings())
    _declares(hx, FOREIGN)
    hx.change_resolver.registered["c1"].plan_day = ""
    seen = _window(hx)
    hx, outcome = _run(hx, over={"body": FOREIGN, "care_day": "2026-09-18"})
    row("calendar.list_events", "window (day)",
        "a `care_day` twelve hours off, with the plan day blank",
        f"{_verdict(hx, outcome, kind='email_reply', exc='ForeignEventLeak')}; "
        f"asked for {seen[0] if seen else None}",
        "the pin's own day", f"asked for {window_b}")

    # ---- calendar port: `location`, the field a model authored end to end ------------------------
    def cal_case(location: str):
        hx = engine_harness()
        change = hx.engine.prepare(hx.engine.detect(None)[0].id)
        assert change.venue_id, "the prepared change carries no venue selection"
        hx.change_resolver.registered["c1"] = change
        cal = hx.calendar_args(location=location)
        cal["change_id"] = "c1"
        mail = hx.email_args(body=" ".join([change.reply_template]
                                           + [f["text"] for f in change.reply_fragments]))
        mail["change_id"] = "c1"
        cs = hx.approved_changeset(calendar_args=cal, email_args=mail)
        assert cs.status == "approved", "the fixture failed to approve, so nothing was tested"
        return hx, hx.executor.execute(cs.id), change

    attacker, out_a, prepared = cal_case("Room 2, Town Hall")
    record = venue_record(prepared.venue_catalogue, prepared.venue_id)
    rendered = render_venue(record)
    control_h, out_c, _ = cal_case(rendered)
    written = control_h.rec.surfaces("calendar")
    row("calendar.update_event", "location",
        "a place the research never returned",
        _verdict(attacker, out_a, kind="calendar_update", exc="CalendarRefused"),
        f"the render of the record the change chose ({rendered!r})",
        f"written ({written[0]['payload']['location']!r})" if written else "refused (control)")

    # ---- calendar port: the window its foreign check reasons about -------------------------------
    # The same door as the reply's, at the port the review's own sentence names ("a foreign location
    # and reply went out"). The window used to come off the `start` being written — day AND offset —
    # so a `-12:00` stamp asked about a window the owner's day does not occupy and the day's other
    # meeting fell outside it. Both rows below show the window the port ASKED FOR.
    #
    # The place is written from a catalogue record THIS CHANGE CHOSE, and that record renders the
    # other meeting's own address. That is deliberate and it is what keeps this row about the
    # window: since round seven a `location` with no venue record behind it is refused by the venue
    # check, which would fire first and hide whether the foreign check ever saw the day. With a real
    # record behind it, the venue check passes and the FOREIGN check is the only thing left standing.
    def cal_window(*, stamp: str, other_meeting_place: bool):
        import dataclasses
        from fakes_v2 import at
        from privatemesh.ports import CalendarEvent
        second = CalendarEvent(id="ev2", etag="etag-ev2", summary="Someone else",
                               start=at("11:15"), end=at("11:45"), timezone="Europe/London",
                               location="Room 2, Town Hall", attendees=("other@example.com",))
        events = [dataclasses.replace(_two_meetings()[0], start=f"{SAMPLE_DATE}T15:00:00+01:00",
                                      end=f"{SAMPLE_DATE}T15:45:00+01:00"), second]
        hx = Harness(events=events)
        change = hx.change_resolver.registered["c1"]
        if other_meeting_place:
            change.venue_id = "v-other"
            change.venue_catalogue = [{"id": "v-other", "name": "Room 2", "address": "Town Hall",
                                       "url": "https://example.com/room-2",
                                       "title": "Room 2, Town Hall",
                                       "excerpt": "The other meeting's address."}]
        _declares(hx, FOREIGN)      # the reply declares a sentence about the other meeting too, so
        seen = _window(hx)          # the run reaches the calendar port with the day in hand
        import datetime as _d
        end = (_d.datetime.fromisoformat(stamp) + _d.timedelta(minutes=45)).isoformat()
        location = ("Room 2, Town Hall" if other_meeting_place
                    else render_venue(venue_record(change.venue_catalogue, change.venue_id)))
        cs = hx.approved_changeset(calendar_args=hx.calendar_args(start=stamp, end=end,
                                                                  location=location),
                                   email_args=hx.email_args(body=FOREIGN))
        outcome = hx.executor.execute(cs.id)
        return hx, outcome, (seen[0] if seen else None)

    att, out_att, win_att = cal_window(stamp="2026-09-19T15:00:00-12:00",
                                       other_meeting_place=True)
    hones, out_hon, win_hon = cal_window(stamp=f"{SAMPLE_DATE}T15:00:00+01:00",
                                         other_meeting_place=True)
    clean, out_clean, _ = cal_window(stamp=f"{SAMPLE_DATE}T15:00:00+01:00",
                                     other_meeting_place=False)
    row("calendar.update_event", "window (foreign set)",
        "a start stamped -12:00, with the day's other meeting's place as the location",
        f"{_verdict(att, out_att, kind='calendar_update', exc='ForeignEventLeak')}; "
        f"asked for {win_att}",
        "the same write at the owner's own stamp — same window, so the bundle is not choosing it",
        f"asked for {win_hon}; a location sharing nothing with the day is "
        f"{'written' if clean.rec.surfaces('calendar') else _verdict(clean, out_clean, kind='calendar_update', exc='?')}")

    # ---- the digest: a line, and the record it has to carry --------------------------------------
    def digest(lines, records):
        try:
            plan = compose_digest(day=SAMPLE_DATE, recipients=("alex@example.com",), coming=lines,
                                  records=records)
            return ("composed, event_ids="
                    f"{[f.event_id for f in plan.fragments if f.source == 'care_coming']}")
        except DigestBadLine as exc:
            return f"refused (DigestBadLine: {str(exc)[:52]}…)"

    row("compose_digest → mail", "digest line / fragment event_id",
        "a line naming a record this build cannot read",
        digest([DigestLine(source="care_coming", record_id="does-not-exist",
                           text="Standup at 09:15")], {"ev1": "Standup at 09:15"}),
        "a line naming a record it can",
        digest([DigestLine(source="care_coming", record_id="ev1", text="Standup at 09:15")],
               {"ev1": "Standup at 09:15"}))

    # ============================== round seven: every field, not just the rendered ones =========

    # ---- ITEM 1, the reply subject's PROVENANCE ------------------------------------------------
    # The row above attacks the subject's CONTENT (it names another meeting, and the foreign check
    # refuses it). This one attacks its ORIGIN: the bytes are innocuous, and no source declared
    # them. That is the reviewed door — `subject` was read from the caller and handed to the
    # provider with only a protected-value scan between them, and a value scan cannot see a subject
    # that is merely untrue.
    h, out = _run(Harness(events=_two_meetings()), over={"subject": "Re: Quarterly planning"})
    row("mail.send_reply", "subject (provenance)",
        "a subject whose words no fragment of this change declares",
        _verdict(h, out, kind="email_reply", exc="SchemaLeak"),
        "the thread's own subject, rendered from the change's own pieces", control)

    # ---- ITEM 1, the DIGEST subject ------------------------------------------------------------
    # A digest has a subject too, and it is the same rule: the closed template plus the plan's own
    # day. The attack is what a caller (or an adapter) would produce — the right shape, a different
    # day — and it is refused for the reason the reply's is: nothing rendered it.
    def digest_subject_case(*, override: str | None):
        import dataclasses
        from privatemesh.boundary import BoundaryStore, PrivacyGate
        from privatemesh.care import CareCircleStore, digest_email

        circle = CareCircleStore()
        circle.add(name="Alex", email="alex@example.com", may_receive=["digest"], by="owner")
        gate = PrivacyGate(BoundaryStore(), care_circle=circle)
        plan = compose_digest(day=SAMPLE_DATE, recipients=("alex@example.com",),
                              records={}, happened=[])
        built = digest_email(plan, gate=gate, care_circle=circle)
        if override is None:
            return f"accepted ({built.subject!r})"
        forged = dataclasses.replace(built, subject=override)
        try:
            gate.guard_email(forged, permitted=("alex@example.com",))
            return f"accepted ({forged.subject!r})"
        except Exception as exc:                                      # noqa: BLE001
            return f"refused ({type(exc).__name__}: {str(exc)[:44]}…)"

    row("care.digest_email", "subject",
        "a subject for a day this plan is not about, set after composition",
        digest_subject_case(override="Today — 2026-09-23"),
        "the render of the closed template and the plan's own day",
        digest_subject_case(override=None))

    # ---- ITEM 2, the HEADER SET itself ---------------------------------------------------------
    # Driven through `GmailClient.send_reply`, because the moment the finding is about is the
    # client's: everything above it (builder, adapters) has already had its say, and this is the
    # last line before `as_bytes()`. The attack is a header the BUILDER did not write — the shape
    # `cc`/`bcc`/`reply-to` would arrive in. The control is the same call with the same builder.
    def gmail_header_case(*, extra: tuple[str, str] | None):
        from privatemesh import gmail as gmail_mod
        from privatemesh.ports import MailMessage

        parent = MailMessage(id="m1", thread_id="t1", sender="alex@example.com",
                             to=("me@example.com",), subject="Our usual cafe has closed",
                             body_text="", received_at="", rfc822_message_id="<m1@example.com>")
        client = gmail_mod.GmailClient(account="probe")
        client._call = lambda *a, **k: {"id": "probe-1", "threadId": "t1"}   # no network
        real = gmail_mod.build_reply

        def build_reply(**kw):
            message = real(**kw)
            if extra is not None:
                message[extra[0]] = extra[1]
            return message

        gmail_mod.build_reply = build_reply
        try:
            # An EMPTY idempotency key, so `find_sent` returns without a transport call: the probe
            # is about the bytes, and a probe that needed a mailbox would be measuring the stub.
            client.send_reply(parent, ("alex@example.com",), "Re: Our usual cafe has closed",
                              "Hi Alex, 15:00-15:45 works for me.", "")
            return "SENT"
        except gmail_mod.GmailError as exc:
            return f"refused (GmailError: {str(exc)[:48]}…)"
        except Exception as exc:                                      # noqa: BLE001
            return f"refused (other: {type(exc).__name__}: {str(exc)[:40]}…)"
        finally:
            gmail_mod.build_reply = real

    row("gmail.send_reply", "header set (Cc/Bcc/Reply-To)",
        "a header the builder did not write, added after it returned",
        gmail_header_case(extra=("Cc", "dave@example.com")),
        "the headers the one builder produces, unmodified",
        gmail_header_case(extra=None))

    # ---- ITEM 2, the ENVELOPE fields the reply threads by --------------------------------------
    # `In-Reply-To` and `References` are built from the pinned message's own `Message-ID`, and the
    # port requires the value the change APPROVED to be the one the provider is serving. The attack
    # is an approved id that names a message the provider does not serve under it: the reply would
    # be filed into a conversation nobody chose, by headers that looked ratified.
    h, out = _run(Harness(events=_two_meetings()),
                  over={"rfc822_message_id": "<somewhere-else@example.com>"})
    row("mail.send_reply", "In-Reply-To + References",
        "an approved message id the provider does not serve for the pinned message",
        _verdict(h, out, kind="email_reply", exc="ApprovedArgumentsRefused"),
        "the provider's own Message-ID for the message this change is about", control)

    # ---- ITEM 2, the idempotency header --------------------------------------------------------
    # The key a retry reconciles on. Two runs of one approved change must present the same one, and
    # it is the approval's, not the caller's. Built with `_split` rather than `_run`: the bundle the
    # owner approved and the approval the broker spends are raised over DIFFERENT arguments, which
    # is the shape of the finding — `_run` builds both from one dict, and a value compared with
    # itself is not a check.
    h, out = _split(Harness(events=_two_meetings()),
                    email_over={"idempotency_key": "idem-somebody-else"})
    row("mail.send_reply", "X-PrivateMesh-Key (idempotency)",
        "an approval raised over a key the approved plan does not carry",
        _verdict(h, out, kind="email_reply", exc="ApprovedArgumentsRefused"),
        "the key on the approved bundle", control)

    # ---- ITEM 3, the calendar engine fields ----------------------------------------------------
    # One row per field, all the same question: is this the value the owner approved? The bundle is
    # approved over the harness's own `calendar_args()`; each attack raises the APPROVAL over one
    # field moved off it, which is precisely the reviewed gap — the store held the arguments the
    # owner read, the broker ran the arguments the approval was requested over, and no line
    # compared them. The control is the unmodified call, run once and shared: it is the same claim
    # for all six, and six copies of it would be six chances for one to be a different call.
    def cal_field_case(**over):
        # Built by hand rather than through `approved_changeset`, for one reason: the bundle's
        # `event_etag` has to follow the `event_id` the attack names, so the run gets PAST the
        # precondition check at `_admit` and the field binding is the thing that has to refuse. A
        # probe whose attack is caught by a different guard reports a refusal that is not about the
        # field it names — the failure mode this whole table exists to avoid.
        from privatemesh.changes import _calendar_display
        from privatemesh.execute import CALENDAR_SKILL, EMAIL_SKILL

        hx = Harness(events=_two_meetings())
        approved = hx.calendar_args()                 # what the owner read on the review screen
        attacked = hx.calendar_args(**over)           # what the approval is raised over
        seen = hx.calendar.get_event(approved["event_id"])
        mail_args = hx.email_args()
        cal = hx.approvals.request("task1", CALENDAR_SKILL, attacked)
        mail = hx.approvals.request("task1", EMAIL_SKILL, mail_args)
        cs = hx.changesets.create(change_id="c1", task_id="task1", event_etag=seen.etag,
                                  boundary_id="pc1",
                                  boundary_version=hx.boundary.version_of("pc1"),
                                  calendar_args=approved, email_args=mail_args,
                                  calendar_display=_calendar_display(seen, approved),
                                  approval_ids=(cal.id, mail.id))
        cs = hx.changesets.approve(cs.id, cs.version, current_event_etag=seen.etag,
                                   current_boundary_version=hx.boundary.version_of("pc1"))
        assert cs.status == "approved", "the fixture failed to approve, so nothing was tested"
        return hx, hx.executor.execute(cs.id)

    control_h, control_out = cal_field_case()
    written = control_h.rec.surfaces("calendar")
    cal_control = (f"written ({written[0]['payload']['location']!r})" if written
                   else _verdict(control_h, control_out, kind="calendar_update", exc="?"))

    def cal_row(field, attack_text, *, exc="ApprovedArgumentsRefused", **over):
        hx, out = cal_field_case(**over)
        row("calendar.update_event", field, attack_text,
            _verdict(hx, out, kind="calendar_update", exc=exc),
            "the value on the approved bundle", cal_control)

    cal_row("start", "a slot the owner did not approve", start="2026-09-19T16:00:00+01:00")
    cal_row("end", "an end the owner did not approve", end="2026-09-19T16:30:00+01:00")
    cal_row("send_updates", "a notification setting the owner did not approve",
            send_updates="none")
    cal_row("if_match_etag", "a precondition the owner did not approve", if_match_etag="etag-9")
    # `event_id` moved to ANOTHER MEETING OF THE DAY — the dangerous direction, because that meeting
    # exists and its invite would move. The etag is moved with it so the write reaches the port.
    cal_row("event_id", "another meeting of the day", event_id="evt-standup")
    # `change_id` is the field the binding is KEYED on. With it stripped, the port cannot read the
    # approved plan at all, and "could not check" must not read as "checked".
    cal_row("change_id", "no change at all, so there is no approved plan to compare against",
            exc="CalendarRefused", change_id="")
    # ---- ITEM 4, the zone the approved instant is read in (round eight) -------------------------
    # The adapter used to add `start.timeZone`/`end.timeZone` itself, from the event it read at the
    # moment of the call — so the one field that tells the provider how to READ `start` was the one
    # field no approval had named. It is on the bundle now and compared like the times above.
    cal_row("timezone", "a zone the owner never approved, on an otherwise identical write",
            timezone="America/New_York")

    # ---- ITEM 4, the venue's two fail-open paths ------------------------------------------------
    # `location` already has a row above, for a place the research never returned. These two are the
    # paths the seventh review named as PARTIAL: a change with NO catalogue and a change with no
    # selected RECORD were both reported clean, because "there is nothing to compare against" was
    # answered as "the value was checked".
    from test_changes import harness as engine_harness

    def venue_case(*, catalogue: bool, venue_id: bool):
        hx = engine_harness()
        change = hx.engine.prepare(hx.engine.detect(None)[0].id)
        assert change.venue_id, "the prepared change carries no venue selection"
        if not catalogue:
            change.venue_catalogue = []
        if not venue_id:
            change.venue_id = ""
        hx.change_resolver.registered["c1"] = change
        cal = hx.calendar_args(location="The Standard, King's Cross")
        cal["change_id"] = "c1"
        mail = hx.email_args(body=" ".join([change.reply_template]
                                           + [f["text"] for f in change.reply_fragments]))
        mail["change_id"] = "c1"
        cs = hx.approved_changeset(calendar_args=cal, email_args=mail)
        assert cs.status == "approved", "the fixture failed to approve, so nothing was tested"
        written = hx.calendar.events.get("ev1")
        outcome = hx.executor.execute(cs.id)
        return _verdict(hx, outcome, kind="calendar_update", exc="CalendarRefused"), written

    no_cat, _ = venue_case(catalogue=False, venue_id=True)
    no_id, _ = venue_case(catalogue=True, venue_id=False)
    row("calendar.update_event", "location (catalogue missing)",
        "a location on a change that records no venue catalogue",
        no_cat, "the control row above (the change's own catalogue record)", cal_control)
    row("calendar.update_event", "location (venue id missing)",
        "a location on a change that names no venue record",
        no_id, "the control row above (the change's own catalogue record)", cal_control)

    # ---- ITEM 4, the digest line's provenance ---------------------------------------------------
    # The row above proves the id must NAME a record. This one proves the words must BE that
    # record's render — a known id is a citation, not evidence, and the seventh review's finding is
    # that `DigestLine(record_id="known", text="anything")` composed cleanly.
    row("compose_digest → mail", "digest line text vs its record's render",
        "a line naming a real record, carrying a sentence that record did not produce",
        digest([DigestLine(source="care_coming", record_id="ev1",
                           text="Standup moved to 09:15")], {"ev1": "Standup at 09:15"}),
        "the record's own render",
        digest([DigestLine(source="care_coming", record_id="ev1", text="Standup at 09:15")],
               {"ev1": "Standup at 09:15"}))

    # ============================== round eight: the origin, the version, and two fields ==========

    # ---- ITEM 1, the PLAN itself -----------------------------------------------------------------
    # Not a field and not a value: the object every digest field comes from. The reviewed door is
    # that `Executor.send_digest` took a caller-built `DigestPlan` and sent it, so no check on the
    # plan's CONTENT could ever have refused it — the probe's pieces were well-formed, its sources
    # allowlisted, its body the exact render of its own declared fragment. The question is origin,
    # and the row is written so that only origin can answer it: the attack and the control carry the
    # SAME text through the SAME function.
    def digest_plan_case(*, forged: bool):
        from privatemesh.care import (CareCircleStore, DigestLedger, DigestPlan,
                                      DigestPlanForged)
        from privatemesh.sim import SAMPLE_DATE

        hx = Harness(events=_two_meetings())
        circle = CareCircleStore()
        circle.add(name="Alex", email="alex@example.com", may_receive=["digest"], by="owner")
        hx.executor.care_circle = circle
        hx.executor.digests = DigestLedger()
        plan = (DigestPlan(day=SAMPLE_DATE, recipients=("alex@example.com",),
                           fragments=(Fragment("care_coming", "OWNER NEVER APPROVED THIS"),))
                if forged else
                hx.executor.build_digest(SAMPLE_DATE, now="2026-09-20T09:00:00+01:00"))
        try:
            hx.executor.send_digest(plan)
            return f"SENT ({hx.mail.sent[-1]['body'][:38]!r})"
        except DigestPlanForged as exc:
            return f"refused (DigestPlanForged: {str(exc)[:40]}…)"
        except Exception as exc:                                      # noqa: BLE001
            return f"refused (other: {type(exc).__name__}: {str(exc)[:34]}…)"

    row("Executor.send_digest", "the plan itself (origin)",
        "a DigestPlan assembled by hand, carrying text no record rendered",
        digest_plan_case(forged=True),
        "the plan this engine built from its own calendar and change records",
        digest_plan_case(forged=False))

    # ---- ITEM 3, the audience's FULL representation ----------------------------------------------
    # The bare address was bound and the representation was not, so an approved `alex@example.com`
    # and a called `Finance <alex@example.com>` were the same to every check on this path — and the
    # second is what `build_reply` writes into `To`, because it serialises the string it is handed.
    hx, out = _split(Harness(events=_two_meetings()),
                     email_over={"to": ["Finance <alex@example.com>"]})
    row("mail.send_reply", "to (display name)",
        "an approval raised over a display name the approved audience does not carry",
        _verdict(hx, out, kind="email_reply", exc="ApprovedArgumentsRefused"),
        "the correspondent's address exactly as the approved bundle spells it", control)

    # ---- ITEM 4, the ZONE the approved instant is read in ----------------------------------------
    # Row lives with the other calendar engine fields, above, where it is compared against the same
    # control as `start` and `end` — the zone and the times it interprets are one claim.

    # ---- ITEM 2, the VERSION the fields were bound to --------------------------------------------
    # The binding was correct and the CALL was not checked: `edit` mints version n+1 without
    # invalidating an approval that has already been spent, so a revision landing between the bind
    # and the provider call left v1's bytes leaving under an approval the owner had replaced. Both
    # legs run the same approved bundle; the attack's store answers v2 at the port's second read.
    class _Revises:
        def __init__(self, store, revise, *, on):
            self._store, self._revise, self._on, self.reads = store, revise, on, 0

        def for_change(self, change_id):
            self.reads += 1
            if self.reads == self._on:
                self._revise()
            return self._store.for_change(change_id)

    def revision_case(*, revise: bool):
        from test_care_review_r8 import _approved, _revise_once

        hx = Harness(events=_two_meetings())
        cs = _approved(hx)
        if revise:
            hx.change_resolver.bundles = _Revises(hx.changesets,
                                                  lambda: _revise_once(hx, cs), on=2)
        outcome = hx.executor.execute(cs.id)
        written = hx.rec.surfaces("calendar")
        if written:
            return f"written ({written[0]['payload']['location']!r})"
        return _verdict(hx, outcome, kind="calendar_update", exc="ApprovedArgumentsRefused")

    row("calendar.update_event", "the bound VERSION",
        "a revision to v2 landing between the port's binding and its call",
        revision_case(revise=True),
        "the same approved bundle with nobody editing it",
        revision_case(revise=False))

    return rows


def doors_table() -> int:
    rows = door_rows()
    width = max(len(r["field"]) for r in rows)
    print(f"{'port':<24} {'field':<{width}}  attack → result")
    for r in rows:
        print(f"{r['port']:<24} {r['field']:<{width}}  {r['attack']}")
        print(f"{'':<24} {'':<{width}}  → {r['attack_result']}")
        print(f"{'':<24} {'':<{width}}  control: {r['control']}")
        print(f"{'':<24} {'':<{width}}  → {r['control_result']}")
    open_doors = [r for r in rows if "SENT" in r["attack_result"]
                  or "written" in r["attack_result"]]
    print(f"\n{len(rows)} outbound field(s) probed; "
          f"{'ALL REFUSED' if not open_doors else 'OPEN: ' + str(open_doors)}")
    return 1 if open_doors else 0


def main() -> int:
    if "--doors" in sys.argv:
        return doors_table()
    cat = catalogue()
    rows = []
    for text, minutes in PROBES:
        # BODY: can this sentence become a body byte? Asked under an allowlisted source, which is
        # the most favourable position a draft can put it in.
        for source in ("template", "time", "venue", "opening_hours"):
            kept, ineligible = eligible_reply_fragments(
                [Fragment(source=source, text=f"Hi Alex, {text}.")], cat, pinned="ev1")
            if kept:
                body = f"ELIGIBLE under {source!r}"
                break
        else:
            body = "not eligible under any allowlisted source"
        # READER: the second net.
        read = time_of_day_mentions(f"Hi Alex, {text}.")
        row = {"spelling": text, "names": f"{minutes // 60:02d}:{minutes % 60:02d}",
               "body": body, "reader": sorted(f"{m // 60:02d}:{m % 60:02d}" for m in read)}
        row.update(end_to_end(text) if "--end-to-end" in sys.argv else {})
        rows.append(row)
    print(json.dumps(rows, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
