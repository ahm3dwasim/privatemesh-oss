"""Round 5: the boundary stops being the matcher.

The fourth review's finding was structural rather than a missed spelling. The clock check worked
by recognising another meeting's time in a sentence, so every spelling the recogniser did not
enumerate walked past it — `09:15-09:45`, `fifteen past eleven`, `in fifteen minutes`,
`quarter after nine pm`, `twelve fifteen pm`, `9h15`, `neuf heures quinze`, `九点十五分`, base64 and
full-width digits. A matcher that enumerates spellings loses that race by construction: the
attacker picks the spelling and the defender has to have thought of it first.

So the boundary moved. A reply body may contain only sentences the ENGINE rendered, each carrying
the meeting it derives from; a sentence the engine did not render is not scanned and allowed, it is
INELIGIBLE. The string matchers stay underneath as the second net.

Two smaller findings are fixed in the same round and tested here: the calendar guard failed OPEN on
a malformed start (the day's window became empty, the foreign set became empty, and the check
passed vacuously while `Room 2, Town Hall` from another meeting was written to the invite), and
`send_reply` skipped its foreign check entirely for an approved bundle with no event pin.
"""
from __future__ import annotations

import json

import pytest

from privatemesh import boundary
from privatemesh.decisions import RULE_OWNER_REVISION
from privatemesh.boundary import (ALLOWED_FRAGMENT_SOURCES, CalendarDayUnknown, Fragment,
                                  eligible_reply_fragments, reply_catalogue)

from fakes_v2 import Harness, at, invite


# --------------------------------------------------------------- composition by construction ----
def _catalogue(pinned: str = "ev1"):
    return reply_catalogue(pinned=pinned, greeting="Hi Alex,", recipient_name="Alex Doe",
                           slot_label="15:00-15:45", venue_name="The Standard",
                           venue_address="King's Cross", owner_name="Sam")


def test_a_sentence_the_engine_did_not_render_is_not_eligible():
    """The whole of item 1 in one assertion: the draft's words do not survive the lookup."""
    catalogue = _catalogue()
    kept, ineligible = eligible_reply_fragments(
        [Fragment(source="time", text="15:00-15:45 works for me.")], catalogue, pinned="ev1")
    assert [f.text for f in kept] == ["15:00-15:45 works for me."]

    # The reviewer's own escape, written as a draft fragment claiming an allowlisted source. Under
    # the old boundary this was SCANNED — and `fifteen past eleven` was one of the spellings the
    # scan did not know. Under this one it is never a candidate for a body byte.
    for sentence in ("Hi Alex, fifteen past eleven.",
                     "Hi Alex, Client review at 11:15.",
                     "Hi Alex, the standup runs 09:15-09:45.",
                     "Hi Alex, 9h15.",
                     "Hi Alex, neuf heures quinze.",
                     "Hi Alex, 九点十五分."):
        kept, ineligible = eligible_reply_fragments(
            [Fragment(source="template", text=sentence)], catalogue, pinned="ev1")
        assert kept == [], f"{sentence!r} was eligible to become a body byte"
        assert [f.text for f in ineligible] == [sentence]


def test_the_catalogue_itself_never_carries_another_meetings_details():
    """Provenance is what makes the lookup safe: the catalogue is built from ONE change's plan.

    Nothing about a second meeting is in it, so there is no entry for a draft to reach for — which
    is why the check no longer has to recognise a spelling to refuse it.
    """
    catalogue = _catalogue()
    assert catalogue, "an empty catalogue would make every assertion below vacuous"
    for piece in catalogue:
        assert piece.source in ALLOWED_FRAGMENT_SOURCES, \
            f"the engine rendered a sentence under an unlisted source: {piece.source}"
        assert piece.event_id in ("", "ev1"), \
            "a catalogue piece derived from a meeting that is not the pinned one"


def test_a_piece_derived_from_another_meeting_is_ineligible_whatever_it_says():
    """The provenance rule, isolated from the text rule.

    A piece whose text IS in the catalogue but whose event id is not the pinned one is refused. This
    is the branch a matcher could not have: nothing about the sentence is wrong, and it still does
    not belong in this reply, because the reply is about one meeting.
    """
    catalogue = _catalogue(pinned="ev1") + (
        Fragment(source="time", text="11:15-11:45 works for me.", event_id="ev-other"),)
    kept, ineligible = eligible_reply_fragments(
        [Fragment(source="time", text="11:15-11:45 works for me.")], catalogue, pinned="ev1")
    assert kept == []
    assert [f.text for f in ineligible] == ["11:15-11:45 works for me."]


# --------------------------------------------------- the two fail-closed ports from the review ----
def _invite(event_id: str, location: str) -> object:
    from privatemesh.ports import CalendarEvent
    return CalendarEvent(id=event_id, etag=f"etag-{event_id}", summary="Someone else",
                         start=at("11:15"), end=at("11:45"), timezone="Europe/London",
                         location=location, attendees=("other@example.com",))


def test_the_composed_body_is_the_engine_sentence_or_it_is_empty():
    """The composition boundary, at the composer rather than at the lookup.

    The draft asks for a greeting the engine never rendered — one space more than the engine's own
    `Hi Alex,`. There is no normalisation to hide in: the body is the catalogue's bytes or it is
    nothing, and the caller is told which pieces were ineligible.
    """
    from privatemesh.audit import AuditLog
    from privatemesh.boundary import BoundaryStore, PrivacyGate
    catalogue = _catalogue()

    class Gate(PrivacyGate):
        """The real gate, with the one method this test wants to see recorded."""
        def __init__(self) -> None:
            super().__init__(BoundaryStore(), audit=AuditLog(":memory:"))
            self.ineligible: list[dict] = []

        def note_ineligible_fragment(self, **kw):
            self.ineligible.append(kw)
            return super().note_ineligible_fragment(**kw)

    gate = Gate()
    email = boundary.compose_email(
        to=("alex@example.com",), subject_template="Re:",
        subject_fragments=(Fragment(source="thread_subject", text="x"),),
        fragments=(Fragment(source="time", text="15:00-15:45 works for me."),),
        template_body="Hi  Alex,", template_source="template",
        pinned_event="ev1", catalogue=catalogue, gate=gate, change_id="c1")
    # The engine's sentence renders; the draft's near-miss greeting does not.
    assert email.body == "15:00-15:45 works for me."
    assert email.template_body == ""
    assert [f.text for f in email.fragments] == ["15:00-15:45 works for me."]
    assert [c["source"] for c in gate.ineligible] == ["template"]


def test_a_reply_from_a_draft_that_wrote_its_own_prose_still_goes_out():
    """The move is not cancelled by a draft that reached for prose the engine never rendered.

    The draft here writes `fifteen past eleven` under an allowlisted source — the reviewer's own
    escape, the one the round-4 matcher did not know. Under the old boundary that sentence was
    SCANNED, and a scan only refuses the spellings it enumerates. Under this one it is not a
    candidate for a body byte at all, so the reply that goes is the ENGINE's and the record says so.
    A meeting the owner asked to move is not lost because the model could not compose.
    """
    from test_changes import harness

    h = harness(script={"draft": {"subject": "Re: Our usual cafe has closed",
                                  "template_body": "Hi Alex, fifteen past eleven.",
                                  "fragments": [{"source": "time",
                                                 "text": "fifteen past eleven works for me."}]}})
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error
    assert change.reply_defaulted, "the engine did not fall back to its own rendering"

    # Every stored piece is the engine's own rendering. The only writer is the composer, and the
    # composer only ever returns catalogue objects — so the record cannot hold a sentence that was
    # never rendered, which is the property this round is for.
    catalogue = reply_catalogue(pinned=change.event_pinned, greeting="Hi Alex,",
                                recipient_name="Alex Doe", slot_label="15:00-15:45",
                                venue_name="The Standard", venue_address="King's Cross",
                                owner_name="Sam")
    allowed = {(p.source, p.text) for p in catalogue}
    assert change.reply_fragments, "the fallback composed nothing"
    for piece in change.reply_fragments:
        assert (piece["source"], piece["text"]) in allowed, \
            f"the stored reply holds a sentence the engine never rendered: {piece}"
    assert ("template", change.reply_template) in allowed
    assert "fifteen past eleven" not in json.dumps(change.reply_fragments), \
        "the draft's own sentence survived into the stored reply"
    # And nothing about the other meeting left the machine on the way.
    assert "fifteen past eleven" not in h.rec.text()


def test_a_calendar_start_that_cannot_be_placed_is_refused_and_nothing_is_written():
    """The calendar guard failed OPEN: `window = ()` made the foreign set empty and the check
    vacuous, so a foreign `Room 2, Town Hall` reached the provider.

    The malformed bundle from the review, re-run against the closed guard. The start names no day
    anything can place, so the day's other meetings cannot be listed — which is a reason to refuse,
    not a reason to write.
    """
    from privatemesh.execute import FAILED

    h = Harness(events=[invite(), _invite("ev2", "Room 2, Town Hall")])
    cs = h.approved_changeset(calendar_args=h.calendar_args(start="tomorrow afternoon",
                                                            end="whenever"))
    outcome = h.executor.execute(cs.id)
    assert outcome.effects[0].state == FAILED
    assert "cannot be placed on a day" in (outcome.effects[0].detail or "")
    # Nothing reached the provider, and the invite is exactly as it was.
    assert h.rec.surfaces("calendar") == []
    assert h.calendar.get_event("ev1").location == "Old Cafe, King's Cross"
    # The refusal is on the change's spine, where the owner reads failures. It arrives as the
    # executor's effect row rather than as a `RULE_CALENDAR_NOTE` row, because this tool's
    # arguments carry no `change_id` — the calendar half of a bundle is written without one, and a
    # note filed from inside the skill could only land in the orphans list. The row's DETAIL is
    # withheld here because this harness's log is built without a gate, which is `_scrub`'s
    # documented fail-closed state and not something this test is about; subject and verdict are
    # the two fields the owner reads either way.
    assert [d["subject"] for d in h.decisions.for_change("c1") if d["verdict"] == "refused"] \
        == ["The invite: failed"]
    assert not h.decisions.orphans(), "the refusal landed where the owner does not read"


def test_a_reply_that_cannot_name_its_meeting_is_refused_at_the_port():
    """`send_reply` skipped its foreign check entirely for an approved bundle with no event pin.

    A producer that writes no pin does not exist — `_prepare` is the only writer — so the honest
    answer to a bundle without one is a refusal, not a silent pass. This drives the same approved
    bundle the engine builds and removes the pin underneath it.
    """
    from privatemesh.execute import DONE, FAILED

    h = Harness()
    change = h.change_resolver.get("c1")
    assert change is not None and change.event_pinned, "the fixture change carries no pin"
    change.event_pinned = ""
    outcome = h.executor.execute(h.approved_changeset().id)
    assert outcome.effects[0].state == DONE, "the calendar half is unaffected"
    assert outcome.effects[1].state == FAILED
    assert "does not name the meeting it is about" in (outcome.effects[1].detail or "")
    assert not h.mail.sent


#: The reviewer's own escape list, each with the minute of the day it names. `tools/r5_probes.py`
#: runs this same list through the engine end to end; this keeps the property it measures from
#: rotting, because a probe script nobody runs is not a control.
REVIEWER_ESCAPES = [
    ("09:15-09:45", 9 * 60 + 15),
    ("fifteen past eleven", 11 * 60 + 15),
    ("in fifteen minutes", 11 * 60 + 15),
    ("quarter after nine pm", 21 * 60 + 15),
    ("twelve fifteen pm", 12 * 60 + 15),
    ("9h15", 9 * 60 + 15),
    ("neuf heures quinze", 9 * 60 + 15),
    ("九点十五分", 9 * 60 + 15),
    ("MToxNSBhbQ==", 11 * 60 + 15),          # base64 of "11:15 am"
    ("１１：１５", 11 * 60 + 15),              # full-width digits
]


@pytest.mark.parametrize("text,minutes", REVIEWER_ESCAPES)
def test_every_spelling_the_review_escaped_with_cannot_become_a_body_byte(text, minutes):
    """Item 1 against the reviewer's list, one spelling at a time.

    Each is offered under EVERY allowlisted source, which is the most favourable position a draft
    can put it in — the sentence is what the draft wants to say, and the source label is the one it
    claims. None of them is eligible, and the reason is not that the reader recognises the time:
    most of these the reader does not read at all (see `tools/r5_probes.py`, and item 25 of the
    threat model for where that still matters). They are ineligible because the ENGINE never
    rendered them, which is a fact about provenance and not about spelling.
    """
    catalogue = _catalogue()
    assert minutes, "the fixture must name a time"
    for source in sorted(ALLOWED_FRAGMENT_SOURCES):
        kept, ineligible = eligible_reply_fragments(
            [Fragment(source=source, text=f"Hi Alex, {text}.")], catalogue, pinned="ev1")
        assert kept == [], f"{text!r} became a body byte under source {source!r}"
        assert [f.text for f in ineligible] == [f"Hi Alex, {text}."]


# ------------------------------------------------- the owner's own words, item 2 ------------------
def _revision_harness():
    """The canonical flow harness with a SECOND meeting on the same day, so a revision has
    something foreign to name. `_invite` puts it at 11:15, which is the time the reviewer's
    spellings all name."""
    from test_changes import harness
    return harness(events=[invite(), _invite("ev2", "Room 2, Town Hall")])


def test_an_owners_revision_that_names_another_meeting_is_shown_and_kept():
    """Item 2: the owner's words are the owner's decision, and they are not laundered.

    Both wrong answers are refused here. The revision is not REFUSED — it is the owner speaking,
    and a system that overrules the person it works for is not the product. It is not silently
    KEPT either: the plan carries the mention, and the decision log records it, both BEFORE the
    owner approves. What the owner does about it is then the owner's business.
    """
    h = _revision_harness()
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error

    revised = h.engine.revise(change.id, reply_body="Hi Alex, 11:15-11:45 it is — see you then.")
    assert revised.stage == "proposal_ready", revised.error
    # Kept, verbatim: the owner's sentence is the body, not a rewrite of it.
    assert revised.reply_template == "Hi Alex, 11:15-11:45 it is — see you then."
    # And said out loud, on the record the review screen reads.
    assert revised.owner_revision_mentions, "the mention was laundered into the plan"
    assert revised.owner_revision_mentions[0]["event"] == "ev2"
    assert revised.owner_revision_mentions[0]["how"] in ("exact", "rewritten")
    served = h.engine.change_dict(change.id)
    assert served["owner_revision_mentions"] == revised.owner_revision_mentions, \
        "the mention is on the record but not on the screen the owner approves from"
    rows = [d for d in h.engine.decisions_for(change.id)
            if d["rule"] == RULE_OWNER_REVISION]
    assert [d["verdict"] for d in rows] == ["allowed"], rows
    # The plan is still a plan the owner can RUN — the half that was missing, because the record
    # still described the PREVIOUS reply and clause 4 read that as smuggled text, and because the
    # edit dropped the guest list the approval has to name.
    cs = h.changesets.for_change(change.id)
    assert h.engine._clause_four(revised, cs.email.arguments)[0], \
        h.engine._clause_four(revised, cs.email.arguments)[1]

    # And then it is REFUSED, at the port, loudly. This is the decision the brief asks to be argued
    # rather than guessed: the send path cannot tell an owner's sentence from a draft's, and a port
    # that trusted a flag saying "the owner wrote this" would be a port a compromised planner could
    # point at any sentence at all. So the refusal stays where the boundary is, and the owner is told
    # at BOTH moments instead of being overruled at either — here, while the words are still theirs
    # to change, and again when it runs. Nothing about it is silent, which is what item 2 asked for.
    from privatemesh.execute import DONE, FAILED
    approved = h.changesets.approve(cs.id, cs.version,
                                    current_event_etag=h.calendar.get_event("ev1").etag,
                                    current_boundary_version=h.boundary.version_of("pc1"))
    outcome = h.executor.execute(approved.id)
    assert [e.state for e in outcome.effects] == [DONE, FAILED]
    assert "describes ev2" in (outcome.effects[1].detail or "")
    assert not h.mail.sent, "the reply left with another meeting in it"


def test_an_owners_revision_that_names_nothing_else_is_not_flagged():
    """The other half of the same rule, and the one that keeps it from being noise.

    A check that fires on every ordinary revision is a check the owner learns to ignore, and this
    one has to still mean something when it fires.
    """
    h = _revision_harness()
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    revised = h.engine.revise(change.id, reply_body="Hi Alex, 15:00-15:45 it is — see you then.")
    assert revised.stage == "proposal_ready", revised.error
    assert revised.reply_template == "Hi Alex, 15:00-15:45 it is — see you then."
    assert revised.owner_revision_mentions == [], \
        "the owner's own meeting was reported as somebody else's"
    assert not [d for d in h.engine.decisions_for(change.id)
                if d["rule"] == RULE_OWNER_REVISION]


def test_a_body_that_is_not_the_render_of_its_fragments_is_refused():
    """The final-bytes equality check, on the one door `compose_email` is not.

    `OutboundEmail` is a dataclass, so a caller can build one directly and hand it a body that is
    not the render of its own fragments. `compose_email` cannot produce that state; this can. The
    check is the last place the bytes are compared against what was declared, which is why it is a
    control in `redteam/mutcheck_findings.py` rather than a comment: a claim about the bytes is
    only worth what the line that reads them is worth.
    """
    from privatemesh.audit import AuditLog
    from privatemesh.boundary import BoundaryStore, OutboundEmail, PrivacyGate, SchemaLeak

    gate = PrivacyGate(BoundaryStore(), audit=AuditLog(":memory:"))
    smuggled = OutboundEmail(
        to=("alex@example.com",), subject="Re: x",
        subject_template="Re:",
        subject_fragments=(Fragment(source="thread_subject", text="x"),),
        body="15:00-15:45 works for me. And the client review at 11:15 is a problem.",
        fragments=(Fragment(source="time", text="15:00-15:45 works for me."),))
    assert smuggled.body != smuggled.rendered(), "the fixture is not actually a mismatch"
    with pytest.raises(SchemaLeak, match="not the render of its allowlisted fragments"):
        gate.guard_email(smuggled, change_id="c1", permitted=("alex@example.com",))

    # And the same object with the body it declares is allowed through, so the refusal above is
    # the check firing and not the fixture being refused for some other reason.
    declared = OutboundEmail(to=("alex@example.com",), subject="Re: x",
                             subject_template="Re:",
                             subject_fragments=(Fragment(source="thread_subject", text="x"),),
                             body="15:00-15:45 works for me.",
                             fragments=(Fragment(source="time", text="15:00-15:45 works for me."),))
    gate.guard_email(declared, change_id="c1", permitted=("alex@example.com",))
