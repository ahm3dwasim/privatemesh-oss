"""The third review's Critical: the owner-session gate was not authentication.

`wiring.owner_session` answered from a process-level boolean, so `build_live(..., owner_session=True)`
made EVERY request reaching the port the owner's — no credential, no session, no caller identity.
A network caller could read the decision log and run the approve route. The boolean was the whole
boundary, and a boolean is a thing the code sets about itself.

The boundary is now a bearer token the SERVER generates at startup and writes 0600 to
`~/.privatemesh/owner.token` (overridable with `PM_OWNER_TOKEN_FILE`), compared with
`hmac.compare_digest`. Sample mode is unchanged and needs no token: its owner is the person running
the demo on their own machine, it has no providers, and the sample's whole point is that a judge can
start it with nothing. Live mode has no boolean that can open it.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from privatemesh.api_v2 import V2Error, build_v2_router
from privatemesh.cli import main
from privatemesh.owner_token import bearer, issue, matches, path

from fakes_v2 import Harness, at, change_email, evidence

SCRIPT = {
    "classify": {"is_meeting_change": True, "reason": "proposes new times"},
    "extract": {"options": [{"start": at("15:00"), "end": at("15:45"), "phrase": "3"}],
                "request_excerpt": "Could we meet near King's Cross?"},
    "query": {"query": "cafe near King's Cross"},
    "choose": {"venue_index": 0, "is_venue": True, "reason_sentence": "Open at 15:00.",
               "rejected": []},
    "draft": {"subject": "Re: Our usual cafe has closed", "template_body": "Hi Alex,",
              "fragments": [{"source": "time", "text": "15:00-15:45 works for me."}]},
}

TOKEN = "tok_" + "a" * 40


def _live_client(token=TOKEN, **kw):
    """A LIVE service — the mode where a boolean must not be able to open anything.

    `authenticated_owner=True` is passed ON PURPOSE in `live_service()`. The old build read that
    flag as the answer; the new one ignores it in live mode entirely, which is the fix. A test that
    left it False would pass against a gate that had merely been re-defaulted.
    """
    h = Harness(script=SCRIPT, messages=[change_email("m1")],
                search={"king's cross": [evidence("https://thestandard.co.uk/",
                                                  "The Standard, King's Cross",
                                                  "Open daily 12:00-23:00")]})
    service = h.v2(mode="live", owner_token=token, owner_name="Sam Owner",
                   timezone="Europe/London", **kw)
    app = FastAPI()
    app.include_router(build_v2_router(service))
    return h, service, TestClient(app)


def _sample_client():
    h = Harness(script=SCRIPT, messages=[change_email("m1")],
                search={"king's cross": [evidence("https://thestandard.co.uk/",
                                                  "The Standard, King's Cross",
                                                  "Open daily 12:00-23:00")]})
    app = FastAPI()
    app.include_router(build_v2_router(h.v2(mode="sample")))
    return h, TestClient(app)


def _owner_routes(api, change_id: str) -> list[tuple[str, str, dict]]:
    """Every route that reads the owner's decisions or causes an effect, with a body that would
    otherwise be accepted. Enumerated from `api_v2.py` rather than sampled: a route left out of the
    gate is the whole defect, so the list is the test."""
    return [
        ("POST", f"/v2/changes/{change_id}/prepare", {}),
        ("POST", f"/v2/changes/{change_id}/leave", {}),
        ("POST", f"/v2/changes/{change_id}/edit", {"reply_body": "Hi Alex, 15:00 works."}),
        ("POST", f"/v2/changes/{change_id}/approve", {"version": 1}),
        ("GET", f"/v2/changes/{change_id}/decisions", None),
        ("POST", f"/v2/changes/{change_id}/learn", {}),
        ("POST", "/v2/learning/any-id/approve", {"identity": "you"}),
        ("POST", "/v2/learning/any-id/reject", {}),
        ("POST", "/v2/skills/any-name/revoke", {}),
        ("POST", "/v2/preferences", {"fact": "I prefer mornings", "scope": "general"}),
        ("PATCH", "/v2/preferences/any-id", {"fact": "x", "scope": "general"}),
        ("DELETE", "/v2/preferences/any-id", None),
        ("POST", "/v2/boundary", {"start": "09:00", "buffer_minutes": 30}),
        ("POST", "/v2/routine", {"paused": True}),
    ]


def _call(api, method: str, url: str, body):
    if method == "GET":
        return api.get(url)
    if method == "DELETE":
        return api.delete(url)
    if method == "PATCH":
        return api.patch(url, json=body)
    return api.post(url, json=body)


# ================================================ the gate is a credential, not a boolean
def test_every_owner_route_refuses_an_unauthenticated_caller():
    """The Critical, one route at a time. No header, no ownership — whatever the wiring declared."""
    h, _service, api = _live_client()
    change_id = h.engine.detect(None)[0].id

    for method, url, body in _owner_routes(api, change_id):
        response = _call(api, method, url, body)
        assert response.status_code == 403, f"{method} {url} answered {response.status_code}"
        assert response.json().get("code"), f"{method} {url} refused without saying why"


def test_the_route_that_carried_the_old_gate_refuses_without_the_token():
    """`approve` was the one route the second review's fix guarded, and it guarded with the boolean.
    The status and the code the UI already renders do not change; only what earns them does."""
    h, _service, api = _live_client()
    change_id = h.engine.detect(None)[0].id

    response = api.post(f"/v2/changes/{change_id}/approve", json={"version": 1})
    assert response.status_code == 403
    assert response.json()["code"] == "approval_forbidden"

    response = api.get(f"/v2/changes/{change_id}/decisions")
    assert response.status_code == 403
    assert response.json()["code"] == "decisions_forbidden"


def test_the_generated_token_is_what_opens_the_owner_routes():
    """The other half: a gate that refuses everybody is a different bug. With the token the same
    request that was refused above is served."""
    h, _service, api = _live_client()
    change_id = h.engine.detect(None)[0].id
    headers = {"Authorization": f"Bearer {TOKEN}"}

    assert api.post(f"/v2/changes/{change_id}/prepare", headers=headers).status_code == 200
    assert api.get(f"/v2/changes/{change_id}/decisions", headers=headers).status_code == 200
    assert api.post(f"/v2/changes/{change_id}/leave", headers=headers).status_code == 200


def test_a_live_service_cannot_be_opened_by_a_declared_boolean():
    """The mutation control for the whole fix.

    `authenticated_owner=True` is exactly what `build_live(..., owner_session=True)` used to set,
    and it is set here on a LIVE service. If the gate still read the boolean, every refusal above
    would be a 200 — so this test is what makes those refusals mean something."""
    h, service, api = _live_client()
    assert service.authenticated_owner is True, "the flag was not set; this control is vacuous"
    change_id = h.engine.detect(None)[0].id

    assert api.get(f"/v2/changes/{change_id}/decisions").status_code == 403
    assert api.post(f"/v2/changes/{change_id}/approve", json={"version": 1}).status_code == 403


def test_a_wrong_token_is_refused():
    h, _service, api = _live_client()
    change_id = h.engine.detect(None)[0].id

    for wrong in ("", "tok_", TOKEN[:-1], TOKEN + "b", "bearer " + TOKEN, "Bearer",
                  "Bearer " + "b" * 44):
        headers = {"Authorization": wrong}
        response = api.get(f"/v2/changes/{change_id}/decisions", headers=headers)
        assert response.status_code == 403, f"{wrong!r} was accepted"


def test_a_prefix_of_the_token_is_not_enough():
    """The comparison is equality, not `startswith`. A token accepted by prefix is a token whose
    search space is one character per guess."""
    h, _service, api = _live_client()
    change_id = h.engine.detect(None)[0].id
    assert api.get(f"/v2/changes/{change_id}/decisions",
                   headers={"Authorization": f"Bearer {TOKEN[:8]}"}).status_code == 403


def test_the_comparison_is_constant_time():
    """`hmac.compare_digest`, not `==`. Asserted by watching the call rather than by reading the
    source: the two are indistinguishable from the outside, which is the point of the primitive."""
    import hmac

    calls: list[tuple] = []
    real = hmac.compare_digest

    def watched(a, b):
        calls.append((a, b))
        return real(a, b)

    import privatemesh.owner_token as ot
    original = ot.hmac.compare_digest
    ot.hmac.compare_digest = watched
    try:
        assert matches(TOKEN, TOKEN) is True
        assert matches("nope", TOKEN) is False
    finally:
        ot.hmac.compare_digest = original
    assert len(calls) == 2, calls


def test_a_non_string_or_missing_presentation_is_refused_rather_than_crashing():
    """`compare_digest` raises TypeError on a non-ASCII `str`, and a header is attacker-controlled
    text. A 500 is not a refusal, and it is not a 403 either."""
    assert matches(None, TOKEN) is False
    assert matches("", TOKEN) is False
    assert matches("tökén", TOKEN) is False
    assert matches("é" * 44, TOKEN) is False


def test_bearer_parsing_is_exact():
    assert bearer(f"Bearer {TOKEN}") == TOKEN
    assert bearer(None) is None
    assert bearer("") is None
    assert bearer("Basic abc") is None
    assert bearer("Bearer") is None
    assert bearer("bearer " + TOKEN) is None, "the scheme is case-sensitive in this build"
    assert bearer(f"Bearer  {TOKEN}") is None


# ================================================ the sample is unchanged
def test_the_sample_still_answers_as_its_owner_with_no_token():
    """A judge starting the sample has nothing to configure. Its owner is the person running it,
    it has no providers, and the decision log is the screen the sample exists to show."""
    h, api = _sample_client()
    change_id = h.engine.detect(None)[0].id

    assert api.post(f"/v2/changes/{change_id}/prepare").status_code == 200
    assert api.get(f"/v2/changes/{change_id}/decisions").status_code == 200
    assert api.get("/v2/session").json()["authenticated_owner"] is True


def test_the_session_says_whether_THIS_request_is_the_owner():
    """The session is how the web client decides whether to render the owner's controls, so the
    answer has to be about the request that asked — not about the process that answered.

    The question "is this request the owner's" is itself only the owner's to ask. `/v2/session`
    carries the owner's name, their timezone, their connection state and the timing of their
    protected window, so in live mode it is served to a caller holding the token and refused to
    everyone else. The refusal is strictly stronger than the old `authenticated_owner: false`: a
    stranger now learns nothing at all rather than learning that they are a stranger."""
    h, _service, api = _live_client()

    refused = api.get("/v2/session")
    assert refused.status_code == 403
    assert refused.json()["code"] == "owner_forbidden"
    assert api.get("/v2/session", headers={"Authorization": f"Bearer {TOKEN}"}
                   ).json()["authenticated_owner"] is True


# ================================================ the token on disk
def test_a_token_is_written_0600_to_the_overridden_path(tmp_path):
    target = tmp_path / "nested" / "owner.token"
    os.environ["PM_OWNER_TOKEN_FILE"] = str(target)
    token = issue()

    assert target.exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text().strip() == token
    assert len(token) >= 32, "a guessable token is not a credential"
    assert target.parent.stat().st_mode & 0o077 == 0


def test_the_token_survives_a_restart(tmp_path):
    """A restart that minted a new token would lock out the browser holding the old one — and the
    fix for that would be a flag that turns the gate off, which is where this started."""
    os.environ["PM_OWNER_TOKEN_FILE"] = str(tmp_path / "owner.token")
    assert issue() == issue()


def test_an_empty_or_unreadable_token_file_is_replaced_rather_than_accepted(tmp_path):
    """The file is the credential. A truncated one — a crash between create and write — must not
    become a token that matches an empty header."""
    target = tmp_path / "owner.token"
    os.environ["PM_OWNER_TOKEN_FILE"] = str(target)
    target.write_text("")
    token = issue()
    assert token
    assert target.read_text().strip() == token


def test_a_loose_mode_is_tightened(tmp_path):
    """The file is written 0600, but a copy restored from a backup with 0644 is still the owner's
    credential sitting in a world-readable file. Opening it is the moment to fix that."""
    target = tmp_path / "owner.token"
    target.write_text("existing-token-value-that-is-long-enough")
    os.chmod(target, 0o644)
    os.environ["PM_OWNER_TOKEN_FILE"] = str(target)

    assert issue() == "existing-token-value-that-is-long-enough"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_the_default_path_is_the_documented_one_and_nothing_else_writes_to_it(monkeypatch):
    monkeypatch.delenv("PM_OWNER_TOKEN_FILE", raising=False)
    assert path() == Path.home() / ".privatemesh" / "owner.token"


def test_the_test_suite_never_touches_the_real_owner_token():
    """The one that protects every other test in this file: the suite runs with the override set,
    so no test can write the operator's real credential as a side effect."""
    assert os.environ.get("PM_OWNER_TOKEN_FILE")
    assert Path(os.environ["PM_OWNER_TOKEN_FILE"]) != Path.home() / ".privatemesh" / "owner.token"


# ================================================ the service, without HTTP
def test_the_service_refuses_programmatic_owner_calls_without_the_token():
    """The check is also reachable without a request, so a host that drives the service directly
    gets the same answer as one that drives it over HTTP."""
    h, service, _api = _live_client()
    with pytest.raises(V2Error) as refused:
        service.require_owner(None)
    assert refused.value.status_code == 403
    service.require_owner(TOKEN)          # the token is the answer; no exception


def test_a_live_service_with_no_token_at_all_refuses_everything():
    """Fail closed on the wiring mistake as well as on the request: a live service that was never
    given a token is not a service with an open gate, it is a service nobody can open."""
    h, _service, api = _live_client(token=None)
    change_id = h.engine.detect(None)[0].id

    assert api.get(f"/v2/changes/{change_id}/decisions").status_code == 403
    assert api.post(f"/v2/changes/{change_id}/approve", json={"version": 1}).status_code == 403


def test_the_read_only_routes_refuse_an_anonymous_caller():
    """The fourth review's Critical, and the correction of what this test used to assert.

    It previously read "the gate is on the owner's routes, not on the product" and required these
    three reads to answer an anonymous caller. That was the defect written down as intent: the
    reads ARE the owner's data. `/session` alone carries the owner's name, timezone, connection
    state, routine state and protected-boundary timing, and `/plans` is the index an attacker pages
    through to reach `/changes/{id}` — which is why the review called the three of them one
    finding rather than three.

    Both halves are asserted, because a gate that refuses everybody is a different bug: the same
    three requests are served once the token is presented."""
    h, _service, api = _live_client()
    headers = {"Authorization": f"Bearer {TOKEN}"}

    for url in ("/v2/session", "/v2/today", "/v2/plans", "/v2/preferences", "/v2/skills"):
        assert api.get(url).status_code == 403, f"{url} answered an anonymous caller"
        assert api.get(url, headers=headers).status_code == 200, f"{url} refused the owner"


# ================================================ the CLI says where the credential is
def test_the_owner_session_flag_prints_the_credential_path_instead_of_claiming_ownership(
        monkeypatch, capsys, tmp_path):
    """`--owner-session` used to make the process the owner by declaring itself one: a claim the
    code makes about itself, at startup, to nobody, and the third review's Critical.

    What the operator actually wants from that flag is where the credential is, so that is what it
    answers now — and it stops there rather than starting a server nobody asked for. The path comes
    from `PM_OWNER_TOKEN_FILE`, which the suite points at a tmp directory; the operator's real
    `~/.privatemesh` is never touched by any of this."""
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.setenv("PM_OWNER_TOKEN_FILE", str(tmp_path / "owner.token"))
    monkeypatch.chdir(tmp_path)
    import uvicorn

    seen: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: seen.update(port=port))

    assert main(["serve", "--v2", "--live", "--owner-session"]) == 0

    out = capsys.readouterr().out
    assert str(tmp_path / "owner.token") in out
    assert "Bearer" in out, "the operator is told how to present it, not only where it is"
    assert seen == {}, "the flag answers a question; it does not bring up a server"
    written = tmp_path / "owner.token"
    assert written.exists(), "being told where a credential is means there is one there"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert issue() == written.read_text(encoding="utf-8").strip()


def test_no_cli_path_prints_the_token_itself(monkeypatch, capsys, tmp_path):
    """Whatever the CLI says about the credential, it says the PATH. This output lands in a
    terminal and in a log, and a credential in a log is a credential published.

    The check is on the VALUE rather than on the wording, so it keeps holding for lines written by
    code that does not exist yet: whatever a future startup prints, it must not contain the secret.
    The sample is included because the sample has no credential to point at and must not pretend
    to have one."""
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.setenv("PM_OWNER_TOKEN_FILE", str(tmp_path / "owner.token"))
    monkeypatch.chdir(tmp_path)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: None)

    assert main(["serve", "--v2", "--live", "--owner-session"]) == 0   # names the path, exits
    assert main(["serve", "--v2", "--sample"]) == 0                   # no credential at all
    assert main(["serve", "--v2", "--live"]) == 1                     # no credentials: a refusal

    printed = capsys.readouterr()
    secret = (tmp_path / "owner.token").read_text(encoding="utf-8").strip()
    assert secret, "the flag was supposed to have minted one"
    assert secret not in printed.out and secret not in printed.err
    assert str(tmp_path / "owner.token") in printed.out
    # Exactly one line mentions the credential, and it is the one the flag was asked for: the
    # sample has no credential to point at, and the unready live start refuses before its banner.
    assert len([line for line in printed.out.splitlines() if "owner token" in line]) == 1
