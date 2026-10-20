"""Live mode: the SAME hero flow, through the REAL connector classes, with no network.

The sample proves the product works. This file proves the sample is a preview of THIS: the wiring
hands the engine `GmailClient`, `GCalClient`, `TavilySearch`, `TokenFactory` and `GoogleAuth`, and
the flow still completes — same proposal, same refusal, same two effects — with every byte crossing
an `httpx.MockTransport` that asserts on the wire format (If-Match, sendUpdates, In-Reply-To, the
watched label) rather than on the wrapper's intentions.

The other half of live mode is what happens when a credential is gone. The rule the wiring holds:
a missing or expired token is a STATE the product reports, and only `require_ready` — which the CLI
calls — refuses to serve. Nothing is sent in that state, and the API says 503 rather than 500,
because retrying a call that cannot succeed until a human consents again is not a thing to invite.

The Gmail and Calendar fakes are the ones `test_gmail.py` / `test_gcal.py` already drive the
connectors with; reusing them here keeps one description of the Google API in this repo.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import stat
import time

import httpx
import pytest

from test_gcal import EVENT_ID, ETAG_V1, FakeCalendar, raw_event
from test_gmail import LABEL_ID, FakeGmail, raw_message

from privatemesh import owner_token
from privatemesh import wiring as wm
from privatemesh.api_v2 import V2Error
from privatemesh.config import Settings
from privatemesh.gcal import GCalClient
from privatemesh.gmail import GmailClient
from privatemesh.google_auth import ClientConfig, GoogleAuth, GoogleToken, TokenStore
from privatemesh.ports import ProtectedCommitment
from privatemesh.tavily_evidence import TavilySearch

TF_HOST = "api.tokenfactory.nebius.com"
#: Priced on purpose: the live wiring attaches a real `SpendLedger`, and an unpriced model is
#: REFUSED there. A live test on a made-up model id would be testing a configuration that cannot
#: run in production.
FAST, REASON, DEEP = ("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "nvidia/nemotron-3-super-120b-a12b",
                      "nvidia/Nemotron-3-Ultra-550b-a55b")

SETTINGS = Settings(api_key="nvapi-test", base_url=f"https://{TF_HOST}/v1/", model_fast=FAST,
                    model_reason=REASON, model_deep=DEEP, tavily_key="tvly-test")

DAY, PLAN_DAY = "2026-09-18", "2026-09-19"
RECEIVED_MS = int(_dt.datetime.fromisoformat("2026-09-18T09:40:00+00:00").timestamp() * 1000)

#: The clock every product in this file is built at, unless a test asks for the real one.
#:
#: The fixture is a fixed day: the email landed 2026-09-18T09:40Z and asks for "tomorrow at 3 or
#: 4", so the plan day is 2026-09-19 and the two offered slots are 15:00 and 16:00 London. The
#: engine reads the clock to decide which slots are still usable, so with no clock injected this
#: file passed until 15:44 London on 2026-09-19 and returned `no_safe_fit` from 15:45 on — green
#: in the morning, red in the afternoon, on an unchanged tree. (15:45 is the END of the offered
#: slot, not its start: a slot that still has a minute left still fits. Measured, not assumed —
#: at 15:44 the flow reaches `proposal_ready`, at 15:45 it does not.)
#:
#: It is 09:00 rather than midnight so the day is unambiguously ahead of both slots, which is what
#: makes "16:00 overlaps the protected buffer" a statement about the buffer rather than about
#: which slots have already gone by.
PLAN_NOW = "2026-09-19T09:00:00+01:00"

COMMITMENT = ProtectedCommitment(
    id="prot-school-run", start="2026-09-19T16:30:00+01:00", end="2026-09-19T17:15:00+01:00",
    buffer_minutes=45, version=1, title="School pickup — Maya", location="St Mary's Primary",
    reason="Maya's class finishes at 16:45", attendees=("maya@example.com",))

ALEX_BODY = ("Hi — bad news, the café we always use has shut. Could we meet somewhere near "
             "King's Cross tomorrow at 3 or 4? You choose and update the invite.")
#: The venue's own site: `kingscrosscafe.co.uk` against the subject the connector derives from the
#: query is what makes `official` true, and that flag is computed by the REAL wrapper here.
CAFE = ("https://www.kingscrosscafe.co.uk/", "King's Cross Café",
        "King's Cross Café. Opening hours: Monday to Friday 07:00-17:00. Kitchen closes 30 "
        "minutes before the door.")
FELLOW = ("https://www.thefellow.co.uk/kings-cross", "The Fellow — King's Cross",
          "The Fellow, King's Cross. Our kitchen is closed between 15:00 and 17:00 every day; the "
          "bar opens at 17:00.")


class Google:
    """One MockTransport for every provider, routing on host — exactly what the wiring points at.

    Every request is recorded, so a test can ask what actually left; and `unexpected` collects any
    host the wiring was not supposed to talk to, which is the assertion that live mode reaches
    nothing else."""

    def __init__(self, *, gmail=None, calendar=None, token_status=200, token_payload=None):
        self.gmail = gmail or FakeGmail(messages=[raw_message(
            "msg-alex-cafe", internal_ms=RECEIVED_MS, sender="Alex Doe <alex@example.com>",
            subject="Our usual café has closed", body=ALEX_BODY)])
        self.calendar = calendar or FakeCalendar()
        self.token_status, self.token_payload = token_status, token_payload
        self.requests: list[httpx.Request] = []
        self.search_calls: list[dict] = []
        self.unexpected: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host == "gmail.googleapis.com":
            return self.gmail._handle(request)
        if host == "www.googleapis.com":
            return self.calendar._handle(request)
        if host == "oauth2.googleapis.com":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json=self.token_payload or {})
            return httpx.Response(200, json={"access_token": "ya29.fresh", "expires_in": 3600,
                                             "token_type": "Bearer"})
        if host == "api.tavily.com":
            return self._tavily(request)
        if host == TF_HOST:
            return self._model(request)
        self.unexpected.append(host)
        return httpx.Response(599, json={"error": f"no route for {host}"})

    def _tavily(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.search_calls.append(body)
        return httpx.Response(200, json={"results": [
            {"url": url, "title": title, "raw_content": text} for url, title, text in
            (CAFE, FELLOW)]})

    def _model(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": wm.sample_reply(body)}}],
            "usage": {"prompt_tokens": 512, "completion_tokens": 64, "total_tokens": 576}})

    def hosts(self) -> set[str]:
        return {r.url.host for r in self.requests}

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=SETTINGS.base_url, timeout=10,
                            transport=httpx.MockTransport(self.handle))


def _auth(tmp_path, transport: httpx.Client, token: GoogleToken | None = None) -> GoogleAuth:
    store = TokenStore("you", directory=tmp_path)
    if token is not None:
        store.save(token)
    return GoogleAuth("you", http=transport, config=ClientConfig("cid", "secret"), store=store)


def fresh_token() -> GoogleToken:
    return GoogleToken(access_token="ya29.stored", refresh_token="rt", scopes=("calendar",),
                       account="you", expires_at=time.time() + 3600)


def expired_token() -> GoogleToken:
    return GoogleToken(access_token="ya29.stale", refresh_token="rt", scopes=("calendar",),
                       account="you", expires_at=time.time() - 60)


def build(tmp_path, google_fake: Google | None = None, *, token=None, commitment=COMMITMENT,
          owner: bool = True, now=PLAN_NOW):
    """A live product over the connector fakes, built at a PINNED clock.

    `now` defaults to `PLAN_NOW` rather than to the real clock, and that default is the fix for
    the time bomb described there: every test below is a statement about 2026-09-19, so every test
    below is built at a moment on that day. `now=None` is the one caller that wants the real
    clock — `test_the_live_path_uses_the_real_clock_unless_one_is_injected`, which is what keeps
    this default a test seam rather than a product behaviour.

    `owner=True` by default HERE and only here: these tests stand in for the operator who started
    the process on their own machine. Being the owner is now a CREDENTIAL rather than a claim — the
    builder mints a token into `tmp_path` and `product.owner_token` is what an owner route must be
    handed, which is what the operator does by reading the file the server printed.

    `owner=False` is NOT "a product with no token" — it is the production path, where `build_live`
    is handed none and mints its own. That is the point of it: a live process has to end up with a
    credential whether or not its caller supplied one, and `owner=False` is how this builder asks
    for that rather than for its absence. What it leaves absent is the CALLER's token;
    `test_a_live_product_refuses_owner_routes_to_a_caller_with_no_token` asserts that through this
    same builder, and `test_a_live_process_mints_its_own_owner_token` asserts the other half."""
    fake = google_fake or Google()
    client = fake.client()
    product = wm.build_live(settings=SETTINGS, http=client, google=_auth(tmp_path, client, token),
                            commitment=commitment, data_dir=tmp_path, timezone="Europe/London",
                            now=now,
                            owner_token=owner_token.issue(tmp_path / "owner.token") if owner else None)
    return product, fake


# ---------------------------------------------------------------- the real connectors ---------
def test_live_mode_hands_the_engine_the_real_connectors(tmp_path):
    product, _ = build(tmp_path, token=fresh_token())
    assert product.mode == "live"
    assert isinstance(product.mail, GmailClient)
    assert isinstance(product.calendar, GCalClient)
    assert isinstance(product.search, TavilySearch)
    assert product.llm.__class__.__name__ == "TokenFactory"
    assert product.ready, product.refusals

    session = product.session()
    assert session["mode"] == "live"
    assert session["authenticated_owner"] is True, (
        "the operator declared this session and the product did not believe them")
    rows = {row["service"]: row for row in session["connections"]}
    assert set(rows) == {"gmail", "calendar", "tavily", "nemotron"}
    assert all(row["state"] == "connected" for row in rows.values()), rows
    # And a live session must never claim to be a sample.
    assert "sample" not in json.dumps(session).lower()
    assert "synthetic" not in json.dumps(session).lower()


def test_the_live_path_uses_the_real_clock_unless_one_is_injected(tmp_path):
    """The pinned clock above is a TEST seam, and the product must not have inherited it.

    `build()`'s default is what keeps this file from being a calendar: it is passed to
    `build_live`, it is not read by it. A live process is handed `now=None` — the CLI does exactly
    that — and must then plan against the actual present, because the cost of getting this wrong
    is not a red test: it is a deployment that offers the owner a 15:00 slot in a week that has
    already happened, or refuses every slot on a day that has not.

    Two assertions, because neither alone is enough. `service.today()` with no date is the
    product-level one — it is the browser's "what day is it" question, answered from the service's
    own clock, and it is what a refactor that moved the clock would still have to satisfy. But on
    2026-09-19 itself the fixture's day and the real day coincide and it cannot tell them apart,
    so the second reads the engine's clock directly and asks how far it is from the wall clock.
    """
    product, _ = build(tmp_path, token=fresh_token(), now=None)

    assert product.service.today()["date"] == _dt.datetime.now().date().isoformat(), (
        "a live product built with no injected clock reported a day that is not today")

    # The assertion above is the product-level one, but it cannot tell the two clocks apart on
    # 2026-09-19 itself, where the fixture's day and the real day coincide. This one can, on every
    # day: a pinned instant is minutes-to-months from the wall clock, and a real one is not.
    drift = abs((product.engine._clock() - _dt.datetime.now().astimezone()).total_seconds())
    assert drift < 60, (
        f"the engine's clock is {drift:.0f}s from the real one — the live path is not reading "
        f"the wall clock, which is what a deployment plans against")


def test_a_live_product_refuses_owner_routes_to_a_caller_with_no_token(tmp_path):
    """The Critical, end to end through the REAL live composition.

    A live process answers `no` to the owner question for every request that does not present the
    token, so the routes that act as the owner refuse. The reviewed build answered yes here — for
    every deployment that had never heard of `--owner-session`, a boolean the process set about
    itself — and served the decision log and ran approved effects to whoever reached the port.
    Reaching the port is not being the owner; the token is what the difference consists of."""
    product, _ = build(tmp_path, token=fresh_token(), owner=False)

    assert product.service.authenticated_owner is False
    assert product.service.session()["authenticated_owner"] is False, (
        "a request that presents nothing is not the owner, whatever the wiring holds")
    change = product.engine.detect(None)[0]
    with pytest.raises(V2Error) as refused:
        product.service.approve(change.id, change.version)
    assert refused.value.status_code == 403
    assert refused.value.code == "approval_forbidden"
    # The control, and the reason the refusal above is about the token rather than about a service
    # that refuses everybody: the SAME call with the token gets PAST the gate — it fails later, on
    # a change set that does not exist because nothing was prepared, which is a different refusal
    # entirely. A 403 here would mean the token bought nothing.
    with pytest.raises(V2Error) as past_the_gate:
        product.service.approve(change.id, change.version, presented=product.owner_token)
    assert past_the_gate.value.status_code == 404
    # And an owner route with no side effects is simply served.
    assert isinstance(product.service.decisions(change.id, presented=product.owner_token), list)


def test_a_live_process_mints_its_own_owner_token(tmp_path):
    """The fourth review's High, from the wiring's side: the credential is the PROCESS's, not the
    caller's.

    `build_live` is handed no token here — the production path — and the product must end up with
    one anyway, written where `PM_OWNER_TOKEN_FILE` says so that the operator can read it. A build
    that took the token only from its caller would be a build whose owner routes are open to
    everyone the moment the caller passed nothing, which is the shape the review found: ownership
    as a claim the code makes about itself rather than a credential the caller presents."""
    product, _ = build(tmp_path, token=fresh_token(), owner=False)
    written = pathlib.Path(os.environ["PM_OWNER_TOKEN_FILE"])

    assert product.owner_token, "a live process with no credential cannot be opened by its owner"
    assert product.owner_token == written.read_text().strip(), (
        "the token the routes check is not the token the operator was told to read")
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    # And it is the credential, not a decoration: presenting it opens a route that refuses silence.
    assert product.service.session()["authenticated_owner"] is False
    assert product.service.session(product.owner_token)["authenticated_owner"] is True


def test_the_hero_flow_runs_through_the_real_connectors(tmp_path):
    product, fake = build(tmp_path, token=fresh_token())

    found = product.tick()                                   # the watcher reads the watched label
    assert [c.from_address for c in found] == ["alex@example.com"]
    assert any(r.url.path.endswith("/labels") for r in fake.requests), (
        "the label scope is proven before claiming there is nothing new")

    change = product.engine.prepare(found[0].id)
    assert change.stage == "proposal_ready"
    proposal = product.service.change(change.id)["proposal"]
    assert (proposal["start"], proposal["end"]) == ("2026-09-19T15:00:00+01:00",
                                                    "2026-09-19T15:45:00+01:00")
    assert proposal["before"][0]["title"] == "Alex — project sync"
    refusal = next(r for r in proposal["rejected"] if r["name"].startswith("16:00"))
    assert refusal["code"] == "overlaps_protected_buffer"
    venue_refusal = next(r for r in proposal["rejected"] if r["code"] == "venue")
    assert venue_refusal["evidence"]["url"] == FELLOW[0]
    assert proposal["venue"]["name"] == CAFE[1]
    # `official` is the CONNECTOR's judgement about provenance, computed from the venue's domain
    # against the subject read out of the query — not a flag the model set.
    assert proposal["venue"]["evidence"]["official"] is True

    # Approve answers with the Change; the effects are the separate read the outcome panel makes.
    approved = product.service.approve(change.id, change.version, presented=product.owner_token)
    assert approved["id"] == change.id and approved["stage"] == "done"
    outcome = product.service.outcome(change.id)
    assert [e["kind"] for e in outcome["effects"]] == ["calendar_update", "email_reply"]
    assert [e["state"] for e in outcome["effects"]] == ["done", "done"]
    assert outcome["summary"]

    # ---- what actually crossed the wire ------------------------------------------------------
    patch = fake.calendar.patches[0]
    assert patch["if_match"] == ETAG_V1, "the reviewed etag is what makes the write conditional"
    assert patch["body"]["start"]["dateTime"] == "2026-09-19T15:00:00+01:00"
    assert patch["body"]["location"] == CAFE[1]
    assert patch["params"]["sendUpdates"] == ["all"]

    assert len(fake.gmail.sent) == 1
    sent = fake.gmail.sent[0]
    assert sent["threadId"] == "thr-1", "the reply stays in the thread"
    assert sent["mime"]["To"] == "alex@example.com"
    assert sent["mime"]["In-Reply-To"] == "<alex-1@example.com>"
    assert sent["key"], "the idempotency key is on the message, so a lost response is not a resend"
    assert "15:00" in sent["mime"].get_payload()

    assert [c["query"] for c in fake.search_calls] == ["King's Cross café opening hours"]
    assert fake.unexpected == [], f"live mode reached hosts it should not: {fake.unexpected}"
    assert fake.hosts() <= {"gmail.googleapis.com", "www.googleapis.com", "api.tavily.com", TF_HOST}


# ---------------------------------------------------------------- missing credentials --------
def test_a_missing_token_is_a_reported_state_not_a_crash(tmp_path):
    product, _ = build(tmp_path)                                    # no token was ever stored
    assert product.ready is False
    assert any("connect" in reason.lower() for reason in product.refusals)

    session = product.session()                                     # the app still ANSWERS
    rows = {row["service"]: row for row in session["connections"]}
    assert rows["gmail"]["state"] == "disconnected"
    assert rows["calendar"]["state"] == "disconnected"
    assert "google-connect" in rows["gmail"]["detail"], (
        "the detail names the command that fixes it")
    assert rows["tavily"]["state"] == "connected" and rows["nemotron"]["state"] == "connected"


def test_an_expired_token_reports_expired_and_today_says_503(tmp_path):
    """The 7-day Testing-mode expiry is a normal lifecycle event, not a bug: the connection says
    `expired`, the request says 503 (nothing about it was wrong), and nothing is sent."""
    fake = Google(token_status=400, token_payload={"error": "invalid_grant"})
    product, _ = build(tmp_path, fake, token=expired_token())

    assert product.ready is False
    assert [r["state"] for r in product.session()["connections"][:2]] == ["expired", "expired"]
    reason = next(r for r in product.refusals if "Google" in r)
    assert "google-connect" in reason and "7 days" in reason

    from fastapi.testclient import TestClient

    from privatemesh.api import default_app
    client = TestClient(default_app(SETTINGS, demo=True, v2=product.service))

    # The owner is refused because of their PROVIDER, and a stranger is refused because of who they
    # are — two different answers, and the second is the one that stops this route being a public
    # read of the owner's day. Asserted together so that neither can be mistaken for the other: a
    # 403 for a caller with no token would otherwise be indistinguishable from the 503 being gone.
    assert client.get("/v2/today", params={"date": PLAN_DAY}).status_code == 403
    response = client.get("/v2/today", params={"date": PLAN_DAY},
                          headers={"Authorization": f"Bearer {product.owner_token}"})
    assert response.status_code == 503, (
        "a 500 would tell the UI to retry a call that cannot succeed until a human consents")
    assert "google-connect" in response.json()["detail"]
    assert fake.gmail.sent == [] and fake.calendar.patches == []


def test_the_cli_refusal_says_which_credential_is_missing(tmp_path):
    product, _ = build(tmp_path)
    with pytest.raises(wm.ProviderUnavailable) as refusal:
        wm.require_ready(product)
    message = str(refusal.value)
    assert "not ready" in message and "connect" in message.lower()


def test_live_without_a_protected_commitment_refuses_to_start(tmp_path):
    """No boundary means nothing to plan around, and a plan with no boundary is exactly the
    product this is not. The refusal names the file to write."""
    product, _ = build(tmp_path, commitment=None)
    assert product.ready is False
    assert any("protected commitment" in reason for reason in product.refusals)
    assert any("protected.json" in reason for reason in product.refusals)
