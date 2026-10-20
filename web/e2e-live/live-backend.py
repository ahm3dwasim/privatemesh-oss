"""A LIVE-mode backend with no credentials, for the live E2E suite's gate assertions.

Why this file exists
--------------------
`serve --v2 --live` refuses to start without a Google account, a Tavily key and a Nebius key,
because a live product that cannot reach its providers is an outage dressed as a feature. That
refusal is correct and this file does not weaken it: it does not touch the CLI, `build_live` or
`require_ready`.

What it does instead is the same thing `tests/test_wiring_live.py` does — hand `build_live` the
collaborators it would otherwise reach the network for. A stub Google that answers `access_token`,
a protected commitment, placeholder key strings and an `httpx.MockTransport` that answers 599 to
every host. The wiring is then genuinely `live`: `mode == "live"`, a real `PrivacyGate`, a real
`TokenFactory`, and a **minted owner token** — which is the whole point, because the assertion this
server exists to carry is that a private route refuses a caller who does not present it.

No credential appears here. `"stub"` is a placeholder, not a key: nothing in the harness makes a
model, search or Gmail call, and the transport would fail loudly if something tried. Nothing binds
8765 or 3201 — the port is an argument, and `live-env.ts` owns it.

Not shipped: `web/e2e-live/` is the live suite's own directory, and this is the one piece of it
that has to be Python, because the thing under test is the Python gate.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from privatemesh.api import default_app  # noqa: E402
from privatemesh.config import Settings  # noqa: E402
from privatemesh.gmail import DEFAULT_LABEL  # noqa: E402
from privatemesh.ports import ProtectedCommitment  # noqa: E402
from privatemesh.wiring import build_live  # noqa: E402


class _ConnectedGoogle:
    """`probe_google` asks a provider exactly one question — `access_token()` — and reports the
    answer as the connection state. That is the whole surface a live wiring needs at STARTUP; the
    connectors hold this object and would use more of it, but nothing in this harness calls them."""

    store = None

    def access_token(self) -> str:
        return "stub-access-token-never-sent"


def _offline_transport() -> httpx.Client:
    """No host is reached: an accidental call surfaces as the loud failure it is rather than as a
    slow test.

    The exceptions are the EMPTY ACCOUNT, not a fabricated result. A live wiring starts its routine
    on boot, so `GET /v2/today` reads the calendar and the routine reads the inbox — both on paths
    the gate spec has to cross to reach the gate. A transport that refused those would make this
    harness crash where the real product returns an empty day, and the difference would be the
    harness's rather than the gate's. "The label exists and nothing is filed under it", "this
    calendar has no events this day": both are answers a real account gives every day, and no
    message, event or venue is ever invented."""
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gmail.googleapis.com":
            # The label id is opaque and this side never sees which name was asked for; echoing the
            # product's own default name back is what makes `label_id()` find it.
            if request.url.path.endswith("/labels"):
                return httpx.Response(200, json={"labels": [{"id": "Label_e2e", "name": DEFAULT_LABEL}]})
            if request.url.path.endswith("/messages"):
                return httpx.Response(200, json={"messages": []})
        if request.url.host == "www.googleapis.com":
            if request.url.path.endswith("/events"):
                return httpx.Response(200, json={"items": []})
            if "freeBusy" in request.url.path:
                return httpx.Response(200, json={"calendars": {}})
            if request.url.path.endswith("/calendarList"):
                return httpx.Response(200, json={"items": []})
        return httpx.Response(599, json={"error": f"the E2E live backend does not call {request.url.host}"})

    return httpx.Client(transport=httpx.MockTransport(handle), timeout=5)


def settings() -> Settings:
    # The model ids are the PRICED ones `tests/test_wiring_live.py` uses: the live wiring attaches
    # a real `SpendLedger`, and an unpriced model is refused there. A made-up id would build a
    # configuration that cannot run in production, which is not what this is standing in for.
    return Settings(
        api_key="stub", base_url="https://api.tokenfactory.nebius.com/v1/",
        model_fast="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        model_reason="nvidia/nemotron-3-super-120b-a12b",
        model_deep="nvidia/Nemotron-3-Ultra-550b-a55b",
        tavily_key="stub",
        web_origins=os.environ.get("PM_WEB_ORIGINS", ""),
    )


def build(data_dir: Path):
    return build_live(
        settings=settings(),
        google=_ConnectedGoogle(),
        http=_offline_transport(),
        commitment=ProtectedCommitment(
            id="pc-e2e", start="2026-09-19T16:30:00+01:00", end="2026-09-19T17:30:00+01:00",
            buffer_minutes=45, version=1, title="E2E protected commitment"),
        data_dir=data_dir,
        timezone="Europe/London",
    )


def _expose_route_listing(app) -> None:
    """Publish the APP'S OWN route table at `/e2e/routes`, outside `/v2`.

    The gate spec used to enumerate `/openapi.json`, and that is the sixth review's finding: the
    schema is a VIEW of the routes, and a route with `include_in_schema=False` — or one registered
    straight on the app, past the router builder — is not in it. The browser half of the gate
    therefore swept a list that was missing exactly the kind of route worth sweeping for.

    This is the app's `routes` attribute, read through the same three helpers the startup guard uses
    (`_walk_routes` descends into included routers and mounts and resolves each route's SERVED path;
    `_under_v2` is the namespace test; `_is_gated` asks the gate's own registry whether the handler
    went through it). Reimplementing any of them here would produce a second opinion about the gate,
    which is the thing that drifts; borrowing them means a change to the gate changes this listing.

    `public` is `_is_public` and not a lookup in `PUBLIC_V2_ROUTES`, which is the eighth review's
    second High: the allowlist says which `(method, path)` MAY be exempt and cannot say which route
    IS, so a listing built from the table would report a rogue handler at an allowlisted method and
    path as public — the exact route the sweep exists to notice. `request_gate` is published with
    it because the other four facts are all about routes, and the eighth review's first High was
    that the gate deciding about them ran once at boot: a browser asserting "this route refuses"
    against a server whose only boundary was a startup scan would be asserting the wrong thing.

    `/e2e/routes` is not under `/v2`, so it is not a route the gate has to decide about, and it is
    registered AFTER `build_app` so it cannot displace the startup check. It serves paths the server
    already publishes in its schema — no data, no credential, and it is not shipped: `web/e2e-live/`
    is the live suite's own directory.
    """
    from privatemesh.api_v2 import (
        _V2RequestGate, _is_gated, _is_public, _under_v2, _walk_routes)

    def routes() -> list[dict]:
        # Asked of the running app's stack rather than of a flag someone set: `build_app` installs
        # the gate and nothing else does, so its presence here is the same question the gate asks.
        live = [entry for entry in (getattr(app, "user_middleware", None) or [])
                if getattr(entry, "cls", None) is _V2RequestGate]
        out: list[dict] = []
        for path, route in _walk_routes(app.routes):
            if not _under_v2(path):
                continue
            methods = sorted((getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"})
            for method in methods:
                out.append({"method": method, "path": path, "gated": _is_gated(route),
                            "public": _is_public(route), "request_gate": bool(live)})
        return out

    app.add_api_route("/e2e/routes", routes, methods=["GET"])


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else 8833
    data_dir = Path(os.environ.get("PM_E2E_DATA_DIR", "/tmp/pm-e2e-live-backend"))
    data_dir.mkdir(parents=True, exist_ok=True)

    wiring = build(data_dir)
    # Printed BEFORE the server takes the process over, for the same reason the CLI does it: an
    # operator reading a log has to be told what mode came up and whether the gate is armed. The
    # path of the token file, never the token.
    print(f"E2E live backend: mode={wiring.mode} ready={wiring.ready} refusals={wiring.refusals}",
          flush=True)
    if not wiring.ready:
        # Not a crash: a wiring that came up unready would make every gate assertion below pass for
        # the WRONG reason, so the harness refuses to serve the test a false green.
        print("refusing to serve: this harness must produce a READY live wiring", file=sys.stderr)
        return 1
    wiring.start()
    settings_obj = settings()
    print(f"  owner token file: {os.environ.get('PM_OWNER_TOKEN_FILE', '(default)')}", flush=True)
    try:
        import uvicorn
        app = default_app(settings_obj, v2=wiring.service)
        _expose_route_listing(app)
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        wiring.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
