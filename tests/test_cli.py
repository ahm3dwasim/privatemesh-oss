"""`privatemesh ping` — the cheapest proof the key, base URL and FAST model are wired.

The CLI builds its own `httpx.Client` from the environment, so these tests replace the factory
with one over a MockTransport. That keeps the thing under test exactly the thing that matters:
which tier and budget ping asks for, what it prints, and what it exits with when the provider
says no."""
from __future__ import annotations

import json

import httpx
import pytest

from privatemesh.cli import main
from privatemesh.config import Settings
from privatemesh.llm import TokenFactory

S = Settings(api_key="k", base_url="https://tf.test/v1/", model_fast="nano",
             model_reason="super", model_deep="ultra", tavily_key="")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "k")
    monkeypatch.setenv("NEBIUS_BASE_URL", "https://tf.test/v1/")
    monkeypatch.setenv("PM_MODEL_FAST", "nano")
    monkeypatch.setenv("PM_MODEL_REASON", "super")
    monkeypatch.setenv("PM_MODEL_DEEP", "ultra")
    return monkeypatch


def _patch(monkeypatch, handler):
    sent = []

    def wrapper(req):
        sent.append(json.loads(req.content))
        return handler(req)

    def factory(settings):
        return TokenFactory(S, httpx.Client(base_url=S.base_url,
                                            transport=httpx.MockTransport(wrapper)))

    monkeypatch.setattr("privatemesh.cli.TokenFactory", factory)
    return sent


def test_ping_uses_fast_with_a_16_token_budget_and_prints_model_content_usage(env, capsys):
    sent = _patch(env, lambda r: httpx.Response(200, json={
        "model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        "choices": [{"message": {"content": "\nready\n"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}}))

    assert main(["ping"]) == 0
    assert sent[0]["model"] == "nano"
    assert sent[0]["max_tokens"] == 16
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False}

    out = capsys.readouterr().out
    assert "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B" in out
    assert "content: ready" in out          # stripped, not "\nready\n"
    assert "prompt_tokens" in out


def test_ping_exits_1_with_a_clear_message_when_the_provider_errors(env, capsys):
    _patch(env, lambda r: httpx.Response(500, json={"error": "boom"}))
    assert main(["ping"]) == 1
    err = capsys.readouterr().err
    assert "ping failed" in err and "500" in err


def test_ping_exits_1_when_reasoning_ate_the_whole_budget(env, capsys):
    """The live failure this guards: Nano with a small budget returns null content. Ping must say
    why and exit 1 — not raise a traceback at whoever is holding the terminal."""
    _patch(env, lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": None, "reasoning_content": "z" * 200},
                     "finish_reason": "length"}], "usage": {}}))
    assert main(["ping"]) == 1
    err = capsys.readouterr().err
    assert "EmptyContent" in err and "16-token budget" in err


def test_ping_exits_1_when_the_key_is_missing(capsys, monkeypatch):
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.setenv("NEBIUS_API_KEY", "")
    assert main(["ping"]) == 1
    assert "NEBIUS_API_KEY" in capsys.readouterr().err


# ------------------------------------------------------------------ google-connect ------------
def _patch_connect(monkeypatch, *, raises=None):
    seen: dict = {}

    def fake_connect(account, **kw):
        seen["account"] = account
        seen.update(kw)
        if raises is not None:
            raise raises
        return 0

    monkeypatch.setattr("privatemesh.google_auth.connect", fake_connect)
    return seen


def test_google_connect_runs_the_flow_for_the_named_account(monkeypatch):
    seen = _patch_connect(monkeypatch)
    assert main(["google-connect", "--account", "alex"]) == 0
    assert seen["account"] == "alex"
    assert seen["open_browser"] is True
    assert seen["timeout"] == 300.0


def test_google_connect_defaults_to_the_owner_and_can_skip_the_browser(monkeypatch):
    seen = _patch_connect(monkeypatch)
    assert main(["google-connect", "--no-browser"]) == 0
    assert seen["account"] == "you"
    assert seen["open_browser"] is False


def test_google_connect_refuses_an_account_name_that_is_really_a_path(monkeypatch, capsys):
    seen = _patch_connect(monkeypatch)
    assert main(["google-connect", "--account", "../../etc/passwd"]) == 2
    assert seen == {}, "a name that cannot be stored must not start an OAuth flow"
    assert "invalid account name" in capsys.readouterr().err


def test_google_connect_rejects_an_unknown_flag(monkeypatch, capsys):
    _patch_connect(monkeypatch)
    assert main(["google-connect", "--acount", "you"]) == 2
    assert "unexpected argument" in capsys.readouterr().err


def test_google_connect_reports_a_failed_flow_and_exits_non_zero(monkeypatch, capsys):
    from privatemesh.google_auth import GoogleAuthError

    _patch_connect(monkeypatch, raises=GoogleAuthError("Google returned access_denied"))
    assert main(["google-connect", "--account", "you"]) == 1
    err = capsys.readouterr().err
    assert "access_denied" in err and "Nothing was stored" in err


# ---------------------------------------------------------------- serve --v2 -------------------
def _no_credentials(monkeypatch):
    """The machine a judge runs this on: no API key, no Tavily key, no Google token."""
    for name in ("NEBIUS_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("PM_PROTECTED_COMMITMENT", "/nonexistent/protected.json")


def _fake_uvicorn(monkeypatch):
    import uvicorn

    seen: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: seen.update(
        app=app, host=host, port=port))
    return seen


def test_serve_v2_sample_starts_with_no_credentials_and_mounts_the_product(
        monkeypatch, capsys, tmp_path):
    """`--v2 --sample` is the path with nothing to configure. It must come up on a machine with no
    key at all — the v1 chat half included — and serve the v2 surface on the web app's port."""
    _no_credentials(monkeypatch)
    monkeypatch.chdir(tmp_path)                 # so the demo store cannot land in the repo
    seen = _fake_uvicorn(monkeypatch)

    assert main(["serve", "--v2", "--sample"]) == 0
    assert seen["port"] == 8765, "the port the web app and the local preview launch config expect"
    # Ask the app that would have been served, rather than reading its route table: what matters
    # is that the mounted surface ANSWERS the contract.
    from fastapi.testclient import TestClient

    session = TestClient(seen["app"]).get("/v2/session").json()
    assert session["mode"] == "sample"
    assert {row["service"] for row in session["connections"]} == {
        "gmail", "calendar", "tavily", "nemotron"}
    out = capsys.readouterr().out
    assert "[v2 sample]" in out
    assert "nemotron" in out, "the four connection rows are printed, so the operator sees the mode"


def test_serve_v2_live_refuses_to_start_without_a_provider(monkeypatch, capsys, tmp_path):
    """Live mode with no credentials is a refusal with a message, not a traceback and not a server
    that comes up looking healthy while every request fails."""
    _no_credentials(monkeypatch)
    monkeypatch.chdir(tmp_path)
    seen = _fake_uvicorn(monkeypatch)

    assert main(["serve", "--v2", "--live"]) == 1
    assert seen == {}, "nothing is served when live mode is not ready"
    err = capsys.readouterr().err
    assert "cannot start in live mode" in err
    assert "google-connect" in err or "GOOGLE" in err.upper()
    assert "Nothing was sent" in err


def test_serve_v2_defaults_to_sample_and_live_wins_over_sample():
    from privatemesh.cli import _v2_mode

    assert _v2_mode([]) is None
    assert _v2_mode(["--v2"]) == "sample"
    assert _v2_mode(["--sample"]) == "sample"
    assert _v2_mode(["--v2", "--sample", "--live"]) == "live"
