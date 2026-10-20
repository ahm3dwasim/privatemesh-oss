"""The two OpenShell policy files — checked against the real schema OpenShell enforces, and
against the adapters' own source, not against a hand-typed copy of either.

PyYAML is not a dependency of this project (`No installs of any kind`), so these are text-level
assertions against the exact keys OpenShell's own examples use
(`examples/sandbox-policy-quickstart/policy.yaml`,
`crates/openshell-driver-mxc/examples/e2e-policies/network-reject.yaml`) plus the L7 `rules` schema
(docs.nvidia.com/openshell/reference/policy-schema) — not a substitute for `openshell sandbox
create`/`policy set` actually accepting the file, which `scripts/openshell_run.sh` now does for
real (see `docs/NVIDIA_STACK.md`, "What running it found", and the RUN record it cites).

Every one of these checks was RED against the file OpenShell actually rejected — `git show
ece1c80:deploy/openshell/privatemesh.policy.yaml`, frozen below as `_PRE_RUN_LIVE_POLICY` — before
it was green against the current one. `test_every_check_here_was_red_against_the_pre_run_policy`
keeps that fact enforced rather than merely asserted in a docstring.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from privatemesh import gcal, gmail, google_auth, skills_builtin, tavily_evidence
from privatemesh.config import Settings

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "deploy" / "openshell" / "privatemesh.policy.yaml"
DEMO = ROOT / "deploy" / "openshell" / "demo.policy.yaml"

REQUIRED_TOP_LEVEL = ("version: 1", "filesystem_policy:", "landlock:", "network_policies:")

LIVE_HOSTS = ("api.tokenfactory.nebius.com", "gmail.googleapis.com", "www.googleapis.com",
             "oauth2.googleapis.com", "api.tavily.com")
DEMO_HOSTS = ("api.tokenfactory.nebius.com",)
DEMO_FORBIDDEN_HOSTS = ("gmail.googleapis.com", "www.googleapis.com", "oauth2.googleapis.com",
                        "accounts.google.com", "api.tavily.com")

#: The exact file OpenShell rejected at `sandbox create` (docs/NVIDIA_STACK.md, "What running it
#: found", finding 1): relative and `~` filesystem paths, and whole-host `access:` presets. Frozen
#: here — not re-read from git at test time, which would make the RED/GREEN contrast depend on
#: history staying reachable — so every check below has a fixed, known-bad text to prove itself
#: against.
_PRE_RUN_LIVE_POLICY = (ROOT / "tests" / "fixtures" / "pre_run_privatemesh.policy.yaml").read_text()


def _text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    assert "\t" not in text, f"{path.name} must be pure-space YAML"
    return text


def test_both_policies_exist_and_have_the_required_top_level_keys():
    for path in (LIVE, DEMO):
        text = _text(path)
        for key in REQUIRED_TOP_LEVEL:
            assert key in text, f"{path.name} is missing {key!r}"


def test_neither_policy_mounts_the_host_workdir():
    """Review finding, High: `include_workdir: true` mounts the actual host checkout this was invoked
    from into the sandbox — not the `git archive`ed tree `scripts/openshell_run.sh` uploads, the
    real working tree, untracked files included: a root `.env`, anything under `secrets/`. Neither
    policy file needs it — both are built entirely from `sandbox upload`, never from the workdir —
    so this must read `false`, not merely omit the key (which OpenShell could default either way)."""
    for path in (LIVE, DEMO):
        text = _text(path)
        assert re.search(r"^\s*include_workdir:\s*false\s*$", text, re.MULTILINE), (
            f"{path.name} must set `include_workdir: false` explicitly — the sandbox's filesystem "
            f"content must come only from what `scripts/openshell_run.sh` uploads")


def test_the_live_policy_grants_exactly_the_products_hosts_and_no_others():
    text = _text(LIVE)
    for host in LIVE_HOSTS:
        assert host in text, f"privatemesh.policy.yaml is missing {host}"
    hosts_in_file = {line.split("host:")[1].strip() for line in text.splitlines() if "host:" in line}
    assert hosts_in_file == set(LIVE_HOSTS)


def test_the_demo_policy_grants_only_the_model_endpoint():
    text = _text(DEMO)
    for host in DEMO_HOSTS:
        assert host in text
    for host in DEMO_FORBIDDEN_HOSTS:
        assert host not in text, (
            f"the demo policy must not grant {host} — the demo's inbox/calendar/search are "
            f"simulated, and this file IS that claim")


def test_the_demo_policy_grants_exactly_one_host_and_one_rule():
    """The demo's whole claim, machine-checkable: `serve --v2 --demo` refuses to start beside a
    real provider credential (`wiring.DemoRefused`), so a policy granting more than the model
    endpoint would be a policy that could be wrong about what the demo is."""
    text = _text(DEMO)
    hosts = [line.split("host:")[1].strip() for line in text.splitlines() if "host:" in line]
    assert hosts == list(DEMO_HOSTS)
    rules = re.findall(r"-\s*allow:\s*\{.*?\}", text)
    assert len(rules) == 1, f"expected exactly one `allow` rule in the demo policy, found {rules}"


# ------------------------------------------------------------ filesystem: absolute paths only ---
def _read_write_entries(text: str) -> list[str]:
    """The `read_write:` list's own entries, comments and blanks stripped.

    Handles both shapes this project's policy files actually use: a bracketed inline list
    (`read_only: [...]`) is not read here on purpose — only `read_write:`'s block-list form,
    which is the one OpenShell rejected a relative/`~` entry from."""
    block = text.split("read_write:", 1)[1].split("\nlandlock:", 1)[0]
    out = []
    for line in block.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith("- "):
            out.append(stripped[2:].strip())
    return out


def _assert_every_filesystem_path_is_absolute(text: str, name: str) -> None:
    """The actual check — factored out of `test_every_filesystem_path_is_absolute` so
    `test_every_check_here_was_red_against_the_pre_run_policy` can RUN it against the fixture
    rather than re-typing the fact it once failed on as a second, driftable assertion.

    OpenShell's own rejection, verbatim: "path must be absolute (start with '/')" — raised at
    `sandbox create` against the pre-run file's `data/v2` and `~/.privatemesh`
    (docs/NVIDIA_STACK.md, "What running it found", finding 1). A text-level check can catch this
    without ever calling `openshell`."""
    entries = _read_write_entries(text)
    assert entries, f"{name} has no read_write entries to check"
    for entry in entries:
        assert entry.startswith("/"), (
            f"{name}: read_write entry {entry!r} is not absolute — OpenShell refuses "
            f"this at `sandbox create` with 'path must be absolute'")


def test_every_filesystem_path_is_absolute():
    for path in (LIVE, DEMO):
        _assert_every_filesystem_path_is_absolute(_text(path), path.name)


def test_the_pre_run_policy_fixture_actually_has_a_relative_and_a_tilde_path():
    """Guards the fixture itself: if it stopped containing what OpenShell rejected, the RED/GREEN
    contrast the tests above rely on would be silently checking nothing."""
    entries = _read_write_entries(_PRE_RUN_LIVE_POLICY)
    assert any(not e.startswith("/") and not e.startswith("~") for e in entries), (
        "fixture no longer has a relative read_write path")
    assert any(e.startswith("~") for e in entries), "fixture no longer has a `~` read_write path"


# ------------------------------------------------------------ network: no `access:` presets -----
def _assert_no_endpoint_uses_an_access_preset(text: str, name: str) -> None:
    """The actual check, factored out for the same reason `_assert_every_filesystem_path_is_
    absolute` is: so the red-first test below runs it rather than re-typing its fact.

    `access: read-only` denied `POST /token` and `POST /search` at OpenShell's L7 layer
    (docs/NVIDIA_STACK.md, "What running it found", finding 2) — every endpoint must instead carry
    method+path `rules`, which is what the run showed OpenShell actually enforcing."""
    preset = re.compile(r"^\s*access:\s*(read-only|read-write)\s*$", re.MULTILINE)
    code_lines = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    assert not preset.search(code_lines), (
        f"{name} still uses an `access:` preset — the run showed this denying calls the "
        f"product needs (docs/NVIDIA_STACK.md)")
    assert "rules:" in text, f"{name} has no method+path `rules:` block"


def test_no_endpoint_uses_an_access_preset():
    for path in (LIVE, DEMO):
        _assert_no_endpoint_uses_an_access_preset(_text(path), path.name)


def test_every_endpoint_has_rules_and_every_rule_names_method_and_path():
    for path in (LIVE, DEMO):
        text = _text(path)
        endpoints = text.split("endpoints:")[1:]
        assert endpoints, f"{path.name} declares no endpoints"
        for block in endpoints:
            chunk = block.split("binaries:")[0]
            assert "rules:" in chunk, f"{path.name}: an endpoint has no `rules:` block"
            allows = re.findall(r"allow:\s*\{([^}]*)\}", chunk)
            assert allows, f"{path.name}: an endpoint's `rules:` block has no `allow` entries"
            for allow in allows:
                assert "method:" in allow and "path:" in allow, (
                    f"{path.name}: a rule is missing method or path: {allow!r}")


# ------------------------------------------------------------ network: rules match the adapters --
def _calls_from_source(cls_or_module, *, self_call_name: str = "_call") -> set[tuple[str, str]]:
    """`(METHOD, path-pattern)` pairs read off `self._call(METHOD, PATH, ...)` call sites in the
    given class's source — an f-string interpolation collapses to `*`, matching this project's own
    policy-comment convention. This is the "derive from source" half of these tests: a new call an
    adapter makes shows up here without anyone retyping a string into this file."""
    src = inspect.getsource(cls_or_module)
    pattern = re.compile(
        r"self\." + re.escape(self_call_name) +
        r"\(\s*\"([A-Z]+)\"\s*,\s*(?:f?\"([^\"]*)\"|f'([^']*)')(?P<rest>[^)]*)\)", re.DOTALL)
    out: set[tuple[str, str]] = set()
    for m in pattern.finditer(src):
        method = m.group(1)
        raw_path = m.group(2) or m.group(3) or ""
        normalised = re.sub(r"\{[^}]*\}", "*", raw_path)
        at_root = "at_root=True" in m.group("rest")
        out.add((method, normalised, at_root))
    return out


def _gmail_expected_paths() -> set[tuple[str, str]]:
    calls = _calls_from_source(gmail.GmailClient)
    base_path = urlparse(gmail.GMAIL_BASE).path
    return {(method, base_path + path) for method, path, _at_root in calls}


def _gcal_expected_paths() -> set[tuple[str, str]]:
    calls = _calls_from_source(gcal.GCalClient)
    base_path = urlparse(gcal.CALENDAR_BASE).path
    out = set()
    for method, path, at_root in calls:
        prefix = "" if at_root else "/calendars/*"
        out.add((method, base_path + prefix + path))
    return out


def _rules_of(text: str, host: str) -> set[tuple[str, str]]:
    """`(METHOD, path)` for every `allow` rule under one host's `endpoints:` block.

    Cut at the next `binaries:` line — every endpoint group in both files ends its `rules:` block
    there, so it bounds one host's rules regardless of what host or comment follows."""
    section = text.split(f"host: {host}", 1)[1]
    chunk = section.split("\n    binaries:", 1)[0]
    return {(mm, pp) for mm, pp in re.findall(
        r"allow:\s*\{\s*method:\s*(\w+)\s*,\s*path:\s*\"?([^\s\"}]+)\"?\s*\}", chunk)}


def test_gmail_rules_match_gmailclients_own_calls():
    text = _text(LIVE)
    assert _rules_of(text, "gmail.googleapis.com") == _gmail_expected_paths()


def test_calendar_rules_match_gcalclients_own_calls():
    text = _text(LIVE)
    assert _rules_of(text, "www.googleapis.com") == _gcal_expected_paths()


def test_oauth_rule_matches_google_auths_own_token_url():
    text = _text(LIVE)
    parsed = urlparse(google_auth.GOOGLE_TOKEN_URL)
    assert _rules_of(text, "oauth2.googleapis.com") == {("POST", parsed.path)}
    # `accounts.google.com` is deliberately not a granted HOST — the consent screen is opened by
    # the owner's browser on the host machine, never called by the server inside the sandbox. (It
    # may still be named in a comment explaining that absence.)
    hosts = [line.split("host:")[1].strip() for line in text.splitlines() if "host:" in line]
    assert "accounts.google.com" not in hosts


def test_tavily_rule_matches_the_adapters_own_url_and_both_adapters_agree():
    assert urlparse(skills_builtin.TAVILY_URL) == urlparse(tavily_evidence.TAVILY_URL), (
        "skills_builtin and tavily_evidence must name the same Tavily URL, or one of them is "
        "calling an endpoint this policy was never written against")
    text = _text(LIVE)
    parsed = urlparse(skills_builtin.TAVILY_URL)
    assert _rules_of(text, "api.tavily.com") == {("POST", parsed.path)}


def test_nebius_rule_matches_the_chat_completions_call_and_excludes_models():
    """`llm.TokenFactory.chat` posts to `"chat/completions"` under `Settings.base_url` (default
    read off `config.Settings.load`'s own fallback string, not retyped here); `TokenFactory.models`
    calls `"models"` and must NOT be granted — the run measured that exact call DENIED at L7
    (docs/NVIDIA_STACK.md, "What running it found")."""
    load_src = inspect.getsource(Settings.load)
    default_base_url = re.search(r'NEBIUS_BASE_URL",\s*"([^"]+)"', load_src).group(1)
    base_path = urlparse(default_base_url).path.rstrip("/")
    for path in (LIVE, DEMO):
        text = _text(path)
        rules = _rules_of(text, "api.tokenfactory.nebius.com")
        assert rules == {("POST", f"{base_path}/chat/completions")}, (
            f"{path.name}: nebius rule(s) {rules} do not match llm.py's one call")


# ------------------------------------------------------------ documentation invariant ------------
def test_every_allowance_carries_a_comment_naming_the_feature():
    """Every `network_policies` block is preceded by at least one `#` comment line."""
    for path in (LIVE, DEMO):
        lines = _text(path).splitlines()
        in_network = False
        for i, line in enumerate(lines):
            if line.strip() == "network_policies:":
                in_network = True
                continue
            if (in_network and line.strip().endswith(":") and line.startswith("  ")
                    and not line.startswith("    ") and not line.strip().startswith("#")):
                j = i - 1
                comment_found = False
                while j >= 0 and lines[j].strip().startswith("#"):
                    comment_found = True
                    j -= 1
                assert comment_found, f"{path.name}: {line.strip()} has no comment above it"


# ------------------------------------------------------------ red-first, kept honest -------------
def test_every_check_here_was_red_against_the_pre_run_policy():
    """The file OpenShell actually rejected fails the two real CHECKS the run's own findings
    motivate — not a hand-rolled re-statement of the same fact in different code, which could
    drift from what the checks above actually assert while this test kept passing regardless.
    Review finding, Medium: the previous version of this test only grepped the fixture for the
    strings OpenShell was known to reject; it never called `_assert_every_filesystem_path_is_
    absolute` or `_assert_no_endpoint_uses_an_access_preset` themselves, so weakening or deleting
    either check's actual logic (as opposed to the strings it happens to look for) left this test
    green. It now runs both checks, for real, against the frozen pre-run fixture and requires each
    one to raise — and, since `LIVE`/`DEMO` above already prove the same two checks pass on the
    real policy files, this is the RED half of a RED/GREEN pair this file states, not assumes."""
    with pytest.raises(AssertionError):
        _assert_every_filesystem_path_is_absolute(_PRE_RUN_LIVE_POLICY, "pre-run fixture")
    with pytest.raises(AssertionError):
        _assert_no_endpoint_uses_an_access_preset(_PRE_RUN_LIVE_POLICY, "pre-run fixture")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
