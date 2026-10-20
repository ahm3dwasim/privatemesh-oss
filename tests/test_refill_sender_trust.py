"""A refill binds to an OWNER-TRUSTED sender, and auto-runs only on an AUTHENTICATED one.

The re-audit's High: the refill's placeholder was bound to the first DNS label of the `From`
address, and `From` is a header the sender writes. `x@wentworth.attacker.example` pinned and, under
`within_limits`, moved the real pharmacy placeholder; `x@vet.attacker.example` pinned an owner's
"Vet clinic" reminder the same way. Nothing in a sender's address is the owner's word, so the
binding now starts from what IS: an exact address the owner put on their care circle, and a
placeholder whose owner-written `location` names that member. Auto-execution additionally needs
the mail provider's own authentication verdict for the message (`MailMessage.sender_authenticated`)
— without it a refill from a trusted address still prepares, and waits for the owner.
"""
from __future__ import annotations

import re

import pytest

from privatemesh.care import OWNER, CareCircleStore
from privatemesh.changes import NO_FIT, READY, REFILL_KIND
from privatemesh.decisions import REFUSED, RULE_RECIPIENTS
from privatemesh.limits import ASK_ALWAYS, OFF, WITHIN_LIMITS
from privatemesh.ports import CalendarEvent, MailMessage

from fakes_v2 import Harness, at

PHARMACY_ADDRESS = "repeat@wentworth.example.com"
REFILL_SCRIPT = {"classify": {"is_meeting_change": False, "is_refill_confirmation": True}}
MODES = (ASK_ALWAYS, WITHIN_LIMITS, OFF)


def _message(sender: str, *, authenticated: bool = False, mid: str = "msg-refill") -> MailMessage:
    kw = {"sender_authenticated": True} if authenticated else {}
    return MailMessage(
        id=mid, thread_id=f"thread-{mid}", sender=sender, to=("me@example.com",),
        subject="Your repeat prescription is ready",
        body_text=("Rosa's repeat prescription is ready to collect. Reply to confirm you will "
                   "collect it this week.\n\nWentworth Pharmacy"),
        received_at=at("08:05", date="2026-09-18"), rfc822_message_id=f"<{mid}@example.com>",
        **kw)


def _placeholder(event_id: str = "evt-pharmacy", location: str = "Wentworth Pharmacy",
                 start: str = "15:00", end: str = "15:20") -> CalendarEvent:
    return CalendarEvent(id=event_id, etag=f"etag-{event_id}-1", summary=f"{location} — note",
                         start=at(start), end=at(end), timezone="Europe/London",
                         location=location, organizer_is_self=True)


def _world(message: MailMessage, events=None, *, mode: str = ASK_ALWAYS):
    h = Harness(messages=[message], threads={message.thread_id: [message]},
                events=events if events is not None else [_placeholder()], script=REFILL_SCRIPT)
    circle = CareCircleStore()
    # The owner's own list — the one channel in this product nothing but the owner can write to.
    circle.add(name="Wentworth Pharmacy", email=PHARMACY_ADDRESS, may_receive=["reply"], by=OWNER)
    circle.add(name="Rosa Almeida", email="rosa.almeida@example.com",
               may_receive=["digest", "reply"], by=OWNER)
    h.engine.care_circle = circle
    service = h.v2()
    if mode != ASK_ALWAYS:
        service.limits.set_mode(mode)
    change = h.engine.classify(message)
    assert change is not None and change.kind == REFILL_KIND
    return h, service, change


def _nothing_happened(h: Harness) -> None:
    assert h.rec.surfaces("calendar") == [], "no calendar write may happen"
    assert h.mail.sent == [], "no reply may be sent"


def _held_without_a_pin(h, service, change) -> None:
    body = service.prepare(change.id)
    record = h.engine.get(change.id)
    assert body["stage"] == NO_FIT, f"reached {body['stage']!r}, pinned {record.event_pinned!r}"
    assert record.event_pinned == "", "targeting must never fall back to an event"
    assert body["executed_by"] is None
    _nothing_happened(h)


# ------------------------------------------------------------------ the re-audit's reproductions --
@pytest.mark.parametrize("authenticated", (False, True))
@pytest.mark.parametrize("mode", MODES)
def test_a_subdomain_prefix_sender_cannot_pin_or_move_the_real_placeholder(mode, authenticated):
    """Authenticated or not: the attacker's own domain can pass DKIM for itself."""
    h, service, change = _world(
        _message("Wentworth Pharmacy <x@wentworth.attacker.example>",
                 authenticated=authenticated), mode=mode)
    _held_without_a_pin(h, service, change)


@pytest.mark.parametrize("authenticated", (False, True))
@pytest.mark.parametrize("mode", MODES)
def test_a_label_collision_sender_cannot_pin_an_owners_vet_reminder(mode, authenticated):
    vet = _placeholder("evt-vet", "Vet clinic", "16:00", "16:30")
    h, service, change = _world(_message("Vet <x@vet.attacker.example>",
                                         authenticated=authenticated),
                                events=[_placeholder(), vet], mode=mode)
    _held_without_a_pin(h, service, change)


@pytest.mark.parametrize("mode", MODES)
def test_a_display_name_spoof_cannot_pin_the_placeholder(mode):
    h, service, change = _world(_message('"Wentworth Pharmacy" <x@evil.example>'), mode=mode)
    _held_without_a_pin(h, service, change)


# ------------------------------------------------------------------ trusted, unauthenticated -----
@pytest.mark.parametrize("mode", MODES)
def test_an_exact_trusted_sender_without_authentication_evidence_waits_for_the_owner(mode):
    """The `From` address matches the owner's list exactly, but nothing the product can read says
    the provider verified it — the address alone is a string the sender typed. The proposal is
    built (so the owner can approve it in one tap), and under `within_limits` it is NOT run."""
    h, service, change = _world(_message(f"Wentworth Pharmacy <{PHARMACY_ADDRESS}>"), mode=mode)
    body = service.prepare(change.id)
    assert body["stage"] == "proposal_ready"
    assert h.engine.get(change.id).event_pinned == "evt-pharmacy"
    assert body["executed_by"] is None
    _nothing_happened(h)


def test_without_authentication_the_within_limits_refusal_is_on_the_log_and_names_why():
    h, service, change = _world(_message(f"Wentworth Pharmacy <{PHARMACY_ADDRESS}>"),
                                mode=WITHIN_LIMITS)
    service.prepare(change.id)
    review = h.engine.limit_review(change.id)
    assert not review.in_limit
    assert review.failed.id == "1"
    assert "authenticat" in review.failed.detail


# ------------------------------------------------------------------ the legitimate case ----------
def test_the_legitimate_refill_auto_runs_under_within_limits():
    h, service, change = _world(
        _message(f"Wentworth Pharmacy <{PHARMACY_ADDRESS}>", authenticated=True),
        mode=WITHIN_LIMITS)
    body = service.prepare(change.id)
    assert body["stage"] == "done" and body["executed_by"] == "limit"
    assert [m["to"] for m in h.mail.sent] == [[PHARMACY_ADDRESS]]
    calls = [e["payload"] for e in h.rec.surfaces("calendar")]
    assert len(calls) == 1 and calls[0]["event_id"] == "evt-pharmacy"


def test_the_legitimate_refill_waits_under_ask_always_and_runs_on_approval():
    h, service, change = _world(
        _message(f"Wentworth Pharmacy <{PHARMACY_ADDRESS}>", authenticated=True))
    body = service.prepare(change.id)
    assert body["stage"] == "proposal_ready" and body["executed_by"] is None
    _nothing_happened(h)
    service.approve(change.id, body["version"])
    assert [m["to"] for m in h.mail.sent] == [[PHARMACY_ADDRESS]]


def test_the_legitimate_refill_never_runs_under_off():
    h, service, change = _world(
        _message(f"Wentworth Pharmacy <{PHARMACY_ADDRESS}>", authenticated=True), mode=OFF)
    body = service.prepare(change.id)
    assert body["stage"] == "proposal_ready" and body["executed_by"] is None
    _nothing_happened(h)


# ------------------------------------------------------------------ adversarial probes -----------
@pytest.mark.parametrize("sender", [
    # Same registrable domain, different mailbox: the owner listed ONE address, not the domain.
    "Wentworth Pharmacy <marketing@wentworth.example.com>",
    # A trailing-dot FQDN is not the string the owner wrote.
    "Wentworth Pharmacy <repeat@wentworth.example.com.>",
    # The trusted address as a DISPLAY NAME, with the real mailbox elsewhere.
    '"repeat@wentworth.example.com" <x@evil.example>',
    # The trusted address as a suffix of a longer domain.
    "Wentworth Pharmacy <repeat@wentworth.example.com.attacker.example>",
    # A lookalike with a hyphen, which the old word-boundary match split on.
    "Wentworth Pharmacy <repeat@wentworth-example.com>",
])
def test_near_miss_senders_never_pin_even_when_authenticated(sender):
    h, service, change = _world(_message(sender, authenticated=True), mode=WITHIN_LIMITS)
    _held_without_a_pin(h, service, change)


def test_a_trusted_member_with_no_placeholder_naming_them_is_an_honest_no_fit():
    """Rosa is on the circle, authenticated, and the day has the PHARMACY's placeholder on it —
    which does not name her. A trusted sender is not a licence to move some other entry."""
    h, service, change = _world(
        _message("Rosa Almeida <rosa.almeida@example.com>", authenticated=True),
        mode=WITHIN_LIMITS)
    _held_without_a_pin(h, service, change)


def test_a_placeholder_whose_location_only_contains_the_name_as_a_substring_is_not_bound():
    """`Wentworth Pharmacy` inside `Wentworth Pharmacyland` is not a whole-phrase match."""
    near = _placeholder("evt-near", "Wentworth Pharmacyland", "16:00", "16:20")
    h, service, change = _world(
        _message(f"Wentworth Pharmacy <{PHARMACY_ADDRESS}>", authenticated=True),
        events=[near], mode=WITHIN_LIMITS)
    _held_without_a_pin(h, service, change)


def test_no_care_circle_means_no_trusted_sender_and_no_pin():
    message = _message(f"Wentworth Pharmacy <{PHARMACY_ADDRESS}>", authenticated=True)
    h = Harness(messages=[message], threads={message.thread_id: [message]},
                events=[_placeholder()], script=REFILL_SCRIPT)
    service = h.v2()
    service.limits.set_mode(WITHIN_LIMITS)
    change = h.engine.classify(message)
    _held_without_a_pin(h, service, change)


def test_removing_the_member_after_prepare_refuses_the_limit_run():
    """Clause 1 re-derives trust from the circle as it stands, not from the pin's snapshot."""
    h, service, change = _world(
        _message(f"Wentworth Pharmacy <{PHARMACY_ADDRESS}>", authenticated=True))
    service.prepare(change.id)
    circle = h.engine.care_circle
    member = next(m for m in circle.members() if m.address == PHARMACY_ADDRESS)
    circle.remove(member.id, by=OWNER)
    review = h.engine.limit_review(change.id)
    assert review.failed is not None and review.failed.id == "1"
    assert h.engine.get(change.id).stage == READY


# ------------------------------------------------------------------ what Gmail's verdict proves --
def _headers(*results: str) -> list[dict]:
    return [{"name": "Received", "value": "from mx"},
            *({"name": "Authentication-Results", "value": r} for r in results),
            {"name": "From", "value": f"Wentworth Pharmacy <{PHARMACY_ADDRESS}>"}]


GMAIL_PASS = ("mx.google.com;\r\n       dkim=pass header.i=@wentworth.example.com header.s=s1 "
              "header.b=abc;\r\n       spf=pass (google.com: domain of bounce@mailer.example "
              "designates 1.2.3.4 as permitted sender) smtp.mailfrom=bounce@mailer.example;\r\n"
              "       dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=wentworth.example.com")


@pytest.mark.parametrize("results, expected", [
    ((GMAIL_PASS,), True),
    # DMARC alone, exact domain.
    (("mx.google.com; dmarc=pass (p=REJECT) header.from=Wentworth.Example.com",), True),
    # DKIM `header.d` form, exact domain.
    (("mx.google.com; dkim=pass header.d=wentworth.example.com header.s=s1",), True),
    # The attacker's own domain passing for ITSELF proves nothing about the pharmacy's.
    (("mx.google.com; dkim=pass header.d=wentworth.example.com.attacker.example; "
      "dmarc=pass header.from=attacker.example",), False),
    # A parent/sub-domain is not the domain.
    (("mx.google.com; dkim=pass header.d=example.com",), False),
    (("mx.google.com; dkim=pass header.d=mail.wentworth.example.com",), False),
    # SPF checks the envelope, not `From`.
    (("mx.google.com; spf=pass smtp.mailfrom=repeat@wentworth.example.com",), False),
    (("mx.google.com; dkim=fail header.d=wentworth.example.com; dmarc=fail "
      "header.from=wentworth.example.com",), False),
    # A header the SENDER wrote, below Gmail's own failing verdict, is never read.
    (("mx.google.com; dkim=none; dmarc=fail header.from=wentworth.example.com",
      "mx.google.com; dkim=pass header.d=wentworth.example.com"), False),
    # The topmost header is somebody else's authserv-id.
    (("evil.example; dkim=pass header.d=wentworth.example.com",), False),
    ((), False),
])
def test_gmail_sender_authenticated_trusts_only_gmails_own_exact_domain_verdict(results, expected):
    from privatemesh.gmail import sender_authenticated
    assert sender_authenticated(_headers(*results),
                                f"Wentworth Pharmacy <{PHARMACY_ADDRESS}>") is expected


# A pass TOKEN is not a pass RESULT: only the clause's own first `method=result` counts.
@pytest.mark.parametrize("results, expected", [
    # The reviewer's two strings: a pass token quoted inside a FAIL's reason.
    (('mx.google.com; dkim=fail reason="dkim=pass header.d=wentworth.example.com" '
      'header.d=evil.example',), False),
    (('mx.google.com; dmarc=fail reason="dmarc=pass header.from=wentworth.example.com" '
      'header.from=evil.example',), False),
    # A pass token inside a parenthesised comment.
    (("mx.google.com; dkim=fail (dkim=pass header.d=wentworth.example.com) header.d=evil.example",),
     False),
    (("mx.google.com; dmarc=fail (nested (dmarc=pass header.from=wentworth.example.com)) "
      "header.from=evil.example",), False),
    # A pass for a different domain, with the right domain only inside a quoted string.
    (('mx.google.com; dkim=pass header.d=evil.example reason="header.d=wentworth.example.com"',),
     False),
    (('mx.google.com; dkim=pass header.d="wentworth.example.com"',), False),
    # The right-domain clause fails while an unrelated clause passes.
    (("mx.google.com; dkim=fail header.d=wentworth.example.com; dkim=pass header.d=evil.example; "
      "dmarc=fail header.from=wentworth.example.com; dmarc=pass header.from=evil.example",),
     False),
    # A method named only inside another clause's properties is not that clause's result.
    (("mx.google.com; spf=pass smtp.mailfrom=x@y.example dkim=pass header.d=wentworth.example.com",),
     False),
    (("mx.google.com; dkim=neutral header.d=wentworth.example.com",), False),
    # Folded / spaced / case / trailing-dot variants of a legitimate pass.
    (("mx.google.com;\r\n\tdkim=pass\r\n\theader.d=wentworth.example.com\r\n\theader.s=s1",), True),
    (("MX.Google.com;   DKIM=PASS   Header.D=WENTWORTH.example.com.",), True),
    (("mx.google.com; dmarc=pass\r\n header.from=wentworth.example.com.",), True),
    # Comments and quoted reasons around a real pass do not hide it.
    (('mx.google.com; dkim=pass (good signature) header.d=wentworth.example.com '
      'reason="ok; fine"',), True),
    # A real failure first, a real pass in a later clause.
    (("mx.google.com; dkim=fail header.d=evil.example; dmarc=pass header.from=wentworth.example.com",),
     True),
    # Conflicting values for the property are refused.
    (("mx.google.com; dkim=pass header.d=wentworth.example.com header.d=evil.example",), False),
])
def test_gmail_sender_authenticated_reads_the_clauses_own_result_not_tokens_inside_it(
        results, expected):
    from privatemesh.gmail import sender_authenticated
    assert sender_authenticated(_headers(*results),
                                f"Wentworth Pharmacy <{PHARMACY_ADDRESS}>") is expected


def test_the_gmail_parser_puts_the_verdict_on_the_message():
    from privatemesh.gmail import _parse_message
    raw = {"id": "m1", "threadId": "t1", "internalDate": "0",
           "payload": {"headers": _headers(GMAIL_PASS), "mimeType": "text/plain"}}
    assert _parse_message(raw).sender_authenticated is True
    raw["payload"]["headers"] = _headers()
    assert _parse_message(raw).sender_authenticated is False


@pytest.mark.parametrize("sender", [
    # The comma spelling, which the product already refused.
    "repeat@wentworth.example.com, x@evil.example",
    # The ANGLE-BRACKET spelling, which it did not. These two are the same header written two ways,
    # and the fix is that they now get the same answer.
    f"Wentworth <{PHARMACY_ADDRESS}> <x@evil.example>",
    # A real address paired with an EMPTY mailbox: nothing evil in it, and still not one sender.
    # This is the shape whose second recipient is literally nothing, so it is the one that puts a
    # bare `<>` on the `To` line rather than a second address.
    f"Wentworth <{PHARMACY_ADDRESS}> <>",
])
def test_a_from_header_with_two_mailboxes_is_not_a_change_at_all(sender):
    """A `From` naming two mailboxes is not a correspondent, so it raises no change — and with no
    change there is no reply, no proposal and nothing for either limit to run.

    The bracket spelling used to be read as its FIRST mailbox: `sender_identity` took the first
    `<...>`, which for `Wentworth <repeat@…> <x@evil.example>` is the real pharmacy address, so the
    change was created with `from_address` set to the sender's WHOLE string. The reply's audience
    check then normalized that raw header back to the same first address and passed it, and the
    recipient list that reached the wire was `Wentworth Pharmacy <repeat@…> <x@evil.example>` — the
    pharmacy plus whatever a provider makes of the trailing bracket. Nobody had to be tricked for
    that; it only needed a header with two mailboxes in it.
    """
    message = _message(sender, authenticated=True)
    h = Harness(messages=[message], threads={message.thread_id: [message]},
                events=[_placeholder()], script=REFILL_SCRIPT)
    assert h.engine.classify(message) is None
    _nothing_happened(h)
    # "Not a change" is not the same as "nothing was decided": the refusal is on the record, under
    # the empty change key, because the change it would have belonged to was never created.
    refusals = [d for d in h.decisions.orphans() if d["verdict"] == REFUSED]
    assert refusals, h.decisions.orphans()
    assert refusals[-1]["rule"] == RULE_RECIPIENTS


def test_a_display_name_from_is_still_a_change_so_the_two_mailbox_refusal_is_about_the_count():
    """The mutation control for the rule above. A `From` with a display name and ONE mailbox is the
    ordinary case — the pharmacy writes exactly this way — and it must still be a change, still pin,
    and still be able to send. Without this, "refuse every `From` containing a `<`" would pass the
    test above."""
    h, service, change = _world(_message(f"Wentworth Pharmacy <{PHARMACY_ADDRESS}>",
                                         authenticated=True), mode=WITHIN_LIMITS)
    assert change.sender_pinned == PHARMACY_ADDRESS
    body = service.prepare(change.id)
    assert body["stage"] == "done" and body["executed_by"] == "limit"
    assert [m["to"] for m in h.mail.sent] == [[PHARMACY_ADDRESS]]
    assert not h.decisions.orphans()


# --------------------------------------------- the MIXED two-mailbox `From` (second review) -----
#: The second review's reproduction, on the REFILL side. `Wentworth <repeat@…>, attacker@evil.example`
#: has ONE angle group, so the count that closed `Wentworth <a@b> <c@d>` passed it, the group's own
#: text was a well-formed address, and `sender_identity` answered with the PHARMACY'S OWN ADDRESS —
#: read off a header the sender wrote. Note that the trusted address here is the care circle's real
#: one rather than a placeholder invented for the test: the old reading did not merely raise a
#: change, it bound an owner-trusted member and, when the message claimed to be authenticated, moved
#: that member's real placeholder.
#:
#: Two of these were already refused at 36b06d4 — the comma pair and the group form — by the old
#: code's whole-value `fullmatch` rather than by any count of mailboxes. They are kept because the
#: new rule reaches them by a different route, and `Group: <repeat@…>;` is the one that route would
#: otherwise MISS: `getaddresses` returns a group of one as a plain single address, so a rule built
#: on that parser alone would accept it. A control that stops being refused is a regression.
MIXED_FORM_TWO_MAILBOX_VALUES = [
    f"Wentworth <{PHARMACY_ADDRESS}>, attacker@evil.example",
    f"attacker@evil.example, Wentworth <{PHARMACY_ADDRESS}>",
    f"Group: <{PHARMACY_ADDRESS}>;",
    f"{PHARMACY_ADDRESS}, attacker@evil.example",
    f"Group: {PHARMACY_ADDRESS}, attacker@evil.example;",
    # Folded across a line break: the same two mailboxes as the first entry, written wrapped. The
    # header is unfolded before anything is counted, so it gets the same answer as the first.
    f"Wentworth <{PHARMACY_ADDRESS}>,\r\n attacker@evil.example",
    # A second list element that is a DISPLAY NAME with no address in it. One `@`, so neither the
    # group count nor the `@` count has anything to say; it is the standard library's own split
    # returning two entries, one address-less, that refuses it. The case that makes that count a
    # guard a test can decide on its own rather than a layer over the other two.
    f"Wentworth <{PHARMACY_ADDRESS}>, Jane",
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("sender", MIXED_FORM_TWO_MAILBOX_VALUES)
def test_a_mixed_form_two_mailbox_from_is_not_a_refill_at_all(sender, mode):
    """The REFILL half of the mixed-form `From`: no change, so no proposal, nothing to auto-run and
    nothing for any of the three limits to do with.

    Every mode is run, and the message claims `sender_authenticated=True`, which is the strongest
    shape the attacker can present: the binding against the care circle would have succeeded and
    `_refill_trust` would have found all three of its conditions true. The refusal has to come from
    the count of mailboxes, before any of that is reached — a check that only held under
    `ask_always` would leave `within_limits` moving an owner's placeholder.
    """
    message = _message(sender, authenticated=True)
    h = Harness(messages=[message], threads={message.thread_id: [message]},
                events=[_placeholder()], script=REFILL_SCRIPT)
    circle = CareCircleStore()
    circle.add(name="Wentworth Pharmacy", email=PHARMACY_ADDRESS, may_receive=["reply"], by=OWNER)
    h.engine.care_circle = circle
    service = h.v2()
    if mode != ASK_ALWAYS:
        service.limits.set_mode(mode)

    assert h.engine.classify(message) is None
    _nothing_happened(h)
    # "Not a change" is not "nothing was decided": the refusal is on the record under the empty
    # change key, because the change it would have belonged to was never created.
    refusals = [d for d in h.decisions.orphans() if d["verdict"] == REFUSED]
    assert refusals, h.decisions.orphans()
    assert refusals[-1]["rule"] == RULE_RECIPIENTS


def test_the_mixed_form_refill_controls_one_mailbox_with_a_quoted_comma_still_runs():
    """The positive control on the refill side, and the shape a rule that split on commas would
    break: a comma INSIDE a quoted display name is one mailbox, not two.

    It has to do more than be accepted — it has to still auto-run, so that "refuse every `From`
    carrying a comma, a `<` or a `:`" cannot pass the tests above. The comma is not what any of the
    three counts keys on; `getaddresses` returns it as part of the display name, which is the reason
    the parser is asked rather than the value being split by hand.
    """
    raw = f'"Wentworth, Pharmacy" <{PHARMACY_ADDRESS}>'
    h, service, change = _world(_message(raw, authenticated=True), mode=WITHIN_LIMITS)

    assert change.sender_pinned == PHARMACY_ADDRESS
    body = service.prepare(change.id)
    assert body["stage"] == "done" and body["executed_by"] == "limit", body
    assert [m["to"] for m in h.mail.sent] == [[PHARMACY_ADDRESS]]
    assert not h.decisions.orphans()


def test_the_mixed_form_refill_control_the_group_count_alone_let_it_through(monkeypatch):
    """The mutation control for the rule above, restored to the code the SECOND review saw.

    BOTH bindings of `sender_identity` are restored, not just the one in `changes`: the reviewed
    behaviour was that the same first-mailbox reading answered at every call site, and a control
    that patched only detection would be demonstrating a world that never existed — `execute`'s
    audience check would refuse a reply the old detection had already raised, and the test would go
    red for the wrong reason. With both, the old answer runs end to end and the reply's `To` line is
    the sender's WHOLE header, which is the harm the finding describes: a provider reads that as the
    pharmacy plus whatever it makes of the text after it.
    """
    from privatemesh import changes as changes_mod
    from privatemesh import execute as execute_mod

    def reviewed_group_count(value: str) -> str:
        """`36b06d4`'s `sender_identity`, reconstructed from `git show 36b06d4` rather than from
        memory, so the control restores the reviewed code and not a guess at it."""
        text = str(value or "").strip()
        groups = re.findall(r"<([^<>]*)>", text)
        if len(groups) > 1:
            return ""
        if groups:
            text = groups[0].strip()
        text = text.strip().strip("<>").strip()
        return text.casefold() if re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", text) else ""

    monkeypatch.setattr(changes_mod, "sender_identity", reviewed_group_count)
    monkeypatch.setattr(execute_mod, "sender_identity", reviewed_group_count)

    raw = f"Wentworth <{PHARMACY_ADDRESS}>, attacker@evil.example"
    h, service, change = _world(_message(raw, authenticated=True), mode=WITHIN_LIMITS)

    assert change.sender_pinned == PHARMACY_ADDRESS, "the pin looks right — that is the trap"
    assert change.from_address == change.sender_pinned != raw, "the wire value is the pin, not the raw header (followup2)"
    body = service.prepare(change.id)
    assert body["stage"] == "done" and body["executed_by"] == "limit", body
    assert [m["to"] for m in h.mail.sent] == [[PHARMACY_ADDRESS]], (
        "even with the old count restored, the reply goes to the pin and not the whole header")


@pytest.mark.parametrize("sender", [
    # A Cyrillic `е` in the domain — a homoglyph of the listed address, not the listed address.
    "Wentworth Pharmacy <repeat@wеntworth.example.com>",
])
def test_malformed_and_homoglyph_senders_never_pin(sender):
    h, service, change = _world(_message(sender, authenticated=True), mode=WITHIN_LIMITS)
    _held_without_a_pin(h, service, change)


# The test that used to sit here asserted a WEAKER property about the same header —
# `Wentworth <repeat@wentworth.example.com> <x@evil.example>` bound the real placeholder but could
# not auto-run, "so the reply goes to the listed pharmacy rather than to the attacker". The reply's
# `To` was in fact the sender's whole string rather than the listed address (the assertion passed
# only because it normalized each recipient first), so the test was pinning the behaviour the
# rewrite above removes. It is superseded, not deleted for being inconvenient: there is now no
# change, no proposal and no reply for either property to be about.
