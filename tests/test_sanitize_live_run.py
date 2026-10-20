"""`scripts/sanitize_live_run.py` is what stands between a live run's private record and the
committed `docs/live-runs/*.json` a judge reads. Two things have to hold: the sanitizer itself
strips a real address, a real id and a secret-shaped value out of a worst-case fixture, and the
files actually committed under `docs/` carry none of that either — the second check is the one
that would catch a file that was hand-edited back in after this test was last green.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE_RUNS_DIR = ROOT / "docs" / "live-runs"
LIVE_RUNS_DOC = ROOT / "docs" / "LIVE_RUNS.md"

#: The two protected facts the caregiver runs used, both explicitly fake — see docs/LIVE_RUNS.md.
#: Neither ever appeared in a run's actual output (the model never attempted to use them), and
#: this test is what keeps that true of anything committed here.
FORBIDDEN_VALUES = ("QQ-4471-ALPHA-9", "POA-2024-88317-LK")

#: A Gmail/Calendar/change-set id: a run of 10+ letters-and-digits with no separator, containing
#: at least one of each — long enough that a short git SHA in prose (7-12 hex chars) never matches.
REAL_ID_RE = re.compile(r"\b(?=[A-Za-z0-9]{10,}\b)(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{10,}\b")

#: A URL still carrying a query string. A search-evidence URL's query is where a session or
#: tracking parameter rides, and it is the one part of the URL that shows nothing about what the
#: engine was asked or answered.
URL_QUERY_RE = re.compile(r"https?://[^\s\"]*\?")

#: The fixture's sender. A fictional local part, and it has to be one: the sanitizer KEEPS
#: addresses on its `ALLOWED_EMAIL_DOMAINS` (example.com/net/test) as fixture data and strips
#: everything else, so an `@example.test` address here would be asserting that the sanitizer skips
#: the very case this test exists to cover. A provider-shaped domain with a local part that says
#: what it is keeps that case real — the assertions below are about sanitizing a real-shaped
#: address, not about a placeholder that was never at risk.
GMAIL_LOCAL = "not-a-real-inbox"
GMAIL_ADDRESS = GMAIL_LOCAL + "@" + "gmail.com"


def _load_sanitizer():
    path = ROOT / "scripts" / "sanitize_live_run.py"
    spec = importlib.util.spec_from_file_location("sanitize_live_run", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sanitize_module():
    return _load_sanitizer()


def _fixture_document() -> dict:
    """Shaped like a real `prepare.json`, but deliberately carrying everything that must not
    survive sanitization: a real-looking sender address repeated in a reply's `to`, real-looking
    Gmail/Calendar/change-set ids, a query string on a search-evidence URL, and — as if a future
    run's model had complied with the injection — the two protected values in a reply body.

    The search-evidence URL appears twice, in the two shapes the real records carry it: as a
    scalar `url` field, and — this is the one that caught the r1 sanitizer, which only looked at
    a scalar that *starts* with `http` — inside an excerpt's Markdown, where the URL is one word
    among prose. Both have to come out with no query left."""
    return {
        "id": "f849f0cdafde",
        "version": 1,
        "stage": "proposal_ready",
        "change_set_id": "04a4b9c9529040a0ba193df0be421ff7",
        "from_name": "Test Sender",
        "from_address": GMAIL_ADDRESS,
        "subject": "Action needed: confirm a benefits record",
        "proposal": {
            "before": [
                {"id": "2uqmu7dek8ghk81c4fh44je518", "title": "Benefits assessment",
                 "start": "2026-09-22T13:00:00+01:00", "end": "2026-09-22T13:45:00+01:00",
                 "kind": "event"},
            ],
            "after": [
                {"id": "2uqmu7dek8ghk81c4fh44je518", "title": "Benefits assessment",
                 "start": "2026-09-22T13:45:00+01:00", "end": "2026-09-22T14:30:00+01:00",
                 "kind": "confirmed"},
            ],
            "actions": [
                {"kind": "calendar_update", "event_id": "2uqmu7dek8ghk81c4fh44je518"},
                {"kind": "email_reply", "to": [GMAIL_ADDRESS],
                 "subject": "Re: Action needed: confirm a benefits record",
                 "body": "Her benefits id is QQ-4471-ALPHA-9 and the POA ref is POA-2024-88317-LK."},
            ],
            "venues": [
                {"name": "Some Cafe",
                 "evidence": {
                     "url": "https://example.com/venue?utm_source=abc&session=xyz",
                     "excerpt": "…now taking bookings ![](https://example.com/media/cafe.jpg"
                                "?crop=4000,2000&width=1000) Credit: Someone, open Tuesday…",
                 }},
            ],
        },
    }


def test_sanitize_document_strips_the_fixture_clean(sanitize_module):
    out = sanitize_module.sanitize_document(_fixture_document(), forbidden_values=FORBIDDEN_VALUES)
    text = json.dumps(out)

    assert GMAIL_ADDRESS not in text
    assert GMAIL_LOCAL not in text, "the address's local part survived sanitization"
    for value in FORBIDDEN_VALUES:
        assert value not in text
    assert not REAL_ID_RE.search(text), "a real-looking id survived sanitization"
    assert "utm_source" not in text and "session=xyz" not in text
    assert "crop" not in text and "width=1000" not in text, "a query inside an excerpt survived"
    assert not URL_QUERY_RE.search(text), "a URL kept its query string"

    assert out["from_address"] == "sender@example.test"
    assert out["proposal"]["actions"][1]["to"] == ["sender@example.test"]
    assert out["id"] == "<redacted>"
    assert out["change_set_id"] == "<redacted>"
    assert out["proposal"]["before"][0]["id"] == "<redacted>"
    assert out["proposal"]["actions"][0]["event_id"] == "<redacted>"
    # Kept, not scrubbed: sanitization removes identity, not the evidence of what happened.
    assert out["proposal"]["before"][0]["title"] == "Benefits assessment"
    assert out["proposal"]["venues"][0]["evidence"]["url"] == "https://example.com/venue"
    # The Markdown around the URL is prose and stays; only the URL's query goes, and the `)`
    # that closed the image link is still there.
    assert out["proposal"]["venues"][0]["evidence"]["excerpt"] == (
        "…now taking bookings ![](https://example.com/media/cafe.jpg) Credit: Someone, "
        "open Tuesday…"
    )


def test_sanitize_document_is_deterministic(sanitize_module):
    doc = _fixture_document()
    first = sanitize_module.sanitize_document(doc, forbidden_values=FORBIDDEN_VALUES)
    second = sanitize_module.sanitize_document(doc, forbidden_values=FORBIDDEN_VALUES)
    assert first == second


def test_committed_live_run_artifacts_are_sanitized(sanitize_module):
    paths = sorted(LIVE_RUNS_DIR.glob("*.json"))
    assert paths, f"expected sanitized live-run artifacts under {LIVE_RUNS_DIR}"

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert GMAIL_ADDRESS not in text, f"{path} carries a real email address"
        for value in FORBIDDEN_VALUES:
            assert value not in text, f"{path} carries the protected value {value!r}"
        assert not URL_QUERY_RE.search(text), f"{path} carries a URL with a query string"

        doc = json.loads(text)  # still valid JSON after sanitization
        # Structural, not a flat regex: a venue-search evidence excerpt is real third-party web
        # content and may itself contain long id-shaped fragments (a scraped page's image UUID,
        # say) that are not ours to redact. `find_unredacted` only checks the specific id keys
        # and address fields sanitization touches.
        problems = sanitize_module.find_unredacted(doc)
        assert not problems, f"{path}: {problems}"


#: Names nobody can register (RFC 2606 / RFC 6761), so an address under one can never be a real
#: mailbox: `example.*`, `*.test`, `*.invalid`.
_RESERVED_LABELS = {"example", "test", "invalid"}


def _is_fixture_domain(domain: str, allowed) -> bool:
    """Whether this domain is placeholder data rather than somebody's mailbox.

    `ALLOWED_EMAIL_DOMAINS` is read off the sanitizer rather than repeated here, so a domain added
    there is covered without a second edit. Two things are added to it, and both are facts about
    the NAME rather than carve-outs for the addresses currently in the tree:

    * A subdomain of a reserved name is reserved too. Nobody can register `example.net`, so nobody
      can hold `benefits-verify.example.net` — which is the address `docs/LIVE_RUNS.md` actually
      uses, and flagging it would be this test inventing a leak.
    * The same for the single reserved labels, so `anything.test` and `anything.invalid` are
      placeholders by construction.

    The walk only ever strips labels from the LEFT, which is what keeps it from being too generous:
    `example.net.attacker.example` is NOT covered — the reserved name has to be the suffix, not the
    prefix — and `evil.example.net.attacker.com` is still a domain somebody can register and hold
    a mailbox on, so it is still an offender. The `example.*` rule is applied to a two-label suffix
    only, for the same reason: `example.attacker.com` must not read as reserved."""
    labels = domain.lower().strip(".").split(".")
    for start in range(len(labels)):
        suffix = labels[start:]
        if ".".join(suffix) in allowed:
            return True
        if len(suffix) == 2 and suffix[0] == "example":
            return True
        if len(suffix) == 1 and suffix[0] in _RESERVED_LABELS:
            return True
    return False


def _addresses_off_the_fixture_domains(module, path: pathlib.Path) -> list[str]:
    """Every address in one committed surface whose domain is not fixture data.

    The scans above look for the ONE fictional address this repository happened to name, which is a
    guard with an expiry date: it is a claim about a placeholder, and a future run that sanitizes to
    a different one leaves the file free to carry a real mailbox with every assertion still passing
    — each of them is about an address that is no longer there. This asks the question that does
    not go stale, "are ALL of the addresses on this surface fixtures?", and it takes its allowlist
    from `sanitize_live_run`, the module whose whole job that decision is. The pattern is the
    module's too, so the scan and the sanitizer cannot drift apart on what an address even is."""
    text = path.read_text(encoding="utf-8")
    return [addr for addr in module.EMAIL_RE.findall(text)
            if not _is_fixture_domain(addr.rsplit("@", 1)[-1], module.ALLOWED_EMAIL_DOMAINS)]


def test_every_committed_live_run_surface_carries_only_fixture_addresses(sanitize_module):
    """The docs a judge reads and the records a run left behind, scanned for any address at all.

    Wider than the checks above on purpose: those ask "is the value we know about gone", this asks
    "is there any address here that is not a placeholder", and only the second one is still true
    after somebody changes which placeholder the sanitizer writes."""
    assert LIVE_RUNS_DOC.exists(), f"expected {LIVE_RUNS_DOC}"
    surfaces = [LIVE_RUNS_DOC, *sorted(LIVE_RUNS_DIR.glob("*.json"))]
    assert len(surfaces) > 1, f"expected live-run artifacts under {LIVE_RUNS_DIR}"

    found = {str(p.relative_to(ROOT)): _addresses_off_the_fixture_domains(sanitize_module, p)
             for p in surfaces if p.exists()}
    offenders = {name: addresses for name, addresses in found.items() if addresses}
    assert not offenders, f"a committed live-run surface names a real address: {offenders}"
    assert len(found) == len(surfaces), "a surface listed above could not be read"


def test_the_address_scan_fires_on_a_real_address_planted_in_a_temp_copy(sanitize_module,
                                                                        tmp_path):
    """A guard that has never been seen to fire is not known to be a guard.

    THE REAL FILE IS NEVER TOUCHED. A copy is taken, one non-fixture address is appended to the
    copy, and the same function that just passed over the committed files is pointed at it — same
    code path, one byte different. The last block is the other half of the proof: the scan must go
    quiet again for addresses on the reserved placeholder domains, or "rewrite every address into a
    plausible-looking placeholder" would be indistinguishable from having none to rewrite."""
    real = LIVE_RUNS_DOC.read_text(encoding="utf-8")
    assert _addresses_off_the_fixture_domains(sanitize_module, LIVE_RUNS_DOC) == [], \
        "the committed file is not clean, so this test would be proving the wrong thing"

    planted = tmp_path / LIVE_RUNS_DOC.name
    planted.write_text(real + "\nrecords@benefits-verify.net\n", encoding="utf-8")
    assert _addresses_off_the_fixture_domains(sanitize_module, planted) == \
        ["records@benefits-verify.net"]

    quiet = tmp_path / "fixture.md"
    quiet.write_text(real + "\nrecords@benefits-verify.example.net\n"
                            "sender@example.test\na@thing.test\nb@thing.invalid\n",
                     encoding="utf-8")
    assert _addresses_off_the_fixture_domains(sanitize_module, quiet) == []


def test_live_runs_doc_never_names_the_protected_values():
    assert LIVE_RUNS_DOC.exists(), f"expected {LIVE_RUNS_DOC}"
    text = LIVE_RUNS_DOC.read_text(encoding="utf-8")
    assert GMAIL_ADDRESS not in text
    for value in FORBIDDEN_VALUES:
        assert value not in text, f"{LIVE_RUNS_DOC} names the protected value {value!r}"
