"""google_auth: installed-app OAuth 2.0 with PKCE, loopback redirect, tokens on disk at 0600.

No sockets and no network: the token endpoint is an `httpx.MockTransport`, and the loopback
listener is injected as a plain object. The one thing these tests cannot cover is the socket
plumbing inside `LoopbackListener` itself (opening the port, the HTTP handler's rfile/wfile);
everything above it — PKCE, state check, exchange, refresh, storage, redaction — is covered.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from privatemesh import google_auth as ga
from privatemesh.ports import ReauthRequired

TOKEN_JSON = {"access_token": "ya29.ACCESS-TOKEN-VALUE", "refresh_token": "1//REFRESH-TOKEN-VALUE",
              "expires_in": 3600, "scope": " ".join(ga.SCOPES), "token_type": "Bearer"}


class FakeGoogle:
    """The token endpoint, scripted. Records every request it was sent."""

    def __init__(self, *, payload=None, status=200, error=None):
        self.payload = TOKEN_JSON if payload is None else payload
        self.status = status
        self.error = error
        self.requests: list[httpx.Request] = []
        self.forms: list[dict] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        self.forms.append({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": self.error or "invalid_grant",
                                                     "error_description": "Token has been expired"})
        return httpx.Response(200, json=self.payload)


def config(tmp_path: Path) -> ga.ClientConfig:
    path = tmp_path / "google_client.json"
    path.write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com",
                                              "client_secret": "csecret",
                                              "auth_uri": ga.GOOGLE_AUTH_URL,
                                              "token_uri": ga.GOOGLE_TOKEN_URL,
                                              "redirect_uris": ["http://127.0.0.1"]}}))
    return ga.load_client_config(path)


def auth(tmp_path: Path, fake: FakeGoogle, *, now=1_000_000.0) -> ga.GoogleAuth:
    return ga.GoogleAuth("you", config=config(tmp_path), http=fake.client,
                         store=ga.TokenStore("you", directory=tmp_path / "tok"), now=lambda: now)


# ------------------------------------------------------------------ PKCE ----------------------
def test_pkce_challenge_is_unpadded_s256_of_the_verifier():
    verifier = ga.new_code_verifier()
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert ga.code_challenge(verifier) == expected
    assert ga.new_code_verifier() != verifier  # fresh entropy per attempt


def test_authorization_url_carries_pkce_state_and_every_required_scope():
    url = ga.authorization_url(config(Path("/tmp")), redirect_uri="http://127.0.0.1:9999/callback",
                               state="st-1", challenge="ch-1")
    q = parse_qs(urlparse(url).query)
    assert url.startswith(ga.GOOGLE_AUTH_URL)
    assert q["client_id"] == ["cid.apps.googleusercontent.com"]
    assert q["redirect_uri"] == ["http://127.0.0.1:9999/callback"]
    assert q["response_type"] == ["code"]
    assert q["code_challenge"] == ["ch-1"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"] == ["st-1"]
    scopes = set(q["scope"][0].split())
    assert scopes == set(ga.SCOPES)
    assert scopes == {"https://www.googleapis.com/auth/gmail.readonly",
                      "https://www.googleapis.com/auth/gmail.send",
                      "https://www.googleapis.com/auth/calendar.events",
                      "https://www.googleapis.com/auth/calendar.readonly"}


# ------------------------------------------------------------------ callback ------------------
def test_parse_callback_reads_code_and_state_and_never_echoes_the_code_back():
    result = ga.parse_callback("/callback?code=abc123&state=st-9")
    assert result.code == "abc123"
    assert result.state == "st-9"
    assert result.error is None


def test_parse_callback_reports_a_denied_consent():
    result = ga.parse_callback("/callback?error=access_denied&state=st-9")
    assert result.code is None
    assert result.error == "access_denied"


def test_parse_callback_ignores_unrelated_paths():
    assert ga.parse_callback("/favicon.ico") is None
    assert ga.parse_callback("/callback") is None


# ------------------------------------------------------------------ storage -------------------
def test_token_is_stored_outside_the_repo_with_mode_0600(tmp_path):
    fake = FakeGoogle()
    a = auth(tmp_path, fake)
    token = ga.exchange_code(a.config, code="c", code_verifier="v",
                             redirect_uri="http://127.0.0.1:1/callback", http=fake.client)
    a.store.save(token)

    path = a.store.path
    assert path.exists()
    assert path.name == "you.json"
    repo = Path(ga.__file__).resolve().parents[2]
    assert repo not in path.resolve().parents, "tokens must never live inside the checkout"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text())["refresh_token"] == "1//REFRESH-TOKEN-VALUE"


def test_rewriting_the_token_file_keeps_mode_0600(tmp_path):
    fake = FakeGoogle()
    a = auth(tmp_path, fake)
    token = ga.exchange_code(a.config, code="c", code_verifier="v",
                             redirect_uri="http://127.0.0.1:1/callback", http=fake.client)
    a.store.save(token)
    a.store.save(token)
    assert stat.S_IMODE(a.store.path.stat().st_mode) == 0o600


def test_account_names_cannot_escape_the_token_directory(tmp_path):
    for bad in ("../evil", "a/b", "", ".", "you/../../etc/passwd"):
        with pytest.raises(ValueError):
            ga.TokenStore(bad, directory=tmp_path)


def test_saved_token_round_trips(tmp_path):
    fake = FakeGoogle()
    a = auth(tmp_path, fake)
    token = ga.exchange_code(a.config, code="c", code_verifier="v",
                             redirect_uri="http://127.0.0.1:1/callback", http=fake.client)
    a.store.save(token)
    loaded = ga.TokenStore("you", directory=a.store.directory).load()
    assert loaded.access_token == token.access_token
    assert loaded.refresh_token == token.refresh_token
    assert loaded.expires_at == token.expires_at
    assert set(loaded.scopes) == set(token.scopes)
    assert loaded.account == "you"


# ------------------------------------------------------------------ exchange / refresh --------
def test_exchange_code_sends_the_verifier_and_records_expiry(tmp_path):
    fake = FakeGoogle()
    a = auth(tmp_path, fake)
    token = ga.exchange_code(a.config, code="the-code", code_verifier="the-verifier",
                             redirect_uri="http://127.0.0.1:7777/callback", http=fake.client,
                             now=lambda: 1_000_000.0)
    form = fake.forms[0]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["code_verifier"] == "the-verifier"
    assert form["redirect_uri"] == "http://127.0.0.1:7777/callback"
    assert token.access_token == TOKEN_JSON["access_token"]
    assert token.expires_at == pytest.approx(1_000_000.0 + 3600, abs=1)


def test_a_valid_token_is_used_without_calling_the_token_endpoint(tmp_path):
    fake = FakeGoogle()
    a = auth(tmp_path, fake)
    a.store.save(ga.exchange_code(a.config, code="c", code_verifier="v",
                                  redirect_uri="r", http=fake.client))
    fake.requests.clear()
    assert a.access_token() == TOKEN_JSON["access_token"]
    assert fake.requests == []


def test_an_expired_token_is_refreshed_before_use(tmp_path):
    fake = FakeGoogle()
    a = auth(tmp_path, fake)
    stale = ga.GoogleToken(access_token="old", refresh_token="1//REFRESH-TOKEN-VALUE",
                           expires_at=999_000.0, scopes=ga.SCOPES, account="you")
    a.store.save(stale)

    assert a.access_token() == TOKEN_JSON["access_token"]
    assert [f["grant_type"] for f in fake.forms] == ["refresh_token"]
    assert fake.forms[0]["refresh_token"] == "1//REFRESH-TOKEN-VALUE"
    assert a.store.load().access_token == TOKEN_JSON["access_token"]


def test_refresh_keeps_the_old_refresh_token_when_google_omits_a_new_one(tmp_path):
    payload = {"access_token": "ya29.NEW", "expires_in": 3600, "token_type": "Bearer"}
    fake = FakeGoogle(payload=payload)
    a = auth(tmp_path, fake)
    a.store.save(ga.GoogleToken(access_token="old", refresh_token="1//KEEP-ME",
                                expires_at=0.0, scopes=ga.SCOPES, account="you"))
    token = a.token()
    assert token.refresh_token == "1//KEEP-ME"
    assert token.access_token == "ya29.NEW"


def test_invalid_grant_raises_reauth_required_for_the_ui_to_say_reconnect(tmp_path):
    fake = FakeGoogle(status=400, error="invalid_grant")
    a = auth(tmp_path, fake)
    a.store.save(ga.GoogleToken(access_token="old", refresh_token="1//DEAD",
                                expires_at=0.0, scopes=ga.SCOPES, account="you"))
    with pytest.raises(ReauthRequired) as err:
        a.access_token()
    assert "you" in str(err.value)
    assert "reconnect" in str(err.value).lower()


def test_a_bad_client_secret_is_not_reported_as_a_reconnect(tmp_path):
    """Only `invalid_grant` means "the owner must consent again". A wrong client secret cannot be
    fixed by reconsenting, so telling them to reconnect would be a loop with no exit."""
    fake = FakeGoogle(status=401, error="invalid_client")
    a = auth(tmp_path, fake)
    a.store.save(ga.GoogleToken(access_token="old", refresh_token="1//GOOD",
                                expires_at=0.0, scopes=ga.SCOPES, account="you"))
    with pytest.raises(ga.GoogleClientConfigError) as err:
        a.access_token()
    assert not isinstance(err.value, ReauthRequired)
    assert "client" in str(err.value).lower()
    assert "reconnect" not in str(err.value).lower()


def test_a_revoked_refresh_token_is_not_retried_forever(tmp_path):
    fake = FakeGoogle(status=400, error="invalid_grant")
    a = auth(tmp_path, fake)
    a.store.save(ga.GoogleToken(access_token="old", refresh_token="1//DEAD",
                                expires_at=0.0, scopes=ga.SCOPES, account="you"))
    with pytest.raises(ReauthRequired):
        a.access_token()
    with pytest.raises(ReauthRequired):
        a.access_token()
    assert len(fake.requests) == 2, "one refresh attempt per call, never a retry loop"


# ------------------------------------------------------------------ secrets -------------------
def test_no_token_value_reaches_the_logs(tmp_path, caplog):
    fake = FakeGoogle()
    a = auth(tmp_path, fake)
    a.store.save(ga.GoogleToken(access_token="ya29.SECRET-ACCESS", refresh_token="1//SECRET-REFRESH",
                                expires_at=0.0, scopes=ga.SCOPES, account="you"))
    with caplog.at_level(logging.DEBUG):
        a.access_token()
    logged = "\n".join(r.getMessage() for r in caplog.records) + caplog.text
    assert "SECRET-ACCESS" not in logged
    assert "SECRET-REFRESH" not in logged
    assert "ya29." not in logged


def test_repr_and_str_of_a_token_are_redacted():
    token = ga.GoogleToken(access_token="ya29.SECRET-ACCESS", refresh_token="1//SECRET-REFRESH",
                           expires_at=1.0, scopes=ga.SCOPES, account="you")
    for rendered in (repr(token), str(token), f"{token}"):
        assert "SECRET-ACCESS" not in rendered
        assert "SECRET-REFRESH" not in rendered
    assert "you" in repr(token)


def test_client_config_error_names_the_path_and_the_env_var(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(ga.GoogleClientConfigError) as err:
        ga.load_client_config(missing)
    assert "PM_GOOGLE_CLIENT" in str(err.value)
    assert str(missing) in str(err.value)


# ------------------------------------------------------------------ loopback flow -------------
class FakeListener:
    """Stands in for the socket listener: hands back a scripted callback result."""

    def __init__(self, result):
        self.result = result
        self.closed = False
        self.port = 53123
        self.redirect_uri = ga.redirect_uri_for(self.port)

    def wait(self, timeout):
        return self.result

    def close(self):
        self.closed = True


def test_redirect_uri_is_loopback_on_the_random_port():
    assert ga.redirect_uri_for(53123) == "http://127.0.0.1:53123/callback"


def test_loopback_flow_sends_the_verifier_matching_the_challenge_it_published(tmp_path):
    fake = FakeGoogle()
    listener = FakeListener(ga.CallbackResult(code="the-code", state="st-fixed", error=None))
    store = ga.TokenStore("you", directory=tmp_path / "tok")
    seen: dict = {}

    def factory():
        return listener

    def on_url(url: str) -> None:
        seen["url"] = url

    token = ga.run_loopback_flow(config(tmp_path), account="you", http=fake.client,
                                 listener_factory=factory, on_url=on_url, store=store,
                                 open_browser=lambda url: None, state="st-fixed")

    q = parse_qs(urlparse(seen["url"]).query)
    assert q["state"] == ["st-fixed"]
    assert q["redirect_uri"] == [ga.redirect_uri_for(listener.port)]
    assert fake.forms[0]["code_verifier"]
    assert ga.code_challenge(fake.forms[0]["code_verifier"]) == q["code_challenge"][0]
    assert token.access_token == TOKEN_JSON["access_token"]
    assert token.account == "you"
    assert listener.closed, "the listener is closed even on the success path"


def test_loopback_flow_rejects_a_mismatched_state(tmp_path):
    fake = FakeGoogle()
    listener = FakeListener(ga.CallbackResult(code="the-code", state="attacker", error=None))
    with pytest.raises(ga.GoogleAuthError) as err:
        ga.run_loopback_flow(config(tmp_path), account="you", http=fake.client,
                             listener_factory=lambda: listener, open_browser=lambda url: None,
                             state="st-fixed")
    assert "state" in str(err.value).lower()
    assert fake.requests == [], "a callback that fails the state check never reaches the token endpoint"
    assert listener.closed


def test_loopback_flow_reports_a_denied_consent(tmp_path):
    fake = FakeGoogle()
    listener = FakeListener(ga.CallbackResult(code=None, state="st", error="access_denied"))
    with pytest.raises(ga.GoogleAuthError) as err:
        ga.run_loopback_flow(config(tmp_path), account="you", http=fake.client,
                             listener_factory=lambda: listener, open_browser=lambda url: None,
                             state="st")
    assert "access_denied" in str(err.value)


def test_loopback_flow_refuses_an_unknown_account_before_opening_anything(tmp_path):
    fake = FakeGoogle()
    with pytest.raises(ValueError):
        ga.run_loopback_flow(config(tmp_path), account="../../etc/passwd", http=fake.client,
                             listener_factory=lambda: pytest.fail("listener must not open"),
                             open_browser=lambda url: None)


def test_the_default_token_directory_is_outside_the_repo(monkeypatch):
    monkeypatch.delenv("PM_GOOGLE_TOKEN_DIR", raising=False)   # the suite-wide isolation override
    store = ga.TokenStore("alex")
    assert store.directory == Path(os.path.expanduser("~/.privatemesh/google"))
    repo = Path(ga.__file__).resolve().parents[2]
    assert repo not in store.directory.resolve().parents
    assert store.path.name == "alex.json"


def test_the_token_directory_can_be_redirected_for_isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("PM_GOOGLE_TOKEN_DIR", str(tmp_path / "tokens"))
    assert ga.TokenStore("you").path == tmp_path / "tokens" / "you.json"
