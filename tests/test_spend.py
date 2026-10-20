"""The card-protection guard. Every test drives the REAL TokenFactory.chat with a counting fake
transport, so 'refused' is measured as 'no request was sent', not as a return value."""
import httpx
import pytest

from privatemesh.config import Settings
from privatemesh.llm import Tier, TokenFactory
from privatemesh.spend import Caps, SpendCapReached, SpendLedger

NANO, SUPER = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "nvidia/nemotron-3-super-120b-a12b"
S = Settings(api_key="k", base_url="https://tf.test/v1/", model_fast=NANO, model_reason=SUPER,
             model_deep="some/unpriced-model", tavily_key="")


def _tf(ledger, usage=None, status=200):
    sent = []

    def h(req):
        sent.append(req)
        return httpx.Response(status, json={"choices": [{"message": {"content": "ok"}}],
                                            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 2,
                                                               "total_tokens": 12}})
    client = httpx.Client(base_url=S.base_url, transport=httpx.MockTransport(h))
    return TokenFactory(S, client, spend=ledger), sent


def _ledger(tmp_path, total=5.0, daily=1.0):
    return SpendLedger(str(tmp_path / "spend.db"), Caps(total, daily))


def test_a_call_over_the_cap_is_refused_before_anything_is_sent(tmp_path):
    tf, sent = _tf(_ledger(tmp_path, total=0.0001))       # far below one REASON call's worst case
    with pytest.raises(SpendCapReached):
        tf.chat([{"role": "user", "content": "hi"}], tier=Tier.REASON)
    assert sent == []


def test_the_daily_cap_is_enforced_separately(tmp_path):
    tf, sent = _tf(_ledger(tmp_path, total=100.0, daily=0.0001))
    with pytest.raises(SpendCapReached, match="daily"):
        tf.chat([{"role": "user", "content": "hi"}], tier=Tier.REASON)
    assert sent == []


def test_an_unpriced_model_is_refused_not_treated_as_free(tmp_path):
    tf, sent = _tf(_ledger(tmp_path))
    with pytest.raises(SpendCapReached, match="no known price"):
        tf.chat([{"role": "user", "content": "hi"}], tier=Tier.DEEP)
    assert sent == []


def test_settled_cost_is_the_reported_usage(tmp_path):
    led = _ledger(tmp_path)
    tf, _ = _tf(led, usage={"prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000})
    tf.chat([{"role": "user", "content": "hi"}], tier=Tier.FAST)
    assert led.totals()["total_usd"] == pytest.approx(0.06)          # Nano input $0.06/M


def test_a_failed_call_keeps_its_worst_case_charge(tmp_path):
    led = _ledger(tmp_path)
    tf, sent = _tf(led, status=500)
    with pytest.raises(httpx.HTTPStatusError):
        tf.chat([{"role": "user", "content": "hi"}], tier=Tier.REASON)
    assert len(sent) == 1 and led.totals()["total_usd"] > 0.0018      # 2048 * $0.90/M ≈ $0.0018


def test_the_ledger_is_shared_so_a_second_process_cannot_reset_it(tmp_path):
    a, b = _ledger(tmp_path, total=0.004), _ledger(tmp_path, total=0.004)
    tfa, _ = _tf(a, usage={"prompt_tokens": 0, "completion_tokens": 3000, "total_tokens": 3000})
    tfa.chat([{"role": "user", "content": "hi"}], tier=Tier.REASON)   # settles at $0.0027
    tfb, sent_b = _tf(b)
    with pytest.raises(SpendCapReached):                              # 0.0027 + ~0.0018 > 0.004
        tfb.chat([{"role": "user", "content": "hi"}], tier=Tier.REASON)
    assert sent_b == []


def test_a_real_network_client_always_gets_a_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_SPEND_LEDGER", str(tmp_path / "default.db"))
    tf = TokenFactory(S)                                              # no injected client
    assert tf.spend is not None
