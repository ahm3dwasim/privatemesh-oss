"""Finding 1: the recipient pin is bound to the approved change, not to whoever holds the record.

The review's first finding, verbatim: `_senders_of()` returned the sender of any message in the
provider's thread whose internal `id` equalled `in_reply_to`, without checking that the message
belongs to the approved change, that `thread_id` is the original thread, that the message is inbound
rather than assistant-sent, or that the sender is a syntactically valid address. Its conclusion is
the sentence this file exists to falsify: "the claimed 'verified thread sender' invariant is
therefore reduced to 'whoever currently occupies that provider record.'"

Every test below comes in the pair the corpus uses everywhere else:

* **the fix** — the binding holds only when the provider's own copy corroborates the change;
* **the mutation control** — the OLD resolution is restored under `monkeypatch`, and the same
  fixture is required to send the reply to the attacker. A guard that has only ever seen an honest
  provider has not been shown to be a guard, and the point of finding 1 is that the old code failed
  on exactly this input.
"""
from __future__ import annotations

import pytest

from privatemesh import execute as execute_mod
from privatemesh.execute import _permitted_recipients, _valid_address
from privatemesh.ports import MailMessage

from fakes_v2 import Harness, at

ALEX = "alex@example.com"
MALLORY = "mallory@evil.test"


class _Change:
    """The fields the binding reads, and nothing else — so a test can vary one at a time.

    `sender_pinned` is what the binding compares against, and it is a DIFFERENT field from
    `from_address` on purpose. The second review found that comparing the provider's sender to
    `from_address` was a comparison whose expected value came from the same mutable surface as the
    value being compared: the provider rewrote its record, and the check agreed with the rewrite.
    The pin is captured and validated at detection and is never read back from a provider.

    Both fields are here, and they are allowed to differ, because that difference is the finding:
    `test_a_pin_recorded_by_this_change_beats_a_record_the_provider_rewrote` is the test that could
    not have been written against the reviewed code."""

    def __init__(self, *, message_id: str = "m1", thread_id: str = "t1",
                 from_address: str = ALEX, sender_pinned: str = ALEX) -> None:
        self.id = "c1"
        self.message_id = message_id
        self.thread_id = thread_id
        self.from_address = from_address
        self.sender_pinned = sender_pinned


class _Mail:
    def __init__(self, thread, *, raises: Exception | None = None) -> None:
        self._thread = thread
        self._raises = raises
        self.asked: list[str] = []

    def get_thread(self, thread_id: str):
        self.asked.append(thread_id)
        if self._raises is not None:
            raise self._raises
        return list(self._thread)


def _message(mid: str = "m1", *, thread_id: str = "t1", sender: str = f"Alex Doe <{ALEX}>"):
    return MailMessage(id=mid, thread_id=thread_id, sender=sender, to=("me@example.com",),
                       subject="Our usual cafe has closed", body_text="...", received_at=at("08:05"))


# ============================================================== the binding holds when it should
def test_the_pin_names_the_person_the_change_was_raised_from():
    """The positive case, so the rest of this file is a set of differences from something."""
    permitted, why, _about = _permitted_recipients(_Mail([_message()]), _Change(),
                                           thread_id="t1", message_id="m1")

    assert permitted == {ALEX}
    assert ALEX in why, "the reason does not say who the reply goes to"


# ============================================================== the review's two named attacks
def test_a_message_planted_in_the_thread_under_the_referenced_id_permits_nobody():
    """The review's attack, first form: "an attacker-controlled ... message with the referenced ID".

    The thread is the real one and the id is the real one. Only the author differs — and under the
    reviewed code the author was the answer."""
    planted = _message(sender=f"Mallory <{MALLORY}>")
    permitted, why, _about = _permitted_recipients(_Mail([planted]), _Change(),
                                           thread_id="t1", message_id="m1")

    assert permitted == set(), "an attacker planted in the thread became the audience"
    assert "was not written by" in why


def test_a_message_this_assistant_sent_permits_nobody():
    """The review's attack, second form: "an assistant-authored message with the referenced ID".

    A reply this product sent lives in the same thread — that is what threading means — so the
    thread alone cannot tell the correspondent from the assistant. The change can: it was raised
    from an INBOUND message, so its `from_address` is the correspondent, and a message from the
    owner's own mailbox is not that. This is the "sent BY someone other than the owner" clause,
    enforced through the one identity the build actually stores."""
    ours = _message(sender="Sam Owner <owner@example.com>")
    permitted, why, _about = _permitted_recipients(_Mail([ours]), _Change(),
                                           thread_id="t1", message_id="m1")

    assert permitted == set(), "a message the assistant sent became the audience"
    assert "was not written by" in why


def test_the_pinned_correspondent_beats_a_record_the_provider_rewrote():
    """The second review's Medium finding, and the test the reviewed code could not have passed.

    `from_address` says Mallory and `sender_pinned` says Alex — the state a change is in after
    somebody edits the provider record it was raised from. The reviewed check compared the
    provider's sender to `from_address`, so with both sides reading Mallory it AGREED and let the
    reply out. The pin is not a field the provider can reach, so the same input is refused."""
    tampered = _Change(from_address=MALLORY, sender_pinned=ALEX)
    permitted, why, _about = _permitted_recipients(_Mail([_message()]), tampered,
                                           thread_id="t1", message_id="m1")

    assert permitted == {ALEX}, "the pin was ignored in favour of the provider's own record"
    assert ALEX in why

    # And the mirror: a provider serving Mallory cannot claim to be the pinned correspondent.
    permitted, why, _about = _permitted_recipients(_Mail([_message(sender=f"Mallory <{MALLORY}>")]),
                                           _Change(), thread_id="t1", message_id="m1")
    assert permitted == set()
    assert "was not written by" in why


# ============================================================== the other three bindings
@pytest.mark.parametrize("thread_id,message_id,expected", [
    ("t2", "m1", "the reply names a different thread"),
    ("t1", "m9", "the reply names a different message"),
])
def test_a_reply_that_names_something_other_than_this_change_permits_nobody(
        thread_id, message_id, expected):
    """`change.message_id` and `change.thread_id` are the bindings, and each fails on its own. The
    reviewed code checked neither: any thread, any message, as long as an id matched."""
    permitted, why, _about = _permitted_recipients(_Mail([_message()]), _Change(),
                                           thread_id=thread_id, message_id=message_id)

    assert permitted == set()
    assert expected in why


def test_a_foreign_record_served_under_the_right_id_permits_nobody():
    """The provider returns a message carrying this change's id, but belonging to another thread.

    Real providers do not usually do this; a compromised or confused one does, and the whole
    finding is that the pin rested on the provider's word. `id` matching is not identity."""
    foreign = _message(thread_id="t-other")
    permitted, why, _about = _permitted_recipients(_Mail([foreign]), _Change(),
                                           thread_id="t1", message_id="m1")

    assert permitted == set()
    assert "different thread" in why


def test_a_change_the_build_cannot_read_permits_nobody():
    """No change, no audience. The resolver answers None for an id it does not know, and a reply
    whose change cannot be read is not a reply anybody has established an audience for."""
    permitted, why, _about = _permitted_recipients(_Mail([_message()]), None,
                                           thread_id="t1", message_id="m1")

    assert permitted == set()
    assert "could not be read" in why


def test_a_thread_that_cannot_be_read_permits_nobody():
    """The port raising is not permission. This was already true before the review and is kept
    because it is the same invariant: an unreadable audience is not an audience."""
    permitted, why, _about = _permitted_recipients(_Mail([], raises=RuntimeError("provider down")),
                                           _Change(), thread_id="t1", message_id="m1")

    assert permitted == set()
    assert "could not be read" in why


def test_the_change_s_own_message_missing_from_its_thread_permits_nobody():
    """The thread reads fine and simply does not contain the message. Empty stays empty."""
    permitted, why, _about = _permitted_recipients(_Mail([_message("m-other")]), _Change(),
                                           thread_id="t1", message_id="m1")

    assert permitted == set()
    assert "not in its thread" in why


@pytest.mark.parametrize("sender", ["", "   ", "Alex Doe", "<>", "not an address", "a@b"])
def test_a_sender_that_is_not_an_address_permits_nobody(sender):
    """The fourth thing the review asked for: "the sender is non-empty and syntactically valid".

    A `From` header that is a display name, empty, or a bare word names no mailbox. Under the
    reviewed code it became the permitted recipient verbatim."""
    permitted, why, _about = _permitted_recipients(_Mail([_message(sender=sender)]), _Change(),
                                           thread_id="t1", message_id="m1")

    assert permitted == set(), f"{sender!r} was accepted as an address"
    assert "usable sender address" in why


def test_a_display_name_wrapping_a_valid_address_is_still_valid():
    """The mutation control for the check above: "Alex Doe <alex@example.com>" must keep working,
    or the four refusals above would be a check that only ever says no."""
    assert _valid_address(ALEX) is True
    permitted, _why, _about = _permitted_recipients(
        _Mail([_message(sender=f"Alex Doe <{ALEX}>")]), _Change(),
        thread_id="t1", message_id="m1")
    assert permitted == {ALEX}


# ============================================================== end to end, through the executor
def _hostile_harness():
    """The fixture with its thread rewritten so the referenced id belongs to Mallory.

    This is the review's own scenario made concrete: the plan is unchanged, it is the provider's
    thread that is not what the plan assumed."""
    return Harness(threads={"t1": [_message(sender=f"Mallory <{MALLORY}>")]})


def _plan_to(harness, address: str):
    return harness.approved_changeset(email_args={
        "to": [address], "subject": "Re: Our usual cafe has closed",
        "body": "Hi Alex, 15:00-15:45 works for me.",
        "in_reply_to": "m1", "thread_id": "t1", "change_id": "c1",
        "idempotency_key": "idem-c1-v1"})


def test_the_reply_the_review_describes_never_leaves_the_machine():
    """End to end, through the executor, the broker, the skill and the port.

    The plan says the reply goes to the address the thread names. The thread names Mallory. Under
    the reviewed code that was sufficient; here nothing may be sent at all, and the reply's effect
    must be FAILED rather than silently absent."""
    h = _hostile_harness()
    outcome = h.executor.execute(_plan_to(h, MALLORY).id)

    assert h.mail.sent == [], f"the reply went to whoever held the record: {h.mail.sent}"
    assert outcome.effects[1].state == "failed"
    # The effect carries the GATE's refusal — the last checkpoint, which is what the broker saw.
    # The binding's own reason ("not written by...") is on the decision log, which is the only
    # place it can be read back; `test_the_pin_that_fails_is_recorded_with_its_reason` asserts it.
    assert "RecipientRefused" in outcome.effects[1].detail


def test_the_old_resolution_really_did_send_that_reply(monkeypatch):
    """The mutation control for the test above, and the reason finding 1 is a High.

    The reviewed resolution is restored — any message whose id matches, its sender is the answer —
    and the SAME fixture is driven through the SAME executor. If the reply now leaves, then the
    refusal above is the fix and not the fixture failing to send for some other reason."""
    def reviewed_resolution(mail, change, *, thread_id, message_id):
        try:
            thread = mail.get_thread(thread_id or "")
        except Exception:                                            # noqa: BLE001
            return set(), "unreadable"
        for message in thread or ():
            if getattr(message, "id", "") == message_id:
                return ({execute_mod._bare_address(getattr(message, "sender", ""))}, "reviewed",
                        message)
        return set(), "not found", None

    monkeypatch.setattr(execute_mod, "_permitted_recipients", reviewed_resolution)
    h = _hostile_harness()
    outcome = h.executor.execute(_plan_to(h, MALLORY).id)

    assert [s["to"] for s in h.mail.sent] == [[MALLORY]], \
        "the reviewed resolution did not reproduce the leak; this test proves nothing as written"
    assert outcome.effects[1].state == "done"


def test_the_pin_that_fails_is_recorded_with_its_reason():
    """The brief's last clause: "anything else permits nobody AND IS RECORDED".

    The gate's row can say an address was not permitted. It cannot say why the permitted set was
    empty — which of the six bindings failed — and that is the sentence the owner needs to read."""
    h = _hostile_harness()
    from privatemesh.decisions import DecisionLog
    from privatemesh.wiring import bind_decisions

    log = DecisionLog()
    bind_decisions(log, h.gate)
    h.registry = execute_mod_skills(h, log)
    from privatemesh.broker import Broker
    from privatemesh.execute import Executor

    h.broker = Broker(h.registry, h.audit, approvals=h.approvals)
    h.executor = Executor(broker=h.broker, changesets=h.changesets, calendar=h.calendar,
                          mail=h.mail, audit=h.audit, boundary=h.boundary, decisions=log)
    h.executor.execute(_plan_to(h, MALLORY).id)

    rows = log.for_change("c1")
    assert rows, "the pin refused and nothing was recorded"
    reasons = [r for r in rows if r["verdict"] == "refused"]
    assert reasons, rows
    assert any("not written by" in (r["detail"] or "") for r in reasons), \
        f"the reason the audience was empty is not on the change: {rows}"
    assert MALLORY not in str(rows), "the log recorded the address it refused"


def execute_mod_skills(h, log):
    from privatemesh.execute import build_effect_skills
    from privatemesh.skills import SkillRegistry

    # `bundles` is the same two-step attachment `wiring._build` does: the ports are bound to the
    # APPROVED arguments, and a resolver that has not been attached answers "no bundle" so both
    # ports refuse. A skill set built without it is one whose ports cannot say what the owner said
    # yes to — which is a refusal, not a default.
    return SkillRegistry(build_effect_skills(h.calendar, h.mail, gate=h.gate, decisions=log,
                                             changes=h.change_resolver,
                                             bundles=h.change_resolver))


# ============================================================== the unattached resolver, fail closed
def test_a_resolver_with_no_engine_permits_nobody():
    """`_ChangeResolver` exists before the engine does — the send skill is built from the registry,
    and the registry is built from the skill, so the two are genuinely mutually dependent. The
    unattached state must answer "no such change", never "any change": this is the state the build
    is in for the few lines between the two, and it is the state a wiring mistake would leave it
    in permanently."""
    from privatemesh.wiring import _ChangeResolver

    resolver = _ChangeResolver()
    assert resolver.get("c1") is None

    class _Engine:
        def get(self, change_id):
            return _Change() if change_id == "c1" else None

    resolver.attach(_Engine())
    assert resolver.get("c1") is not None, "the mutation control: attaching must actually bind"
    assert resolver.get("nope") is None


def test_the_composition_root_attaches_the_resolver_to_the_engine():
    """The two-phase binding is only safe if the second phase happens. A build that forgot it would
    still pass every unit test above and refuse every reply in production — and the failure would
    look like a mail outage, not like a missing wire.

    Driven through the REAL sample composition root, and the proof is a send that works: the
    sample's own prepare → approve → execute path must put the reply in the outbox. If `_build` had
    not attached the engine, the audience would be empty and nothing would be sent."""
    from privatemesh.wiring import build_sample

    wiring = build_sample()
    change = wiring.engine.detect(None)[0]
    wiring.service.prepare(change.id)
    wiring.service.approve(change.id, wiring.changesets.for_change(change.id).version)

    assert wiring.mail.outbox, "the sample build could not send a reply it should have sent"
