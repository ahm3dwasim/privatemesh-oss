"""`deploy/demo/demo.env.example` — the checked-in template an operator copies to `demo.env`.

Two things this pins, both about the FILE rather than the running process (the process's own
refusals are pinned in `test_demo_mode.py`): the caps it documents stay inside the deploy lane's
limits (whole-demo total <= $3.00, daily <= $0.50), and it never grows a credential-shaped value —
this file is meant to be copied and eventually committed-adjacent, so a value that belongs in a
docker secret must never end up here by habit.
"""
from __future__ import annotations

import re
from pathlib import Path

from privatemesh import redact
from privatemesh.demo_session import (
    CEILING_DAILY_USD,
    CEILING_MAX_SESSIONS,
    CEILING_RATE_REQUESTS,
    CEILING_SESSION_CALLS,
    CEILING_SESSION_TTL,
    CEILING_SESSION_USD,
    CEILING_TOTAL_USD,
    DEFAULT_DAILY_USD,
    DEFAULT_MAX_SESSIONS,
    DEFAULT_RATE_REQUESTS,
    DEFAULT_RATE_MAX_CALLERS,
    DEFAULT_RATE_WINDOW,
    DEFAULT_SESSION_CALLS,
    DEFAULT_SESSION_TTL,
    DEFAULT_SESSION_USD,
    DEFAULT_TOTAL_USD,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / "deploy" / "demo" / "demo.env.example"

#: Where the RUNNING system reads its settings from: every module under `src/privatemesh`, plus the
#: demo compose file (which both passes settings through to `api` and interpolates a couple of its
#: own). Scanned, never listed by hand — the drift this check exists to catch is one setting added
#: to the code and forgotten in the template, and a hand-kept list is updated by the same person who
#: did the forgetting.
SRC_DIR = REPO_ROOT / "src" / "privatemesh"
COMPOSE_PATH = REPO_ROOT / "deploy" / "demo" / "compose.yaml"

DEMO_SETTING_RE = re.compile(r"PM_DEMO_[A-Z0-9_]+")

#: A setting is DOCUMENTED when it owns a line of its own, either active (`KEY=value`) or commented
#: out (`#KEY=value` — this file's convention for an optional setting, cf. `#PM_WEB_ORIGINS=`). A
#: name that only appears inside a sentence is not documentation: an operator copying this file can
#: neither read a value off it nor grep for it. Anchored to the start of the line so
#: `PM_DEMO_SESSION_USD` is not "documented" by a line setting `PM_DEMO_SESSION_USD_SOMETHING`.
DOCUMENTED_LINE_RE = re.compile(r"^[ \t]*#?[ \t]*([A-Z][A-Z0-9_]*)=", re.M)


def _referenced_demo_settings() -> set[str]:
    """Every `PM_DEMO_*` name `src/privatemesh` or `deploy/demo/compose.yaml` can read."""
    found: set[str] = set()
    for path in sorted(SRC_DIR.rglob("*.py")):
        found |= set(DEMO_SETTING_RE.findall(path.read_text()))
    found |= set(DEMO_SETTING_RE.findall(COMPOSE_PATH.read_text()))
    return found


def _documented_settings() -> set[str]:
    """Every key `demo.env.example` documents on a line of its own, commented ones included."""
    return set(DOCUMENTED_LINE_RE.findall(ENV_PATH.read_text()))


#: The lane's own limits (task brief, not the code's ceilings): the whole demo may not exceed these
#: regardless of what the code would otherwise allow.
LANE_TOTAL_USD_MAX = 3.00
LANE_DAILY_USD_MAX = 0.50

#: Credential env names that must never appear in this file at all — the model key arrives at
#: runtime through a docker secret (see compose.yaml), and Tavily/Google credentials must never
#: exist on the demo host in the first place (`demo_session.refuse_if_providers_configured`).
FORBIDDEN_KEYS = ("NEBIUS_API_KEY", "TAVILY_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
                  "PM_GOOGLE_CLIENT", "PM_GOOGLE_TOKEN_DIR", "NEBIUS_API_KEY_FILE")


def _parse_env(path: Path) -> dict[str, str]:
    """`KEY=value` pairs from an env-style file, skipping comments, blanks and commented-out keys.

    A line commented out with a leading `#` documents a value; it is never read into the process,
    so it is not a value this file "has" for the purposes of the assertions below."""
    pairs: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        pairs[key.strip()] = value.strip()
    return pairs


def test_the_example_file_exists_and_is_nonempty():
    assert ENV_PATH.is_file(), f"missing {ENV_PATH}"
    assert ENV_PATH.read_text().strip(), f"{ENV_PATH} is empty"


def test_no_forbidden_credential_key_is_set_in_the_file():
    """Not just blank — ABSENT. A `TAVILY_API_KEY=` line left blank is one edit away from a real
    key; the safest version of this file does not carry the name at all."""
    env = _parse_env(ENV_PATH)
    present = sorted(set(env) & set(FORBIDDEN_KEYS))
    assert not present, f"{ENV_PATH} sets forbidden credential key(s): {present}"


def test_no_value_in_the_file_is_credential_shaped():
    """A generic scan over every value with the app's own secret-shape detector
    (`redact.SECRET_VALUE_RE`), independent of the key name — catches a pasted key under an
    unexpected key just as well as one of the forbidden names above."""
    env = _parse_env(ENV_PATH)
    for key, value in env.items():
        assert not redact.SECRET_VALUE_RE.search(value), (
            f"{ENV_PATH} value for {key!r} looks credential-shaped: {value!r}")


def test_the_whole_demo_caps_are_within_the_deploy_lanes_limit():
    env = _parse_env(ENV_PATH)
    total = float(env["PM_DEMO_TOTAL_USD"])
    daily = float(env["PM_DEMO_DAILY_USD"])
    assert 0 < total <= LANE_TOTAL_USD_MAX
    assert 0 < daily <= LANE_DAILY_USD_MAX
    # Documented as the SAME safe default the code falls back to, not a second number that could
    # drift from it.
    assert total == DEFAULT_TOTAL_USD
    assert daily == DEFAULT_DAILY_USD


def test_every_cap_is_set_documents_the_codes_own_safe_default():
    """This file is documentation of `demo_session`'s own fallbacks, not a second policy — every
    numeric default here must equal the constant it documents, and every one must sit under its
    absolute ceiling (a value past the ceiling is treated as unreadable by `_bounded`, so a file
    that set one above it would be documenting a number the code would silently ignore)."""
    env = _parse_env(ENV_PATH)
    checks = (
        ("PM_DEMO_SESSION_USD", DEFAULT_SESSION_USD, CEILING_SESSION_USD),
        ("PM_DEMO_SESSION_CALLS", DEFAULT_SESSION_CALLS, CEILING_SESSION_CALLS),
        ("PM_DEMO_RATE_REQUESTS", DEFAULT_RATE_REQUESTS, CEILING_RATE_REQUESTS),
        ("PM_DEMO_RATE_WINDOW", DEFAULT_RATE_WINDOW, CEILING_SESSION_TTL),
        ("PM_DEMO_RATE_MAX_CALLERS", DEFAULT_RATE_MAX_CALLERS, CEILING_MAX_SESSIONS),
        ("PM_DEMO_TOTAL_USD", DEFAULT_TOTAL_USD, CEILING_TOTAL_USD),
        ("PM_DEMO_DAILY_USD", DEFAULT_DAILY_USD, CEILING_DAILY_USD),
        ("PM_DEMO_MAX_SESSIONS", DEFAULT_MAX_SESSIONS, CEILING_MAX_SESSIONS),
        ("PM_DEMO_SESSION_TTL", DEFAULT_SESSION_TTL, CEILING_SESSION_TTL),
    )
    for key, default, ceiling in checks:
        assert key in env, f"{ENV_PATH} is missing {key}"
        value = float(env[key])
        assert value == float(default), f"{key}={value} in the file, but the code default is {default}"
        assert 0 < value <= ceiling, f"{key}={value} is outside (0, {ceiling}]"


def test_the_ledger_path_points_under_the_named_volumes_mount():
    """compose.yaml mounts the `demo_ledger` volume at `/home/privatemesh/.privatemesh` — a ledger
    path outside that directory would not survive a container restart, silently resetting the
    whole-demo total/daily caps to zero."""
    env = _parse_env(ENV_PATH)
    ledger = env["PM_DEMO_LEDGER"]
    assert ledger.startswith("/home/privatemesh/.privatemesh/"), ledger


def test_the_scan_that_derives_the_setting_list_actually_finds_settings():
    """A scan that silently returns nothing would make the completeness check below pass while
    enforcing nothing — the failure mode a derived set is most prone to, and the reason this is a
    test rather than an inline assumption.

    The three names below are a FLOOR, not the list: `PM_DEMO_WORKERS` is read by
    `demo_session.refuse_if_multi_worker`, `PM_DEMO_DOMAIN` is interpolated into the `caddy`
    service by compose.yaml, and the ledger path is the one setting a demo cannot run without."""
    found = _referenced_demo_settings()
    for name in ("PM_DEMO_WORKERS", "PM_DEMO_DOMAIN", "PM_DEMO_LEDGER"):
        assert name in found, (
            f"the source scan did not find {name} — it is reading the wrong tree, so the "
            f"completeness check below would pass vacuously")


def test_every_setting_the_code_or_compose_reads_is_documented_here():
    """The completeness half of the acceptance criterion, and the reason it is derived rather than
    listed: a `PM_DEMO_*` name the code or compose reads but this file never documents is a setting
    an operator cannot discover from the file they are told to copy — which is how
    `PM_DEMO_WORKERS` (a startup refusal) and `PM_DEMO_DOMAIN` (Caddy's hostname) were missed."""
    missing = sorted(_referenced_demo_settings() - _documented_settings())
    assert not missing, (
        f"{ENV_PATH} does not document these PM_DEMO_* settings that src/privatemesh or "
        f"deploy/demo/compose.yaml read: {missing}")
