"""The fifteen findings of the care review, one test each, each watched red first.

Every test here names a finding, names the guard, and was run against the source with that guard
removed before it was run against the source with the guard in place. The matching entries live in
`redteam/mutcheck_findings.py`, which performs that removal mechanically and reports a verdict per
finding, so "red first" is a recorded measurement rather than a claim in a docstring.

The rule the file is written to: a test that passes for a reason other than the guard is a test that
measures its own fixture. Each one therefore asserts on the REFUSAL or on the ABSENCE of the value,
and each has a control that shows the check would have fired.
"""
from __future__ import annotations

import json

import pytest

from privatemesh import boundary, care
from privatemesh.audit import AuditLog
from privatemesh.changeset import APPROVED_STATUS
from privatemesh.execute import CALENDAR_SKILL, CALENDAR_UPDATE_KIND, EMAIL_SKILL
from privatemesh.llm import Tier
from privatemesh.boundary import CareAudienceRefused, PrivacyGate, ProtectedLeak, compose_email
from privatemesh.boundary import Fragment
from privatemesh.care import (CareCircleStore, CareRefused, DigestAlreadySent, DigestDayMissing,
                              DigestLedger, ProtectedFactStore)
from privatemesh.execute import DigestLedgerMissing
from privatemesh.sim import build_sample_ports, sample_protected_facts
from privatemesh.wiring import build_sample

from fakes_v2 import COMMITMENT, Harness, at  # noqa: F401

BENEFITS = "QQ-4471-ALPHA-9"
POA = "POA-2024-88317-LK"
HOME = "Flat 3, 118 Wentworth Road, London N17 6XQ"
ROSA = "rosa.almeida@example.com"
TOM = "tom.almeida@example.com"
OUTSIDE = "records@benefits-verify.example.net"
WHEN = "2026-09-22T09:00:00+01:00"


def _circle(*, may_receive=("digest", "reply", "schedule")) -> CareCircleStore:
    circle = CareCircleStore()
    for name, address in (("Rosa", ROSA), ("Tom", TOM)):
        circle.add(name=name, email=address, may_receive=may_receive, by=care.OWNER)
    return circle


@pytest.fixture
def h():
    """The hand-built engine over fakes, which is the only way to ask what WOULD happen if the pin
    upstream had failed: the approval below genuinely authorises what the engine would never
    produce, so a refusal here is the guard's and not the fixture's."""
    return Harness()


def _approved(h, **cal_over):
    """An approved bundle whose calendar arguments are whatever the caller asked for."""
    from privatemesh.changes import _calendar_display

    cal_args = h.calendar_args(**cal_over)
    mail_args = h.email_args()
    cal = h.approvals.request("task1", CALENDAR_SKILL, cal_args)
    mail = h.approvals.request("task1", EMAIL_SKILL, mail_args)
    cs = h.changesets.create(change_id="c1", task_id="task1",
                             event_etag=h.calendar.get_event("ev1").etag,
                             boundary_id="pc1", boundary_version=h.boundary.version_of("pc1"),
                             calendar_args=cal_args, email_args=mail_args,
                             calendar_display=_calendar_display(h.calendar.get_event("ev1"),
                                                                cal_args),
                             approval_ids=(cal.id, mail.id))
    return h.changesets.approve(cs.id, cs.version,
                                current_event_etag=h.calendar.get_event("ev1").etag,
                                current_boundary_version=h.boundary.version_of("pc1"))


# ---------------------------------------------------------------- 1: the calendar write ---------
def test_finding_01_a_calendar_write_passes_the_gate_before_the_provider(h):
    """Finding 1: `update_event` wrote whatever the bundle told it to.

    The calendar is an egress surface like any other — a location string goes to a provider and
    comes back on every device the owner has — and nothing on that path asked the gate. The venue
    the model chose is what lands in `location`, so a model that had been shown the home address
    could put it in the invitation.

    Red first: with `gate.guard_calendar_fields(...)` removed from the calendar skill, the write
    reaches the port and the event moves."""
    secret = COMMITMENT["location"]
    cs = _approved(h, location=secret)
    assert cs.status == APPROVED_STATUS, "the fixture failed to approve, so nothing was tested"

    outcome = h.executor.execute(cs.id)

    assert h.calendar.get_event("ev1").location != secret, "the address is in the calendar"
    effect = next(e for e in outcome.effects if e.kind == CALENDAR_UPDATE_KIND)
    assert effect.state == "failed"
    assert secret not in effect.detail


def test_finding_01_control_a_clean_calendar_write_is_let_through(h):
    """The control. A guard that refused every calendar write would pass the test above and be an
    outage: moving the meeting IS the feature."""
    cs = _approved(h, location="The Standard, King's Cross")
    outcome = h.executor.execute(cs.id)
    effect = next(e for e in outcome.effects if e.kind == CALENDAR_UPDATE_KIND)
    assert effect.state == "done"
    assert h.calendar.get_event("ev1").location == "The Standard, King's Cross"


# ------------------------------------------------------- 2: the prompt STORE, not the prompt ----
def test_finding_02_the_audit_row_holds_the_masked_prompt_and_not_the_value():
    """Finding 2: the audit stored what the gate had just taken the value OUT of.

    `egress_sanitize` strips credentials and knows nothing about protected facts, so recording its
    output left `QQ-4471-ALPHA-9` sitting in the audit database beside the masked prompt that was
    actually sent — a copy of the fact, in a durable store, that `/model-view` reads back.

    Driven through `_ask`, which is the ONE function that writes a prompt receipt, over a payload
    that genuinely carries the value. Red first: with `prompt_sent = self._mask_for_receipt(...)`
    removed, the stored row carries the value and both assertions fail."""
    wiring = build_sample(care_week=True)
    engine = wiring.engine

    class _Answering:
        def model_for(self, tier):
            return "stub"

        def chat(self, messages, tier=None, *, task_id="", phase=""):
            return {"choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    engine.llm = _Answering()
    engine._ask([{"role": "user", "content": f"her benefits id is {BENEFITS} and {HOME}"}],
                tier=Tier.REASON, phase="extract", change_id="chg-stored")

    rows = wiring.audit.rows(event="model_saw", task_id="chg-stored")
    assert rows, "no prompt receipt was filed, so this test would pass vacuously"
    blob = json.dumps(rows, default=str)
    for row in sample_protected_facts():
        assert row["value"] not in blob, f"the audit stored {row['label']}"
    assert "[[private:" in blob, "the receipt does not show the model being told anything"


def test_finding_02_the_review_screen_never_re_renders_a_value():
    """The other half of finding 2, and the half the audit test does not cover on its own.

    Masking at the moment of STORAGE leaves a second question open: does the screen that reads the
    row back out apply its own check, or does it trust what it finds? Those are two guards, and
    they fail independently — a build that masked correctly could still serve a leak written by an
    older version, a restored database, or the other writer of `model_saw` rows (`broker.
    record_model_saw` files an agent's turns under the same event name).

    So the row is planted RAW, with a protected value in it, exactly as a leaky store would have
    left it. That isolates the serving guard: with `_gate_prompts`' redaction removed, the planted
    value reaches the response. Both modes AND both owner states, because a view that only redacts
    for one of them is the shape this kind of fix takes when it is written around a mode check
    rather than through it."""
    for care_week, presented in ((False, "redteam"), (True, "redteam"), (True, None)):
        wiring = build_sample(care_week=care_week)
        change = wiring.engine.detect(None)[0]
        wiring.audit.record("model_saw", task_id=change.id, detail={
            "phase": "planted", "tier": "reason", "model": "planted",
            "prompt_sent": [{"role": "user", "content": f"her benefits id is {BENEFITS}"}],
            "withheld": [], "tokens": {}, "cost_usd": None})
        served = json.dumps(wiring.service.model_view(change.id, presented=presented), default=str)
        assert "planted" in served, "the planted call was not served, so this proves nothing"
        assert BENEFITS not in served, "the review screen re-served a value out of the stored row"


# ------------------------------------------- 3: inbound excerpts and model prose are scrubbed ---
def _hostile_model(reason: str, rejected_name: str):
    """A model that is fully on the attacker's side and says so in its own prose.

    The change record stores the model's `reason_sentence` and its rejected-options table, and
    `/changes/{id}` serves both. So a model that was shown a protected value and asked to reword it
    could put it in the sentence the engine files as its own explanation — the disclosure travelling
    as an annotation rather than as a payload, past a gate that only inspects what leaves."""

    def build(gate):
        from privatemesh.redteam import CompromisedModel

        case = {"id": "finding-3", "model": {
            "extract": {"options": [{"start": at("15:00"), "end": at("15:45"),
                                     "phrase": "3pm"}],
                        "request_excerpt": "meet near King's Cross tomorrow at 3"},
            "query": {"query": "cafes near King's Cross", "include_domains": []},
            "choose": {"venue_index": 0, "name": "The Standard, King's Cross",
                       "address": "The Standard, King's Cross",
                       "reason_sentence": reason, "rejected": [{"name": rejected_name}],
                       "is_venue": True},
            "draft": {"template_body": "Re: Our usual cafe has closed",
                      "fragments": [{"source": "template",
                                     "text": "Hi Alex, 15:00-15:45 works for me."}]}}}
        return CompromisedModel(case, [], [])(gate)

    return build


def test_finding_03_the_model_s_own_sentences_are_scrubbed_before_they_are_stored():
    """Finding 3, the model-authored half, driven through the REAL preparation.

    Red first: with `self._scrub(...)` and `self._scrub_payload(...)` removed from the proposal in
    `_commit_plan`, both values are in the change record and this fails. A test that called
    `_scrub_payload` directly would stay green through exactly that mutation, because the function
    would still be correct and would no longer be called."""
    wiring = build_sample(llm=_hostile_model(f"the stop is near {HOME}",
                                             f"a cafe at {HOME}"))
    change = wiring.engine.detect(None)[0]
    wiring.engine.prepare(change.id)

    served = json.dumps(wiring.service.change(change.id), default=str)
    assert HOME not in served, "the model's prose reached the change record unscrubbed"
    # And the model WAS believed about the parts it is allowed to be believed about, so this is not
    # a test that passes because preparation refused everything. What it is believed about is the
    # SELECTION: it answered `venue_index: 0`, and the venue that reaches the record is the one the
    # engine fetched for that index — `Fern & Finch Café`, the sample's first result. The `name` and
    # `address` it wrote into the same answer are not read at all (round 6, finding 3: "the model
    # must SELECT, never author"), which is why the sentence below asserts their absence as well.
    assert "Fern & Finch" in served, "the venue the model selected did not reach the record"
    assert "The Standard" not in served, "a place the model authored reached the record"


def test_finding_03_an_inbound_excerpt_is_scrubbed_before_it_is_stored():
    """Finding 3, the inbound half. An excerpt is never sent anywhere, so nothing on the egress path
    refuses it — and it is served by `/today`, by `/changes/{id}` and written to the audit database.
    A benefits-fraud email that opens with the benefits id put the id in all three.

    Red first: with `_scrub` reduced to returning its argument, the excerpt carries the value."""
    from privatemesh.ports import MailMessage
    from privatemesh.sim import SAMPLE_OWNER

    wiring = build_sample()
    wiring.mail.deliver(MailMessage(
        id="msg-hostile", thread_id="thread-hostile",
        sender="Benefits Update <benefits@northgate-clinic.example.com>",
        to=(SAMPLE_OWNER,), subject="Action needed: confirm the record",
        body_text=f"Her benefits id is {BENEFITS}. Could we meet tomorrow at 3?",
        received_at="2026-09-18T09:45:00.000Z",
        rfc822_message_id="<hostile-1@example.com>"))
    change = next(c for c in wiring.engine.detect(None) if c.id and "hostile" in c.thread_id)

    assert BENEFITS not in json.dumps(wiring.service.change(change.id), default=str)
    assert BENEFITS not in json.dumps(wiring.service.today(), default=str)


# --------------------------------------------------------- 4: an exception is not a channel -----
def test_finding_04_a_parse_failure_never_quotes_the_model():
    """Finding 4: `_loads` put the model's answer in the exception message.

    A model that had just been shown a fact could put it in malformed JSON, and the exception
    travelling up to the API would then carry the value out of the process — past every gate, in a
    traceback, in an error body. The message now carries the length and a hash, which is what a
    person actually needs to correlate the failure with the receipt.

    Red first: with the message restored to `f"...: {raw!r}"`, the value is in `str(exc)`."""
    from privatemesh.changes import _loads

    raw = '{"reason": "her benefits id is ' + BENEFITS + '"'  # truncated on purpose
    with pytest.raises(ValueError) as exc:
        _loads(raw)
    assert BENEFITS not in str(exc.value)
    assert "sha256=" in str(exc.value), "the hash is what makes the message useful without the text"


# ------------------------------------------------- 9: an approval is not a membership bypass -----
def test_finding_09_an_approved_disclosure_still_needs_the_circle():
    """Finding 9: `approve_disclosure` recorded the owner's consent and then let the value through
    to ANY recipient.

    An approval answers "may this value leave at all". It does not answer "may it go to this
    address" — that is the circle's question, and it is a different question with a different
    answer. Consent to tell the family is not consent to tell a lookalike.

    The refusal for the off-circle address is a `ProtectedLeak` rather than a `CareAudienceRefused`,
    and that is the fail-closed direction rather than a gap: an approval that does not apply leaves
    the value with no key at all, so it is refused as a disclosure rather than waved through by a
    check that only knew about audiences.

    Red first: with the membership half of `_disclosure_allowed` removed, the second call below
    raises nothing and the lookalike is told."""
    circle = CareCircleStore()
    circle.add(name="Rosa", email=ROSA, may_receive=("digest", "reply"), by=care.OWNER)
    facts = ProtectedFactStore()
    fact = facts.add(label="Benefits id", kind="benefits_id", value=BENEFITS, by=care.OWNER)
    gate = PrivacyGate(boundary.BoundaryStore(), facts=facts, care_circle=circle)

    assert gate.approve_disclosure("email-abc", [fact.label], [ROSA]) == 1
    gate.check_text(f"her id is {BENEFITS}", kind="email", field="body",
                    artifact="email-abc", recipients=[ROSA])

    # And the same approval does NOT cover a recipient the owner never listed. The approval names
    # people; the people are the circle; an address off it holds no key even for a fact that was
    # genuinely approved.
    with pytest.raises(ProtectedLeak):
        gate.check_text(f"her id is {BENEFITS}", kind="email", field="body",
                        artifact="email-abc", recipients=[OUTSIDE])
    # And the WRITE side refuses it too, so there is no ordering of the two calls that gets there.
    with pytest.raises(CareAudienceRefused):
        gate.approve_disclosure("email-abc", [fact.label], [OUTSIDE])

    # AND THE MEMBERSHIP IS RE-ASKED AT THE MOMENT OF THE CHECK, which is a different claim from
    # the one above and needs its own attempt. Rosa held the approval AND was on the circle when it
    # was recorded; the audience-derived refusal above cannot show this, because an approval that
    # never named the lookalike fails for a reason that does not involve the circle at all. Remove
    # Rosa and the same approval must stop fitting — otherwise a member the owner deleted keeps a
    # key that still turns, which is the whole content of "re-asked here, not trusted from there".
    rosa = next(m for m in circle.members() if m.address == ROSA)
    assert circle.remove(rosa.id, by=care.OWNER) is True
    with pytest.raises(ProtectedLeak):
        gate.check_text(f"her id is {BENEFITS}", kind="email", field="body",
                        artifact="email-abc", recipients=[ROSA])


def test_finding_09_the_gate_refuses_a_care_recipient_off_the_circle():
    """The same refusal one layer down, at the gate that every care send passes through.

    `check_text` is only reached by a payload that carries a protected value; `guard_care_recipients`
    is the check that runs on every care action whether or not there is a fact in it — an
    off-circle address is refused for being off the circle, not for what the message happens to say.
    Driven here directly, because a test through the engine is answered by clause 7 first and would
    keep passing with this guard deleted.

    The assertion is the SENTENCE, and that is not decoration. The two questions refuse either way
    — `may_receive` answers False for an address that is not a member, so deleting the membership
    half still refuses, one branch later. What the membership half uniquely produces is the answer
    the owner can act on: a stranger is "who is this?", not "you did not give them this", and a
    screen that told the owner to grant a permission to somebody they have never heard of would be
    a worse screen at the exact moment they are trying to work out what happened. So the check is
    pinned by its sentence and by the audit's `why`, which is the pair the review screen reads.

    Red first: with `if not circle.knows(bare): raise CareAudienceRefused(...)` removed, the
    stranger is refused by the grant branch instead and the sentence assertion fails."""
    circle = CareCircleStore()
    circle.add(name="Rosa", email=ROSA, may_receive=("digest", "reply"), by=care.OWNER)
    audit = AuditLog()
    gate = PrivacyGate(boundary.BoundaryStore(), audit=audit, care_circle=circle)

    # On the circle and listed for the kind: allowed.
    gate.guard_care_recipients([ROSA], kind="digest")
    # Off the circle: refused as a STRANGER, and refused whether the kind would have been granted.
    for to in ([OUTSIDE], [ROSA, OUTSIDE]):
        with pytest.raises(CareAudienceRefused) as caught:
            gate.guard_care_recipients(to, kind="digest")
        assert "not on your care circle" in str(caught.value), (
            f"a stranger was refused as if they were a member: {caught.value}")
    # On the circle but not listed for THIS kind: refused by the grant half, and the sentence is
    # the other one — the two failures are distinguishable or the split is not doing anything.
    with pytest.raises(CareAudienceRefused) as caught:
        gate.guard_care_recipients([ROSA], kind="schedule")
    assert "did not list them for" in str(caught.value)
    assert "not on your care circle" not in str(caught.value)
    whys = [row for row in audit.rows(event="egress.care_audience_refused")]
    assert any("not_a_member" in json.dumps(r, default=str) for r in whys), whys
    assert any("not_granted" in json.dumps(r, default=str) for r in whys), whys


# --------------------------------------- 10: clause 7 cannot be skipped by saying nothing --------
def test_finding_10_omitting_the_care_metadata_does_not_skip_clause_seven():
    """Finding 10: clause 7 read `care_kind` and `care_recipients` from the bundle, and a bundle
    that simply did not declare them was not checked at all.

    The ADDRESS is on the plan whether or not the metadata is — it is in the `to` field the send
    path reads — so the clause derives the audience from the address. The case that proves it is a
    member the owner listed for the DIGEST ONLY, written to with no care metadata at all: an
    implementation that trusted the absent field would pass it.

    Red first: with the address-derived recipient set removed, the first call returns `ok`."""
    wiring = build_sample(care_week=True)
    change = wiring.engine.detect(None)[0]
    # Tom is on the circle for the digest and nothing else, so "reply" to him is the refusal.
    ok, detail = wiring.engine._clause_seven(change, {"to": [TOM]})
    assert not ok, "a member was written to for a kind the owner did not list them for"
    assert "did not list them for" in detail

    # And a care action that names NOBODY is refused rather than treated as a pass.
    ok, detail = wiring.engine._clause_seven(change, {"care_kind": "reply", "care_recipients": []})
    assert not ok and "no recipient" in detail, "an empty recipient list was treated as a pass"

    # And a bundle that names care recipients without saying what kind of care action it is.
    ok, detail = wiring.engine._clause_seven(change, {"care_recipients": [OUTSIDE]})
    assert not ok and "what kind" in detail


def test_finding_10_control_an_ordinary_scheduling_reply_still_passes():
    """The control, and the one that keeps the derivation from becoming an outage: a reply to
    somebody who is NOT on the care circle is an ordinary reply, not a care action."""
    wiring = build_sample()
    change = wiring.engine.detect(None)[0]
    ok, detail = wiring.engine._clause_seven(change, {"to": ["alex@example.com"]})
    assert ok, detail


# ------------------------------------------------------ 11: the digest is on the real path ------
def test_finding_11_a_digest_is_recorded_in_the_ledger_when_it_is_SENT():
    """Finding 11: `digest_email` existed and nothing in the product called it, so the once-a-day
    cap in clause 7 was a clause about a table nothing ever wrote to.

    Red first: with `self.digests.confirm(entry_id)` removed from `send_digest`, the ledger row
    stays `pending` and the second assertion fails — which is the difference between "a digest was
    attempted" and "a digest went out"."""
    wiring = build_sample(care_week=True)
    plan = wiring.executor.build_digest("2026-09-22", now=WHEN)
    assert plan.recipients and plan.fragments

    # No `subject` argument any more: the seventh review's first finding is that a caller-supplied
    # one reached the provider's `Subject` header with nothing having rendered it, so the subject is
    # the render of `DIGEST_SUBJECT_TEMPLATE` and the plan's own day — see `care.digest_email`.
    sent = wiring.executor.send_digest(plan)
    assert sent["day"] == "2026-09-22"
    rows = wiring.digests.recent(10)
    assert [r["id"] for r in rows] == [sent["entry_id"]]
    assert rows[0]["pending"] is False, "the ledger recorded an attempt, not a send"
    assert rows[0]["recipients"] == list(plan.recipients)


def test_finding_11_a_failed_send_gives_the_day_back():
    """The other half, and the half a naive `record`-on-entry implementation gets wrong: an error
    is not a digest, and a claim left behind burns the owner's one digest for a day on which
    nothing was sent."""
    wiring = build_sample(care_week=True)
    plan = wiring.executor.build_digest("2026-09-22", now=WHEN)
    # The port refuses: the fresh membership read at send time is against a circle that no longer
    # holds the address the digest was composed for.
    for member in wiring.care_circle.members():
        wiring.care_circle.remove(member.id, by=care.OWNER)
    with pytest.raises(CareAudienceRefused):
        wiring.executor.send_digest(plan)
    assert wiring.digests.recent(10) == [], "the failure left the day spent"


# ------------------------------------------- 12: the cap is atomic, and it fails CLOSED ----------
def test_finding_12_the_cap_is_taken_by_the_insert_not_by_a_count():
    """Finding 12: `DIGEST_PER_DAY` was checked with a read and then written, so two writers that
    both read zero both inserted and the report said one digest went out when two did.

    Red first: with the UNIQUE index dropped from `_LEDGER_SCHEMA`, the second `claim` succeeds and
    this fails."""
    ledger = DigestLedger()
    ledger.claim(day="2026-09-22", recipients=[ROSA], artifact="a")
    with pytest.raises(DigestAlreadySent):
        ledger.claim(day="2026-09-22", recipients=[ROSA], artifact="b")
    assert ledger.sent_on("2026-09-22") == 1


def test_finding_12_an_empty_day_is_refused_rather_than_being_a_bucket():
    """An empty day is not "a day nobody has sent one for". It is a bucket every day falls into, so
    the first empty-day digest would refuse every real digest for the rest of time — and an
    attacker who controls the field gets to choose the bucket."""
    ledger = DigestLedger()
    with pytest.raises(DigestDayMissing):
        ledger.claim(day="", recipients=[ROSA], artifact="a")
    with pytest.raises(DigestDayMissing):
        ledger.claim(day="   ", recipients=[ROSA], artifact="a")


def test_finding_12_a_missing_ledger_fails_closed():
    """Finding 12's last clause. "Cannot check the limit" and "there is no limit" are the same
    sentence only to a build that has decided to send.

    Red first: with the `DigestLedgerMissing` raise removed, the send proceeds unledgered — and the
    assertion is written as "the refusal, and not some other failure" rather than as
    `pytest.raises(DigestLedgerMissing)` alone, because without the guard the very next line reaches
    `None.claim(...)`: a `pytest.raises` for the right class would ERR on the `AttributeError`
    instead of failing, and a control that errors out has not been shown to measure anything."""
    wiring = build_sample(care_week=True)
    plan = wiring.executor.build_digest("2026-09-22", now=WHEN)
    wiring.executor.digests = None
    with pytest.raises(BaseException) as caught:                   # noqa: PT011 - narrowed below
        wiring.executor.send_digest(plan)
    assert isinstance(caught.value, DigestLedgerMissing), (
        f"a missing ledger produced {type(caught.value).__name__}, not the refusal")
    assert "cannot be checked" in str(caught.value)


# ------------------------------------------- 13: one snapshot for membership AND grant -----------
def test_finding_13_the_audience_is_read_once_at_send_time():
    """Finding 13: membership and the per-kind grant were checked against two different readings of
    a list the owner is editing, so a member removed between them could still be sent to.

    A `CircleSnapshot` is taken once and both halves are answered from it. The snapshot is the
    object the port is handed, so this is a fact about the type rather than about the order two
    calls happen to run in.

    Red first: with `snapshot=self.care_circle.snapshot()` replaced by no snapshot at all, the
    revoked member is looked up live and the send goes through."""
    circle = _circle()
    snapshot = circle.snapshot()
    assert snapshot.may_receive(ROSA, "digest") is True
    # By ADDRESS, not by position: `CareCircleStore.snapshot` orders on `(added_at, id)` and
    # `added_at` is second-resolution, so two members added in the same second come back in
    # whatever order their hashed ids sort in. `members()[0]` is a coin toss and a test that
    # revokes a random member is a test that measures the coin.
    victim = next(m for m in circle.members() if m.address == ROSA)
    assert circle.remove(victim.id, by=care.OWNER) is True
    assert snapshot.may_receive(ROSA, "digest") is True, "the snapshot re-read the live list"
    assert circle.may_receive(ROSA, "digest") is False
    assert snapshot.addresses() >= {ROSA, TOM}


def test_finding_13_a_member_removed_before_the_send_is_not_sent_to():
    """The same finding through the real send path, at the coarse granularity: a member revoked
    before `send_digest` is called is refused there.

    This is the easy half — the composition happens inside `send_digest`, so its own snapshot is
    already taken after the removal and would refuse on its own. It is kept because it is the
    behaviour an owner actually performs (remove somebody, then send), and the test below is the
    one that isolates the fresh read."""
    wiring = build_sample(care_week=True)
    plan = wiring.executor.build_digest("2026-09-22", now=WHEN)
    victim = next(m for m in wiring.care_circle.members() if m.address in plan.recipients)
    assert wiring.care_circle.remove(victim.id, by=care.OWNER) is True
    with pytest.raises(CareAudienceRefused):
        wiring.executor.send_digest(plan)


def test_finding_13_a_revocation_between_composition_and_the_port_is_caught(monkeypatch):
    """The window itself, which is the only thing the fresh read exists for.

    `digest_email` reads the circle ONCE and the snapshot travels on the email; `guard_email` then
    re-asks it, so a revocation is caught twice against the SAME reading. Two reads of one snapshot
    is still one read: a member removed between composition and the port is in that snapshot and
    passes both. The fresh snapshot immediately before the port is the third check, and it is the
    only one that can see the change — so the test has to make the change land in that window, and
    a revocation performed before `send_digest` is called does not: composition has not happened yet.

    The revocation is attached to the composition call, which is exactly where an owner editing the
    circle from the other screen would land. Red first: with `snapshot=self.care_circle.snapshot()`
    replaced by the email's own composition snapshot, both checks answer from the stale reading, the
    removed member is written to, and nothing is raised."""
    wiring = build_sample(care_week=True)
    plan = wiring.executor.build_digest("2026-09-22", now=WHEN)
    victim = next(m for m in wiring.care_circle.members() if m.address in plan.recipients)
    real = care.digest_email

    def compose_then_revoke(*args, **kwargs):
        email = real(*args, **kwargs)
        assert wiring.care_circle.remove(victim.id, by=care.OWNER) is True
        return email

    monkeypatch.setattr(care, "digest_email", compose_then_revoke)
    with pytest.raises(CareAudienceRefused):
        wiring.executor.send_digest(plan)


# ------------------------------------------------------------------ 14: sample mode -------------
def test_finding_14_no_route_returns_a_value_in_either_mode():
    """Finding 14: sample mode stays open by design — the data is synthetic — but "open" must mean
    "nobody is authenticated", never "the facts are readable".

    Every care read route, in both modes, with the owner presented and without. A route that
    answered a value would be the disclosure path the feature exists to close, wearing a settings
    page. The decision to keep sample mode unauthenticated is written down in `docs/THREAT_MODEL.md`
    rather than left to be inferred from this test.

    Red first: with `as_dict` on `ProtectedFact` changed to include `value`, this fails."""
    values = [row["value"] for row in sample_protected_facts()]
    for care_week in (False, True):
        wiring = build_sample(care_week=care_week)
        change = wiring.engine.detect(None)[0]
        try:
            wiring.engine.prepare(change.id)
        except Exception:                                  # noqa: BLE001
            # A change that cannot be planned still has a record, and the record is one of the
            # surfaces. Preparing is a bonus here, not the subject.
            pass
        for presented in (None, "redteam"):
            surfaces = [
                wiring.service.protected_facts_state(presented),
                wiring.service.care_circle_state(presented),
                wiring.service.model_view(change.id, presented=presented),
                wiring.service.change(change.id),
                wiring.service.today(),
            ]
            blob = json.dumps(surfaces, default=str)
            for value in values:
                assert value not in blob, f"{value!r} served in care_week={care_week}"


# ------------------------------------------------------------- 15: error bodies are fixed -------
def test_finding_15_a_refusal_body_never_echoes_the_caller():
    """Finding 15: `_care_refusal` answered with `str(exc)`, and the store's messages are built
    while looking at owner-supplied input.

    The answer is now the exception's TYPE, as a fixed sentence. This drives the two ways a caller
    reaches it and requires the caller's own text to be absent from the body both times.

    Red first: with `_care_refusal` restored to `V2Error(422, str(exc))`, the address and the
    unknown kind both come back in the body."""
    from privatemesh.api_v2 import _care_refusal

    for exc in (CareRefused(f"the email field is not an email address: {OUTSIDE}"),
                CareRefused(f"a member may receive {BENEFITS}"),
                DigestAlreadySent(f"a digest has already gone out for 2026-09-22"),
                DigestDayMissing(f"a digest must say which day it is for: {OUTSIDE}")):
        body = _care_refusal(exc)
        # `detail` and not `message`: `V2Error` subclasses `HTTPException`, whose field is
        # `detail`, and `coded` renders `{detail, code}`. Reading an attribute this class does not
        # have made the assertion below run against an empty string and pass for the wrong reason —
        # so the guard is read from where the client would actually get it.
        text = json.dumps({"detail": getattr(body, "detail", ""), "code": getattr(body, "code", "")},
                          default=str)
        assert text.strip() not in ('{"detail": "", "code": ""}', ""), "read no body at all"
        for needle in (OUTSIDE, BENEFITS, "2026-09-22"):
            assert needle not in text, f"{type(exc).__name__} echoed {needle!r}: {text}"
