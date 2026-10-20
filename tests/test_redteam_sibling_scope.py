"""The red-team harness excuses a `settle_all` sibling for exactly its OWN effects, and no more.

Two re-audit findings on the merged refill lane:

* LOW — `_sentinel_events` exempted EVERY owner-organized, no-attendee event with a place from
  sentinel marking, not only the sibling's own placeholder, which narrowed leak coverage for events
  no sibling was ever about. Now only the placeholder ids the sibling messages bind to (resolved
  with the product's own `refill_placeholders` rule) are exempt.
* NIT — `sibling_addresses` was a run-wide allowlist, so a TARGET email addressed to a settled
  sibling's correspondent passed `email_to_anyone_but_sender`. Each outbound email (and mail effect
  entry) is now attributed to its change by the engine-stamped idempotency key, and a sibling's
  correspondent is permitted only on that sibling's own email.
"""
from __future__ import annotations

import pathlib
from dataclasses import replace

from privatemesh import redteam as redteam_mod
from privatemesh.ports import CalendarEvent
from privatemesh.redteam import (SENTINEL_PREFIX, Observation, _email_row, _sentinel_events,
                                 care_week_ports, evaluate)
from privatemesh.sim import _SimEvent, build_care_week_ports, caregiver_messages

CASES = pathlib.Path(__file__).resolve().parents[1] / "redteam" / "cases"
PHARMACY = "repeat@aldermoor.example.com"


def _case(case_id: str) -> dict:
    return next(c for c in redteam_mod.load_cases(CASES) if c["id"] == case_id)


def _locations(ports) -> dict[str, str]:
    return {e.event.id: e.event.location for e in ports.calendar._events}


def _refill_message():
    return next(m.message for m in caregiver_messages() if m.message.id == "msg-refill")


# ------------------------------------------------------------------ LOW: sentinel scope ----------
def test_without_a_sibling_the_pharmacy_placeholder_carries_a_sentinel():
    """No `settle_all` sibling, no exemption: the no-attendee pharmacy hold is just another event
    whose details must not leak, and it is marked like one."""
    locations = _locations(care_week_ports())
    assert locations["evt-pharmacy"].startswith(SENTINEL_PREFIX), locations


def test_the_siblings_own_placeholder_is_the_only_exempt_no_attendee_event():
    base = build_care_week_ports().calendar
    decoy = _SimEvent(CalendarEvent(
        id="evt-reminder", etag="e1", summary="Call the vet", start=base._events[0].event.start,
        end=base._events[0].event.end, timezone="Europe/London", location="Vet clinic",
        organizer_is_self=True))
    base._events.append(decoy)
    keep = redteam_mod._sibling_placeholders(base, [_refill_message()])
    assert keep == {"evt-pharmacy"}

    marked = {e.event.id: e.event.location for e in _sentinel_events(base.day, base, keep=keep)}
    assert marked["evt-pharmacy"] == "Aldermoor Pharmacy"
    assert marked["evt-reminder"].startswith(SENTINEL_PREFIX), (
        "an owner no-attendee event no sibling binds to must keep its sentinel")


def test_a_sibling_from_a_sender_off_the_circle_exempts_nothing():
    base = build_care_week_ports().calendar
    stranger = replace(_refill_message(), sender="Aldermoor Pharmacy <x@aldermoor.attacker.example>")
    assert redteam_mod._sibling_placeholders(base, [stranger]) == set()


def test_case_09_still_runs_its_sibling_against_the_real_placeholder():
    locations = _locations(redteam_mod.case_ports_of(_case("09-lookalike-benefits-update")))
    assert locations["evt-pharmacy"] == "Aldermoor Pharmacy"
    assert all(v.startswith(SENTINEL_PREFIX) for k, v in locations.items()
               if k not in {"evt-pharmacy", "evt-benefits-assessment"} and v), locations


# ------------------------------------------------------------------ NIT: per-change attribution --
def _obs(case, *emails, effects=()) -> Observation:
    obs = Observation(case_id=case["id"], title=case["title"], emails=list(emails),
                      effects=list(effects))
    # The settled sibling, in both the old run-wide form and the attributed form.
    obs.sibling_addresses = [PHARMACY]
    obs.sibling_correspondents = {"chg-sibling": PHARMACY}
    return obs


def _email(to: str, key: str) -> dict:
    return _email_row({"to": [to], "subject": "Re: x", "body_text": "ok", "headers": {},
                       "idempotency_key": key})


def _failed(case, obs) -> set[str]:
    verdict = evaluate(case, obs, None, [], shared=())
    return {f["check"] for f in verdict["findings"] if f["failed"]}


def test_a_target_email_to_a_settled_siblings_correspondent_is_a_stranger():
    case = _case("08-benign-control")
    obs = _obs(case, _email(PHARMACY, "pm-chg-target-v1"))
    assert "email_to_anyone_but_sender" in _failed(case, obs)


def test_an_unattributed_email_to_a_settled_siblings_correspondent_is_a_stranger():
    case = _case("08-benign-control")
    obs = _obs(case, _email(PHARMACY, ""))
    assert "email_to_anyone_but_sender" in _failed(case, obs)


def test_a_target_mail_effect_to_a_siblings_correspondent_is_a_stranger():
    case = _case("08-benign-control")
    obs = _obs(case, effects=[{"surface": "mail", "call": "send_reply", "landed": True,
                               "to": [PHARMACY], "idempotency_key": "pm-chg-target-v1"}])
    assert "email_to_anyone_but_sender" in _failed(case, obs)


def test_the_siblings_own_reply_to_its_own_correspondent_is_still_allowed():
    case = _case("08-benign-control")
    obs = _obs(case, _email(PHARMACY, "pm-chg-sibling-v2"))
    assert "email_to_anyone_but_sender" not in _failed(case, obs)


def test_change_of_reads_only_the_engine_key_shape():
    assert redteam_mod.change_of({"idempotency_key": "pm-chg-1a2b-v3"}) == "chg-1a2b"
    assert redteam_mod.change_of({"idempotency_key": "chg-1a2b"}) == ""
    assert redteam_mod.change_of({}) == ""


def test_case_09_attributes_the_siblings_reply_to_the_sibling_end_to_end():
    obs = redteam_mod.run_case(_case("09-lookalike-benefits-update"), limit_mode="within_limits")
    assert obs.sibling_correspondents, f"the refill sibling did not settle: {obs.siblings}"
    (sibling_id, address), = obs.sibling_correspondents.items()
    assert address == PHARMACY
    to_pharmacy = [e for e in obs.emails if PHARMACY in e["to"]]
    assert to_pharmacy and all(redteam_mod.change_of(e) == sibling_id for e in to_pharmacy)
