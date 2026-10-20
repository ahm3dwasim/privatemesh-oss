"""Google Calendar over the REST API with plain httpx.

The engine treats an external write as non-atomic and never trusts its own memory of an event, so
this connector is built around two rules:

* **Conditional or nothing.** `update_event` refuses an empty `if_match_etag` outright — there is
  no code path here that overwrites an event the owner has not reviewed. A `412` becomes
  `ports.EtagMismatch`, which stops execution and demands a fresh review.
* **v1 changes only what it can defend.** Recurring events and events the owner does not organise
  are refused with a clear reason, before a request is sent, because "move this week's instance"
  and "move a meeting somebody else owns" are different promises than the one the product makes.

Reads stay permissive (an event is still *readable* when it is recurring — refusing to describe
it would only make the UI worse). The refusal lives on the mutation path.
"""
from __future__ import annotations

import logging

import httpx

from .ports import BusyInterval, CalendarEvent, EtagMismatch, ReauthRequired
from typing import Protocol

log = logging.getLogger(__name__)

CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
DEFAULT_CALENDAR = "primary"
SEND_UPDATES = ("all", "externalOnly", "none")


class CalendarError(RuntimeError):
    """Calendar refused the call; the message names the status and the provider's reason."""


class TokenProvider(Protocol):
    def access_token(self) -> str: ...


def _parse_event(raw: dict) -> CalendarEvent:
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    organizer = raw.get("organizer") or {}
    return CalendarEvent(
        id=str(raw.get("id") or ""),
        etag=str(raw.get("etag") or ""),
        summary=str(raw.get("summary") or ""),
        start=str(start.get("dateTime") or start.get("date") or ""),
        end=str(end.get("dateTime") or end.get("date") or ""),
        timezone=str(start.get("timeZone") or raw.get("timeZone") or "UTC"),
        location=str(raw.get("location") or ""),
        attendees=tuple(str(a.get("email") or "") for a in raw.get("attendees") or ()
                        if a.get("email")),
        organizer_is_self=bool(organizer.get("self")),
        # Only when the provider says the organizer IS the authenticated user. A foreign organizer
        # has an address too, and taking it would put a stranger's address in the permitted
        # audience of clause 3 — the clause refuses an event this calendar does not organize, and
        # an address it never read is what keeps "the owner" from meaning "whoever is organizing".
        organizer_address=(str(organizer.get("email") or "")
                           if organizer.get("self") else ""),
        # A single instance of a series carries recurringEventId; the series itself carries
        # recurrence. Both mean "not a plain event", and both are refused on the write path.
        recurring=bool(raw.get("recurrence") or raw.get("recurringEventId")),
    )


class GCalClient:
    """Implements `ports.CalendarPort` for one account."""

    def __init__(self, account: str = "you", *, http: httpx.Client | None = None,
                 tokens: TokenProvider | None = None, calendar_id: str = DEFAULT_CALENDAR,
                 base_url: str = CALENDAR_BASE):
        self.account = account
        self.calendar_id = calendar_id
        self._base = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=30)
        self._tokens = tokens

    # -------------------------------------------------------------- transport
    def _auth_header(self) -> dict:
        if self._tokens is None:
            raise CalendarError(f"no Google token provider for account '{self.account}'")
        return {"Authorization": f"Bearer {self._tokens.access_token()}"}

    def _send(self, method: str, url: str, *, headers: dict, params: dict | None,
              json_body: dict | None) -> httpx.Response:
        try:
            return self._http.request(method, url, headers=headers, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise CalendarError(f"Calendar {method} {url} failed: {exc!r}") from exc

    def _call(self, method: str, path: str, *, params: dict | None = None,
              json_body: dict | None = None, extra_headers: dict | None = None,
              at_root: bool = False) -> dict:
        # Events live under /calendars/{id}; free/busy lives at the API root. The first live run
        # sent free/busy under the calendar and got Google's HTML 404.
        url = f"{self._base}{path}" if at_root else f"{self._base}/calendars/{self.calendar_id}{path}"
        headers = self._auth_header()
        if extra_headers:
            headers.update(extra_headers)
        response = self._send(method, url, headers=headers, params=params, json_body=json_body)
        if response.status_code == 401:
            refresh = getattr(self._tokens, "refresh", None)
            if not callable(refresh):
                raise ReauthRequired(
                    f"Calendar rejected the token for '{self.account}' — reconnect Google")
            refresh()
            headers = self._auth_header()
            if extra_headers:
                headers.update(extra_headers)
            response = self._send(method, url, headers=headers, params=params, json_body=json_body)
            if response.status_code == 401:
                raise ReauthRequired(
                    f"Calendar rejected the refreshed token for '{self.account}' — reconnect "
                    f"Google (Testing-mode tokens expire after 7 days)")
        if response.status_code == 412:
            # Only ever reached by a conditional request, which is the only kind this class sends.
            raise EtagMismatch(
                "the event changed since it was reviewed (HTTP 412) — prepare the proposal again")
        if response.status_code >= 400:
            raise CalendarError(f"Calendar {method} {path} returned {response.status_code}: "
                                f"{self._reason(response)}")
        try:
            return response.json()
        except ValueError as exc:
            raise CalendarError(f"Calendar {method} {path} returned a non-JSON body") from exc

    @staticmethod
    def _reason(response: httpx.Response) -> str:
        try:
            error = response.json().get("error") or {}
        except ValueError:
            return (response.text or "")[:200]
        if isinstance(error, str):
            return error
        return str(error.get("message") or error.get("status") or "")[:300]

    # -------------------------------------------------------------- CalendarPort
    def get_event(self, event_id: str) -> CalendarEvent:
        return _parse_event(self._call("GET", f"/events/{event_id}"))

    def list_events(self, day_start: str, day_end: str) -> list[CalendarEvent]:
        data = self._call("GET", "/events", params={
            "timeMin": day_start, "timeMax": day_end,
            # singleEvents expands a series into instances; the day view needs the instances, and
            # the write path refuses them either way.
            "singleEvents": "true", "orderBy": "startTime", "maxResults": "250"})
        events = [_parse_event(raw) for raw in data.get("items") or ()
                  if str(raw.get("status") or "") != "cancelled"]
        return sorted(events, key=lambda e: e.start)

    def free_busy(self, start: str, end: str) -> list[BusyInterval]:
        data = self._call("POST", "/freeBusy", at_root=True, json_body={
            "timeMin": start, "timeMax": end, "items": [{"id": self.calendar_id}]})
        entry = ((data.get("calendars") or {}).get(self.calendar_id) or {})
        errors = entry.get("errors") or ()
        if errors:
            # An unreadable calendar is not a free calendar. Reporting "no conflicts" because the
            # provider refused to answer is the exact failure this product exists to avoid.
            raise CalendarError("free/busy could not be read for "
                                f"'{self.calendar_id}': "
                                + "; ".join(str(e.get("reason") or e) for e in errors))
        return [BusyInterval(start=str(b.get("start") or ""), end=str(b.get("end") or ""))
                for b in entry.get("busy") or ()]

    def update_event(self, event_id: str, if_match_etag: str, *, start: str, end: str,
                     location: str, send_updates: str, timezone: str = "") -> CalendarEvent:
        if not if_match_etag:
            raise ValueError(
                "update_event requires the reviewed etag: an unconditional calendar write is not "
                "something this connector will do")
        if send_updates not in SEND_UPDATES:
            raise ValueError(f"send_updates must be one of {SEND_UPDATES}, not {send_updates!r}")

        current = self.get_event(event_id)          # the read that justifies the refusal below
        if current.recurring:
            raise CalendarError(
                f"'{current.summary or event_id}' is part of a recurring series; this product only "
                f"moves single events in v1 — edit the series in Google Calendar instead")
        if not current.organizer_is_self:
            raise CalendarError(
                f"'{current.summary or event_id}' is organised by somebody else; only an event the "
                f"owner organises can be changed — ask the organiser instead")

        # THE ZONE IS THE APPROVED ONE, and the provider is asked to agree with it rather than
        # asked what it is. The engine carries the zone the plan was built in; this adapter used to
        # supply `current.timezone` itself, which put a field on the wire that no approval had ever
        # named — the one field that decides how the provider reads the approved instant.
        #
        # A mismatch REFUSES rather than picking a winner. Neither choice is safe to make here: the
        # approved zone with an event that no longer has it writes a wall clock the owner did not
        # see, and the provider's zone writes an interpretation nobody approved. "The event this
        # write is against has moved" is the honest reading, and it is the same class of answer as
        # `EtagMismatch` — which a zone change on the provider's side raises anyway.
        zone = str(timezone or "")
        served = str(current.timezone or "")
        if zone and served and zone != served:
            raise CalendarError(
                f"refused: '{current.summary or event_id}' is now in {served}, and the plan the "
                f"owner approved was written in {zone}. The zone decides which wall clock the "
                f"approved times land on, so a write carrying a zone nobody approved — or a zone "
                f"the meeting has moved out of — is not the write that was reviewed.")
        zone = zone or served
        self._call("PATCH", f"/events/{event_id}", params={"sendUpdates": send_updates},
                   extra_headers={"If-Match": if_match_etag},
                   json_body={"start": {"dateTime": start, "timeZone": zone},
                              "end": {"dateTime": end, "timeZone": zone},
                              "location": location})
        # Read it back rather than echoing the mutation response: the record the owner sees should
        # be what the provider actually stores, with the new etag.
        return self.get_event(event_id)


CalendarPort = GCalClient  # the name the lane brief uses for this implementation
