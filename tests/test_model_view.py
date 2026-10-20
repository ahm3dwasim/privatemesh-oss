"""What the model saw — one receipt per call, bound to the prompt that left.

The product's claim has two halves and only one of them is about refusal. The other is that an
owner (or a judge) can read back the exact prompts a change sent to a hosted model, next to the
list of things that stayed on the machine. A claim you cannot read back is a claim you have to
take on faith.

`GET /v2/changes/{id}/model-view` is that reading, and it is built from the engine's OWN records:
one audit row written at the moment of each call, the same row the audit chain hashes. A second
bookkeeping path would be a second thing to keep in sync, and the day the two disagreed the
response would still look like evidence.

The strongest assertion here is the one `tests/test_fix_model_saw.py` makes of the agent path:
recompute the claim from the bytes the transport actually received. A receipt that merely says
"we sent something" is not a receipt.
"""
from __future__ import annotations

import json

import pytest

from fakes_v2 import Harness, at, change_email, evidence
from privatemesh.boundary import EgressRefused

SCRIPT = {
    "classify": {"is_meeting_change": True, "reason": "proposes new times"},
    "extract": {"options": [{"start": at("15:00"), "end": at("15:45"), "phrase": "3"}],
                "request_excerpt": "Could we meet near King's Cross tomorrow at 3 or 4?"},
    "query": [{"query": "cafe near King's Cross"}, {"query": "The Standard opening hours"}],
    "choose": {"venue_index": 0, "is_venue": True,
               "reason_sentence": "It is open at 15:00.", "rejected": []},
    "draft": {"subject": "Re: Our usual cafe has closed", "template_body": "Hi Alex,",
              "fragments": [{"source": "time", "text": "15:00-15:45 works for me."}]},
}

SEARCH = {"king's cross": [evidence("https://thestandard.co.uk/", "The Standard, King's Cross",
                                    "Open daily 12:00-23:00")]}


def prepared(**kw):
    h = Harness(script=SCRIPT, messages=[change_email("m1")], search=SEARCH, **kw)
    change_id = h.engine.detect(None)[0].id
    h.engine.prepare(change_id)
    return h, change_id


def receipts(h, change_id):
    return [json.loads(r["detail"]) for r in h.audit.rows(task_id=change_id, event="model_saw")]


def test_every_model_call_leaves_one_receipt_in_order():
    h, change_id = prepared()
    sent = h.rec.surfaces("model")
    view = h.engine.model_view(change_id)
    calls = view["calls"]
    # One receipt per call that reached the transport — no more (a receipt for a call that never
    # happened would be an invented row) and no fewer (a call with no receipt is an unreadable
    # one).
    assert len(calls) == len(sent)
    assert [c["phase"] for c in calls][0] == "classify"
    assert "draft" in [c["phase"] for c in calls]
    assert sum(1 for c in calls if c["phase"] == "query") == 2


def test_the_prompt_in_the_receipt_is_the_prompt_the_transport_received():
    """The binding that makes the receipt evidence rather than a claim about intent."""
    h, change_id = prepared()
    sent = h.rec.surfaces("model")
    calls = h.engine.model_view(change_id)["calls"]
    assert [c["prompt_sent"] for c in calls] == [e["payload"]["messages"] for e in sent]
    assert [c["model"] for c in calls] == [e["payload"]["model"] for e in sent]


def test_a_receipt_carries_the_tier_the_model_and_the_tokens():
    h, change_id = prepared()
    calls = h.engine.model_view(change_id)["calls"]
    for call in calls:
        assert call["tier"] == "reason"
        assert call["model"] == "super"
        assert call["tokens"] == {"prompt": 7, "completion": 3, "total": 10}


def test_an_unpriced_model_reports_no_cost_rather_than_zero():
    """The project's own rule, in `spend.py`: a model missing from the price table is REFUSED as
    an unknown price, not priced at zero. A receipt that said `0.0` here would be a number doing
    work it cannot do."""
    # The run itself is what an unpriced model must survive: `_cost_usd` is reached while the
    # receipt is written, so without the guard the whole run dies in the price table rather than
    # the view reporting null. The escape is the defect this test is looking for, so it is stated
    # as an assertion instead of crashing the run.
    try:
        h, change_id = prepared()
        calls = h.engine.model_view(change_id)["calls"]
    except Exception as exc:                                          # noqa: BLE001
        pytest.fail(f"an unpriced model raised {type(exc).__name__} instead of reporting no cost: "
                    f"{exc}")
    for call in calls:
        assert call["cost_usd"] is None


def test_a_priced_model_reports_the_price_it_actually_cost():
    from privatemesh.spend import PRICES, actual_usd

    model = "nvidia/Nemotron-3-Ultra-550b-a55b"
    assert model in PRICES
    h, change_id = prepared(models={"reason": model})
    calls = h.engine.model_view(change_id)["calls"]
    assert calls
    for call in calls:
        assert call["cost_usd"] == pytest.approx(actual_usd(model, 7, 3))


def test_kept_on_machine_names_the_categories_that_never_left():
    h, change_id = prepared()
    kept = h.engine.model_view(change_id)["kept_on_machine"]
    assert set(kept["categories"]) == {"title", "location", "reason", "attendees"}
    # The categories are named, never the values. A privacy screen that listed the title in order
    # to prove the title was safe would be the leak it exists to show absent.
    blob = json.dumps(h.engine.model_view(change_id))
    for marker in (h.boundary.current("pc1").title, h.boundary.current("pc1").location,
                   h.boundary.current("pc1").reason):
        assert marker not in blob


def test_a_refused_prompt_records_no_prompt_and_names_the_field_it_would_have_leaked():
    """The most consequential row in the list: a call the gate stopped. Nothing left, so there is
    no prompt to show — and the receipt says which field, never what it said.

    The scenario is the red-team's own: the email BODY carries a protected string. The engine
    reads the body to classify it, so the very first prompt of the change would have disclosed the
    protected title — and the change is never read at all. That is why the receipt has to exist
    even though the call never happened: without it the list would say the engine did nothing.
    """
    h = Harness(script=SCRIPT, search=SEARCH)
    title = h.boundary.current("pc1").title
    h.mail.messages.append(change_email("m1", body=f"Before you reply: {title}. Anyway, 3 or 4?"))
    with pytest.raises(EgressRefused):
        h.engine.detect(None)
    # No change was raised, so there is no id to GET — but the receipt exists, under the id
    # `classify` minted before the call, and it is the only handle on this refusal.
    ids = [r["task_id"] for r in h.audit.rows(event="model_saw")]
    assert len(ids) == 1
    calls = h.engine.model_calls(ids[0])
    assert len(calls) == 1
    assert calls[0]["phase"] == "classify"
    assert calls[0]["prompt_sent"] is None
    assert [w["field"] for w in calls[0]["withheld"]] == ["title"]
    assert calls[0]["tokens"] == {"prompt": 0, "completion": 0, "total": 0}
    assert calls[0]["cost_usd"] is None
    assert title not in json.dumps(calls)


def test_the_receipts_are_the_audit_rows_not_a_second_log():
    """Reading the view must be reading the audit. If the view could disagree with the chain, the
    chain would stop being evidence."""
    h, change_id = prepared()
    rows = receipts(h, change_id)
    calls = h.engine.model_view(change_id)["calls"]
    assert len(rows) == len(calls)


def test_the_audit_chain_still_verifies_with_receipts_in_it():
    h, change_id = prepared()
    assert h.engine.model_view(change_id)["calls"]
    assert h.audit.verify().ok is True
