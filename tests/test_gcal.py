"""gcal: the CalendarPort over the Calendar REST API, against an httpx.MockTransport.

The conditional update is the whole point of this file. A reviewed proposal names an exact event
version; if the event moved since the review, the write must stop with `EtagMismatch` rather than
silently overwrite a plan the owner never saw.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from privatemesh import gcal as gc
from privatemesh.ports import EtagMismatch, ReauthRequired

EVENT_ID = "evt-meeting-1"
ETAG_V1 = '"etag-v1"'
ETAG_V2 = '"etag-v2"'


def raw_event(*, etag=ETAG_V1, start="2026-09-19T11:00:00+01:00", end="2026-09-19T11:45:00+01:00",
              timezone="Europe/London", summary="Alex — project sync", location="Café Granger",
              organiser_self=True, recurring=False, attendees=("alex@example.com",),
              status="confirmed") -> dict:
    raw = {"id": EVENT_ID, "etag": etag, "status": status, "summary": summary, "location": location,
           "start": {"dateTime": start, "timeZone": timezone},
           "end": {"dateTime": end, "timeZone": timezone},
           "organizer": {"email": "owner@example.com", "self": organiser_self},
           "attendees": [{"email": a} for a in attendees]}
    if recurring:
        raw["recurrence"] = ["RRULE:FREQ=WEEKLY;BYDAY=FR"]
    return raw


class FakeCalendar:
    """Scripted Calendar API. Records every request so the wire format can be asserted."""

    def __init__(self, event=None, *, patch_status=200, busy=None, list_items=None,
                 freebusy_errors=None, always_401=False, one_401=False):
        self.event = event if event is not None else raw_event()
        self.patch_status = patch_status
        self.busy = busy if busy is not None else [
            {"start": "2026-09-19T16:30:00Z", "end": "2026-09-19T17:15:00Z"}]
        self.list_items = list_items
        self.freebusy_errors = freebusy_errors
        self.always_401 = always_401
        self._unauth_left = 1 if one_401 else 0
        self.requests: list[httpx.Request] = []
        self.patches: list[dict] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def calls(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method]

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.always_401 or self._unauth_left > 0:
            self._unauth_left -= 1
            return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
        path = req.url.path
        if path.endswith("/freeBusy") and path != "/calendar/v3/freeBusy":
            # Google serves free/busy at the API root, NOT under /calendars/{id}; anywhere else is
            # the HTML 404 the first live run hit. A mock that accepts both hides that bug.
            return httpx.Response(404, text="<!DOCTYPE html><title>Error 404 (Not Found)!!1</title>")
        if path == "/calendar/v3/freeBusy":
            if self.freebusy_errors:
                return httpx.Response(200, json={"calendars": {"primary": {
                    "busy": [], "errors": self.freebusy_errors}}})
            return httpx.Response(200, json={"calendars": {"primary": {"busy": self.busy}}})
        if path.endswith("/events"):
            items = self.list_items if self.list_items is not None else [self.event]
            return httpx.Response(200, json={"items": items, "timeZone": "Europe/London"})
        if "/events/" in path:
            if path.rsplit("/", 1)[-1] != self.event["id"]:
                return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
            if req.method == "PATCH":
                body = json.loads(req.content)
                self.patches.append({"body": body, "if_match": req.headers.get("If-Match"),
                                     "params": parse_qs(urlparse(str(req.url)).query)})
                if self.patch_status != 200:
                    return httpx.Response(self.patch_status, json={"error": {
                        "code": self.patch_status, "message": "Precondition Failed"}})
                self.event = {**self.event, **body, "etag": ETAG_V2}
                return httpx.Response(200, json=self.event)
            return httpx.Response(200, json=self.event)
        return httpx.Response(404, json={"error": {"code": 404, "message": "no route"}})


class Tokens:
    def __init__(self, value="ya29.CAL-TOKEN", *, refresh=None):
        self.value = value
        self.refresh = refresh
        self.refreshes = 0

    def access_token(self) -> str:
        return self.value


def client(fake: FakeCalendar, tokens=None) -> gc.GCalClient:
    return gc.GCalClient("you", http=fake.client, tokens=tokens or Tokens())


# ------------------------------------------------------------------ reads ----------------------
def test_get_event_parses_the_fields_the_proposal_needs():
    event = client(FakeCalendar()).get_event(EVENT_ID)
    assert event.id == EVENT_ID
    assert event.etag == ETAG_V1
    assert event.start == "2026-09-19T11:00:00+01:00"
    assert event.end == "2026-09-19T11:45:00+01:00"
    assert event.timezone == "Europe/London"
    assert event.location == "Café Granger"
    assert event.attendees == ("alex@example.com",)
    assert event.organizer_is_self is True
    assert event.recurring is False


def test_get_event_flags_an_event_organised_by_somebody_else():
    event = client(FakeCalendar(raw_event(organiser_self=False))).get_event(EVENT_ID)
    assert event.organizer_is_self is False


def test_get_event_flags_a_recurring_instance():
    event = client(FakeCalendar(raw_event(recurring=True))).get_event(EVENT_ID)
    assert event.recurring is True


def test_list_events_skips_cancelled_and_asks_for_the_requested_window():
    items = [raw_event(), {**raw_event(), "id": "evt-cancelled", "status": "cancelled"}]
    fake = FakeCalendar(list_items=items)
    events = client(fake).list_events("2026-09-19T00:00:00+01:00", "2026-09-20T00:00:00+01:00")
    assert [e.id for e in events] == [EVENT_ID]
    q = parse_qs(urlparse(str(fake.calls("GET")[0].url)).query)
    assert q["timeMin"] == ["2026-09-19T00:00:00+01:00"]
    assert q["timeMax"] == ["2026-09-20T00:00:00+01:00"]
    assert q["singleEvents"] == ["true"], "recurring events must not be expanded into the day view"


def test_free_busy_returns_busy_intervals():
    intervals = client(FakeCalendar()).free_busy("2026-09-19T00:00:00Z", "2026-09-20T00:00:00Z")
    assert [(b.start, b.end) for b in intervals] == [
        ("2026-09-19T16:30:00Z", "2026-09-19T17:15:00Z")]


def test_free_busy_surfaces_a_provider_error_instead_of_reporting_an_empty_day():
    fake = FakeCalendar(freebusy_errors=[{"domain": "global", "reason": "notFound"}])
    with pytest.raises(gc.CalendarError) as err:
        client(fake).free_busy("2026-09-19T00:00:00Z", "2026-09-20T00:00:00Z")
    assert "notFound" in str(err.value)


# ------------------------------------------------------------------ the conditional update ----
def test_update_event_sends_if_match_and_returns_the_read_back_event():
    fake = FakeCalendar()
    updated = client(fake).update_event(EVENT_ID, ETAG_V1, start="2026-09-19T15:00:00+01:00",
                                        end="2026-09-19T15:45:00+01:00",
                                        location="Granger & Co, King's Cross",
                                        send_updates="all")
    patch = fake.patches[0]
    assert patch["if_match"] == ETAG_V1
    assert patch["params"]["sendUpdates"] == ["all"]
    assert patch["body"]["start"] == {"dateTime": "2026-09-19T15:00:00+01:00",
                                      "timeZone": "Europe/London"}
    assert patch["body"]["end"]["dateTime"] == "2026-09-19T15:45:00+01:00"
    assert patch["body"]["location"] == "Granger & Co, King's Cross"
    assert updated.etag == ETAG_V2, "the caller must be handed the new version"
    assert updated.start == "2026-09-19T15:00:00+01:00"
    assert updated.location == "Granger & Co, King's Cross"
    assert len(fake.calls("GET")) == 2, "one read to validate, one read back after the write"


def test_a_stale_etag_stops_the_write_with_etag_mismatch():
    fake = FakeCalendar(patch_status=412)
    with pytest.raises(EtagMismatch):
        client(fake).update_event(EVENT_ID, '"etag-reviewed-an-hour-ago"',
                                  start="2026-09-19T15:00:00+01:00",
                                  end="2026-09-19T15:45:00+01:00", location="Somewhere",
                                  send_updates="all")
    assert len(fake.patches) == 1


def test_no_conditional_header_means_no_write():
    fake = FakeCalendar()
    with pytest.raises(ValueError):
        client(fake).update_event(EVENT_ID, "", start="2026-09-19T15:00:00+01:00",
                                  end="2026-09-19T15:45:00+01:00", location="x",
                                  send_updates="all")
    assert fake.calls("PATCH") == [], "an unconditional calendar write must be impossible here"


def test_a_recurring_event_is_refused_before_anything_is_written():
    fake = FakeCalendar(raw_event(recurring=True))
    with pytest.raises(gc.CalendarError) as err:
        client(fake).update_event(EVENT_ID, ETAG_V1, start="2026-09-19T15:00:00+01:00",
                                  end="2026-09-19T15:45:00+01:00", location="x",
                                  send_updates="all")
    assert "recurring" in str(err.value).lower()
    assert fake.calls("PATCH") == []


def test_an_event_the_owner_does_not_organise_is_refused_before_anything_is_written():
    fake = FakeCalendar(raw_event(organiser_self=False))
    with pytest.raises(gc.CalendarError) as err:
        client(fake).update_event(EVENT_ID, ETAG_V1, start="2026-09-19T15:00:00+01:00",
                                  end="2026-09-19T15:45:00+01:00", location="x",
                                  send_updates="all")
    assert "organis" in str(err.value).lower()
    assert fake.calls("PATCH") == []


def test_send_updates_is_passed_through_and_never_invented():
    fake = FakeCalendar()
    client(fake).update_event(EVENT_ID, ETAG_V1, start="2026-09-19T15:00:00+01:00",
                              end="2026-09-19T15:45:00+01:00", location="x",
                              send_updates="none")
    assert fake.patches[0]["params"]["sendUpdates"] == ["none"]


# ------------------------------------------------------------------ auth / errors --------------
def test_a_401_refreshes_once_and_then_retries():
    fake = FakeCalendar(one_401=True)
    tokens = Tokens()
    tokens.refresh = lambda: setattr(tokens, "refreshes", tokens.refreshes + 1)
    event = gc.GCalClient("you", http=fake.client, tokens=tokens).get_event(EVENT_ID)
    assert event.id == EVENT_ID
    assert tokens.refreshes == 1


def test_a_401_that_survives_the_refresh_requires_reconnecting():
    fake = FakeCalendar(always_401=True)
    tokens = Tokens()
    tokens.refresh = lambda: None
    with pytest.raises(ReauthRequired):
        gc.GCalClient("you", http=fake.client, tokens=tokens).get_event(EVENT_ID)


def test_a_404_is_reported_as_a_calendar_error():
    fake = FakeCalendar()
    with pytest.raises(gc.CalendarError) as err:
        client(fake).get_event("no-such-event")
    assert "404" in str(err.value)


def test_no_token_value_reaches_the_logs(caplog):
    fake = FakeCalendar()
    g = gc.GCalClient("you", http=fake.client, tokens=Tokens("ya29.CAL-SECRET"))
    with caplog.at_level(logging.DEBUG):
        g.get_event(EVENT_ID)
    combined = caplog.text + "\n".join(r.getMessage() for r in caplog.records)
    assert "CAL-SECRET" not in combined
