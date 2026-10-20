import json

import httpx
import pytest

from privatemesh.config import Settings
from privatemesh.llm import TIER_DEFAULTS, EmptyContent, Tier, TokenFactory

S = Settings(api_key="k", base_url="https://tf.test/v1/", model_fast="nano",
             model_reason="super", model_deep="", tavily_key="")
FULL = Settings(api_key="k", base_url="https://tf.test/v1/", model_fast="nano",
                model_reason="super", model_deep="ultra", tavily_key="")


def _tf(handler):
    client = httpx.Client(base_url=S.base_url, transport=httpx.MockTransport(handler))
    return TokenFactory(S, client)


def _reply(handler, settings=FULL):
    """A factory that also hands back every request body the client sent."""
    sent = []

    def wrapper(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return handler(req)

    client = httpx.Client(base_url=settings.base_url, transport=httpx.MockTransport(wrapper))
    return TokenFactory(settings, client), sent


def _ok(content="ready", *, usage=None, finish="stop", message_extra=None):
    message = {"content": content}
    if message_extra:
        message.update(message_extra)
    return httpx.Response(200, json={"model": "m", "choices": [{"message": message,
                                                               "finish_reason": finish}],
                                     "usage": usage or {}})


def test_chat_routes_tier_to_model_and_counts_usage():
    seen = {}

    def h(req):
        import json
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ready"}}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 1,
                                                   "total_tokens": 8}})

    tf = _tf(h)
    tf.chat([{"role": "user", "content": "hi"}], tier=Tier.FAST)
    assert seen["model"] == "nano"
    assert tf.usage.calls == 1 and tf.usage.by_model == {"nano": 8}


def test_unconfigured_tier_refuses_instead_of_guessing_a_model():
    tf = _tf(lambda r: httpx.Response(500))
    with pytest.raises(RuntimeError, match="PM_MODEL_DEEP"):
        tf.chat([], tier=Tier.DEEP)


def test_missing_key_refuses():
    with pytest.raises(RuntimeError, match="NEBIUS_API_KEY"):
        TokenFactory(Settings("", S.base_url, "", "", "", ""))


# ---- thinking is stated per tier, in the body --------------------------------------------------
@pytest.mark.parametrize("tier,expected", [(Tier.FAST, False), (Tier.REASON, False), (Tier.DEEP, True)])
def test_each_tier_sends_its_thinking_default(tier, expected):
    """The flag must be in the BODY — a "/no_think" system message is ignored by the provider, so
    a tier that stays silent gets a reasoning pass it did not ask for."""
    tf, sent = _reply(lambda r: _ok())
    tf.chat([{"role": "user", "content": "hi"}], tier=tier)
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": expected}


@pytest.mark.parametrize("tier,expected", [(Tier.FAST, 256), (Tier.REASON, 2048), (Tier.DEEP, 8192)])
def test_each_tier_sends_its_max_tokens_default(tier, expected):
    tf, sent = _reply(lambda r: _ok())
    tf.chat([{"role": "user", "content": "hi"}], tier=tier)
    assert sent[0]["max_tokens"] == expected == TIER_DEFAULTS[tier].max_tokens


def test_thinking_and_max_tokens_are_overridable_per_call():
    tf, sent = _reply(lambda r: _ok())
    tf.chat([{"role": "user", "content": "hi"}], tier=Tier.REASON, max_tokens=16,
            enable_thinking=True)
    assert sent[0]["max_tokens"] == 16
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": True}


# ---- empty content is a named cause, never an AttributeError -----------------------------------
def test_content_none_raises_a_clear_error_naming_the_cause():
    """The measured failure: reasoning is billed against max_tokens, so a small budget is spent
    thinking and `content` arrives null. The message must say so, not crash on `.strip()`."""
    tf, _ = _reply(lambda r: _ok(None, usage={"completion_tokens": 16},
                                 finish="length",
                                 message_extra={"reasoning_content": "x" * 240}))
    with pytest.raises(EmptyContent) as exc:
        tf.chat([{"role": "user", "content": "hi"}], tier=Tier.FAST, max_tokens=16)
    message = str(exc.value)
    assert "reasoning pass spent the 16-token budget" in message
    assert "enable_thinking=False" in message
    assert "finish_reason='length'" in message
    assert "reasoning=240 chars" in message


def test_no_choices_at_all_is_also_an_empty_content_error():
    tf, _ = _reply(lambda r: httpx.Response(200, json={"choices": []}))
    with pytest.raises(EmptyContent, match="no choices"):
        tf.chat([{"role": "user", "content": "hi"}], tier=Tier.FAST)


# ---- content is normalised; reasoning is dropped ------------------------------------------------
def test_content_is_stripped_of_the_providers_padding():
    tf, _ = _reply(lambda r: _ok("\n  ready \n"))
    out = tf.chat([{"role": "user", "content": "hi"}], tier=Tier.FAST)
    assert out["choices"][0]["message"]["content"] == "ready"


def test_reasoning_content_never_comes_back_in_the_response():
    """The text must not survive in the dict the caller holds, or a caller that echoes the message
    back (the planner's repair retry does exactly that) would send it to the provider."""
    tf, _ = _reply(lambda r: _ok("ready", message_extra={"reasoning_content": "private scratch"}))
    out = tf.chat([{"role": "user", "content": "hi"}], tier=Tier.DEEP)
    message = out["choices"][0]["message"]
    assert "reasoning_content" not in message
    assert "private scratch" not in json.dumps(out)


def test_reasoning_usage_is_recorded_separately_and_as_length_only():
    tf, _ = _reply(lambda r: _ok("ready", usage={"prompt_tokens": 10, "completion_tokens": 40,
                                                 "total_tokens": 50,
                                                 "completion_tokens_details":
                                                     {"reasoning_tokens": 31}},
                                 message_extra={"reasoning_content": "y" * 120}))
    tf.chat([{"role": "user", "content": "hi"}], tier=Tier.DEEP)
    assert tf.usage.reasoning_tokens == 31
    assert tf.usage.reasoning_chars == 120
    assert tf.usage.completion_tokens == 40 and tf.usage.prompt_tokens == 10
    assert tf.usage.by_model == {"ultra": 50}


def test_a_response_with_no_usage_is_not_an_error():
    tf, _ = _reply(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    tf.chat([{"role": "user", "content": "hi"}], tier=Tier.FAST)
    assert tf.usage.calls == 1 and tf.usage.completion_tokens == 0
