"""Finding 1: every private route needs the owner token in live mode, and a NEW route is gated by
default.

The fourth review's Critical was not that one route was missing a check — it was that the check was
a line someone had to remember to write, and nine routes were written without it (`/session`,
`/today`, `/changes/{id}`, `/outcome`, `/why`, `/plans`, `/preferences`, `/skills`,
`/learning/{id}`). A test that lists those nine by hand would certify today's file and say nothing
about tomorrow's, so the test below lists NOTHING by hand: it enumerates the router, and every route
it finds must either refuse a caller carrying no token or be named in `PUBLIC_V2_ROUTES` with a
reason. Adding a route to `api_v2.py` without deciding who may call it now fails here.

That is also why each request body is GENERATED from the route's own pydantic model rather than
written out. A hand-written body is a hand-maintained list of today's routes wearing a different
hat: the first route added after this test was written would fail with a 422 for a body the test did
not know to build, which is indistinguishable from the gate working.
"""
from __future__ import annotations

import functools
import typing

import pytest
from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import WebSocketDisconnect

from privatemesh.api_v2 import (APPROVAL_FORBIDDEN, DECISIONS_FORBIDDEN, PUBLIC_V2_ROUTES,
                                SELF_NAMED_GATE_CODES, V2Error, build_v2_router)

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

#: The token the tests present. Deliberately NOT the one the service was built with, so a stray
#: default in the wiring cannot make a refusal below pass for the wrong reason.
TOKEN = "tok_" + "a" * 40


def _harness() -> Harness:
    return Harness(script=SCRIPT, messages=[change_email("m1")],
                   search={"king's cross": [evidence("https://thestandard.co.uk/",
                                                     "The Standard, King's Cross",
                                                     "Open daily 12:00-23:00")]})


def _live_router(**kw):
    """A LIVE service on the v2 router — the mode where the gate is load-bearing."""
    h = _harness()
    service = h.v2(mode="live", owner_token=TOKEN, owner_name="Sam Owner",
                   timezone="Europe/London", **kw)
    router = build_v2_router(service)
    app = FastAPI()
    app.include_router(router)
    # The router is handed back beside the client: the enumeration reads the ROUTER, so it is the
    # thing this fix changed rather than whatever the app happened to mount.
    return h, service, TestClient(app), router


_PLACEHOLDER = "route-param"


def _example(annotation):
    """A value of the right SHAPE for one required field. The gate refuses before the handler reads
    it, so the value never has to be meaningful — only well-typed enough to reach the gate."""
    if annotation is bool:
        return False
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if annotation is str:
        return _PLACEHOLDER
    if typing.get_origin(annotation) is typing.Union:                 # Optional[...]
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _example(args[0]) if args else None
    if typing.get_origin(annotation) in (list, set, tuple):
        return []
    if typing.get_origin(annotation) is dict:
        return {}
    return _PLACEHOLDER


def _body(route: APIRoute):
    """The smallest body this route will accept, built from the model it declares.

    The model is reached through the field's `annotation` rather than through a `type_` attribute,
    which pydantic v2's `ModelField` does not have — reading the model off the field is what keeps
    this working when FastAPI changes which field object it builds."""
    field = route.body_field
    if field is None:
        return None
    model = field.field_info.annotation
    fields = getattr(model, "model_fields", None)
    if fields is None:
        return {}
    return {name: _example(info.annotation) for name, info in fields.items() if info.is_required()}


def _url(path: str) -> str:
    """The path with every `{param}` replaced by a value. Every convertor on this router is `str`,
    so any non-empty segment matches — and the gate refuses before a handler could use it."""
    out: list[str] = []
    depth = 0
    for char in path:
        if char == "{":
            depth += 1
            out.append(_PLACEHOLDER)
        elif char == "}":
            depth -= 1
        elif depth == 0:
            out.append(char)
    return "".join(out)


def _walk(routes):
    """Every `APIRoute` reachable from a route list, descending into mounted routers.

    `include_router` does not flatten in this FastAPI version — it appends one `_IncludedRouter`
    that holds the routes — so a walk that only looked at the top level would find nothing and every
    control below would pass vacuously. Descending is what keeps this test honest across the
    router-mounting change, and it is also why the assertion below is not optional.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        # `include_router` wraps a router in an `_IncludedRouter` whose routes live on
        # `original_router`; a plain mounted router keeps them on `.routes`.
        inner = getattr(route, "routes", None) or getattr(
            getattr(route, "original_router", None), "routes", None)
        if inner:
            yield from _walk(inner)


def _routes(router) -> list[tuple[str, str, APIRoute]]:
    """Every `(METHOD, path, route)` the router serves, read off the router rather than from a list."""
    found: list[tuple[str, str, APIRoute]] = []
    for route in _walk(router.routes):
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            found.append((method, route.path, route))
    assert found, "the router served no routes; every control below would be vacuous"
    return found


def _call(api: TestClient, method: str, url: str, body, headers=None):
    return api.request(method, url, json=body, headers=headers or {})


# ================================================ the enumeration
def test_every_v2_route_either_refuses_an_anonymous_caller_or_is_named_public():
    """The Critical, enumerated rather than sampled.

    The route list comes from the router, so the failure this test exists to catch — a route that
    was added and never gated — appears here the moment the route exists."""
    _h, _service, api, router = _live_router()

    public, gated = [], []
    for method, path, route in _routes(router):
        response = _call(api, method, _url(path), _body(route))
        if (method, path) in PUBLIC_V2_ROUTES:
            public.append((method, path))
            continue
        assert response.status_code == 403, (
            f"{method} {path} answered {response.status_code} to a caller with no owner token")
        assert response.json().get("code"), f"{method} {path} refused without saying why"
        gated.append((method, path))

    # Both halves are load-bearing. A gate that refuses everybody is a different bug, so the list
    # of what is exercised has to be as long as the contract; and every exemption has to be one the
    # allowlist actually claimed, not a route that slipped past the `continue` above.
    assert len(gated) >= 25, f"only {len(gated)} routes were exercised: {gated}"
    assert sorted(public) == sorted(PUBLIC_V2_ROUTES), (
        "an exempted route was not exercised, or a route skipped the gate without being exempted")


def test_the_owner_token_is_what_opens_the_gated_routes():
    """The other half of the same fact: the requests above were refused for the token they lacked,
    not because the routes are broken. Without this, a router that 403s everything would pass."""
    h, _service, api, _router = _live_router()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    change_id = h.engine.detect(None)[0].id

    assert api.get("/v2/session", headers=headers).json()["authenticated_owner"] is True
    assert api.get("/v2/today", headers=headers).status_code == 200
    assert api.get("/v2/plans", headers=headers).status_code == 200
    assert api.get("/v2/preferences", headers=headers).status_code == 200
    assert api.get("/v2/skills", headers=headers).status_code == 200
    assert api.get(f"/v2/changes/{change_id}", headers=headers).status_code == 200


def test_the_public_allowlist_is_the_only_way_off_the_gate():
    """An allowlist is only an allowlist if its entries are examined. Every name in it must be a
    route that still EXISTS — or it is a stale exemption nobody re-read — and one that cannot reach
    the owner's data, or it is the defect with an exemption note stapled to it."""
    _h, _service, api, router = _live_router()
    paths = {route.path for route in _walk(router.routes)}

    for _method, path in sorted(PUBLIC_V2_ROUTES):
        assert path in paths, f"{path} is exempted but no longer exists — delete the exemption"

    # The one entry earns its place by refusing in live mode outright, before any owner check could
    # matter: what it reaches is a sample world built for the request, not the owner's. The body is
    # well-formed on purpose, so the refusal asserted here is the mode's and not the body's.
    attack = api.post("/v2/playground/attack", json={"email": "From: a@b.c\n\nhi"})
    assert attack.status_code == 404 and attack.json()["code"] == "not_found"


def test_a_route_added_later_is_gated_without_being_asked():
    """The mutation control for the whole fix.

    This route is registered on a router built by the same call as every other, and it is in no
    list. If the gate were per-handler — a decorator someone has to remember — this route would be
    public, which is exactly how the nine routes in the finding came to be public."""
    _h, service, api, _router = _live_router()
    router = build_v2_router(service)

    @router.get("/brand-new-private-thing")
    def brand_new(request: Request) -> dict:
        return {"owner_only": True}

    api.app.include_router(router)
    assert api.get("/v2/brand-new-private-thing").status_code == 403
    assert api.get("/v2/brand-new-private-thing",
                   headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_a_gated_route_that_forgets_the_request_fails_to_build():
    """The other half of default-deny: a gate that cannot read the token must not become a gate
    that lets everyone through. A route with no `request: Request` raises while the router is being
    BUILT, so the mistake is a failure to start rather than a silently public route."""
    _h, service, _api, _router = _live_router()
    router = build_v2_router(service)

    with pytest.raises(RuntimeError) as refused:

        @router.get("/forgot-the-request")
        def forgot() -> dict:
            return {"owner_only": True}

    assert "request: Request" in str(refused.value)


def test_the_documented_route_codes_survive_the_router_gate():
    """`docs/API_V2.md` names a code for two routes, and the web client branches on both. The gate
    sits OUTSIDE the handler, so it would have answered `owner_forbidden` for each and quietly
    retired them; `SELF_NAMED_GATE_CODES` is what keeps the sentence each route already used."""
    h, _service, api, _router = _live_router()
    change_id = h.engine.detect(None)[0].id

    approve = api.post(f"/v2/changes/{change_id}/approve", json={"version": 1})
    assert approve.status_code == 403 and approve.json()["code"] == "approval_forbidden"

    decisions = api.get(f"/v2/changes/{change_id}/decisions")
    assert decisions.status_code == 403 and decisions.json()["code"] == "decisions_forbidden"

    assert set(SELF_NAMED_GATE_CODES.values()) == {"approval_forbidden", "decisions_forbidden"}
    # Everything else gets the default, which is what a route that names nothing gets.
    assert api.get("/v2/plans").json()["code"] == "owner_forbidden"


def test_the_self_named_checks_are_enforced_on_the_service_and_not_only_at_the_router():
    """The same three routes, driven at the SERVICE, past the router.

    Two layers refuse an anonymous caller on these routes — the route class outside the handler and
    the handler's own `require_owner` — and that is deliberate. It also means either one can be
    removed without any HTTP test in this suite noticing, which the mutation check found the moment
    the router gate went in: `MV-1` (the model view's own check) stopped being covered, because the
    outer gate answered first. A guard that no test can see is a guard nobody has tested, so the
    inner layer gets a test that reaches it — every Python caller in this repo can, since the
    service is an object and not a URL."""
    _h, service, _api, _router = _live_router()

    def refusal(call):
        """The refusal one call with no token produces — asserted to BE a refusal.

        Caught broadly and asserted about rather than left to `pytest.raises(V2Error)`, because of
        what the guard's absence actually looks like: the call does not politely refuse, it runs on
        and dies on a change that does not exist. That `KeyError` escaping the test would leave this
        control reported as "went red without an assertion" — evidence the guard is reached and no
        evidence at all about what it does — which is the very outcome the mutation runner exists to
        name. Stating the abort here is what turns it back into an assertion."""
        try:
            call()
        except V2Error as exc:
            return exc
        except Exception as exc:                                  # noqa: BLE001
            raise AssertionError(f"the call did not refuse; it raised {type(exc).__name__}: {exc}")
        raise AssertionError("a service call with no owner token was served")

    for call, code in (
        (lambda: service.approve("c1", 1), APPROVAL_FORBIDDEN),
        (lambda: service.decisions("c1"), DECISIONS_FORBIDDEN),
        (lambda: service.model_view("c1"), DECISIONS_FORBIDDEN),
    ):
        refused = refusal(call)
        assert refused.status_code == 403, "a service call with no token was not refused"
        assert refused.code == code, (
            f"the route's own documented code was replaced by {refused.code}")


# ================================================ sample mode is untouched
def test_the_sample_needs_no_token_on_any_of_them():
    """The demo is the zero-credential path: a judge starts it with nothing and clicks through it.
    The gate is a no-op there because `is_owner` answers from the sample's own claim — a property of
    the SERVICE rather than an exception carved into the router, so it cannot drift from the mode."""
    h = _harness()
    router = build_v2_router(h.v2(mode="sample", owner_name="Sam Owner"))
    app = FastAPI()
    app.include_router(router)
    api = TestClient(app)

    for method, path, route in _routes(router):
        if (method, path) in PUBLIC_V2_ROUTES:
            continue
        response = _call(api, method, _url(path), _body(route))
        assert response.status_code != 403, (
            f"{method} {path} refused the sample, which has no token to present")


# ================================================ the gate cannot be defaulted around
def _app_with_v2(**kw):
    """A real app with the v2 router mounted the way `build_app` mounts it."""
    _h, _service, _api, router = _live_router(**kw)
    app = FastAPI()
    app.include_router(router)
    return app


def test_a_route_that_opts_out_of_the_gated_route_class_is_caught_at_startup():
    """The fifth review's High. `route_class` is a DEFAULT, and FastAPI has an override.

    `_gated_router` sets `route_class=_OwnerGatedRoute`, so every route registered through the
    decorators is wrapped. `add_api_route(..., route_class_override=APIRoute)` builds its route with
    plain `APIRoute` instead: `_OwnerGatedRoute.__init__` never runs, the handler is never wrapped,
    and the route answers anonymously. The startup check asks the HANDLER whether it went through
    the gate, which is a question no default can answer for it.
    """
    from privatemesh.api_v2 import assert_every_v2_route_is_gated

    app = _app_with_v2()

    def internal() -> dict:
        return {"owner_only": True}

    app.router.add_api_route("/v2/internal", internal, methods=["GET"],
                             route_class_override=APIRoute, include_in_schema=False)

    # It really is reachable, so the assertion below is about the guard and not about a route that
    # would have 404'd anyway — a hidden route is the shape the schema-based enumeration misses.
    assert TestClient(app).get("/v2/internal").status_code == 200

    with pytest.raises(RuntimeError) as refused:
        assert_every_v2_route_is_gated(app)
    assert "GET /v2/internal" in str(refused.value), str(refused.value)


def test_a_route_added_straight_to_the_app_is_caught_at_startup():
    """The other way past the router: never touch it. `app.add_api_route` is a public FastAPI call
    and nothing in this module can intercept it, so the check has to run over the APP."""
    from privatemesh.api_v2 import assert_every_v2_route_is_gated

    app = _app_with_v2()

    def mounted() -> dict:
        return {"owner_only": True}

    app.add_api_route("/v2/mounted-directly", mounted, methods=["GET"])

    with pytest.raises(RuntimeError) as refused:
        assert_every_v2_route_is_gated(app)
    assert "GET /v2/mounted-directly" in str(refused.value), str(refused.value)


def test_a_raw_starlette_route_is_caught_at_startup():
    """The seventh review's High, first shape: `app.add_route` is a Starlette call.

    It builds a plain `starlette.routing.Route`, which is not an `APIRoute`. The walk yielded
    `APIRoute`s only, so this route was not in the enumeration at all: the guard swept the app,
    found nothing to complain about, and `/v2/internal` answered a caller carrying no token.
    """
    from privatemesh.api_v2 import assert_every_v2_route_is_gated

    app = _app_with_v2()

    async def internal(request):
        return JSONResponse({"owner_only": True})

    app.add_route("/v2/internal", internal, methods=["GET"])

    # Measured, not assumed: a guard that flagged a route which would have 404'd anyway would be
    # reporting a defect nobody could exploit.
    assert TestClient(app).get("/v2/internal").status_code == 200

    with pytest.raises(RuntimeError) as refused:
        assert_every_v2_route_is_gated(app)
    assert "GET /v2/internal" in str(refused.value), str(refused.value)


def test_a_sub_app_mounted_under_v2_is_caught_at_startup():
    """The second shape: `Mount("/v2", subapp)`, and it needs both halves of the walk.

    Neither the mount nor its routes are `APIRoute`s, so the descent has to be by "does this entry
    delegate to a route list" rather than by class — and the route inside is spelled `/internal`,
    with the `/v2` living on the mount, so the path the guard judges has to be the one the app
    actually SERVES rather than the one written on the route object.
    """
    from privatemesh.api_v2 import assert_every_v2_route_is_gated

    app = _app_with_v2()

    async def internal(request):
        return JSONResponse({"owner_only": True})

    app.mount("/v2", Starlette(routes=[Route("/internal", internal, methods=["GET"])]))

    assert TestClient(app).get("/v2/internal").status_code == 200

    with pytest.raises(RuntimeError) as refused:
        assert_every_v2_route_is_gated(app)
    assert "GET /v2/internal" in str(refused.value), str(refused.value)


def test_a_mount_with_no_route_list_is_refused_rather_than_passed_over():
    """A mount whose app exposes no `routes` cannot be enumerated — which is a reason to decide
    about it, not a reason to skip it.

    The mount itself is what gets judged here, because a bare ASGI app is the one shape the walk
    cannot see inside. "It might be fine" is not the guard's default anywhere else, so it is not the
    default here: a `/v2` mount nobody can enumerate stops the process instead of serving.
    """
    from privatemesh.api_v2 import assert_every_v2_route_is_gated

    app = _app_with_v2()

    async def opaque(scope, receive, send):                  # a bare ASGI app: no route list
        return None

    app.mount("/v2", opaque)

    with pytest.raises(RuntimeError) as refused:
        assert_every_v2_route_is_gated(app)
    assert "? /v2" in str(refused.value), str(refused.value)


def test_a_listed_method_does_not_exempt_the_routes_other_methods():
    """The seventh review's second High, at the registration end.

    `PUBLIC_V2_ROUTES` names `POST /v2/playground/attack`. A `GET` on the same path reaches a
    different handler, and the allowlist is keyed by `(method, path)` so it says nothing about it.

    The route is registered with BOTH methods on purpose: that is the only shape that can tell the
    two rules apart. A route is exempt either because every method it serves is listed or because
    one of them is — and for a single-method route those are the same statement, so a one-method
    version of this test passes under both and measures nothing."""
    _h, service, api, _router = _live_router()
    router = build_v2_router(service)

    @router.api_route("/playground/attack", methods=["GET", "POST"])
    def attack_again(request: Request) -> dict:
        return {"owner_only": True}

    api.app.include_router(router)

    assert api.get("/v2/playground/attack").status_code == 403, \
        "a GET added to a publicly-listed POST path answered a caller carrying no token"


def test_a_route_serving_a_listed_and_an_unlisted_method_is_refused_at_startup():
    """The same rule for the shape that has no second route object to catch: ONE route serving two
    methods, one of them listed.

    `@router.api_route(path, methods=["GET", "POST"])` builds a single route whose `methods` is
    `{GET, POST}`. An exemption decided with `any` exempts the whole route and answers the unlisted
    `GET` anonymously; the guard decides about the route, and a route is exempt only when every
    method it serves is named.
    """
    from privatemesh.api_v2 import assert_every_v2_route_is_gated

    app = _app_with_v2()

    async def attack_again(request):
        return JSONResponse({"owner_only": True})

    app.router.add_api_route("/v2/playground/attack", attack_again, methods=["GET", "POST"],
                             route_class_override=APIRoute, include_in_schema=False)

    with pytest.raises(RuntimeError) as refused:
        assert_every_v2_route_is_gated(app)
    assert "/v2/playground/attack" in str(refused.value), str(refused.value)


def test_a_handler_that_only_copies_the_gate_mark_is_not_accepted_as_gated():
    """The seventh review's Low, driven rather than reasoned about.

    The proof that a route is gated used to be an attribute on the handler's `__dict__` — and
    `functools.update_wrapper` copies `__dict__`, which is exactly what a decorator sitting OUTSIDE
    the gate does. So a wrapper around a gated handler inherited the proof while the gate never ran
    on the request, and the startup check called it gated. `update_wrapper` is used here for that
    reason: this is not a contrived spoof, it is the ordinary way to write the decorator that would
    have done it by accident.

    The registry answers by IDENTITY, so the copy is a different function and the route is refused.
    """
    from privatemesh.api_v2 import assert_every_v2_route_is_gated

    app = _app_with_v2()
    gated = next(route for route in _walk(app.routes) if route.path == "/v2/plans")

    def spoof(request: Request) -> dict:
        return {"owner_only": True}

    functools.update_wrapper(spoof, gated.endpoint)
    # The copied `__wrapped__` and `__annotations__` are put back to this function's own, because
    # FastAPI reads the response type from them and would hold this route to the borrowed handler's
    # return annotation. The `__dict__` copy is the whole point of the spoof and it survives: that is
    # where the old proof of gating lived.
    del spoof.__wrapped__
    spoof.__annotations__ = {"request": Request, "return": dict}
    app.add_api_route("/v2/spoofed", spoof, methods=["GET"], include_in_schema=False)

    # Reachable, so the refusal below is about the guard and not about a route that 404s.
    assert TestClient(app).get("/v2/spoofed").status_code == 200

    with pytest.raises(RuntimeError) as refused:
        assert_every_v2_route_is_gated(app)
    assert "GET /v2/spoofed" in str(refused.value), str(refused.value)


def test_the_startup_check_passes_on_the_app_the_product_actually_serves():
    """The control for the two above: the assertion is not a function that refuses everything.

    A guard that failed on the real app would be a guard that made the product unstartable, and a
    test that only ever saw it raise would report that as success."""
    from privatemesh.api_v2 import assert_every_v2_route_is_gated

    assert_every_v2_route_is_gated(_app_with_v2()) is None


def test_a_gated_route_hidden_from_the_schema_is_still_enumerated_and_still_refused():
    """`include_in_schema=False` is how the browser gate's `/openapi.json` walk loses a route.

    The Python enumeration walks `app.routes`, which is the list the app serves from and the one a
    route cannot be absent from, so a hidden route is exercised exactly like a visible one."""
    from privatemesh.api_v2 import assert_every_v2_route_is_gated

    _h, _service, api, router = _live_router()

    # The hidden route is built on a router of its own and only THAT one is included. `_live_router`
    # has already mounted the router's 42 `/v2` routes on the app, so re-including `router` — which
    # is what this test used to do — registers every one of them a second time, and FastAPI reports
    # that as 42 "Duplicate Operation ID" warnings that say nothing about what is being tested here.
    # The route class comes off `router`, so the hidden route is built by the same gate as every
    # other `/v2` route and every assertion below is unchanged.
    hidden_router = APIRouter(prefix="/v2", route_class=router.route_class)

    @hidden_router.get("/hidden-but-private", include_in_schema=False)
    def hidden(request: Request) -> dict:
        return {"owner_only": True}

    api.app.include_router(hidden_router)
    assert_every_v2_route_is_gated(api.app)

    assert "/v2/hidden-but-private" not in api.get("/openapi.json").json()["paths"], \
        "this route is meant to be the one the schema walk cannot see"
    assert api.get("/v2/hidden-but-private").status_code == 403
    assert api.get("/v2/hidden-but-private",
                   headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_every_public_allowlist_entry_is_exercised_and_returns_nothing_private():
    """An exemption is a claim, and a claim needs a measurement.

    The enumeration above skips allowlisted routes, so an entry added for a PRIVATE route would be
    skipped by the very test that exists to catch it: the route would answer an anonymous caller
    with owner data and the suite would report "exempted, as declared". This drives each entry for
    real, with no token, and requires the answer to carry no owner state — and, because "it 404s"
    and "it serves a sample world" are different facts, requires the entry to state which one it is.
    """
    _h, _service, api, _router = _live_router()

    assert PUBLIC_V2_ROUTES, "an empty allowlist would make this test vacuous"

    for method, path in sorted(PUBLIC_V2_ROUTES):
        body = {"email": "From: a@b.c\n\nhi"} if path.endswith("/attack") else None
        response = _call(api, method, _url(path), body)
        assert response.status_code in (404, 405), (
            f"{method} {path} is allowlisted and answered {response.status_code} anonymously; an "
            "exemption must be a route that cannot serve the owner's data, not one that does")
        if response.status_code == 404:
            assert response.json().get("code") == "not_found", (
                f"{method} {path} refused without the mode's own code, so the refusal came from "
                "somewhere other than the mode that is supposed to make this safe")

        # The owner's own state, named by the fields `/session` exists to carry. None of it may
        # appear in an allowlisted route's answer to a caller with no token — the check is on the
        # BYTES, so it does not depend on reading the handler.
        text = response.text
        for secret in ("owner_name", "authenticated_owner", "timezone", "boundary", "connected"):
            assert secret not in text, f"{method} {path} leaked {secret!r} to an anonymous caller"


# ================================================ the gate is per REQUEST, not per boot
#
# The eighth review found the two holes below in the startup scan, and they are one hole seen twice:
# the guard was a fact about the route table at ONE moment, and the exemption was a fact about a
# SPELLING. Both are closed by asking the question again on every request, of the route that is
# actually about to answer, and by granting the exemption to a route OBJECT rather than to a
# `(method, path)`.
#
# The startup scan stays, because a developer who registers an ungated `/v2` route by mistake should
# hear about it before the port opens rather than from a stranger's 403. What changed is that it is
# no longer the only thing standing there.
ATTACK_BODY = {"email": "From: a@b.c\n\nhi"}


def _gated_client(**kw):
    """An app with the v2 router mounted AND the per-request gate installed, the way `build_app`
    mounts it.

    Both halves of that wiring are reproduced here because both are what the findings are about: the
    startup scan is the fail-fast a developer gets (a real `on_startup` callback, so the client's
    context manager genuinely runs it), and the middleware is the boundary.

    Imports are local, like every other guard import in this file, so that the file still collects
    against a tree where the per-request gate does not exist yet: a module-level import of a name
    the fix introduces would take the whole file down instead of failing the tests that are about
    it."""
    from privatemesh.api_v2 import assert_every_v2_route_is_gated, install_v2_request_gate

    h = _harness()
    service = h.v2(mode="live", owner_token=TOKEN, owner_name="Sam Owner",
                   timezone="Europe/London", **kw)
    router = build_v2_router(service)
    app = FastAPI()
    app.include_router(router)
    app.router.on_startup.append(lambda: assert_every_v2_route_is_gated(app))
    install_v2_request_gate(app)
    return h, service, TestClient(app), router


def test_a_route_registered_after_the_startup_scan_is_refused():
    """The eighth review's first High, driven the way it was measured.

    `app.add_api_route("/v2/after-startup", ...)` after the startup callback had run answered 200
    anonymously. Nothing about the scan was wrong; the scan was simply over. A route table is a list
    anyone holding the app can append to, so the question has to be asked of each request.

    The owner's own token is asserted against too, and that is not a flourish: this route is not
    gated, so there is no caller for whom it may serve, and a gate that refused only tokenless
    callers would be answering a different question than the one the route raises."""
    _h, _service, api, _router = _gated_client()

    with api:                                   # the startup scan runs here, over today's table
        def after_startup(request: Request) -> dict:
            return {"owner_only": True, "owner_name": "Sam Owner"}

        api.app.add_api_route("/v2/after-startup", after_startup, methods=["GET"])
        anonymous = api.get("/v2/after-startup")
        owner = api.get("/v2/after-startup", headers={"Authorization": f"Bearer {TOKEN}"})

    for response, caller in ((anonymous, "a caller carrying no token"),
                             (owner, "the owner's own token")):
        assert response.status_code == 403, (
            f"/v2/after-startup, registered after the startup scan, answered "
            f"{response.status_code} to {caller}")
        assert response.json()["code"] == "owner_forbidden"
        assert "owner_name" not in response.text


def test_the_refusal_reaches_a_browser_with_the_code_it_needs_to_read():
    """A refusal a browser is not allowed to read is the round-2 finding again, one layer down.

    `detail` on this 403 explains a wiring defect, and on the web client's origin the two are a
    matched pair: CORS has to be OUTSIDE the gate, or the response arrives with no
    `access-control-allow-origin` and the client sees an opaque network failure instead of the
    sentence written for it. `build_app` is the app that decides this, so `build_app` is what is
    asked — a hand-built app in this file would have whatever order this test gave it and could not
    tell anyone anything about the real one.

    The positive halves are asserted with it. Moving CORS outward is only safe if it still grants
    what it granted before: the owner's own read is readable, and a preflight is answered by CORS
    rather than by the gate resolving a route for a request no handler serves."""
    from privatemesh.api import default_app
    from privatemesh.config import Settings

    origin = "http://localhost:3000"
    h = _harness()
    service = h.v2(mode="live", owner_token=TOKEN, owner_name="Sam Owner", timezone="Europe/London")
    app = default_app(Settings(api_key="stub", base_url="https://x/v1/", model_fast="m",
                               model_reason="m", model_deep="m", tavily_key="stub",
                               web_origins=origin), demo=True, v2=service)
    api = TestClient(app)

    with api:
        def after_startup(request: Request) -> dict:
            return {"owner_only": True, "owner_name": "Sam Owner"}

        api.app.add_api_route("/v2/after-startup", after_startup, methods=["GET"])
        refused = api.get("/v2/after-startup", headers={"Origin": origin})

    assert refused.status_code == 403
    assert refused.json()["code"] == "owner_forbidden"
    assert refused.headers.get("access-control-allow-origin") == origin, (
        "the gate's refusal reached the browser with no CORS grant, so the client cannot read the "
        "code it branches on and the detail it was written to explain")

    owner = api.get("/v2/session", headers={"Origin": origin, "Authorization": f"Bearer {TOKEN}"})
    assert owner.status_code == 200
    assert owner.headers.get("access-control-allow-origin") == origin

    preflight = api.options("/v2/session", headers={"Origin": origin,
                                                    "Access-Control-Request-Method": "GET"})
    assert preflight.status_code == 200
    assert "GET" in (preflight.headers.get("access-control-allow-methods") or "")


def test_a_mount_registered_after_the_startup_scan_is_refused():
    """The seventh review's two mount shapes, asked the eighth review's question.

    A mount is the shape most likely to be added late — it is how one app is hung under another —
    and it is the one the startup scan is structurally worst at, because the scan is over by then.
    Both shapes are driven: a mount whose routes can be enumerated, and a bare ASGI app that exposes
    no route list at all. The second is the reason the gate judges the ENTRY rather than only its
    contents: "it might be fine" is not this guard's default anywhere else."""
    _h, _service, api, _router = _gated_client()

    with api:
        async def internal(request):
            return JSONResponse({"owner_only": True})

        api.app.mount("/v2/mounted",
                      Starlette(routes=[Route("/internal", internal, methods=["GET"])]))
        assert api.get("/v2/mounted/internal").status_code == 403

        async def opaque(scope, receive, send):              # a bare ASGI app: no route list
            return None

        api.app.mount("/v2/opaque", opaque)
        assert api.get("/v2/opaque/anything").status_code == 403


def _app_with_a_rogue_at_the_public_path(service) -> FastAPI:
    """An app where a raw handler — NOT the vetted route — is what reaches
    `POST /v2/playground/attack`.

    Registered first on purpose: the router matches in table order, so the first route at a path is
    the one that answers, which is how the review's handler came to return its payload."""
    app = FastAPI()

    def attack_rogue(request: Request) -> dict:
        return {"owner_only": True, "owner_name": "Sam Owner"}

    rogue = APIRouter()
    rogue.post("/v2/playground/attack")(attack_rogue)
    app.include_router(rogue)
    app.include_router(build_v2_router(service))
    return app


def test_a_route_that_is_only_SPELLED_like_the_exempt_one_is_refused_per_request():
    """The eighth review's second High. The exemption was keyed by `(method, path)`, so ANY route
    spelled that way inherited it — and the raw handler here, registered ahead of the official one,
    answered an owner payload to a caller carrying no token while passing startup validation.

    The premise is measured on the same app without the gate, so the 403 below is the gate's answer
    and not a 404 from a route that was never reachable in the first place."""
    _h, service, _api, _router = _live_router()

    leaked = TestClient(_app_with_a_rogue_at_the_public_path(service)).post(
        "/v2/playground/attack", json=ATTACK_BODY)
    assert leaked.status_code == 200, "the rogue route is not the one this path reaches"
    assert leaked.json()["owner_name"] == "Sam Owner", (
        "this test needs the rogue route to be the one that leaks, or it measures nothing")

    from privatemesh.api_v2 import install_v2_request_gate

    app = _app_with_a_rogue_at_the_public_path(service)
    install_v2_request_gate(app)
    refused = TestClient(app).post("/v2/playground/attack", json=ATTACK_BODY)

    assert refused.status_code == 403, (
        f"a route merely spelled like the exempt one answered {refused.status_code}")
    assert refused.json()["code"] == "owner_forbidden"
    assert "owner_name" not in refused.text


def test_a_route_that_is_only_SPELLED_like_the_exempt_one_is_refused_by_the_scan_too():
    """The same route, refused by the scan that runs before the port opens.

    The finding's own words were "a handler registered BEFORE the official route passed validation",
    so this is the validation, asked again: the exemption now belongs to the route the gated router
    built, and a route nobody vetted is not it however it is spelled."""
    from privatemesh.api_v2 import assert_every_v2_route_is_gated

    _h, service, _api, _router = _live_router()
    app = _app_with_a_rogue_at_the_public_path(service)

    with pytest.raises(RuntimeError) as refused:
        assert_every_v2_route_is_gated(app)
    assert "POST /v2/playground/attack" in str(refused.value), str(refused.value)


def test_the_vetted_public_route_is_exempt_by_BEING_itself_and_still_answers():
    """The control for the identity rule, and the reason it is `is` and not `==` or a set.

    `Route.__eq__` is value-based on `path`, `endpoint` and `methods`, and `Route.__hash__` is
    `None` — so a set could not even hold a route, and an equality test would accept a COPY of the
    vetted one. `copy.copy` is not a contrived attack: it is what a framework does when it
    re-parents a route, and the copy below is value-equal to the vetted route by construction."""
    import copy as copy_module

    from privatemesh.api_v2 import _is_gated, _is_public

    _h, _service, api, _router = _gated_client()
    vetted = next(route for route in _walk(api.app.routes)
                  if route.path == "/v2/playground/attack")

    assert _is_public(vetted) is True, "the router's own route is not in the exemption registry"
    assert _is_gated(vetted) is False, "the exempt route is gated as well, so this measures nothing"

    like_it = copy_module.copy(vetted)
    assert like_it == vetted and like_it is not vetted, (
        "the copy is not value-equal to the vetted route, so this test measures nothing")
    assert _is_public(like_it) is False, "a copy of the vetted route inherited its exemption"
    with pytest.raises(TypeError):
        {vetted}                        # unhashable: this is why the registry is not a set

    # And the exemption is not a refusal to serve: the vetted route still reaches its handler. In
    # live mode that handler answers `not_found`, which is the mode's answer rather than the gate's
    # — a 403 here would mean the middleware had refused the one route it must let through.
    with api:
        assert api.post("/v2/playground/attack", json=ATTACK_BODY).status_code == 404
        assert api.get("/v2/session").status_code == 403
        assert api.get("/v2/session", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_the_request_gate_leaves_the_sample_alone():
    """The demo is the zero-credential path, and a per-request gate is exactly the kind of thing
    that could take it away without any test in live mode noticing."""
    from privatemesh.api_v2 import install_v2_request_gate

    h = _harness()
    service = h.v2(mode="sample", owner_name="Sam Owner")
    app = FastAPI()
    app.include_router(build_v2_router(service))
    install_v2_request_gate(app)
    api = TestClient(app)

    assert api.get("/v2/session").status_code == 200
    assert api.post("/v2/playground/attack", json=ATTACK_BODY).status_code == 200


def test_the_resolver_names_the_route_that_actually_serves():
    """The gate's decision is only as good as its answer to "which route is about to answer".

    `_serving_route` MIRRORS the router's own loop rather than modelling it, and this is what says
    the mirror is faithful: every route the app serves is instrumented to record itself, every one
    is driven, and the route the gate resolved has to be the route that ran. The rogue route added
    straight to the app is in the sweep on purpose — "the route nobody vetted" is precisely the one
    the resolver has to name correctly, and a mirror that disagreed with the dispatch only there
    would be a mirror that failed exactly where it is load-bearing.

    The app here has the ROUTER but not the middleware, and that is deliberate rather than a
    shortcut: with the gate installed, a route it refuses never reaches its handler, so there would
    be nothing to compare the resolution against for exactly the rows that matter. Resolution does
    not depend on the middleware — the same `app.routes` is read either way — so the comparison is
    the same comparison, and every row is driven to a real dispatch."""
    from privatemesh.api_v2 import _serving_route, _under_v2, _walk_routes

    _h, _service, api, router = _live_router()

    with api:
        def after_startup(request: Request) -> dict:
            return {"owner_only": True}

        api.app.add_api_route("/v2/after-startup", after_startup, methods=["GET"])

        served: list[object] = []
        for served_path, route in _walk_routes(api.app.routes):
            if not _under_v2(served_path):
                continue
            inner = route.handle

            def handle(scope, receive, send, _route=route, _inner=inner):
                served.append(_route)
                return _inner(scope, receive, send)

            route.handle = handle

        checked = 0
        for method, path, route in _routes(router) + [("GET", "/v2/after-startup", None)]:
            url = _url(path)
            scope = {"type": "http", "method": method, "path": url, "root_path": "", "headers": []}
            _match, resolved = _serving_route(api.app.routes, scope)
            served.clear()
            api.request(method, url, json=_body(route) if route is not None else None,
                        headers={"Authorization": f"Bearer {TOKEN}"})
            assert served, f"nothing served {method} {url}, so this row measures nothing"
            assert resolved is served[0], (
                f"the gate resolved {getattr(resolved, 'path', resolved)!r} for {method} {path} and "
                f"{getattr(served[0], 'path', served[0])!r} served it")
            checked += 1

    assert checked >= 25, f"only {checked} routes were driven: {checked}"


# ---------------------------------------------------------------------------------------------
# The ninth review. Two Highs about what the gate was willing to LOOK AT, rather than about how it
# judges what it sees: one scope type it declined to have an opinion about, and one place in the
# middleware stack where a route could be swapped after the gate had already said yes.
# ---------------------------------------------------------------------------------------------


class _Rewriter:
    """The ninth review's exact middleware: a gated path becomes an ungated one.

    A plain ASGI middleware rather than a `BaseHTTPMiddleware`, because that is what the review
    installed and what the ordering question is actually about: `add_middleware` takes any ASGI
    callable, and the gate's order in the stack does not depend on which shape it is."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/v2/session":
            scope = dict(scope)
            scope["path"] = "/v2/inner-leak"
        await self.app(scope, receive, send)


def _ws_leak_app(**kw):
    """A LIVE app on the v2 router with NO gate, for the premise half of the WebSocket rows."""
    h = _harness()
    service = h.v2(mode="live", owner_token=TOKEN, owner_name="Sam Owner",
                   timezone="Europe/London", **kw)
    app = FastAPI()
    app.include_router(build_v2_router(service))
    return h, service, app


async def _runtime_ws(websocket: WebSocket) -> None:
    """The route the ninth review registered after startup, in its own words: an owner payload."""
    await websocket.accept()
    await websocket.send_json({"owner_only": True, "owner_name": "Sam Owner"})
    await websocket.close()


def test_a_websocket_route_added_before_the_startup_scan_is_refused_at_startup():
    """The scan half of the scope finding, and the reason the fix is not "WebSockets are banned".

    A WebSocket route under `/v2` is a route like any other to this guard, and the walk has yielded
    it since the seventh review — a `WebSocketRoute` is simply an entry with no route list. What the
    ninth review measured was the OTHER half: once startup is over, only the middleware is left, and
    the middleware had a `scope["type"] != "http"` branch that meant "not my business"."""
    from privatemesh.api_v2 import assert_every_v2_route_is_gated

    _h, _service, app = _ws_leak_app()
    app.add_api_websocket_route("/v2/runtime-ws", _runtime_ws)

    with pytest.raises(RuntimeError) as refused:
        assert_every_v2_route_is_gated(app)
    assert "/v2/runtime-ws" in str(refused.value), str(refused.value)


def test_a_websocket_route_added_after_the_startup_scan_is_closed_and_not_served():
    """The ninth review's second High, driven the way it was measured.

    The premise is measured on the same route without the gate, so the close below is the gate's
    answer and not a route that never worked: a WebSocket handler that is never reached and one
    that is refused look identical from the client's end, and only the first proves anything.

    Closed rather than passed through is the whole of the ruling. There is no response to put a
    body in, so the refusal is a close frame — and every non-HTTP path under `/v2` gets one,
    including a path no route matches, because "we do not serve this" is an answer the gate can
    give and "somebody else will deal with it" is the branch that leaked."""
    _h, _service, app = _ws_leak_app()
    app.add_api_websocket_route("/v2/runtime-ws", _runtime_ws)
    with TestClient(app).websocket_connect("/v2/runtime-ws") as leaked:
        assert leaked.receive_json()["owner_name"] == "Sam Owner", (
            "this row needs the route to leak without the gate, or it measures nothing")

    from privatemesh.api_v2 import install_v2_request_gate

    _h2, _service2, gated = _ws_leak_app()
    gated.add_api_websocket_route("/v2/runtime-ws", _runtime_ws)
    install_v2_request_gate(gated)

    with TestClient(gated) as api:
        gated.add_api_websocket_route("/v2/runtime-ws-added-later", _runtime_ws)
        for path in ("/v2/runtime-ws", "/v2/runtime-ws-added-later", "/v2/no-such-route"):
            with pytest.raises(WebSocketDisconnect) as closed:
                with api.websocket_connect(path) as session:
                    pytest.fail(f"{path} was served anonymously: {session.receive_json()}")
            assert closed.value.code == 1008, (
                f"{path} was closed with {closed.value.code}, which is not the refusal")


def test_a_websocket_route_the_gate_knows_is_still_served():
    """The control, and the reason the row above is a gate rather than a ban on the scope type.

    The refusal is about what the route can PROVE, so a route that can prove it is served whatever
    its scope. A gate that closed every WebSocket under `/v2` would pass the row above while making
    a whole protocol unservable, and the difference is invisible from the outside — which is exactly
    the kind of guard this review keeps finding."""
    from privatemesh.api_v2 import _V2_GATED_ENDPOINTS, install_v2_request_gate

    async def gated_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"gated": True})
        await websocket.close()

    # The same registry the gate asks `_is_gated` of. A real gated WebSocket feature would earn its
    # place in it the way an HTTP route does; this stands in for that without inventing the feature.
    _V2_GATED_ENDPOINTS.add(gated_ws)

    _h, _service, app = _ws_leak_app()
    app.add_api_websocket_route("/v2/gated-ws", gated_ws)
    install_v2_request_gate(app)

    with TestClient(app).websocket_connect("/v2/gated-ws") as served:
        assert served.receive_json() == {"gated": True}
    _V2_GATED_ENDPOINTS.discard(gated_ws)


def test_the_installer_refuses_a_stack_that_would_leave_middleware_inside_the_gate():
    """The ninth review's third High, at the moment it is created.

    `add_middleware` inserts at the FRONT of `user_middleware` and the front is OUTERMOST, so a
    middleware added BEFORE the gate ends up INSIDE it — between the gate's resolution and the
    dispatch that resolution was about. The reviewer's rewriter went on first, the gate went on
    second, and `/v2/session` — which the gate validated, correctly — was then rewritten onto
    `/v2/inner-leak`, a route nobody vetted, and answered 200 with the owner's name.

    Refused rather than reordered. Silently moving the gate would make the rewriter's author's
    intent unguessable, and the ordering is a decision someone made; the fix is to say so at the
    moment it is made, while the line that made it is still on screen."""
    from privatemesh.api_v2 import install_v2_request_gate

    _h, _service, app = _ws_leak_app()
    app.add_middleware(_Rewriter)                    # FIRST — so the gate lands on top of it

    with pytest.raises(RuntimeError) as refused:
        install_v2_request_gate(app)
    assert "_Rewriter" in str(refused.value), (
        "the refusal does not name the middleware that would be inside the gate, so it is a "
        f"refusal nobody can act on: {refused.value}")


def test_a_rewriting_middleware_OUTSIDE_the_gate_cannot_get_a_gated_path_served_ungated():
    """The other half of the ordering rule, and the reason only one side of it is refused.

    A middleware outside the gate rewrites the scope BEFORE the gate reads it, so the gate asks
    about the path that will actually be dispatched and the two still agree. The same rewriter that
    leaked from inside is inert from outside, which is what makes "innermost" the right invariant
    rather than "no middleware anywhere near it"."""
    from privatemesh.api_v2 import install_v2_request_gate

    def rogue(request: Request) -> dict:
        return {"owner_only": True, "owner_name": "Sam Owner"}

    _h, _service, bare = _ws_leak_app()
    bare.add_api_route("/v2/inner-leak", rogue, methods=["GET"])
    assert TestClient(bare).get("/v2/inner-leak").json()["owner_name"] == "Sam Owner", (
        "this row needs the rogue route to leak when nothing gates it, or it measures nothing")

    _h2, _service2, app = _ws_leak_app()
    app.include_router(build_v2_router(_service2))   # no-op on an app that already has it
    install_v2_request_gate(app)
    app.add_middleware(_Rewriter)                    # AFTER the install — lands OUTSIDE
    app.add_api_route("/v2/inner-leak", rogue, methods=["GET"])

    refused = TestClient(app).get("/v2/session")
    assert refused.status_code == 403, (
        "a middleware outside the gate got a gated path served from an ungated one")
    assert "owner_name" not in refused.text


def test_a_stack_with_something_inside_the_gate_is_refused_by_the_ordering_check():
    """The invariant re-checked on an app that was assembled some other way.

    `install_v2_request_gate` refuses to CREATE the bad ordering; this refuses to SERVE one. The
    two are not the same check: a stack can be rearranged, or built by a caller who never went
    through the installer at all, and the assembled app is what actually dispatches."""
    from privatemesh.api_v2 import assert_v2_gate_is_innermost, install_v2_request_gate

    _h, _service, app = _ws_leak_app()
    install_v2_request_gate(app)
    assert_v2_gate_is_innermost(app)                 # the app as installed is fine

    app.user_middleware.insert(1, Middleware(_Rewriter))    # something gets INSIDE the gate
    with pytest.raises(RuntimeError) as refused:
        assert_v2_gate_is_innermost(app)
    assert "_Rewriter" in str(refused.value), str(refused.value)


def test_the_ordering_the_product_serves_is_both_checked_and_what_the_gate_needs():
    """The two ends of the ordering rule, asserted on the app the product actually serves.

    CORS must be OUTSIDE the gate — that is the round-2 finding, and a refusal a browser cannot
    read is the same defect one layer down — and the gate must be INNERMOST, which is this round's.
    A hand-built app in this file would have whatever order the test gave it, so `build_app` is what
    is asked here, and it is asked through the same check `build_app` itself now calls."""
    from privatemesh.api import default_app
    from privatemesh.api_v2 import _V2RequestGate, assert_v2_gate_is_innermost
    from privatemesh.config import Settings

    h = _harness()
    service = h.v2(mode="live", owner_token=TOKEN, owner_name="Sam Owner", timezone="Europe/London")
    app = default_app(Settings(api_key="stub", base_url="https://x/v1/", model_fast="m",
                               model_reason="m", model_deep="m", tavily_key="stub",
                               web_origins="http://localhost:3000"), demo=True, v2=service)

    assert_v2_gate_is_innermost(app)                 # does not raise: the product's order is right
    stack = app.user_middleware
    assert getattr(stack[-1], "cls", None) is _V2RequestGate, (
        "the product's gate is not the innermost middleware: "
        f"{[getattr(e, 'cls', e).__name__ for e in stack]}")
    outside = [getattr(entry, "cls", None).__name__ for entry in stack[:-1]]
    assert "CORSMiddleware" in outside, (
        f"CORS is not outside the gate, so its grant is not on the gate's refusal: {outside}")
