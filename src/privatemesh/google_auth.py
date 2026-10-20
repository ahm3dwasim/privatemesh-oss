"""Google OAuth 2.0 for an installed app: PKCE, a loopback redirect, and tokens kept off-repo.

Three decisions worth stating, because each one is a boundary rather than a convenience:

* **PKCE with a loopback redirect**, not a client secret typed by a human. The desktop client
  secret ships in every copy of an installed app, so it is not a secret; the code verifier is.
  The redirect is `http://127.0.0.1:<random port>/callback` and the listener binds loopback only.
* **Tokens live outside the checkout** (`~/.privatemesh/google/<account>.json`, mode 0600) and are
  never logged: the dataclass repr, the config repr and every log line are redacted, because a
  token in a log file is a token in the next bug report.
* **`invalid_grant` becomes `ports.ReauthRequired`, never a retry loop.** Google expires refresh
  tokens for apps still in Testing mode after 7 days. That is a normal lifecycle event, and the UI
  is expected to say "Reconnect Google" rather than fail quietly.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import httpx

from .ports import ReauthRequired

log = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
)
#: The checkout's own `secrets/google_client.json` (gitignored), found from THIS file rather than
#: spelled as a path fixed to one development machine's layout. It used to be hard-coded to a
#: specific developer folder, true only for as long as the checkout stayed at that exact place —
#: when the checkout moved (2026-09-22) the live product would have lost its client secret, and —
#: worse — the demo's credential refusal, which checks this exact path, would have gone on passing
#: beside a real secret it could no longer see.
DEFAULT_CLIENT_PATH = str(Path(__file__).resolve().parents[2] / "secrets" / "google_client.json")
DEFAULT_TOKEN_DIR = "~/.privatemesh/google"
#: Every default filesystem location a real Google credential for this adapter may live at,
#: named once so a caller that must refuse on their PRESENCE — `demo_session.
#: real_provider_credentials` — checks exactly this set rather than re-typing the two strings
#: above a second time. `demo_session` used to do that, and the copy drifted: it checked
#: `~/.privatemesh/google-client.json` while this adapter's own default was (and is)
#: the checkout's `secrets/google_client.json` — different directory, different
#: filename — so a demo process on a machine with a REAL client secret at the real default path
#: started anyway. Importing this tuple is what makes that drift impossible rather than merely
#: fixed once.
CREDENTIAL_DEFAULT_PATHS = (DEFAULT_CLIENT_PATH, DEFAULT_TOKEN_DIR)
ACCOUNT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
# Refresh this many seconds before the stated expiry: a token that expires mid-request is a 401
# that looks like a permissions problem.
EXPIRY_SKEW_SECONDS = 120


class GoogleAuthError(RuntimeError):
    """The OAuth flow itself failed (bad config, denied consent, timed out, state mismatch)."""


class GoogleClientConfigError(GoogleAuthError):
    """No usable installed-app client config; the operator has to create one."""


# ---------------------------------------------------------------- PKCE + URLs -----------------
def new_code_verifier() -> str:
    """RFC 7636 §4.1: 43-128 chars from the unreserved set. 32 random bytes gives 43."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def redirect_uri_for(port: int) -> str:
    return f"http://127.0.0.1:{port}/callback"


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    client_secret: str
    auth_uri: str = GOOGLE_AUTH_URL
    token_uri: str = GOOGLE_TOKEN_URL

    def __repr__(self) -> str:  # a client secret in a traceback is still a leaked secret
        return (f"ClientConfig(client_id={self.client_id!r}, client_secret='<redacted>', "
                f"auth_uri={self.auth_uri!r}, token_uri={self.token_uri!r})")


def client_config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return Path(os.environ.get("PM_GOOGLE_CLIENT") or DEFAULT_CLIENT_PATH).expanduser()


def load_client_config(path: str | Path | None = None) -> ClientConfig:
    p = client_config_path(path)
    if not p.exists():
        raise GoogleClientConfigError(
            f"no Google client config at {p} — download the OAuth client (Desktop app) JSON "
            f"from Google Cloud Console and either put it there or point PM_GOOGLE_CLIENT at it")
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        raise GoogleClientConfigError(f"Google client config at {p} is unreadable: {exc!r}") from exc
    node = raw.get("installed") or raw.get("web") or raw
    client_id = str(node.get("client_id") or "")
    if not client_id:
        raise GoogleClientConfigError(f"Google client config at {p} has no client_id")
    return ClientConfig(client_id=client_id,
                        client_secret=str(node.get("client_secret") or ""),
                        auth_uri=str(node.get("auth_uri") or GOOGLE_AUTH_URL),
                        token_uri=str(node.get("token_uri") or GOOGLE_TOKEN_URL))


def authorization_url(config: ClientConfig, *, redirect_uri: str, state: str,
                      challenge: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        # Without both of these Google hands back a refresh token only on the very first consent,
        # so a re-run of `google-connect` would silently leave the owner without one.
        "access_type": "offline",
        "prompt": "consent",
    })
    return f"{config.auth_uri}?{query}"


# ---------------------------------------------------------------- the token --------------------
@dataclass(frozen=True)
class GoogleToken:
    access_token: str
    refresh_token: str
    expires_at: float                     # epoch seconds
    scopes: tuple[str, ...]
    account: str = ""
    token_type: str = "Bearer"

    def expired(self, now: float, skew: float = EXPIRY_SKEW_SECONDS) -> bool:
        return self.expires_at - skew <= now

    def to_json(self) -> dict:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "expires_at": self.expires_at, "scopes": list(self.scopes),
                "account": self.account, "token_type": self.token_type}

    @classmethod
    def from_json(cls, data: dict, *, account: str = "") -> "GoogleToken":
        return cls(access_token=str(data.get("access_token") or ""),
                   refresh_token=str(data.get("refresh_token") or ""),
                   expires_at=float(data.get("expires_at") or 0.0),
                   scopes=tuple(data.get("scopes") or ()),
                   account=str(data.get("account") or account),
                   token_type=str(data.get("token_type") or "Bearer"))

    def __repr__(self) -> str:  # dataclass repr would print both tokens verbatim
        return (f"GoogleToken(account={self.account!r}, expires_at={self.expires_at!r}, "
                f"scopes={self.scopes!r}, access_token='<redacted>', refresh_token='<redacted>')")

    __str__ = __repr__


def validate_account(account: str) -> str:
    """The account name is a filename, so it is validated like one."""
    if not ACCOUNT_RE.fullmatch(account or ""):
        raise ValueError(f"invalid account name {account!r}: expected [a-z0-9][a-z0-9_-]* (max 32)")
    return account


class TokenStore:
    """`~/.privatemesh/google/<account>.json`, 0600, written atomically."""

    def __init__(self, account: str, directory: str | Path | None = None):
        self.account = validate_account(account)
        self.directory = Path(directory).expanduser() if directory is not None \
            else Path(os.environ.get("PM_GOOGLE_TOKEN_DIR") or DEFAULT_TOKEN_DIR).expanduser()

    @property
    def path(self) -> Path:
        return self.directory / f"{self.account}.json"

    def load(self) -> GoogleToken | None:
        if not self.path.exists():
            return None
        try:
            return GoogleToken.from_json(json.loads(self.path.read_text()), account=self.account)
        except (OSError, ValueError) as exc:
            raise GoogleAuthError(
                f"stored Google token for '{self.account}' is unreadable — reconnect Google: "
                f"{exc!r}") from exc

    def save(self, token: GoogleToken) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        fd, tmp = tempfile.mkstemp(dir=str(self.directory), prefix=f".{self.account}-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(token.to_json(), fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        return self.path

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------- the HTTP calls --------------
def _token_request(config: ClientConfig, form: dict, *, http: httpx.Client | None,
                   ) -> tuple[int, dict]:
    client = http or httpx.Client(timeout=30)
    try:
        r = client.post(config.token_uri, data=form,
                        headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise GoogleAuthError(f"could not reach the Google token endpoint: {exc!r}") from exc
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    return r.status_code, payload if isinstance(payload, dict) else {}


def _token_from_response(payload: dict, *, account: str, previous: GoogleToken | None,
                         now: float) -> GoogleToken:
    return GoogleToken(
        access_token=str(payload.get("access_token") or ""),
        # Google returns a refresh token only on the first consent; on every later refresh it is
        # omitted, so losing the stored one here would silently break the next run.
        refresh_token=str(payload.get("refresh_token") or (previous.refresh_token if previous else "")),
        expires_at=now + float(payload.get("expires_in") or 3600),
        scopes=tuple(str(payload.get("scope") or "").split()) or (previous.scopes if previous else SCOPES),
        account=account,
        token_type=str(payload.get("token_type") or "Bearer"),
    )


def exchange_code(config: ClientConfig, *, code: str, code_verifier: str, redirect_uri: str,
                  http: httpx.Client | None = None, account: str = "",
                  now: Callable[[], float] = time.time) -> GoogleToken:
    status, payload = _token_request(config, {
        "grant_type": "authorization_code", "code": code, "code_verifier": code_verifier,
        "redirect_uri": redirect_uri, "client_id": config.client_id,
        "client_secret": config.client_secret}, http=http)
    if status != 200 or not payload.get("access_token"):
        raise GoogleAuthError(f"Google refused the authorization code (HTTP {status}, "
                              f"error={payload.get('error', 'unknown')})")
    log.debug("google authorization code exchanged for account=%s", account or "(unnamed)")
    return _token_from_response(payload, account=account, previous=None, now=now())


def refresh_access_token(config: ClientConfig, token: GoogleToken, *,
                         http: httpx.Client | None = None,
                         now: Callable[[], float] = time.time) -> GoogleToken:
    """A refresh that fails with invalid_grant is a *reconnect*, not an error to retry."""
    status, payload = _token_request(config, {
        "grant_type": "refresh_token", "refresh_token": token.refresh_token,
        "client_id": config.client_id, "client_secret": config.client_secret}, http=http)
    if status != 200 or not payload.get("access_token"):
        error = str(payload.get("error") or "")
        if error == "invalid_grant":
            # Only this one means "the owner must consent again". Google expires refresh tokens
            # for apps still in Testing mode after 7 days, which is a normal lifecycle event.
            raise ReauthRequired(
                f"Google no longer accepts the stored token for '{token.account or 'this account'}' "
                f"({error}) — Testing-mode tokens expire after 7 days. "
                f"Reconnect Google: privatemesh google-connect --account "
                f"{token.account or 'you'}")
        if error in ("invalid_client", "unauthorized_client", "invalid_request"):
            # A wrong client id/secret is a setup fault. Saying "Reconnect Google" here would send
            # the owner round a consent loop that cannot possibly fix it.
            raise GoogleClientConfigError(
                f"Google rejected the OAuth client configuration ({error}) — check the client "
                f"JSON at {client_config_path()} and that PM_GOOGLE_CLIENT points at it")
        raise GoogleAuthError(f"Google token refresh failed (HTTP {status}, error={error or 'unknown'})")
    log.debug("google token refreshed for account=%s", token.account or "(unnamed)")
    return _token_from_response(payload, account=token.account, previous=token, now=now())


class TokenProvider(Protocol):
    """What the connectors need: one valid bearer token, refreshed underneath if necessary."""

    def access_token(self) -> str: ...


class GoogleAuth:
    """Owns one account's token: loads it, refreshes it on expiry, and never logs it."""

    def __init__(self, account: str, *, http: httpx.Client | None = None,
                 config: ClientConfig | None = None, store: TokenStore | None = None,
                 now: Callable[[], float] = time.time):
        self.account = validate_account(account)
        self.store = store or TokenStore(self.account)
        self._http = http
        self._config = config
        self._now = now
        self._cached: GoogleToken | None = None

    @property
    def config(self) -> ClientConfig:
        if self._config is None:
            self._config = load_client_config()
        return self._config

    def token(self) -> GoogleToken:
        token = self._cached or self.store.load()
        if token is None:
            raise ReauthRequired(
                f"no stored Google token for '{self.account}' — connect it first: "
                f"privatemesh google-connect --account {self.account}")
        if token.expired(self._now()):
            token = self.refresh(token)
        self._cached = token
        return token

    def access_token(self) -> str:
        return self.token().access_token

    def refresh(self, token: GoogleToken | None = None, *, config: ClientConfig | None = None,
                ) -> GoogleToken:
        current = token or self._cached or self.store.load()
        if current is None:
            raise ReauthRequired(f"nothing to refresh for '{self.account}' — reconnect Google")
        refreshed = refresh_access_token(config or self.config, current, http=self._http,
                                         now=self._now)
        self.store.save(refreshed)
        self._cached = refreshed
        return refreshed


# ---------------------------------------------------------------- loopback listener -----------
@dataclass(frozen=True)
class CallbackResult:
    code: str | None
    state: str | None
    error: str | None


def parse_callback(path: str) -> CallbackResult | None:
    """None means 'not our callback' — the browser asks for /favicon.ico too."""
    parsed = urllib.parse.urlparse(path)
    if parsed.path != "/callback":
        return None
    q = urllib.parse.parse_qs(parsed.query)
    code = (q.get("code") or [None])[0]
    error = (q.get("error") or [None])[0]
    if not code and not error:
        return None
    return CallbackResult(code=code, state=(q.get("state") or [None])[0], error=error)


_DONE_PAGE = (b"<!doctype html><meta charset=utf-8><title>PrivateMesh</title>"
              b"<body style='font:16px system-ui;padding:2rem'>"
              b"<h1>PrivateMesh is connected.</h1><p>You can close this tab.</p>")


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server's spelling)
        result = parse_callback(self.path)
        if result is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.server.listener.result = result  # type: ignore[attr-defined]
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_DONE_PAGE)))
        self.end_headers()
        self.wfile.write(_DONE_PAGE)

    def log_message(self, *args) -> None:
        """Silenced on purpose: the request line carries the authorization code."""


class LoopbackListener:
    """A one-shot loopback listener on a random port. The socket is loopback-only by binding."""

    def __init__(self, host: str = "127.0.0.1"):
        self._server = http.server.HTTPServer((host, 0), _CallbackHandler)
        self._server.listener = self            # type: ignore[attr-defined]
        self._server.timeout = 1.0
        self.port = int(self._server.server_address[1])
        self.redirect_uri = redirect_uri_for(self.port)
        self.result: CallbackResult | None = None

    def wait(self, timeout: float) -> CallbackResult:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._server.timeout = max(0.2, min(1.0, deadline - time.monotonic()))
            self._server.handle_request()
            if self.result is not None:
                return self.result
        raise GoogleAuthError(f"no OAuth callback within {int(timeout)}s — re-run "
                              f"`privatemesh google-connect` when the browser is ready")

    def close(self) -> None:
        self._server.server_close()


class _Listener(Protocol):
    port: int
    redirect_uri: str
    def wait(self, timeout: float) -> CallbackResult: ...
    def close(self) -> None: ...


def run_loopback_flow(config: ClientConfig, *, account: str, http: httpx.Client | None = None,
                      timeout: float = 300.0,
                      listener_factory: Callable[[], _Listener] = LoopbackListener,
                      on_url: Callable[[str], None] | None = None,
                      open_browser: Callable[[str], object] | None = None,
                      state: str | None = None, store: TokenStore | None = None,
                      now: Callable[[], float] = time.time) -> GoogleToken:
    """Print the consent URL, wait for the redirect, exchange the code. Never opens a port first
    for an account name that could not be stored anyway."""
    validate_account(account)
    listener = listener_factory()
    try:
        challenge_state = state or secrets.token_urlsafe(24)
        verifier = new_code_verifier()
        url = authorization_url(config, redirect_uri=listener.redirect_uri,
                                state=challenge_state, challenge=code_challenge(verifier))
        if on_url is not None:
            on_url(url)
        if open_browser is not None:
            open_browser(url)
        result = listener.wait(timeout)
        if result.state != challenge_state:
            raise GoogleAuthError("the OAuth callback did not carry the state we sent — refusing it")
        if result.error:
            raise GoogleAuthError(f"Google returned an error instead of a code: {result.error}")
        if not result.code:
            raise GoogleAuthError("the OAuth callback carried no authorization code")
        token = exchange_code(config, code=result.code, code_verifier=verifier,
                              redirect_uri=listener.redirect_uri, http=http, account=account,
                              now=now)
    finally:
        listener.close()
    (store or TokenStore(account)).save(token)
    log.info("google account '%s' connected", account)
    return token


# ---------------------------------------------------------------- CLI -------------------------
def connect(account: str, *, open_browser: bool = True, timeout: float = 300.0) -> int:
    config = load_client_config()
    print(f"Connecting Google account '{account}'.")
    print("Scopes requested (read the mail you select, send replies, read and change calendar "
          "events you organise):")
    for scope in SCOPES:
        print(f"  - {scope}")
    token = run_loopback_flow(
        config, account=account, timeout=timeout,
        on_url=lambda url: print(f"\nOpen this URL and approve access:\n\n  {url}\n"),
        open_browser=_open_browser if open_browser else None)
    print(f"Connected. Token stored at {TokenStore(account).path} (mode 0600).")
    print(f"Scopes granted: {' '.join(token.scopes) or '(none reported)'}")
    return 0


def _open_browser(url: str) -> None:
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:  # a headless box is fine: the URL is already printed
        log.debug("could not open a browser automatically")
