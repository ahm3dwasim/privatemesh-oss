"""The private boundary: what may leave, what may never.

The claim under test is not "the UI hides the title". It is that a protected field cannot reach a
provider even when a caller tries, and that the refusal is structural rather than a rule each call
site remembers. Two different leaks are exercised, because they need two different defences:

* a **verbatim** field value — caught by scanning the payload for the protected values;
* a **paraphrase** — caught by an allowlist on which outbound fields and fragment sources may
  exist at all, plus a content-word overlap backstop for text that bypassed the composer.
"""
from __future__ import annotations

import pytest

from privatemesh.boundary import (
    ALLOWED_FRAGMENT_SOURCES,
    BoundaryStore,
    EgressRefused,
    Fragment,
    ParaphraseLeak,
    PrivacyGate,
    ProtectedLeak,
    SchemaLeak,
    compose_email,
)
from privatemesh.ports import ProtectedCommitment

from fakes_v2 import at

TITLE = "MARKER_TITLE_School pick-up for Ada"
LOCATION = "MARKER_LOCATION_42 Kings Cross conservatory"
REASON = "MARKER_REASON_Ada piano recital at the conservatory"
ATTENDEE = "marker_attendee_ada@example.com"

def reply_subject(text: str = "tomorrow"):
    """The subject kwargs for `compose_email`, spelled the way the engine spells them.

    A subject is not a string a caller hands over any more — the sixth review's first finding is
    that one did, straight into the provider's `Subject` header. It is a TEMPLATE from the closed set
    plus the fragments that fill it, so writing one here means writing the same two things
    `ChangeEngine._prepare` writes.
    """
    return {"subject_template": "Re:",
            "subject_fragments": [Fragment(source="thread_subject", text=text)]}


PROTECTED_FIELDS = {"title": TITLE, "location": LOCATION, "reason": REASON,
                    "attendee": ATTENDEE}


@pytest.fixture
def boundary():
    store = BoundaryStore()
    store.put(id="pc1", start=at("16:30"), end=at("17:30"), buffer_minutes=45,
              title=TITLE, location=LOCATION, reason=REASON, attendees=(ATTENDEE,))
    return store


@pytest.fixture
def gate(boundary):
    return PrivacyGate(boundary)


# ---------------------------------------------------------------- the store --------------------
def test_planning_fact_is_only_the_four_allowed_keys(boundary):
    fact = boundary.planning_fact("pc1")
    assert set(fact) == {"start", "end", "buffer_minutes", "label"}
    assert fact["label"] == "protected"
    assert fact["buffer_minutes"] == 45
    blob = repr(fact)
    for name, value in PROTECTED_FIELDS.items():
        assert value not in blob, f"planning_fact leaked {name}"


def test_boundary_edits_are_versioned(boundary):
    assert boundary.version_of("pc1") == 1
    v2 = boundary.put(id="pc1", start=at("17:00"), end=at("18:00"), buffer_minutes=30,
                      title=TITLE, location=LOCATION, reason=REASON, attendees=(ATTENDEE,))
    assert v2.version == 2
    assert boundary.version_of("pc1") == 2
    assert boundary.current("pc1").start == at("17:00")
    assert boundary.current("pc1").buffer_minutes == 30


def test_only_the_current_version_is_a_planning_fact(boundary):
    boundary.put(id="pc1", start=at("17:00"), end=at("18:00"), buffer_minutes=30, title=TITLE)
    assert boundary.planning_fact("pc1")["start"] == at("17:00")


def test_planning_fact_for_an_unknown_commitment_is_a_key_error(boundary):
    with pytest.raises(KeyError):
        boundary.planning_fact("nope")


# ---------------------------------------------------------------- verbatim leaks --------------
@pytest.mark.parametrize("field", sorted(PROTECTED_FIELDS))
def test_gate_refuses_each_protected_field_verbatim(gate, field):
    """Removing one field's marker must not make the others invisible: each is asserted alone."""
    payload = {"note": f"the owner has a {PROTECTED_FIELDS[field]} tomorrow"}
    with pytest.raises(ProtectedLeak) as exc:
        gate.check_payload(payload, kind="model")
    assert field in str(exc.value)


def test_gate_refuses_a_leak_nested_in_a_list_of_parts(gate):
    payload = [{"role": "user", "content": [{"type": "text", "text": f"see {LOCATION}"}]}]
    with pytest.raises(ProtectedLeak):
        gate.check_payload(payload, kind="model")


def test_gate_refuses_whitespace_and_case_variants(gate):
    with pytest.raises(ProtectedLeak):
        gate.check_payload({"q": f"  {LOCATION.upper()}  "}, kind="search")


def test_gate_refuses_a_search_query_carrying_the_location(gate):
    with pytest.raises(ProtectedLeak):
        gate.guard_search_query(f"cafes near {LOCATION}")


def test_gate_refuses_calendar_fields_carrying_the_reason(gate):
    with pytest.raises(ProtectedLeak):
        gate.guard_calendar_fields(location="Old Cafe", description=REASON)


def test_gate_refuses_a_model_payload_carrying_the_title(gate):
    messages = [{"role": "system", "content": "You are helpful."},
                {"role": "user", "content": f"context: {TITLE}"}]
    with pytest.raises(ProtectedLeak):
        gate.guard_model_payload(messages, task_id="t", phase="intent")


def test_gate_refuses_an_email_addressed_to_a_protected_attendee(gate):
    email = compose_email(to=(ATTENDEE,), **reply_subject(),
                          fragments=[Fragment(source="template", text="15:00 works for me.")],
                          gate=None)
    with pytest.raises(ProtectedLeak) as exc:
        gate.guard_email(email)
    assert "attendee" in str(exc.value)


def test_a_clean_payload_passes(gate):
    gate.check_payload({"planning_fact": gate.planning_fact("pc1")}, kind="model")
    gate.guard_search_query("quiet cafe near King's Cross open at 15:00")
    gate.guard_email(compose_email(to=("alex@example.com",), **reply_subject(),
                                   fragments=[Fragment(source="time", text="15:00-15:45")],
                                   gate=None))


# ---------------------------------------------------------------- paraphrase ------------------
def test_reworded_reason_in_the_email_body_is_refused(gate):
    """A marker test alone cannot establish semantic privacy: the reason is reworded, not copied."""
    reworded = "I have a piano recital at the conservatory that afternoon, so earlier is better."
    assert REASON not in reworded                      # not a verbatim leak
    email = compose_email(to=("alex@example.com",), **reply_subject(),
                          fragments=[Fragment(source="template", text=reworded)], gate=None)
    with pytest.raises(ParaphraseLeak):
        gate.guard_email(email)


def test_a_fragment_from_a_protected_source_never_composes(gate):
    """The schema allowlist: only declared fragment sources may become outbound text."""
    with pytest.raises(SchemaLeak):
        compose_email(to=("alex@example.com",), **reply_subject(),
                      fragments=[Fragment(source="protected_reason", text="something else")],
                      gate=None)


def test_the_composer_refuses_a_fragment_source_outside_the_allowlist(gate):
    assert "protected_reason" not in ALLOWED_FRAGMENT_SOURCES
    assert {"template", "time", "venue", "recipient"} <= ALLOWED_FRAGMENT_SOURCES


def test_the_gate_refuses_free_form_body_text(gate):
    """Defence in depth: a caller that skipped the composer still cannot ship unbound prose."""
    email = compose_email(to=("alex@example.com",), **reply_subject(),
                          fragments=[Fragment(source="template", text="sure")], gate=None)
    forged = type(email)(to=email.to, subject=email.subject, body="something nobody allowed",
                         fragments=email.fragments, in_reply_to=email.in_reply_to,
                         subject_template=email.subject_template,
                         subject_fragments=email.subject_fragments)
    with pytest.raises(SchemaLeak):
        gate.guard_email(forged)


def test_the_gate_refuses_a_protected_field_smuggled_into_the_subject(gate):
    email = compose_email(to=("alex@example.com",), **reply_subject(TITLE),
                          fragments=[Fragment(source="template", text="Hello")], gate=None)
    with pytest.raises(ProtectedLeak):
        gate.guard_email(email)


# ---------------------------------------------------------------- the harness itself ----------
def test_the_capture_harness_can_see_a_leak():
    """Negative control. Without this, an all-green privacy suite proves only that the assertion
    is blind. With no gate attached, the protected title DOES reach the captured model payload."""
    from fakes_v2 import Recorder, ScriptedTF

    rec = Recorder()
    tf = ScriptedTF(rec, answer="ok").tf          # gate=None: nothing stands between caller and wire
    tf.chat([{"role": "user", "content": f"context: {TITLE}"}])
    assert rec.find(TITLE, "model"), "the recorder cannot see a leak, so its silence proves nothing"


def test_with_the_gate_attached_the_same_call_never_reaches_the_wire(gate):
    from fakes_v2 import Recorder, ScriptedTF

    rec = Recorder()
    tf = ScriptedTF(rec, answer="ok").bind(gate)
    with pytest.raises(ProtectedLeak):
        tf.chat([{"role": "user", "content": f"context: {TITLE}"}])
    assert not rec.surfaces("model"), "a refused call still posted a body"


def test_the_gate_reports_a_refusal_to_the_audit_log(gate, audit):
    gate.audit = audit
    with pytest.raises(EgressRefused):
        gate.check_payload({"x": TITLE}, kind="model")
    rows = [r for r in audit.rows() if r["event"] == "egress.protected_refused"]
    assert len(rows) == 1
    assert TITLE not in str(rows[0]["detail"])
