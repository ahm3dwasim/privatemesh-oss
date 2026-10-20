"""The API the web app builds against — docs/API_V2.md, called as the UI calls it.

These tests are about the contract rather than about the flow: that the shapes are the documented
ones, that a stale approval is refused with 409 rather than performed, and that a leak is refused
with a status the UI can render instead of a stack trace.
"""
from __future__ import annotations

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


@pytest.fixture
def client():
    h = Harness(script=SCRIPT, messages=[change_email("m1")],
                search={"king's cross": [evidence("https://thestandard.co.uk/",
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


def test_session_reports_mode_connections_and_the_watcher(client):
    _h, api = client
    body = api.get("/v2/session").json()
    assert body["mode"] == "sample"
    assert body["owner_name"] == "Sam Owner"
    assert {c["service"] for c in body["connections"]} == {"gmail", "calendar", "tavily",
                                                           "nemotron"}
    assert set(body["routine"]) == {"enabled", "paused", "last_checked_at"}


def test_the_routine_can_be_paused_and_resumed(client):
    h, api = client
    # `POST routine` answers with the Session, not the routine row: the UI repaints the whole
    # header from it and has no second call to make (docs/API_V2.md, "returns Session").
    assert api.post("/v2/routine", json={"paused": True}).json()["routine"]["paused"] is True
    assert h.routine.paused
    assert api.post("/v2/routine", json={"paused": False}).json()["routine"]["paused"] is False


def test_today_returns_blocks_and_never_a_protected_title(client):
    h, api = client
    h.engine.detect(None)
    body = api.get("/v2/today", params={"date": "2026-09-19"}).json()
    assert body["date"] == "2026-09-19"
    assert [e["kind"] for e in body["events"]] == ["event"]
    protected = body["protected"][0]
    assert set(protected) == {"id", "start", "end", "buffer_minutes", "label"}
    assert protected["label"] == "Protected"
    assert "PC_TITLE" not in str(body)


def test_prepare_then_read_the_change(client):
    h, api, change_id = prepared(client)
    body = api.get(f"/v2/changes/{change_id}").json()
    assert body["stage"] == "proposal_ready"
    assert body["proposal"]["start"] == at("15:00")
    assert {a["kind"] for a in body["proposal"]["actions"]} == {"calendar_update", "email_reply"}
    assert body["proposal"]["privacy_line"].startswith("Your 16:30 commitment stays protected")
    assert body["proposal"]["expires_at"]


def test_approving_the_current_version_executes_both_effects(client):
    h, api, change_id = prepared(client)
    cs = h.changesets.for_change(change_id)
    body = api.post(f"/v2/changes/{change_id}/approve", json={"version": cs.version}).json()
    # Approve answers with the Change itself — the UI renders it directly and reads the outcome
    # separately, so a `{effects, stage}` receipt was a screen with nothing on it.
    assert body["id"] == change_id and body["stage"] == "done"
    outcome = api.get(f"/v2/changes/{change_id}/outcome").json()
    assert [e["state"] for e in outcome["effects"]] == ["done", "done"]


def test_approving_a_stale_version_is_409_and_writes_nothing(client):
    h, api, change_id = prepared(client)
    cs = h.changesets.for_change(change_id)
    response = api.post(f"/v2/changes/{change_id}/approve", json={"version": cs.version + 1})
    assert response.status_code == 409
    assert "version" in response.json()["detail"]
    assert not h.mail.sent and not h.rec.surfaces("calendar")


def test_approving_after_the_event_moved_is_409(client):
    h, api, change_id = prepared(client)
    cs = h.changesets.for_change(change_id)
    h.calendar.update_event("ev1", h.calendar.get_event("ev1").etag, start=at("11:00"),
                            end=at("11:45"), location="somewhere else", send_updates="all")
    response = api.post(f"/v2/changes/{change_id}/approve", json={"version": cs.version})
    assert response.status_code == 409
    assert "changed" in response.json()["detail"]
    assert not h.mail.sent


def test_an_edit_mints_a_new_version_and_invalidates_the_old_approval(client):
    h, api, change_id = prepared(client)
    first = h.changesets.for_change(change_id)
    body = api.post(f"/v2/changes/{change_id}/edit",
                    json={"reply_body": "Hi Alex, 15:00 works. See you then."}).json()
    assert body["version"] == first.version + 1
    assert api.post(f"/v2/changes/{change_id}/approve",
                    json={"version": first.version}).status_code == 409
    assert "15:00 works. See you then." in body["proposal"]["actions"][1]["body"]


def test_an_edit_into_the_protected_buffer_is_refused(client):
    """The validator still decides after a human edit: 16:00 cannot be typed into a yes."""
    _h, api, change_id = prepared(client)
    response = api.post(f"/v2/changes/{change_id}/edit", json={"start": at("16:00")})
    assert response.status_code == 422
    assert "15:45" in response.json()["detail"]


def test_leaving_the_meeting_unchanged_stops_the_change(client):
    h, api, change_id = prepared(client)
    body = api.post(f"/v2/changes/{change_id}/leave").json()
    assert body["stage"] == "stopped"
    assert not h.mail.sent and not h.rec.surfaces("calendar")


def test_why_discloses_what_was_used_and_what_was_withheld(client):
    _h, api, change_id = prepared(client)
    body = api.get(f"/v2/changes/{change_id}/why").json()
    assert body["kept_private"]["categories"] == ["title", "location", "reason", "attendees"]
    assert body["technical"]["model_calls"] >= 3
    assert body["used"]


def test_plans_lists_the_change_with_its_state(client):
    _h, api, change_id = prepared(client)
    plans = api.get("/v2/plans").json()
    assert [p["id"] for p in plans] == [change_id]
    assert plans[0]["state"] in {"completed", "needs_you", "partial", "stopped"}


def test_preferences_round_trip(client):
    _h, api = client
    # The fact is `fact`, not `text`, and both reads are bare lists — the shape the UI's
    # preference editor posts and maps over (docs/API_V2.md, Preference).
    created = api.post("/v2/preferences", json={"fact": "Prefer quiet cafes",
                                                "scope": "venues",
                                                "source": "Owner entered"}).json()
    assert created["id"] and created["source"] and "last_used" in created
    assert any(p["id"] == created["id"] for p in api.get("/v2/preferences").json())
    assert api.delete(f"/v2/preferences/{created['id']}").status_code == 204
    assert api.get("/v2/preferences").json() == []


def test_an_edit_to_an_empty_reply_is_refused(client):
    """Clearing the box is not an edit to a reply, it is a request to send nothing."""
    _h, api, change_id = prepared(client)
    response = api.post(f"/v2/changes/{change_id}/edit", json={"reply_body": "   "})
    assert response.status_code == 422
    body = api.get(f"/v2/changes/{change_id}").json()
    assert body["proposal"]["actions"][1]["body"].startswith("Hi Alex,")


def test_an_edit_to_a_time_that_cannot_be_read_is_refused(client):
    _h, api, change_id = prepared(client)
    response = api.post(f"/v2/changes/{change_id}/edit", json={"start": "tomorrow-ish"})
    assert response.status_code == 422


def test_an_unknown_change_is_404(client):
    _h, api = client
    assert api.get("/v2/changes/nope").status_code == 404
    assert api.post("/v2/changes/nope/prepare").status_code == 404


def test_a_refused_egress_is_422_with_a_safe_detail(client):
    """The refusal reaches the UI as a refusal, and the detail does not quote what leaked."""
    _h, api, change_id = prepared(client)
    response = api.post(f"/v2/changes/{change_id}/edit",
                        json={"reply_body": "About the PC_REASON_Marker_Ada piano recital: "
                                            "I would rather not say."})
    assert response.status_code == 422
    assert "PC_REASON" not in response.text
    assert "protected" in response.json()["detail"]


def test_the_router_is_mounted_in_the_v1_app(agent, conn, audit, broker, grants, registry):
    from privatemesh.api import build_app

    h = Harness(script=SCRIPT)
    app = build_app(agent=agent, conn=conn, audit=audit, broker=broker, grants=grants,
                    llm=None, registry=registry, v2=h.v2())
    # The OpenAPI document rather than `app.routes`: it is what the UI is generated from, and it
    # only lists routes that are actually reachable.
    paths = app.openapi()["paths"]
    assert "/v2/session" in paths and "/v2/changes/{change_id}/approve" in paths
