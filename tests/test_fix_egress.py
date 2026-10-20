"""HIGH 3 — nothing leaves the machine without passing the sanitizer first.

The review's finding: `agent.py` sent the raw user request three times over (intent, plan, answer)
and `llm.py` forwarded messages untouched, so a user pasting a key or a JWT into a request shipped
it off-box — even though the memory write path and the built-in tools already scrubbed.

These tests assert against the OUTBOUND HTTP BODY, not against a helper, because the claim is
about what the provider receives. `EgressBlocked` is imported inside the tests that need it so
this module can still be collected and run against pre-fix code, where it does not exist — that
is what makes the red check meaningful rather than a collection error.
"""
from __future__ import annotations

import json

import httpx
import pytest

from privatemesh.config import Settings
from privatemesh.llm import Tier, TokenFactory
from privatemesh.redact import REDACTED, egress_sanitize

S = Settings(api_key="k", base_url="https://tf.test/v1/", model_fast="nano",
             model_reason="super", model_deep="ultra", tavily_key="")

PLANTED_KEY = "sk-ant-api03-" + "Q" * 40
PLANTED_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"


class Recorder:
    """A Token Factory whose transport keeps every request body it was asked to send."""

    def __init__(self):
        self.bodies: list[dict] = []
        self.tf = TokenFactory(S, httpx.Client(base_url=S.base_url,
                                               transport=httpx.MockTransport(self._handler)))

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                                   "total_tokens": 2}})

    def sent(self) -> str:
        return json.dumps(self.bodies)


def test_a_secret_shaped_literal_in_user_text_never_reaches_the_request_body():
    """The planted-secret test the review asked for, against the wire."""
    rec = Recorder()
    rec.tf.chat([{"role": "user", "content": f"here is my key: {PLANTED_KEY}"}], tier=Tier.FAST)

    assert PLANTED_KEY not in rec.sent(), "a planted secret-shaped literal left the machine"
    assert REDACTED in rec.sent()
    assert "here is my key:" in rec.sent()          # the rest of the message is intact


def test_a_real_environment_secret_value_is_scrubbed(monkeypatch):
    """Not just pattern-shaped strings: the actual value of a secret-named env var."""
    monkeypatch.setenv("PM_DEMO_API_KEY", "a-real-looking-secret-value-1234")
    rec = Recorder()
    rec.tf.chat([{"role": "user", "content": "my key is a-real-looking-secret-value-1234"}],
                tier=Tier.FAST)
    assert "a-real-looking-secret-value-1234" not in rec.sent()
    assert REDACTED in rec.sent()


def test_every_message_role_is_sanitized_not_just_the_user_turn():
    """Tool results and assistant transcripts carry third-party content into the next prompt, so
    they get the same treatment as the owner's own words."""
    rec = Recorder()
    rec.tf.chat([{"role": "system", "content": f"context: {PLANTED_JWT}"},
                 {"role": "assistant", "content": f"I saw {PLANTED_KEY}"},
                 {"role": "tool", "content": f'{{"token": "{PLANTED_JWT}"}}'},
                 {"role": "user", "content": "carry on"}], tier=Tier.FAST)
    assert PLANTED_KEY not in rec.sent() and PLANTED_JWT not in rec.sent()


def test_a_multimodal_content_array_is_sanitized_part_by_part():
    rec = Recorder()
    rec.tf.chat([{"role": "user", "content": [{"type": "text", "text": f"key {PLANTED_KEY}"}]}],
                tier=Tier.FAST)
    assert PLANTED_KEY not in rec.sent() and REDACTED in rec.sent()


def test_an_uninspectable_payload_is_refused_and_nothing_is_sent():
    """Fail-closed is the whole point. A shape the sanitizer cannot read is a shape it cannot
    certify, and an uncertified prompt must not be posted — so this raises rather than forwards."""
    from privatemesh.redact import EgressBlocked

    rec = Recorder()
    with pytest.raises(EgressBlocked):
        rec.tf.chat([{"role": "user", "content": 12345}], tier=Tier.FAST)
    assert rec.bodies == [], "an uncertified payload was sent anyway"


def test_a_non_message_payload_is_refused():
    from privatemesh.redact import EgressBlocked

    rec = Recorder()
    with pytest.raises(EgressBlocked):
        rec.tf.chat("just a string, not a message list", tier=Tier.FAST)
    assert rec.bodies == []


def test_the_sanitizer_is_idempotent_so_the_late_pass_cannot_corrupt_the_prompt():
    """There are two passes — one where the agent takes the prompt's digest, one immediately
    before the request. The second must be a no-op on clean input, or the digest the audit
    recorded would not describe the bytes that were sent."""
    once, first = egress_sanitize([{"role": "user", "content": f"key {PLANTED_KEY} and text"}])
    twice, second = egress_sanitize(once)
    assert second == 0 and twice == once and first == 1


def test_the_sanitizer_reports_a_count_and_never_the_value():
    """The audit event carries a count. The removed value is exactly the thing that must not be
    written down, so it must not appear in the return value either."""
    clean, count = egress_sanitize([{"role": "user", "content": f"a {PLANTED_KEY} b {PLANTED_JWT}"}])
    assert count == 2
    assert PLANTED_KEY not in json.dumps(clean) and PLANTED_JWT not in json.dumps(clean)


def test_a_partial_environment_failure_blocks_rather_than_forwards(monkeypatch):
    """If the scrubber itself breaks, the honest answer is "not sent", not "sent unscrubbed"."""
    from privatemesh import redact
    from privatemesh.redact import EgressBlocked

    def explode(*a, **kw):
        raise RuntimeError("the environment could not be read")

    monkeypatch.setattr(redact, "_secret_candidates", explode)
    with pytest.raises(EgressBlocked):
        egress_sanitize([{"role": "user", "content": "anything"}])
