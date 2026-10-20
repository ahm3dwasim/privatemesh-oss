"""The web client's contract: every addition `web/lib/api.ts` reads, over HTTP.

`tests/test_api_v2.py` pins the engine's behaviours. This file pins the SHAPES the browser
actually consumes, because that is where the two halves of this product have never met:

* the session the UI gates approval on (`authenticated_owner`, the protected `boundary`);
* `GET /v2/today` with the date omitted, which is how the UI asks for "today";
* the operations the UI performs before it can plan anything (boundary, connections, routine);
* **full `Change` from prepare and approve** — the UI renders the returned object directly and
  never re-reads the change afterwards, so a `{change_id, stage}` receipt is a blank screen;
* the fields the change and the proposal were missing (`blocked_reason`, `updated_at`,
  `venues`, `skill_used`, `learning_id`);
* `used` as labelled entries rather than bare sentences, bare lists for preferences and plans;
* an error body a client can branch on (`code`), not only prose.

Where a shape here contradicts the current code, `docs/API_V2.md` wins — that is the document the
web lane built against, and it is the one the UI has already been written to render.
"""
from __future__ import annotations

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from privatemesh.api_v2 import build_v2_router

from fakes_v2 import Harness, at, change_email, evidence

SCRIPT = {
    "classify": {"is_meeting_change": True, "reason": "proposes new times"},
    "extract": {"options": [{"start": at("16:00"), "end": at("16:45"), "phrase": "4"},
                            {"start": at("15:00"), "end": at("15:45"), "phrase": "3"}],
                "request_excerpt": "Could we meet near King's Cross tomorrow at 3 or 4?"},
    "query": [{"query": "cafe near King's Cross"}, {"query": "The Standard opening hours"}],
    "choose": {"venue_index": 0, "is_venue": True,
               "reason_sentence": "It is open at 15:00.", "rejected": []},
    "draft": {"subject": "Re: Our usual cafe has closed", "template_body": "Hi Alex,",
              "fragments": [{"source": "time", "text": "15:00-15:45 works for me."}]},
}

#: The day the harness clock is pinned to. `today` with no date must answer about THIS day.
TODAY = at("09:00")[:10]


@pytest.fixture
def client():
    # A FRESH script per test. `ScriptedTF` answers a list-valued phase by popping from it, so a
    # module-level script shared between tests is a script the first test edits for the second —
    # which showed up here as "the venue has no evidence" in whichever test ran second.
    h = Harness(script=copy.deepcopy(SCRIPT), messages=[change_email("m1")],
                search={"king's cross": [evidence("https://thestandard.co.uk/",
                                                  "The Standard, King's Cross",
                                                  "Open daily 12:00-23:00")],
                        "the standard": [evidence("https://thestandard.co.uk/",
                                                  "The Standard, King's Cross",
                                                  "Open daily 12:00-23:00")]})
    app = FastAPI()
    app.include_router(build_v2_router(h.v2(mode="sample", owner_name="Sam Owner",
                                            timezone="Europe/London")))
    return h, TestClient(app)


def prepared(client):
    h, api = client
    change_id = h.engine.detect(None)[0].id
    assert api.post(f"/v2/changes/{change_id}/prepare").status_code == 200
    return h, api, change_id


# ---------------------------------------------------------------- session, day, operations -------
def test_session_carries_owner_authorization_and_the_protected_boundary(client):
    """The UI disables every approve control on `authenticated_owner`, and the connections screen
    edits `boundary`. Neither was on the response, so neither could be honoured."""
    _h, api = client
    body = api.get("/v2/session").json()
    assert body["authenticated_owner"] is True
    assert body["boundary"] == {"start": "16:30", "buffer_minutes": 45, "configured": True}


def test_today_defaults_to_the_services_own_day(client):
    """`api.today()` is called with no argument on first load. A required `date` query parameter
    made that a 422, which is why the UI never rendered a day."""
    _h, api = client
    response = api.get("/v2/today")
    assert response.status_code == 200, response.text
    assert response.json()["date"] == TODAY
    assert api.get("/v2/today", params={"date": "2026-01-02"}).json()["date"] == "2026-01-02"


def test_the_boundary_can_be_moved_and_is_refused_when_it_is_not_a_boundary(client):
    h, api = client
    body = api.post("/v2/boundary", json={"start": "17:00", "buffer_minutes": 30}).json()
    assert body["boundary"] == {"start": "17:00", "buffer_minutes": 30, "configured": True}
    assert h.boundary.current("pc1").buffer_minutes == 30

    for bad in ({"start": "25:00", "buffer_minutes": 30}, {"start": "17:00", "buffer_minutes": -1},
                {"start": "17:00", "buffer_minutes": 241}):
        refused = api.post("/v2/boundary", json=bad)
        assert refused.status_code == 422, bad
        assert refused.json()["code"] == "invalid_boundary"


def test_connecting_a_service_returns_the_session(client):
    _h, api = client
    body = api.post("/v2/connections/gmail").json()
    assert [c for c in body["connections"] if c["service"] == "gmail"][0]["state"] == "connected"
    assert api.post("/v2/connections/telegram").status_code == 404


def test_pausing_the_routine_returns_the_session(client):
    h, api = client
    body = api.post("/v2/routine", json={"paused": True}).json()
    assert body["mode"] == "sample" and body["routine"]["paused"] is True
    assert h.routine.paused


# ---------------------------------------------------------------- one change ---------------------
def test_prepare_returns_the_change_the_ui_renders(client):
    """The UI does `setChange(await api.prepare(id))`. `{change_id, stage}` left the screen with
    no proposal, so the hero flow stopped at the first click."""
    _h, api, change_id = prepared(client)
    body = api.post(f"/v2/changes/{change_id}/prepare").json()
    assert body["id"] == change_id and body["stage"] == "proposal_ready"
    assert body["proposal"]["start"] == at("15:00")
    assert body["updated_at"]


def test_approve_returns_the_change_and_its_new_stage(client):
    _h, api, change_id = prepared(client)
    version = api.get(f"/v2/changes/{change_id}").json()["version"]
    body = api.post(f"/v2/changes/{change_id}/approve", json={"version": version}).json()
    assert body["id"] == change_id
    assert body["stage"] == "done"
    assert body["version"] == version


def test_a_change_always_carries_blocked_reason_and_updated_at(client):
    h, api = client
    change_id = h.engine.detect(None)[0].id
    body = api.get(f"/v2/changes/{change_id}").json()
    assert body["blocked_reason"] is None
    assert body["updated_at"]
    assert "skill_used" not in body or body["skill_used"] is None


def test_the_proposal_offers_the_venues_it_checked(client):
    """The edit form's venue select and the "options that didn't fit" list both read
    `proposal.venues`; the engine only ever emitted the chosen one."""
    _h, api, change_id = prepared(client)
    proposal = api.get(f"/v2/changes/{change_id}").json()["proposal"]
    assert isinstance(proposal["venues"], list) and proposal["venues"]
    assert proposal["venues"][0]["name"] == proposal["venue"]["name"]
    assert proposal["venues"][0]["evidence"]["sample"] is True


def test_the_calendar_action_names_the_attendees_it_will_notify(client):
    """`docs/API_V2.md`: "Calendar actions include `attendees: string[]` independently of email `to`
    recipients. ... Missing calendar attendees prevents approval when notifications are enabled."

    The web client takes the second sentence literally — `validBundle` in `app/page.tsx` requires
    `Array.isArray(calendar.attendees)` before it will enable "Approve these 2 actions". The engine
    wrote the diff and the notification flag and left the recipients out, so the review screen of a
    LIVE client was a screen with a dead approval button: the plan was right, the action manifest
    was incomplete, and the owner could not say yes to it.
    """
    _h, api, change_id = prepared(client)
    body = api.get(f"/v2/changes/{change_id}").json()
    calendar = next(a for a in body["proposal"]["actions"] if a["kind"] == "calendar_update")
    assert calendar["attendees"] == ["alex@example.com"], (
        "the calendar update must name the invitees it would notify")
    assert calendar["notifies_attendees"] is True
    # The email recipients are a DIFFERENT list and must not be derived from this one.
    mail = next(a for a in body["proposal"]["actions"] if a["kind"] == "email_reply")
    assert mail["to"] == ["alex@example.com"]


def test_evidence_says_when_it_is_synthetic(client):
    _h, api, change_id = prepared(client)
    proposal = api.get(f"/v2/changes/{change_id}").json()["proposal"]
    assert proposal["venue"]["evidence"]["sample"] is True
    for rejected in proposal["rejected"]:
        assert rejected["evidence"] is None or "sample" in rejected["evidence"]


# ---------------------------------------------------------------- disclosure ---------------------
def test_why_labels_every_thing_it_used(client):
    """`web/app/page.tsx` renders `used[].label` and `used[].detail`. Bare sentences rendered as
    empty rows."""
    _h, api, change_id = prepared(client)
    body = api.get(f"/v2/changes/{change_id}/why").json()
    assert body["used"] and all(set(item) >= {"label", "detail"} for item in body["used"])
    assert body["kept_private"]["categories"]
    assert set(body["kept_private"]["categories"]) == set(body["kept_private"]["counts"])
    assert isinstance(body["technical"]["model_calls"], int)
    assert isinstance(body["changed"], list)


def test_why_answers_after_the_change_has_run(client):
    """`/why` on a FINISHED change, which is when a reader most wants it.

    The effect manifest reads how far each action got out of the change set's stored outcome, and
    the store round-trips that outcome through JSON — so what comes back is `Outcome.as_dict()`,
    a dict of dicts. Reading it as objects raised AttributeError, and the endpoint answered 500
    for every change that had executed while answering fine for every change that had not: the
    comprehension over the effects never iterates when there is no outcome at all, which is
    exactly why the pre-approval test above could not catch it.
    """
    _h, api, change_id = prepared(client)
    version = api.get(f"/v2/changes/{change_id}").json()["version"]
    assert api.post(f"/v2/changes/{change_id}/approve",
                    json={"version": version}).status_code == 200
    response = api.get(f"/v2/changes/{change_id}/why")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {e["kind"]: e["state"] for e in body["effects"]} == {"calendar_update": "confirmed",
                                                                "email_reply": "confirmed"}
    calendar = next(e for e in body["effects"] if e["kind"] == "calendar_update")
    assert set(calendar["fields"]) == {"start", "end", "location", "attendees"}
    assert calendar["fields"]["start"] == at("15:00")
    # Named, not merely present. The manifest reads the attendee list back out of the stored
    # calendar display, so an unwritten one came back as `[]` and this assertion passed on an
    # executed change that told the owner it had notified nobody.
    assert calendar["fields"]["attendees"] == ["alex@example.com"]


# ---------------------------------------------------------------- lists --------------------------
def test_preferences_are_a_bare_list_of_the_contract_shape(client):
    _h, api = client
    assert api.get("/v2/preferences").json() == []
    created = api.post("/v2/preferences", json={"fact": "Prefer quiet cafés",
                                                "scope": "Meeting planning",
                                                "source": "Owner entered"}).json()
    assert set(created) == {"id", "fact", "scope", "source", "last_used"}
    assert created["source"] == "Owner entered" and created["last_used"] is None
    assert api.get("/v2/preferences").json() == [created]

    edited = api.patch(f"/v2/preferences/{created['id']}",
                       json={"fact": "Prefer quiet cafés", "scope": "Meeting planning"})
    assert edited.status_code == 200 and edited.json()["scope"] == "Meeting planning"
    assert api.patch("/v2/preferences/nope", json={"fact": "x", "scope": "y"}).status_code == 404
    assert api.patch(f"/v2/preferences/{created['id']}",
                     json={"fact": " ", "scope": "y"}).status_code == 422
    assert api.delete(f"/v2/preferences/{created['id']}").status_code == 204
    assert api.get("/v2/preferences").json() == []


def test_plans_are_a_bare_list_with_a_detail_line(client):
    h, api = client
    change_id = h.engine.detect(None)[0].id
    plans = api.get("/v2/plans").json()
    assert isinstance(plans, list) and [p["id"] for p in plans] == [change_id]
    assert set(plans[0]) >= {"id", "title", "state", "detail"}
    assert plans[0]["detail"]


# ---------------------------------------------------------------- errors -------------------------
def test_a_refusal_carries_a_code_the_client_can_branch_on(client):
    _h, api, change_id = prepared(client)
    version = api.get(f"/v2/changes/{change_id}").json()["version"]
    stale = api.post(f"/v2/changes/{change_id}/approve", json={"version": version + 7})
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_version"
    assert stale.json()["detail"]

    assert api.get("/v2/changes/nope").json()["code"] == "not_found"
    assert api.post("/v2/changes/nope/prepare").json()["code"] == "not_found"


def test_a_paused_routine_refuses_preparation_with_its_own_code(client):
    h, api, change_id = prepared(client)
    h.routine.pause()
    other = h.engine.detect(None)
    target = other[0].id if other else change_id
    if target == change_id:
        pytest.skip("the harness only exposes one change")
    refused = api.post(f"/v2/changes/{target}/prepare")
    assert refused.status_code == 409 and refused.json()["code"] == "routine_paused"
