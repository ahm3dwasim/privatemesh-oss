#!/usr/bin/env python3
"""What the OpenShell sandbox lets out, per host and per HTTP method, run FROM INSIDE the sandbox.

No credential is sent anywhere — every body is `{}` and every header is the bare minimum a
well-formed request needs. Three things can happen to a probe, and they are different facts about
different layers:

* **REACHED** — the provider answered (even a 4xx: an anonymous caller was refused, but the
  connection went through OpenShell's tunnel and past its L7 rule).
* **DENIED-by-L7** — OpenShell's own proxy answered instead of the provider, body carrying
  `"policy_denied"` (`{"error":"policy_denied","layer":"l7",...}`). The host is allowed; this
  particular method+path is not.
* **BLOCKED-at-tunnel** — the connection itself never completed. The host is not on any allowed
  endpoint list at all.

This is a probe, not a report: `--policy {product,demo}` selects the EXPECTED class for every row
from the table below (read off `deploy/openshell/privatemesh.policy.yaml` /
`demo.policy.yaml` and `docs/NVIDIA_STACK.md`, "What running it found"), and a row that does not
match its expectation is a surprise the run exits non-zero on. A probe that only prints is a probe
nobody will notice going wrong.
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request

REACHED = "REACHED"
DENIED_L7 = "DENIED-by-L7"
BLOCKED = "BLOCKED-at-tunnel"

#: (method, url, reason it is in this corpus). Every URL here is either a real call one of the
#: adapters makes (`gmail.py`, `gcal.py`, `google_auth.py`, `skills_builtin.py`/`tavily_evidence.py`,
#: `llm.py`), a neighboring method/path on the same host the policy must NOT grant, or a host the
#: policy grants nothing on at all.
PROBES: list[tuple[str, str, str]] = [
    ("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile",
     "not a call any adapter makes — a read-only host preset would have allowed it anyway"),
    ("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
     "gmail.GmailClient.send_reply's one write — the assistant's job depends on this reaching"),
    ("GET", "https://www.googleapis.com/calendar/v3/users/me/calendarList",
     "not a call gcal.GCalClient makes (it lists/reads/patches one calendar's events, never the list of calendars)"),
    ("GET", "https://api.tokenfactory.nebius.com/v1/models",
     "not a call llm.TokenFactory.chat makes — only chat/completions is granted"),
    ("POST", "https://api.tokenfactory.nebius.com/v1/chat/completions",
     "llm.TokenFactory.chat's one call"),
    ("DELETE", "https://gmail.googleapis.com/gmail/v1/users/me/messages/abc",
     "no adapter ever deletes a message"),
    ("GET", "https://gmail.googleapis.com/gmail/v1/users/me/settings/forwardingAddresses",
     "not a call any adapter makes — exactly the kind of read a hostile injection would want"),
    ("POST", "https://oauth2.googleapis.com/token",
     "google_auth.GOOGLE_TOKEN_URL — the refresh call every live run needs"),
    ("POST", "https://api.tavily.com/search",
     "skills_builtin.TAVILY_URL / tavily_evidence.TAVILY_URL — the venue-research call"),
    ("GET", "https://example.com/", "an arbitrary host — granted by no policy"),
    ("GET", "https://pypi.org/simple/",
     "the package registry — deliberately never granted; dependencies ship as pre-downloaded wheels"),
    ("POST", "https://attacker.example.net/collect",
     "the injection corpus's own exfiltration target (redteam/cases)"),
]

#: The expected class of every probe, per policy. A probe not listed for a policy is a bug in this
#: file, not a thing to skip — `main` asserts every PROBES entry has an expectation under the
#: policy it was asked to check.
EXPECTED: dict[str, dict[tuple[str, str], str]] = {
    "product": {
        ("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile"): DENIED_L7,
        ("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"): REACHED,
        ("GET", "https://www.googleapis.com/calendar/v3/users/me/calendarList"): DENIED_L7,
        ("GET", "https://api.tokenfactory.nebius.com/v1/models"): DENIED_L7,
        ("POST", "https://api.tokenfactory.nebius.com/v1/chat/completions"): REACHED,
        ("DELETE", "https://gmail.googleapis.com/gmail/v1/users/me/messages/abc"): DENIED_L7,
        ("GET", "https://gmail.googleapis.com/gmail/v1/users/me/settings/forwardingAddresses"): DENIED_L7,
        ("POST", "https://oauth2.googleapis.com/token"): REACHED,
        ("POST", "https://api.tavily.com/search"): REACHED,
        ("GET", "https://example.com/"): BLOCKED,
        ("GET", "https://pypi.org/simple/"): BLOCKED,
        ("POST", "https://attacker.example.net/collect"): BLOCKED,
    },
    "demo": {
        # The demo policy grants exactly one host and one rule (`POST /v1/chat/completions` on
        # Nebius) — every other row, including a neighboring method on that SAME host, is blocked
        # or denied.
        ("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile"): BLOCKED,
        ("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"): BLOCKED,
        ("GET", "https://www.googleapis.com/calendar/v3/users/me/calendarList"): BLOCKED,
        ("GET", "https://api.tokenfactory.nebius.com/v1/models"): DENIED_L7,
        ("POST", "https://api.tokenfactory.nebius.com/v1/chat/completions"): REACHED,
        ("DELETE", "https://gmail.googleapis.com/gmail/v1/users/me/messages/abc"): BLOCKED,
        ("GET", "https://gmail.googleapis.com/gmail/v1/users/me/settings/forwardingAddresses"): BLOCKED,
        ("POST", "https://oauth2.googleapis.com/token"): BLOCKED,
        ("POST", "https://api.tavily.com/search"): BLOCKED,
        ("GET", "https://example.com/"): BLOCKED,
        ("GET", "https://pypi.org/simple/"): BLOCKED,
        ("POST", "https://attacker.example.net/collect"): BLOCKED,
    },
}

#: Per-binary enforcement is NOT tested by this script. It has to be a SEPARATE `sandbox exec`
#: whose own top-level command is the unlisted binary — measured today (docs/NVIDIA_STACK.md,
#: "What running it found") to be checked against the exec session's own command, not against
#: each descendant process's binary: `curl` run as a *subprocess of this probe* (an allowed
#: `/usr/bin/python3` session) reaches the network the parent was granted, while the identical
#: `curl` invocation run as `sandbox exec`'s own command is blocked at the tunnel (exit 56, CONNECT
#: tunnel failed, 403). `scripts/openshell_run.sh` runs that check itself, as its own exec call.


def _classify(method: str, url: str) -> str:
    req = urllib.request.Request(url, data=(b"{}" if method == "POST" else None), method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return f"{REACHED} {r.status}"
    except urllib.error.HTTPError as e:
        body = e.read()[:400].decode("utf-8", "replace")
        return f"{DENIED_L7} ({e.code})" if '"policy_denied"' in body else f"{REACHED} {e.code}"
    except Exception as e:                                          # noqa: BLE001
        return f"{BLOCKED} ({type(e).__name__}: {str(e)[:60]})"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, choices=sorted(EXPECTED))
    args = parser.parse_args(argv)
    expected = EXPECTED[args.policy]
    missing = [p[:2] for p in PROBES if p[:2] not in expected]
    if missing:
        print(f"egress_probe: no expectation recorded for {missing} under {args.policy!r} — "
              f"fix EXPECTED before trusting this run", file=sys.stderr)
        return 2

    surprises = 0
    print(f"{'METHOD':<7} {'URL':<66} {'RESULT':<28} EXPECTED", flush=True)
    for method, url, _reason in PROBES:
        result = _classify(method, url)
        want = expected[(method, url)]
        ok = result.split()[0] == want or (want == REACHED and result.startswith(REACHED))
        marker = "" if ok else "  <-- SURPRISE"
        print(f"{method:<7} {url:<66} {result:<28} {want}{marker}", flush=True)
        if not ok:
            surprises += 1

    print(f"\n{len(PROBES)} probes, {surprises} surprise(s) "
          f"(per-binary enforcement is checked separately — see scripts/openshell_run.sh)",
          flush=True)
    return 1 if surprises else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
