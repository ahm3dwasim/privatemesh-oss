"""`deploy/demo/{web.Dockerfile,Caddyfile}` — the live demo is a Next.js NODE server, not a static
export, and the demo API is reachable same-origin at `/v2/*`.

`web/` has no build mode where `NEXT_PUBLIC_API_MODE=demo` can be served as static files:
`web/scripts/build-guards.mjs`'s `assertStaticExportIsSample` refuses `PM_STATIC_EXPORT=1` with
anything but `NEXT_PUBLIC_API_MODE=sample` (pinned by that module's own `selfTest`, not repeated
here). These tests pin the deploy-kit side of that decision: the web image is built and run as a
real Next.js server in demo mode, and Caddy routes the paths `web/lib/demo-api.ts` actually calls
to the `api` service.

`deploy/demo/compose.yaml` is deliberately not asserted here. It decides neither of the two things
this module guards — which image runs, and where a request goes — so any assertion over that file
would hold just as well for a static-export demo as for this one, and would be evidence for
nothing. The `${PM_*}` port conventions it uses are a runbook matter (`docs/DEPLOY.md`).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = REPO_ROOT / "deploy" / "demo"
WEB_DOCKERFILE = DEMO_DIR / "web.Dockerfile"
CADDYFILE_PATH = DEMO_DIR / "Caddyfile"
DEMO_API_TS = REPO_ROOT / "web" / "lib" / "demo-api.ts"

# The one template `createDemoClient`'s `request()` helper builds every URL from:
# `fetch(`${base}/v2/demo${path}`)`. Read rather than hard-coded so moving the demo API under a
# different prefix moves what the routing test below checks with it.
_DEMO_PATH_TEMPLATE_RE = re.compile(r"fetch\(`\$\{base\}([^`$]*)")
# The call sites whose first argument is the path appended to that prefix.
_REQUEST_CALL_RE = re.compile(r"request\(\s*[`'\"]([^`'\"]*)")
# `${encodeURIComponent(sessionId)}` and friends, rendered as `*` so a path keeps its full shape.
_INTERPOLATION_RE = re.compile(r"\$\{[^}]*\}")

# One top-level directive inside the site block: `handle <matcher> {`, `handle_path <matcher> {` or
# a bare `handle {`. Requiring exactly ONE leading whitespace character is what limits this to
# depth-1 directives — a nested block's line opens with a second whitespace character, not `handle`.
_HANDLE_RE = re.compile(r"^[ \t](handle_path|handle)(?:\s+(\S+))?\s*\{([^}]*)\}", re.M)


def _demo_api_url_paths() -> set[str]:
    """Every path `createDemoClient` requests, as the same-origin URL path Caddy sees: the literal
    prefix its own `request()` helper builds into every URL (`/v2/demo`), followed by each call
    site's path with interpolated segments rendered as `*` — so the call site whose path is
    `/session/<id>/email` contributes `/v2/demo/session/*/email`.

    Read from the source rather than hard-coded, so a call site that moves outside the routed
    prefix fails the routing test below instead of quietly 404ing in a judge's browser."""
    text = DEMO_API_TS.read_text()
    template = _DEMO_PATH_TEMPLATE_RE.search(text)
    assert template, (
        "demo-api.ts has no `fetch(` template of the shape `${base}<prefix>${path}` — the scan is "
        "reading the wrong file, or the client no longer builds every URL in one place")
    calls = _REQUEST_CALL_RE.findall(text)
    assert calls, (
        "demo-api.ts has no `request(...)` call sites — the scan is reading the wrong file")
    return {template.group(1) + _INTERPOLATION_RE.sub("*", call) for call in calls}


def _caddy_routes() -> list[tuple[str | None, str]]:
    """Every top-level `handle`/`handle_path` in `deploy/demo/Caddyfile`, in the order Caddy
    evaluates them, as `(matcher, upstream)` — where a `None` matcher is the unconditional
    `handle {}` fallback, and an empty upstream is a block that proxies nowhere."""
    routes = []
    for _directive, matcher, body in _HANDLE_RE.findall(CADDYFILE_PATH.read_text()):
        upstream = re.search(r"reverse_proxy\s+(\S+)", body)
        routes.append((matcher or None, upstream.group(1) if upstream else ""))
    return routes


def _upstream_for(path: str) -> str | None:
    """The upstream Caddy sends `path` to, or `None` if nothing matches it. `handle` and
    `handle_path` are mutually exclusive per request, so the first route whose matcher accepts the
    path is the one that serves it — which is what makes the fallback's position, not just its
    presence, testable below.

    Only the matcher forms this Caddyfile uses are understood: `/*` path prefixes and the bare
    fallback. An unrecognised matcher form matches nothing, so a route rewritten into some other
    shape fails the tests below rather than silently passing them."""
    for matcher, upstream in _caddy_routes():
        if matcher is None or (matcher.endswith("/*") and path.startswith(matcher[:-1])):
            return upstream
    return None


def test_caddy_routes_every_path_the_demo_client_calls_to_the_api_service():
    """The two halves of one claim: `lib/demo-api.ts` builds every request from `${base}/v2/demo`
    with `base` empty (`NEXT_PUBLIC_API_BASE_URL` is unset in web.Dockerfile, so the calls are
    same-origin), and Caddy sends each of those same-origin paths to `api`.

    Checked per extracted path against the parsed route list, so all three ways this can go wrong
    fail here: no route for the prefix at all (the path falls through to the web app), a route
    re-prefixed to something else, and a route pointed at the wrong upstream."""
    dockerfile = WEB_DOCKERFILE.read_text()
    assert not re.search(r"^\s*(ENV|ARG)\s+NEXT_PUBLIC_API_BASE_URL\b", dockerfile, re.M), (
        "web.Dockerfile sets NEXT_PUBLIC_API_BASE_URL — with an absolute base the demo client no "
        "longer calls this origin, and the Caddy routes this test checks stop being what serves it")
    paths = _demo_api_url_paths()
    assert paths, "no demo API paths were extracted from lib/demo-api.ts"
    routed = {path: _upstream_for(path) for path in sorted(paths)}
    wrong = {path: upstream for path, upstream in routed.items() if upstream != "api:8765"}
    assert not wrong, (
        f"deploy/demo/Caddyfile does not route these demo API paths to `api`: {wrong}; "
        f"the routes it declares are {_caddy_routes()}")


def test_web_dockerfile_builds_a_next_server_not_a_static_export():
    text = WEB_DOCKERFILE.read_text()
    assert not re.search(r"^\s*(ENV|ARG)\s+PM_STATIC_EXPORT", text, re.M), (
        "web.Dockerfile sets PM_STATIC_EXPORT — that builds a static export, which "
        "assertStaticExportIsSample refuses for anything but NEXT_PUBLIC_API_MODE=sample")
    assert "serve@" not in text and '"serve"' not in text, (
        "web.Dockerfile still installs/runs the static file server `serve` — "
        "the live demo needs a real Next.js server (next start), not a static host")


def test_web_dockerfile_builds_in_demo_mode():
    text = WEB_DOCKERFILE.read_text()
    assert re.search(r"ENV\s+NEXT_PUBLIC_API_MODE=demo", text), (
        "web.Dockerfile does not set NEXT_PUBLIC_API_MODE=demo before `npm run build` — "
        "without it the root page is the sample workspace, not the live demo "
        "(web/app/page.tsx branches on apiMode at build time)")
    build_stage, _, runtime_stage = text.partition("FROM node:20-slim\n")
    assert "npm run build" in build_stage
    assert "NEXT_PUBLIC_API_MODE=demo" in build_stage.split("RUN npm run build")[0]


def test_web_dockerfile_runs_next_start_as_a_non_root_user():
    text = WEB_DOCKERFILE.read_text()
    assert re.search(r'CMD\s*\[\s*"npm"\s*,\s*"start"\s*\]', text), (
        "web.Dockerfile's CMD does not run `npm start` (next start) — "
        "check web/package.json's \"start\" script is what this launches")
    assert re.search(r"^USER\s+\S+", text, re.M), "web.Dockerfile never switches off root"
    assert not re.search(r"^USER\s+root\s*$", text, re.M)


def test_caddyfile_routes_v2_to_the_api_service():
    """`web/lib/demo-api.ts` fetches `/v2/demo/*` same-origin (no NEXT_PUBLIC_API_BASE_URL set in
    web.Dockerfile) — Caddy must proxy that prefix to `api`, unmodified (the api container mounts
    its routes at `/v2/demo/*` directly, `api.py`'s `mount_v1=False` build)."""
    text = CADDYFILE_PATH.read_text()
    match = re.search(r"handle\s+/v2/\*\s*\{([^}]*)\}", text)
    assert match, "Caddyfile has no `handle /v2/*` block routing the demo API"
    assert "reverse_proxy api:8765" in match.group(1)


def test_caddy_sends_the_pages_to_web_without_shadowing_the_api_route():
    """The catch-all fallback is correct only in the position it holds, which is why it is asserted
    through the same ordered route list rather than on its own: `/`, `/tour` and the built assets
    have to reach the web app, while `/v2/*` still has to reach `api` first.

    Both halves fail if the fallback moves above `handle /v2/*` — the API calls would be swallowed
    into the web app and 404 at the landing page's first request — and the first half fails if the
    fallback stops pointing at `web`.

    The API half is not written here: it is the set of same-origin paths `lib/demo-api.ts` actually
    requests, extracted by the same scan the test above uses. A demo call site that moves then
    changes what this asserts, instead of leaving a hard-coded path that keeps passing while the
    call it stood for 404s."""
    expected = {
        "/": "web:3000",
        "/tour": "web:3000",
        "/_next/static/chunks/main-app.js": "web:3000",
        "/design-shots/demo/01-session-started-1440.png": "web:3000",
    }
    expected.update({path: "api:8765" for path in _demo_api_url_paths()})
    actual = {path: _upstream_for(path) for path in expected}
    assert actual == expected, f"Caddyfile routes {actual}; expected {expected}"


def test_caddyfile_v2_route_is_ordered_before_the_fallback():
    """Caddy evaluates `handle` blocks in the order they appear for a given request path; if the
    catch-all `handle {}` were written before `handle /v2/*`, the api route would never be
    reached — a mistake this test catches statically rather than only by hitting it at runtime."""
    text = CADDYFILE_PATH.read_text()
    v2_pos = text.index("handle /v2/*")
    fallback_pos = re.search(r"handle\s*\{", text[v2_pos + 1:])
    assert fallback_pos, "no fallback handle block found after the /v2/* route"
