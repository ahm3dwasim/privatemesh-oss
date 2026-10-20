"""The sample product, end to end, over HTTP, driven only through `docs/API_V2.md`.

Every request below is one the web app can make. Nothing here reaches into the engine to force a
plan, sets a test flag to make the model answer, or inspects a store instead of a response —
because the thing being tested is the assembled product, and an assembled product is exactly what
two lanes testing themselves in isolation never check.

What is pinned:

* the whole hero flow: today's change, prepare, `proposal_ready`, 15:00 chosen with 16:00 refused
  by the boundary and one venue refused with its own evidence, approval, and both effects landing;
* the two refusals that keep an approval meaningful — an old version, and a boundary the owner
  edited after reviewing;
* the privacy claim, at the surface where it could actually fail: no outbound payload of the whole
  run carries the protected commitment's own words, and the recorder proves it was watching.

The single most important assertion in this file is that the recorder is NOT empty. "The marker
was absent" is worth nothing if nothing was captured.
"""
from __future__ import annotations

import dataclasses
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from fakes_v2 import Recorder
from privatemesh import wiring as wm
from privatemesh.api import default_app
from privatemesh.config import Settings
from privatemesh.llm import TokenFactory
from privatemesh.sim import SAMPLE_DATE, build_sample_ports

SETTINGS = Settings(api_key="sample-no-key", base_url="https://tf.test/v1/", model_fast="nano-fast",
                    model_reason="super-reason", model_deep="ultra-deep", tavily_key="")

#: The morning Alex's email arrives, and the day it is about: the email says "tomorrow at 3 or 4",
#: so the plan targets the 19th while the clock — and the change — are on the 18th. Both facts are
#: asserted, because a product that confused them would still look plausible.
DAY, PLAN_DAY = "2026-09-18", "2026-09-19"
MEETING_ID, COMMITMENT_ID = "evt-alex-sync", "prot-school-run"

#: The protected commitment's own words — its title, its location and its reason. One of these
#: leaving the device is the failure this whole product exists to prevent.
MARKERS = ("Maya", "St Mary", "School pickup", "16:45")
#: `MARKERS` minus the time. The window's TIMES are a planning fact and leave on purpose; the
#: title, the place and the reason are the commitment, and those never do.
CONTENT_MARKERS = MARKERS[:3]
SURFACES = ("model", "search", "email", "calendar")


# ---------------------------------------------------------------- recording ports --------------
class RecordingMail:
    """Records the exact fields handed to the provider, at the moment of the send."""

    def __init__(self, inner, recorder: Recorder) -> None:
        self.inner, self.recorder = inner, recorder

    def __getattr__(self, name):                 # reads and sample affordances pass straight through
        return getattr(self.inner, name)

    def send_reply(self, in_reply_to, to, subject, body_text, idempotency_key):
        self.recorder.add("email", {"to": [str(a) for a in to], "subject": subject,
                                    "body": body_text, "in_reply_to": in_reply_to.rfc822_message_id,
                                    "idempotency_key": idempotency_key})
        return self.inner.send_reply(in_reply_to, to, subject, body_text, idempotency_key)


class RecordingCalendar:
    def __init__(self, inner, recorder: Recorder) -> None:
        self.inner, self.recorder = inner, recorder

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def update_event(self, event_id, if_match_etag, *, start, end, location, send_updates,
                     timezone=""):
        self.recorder.add("calendar", {"event_id": event_id, "if_match": if_match_etag,
                                       "start": start, "end": end, "location": location,
                                       "send_updates": send_updates, "timezone": timezone})
        return self.inner.update_event(event_id, if_match_etag, start=start, end=end,
                                       location=location, send_updates=send_updates,
                                       timezone=timezone)


class RecordingSearch:
    def __init__(self, inner, recorder: Recorder) -> None:
        self.inner, self.recorder = inner, recorder

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def search(self, query, **kw):
        # The query TEXT is the leak a link-list product never notices: "near <the venue my
        # daughter's school is opposite>" is a disclosure even when no result is ever shown.
        self.recorder.add("search", {"query": query, "subject": kw.get("subject"),
                                     "include_domains": list(kw.get("include_domains") or ())})
        return self.inner.search(query, **kw)


def recording_model(recorder: Recorder):
    """A model FACTORY: it receives the gate the wiring built and attaches it to a real
    `TokenFactory` over a recording transport. The replies come from `wiring.sample_reply` — the
    production script — so this observes the scripted model rather than replacing it."""

    def factory(gate):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            recorder.add("model", body)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": wm.sample_reply(body)}}],
                "usage": {"prompt_tokens": 430, "completion_tokens": 90, "total_tokens": 520}})

        http = httpx.Client(base_url=SETTINGS.base_url, timeout=5,
                            transport=httpx.MockTransport(handler))
        return TokenFactory(SETTINGS, http, boundary=gate)

    return factory


# ---------------------------------------------------------------- fixtures --------------------
@pytest.fixture
def wired():
    """A built sample product, not yet watching. `tick`/`start` is the caller's choice, because
    "the watcher has not run yet" is a state the product has and the tests need to be able to be
    in it."""
    recorder = Recorder()
    ports = build_sample_ports(SAMPLE_DATE)
    ports = dataclasses.replace(ports, mail=RecordingMail(ports.mail, recorder),
                                calendar=RecordingCalendar(ports.calendar, recorder),
                                search=RecordingSearch(ports.search, recorder))
    product = wm.build_sample(settings=SETTINGS, ports=ports, llm=recording_model(recorder))
    yield product, recorder
    product.stop()


@pytest.fixture
def client(wired):
    """The app the CLI serves: v1 mounted as usual, v2 mounted from the wiring. Going through
    `default_app` rather than a v2-only app is deliberate — the mounting is part of the deliverable.
    """
    product, _ = wired
    return TestClient(default_app(SETTINGS, demo=True, v2=product.service))


@pytest.fixture
def running(wired, client):
    """Watching, the way `privatemesh serve --v2 --sample` leaves it: the first tick has already
    happened before the first request arrives."""
    product, _ = wired
    product.start()
    return client


# ---------------------------------------------------------------- helpers ---------------------
def _until(client, change_id: str, stage: str, *, tries: int = 100) -> dict:
    """What the UI does: poll `GET /v2/changes/{id}` until the stage machine has moved. The engine
    is synchronous today; the CONTRACT is a stage machine, so the test drives it as one."""
    body: dict = {}
    for _ in range(tries):
        body = client.get(f"/v2/changes/{change_id}").json()
        if body["stage"] == stage:
            return body
        time.sleep(0.01)
    raise AssertionError(f"stage {body.get('stage')!r} never reached {stage!r}")


def _change_id(client) -> str:
    """The change the watcher found, read from the API the web app reads."""
    for _ in range(100):
        today = client.get("/v2/today", params={"date": DAY}).json()
        if today["changes"]:
            return today["changes"][0]["id"]
        time.sleep(0.01)
    raise AssertionError("the watcher never surfaced a change")


def _prepared(client) -> dict:
    change_id = _change_id(client)
    client.post(f"/v2/changes/{change_id}/prepare")
    return _until(client, change_id, "proposal_ready")


def _hero_flow(client) -> dict:
    """Detect, plan, approve — the whole run, so the privacy assertion covers every surface an
    outbound byte can leave through rather than a fragment of the flow."""
    change = _prepared(client)
    approved = client.post(f"/v2/changes/{change['id']}/approve",
                           json={"version": change["version"]})
    assert approved.status_code == 200, approved.text
    return approved.json()


# ---------------------------------------------------------------- the session -----------------
def test_a_sample_session_says_it_is_a_sample_on_every_row(client):
    body = client.get("/v2/session").json()
    assert body["mode"] == "sample"
    assert body["timezone"] == "Europe/London"
    rows = {row["service"]: row for row in body["connections"]}
    assert set(rows) == {"gmail", "calendar", "tavily", "nemotron"}, (
        "the contract names four services; a missing row reads as a service that is not there")
    for name, row in rows.items():
        assert row["state"] == "connected", name
        detail = row["detail"].lower()
        # A "connected" badge on synthetic data is the small lie that makes every other badge
        # worthless, so the detail has to say what it is.
        assert "sample" in detail or "synthetic" in detail, name
    assert body["routine"]["enabled"] is True and body["routine"]["paused"] is False


# ---------------------------------------------------------------- today -----------------------
def test_today_carries_the_change_and_the_day_it_plans_for(client, running):
    """Two days are in play and the product is honest about both: the email arrives on the 18th
    saying "tomorrow", so the change is on the 18th and the meeting it is about is on the 19th."""
    today = client.get("/v2/today", params={"date": DAY}).json()
    assert [c["from_name"] for c in today["changes"]] == ["Alex Doe"]
    assert today["changes"][0]["stage"] == "detected"
    assert today["changes"][0]["thread_url"].startswith("https://mail.google.com/")
    assert today["events"] == [], "the sample calendar's day is the 19th, not the 18th"

    plan_day = client.get("/v2/today", params={"date": PLAN_DAY}).json()
    assert MEETING_ID in [e["id"] for e in plan_day["events"]]
    assert [p["id"] for p in plan_day["protected"]] == [COMMITMENT_ID]
    # ...and the change is on the day it plans for TOO. It needs a decision, and "this needs you"
    # is not a fact about a timestamp: filtering only on `received_at` left the owner who opens the
    # workspace on the day they are planning with a full calendar and nothing to act on.
    assert [c["id"] for c in plan_day["changes"]] == [today["changes"][0]["id"]]


def test_today_with_no_date_is_the_day_the_workspace_is_on(client, running):
    """`api.today()` is called with no argument on first load, and the browser renders what comes
    back: `date` in the day heading, `events` in the day strip, `changes[0]` as the thing to
    handle. Answering about the clock's own morning — the day BEFORE the plan — put an empty day on
    screen, which is a different story from the one the web sample tells."""
    body = client.get("/v2/today").json()
    assert body["date"] == PLAN_DAY
    assert MEETING_ID in [e["id"] for e in body["events"]]
    assert [p["id"] for p in body["protected"]] == [COMMITMENT_ID]
    assert [c["from_name"] for c in body["changes"]] == ["Alex Doe"]
    assert body["changes"][0]["id"] == _change_id(client)


def test_a_settled_change_stays_on_the_day_it_planned_for(client, running):
    """After approval the change is finished with the owner, so it leaves the day it arrived — and
    it must NOT leave the day it is about: that is the day whose screen renders its outcome, and a
    reload there would otherwise drop the owner back to "No changes need you"."""
    change = _prepared(client)
    client.post(f"/v2/changes/{change['id']}/approve", json={"version": change["version"]})
    planned = client.get("/v2/today", params={"date": PLAN_DAY}).json()
    row = next(c for c in planned["changes"] if c["id"] == change["id"])
    assert row["stage"] == "done"


def test_the_web_origin_is_granted_read_access_to_the_v2_surface(client, running):
    """The browser is a different origin from the API. Without this grant every fetch fails CORS
    and the live mode is a blank page with a console full of errors."""
    origin = "http://127.0.0.1:3000"
    granted = client.get("/v2/session", headers={"Origin": origin})
    assert granted.headers.get("access-control-allow-origin") == origin
    assert client.get("/v2/today", headers={"Origin": "https://stranger.example"}).headers.get(
        "access-control-allow-origin") is None


def test_the_protected_block_is_times_and_a_label_only(client, running):
    """`ProtectedBlock {id, start, end, buffer_minutes, label}` — the title, the place and the
    reason are not in this response, and asserting the exact key set is what keeps them out when
    somebody later "improves" the payload."""
    block = client.get("/v2/today", params={"date": PLAN_DAY}).json()["protected"][0]
    assert set(block) == {"id", "start", "end", "buffer_minutes", "label"}
    assert block["label"] == "Protected"
    assert block["start"] == "2026-09-19T16:30:00+01:00" and block["buffer_minutes"] == 45
    body = client.get("/v2/today", params={"date": PLAN_DAY}).text
    for marker in MARKERS:
        assert marker.lower() not in body.lower(), f"{marker!r} is in the day view"


# ---------------------------------------------------------------- the hero flow ---------------
def test_the_proposal_answers_the_email_with_times_and_evidence(client, running):
    change = _prepared(client)
    proposal = change["proposal"]

    assert (proposal["start"], proposal["end"]) == ("2026-09-19T15:00:00+01:00",
                                                    "2026-09-19T15:45:00+01:00")
    # The move is from the event the SENDER is on, not simply the first event of the day: the
    # standup is earlier and is not what Alex wrote about.
    assert [b["title"] for b in proposal["before"]] == ["Catch up with Alex"]
    assert proposal["before"][0]["start"] == "2026-09-19T11:00:00+01:00"

    rejected = proposal["rejected"]
    time_refusal = next(r for r in rejected if r["name"].startswith("16:00"))
    assert time_refusal["code"] == "overlaps_protected_buffer"
    assert "15:45" in time_refusal["reason"], (
        "a refusal without the number in it is a validation complaint, not an answer")
    assert time_refusal["evidence"] is None, "the boundary is not a web source"

    venue_refusals = [r for r in rejected if r["code"] == "venue"]
    assert len(venue_refusals) == 1
    assert venue_refusals[0]["evidence"]["url"], "a venue is refused with its source, or not at all"
    assert proposal["venue"]["name"] and proposal["venue"]["evidence"]["official"] is True
    assert proposal["privacy_line"] and proposal["needs_choice"] is False

    kinds = [a["kind"] for a in proposal["actions"]]
    assert kinds == ["calendar_update", "email_reply"]
    assert proposal["actions"][0]["event_id"] == MEETING_ID
    assert proposal["actions"][0]["diff"]["start"] == ["2026-09-19T11:00:00+01:00",
                                                       "2026-09-19T15:00:00+01:00"]
    assert proposal["actions"][1]["to"] == ["alex@example.com"]


def test_approving_runs_the_calendar_then_the_email(client, running, wired):
    product, _ = wired
    change = _prepared(client)
    approved = client.post(f"/v2/changes/{change['id']}/approve",
                           json={"version": change["version"]}).json()

    # Approve answers with the Change (the UI repaints the card from it) and the run's effects are
    # a separate read — the same two calls the review screen actually makes.
    assert approved["id"] == change["id"] and approved["stage"] == "done"
    outcome = client.get(f"/v2/changes/{change['id']}/outcome").json()
    assert [e["kind"] for e in outcome["effects"]] == ["calendar_update", "email_reply"]
    assert [e["state"] for e in outcome["effects"]] == ["done", "done"]
    assert "15:00" in outcome["effects"][0]["detail"]

    # The invite really moved, and the reply really went — read back through the API and the
    # sample's own outbox, not through the change set that claims it.
    day = client.get("/v2/today", params={"date": PLAN_DAY}).json()
    moved = next(e for e in day["events"] if e["id"] == MEETING_ID)
    assert moved["start"] == "2026-09-19T15:00:00+01:00"
    assert len(product.mail.outbox) == 1
    sent = product.mail.outbox[0]
    assert sent["to"] == ("alex@example.com",)
    assert "15:00" in sent["body_text"]
    assert "Fern & Finch Café" in sent["body_text"], "the reply names the venue the sample story names"
    assert "[sample]" not in sent["body_text"], "the synthetic-page marker is not email copy"
    assert sent["in_reply_to"] == "<alex-cafe-1@example.com>", "the reply must be threaded"

    # The plan list the UI shows is the same run, keyed by the same change id it links to.
    plans = client.get("/v2/plans").json()
    row = next(p for p in plans if p["id"] == change["id"])
    assert set(row) >= {"id", "title", "state", "detail"}
    assert row["state"] == "completed" and row["detail"]
    assert row["title"].startswith("Alex Doe:")


def test_why_says_what_was_used_and_what_stayed_private(client, running):
    change = _prepared(client)
    why = client.get(f"/v2/changes/{change['id']}/why").json()

    # `used` is labelled entries, not bare sentences: the UI renders `label` and `detail`
    # separately and put an empty row on screen for every string it was handed.
    used = [f"{item['label']} {item['detail']}" for item in why["used"]]
    assert any("search" in item.lower() for item in used)
    assert any("protected" in item.lower() for item in used)
    # The disclosure NAMES the categories it withheld — it has to, or "kept private" is unfalsifiable
    # — so the check is on the protected VALUES, the same markers the privacy test above uses.
    for marker in MARKERS:
        assert marker.lower() not in " ".join(used).lower(), f"{marker!r} leaked into /why"
    assert set(why["kept_private"]["categories"]) == {"title", "location", "reason", "attendees"}
    assert all(count >= 1 for count in why["kept_private"]["counts"].values())
    assert why["changed"]
    assert why["technical"]["model_calls"] >= 4
    assert why["technical"]["audit_ids"], "every run is receipted, or the claim is unverifiable"


# ---------------------------------------------------------------- the refusals ----------------
def test_approving_a_version_that_is_no_longer_current_is_refused(client, running, wired):
    product, _ = wired
    change = _prepared(client)
    edited = client.post(f"/v2/changes/{change['id']}/edit",
                         json={"reply_body": "Sounds good — see you at three."}).json()
    assert edited["version"] == change["version"] + 1

    refused = client.post(f"/v2/changes/{change['id']}/approve", json={"version": change["version"]})
    assert refused.status_code == 409
    assert product.mail.outbox == [], "a refused approval sends nothing"
    assert product.calendar.get_event(MEETING_ID).start == "2026-09-19T11:00:00+01:00"


def test_a_boundary_edited_after_the_review_stops_the_approval(client, running, wired):
    """The owner moves their own protected window between reviewing and approving. The bundle was
    built against a boundary that no longer exists, so it must not run — this is the compare-and-
    swap that makes "you approved exactly this" true."""
    product, _ = wired
    change = _prepared(client)
    product.boundary.put(id=COMMITMENT_ID, start="2026-09-19T16:00:00+01:00",
                         end="2026-09-19T17:15:00+01:00", buffer_minutes=45,
                         title="School pickup — Maya", location="St Mary's Primary",
                         reason="Maya's class finishes at 16:45",
                         attendees=("maya@example.com",))

    refused = client.post(f"/v2/changes/{change['id']}/approve", json={"version": change["version"]})
    assert refused.status_code == 409
    assert "nothing was sent" in refused.json()["detail"]
    assert product.mail.outbox == []
    assert product.calendar.get_event(MEETING_ID).start == "2026-09-19T11:00:00+01:00"


def test_leaving_the_meeting_alone_sends_nothing(client, running, wired):
    product, _ = wired
    change_id = _change_id(client)
    left = client.post(f"/v2/changes/{change_id}/leave").json()
    assert left["stage"] == "stopped"
    outcome = client.get(f"/v2/changes/{change_id}/outcome").json()
    assert [e["state"] for e in outcome["effects"]] == ["pending", "pending"]
    assert product.mail.outbox == []


def test_the_watcher_can_be_paused_and_resumed(client, wired):
    """Pausing is a real stop, not a hidden poll: while paused the mailbox is not even read, and
    the change is still there afterwards — nothing is consumed by being paused."""
    product, _ = wired
    assert client.post("/v2/routine", json={"paused": True}).json()["routine"]["paused"] is True
    assert product.tick() == []
    assert client.get("/v2/today", params={"date": DAY}).json()["changes"] == []

    assert client.post("/v2/routine", json={"paused": False}).json()["routine"]["paused"] is False
    assert [c.from_address for c in product.tick()] == ["alex@example.com"]


# ---------------------------------------------------------------- the privacy claim -----------
@pytest.mark.parametrize("marker", MARKERS)
def test_no_private_marker_leaves_the_device_on_any_surface(client, running, wired, marker):
    """One assertion per marker, over every payload the whole run produced.

    The three liveness checks first are the point: "the marker was absent" proves nothing if the
    recorder captured nothing, and a flow that silently stopped early would otherwise pass this
    test by doing less."""
    product, recorder = wired
    _hero_flow(client)

    for surface in SURFACES:
        assert recorder.surfaces(surface), f"nothing was captured on {surface} — vacuous test"
    assert len(recorder.surfaces("model")) >= 4, "classify, extract, query, choose, draft"

    leaked = recorder.find(marker)
    assert leaked == [], (f"{marker!r} left the device on "
                          f"{sorted({e['surface'] for e in leaked})}")


def test_the_captured_model_prompts_are_the_real_ones(client, running, wired):
    """A second, independent check on the privacy run: the recorder saw the engine's own prompts
    (not a stub's), and what the model was asked for is counts and times rather than content."""
    _, recorder = wired
    _hero_flow(client)
    prompts = recorder.text("model")
    # Read the system messages off the parsed requests, not out of the dump: a prompt is multi-line
    # prose and `json.dumps` escapes the newlines, so a substring match on the dump stops matching
    # the moment a prompt gains a line — which is what happened when the prompts started stating
    # their JSON object. `prompts` below is still the dump, and still the right thing for the
    # content assertions, which look for short single-line markers.
    sent = [str(m.get("content") or "")
            for entry in recorder.surfaces("model")
            for m in (entry["payload"].get("messages") or []) if m.get("role") == "system"]
    for system_prompt in (wm.CLASSIFY_SYSTEM, wm.EXTRACT_SYSTEM, wm.QUERY_SYSTEM,
                          wm.CHOOSE_SYSTEM, wm.DRAFT_SYSTEM):
        assert system_prompt in sent
    assert "16:30" in prompts and "45" in prompts, "the planning fact is times, and it was sent"
    assert "maya@example.com" not in prompts.lower(), "the attendee list is not a planning fact"


# ---------------------------------------------------------------- the decision log ---------------
def test_the_run_reads_back_as_a_spine_of_decisions(client, running):
    """The endpoint the UI lane is building against, driven against a real sample run.

    Every other test of the log asserts it over rows a test wrote. This one asserts it over rows the
    PRODUCT wrote: the sample engine detected, planned and approved a change, and the decisions are
    what is left behind. If the log were wired into the fakes but not into the composition root,
    every other test in the suite would still pass and this one would return an empty list."""
    change = _hero_flow(client)

    body = client.get(f"/v2/changes/{change['id']}/decisions").json()
    assert isinstance(body, list), "the contract is a bare list, not an envelope"
    assert [row["seq"] for row in body] == list(range(len(body))), "seq is the spine's index"
    assert [row["at"] for row in body] == sorted(row["at"] for row in body), "the run went back in time"

    # The stages are NOT in the order the enum lists them, and the API must not claim otherwise.
    # `validate` runs before the second `query` because a slot that does not fit is never worth
    # searching for; the enum's order is the narrative, and the run's order is what happened.
    stages = {row["stage"] for row in body}
    assert {"read", "model", "search", "validate", "approve", "execute", "send"} <= stages, stages
    assert body[0]["stage"] == "read", "the thread is read before anything is decided about it"

    # The invariant the whole product is: nothing touches a provider before the owner said yes.
    # A `send` row above the `approve` row is a reply that left before it was approved, and this
    # is the assertion that catches it — it caught one, which is why `guard_email` takes a stage.
    stages_in_order = [row["stage"] for row in body]
    first_approve = stages_in_order.index("approve")
    for effect in ("execute", "send"):
        assert effect not in stages_in_order[:first_approve], \
            f"a {effect} decision was recorded before the change was approved"
    # The model and the email are the two untrusted sources this run actually touched.
    assert {row["source"] for row in body if row["untrusted"]} == {"model", "email"}
    for row in body:
        assert row["verdict"] in ("allowed", "refused", "withheld", "discarded")
        assert row["rule"].endswith("."), "a rule is a sentence an owner reads"
        assert set(row) == {"seq", "at", "stage", "source", "untrusted", "subject", "verdict",
                            "rule", "detail"}
    # `discarded` is the one verdict that is NOT a refusal, and this is the invariant that makes it
    # one: a slot can only be "not chosen" if a sibling slot at the same stage was taken. A run
    # where nothing was accepted must say `refused`, because then the request really was stopped —
    # which is exactly the distinction the fourth review found collapsed.
    for stage in {row["stage"] for row in body if row["verdict"] == "discarded"}:
        assert any(row["verdict"] == "allowed" for row in body if row["stage"] == stage), (
            f"a slot at {stage} was recorded as discarded with nothing accepted beside it")

    # The other direction, and it is the one a vacuous implication hides: the loop above passes
    # trivially on a run where NOTHING was discarded, which is exactly what the reviewed code
    # produced. The sample offers 15:00 and 16:00 and 16:00 loses to the protected window, so a run
    # that records no `discarded` row has collapsed the distinction the finding is about, and a
    # `refused` row at this stage would be the collapse itself — the plan picked the other time and
    # the log says the owner's rules stopped something.
    validate = [row for row in body if row["stage"] == "validate"]
    assert any(row["verdict"] == "discarded" for row in validate), (
        "the losing slot was not recorded as not-chosen: " + str(validate))
    assert all(row["verdict"] in ("allowed", "discarded") for row in validate), (
        "a validate row reported a refusal for a slot that merely lost: " + str(validate))


def test_the_decision_log_never_quotes_the_protected_commitment(client, running, wired):
    """The log is a new surface, so it is checked like every other one.

    It renders the protected title, location, reason and attendees NOWHERE — not in a subject, not
    in a rule, and not in a detail. The commitment is what the product exists to keep in, and a log
    that quoted it to prove it was keeping it would be the joke the brief is written against."""
    product, recorder = wired
    change = _hero_flow(client)

    rows = client.get(f"/v2/changes/{change['id']}/decisions").json()
    blob = json.dumps(rows, ensure_ascii=False).casefold()
    assert rows, "no decisions were recorded, so this test proves nothing"
    # The CONTENT of the commitment — who, where, what it is — appears nowhere.
    for marker in CONTENT_MARKERS:
        assert marker.casefold() not in blob, f"{marker!r} is quoted in the decision log"
    # The TIMES do appear, and that is the design rather than a slip: the window is planned around
    # as an interval and never described, which is why `16:45` is a marker the egress tests check
    # for and the log legitimately carries. Asserting it here is what stops this test from being
    # satisfied by a log that recorded nothing at all.
    assert "16:30" in blob or "17:15" in blob, "the planning fact is missing from the log"
    assert "16:30" in recorder.text("model")


def test_a_change_nobody_has_seen_has_an_empty_spine_not_a_404(client, running):
    """A freshly detected change has made no decisions yet. "Nothing has happened" is an empty
    list — a 404 would tell the UI the change does not exist, which is a different and wrong
    thing to render."""
    change_id = _change_id(client)

    body = client.get(f"/v2/changes/{change_id}/decisions")

    assert body.status_code == 200 and body.json() == []


def test_the_spine_for_a_change_that_does_not_exist_is_a_404(client, running):
    """The other half: an unknown id is not an empty list. The UI has to be able to tell "this
    change has decided nothing" from "this change is not a thing", because only one of them is
    worth rendering."""
    assert client.get("/v2/changes/no-such-change/decisions").status_code == 404


def test_blocked_attempts_is_on_the_change_the_ui_already_reads(client, running):
    """`blocked_attempts` rides on the change object rather than the decisions list, because the
    day view shows it before anybody opens the spine — the count is the thing that makes an owner
    look. A count that only existed inside the list would be invisible from the screen that needs
    it most."""
    change = _hero_flow(client)

    body = client.get(f"/v2/changes/{change['id']}").json()
    assert isinstance(body["blocked_attempts"], int)
    rows = client.get(f"/v2/changes/{change['id']}/decisions").json()
    assert body["blocked_attempts"] == sum(1 for r in rows
                                           if r["verdict"] in ("refused", "withheld"))
    assert body["blocked_attempts"] >= 1, (
        "the sample offers a 16:00 option that the buffer refuses; a run with no blocked attempt "
        "means either the validator stopped refusing or the count stopped counting")
