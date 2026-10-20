"""In-memory fakes for the v2 engine ("Handle the change").

Every provider the engine can reach is faked here and every fake RECORDS what it was asked to
carry. That is the whole point: the privacy test is not "the UI hid the title", it is "no
provider-bound payload ever contained it", and that assertion needs a place where the bytes
actually accumulate. `Recorder` is that place.

No network. The model is an httpx.MockTransport; mail, calendar and search are plain objects.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import sqlite3

import httpx

from privatemesh.config import Settings
from privatemesh.llm import TokenFactory
from privatemesh.wiring import _ChangeResolver
from privatemesh.ports import (
    BusyInterval,
    CalendarEvent,
    EtagMismatch,
    Evidence,
    MailMessage,
    SentMail,
)

S = Settings(api_key="k", base_url="https://tf.test/v1/", model_fast="nano",
             model_reason="super", model_deep="ultra", tavily_key="tvly-test")


class _FixtureChanges(_ChangeResolver):
    """The engine's changes, plus the one this fixture's change set is about.

    Two ways a change exists here, and both are real. A test that drives the engine — `detect`,
    then `prepare` — gets the change the engine built, and the reply's audience is bound to it. A
    test that wants only the executor calls `approved_changeset()`, which plans a change with the
    ids this fixture's thread already uses ("c1"/"m1"/"t1"); that change is registered here, as a
    real `Change`, because a change set about a change the engine has never seen is not a state the
    product can be in. Registering it keeps `test_execute.py` on the real binding instead of on a
    stub that would answer whatever the test wanted to hear."""

    def __init__(self) -> None:
        super().__init__()
        self.registered: dict = {}
        # The approved arguments the ports are bound to, attached by `Harness` once the change-set
        # store exists — the same two-step `wiring._build` uses, and the same reason for it. Until
        # it is attached, `for_change` answers "no bundle" and both ports refuse, which is what
        # makes a test that forgot to attach it a refusal rather than a silent pass.
        self.bundles = None

    def for_change(self, change_id: str):
        if self.bundles is None:
            return None
        return self.bundles.for_change(change_id)

    def register(self, change) -> None:
        self.registered[change.id] = change

    def get(self, change_id: str):
        return super().get(change_id) or self.registered.get(change_id)

TZ = "+01:00"


def at(hhmm: str, date: str = "2026-09-19") -> str:
    """An ISO-8601 instant with the offset, e.g. at("16:30") -> 2026-09-19T16:30:00+01:00."""
    return f"{date}T{hhmm}:00{TZ}"


# ------------------------------------------------------------------ capture --------------------
class Recorder:
    """Every provider-bound payload, in order, with the surface it left through."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def add(self, surface: str, payload) -> None:
        self.entries.append({"surface": surface, "payload": payload})

    def surfaces(self, *names: str) -> list[dict]:
        return [e for e in self.entries if e["surface"] in names]

    def text(self, *names: str) -> str:
        """Everything captured, serialised — markers cannot hide in a nested field."""
        chosen = self.surfaces(*names) if names else self.entries
        return json.dumps([e["payload"] for e in chosen], ensure_ascii=False, default=str)

    def find(self, needle: str, *names: str) -> list[dict]:
        """The entries that contain `needle` anywhere, so a failure can name the surface that
        leaked rather than only that something did."""
        chosen = self.surfaces(*names) if names else self.entries
        return [e for e in chosen
                if needle.lower() in json.dumps(e["payload"], ensure_ascii=False,
                                                default=str).lower()]


# ------------------------------------------------------------------ model ----------------------
class ScriptedTF:
    """A scripted Token Factory that captures every request body.

    Routes on the system-prompt marker, the same way `conftest.FakeTF` does, so a test reads as
    the flow rather than as an httpx handler.
    """

    def __init__(self, recorder: Recorder, *, classify=None, extract=None, query=None,
                 choose=None, draft=None, answer=None, raw: dict | None = None,
                 settings=None) -> None:
        from privatemesh import changes as ch
        # The model IDS are settings, and a test that wants to price a call has to be able to name
        # a priced model. Defaulting to the module constant keeps every existing caller identical.
        self.settings = settings or S
        self.recorder = recorder
        self.marks = {"classify": ch.CLASSIFY_SYSTEM, "extract": ch.EXTRACT_SYSTEM,
                      "query": ch.QUERY_SYSTEM, "choose": ch.CHOOSE_SYSTEM,
                      "draft": ch.DRAFT_SYSTEM}
        self.script = {"classify": classify, "extract": extract, "query": query,
                       "choose": choose, "draft": draft}
        self.answer = answer or "ok"
        self.raw = raw or {}
        self.calls: list[dict] = []
        self.tf = TokenFactory(self.settings,
                               httpx.Client(base_url=self.settings.base_url,
                                            transport=httpx.MockTransport(self._handler)))

    def bind(self, gate) -> TokenFactory:
        """The same client, now behind the privacy gate — what the engine actually uses."""
        self.tf.boundary = gate
        return self.tf

    def _phase(self, system: str) -> str:
        for name, mark in self.marks.items():
            if system.startswith(mark):
                return name
        return "answer"

    def _handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        self.recorder.add("model", body)
        system = next((m.get("content", "") for m in body["messages"]
                       if m.get("role") == "system"), "")
        phase = self._phase(system)
        self.calls.append({"phase": phase, "model": body["model"]})
        scripted = self.raw.get(phase) or self.script.get(phase)
        if isinstance(scripted, list):        # a phase answered differently on each call
            scripted = scripted.pop(0) if len(scripted) > 1 else scripted[0]
        content = scripted if isinstance(scripted, str) else json.dumps(
            scripted if scripted is not None else {})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})

    def phases(self) -> list[str]:
        return [c["phase"] for c in self.calls]


# ------------------------------------------------------------------ mail -----------------------
class FakeMail:
    def __init__(self, recorder: Recorder, messages: list[MailMessage] | None = None,
                 threads: dict[str, list[MailMessage]] | None = None) -> None:
        self.recorder = recorder
        self.messages = list(messages or [])
        self.threads = dict(threads or {})
        self.sent: list[dict] = []
        self.cursor = ""
        self.fail_send: Exception | None = None   # a timeout-shaped send, for reconciliation
        self.ghost_send = False                   # the send LANDED but the response was lost
        self.polls = 0                            # how many times the watcher asked

    def list_new(self, since_cursor: str | None) -> tuple[list[MailMessage], str]:
        self.polls += 1
        new = [m for m in self.messages if m.id > (since_cursor or "")]
        return sorted(new, key=lambda m: m.id), (new[-1].id if new else (since_cursor or ""))

    def get_thread(self, thread_id: str) -> list[MailMessage]:
        return list(self.threads.get(thread_id, []))

    def send_reply(self, in_reply_to: MailMessage, to: tuple[str, ...], subject: str,
                   body_text: str, idempotency_key: str) -> SentMail:
        call = {"to": list(to), "subject": subject, "body": body_text,
                "idempotency_key": idempotency_key, "in_reply_to": in_reply_to.id}
        self.recorder.add("mail", call)
        if self.fail_send is not None:
            if self.ghost_send:            # the provider took it; only the answer was lost
                self.sent.append({**call, "id": f"sent-{len(self.sent) + 1}"})
                raise self.fail_send
            raise self.fail_send
        self.sent.append({**call, "id": f"sent-{len(self.sent) + 1}"})
        return SentMail(id=self.sent[-1]["id"], thread_id=in_reply_to.thread_id,
                        rfc822_message_id=f"<{idempotency_key}@example.com>")

    def find_sent(self, idempotency_key: str) -> SentMail | None:
        for s in self.sent:
            if s["idempotency_key"] == idempotency_key:
                return SentMail(id=s["id"], thread_id="t1",
                                rfc822_message_id=f"<{idempotency_key}@example.com>")
        return None


# ------------------------------------------------------------------ calendar -------------------
def _instant(stamp: str):
    """An ISO-8601 instant, or None when the stamp is not one. Used by `FakeCalendar.list_events`
    so the fake answers about the window it was asked for rather than about everything it holds."""
    try:
        return dt.datetime.fromisoformat(str(stamp or ""))
    except (TypeError, ValueError):
        return None


class FakeCalendar:
    def __init__(self, recorder: Recorder, events: list[CalendarEvent] | None = None,
                 busy: list[BusyInterval] | None = None) -> None:
        self.recorder = recorder
        self.events = {e.id: e for e in (events or [])}
        self.busy = list(busy or [])
        self.etag_seq = 0
        self.fail_update: Exception | None = None
        self.ignores_update = False   # answers 200 and applies nothing: what read-back is for

    def get_event(self, event_id: str) -> CalendarEvent:
        ev = self.events.get(event_id)
        if ev is None:
            raise KeyError(event_id)
        return ev

    def list_events(self, day_start: str, day_end: str) -> list[CalendarEvent]:
        # THE WINDOW IS HONOURED, because a fake that ignores it cannot fail a check that depends on
        # one. The fifth review's second finding is exactly that: a `-12:00` stamp moved the queried
        # window half a day, the day's other meetings fell outside it, and the empty answer read as
        # innocence. A calendar that answers with everything it holds regardless of the window
        # reports that failure as held — the mutation controls for round 6 went green against it —
        # so the fake filters by the event's own start against the half-open window it was asked
        # for. A stamp nothing can place is NOT filtered: this is a fixture, and an unparseable
        # window is the caller's problem rather than a silent empty day.
        start, end = _instant(day_start), _instant(day_end)
        out = []
        for event in self.events.values():
            when = _instant(getattr(event, "start", ""))
            if start is None or end is None or when is None or start <= when < end:
                out.append(event)
        return out

    def free_busy(self, start: str, end: str) -> list[BusyInterval]:
        return list(self.busy)

    def update_event(self, event_id: str, if_match_etag: str, *, start: str, end: str,
                     location: str, send_updates: str, timezone: str = "") -> CalendarEvent:
        # `timezone` is RECORDED rather than applied, because the field under test is what the port
        # hands over — a fake that quietly substituted the event's own zone would report the
        # approved zone on the wire whatever the port passed.
        call = {"event_id": event_id, "if_match": if_match_etag, "start": start, "end": end,
                "location": location, "send_updates": send_updates, "timezone": timezone}
        self.recorder.add("calendar", call)
        if self.fail_update is not None:
            raise self.fail_update
        ev = self.events.get(event_id)
        if ev is None:
            raise KeyError(event_id)
        if if_match_etag != ev.etag:
            raise EtagMismatch(f"event {event_id} changed (etag {ev.etag!r} != {if_match_etag!r})")
        if self.ignores_update:
            return ev

        self.etag_seq += 1
        updated = CalendarEvent(id=ev.id, etag=f"etag-{self.etag_seq + 1}", summary=ev.summary,
                                start=start, end=end, timezone=ev.timezone, location=location,
                                attendees=ev.attendees, organizer_is_self=ev.organizer_is_self,
                                recurring=ev.recurring,
                                # An update does not change who organizes the event, and a fake
                                # that dropped this would answer the next clause-3 read with an
                                # audience it could no longer account for.
                                organizer_address=ev.organizer_address)
        self.events[event_id] = updated
        return updated


# ------------------------------------------------------------------ search ---------------------
class FakeSearch:
    """A search port over fixed results, and honest about being one: `is_sample` is what makes the
    evidence the engine builds say `"sample": true`, which is what the UI labels a source from."""

    is_sample = True

    def __init__(self, recorder: Recorder,
                 results: dict[str, list[Evidence]] | None = None,
                 default: list[Evidence] | None = None) -> None:
        self.recorder = recorder
        self.results = dict(results or {})
        self.default = list(default or [])
        self.queries: list[str] = []

    def search(self, query: str, *, max_results: int = 5,
               include_domains: tuple[str, ...] = ()) -> list[Evidence]:
        self.queries.append(query)
        self.recorder.add("search", {"query": query, "include_domains": list(include_domains)})
        for needle, ev in self.results.items():
            if needle.lower() in query.lower():
                return ev[:max_results]
        return self.default[:max_results]


def evidence(url: str, title: str, excerpt: str, *, official: bool = True) -> Evidence:
    return Evidence(url=url, title=title, excerpt=excerpt, retrieved_at=at("09:00"), official=official)


def change_email(mid: str = "m2", *, sender: str = "Alex Doe <alex@example.com>",
                 subject: str = "Our usual cafe has closed",
                 body: str = "Could we meet near King's Cross tomorrow at 3 or 4? "
                             "You choose and update the invite.") -> MailMessage:
    return MailMessage(id=mid, thread_id="t1", sender=sender, to=("me@example.com",),
                       subject=subject, body_text=body, received_at=at("08:05"),
                       rfc822_message_id=f"<{mid}@example.com>")


def invite(event_id: str = "ev1", *, start: str = "15:00", end: str = "15:45",
           etag: str = "etag-1") -> CalendarEvent:
    return CalendarEvent(id=event_id, etag=etag, summary="Alex Doe and You",
                         start=at(start), end=at(end), timezone="Europe/London",
                         location="Old Cafe, King's Cross", attendees=("alex@example.com",))


def busy(start: str, end: str) -> BusyInterval:
    return BusyInterval(start=at(start), end=at(end))


def now() -> dt.datetime:
    return dt.datetime.fromisoformat(at("09:00"))


# ------------------------------------------------------------------ the wiring ------------------
COMMITMENT = {"title": "PC_TITLE_Marker_School pickup for Ada",
              "location": "PC_LOCATION_Marker_42 Conservatory Lane",
              "reason": "PC_REASON_Marker_Ada piano recital at the conservatory",
              "attendees": ("pc_marker_attendee_ada@example.com",)}

#: The canonical day: a 45-minute meeting organized by the owner, a protected commitment at
#: 16:30, and the owner's own 45-minute buffer. 16:00 is the option that must lose.
DAY = {"start": at("16:30"), "end": at("17:30"), "buffer_minutes": 45}


class Harness:
    """The whole engine, wired to fakes, with every provider-bound payload recorded.

    One object rather than a pile of fixtures because the privacy claim is about the RUN: the
    recorder has to survive the same lifetime as the engine that filled it.
    """

    def __init__(self, *, messages=None, threads=None, events=None, busy=None, search=None,
                 script=None, commitment=None, day=None, branch="build/kernel2",
                 data_dir=None, models: dict | None = None) -> None:
        """`data_dir` puts the three stores that outlive a process on disk, the way `wiring._build`
        does — the change records, the change sets and the approvals. Without it everything is in
        memory, which is what every other test wants. The boundary stays in memory either way:
        `_build` receives it rather than opening it, so a restart test that re-put the commitment
        would be testing the boundary's own versioning, not the change records this file is for."""
        from pathlib import Path

        from privatemesh.approval import ApprovalStore
        from privatemesh.audit import AuditLog
        from privatemesh.boundary import BoundaryStore, GuardedSearch, PrivacyGate
        from privatemesh.broker import Broker
        from privatemesh.changes import Change, ChangeEngine, ChangeStore
        from privatemesh.changeset import ChangeSetStore
        from privatemesh.execute import Executor, build_effect_skills
        from privatemesh.limits import LimitStore
        from privatemesh.routine import Routine
        from privatemesh.skills import SkillRegistry

        def db(name: str) -> str | None:
            if data_dir is None:
                return None
            Path(data_dir).mkdir(parents=True, exist_ok=True)
            return str(Path(data_dir) / name)

        self.rec = Recorder()
        self.audit = AuditLog(":memory:")
        self.boundary = BoundaryStore()
        self.commitment = self.boundary.put(id="pc1", **(day or DAY), **(commitment or COMMITMENT))
        self.gate = PrivacyGate(self.boundary, audit=self.audit)
        # `tf` is the SCRIPTED client (it knows which phase each call was, and how many), and
        # `llm` is the same client behind the gate — what the engine is handed.
        import dataclasses
        settings = S if not models else dataclasses.replace(
            S, **{f"model_{tier}": name for tier, name in models.items()})
        # DEEP-COPIED, and this is not tidiness. A phase whose script is a LIST is consumed with
        # `pop(0)` — "answer differently on each call" — and a list held in a module-level SCRIPT
        # constant is therefore drained by whichever test ran first. Every later test in the file
        # then gets a different script than the one written down, which is a test that measures the
        # order it ran in. It stayed invisible while the only consequence was a slightly different
        # venue name; round 6 makes an index with no fetched result a REFUSAL, and the drained
        # script turned that into eleven failures that pass one at a time.
        self.tf = ScriptedTF(self.rec, settings=settings, **copy.deepcopy(script or {}))
        self.llm = self.tf.bind(self.gate)
        self.mail = FakeMail(self.rec, messages=messages,
                             threads=threads if threads is not None
                             else {"t1": [change_email("m1")]})
        self.calendar = FakeCalendar(self.rec, events=events if events is not None else [invite()],
                                     busy=busy or [])
        self.search = FakeSearch(self.rec, results=search)
        self.approvals = ApprovalStore(path=db("approvals.db"))
        # The same two-phase binding the composition root uses: the resolver exists before the
        # engine, because the send skill is built from the registry and needs it, and is attached
        # after. Unattached it answers "no such change", which permits no recipient at all.
        self.change_resolver = _FixtureChanges()
        self.registry = SkillRegistry(build_effect_skills(self.calendar, self.mail, gate=self.gate,
                                                          changes=self.change_resolver,
                                                          bundles=self.change_resolver,
                                                          timezone=at("15:00")[-6:]))
        self.broker = Broker(self.registry, self.audit, approvals=self.approvals)
        # One connection for both stores, the change-set file ATTACHed — the same wiring the
        # composition root uses, and for the same reason: a change's version and the bundle it
        # points at have to be one commit, and SQLite transactions are per-connection.
        store_conn = sqlite3.connect(db("changes.db") or ":memory:", check_same_thread=False,
                                     timeout=5.0)
        store_conn.execute("ATTACH DATABASE ? AS change_sets",
                           (db("changesets.db") or ":memory:",))
        self.changesets = ChangeSetStore(self.approvals, conn=store_conn, schema="change_sets")
        self.changes = ChangeStore(conn=store_conn)
        # The decision log, built BEFORE the engine because the engine is handed it — `wiring._build`
        # does both, and this harness did not: without it every `ChangeEngine._note` was a silent
        # no-op, so the prepare-and-validate half of a change's spine (the half the owner reads
        # BEFORE approving) could not be asserted at all. A harness that cannot see a row reports a
        # missing row as a passing test, which is the failure mode this file exists to avoid.
        from privatemesh.decisions import DecisionLog
        self.decisions = DecisionLog(now=lambda: dt.datetime.fromisoformat(at("09:00")))
        self.engine = ChangeEngine(mail=self.mail, calendar=self.calendar,
                                   search=GuardedSearch(self.search, self.gate), llm=self.llm,
                                   gate=self.gate, boundary=self.boundary, broker=self.broker,
                                   changesets=self.changesets, audit=self.audit,
                                   store=self.changes, decisions=self.decisions,
                                   now=lambda: dt.datetime.fromisoformat(at("09:00")))
        self.change_resolver.attach(self.engine)
        # The approved arguments the ports are bound to. Attached here rather than passed to
        # `build_effect_skills` because the store is built after the registry — the same order, and
        # the same two-step, as `wiring._build`.
        self.change_resolver.bundles = self.changesets
        self.change_resolver.register(Change(
            id="c1", thread_id="t1", message_id="m1", from_name="Alex Doe",
            from_address="alex@example.com", subject="Our usual cafe has closed",
            excerpt="Could we meet near King's Cross tomorrow at 3 or 4?",
            received_at=at("08:05"),
            # The correspondent, captured and validated when the change was DETECTED. Every
            # change a real detection raises carries one, and the reply's audience is checked
            # against it rather than against a record the provider can rewrite — a fixture change
            # without it would refuse every send, which is the fail-closed behaviour and not
            # something these tests are about.
            sender_pinned="alex@example.com",
            # The three the ENGINE always writes together, and for the same reason as `thread_id`
            # and `change_id` above: `_prepare` pins the meeting this change is about, records the
            # day it lands on, and mints the slot, and a reply missing any of them is not a reply
            # the engine built. `execute.send_reply` now fails CLOSED on that — the fourth review
            # sent an approved bundle with no pin and the foreign-event check never ran — so a
            # fixture that omitted them would be refused, and refused is the right behaviour for a
            # producer that does not exist."""
            event_pinned="ev1", plan_day="2026-09-19",
            proposal={"start": at("15:00"), "end": at("15:45")},
            # The reply's own decomposition, and the fifth review's first finding is why a fixture
            # without one is no longer a simpler fixture: the port rebuilds the message the bundle
            # CLAIMS to be from these two fields and refuses a body that is not their render. A
            # bundle whose reply has no declared pieces has no engine-composed reply at all, so it
            # is refused — correctly, and for the same reason `sender_pinned` and `event_pinned`
            # are here. These render exactly `email_args()`'s default body: "Hi Alex," plus the
            # time fragment, joined with a space.
            reply_template="Hi Alex,", reply_template_source="template",
            reply_fragments=[{"source": "time", "text": "15:00-15:45 works for me.",
                              "event_id": "ev1"}],
            # The reply SUBJECT's pieces, which the engine writes beside the body's and which the
            # port rebuilds the render from. `email_args()`'s default subject is their render, so
            # this fixture states the same reply the real producer would — and a fixture that left
            # them out would be refused at the port, which is the sixth review's first finding
            # working rather than a fixture inconvenience.
            reply_subject_template="Re:",
            reply_subject_fragments=[{"source": "thread_subject",
                                      "text": "Our usual cafe has closed"}],
            # The venue catalogue this change selected from, and the record it selected. The port
            # refuses a calendar write that carries a location with no catalogue behind it — the
            # sixth review's fourth finding is that "no record to compare against" was being
            # reported as "the value was checked" — so a change without one is not a simpler
            # fixture, it is a plan whose `location` nothing rendered. `render_venue` of the record
            # below is exactly `calendar_args()`'s default location.
            venue_catalogue=[{"id": "v1", "name": "The Standard", "address": "King's Cross",
                              "url": "https://example.com/the-standard",
                              "title": "The Standard, King's Cross",
                              "excerpt": "A cafe near King's Cross."}],
            venue_id="v1"))
        # The owner's standing answer, and the ONE store the executor re-checks a grant against.
        # Building it here rather than inside `v2()` is what makes that true: the executor is
        # constructed once, so a service handed a second store would leave the re-check reading a
        # different mode from the one the run was authorized under — the exact split the grant
        # exists to close.
        self.limits = LimitStore(now=lambda: dt.datetime.fromisoformat(at("09:00")))
        # What the owner agreed to. Built the way `wiring._build` builds it — a file under the data
        # dir when there is one, `:memory:` otherwise — so the restart test exercises the same
        # persistence the product has rather than a second implementation of it.
        from privatemesh.agreements import AgreementStore
        self.agreements = AgreementStore(path=db("agreements.db"),
                                         now=lambda: dt.datetime.fromisoformat(at("09:00")))
        # `self.decisions` is built above, before the engine: the engine and the executor are handed
        # the SAME log, which is what `wiring._build` does. Two logs would make "the decision log
        # records it" a claim about whichever one the reader happened to open.
        self.executor = Executor(broker=self.broker, changesets=self.changesets,
                                 calendar=self.calendar, mail=self.mail, audit=self.audit,
                                 boundary=self.boundary, limits=self.limits, changes=self.engine,
                                 decisions=self.decisions, agreements=self.agreements,
                                 # The owner's zone, so the digest's day window is built the way
                                 # `wiring._build` builds it rather than off the host's clock.
                                 timezone=at("15:00")[-6:])
        self.routine = Routine(self.engine, self.mail,
                               now=lambda: dt.datetime.fromisoformat(at("09:00")))

    def v2(self, **kw):
        """The API service over this harness — the same engine the tests drive directly.

        `authenticated_owner` is set here, explicitly, and not left to `V2Service`'s default, which
        is now CLOSED. This harness builds the SAMPLE product, and the sample's owner is the person
        running it — the same claim `wiring.build_sample` makes in the same words. A test that
        wants the closed behaviour passes `authenticated_owner=False`, which now means what it says.
        """
        from privatemesh.api_v2 import V2Service
        kw.setdefault("mode", "sample")
        kw.setdefault("authenticated_owner", True)
        # The harness's own store, unless a caller names one — and a caller that does must hand the
        # SAME object to the executor, or the mode the service authorizes under and the mode the
        # executor re-checks are two different rows.
        kw.setdefault("limits", self.limits)
        # Same store the executor writes to, for the same reason as the limits store above: the
        # service must serve what the run recorded, not a second empty one.
        kw.setdefault("agreements", self.agreements)
        return V2Service(engine=self.engine, routine=self.routine, boundary=self.boundary,
                         changesets=self.changesets, executor=self.executor,
                         calendar=self.calendar, **kw)

    # ---- a change set, without paying for a whole preparation --------------------------------
    def calendar_args(self, **over) -> dict:
        # `change_id` is here because the engine's `_prepare` always sets it, and the port is bound
        # to the APPROVED bundle for that change — `event_id`, `start`, `end`, `location`,
        # `send_updates` and `if_match_etag` are compared against it before anything is written. A
        # fixture that omitted it would model a producer that does not exist and the write would be
        # refused, correctly. Same argument as `thread_id` on `email_args` below.
        # `timezone` is here because the engine's `_prepare` always writes it — off the event it
        # pinned — and the port is bound to the APPROVED bundle for it like the times are. A
        # fixture without one models a producer that does not exist: the real bundle carries the
        # zone, so the port would compare the engine's own value against a plan that never had one
        # and refuse, which is the binding working rather than a fixture inconvenience.
        args = {"event_id": "ev1", "if_match_etag": self.calendar.get_event("ev1").etag,
                "start": at("15:00"), "end": at("15:45"),
                "location": "The Standard, King's Cross", "send_updates": "all",
                "timezone": self.calendar.get_event("ev1").timezone,
                "change_id": "c1"}
        args.update(over)
        return args

    def email_args(self, **over) -> dict:
        # `thread_id` and `change_id` are here because the engine's `_prepare` always sets both. The
        # executor's recipient check reads the thread back from the port to establish who wrote, and
        # the gate files its recipient decision against the change — so a fixture that omits either
        # is not a simpler fixture, it is one that models a producer that does not exist, and the
        # omission is invisible: the send is still correctly refused, but the row that explains it
        # lands in `DecisionLog.orphans()` instead of on the change the owner is looking at.
        #
        # `rfc822_message_id` is here for the same reason and is the sixth review's second finding
        # seen from the fixture side: `_prepare` reads it off the thread (`ChangeEngine._rfc822`) and
        # the port now requires it to equal BOTH the approved value and the provider's own copy of
        # the pinned message, because `In-Reply-To` and `References` are built from it. `<m1@...>`
        # is what `message("m1")` serves, so this is the value the real producer would have written.
        args = {"to": ["alex@example.com"], "subject": "Re: Our usual cafe has closed",
                "body": "Hi Alex, 15:00-15:45 works for me.",
                "in_reply_to": "m1", "thread_id": "t1", "change_id": "c1",
                "rfc822_message_id": "<m1@example.com>",
                "idempotency_key": "idem-c1-v1"}
        args.update(over)
        return args

    def pending_changeset(self, **over):
        # The effect arguments are replaceable so a test can plan a different send — a different
        # audience, a different thread — without rebuilding the fixture. Everything else is passed
        # through to the store as before.
        cal_args = over.pop("calendar_args", None) or self.calendar_args()
        mail_args = over.pop("email_args", None) or self.email_args()
        cal = self.approvals.request("task1", "calendar_update_event", cal_args)
        mail = self.approvals.request("task1", "email_reply", mail_args)
        # The display is built by the REAL producer, from the event the calendar actually serves —
        # that is what `prepare` does, and `notifies` (who the invite update would email) is only
        # knowable from there. A fixture that left it out would hand the review screen a bundle
        # that cannot say who it emails, which is the state the review's Critical was about.
        from privatemesh.changes import _calendar_display
        over.setdefault("calendar_display",
                        _calendar_display(self.calendar.get_event(cal_args["event_id"]), cal_args))
        return self.changesets.create(change_id="c1", task_id="task1",
                                      event_etag=self.calendar.get_event("ev1").etag,
                                      boundary_id="pc1",
                                      boundary_version=self.boundary.version_of("pc1"),
                                      calendar_args=cal_args,
                                      email_args=mail_args,
                                      approval_ids=(cal.id, mail.id), **over)

    def approved_changeset(self, **over):
        cs = self.pending_changeset(**over)
        return self.changesets.approve(cs.id, cs.version,
                                       current_event_etag=self.calendar.get_event("ev1").etag,
                                       current_boundary_version=self.boundary.version_of("pc1"))
