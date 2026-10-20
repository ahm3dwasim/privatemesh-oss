"""tavily_evidence: the SearchPort, and the two judgements it has to make out loud.

A search result is not evidence until it says *which passage* supports the claim and *whose* site
it came from. The excerpt is capped and centred on the query; the official flag is decided by the
domain, never by Tavily's ranking — an aggregator that ranks first is still an aggregator.
"""
from __future__ import annotations

import inspect
import json
import logging

import httpx
import pytest

from privatemesh import tavily_evidence as tv
from privatemesh.ports import SearchPort

LONG_RAW = (
    "Granger & Co King's Cross — a neighbourhood café. " + ("Padding. " * 60) +
    "Opening hours: Monday to Friday 07:00-11:30, Saturday and Sunday 08:00-16:00. "
    "The kitchen closes at 15:00 on weekdays. " + ("More padding. " * 60))


class FakeTavily:
    def __init__(self, results, *, status=200):
        self.results = results
        self.status = status
        self.requests: list[dict] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(req.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": {"error": "Unauthorized"}})
        return httpx.Response(200, json={"results": self.results})


def result(url, title="Granger & Co", content="", raw=None) -> dict:
    out = {"url": url, "title": title, "content": content, "score": 0.9}
    if raw is not None:
        out["raw_content"] = raw
    return out


def searcher(fake: FakeTavily, **kw) -> tv.TavilySearch:
    return tv.TavilySearch("tvly-test-key", http=fake.client, **kw)


# ------------------------------------------------------------------ the shape of a result ------
def test_search_returns_evidence_with_an_excerpt_and_a_retrieval_time():
    fake = FakeTavily([result("https://www.grangerandco.com/kings-cross", raw=LONG_RAW)])
    found = searcher(fake, clock=lambda: "2026-09-18T09:41:00Z").search(
        "Granger & Co opening hours kitchen closes weekdays", subject="Granger & Co")
    assert len(found) == 1
    evidence = found[0]
    assert evidence.url == "https://www.grangerandco.com/kings-cross"
    assert evidence.title == "Granger & Co"
    assert "closes at 15:00" in evidence.excerpt
    assert evidence.retrieved_at == "2026-09-18T09:41:00Z"
    assert evidence.official is True


def test_the_excerpt_is_capped_at_400_characters():
    fake = FakeTavily([result("https://www.grangerandco.com", raw=LONG_RAW)])
    evidence = searcher(fake).search("Granger & Co opening hours kitchen closes weekdays")[0]
    assert len(evidence.excerpt) <= 400
    assert evidence.excerpt.startswith("…") and evidence.excerpt.endswith("…"), \
        "a trimmed excerpt must admit that it was trimmed"


def test_the_excerpt_falls_back_to_the_snippet_when_there_is_no_raw_content():
    fake = FakeTavily([result("https://www.grangerandco.com", content="Closed on Mondays.")])
    evidence = searcher(fake).search("Granger & Co opening hours")[0]
    assert evidence.excerpt == "Closed on Mondays."


def test_an_excerpt_never_exceeds_the_cap_even_with_no_keyword_match():
    fake = FakeTavily([result("https://example.com", raw="z" * 2000)])
    evidence = searcher(fake).search("nothing in common here")[0]
    assert len(evidence.excerpt) <= 400


def test_no_results_means_no_evidence_rather_than_an_invented_venue():
    assert searcher(FakeTavily([])).search("anything at all") == []


# ------------------------------------------------------------------ the official flag ----------
def test_the_official_flag_follows_the_domain_not_the_ranking():
    fake = FakeTavily([
        result("https://www.yelp.co.uk/biz/granger-and-co-london", title="Granger & Co - Yelp",
               raw="Reviews of Granger & Co."),
        result("https://www.grangerandco.com/kings-cross", title="Granger & Co King's Cross",
               raw="Our opening hours are 07:00 to 15:00 on weekdays."),
        result("https://en.wikipedia.org/wiki/Granger_%26_Co", raw="Granger & Co is a chain."),
    ])
    found = searcher(fake).search("Granger & Co opening hours kitchen closes",
                                  subject="Granger & Co")
    assert [e.official for e in found] == [False, True, False]


def test_a_venue_domain_with_a_different_shape_still_matches():
    assert tv.is_official("https://grangerandco.com/", "Granger & Co") is True
    assert tv.is_official("https://www.granger-co.co.uk/", "Granger & Co") is True


def test_an_unrelated_blog_is_not_official():
    assert tv.is_official("https://londoncoffeeblog.com/granger", "Granger & Co") is False


def test_an_aggregator_is_never_official_even_with_the_name_in_the_domain():
    for url in ("https://www.tripadvisor.co.uk/Restaurant_Review-granger",
                "https://www.opentable.co.uk/r/granger-and-co",
                "https://www.timeout.com/london/restaurants/granger-and-co"):
        assert tv.is_official(url, "Granger & Co") is False, url


def test_the_subject_can_be_read_from_a_quoted_query():
    assert tv.subject_from_query('"Granger & Co" opening hours King\'s Cross') == "Granger & Co"
    assert tv.subject_from_query("Granger & Co opening hours") == "Granger & Co"
    assert tv.subject_from_query("cafés near King's Cross open at 15:00") != ""


# ------------------------------------------------------------------ the request ---------------
def test_the_request_asks_for_raw_content_and_the_domain_filter():
    fake = FakeTavily([result("https://www.grangerandco.com", raw="x")])
    searcher(fake).search("venue", max_results=3, include_domains=("grangerandco.com",))
    body = fake.requests[0]
    assert body["query"] == "venue"
    assert body["max_results"] == 3
    assert body["include_raw_content"] is True
    assert body["include_domains"] == ["grangerandco.com"]
    assert body["api_key"] == "tvly-test-key"


def test_an_empty_domain_filter_is_not_sent_as_an_empty_list():
    fake = FakeTavily([result("https://x.com", raw="x")])
    searcher(fake).search("venue")
    assert "include_domains" not in fake.requests[0]


def test_a_provider_error_is_reported_rather_than_read_as_no_results():
    with pytest.raises(tv.TavilyError) as err:
        searcher(FakeTavily([], status=401)).search("venue")
    assert "401" in str(err.value)


def test_the_connector_can_stand_in_for_the_search_port():
    """The port is a Protocol, so the engine binds to it by shape. A renamed keyword here is a
    TypeError at runtime in the middle of a live proposal, which is too late to find out."""
    impl = searcher(FakeTavily([result("https://www.grangerandco.com", raw=LONG_RAW)])).search
    got = inspect.signature(impl)
    for name, param in inspect.signature(SearchPort.search).parameters.items():
        if name == "self":
            continue
        assert name in got.parameters, f"the port passes {name!r}; the connector does not accept it"
        assert got.parameters[name].kind == param.kind, name
    # And a call in the port's own vocabulary actually works.
    found = impl("Granger & Co opening hours", max_results=3, include_domains=())
    assert found and found[0].url == "https://www.grangerandco.com"


def test_no_api_key_reaches_the_logs(caplog):
    fake = FakeTavily([result("https://www.grangerandco.com", raw="x")])
    with caplog.at_level(logging.DEBUG):
        searcher(fake).search("venue")
    combined = caplog.text + "\n".join(r.getMessage() for r in caplog.records)
    assert "tvly-test-key" not in combined


def test_a_result_without_content_is_skipped():
    fake = FakeTavily([{"url": "https://x.com", "title": "no body"}, result("https://y.com",
                                                                           raw="real")])
    found = searcher(fake).search("venue")
    assert [e.url for e in found] == ["https://y.com"]


def test_the_existing_web_research_skill_still_has_its_wrapper():
    """The v1 skill is untouched by this module; this pins that it still imports and builds."""
    import sqlite3

    from privatemesh.skills_builtin import build_registry

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    registry = build_registry(conn=conn, tavily_key="tvly-test")
    assert "web_research" in registry.names()
