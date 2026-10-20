"""Table-driven egress check. The fix-lane test planted one secret shape the scrubber already knew,
so it could not fail; this table plants the shapes people actually paste.

Each fixture is assembled from split fragments below, never written as one contiguous literal,
so a secret scanner reading this file finds no matching token — only egress_sanitize ever sees
the joined string, exactly as it would see a real paste at runtime.

`test_this_file_holds_no_scanner_shaped_literal` is what keeps that true: it reads this file's own
source and fails if a later edit re-joins any of the fragments. Parametrised cases carry explicit
`ids`, so pytest writes only a label — never a joined fixture — into its cache node ids.
"""
import re
from pathlib import Path

import pytest

from privatemesh.redact import egress_sanitize


def _join(*parts: str) -> str:
    return "".join(parts)


LEAKS = [
    _join("sk-proj-", "A1b2C3d4E5f6", "G7h8I9j0K1l2M3n4O5p6"),      # OpenAI project key (hyphenated)
    _join("sk-svcacct-", "Zz9Yy8Xx7Ww6", "Vv5Uu4Tt3Ss2Rr1"),
    _join("sk-ant-api03-", "abcdefghijklm", "nopqrstuvwxyz012345"),
    _join("tvly-", "abcdefghijk", "lmnop1234567890"),               # Tavily — our own product's key
    _join("nvapi-", "abcdefghijklmn", "op1234567890"),               # NVIDIA API key
    _join("ghp_", "abcdefghijklmnop", "qrstuvwxyz0123456789"),
    _join("github_pat_", "11ABCDEFG0123456789", "_abcdefghijklmnop"),
    _join("hf_", "abcdefghijklm", "nopqrstuvwxyz012345"),
    _join("sk_live_", "abcdefghij", "klmnop1234"),
    _join("AKIA", "IOSFODNN", "7EXAMPLE"),
    _join("eyJhbGciOiJIUzI1", "NiJ9.", "eyJzdWIiOiIxMjM0NTY3ODkwIn0.", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
]
LEAK_IDS = [
    "hyphenated-project-key",
    "hyphenated-service-account-key",
    "hyphenated-vendor-key",
    "search-api-key",
    "accelerator-vendor-key",
    "github-classic-pat",
    "github-fine-grained-pat",
    "model-hub-token",
    "payments-live-key",
    "cloud-access-key-id",
    "jwt",
]
ASSIGNMENTS = [
    (_join("NEBIUS_API_KEY=", "abc123def456ghi789"), "abc123def456ghi789"),
    (_join("password: ", "hunter2hunter2"), "hunter2hunter2"),
    (_join('db_password = "', "correct-horse-battery", '"'), "correct-horse-battery"),
    (_join("export GITHUB_TOKEN='", "x9y8z7w6v5u4", "'"), "x9y8z7w6v5u4"),
]
ASSIGNMENT_IDS = [
    "api-key-assignment",
    "password-colon",
    "db-password-assignment",
    "exported-token",
]


@pytest.mark.parametrize("secret", LEAKS, ids=LEAK_IDS)
def test_secret_shaped_literals_never_leave(secret):
    out, n = egress_sanitize([{"role": "user", "content": f"here: {secret} thanks"}])
    assert n >= 1
    assert secret not in out[0]["content"]
    # no recognisable tail fragment survives either (a partial JWT/key is still a leak)
    assert secret[-12:] not in out[0]["content"]


@pytest.mark.parametrize("text,value", ASSIGNMENTS, ids=ASSIGNMENT_IDS)
def test_credential_assignments_lose_their_value(text, value):
    out, n = egress_sanitize([{"role": "user", "content": f"my config: {text}"}])
    assert n >= 1 and value not in out[0]["content"]


@pytest.mark.parametrize("text", [
    "The token budget is 5000 per turn",
    "Reset my password tomorrow morning",
    "Send Alex the secret recipe for the launch party",
    "skip the meeting",
])
def test_ordinary_sentences_are_not_mangled(text):
    out, n = egress_sanitize([{"role": "user", "content": text}])
    assert n == 0 and out[0]["content"] == text


# Every shape the four pre-fix scanner hits matched — a JWT, two credential assignments and a
# payments live key (commit 63832b55) — plus the remaining prefixes this table plants. Each entry
# is (regex source, the runtime fixtures it must match), and each source is split for the same
# reason the fixtures are: written whole, these regexes would match this file's own text.
_SHAPES = {
    "jwt": (
        _join("ey", r"J[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        (LEAKS[10],),
    ),
    "credential-assignment": (
        _join(r"(?i)[A-Za-z0-9_]*(?:key|token|secret|password)",
              r"[A-Za-z0-9_]*\s*[=:]\s*['\"]?[A-Za-z0-9_+/-]{8,}"),
        tuple(text for text, _ in ASSIGNMENTS),
    ),
    "payments-live-key": (
        _join(r"\b(?:sk|rk)_(?:test|live)_", r"[0-9a-z]{10,99}\b"),
        (LEAKS[8],),
    ),
    "hyphenated-key": (
        _join("s", r"k-[A-Za-z0-9_-]{20,}"),
        (LEAKS[0], LEAKS[1], LEAKS[2]),
    ),
    "github-classic-pat": (
        _join("gh", r"p_[A-Za-z0-9]{36}"),
        (LEAKS[5],),
    ),
    "github-fine-grained-pat": (
        _join("github_", r"pat_[A-Za-z0-9_]{20,}"),
        (LEAKS[6],),
    ),
    "cloud-access-key-id": (
        _join("AK", r"IA[0-9A-Z]{16}"),
        (LEAKS[9],),
    ),
    "chat-platform-token": (
        _join("xo", r"x[bpoa]-[A-Za-z0-9-]{10,}"),
        (_join("xo", "x", "b", "-", "1234567890", "12-abcdefghijklmn"),),
    ),
    "model-hub-token": (
        _join("h", r"f_[A-Za-z0-9]{20,}"),
        (LEAKS[7],),
    ),
    "search-api-key": (
        _join("tv", r"ly-[A-Za-z0-9]{10,}"),
        (LEAKS[3],),
    ),
    "accelerator-vendor-key": (
        _join("nv", r"api-[A-Za-z0-9]{10,}"),
        (LEAKS[4],),
    ),
}

_SOURCE = Path(__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("label", list(_SHAPES))
def test_this_file_holds_no_scanner_shaped_literal(label):
    """A scanner reads bytes, not intentions: the fragmenting above has to survive later edits.

    Each pattern is first checked against the fixture it guards — a pattern that matches nothing
    would pass this test no matter what the file contained. Only then is the file's own source
    searched, which is the property a scanner actually sees.
    """
    source, samples = _SHAPES[label]
    pattern = re.compile(source)
    for sample in samples:
        assert pattern.search(sample), f"{label}: pattern matches no fixture, so it proves nothing"
    found = pattern.search(_SOURCE)
    assert found is None, (
        f"{label}: this file carries a contiguous literal a scanner would flag "
        f"(offset {found.start()}) — rebuild it from split fragments"
    )
